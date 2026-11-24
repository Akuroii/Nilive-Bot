"""Reproduction & regression tests for F-02:
Level-reward claims encountering an item-type/name conflict (`ItemTypeConflict`)
must NEVER become permanently `failed` or lose temporary-role duration while
blocked. Instead, they stay safely held in `pending` (with a deduplicated
`pending_rewards` pointer) and redeliver once when the conflict clears — for
both permanent (`custom`, `title`, `potion`) and temporary (`temp_role`) types.
"""
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from phase1_support import GUILD, USER, execute, rows, reset_database, seed_member  # noqa: E402
from utils import inventory, level_claims, pending_rewards, potion_engine, reward_engine  # noqa: E402

CAPE_ROLE = 9801
POTION_META = potion_engine.build_metadata("xp_boost", 2.0, 2)


class StubRole:
    def __init__(self, role_id: int, name: str, position: int = 3):
        self.id = int(role_id)
        self.name = name
        self.position = position
        self.mention = f"<@&{role_id}>"
        self.managed = False

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.position >= other.position


def make_env():
    cape = StubRole(CAPE_ROLE, "Cape")
    bot_top = StubRole(9999, "BotTop", position=50)
    member = SimpleNamespace(
        id=USER,
        roles=[],
        premium_since=None,
        bot=False,
    )

    async def add_roles(role, reason=None):
        if role not in member.roles:
            member.roles.append(role)

    async def remove_roles(role, reason=None):
        if role in member.roles:
            member.roles.remove(role)

    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)
    guild = SimpleNamespace(
        id=GUILD,
        owner_id=1,
        me=SimpleNamespace(
            id=777,
            top_role=bot_top,
            guild_permissions=SimpleNamespace(manage_roles=True, administrator=False),
        ),
        get_role=lambda rid: cape if int(rid) == CAPE_ROLE else None,
        get_member=lambda uid: member if int(uid) == USER else None,
        fetch_member=AsyncMock(side_effect=lambda uid: member if int(uid) == USER else None),
    )
    member.guild = guild
    bot = SimpleNamespace(get_guild=lambda gid: guild if int(gid) == GUILD else None)
    return bot, guild, member, cape


def seed_shop_reward(level: int, name: str, item_type: str, quantity: int = 1,
                     *, role_id: int | None = None,
                     duration_hours: int | None = None,
                     multiplier: float | None = None) -> int:
    item_id = execute(
        "INSERT INTO shop_items (guild_id, name, price, type, role_id, "
        "duration_hours, max_stock, current_stock, enabled, xp_boost_multiplier) "
        "VALUES (?, ?, 100, ?, ?, ?, 10, 10, 1, ?)",
        (GUILD, name, item_type, role_id, duration_hours, multiplier),
    )
    execute(
        "INSERT INTO leveling_shop_rewards (guild_id, level, item_id, quantity) "
        "VALUES (?, ?, ?, ?)",
        (GUILD, level, item_id, quantity),
    )
    return item_id


class F02LevelClaimConflictTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(user=USER, guild=GUILD, balance=100, tier=0)
        execute(
            "UPDATE levels SET xp=0, level=0, prestige=0 WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        )
        self.bot, self.guild, self.member, self.cape = make_env()

    async def test_f02_permanent_level_claims_held_pending_on_conflict_and_redelivered_via_retry(self):
        # Level 1 rewards: Title "Tonic" (2x) and Collectible "Crown" (1x)
        seed_shop_reward(1, "Tonic", "title", quantity=2)
        seed_shop_reward(1, "Crown", "custom", quantity=1)
        await reward_engine.give_reward(
            self.bot, GUILD, USER, "xp", amount=150, reason="play", source="test"
        )

        # Member already owns conflicting items under both names:
        # "Tonic" as a potion and "Crown" as a title.
        await inventory.give_item(
            GUILD, USER, "Tonic", 1, item_type="potion", metadata=POTION_META
        )
        await inventory.give_item(
            GUILD, USER, "Crown", 1, item_type="title"
        )

        first = await level_claims.claim_available(
            GUILD, USER, member=self.member, bot=self.bot
        )
        self.assertEqual(first["fulfilled"], [])
        self.assertEqual(first["failed"], [], "conflicted claims must NOT be marked failed")
        self.assertEqual(len(first.get("held_pending") or []), 2)

        statuses = rows(
            "SELECT status FROM level_reward_claims WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        )
        self.assertEqual(statuses, [("pending",), ("pending",)])

        # Held in pending_rewards (deduplicated even if Claim All is clicked again)
        await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
        held_rows = await pending_rewards.list_pending(GUILD, USER)
        self.assertEqual(len(held_rows), 2)

        # Free "Tonic" (consume potion) while "Crown" stays conflicted; run pending_rewards.retry()
        await inventory.remove_item(GUILD, USER, "Tonic", 1)
        retry_1 = await pending_rewards.retry(self.bot, GUILD, USER)
        self.assertEqual(retry_1["delivered"], 1)
        self.assertEqual(retry_1["still_pending"], 1)
        self.assertEqual(
            rows("SELECT item_type, quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Tonic'", (GUILD, USER)),
            [("title", 2)],
        )

        # Free "Crown" and click Claim All directly -> delivers "Crown" once and settles pending_rewards!
        await inventory.remove_item(GUILD, USER, "Crown", 1)
        second = await level_claims.claim_available(
            GUILD, USER, member=self.member, bot=self.bot
        )
        self.assertEqual(len(second["fulfilled"]), 1)
        self.assertEqual(
            rows("SELECT item_type, quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Crown'", (GUILD, USER)),
            [("shop_custom", 1)],
        )
        self.assertEqual(await pending_rewards.list_pending(GUILD, USER), [])

        # Subsequent retry or Claim All never double-delivers
        await pending_rewards.retry(self.bot, GUILD, USER)
        await level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot)
        self.assertEqual(
            rows("SELECT item_type, quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Tonic'", (GUILD, USER)),
            [("title", 2)],
        )

    async def test_f02_temp_role_level_claim_does_not_grant_role_or_start_timer_on_conflict_and_redelivers_fresh(self):
        # Level 1 reward: temp_role "Cape" (role_id=CAPE_ROLE, duration=24h)
        seed_shop_reward(
            1, "Cape", "temp_role", quantity=1, role_id=CAPE_ROLE, duration_hours=24
        )
        await reward_engine.give_reward(
            self.bot, GUILD, USER, "xp", amount=150, reason="play", source="test"
        )

        # Member currently holds a potion named "Cape" -> name/type conflict!
        await inventory.give_item(
            GUILD, USER, "Cape", 1, item_type="potion", metadata=POTION_META
        )

        blocked_res = await level_claims.claim_available(
            GUILD, USER, member=self.member, bot=self.bot
        )
        self.assertEqual(blocked_res["fulfilled"], [])
        self.assertEqual(blocked_res["failed"], [])
        self.assertEqual(len(blocked_res.get("held_pending") or []), 1)

        # Crucial: Discord role was NOT added and temp_roles timer was NOT started!
        self.assertEqual([r.id for r in self.member.roles], [])
        self.assertEqual(
            rows("SELECT COUNT(*) FROM temp_roles WHERE guild_id=? AND user_id=?", (GUILD, USER)),
            [(0,)],
        )
        self.assertEqual(
            rows("SELECT status FROM level_reward_claims WHERE guild_id=? AND user_id=?", (GUILD, USER)),
            [("pending",)],
        )

        # Free the conflicting potion and trigger automatic redelivery via pending_rewards.retry()
        await inventory.remove_item(GUILD, USER, "Cape", 1)
        before_delivery = datetime.now(timezone.utc)
        redelivered = await pending_rewards.retry(self.bot, GUILD, USER)
        self.assertEqual(redelivered["delivered"], 1)

        # Role is now on Discord, temp_roles timer started fresh at redelivery, claim fulfilled!
        self.assertEqual([r.id for r in self.member.roles], [CAPE_ROLE])
        tr = rows(
            "SELECT expires_at FROM temp_roles WHERE guild_id=? AND user_id=? AND role_id=?",
            (GUILD, USER, CAPE_ROLE),
        )
        self.assertEqual(len(tr), 1)
        exp_dt = datetime.fromisoformat(tr[0][0])
        self.assertGreaterEqual((exp_dt - before_delivery).total_seconds(), 23.9 * 3600)
        self.assertEqual(
            rows("SELECT item_type, quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Cape'", (GUILD, USER)),
            [("temp_role", 1)],
        )
        self.assertEqual(
            rows("SELECT status FROM level_reward_claims WHERE guild_id=? AND user_id=?", (GUILD, USER)),
            [("fulfilled",)],
        )
        self.assertEqual(await pending_rewards.list_pending(GUILD, USER), [])

    async def test_f02_potion_conflict_concurrent_redelivery_race_and_arabic_ui_summary(self):
        from cogs.leveling import claim_confirmation, claim_result_footer
        # Level 1 rewards: 100 Coins + 2x Potion "Elixir" (2.0x, 2h)
        execute(
            "INSERT INTO leveling_currency_rewards (guild_id, level, currency, amount) "
            "VALUES (?, 1, 'balance', 100)",
            (GUILD,),
        )
        seed_shop_reward(
            1, "Elixir", "potion", quantity=2, duration_hours=2, multiplier=2.0
        )
        await reward_engine.give_reward(
            self.bot, GUILD, USER, "xp", amount=150, reason="play", source="test"
        )
        # Member already owns a Title named "Elixir"
        await inventory.give_item(GUILD, USER, "Elixir", 1, item_type="title")

        res = await level_claims.claim_available(
            GUILD, USER, member=self.member, bot=self.bot
        )
        # Coins delivered, Potion held pending (never failed!)
        self.assertEqual(len(res["fulfilled"]), 1)
        self.assertEqual(res["failed"], [])
        self.assertEqual(len(res["held_pending"]), 1)

        claims = await level_claims.list_claims(GUILD, USER)
        footer = claim_result_footer(res)
        self.assertIn("محفوظة كمعلقة", footer)
        conf = await claim_confirmation(self.guild, res, claims, False)
        self.assertIsNotNone(conf)
        self.assertIn("محفوظة كمعلقة", conf[1])

        # Free the conflicting Title and fire retry() + claim_available() concurrently
        await inventory.remove_item(GUILD, USER, "Elixir", 1)
        r_retry, r_claim = await asyncio.gather(
            pending_rewards.retry(self.bot, GUILD, USER),
            level_claims.claim_available(GUILD, USER, member=self.member, bot=self.bot),
        )
        # Delivered at most once total!
        self.assertEqual(
            rows("SELECT item_type, quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Elixir'", (GUILD, USER)),
            [("potion", 2)],
        )
        self.assertEqual(await pending_rewards.list_pending(GUILD, USER), [])

    async def test_f02_admin_reset_and_staff_review_keep_pending_and_claims_synchronized(self):
        # 1) Conflicted claim parked in pending_rewards -> admin deletes/resets level_reward_claims
        #    -> list_pending() auto-settles the orphan pointer and returns [] immediately!
        seed_shop_reward(1, "Relic", "title", quantity=1)
        await reward_engine.give_reward(
            self.bot, GUILD, USER, "xp", amount=150, reason="play", source="test"
        )
        await inventory.give_item(
            GUILD, USER, "Relic", 1, item_type="potion", metadata=POTION_META
        )
        res = await level_claims.claim_available(
            GUILD, USER, member=self.member, bot=self.bot
        )
        self.assertEqual(len(res["held_pending"]), 1)
        self.assertEqual(len(await pending_rewards.list_pending(GUILD, USER)), 1)

        execute("DELETE FROM level_reward_claims WHERE guild_id=? AND user_id=?", (GUILD, USER))
        self.assertEqual(await pending_rewards.list_pending(GUILD, USER), [])
        self.assertEqual(
            rows("SELECT status FROM pending_rewards WHERE guild_id=? AND user_id=?", (GUILD, USER)),
            [("delivered",)],
        )

        # 2) Staff resolve_review(..., action='delivered') on a needs_review level_claim
        #    marks level_reward_claims fulfilled so Claim All cannot deliver a 2nd copy!
        await inventory.remove_item(GUILD, USER, "Relic", 1)
        execute("DELETE FROM pending_rewards WHERE guild_id=? AND user_id=?", (GUILD, USER))
        execute("DELETE FROM levels WHERE guild_id=? AND user_id=?", (GUILD, USER))
        seed_shop_reward(2, "Halo", "title", quantity=1)
        await reward_engine.give_reward(
            self.bot, GUILD, USER, "xp", amount=500, reason="play", source="test"
        )
        await inventory.give_item(
            GUILD, USER, "Halo", 1, item_type="potion", metadata=POTION_META
        )
        res2 = await level_claims.claim_available(
            GUILD, USER, member=self.member, bot=self.bot
        )
        halo_claim_id = [
            c["id"] for c in await level_claims.list_claims(GUILD, USER)
            if c["reward_level"] == 2
        ][0]
        self.assertIn(halo_claim_id, res2["held_pending"])
        pr_rows = await pending_rewards.list_pending(GUILD, USER)
        halo_pr_id = [p["id"] for p in pr_rows if p["payload"].get("claim_id") == halo_claim_id][0]

        # Simulate mid-delivery crash leaving level_reward_claims in stale 'processing'
        # and pending_rewards in 'needs_review'; staff marks 'delivered' after verifying grant
        await inventory.remove_item(GUILD, USER, "Halo", 1)
        execute(
            "UPDATE level_reward_claims SET status='processing', owner_token='crashed-worker', "
            "processing_started_at='2000-01-01 00:00:00' WHERE id=?",
            (halo_claim_id,),
        )
        execute("UPDATE pending_rewards SET status='needs_review' WHERE id=?", (halo_pr_id,))
        resolved = await pending_rewards.resolve_review(GUILD, halo_pr_id, "delivered", actor="admin")
        self.assertTrue(resolved)
        self.assertEqual(
            rows("SELECT status, owner_token, processing_started_at FROM level_reward_claims WHERE id=?", (halo_claim_id,)),
            [("fulfilled", None, None)],
        )
        # Subsequent Claim All cannot reclaim the expired 'processing' row or deliver Halo twice!
        after_claim = await level_claims.claim_available(
            GUILD, USER, member=self.member, bot=self.bot, claim_ids=[halo_claim_id]
        )
        self.assertEqual(after_claim["fulfilled"], [])
        self.assertEqual(
            rows("SELECT COUNT(*) FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Halo' AND quantity > 0", (GUILD, USER)),
            [(0,)],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

