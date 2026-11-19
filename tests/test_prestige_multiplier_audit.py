"""Prestige multiplier audit: real isolated SQLite, real Prestige lookup.

No Prestige/credit mocks. Each test seeds a permanent Prestige tier in the
`levels` table and multipliers in `prestige_tiers`, drives the production
reward path, then reads `economy` and `transaction_ledger` straight from the
database. Run: PYTHONPATH=$PWD python3 -m pytest -q tests/test_prestige_multiplier_audit.py
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ["OWNER_ID"] = "1"
os.environ.setdefault("SECRET_KEY", "isolated-test-only")
os.environ["DATABASE_PATH"] = f"/tmp/nilive-mult-import-{os.getpid()}.sqlite"

import aiosqlite
import database
# Import every DB-using module BEFORE any test patches DB_PATH, so each one is
# in sys.modules with the original path and is restored after every test.
import utils.currency, utils.daily_engine, utils.economy_safe, utils.inventory  # noqa: E401,F401
import utils.ledger, utils.level_claims, utils.level_ops, utils.pending_rewards  # noqa: E401,F401
import utils.prestige, utils.reward_engine, utils.streak_notifications  # noqa: E401,F401
import utils.streak_settings, utils.xp_calculator  # noqa: E401,F401
from utils.timezone import CAIRO_TZ

G1, G2, USER = 10, 11, 20
V = 5  # permanent Prestige V in these tests: Coins x1.2, Diamonds x1.1


class PrestigeMultiplierAudit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory(prefix="nilive-mult-")
        self.db_path = str(Path(self.tmp.name) / "iso.sqlite")
        original = database.DB_PATH
        self._patches = []
        for name, mod in list(sys.modules.items()):
            if mod is not None and getattr(mod, "DB_PATH", None) == original:
                p = patch.object(mod, "DB_PATH", self.db_path)
                p.start()
                self._patches.append(p)
        await database.init_db()
        from utils import prestige
        for guild in (G1, G2):
            await prestige.set_prestige_multipliers(guild, tiers={
                V: {"coins": 1.2, "diamonds": 1.1}})
        # USER is Prestige V in guild G1 only.
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO levels (guild_id, user_id, xp, level, prestige) "
                "VALUES (?,?,?,?,?)", (G1, USER, 0, 0, V))
            await db.commit()

    async def asyncTearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    async def rows(self, query, args=()):
        async with aiosqlite.connect(self.db_path) as db:
            return await (await db.execute(query, args)).fetchall()

    async def balance(self, guild, column="balance"):
        r = await self.rows(
            f"SELECT {column} FROM economy WHERE guild_id=? AND user_id=?",
            (guild, USER))
        return r[0][0] if r else 0

    async def ledger(self, guild, source=None):
        q = ("SELECT currency, amount, source FROM transaction_ledger "
             "WHERE guild_id=? AND user_id=?")
        args = [guild, USER]
        if source:
            q += " AND source=?"
            args.append(source)
        return await self.rows(q + " ORDER BY rowid", args)

    # ── Path 1: Daily / Streak (utils/daily_engine.py) ───────────────────
    async def test_daily_multiplier_once_and_display_matches_credit(self):
        from utils import daily_engine, streak_settings
        await streak_settings.save_streak_settings(G1, {
            "stages": [100] * 5, "stage6_min": 100, "stage6_max": 100})
        await streak_settings.save_streak_settings(G2, {
            "stages": [100] * 5, "stage6_min": 100, "stage6_max": 100})
        when = datetime(2026, 1, 5, 12, tzinfo=CAIRO_TZ)
        r1 = await daily_engine.perform_streak_claim(None, G1, USER, now=when)
        self.assertEqual(r1["pre_prestige_total"], 100)
        self.assertEqual(r1["multiplier"], 1.2)
        # displayed == credited == stored balance == ledger (x1.2 exactly once)
        self.assertEqual(r1["amount"], 120)
        self.assertEqual(r1["credited_total"], 120)
        self.assertEqual(await self.balance(G1), 120)
        self.assertEqual(await self.ledger(G1), [("balance", 120, "daily")])
        self.assertEqual(await self.balance(G1, "diamonds"), 0)
        # Same member, other guild: no Prestige there -> raw 100.
        r2 = await daily_engine.perform_streak_claim(None, G2, USER, now=when)
        self.assertEqual((r2["multiplier"], r2["amount"]), (1.0, 100))
        self.assertEqual(await self.balance(G2), 100)

    # ── Path 2: reward_engine.give_reward ────────────────────────────────
    async def test_give_reward_scales_positive_once_never_deductions(self):
        from utils.reward_engine import give_reward
        coins = await give_reward(None, G1, USER, "coins", 100, source="mission")
        self.assertEqual((coins["amount"], coins["new_balance"]), (120, 120))
        gems = await give_reward(None, G1, USER, "diamonds", 100, source="mission")
        self.assertEqual((gems["amount"], gems["new_balance"]), (110, 110))
        self.assertEqual(await self.ledger(G1),
                         [("balance", 120, "mission"), ("diamonds", 110, "mission")])
        spent = await give_reward(None, G1, USER, "coins", -50, source="mission")
        self.assertEqual(spent["amount"], -50)       # deduction not multiplied
        self.assertEqual(await self.balance(G1), 70)
        other = await give_reward(None, G2, USER, "coins", 100, source="mission")
        self.assertEqual(other["amount"], 100)       # guild-scoped tier

    # ── Path 3: level_claims (freeze at crossing, raw credit at claim) ───
    async def test_level_claim_multiplier_frozen_once_and_not_reapplied(self):
        from utils.reward_engine import give_reward
        from utils import level_claims
        from utils.xp_calculator import xp_for_level, xp_progress
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO leveling_currency_rewards (guild_id, level, currency, amount) "
                "VALUES (?,?,?,?)",
                [(G1, 1, "balance", 100), (G1, 1, "diamonds", 50)])
            await db.commit()
        xp = xp_for_level(1)
        self.assertEqual(xp_progress(xp)[0], 1)
        res = await give_reward(None, G1, USER, "xp", xp, source="mission")
        self.assertTrue(res.get("success"), res)
        # Crossing recorded with the multiplier frozen into the payload.
        payloads = await self.rows(
            "SELECT payload_json, status FROM level_reward_claims "
            "WHERE guild_id=? AND user_id=? AND track='currency'", (G1, USER))
        self.assertEqual(len(payloads), 2, payloads)
        self.assertTrue(all(s == "pending" for _, s in payloads), payloads)
        self.assertEqual(await self.balance(G1), 0)  # nothing paid before claim
        # A later multiplier change must NOT be re-applied at claim time.
        from utils import prestige
        await prestige.set_prestige_multipliers(G1, tiers={
            V: {"coins": 3.0, "diamonds": 3.0}})
        claimed = await level_claims.claim_available(G1, USER)
        self.assertEqual(claimed["delivered_currency"], 2, claimed)
        self.assertEqual(await self.balance(G1), 120)             # 100 x1.2
        self.assertEqual(await self.balance(G1, "diamonds"), 55)  # 50 x1.1
        self.assertEqual(
            sorted(await self.ledger(G1, "level_claim")),
            [("balance", 120, "level_claim"), ("diamonds", 55, "level_claim")])
        # Idempotent: a second claim pays nothing more.
        again = await level_claims.claim_available(G1, USER)
        self.assertEqual(again["delivered_currency"], 0)
        self.assertEqual(await self.balance(G1), 120)

    # ── Currency conversion is never multiplied ──────────────────────────
    async def test_conversion_receives_no_prestige_multiplier(self):
        from utils.economy_safe import safe_credit, safe_convert
        await safe_credit(G1, USER, 1000, currency="balance",
                          reason="seed", source="admin")
        before = await self.balance(G1, "diamonds")
        out = await safe_convert(G1, USER, 500, 100)   # 500 coins @ 100:1
        self.assertEqual(await self.balance(G1, "diamonds") - before, 5)
        self.assertEqual(await self.balance(G1), 500)
        self.assertEqual(out.get("diamonds_gained", 5), 5)  # not 5 x1.1

    # ── Disabled Prestige -> raw everywhere ──────────────────────────────
    async def test_disabled_prestige_is_raw_on_give_reward(self):
        from utils import prestige
        from utils.reward_engine import give_reward
        await prestige.set_prestige_multipliers(G1, enabled=False)
        r = await give_reward(None, G1, USER, "coins", 100, source="mission")
        self.assertEqual(r["amount"], 100)


if __name__ == "__main__":
    unittest.main()
