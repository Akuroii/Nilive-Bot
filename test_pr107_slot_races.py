"""Race/lifecycle regressions adapted from PR #107; scratch SQLite and Discord stubs."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from phase1_support import GUILD, USER, execute, rows, reset_database, seed_member, member
from test_timed_ownership import role_stub, purchase_ctx, purge_timed, iso_in, shop_item
from utils import timed_ownership as TO, equip_engine as EE
from utils.inventory import get_inventory, find_entry, set_quantity
from utils.equip_engine import get_equipped, equip_role
from utils.reward_engine import give_reward

class SameRoleSlotRaceTests(unittest.IsolatedAsyncioTestCase):
    """One Discord role, two holdings, two slot operations in flight.

    The equip slot is a *single Discord role*, so `equip_role` and `unequip_role`
    cannot decide independently: `equip_role` skips `add_roles` when it sees the
    role already worn, and `unequip_role` removes the role and only afterwards
    writes the slot. Interleaved on the same member, the stale removal strips a
    role the newer equip deliberately left in place: the durable slot names an
    equip the member does not wear. The conditional DELETE cannot help - it only
    decides which row survives, not what Discord shows.
    """

    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=5000)
        purge_timed()
        for table in ("equipped_roles", "inventory_items", "timed_holdings"):
            execute(f"DELETE FROM {table}")

    async def _same_role_pair(self, role_id):
        """One permanent and one timed holding of the SAME Discord role."""
        role = role_stub(role_id, "SameRoleItem")
        _, person, bot, guild = purchase_ctx(granted_role=role)
        guild.get_role = lambda rid: role if int(rid) == int(role_id) else None
        person.roles = [role]

        added = []

        async def _add(target, reason=None):
            added.append(int(target.id))
            if target not in person.roles:
                person.roles.append(target)

        person.add_roles = AsyncMock(side_effect=_add)

        execute("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,"
                "quantity,metadata,source) VALUES (?,?,'SameRolePerm','role',1,?,'shop')",
                (GUILD, USER, f'{{"role_id": {int(role_id)}}}'))
        inv_id = rows("SELECT id FROM inventory_items WHERE guild_id=? AND user_id=?"
                      " AND item_name='SameRolePerm'", (GUILD, USER))[0][0]
        permanent_key = f"i:{inv_id}"

        holding_id = await TO.grant_timed_holding(
            GUILD, USER, "SameRoleTimed", expires_at=iso_in(5),
            item_type="temp_role", metadata={"role_id": int(role_id)},
            role_id=int(role_id), source="shop")
        timed_key = f"h:{holding_id}"

        return {"role": role, "person": person, "bot": bot, "guild": guild,
                "added": added, "permanent": permanent_key, "timed": timed_key,
                "item_permanent": "SameRolePerm", "item_timed": "SameRoleTimed"}

    async def _wait_in_window(self, task, budget=1.5):
        """Let `task` finish inside the current await window if it can.

        Returns True when the concurrent operation completed while the caller
        was suspended - which the unfixed code allows and a serialised
        implementation must not - and False when it is still waiting its turn.
        """
        import asyncio
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=budget)
            return True
        except asyncio.TimeoutError:
            return False

    async def test_equip_of_the_same_role_during_an_unequip_keeps_it_worn(self):
        """The reported race: the surviving slot must match what Discord shows."""
        import asyncio
        from utils.equip_engine import record_equipped, unequip_role, equip_role

        ctx = await self._same_role_pair(9631)
        role, person = ctx["role"], ctx["person"]
        await record_equipped(GUILD, USER, ctx["item_permanent"],
                              role_id=9631, entry_key=ctx["permanent"])

        window = {}

        async def _racing_remove(target, reason=None):
            # A legitimate equip of ANOTHER holding of the same role, exactly as
            # a second click / another device would issue it.
            task = asyncio.create_task(
                equip_role(ctx["bot"], GUILD, USER, ctx["item_timed"],
                           entry_key=ctx["timed"]))
            window["task"] = task
            window["finished_in_window"] = await self._wait_in_window(task)
            person.roles = [r for r in person.roles if r.id != target.id]

        person.remove_roles = AsyncMock(side_effect=_racing_remove)

        result = await unequip_role(ctx["bot"], GUILD, USER,
                                    ctx["item_permanent"],
                                    entry_key=ctx["permanent"])
        equip_result = await window["task"]

        slot = await get_equipped(GUILD, USER)
        self.assertTrue(equip_result.get("success"), equip_result)
        self.assertEqual(slot["entry_key"], ctx["timed"],
                         "the slot must belong to the holding that was equipped")
        self.assertIn(
            role, person.roles,
            "the database records the role as equipped but the member does not "
            f"wear it (concurrent equip completed in the window: "
            f"{window['finished_in_window']})")
        self.assertEqual(ctx["added"], [9631],
                         "the surviving equip had to re-add the role itself")
        # The unequip clears the entry it validated BEFORE the queued grant
        # records the new holding, so it succeeds; the grant owns the slot. A
        # `success or error` style check would also accept the old behaviour
        # (refused because the slot had changed), which is the opposite result.
        self.assertTrue(result["success"], result)
        self.assertFalse(
            window["finished_in_window"],
            "the concurrent equip ran to completion inside the unequip's Discord "
            "await, so nothing serialises the two operations")
        # The stale unequip must never claim it removed the new holding.
        self.assertFalse(
            result.get("success") and result.get("item_name") == ctx["item_timed"],
            result)

    async def test_a_later_unequip_still_clears_the_serialised_slot(self):
        """The fix must not leave a slot (or role) that can no longer be cleared."""
        import asyncio
        from utils.equip_engine import record_equipped, unequip_role, equip_role

        ctx = await self._same_role_pair(9632)
        role, person = ctx["role"], ctx["person"]
        await record_equipped(GUILD, USER, ctx["item_permanent"],
                              role_id=9632, entry_key=ctx["permanent"])

        window = {}

        async def _racing_remove(target, reason=None):
            task = asyncio.create_task(
                equip_role(ctx["bot"], GUILD, USER, ctx["item_timed"],
                           entry_key=ctx["timed"]))
            window["task"] = task
            await self._wait_in_window(task)
            person.roles = [r for r in person.roles if r.id != target.id]

        person.remove_roles = AsyncMock(side_effect=_racing_remove)
        await unequip_role(ctx["bot"], GUILD, USER, ctx["item_permanent"],
                           entry_key=ctx["permanent"])
        await window["task"]

        # The racing stub exists to create exactly ONE concurrent equip. Left
        # installed, the second unequip would spawn a second equip task that
        # nothing awaits: it races this test's own assertions, and when the
        # event loop closes with it still pending, the aiosqlite connection it
        # opened is never closed - its non-daemon worker thread then keeps the
        # interpreter alive at shutdown (the suite hangs after printing its
        # summary). Restore a plain stub before the follow-up unequip.
        person.remove_roles = AsyncMock(
            side_effect=lambda target, reason=None: person.roles.remove(target)
            if target in person.roles else None)

        later = await unequip_role(ctx["bot"], GUILD, USER, ctx["item_timed"],
                                   entry_key=ctx["timed"])

        self.assertTrue(later.get("success"), later)
        self.assertIsNone(await get_equipped(GUILD, USER))
        self.assertNotIn(role, person.roles,
                         "the later unequip left the role worn with no slot")

class SlotConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Every role-bearing path that decides "is the role worn?" needs one gate.

    `equip_role` and `unequip_role` are serialised per member, but a timed grant,
    an expiry and the legacy temp-role sweep are the same kind of operation: each
    reads the member's roles, acts on Discord, and writes the single
    `equipped_roles` row. Outside the same lock they interleave inside each
    other's Discord await, and the durable slot ends up naming an equip the
    member does not wear - a state no read path repairs, because the later
    operation already saw the role present and skipped adding it.
    """

    WINDOW = 0.75   # how long a competing operation is watched for

    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=5000)
        purge_timed()
        for table in ("equipped_roles", "inventory_items", "timed_holdings",
                      "temp_roles"):
            execute(f"DELETE FROM {table}")

    # ── fixtures ────────────────────────────────────────────────────────

    def _world(self, role_ids, worn=()):
        roles = {rid: role_stub(rid, f"SlotRole{rid}") for rid in role_ids}
        person = member()
        person.roles = [roles[rid] for rid in worn]
        person.add_roles = AsyncMock()
        person.remove_roles = AsyncMock()
        guild = SimpleNamespace(
            id=GUILD, owner_id=99,
            me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
            get_role=lambda rid: roles.get(int(rid)),
            get_member=lambda uid: person if uid == person.id else None,
        )
        bot = SimpleNamespace(
            get_guild=lambda gid: guild if gid == GUILD else None)
        return person, guild, bot, roles

    def _faithful_roles(self, person, gate):
        """add_roles/remove_roles that really update `person.roles`.

        `gate(kind, role)` is awaited at the very start of the Discord call, so a
        test can hold that operation inside its own await window.
        """
        async def _add(role, reason=None):
            await gate("add", role)
            if role not in person.roles:
                person.roles.append(role)

        async def _remove(role, reason=None):
            await gate("remove", role)
            person.roles = [r for r in person.roles if r.id != role.id]

        person.add_roles = AsyncMock(side_effect=_add)
        person.remove_roles = AsyncMock(side_effect=_remove)

    async def _holding(self, role_id, name, *, expired=False):
        holding_id = await TO.grant_timed_holding(
            GUILD, USER, name,
            expires_at=(iso_in(-2) if expired else iso_in(5)),
            item_type="temp_role", metadata={"role_id": int(role_id)},
            role_id=int(role_id), source="shop")
        return holding_id, f"h:{holding_id}"

    def _permanent(self, role_id, name):
        execute("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,"
                "quantity,metadata,source) VALUES (?,?,?,'role',1,?,'shop')",
                (GUILD, USER, name, json.dumps({"role_id": int(role_id)})))
        inv_id = rows("SELECT id FROM inventory_items WHERE guild_id=? AND user_id=?"
                      " AND item_name=?", (GUILD, USER, name))[0][0]
        return f"i:{inv_id}"

    async def _settle(self, task):
        """True when `task` completed while the caller was suspended."""
        try:
            await asyncio.wait_for(asyncio.shield(task),
                                   timeout=self.WINDOW)
            return True
        except asyncio.TimeoutError:
            return False

    def _slot(self):
        found = rows("SELECT item_name, entry_key, role_id FROM equipped_roles")
        return found[0] if found else None

    def _worn_ids(self, person):
        return {r.id for r in person.roles}

    async def test_expiry_blocks_permanent_reward_before_ownership_write(self):
        person, guild, bot, roles = self._world([9640], worn=[9640])
        old_id, old_key = await self._holding(9640, "ExpiredReward", expired=True)
        await EE.record_equipped(GUILD, USER, "ExpiredReward", role_id=9640,
                                 entry_key=old_key)
        seen = {}

        async def gate(kind, target):
            if kind == "remove":
                seen["grant"] = asyncio.create_task(give_reward(
                    bot, GUILD, USER, "role", role_id=9640,
                    item_name="NewReward", source="event"))
                seen["ran"] = await self._settle(seen["grant"])
                seen["rows_during"] = rows(
                    "SELECT quantity FROM inventory_items WHERE item_name='NewReward'")

        self._faithful_roles(person, gate)
        result = await TO.expire_one(bot, (await TO.expired_holdings(limit=None))[0])
        grant = await seen["grant"]
        self.assertFalse(seen["ran"])
        self.assertEqual(seen["rows_during"], [])
        self.assertEqual(result["status"], "removed")
        self.assertTrue(grant["success"], grant)
        self.assertIn(roles[9640], person.roles)
        self.assertEqual(self._slot()[1], grant["entry_key"])

    # ── R1: timed grant vs an in-flight unequip ─────────────────────────

    async def test_a_timed_grant_keeps_the_role_it_equipped(self):
        person, guild, bot, roles = self._world([9641], worn=[9641])
        _, owner_key = await self._holding(9641, "SlotOwner")
        await EE.record_equipped(GUILD, USER, "SlotOwner", role_id=9641,
                              entry_key=owner_key)
        seen = {}

        async def gate(kind, target):
            if kind != "remove":
                return
            seen["grant"] = asyncio.create_task(TO.grant_timed_product(
                bot, GUILD, USER, item_name="SlotRebuy", duration_hours=5,
                item_type="temp_role", role_id=9641, source="shop"))
            seen["ran_in_window"] = await self._settle(seen["grant"])

        self._faithful_roles(person, gate)
        result = await EE.unequip_role(bot, GUILD, USER, "SlotOwner",
                                    entry_key=owner_key)
        granted = await seen["grant"]

        slot = self._slot()
        self.assertTrue(granted["success"], granted)
        self.assertIsNotNone(slot, "no slot survived the pair of operations")
        self.assertEqual(slot[1], granted["entry_key"],
                         "the durable slot must name the newly granted holding")
        self.assertIn(roles[9641], person.roles,
                      "the slot names a holding whose role the member does not "
                      "wear")

        self.assertFalse(
            seen["ran_in_window"],
            "the grant ran to completion inside the unequip's Discord await")

    # ── R2: expiry vs a rebuy of the same role ──────────────────────────

    async def test_expiry_does_not_strip_the_role_a_rebuy_now_needs(self):
        person, guild, bot, roles = self._world([9661], worn=[9661])
        old_id, old_key = await self._holding(9661, "SlotExpired", expired=True)
        await EE.record_equipped(GUILD, USER, "SlotExpired", role_id=9661,
                              entry_key=old_key)
        seen = {}

        async def gate(kind, target):
            if kind != "remove":
                return
            seen["grant"] = asyncio.create_task(TO.grant_timed_product(
                bot, GUILD, USER, item_name="SlotRebuy", duration_hours=5,
                item_type="temp_role", role_id=9661, source="shop"))
            seen["ran_in_window"] = await self._settle(seen["grant"])

        self._faithful_roles(person, gate)
        holding = (await TO.expired_holdings(limit=None))[0]
        outcome = await TO.expire_one(bot, holding)
        granted = await seen["grant"]

        self.assertEqual(outcome["status"], "removed", outcome)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE id=?", (old_id,)),
            [(0,)], "the expired holding must still be retired")
        self.assertTrue(granted["success"], granted)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE id=?",
                 (int(granted["entry_key"].split(":")[1]),)),
            [(1,)], "the new paid holding must remain live")
        self.assertEqual(self._slot()[1], granted["entry_key"],
                         "the slot must name the live holding")
        self.assertIn(roles[9661], person.roles,
                      "expiry stripped the role the new holding needs")
        self.assertFalse(
            seen["ran_in_window"],
            "the rebuy ran to completion inside the expiry's Discord await")

    # ── R3: a locked equip vs the timed-grant swap ──────────────────────

    async def test_an_equip_does_not_orphan_a_role_against_a_timed_grant(self):
        person, guild, bot, roles = self._world([9651, 9652, 9653],
                                                worn=[9651])
        await EE.record_equipped(GUILD, USER, "SlotA", role_id=9651,
                              entry_key="i:a")
        perm = self._permanent(9652, "SlotB")

        async def _noop(kind, role):
            return None

        self._faithful_roles(person, _noop)

        release = asyncio.Event()
        stalled = asyncio.Event()
        real_record = EE.record_equipped
        calls = {"n": 0}

        async def _record(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # The grant has already read the old slot inside
                # swap_and_record_equipped; hold it at its slot write.
                stalled.set()
                await release.wait()
            return await real_record(*args, **kwargs)

        with patch.object(EE, "record_equipped", side_effect=_record):
            grant = asyncio.create_task(TO.grant_timed_product(
                bot, GUILD, USER, item_name="SlotTimed", duration_hours=5,
                item_type="temp_role", role_id=9653, source="shop"))
            await asyncio.wait_for(stalled.wait(), timeout=2)
            equip = asyncio.create_task(equip_role(
                bot, GUILD, USER, "SlotB", entry_key=perm))
            ran_in_window = await self._settle(equip)
            release.set()
            granted, equipped = await asyncio.gather(grant, equip)

        self.assertTrue(granted["success"], granted)
        self.assertTrue(equipped["success"], equipped)
        slot = self._slot()
        self.assertIsNotNone(slot, "no slot survived the pair of operations")
        self.assertEqual(
            self._worn_ids(person), {slot[2]},
            "exactly the role the durable slot names must be worn; worn="
            f"{sorted(self._worn_ids(person))}, slot role={slot[2]}")
        self.assertFalse(
            ran_in_window,
            "the equip ran to completion inside the grant's slot write")

    # ── R4: expiry vs a permanent same-role equip ───────────────────────

    async def test_expiry_does_not_strip_a_role_a_permanent_equip_selected(self):
        person, guild, bot, roles = self._world([9671], worn=[9671])
        old_id, old_key = await self._holding(9671, "SlotExpiredPerm",
                                              expired=True)
        await EE.record_equipped(GUILD, USER, "SlotExpiredPerm", role_id=9671,
                              entry_key=old_key)
        seen = {}

        async def gate(kind, target):
            if kind != "remove":
                return
            # The permanent entry is bought now, inside the expiry's await: it
            # did not exist when expiry made its entitlement decision.
            seen["perm"] = self._permanent(9671, "SlotPermSameRole")
            seen["equip"] = asyncio.create_task(equip_role(
                bot, GUILD, USER, "SlotPermSameRole", entry_key=seen["perm"]))
            seen["ran_in_window"] = await self._settle(seen["equip"])

        self._faithful_roles(person, gate)
        holding = (await TO.expired_holdings(limit=None))[0]
        outcome = await TO.expire_one(bot, holding)
        equipped = await seen["equip"]

        self.assertEqual(outcome["status"], "removed", outcome)
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE id=?", (old_id,)),
            [(0,)], "the expired holding must still be retired")
        self.assertEqual(self._slot()[1], seen["perm"],
                         "the permanent entry must own the slot")
        self.assertIn(roles[9671], person.roles,
                      "expiry stripped the role the permanent entry needs")
        self.assertFalse(
            seen["ran_in_window"],
            "the equip ran to completion inside the expiry's Discord await")

    # ── R5: the legacy temp-role sweep ──────────────────────────────────

    async def test_the_legacy_temp_role_sweep_keeps_a_concurrent_grant_alive(self):
        person, guild, bot, roles = self._world([9681], worn=[9681])
        execute("INSERT INTO temp_roles (guild_id,user_id,role_id,expires_at,source)"
                " VALUES (?,?,?,?,'shop')", (GUILD, USER, 9681, iso_in(-2)))
        seen = {}

        async def gate(kind, target):
            if kind != "remove":
                return
            seen["grant"] = asyncio.create_task(TO.grant_timed_product(
                bot, GUILD, USER, item_name="SlotSweepRebuy", duration_hours=5,
                item_type="temp_role", role_id=9681, source="shop"))
            seen["ran_in_window"] = await self._settle(seen["grant"])

        self._faithful_roles(person, gate)
        from cogs.shop import Shop

        class _CogShim:
            """`self` for the real sweep coroutine, with no cog scaffolding.

            The coroutine is run on both source layouts: before the refactor the
            whole per-row body was inline in `temp_role_cleanup`, afterwards it
            calls `_retire_expired_temp_role`. Binding the helper only when the
            implementation has it keeps the failure against the old tree
            behavioural (the role really is stripped) instead of an
            AttributeError from the fixture.
            """

            def __init__(self, bot):
                self.bot = bot

        if hasattr(Shop, "_retire_expired_temp_role"):
            _CogShim._retire_expired_temp_role = Shop._retire_expired_temp_role

        await Shop.temp_role_cleanup.coro(_CogShim(bot))
        granted = await seen["grant"]

        self.assertEqual(rows("SELECT COUNT(*) FROM temp_roles"), [(0,)],
                         "the swept legacy row must still be deleted")
        self.assertTrue(granted["success"], granted)
        self.assertEqual(self._slot()[1], granted["entry_key"],
                         "the slot must name the live holding")
        self.assertIn(roles[9681], person.roles,
                      "the legacy sweep stripped a role a live holding needs")
        self.assertFalse(
            seen["ran_in_window"],
            "the grant ran to completion inside the sweep's Discord await")

class SlotLockLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """The slot gate must be released by every exit path, and stay per member.

    These are guards for the coordination added around grants, expiry and the
    legacy sweep; they do not reproduce a defect on the pre-lock code (which had
    no gate to leak). They exist because a lock held across Discord awaits is
    only safe if an exception, a cancellation, or a different member's work
    leaves it usable.
    """

    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=5000)
        purge_timed()
        for table in ("equipped_roles", "inventory_items", "timed_holdings",
                      "temp_roles"):
            execute(f"DELETE FROM {table}")

    def _world(self, person, roles):
        guild = SimpleNamespace(
            id=GUILD, owner_id=99,
            me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
            get_role=lambda rid: roles.get(int(rid)),
            get_member=lambda uid: person if uid == person.id else None,
        )
        return SimpleNamespace(
            get_guild=lambda gid: guild if gid == GUILD else None)

    async def test_a_failed_grant_releases_the_slot_gate(self):
        role = role_stub(9711, "LifecycleRole")
        person = member()
        person.roles = [role]
        bot = self._world(person, {9711: role})

        async def _boom(target, reason=None):
            raise RuntimeError("Discord said no")

        person.add_roles = AsyncMock(side_effect=_boom)
        person.remove_roles = AsyncMock()
        person.roles = []      # force the add path

        failed = await TO.grant_timed_product(
            bot, GUILD, USER, item_name="LifecycleRole", duration_hours=5,
            item_type="temp_role", role_id=9711, source="shop")
        self.assertFalse(failed["success"], failed)

        # The gate must still be free: a new equip of an owned entry completes.
        execute("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,"
                "quantity,metadata,source) VALUES (?,?,'LifecyclePerm','role',1,?,'shop')",
                (GUILD, USER, json.dumps({"role_id": 9712})))
        role2 = role_stub(9712, "LifecyclePerm")
        person.roles = []
        person.add_roles = AsyncMock(
            side_effect=lambda target, reason=None: person.roles.append(target))
        # `_world`'s get_role closure must know the second role too.
        bot = self._world(person, {9711: role, 9712: role2})
        perm = f"i:{rows('SELECT id FROM inventory_items WHERE item_name=?', ('LifecyclePerm',))[0][0]}"

        equipped = await asyncio.wait_for(
            equip_role(bot, GUILD, USER, "LifecyclePerm", entry_key=perm),
            timeout=5)
        self.assertTrue(equipped["success"], equipped)

    async def test_a_cancelled_expiry_releases_the_slot_gate(self):
        role = role_stub(9721, "CancelledRole")
        person = member()
        person.roles = [role]
        bot = self._world(person, {9721: role})
        entered = asyncio.Event()

        async def _hanging_remove(target, reason=None):
            entered.set()
            await asyncio.Event().wait()      # never completes by itself

        person.remove_roles = AsyncMock(side_effect=_hanging_remove)
        holding_id = await TO.grant_timed_holding(
            GUILD, USER, "CancelledRole", expires_at=iso_in(-2),
            item_type="temp_role", metadata={"role_id": 9721},
            role_id=9721, source="shop")
        await EE.record_equipped(GUILD, USER, "CancelledRole", role_id=9721,
                                 entry_key=f"h:{holding_id}")
        holding = (await TO.expired_holdings(limit=None))[0]

        task = asyncio.create_task(TO.expire_one(bot, holding))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # The interrupted expiry kept its retry marker and released the gate.
        self.assertEqual(
            rows("SELECT COUNT(*) FROM timed_holdings WHERE id=?", (holding_id,)),
            [(1,)], "a cancelled expiry must keep its row as the retry marker")
        person.remove_roles = AsyncMock()
        person.roles = []
        freed = await asyncio.wait_for(
            EE.unequip_role(bot, GUILD, USER, "CancelledRole",
                            entry_key=f"h:{holding_id}"),
            timeout=5)
        # The gate is free, but a stale UI action must not unequip an expired
        # entry. The expiry retry, not the user action, owns that cleanup.
        self.assertFalse(freed["success"], freed)
        retried = await asyncio.wait_for(TO.expire_one(bot, holding), timeout=5)
        self.assertEqual(retried["status"], "removed")
        self.assertIsNone(await get_equipped(GUILD, USER))

    async def test_one_members_slot_work_does_not_block_another(self):
        """The gate is per member: a stalled grant must not stall the guild."""
        role = role_stub(9731, "SharedRole")
        other = 555
        mine = member()
        mine.roles = []
        theirs = member()
        theirs.id = other
        theirs.roles = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _slow_add(target, reason=None):
            entered.set()
            await release.wait()
            theirs.roles.append(target)

        theirs.add_roles = AsyncMock(side_effect=_slow_add)
        guild = SimpleNamespace(
            id=GUILD, owner_id=99,
            me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
            get_role=lambda rid: role if int(rid) == 9731 else None,
            get_member=lambda uid: {USER: mine, other: theirs}.get(uid),
        )
        bot = SimpleNamespace(
            get_guild=lambda gid: guild if gid == GUILD else None)

        stalled = asyncio.create_task(TO.grant_timed_product(
            bot, GUILD, other, item_name="OtherMemberItem", duration_hours=5,
            item_type="temp_role", role_id=9731, source="shop"))
        await asyncio.wait_for(entered.wait(), timeout=5)

        # A different member's slot work must complete while that one is stuck:
        # if the gate were global, this equip would wait out the timeout.
        spare = role_stub(9732, "SpareRole")
        guild.get_role = lambda rid: {9731: role, 9732: spare}.get(int(rid))
        execute("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,"
                "quantity,metadata,source) VALUES (?,?,'SpareItem','role',1,?,'shop')",
                (GUILD, USER, json.dumps({"role_id": 9732})))
        spare_key = "i:%d" % rows(
            "SELECT id FROM inventory_items WHERE item_name='SpareItem'")[0][0]
        mine.add_roles = AsyncMock(
            side_effect=lambda target, reason=None: mine.roles.append(target))
        equipped = await asyncio.wait_for(
            EE.equip_role(bot, GUILD, USER, "SpareItem", entry_key=spare_key),
            timeout=5)
        self.assertTrue(equipped["success"], equipped)
        mine_holding = await asyncio.wait_for(
            TO.grant_timed_holding(GUILD, USER, "Unrelated", expires_at=iso_in(3),
                                   item_type="shop_custom", source="shop"),
            timeout=5)
        self.assertIsInstance(mine_holding, int)

        release.set()
        granted = await stalled
        self.assertTrue(granted["success"], granted)
