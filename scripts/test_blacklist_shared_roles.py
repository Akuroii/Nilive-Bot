"""Blacklisting a member must not revoke a role another system also justifies.

A Level reward role can also be owned through the Shop (inventory item,
temp_roles unit, timed_holdings row) or be the configured MVP / Booster /
Booster-colour / Prestige role. Those must survive a blacklist; a role that only
a fulfilled Level claim justifies must still be removed.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DASHBOARD_TESTING"] = "1"

from scripts.phase1_support import GUILD, USER, reset_database, execute  # noqa: E402
from scripts.test_member_blacklist_d3 import StubGuild, StubMember, StubRole  # noqa: E402
from utils.level_claims import blacklist_member, retry_blacklist_role_removal  # noqa: E402

ROLE = 505


def _iso(delta_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat()


class TestBlacklistSharedRoles(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        self.guild = StubGuild(GUILD)
        self.role = StubRole(ROLE, "Shared", position=10)
        self.guild.add_role(self.role)
        self.member = StubMember(USER, self.guild, roles=[self.role])
        self.guild.add_member(self.member)

    def _level_claim(self, role_id=ROLE):
        execute(
            "INSERT INTO level_reward_claims (guild_id, user_id, reward_level, track, "
            "reward_ref, payload_json, status, created_at, fulfilled_at) "
            "VALUES (?, ?, 5, 'role', ?, ?, 'fulfilled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (GUILD, USER, str(role_id), json.dumps({"role_id": role_id})))

    async def _blacklist(self):
        return await blacklist_member(GUILD, USER, member=self.member)

    async def test_control_level_only_role_is_removed(self):
        self._level_claim()
        res = await self._blacklist()
        self.assertEqual(res["removed_roles"], [ROLE])
        self.assertNotIn(self.role, self.member.roles)

    async def test_shop_inventory_role_is_kept(self):
        self._level_claim()
        execute("INSERT INTO inventory_items (guild_id, user_id, item_name, item_type, quantity, metadata, source) "
                "VALUES (?, ?, 'Shared VIP', 'role', 1, ?, 'shop')",
                (GUILD, USER, json.dumps({"role_id": ROLE})))
        res = await self._blacklist()
        self.assertIn(self.role, self.member.roles)
        self.assertEqual(res["removed_roles"], [])
        self.assertEqual([k["role_id"] for k in res["kept_roles"]], [ROLE])

    async def test_shop_temp_role_unit_is_kept(self):
        self._level_claim()
        execute("INSERT INTO temp_roles (guild_id, user_id, role_id, expires_at, source) VALUES (?, ?, ?, ?, 'shop')",
                (GUILD, USER, ROLE, _iso(5)))
        await self._blacklist()
        self.assertIn(self.role, self.member.roles)

    async def test_live_timed_holding_is_kept(self):
        self._level_claim()
        execute("INSERT INTO timed_holdings (guild_id, user_id, item_name, item_type, quantity, role_id, expires_at) "
                "VALUES (?, ?, 'Timed', 'role', 1, ?, ?)", (GUILD, USER, ROLE, _iso(5)))
        await self._blacklist()
        self.assertIn(self.role, self.member.roles)

    async def test_expired_timed_holding_does_not_protect(self):
        self._level_claim()
        execute("INSERT INTO timed_holdings (guild_id, user_id, item_name, item_type, quantity, role_id, expires_at) "
                "VALUES (?, ?, 'Timed', 'role', 1, ?, ?)", (GUILD, USER, ROLE, _iso(-5)))
        await self._blacklist()
        self.assertNotIn(self.role, self.member.roles)

    async def test_mvp_role_is_kept(self):
        self._level_claim()
        execute("INSERT INTO mvp_config (guild_id, mvp_role_id) VALUES (?, ?)", (GUILD, ROLE))
        await self._blacklist()
        self.assertIn(self.role, self.member.roles)

    async def test_booster_role_is_kept(self):
        self._level_claim()
        execute("INSERT INTO boost_config (guild_id, boost1_role_id) VALUES (?, ?)", (GUILD, ROLE))
        await self._blacklist()
        self.assertIn(self.role, self.member.roles)

    async def test_booster_colour_role_is_kept(self):
        self._level_claim()
        execute("INSERT INTO boost_color_roles (guild_id, role_id, role_name) VALUES (?, ?, 'c')", (GUILD, ROLE))
        await self._blacklist()
        self.assertIn(self.role, self.member.roles)

    async def test_prestige_role_is_kept(self):
        self._level_claim()
        execute("INSERT INTO prestige_roles (guild_id, tier, role_id) VALUES (?, 1, ?)", (GUILD, ROLE))
        await self._blacklist()
        self.assertIn(self.role, self.member.roles)

    async def test_pending_retention_obligation_is_kept(self):
        """A booster-retention row means the bot still owes the role (see timed_ownership)."""
        self._level_claim()
        execute("INSERT INTO timed_holdings (guild_id, user_id, item_name, item_type, quantity, source, role_id, expires_at) "
                "VALUES (?, ?, 'retention', 'role_retention', 1, 'booster_retention', ?, '9999-12-31T23:59:59+00:00')",
                (GUILD, USER, ROLE))
        await self._blacklist()
        self.assertIn(self.role, self.member.roles)

    async def test_retry_path_keeps_shop_owned_role(self):
        """The retry sweep shares the selector, so it cannot strip a Shop-owned role either."""
        execute("INSERT INTO leveling_member_blacklist (guild_id, user_id, reason, created_by) VALUES (?, ?, '', 0)",
                (GUILD, USER))
        self._level_claim()
        execute("INSERT INTO inventory_items (guild_id, user_id, item_name, item_type, quantity, metadata, source) "
                "VALUES (?, ?, 'Shared VIP', 'role', 1, ?, 'shop')",
                (GUILD, USER, json.dumps({"role_id": ROLE})))
        res = await retry_blacklist_role_removal(GUILD, USER, member=self.member)
        self.assertIn(self.role, self.member.roles)
        self.assertEqual([k["role_id"] for k in res["kept_roles"]], [ROLE])

    async def test_retry_path_removes_level_only_role(self):
        execute("INSERT INTO leveling_member_blacklist (guild_id, user_id, reason, created_by) VALUES (?, ?, '', 0)",
                (GUILD, USER))
        self._level_claim()
        res = await retry_blacklist_role_removal(GUILD, USER, member=self.member)
        self.assertEqual(res["removed_roles"], [ROLE])

    async def test_configured_but_unclaimed_role_is_not_removed(self):
        """No Level claim records this role, so Level cannot prove it granted it."""
        execute("INSERT INTO leveling_rewards (guild_id, level, role_id) VALUES (?, 5, ?)", (GUILD, ROLE))
        res = await self._blacklist()
        self.assertIn(self.role, self.member.roles)
        self.assertEqual(res["removed_roles"], [])


if __name__ == "__main__":
    unittest.main()
