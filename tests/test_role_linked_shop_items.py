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


if __name__ == "__main__":
    unittest.main(verbosity=2)
