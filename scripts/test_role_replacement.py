"""Role Replacement — runtime regression harness (scratch database, Discord fakes).

What is pinned here:

  * both replacement directions: A arrives while B is worn, B arrives while A is
    worn (removing only the newly conflicting role);
  * A and B arriving in the SAME update: A wins, B is removed;
  * unrelated roles are never removed;
  * a removal-only event does nothing (no cascade from a removed role);
  * duplicate / repeated events are idempotent;
  * saving or editing a rule never scans members (no retroactive cleanup);
  * configuration validation: self-conflict, duplicate conflicts, a second rule
    for the same keep role, and directed cycles are refused;
  * ownership protection: a fulfilled Level claim, an equipped Shop item, a live
    timed holding, a timed reaction role, and a reaction-panel requirement all
    keep the conflicting role;
  * a role that is another rule's keep role is protected by ordering, not by luck;
  * Discord permission / hierarchy refusal and an unconfirmed removal are never
    reported as success: they create a retry row, and the retry loop resolves it;
  * runtime visibility: a rule saved through the DB is honoured by the very next
    member update with no restart and no cache step.

Run from the repository root:
    python scripts/test_role_replacement.py
The exit code is the harness's own (0 only when every check passed).
"""
import asyncio
import inspect
import discord
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from phase1_support import GUILD, USER, execute, reset_database, rows  # noqa: E402

from utils import role_replacement as RR  # noqa: E402

BOT_ROLE = 900
ROLE_POSITIONS = (
    (BOT_ROLE, 50),        # the bot's own top role
    (4101, 10), (4102, 9), (4103, 8), (4104, 7),
    (4111, 6), (4112, 5),
    (7101, 4), (7102, 3), (7103, 2), (7104, 1),
)

KEEP = 4101
CONFLICT_B = 4102
CONFLICT_C = 4103
UNRELATED = 4104
KEEP2 = 4111
CONFLICT_D = 4112
FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


class Role:
    """Discord role double: identity is the id, order is the POSITION.

    discord.Role compares by position, never by id; the hierarchy guard depends
    on that, so the double must behave the same or the guard goes untested.
    """
    __hash__ = object.__hash__

    def __init__(self, role_id, position=5, managed=False, default=False,
                 guild=None):
        self.id = int(role_id)
        self.name = f"role-{role_id}"
        self.position = position
        self.managed = managed
        self._default = default
        self._guild = guild

    def is_default(self):
        return self._default

    def is_assignable(self):
        """Same contract as discord.Role: @everyone, managed, and anything at or
        above the bot's own top role cannot be assigned."""
        if self._default or self.managed:
            return False
        top = getattr(getattr(self._guild, "me", None), "top_role", None)
        if top is not None:
            return top.position > self.position
        return True

    def __eq__(self, other):
        return isinstance(other, Role) and other.id == self.id

    def __lt__(self, other):
        return self.position < other.position

    def __gt__(self, other):
        return self.position > other.position

    def __ge__(self, other):
        return self.position >= other.position

    def __le__(self, other):
        return self.position <= other.position


class _NotFound(discord.NotFound):
    """A real discord.NotFound: the member is confirmed absent from the guild."""

    def __init__(self):
        Exception.__init__(self, "Unknown Member")


class Member:
    """A member whose Discord calls can fail, or can return without effect."""

    def __init__(self, guild, user_id=USER, roles=(), *, fail_remove=False,
                 lie_remove=False):
        self.id = int(user_id)
        self.guild = guild
        self.bot = False
        self.premium_since = None
        self.fail_remove = fail_remove
        self.lie_remove = lie_remove
        # Runs on the first authoritative read a removal makes; lets a test
        # stage "another operation acted just before the read".
        self.on_fetch = None
        self.fetch_count = 0
        self.remove_calls = []
        self.roles = [guild.roles[r] for r in roles]
        self.top_role = guild.roles[BOT_ROLE]
        self.remove_roles = AsyncMock(side_effect=self._remove)

    def role_ids(self):
        return {role.id for role in self.roles}

    async def fetched(self):
        """What Guild.fetch_member hands back: a member honest about its roles."""
        self.fetch_count += 1
        hook, self.on_fetch = self.on_fetch, None
        if hook is not None:
            staged = hook()
            if inspect.isawaitable(staged):
                await staged
        return self

    async def _remove(self, *roles, reason=None):
        self.remove_calls.append([role.id for role in roles])
        if self.fail_remove:
            raise RuntimeError("Missing Permissions")
        if self.lie_remove:
            return
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)


class Guild:
    def __init__(self, guild_id=GUILD):
        self.id = int(guild_id)
        self.owner_id = 999
        self.roles = {}
        for role_id, position in ROLE_POSITIONS:
            self.roles[role_id] = Role(role_id, position=position, guild=self)
        self.me = SimpleNamespace(
            id=9000, top_role=self.roles[BOT_ROLE],
            guild_permissions=SimpleNamespace(administrator=False,
                                              manage_roles=True))
        self.members = {}

    def get_role(self, role_id):
        return self.roles.get(int(role_id))

    def get_member(self, user_id):
        return self.members.get(int(user_id))

    def add_role(self, role_id, position=1):
        """Register a role the fixture tables do not know about yet."""
        self.roles[int(role_id)] = Role(role_id, position=position, guild=self)
        return self.roles[int(role_id)]

    async def fetch_member(self, user_id):
        member = self.members.get(int(user_id))
        if member is None:
            raise _NotFound()
        return await member.fetched()


class Bot:
    def __init__(self, guild):
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild if int(guild_id) == self.guild.id else None


class Snapshot:
    """The before/after member snapshot an event carries.

    Only the fields the engine is allowed to read are present, which keeps the
    test honest about "never trust the event snapshot for member state".
    """

    def __init__(self, guild, roles):
        self.id = USER
        self.guild = guild
        self.bot = False
        self.roles = [guild.roles[r] for r in roles]


class World:
    """One guild, one member, one bot - the only source of truth in a test."""

    def __init__(self, held=(), *, guild_id=GUILD, **member_kwargs):
        self.guild = Guild(guild_id)
        for role_id in held:
            if int(role_id) not in self.guild.roles:
                self.guild.add_role(role_id, position=5)
        self.bot = Bot(self.guild)
        self.member = Member(self.guild, roles=held, **member_kwargs)
        self.guild.members[self.member.id] = self.member

    def set_roles(self, *role_ids):
        self.member.roles = [self.guild.roles[int(r)] for r in role_ids]
        return self

    async def event(self, before_ids, after_ids):
        result = await RR.process_member_update(
            self.bot, Snapshot(self.guild, before_ids),
            Snapshot(self.guild, after_ids))
        return result

    async def retry(self):
        return await RR.retry_pending(self.bot)


def pending_rows():
    return db_rows("SELECT guild_id, user_id, role_id FROM role_replacement_pending")


def db_rows(query, args=()):
    from phase1_support import rows as _rows
    return _rows(query, args)


def db_exec(query, args=()):
    execute(query, args)


async def add_rule(keep, conflicts, *, guild_id=GUILD, enabled=1):
    return await RR.save_rule(guild_id, keep, conflicts, enabled=enabled)


async def expect_rejected(label, keep, conflicts):
    try:
        await RR.save_rule(GUILD, keep, conflicts)
    except RR.RoleReplacementError as exc:
        check(label, True)
        return str(exc)
    check(label, False, "the configuration was accepted")
    return ""


# ── 1. Both directions ───────────────────────────────────────────────────

async def test_keep_role_arrives_removes_conflicting():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B, CONFLICT_C])
    world = World(held=(UNRELATED, CONFLICT_B, CONFLICT_C, KEEP))
    result = await world.event({UNRELATED}, {UNRELATED, CONFLICT_B, CONFLICT_C, KEEP})
    check("A arrives: conflicting roles removed",
          world.member.role_ids() == {UNRELATED, KEEP},
          str(world.member.role_ids()))
    check("A arrives: only the conflicting roles were touched",
          sorted(sum(world.member.remove_calls, [])) == [CONFLICT_B, CONFLICT_C],
          str(world.member.remove_calls))
    check("A arrives: unrelated role preserved", UNRELATED in world.member.role_ids())
    check("A arrives: result reports the removals",
          sorted(result["removed"]) == [CONFLICT_B, CONFLICT_C], str(result))
    check("A arrives: no retry markers", pending_rows() == [], str(pending_rows()))


async def test_conflicting_role_arrives_removes_only_itself():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B, CONFLICT_C])
    world = World(held=(KEEP, CONFLICT_B))
    await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("B arrives while A is held: only B removed",
          world.member.role_ids() == {KEEP}, str(world.member.role_ids()))
    check("B arrives: A kept", KEEP in world.member.role_ids())
    check("B arrives: exactly one removal call",
          world.member.remove_calls == [[CONFLICT_B]],
          str(world.member.remove_calls))


async def test_keep_and_conflict_arrive_together():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B))
    await world.event(set(), {KEEP, CONFLICT_B})
    check("simultaneous A+B: A wins", world.member.role_ids() == {KEEP},
          str(world.member.role_ids()))
    check("simultaneous A+B: single verified removal",
          world.member.remove_calls == [[CONFLICT_B]],
          str(world.member.remove_calls))


async def test_unrelated_and_removal_only_updates_do_not_act():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(UNRELATED, CONFLICT_C))
    result = await world.event({UNRELATED}, {UNRELATED, CONFLICT_C})
    check("unrelated addition: nothing removed",
          world.member.remove_calls == [] and result["removed"] == [], str(result))

    # A removal-only event (our own effect, or anyone else's) must not cascade.
    await add_rule(KEEP2, [CONFLICT_D])
    world.set_roles(CONFLICT_D)
    result = await world.event({KEEP2, CONFLICT_D}, {CONFLICT_D})
    check("removal-only update: no cascade",
          world.member.remove_calls == [] and result["evaluated"] is False,
          str(result))
    check("removal of a keep role does not strip its conflicts",
          CONFLICT_D in world.member.role_ids(), str(world.member.role_ids()))


async def test_duplicate_events_are_idempotent():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B))
    await world.event({KEEP}, {KEEP, CONFLICT_B})
    calls_first = list(world.member.remove_calls)
    # The same event replayed: B is no longer on the member, so nothing happens.
    await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("duplicate event: no second removal",
          world.member.remove_calls == calls_first, str(world.member.remove_calls))
    check("duplicate event: no retry marker", pending_rows() == [])


async def test_disabled_rule_is_inert():
    await reset_database()
    rule = await add_rule(KEEP, [CONFLICT_B])
    await RR.set_rule_enabled(GUILD, rule["id"], False)
    world = World(held=(KEEP, CONFLICT_B))
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("disabled rule: nothing removed",
          world.member.remove_calls == [] and result["evaluated"] is False,
          str(result))


# ── 2. No retroactive cleanup ────────────────────────────────────────────

async def test_saving_a_rule_never_touches_members():
    await reset_database()
    world = World(held=(CONFLICT_B, CONFLICT_C))
    snapshot = sorted(world.member.role_ids())
    rule = await add_rule(KEEP, [CONFLICT_B, CONFLICT_C])
    check("save: member roles untouched", sorted(world.member.role_ids()) == snapshot)
    check("save: no Discord call at all", world.member.remove_calls == [])
    await RR.save_rule(GUILD, KEEP, [CONFLICT_B], rule_id=rule["id"])
    check("edit: member roles still untouched",
          sorted(world.member.role_ids()) == snapshot
          and world.member.remove_calls == [])
    await RR.delete_rule(GUILD, rule["id"])
    check("delete: member roles still untouched",
          sorted(world.member.role_ids()) == snapshot
          and world.member.remove_calls == [])
    check("delete: no pending rows", pending_rows() == [])


# ── 3. Validation ────────────────────────────────────────────────────────

async def test_validation():
    await reset_database()
    error = await expect_rejected("validation: keep role in its own conflicts",
                                  KEEP, [KEEP, CONFLICT_B])
    check("validation: self-conflict message names the role",
          str(KEEP) in error, error)

    await expect_rejected("validation: duplicate conflicting roles",
                          KEEP, [CONFLICT_B, CONFLICT_B])
    await expect_rejected("validation: empty conflict list", KEEP, [])
    await expect_rejected("validation: malformed role id", KEEP, ["not-a-role"])

    await add_rule(KEEP, [CONFLICT_B])
    await expect_rejected("validation: second rule for the same keep role",
                          KEEP, [CONFLICT_C])

    # A chain (A > B > C) is a priority ordering, not a contradiction: accepted.
    chain = await add_rule(CONFLICT_B, [CONFLICT_C])
    check("validation: a priority chain A > B > C is accepted",
          chain["keep_role_id"] == CONFLICT_B
          and chain["conflict_role_ids"] == [CONFLICT_C], str(chain))
    await RR.delete_rule(GUILD, chain["id"])

    # A real directed cycle is refused: A removes B while B removes A.
    await expect_rejected("validation: cycle across two rules",
                          CONFLICT_B, [KEEP])

    rules = await RR.list_rules(GUILD)
    check("validation: rejected configurations were not written",
          len(rules) == 1 and rules[0]["keep_role_id"] == KEEP
          and rules[0]["conflict_role_ids"] == [CONFLICT_B], str(rules))

    updated = await RR.save_rule(GUILD, KEEP, [CONFLICT_C], rule_id=rules[0]["id"])
    check("validation: a rule may edit its own conflicting set",
          updated["conflict_role_ids"] == [CONFLICT_C], str(updated))

    second = await add_rule(KEEP2, [CONFLICT_C])
    check("validation: a role may conflict with more than one keep role",
          second["conflict_role_ids"] == [CONFLICT_C], str(second))

    db_exec("INSERT INTO reaction_roles (guild_id, message_id, role_id, "
            "button_label) VALUES (?,?,?,?)", (GUILD, 1, 7201, "RR"))
    await expect_rejected("validation: reaction-panel role refused as a conflict",
                          KEEP2, [7201])
    db_exec("INSERT INTO reaction_roles (guild_id, message_id, role_id, "
            "button_label, required_role_id) VALUES (?,?,?,?,?)",
            (GUILD, 2, 7202, "Dep", 7203))
    await expect_rejected("validation: reaction-panel gate role refused",
                          7300, [7203])


# ── 4. Ownership protection ──────────────────────────────────────────────

async def test_level_claim_keeps_the_role():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
            "reward_ref,track,status,payload_json,created_at) VALUES "
            "(?,?,?,?,'role','fulfilled',?,CURRENT_TIMESTAMP)",
            (GUILD, USER, 5, "member", json.dumps({"role_id": CONFLICT_B})))
    world = World(held=(KEEP, CONFLICT_B))
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("ownership: fulfilled Level claim keeps the conflicting role",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("ownership: the keep is reported, not silently skipped",
          result["kept"] and "level role claim" in result["kept"][0]["reason"],
          str(result))
    check("ownership: keeping writes no retry marker", pending_rows() == [])


async def test_timed_holding_keeps_the_role():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    db_exec("INSERT INTO timed_holdings (guild_id,user_id,item_name,item_type,"
            "quantity,role_id,expires_at) VALUES (?,?,?,?,1,?,?)",
            (GUILD, USER, "Golden Key", "role", CONFLICT_B, future))
    world = World(held=(KEEP, CONFLICT_B))
    await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("ownership: an unexpired timed holding keeps the role",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))


async def test_equipped_item_keeps_the_role():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO inventory_items (guild_id,user_id,item_name,item_type,"
            "quantity,metadata,source) VALUES (?,?,?,?,1,?,?)",
            (GUILD, USER, "Flame", "role", json.dumps({"role_id": CONFLICT_B}),
             "shop"))
    db_exec("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
            "entry_key) VALUES (?,?,?,?,NULL)", (GUILD, USER, "Flame", CONFLICT_B))
    world = World(held=(KEEP, CONFLICT_B))
    await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("ownership: an equipped Shop role keeps the role",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))


async def test_timed_reaction_role_keeps_the_role():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    db_exec("INSERT INTO reaction_role_expiry (guild_id,user_id,role_id,"
            "message_id,expires_at) VALUES (?,?,?,?,?)",
            (GUILD, USER, CONFLICT_B, 55, future))
    world = World(held=(KEEP, CONFLICT_B))
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("ownership: a live reaction-role expiry keeps the role",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("ownership: the reaction reason is reported",
          result["kept"] and "timed reaction role" in result["kept"][0]["reason"],
          str(result))


async def test_reaction_requirement_blocks_removal():
    await reset_database()
    # A gate role that is NOT a panel button (those are refused at config time).
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B, 7102))
    # The panel is edited AFTER the rule exists; config-time validation is a
    # snapshot, so the runtime guard is what must hold this line.
    # Panel button grants 7102 and requires CONFLICT_B: losing the gate would
    # make that panel strip 7102 from this member.
    db_exec("INSERT INTO reaction_roles (guild_id, message_id, role_id, "
            "button_label, required_role_id) VALUES (?,?,?,?,?)",
            (GUILD, 77, 7102, "Dep", CONFLICT_B))
    result = await world.event({KEEP, 7102}, {KEEP, CONFLICT_B, 7102})
    check("ownership: a reaction-role gate role is kept",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("ownership: dependent role is not stripped by us",
          7102 in world.member.role_ids(), str(world.member.role_ids()))
    check("ownership: requirement reason reported",
          result["kept"]
          and "reaction-role panel requires" in result["kept"][0]["reason"],
          str(result))


async def test_expired_ownership_does_not_block():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db_exec("INSERT INTO timed_holdings (guild_id,user_id,item_name,item_type,"
            "quantity,role_id,expires_at) VALUES (?,?,?,?,1,?,?)",
            (GUILD, USER, "Old", "role", CONFLICT_B, past))
    world = World(held=(KEEP, CONFLICT_B))
    await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("ownership: an expired holding does not block the replacement",
          world.member.role_ids() == {KEEP}, str(world.member.role_ids()))


# ── 5. Stale snapshots, lock races and configuration drift ───────────────

async def test_stale_snapshot_keep_role_already_gone():
    """The event says A arrived; by the time we look, A is gone again.

    Both directions must re-check the KEEP role against authoritative state, not
    just the conflicting one, or a stale event removes a role the member is no
    longer meant to lose.
    """
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(CONFLICT_B,))        # A was removed before we looked
    result = await world.event({CONFLICT_B}, {KEEP, CONFLICT_B})
    check("stale keep: the conflicting role survives",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("stale keep: reported as a decision, not a failure",
          result["removed"] == [] and result["events"]
          and result["events"][0]["status"] == "keep_absent", str(result))
    check("stale keep: no retry marker is written, nothing is owed",
          pending_rows() == [], str(pending_rows()))

    # Same guarantee in the other direction: B arrives while A is held, then A
    # is lost before the removal runs.
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(CONFLICT_B,))     # the event says A is held; it is not
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("stale keep (reverse): the conflicting role survives",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("stale keep (reverse): keep_absent reported",
          result["removed"] == [] and result["events"]
          and result["events"][0]["status"] == "keep_absent", str(result))


async def test_keep_role_lost_while_waiting_for_the_slot_lock():
    """A is lost after the caller decided, but before the in-lock read."""
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B))
    world.member.on_fetch = lambda: world.set_roles(CONFLICT_B)
    result = await world.event({CONFLICT_B}, {KEEP, CONFLICT_B})  # A "arrived"
    check("in-lock keep check: the conflicting role survives",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("in-lock keep check: nothing was even attempted",
          world.member.remove_calls == [], str(world.member.remove_calls))
    check("in-lock keep check: reported as keep_absent",
          result["events"][0]["status"] == "keep_absent", str(result))
    check("in-lock keep check: no retry marker", pending_rows() == [],
          str(pending_rows()))


async def test_equip_wins_the_race_inside_the_slot_lock():
    """An equip records its entitlement just before the in-lock read.

    The ownership gate must see it, because the gate is now re-run against a
    member read taken while the same slot_lock the equip takes is held.
    """
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP,))               # B arrives during the race

    async def equip_lands():
        world.set_roles(KEEP, CONFLICT_B)
        db_exec("INSERT INTO inventory_items (guild_id,user_id,item_name,"
                "item_type,quantity,metadata,source) VALUES (?,?,?,?,1,?,?)",
                (GUILD, USER, "Flame", "role", json.dumps({"role_id": CONFLICT_B}),
                 "shop"))
        db_exec("INSERT INTO equipped_roles (guild_id,user_id,item_name,role_id,"
                "entry_key) VALUES (?,?,?,?,NULL)", (GUILD, USER, "Flame", CONFLICT_B))

    world.member.on_fetch = equip_lands
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("lock race: the freshly equipped role is preserved",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("lock race: the ownership gate is what saved it",
          result["events"] and result["events"][0]["status"] == "kept"
          and "owned role item" in result["events"][0]["reason"],
          str(result["events"]))
    check("lock race: nothing was sent to Discord",
          world.member.remove_calls == [], str(world.member.remove_calls))
    check("lock race: no retry marker", pending_rows() == [], str(pending_rows()))


async def _failed_pending_then(mutate, *, keep=KEEP, conflict=CONFLICT_B):
    """A failed removal that leaves one durable pending row, then a mutation."""
    await reset_database()
    rule = await add_rule(keep, [conflict])
    world = World(held=(keep, conflict), fail_remove=True)
    await world.event({keep}, {keep, conflict})
    before = pending_rows()
    await mutate(rule)
    world.member.fail_remove = False
    world.member.remove_calls.clear()   # ignore the attempt that failed
    summary = await world.retry()
    return rule, world, before, summary


async def test_an_equip_holding_the_slot_lock_is_not_overtaken():
    """The genuine race: an equip holds slot_lock while it records entitlement.

    A deterministic interleaving - the equip grants the role FIRST and writes its
    entitlement a moment later, all while holding the lock. If the removal
    decision is taken before the lock (the pre-fix order) it sees no
    entitlement yet and strips a role the member has just been granted. Taking
    the decision inside the lock forces it to wait and re-read.
    """
    from utils.equip_engine import slot_lock

    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP,))
    role_granted = asyncio.Event()

    async def equip():
        async with slot_lock(GUILD, USER):
            world.set_roles(KEEP, CONFLICT_B)      # the role lands...
            role_granted.set()
            await asyncio.sleep(0.05)              # ...entitlement written later
            db_exec("INSERT INTO inventory_items (guild_id,user_id,item_name,"
                    "item_type,quantity,metadata,source) VALUES (?,?,?,?,1,?,?)",
                    (GUILD, USER, "Flame", "role",
                     json.dumps({"role_id": CONFLICT_B}), "shop"))
            db_exec("INSERT INTO equipped_roles (guild_id,user_id,item_name,"
                    "role_id,entry_key) VALUES (?,?,?,?,NULL)",
                    (GUILD, USER, "Flame", CONFLICT_B))

    equip_task = asyncio.create_task(equip())
    await role_granted.wait()
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    await equip_task

    check("lock ordering: the freshly equipped role survives the race",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("lock ordering: the ownership gate decided, not Discord",
          world.member.remove_calls == [], str(world.member.remove_calls))
    check("lock ordering: reported as kept for ownership",
          result["events"] and result["events"][0]["status"] == "kept"
          and "owned role item" in result["events"][0]["reason"],
          str(result["events"]))


async def _chain_world(keep, conflict, d_role, *, held, before, after):
    """A > B > D with the ids handed in, so the caller controls ordering."""
    await reset_database()
    await add_rule(keep, [conflict])
    await add_rule(conflict, [d_role])
    world = World(held=held)
    result = await world.event(before, after)
    return world, result


async def test_chain_b_added_while_a_and_d_are_held():
    """A > B > D, A and D already held, B newly added: remove B, keep D.

    B is being displaced by A, so B must not get to run its own rule and strip
    D in the same pass. This is the reproduction from the review.
    """
    world, result = await _chain_world(
        KEEP, CONFLICT_B, CONFLICT_D,
        held=(KEEP, CONFLICT_D, CONFLICT_B),
        before={KEEP, CONFLICT_D}, after={KEEP, CONFLICT_D, CONFLICT_B})
    check("chain/held: B is removed", result["removed"] == [CONFLICT_B],
          str(result))
    check("chain/held: D is preserved", CONFLICT_D in world.member.role_ids(),
          str(world.member.role_ids()))
    check("chain/held: A survives", KEEP in world.member.role_ids(),
          str(world.member.role_ids()))


async def test_chain_a_and_b_arrive_together():
    """Same chain, but A and B arrive in one update while D is held."""
    world, result = await _chain_world(
        KEEP, CONFLICT_B, CONFLICT_D,
        held=(CONFLICT_D, KEEP, CONFLICT_B),
        before={CONFLICT_D}, after={CONFLICT_D, KEEP, CONFLICT_B})
    check("chain/simultaneous: B is removed", result["removed"] == [CONFLICT_B],
          str(result))
    check("chain/simultaneous: D is preserved",
          CONFLICT_D in world.member.role_ids(), str(world.member.role_ids()))


async def test_chain_is_independent_of_role_id_order():
    """The same chain with the ids reversed must behave identically.

    Iterating roles by id, or trusting set/dict order, makes the A > B > D
    outcome depend on which role is looked at first. Swapping the numeric order
    of the whole chain proves the result comes from the rules, not the ids.
    """
    # Low-to-high ids (the natural case).
    forward, forward_result = await _chain_world(
        4301, 4302, 4303,
        held=(4301, 4303, 4302),
        before={4301, 4303}, after={4301, 4303, 4302})
    check("chain/id order forward: only B removed",
          forward_result["removed"] == [4302], str(forward_result))
    check("chain/id order forward: D preserved",
          4303 in forward.member.role_ids(), str(forward.member.role_ids()))

    # The same three roles with the numbers inverted: A > B > D stays A > B > D
    # logically, but B now has the LOWEST id and D the HIGHEST.
    reverse, reverse_result = await _chain_world(
        4503, 4502, 4501,
        held=(4503, 4501, 4502),
        before={4503, 4501}, after={4503, 4501, 4502})
    check("chain/id order reversed: only B removed",
          reverse_result["removed"] == [4502], str(reverse_result))
    check("chain/id order reversed: D preserved",
          4501 in reverse.member.role_ids(), str(reverse.member.role_ids()))


async def test_chain_outcome_does_not_use_set_iteration_order():
    """Same ids, different insertion order into the added-role set."""
    outcomes = []
    for order in ((KEEP, CONFLICT_B), (CONFLICT_B, KEEP)):
        await reset_database()
        await add_rule(KEEP, [CONFLICT_B])
        await add_rule(CONFLICT_B, [CONFLICT_D])
        world = World(held=(CONFLICT_D,))
        world.set_roles(CONFLICT_D, *order)
        # The event snapshot's role order is what varies; the engine sees sets.
        after = {CONFLICT_D}
        for role in order:
            after.add(role)
        result = await world.event({CONFLICT_D}, after)
        outcomes.append((tuple(sorted(result["removed"])),
                         CONFLICT_D in world.member.role_ids()))
    check("chain/iteration order: both snapshot orders agree",
          outcomes[0] == outcomes[1] == ((CONFLICT_B,), True), str(outcomes))


# ── 6b. Ownership source unavailable vs. confirmed entitlement ───────────

class _OwnershipDown:
    """Stand-in for utils.role_replacement.role_removal_verdict being unreadable."""

    def __init__(self, message="ownership could not be verified (store unavailable)"):
        self.message = message
        self.calls = 0

    async def __call__(self, *_args, **_kwargs):
        self.calls += 1
        raise RR.OwnershipUnavailable(self.message)


async def test_unanswered_ownership_keeps_the_retry_row():
    """A source we could not read must not erase the pending removal.

    "Could not check" is not "nobody else owns it": the role stays, but the
    retry has to survive so a later sweep can finish the job.
    """
    await reset_database()
    rule = await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B))
    down = _OwnershipDown()
    original = RR.role_removal_verdict
    RR.role_removal_verdict = down
    try:
        result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    finally:
        RR.role_removal_verdict = original

    check("source down: the role is NOT removed",
          result["removed"] == [] and CONFLICT_B in world.member.role_ids(),
          str(result))
    check("source down: never sent a removal to Discord",
          world.member.remove_calls == [], str(world.member.remove_calls))
    check("source down: the check was actually attempted", down.calls >= 1,
          str(down.calls))
    check("source down: reported as unverified, not as a keep decision",
          result["events"] and result["events"][0]["status"] == "unverified",
          str(result["events"]))
    check("source down: the retry row SURVIVES",
          pending_rows() == [(GUILD, USER, CONFLICT_B)], str(pending_rows()))


async def test_transient_ownership_failure_then_success_completes():
    """After the source recovers the retry completes, conditions permitting."""
    await reset_database()
    rule = await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B))
    down = _OwnershipDown()
    original = RR.role_removal_verdict
    RR.role_removal_verdict = down
    try:
        await world.event({KEEP}, {KEEP, CONFLICT_B})
    finally:
        RR.role_removal_verdict = original
    check("recovery: precondition - a retry row exists",
          pending_rows() == [(GUILD, USER, CONFLICT_B)], str(pending_rows()))
    check("recovery: precondition - the role is still held",
          CONFLICT_B in world.member.role_ids())

    # The source is readable again; nothing else changed.
    summary = await world.retry()
    check("recovery: the retry now completes the removal",
          summary["removed"] == 1 and pending_rows() == [], str(summary))
    check("recovery: the role is gone",
          CONFLICT_B not in world.member.role_ids(), str(world.member.role_ids()))


async def test_recovery_does_not_remove_when_the_keep_role_is_gone():
    """Recovery alone is not enough: the rule conditions are re-checked."""
    await reset_database()
    rule = await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B))
    down = _OwnershipDown()
    original = RR.role_removal_verdict
    RR.role_removal_verdict = down
    try:
        await world.event({KEEP}, {KEEP, CONFLICT_B})
    finally:
        RR.role_removal_verdict = original

    world.set_roles(CONFLICT_B)          # the keep role is lost before retry
    summary = await world.retry()
    check("recovery/keep gone: the conflicting role survives",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("recovery/keep gone: nothing attempted",
          world.member.remove_calls == [], str(world.member.remove_calls))
    check("recovery/keep gone: the row is cancelled, not retried forever",
          summary["cleared"] == 1 and pending_rows() == [], str(summary))


async def test_confirmed_entitlement_is_terminal_not_an_endless_retry():
    """A real entitlement keeps the role AND drops the row, permanently."""
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
            "reward_ref,track,status,payload_json,created_at) VALUES "
            "(?,?,?,?,'role','fulfilled',?,CURRENT_TIMESTAMP)",
            (GUILD, USER, 5, "member", json.dumps({"role_id": CONFLICT_B})))
    world = World(held=(KEEP, CONFLICT_B))
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("entitled: a terminal keep decision",
          result["kept"] and result["events"][0]["status"] == "kept",
          str(result["events"]))
    check("entitled: no retry row is created", pending_rows() == [],
          str(pending_rows()))
    # Retrying must not resurrect the removal either.
    summary = await world.retry()
    check("entitled: a later sweep removes nothing",
          summary["removed"] == 0 and CONFLICT_B in world.member.role_ids(),
          str(summary))
    check("entitled: still no retry row", pending_rows() == [],
          str(pending_rows()))


async def test_confirmed_entitlement_clears_an_existing_retry_row():
    """A forced pending row is cancelled once ownership is confirmed."""
    await reset_database()
    rule = await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO role_replacement_pending (guild_id,user_id,role_id,"
            "rule_id,attempts,last_error,created_at,updated_at) VALUES (?,?,?,?,1,?,?,?)",
            (GUILD, USER, CONFLICT_B, rule["id"], "boom", "x", "x"))
    db_exec("INSERT INTO level_reward_claims (guild_id,user_id,reward_level,"
            "reward_ref,track,status,payload_json,created_at) VALUES "
            "(?,?,?,?,'role','fulfilled',?,CURRENT_TIMESTAMP)",
            (GUILD, USER, 5, "member", json.dumps({"role_id": CONFLICT_B})))
    world = World(held=(KEEP, CONFLICT_B))
    summary = await world.retry()
    check("entitled/retry: the role is kept",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("entitled/retry: the row is cleared as a decision",
          summary["cleared"] == 1 and pending_rows() == [], str(summary))


async def test_unanswered_ownership_during_retry_keeps_the_row():
    """The same distinction holds on the retry path, not just on events."""
    await reset_database()
    rule = await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO role_replacement_pending (guild_id,user_id,role_id,"
            "rule_id,attempts,last_error,created_at,updated_at) VALUES (?,?,?,?,1,?,?,?)",
            (GUILD, USER, CONFLICT_B, rule["id"], "boom", "x", "x"))
    world = World(held=(KEEP, CONFLICT_B))
    down = _OwnershipDown()
    original = RR.role_removal_verdict
    RR.role_removal_verdict = down
    try:
        summary = await world.retry()
    finally:
        RR.role_removal_verdict = original
    check("retry/source down: nothing removed",
          CONFLICT_B in world.member.role_ids() and summary["removed"] == 0,
          str(summary))
    check("retry/source down: the row is NOT deleted",
          summary["pending"] == 1 and pending_rows() == [(GUILD, USER, CONFLICT_B)],
          str(summary))
    check("retry/source down: the attempt was counted",
          db_rows("SELECT attempts FROM role_replacement_pending")[0][0] >= 1)


async def test_retry_respects_a_disabled_rule():
    async def disable(rule):
        await RR.set_rule_enabled(GUILD, rule["id"], False)

    rule, world, before, summary = await _failed_pending_then(disable)
    check("retry/disabled: a pending row was created to begin with",
          before == [(GUILD, USER, CONFLICT_B)], str(before))
    check("retry/disabled: the role is NOT removed",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("retry/disabled: never even attempted",
          world.member.remove_calls == [], str(world.member.remove_calls))
    check("retry/disabled: the row is cancelled, not left pending",
          summary["removed"] == 0 and summary["cleared"] == 1
          and pending_rows() == [], str(summary))


async def test_retry_respects_a_deleted_rule():
    async def delete(rule):
        await RR.delete_rule(GUILD, rule["id"])

    rule, world, before, summary = await _failed_pending_then(delete)
    check("retry/deleted: the role is NOT removed",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("retry/deleted: never even attempted",
          world.member.remove_calls == [], str(world.member.remove_calls))
    check("retry/deleted: the row is cancelled",
          summary["cleared"] == 1 and pending_rows() == [], str(summary))


async def test_retry_respects_the_conflict_being_edited_away():
    async def edit(rule):
        await RR.save_rule(GUILD, KEEP, [CONFLICT_C], rule_id=rule["id"])

    rule, world, before, summary = await _failed_pending_then(edit)
    check("retry/edited: the removed conflict is no longer configured",
          CONFLICT_B not in (await RR.get_rule(GUILD, rule["id"]))["conflict_role_ids"],
          str(await RR.get_rule(GUILD, rule["id"])))
    check("retry/edited: the role is NOT removed",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("retry/edited: the row is cancelled",
          summary["cleared"] == 1 and pending_rows() == [], str(summary))


async def test_retry_respects_a_pending_row_with_no_rule():
    """A row that names no rule has nothing to justify it: keep the role."""
    await reset_database()
    db_exec("INSERT INTO role_replacement_pending (guild_id,user_id,role_id,"
            "rule_id,attempts,last_error,created_at,updated_at) "
            "VALUES (?,?,?,NULL,1,?,?,?)",
            (GUILD, USER, CONFLICT_B, "boom", "x", "x"))
    world = World(held=(CONFLICT_B,))
    summary = await world.retry()
    check("retry/no rule: the role is NOT removed",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("retry/no rule: the orphan row is cancelled",
          summary["cleared"] == 1 and pending_rows() == [], str(summary))


async def test_retry_respects_a_keep_role_lost_before_the_retry():
    await reset_database()
    rule = await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO role_replacement_pending (guild_id,user_id,role_id,"
            "rule_id,attempts,last_error,created_at,updated_at) VALUES (?,?,?,?,1,?,?,?)",
            (GUILD, USER, CONFLICT_B, rule["id"], "boom", "x", "x"))
    world = World(held=(CONFLICT_B,))        # A is gone by retry time
    summary = await world.retry()
    check("retry/keep lost: the conflicting role survives",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("retry/keep lost: nothing was attempted",
          world.member.remove_calls == [], str(world.member.remove_calls))
    check("retry/keep lost: the row is cleared",
          summary["removed"] == 0 and summary["cleared"] == 1
          and pending_rows() == [], str(summary))


async def test_retry_with_an_unreadable_rule_keeps_waiting():
    """A failed configuration read must never be read as "not configured"."""
    from unittest.mock import patch
    await reset_database()
    rule = await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO role_replacement_pending (guild_id,user_id,role_id,"
            "rule_id,attempts,last_error,created_at,updated_at) VALUES (?,?,?,?,1,?,?,?)",
            (GUILD, USER, CONFLICT_B, rule["id"], "boom", "x", "x"))
    world = World(held=(KEEP, CONFLICT_B))

    async def boom(*_a, **_k):
        raise RuntimeError("config store unavailable")

    with patch.object(RR, "get_rule", boom):
        summary = await world.retry()
    check("retry/unreadable config: the role is NOT removed",
          CONFLICT_B in world.member.role_ids(), str(world.member.role_ids()))
    check("retry/unreadable config: the row stays pending, not cancelled",
          summary["pending"] == 1 and summary["cleared"] == 0
          and bool(pending_rows()), str(summary))
    check("retry/unreadable config: nothing was attempted",
          world.member.remove_calls == [], str(world.member.remove_calls))


async def test_retry_still_applies_a_genuinely_pending_removal():
    """The configuration checks must not turn into a blanket refusal."""
    async def noop(_rule):
        return None

    rule, world, before, summary = await _failed_pending_then(noop)
    check("retry/applies: a still-configured removal does go through",
          summary["removed"] == 1 and pending_rows() == [], str(summary))
    check("retry/applies: the role is gone",
          CONFLICT_B not in world.member.role_ids(), str(world.member.role_ids()))


# ── 5. Discord failures and retries ──────────────────────────────────────

async def test_permission_failure_records_retry_then_succeeds():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B), fail_remove=True)
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("failure: reported as failed, never as removed",
          result["removed"] == [] and result["failed"], str(result))
    check("failure: a durable retry row exists",
          pending_rows() == [(GUILD, USER, CONFLICT_B)], str(pending_rows()))
    check("failure: the role is still on the member",
          CONFLICT_B in world.member.role_ids())

    world.member.fail_remove = False
    summary = await world.retry()
    check("retry: removal succeeded", summary["removed"] == 1, str(summary))
    check("retry: retry row cleared", pending_rows() == [], str(pending_rows()))
    check("retry: role gone from the member",
          CONFLICT_B not in world.member.role_ids())


async def test_unconfirmed_removal_is_not_success():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B), lie_remove=True)
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("unconfirmed: not reported as removed", result["removed"] == [],
          str(result))
    check("unconfirmed: retry row recorded",
          pending_rows() == [(GUILD, USER, CONFLICT_B)], str(pending_rows()))

    # Now Discord confirms the role is gone (removed out of band): retry clears.
    world.set_roles(KEEP)
    summary = await world.retry()
    check("unconfirmed: retry records the resolution",
          summary["cleared"] == 1 and pending_rows() == [], str(summary))


async def test_hierarchy_failure_is_reported_and_kept_pending():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B))
    world.guild.roles[CONFLICT_B].position = 80     # above the bot's own role
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("hierarchy: blocked before any Discord call",
          world.member.remove_calls == [] and result["blocked"], str(result))
    check("hierarchy: the reason names the hierarchy",
          "above" in (result["blocked"][0]["reason"] or "").lower(),
          str(result["blocked"]))
    check("hierarchy: a retry row exists while it stays unassignable",
          pending_rows() == [(GUILD, USER, CONFLICT_B)], str(pending_rows()))
    summary = await world.retry()
    check("hierarchy: retry keeps waiting, never claims success",
          summary["pending"] == 1 and bool(pending_rows()), str(summary))
    # Once the role is moved back below the bot's role the retry succeeds.
    world.guild.roles[CONFLICT_B].position = 9
    summary = await world.retry()
    check("hierarchy: retry succeeds once it becomes assignable",
          summary["removed"] == 1 and pending_rows() == [], str(summary))


async def test_deleted_conflicting_role_clears_pending():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO role_replacement_pending (guild_id,user_id,role_id,"
            "attempts,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (GUILD, USER, CONFLICT_B, 1, "boom", "x", "x"))
    world = World(held=(KEEP,))
    del world.guild.roles[CONFLICT_B]
    summary = await world.retry()
    check("deleted role: pending row cleared",
          summary["cleared"] == 1 and pending_rows() == [], str(summary))


async def test_member_left_the_guild_clears_pending():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    db_exec("INSERT INTO role_replacement_pending (guild_id,user_id,role_id,"
            "attempts,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (GUILD, USER, CONFLICT_B, 1, "boom", "x", "x"))
    world = World(held=())
    del world.guild.members[USER]
    summary = await world.retry()
    check("member absent: pending row cleared, not reported as a removal",
          summary["cleared"] == 1 and summary["removed"] == 0
          and pending_rows() == [], str(summary))


async def test_unreadable_ownership_fails_closed():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B])
    world = World(held=(KEEP, CONFLICT_B))

    async def boom(*args, **kwargs):
        raise RuntimeError("store unavailable")

    original = RR.role_removal_verdict
    RR.role_removal_verdict = boom
    try:
        result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    finally:
        RR.role_removal_verdict = original
    check("fail closed: an unreadable ownership store keeps the role",
          result["removed"] == [] and result["failed"], str(result))
    check("fail closed: the reason says it could not be verified",
          "could not be verified" in (result["failed"][0]["reason"] or ""),
          str(result["failed"]))
    check("fail closed: nothing was removed from the member",
          CONFLICT_B in world.member.role_ids(),
          str(world.member.role_ids()))


# ── 6. Runtime visibility ────────────────────────────────────────────────

async def test_rule_is_visible_without_restart():
    await reset_database()
    world = World(held=(KEEP, CONFLICT_B))
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("no rule yet: nothing happens",
          world.member.remove_calls == [] and result["evaluated"] is False,
          str(result))
    await add_rule(KEEP, [CONFLICT_B])
    # No cache to invalidate, no reload: the very next update honours the rule.
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("rule saved mid-process is honoured by the next update",
          result["removed"] == [CONFLICT_B], str(result))
    check("runtime visibility: role actually removed",
          world.member.role_ids() == {KEEP}, str(world.member.role_ids()))


async def test_guild_isolation():
    await reset_database()
    await add_rule(KEEP, [CONFLICT_B], guild_id=GUILD)
    other = GUILD + 500
    world = World(held=(KEEP, CONFLICT_B), guild_id=other)
    result = await world.event({KEEP}, {KEEP, CONFLICT_B})
    check("guild isolation: another guild's rule does not apply",
          result["evaluated"] is False and world.member.remove_calls == [],
          str(result))
    check("guild isolation: rules are per guild",
          await RR.list_rules(other) == [])


async def test_chain_ordering_is_deterministic():
    await reset_database()
    # Rule 1: A displaces B. Rule 2: B displaces D.  (A > B > D, no cycle)
    await add_rule(KEEP, [CONFLICT_B])
    await add_rule(CONFLICT_B, [CONFLICT_D])
    world = World(held=(CONFLICT_D, KEEP, CONFLICT_B))
    result = await world.event({CONFLICT_D}, {CONFLICT_D, KEEP, CONFLICT_B})
    check("chain: B removed because A displaced it",
          result["removed"] == [CONFLICT_B], str(result))
    check("chain: D (only B displaces it) is untouched, no cascade",
          CONFLICT_D in world.member.role_ids(), str(world.member.role_ids()))
    check("chain: A survives", KEEP in world.member.role_ids())


async def test_schema_is_additive():
    await reset_database()
    tables = {r[0] for r in db_rows(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("role_replacement_rules", "role_replacement_conflicts",
                  "role_replacement_pending"):
        check(f"schema: {table} exists", table in tables)
    columns = [r[1] for r in db_rows("PRAGMA table_info(role_replacement_rules)")]
    check("schema: expected rule columns",
          columns == ["id", "guild_id", "keep_role_id", "enabled",
                      "created_at", "updated_at"], str(columns))


async def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and asyncio.iscoroutinefunction(value)]
    for test in tests:
        print(f"\n== {test.__name__} ==")
        await test()
    print(f"\nROLE REPLACEMENT RUNTIME: "
          f"{'ALL CHECKS PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    for name in FAILS:
        print(f"  FAILED: {name}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
