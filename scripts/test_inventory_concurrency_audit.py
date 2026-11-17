#!/usr/bin/env python3
"""
Inventory reliability, database integrity and concurrency audit.

Everything runs on a scratch SQLite database against the REAL engines
(utils/equip_engine, title_engine, potion_engine, economy_safe, daily_engine,
cogs/shop purchase flow, cogs/wallet panels, dashboard /api/edit-member).
Only Discord is stubbed.

How "at the same time" is produced here (sequential calls are not used as
evidence of anything concurrent):

  * tasks   - asyncio.gather() on one event loop with a Discord stub that
              really suspends, so the callers interleave at every await.
              This is how one bot process sees double clicks.
  * threads - the Flask dashboard route runs in another thread with its own
              event loop and its own SQLite connections while the bot-side
              coroutine runs, as the dashboard process does in production.
  * procs   - separate OS processes opened on the same database file and
              released together by a start-file barrier. No in-process lock
              (asyncio or `slot_lock`) can help here; only SQLite can.

Every test runs the integrity checker before and after. Process workers have
a start deadline and are always killed on the way out, so a failing test
cannot hang the run.

Run:  python3 scripts/test_inventory_concurrency_audit.py
"""
import asyncio
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from phase1_support import (
    GUILD, USER, ROOT, execute, rows, reset_database, seed_member,
)

import discord  # noqa: E402

import test_inventory_actions as actions  # noqa: E402
from database import DB_PATH  # noqa: E402
from cogs import wallet  # noqa: E402
from cogs.shop import process_purchase  # noqa: E402
from utils import (  # noqa: E402
    daily_engine, economy_safe, equip_engine, inventory, potion_engine,
    reward_engine, timed_ownership, title_engine,
)
import dashboard.app as dashboard  # noqa: E402

FLAME, BLUE = actions.FLAME, actions.BLUE
OTHER_USER = actions.OTHER_USER
OTHER_GUILD = actions.OTHER_GUILD
POTION = potion_engine.build_metadata("xp_boost", 2.0, 3)


# ── integrity checker ───────────────────────────────────────────────────────
def integrity_problems() -> list[str]:
    """Invariants the schema itself does not enforce (no CHECK / FK)."""
    found = []

    def check(label, sql):
        bad = rows(sql)
        if bad:
            found.append(f"{label}: {bad[:5]}")

    check("sqlite integrity_check",
          "SELECT * FROM pragma_integrity_check WHERE integrity_check != 'ok'")
    check("negative wallet",
          "SELECT guild_id,user_id,balance,diamonds FROM economy "
          "WHERE balance < 0 OR diamonds < 0")
    check("negative inventory quantity",
          "SELECT guild_id,user_id,item_name,quantity FROM inventory_items "
          "WHERE quantity < 0")
    check("negative timed quantity",
          "SELECT id,quantity FROM timed_holdings WHERE quantity < 0")
    check("duplicate inventory stack",
          "SELECT guild_id,user_id,item_name,COUNT(*) FROM inventory_items "
          "GROUP BY 1,2,3 HAVING COUNT(*) > 1")
    check("more than one equipped role",
          "SELECT guild_id,user_id,COUNT(*) FROM equipped_roles "
          "GROUP BY 1,2 HAVING COUNT(*) > 1")
    check("more than one equipped title",
          "SELECT guild_id,user_id,COUNT(*) FROM equipped_titles "
          "GROUP BY 1,2 HAVING COUNT(*) > 1")
    found.extend(slot_problems())
    check("equipped title that is not owned",
          "SELECT e.guild_id,e.user_id,e.item_name FROM equipped_titles e "
          "WHERE NOT EXISTS (SELECT 1 FROM inventory_items i WHERE "
          " i.guild_id=e.guild_id AND i.user_id=e.user_id AND "
          " i.item_name=e.item_name AND i.quantity > 0 AND i.item_type='title')")
    check("purchase both delivered and refunded",
          "SELECT op_id FROM shop_purchase_operations "
          "WHERE status='delivered' AND refunded=1")
    check("negative stock",
          "SELECT id,current_stock FROM shop_items WHERE current_stock < 0")
    return found


def slot_problems() -> list[str]:
    """Every `equipped_roles` row must point at exactly the entry it wears.

    The slot names an ownership entry three ways (entry_key, item_name,
    role_id); all three have to agree with a live, equippable entry.
    An expired timed holding counts as a problem here because no expiry sweep
    runs in these tests - in production it is a normal state until the sweep.

    Deliberately NOT checked, because they are not invariants of this schema:
    balance vs ledger (seeds and dashboard edits write no ledger row) and the
    number of boost rows (two potions are two rows); tests assert those
    directly where they matter.
    """
    found = []
    now = datetime.now(timezone.utc)
    for guild, user, name, role_id, key in rows(
            "SELECT guild_id,user_id,item_name,role_id,entry_key FROM equipped_roles"):
        label = f"equipped role {name!r} ({key or 'legacy'})"
        expires = None
        if key and str(key).startswith(("t:", "h:")):
            entry = rows("SELECT item_name,item_type,quantity,metadata,role_id,"
                         "expires_at FROM timed_holdings WHERE id=? AND guild_id=? "
                         "AND user_id=?", (str(key)[2:], guild, user))
            if entry:
                expires = entry[0][5]
        elif key and str(key).startswith("i:"):
            entry = rows("SELECT item_name,item_type,quantity,metadata,NULL "
                         "FROM inventory_items WHERE id=? AND guild_id=? AND "
                         "user_id=?", (str(key)[2:], guild, user))
        elif key:
            found.append(f"{label}: unknown entry key")
            continue
        else:
            entry = rows("SELECT item_name,item_type,quantity,metadata,NULL "
                         "FROM inventory_items WHERE guild_id=? AND user_id=? "
                         "AND item_name=?", (guild, user, name))
        if not entry:
            found.append(f"{label}: points at an entry that does not exist")
            continue
        entry_name, kind, held, metadata, column_role = entry[0][:5]
        try:
            meta_role = (json.loads(metadata) or {}).get("role_id") if metadata else None
        except (TypeError, ValueError):
            meta_role = None
        entry_role = meta_role or column_role
        if int(held or 0) <= 0:
            found.append(f"{label}: is not owned (quantity {held})")
        if kind not in ("role", "temp_role"):
            found.append(f"{label}: wears a non-role item ({kind})")
        if entry_name != name:
            found.append(f"{label}: entry is named {entry_name!r}")
        if entry_role is None or int(entry_role) != int(role_id):
            found.append(f"{label}: slot role {role_id} != item role {entry_role}")
        if expires:
            try:
                when = datetime.fromisoformat(str(expires))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when <= now:
                    found.append(f"{label}: timed holding expired at {expires}")
            except ValueError:
                found.append(f"{label}: unreadable expiry {expires!r}")
    return found


# ── helpers ─────────────────────────────────────────────────────────────────
def slow_discord(member, delay=0.01):
    """Make the role calls really suspend, like an HTTP round-trip."""
    async def add_roles(role, reason=None):
        await asyncio.sleep(delay)
        if role not in member.roles:
            member.roles.append(role)

    async def remove_roles(role, reason=None):
        await asyncio.sleep(delay)
        if role in member.roles:
            member.roles.remove(role)

    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)


def worn(member):
    return sorted(role.name for role in member.roles)


def slot():
    return rows("SELECT item_name, role_id FROM equipped_roles "
                "WHERE guild_id=? AND user_id=?", (GUILD, USER))


def balance(user=USER):
    return rows("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (GUILD, user))[0][0]


def quantity(name):
    found = rows("SELECT quantity FROM inventory_items WHERE guild_id=? "
                 "AND user_id=? AND item_name=?", (GUILD, USER, name))
    return found[0][0] if found else None


def boosts():
    return rows("SELECT multiplier, expires_at FROM leveling_active_boosts "
                "WHERE guild_id=? AND user_id=?", (GUILD, USER))


def running_effects():
    """Boost rows that are the configured potion effect and still running.

    POTION is x2.0 for 3 hours from activation, so each effect must end
    between 2h59 and 3h01 from now.
    """
    now = datetime.now(timezone.utc)
    good = []
    for multiplier, expires in boosts():
        when = datetime.fromisoformat(expires)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if (multiplier == 2.0 and
                timedelta(hours=2, minutes=59) < when - now < timedelta(hours=3, minutes=1)):
            good.append(expires)
    return good


def listing(name, kind, price=300, stock=None, **extra):
    cols = {"guild_id": GUILD, "name": name, "type": kind, "price": price,
            "enabled": 1, "max_stock": stock, "current_stock": stock, **extra}
    return execute(
        f"INSERT INTO shop_items ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))


def shop_interaction(member, guild, bot):
    return SimpleNamespace(
        user=member, guild=guild, client=bot, guild_id=GUILD, channel=None,
        response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(),
            edit_message=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()))


class WriteLock:
    """Another connection holding SQLite's write lock (a busy second writer)."""

    def __enter__(self):
        self.db = sqlite3.connect(DB_PATH, isolation_level=None,
                                  check_same_thread=False)
        self.db.execute("BEGIN IMMEDIATE")
        return self

    def take(self):
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, *exc):
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")
        self.db.close()


# A separate OS process on the same database file. It imports the real modules,
# waits for the start file (the barrier), runs one operation and prints JSON.
WORKER = r"""
import asyncio, json, os, sys, time
sys.path.insert(0, os.environ["AUDIT_ROOT"])
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    from utils import economy_safe, potion_engine
GUILD, USER = int(os.environ["AUDIT_GUILD"]), int(os.environ["AUDIT_USER"])
action, start = sys.argv[1], sys.argv[2]
print("READY", flush=True)
while not os.path.exists(start):
    time.sleep(0.001)

async def run():
    if action == "deduct":
        try:
            await economy_safe.safe_deduct(
                GUILD, USER, 300, reason="audit", source="audit")
            return {"ok": True}
        except economy_safe.InsufficientBalance:
            return {"ok": False, "why": "insufficient"}
    if action == "potion":
        result = await potion_engine.use_potion(GUILD, USER, "Tonic")
        return {"ok": bool(result.get("success")), "why": result.get("error")}
    if action == "crash_mid_potion":
        # The process dies after the potion was taken off the stack and before
        # the transaction commits - a restart in the middle of an activation.
        from utils import xp_calculator
        async def die(*a, **k):
            os._exit(9)
        xp_calculator.grant_xp_boost = die
        await potion_engine.use_potion(GUILD, USER, "Tonic")
        return {"ok": True}
    if action == "crash_mid_delivery":
        # A real purchase through cogs/shop.py. The process dies at the moment
        # delivery starts: the member is charged, the operation says
        # `delivering`, and no worker is left to finish it.
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import cogs.shop as shop
        async def die(*a, **k):
            os._exit(9)
        shop._deliver_purchase = die
        member = SimpleNamespace(
            id=USER, roles=[], premium_since=None, bot=False, name="M",
            display_name="M", mention="<@1>", display_avatar=SimpleNamespace(url=""),
            add_roles=AsyncMock(), remove_roles=AsyncMock())
        guild = SimpleNamespace(
            id=GUILD, name="Scratch", owner_id=99, roles=[],
            premium_subscriber_role=None,
            me=SimpleNamespace(top_role=None, guild_permissions=SimpleNamespace(
                manage_roles=True, administrator=True)),
            get_member=lambda uid: member, get_role=lambda rid: None)
        member.guild = guild
        bot = SimpleNamespace(get_guild=lambda gid: guild,
                              user=SimpleNamespace(name="Nilive", id=5),
                              application_id=None)
        click = SimpleNamespace(
            user=member, guild=guild, client=bot, guild_id=GUILD, channel=None,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                     edit_message=AsyncMock(), is_done=lambda: False),
            followup=SimpleNamespace(send=AsyncMock()))
        await shop.process_purchase(click, int(os.environ["AUDIT_ITEM"]))
        return {"ok": False, "why": "delivery was never reached"}

try:
    out = asyncio.run(run())
except Exception as e:
    out = {"ok": False, "why": "raised", "error": repr(e)}
print("RESULT " + json.dumps(out), flush=True)
"""


def run_processes(action, count, *, ready_timeout=60, run_timeout=120, **extra_env):
    """Start `count` workers, release them together, return their results.

    A worker that has not loaded within `ready_timeout`, or not finished within
    `run_timeout`, fails the test; every worker is killed on the way out
    whatever happened, so nothing is left running and nothing can hang.
    """
    env = dict(os.environ, AUDIT_ROOT=str(ROOT), AUDIT_GUILD=str(GUILD),
               AUDIT_USER=str(USER), DATABASE_PATH=str(DB_PATH),
               PYTHONDONTWRITEBYTECODE="1",
               **{key: str(value) for key, value in extra_env.items()})
    workers = []
    with tempfile.TemporaryDirectory() as folder:
        start = os.path.join(folder, "go")
        try:
            for _ in range(count):
                worker = subprocess.Popen(
                    [sys.executable, "-c", WORKER, action, start], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                worker.ready = threading.Event()
                worker.lines = []

                def pump(worker=worker):
                    for line in worker.stdout:
                        worker.lines.append(line)
                        if "READY" in line:
                            worker.ready.set()
                    worker.ready.set()            # EOF: stop waiting for it
                worker.reader = threading.Thread(target=pump, daemon=True)
                worker.reader.start()
                workers.append(worker)
            deadline = time.monotonic() + ready_timeout
            for worker in workers:                # all loaded and waiting
                if not worker.ready.wait(max(0.0, deadline - time.monotonic())):
                    raise AssertionError(
                        f"worker not ready within {ready_timeout}s")
                if not any("READY" in line for line in worker.lines):
                    worker.wait(timeout=10)
                    raise AssertionError(
                        "worker died while loading: " + worker.stderr.read()[-2000:])
            Path(start).touch()                   # release the barrier
            results = []
            deadline = time.monotonic() + run_timeout
            for worker in workers:
                try:
                    worker.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise AssertionError(
                        f"worker still running after {run_timeout}s") from None
                worker.reader.join(timeout=10)
                line = next((l for l in worker.lines if l.startswith("RESULT ")), None)
                results.append(json.loads(line[7:]) if line else
                               {"ok": False, "why": "died", "rc": worker.returncode})
            return results
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                with contextlib.suppress(Exception):
                    worker.wait(timeout=10)
                for stream in (worker.stdout, worker.stderr):
                    with contextlib.suppress(Exception):
                        stream.close()


class AuditCase(actions.InventoryActionCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        seed_member(balance=1000)
        self.assertEqual(integrity_problems(), [])

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.assertEqual(integrity_problems(), [])

    async def panel_of(self, name):
        """Open the item panel from the tab the wallet files the item under."""
        entries = await inventory.get_inventory_entries(GUILD, USER)
        entry = next(e for e in entries if e["item_name"] == name)
        tab = next(tab for tab, items in wallet.split_by_tab(entries).items()
                   if entry in items)
        return await self.open_panel(tab, entry["entry_key"])

    async def give_roles(self):
        """Own Flame and Blue, nothing equipped, nothing worn."""
        for role_id in (FLAME, BLUE):
            await reward_engine.give_reward(
                self.bot, GUILD, USER, "role", role_id=role_id, source="test")
        await equip_engine.unequip_role(self.bot, GUILD, USER)
        self.assertEqual((slot(), worn(self.member)), ([], []))
        slow_discord(self.member)

    async def booster(self, *, colour_level=None, boost2=False, count=None,
                      boosting=True):
        await self.give_roles()
        self.addCleanup(self.booster_cleanup)
        if boosting:
            self.member.premium_since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        if count is not None:
            self.member.premium_subscription_count = count
        execute("INSERT OR REPLACE INTO boost_config (guild_id,enabled,"
                "boost2_role_id) VALUES (?,1,?)", (GUILD, FLAME if boost2 else None))
        if colour_level:
            execute("INSERT INTO boost_color_roles (guild_id,role_id,role_name,"
                    "requires_boost_level) VALUES (?,?,?,?)",
                    (GUILD, FLAME, "Flame", colour_level))

    def booster_cleanup(self):
        self.member.premium_since = None
        self.member.roles.clear()
        if hasattr(self.member, "premium_subscription_count"):
            del self.member.premium_subscription_count
        for table in ("boost_color_roles", "boost_config", "mvp_history",
                      "mvp_config"):
            execute(f"DELETE FROM {table}")


# ── 1. repeated clicks ──────────────────────────────────────────────────────
class RepeatedClickTests(AuditCase):
    async def test_twenty_activate_clicks_on_one_panel_use_one_potion(self):
        await inventory.give_item(GUILD, USER, "Tonic", 3, item_type="potion",
                                  metadata=POTION)
        view, _ = await self.panel_of("Tonic")
        outcomes = await asyncio.gather(*[
            self.click(view, wallet.ActivateButton) for _ in range(20)])
        self.assertEqual([o[0] for o in outcomes].count("rendered"), 1)
        refused = [o[1] for o in outcomes if o[0] == "refused"]
        self.assertEqual(len(refused), 19)
        self.assertTrue(all("Open it again" in text for text in refused), refused)
        self.assertEqual(quantity("Tonic"), 2)
        self.assertEqual(len(boosts()), 1)

    async def test_ten_equip_clicks_on_one_panel_equip_once(self):
        await self.give_roles()
        view, _ = await self.panel_of("Flame")
        outcomes = await asyncio.gather(*[
            self.click(view, wallet.EquipToggleButton) for _ in range(10)])
        # The first click wins; the others hold a panel that is stale by then.
        self.assertEqual([o[0] for o in outcomes].count("rendered"), 1, outcomes)
        self.assertEqual(slot(), [("Flame", FLAME)])
        self.assertEqual(worn(self.member), ["Flame"])
        self.assertEqual(self.member.add_roles.await_count, 1)

    async def test_ten_unequip_clicks_remove_the_role_once(self):
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        view, _ = await self.panel_of("Flame")
        outcomes = await asyncio.gather(*[
            self.click(view, wallet.EquipToggleButton) for _ in range(10)])
        self.assertEqual([o[0] for o in outcomes].count("rendered"), 1, outcomes)
        self.assertEqual((slot(), worn(self.member)), ([], []))
        self.assertEqual(rows(
            "SELECT COUNT(*) FROM inventory_activity WHERE event_type='unequipped'"
            " AND item_name='Flame'"), [(1,)])

    async def test_six_daily_claims_at_once_pay_once(self):
        before = balance()
        outcomes = await asyncio.gather(*[
            daily_engine.perform_streak_claim(self.bot, GUILD, USER, member=self.member)
            for _ in range(6)], return_exceptions=True)
        paid = [o for o in outcomes if isinstance(o, dict)]
        late = [o for o in outcomes if isinstance(o, daily_engine.DailyAlreadyClaimed)]
        self.assertEqual((len(paid), len(late)), (1, 5), outcomes)
        self.assertGreater(balance(), before)
        self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger WHERE "
                              "user_id=? AND currency='balance'", (USER,)), [(1,)])
        # A retry after the "timeout" is refused as well.
        with self.assertRaises(daily_engine.DailyAlreadyClaimed):
            await daily_engine.perform_streak_claim(
                self.bot, GUILD, USER, member=self.member)


# ── 2. simultaneous requests ────────────────────────────────────────────────
class SimultaneousRequestTests(AuditCase):
    async def test_eight_activations_of_a_stack_of_two_use_exactly_two(self):
        await inventory.give_item(GUILD, USER, "Tonic", 2, item_type="potion",
                                  metadata=POTION)
        outcomes = await asyncio.gather(*[
            potion_engine.use_potion(GUILD, USER, "Tonic") for _ in range(8)])
        self.assertEqual(sum(1 for o in outcomes if o["success"]), 2)
        self.assertEqual(quantity("Tonic"), 0)
        # Two potions consumed <=> two effects running, each the configured
        # x2.0 for 3 hours: none consumed without its effect, none extra.
        self.assertEqual(len(boosts()), 2, boosts())
        self.assertEqual(len(running_effects()), 2, boosts())
        self.assertEqual(rows("SELECT COUNT(*) FROM inventory_activity WHERE "
                              "event_type='used' AND item_name='Tonic'"), [(2,)])

    async def test_six_processes_activating_a_stack_of_two_use_exactly_two(self):
        await inventory.give_item(GUILD, USER, "Tonic", 2, item_type="potion",
                                  metadata=POTION)
        results = await asyncio.to_thread(run_processes, "potion", 6)
        self.assertEqual(sum(1 for r in results if r["ok"]), 2, results)
        self.assertEqual(quantity("Tonic"), 0)
        self.assertFalse([r for r in results if r.get("why") in ("raised", "died")],
                         results)
        self.assertEqual(len(boosts()), 2, boosts())
        self.assertEqual(len(running_effects()), 2, boosts())
        self.assertEqual(rows("SELECT COUNT(*) FROM inventory_activity WHERE "
                              "event_type='used' AND item_name='Tonic'"), [(2,)])
        # The four losers were refused for the right reason, not by an error.
        self.assertEqual(sorted(str(r["why"]) for r in results if not r["ok"]),
                         ["You don't own **Tonic**."] * 4, results)

    async def test_two_purchases_cannot_spend_the_same_balance(self):
        execute("UPDATE economy SET balance=300 WHERE user_id=?", (USER,))
        item = listing("Thing", "custom", 300)
        member = self.member
        member.display_name = member.name = "M"
        member.mention = "<@1>"
        member.display_avatar = SimpleNamespace(url="")
        self.guild.owner_id = 99
        clicks = [shop_interaction(member, self.guild, self.bot) for _ in range(2)]
        await asyncio.gather(*[process_purchase(c, item) for c in clicks])
        self.assertEqual(balance(), 0)
        self.assertEqual(quantity("Thing"), 1)
        self.assertEqual(rows("SELECT COUNT(*) FROM purchase_history"), [(1,)])
        self.assertEqual(sorted(rows(
            "SELECT status, charged FROM shop_purchase_operations")),
            [("delivered", 1), ("failed", 0)])

    async def test_five_purchases_of_the_last_unit_sell_it_once(self):
        item = listing("Thing", "custom", 300, stock=1)
        member = self.member
        member.display_name = member.name = "M"
        member.mention = "<@1>"
        member.display_avatar = SimpleNamespace(url="")
        self.guild.owner_id = 99
        clicks = [shop_interaction(member, self.guild, self.bot) for _ in range(5)]
        await asyncio.gather(*[process_purchase(c, item) for c in clicks])
        self.assertEqual(balance(), 700)
        self.assertEqual(quantity("Thing"), 1)
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(0,)])

    async def test_eight_processes_deducting_300_from_1000_charge_three_times(self):
        results = await asyncio.to_thread(run_processes, "deduct", 8)
        self.assertEqual(sum(1 for r in results if r["ok"]), 3, results)
        self.assertEqual([r["why"] for r in results if not r["ok"]],
                         ["insufficient"] * 5, results)
        self.assertEqual(balance(), 100)
        self.assertEqual(rows("SELECT COUNT(*), SUM(amount) FROM transaction_ledger "
                              "WHERE user_id=?", (USER,)), [(3, -900)])


# ── 3. competing Equip / Unequip ────────────────────────────────────────────
class CompetingEquipTests(AuditCase):
    async def test_two_items_racing_for_the_slot_leave_one_item_and_one_role(self):
        await self.give_roles()
        outcomes = await asyncio.gather(*[
            equip_engine.equip_role(self.bot, GUILD, USER, name)
            for name in ("Flame", "Blue") * 5])
        self.assertTrue(all(o["success"] for o in outcomes), outcomes)
        equipped = slot()
        self.assertEqual(len(equipped), 1)
        self.assertEqual(worn(self.member), [equipped[0][0]])

    async def test_two_panels_racing_for_the_slot_let_one_win(self):
        await self.give_roles()
        flame, _ = await self.panel_of("Flame")
        blue, _ = await self.panel_of("Blue")
        first, second = await asyncio.gather(
            self.click(flame, wallet.EquipToggleButton),
            self.click(blue, wallet.EquipToggleButton))
        self.assertEqual(sorted([first[0], second[0]]), ["refused", "rendered"])
        equipped = slot()
        self.assertEqual(len(equipped), 1)
        self.assertEqual(worn(self.member), [equipped[0][0]])

    async def test_equip_and_unequip_of_one_item_agree_with_discord(self):
        await self.give_roles()
        outcomes = await asyncio.gather(*(
            [equip_engine.equip_role(self.bot, GUILD, USER, "Flame") for _ in range(4)]
            + [equip_engine.unequip_role(self.bot, GUILD, USER, "Flame") for _ in range(4)]))
        equips, unequips = outcomes[:4], outcomes[4:]
        self.assertTrue(all(o["success"] for o in equips), equips)
        # Every refused Unequip found nothing equipped; none failed otherwise.
        for o in unequips:
            if not o["success"]:
                self.assertIn("do not have a role equipped", o["error"])
        equipped = slot()
        self.assertEqual(worn(self.member), [name for name, _ in equipped])
        # Discord calls match the outcomes: one removal per successful
        # Unequip, and one more add than removals only if Flame ended up worn.
        removed = sum(1 for o in unequips if o["success"])
        self.assertEqual(self.member.remove_roles.await_count, removed)
        self.assertEqual(self.member.add_roles.await_count,
                         removed + (1 if equipped else 0))

    async def test_titles_racing_for_the_slot_leave_one_title(self):
        for name in ("Sage", "Hero"):
            await inventory.give_item(GUILD, USER, name, 1, item_type="title")
        outcomes = await asyncio.gather(*[
            title_engine.equip_title(GUILD, USER, name) for name in ("Sage", "Hero") * 4])
        self.assertTrue(all(o["success"] for o in outcomes))
        self.assertEqual(len(rows("SELECT item_name FROM equipped_titles")), 1)

    async def test_title_panels_rendered_against_one_slot_let_one_win(self):
        """Four stale panels, two per title, none for the equipped title.

        Re-equipping the title that is already equipped is a legitimate no-op
        success, so the racing panels are for two OTHER titles: whichever
        commits first changes the slot and every other panel is stale.
        """
        for name in ("Monk", "Sage", "Hero"):
            await inventory.give_item(GUILD, USER, name, 1, item_type="title")
        self.assertTrue((await title_engine.equip_title(GUILD, USER, "Monk"))["success"])
        outcomes = await asyncio.gather(*[
            title_engine.equip_title(GUILD, USER, name, expected_title="Monk")
            for name in ("Sage", "Hero") * 2])
        winners = [o["item_name"] for o in outcomes if o["success"]]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(rows("SELECT item_name FROM equipped_titles"),
                         [(winners[0],)])
        for o in outcomes:
            if not o["success"]:
                self.assertIn("changed", o["error"])
        # One slot change only: Monk -> winner.
        self.assertEqual(rows(
            "SELECT COUNT(*) FROM inventory_activity WHERE event_type='equipped' "
            "AND item_type='title' AND item_name IN ('Sage','Hero')"), [(1,)])

    async def test_re_equipping_the_equipped_title_is_a_harmless_no_op(self):
        await inventory.give_item(GUILD, USER, "Sage", 1, item_type="title")
        await title_engine.equip_title(GUILD, USER, "Sage")
        again = await title_engine.equip_title(GUILD, USER, "Sage", expected_title="Sage")
        self.assertTrue(again["success"])
        self.assertEqual(rows("SELECT item_name FROM equipped_titles"), [("Sage",)])
        self.assertEqual(rows(
            "SELECT COUNT(*) FROM inventory_activity WHERE event_type='equipped' "
            "AND item_name='Sage'"), [(1,)])


# ── 4. potion consumption + effect atomicity ────────────────────────────────
class PotionAtomicityTests(AuditCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await inventory.give_item(GUILD, USER, "Tonic", 2, item_type="potion",
                                  metadata=POTION)

    async def test_effect_failure_keeps_the_potion(self):
        with patch("utils.xp_calculator.grant_xp_boost",
                   AsyncMock(side_effect=RuntimeError("boost table unavailable"))):
            try:
                result = await potion_engine.use_potion(GUILD, USER, "Tonic")
            except RuntimeError:
                result = {"success": False}
        self.assertFalse(result["success"])
        self.assertEqual((quantity("Tonic"), boosts()), (2, []))
        self.assertEqual(rows("SELECT COUNT(*) FROM inventory_activity "
                              "WHERE event_type LIKE '%potion%' OR event_type='used'"),
                         [(0,)])

    async def test_success_consumes_one_and_applies_one_effect(self):
        result = await potion_engine.use_potion(GUILD, USER, "Tonic")
        self.assertTrue(result["success"])
        self.assertEqual((quantity("Tonic"), len(boosts())), (1, 1))

    async def test_busy_database_reports_not_used_instead_of_failing_silently(self):
        view, _ = await self.panel_of("Tonic")
        with WriteLock():
            started = time.monotonic()
            outcome = await self.click(view, wallet.ActivateButton)
            waited = time.monotonic() - started
        self.assertEqual(outcome[0], "refused", outcome)
        self.assertIn("was not used", outcome[1])
        self.assertGreater(waited, 3)            # it really waited for the lock
        self.assertEqual((quantity("Tonic"), boosts()), (2, []))
        # The same panel still works once the database is free again.
        outcome = await self.click(view, wallet.ActivateButton)
        self.assertEqual(outcome[0], "rendered")
        self.assertEqual((quantity("Tonic"), len(boosts())), (1, 1))


# ── 5. bot vs dashboard ─────────────────────────────────────────────────────
class BotVersusDashboardTests(AuditCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,"
                "enabled) VALUES (?,?,'owner',1)", (GUILD, USER))
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Staff", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="audit")
        self.xp = rows("SELECT xp FROM levels WHERE user_id=?", (USER,))[0][0]

    def wallet(self):
        return rows("SELECT balance, diamonds FROM economy WHERE guild_id=? "
                    "AND user_id=?", (GUILD, USER))[0]

    def edit(self, **body):
        response = self.client.post(
            "/api/edit-member", headers={"X-CSRF-Token": "audit"},
            json={"user_id": str(USER), "xp": self.xp, **body})
        return response.get_json()

    async def test_form_opened_before_a_purchase_cannot_hand_the_coins_back(self):
        # Staff opened the form at 1000 / 138, then the member spent 300.
        await economy_safe.safe_deduct(GUILD, USER, 300, reason="Shop", source="shop")
        answer = await asyncio.to_thread(
            self.edit, coins=1000, expected_coins=1000)
        self.assertFalse(answer["success"])
        self.assertTrue(answer["stale"])
        self.assertIn("700", answer["error"])
        self.assertEqual(balance(), 700)

    async def test_form_opened_before_earnings_cannot_wipe_them(self):
        await economy_safe.safe_credit(GUILD, USER, 250, reason="Daily", source="daily")
        answer = await asyncio.to_thread(
            self.edit, coins=1000, diamonds=138, expected_coins=1000,
            expected_diamonds=138)
        self.assertFalse(answer["success"])
        self.assertEqual(balance(), 1250)

    async def test_stale_diamonds_are_refused_and_nothing_is_written(self):
        execute("UPDATE economy SET diamonds=100 WHERE user_id=?", (USER,))
        answer = await asyncio.to_thread(
            self.edit, coins=5, diamonds=138, expected_coins=1000,
            expected_diamonds=138)
        self.assertFalse(answer["success"])
        self.assertEqual(rows("SELECT balance, diamonds FROM economy WHERE user_id=?",
                              (USER,)), [(1000, 100)])

    async def test_an_up_to_date_form_still_saves(self):
        answer = await asyncio.to_thread(
            self.edit, coins=1500, diamonds=140, expected_coins=1000,
            expected_diamonds=138)
        self.assertEqual(answer, {"success": True})
        self.assertEqual(rows("SELECT balance, diamonds FROM economy WHERE user_id=?",
                              (USER,)), [(1500, 140)])

    async def test_writing_coins_without_a_snapshot_is_refused(self):
        answer = await asyncio.to_thread(self.edit, coins=42)
        self.assertFalse(answer["success"])
        self.assertEqual(answer["code"], "WALLET_SNAPSHOT_REQUIRED")
        self.assertIn("expected_coins", answer["error"])
        self.assertEqual(self.wallet(), (1000, 138))
        # Not even when the value sent equals the current balance.
        answer = await asyncio.to_thread(self.edit, coins=1000)
        self.assertEqual(answer["code"], "WALLET_SNAPSHOT_REQUIRED")
        self.assertEqual(self.wallet(), (1000, 138))

    async def test_writing_diamonds_without_a_snapshot_is_refused(self):
        answer = await asyncio.to_thread(self.edit, diamonds=5)
        self.assertEqual(answer["code"], "WALLET_SNAPSHOT_REQUIRED")
        self.assertIn("expected_diamonds", answer["error"])
        self.assertEqual(self.wallet(), (1000, 138))

    async def test_each_currency_needs_its_own_snapshot(self):
        # A coins snapshot does not cover diamonds, and the reverse.
        for body in ({"coins": 5, "diamonds": 5, "expected_coins": 1000},
                     {"coins": 5, "diamonds": 5, "expected_diamonds": 138}):
            answer = await asyncio.to_thread(lambda body=body: self.edit(**body))
            self.assertEqual(answer["code"], "WALLET_SNAPSHOT_REQUIRED", body)
            self.assertEqual(self.wallet(), (1000, 138))

    async def test_missing_snapshot_is_refused_with_http_400_and_no_audit_row(self):
        response = await asyncio.to_thread(
            lambda: self.client.post(
                "/api/edit-member", headers={"X-CSRF-Token": "audit"},
                json={"user_id": str(USER), "xp": self.xp, "coins": 0, "diamonds": 0}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.wallet(), (1000, 138))
        self.assertEqual(rows("SELECT COUNT(*) FROM audit_log WHERE action LIKE "
                              "'Edited member%'"), [(0,)])

    async def test_stale_snapshot_answers_http_409(self):
        execute("UPDATE economy SET balance=700 WHERE user_id=?", (USER,))
        response = await asyncio.to_thread(
            lambda: self.client.post(
                "/api/edit-member", headers={"X-CSRF-Token": "audit"},
                json={"user_id": str(USER), "xp": self.xp, "coins": 1000,
                      "expected_coins": 1000}))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.wallet(), (700, 138))

    async def test_a_request_without_wallet_fields_leaves_the_wallet_alone(self):
        # An XP-only edit writes neither currency and needs no snapshot, even
        # if the member's balance moved while the form was open.
        await economy_safe.safe_deduct(GUILD, USER, 300, reason="Shop", source="shop")
        self.assertEqual(await asyncio.to_thread(self.edit), {"success": True})
        self.assertEqual(self.wallet(), (700, 138))

    async def test_only_the_currency_that_is_sent_is_written(self):
        answer = await asyncio.to_thread(self.edit, coins=1500, expected_coins=1000)
        self.assertEqual(answer, {"success": True})
        self.assertEqual(self.wallet(), (1500, 138))
        answer = await asyncio.to_thread(self.edit, diamonds=7, expected_diamonds=138)
        self.assertEqual(answer, {"success": True})
        self.assertEqual(self.wallet(), (1500, 7))
        # A stale snapshot for one currency blocks the whole request.
        answer = await asyncio.to_thread(
            self.edit, coins=1, diamonds=1, expected_coins=1500, expected_diamonds=138)
        self.assertTrue(answer["stale"])
        self.assertEqual(self.wallet(), (1500, 7))

    async def test_an_explicit_zero_is_a_write_and_needs_a_snapshot(self):
        answer = await asyncio.to_thread(self.edit, coins=0)
        self.assertEqual(answer["code"], "WALLET_SNAPSHOT_REQUIRED")
        answer = await asyncio.to_thread(self.edit, coins=0, expected_coins=1000)
        self.assertEqual(answer, {"success": True})
        self.assertEqual(self.wallet(), (0, 138))

    async def test_unreadable_snapshot_is_rejected(self):
        for body in ({"coins": 5, "expected_coins": "lots"},
                     {"coins": 5, "expected_coins": True},
                     {"coins": 5, "expected_coins": None},
                     {"diamonds": 5, "expected_diamonds": "many"},
                     {"coins": "lots", "expected_coins": 1000}):
            answer = await asyncio.to_thread(lambda body=body: self.edit(**body))
            self.assertFalse(answer["success"], body)
            self.assertEqual(self.wallet(), (1000, 138), body)

    async def test_edit_and_purchase_released_together_never_lose_the_charge(self):
        """Dashboard thread and bot coroutine queued on one lock, 8 rounds.

        Both are started while another connection holds SQLite's write lock,
        so both are genuinely waiting on it; releasing it lets them contend.
        Whatever the order, the 300 charge must survive: either the edit won
        (1000 -> 1500, then 1200) or the charge won and the stale edit was
        refused (700). 1500 would mean the charge was overwritten.
        """
        seen = set()
        for _ in range(8):
            execute("UPDATE economy SET balance=1000 WHERE user_id=?", (USER,))
            with WriteLock() as lock:
                edit = asyncio.ensure_future(asyncio.to_thread(
                    self.edit, coins=1500, expected_coins=1000))
                charge = asyncio.ensure_future(economy_safe.safe_deduct(
                    GUILD, USER, 300, reason="Shop", source="shop"))
                await asyncio.sleep(0.4)          # both are blocked on the lock
                self.assertFalse(edit.done() or charge.done())
                self.assertEqual(balance(), 1000)
                lock.db.execute("ROLLBACK")       # release: they race
            answer, _ = await asyncio.gather(edit, charge)
            final = balance()
            self.assertEqual(final, 1200 if answer["success"] else 700, answer)
            if not answer["success"]:
                self.assertTrue(answer["stale"])
            seen.add(final)
        print(f"\n    [bot-vs-dashboard] final balances observed over 8 rounds: {sorted(seen)}")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_both_dashboard_forms_really_send_the_snapshot(self):
        """Runs the two browser forms' own JavaScript (not a text match)."""
        done = subprocess.run(
            ["node", str(ROOT / "scripts" / "test_member_wallet_snapshot.js")],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout[-3000:] + done.stderr[-2000:])

    async def test_edit_first_then_purchase_keeps_both(self):
        answer = await asyncio.to_thread(self.edit, coins=1500, expected_coins=1000)
        self.assertEqual(answer, {"success": True})
        await economy_safe.safe_deduct(GUILD, USER, 300, reason="Shop", source="shop")
        self.assertEqual(balance(), 1200)


# ── 6. restart / interrupted operation ──────────────────────────────────────
class InterruptedOperationTests(AuditCase):
    async def test_process_killed_mid_activation_consumes_nothing(self):
        await inventory.give_item(GUILD, USER, "Tonic", 2, item_type="potion",
                                  metadata=POTION)
        results = await asyncio.to_thread(run_processes, "crash_mid_potion", 1)
        self.assertEqual(results[0]["why"], "died", results)
        self.assertEqual(results[0]["rc"], 9)
        self.assertEqual((quantity("Tonic"), boosts()), (2, []))
        # After the "restart" the potion works normally.
        self.assertTrue((await potion_engine.use_potion(GUILD, USER, "Tonic"))["success"])
        self.assertEqual((quantity("Tonic"), len(boosts())), (1, 1))

    async def test_unequip_interrupted_after_discord_is_reported_and_finishable(self):
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        view, _ = await self.panel_of("Flame")
        with WriteLock() as lock:
            lock.db.execute("ROLLBACK")

            async def remove_then_lose_the_database(role, reason=None):
                self.member.roles.remove(role)
                lock.take()
            self.member.remove_roles = AsyncMock(
                side_effect=remove_then_lose_the_database)
            outcome = await self.click(view, wallet.EquipToggleButton)
        # Told what happened, not "This interaction failed".
        self.assertEqual(outcome[0], "refused", outcome)
        self.assertIn("Press Unequip again", outcome[1])
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], []))
        # The panel still says Unequip, and pressing it finishes the job
        # without a second Discord call.
        slow_discord(self.member)
        view, _ = await self.panel_of("Flame")
        self.assertEqual(actions.labels(view), ["Unequip", "Back"])
        outcome = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "rendered")
        self.assertEqual((slot(), worn(self.member)), ([], []))
        self.assertEqual(self.member.remove_roles.await_count, 0)

    async def test_old_pending_charge_is_parked_for_review_and_not_refunded(self):
        """A hand-built row, NOT a process death: an operation that was charged
        and never left `pending` is parked as `uncertain` once it is old."""
        op = await timed_ownership.open_purchase(
            GUILD, USER, item_id=1, item_name="Thing", currency="balance", amount=300)
        await economy_safe.safe_deduct(GUILD, USER, 300, reason="Shop", source="shop")
        execute("UPDATE shop_purchase_operations SET charged=1, "
                "created_at=datetime('now','-2 hours'), "
                "updated_at=datetime('now','-2 hours') WHERE op_id=?", (op,))
        timed_ownership._LIVE_DELIVERIES.discard(op)
        await asyncio.gather(*[timed_ownership.reconcile_purchases() for _ in range(4)])
        self.assertEqual(rows("SELECT status, charged, refunded FROM "
                              "shop_purchase_operations"), [("uncertain", 1, 0)])
        self.assertEqual(balance(), 700)          # not auto-refunded
        self.assertIsNone(quantity("Thing"))      # not auto-delivered

    async def test_worker_killed_while_delivering_is_parked_never_refunded_or_redelivered(self):
        """A real purchase in a real process that dies as delivery starts."""
        item = listing("Thing", "custom", 300, stock=5)
        results = await asyncio.to_thread(
            lambda: run_processes("crash_mid_delivery", 1, AUDIT_ITEM=item))
        self.assertEqual((results[0]["why"], results[0]["rc"]), ("died", 9), results)
        # What the dead worker left behind: charged, stock reserved, delivering.
        self.assertEqual(rows(
            "SELECT status, charged, refunded, stock_reserved, stock_returned "
            "FROM shop_purchase_operations"), [("delivering", 1, 0, 1, 0)])
        self.assertEqual((balance(), quantity("Thing")), (700, None))
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(4,)])

        # Inside the delivery lease nothing may touch it: the worker could
        # still be alive as far as another process can tell.
        await timed_ownership.reconcile_purchases()
        self.assertEqual(rows("SELECT status FROM shop_purchase_operations"),
                         [("delivering",)])

        # Past the lease, several reconcilers at once park it exactly once.
        stats = await asyncio.gather(*[
            timed_ownership.reconcile_stranded_deliveries(lease_minutes=0)
            for _ in range(4)])
        self.assertEqual(sum(s["parked"] for s in stats), 1, stats)
        await timed_ownership.reconcile_purchases()
        self.assertEqual(rows(
            "SELECT status, charged, refunded, stock_returned "
            "FROM shop_purchase_operations"), [("uncertain", 1, 0, 0)])
        self.assertEqual((balance(), quantity("Thing")), (700, None))
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(4,)])
        self.assertEqual(rows("SELECT COUNT(*) FROM purchase_history"), [(0,)])


# ── 6b. cancellation in the middle of an Equip ──────────────────────────────
class EquipCancellationTests(AuditCase):
    """The Discord role is on, the slot row is not written yet, and the
    coroutine is cancelled. Cancellation is requested from inside the Discord
    stub right after it applied the role, and the stub then yields once - so
    it lands deterministically at that point: after the side effect, before
    anything is saved, and never in the middle of a database call."""

    async def run_cancelled(self, cancel):
        """Equip Flame; `cancel(task)` is called right after Discord added it."""
        log = io.StringIO()

        async def add_roles(role, reason=None):
            self.member.roles.append(role)
            cancel(task)
            await asyncio.sleep(0)
        self.member.add_roles = AsyncMock(side_effect=add_roles)
        with contextlib.redirect_stdout(log):
            task = asyncio.ensure_future(
                equip_engine.equip_role(self.bot, GUILD, USER, "Flame"))
            with self.assertRaises(asyncio.CancelledError):
                await task
        return log.getvalue()

    async def test_caller_cancelled_right_after_the_role_was_added(self):
        await self.give_roles()
        log = await self.run_cancelled(lambda task: task.cancel())
        # The Discord step and the slot write ran to their end as one unit;
        # the caller still sees its cancellation.
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual(self.member.add_roles.await_count, 1)       # not re-sent
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertIn("ran to its end after its caller was cancelled: equipped", log)
        self.assertNotIn("UNCERTAIN", log)

    async def test_caller_cancelled_repeatedly_still_finishes_the_unit(self):
        await self.give_roles()
        saving = asyncio.Event()
        release = asyncio.Event()
        real = equip_engine.record_equipped

        async def slow_record(*args, **kwargs):
            saving.set()
            await release.wait()
            return await real(*args, **kwargs)

        with patch.object(equip_engine, "record_equipped", slow_record):
            task = asyncio.ensure_future(
                equip_engine.equip_role(self.bot, GUILD, USER, "Flame"))
            await saving.wait()                   # role on, slot not written
            self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
            for _ in range(3):                    # cancelled again and again
                task.cancel()
                await asyncio.sleep(0.01)
            self.assertFalse(task.done())         # still finishing the unit
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))

    async def test_caller_cancelled_and_the_save_fails_takes_the_role_back(self):
        await self.give_roles()
        with patch.object(equip_engine, "record_equipped", AsyncMock(
                side_effect=sqlite3.OperationalError("database is locked"))):
            log = await self.run_cancelled(lambda task: task.cancel())
        self.assertEqual((slot(), worn(self.member)), ([], []))
        self.assertEqual(self.member.remove_roles.await_count, 1)
        self.assertNotIn("UNCERTAIN", log)

    async def test_caller_cancelled_save_and_revert_both_fail_is_logged_uncertain(self):
        await self.give_roles()
        self.member.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 500"))
        with patch.object(equip_engine, "record_equipped", AsyncMock(
                side_effect=sqlite3.OperationalError("database is locked"))):
            log = await self.run_cancelled(lambda task: task.cancel())
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.assertIn("[EQUIP][UNCERTAIN]", log)
        self.assertEqual(self.member.remove_roles.await_count, 1)    # tried once

    async def test_unit_itself_cancelled_takes_the_role_back(self):
        """Event-loop shutdown cancels every task, the protected unit included.
        The stub runs inside that unit, so it cancels the unit directly."""
        await self.give_roles()
        log = await self.run_cancelled(lambda task: asyncio.current_task().cancel())
        self.assertEqual((slot(), worn(self.member)), ([], []))
        self.assertEqual(self.member.remove_roles.await_count, 1)
        self.assertIn("the role it added was taken back", log)
        self.assertNotIn("UNCERTAIN", log)

    async def test_unit_cancelled_and_revert_fails_is_logged_uncertain(self):
        await self.give_roles()
        self.member.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 500"))
        log = await self.run_cancelled(lambda task: asyncio.current_task().cancel())
        # Nothing claims success or a clean failure: the caller gets its
        # cancellation and the log names the member and the role.
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.assertIn("[EQUIP][UNCERTAIN]", log)
        self.assertIn(str(FLAME), log)
        self.assertIn(str(USER), log)
        self.assertEqual(self.member.remove_roles.await_count, 1)

    async def test_cancelled_swap_never_strips_the_previous_item(self):
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        blue = self.guild.get_role(BLUE)
        log = io.StringIO()

        async def add_roles(role, reason=None):
            self.member.roles.append(role)
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        self.member.add_roles = AsyncMock(side_effect=add_roles)
        with contextlib.redirect_stdout(log):
            with self.assertRaises(asyncio.CancelledError):
                await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))
        self.member.remove_roles.assert_awaited_once()
        self.assertEqual(self.member.remove_roles.await_args.args[0], blue)

    async def test_cancellation_never_removes_a_role_the_member_already_wore(self):
        await self.give_roles()
        self.member.roles.append(self.guild.get_role(FLAME))     # e.g. a level reward

        async def cancelled_record(*args, **kwargs):
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        with patch.object(equip_engine, "record_equipped", cancelled_record):
            with self.assertRaises(asyncio.CancelledError):
                await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.assertEqual(self.member.remove_roles.await_count, 0)

    async def test_slot_lock_is_released_after_a_cancelled_equip(self):
        await self.give_roles()
        await self.run_cancelled(lambda task: asyncio.current_task().cancel())
        slow_discord(self.member)
        result = await asyncio.wait_for(
            equip_engine.equip_role(self.bot, GUILD, USER, "Blue"), timeout=10)
        self.assertTrue(result["success"])
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))


# ── 6c. the slot changes while Unequip is waiting for Discord ───────────────
class UnequipSlotChangeTests(AuditCase):
    async def unequip_flame_while(self, change):
        """Unequip Flame; `change()` runs inside the Discord removal call."""
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.flame_key = await self.key_of("Flame")
        self.blue_key = await self.key_of("Blue")

        async def remove_roles(role, reason=None):
            self.member.roles.remove(role)
            await change()
        self.member.remove_roles = AsyncMock(side_effect=remove_roles)
        self.member.add_roles = AsyncMock(
            side_effect=lambda role, reason=None: self.member.roles.append(role))
        return await equip_engine.unequip_role(
            self.bot, GUILD, USER, "Flame", entry_key=self.flame_key)

    async def test_newer_slot_written_during_the_discord_call_is_preserved(self):
        def blue_unequips():
            return rows("SELECT COUNT(*) FROM inventory_activity WHERE "
                        "event_type='unequipped' AND item_name='Blue'")
        before = None

        async def another_writer_equips_blue():
            nonlocal before
            before = blue_unequips()
            # A writer outside this process's slot lock (another process).
            self.member.roles.append(self.guild.get_role(BLUE))
            await equip_engine.record_equipped(
                GUILD, USER, "Blue", role_id=BLUE, entry_key=self.blue_key)
        result = await self.unequip_flame_while(another_writer_equips_blue)
        self.assertFalse(result["success"])
        self.assertIn("changed while it was being unequipped", result["error"])
        self.assertEqual(rows("SELECT item_name, role_id, entry_key FROM equipped_roles"),
                         [("Blue", BLUE, self.blue_key)])
        self.assertEqual(worn(self.member), ["Blue"])
        self.assertEqual(blue_unequips(), before)    # Blue was not "unequipped"
        self.assertEqual(self.member.add_roles.await_count, 0)

    async def test_a_slot_that_moves_and_cannot_be_read_back_is_uncertain(self):
        """B: an unreadable new record is not a settled failure.

        The slot changed while Flame was being removed, and the record that
        replaced it cannot be read. Nothing is restored (that role may belong to
        the new slot) and nothing is retried, but the caller must not be told
        the operation is settled: whether Flame is still needed is unknown.
        """
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.flame_key = await self.key_of("Flame")
        self.blue_key = await self.key_of("Blue")
        real = equip_engine.get_equipped
        armed = {"on": False}

        async def unreadable_once(*args, **kwargs):
            if armed["on"]:
                raise sqlite3.OperationalError("database is locked")
            return await real(*args, **kwargs)

        async def remove_roles(role, reason=None):
            self.member.roles.remove(role)
            # Another process moves the slot on while the role is off.
            self.member.roles.append(self.guild.get_role(BLUE))
            await equip_engine.record_equipped(
                GUILD, USER, "Blue", role_id=BLUE, entry_key=self.blue_key)
            armed["on"] = True

        self.member.remove_roles = AsyncMock(side_effect=remove_roles)
        self.member.add_roles = AsyncMock(
            side_effect=lambda role, reason=None: self.member.roles.append(role))
        log = io.StringIO()
        with patch.object(equip_engine, "get_equipped", unreadable_once), \
                contextlib.redirect_stdout(log):
            result = await equip_engine.unequip_role(
                self.bot, GUILD, USER, "Flame", entry_key=self.flame_key)
        self.assertFalse(result["success"], result)
        self.assertTrue(result["uncertain"], result)          # B
        self.assertIn("could not be read", result["error"])
        self.assertFalse(result["role_kept"])
        # Nothing was restored over the slot's new owner, and the slot is left
        # exactly as the other writer committed it.
        self.assertEqual(self.member.add_roles.await_count, 0)
        self.assertEqual(slot(), [("Blue", BLUE)])
        self.assertEqual(worn(self.member), ["Blue"])
        self.assertEqual(len([line for line in log.getvalue().splitlines()
                              if "[EQUIP][UNCERTAIN]" in line]), 1)

    async def test_same_entry_key_with_a_different_role_is_not_deleted(self):
        # The row must match on entry, role AND name - not on the key alone.
        async def key_reused_for_another_role():
            execute("UPDATE equipped_roles SET role_id=?, item_name='Blue' "
                    "WHERE guild_id=? AND user_id=?", (BLUE, GUILD, USER))
        result = await self.unequip_flame_while(key_reused_for_another_role)
        self.assertFalse(result["success"])
        self.assertEqual(rows("SELECT item_name, role_id FROM equipped_roles"),
                         [("Blue", BLUE)])
        execute("DELETE FROM equipped_roles")     # planted row, not a real state

    async def test_newer_slot_wearing_the_same_role_gets_its_role_back(self):
        async def another_entry_with_the_same_role():
            execute("UPDATE inventory_items SET metadata=? WHERE item_name='Blue'",
                    (json.dumps({"role_id": FLAME}),))
            await equip_engine.record_equipped(
                GUILD, USER, "Blue", role_id=FLAME, entry_key=self.blue_key)
        result = await self.unequip_flame_while(another_entry_with_the_same_role)
        self.assertFalse(result["success"])
        self.assertEqual(rows("SELECT item_name, role_id FROM equipped_roles"),
                         [("Blue", FLAME)])
        self.assertEqual(worn(self.member), ["Flame"])       # restored
        self.assertEqual(self.member.add_roles.await_count, 1)

    async def test_role_that_cannot_be_restored_is_reported_uncertain(self):
        async def another_entry_with_the_same_role():
            execute("UPDATE inventory_items SET metadata=? WHERE item_name='Blue'",
                    (json.dumps({"role_id": FLAME}),))
            await equip_engine.record_equipped(
                GUILD, USER, "Blue", role_id=FLAME, entry_key=self.blue_key)
            self.member.add_roles = AsyncMock(side_effect=RuntimeError("Discord 500"))
        result = await self.unequip_flame_while(another_entry_with_the_same_role)
        self.assertEqual((result["success"], result.get("uncertain")), (False, True))
        self.assertEqual(rows("SELECT item_name, role_id FROM equipped_roles"),
                         [("Blue", FLAME)])
        self.assertEqual(worn(self.member), [])
        self.assertEqual(self.member.add_roles.await_count, 1)   # tried once


# ── 6c. failures after the Discord role step (no cancellation involved) ─────
class EquipAfterRoleStepTests(AuditCase):
    """Discord added the role; then the equip cannot be recorded.

    Three places: the ownership re-read fails, the ownership entry is gone,
    or Discord itself did not confirm the add. The role this call added is
    taken back unless another system entitles the member to it; whenever the
    Discord state cannot be confirmed the result AND the log say "uncertain".
    Every test asserts the result, the slot, the worn roles and the log.
    """
    async def equip_flame(self, *, reread_fails=False, entry_vanishes=False):
        """Equip Flame with one fault injected right after Discord added it."""
        real_entries = inventory.get_inventory_entries
        added = self.member.add_roles
        state = {"role_on": False}

        async def add_roles(role, reason=None):
            await added(role, reason=reason)
            state["role_on"] = True
            if entry_vanishes:
                execute("DELETE FROM inventory_items WHERE user_id=? AND "
                        "item_name='Flame'", (USER,))

        async def entries(*args, **kwargs):
            if reread_fails and state["role_on"]:
                raise sqlite3.OperationalError("disk I/O error")
            return await real_entries(*args, **kwargs)

        self.member.add_roles = AsyncMock(side_effect=add_roles)
        log = io.StringIO()
        try:
            with patch.object(inventory, "get_inventory_entries", entries), \
                    contextlib.redirect_stdout(log):
                result = await equip_engine.equip_role(
                    self.bot, GUILD, USER, "Flame")
        finally:
            self.member.add_roles = added          # the fault is one-shot
        return result, log.getvalue()

    def entitled(self, reason="level reward role"):
        from utils import timed_ownership
        return patch.object(timed_ownership, "non_slot_role_entitlement",
                            AsyncMock(return_value=reason))

    def uncertain_lines(self, log):
        return [line for line in log.splitlines() if "[EQUIP][UNCERTAIN]" in line]

    def assert_traceable(self, line, stage):
        """The log line names everything needed to check the member by hand."""
        for part in ("operation=equip", f"stage={stage}", f"guild={GUILD}",
                     f"user={USER}", "item='Flame'", f"role={FLAME}", "entry=i:"):
            self.assertIn(part, line)

    # ── the ownership re-read fails ────────────────────────────────────────
    async def test_failed_ownership_reread_is_a_result_and_takes_the_role_back(self):
        await self.give_roles()
        result, log = await self.equip_flame(reread_fails=True)   # does not raise
        self.assertFalse(result["success"])
        self.assertEqual((result["role_added"], result["uncertain"]), (False, False))
        self.assertIn("nothing was changed", result["error"])
        self.assertEqual((slot(), worn(self.member)), ([], []))
        self.assertEqual(self.member.remove_roles.await_count, 1)
        self.assertEqual(self.uncertain_lines(log), [])

    async def test_failed_reread_and_failed_removal_is_uncertain_and_logged(self):
        await self.give_roles()
        self.member.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 500"))
        result, log = await self.equip_flame(reread_fails=True)
        self.assertFalse(result["success"])
        self.assertEqual((result["role_added"], result["uncertain"]), (True, True))
        self.assertIn("reconcile", result["error"])
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.assertEqual(self.member.remove_roles.await_count, 1)     # not retried
        lines = self.uncertain_lines(log)
        self.assertEqual(len(lines), 1)
        self.assert_traceable(lines[0], "ownership_recheck")
        self.member.roles.clear()        # the fixture's own inconsistency

    async def test_failed_reread_through_the_equip_button_gets_a_reply(self):
        await self.give_roles()
        view, _ = await self.panel_of("Flame")
        real_entries = inventory.get_inventory_entries

        async def entries(*args, **kwargs):
            if worn(self.member):
                raise sqlite3.OperationalError("disk I/O error")
            return await real_entries(*args, **kwargs)

        with patch.object(inventory, "get_inventory_entries", entries), \
                contextlib.redirect_stdout(io.StringIO()):
            outcome = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "refused")
        self.assertIn("nothing was changed", outcome[1])
        self.assertEqual((slot(), worn(self.member)), ([], []))

    # ── the ownership entry is gone ────────────────────────────────────────
    async def test_entry_gone_during_the_discord_call_takes_the_role_back(self):
        await self.give_roles()
        result, log = await self.equip_flame(entry_vanishes=True)
        self.assertFalse(result["success"])
        self.assertEqual((result["role_added"], result["uncertain"]), (False, False))
        self.assertIn("nothing was changed", result["error"])
        self.assertEqual((slot(), worn(self.member)), ([], []))
        self.assertEqual(self.member.remove_roles.await_count, 1)
        self.assertEqual(self.uncertain_lines(log), [])

    async def test_entry_gone_and_failed_removal_is_uncertain_and_logged(self):
        await self.give_roles()
        self.member.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 500"))
        result, log = await self.equip_flame(entry_vanishes=True)
        self.assertEqual((result["success"], result["role_added"],
                          result["uncertain"]), (False, True, True))
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        lines = self.uncertain_lines(log)
        self.assertEqual(len(lines), 1)
        self.assert_traceable(lines[0], "ownership_changed")
        self.member.roles.clear()

    # ── another system entitles the member to the same role ────────────────
    async def test_role_covered_by_another_entitlement_is_kept_not_removed(self):
        for fault in ({"entry_vanishes": True}, {"reread_fails": True}):
            with self.subTest(fault):
                await self.give_roles()
                with self.entitled() as check:
                    result, log = await self.equip_flame(**fault)
                self.assertFalse(result["success"])
                self.assertEqual(check.await_count, 1)
                self.assertEqual(check.await_args.args, (GUILD, USER, FLAME))
                self.assertFalse(result["success"])
                self.assertEqual((result["role_added"], result["uncertain"]),
                                 (False, False))
                self.assertTrue(result["role_kept_by_entitlement"])
                self.assertEqual(self.member.remove_roles.await_count, 0)
                self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
                self.assertEqual(self.uncertain_lines(log), [])
                self.assertIn("another entitlement covers it", log)
                self.member.roles.clear()
                execute("DELETE FROM inventory_items WHERE user_id=?", (USER,))

    async def test_failed_slot_write_keeps_a_role_another_entitlement_covers(self):
        await self.give_roles()
        with self.entitled(), patch.object(
                equip_engine, "record_equipped",
                AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual((result["success"], result["uncertain"]), (False, False))
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.member.roles.clear()

    async def test_unverifiable_entitlement_leaves_the_role_and_is_uncertain(self):
        await self.give_roles()
        from utils import timed_ownership
        with patch.object(timed_ownership, "non_slot_role_entitlement",
                          AsyncMock(side_effect=sqlite3.OperationalError("locked"))):
            result, log = await self.equip_flame(entry_vanishes=True)
        self.assertEqual((result["success"], result["role_added"],
                          result["uncertain"]), (False, True, True))
        self.assertEqual(self.member.remove_roles.await_count, 0)   # unknown is not "no"
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.assertEqual(len(self.uncertain_lines(log)), 1)
        self.member.roles.clear()

    # ── a role the member wore before the operation ────────────────────────
    async def test_role_worn_before_the_operation_is_never_removed(self):
        for fault in ({"entry_vanishes": True}, {"reread_fails": True}):
            with self.subTest(fault):
                await self.give_roles()
                self.member.roles.append(self.guild.get_role(FLAME))
                real_entries = inventory.get_inventory_entries
                calls = {"n": 0}

                async def entries(*args, **kwargs):
                    # No Discord add happens here, so the fault is injected at
                    # the re-read itself: the second read of this equip.
                    calls["n"] += 1
                    if calls["n"] == 2:
                        if fault.get("reread_fails"):
                            raise sqlite3.OperationalError("disk I/O error")
                        execute("DELETE FROM inventory_items WHERE user_id=? "
                                "AND item_name='Flame'", (USER,))
                    return await real_entries(*args, **kwargs)

                log = io.StringIO()
                with patch.object(inventory, "get_inventory_entries", entries), \
                        contextlib.redirect_stdout(log):
                    result = await equip_engine.equip_role(
                        self.bot, GUILD, USER, "Flame")
                self.assertEqual(calls["n"], 2)
                self.assertFalse(result["success"])
                self.assertEqual((result["role_added"], result["uncertain"]),
                                 (False, False))
                self.assertEqual(self.member.add_roles.await_count, 0)
                self.assertEqual(self.member.remove_roles.await_count, 0)
                self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
                self.assertEqual(self.uncertain_lines(log.getvalue()), [])
                self.member.roles.clear()
                execute("DELETE FROM inventory_items WHERE user_id=?", (USER,))

    # ── Discord did not confirm the add ────────────────────────────────────
    async def test_discord_applied_the_role_but_timed_out(self):
        await self.give_roles()

        async def add_roles(role, reason=None):
            self.member.roles.append(role)
            raise asyncio.TimeoutError()

        self.member.add_roles = AsyncMock(side_effect=add_roles)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual((result["success"], result["role_added"],
                          result["uncertain"]), (False, True, True))
        self.assertIn("may or may not have been applied", result["error"])
        # Deliberately neither retried nor undone.
        self.assertEqual(self.member.add_roles.await_count, 1)
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        lines = self.uncertain_lines(log.getvalue())
        self.assertEqual(len(lines), 1)
        self.assert_traceable(lines[0], "discord_add")
        self.assertIn("role visible on the member: True", lines[0])
        self.member.roles.clear()

    async def test_discord_timeout_with_no_role_visible_is_still_uncertain(self):
        await self.give_roles()
        self.member.add_roles = AsyncMock(side_effect=asyncio.TimeoutError())
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual((result["success"], result["role_added"],
                          result["uncertain"]), (False, False, True))
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([], []))
        lines = self.uncertain_lines(log.getvalue())
        self.assertEqual(len(lines), 1)
        self.assertIn("role visible on the member: False", lines[0])

    async def test_definite_discord_refusal_is_a_clean_failure_without_the_log(self):
        await self.give_roles()
        refused = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                    "Missing Permissions")
        self.member.add_roles = AsyncMock(side_effect=refused)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual((result["success"], result["role_added"],
                          result["uncertain"]), (False, False, False))
        self.assertNotIn("may or may not", result["error"])
        self.assertEqual(self.uncertain_lines(log.getvalue()), [])
        self.assertEqual((slot(), worn(self.member)), ([], []))

    # ── the slot cannot be read back ───────────────────────────────────────
    # A failed read is not proof that the slot is empty: the role stays on.
    def slot_unreadable_once(self, armed):
        real = equip_engine.get_equipped

        async def get_equipped(*args, **kwargs):
            if armed["on"]:
                raise sqlite3.OperationalError("database is locked")
            return await real(*args, **kwargs)
        return patch.object(equip_engine, "get_equipped", get_equipped)

    def committing_then_raising(self, armed):
        real = equip_engine.record_equipped

        async def record(*args, **kwargs):
            await real(*args, **kwargs)                 # the row IS committed
            armed["on"] = True
            raise sqlite3.OperationalError("disk I/O error")
        return patch.object(equip_engine, "record_equipped", record)

    async def test_unreadable_slot_never_removes_a_role_the_slot_records(self):
        await self.give_roles()
        armed, log = {"on": False}, io.StringIO()
        with self.committing_then_raising(armed), self.slot_unreadable_once(armed), \
                contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual((result["success"], result["role_added"],
                          result["uncertain"]), (False, True, True))
        lines = self.uncertain_lines(log.getvalue())
        self.assertEqual(len(lines), 1)
        self.assert_traceable(lines[0], "slot_write")

    async def test_unreadable_slot_during_a_swap_keeps_the_new_role(self):
        # The previous role is released BEFORE the slot is written (so a swap
        # that cannot remove it never overwrites the old record). Here the new
        # row commits and the slot then becomes unreadable: the swap itself is
        # durable, the old role was correctly taken off, and the new role is
        # kept because the slot cannot be read back to confirm it.
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        removed_before = self.member.remove_roles.await_count
        armed = {"on": False}
        with self.committing_then_raising(armed), self.slot_unreadable_once(armed), \
                contextlib.redirect_stdout(io.StringIO()):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual(self.member.remove_roles.await_count, removed_before + 1)
        self.assertEqual(self.member.remove_roles.await_args.args[0].id, BLUE)
        self.assertEqual(slot(), [("Flame", FLAME)])
        self.assertEqual(worn(self.member), ["Flame"])   # old off, new kept
        self.assertTrue(result["uncertain"])

    async def test_unreadable_slot_after_a_failed_write_is_uncertain_not_removed(self):
        # Nothing was written here, but the helper cannot know that.
        await self.give_roles()
        armed, log = {"on": False}, io.StringIO()

        async def record(*args, **kwargs):
            armed["on"] = True
            raise sqlite3.OperationalError("database is locked")

        with patch.object(equip_engine, "record_equipped", record), \
                self.slot_unreadable_once(armed), contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((result["success"], result["role_added"],
                          result["uncertain"]), (False, True, True))
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.assertEqual(len(self.uncertain_lines(log.getvalue())), 1)
        self.member.roles.clear()

    async def test_cancelled_after_the_slot_was_saved_with_unreadable_slot(self):
        await self.give_roles()
        armed, log = {"on": False}, io.StringIO()
        real = equip_engine.record_equipped

        async def record(*args, **kwargs):
            await real(*args, **kwargs)
            armed["on"] = True
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

        with patch.object(equip_engine, "record_equipped", record), \
                self.slot_unreadable_once(armed), contextlib.redirect_stdout(log):
            task = asyncio.ensure_future(
                equip_engine.equip_role(self.bot, GUILD, USER, "Flame"))
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))
        self.assertIn("[EQUIP][UNCERTAIN]", log.getvalue())

    # ── a role must not vouch for itself (Booster "already worn" rule) ──────
    # Nothing about entitlement is stubbed below unless a test says so: the
    # real non_slot_role_entitlement and Booster rules run. A live
    # discord.Member carries no boost count, so every active booster is
    # "count unknown", and the Booster rule then lets a level-2 role the
    # member already WEARS count as theirs. The role an equip has just added
    # is worn too, and Member.add_roles does not update member.roles (the
    # gateway echo does, later), so the answer used to depend on timing.

    def discord_state(self, *, echo, during_add=None):
        """Keep what Discord HAS apart from what the member cache SHOWS.

        Returns the set of role ids really on the member. `echo=False` is the
        real client between a role call and its gateway event: the call
        reaches Discord, member.roles does not change.
        """
        on_discord = set()

        async def add_roles(role, reason=None):
            await asyncio.sleep(0.01)
            on_discord.add(role.id)
            if echo and role not in self.member.roles:
                self.member.roles.append(role)
            if during_add:
                during_add(on_discord)

        async def remove_roles(role, reason=None):
            await asyncio.sleep(0.01)
            on_discord.discard(role.id)
            if role in self.member.roles:
                self.member.roles.remove(role)

        self.member.add_roles = AsyncMock(side_effect=add_roles)
        self.member.remove_roles = AsyncMock(side_effect=remove_roles)
        return on_discord

    async def real_entitlement(self):
        return await timed_ownership.non_slot_role_entitlement(
            GUILD, USER, FLAME, bot=self.bot, member=self.member)

    async def equip_with_failed_write(self):
        log = io.StringIO()
        with patch.object(equip_engine, "record_equipped", AsyncMock(
                side_effect=sqlite3.OperationalError("database is locked"))), \
                contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        return result, log.getvalue()

    def outcome(self, result):
        return (result["success"], result["role_added"], result["uncertain"],
                bool(result.get("role_kept_by_entitlement")))

    def assert_kept_uncertain(self, result, log):
        """Kept, NOT called an entitlement, and one line an admin can act on."""
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual(slot(), [])
        self.assertEqual(self.outcome(result), (False, True, True, False))
        lines = self.uncertain_lines(log)
        self.assertEqual(len(lines), 1)
        self.assert_traceable(lines[0], "slot_write")

    async def test_booster_level2_colour_added_by_the_equip_is_not_an_entitlement(self):
        await self.booster(colour_level=2)
        self.assertIsNone(await self.real_entitlement())     # before the equip
        result, log = await self.equip_with_failed_write()
        self.assertEqual(worn(self.member), ["Flame"])
        self.assert_kept_uncertain(result, log)

    async def test_booster_boost2_role_added_by_the_equip_is_not_an_entitlement(self):
        await self.booster(boost2=True)
        self.assertIsNone(await self.real_entitlement())
        result, log = await self.equip_with_failed_write()
        self.assertEqual(worn(self.member), ["Flame"])
        self.assert_kept_uncertain(result, log)

    async def test_booster_fallback_role_when_the_unit_is_cancelled(self):
        await self.booster(colour_level=2)

        async def add_roles(role, reason=None):
            self.member.roles.append(role)
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        self.member.add_roles = AsyncMock(side_effect=add_roles)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            with self.assertRaises(asyncio.CancelledError):
                await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.assertEqual(len(self.uncertain_lines(log.getvalue())), 1)
        self.assertNotIn("another entitlement covers", log.getvalue())

    async def test_proven_boost_count_still_keeps_the_role_as_an_entitlement(self):
        await self.booster(colour_level=2, count=2)          # two boosts KNOWN
        self.assertTrue(await self.real_entitlement())
        result, log = await self.equip_with_failed_write()
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        self.assertEqual(self.outcome(result), (False, False, False, True))
        self.assertEqual(self.uncertain_lines(log), [])

    async def test_level1_booster_colour_still_keeps_the_role_as_an_entitlement(self):
        await self.booster(colour_level=1)
        self.assertTrue(await self.real_entitlement())
        result, log = await self.equip_with_failed_write()
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual(worn(self.member), ["Flame"])
        self.assertEqual(self.outcome(result), (False, False, False, True))

    async def test_booster_role_worn_before_the_equip_is_never_touched(self):
        await self.booster(colour_level=2)
        self.member.roles.append(self.guild.get_role(FLAME))
        result, log = await self.equip_with_failed_write()
        self.assertEqual((self.member.add_roles.await_count,
                          self.member.remove_roles.await_count), (0, 0))
        self.assertEqual(worn(self.member), ["Flame"])
        self.assertEqual(self.outcome(result), (False, False, False, False))
        self.assertEqual(self.uncertain_lines(log), [])

    async def test_recorded_entitlement_granted_during_the_equip_keeps_the_role(self):
        # An MVP award written while Discord is awaited: a check taken before
        # the add would miss it. No echo, so only the record can speak.
        await self.booster(boosting=False)

        def crown(on_discord):
            execute("INSERT OR REPLACE INTO mvp_config (guild_id,enabled,"
                    "mvp_role_id) VALUES (?,1,?)", (GUILD, FLAME))
            execute("INSERT INTO mvp_history (guild_id,user_id,user_display_name,"
                    "cycle_start,cycle_end,score) VALUES (?,?,'M','2026-11-12',"
                    "'2026-11-13',1)", (GUILD, USER))
        on_discord = self.discord_state(echo=False, during_add=crown)
        self.assertIsNone(await self.real_entitlement())
        result, log = await self.equip_with_failed_write()
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertIn(FLAME, on_discord)
        self.assertEqual(self.outcome(result), (False, False, False, True))
        self.assertEqual(self.uncertain_lines(log), [])

    async def test_failed_entitlement_check_at_either_stage_is_uncertain(self):
        await self.booster(colour_level=2)
        real = timed_ownership.non_slot_role_entitlement
        for failing_call in (1, 2):
            with self.subTest(failing_call=failing_call):
                self.member.roles.clear()
                slow_discord(self.member)
                calls = []

                async def check(*args, **kwargs):
                    calls.append(1)
                    if len(calls) == failing_call:
                        raise timed_ownership.EntitlementCheckError(
                            "boost tables unreadable")
                    return await real(*args, **kwargs)
                log = io.StringIO()
                with patch.object(equip_engine, "record_equipped", AsyncMock(
                side_effect=sqlite3.OperationalError("database is locked"))), \
                        patch.object(timed_ownership, "non_slot_role_entitlement",
                                     check), contextlib.redirect_stdout(log):
                    result = await equip_engine.equip_role(
                        self.bot, GUILD, USER, "Flame")
                self.assertEqual(len(calls), failing_call)
                self.assertEqual(worn(self.member), ["Flame"])
                self.assert_kept_uncertain(result, log.getvalue())

    async def test_booster_with_unreadable_slot_after_a_committed_write(self):
        await self.booster(colour_level=2)
        armed, log = {"on": False}, io.StringIO()
        with self.committing_then_raising(armed), self.slot_unreadable_once(armed), \
                contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual(self.outcome(result), (False, True, True, False))
        self.assertEqual(len(self.uncertain_lines(log.getvalue())), 1)

    async def test_booster_with_readable_slot_after_a_committed_write(self):
        await self.booster(colour_level=2)
        armed, log = {"on": False}, io.StringIO()
        with self.committing_then_raising(armed), contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual(self.outcome(result), (False, True, True, False))
        self.assertIn("the slot names this role", log.getvalue())

    async def test_stale_cache_does_not_remove_a_booster_role_granted_earlier(self):
        # The role is on Discord already (put there outside this bot), but the
        # member cache has not caught up, so the equip believes it added it.
        await self.booster(colour_level=2)
        on_discord = self.discord_state(echo=False)
        on_discord.add(FLAME)
        result, log = await self.equip_with_failed_write()
        self.assertIn(FLAME, on_discord)
        self.assert_kept_uncertain(result, log)

    async def test_booster_role_granted_during_the_equip_is_not_removed(self):
        await self.booster(colour_level=2)
        on_discord = self.discord_state(
            echo=False, during_add=lambda held: held.add(FLAME))
        result, log = await self.equip_with_failed_write()
        self.assertIn(FLAME, on_discord)
        self.assert_kept_uncertain(result, log)

    async def test_booster_fallback_outcome_does_not_depend_on_the_gateway_echo(self):
        await self.booster(colour_level=2)
        seen = {}
        for echo in (True, False):
            self.member.roles.clear()
            on_discord = self.discord_state(echo=echo)
            result, log = await self.equip_with_failed_write()
            self.assert_kept_uncertain(result, log)
            seen[echo] = (self.outcome(result), sorted(on_discord),
                          self.member.remove_roles.await_count)
        self.assertEqual(seen[True], seen[False])
        self.assertEqual(seen[True][1], [FLAME])

    # ── anything unforeseen after the role step ────────────────────────────
    async def test_unexpected_error_after_the_role_step_is_uncertain_not_raised(self):
        await self.give_roles()
        log = io.StringIO()
        with patch.object(equip_engine, "_take_back_unsaved_role",
                          AsyncMock(side_effect=RuntimeError("bug"))), \
                patch.object(equip_engine, "record_equipped",
                             AsyncMock(side_effect=sqlite3.OperationalError("locked"))), \
                contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual((result["success"], result["role_added"],
                          result["uncertain"]), (False, True, True))
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        lines = self.uncertain_lines(log.getvalue())
        self.assertEqual(len(lines), 1)
        self.assert_traceable(lines[0], "unexpected_error")
        self.member.roles.clear()


# ── 7. Discord failure / uncertain outcome ──────────────────────────────────
class DiscordFailureTests(AuditCase):
    async def test_role_that_could_not_be_added_changes_nothing(self):
        await self.give_roles()
        self.member.add_roles = AsyncMock(side_effect=RuntimeError("Discord 500"))
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"])
        self.assertEqual((slot(), worn(self.member)), ([], []))
        self.assertEqual(self.member.add_roles.await_count, 1)      # no auto-retry

    async def test_role_that_could_not_be_removed_stays_equipped(self):
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.member.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 500"))
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"])
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual(self.member.remove_roles.await_count, 1)   # no auto-retry

    async def test_equip_that_cannot_be_saved_takes_its_role_back(self):
        await self.give_roles()
        with patch.object(equip_engine, "record_equipped",
                          AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"])
        self.assertEqual((result["role_added"], result["uncertain"]), (False, False))
        self.assertIn("nothing was changed", result["error"])
        self.assertEqual((slot(), worn(self.member)), ([], []))
        # The follow-up that used to end with two roles worn:
        self.assertTrue((await equip_engine.equip_role(self.bot, GUILD, USER, "Blue"))["success"])
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))

    async def test_failed_swap_keeps_the_previous_item_fully_equipped(self):
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        with patch.object(equip_engine, "record_equipped",
                          AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        self.assertFalse(result["success"])
        self.assertEqual((slot(), worn(self.member)), ([("Flame", FLAME)], ["Flame"]))

    async def test_revert_never_removes_a_role_the_member_already_wore(self):
        await self.give_roles()
        self.member.roles.append(self.guild.get_role(FLAME))   # e.g. a level reward
        with patch.object(equip_engine, "record_equipped",
                          AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"])
        self.assertEqual(worn(self.member), ["Flame"])
        self.assertEqual(self.member.remove_roles.await_count, 0)

    async def test_when_the_revert_fails_too_the_outcome_is_reported_uncertain(self):
        await self.give_roles()
        self.member.remove_roles = AsyncMock(side_effect=RuntimeError("Discord 500"))
        with patch.object(equip_engine, "record_equipped",
                          AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"])
        self.assertEqual((result["role_added"], result["uncertain"]), (True, True))
        self.assertIn("reconcile", result["error"])
        self.assertEqual(self.member.remove_roles.await_count, 1)   # tried once only

    async def test_engine_error_in_a_button_gets_a_reply(self):
        await self.give_roles()
        view, _ = await self.panel_of("Flame")
        with patch.object(wallet, "equip_role",
                          AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))):
            outcome = await self.click(view, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "refused")
        self.assertIn("Open the item again", outcome[1])


# ── 8. stale panels, unauthorized users, labels, limits ─────────────────────
class PanelTests(AuditCase):
    async def test_stale_equip_panel_after_the_slot_changed_is_refused(self):
        await self.give_roles()
        stale, _ = await self.panel_of("Flame")
        await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        outcome = await self.click(stale, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "refused")
        self.assertIn("changed", outcome[1])
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))

    async def test_stale_unequip_panel_does_not_clear_another_item(self):
        await self.give_roles()
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        stale, _ = await self.panel_of("Flame")
        self.assertEqual(actions.labels(stale), ["Unequip", "Back"])
        await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        outcome = await self.click(stale, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "refused", outcome)
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))

    async def test_panel_of_an_item_that_is_gone_is_refused(self):
        await self.give_roles()
        stale, _ = await self.panel_of("Flame")
        execute("UPDATE inventory_items SET quantity=0 WHERE item_name='Flame'")
        outcome = await self.click(stale, wallet.EquipToggleButton)
        self.assertEqual(outcome[0], "refused")
        self.assertIn("no longer", outcome[1])
        self.assertEqual((slot(), worn(self.member)), ([], []))

    async def test_stale_potion_panel_after_the_stack_was_used_up(self):
        await inventory.give_item(GUILD, USER, "Tonic", 1, item_type="potion",
                                  metadata=POTION)
        stale, _ = await self.panel_of("Tonic")
        await potion_engine.use_potion(GUILD, USER, "Tonic")
        outcome = await self.click(stale, wallet.ActivateButton)
        self.assertEqual(outcome[0], "refused")
        self.assertEqual((quantity("Tonic"), len(boosts())), (0, 1))

    async def test_other_members_and_other_guilds_cannot_press_anything(self):
        await self.give_roles()
        await inventory.give_item(GUILD, USER, "Tonic", 1, item_type="potion",
                                  metadata=POTION)
        role_panel, _ = await self.panel_of("Flame")
        potion_panel, _ = await self.panel_of("Tonic")
        for view, button in ((role_panel, wallet.EquipToggleButton),
                             (role_panel, wallet.ItemBackButton),
                             (potion_panel, wallet.ActivateButton)):
            for who in ({"user": OTHER_USER}, {"guild": OTHER_GUILD}):
                outcome = await self.click(view, button, **who)
                self.assertEqual(outcome[0], "denied", (button, who))
                self.assertIn("not your wallet", outcome[1])
        self.assertEqual((slot(), worn(self.member), quantity("Tonic"), boosts()),
                         ([], [], 1, []))

    async def test_labels_colours_and_component_limits(self):
        await self.give_roles()
        await inventory.give_item(GUILD, USER, "Tonic", 1, item_type="potion",
                                  metadata=POTION)
        await inventory.give_item(GUILD, USER, "Sage", 1, item_type="title")
        await inventory.give_item(GUILD, USER, "Shell", 1, item_type="custom")
        style = discord.ButtonStyle

        def look(view):
            return [(c.label, c.style) for c in view.children]

        async def panel(name):
            return (await self.panel_of(name))[0]

        self.assertEqual(look(await panel("Flame")),
                         [("Equip", style.success), ("Back", style.secondary)])
        await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertEqual(look(await panel("Flame")),
                         [("Unequip", style.secondary), ("Back", style.secondary)])
        self.assertEqual(look(await panel("Sage")),
                         [("Equip", style.success), ("Back", style.secondary)])
        await title_engine.equip_title(GUILD, USER, "Sage")
        self.assertEqual(look(await panel("Sage")),
                         [("Unequip", style.secondary), ("Back", style.secondary)])
        self.assertEqual(look(await panel("Tonic")),
                         [("Activate", style.success), ("Back", style.secondary)])
        self.assertEqual(look(await panel("Shell")), [("Back", style.secondary)])

        # No destructive (red) control exists on any inventory surface, and
        # every view fits Discord's limits: 5 rows, 5 buttons per row, 25 total.
        views = list(self._views)
        for tab in wallet.TAB_ORDER:
            views.append((await self.open_tab(tab))[0])
        for view in views:
            self.assertLessEqual(len(view.children), 25)
            per_row = {}
            for child in view.children:
                width = 5 if isinstance(child, discord.ui.Select) else 1
                per_row[child.row] = per_row.get(child.row, 0) + width
                if isinstance(child, discord.ui.Button):
                    self.assertNotEqual(child.style, style.danger, child.label)
            explicit = {row: used for row, used in per_row.items() if row is not None}
            self.assertTrue(all(0 <= row <= 4 for row in explicit), explicit)
            self.assertTrue(all(used <= 5 for used in explicit.values()), explicit)
            self.assertLessEqual(per_row.get(None, 0), 5 * (5 - len(explicit)))

    async def test_timed_out_panel_disables_its_buttons(self):
        await self.give_roles()
        view, _ = await self.panel_of("Flame")
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))


# ── 9. the integrity checker itself ─────────────────────────────────────────
class IntegrityCheckerTests(actions.InventoryActionCase):
    async def test_it_reports_each_kind_of_planted_corruption(self):
        """One corruption at a time, so no check can hide behind another."""
        role_meta = json.dumps({"role_id": FLAME})
        own_flame = ("INSERT INTO inventory_items (id,guild_id,user_id,item_name,"
                     "item_type,quantity,metadata) VALUES (1,?,?,'Flame','role',1,?)",
                     (GUILD, USER, role_meta))
        cases = {
            "negative wallet": [
                ("UPDATE economy SET balance=-5 WHERE user_id=?", (USER,))],
            "negative wallet (diamonds)": [
                ("UPDATE economy SET diamonds=-1 WHERE user_id=?", (USER,))],
            "negative inventory quantity": [
                ("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,"
                 "quantity) VALUES (?,?,'Ghost','custom',-1)", (GUILD, USER))],
            "negative timed quantity": [
                ("INSERT INTO timed_holdings (guild_id,user_id,item_name,item_type,"
                 "quantity,expires_at) VALUES (?,?,'T','temp_role',-1,'2099-01-01')",
                 (GUILD, USER))],
            "negative stock": [
                ("INSERT INTO shop_items (guild_id,name,type,price,enabled,max_stock,"
                 "current_stock) VALUES (?,'X','custom',1,1,1,-1)", (GUILD,))],
            "purchase both delivered and refunded": [
                ("INSERT INTO shop_purchase_operations (op_id,guild_id,user_id,"
                 "item_name,currency,amount,status,charged,refunded) VALUES "
                 "('o',?,?,'X','balance',1,'delivered',1,1)", (GUILD, USER))],
            "equipped title that is not owned": [
                ("INSERT INTO equipped_titles (guild_id,user_id,item_name) "
                 "VALUES (?,?,'Sage')", (GUILD, USER))],
            "points at an entry that does not exist": [
                ("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id) "
                 "VALUES (?,?,'Flame',?)", (GUILD, USER, FLAME))],
            "points at an entry that does not exist (keyed)": [
                own_flame,
                ("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
                 "entry_key) VALUES (?,?,'Flame',?,'t:999')", (GUILD, USER, FLAME))],
            "is not owned (quantity 0)": [
                own_flame,
                ("UPDATE inventory_items SET quantity=0 WHERE id=1", ()),
                ("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
                 "entry_key) VALUES (?,?,'Flame',?,'i:1')", (GUILD, USER, FLAME))],
            "wears a non-role item (potion)": [
                ("INSERT INTO inventory_items (id,guild_id,user_id,item_name,"
                 "item_type,quantity) VALUES (1,?,?,'Flame','potion',1)", (GUILD, USER)),
                ("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id) "
                 "VALUES (?,?,'Flame',?)", (GUILD, USER, FLAME))],
            f"slot role {BLUE} != item role {FLAME}": [
                own_flame,
                ("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
                 "entry_key) VALUES (?,?,'Flame',?,'i:1')", (GUILD, USER, BLUE))],
            "entry is named 'Flame'": [
                own_flame,
                ("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
                 "entry_key) VALUES (?,?,'Other',?,'i:1')", (GUILD, USER, FLAME))],
            "timed holding expired": [
                ("INSERT INTO timed_holdings (id,guild_id,user_id,item_name,item_type,"
                 "quantity,role_id,expires_at) VALUES (1,?,?,'Old','temp_role',1,?,"
                 "'2001-01-01T00:00:00+00:00')", (GUILD, USER, FLAME)),
                ("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
                 "entry_key) VALUES (?,?,'Old',?,'t:1')", (GUILD, USER, FLAME))],
            "unknown entry key": [
                own_flame,
                ("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
                 "entry_key) VALUES (?,?,'Flame',?,'x:1')", (GUILD, USER, FLAME))],
        }
        needles = {
            "negative wallet (diamonds)": "negative wallet",
            "points at an entry that does not exist (keyed)":
                "points at an entry that does not exist",
        }
        for label, statements in cases.items():
            with self.subTest(label):
                await reset_database()
                seed_member(balance=1000)
                self.assertEqual(integrity_problems(), [])
                for sql, args in statements:
                    execute(sql, args)
                found = " | ".join(integrity_problems())
                self.assertIn(needles.get(label, label), found)
        await reset_database()

    async def test_it_accepts_every_legitimate_equipped_state(self):
        """No false alarms: permanent, legacy (no key) and live timed slots."""
        seed_member(balance=1000)
        role_meta = json.dumps({"role_id": FLAME})
        execute("INSERT INTO inventory_items (id,guild_id,user_id,item_name,item_type,"
                "quantity,metadata) VALUES (1,?,?,'Flame','role',1,?)",
                (GUILD, USER, role_meta))
        execute("INSERT INTO timed_holdings (id,guild_id,user_id,item_name,item_type,"
                "quantity,role_id,expires_at) VALUES (1,?,?,'Timed','temp_role',1,?,?)",
                (GUILD, USER, BLUE,
                 (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()))
        for name, role_id, key in (("Flame", FLAME, "i:1"), ("Flame", FLAME, None),
                                   ("Timed", BLUE, "t:1")):
            execute("DELETE FROM equipped_roles")
            execute("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
                    "entry_key) VALUES (?,?,?,?,?)", (GUILD, USER, name, role_id, key))
            self.assertEqual(integrity_problems(), [], (name, key))

    async def test_the_schema_itself_refuses_duplicates(self):
        seed_member(balance=1000)
        await inventory.give_item(GUILD, USER, "Shell", 1, item_type="custom")
        for sql, args in (
            ("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,"
             "quantity) VALUES (?,?,'Shell','custom',1)", (GUILD, USER)),
            ("INSERT INTO economy (guild_id,user_id,balance,diamonds) "
             "VALUES (?,?,1,1)", (GUILD, USER)),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                execute(sql, args)
        execute("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id) "
                "VALUES (?,?,'A',1)", (GUILD, USER))
        with self.assertRaises(sqlite3.IntegrityError):
            execute("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id) "
                    "VALUES (?,?,'B',2)", (GUILD, USER))
        execute("DELETE FROM equipped_roles")


# ── 8. role-observation metadata on equipped_roles ──────────────────────────
class LegacySchemaMigrationTests(AuditCase):
    """`init_db()` on a database created BEFORE the two columns existed."""

    async def test_the_real_alter_path_keeps_rows_and_invents_nothing(self):
        import database
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "legacy.db")
        # The table exactly as `2565221` created it, with a live row.
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE equipped_roles (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                item_name  TEXT    NOT NULL,
                role_id    INTEGER NOT NULL,
                equipped_at TEXT   DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id))""")
        conn.execute("INSERT INTO equipped_roles "
                     "(guild_id,user_id,item_name,role_id,equipped_at) "
                     "VALUES (?,?,?,?,?)",
                     (GUILD, USER, "Flame", FLAME, "2026-01-01 00:00:00"))
        conn.commit()
        conn.close()

        with patch.object(database, "DB_PATH", path):
            await database.init_db()
            await database.init_db()      # a second start must be a no-op

        conn = sqlite3.connect(path)
        try:
            columns = {row[1] for row in
                       conn.execute("PRAGMA table_info(equipped_roles)")}
            self.assertLessEqual({"entry_key", "role_added",
                                  "role_added_role_id"}, columns)
            row = conn.execute(
                "SELECT item_name, role_id, entry_key, role_added, "
                "role_added_role_id, equipped_at FROM equipped_roles").fetchone()
            self.assertEqual(row[:5], ("Flame", FLAME, None, None, None))
            self.assertEqual(row[5], "2026-01-01 00:00:00",
                             "the migration must not touch existing rows")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM equipped_roles").fetchone()[0], 1)
        finally:
            conn.close()

# ── 8. role-observation metadata on equipped_roles ──────────────────────────
class RoleObservationMetadataTests(AuditCase):
    """`equipped_roles` records what the writing equip OBSERVED about the role.

    `role_added` is 1 (seen absent, add sent), 0 (already on the member) or
    NULL (unknown); `role_added_role_id` binds that observation to the Discord
    role it is about. The binding is what makes the metadata rollback-safe: an
    older writer only knows the old columns, so it rewrites `role_id` and
    leaves the observation behind — a value recorded for a DIFFERENT role is
    irrelevant to the role the row now names and must read as unknown.
    """

    def observation(self):
        return rows("SELECT role_added, role_added_role_id FROM equipped_roles")

    def legacy_upsert(self, item_name, role_id, entry_key=None):
        """The exact upsert an OLDER `record_equipped` performed."""
        execute("""
            INSERT INTO equipped_roles
                (guild_id, user_id, item_name, role_id, entry_key, equipped_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                item_name   = excluded.item_name,
                role_id     = excluded.role_id,
                entry_key   = excluded.entry_key,
                equipped_at = CURRENT_TIMESTAMP
        """, (GUILD, USER, item_name, int(role_id), entry_key))

    async def test_migration_adds_the_columns_and_repeats_safely(self):
        columns = {row[1] for row in rows("PRAGMA table_info(equipped_roles)")}
        self.assertIn("role_added", columns)
        self.assertIn("role_added_role_id", columns)
        # A row written by the OLD writer is unknown, not "observed present".
        self.legacy_upsert("Flame", FLAME)
        self.assertEqual(self.observation(), [(None, None)])
        # The migration is a no-op on a database that already has them, and it
        # does not invent anything for the row that is there.
        from database import init_db
        await init_db()
        await init_db()
        self.assertEqual(self.observation(), [(None, None)])
        state = await equip_engine.get_equipped(GUILD, USER)
        self.assertIsNone(equip_engine.observed_role_state(state))
        execute("DELETE FROM equipped_roles")

    async def test_a_legacy_row_has_no_usable_observation(self):
        self.legacy_upsert("Flame", FLAME)
        state = await equip_engine.get_equipped(GUILD, USER)
        self.assertIsNone(state["role_added"])
        self.assertIsNone(state["role_added_role_id"])
        self.assertIsNone(equip_engine.observed_role_state(state))
        execute("DELETE FROM equipped_roles")

    async def test_an_older_writer_changing_the_role_leaves_unknown_metadata(self):
        await self.give_roles()
        flame_key, blue_key = await self.key_of("Flame"), await self.key_of("Blue")
        await equip_engine.record_equipped(
            GUILD, USER, "Flame", role_id=FLAME, entry_key=flame_key,
            role_added=True)
        self.assertEqual(self.observation(), [(1, FLAME)])
        # A rollback deploy (or any writer that only knows the old columns)
        # overwrites the slot with ANOTHER role and cannot clear the metadata.
        self.legacy_upsert("Blue", BLUE, blue_key)
        self.assertEqual(self.observation(), [(1, FLAME)], "stale, not updated")
        state = await equip_engine.get_equipped(GUILD, USER)
        self.assertEqual((state["item_name"], state["role_id"]), ("Blue", BLUE))
        self.assertIsNone(equip_engine.observed_role_state(state),
                          "an observation about Flame says nothing about Blue")
        # ... and that unknown state is never acted on.
        self.member.roles.append(self.guild.get_role(BLUE))
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Blue")
        self.assertFalse(result["success"], result)
        self.assertTrue(result["uncertain"])
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))

    async def test_an_older_writer_with_the_same_role_keeps_it_bound(self):
        await self.give_roles()
        holding = await timed_ownership.grant_timed_holding(
            GUILD, USER, "Timed Flame", expires_at=timed_ownership.expiry_from_hours(2),
            item_type="temp_role", metadata={"role_id": FLAME}, role_id=FLAME,
            source="shop")
        timed_key = f"h:{holding}"
        flame_key = await self.key_of("Flame")
        await equip_engine.record_equipped(
            GUILD, USER, "Flame", role_id=FLAME, entry_key=flame_key,
            role_added=True)
        # Same Discord role, a changed item AND entry key, written by the old
        # writer: the role never changed hands, so the observation still holds.
        self.legacy_upsert("Timed Flame", FLAME, timed_key)
        state = await equip_engine.get_equipped(GUILD, USER)
        self.assertEqual((state["item_name"], state["entry_key"]),
                         ("Timed Flame", timed_key))
        self.assertIs(equip_engine.observed_role_state(state), True)

    async def test_a_changed_entry_key_on_the_same_role_still_allows_removal(self):
        await self.give_roles()
        holding = await timed_ownership.grant_timed_holding(
            GUILD, USER, "Timed Flame", expires_at=timed_ownership.expiry_from_hours(2),
            item_type="temp_role", metadata={"role_id": FLAME}, role_id=FLAME,
            source="shop")
        timed_key = f"h:{holding}"
        await equip_engine.record_equipped(
            GUILD, USER, "Flame", role_id=FLAME,
            entry_key=await self.key_of("Flame"), role_added=True)
        self.legacy_upsert("Timed Flame", FLAME, timed_key)
        self.member.roles.append(self.guild.get_role(FLAME))
        result = await equip_engine.unequip_role(
            self.bot, GUILD, USER, "Timed Flame", entry_key=timed_key)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["role_removed"])
        self.assertEqual((slot(), worn(self.member)), ([], []))

    async def test_a_new_writer_without_an_observation_stores_unknown(self):
        await self.give_roles()
        await equip_engine.record_equipped(
            GUILD, USER, "Flame", role_id=FLAME,
            entry_key=await self.key_of("Flame"), role_added=None)
        self.assertEqual(self.observation(), [(None, None)])
        execute("DELETE FROM equipped_roles")

    async def test_a_same_role_equip_inherits_but_a_new_role_does_not(self):
        await self.give_roles()
        flame_key, blue_key = await self.key_of("Flame"), await self.key_of("Blue")
        await equip_engine.record_equipped(
            GUILD, USER, "Flame", role_id=FLAME, entry_key=flame_key,
            role_added=True)
        # Switching between two holdings of one role does not change who put
        # the role on the member, so "already present" must not overwrite it.
        await equip_engine.record_equipped(
            GUILD, USER, "Flame", role_id=FLAME, entry_key=flame_key,
            role_added=False)
        self.assertEqual(self.observation(), [(1, FLAME)])
        # A different role starts from this operation's own observation.
        await equip_engine.record_equipped(
            GUILD, USER, "Blue", role_id=BLUE, entry_key=blue_key,
            role_added=False)
        self.assertEqual(self.observation(), [(0, BLUE)])
        execute("DELETE FROM equipped_roles")


class UnequipRoleObservationTests(AuditCase):
    """Unequip acts only on roles whose recorded observation supports it.

    A role the equip ADDED may be taken off when no independent entitlement
    covers it. A role the equip saw ALREADY WORN is kept. An unknown row (a
    legacy row, or metadata bound to another role) keeps the role AND the slot
    and reports an uncertain outcome: an admin removes the role by hand, then
    the member's retry clears the slot.
    """

    def observation(self):
        return rows("SELECT role_added, role_added_role_id FROM equipped_roles")

    def forget_observation(self):
        execute("UPDATE equipped_roles SET role_added=NULL, "
                "role_added_role_id=NULL WHERE guild_id=? AND user_id=?",
                (GUILD, USER))

    async def test_ordinary_unequip_removes_the_role_the_equip_added(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        self.assertEqual(self.observation(), [(1, FLAME)])
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["role_removed"])
        self.assertEqual((slot(), worn(self.member)), ([], []))

    async def test_a_role_observed_as_already_present_is_preserved(self):
        await self.give_roles()
        self.member.roles.append(self.guild.get_role(FLAME))
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertFalse(result["role_added"])
        self.assertEqual(self.observation(), [(0, FLAME)])
        self.assertEqual(self.member.add_roles.await_count, 0)
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["role_kept"])
        self.assertFalse(result["role_removed"])
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))

    async def test_booster_level_two_colour_role_is_removed_on_unequip(self):
        # The reported defect: the Shop added this level-2 colour role, the
        # member's per-member boost count is unknown (a live discord.Member
        # exposes none), and the Booster "already worn" rule let the role vouch
        # for itself, so Unequip kept a role the Shop had added.
        await self.booster(colour_level=2)
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["role_added"])
        self.assertEqual(self.observation(), [(1, FLAME)])
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["role_removed"], result)
        self.assertEqual((slot(), worn(self.member)), ([], []))

    async def test_a_known_boost_level_keeps_the_colour_role(self):
        # Same role, but this member's boost count is KNOWN to be 2, which is a
        # real entitlement independent of wearing it.
        await self.booster(colour_level=2, count=2)
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["role_kept"])
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))

    async def test_an_mvp_award_keeps_the_role(self):
        await self.give_roles()
        execute("INSERT OR REPLACE INTO mvp_config (guild_id, enabled, "
                "mvp_role_id) VALUES (?, 1, ?)", (GUILD, FLAME))
        execute("INSERT INTO mvp_history (guild_id,user_id,user_display_name,"
                "cycle_start,cycle_end,score) VALUES (?,?,?,?,?,?)",
                (GUILD, USER, "Phase One", "2026-01-01T00:00:00+00:00",
                 "2026-01-02T00:00:00+00:00", 10))
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["role_kept"])
        self.assertEqual((slot(), worn(self.member)), ([], ["Flame"]))
        for table in ("mvp_history", "mvp_config"):
            execute(f"DELETE FROM {table}")

    async def test_an_unknown_row_keeps_the_role_and_the_slot(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        self.forget_observation()
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"], result)
        self.assertTrue(result["uncertain"])
        self.assertTrue(result["role_kept"])
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual(self.member.remove_roles.await_count, 0)

    async def test_an_unknown_row_whose_role_is_gone_clears_the_slot(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        self.forget_observation()
        self.member.roles.clear()          # removed by an admin, or by Discord
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertEqual(slot(), [])

    async def test_a_failed_entitlement_check_keeps_the_role_and_the_slot(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        with patch.object(timed_ownership, "non_slot_role_entitlement",
                          AsyncMock(side_effect=sqlite3.OperationalError("locked"))):
            result = await equip_engine.unequip_role(
                self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"], result)
        self.assertTrue(result["uncertain"])
        self.assertIn("Could not verify role entitlements", result["error"])
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual(self.member.remove_roles.await_count, 0)

    async def test_a_refused_removal_keeps_the_slot(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        refused = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        self.member.remove_roles = AsyncMock(side_effect=refused)
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"], result)
        self.assertFalse(result["uncertain"])
        self.assertIn("Discord error removing role", result["error"])
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual(self.member.remove_roles.await_count, 1)

    async def test_a_removal_that_may_have_applied_is_uncertain_and_retry_clears(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])

        async def remove_then_timeout(role, reason=None):
            self.member.roles.remove(role)
            raise asyncio.TimeoutError()

        self.member.remove_roles = AsyncMock(side_effect=remove_then_timeout)
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"], result)
        self.assertTrue(result["uncertain"])
        # The slot is NOT cleared on an unconfirmed removal ...
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], []))
        # ... and the retry (the role is gone by now) only clears the slot.
        slow_discord(self.member)
        self.member.roles.clear()
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertEqual(slot(), [])
        self.assertEqual(self.member.remove_roles.await_count, 0)

    async def test_caller_cancellation_after_the_removal_still_clears_the_slot(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        log = io.StringIO()
        task = asyncio.ensure_future(
            equip_engine.unequip_role(self.bot, GUILD, USER, "Flame"))

        async def remove_and_cancel_caller(role, reason=None):
            self.member.roles.remove(role)
            task.cancel()
            await asyncio.sleep(0)

        self.member.remove_roles = AsyncMock(side_effect=remove_and_cancel_caller)
        with contextlib.redirect_stdout(log):
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((slot(), worn(self.member)), ([], []))
        self.assertEqual(self.member.remove_roles.await_count, 1)
        self.assertIn("ran to its end after its caller was cancelled: unequipped",
                      log.getvalue())
        self.assertNotIn("[EQUIP][UNCERTAIN]", log.getvalue())

    async def test_the_unit_itself_cancelled_at_the_removal_is_logged_uncertain(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        log = io.StringIO()

        async def remove_and_cancel_unit(role, reason=None):
            self.member.roles.remove(role)
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

        self.member.remove_roles = AsyncMock(side_effect=remove_and_cancel_unit)
        with contextlib.redirect_stdout(log):
            with self.assertRaises(asyncio.CancelledError):
                await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        # The slot was never cleared, and nothing claims it was: the line says
        # the member may no longer wear the role the slot records.
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], []))
        lines = [line for line in log.getvalue().splitlines()
                 if "[EQUIP][UNCERTAIN]" in line]
        self.assertEqual(len(lines), 1, log.getvalue())
        self.assertIn("operation=unequip", lines[0])
        self.assertIn(f"role={FLAME}", lines[0])
        self.assertIn(f"user={USER}", lines[0])


class SwapRoleObservationTests(AuditCase):
    """A swap never reports success while the old role is unresolved."""

    def observation(self):
        return rows("SELECT role_added, role_added_role_id FROM equipped_roles")

    async def test_swap_removes_the_role_the_previous_equip_added(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        self.assertTrue(result["success"], result)
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))
        self.assertEqual(self.observation(), [(1, BLUE)])
        self.assertEqual(self.member.remove_roles.await_count, 1)
        self.assertEqual(self.member.remove_roles.await_args.args[0].id, FLAME)

    async def test_a_lost_old_role_is_uncertain_even_when_the_new_one_is_taken_back(self):
        """Both compensations failing must reach the caller as uncertain.

        The previous role is taken off, the slot write fails, and putting that
        role back fails too. Taking back the role THIS call added then succeeds
        - and that used to be enough to report a tidy failure, even though the
        old role is gone while the old slot still records it.
        """
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        refused = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

        async def remove_the_role(role, reason=None):
            self.member.roles.remove(role)

        async def cannot_restore_the_old_role(role, reason=None):
            if role.id == FLAME:                     # putting the old one back
                raise refused
            self.member.roles.append(role)

        self.member.remove_roles = AsyncMock(side_effect=remove_the_role)
        self.member.add_roles = AsyncMock(side_effect=cannot_restore_the_old_role)
        log = io.StringIO()
        with patch.object(equip_engine, "record_equipped", AsyncMock(
                side_effect=sqlite3.OperationalError("database is locked"))), \
                contextlib.redirect_stdout(log):
            result = await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        self.assertFalse(result["success"], result)
        self.assertTrue(result["uncertain"], result)          # A
        self.assertIn("previous role", result["error"])
        # The old slot still records Flame, Flame is no longer worn, and the
        # role this call added was taken back: only an admin can reconcile it.
        self.assertEqual(slot(), [("Flame", FLAME)])
        self.assertEqual(worn(self.member), [])
        lines = [line for line in log.getvalue().splitlines()
                 if "[EQUIP][UNCERTAIN]" in line]
        equip_lines = [line for line in lines if "operation=equip" in line]
        self.assertEqual(len(equip_lines), 1, log.getvalue())
        for part in ("stage=slot_write", f"guild={GUILD}", f"user={USER}",
                     "item='Blue'", f"role={BLUE}", "entry=i:"):
            self.assertIn(part, equip_lines[0])
        # The swap line keeps the reason the previous role could not be put back.
        self.assertTrue(any("operation=swap" in line and "could not be restored" in line
                            for line in lines), log.getvalue())

    async def test_swap_is_refused_when_the_old_role_cannot_be_removed(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        self.member.add_roles.reset_mock()
        refused = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

        async def fail_for_the_old_role(role, reason=None):
            if role.id == FLAME:
                raise refused
            self.member.roles.remove(role)

        self.member.remove_roles = AsyncMock(side_effect=fail_for_the_old_role)
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        self.assertFalse(result["success"], result)
        self.assertIn("Discord error removing role", result["error"])
        # The old slot is intact, the old role is still worn, and the role this
        # call added was taken back.
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], ["Flame"]))
        self.assertEqual(self.member.add_roles.await_count, 1)
        ids = [call.args[0].id for call in self.member.remove_roles.await_args_list]
        self.assertEqual(ids, [FLAME, BLUE])

    async def test_swap_is_refused_when_the_old_entitlement_cannot_be_verified(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        real = timed_ownership.non_slot_role_entitlement

        async def check(guild_id, user_id, role_id, **kwargs):
            if int(role_id) == FLAME:
                raise sqlite3.OperationalError("database is locked")
            return await real(guild_id, user_id, role_id, **kwargs)

        with patch.object(timed_ownership, "non_slot_role_entitlement", check):
            result = await equip_engine.equip_role(
                self.bot, GUILD, USER, "Blue")
        self.assertFalse(result["success"], result)
        self.assertIn("Could not verify role entitlements", result["error"])
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], ["Flame"]))

    async def test_swap_keeps_an_old_role_observed_as_already_present(self):
        await self.give_roles()
        self.member.roles.append(self.guild.get_role(FLAME))
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        self.assertEqual(self.observation(), [(0, FLAME)])
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Blue")
        self.assertTrue(result["success"], result)
        self.assertEqual((slot(), worn(self.member)),
                         ([("Blue", BLUE)], ["Blue", "Flame"]))
        self.assertEqual(self.member.remove_roles.await_count, 0)

    async def test_same_role_swap_carries_the_observation(self):
        await self.give_roles()
        holding = await timed_ownership.grant_timed_holding(
            GUILD, USER, "Timed Flame", expires_at=timed_ownership.expiry_from_hours(2),
            item_type="temp_role", metadata={"role_id": FLAME}, role_id=FLAME,
            source="shop")
        timed_key = f"h:{holding}"
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        self.assertEqual(self.observation(), [(1, FLAME)])
        # The member already wears the role, so this equip sends no add ...
        result = await equip_engine.equip_role(
            self.bot, GUILD, USER, "Timed Flame", entry_key=timed_key)
        self.assertTrue(result["success"], result)
        self.assertFalse(result["role_added"])
        self.assertEqual(self.member.add_roles.await_count, 1)   # only the first
        # ... and the observation bound to this role is carried over, so the
        # role stays removable.
        self.assertEqual(self.observation(), [(1, FLAME)])
        result = await equip_engine.unequip_role(
            self.bot, GUILD, USER, "Timed Flame", entry_key=timed_key)
        self.assertTrue(result["success"], result)
        self.assertEqual((slot(), worn(self.member)), ([], []))


class TimedGrantObservationTests(AuditCase):
    """Timed grants follow the same metadata contract and fail the same way."""

    def observation(self):
        return rows("SELECT role_added, role_added_role_id FROM equipped_roles")

    async def test_a_timed_grant_records_that_it_added_the_role(self):
        await self.give_roles()
        result = await timed_ownership.grant_timed_product(
            self.bot, GUILD, USER, item_name="Timed Flame", duration_hours=2,
            item_type="temp_role", role_id=FLAME, source="shop")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["equipped"])
        self.assertTrue(result["role_added"])
        self.assertEqual(self.observation(), [(1, FLAME)])
        self.assertEqual(worn(self.member), ["Flame"])
        unequip = await equip_engine.unequip_role(
            self.bot, GUILD, USER, "Timed Flame",
            entry_key=result["entry_key"])
        self.assertTrue(unequip["success"], unequip)
        self.assertTrue(unequip["role_removed"])
        self.assertEqual((slot(), worn(self.member)), ([], []))

    async def test_a_timed_grant_of_a_role_already_worn_records_present(self):
        await self.give_roles()
        self.member.roles.append(self.guild.get_role(FLAME))
        result = await timed_ownership.grant_timed_product(
            self.bot, GUILD, USER, item_name="Timed Flame", duration_hours=2,
            item_type="temp_role", role_id=FLAME, source="shop")
        self.assertTrue(result["success"], result)
        self.assertFalse(result["role_added"])
        self.assertEqual(self.member.add_roles.await_count, 0)
        self.assertEqual(self.observation(), [(0, FLAME)])

    async def test_a_refused_swap_keeps_the_old_slot_and_the_holding(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        # An unknown row: the swap of the OLD role cannot be resolved.
        execute("UPDATE equipped_roles SET role_added=NULL, "
                "role_added_role_id=NULL WHERE guild_id=? AND user_id=?",
                (GUILD, USER))
        result = await timed_ownership.grant_timed_product(
            self.bot, GUILD, USER, item_name="Timed Blue", duration_hours=2,
            item_type="temp_role", role_id=BLUE, source="shop")
        self.assertTrue(result["success"], result)
        self.assertFalse(result["equipped"], result)
        self.assertIn("not equipped", result["equip_warning"])
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], ["Flame"]))
        # The purchase is owned, just not worn.
        self.assertIsNotNone(
            await timed_ownership.get_timed_holding(result["entry_key"]))
        self.assertIn(result["entry_key"],
                      [h["entry_key"] for h in await timed_ownership.get_timed_holdings(GUILD, USER)])

    async def test_the_unequipped_warning_names_the_real_source(self):
        """E: an earned reward is never described as a purchase."""
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        # An unknown row: the swap of the OLD role cannot be resolved, so the
        # grant succeeds but is left unequipped.
        execute("UPDATE equipped_roles SET role_added=NULL, "
                "role_added_role_id=NULL WHERE guild_id=? AND user_id=?",
                (GUILD, USER))

        async def grant(source):
            return await timed_ownership.grant_timed_product(
                self.bot, GUILD, USER, item_name=f"Timed {source}",
                duration_hours=2, item_type="temp_role", role_id=BLUE,
                source=source)

        bought = await grant("shop")
        earned = await grant("mission")
        self.assertTrue(bought["success"], bought)
        self.assertTrue(earned["success"], earned)
        self.assertIn("was purchased", bought["equip_warning"])
        self.assertIn("granted as a mission reward", earned["equip_warning"])
        self.assertNotIn("purchased", earned["equip_warning"])
        self.assertNotIn("shop", earned["equip_warning"])

    async def test_a_failed_slot_write_does_not_overwrite_the_old_slot(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Flame"))["success"])
        with patch.object(equip_engine, "record_equipped", AsyncMock(
                side_effect=sqlite3.OperationalError("database is locked"))):
            result = await timed_ownership.grant_timed_product(
                self.bot, GUILD, USER, item_name="Timed Blue", duration_hours=2,
                item_type="temp_role", role_id=BLUE, source="shop")
        self.assertTrue(result["success"], result)
        self.assertFalse(result["equipped"], result)
        self.assertTrue(result["equip_uncertain"], result)
        self.assertIn("not equipped", result["equip_warning"])
        # The old slot and its role are intact; the new role was taken back.
        self.assertEqual((slot(), worn(self.member)),
                         ([("Flame", FLAME)], ["Flame"]))
        self.assertIsNotNone(
            await timed_ownership.get_timed_holding(result["entry_key"]))


class PermanentRoleKeptUnequippedTests(AuditCase):
    """PATCH 5: a refused equip never costs the member the permanent role.

    The previous role cannot be released and the new role is confirmed off the
    member, so the equip failed CLEANLY - but the ownership is real. The role
    stays in the Inventory, unequipped; nothing is deleted, nothing is
    refunded, nothing is uncertain.
    """

    async def occupy_the_slot_with_blue(self):
        """Blue is equipped and CANNOT be released (the refused swap)."""
        self.assertTrue((await reward_engine.give_reward(
            self.bot, GUILD, USER, "role", role_id=BLUE,
            source="test"))["success"])
        self.assertEqual(slot(), [("Blue", BLUE)])
        refused = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

        async def cannot_release_blue(role, reason=None):
            if role.id == BLUE:
                raise refused
            if role in self.member.roles:
                self.member.roles.remove(role)

        self.member.remove_roles = AsyncMock(side_effect=cannot_release_blue)

    async def test_an_earned_role_reward_is_kept_unequipped_with_reward_wording(self):
        await self.occupy_the_slot_with_blue()
        result = await reward_engine.give_reward(
            self.bot, GUILD, USER, "role", role_id=FLAME, source="mission",
            reason="Mission complete: Chatter")
        # Ownership kept, the equip is honest about it, nothing is uncertain.
        self.assertTrue(result["success"], result)
        self.assertFalse(result["uncertain"], result)
        self.assertTrue(result["keep_unequipped"], result)
        self.assertFalse(result["equipped"], result)
        self.assertFalse(result["role_added"], result)
        # The item is the member's, the slot and Discord still say Blue.
        self.assertEqual(rows("SELECT item_name, item_type, quantity "
                              "FROM inventory_items WHERE item_name='Flame'"),
                         [("Flame", "role", 1)])
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))
        self.assertEqual(rows("SELECT COUNT(*) FROM equipped_roles"), [(1,)])
        # E: the wording names the source, never a purchase.
        self.assertIn("granted as a mission reward", result["warning"])
        self.assertNotIn("purchased", result["warning"])
        # Nothing was held for a retry: the reward is already delivered.
        self.assertFalse(result.get("pending"))
        self.assertEqual(rows("SELECT COUNT(*) FROM pending_rewards"), [(0,)])

    async def test_a_purchased_permanent_role_is_kept_and_says_purchased(self):
        from cogs.shop import _deliver_purchase_effects
        await self.occupy_the_slot_with_blue()      # the previous role holds
        itx = shop_interaction(self.member, self.guild, self.bot)
        result = await _deliver_purchase_effects(
            itx, guild_id=GUILD, user_id=USER, itype="role", listing_type="role",
            name="Flame", role_id=FLAME, product_id=77, source="shop",
            op_id="op-kept-1")
        # Delivered: success (so nothing is refunded) but not equipped.
        self.assertTrue(result["success"], result)
        self.assertIsNone(result["error"])
        self.assertTrue(result["keep_unequipped"], result)
        self.assertEqual(rows("SELECT item_name, item_type FROM inventory_items "
                              "WHERE item_name='Flame'"), [("Flame", "role")])
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))
        # E: purchase wording on the purchase branch.
        self.assertIn("was purchased", result["warning"])
        self.assertNotIn("granted as", result["warning"])


class FreshMemberCheckTests(AuditCase):
    """An absence in the member cache is confirmed with Discord before an add."""

    def observation(self):
        return rows("SELECT role_added, role_added_role_id FROM equipped_roles")

    async def test_a_stale_cache_absence_is_confirmed_before_adding(self):
        await self.give_roles()
        # Discord has the role; the cache has not caught up.
        fresh = SimpleNamespace(id=USER, roles=[self.guild.get_role(FLAME)],
                                premium_since=None, bot=False)
        self.guild.fetch_member = AsyncMock(return_value=fresh)
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertFalse(result["role_added"])
        self.assertEqual(self.member.add_roles.await_count, 0)
        self.assertEqual(self.observation(), [(0, FLAME)])
        # Unequip reads presence from the same cache, so it sees the role as
        # absent and only clears the obsolete slot: the role is left on Discord
        # (the documented stale-cache limit - Unequip does not fetch).
        result = await equip_engine.unequip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertEqual(self.member.remove_roles.await_count, 0)
        self.assertEqual(slot(), [])

    async def test_a_failed_fetch_changes_nothing(self):
        await self.give_roles()
        self.assertTrue((await equip_engine.equip_role(
            self.bot, GUILD, USER, "Blue"))["success"])
        self.guild.fetch_member = AsyncMock(side_effect=RuntimeError("Discord 503"))
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertFalse(result["success"], result)
        self.assertIn("Could not confirm your current roles", result["error"])
        self.assertEqual(self.member.add_roles.await_count, 1)  # only the Blue equip
        self.assertEqual((slot(), worn(self.member)), ([("Blue", BLUE)], ["Blue"]))

    async def test_a_failed_fetch_does_not_grant_a_timed_role_either(self):
        await self.give_roles()
        self.guild.fetch_member = AsyncMock(side_effect=RuntimeError("Discord 503"))
        result = await timed_ownership.grant_timed_product(
            self.bot, GUILD, USER, item_name="Timed Flame", duration_hours=2,
            item_type="temp_role", role_id=FLAME, source="shop")
        self.assertFalse(result["success"], result)
        self.assertFalse(result["uncertain"])
        self.assertIn("Could not confirm your current roles", result["error"])
        self.assertEqual(self.member.add_roles.await_count, 0)
        self.assertEqual(slot(), [])
        self.assertEqual(await timed_ownership.get_timed_holdings(GUILD, USER), [])

    async def test_no_fetch_when_the_cache_already_shows_the_role(self):
        await self.give_roles()
        self.guild.fetch_member.reset_mock()
        self.member.roles.append(self.guild.get_role(FLAME))
        result = await equip_engine.equip_role(self.bot, GUILD, USER, "Flame")
        self.assertTrue(result["success"], result)
        self.assertEqual(self.guild.fetch_member.await_count, 0)


class TimedGrantPurchaseWarningTests(AuditCase):
    async def test_a_delivered_but_unequipped_timed_role_warns_the_buyer(self):
        from cogs.shop import _deliver_purchase_effects
        itx = shop_interaction(self.member, self.guild, self.bot)
        granted = {"success": True, "entry_key": "h:1", "expires_at": "soon",
                   "role_added": True, "equipped": False,
                   "equip_warning": "**Timed Flame** is owned but not equipped.",
                   "equip_uncertain": False, "uncertain": False, "error": None}
        with patch.object(timed_ownership, "grant_timed_product",
                          AsyncMock(return_value=granted)):
            result = await _deliver_purchase_effects(
                itx, guild_id=GUILD, user_id=USER, itype="temp_role",
                listing_type="temp_role", name="Timed Flame", role_id=FLAME,
                duration_hours=2, source="shop")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["warning"],
                         "**Timed Flame** is owned but not equipped.")
        self.assertIsNone(result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
