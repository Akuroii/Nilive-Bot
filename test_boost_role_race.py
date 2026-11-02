"""Real SQLite/equip flow racing simulated Discord role removal."""
import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from phase1_support import GUILD, USER, reset_database, execute, rows, seed_member
from test_timed_ownership import purchase_ctx, role_stub
from cogs.boost import Boost
from utils.reward_engine import give_reward
from utils.equip_engine import get_equipped


class BoostRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        self.old = role_stub(9011, 'Old')
        self.new = role_stub(9012, 'New')
        self.itx, self.person, self.bot, self.guild = purchase_ctx(self.old, [self.old])
        self.guild.get_role = lambda rid: {9011: self.old, 9012: self.new}.get(int(rid))
        self.person.premium_since = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

        async def remove(*roles, reason=None):
            self.started.set()
            await self.release.wait()
            self.person.roles = [r for r in self.person.roles if r not in roles]

        async def add(*roles, reason=None):
            for role in roles:
                if role not in self.person.roles:
                    self.person.roles.append(role)
        self.person.remove_roles = AsyncMock(side_effect=remove)
        self.person.add_roles = AsyncMock(side_effect=add)

    def configure(self, site):
        if site == 'reaction':
            execute('INSERT INTO reaction_roles(guild_id,message_id,role_id,booster_only) VALUES (?,?,?,1)', (GUILD,1,self.old.id))
        if site in ('colour', 'switch'):
            execute('INSERT INTO boost_color_roles(guild_id,role_id,role_name,requires_boost_level) VALUES (?,?,?,1)', (GUILD,self.old.id,'Old'))
        if site == 'switch':
            execute('INSERT INTO boost_color_roles(guild_id,role_id,role_name,requires_boost_level) VALUES (?,?,?,1)', (GUILD,self.new.id,'New'))
            self.person.premium_since = datetime(2026,1,1,tzinfo=timezone.utc)
            self.person.premium_subscription_count = 1

    async def remove(self, site):
        if site == 'switch':
            return await Boost.boostcolor.callback(SimpleNamespace(bot=self.bot), self.itx, str(self.new.id))
        return await Boost._handle_unboost(SimpleNamespace(bot=self.bot), self.guild, self.person,
                                          self.old.id if site == 'reward' else None, None,
                                          {'auto_remove_on_unboost': 1})

    async def grant(self):
        return await give_reward(self.bot,GUILD,USER,'role',role_id=self.old.id,source='shop',item_name='Old')

    async def race(self, site):
        self.configure(site)
        removal = asyncio.create_task(self.remove(site))
        await asyncio.wait_for(self.started.wait(),5)
        grant = asyncio.create_task(self.grant())
        # Allow the real SQLite grant to reach its lock or finish on old code.
        try:
            await asyncio.wait_for(asyncio.shield(grant), 1)
        except asyncio.TimeoutError:
            pass
        finally:
            finished_in_window = grant.done()
            self.release.set()
        await asyncio.wait_for(removal,5)
        result = await asyncio.wait_for(grant,5)
        self.assertTrue(result['success'],result)
        self.assertEqual(rows("SELECT quantity FROM inventory_items WHERE guild_id=? AND user_id=? AND item_name='Old'", (GUILD,USER)),[(1,)])
        self.assertEqual((await get_equipped(GUILD,USER))['role_id'], self.old.id)
        self.assertIn(self.old,self.person.roles, 'Shop-owned and equipped role was removed')
        self.assertFalse(finished_in_window, 'grant completed before removal released the slot')

    async def test_race_reward(self): await self.race('reward')
    async def test_race_reaction(self): await self.race('reaction')
    async def test_race_colour(self): await self.race('colour')
    async def test_race_switch(self): await self.race('switch')

    async def test_reboost_while_retirement_waits_for_slot_lock_keeps_role(self):
        """Unboost event queued behind the slot lock; the member re-boosts before
        the lock is free. The stale retirement must not take the role away."""
        from utils.equip_engine import slot_lock
        self.release.set()
        gate = slot_lock(GUILD, USER)
        await gate.acquire()  # e.g. a Shop delivery / equip holding the slot
        try:
            retire = asyncio.create_task(self.remove('reward'))
            await asyncio.sleep(0.05)
            self.assertFalse(retire.done(), 'retirement did not wait for the lock')
            self.person.premium_since = datetime(2026, 11, 2, tzinfo=timezone.utc)  # re-boost
        finally:
            gate.release()
        await asyncio.wait_for(retire, 5)
        self.person.remove_roles.assert_not_awaited()
        self.assertIn(self.old, self.person.roles)

    async def test_still_unboosted_after_lock_wait_is_retired(self):
        """Control: with no re-boost the same queued retirement still removes it."""
        from utils.equip_engine import slot_lock
        self.release.set()
        gate = slot_lock(GUILD, USER)
        await gate.acquire()
        try:
            retire = asyncio.create_task(self.remove('reward'))
            await asyncio.sleep(0.05)
        finally:
            gate.release()
        await asyncio.wait_for(retire, 5)
        self.assertNotIn(self.old, self.person.roles)

    async def test_unboost_removes_without_other_owner(self):
        self.release.set()
        await self.remove('reward')
        self.assertNotIn(self.old,self.person.roles)

    async def test_unboost_keeps_shop_owner(self):
        self.assertTrue((await self.grant())['success'])
        self.release.set()
        await self.remove('reward')
        self.assertIn(self.old,self.person.roles)
        self.person.remove_roles.assert_not_awaited()

    async def test_colour_switch_completes(self):
        self.configure('switch'); self.release.set()
        await asyncio.wait_for(self.remove('switch'),5)
        self.assertNotIn(self.old,self.person.roles)
        self.assertIn(self.new,self.person.roles)

    async def test_colour_switch_keeps_shop_owner(self):
        self.configure('switch')
        self.assertTrue((await self.grant())['success'])
        self.release.set()
        await asyncio.wait_for(self.remove('switch'),5)
        self.assertIn(self.old,self.person.roles)
        self.assertIn(self.new,self.person.roles)
