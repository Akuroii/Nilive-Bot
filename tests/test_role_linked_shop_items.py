"""End-to-end regression suite for Optional Role-Linked Shop Items (`type='custom'`).

Covers:
  1. Dashboard API CRUD, 64-bit Discord snowflake preservation, and kind/name
     conflict detection (`POST /api/shop/item`, `GET /api/shop/item/<id>`,
     `PUT /api/shop/item/<id>`), while keeping `Color` required-role + auto-equip
     semantics intact.
  2. Permanent Role-Linked Item purchase -> ownership without auto-assigning
     Discord role -> `/inventory` `TAB_ITEMS` detail embed + `Equip`/`Unequip`
     toggle -> `TAB_EQUIPPED` + dashboard `/inventory/<user_id>` equipped flag.
  3. Single-slot swap interoperability with `Color` roles, plus preservation of
     independently held Discord roles (`role_added == 0` and non-slot system
     entitlements).
  4. Timed Role-Linked Item lifecycle:
     - Case 1: expires without ever being equipped (zero Discord role changes)
     - Case 2: manually equipped, then unequipped before expiry, then expires
     - Case 3: manually equipped and still equipped at expiry (removes Discord
       role unless independently held or still entitled by another active grant)
     - Unequipped Role-Linked Items never passively block another grant's expiry,
       whereas an actively equipped live Role-Linked Item retains the role.
  5. Permanent stack separation (`ItemTypeConflict` on plain vs role-linked or
     mismatched `role_id`; clean reuse of emptied `quantity == 0` rows).
  6. Late-delivery rollback (`compensate_late_delivery`) for permanent and timed
     Role-Linked Items, both unequipped and manually equipped in the race window.
"""
import os
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("OWNER_ID", "999999999")

from phase1_support import (  # noqa: E402
    GUILD,
    USER,
    execute,
    reset_database,
    rows,
    seed_member,
)
from test_f01_f03_role_reconciliation_and_purchase import (  # noqa: E402
    BLUE,
    FLAME,
    make_discord_env,
    make_interaction,
    worn_ids,
)
import cogs.shop as shop_cog  # noqa: E402
import cogs.wallet as wallet  # noqa: E402
import dashboard.app as dapp  # noqa: E402
from utils import equip_engine, inventory, timed_ownership  # noqa: E402

SNOWFLAKE_ROLE = 1309876543210987654  # > 2^53 - 1
ALT_ROLE = 1309876543210987999
CURRENCY = {
    "coins": {"name": "Coins", "emoji": "🪙"},
    "diamonds": {"name": "Diamonds", "emoji": "💎"},
}


def _add_role_to_env(guild, roles_dict, role_id: int, name: str, position: int = 4):
    role = SimpleNamespace(id=role_id, name=name, position=position, mention=f"<@&{role_id}>")
    roles_dict[role_id] = role
    guild.roles.append(role)
    return role


def _seed_shop_item(
    name: str,
    *,
    kind: str = "custom",
    price: int = 250,
    role_id: int | None = None,
    duration_hours: int | None = None,
) -> int:
    return execute(
        "INSERT INTO shop_items "
        "(guild_id, name, type, price, role_id, duration_hours, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (GUILD, name, kind, price, role_id, duration_hours),
    )


class RoleLinkedShopItemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        import database
        self._prev_db_env = os.environ.get("DATABASE_PATH")
        self._prev_prod_db_env = os.environ.get("PRODUCTION_DATABASE_PATH")
        self._prev_nero_env = os.environ.get("NERO_ENVIRONMENT")
        os.environ["DATABASE_PATH"] = database.DB_PATH
        os.environ["PRODUCTION_DATABASE_PATH"] = database.DB_PATH
        os.environ["NERO_ENVIRONMENT"] = "production"
        seed_member(user=USER, guild=GUILD, balance=100_000)
        self.bot, self.guild, self.member, self.roles = make_discord_env()
        self.guild.name = "Test Guild"
        _add_role_to_env(self.guild, self.roles, SNOWFLAKE_ROLE, "Knight Crest")
        _add_role_to_env(self.guild, self.roles, ALT_ROLE, "Shadow Crest")

        # Dashboard session setup for API + /inventory/<user_id> verification.
        execute(
            "INSERT OR REPLACE INTO dashboard_users "
            "(guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'admin', 1)",
            (GUILD, 555001),
        )
        self.client = dapp.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = {"id": "555001", "username": "ShopAdmin", "avatar": None}
            sess["guild_id"] = GUILD
            sess["expires_at"] = time.time() + 7200
            sess["csrf_token"] = "test-csrf"
        self.headers = {"X-CSRF-Token": "test-csrf"}

    async def asyncTearDown(self):
        for k, v in (
            ("DATABASE_PATH", self._prev_db_env),
            ("PRODUCTION_DATABASE_PATH", self._prev_prod_db_env),
            ("NERO_ENVIRONMENT", self._prev_nero_env),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _panel_ix(self):
        ix = make_interaction(self.bot, self.guild, self.member)
        ix.message = SimpleNamespace()
        ix.original_response = AsyncMock(return_value=ix.message)
        ix.response.edit_message = AsyncMock()
        return ix

    async def _buy(self, item_id: int):
        ix = make_interaction(self.bot, self.guild, self.member)
        with patch.object(shop_cog, "get_currency_config", AsyncMock(return_value=CURRENCY)):
            await shop_cog.process_purchase(ix, item_id)
        return ix.response.send_message.await_args.kwargs

    # ── 1. Dashboard API CRUD, 64-bit Snowflakes & Name/Kind Identity ──────

    async def test_01_dashboard_crud_64bit_snowflake_and_kind_conflicts(self):
        # Plain collectible Item (role_id omitted / blank)
        r_plain = self.client.post(
            "/api/shop/item",
            json={"name": "Relic Coin", "type": "custom", "price": 100, "role_id": None},
            headers=self.headers,
        )
        self.assertEqual(r_plain.status_code, 200)
        self.assertTrue(r_plain.get_json()["success"])

        # Role-Linked Item with 64-bit Discord snowflake string
        r_linked = self.client.post(
            "/api/shop/item",
            json={
                "name": "Knight Crest",
                "type": "custom",
                "price": 300,
                "role_id": str(SNOWFLAKE_ROLE),
                "duration_hours": 24,
            },
            headers=self.headers,
        )
        self.assertEqual(r_linked.status_code, 200, r_linked.get_data(as_text=True))
        linked_id, linked_type, linked_rid, linked_dur = rows(
            "SELECT id, type, role_id, duration_hours FROM shop_items WHERE name='Knight Crest'"
        )[0]
        self.assertEqual(
            (linked_type, linked_rid, linked_dur),
            ("custom", SNOWFLAKE_ROLE, 24),
        )

        # GET /api/shop/item/<id> returns exact 64-bit string for prefill
        got = self.client.get(f"/api/shop/item/{linked_id}").get_json()
        self.assertTrue(got["success"])
        self.assertEqual(got["item"]["role_id"], str(SNOWFLAKE_ROLE))

        # Same-name plain item vs Role-Linked Item must be rejected
        clash = self.client.post(
            "/api/shop/item",
            json={"name": "Knight Crest", "type": "custom", "price": 50, "role_id": None},
            headers=self.headers,
        )
        self.assertEqual(clash.status_code, 400)
        self.assertIn("role item", clash.get_json()["error"])

        # Converting an existing plain Item into a Role-Linked Item when a member
        # already owns a plain collectible of that name must be rejected on PUT.
        plain_id = rows("SELECT id FROM shop_items WHERE name='Relic Coin'")[0][0]
        await inventory.give_item(GUILD, USER, "Relic Coin", 1, item_type="custom")
        bad_put = self.client.put(
            f"/api/shop/item/{plain_id}",
            json={"role_id": str(SNOWFLAKE_ROLE)},
            headers=self.headers,
        )
        self.assertEqual(bad_put.status_code, 400)

        # Changing role_id on a permanent Role-Linked Item when a member already
        # owns it with the previous role_id must also be rejected on PUT.
        perm_linked = self.client.post(
            "/api/shop/item",
            json={"name": "Owned Badge", "type": "custom", "price": 100, "role_id": str(SNOWFLAKE_ROLE)},
            headers=self.headers,
        )
        self.assertEqual(perm_linked.status_code, 200)
        perm_linked_id = rows("SELECT id FROM shop_items WHERE name='Owned Badge'")[0][0]
        await self._buy(perm_linked_id)
        mismatch_put = self.client.put(
            f"/api/shop/item/{perm_linked_id}",
            json={"role_id": str(ALT_ROLE)},
            headers=self.headers,
        )
        self.assertEqual(mismatch_put.status_code, 400)

        # Clearing role_id on Knight Crest when no conflicting definition/owner exists succeeds
        clear_put = self.client.put(
            f"/api/shop/item/{linked_id}",
            json={"role_id": ""},
            headers=self.headers,
        )
        self.assertEqual(clear_put.status_code, 200, clear_put.get_data(as_text=True))
        self.assertIsNone(
            rows("SELECT role_id FROM shop_items WHERE id=?", (linked_id,))[0][0]
        )

        # Color still strictly requires role_id
        bad_color = self.client.post(
            "/api/shop/item",
            json={"name": "NoRoleColor", "type": "color", "price": 200, "role_id": None},
            headers=self.headers,
        )
        self.assertEqual(bad_color.status_code, 400)

    # ── 2. Permanent Role-Linked Item Purchase, /inventory UI & Toggle ─────

    async def test_02_permanent_role_linked_item_purchase_inventory_equip_unequip(self):
        linked_pid = _seed_shop_item("Knight Crest", price=300, role_id=SNOWFLAKE_ROLE)
        plain_pid = _seed_shop_item("Souvenir Pin", price=100, role_id=None)

        # Buy both items: NEITHER assigns a Discord role at purchase time!
        msg_linked = await self._buy(linked_pid)
        await self._buy(plain_pid)
        self.assertEqual(set(worn_ids(self.member)), set())
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
        field_names = [f.name for f in msg_linked["embed"].fields]
        self.assertIn("🎭 Role-linked item", field_names)

        # Both appear in TAB_ITEMS (never TAB_COLORS)
        all_entries = await inventory.get_inventory_entries(GUILD, USER)
        by_tab = wallet.split_by_tab(all_entries)
        self.assertEqual(
            {e["item_name"] for e in by_tab[wallet.TAB_ITEMS]},
            {"Knight Crest", "Souvenir Pin"},
        )
        self.assertEqual(by_tab[wallet.TAB_COLORS], [])

        # Plain item panel shows Collectible + NO Equip button
        ix_plain = self._panel_ix()
        await wallet.open_item_panel(ix_plain, GUILD, USER, wallet.TAB_ITEMS, "Souvenir Pin")
        plain_kwargs = ix_plain.response.edit_message.await_args.kwargs
        plain_labels = [getattr(c, "label", None) for c in plain_kwargs["view"].children]
        self.assertNotIn("Equip", plain_labels)
        self.assertNotIn("Unequip", plain_labels)
        self.assertIn("Collectible", [f.value for f in plain_kwargs["embed"].fields])

        # Role-Linked Item panel shows Not equipped + Role mention + Equip button
        ix_open = self._panel_ix()
        await wallet.open_item_panel(ix_open, GUILD, USER, wallet.TAB_ITEMS, "Knight Crest")
        open_kwargs = ix_open.response.edit_message.await_args.kwargs
        equip_btn = next(
            c for c in open_kwargs["view"].children if getattr(c, "label", "") == "Equip"
        )
        embed_map = {f.name: f.value for f in open_kwargs["embed"].fields}
        self.assertEqual(embed_map["Status"], "Not equipped")
        self.assertEqual(embed_map["Role"], f"<@&{SNOWFLAKE_ROLE}>")

        # Click Equip -> assigns Discord role, updates equipped_roles, renders Unequip
        ix_eq = self._panel_ix()
        await equip_btn.callback(ix_eq)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        slot = await equip_engine.get_equipped(GUILD, USER)
        self.assertIsNotNone(slot)
        self.assertEqual(slot["item_name"], "Knight Crest")
        self.assertEqual(slot["role_id"], SNOWFLAKE_ROLE)
        self.assertEqual(slot["role_added"], 1)

        # TAB_EQUIPPED includes Knight Crest while equipped
        ix_tab_eq = self._panel_ix()
        with patch.object(wallet, "get_currency_config", AsyncMock(return_value=CURRENCY)):
            await wallet.open_inventory_panel(
                ix_tab_eq, GUILD, USER, wallet.TAB_EQUIPPED, first=True
            )
        tab_eq_embed = ix_tab_eq.response.send_message.await_args.kwargs["embed"]
        self.assertIn("Knight Crest", str(tab_eq_embed.to_dict()))

        # Dashboard read-only member inventory marks Knight Crest as Equipped
        with patch("utils.discord_user_cache.resolve_users", AsyncMock(return_value={})):
            dash_inv = self.client.get(f"/inventory/{USER}")
        self.assertEqual(dash_inv.status_code, 200)
        self.assertIn("Equipped", dash_inv.get_data(as_text=True))

        # Click Unequip -> removes Discord role, clears equipped_roles, keeps Item owned
        after_eq_view = ix_eq.response.edit_message.await_args.kwargs["view"]
        unequip_btn = next(
            c for c in after_eq_view.children if getattr(c, "label", "") == "Unequip"
        )
        ix_un = self._panel_ix()
        await unequip_btn.callback(ix_un)
        self.assertEqual(set(worn_ids(self.member)), set())
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
        still_owned = await inventory.get_inventory_entries(GUILD, USER)
        self.assertTrue(any(e["item_name"] == "Knight Crest" and e["quantity"] == 1 for e in still_owned))

    # ── 3. Single-Slot Swap with Color & Independent Role Preservation ─────

    async def test_03_single_slot_swap_with_color_and_independent_role_safety(self):
        color_pid = _seed_shop_item("Flame Color", kind="color", price=200, role_id=FLAME)
        crest_pid = _seed_shop_item("Knight Crest", kind="custom", price=200, role_id=SNOWFLAKE_ROLE)

        # Buying Color auto-equips FLAME immediately
        await self._buy(color_pid)
        self.assertEqual(set(worn_ids(self.member)), {FLAME})

        # Buying Role-Linked Item leaves FLAME equipped
        await self._buy(crest_pid)
        self.assertEqual(set(worn_ids(self.member)), {FLAME})

        # Manually equipping Knight Crest swaps out FLAME -> SNOWFLAKE_ROLE; both remain owned
        res_swap = await equip_engine.equip_role(self.bot, GUILD, USER, "Knight Crest")
        self.assertTrue(res_swap["success"], res_swap)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})

        # Equipping Flame Color back swaps out SNOWFLAKE_ROLE -> FLAME; Knight Crest stays owned
        res_back = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame Color")
        self.assertTrue(res_back["success"], res_back)
        self.assertEqual(set(worn_ids(self.member)), {FLAME})
        entries = await inventory.get_inventory_entries(GUILD, USER)
        self.assertEqual({e["item_name"] for e in entries}, {"Flame Color", "Knight Crest"})

        # Independent role safety: member ALREADY holds ALT_ROLE externally before equipping
        alt_pid = _seed_shop_item("Shadow Crest", kind="custom", price=150, role_id=ALT_ROLE)
        await self._buy(alt_pid)
        self.member.roles.append(self.roles[ALT_ROLE])
        eq_alt = await equip_engine.equip_role(self.bot, GUILD, USER, "Shadow Crest")
        self.assertTrue(eq_alt["success"], eq_alt)
        self.assertEqual(set(worn_ids(self.member)), {ALT_ROLE})
        slot = await equip_engine.get_equipped(GUILD, USER)
        self.assertEqual(slot["role_added"], 0)

        # Unequipping Shadow Crest MUST preserve the pre-existing ALT_ROLE!
        un_alt = await equip_engine.unequip_role(self.bot, GUILD, USER, "Shadow Crest")
        self.assertTrue(un_alt["success"], un_alt)
        self.assertEqual(set(worn_ids(self.member)), {ALT_ROLE})

    # ── 4. Timed Role-Linked Item Lifecycle & Expiry Cases ─────────────────

    async def test_04_timed_role_linked_item_all_expiry_and_entitlement_cases(self):
        # Case 1: Timed Role-Linked Item expires WITHOUT ever being equipped
        timed_pid = _seed_shop_item(
            "24h Knight Pass", kind="custom", price=150, role_id=SNOWFLAKE_ROLE, duration_hours=24
        )
        buy_msg = await self._buy(timed_pid)
        self.assertEqual(set(worn_ids(self.member)), set())
        fnames = [f.name for f in buy_msg["embed"].fields]
        self.assertIn("🎭 Role-linked item", fnames)
        self.assertIn("⏳ Temporary ownership", fnames)

        # Even if member independently holds SNOWFLAKE_ROLE, expiring an unequipped
        # manual-equip holding NEVER removes the Discord role!
        self.member.roles.append(self.roles[SNOWFLAKE_ROLE])
        past = (timed_ownership.utcnow() - timedelta(hours=1)).isoformat()
        execute("UPDATE timed_holdings SET expires_at=?", (past,))
        stats1 = await timed_ownership.cleanup_expired_holdings(self.bot)
        self.assertEqual(stats1["removed"], 1)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        self.member.roles.remove(self.roles[SNOWFLAKE_ROLE])

        # Case 2: Timed Role-Linked Item equipped -> manually unequipped -> expires
        await self._buy(timed_pid)
        eq2 = await equip_engine.equip_role(self.bot, GUILD, USER, "24h Knight Pass")
        self.assertTrue(eq2["success"], eq2)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        un2 = await equip_engine.unequip_role(self.bot, GUILD, USER, "24h Knight Pass")
        self.assertTrue(un2["success"], un2)
        self.assertEqual(set(worn_ids(self.member)), set())
        # Still owned until expiry!
        self.assertEqual(len(await inventory.get_inventory_entries(GUILD, USER)), 1)
        execute("UPDATE timed_holdings SET expires_at=?", (past,))
        stats2 = await timed_ownership.cleanup_expired_holdings(self.bot)
        self.assertEqual(stats2["removed"], 1)
        self.assertEqual(await inventory.get_inventory_entries(GUILD, USER), [])

        # Case 3a: Timed Role-Linked Item STILL equipped at expiry (sole grant) -> removes Discord role
        await self._buy(timed_pid)
        eq3 = await equip_engine.equip_role(self.bot, GUILD, USER, "24h Knight Pass")
        self.assertTrue(eq3["success"], eq3)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        execute("UPDATE timed_holdings SET expires_at=?", (past,))
        stats3 = await timed_ownership.cleanup_expired_holdings(self.bot)
        self.assertEqual(stats3["removed"], 1)
        self.assertEqual(set(worn_ids(self.member)), set())
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))

        # Case 3b: Timed Role-Linked Item equipped OVER a pre-existing independent role (role_added=0)
        self.member.roles.append(self.roles[SNOWFLAKE_ROLE])
        await self._buy(timed_pid)
        eq3b = await equip_engine.equip_role(self.bot, GUILD, USER, "24h Knight Pass")
        self.assertTrue(eq3b["success"], eq3b)
        execute("UPDATE timed_holdings SET expires_at=?", (past,))
        stats3b = await timed_ownership.cleanup_expired_holdings(self.bot)
        self.assertEqual(stats3b["removed"], 1)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
        self.member.roles.remove(self.roles[SNOWFLAKE_ROLE])

        # Case 4: Unequipped Role-Linked Item does NOT block expiry of a timed Color
        # sharing the same role_id, whereas an EQUIPPED Role-Linked Item DOES retain it!
        perm_crest = _seed_shop_item("Perm Crest", kind="custom", price=100, role_id=BLUE)
        timed_blue_color = _seed_shop_item(
            "Timed Blue", kind="color", price=100, role_id=BLUE, duration_hours=12
        )
        await self._buy(perm_crest)
        await self._buy(timed_blue_color)  # auto-equips BLUE
        self.assertEqual(set(worn_ids(self.member)), {BLUE})
        # Perm Crest is UNEQUIPPED -> expiring Timed Blue removes BLUE from Discord!
        execute("UPDATE timed_holdings SET expires_at=?", (past,))
        await timed_ownership.cleanup_expired_holdings(self.bot)
        self.assertEqual(set(worn_ids(self.member)), set())
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))

        # Now buy Timed Blue again, then manually equip Perm Crest -> expiring Timed Blue
        # MUST retain BLUE on Discord because Perm Crest is actively equipped!
        await self._buy(timed_blue_color)
        await equip_engine.equip_role(self.bot, GUILD, USER, "Perm Crest")
        execute("UPDATE timed_holdings SET expires_at=?", (past,))
        await timed_ownership.cleanup_expired_holdings(self.bot)
        self.assertEqual(set(worn_ids(self.member)), {BLUE})
        slot_after = await equip_engine.get_equipped(GUILD, USER)
        self.assertEqual(slot_after["item_name"], "Perm Crest")

    # ── 5. Stack Isolation & Late-Delivery Rollback ────────────────────────

    async def test_05_stack_conflict_and_late_delivery_rollback(self):
        # Plain collectible vs Role-Linked Item never merge
        await inventory.give_item(GUILD, USER, "SharedName", 1, item_type="shop_custom")
        with self.assertRaises(inventory.ItemTypeConflict):
            await inventory.give_item(
                GUILD,
                USER,
                "SharedName",
                1,
                item_type="shop_custom",
                metadata={"role_id": SNOWFLAKE_ROLE},
            )

        # Emptying the row to quantity=0 allows reuse and wipes stale metadata
        await inventory.remove_item(GUILD, USER, "SharedName", 1)
        await inventory.give_item(
            GUILD,
            USER,
            "SharedName",
            1,
            item_type="shop_custom",
            metadata={"role_id": SNOWFLAKE_ROLE},
        )
        with self.assertRaises(inventory.ItemTypeConflict):
            await inventory.give_item(
                GUILD,
                USER,
                "SharedName",
                1,
                item_type="shop_custom",
                metadata={"role_id": ALT_ROLE},
            )
        # Same role_id stacks cleanly
        qty = await inventory.give_item(
            GUILD,
            USER,
            "SharedName",
            1,
            item_type="shop_custom",
            metadata={"role_id": SNOWFLAKE_ROLE},
        )
        self.assertEqual(qty, 2)
        await inventory.remove_item(GUILD, USER, "SharedName", 2)
        # Reusing emptied role-linked row for plain item clears stale role_id metadata!
        await inventory.give_item(GUILD, USER, "SharedName", 1, item_type="shop_custom")
        reused = [
            e for e in await inventory.get_inventory_entries(GUILD, USER)
            if e["item_name"] == "SharedName"
        ][0]
        self.assertFalse(timed_ownership.is_role_equippable_entry(reused))

        # Late-delivery rollback when operator refunded BEFORE delivery completed,
        # and member manually equipped a Permanent Role-Linked Item in the race window:
        perm_pid = _seed_shop_item("Race Crest", kind="custom", price=200, role_id=SNOWFLAKE_ROLE)
        timed_race_pid = _seed_shop_item(
            "Timed Race Crest", kind="custom", price=200, role_id=ALT_ROLE, duration_hours=24
        )
        real_deliver = shop_cog._deliver_purchase_effects

        for target_pid, target_name in ((perm_pid, "Race Crest"), (timed_race_pid, "Timed Race Crest")):
            async def _refund_then_deliver_and_equip(interaction, _name=target_name, **kw):
                op_id = kw["op_id"]
                execute(
                    "UPDATE shop_purchase_operations SET status='pending', "
                    "delivery_intent=NULL, provenance_tracked=0 WHERE op_id=?",
                    (op_id,),
                )
                await timed_ownership.claim_purchase_transition(
                    op_id,
                    from_statuses=(timed_ownership.PURCHASE_PENDING,),
                    to_status=timed_ownership.PURCHASE_UNCERTAIN,
                    detail="operator timeout",
                )
                await timed_ownership.resolve_purchase_operation(
                    op_id,
                    action="refund",
                    confirmed=True,
                    note="refunded before late delivery returned",
                )
                res = await real_deliver(interaction, **kw)
                await equip_engine.equip_role(self.bot, GUILD, USER, _name)
                return res

            with patch.object(
                shop_cog, "_deliver_purchase", side_effect=_refund_then_deliver_and_equip
            ):
                await self._buy(target_pid)

            # Rollback reversed ownership, cleared equipped_roles, and removed the Discord role!
            self.assertEqual(set(worn_ids(self.member)), set())
            self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
            self.assertFalse(
                any(
                    e["item_name"] == target_name
                    for e in await inventory.get_inventory_entries(GUILD, USER)
                )
            )

    # ── 6. Adversarial Fail-Closed Late-Delivery Rollback & Concurrency ───

    async def test_06_adversarial_late_delivery_rollback_fail_closed_and_cold_restart(self):
        import asyncio
        import json

        # 6a. Failed remove_roles -> incomplete rollback + retirement blocked + cold-restart retry
        perm_pid = _seed_shop_item("FailClosed Crest", kind="custom", price=200, role_id=SNOWFLAKE_ROLE)
        real_deliver = shop_cog._deliver_purchase_effects
        captured = {}

        orig_remove = self.member.remove_roles

        async def _fail_remove(*args, **kwargs):
            raise RuntimeError("Simulated Discord 500 on remove_roles")

        async def _deliver_equip_and_fail_remove(interaction, **kw):
            op_id = kw["op_id"]
            captured["op_id"] = op_id
            execute(
                "UPDATE shop_purchase_operations SET status='pending', "
                "delivery_intent=NULL, provenance_tracked=0 WHERE op_id=?",
                (op_id,),
            )
            await timed_ownership.claim_purchase_transition(
                op_id,
                from_statuses=(timed_ownership.PURCHASE_PENDING,),
                to_status=timed_ownership.PURCHASE_UNCERTAIN,
                detail="operator timeout",
            )
            await timed_ownership.resolve_purchase_operation(
                op_id,
                action="refund",
                confirmed=True,
                note="refunded before late delivery returned",
            )
            res = await real_deliver(interaction, **kw)
            await equip_engine.equip_role(self.bot, GUILD, USER, "FailClosed Crest")
            self.member.remove_roles = AsyncMock(side_effect=_fail_remove)
            return res

        with patch.object(shop_cog, "_deliver_purchase", side_effect=_deliver_equip_and_fail_remove):
            await self._buy(perm_pid)

        op_id = captured["op_id"]
        # Role is STILL worn on Discord; rollback MUST NOT be marked complete!
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        raw_rb = rows(
            "SELECT delivery_rollback FROM shop_purchase_operations WHERE op_id=?",
            (op_id,),
        )[0][0]
        self.assertTrue(raw_rb and raw_rb != "", f"Expected pending rollback JSON, got {raw_rb!r}")
        persisted_spec = json.loads(raw_rb)
        self.assertTrue(persisted_spec.get("role_added"))
        self.assertFalse(persisted_spec.get("role_reconciled"))

        # Retirement evidence MUST refuse (even with accept_unprovable=True)!
        verdict = await timed_ownership.rollback_retirement_evidence(op_id)
        self.assertFalse(verdict["safe"], verdict)
        self.assertEqual(verdict["code"], "discord_state_unobservable")
        retired_ok, _ = await timed_ownership.retire_pending_rollback(
            op_id, confirmed=True, note="operator attempt", accept_unprovable=True
        )
        self.assertFalse(retired_ok)

        # Uncached guild role (get_role -> None without fetch_roles confirmation) MUST fail closed!
        self.member.remove_roles = orig_remove
        orig_get_role = self.guild.get_role
        self.guild.get_role = lambda _rid: None
        timed_ownership._PENDING_DELIVERY_ROLLBACKS.clear()
        caller_stale_spec = dict(persisted_spec, role_added=False)
        res_uncached = await timed_ownership.compensate_late_delivery(
            op_id, caller_stale_spec, bot=self.bot
        )
        self.assertFalse(res_uncached["complete"], res_uncached)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})

        # Uncached get_role WITH working guild.fetch_roles + stale cached member.roles == []
        # while fetch_member returns the fresh member wearing SNOWFLAKE_ROLE:
        fresh_member = SimpleNamespace(
            id=USER,
            guild=self.guild,
            roles=[self.roles[SNOWFLAKE_ROLE]],
        )

        async def _fresh_remove(role, reason=None):
            if role in fresh_member.roles:
                fresh_member.roles.remove(role)
            if role in self.member.roles:
                self.member.roles.remove(role)

        fresh_member.remove_roles = AsyncMock(side_effect=_fresh_remove)
        self.member.roles.clear()  # Stale cache claims role is absent!
        self.guild.fetch_roles = AsyncMock(return_value=list(self.guild.roles))
        self.guild.fetch_member = AsyncMock(return_value=fresh_member)

        # Simulate cold restart: empty in-memory cache + caller spec has role_added=False
        timed_ownership._PENDING_DELIVERY_ROLLBACKS.clear()
        res_cold = await timed_ownership.compensate_late_delivery(
            op_id, dict(persisted_spec, role_added=False), bot=self.bot
        )
        self.assertTrue(res_cold["complete"], res_cold)
        self.assertEqual([r.id for r in fresh_member.roles], [])
        self.assertEqual(
            rows("SELECT delivery_rollback FROM shop_purchase_operations WHERE op_id=?", (op_id,))[0][0],
            "",
        )
        self.guild.get_role = orig_get_role
        delattr(self.guild, "fetch_roles")

        # 6b. Unknown/corrupt equipped_roles observation & mismatched slot.role_id fail closed
        for mode in ("corrupt_obs", "wrong_role"):
            bad_name = f"CorruptSlot_{mode}"
            bad_op = f"op-{mode}"
            execute(
                "INSERT INTO shop_purchase_operations "
                "(op_id, guild_id, user_id, item_id, item_name, currency, amount, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'coins', 100, 'refunded', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                (bad_op, GUILD, USER, perm_pid, bad_name),
            )
            await inventory.give_item(
                GUILD, USER, bad_name, 1, item_type="shop_custom",
                metadata={"role_id": SNOWFLAKE_ROLE},
            )
            inv_id = rows(
                "SELECT id FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name=?",
                (GUILD, USER, bad_name),
            )[0][0]
            from utils import level_ops
            import aiosqlite
            from database import DB_PATH
            async with aiosqlite.connect(DB_PATH) as db:
                await level_ops.record_purchase_grant(
                    db, operation_id=bad_op, guild_id=GUILD, user_id=USER,
                    item_name=bad_name, quantity=1,
                )
                await db.commit()
            slot_rid = SNOWFLAKE_ROLE if mode == "corrupt_obs" else ALT_ROLE
            bound_rid = ALT_ROLE  # Deliberately mismatched/corrupt for corrupt_obs!
            self.member.roles = [self.roles[slot_rid]]
            execute(
                "INSERT OR REPLACE INTO equipped_roles "
                "(guild_id, user_id, item_name, role_id, equipped_at, entry_key, role_added, role_added_role_id) "
                "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00', ?, 1, ?)",
                (GUILD, USER, bad_name, slot_rid, f"i:{inv_id}", bound_rid),
            )
            bad_spec = {
                "guild_id": GUILD,
                "user_id": USER,
                "item_id": perm_pid,
                "item_name": bad_name,
                "internal_type": "shop_custom",
                "role_id": SNOWFLAKE_ROLE,
                "entry_key": f"i:{inv_id}",
                "role_added": False,
                "inventory_rolled_back": False,
                "equip_reconciled": False,
                "role_reconciled": False,
            }
            await timed_ownership.record_pending_delivery_rollback(bad_op, bad_spec, bot=None)
            res_bad = await timed_ownership.compensate_late_delivery(bad_op, bad_spec, bot=self.bot)
            self.assertFalse(res_bad["complete"], res_bad)
            # Slot and worn role MUST remain untouched!
            self.assertIsNotNone(await equip_engine.get_equipped(GUILD, USER))
            self.assertEqual(set(worn_ids(self.member)), {slot_rid})
            bad_ev = await timed_ownership.rollback_retirement_evidence(bad_op)
            self.assertFalse(bad_ev["safe"], bad_ev)
            execute("DELETE FROM equipped_roles WHERE guild_id=? AND user_id=?", (GUILD, USER))
            self.member.roles.clear()

        # 6c. Concurrent equip_role() vs timed-holding rollback race under slot_lock
        for equip_first in (True, False):
            race_op = f"op-timed-race-{equip_first}"
            execute(
                "INSERT INTO shop_purchase_operations "
                "(op_id, guild_id, user_id, item_id, item_name, currency, amount, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'RaceTimedPass', 'coins', 100, 'refunded', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                (race_op, GUILD, USER, perm_pid),
            )
            grant = await timed_ownership.grant_timed_product(
                self.bot, GUILD, USER,
                item_name="RaceTimedPass",
                item_type="shop_custom",
                duration_hours=24,
                role_id=SNOWFLAKE_ROLE,
                assign_role_now=False,
                source="shop",
                product_id=perm_pid,
                metadata={"role_id": SNOWFLAKE_ROLE, "op_id": race_op, "origin_op_id": race_op},
            )
            h_key = grant["entry_key"]
            t_spec = {
                "guild_id": GUILD,
                "user_id": USER,
                "item_id": perm_pid,
                "item_name": "RaceTimedPass",
                "internal_type": "shop_custom",
                "role_id": SNOWFLAKE_ROLE,
                "entry_key": h_key,
                "role_added": False,
                "inventory_rolled_back": False,
                "equip_reconciled": False,
                "role_reconciled": False,
            }
            await timed_ownership.record_pending_delivery_rollback(race_op, t_spec, bot=None)
            self.guild.fetch_member = AsyncMock(return_value=self.member)
            if equip_first:
                t1 = asyncio.create_task(
                    equip_engine.equip_role(self.bot, GUILD, USER, "RaceTimedPass")
                )
                await asyncio.sleep(0)
                t2 = asyncio.create_task(
                    timed_ownership.compensate_late_delivery(race_op, t_spec, bot=self.bot)
                )
            else:
                t2 = asyncio.create_task(
                    timed_ownership.compensate_late_delivery(race_op, t_spec, bot=self.bot)
                )
                await asyncio.sleep(0)
                t1 = asyncio.create_task(
                    equip_engine.equip_role(self.bot, GUILD, USER, "RaceTimedPass")
                )
            _, rb_out = await asyncio.gather(t1, t2)
            self.assertTrue(rb_out["complete"], rb_out)
            self.assertEqual(set(worn_ids(self.member)), set())
            self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
            self.assertEqual(
                rows("SELECT id FROM timed_holdings WHERE guild_id=? AND user_id=?", (GUILD, USER)),
                [],
            )

    # ── 7. Timed Role-Linked Items Blocked from Leveling Rewards ───────────

    async def test_07_timed_role_linked_item_blocked_from_permanent_leveling_rewards(self):
        import json
        from utils import level_claims

        timed_id = _seed_shop_item(
            "24h Level Crest", kind="custom", price=200, role_id=SNOWFLAKE_ROLE, duration_hours=24
        )
        perm_id = _seed_shop_item(
            "Permanent Level Crest", kind="custom", price=200, role_id=ALT_ROLE, duration_hours=None
        )

        # GET /api/leveling/shop-rewards excludes timed role-linked custom items
        listed = self.client.get("/api/leveling/shop-rewards").get_json()
        product_ids = {p["id"] for p in listed["products"]}
        self.assertNotIn(timed_id, product_ids)
        self.assertIn(perm_id, product_ids)

        # POST /api/leveling/shop-reward rejects timed role-linked custom item
        r_bad = self.client.post(
            "/api/leveling/shop-reward",
            json={"level": 5, "item_id": timed_id, "quantity": 1, "audience": "everyone"},
            headers=self.headers,
        )
        bad_json = r_bad.get_json()
        self.assertFalse(bad_json["success"], bad_json)
        self.assertIn("Timed role-linked Items cannot be used", bad_json["error"])

        # Permanent role-linked custom item is accepted, and cannot later be edited into timed!
        r_ok = self.client.post(
            "/api/leveling/shop-reward",
            json={"level": 5, "item_id": perm_id, "quantity": 1, "audience": "everyone"},
            headers=self.headers,
        )
        self.assertEqual(r_ok.status_code, 200, r_ok.get_data(as_text=True))
        self.assertTrue(r_ok.get_json()["success"], r_ok.get_json())
        r_edit_timed = self.client.put(
            f"/api/shop/item/{perm_id}",
            json={"duration_hours": 24},
            headers=self.headers,
        )
        self.assertEqual(r_edit_timed.status_code, 400, r_edit_timed.get_data(as_text=True))

        # Direct DB bypass into leveling_shop_rewards is skipped by _definitions / record_crossing
        execute(
            "INSERT INTO leveling_shop_rewards (guild_id, level, item_id, quantity, audience) "
            "VALUES (?, 10, ?, 1, 'everyone')",
            (GUILD, timed_id),
        )
        import aiosqlite
        from database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            await level_claims.ensure_tables(db)
            await level_claims.record_crossing(
                db, GUILD, USER, 0, 50_000, source="message"
            )
            await db.commit()
        claim_refs = [
            r[0] for r in rows(
                "SELECT reward_ref FROM level_reward_claims WHERE guild_id=? AND user_id=?",
                (GUILD, USER),
            )
        ]
        self.assertNotIn(str(timed_id), claim_refs)

        # Even a stale/forged level_reward_claims row fails closed in claim_available!
        forged_payload = json.dumps({
            "item_id": timed_id,
            "name": "24h Level Crest",
            "type": "custom",
            "quantity": 1,
            "metadata": {"role_id": SNOWFLAKE_ROLE, "product_id": timed_id},
        })
        forged_cid = execute(
            "INSERT INTO level_reward_claims "
            "(guild_id, user_id, reward_level, track, reward_ref, status, payload_json, created_at) "
            "VALUES (?, ?, 10, 'shop', ?, 'pending', ?, '2026-01-01T00:00:00+00:00')",
            (GUILD, USER, str(timed_id), forged_payload),
        )
        claimed = await level_claims.claim_available(
            GUILD, USER, member=self.member, bot=self.bot, claim_ids=[forged_cid]
        )
        self.assertEqual(claimed["fulfilled"], [])
        self.assertEqual(claimed["failed"], [forged_cid])
        self.assertEqual(
            rows(
                "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='24h Level Crest'",
                (GUILD, USER),
            ),
            [],
        )

    # ── 8. Level-Drop & XP-Reset Reversal of Role-Linked Items ─────────────

    async def test_08_level_drop_and_xp_reset_reconcile_role_linked_items_and_provenance(self):
        import aiosqlite
        from database import DB_PATH
        from utils import level_claims, level_test_reset

        async def _award_to_11(op_tag: str = "admin-grant-1"):
            target_xp = 50_000
            execute(
                "INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, 15)",
                (GUILD, USER, target_xp),
            )
            async with aiosqlite.connect(DB_PATH) as db:
                await level_claims.ensure_tables(db)
                await level_claims.record_crossing(
                    db, GUILD, USER, 0, target_xp,
                    source="setlevel", operation_id=op_tag,
                )
                await db.commit()

        self.guild.fetch_member = AsyncMock(return_value=self.member)
        crest_pid = _seed_shop_item("Order Seal", kind="custom", price=200, role_id=SNOWFLAKE_ROLE)
        shadow_pid = _seed_shop_item("Shadow Seal", kind="custom", price=200, role_id=ALT_ROLE)

        # 8a. Claimed + equipped Role-Linked Item reversed by level-drop (including retry after failed remove_roles)
        execute(
            "INSERT INTO leveling_shop_rewards (guild_id, level, item_id, quantity, audience) "
            "VALUES (?, 10, ?, 1, 'everyone')",
            (GUILD, crest_pid),
        )
        await _award_to_11()
        await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
        eq = await equip_engine.equip_role(self.bot, GUILD, USER, "Order Seal")
        self.assertTrue(eq["success"], eq)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})

        orig_remove = self.member.remove_roles
        self.member.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 503"))
        execute("UPDATE levels SET xp=0, level=1 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        drop_fail = await level_claims.reconcile_level_drop(
            GUILD, USER, 1, operation_id="corr-fail-1", member=self.member, reason="admin setlevel"
        )
        self.assertEqual(len(drop_fail["kept_unresolved"]), 1)
        self.assertIsNotNone(await equip_engine.get_equipped(GUILD, USER))
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})

        # Retry once Discord recovers removes role, clears slot, and deletes claim!
        self.member.remove_roles = orig_remove
        drop_ok = await level_claims.reconcile_level_drop(
            GUILD, USER, 1, operation_id="corr-retry-1", member=self.member, reason="admin setlevel retry"
        )
        self.assertEqual(drop_ok["kept_unresolved"], [])
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
        self.assertEqual(set(worn_ids(self.member)), set())
        self.assertEqual(
            rows("SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=?", (GUILD, USER)),
            [],
        )

        # 8b. Same scenario reversed via /xp_reset (reset_member apply=True)
        await _award_to_11()
        await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
        await equip_engine.equip_role(self.bot, GUILD, USER, "Order Seal")
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        reset_rep = await level_test_reset.reset_member(
            GUILD, USER, apply=True, allow_production=True,
            member=self.member, reason="qa reset"
        )
        self.assertTrue(reset_rep["applied"], reset_rep)
        self.assertTrue(reset_rep["new_game_complete"], reset_rep)
        self.assertEqual(set(worn_ids(self.member)), set())
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))

        # 8b2. Same scenario reversed via /xp_rollback (rollback_xp_operation apply=True)
        rb_grant_op = "admin-grant-rb"
        await _award_to_11(rb_grant_op)
        execute(
            "INSERT INTO xp_mutations "
            "(operation_id, guild_id, user_id, source, actor_id, reason, "
            "xp_before, xp_after, level_before, level_after, delta, created_at) "
            "VALUES (?, ?, ?, 'setlevel', ?, 'test grant', 0, 50000, 0, 15, 50000, '2026-01-01T00:00:00+00:00')",
            (rb_grant_op, GUILD, USER, 999999999),
        )
        await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
        await equip_engine.equip_role(self.bot, GUILD, USER, "Order Seal")
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        rb_res = await level_test_reset.rollback_xp_operation(
            GUILD, USER, operation_id=rb_grant_op, apply=True, member=self.member,
        )
        self.assertTrue(rb_res["applied"], rb_res)
        self.assertEqual(rb_res["role_failures"], [])
        self.assertEqual(set(worn_ids(self.member)), set())
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
        self.assertEqual(await inventory.get_inventory(GUILD, USER), [])

        # 8c. Level Role reward + Role-Linked Shop Item granting SAME role in one correction cycle
        # Test BOTH orderings: (Role @ L8, Item @ L10) AND (Role @ L12, Item @ L10)
        for role_lvl in (8, 12):
            execute("DELETE FROM leveling_rewards WHERE guild_id=?", (GUILD,))
            execute(
                "INSERT INTO leveling_rewards (guild_id, level, role_id, audience) VALUES (?, ?, ?, 'everyone')",
                (GUILD, role_lvl, SNOWFLAKE_ROLE),
            )
            await _award_to_11()
            await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
            await equip_engine.equip_role(self.bot, GUILD, USER, "Order Seal")
            self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
            execute("UPDATE levels SET xp=0, level=1 WHERE guild_id=? AND user_id=?", (GUILD, USER))
            same_role_drop = await level_claims.reconcile_level_drop(
                GUILD, USER, 1, operation_id=f"corr-same-{role_lvl}",
                member=self.member, reason=f"drop both role@{role_lvl} and item@10"
            )
            self.assertEqual(same_role_drop["kept_unresolved"], [])
            self.assertEqual(set(worn_ids(self.member)), set())
            self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
            self.assertEqual(
                rows("SELECT id FROM level_reward_claims WHERE guild_id=? AND user_id=?", (GUILD, USER)),
                [],
            )
        execute("DELETE FROM leveling_rewards WHERE guild_id=?", (GUILD,))

        # 8d. Mixed Leveling Reward + Shop purchase ownership (stack source == 'level_claim')
        await _award_to_11()
        await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
        await self._buy(crest_pid)  # Merges into stack whose source remains 'level_claim'!
        src_row = rows(
            "SELECT quantity, source FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Order Seal'",
            (GUILD, USER),
        )[0]
        self.assertEqual(src_row, (2, "level_claim"))
        await equip_engine.equip_role(self.bot, GUILD, USER, "Order Seal")
        execute("UPDATE levels SET xp=0, level=1 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        mixed_drop = await level_claims.reconcile_level_drop(
            GUILD, USER, 1, operation_id="corr-mixed-1",
            member=self.member, reason="drop level claims but keep shop unit"
        )
        self.assertEqual(mixed_drop["kept_unresolved"], [])
        # Purchased unit (quantity == 1), equipped slot, and Discord role MUST remain intact!
        self.assertEqual(
            rows(
                "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Order Seal'",
                (GUILD, USER),
            ),
            [(1,)],
        )
        self.assertEqual((await equip_engine.get_equipped(GUILD, USER))["item_name"], "Order Seal")
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})
        await equip_engine.unequip_role(self.bot, GUILD, USER, "Order Seal")
        await inventory.remove_item(GUILD, USER, "Order Seal", 1)
        execute("DELETE FROM leveling_rewards WHERE guild_id=?", (GUILD,))
        execute("DELETE FROM leveling_shop_rewards WHERE guild_id=?", (GUILD,))

        # 8e. Independently held Discord role (role_added == 0) is NEVER removed by level-drop
        execute(
            "INSERT INTO leveling_shop_rewards (guild_id, level, item_id, quantity, audience) "
            "VALUES (?, 10, ?, 1, 'everyone')",
            (GUILD, shadow_pid),
        )
        self.member.roles = [self.roles[ALT_ROLE]]
        await _award_to_11()
        await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
        await equip_engine.equip_role(self.bot, GUILD, USER, "Shadow Seal")
        self.assertEqual((await equip_engine.get_equipped(GUILD, USER))["role_added"], 0)
        execute("UPDATE levels SET xp=0, level=1 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        indep_drop = await level_claims.reconcile_level_drop(
            GUILD, USER, 1, operation_id="corr-indep-1",
            member=self.member, reason="drop shadow seal"
        )
        self.assertEqual(indep_drop["kept_unresolved"], [])
        self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
        self.assertEqual(set(worn_ids(self.member)), {ALT_ROLE})

    # ── 9. Role ID Consistency Across Definitions & Timed Holdings ─────────

    async def test_09_role_id_consistency_across_definitions_and_timed_holdings(self):
        # Create timed Role-Linked Item bound to SNOWFLAKE_ROLE and grant an active timed holding
        r_create = self.client.post(
            "/api/shop/item",
            json={
                "name": "Timed Sovereign Crest",
                "type": "custom",
                "price": 300,
                "duration_hours": 24,
                "role_id": str(SNOWFLAKE_ROLE),
            },
            headers=self.headers,
        )
        self.assertEqual(r_create.status_code, 200, r_create.get_data(as_text=True))
        timed_id = rows(
            "SELECT id FROM shop_items WHERE guild_id=? AND name='Timed Sovereign Crest'",
            (GUILD,),
        )[0][0]

        # POST same-name item with a DIFFERENT role_id conflicts with existing definition
        r_def_conflict = self.client.post(
            "/api/shop/item",
            json={
                "name": "Timed Sovereign Crest",
                "type": "custom",
                "price": 500,
                "role_id": str(ALT_ROLE),
            },
            headers=self.headers,
        )
        self.assertEqual(r_def_conflict.status_code, 400, r_def_conflict.get_data(as_text=True))

        # Same-name product with the SAME 64-bit role_id IS compatible and allowed!
        r_same_role = self.client.post(
            "/api/shop/item",
            json={
                "name": "Timed Sovereign Crest",
                "type": "custom",
                "price": 900,
                "role_id": str(SNOWFLAKE_ROLE),
            },
            headers=self.headers,
        )
        self.assertEqual(r_same_role.status_code, 200, r_same_role.get_data(as_text=True))
        sibling_id = rows(
            "SELECT id FROM shop_items WHERE guild_id=? AND name='Timed Sovereign Crest' AND id != ?",
            (GUILD, timed_id),
        )[0][0]
        self.client.delete(f"/api/shop/item/{sibling_id}", headers=self.headers)

        # Grant active timed holding for Timed Sovereign Crest
        await self._buy(timed_id)

        # PUT changing R1 -> R2 while active timed holding exists -> rejected (400)!
        r_put_r2 = self.client.put(
            f"/api/shop/item/{timed_id}",
            json={"role_id": str(ALT_ROLE)},
            headers=self.headers,
        )
        self.assertEqual(r_put_r2.status_code, 400, r_put_r2.get_data(as_text=True))

        # PUT clearing role_id (R1 -> None) while active timed holding exists -> rejected (400)!
        r_put_clear = self.client.put(
            f"/api/shop/item/{timed_id}",
            json={"role_id": None},
            headers=self.headers,
        )
        self.assertEqual(r_put_clear.status_code, 400, r_put_clear.get_data(as_text=True))

        # Delete the shop listing so only member holdings remain: POST with R2 STILL conflicts
        # against both active timed_holdings AND owned permanent inventory_items!
        self.client.delete(f"/api/shop/item/{timed_id}", headers=self.headers)
        r_post_timed_conflict = self.client.post(
            "/api/shop/item",
            json={
                "name": "Timed Sovereign Crest",
                "type": "custom",
                "price": 300,
                "role_id": str(ALT_ROLE),
            },
            headers=self.headers,
        )
        self.assertEqual(r_post_timed_conflict.status_code, 400, r_post_timed_conflict.get_data(as_text=True))

        await inventory.give_item(
            GUILD, USER, "PermOwnedCrest", 1, item_type="shop_custom",
            metadata={"role_id": SNOWFLAKE_ROLE},
        )
        r_post_perm_conflict = self.client.post(
            "/api/shop/item",
            json={
                "name": "PermOwnedCrest",
                "type": "custom",
                "price": 300,
                "role_id": str(ALT_ROLE),
            },
            headers=self.headers,
        )
        self.assertEqual(r_post_perm_conflict.status_code, 400, r_post_perm_conflict.get_data(as_text=True))

        # Once the timed holding is EXPIRED, it no longer blocks creating/editing to ALT_ROLE or plain!
        past = (timed_ownership.utcnow() - timedelta(hours=2)).isoformat()
        execute("UPDATE timed_holdings SET expires_at=?", (past,))
        r_after_exp = self.client.post(
            "/api/shop/item",
            json={
                "name": "Timed Sovereign Crest",
                "type": "custom",
                "price": 300,
                "role_id": str(ALT_ROLE),
            },
            headers=self.headers,
        )
        self.assertEqual(r_after_exp.status_code, 200, r_after_exp.get_data(as_text=True))
        new_id = rows(
            "SELECT id, role_id FROM shop_items WHERE guild_id=? AND name='Timed Sovereign Crest'",
            (GUILD,),
        )[0]
        self.assertEqual(new_id[1], ALT_ROLE)
        r_clear_after_exp = self.client.put(
            f"/api/shop/item/{new_id[0]}",
            json={"role_id": None},
            headers=self.headers,
        )
        self.assertEqual(r_clear_after_exp.status_code, 200, r_clear_after_exp.get_data(as_text=True))

    # ── 10. Pre-Charge Role Assignability Validation ───────────────────────

    async def test_10_pre_charge_role_assignability_validation(self):
        everyone_role = _add_role_to_env(self.guild, self.roles, GUILD, "@everyone", position=0)
        everyone_role.is_default = lambda: True
        managed_role = _add_role_to_env(self.guild, self.roles, 990001, "BotManaged", position=3)
        managed_role.managed = True
        high_role = _add_role_to_env(self.guild, self.roles, 990002, "AboveBot", position=999)

        cases = [
            ("MissingRoleCrest", 999999999999999999, 24),
            ("EveryoneCrest", GUILD, None),
            ("ManagedCrest", 990001, 24),
            ("HierarchyCrest", 990002, 24),
        ]
        for cname, crid, cdur in cases:
            pid = execute(
                "INSERT INTO shop_items "
                "(guild_id, name, type, price, role_id, duration_hours, max_stock, current_stock, enabled) "
                "VALUES (?, ?, 'custom', 500, ?, ?, 3, 3, 1)",
                (GUILD, cname, crid, cdur),
            )
            msg = await self._buy(pid)
            self.assertTrue(msg.get("ephemeral"), f"Expected ephemeral refusal for {cname}")
            # Zero coins deducted, zero stock consumed, zero purchase ops, zero holdings/items!
            bal = rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0]
            stock = rows("SELECT current_stock FROM shop_items WHERE id=?", (pid,))[0][0]
            self.assertEqual(bal, 100_000, f"{cname} charged buyer!")
            self.assertEqual(stock, 3, f"{cname} consumed stock!")

        # Also fail closed when bot lacks Manage Roles permission!
        valid_pid = execute(
            "INSERT INTO shop_items "
            "(guild_id, name, type, price, role_id, duration_hours, max_stock, current_stock, enabled) "
            "VALUES (?, 'NoPermCrest', 'custom', 500, ?, 24, 3, 3, 1)",
            (GUILD, SNOWFLAKE_ROLE),
        )
        self.guild.me.guild_permissions.manage_roles = False
        self.guild.me.guild_permissions.administrator = False
        await self._buy(valid_pid)
        # Also fail closed when role verification hits a Discord error on an uncached role!
        self.guild.me.guild_permissions.manage_roles = True
        orig_get_role = self.guild.get_role
        self.guild.get_role = lambda _rid: None
        self.guild.fetch_roles = AsyncMock(side_effect=RuntimeError("Discord 503"))
        await self._buy(valid_pid)
        self.guild.get_role = orig_get_role
        delattr(self.guild, "fetch_roles")

        self.assertEqual(
            rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?", (GUILD, USER))[0][0],
            100_000,
        )
        self.assertEqual(
            rows("SELECT current_stock FROM shop_items WHERE id=?", (valid_pid,))[0][0],
            3,
        )
        self.assertEqual(rows("SELECT COUNT(*) FROM shop_purchase_operations"), [(0,)])
        self.assertEqual(rows("SELECT COUNT(*) FROM timed_holdings"), [(0,)])
        self.assertEqual(rows("SELECT COUNT(*) FROM inventory_items"), [(0,)])

    # ── 11. Entitlement DB Failure in Reversed Role-Linked Item Reconciler ──

    async def test_11_entitlement_db_failure_during_reversed_role_linked_item_reconciliation(self):
        import aiosqlite
        from database import DB_PATH
        from utils import level_claims, level_ops as ops

        execute("UPDATE levels SET xp=0, level=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        crest_pid = _seed_shop_item("Fault Seal", kind="custom", price=200, role_id=SNOWFLAKE_ROLE)
        execute(
            "INSERT INTO leveling_shop_rewards (guild_id, level, item_id, quantity, audience) "
            "VALUES (?, 10, ?, 1, 'everyone')",
            (GUILD, crest_pid),
        )
        execute(
            "INSERT OR REPLACE INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, 50000, 15)",
            (GUILD, USER),
        )
        async with aiosqlite.connect(DB_PATH) as db:
            await level_claims.ensure_tables(db)
            await level_claims.record_crossing(
                db, GUILD, USER, 0, 50000,
                source="setlevel", operation_id="admin-grant-ent-fault",
            )
            await db.commit()

        claimed = await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
        self.assertEqual(len(claimed["fulfilled"]), 1)
        claim_id = int(claimed["fulfilled"][0])

        eq = await equip_engine.equip_role(self.bot, GUILD, USER, "Fault Seal")
        self.assertTrue(eq["success"], eq)
        self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})

        execute("UPDATE levels SET xp=0, level=1 WHERE guild_id=? AND user_id=?", (GUILD, USER))

        # Break the real SQLite `timed_holdings` table so `_role_granted_elsewhere()`
        # hits a genuine OperationalError after `ops.reverse_inventory` succeeds.
        execute("ALTER TABLE timed_holdings RENAME TO _timed_holdings_fault")
        table_renamed = True
        try:
            # 11a. Prove `_role_granted_elsewhere()` raises `EntitlementCheckError` on DB failure.
            async with aiosqlite.connect(DB_PATH) as db:
                with self.assertRaises(timed_ownership.EntitlementCheckError) as ctx:
                    await level_claims._role_granted_elsewhere(
                        db,
                        GUILD,
                        USER,
                        SNOWFLAKE_ROLE,
                        exclude_claim_id=claim_id,
                        exclude_item="Fault Seal",
                    )
            self.assertIn("Failed to verify timed_holdings entitlement", str(ctx.exception))

            # 11b. End-to-end level-drop caller: `ops.reverse_inventory` succeeds, then
            # `reconcile_reversed_role_linked_item` catches `EntitlementCheckError` (no NameError),
            # preserves the equipped slot and Discord role, and caller records `KIND_ROLE`
            # as `STATUS_OUTSTANDING` + `kept_unresolved`.
            drop_fail = await level_claims.reconcile_level_drop(
                GUILD,
                USER,
                1,
                operation_id="corr-ent-fail-1",
                member=self.member,
                reason="admin setlevel",
            )
            self.assertEqual(
                rows(
                    "SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Fault Seal'",
                    (GUILD, USER),
                ),
                [(0,)],
            )
            self.assertEqual(len(drop_fail["kept_unresolved"]), 1)
            unresolved = drop_fail["kept_unresolved"][0]
            self.assertEqual(unresolved["claim_id"], claim_id)
            self.assertEqual(unresolved["kind"], ops.KIND_ROLE)
            self.assertEqual(unresolved["role_id"], SNOWFLAKE_ROLE)
            self.assertIn("Failed to verify timed_holdings entitlement", unresolved["reason"])
            self.assertEqual(drop_fail["deleted_claims"], [])

            slot_during_fault = await equip_engine.get_equipped(GUILD, USER)
            self.assertIsNotNone(slot_during_fault)
            self.assertEqual(slot_during_fault["item_name"], "Fault Seal")
            self.assertEqual(int(slot_during_fault["role_id"]), SNOWFLAKE_ROLE)
            self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})

            rev_rows = rows(
                "SELECT kind, status, reason FROM reward_reversals "
                "WHERE guild_id=? AND user_id=? AND claim_id=? ORDER BY kind",
                (GUILD, USER, claim_id),
            )
            self.assertEqual([(r[0], r[1]) for r in rev_rows], [
                (ops.KIND_INVENTORY, ops.STATUS_APPLIED),
                (ops.KIND_ROLE, ops.STATUS_OUTSTANDING),
            ])
            self.assertIn("Failed to verify timed_holdings entitlement", rev_rows[1][2])
            self.assertEqual(
                rows("SELECT id FROM level_reward_claims WHERE id=?", (claim_id,)),
                [(claim_id,)],
            )

            # 11c. Direct reconciler assertion on the already-emptied inventory row:
            # returns `ok=False` with the error message and leaves slot + Discord role intact.
            rec_direct = await level_claims.reconcile_reversed_role_linked_item(
                GUILD,
                USER,
                item_name="Fault Seal",
                role_id=SNOWFLAKE_ROLE,
                member=self.member,
                exclude_claim_id=claim_id,
            )
            self.assertFalse(rec_direct["ok"])
            self.assertFalse(rec_direct["role_removed"])
            self.assertIn("Failed to verify timed_holdings entitlement", rec_direct["error"])
            self.assertIsNotNone(await equip_engine.get_equipped(GUILD, USER))
            self.assertEqual(set(worn_ids(self.member)), {SNOWFLAKE_ROLE})

            # 11d. Restore `timed_holdings` and verify safe retry resolves the open
            # `KIND_ROLE` reversal, removes the Discord role, clears `equipped_roles`,
            # and retires the claim row.
            execute("ALTER TABLE _timed_holdings_fault RENAME TO timed_holdings")
            table_renamed = False

            drop_retry = await level_claims.reconcile_level_drop(
                GUILD,
                USER,
                1,
                operation_id="corr-ent-retry-1",
                member=self.member,
                reason="admin setlevel retry",
            )
            self.assertEqual(drop_retry["kept_unresolved"], [])
            self.assertIsNone(await equip_engine.get_equipped(GUILD, USER))
            self.assertEqual(set(worn_ids(self.member)), set())
            self.assertEqual(
                rows(
                    "SELECT kind, status FROM reward_reversals "
                    "WHERE guild_id=? AND user_id=? AND claim_id=? ORDER BY kind",
                    (GUILD, USER, claim_id),
                ),
                [
                    (ops.KIND_INVENTORY, ops.STATUS_APPLIED),
                    (ops.KIND_ROLE, ops.STATUS_APPLIED),
                ],
            )
            self.assertEqual(
                rows("SELECT id FROM level_reward_claims WHERE id=?", (claim_id,)),
                [],
            )
        finally:
            if table_renamed:
                execute("ALTER TABLE _timed_holdings_fault RENAME TO timed_holdings")


if __name__ == "__main__":
    unittest.main(verbosity=2)
