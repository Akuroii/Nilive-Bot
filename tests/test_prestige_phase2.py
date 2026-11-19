"""Prestige phase acceptance tests (isolated SQLite, real purchase/reward paths).

Discord objects are small fakes (a purchase needs an Interaction, a role sync
needs a Guild/Member/Role); every balance, tier, ledger and claim assertion is
read back from the real database. Behaviour that needs a live Discord server
(real role hierarchy, real Forbidden errors) is NOT proven here.

Run: PYTHONPATH=$PWD python3 -m pytest -q tests/test_prestige_phase2.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

os.environ["OWNER_ID"] = "1"
os.environ.setdefault("SECRET_KEY", "isolated-test-only")
os.environ["DATABASE_PATH"] = f"/tmp/nilive-prestige2-import-{os.getpid()}.sqlite"

import aiosqlite
import database
import cogs.shop  # noqa: F401  (import before DB_PATH patching)
import utils.currency, utils.daily_engine, utils.economy_safe, utils.inventory  # noqa: E401,F401
import utils.ledger, utils.level_claims, utils.level_ops, utils.pending_rewards  # noqa: E401,F401
import utils.permissions, utils.prestige, utils.reward_engine, utils.shop_validation  # noqa: E401,F401
import utils.streak_settings, utils.xp_calculator, utils.equip_engine  # noqa: E401,F401

from utils import prestige
from utils.prestige import PrestigeError

G1, G2, USER = 10, 11, 20
BOOST_SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── Discord fakes ────────────────────────────────────────────────────────
class FakeRole:
    def __init__(self, role_id, assignable=True):
        self.id, self._assignable = role_id, assignable

    def is_assignable(self):
        return self._assignable


class FakeMember:
    def __init__(self, guild, user_id=USER, boosting=False, add_roles_error=None):
        self.id, self.guild, self.roles = user_id, guild, []
        self.display_name, self.mention = "Tester", f"<@{user_id}>"
        self.premium_since = BOOST_SINCE if boosting else None
        self.added, self._error = [], add_roles_error

    async def add_roles(self, role, reason=None):
        if self._error:
            raise self._error
        self.roles.append(role)
        self.added.append(role.id)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)


class FakeGuild:
    def __init__(self, guild_id, roles=()):
        self.id = guild_id
        self._roles = {r.id: r for r in roles}
        self.members = {}
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(administrator=False, manage_roles=True))

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, user_id):
        return self.members.get(user_id)

    async def fetch_member(self, user_id):
        return self.members.get(user_id)


class FakeBot:
    def __init__(self, *guilds):
        self._g = {g.id: g for g in guilds}

    def get_guild(self, guild_id):
        return self._g.get(guild_id)


class FakeResponse:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, content=None, **kw):
        self.sent.append({"content": content, **kw})

    async def edit_message(self, **kw):
        self.edited.append(kw)


class FakeInteraction:
    def __init__(self, guild, member, bot):
        self.guild, self.user, self.client = guild, member, bot
        self.response = FakeResponse()


def text_of(call):
    embed = call.get("embed")
    parts = [call.get("content") or ""]
    if embed is not None:
        parts += [embed.title or "", embed.description or ""]
        parts += [f"{f.name} {f.value}" for f in embed.fields]
    return " ".join(parts)


class PrestigePhase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory(prefix="nilive-prestige2-")
        self.db_path = str(Path(self.tmp.name) / "iso.sqlite")
        original = database.DB_PATH
        self._patches = []
        for mod in list(sys.modules.values()):
            if mod is not None and getattr(mod, "DB_PATH", None) == original:
                p = patch.object(mod, "DB_PATH", self.db_path)
                p.start()
                self._patches.append(p)
        await database.init_db()

    async def asyncTearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    # ── helpers ──
    async def q(self, sql, args=()):
        async with aiosqlite.connect(self.db_path) as db:
            return await (await db.execute(sql, args)).fetchall()

    async def x(self, sql, args=()):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, args)
            await db.commit()

    async def seed_member(self, guild=G1, *, coins=1000, diamonds=7, xp=500,
                          level=3, prestige_tier=0, streak=4):
        await self.x("INSERT OR REPLACE INTO economy (guild_id,user_id,balance,diamonds) VALUES (?,?,?,?)",
                     (guild, USER, coins, diamonds))
        await self.x("INSERT OR REPLACE INTO levels (guild_id,user_id,xp,level,prestige) VALUES (?,?,?,?,?)",
                     (guild, USER, xp, level, prestige_tier))
        await self.x("INSERT OR REPLACE INTO daily_claims (guild_id,user_id,last_claim_date,streak_count) "
                     "VALUES (?,?,?,?)", (guild, USER, "2026-11-17", streak))

    async def snapshot(self, guild=G1):
        eco = await self.q("SELECT balance,diamonds FROM economy WHERE guild_id=? AND user_id=?", (guild, USER))
        lvl = await self.q("SELECT xp,level,prestige FROM levels WHERE guild_id=? AND user_id=?", (guild, USER))
        dc = await self.q("SELECT last_claim_date,streak_count FROM daily_claims WHERE guild_id=? AND user_id=?",
                          (guild, USER))
        return eco[0], lvl[0], dc[0]

    async def add_shop_item(self, guild, name, tier, price):
        await self.x("INSERT INTO shop_items (guild_id,name,price,type,prestige_tier,enabled) "
                     "VALUES (?,?,?,'prestige',?,1)", (guild, name, price, tier))
        return (await self.q("SELECT id FROM shop_items WHERE guild_id=? AND name=?", (guild, name)))[0][0]

    # ── 1. price validation: Free Tier I only ──
    def test_prestige_price_rules(self):
        from utils.shop_validation import prestige_terms, ShopValidationError
        self.assertEqual(prestige_terms(0, 1), (0, 1))          # I is Free
        self.assertEqual(prestige_terms("450", 5), (450, 5))
        self.assertEqual(prestige_terms(0, 6), (0, 6))          # VI no price
        for price, tier in ((0, 2), (0, 3), (0, 4), (0, 5), (1, 6), (-1, 1)):
            with self.assertRaises(ShopValidationError, msg=(price, tier)):
                prestige_terms(price, tier)
        with self.assertRaises(ShopValidationError):
            prestige_terms(30, 2, diamonds=5)                    # no diamond price

    # ── 2. sequential purchase, free I, resets for I-V ──
    async def test_free_tier_one_then_sequential_with_coin_reset(self):
        await self.seed_member(coins=500)
        before = await self.snapshot()
        with self.assertRaises(PrestigeError):
            await prestige.purchase_prestige(G1, USER, 3, 140)    # skip
        self.assertEqual(await self.snapshot(), before)           # nothing changed
        r = await prestige.purchase_prestige(G1, USER, 1, 0)      # free I
        self.assertEqual(r["new_tier"], 1)
        eco, lvl, dc = await self.snapshot()
        self.assertEqual(eco, (0, 7))                             # Coins 0, Diamonds kept
        self.assertEqual(lvl, (500, 3, 1))                        # XP/Level kept
        self.assertEqual(dc, ("2026-11-17", 4))                   # Streak + claim date kept
        with self.assertRaises(PrestigeError):
            await prestige.purchase_prestige(G1, USER, 1, 0)      # duplicate
        with self.assertRaises(PrestigeError):
            await prestige.purchase_prestige(G1, USER, 3, 140)    # still skipping II
        # II..V each reset Coins and leave everything else untouched.
        for tier, price in ((2, 30), (3, 140), (4, 270), (5, 450)):
            await self.x("UPDATE economy SET balance=? WHERE guild_id=? AND user_id=?", (price + 5, G1, USER))
            res = await prestige.purchase_prestige(G1, USER, tier, price)
            self.assertEqual(res["new_tier"], tier)
            eco, lvl, dc = await self.snapshot()
            self.assertEqual((eco, lvl, dc), ((0, 7), (500, 3, tier), ("2026-11-17", 4)), tier)
        with self.assertRaises(PrestigeError):
            await prestige.purchase_prestige(G1, USER, 5, 450)    # already V
        # below-price balance is refused and not reset
        await self.seed_member(guild=G2, coins=29, prestige_tier=1)
        with self.assertRaises(PrestigeError):
            await prestige.purchase_prestige(G2, USER, 2, 30)
        self.assertEqual((await self.snapshot(G2))[0], (29, 7))

    # ── 3. per-guild isolation (price + tier state) through the shop flow ──
    async def test_shop_flow_confirmation_and_per_guild_prices(self):
        import cogs.shop as shop
        item1 = await self.add_shop_item(G1, "Prestige II", 2, 30)
        item2 = await self.add_shop_item(G2, "Prestige II", 2, 225)
        await self.seed_member(G1, coins=100, prestige_tier=1)
        await self.seed_member(G2, coins=100, prestige_tier=1)
        g1, g2 = FakeGuild(G1), FakeGuild(G2)
        bot = FakeBot(g1, g2)

        # G1: price 30, balance 100 -> confirmation first, NOTHING written yet.
        m1 = FakeMember(g1)
        g1.members[USER] = m1
        it = FakeInteraction(g1, m1, bot)
        await shop._process_purchase(it, item1)
        sent = it.response.sent[0]
        warn = text_of(sent)
        self.assertTrue(sent["ephemeral"])
        self.assertIn("reset your", warn)
        self.assertIn("**0**", warn)
        self.assertIn("100", warn)                                 # shows current balance
        self.assertIsNotNone(sent.get("view"))
        self.assertEqual((await self.snapshot(G1))[0], (100, 7))
        self.assertEqual((await self.snapshot(G1))[1][2], 1)

        # Someone else cannot confirm it.
        class Other: id = 999
        self.assertFalse(await sent["view"].interaction_check(
            SimpleNamespace(user=Other(), response=FakeResponse())))

        # Cancel changes nothing.
        cancel = FakeInteraction(g1, m1, bot)
        await sent["view"].cancel.callback(cancel)
        self.assertIn("cancelled", cancel.response.edited[0]["content"].lower())
        self.assertEqual((await self.snapshot(G1))[0], (100, 7))

        # Fresh confirmation -> Confirm performs the purchase.
        it = FakeInteraction(g1, m1, bot)
        await shop._process_purchase(it, item1)
        view = it.response.sent[0]["view"]
        click = FakeInteraction(g1, m1, bot)
        with contextlib.redirect_stdout(io.StringIO()):
            await view.confirm.callback(click)
        done = text_of(click.response.edited[0])
        self.assertIn("Prestige II", done)
        eco, lvl, dc = await self.snapshot(G1)
        self.assertEqual((eco, lvl[2], dc), ((0, 7), 2, ("2026-11-17", 4)))
        # Double-click is a no-op.
        again = FakeInteraction(g1, m1, bot)
        await view.confirm.callback(again)
        self.assertEqual(again.response.sent[0]["content"], "Already processed.")

        # G2 is unaffected by G1's purchase and has its own price (225 > 100).
        m2 = FakeMember(g2)
        g2.members[USER] = m2
        it2 = FakeInteraction(g2, m2, bot)
        await shop._process_purchase(it2, item2)
        click2 = FakeInteraction(g2, m2, bot)
        await it2.response.sent[0]["view"].confirm.callback(click2)
        self.assertIn("225", click2.response.edited[0]["content"])
        self.assertEqual(await self.snapshot(G2), ((100, 7), (500, 3, 1), ("2026-11-17", 4)))

    async def test_free_tier_one_through_shop_still_confirms_reset(self):
        import cogs.shop as shop
        item = await self.add_shop_item(G1, "Prestige I", 1, 0)
        await self.seed_member(G1, coins=321, prestige_tier=0)
        g1 = FakeGuild(G1)
        m = FakeMember(g1)
        it = FakeInteraction(g1, m, FakeBot(g1))
        await shop._process_purchase(it, item)
        warn = text_of(it.response.sent[0])
        self.assertIn("reset your", warn)
        self.assertIn("321", warn)
        self.assertEqual((await self.snapshot())[0], (321, 7))     # nothing until Confirm

    # ── 4. Prestige VI: no reset, no confirmation ──
    async def test_vi_activation_does_not_reset_coins_or_ask_confirmation(self):
        import cogs.shop as shop
        item = await self.add_shop_item(G1, "Prestige VI", 6, 0)
        await self.seed_member(G1, coins=777, prestige_tier=3)
        g1 = FakeGuild(G1)
        m = FakeMember(g1, boosting=True)
        g1.members[USER] = m
        it = FakeInteraction(g1, m, FakeBot(g1))
        with contextlib.redirect_stdout(io.StringIO()):
            await shop._process_purchase(it, item)
        sent = it.response.sent[0]
        self.assertIsNone(sent.get("view"))                        # no confirmation view
        self.assertIn("Prestige VI", text_of(sent))
        eco, lvl, dc = await self.snapshot()
        self.assertEqual((eco, lvl, dc), ((777, 7), (500, 3, 3), ("2026-11-17", 4)))
        self.assertEqual(len(await self.q("SELECT 1 FROM prestige_vi_activations WHERE guild_id=?", (G1,))), 1)
        # Non-boosters are refused.
        g2 = FakeGuild(G1)
        plain = FakeMember(g2, user_id=USER + 1)
        it2 = FakeInteraction(g2, plain, FakeBot(g2))
        await shop._process_purchase(it2, item)
        self.assertIn("Booster", text_of(it2.response.sent[0]))

    # ── 5. multipliers: defaults, no overwrite of existing config, V and VI ──
    async def test_default_multipliers_seed_only_fresh_guilds(self):
        fresh = await prestige.get_prestige_config(G2)
        self.assertEqual(fresh["tiers"][5], {"coins": 1.2, "diamonds": 1.1})
        self.assertEqual(fresh["tiers"][6], {"coins": 1.3, "diamonds": 1.2})
        for t in (1, 2, 3, 4):
            self.assertEqual(fresh["tiers"][t], {"coins": 1.0, "diamonds": 1.0})
        # A server that already saved its own values keeps them.
        await prestige.set_prestige_multipliers(G1, tiers={5: {"coins": 1.5, "diamonds": 1.25}})
        kept = await prestige.get_prestige_config(G1)
        self.assertEqual(kept["tiers"][5], {"coins": 1.5, "diamonds": 1.25})
        again = await prestige.get_prestige_config(G2)
        self.assertEqual(again["tiers"][5], {"coins": 1.2, "diamonds": 1.1})

    async def test_v_and_vi_multipliers_apply_exactly_once_never_to_conversion(self):
        from utils.reward_engine import give_reward
        from utils.economy_safe import safe_convert
        g1 = FakeGuild(G1)
        m = FakeMember(g1, boosting=True)
        g1.members[USER] = m
        bot = FakeBot(g1)
        await self.seed_member(G1, coins=0, diamonds=0, prestige_tier=5)
        await prestige.get_prestige_config(G1)                    # seed defaults 1.2/1.1 + 1.3/1.2
        # V
        c = await give_reward(bot, G1, USER, "coins", 100, source="t")
        d = await give_reward(bot, G1, USER, "diamonds", 100, source="t")
        # Without an active VI the booster-flagged member is still tier V.
        self.assertEqual((c["amount"], d["amount"]), (120, 110))
        # VI active (activation row inside this boost session)
        await self.x("INSERT INTO prestige_vi_activations (guild_id,user_id,activated_at) VALUES (?,?,?)",
                     (G1, USER, "2026-06-01T00:00:00.000000+00:00"))
        c = await give_reward(bot, G1, USER, "coins", 100, source="t")
        d = await give_reward(bot, G1, USER, "diamonds", 100, source="t")
        self.assertEqual((c["amount"], d["amount"]), (130, 120))
        led = await self.q("SELECT currency,amount FROM transaction_ledger WHERE guild_id=? AND user_id=? "
                           "ORDER BY rowid", (G1, USER))
        self.assertEqual(led, [("balance", 120), ("diamonds", 110), ("balance", 130), ("diamonds", 120)])
        # Conversion and deductions are never multiplied.
        await self.x("UPDATE economy SET balance=1000 WHERE guild_id=? AND user_id=?", (G1, USER))
        bal_before = (await self.snapshot())[0]
        await safe_convert(G1, USER, 500, 100)
        eco = (await self.snapshot())[0]
        self.assertEqual((bal_before[0] - eco[0], eco[1] - bal_before[1]), (500, 5))
        spent = await give_reward(bot, G1, USER, "coins", -10, source="t")
        self.assertEqual(spent["amount"], -10)

    # ── 6. Booster VI context at the level_ops call site ──
    async def _vi_member_with_level_reward(self):
        g1 = FakeGuild(G1)
        m = FakeMember(g1, boosting=True)
        g1.members[USER] = m
        await self.seed_member(G1, coins=0, diamonds=0, xp=0, level=0, prestige_tier=5, streak=1)
        await prestige.get_prestige_config(G1)
        await self.x("INSERT INTO prestige_vi_activations (guild_id,user_id,activated_at) VALUES (?,?,?)",
                     (G1, USER, "2026-06-01T00:00:00.000000+00:00"))
        await self.x("INSERT INTO leveling_currency_rewards (guild_id,level,currency,amount) VALUES (?,1,'balance',100)",
                     (G1,))
        return FakeBot(g1)

    async def _frozen_amount(self):
        import json
        rows = await self.q("SELECT payload_json FROM level_reward_claims WHERE guild_id=? AND user_id=? "
                            "AND track='currency'", (G1, USER))
        self.assertEqual(len(rows), 1, rows)
        return json.loads(rows[0][0])["amount"]

    async def test_drain_xp_grants_freezes_vi_multiplier_when_bot_is_passed(self):
        from utils import level_ops as ops
        from utils.xp_calculator import xp_for_level
        bot = await self._vi_member_with_level_reward()
        await ops.enqueue_xp_grant(G1, USER, xp_for_level(1), idem_key="mission:1", source="mission")
        out = await ops.drain_xp_grants(G1, USER, bot=bot)
        self.assertEqual(len(out["applied"]), 1, out)
        self.assertEqual(await self._frozen_amount(), 130)         # 100 x1.3 (VI), not x1.2 (V)

    async def test_drain_xp_grants_without_bot_keeps_permanent_tier_limitation(self):
        """Documented limitation: no member context -> permanent tier (V)."""
        from utils import level_ops as ops
        from utils.xp_calculator import xp_for_level
        await self._vi_member_with_level_reward()
        await ops.enqueue_xp_grant(G1, USER, xp_for_level(1), idem_key="mission:2", source="mission")
        await ops.drain_xp_grants(G1, USER)                        # legacy call, no bot
        self.assertEqual(await self._frozen_amount(), 120)

    async def test_drain_with_stale_vi_activation_does_not_deadlock_or_fall_back_to_raw(self):
        """Resolving a lapsed Booster deletes its activation on its own
        connection; that must happen before the drain's write transaction."""
        from utils import level_ops as ops
        from utils.xp_calculator import xp_for_level
        bot = await self._vi_member_with_level_reward()
        bot.get_guild(G1).members[USER].premium_since = None      # boost lapsed
        await ops.enqueue_xp_grant(G1, USER, xp_for_level(1), idem_key="mission:3", source="mission")
        out = await ops.drain_xp_grants(G1, USER, bot=bot)
        self.assertEqual(len(out["applied"]), 1, out)
        self.assertEqual(await self._frozen_amount(), 120)         # falls to permanent V, not raw 100
        self.assertEqual(await self.q("SELECT 1 FROM prestige_vi_activations WHERE guild_id=?", (G1,)), [])

    async def test_sweep_and_reward_engine_pass_the_bot(self):
        from utils import level_ops as ops
        from utils.xp_calculator import xp_for_level
        bot = await self._vi_member_with_level_reward()
        await ops.enqueue_xp_grant(G1, USER, xp_for_level(1), idem_key="mission:4", source="mission")
        swept = await ops.drain_xp_grants_sweep(bot=bot)
        self.assertEqual(swept["applied"], 1, swept)
        self.assertEqual(await self._frozen_amount(), 130)

    async def test_dashboard_snapshot_without_member_documents_limitation(self):
        """dashboard/app.py:3055 runs in the Flask process: no Discord gateway,
        no member object. snapshot_multipliers(guild, user) therefore resolves
        the permanent tier even for an active Booster. Not worked around."""
        from utils.level_claims import snapshot_multipliers
        await self._vi_member_with_level_reward()
        self.assertEqual(await snapshot_multipliers(G1, USER), {"balance": 1.2, "diamonds": 1.1})

    # ── 7. role assignment + diagnostics ──
    async def _sync(self, *, roles_cfg, guild_roles, member_kwargs=None, effective=6, assignable=True):
        g = FakeGuild(G1, guild_roles)
        m = FakeMember(g, **(member_kwargs or {}))
        g.members[USER] = m
        for tier, rid in roles_cfg.items():
            await self.x("INSERT INTO prestige_roles (guild_id,tier,role_id) VALUES (?,?,?)", (G1, tier, str(rid)))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await prestige.sync_prestige_roles(FakeBot(g), g, m, effective_tier=effective)
        return m, buf.getvalue()

    async def test_role_granted_for_configured_vi_role(self):
        m, log = await self._sync(roles_cfg={6: 600}, guild_roles=[FakeRole(600)])
        self.assertEqual(m.added, [600])
        self.assertEqual(log, "")

    async def test_role_missing_config_logs_actionable_warning(self):
        m, log = await self._sync(roles_cfg={5: 500}, guild_roles=[FakeRole(500)])
        self.assertEqual(m.added, [])
        self.assertIn("[PRESTIGE ROLE WARNING]", log)
        self.assertIn(f"guild={G1} user={USER} tier=VI", log)
        self.assertIn("no role is configured", log)
        self.assertIn("V", log.split("configured tiers:")[1])

    async def test_role_id_not_in_guild_logs_warning(self):
        m, log = await self._sync(roles_cfg={6: 600}, guild_roles=[])
        self.assertEqual(m.added, [])
        self.assertIn("was not found in this server", log)
        self.assertIn("600", log)

    async def test_role_above_bot_logs_hierarchy_warning(self):
        m, log = await self._sync(roles_cfg={6: 600}, guild_roles=[FakeRole(600, assignable=False)])
        self.assertEqual(m.added, [])
        self.assertIn("[PRESTIGE ROLE WARNING]", log)
        self.assertIn("role=600", log)

    async def test_add_roles_failure_logs_exception_type_and_ids(self):
        err = PermissionError("Missing Permissions")
        m, log = await self._sync(roles_cfg={6: 600}, guild_roles=[FakeRole(600)],
                                  member_kwargs={"add_roles_error": err})
        self.assertIn("Failed to add tier role: PermissionError: Missing Permissions", log)
        self.assertIn(f"guild={G1} user={USER} tier=VI role=600", log)

    async def test_no_prestige_means_no_warning(self):
        m, log = await self._sync(roles_cfg={}, guild_roles=[], effective=0)
        self.assertEqual((m.added, log), ([], ""))

    async def test_missing_context_is_logged_not_silent(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await prestige.sync_prestige_roles(None, None, None)
        self.assertIn("role sync skipped", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
