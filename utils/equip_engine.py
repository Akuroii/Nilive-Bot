import asyncio
import functools

import discord
import aiosqlite
from database import DB_PATH
from utils.permissions import check_bot_role_position

# ═══════════════════════════════════════════════════════════════════════
# EQUIP ENGINE — Rank Card foundation (pass 1: schema + backend)
#
# One member can have exactly one "equipped" role/temp_role-type
# inventory item at a time (enforced by equipped_roles' PK on
# (guild_id, user_id) — see database.py). equip_role() is the single
# place that ever performs the actual Discord-side swap: remove
# whatever was previously equipped (if different), add the new one,
# update equipped_roles. Two callers share this:
#
#   1. utils/reward_engine.py's role/temp_role branch — buying or
#      winning a role/temp_role item auto-equips it, matching the
#      role/temp_role reward type's existing behavior (the Discord
#      role has always been granted immediately; this pass just
#      routes that grant through the shared swap instead of a raw
#      add_roles() call, so it also updates equipped_roles/inventory
#      correctly). Locked with Dark: this applies uniformly to every
#      reward_type role/temp_role grant — shop, events, minigames,
#      missions, tag missions, tag partners — since all of those are
#      "you got a cosmetic role as a reward" in the same sense.
#   2. cogs/shop.py's /inventory command — the member manually picking
#      a different OWNED role item to wear instead.
#
# Deliberately does NOT touch anything about the Rank Card's display —
# per Dark: "The Rank Card has no connection to the equipped-role
# system." This module only manages the Discord-role side + the
# equipped_roles bookkeeping table.
# ═══════════════════════════════════════════════════════════════════════


# Shared with timed ownership, legacy expiry, and booster role changes.
# This lock is process-local; it does not coordinate separate bot processes.
# Keep one lock per member per active event loop so tests and bot reloads do not
# reuse asyncio locks bound to a different loop. Locks are retained rather than
# evicted: removing a lock with waiters could create two locks for one member.
# This assumes one bot event loop at a time; simultaneous loops, or multiple
# processes sharing the database, are not coordinated by this registry.
_SLOT_LOCKS: dict[tuple[int, int], tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def slot_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (int(guild_id), int(user_id))
    entry = _SLOT_LOCKS.get(key)
    if entry is None or entry[0] is not loop:
        entry = _SLOT_LOCKS[key] = (loop, asyncio.Lock())
    return entry[1]


def _serialised_per_member(func):
    @functools.wraps(func)
    async def wrapper(bot, guild_id, user_id, *args, **kwargs):
        async with slot_lock(guild_id, user_id):
            return await func(bot, guild_id, user_id, *args, **kwargs)
    return wrapper


async def get_equipped(guild_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT item_name, role_id, equipped_at, entry_key,
                   role_added, role_added_role_id
            FROM equipped_roles WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    if not row:
        return None
    return {"item_name": row[0], "role_id": row[1], "equipped_at": row[2],
            # NULL for rows written before timed ownership existed; the caller
            # treats that as "legacy permanent ownership".
            "entry_key": row[3],
            # What the writing operation observed about this Discord role, and
            # the role that observation is about. Read them through
            # observed_role_state(), never directly: NULL and a value bound to
            # another role both mean "unknown".
            "role_added": row[4], "role_added_role_id": row[5]}


def observed_role_state(slot: dict | None) -> bool | None:
    """What the equip that wrote `slot` recorded about its Discord role.

    True  - it observed the role absent and its add-role request returned;
    False - it observed the role already on the member and sent no add;
    None  - unknown: no observation (a row written before the column, an older
            writer, or one that could not establish it), or a value recorded
            for a DIFFERENT role than the slot now names. Stale metadata is
            never read as permission to remove the current role.

    This is a record of observation, not proof of ownership: a role granted by
    an admin or another bot after the equip cannot be told apart from the one
    the equip added.
    """
    if not slot:
        return None
    try:
        role_id = int(slot["role_id"])
        recorded = slot.get("role_added")
        bound_to = slot.get("role_added_role_id")
        if recorded is None or bound_to is None or int(bound_to) != role_id:
            return None
        value = int(recorded)
    except (KeyError, TypeError, ValueError):
        return None
    if value == 1:
        return True
    if value == 0:
        return False
    return None


def _observation_to_store(previous: dict | None, role_id: int,
                          role_added: bool | None) -> tuple[int | None, int | None]:
    """The (role_added, role_added_role_id) pair to persist for a slot write.

    A known "this operation added it" always wins: it is direct evidence.
    Otherwise a previous observation BOUND TO THE SAME Discord role carries
    over — switching between two holdings of one role does not change who put
    the role on the member, and recording "already present" there would leak
    the role on a later Unequip. A previous observation for another role, or
    none at all, is never inherited. `None` means unknown and is stored as
    NULL, exactly like a legacy row.
    """
    role_id = int(role_id)
    if role_added is True:
        return 1, role_id
    if previous is not None:
        try:
            same_role = int(previous["role_id"]) == role_id
        except (KeyError, TypeError, ValueError):
            same_role = False
        state = observed_role_state(previous) if same_role else None
        if state is not None:
            return (1 if state else 0), role_id
    if role_added is False:
        return 0, role_id
    return None, None


async def record_equipped(guild_id: int, user_id: int, item_name: str,
                          *, role_id: int, entry_key: str | None,
                          role_added: bool | None = None) -> None:
    """Write the single equip slot. Bookkeeping only — no Discord calls.

    `role_added` is the observation the writing operation made about this
    Discord role (see observed_role_state). Every writer must pass what it
    observed, or leave it None to store "unknown" explicitly — never guess.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        previous = await (await db.execute(
            "SELECT item_name,role_id,entry_key,role_added,role_added_role_id"
            " FROM equipped_roles WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))).fetchone()
        carry = None
        if previous is not None:
            carry = {"role_id": previous[1], "role_added": previous[3],
                     "role_added_role_id": previous[4]}
        stored_state, stored_role = _observation_to_store(carry, role_id,
                                                          role_added)
        await db.execute("""
            INSERT INTO equipped_roles
                (guild_id, user_id, item_name, role_id, entry_key, equipped_at,
                 role_added, role_added_role_id)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                item_name   = excluded.item_name,
                role_id     = excluded.role_id,
                entry_key   = excluded.entry_key,
                equipped_at = CURRENT_TIMESTAMP,
                role_added  = excluded.role_added,
                role_added_role_id = excluded.role_added_role_id
        """, (guild_id, user_id, item_name, int(role_id), entry_key,
              stored_state, stored_role))
        if previous is None or previous[:3] != (item_name, int(role_id), entry_key):
            from utils.inventory_activity import record_activity
            await record_activity(db, guild_id=guild_id, user_id=user_id,
                                  event_type="equipped", item_name=item_name,
                                  item_type="role", quantity=1, source="equipment")
        await db.commit()


async def _role_presence_for_add(guild, member, role, user_id: int
                                 ) -> tuple[bool, dict | None]:
    """(role_was_present, failure) for an operation about to add `role`.

    The cached member can say "absent" long after the role was granted by an
    admin or another bot. Adding it then would also record it as a role THIS
    operation added, and a later Unequip would strip someone's other grant. So
    an observed absence is confirmed against Discord first:

      * fetch says the role is on the member -> treat it as already present
        and do not add it;
      * fetch says it is absent -> add it and record the observation;
      * fetch fails -> do NOT add on an unverified absence. Nothing is sent to
        Discord and nothing is written; the caller gets a clean failure.

    This narrows the window to the gap between the fetch and the add. It does
    not close it: a grant landing in that gap is still invisible here, and so
    is any grant made after the add.
    """
    if role in (getattr(member, "roles", None) or ()):
        return True, None
    fetch = getattr(guild, "fetch_member", None)
    if not callable(fetch):
        print(f"[EQUIP] Guild {getattr(guild, 'id', '?')} cannot fetch member "
              f"{user_id}; refusing to add a role on an unverified absence")
        return False, {"success": False, "role_added": False,
                       "uncertain": False,
                       "error": ("Could not confirm your current roles with "
                                 "Discord. Nothing was changed — please try "
                                 "again.")}
    try:
        fresh = await fetch(user_id)
    except discord.NotFound:
        return False, {"success": False, "role_added": False,
                       "uncertain": False,
                       "error": "Member not found in guild."}
    except Exception as e:
        print(f"[EQUIP] Could not fetch member {user_id} in "
              f"{getattr(guild, 'id', '?')} before adding role "
              f"{getattr(role, 'id', role)}: {e}")
        return False, {"success": False, "role_added": False,
                       "uncertain": False,
                       "error": ("Could not confirm your current roles with "
                                 "Discord. Nothing was changed — please try "
                                 "again.")}
    return role in (getattr(fresh, "roles", None) or ()), None


def _removal_failure_is_uncertain(exc: BaseException, member, role) -> bool:
    """Whether a failed remove_roles may still have taken the role off.

    A definitive client rejection (`Forbidden`, or another 4xx that is not a
    rate limit) means Discord did not apply the request. A timeout, transport
    error, 5xx or 429 says nothing either way, and neither does the member
    cache, which only the gateway updates. `NotFound` is included in
    "uncertain": for a removal it usually means the assignment is already gone.
    """
    if role not in (getattr(member, "roles", None) or ()):
        return True                     # off the member: maybe this call did it
    if isinstance(exc, discord.NotFound):
        return True
    if isinstance(exc, discord.Forbidden):
        return False
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", None)
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            return False
    return True


async def _release_role(bot, guild_id: int, user_id: int, slot: dict, *,
                        guild, member, reason: str, journal: dict) -> dict:
    """Decide, and where allowed perform, what happens to the role `slot` names.

    One rule for Unequip and for the outgoing half of a Swap:

      "absent"     - the role is not on the member (or no longer exists):
                     nothing to remove, the caller may clear/replace the slot;
      "kept"       - the role stays because the writing equip observed it
                     already on the member, or because an entitlement that does
                     not rest on this slot covers it; the caller may
                     clear/replace the slot;
      "removed"    - this call removed it and Discord confirmed; the caller may
                     clear/replace the slot;
      "unresolved" - the role stays AND the slot must not be cleared or
                     replaced: the observation is missing or stale, an
                     entitlement could not be verified, or the removal was
                     refused or not confirmed.

    `journal["discord_step"]` is set to "removing"/"removed" around the removal
    so a cancellation the caller sees can be reported as "may have changed the
    member". Never raises: a cancellation is left to propagate.
    """
    def back(action, *, uncertain=False, error=None, role=None, detail=None):
        return {"action": action, "uncertain": bool(uncertain), "error": error,
                "role": role, "detail": detail}

    try:
        role_id = int(slot["role_id"])
    except (KeyError, TypeError, ValueError) as e:
        return back("unresolved", uncertain=True,
                    error=f"Could not read the equipped role: {e}")

    if guild is None or member is None:
        return back("unresolved", uncertain=True,
                    error=(f"Could not check the member's current roles with "
                           f"Discord, so role {role_id} and its slot were left "
                           f"as they are."))
    role = guild.get_role(role_id)
    if role is None or role not in (getattr(member, "roles", None) or ()):
        # Gone from Discord: the slot is obsolete bookkeeping, nothing to
        # remove. (The cache is the only witness here; a stale "absent" leaves
        # an unrecorded role on the member — documented, and no worse than the
        # old code, which also only looked at the cache.)
        return back("absent", role=role)

    state = observed_role_state(slot)
    if state is None:
        print(f"[EQUIP] Role {role_id} on {user_id} in {guild_id} has no usable "
              f"record of who added it (legacy or stale metadata); the role "
              f"and the slot are left alone.")
        return back("unresolved", uncertain=True,
                    error=(f"Nilive cannot confirm whether it added role "
                           f"{role_id}, so the role and the slot were left as "
                           f"they are. An admin can remove the role by hand; "
                           f"Unequip then clears the slot."))
    if state is False:
        return back("kept", detail="observed already present before its equip")

    # The writing equip added it. Only an entitlement that does not rest on
    # wearing the role may keep it: the role itself must not vouch for itself
    # (the Booster "already worn" rule), so it is asked as if not worn.
    from utils.timed_ownership import non_slot_role_entitlement
    try:
        entitled = await non_slot_role_entitlement(
            guild_id, user_id, role_id, bot=bot,
            member=_MemberRoleView(member, role, worn=False))
    except Exception as e:
        print(f"[EQUIP] Could not verify other entitlements to role {role_id} "
              f"for {user_id} in {guild_id}; role and slot left as they are: "
              f"{e}")
        return back("unresolved", uncertain=True,
                    error=f"Could not verify role entitlements: {e}")
    if entitled:
        return back("kept", detail=f"entitled by {entitled}")

    journal["discord_step"] = "removing"
    try:
        await member.remove_roles(role, reason=reason)
    except Exception as e:
        uncertain = _removal_failure_is_uncertain(e, member, role)
        print(f"[EQUIP] Failed to remove role {role_id} from {user_id} in "
              f"{guild_id}: {e}"
              + (" (it may or may not have been removed)" if uncertain else ""))
        return back("unresolved", uncertain=uncertain,
                    error=(f"Discord error removing role: {e}"
                           + (" The role may or may not have been removed."
                              if uncertain else "")))
    journal["discord_step"] = "removed"
    return back("removed", role=role)


class _SlotWriteOutcomeUnknown(Exception):
    """The slot write failed and its outcome could not be reconciled.

    Raised instead of the underlying database error in the two cases where the
    caller must NOT report a clean failure:

      * the previously equipped role had already been released and putting it
        back failed, so the old slot names a role the member may no longer wear;
      * the slot could not be read back, so whether the write landed - and
        therefore whether the previous role is still needed - is unknown.

    `cause` is the original error, kept for the message and the log.
    """

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


async def swap_and_record_equipped(bot: discord.Client | None,
                                   guild_id: int, user_id: int, *,
                                   item_name: str, role_id: int,
                                   entry_key: str | None = None,
                                   guild=None, member=None,
                                   reason: str = "Equipped",
                                   role_added: bool | None = None) -> dict:
    """Perform the guarded single-slot Discord swap and update `equipped_roles`.

    The previously equipped entry is resolved BEFORE the slot is written:

      * it names the same `role_id` -> the Discord role stays worn and the slot
        is rewritten (the observation bound to that role is carried over);
      * it names a different role -> `_release_role` decides whether that role
        may come off: it stays if the equip that added it saw it already there
        or an independent entitlement covers it, and it is removed only when
        the recorded observation allows it and Discord confirms the removal.
        If it cannot be resolved, NOTHING is written: the old slot stays, the
        member keeps the old role, and the caller is told the swap was refused
        (`{"success": False, ...}`) so it can take back the role it added.

    `role_added` is the caller's own observation for the NEW role (it added it,
    or it found it already worn); see `record_equipped`.

    Inventory / timed ownership of the old item is never modified. Raises only
    on a real database error (the caller then treats the slot as unwritten).
    """
    if guild is None and bot is not None:
        guild = bot.get_guild(guild_id)
    if member is None and guild is not None:
        member = guild.get_member(user_id)

    current = await get_equipped(guild_id, user_id)
    released = None

    if current:
        same_slot = (
            (current.get("entry_key") is not None
             and entry_key is not None
             and current.get("entry_key") == entry_key)
            or (current.get("entry_key") is None
                and (entry_key is None or str(entry_key).startswith("i:"))
                and current["item_name"] == item_name
                and int(current["role_id"]) == int(role_id))
        )
        if not same_slot and int(current["role_id"]) != int(role_id):
            journal = {"discord_step": "none"}
            released = await _release_role(
                bot, guild_id, user_id, current, guild=guild, member=member,
                reason=f"{reason} — swapped out", journal=journal)
            if released["action"] == "unresolved":
                return {"success": False, "previous": current,
                        "entry_key": entry_key, "role_id": int(role_id),
                        "uncertain": released["uncertain"],
                        "previous_role_uncertain": (
                            journal["discord_step"] != "none"
                            and bool(released["uncertain"])),
                        "old_role": int(current["role_id"]),
                        "error": (released["error"] or
                                  "The previously equipped role could not be "
                                  "removed, so nothing was changed.")}

    try:
        await record_equipped(guild_id, user_id, item_name,
                              role_id=int(role_id), entry_key=entry_key,
                              role_added=role_added)
    except Exception as save_error:
        # The previous role may already be off the member (it is released
        # before the slot is written, so the old slot is never overwritten by
        # a swap that has not happened). What the failed write left behind
        # decides the compensation:
        try:
            landed = await get_equipped(guild_id, user_id)
        except Exception as read_error:
            # Cannot tell whether the row landed: do not put the old role back
            # on top of a slot that may name the new one, and never report
            # success. The caller reports and logs the uncertainty for the
            # role it added (this line only adds the previous-role fact).
            print(f"[EQUIP] Slot write for {user_id} in {guild_id} failed "
                  f"({save_error!r}) and the slot can no longer be read "
                  f"({read_error!r}); the previous role may already be off "
                  f"the member. Nothing was retried.")
            raise _SlotWriteOutcomeUnknown(save_error) from save_error
        if landed is not None and int(landed["role_id"]) == int(role_id) and (
                landed.get("entry_key") == entry_key):
            # The row is there: the swap is durable despite the error. Still
            # NOT reported as success - a write that raised is never claimed as
            # a completed equip; the caller's compensation sees the slot naming
            # this role and leaves the role alone.
            print(f"[EQUIP] Slot write for {user_id} in {guild_id} reported "
                  f"{save_error!r} but the row is present; the slot records "
                  f"the new role and it is not removed.")
            raise
        if (released is not None and released["action"] == "removed"
                and released["role"] is not None and member is not None):
            # The old slot is still the durable record and its role is no
            # longer worn: put it back so the record and Discord agree.
            try:
                await member.add_roles(
                    released["role"],
                    reason=f"{reason} — swap could not be saved, previous "
                           f"role restored")
            except Exception as restore_error:
                print(f"[EQUIP][UNCERTAIN] operation=swap guild={guild_id} "
                      f"user={user_id} item={item_name!r} role={role_id} "
                      f"entry={entry_key} - the slot write failed and the "
                      f"previous role could not be restored ({restore_error!r}). "
                      f"The old slot names a role the member may no longer "
                      f"wear. Nothing was retried.")
                raise _SlotWriteOutcomeUnknown(save_error) from save_error
        raise
    return {"success": True, "previous": current, "entry_key": entry_key,
            "role_id": int(role_id)}


_SLOT_UNSPECIFIED = object()


async def _finish(task: asyncio.Future) -> None:
    """Wait until `task` is done, even if the waiter is cancelled meanwhile.

    The caller re-raises its own cancellation afterwards; this only makes sure
    a Discord side effect and its bookkeeping (or its reversal) are not left
    half-done. Bounded by the task itself: one Discord call plus SQLite's busy
    timeout.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break


def _log_uncertain(stage: str, *, guild_id: int, user_id: int, item_name: str,
                   role_id: int, entry_key: str | None, detail: str) -> None:
    """One greppable line for an equip whose Discord state is not confirmed.

    Nothing is retried or removed when this is written: the line carries what
    is needed to check the member by hand (stage is where the operation was).
    """
    print(f"[EQUIP][UNCERTAIN] operation=equip stage={stage} guild={guild_id} "
          f"user={user_id} item={item_name!r} role={role_id} "
          f"entry={entry_key} - {detail}")


class _MemberRoleView:
    """The live member with ONE role forced to be worn or not worn.

    Every other attribute, and every other role, is the live member's own.
    """

    def __init__(self, member, role, *, worn: bool):
        self._member, self._role, self._worn = member, role, worn

    def __getattr__(self, name):
        return getattr(self._member, name)

    @property
    def roles(self):
        role_id = getattr(self._role, "id", self._role)
        others = [r for r in (getattr(self._member, "roles", None) or ())
                  if getattr(r, "id", r) != role_id]
        return others + [self._role] if self._worn else others


async def _take_back_unsaved_role(bot, guild_id: int, user_id: int, member,
                                  role, role_id: int) -> str:
    """Undo a role an equip added but could not record.

    Returns
      "removed"  - the role was taken back;
      "recorded" - the slot does name this role, so the equip landed and the
                   role is left alone;
      "entitled" - another system (Level claim, Prestige/Booster, Twitch, MVP)
                   entitles the member to this role, so it is left on: taking
                   it back would remove a role that system granted;
      "left_on"  - the removal failed, or the slot or the entitlement could
                   not be read, or the only claim to the role is that the
                   member wears it: the role stays on and nothing here
                   accounts for it. Only a slot that was READ and does not
                   name the role, plus two entitlement checks that both
                   answered no, allow the removal.
    Never raises.
    """
    try:
        recorded = await get_equipped(guild_id, user_id)
    except Exception as read_error:
        # Not being able to read the slot is not proof that it is empty: the
        # write this call attempted may have committed. The role stays.
        print(f"[EQUIP] Could not read the equip slot of {user_id} in "
              f"{guild_id}; role {role_id} is left on: {read_error}")
        return "left_on"
    if recorded is not None and int(recorded["role_id"]) == role_id:
        return "recorded"
    try:
        from utils.timed_ownership import non_slot_role_entitlement
        # Asked twice, and never with the live `member.roles` for this role:
        # whether the role shows there depends on gateway timing, and the role
        # this very equip put on must not vouch for itself.
        #   1. As if the role were NOT worn. Only evidence that does not rest
        #      on wearing it (a record, a proven boost level) can say yes.
        #   2. As if it WERE worn. A yes here after a no above means the only
        #      possible claim is "the member wears it" (the Booster rule for
        #      an unknown boost count). Nothing can tell who put the role on,
        #      so it is neither a confirmed entitlement nor safe to remove.
        entitled = await non_slot_role_entitlement(
            guild_id, user_id, role_id, bot=bot,
            member=_MemberRoleView(member, role, worn=False))
        if not entitled and await non_slot_role_entitlement(
                guild_id, user_id, role_id, bot=bot,
                member=_MemberRoleView(member, role, worn=True)):
            print(f"[EQUIP] Role {role_id} on {user_id} in {guild_id} would "
                  f"count as a Booster role only because it is worn; who "
                  f"added it cannot be established, so it is left on.")
            return "left_on"
    except Exception as check_error:
        # Unknown is not "no": the role stays and the caller reports it.
        print(f"[EQUIP] Could not verify other entitlements to role {role_id} "
              f"for {user_id} in {guild_id}; the role is left on: {check_error}")
        return "left_on"
    if entitled:
        return "entitled"
    try:
        await member.remove_roles(
            role, reason="Equip could not be saved - reverted")
        return "removed"
    except Exception as revert_error:
        print(f"[EQUIP] Could not revert role {role_id} on {user_id} in "
              f"{guild_id} after a failed equip: {revert_error}")
        return "left_on"


def slot_identity(slot: dict | None):
    return (slot.get("entry_key"), slot.get("item_name"), int(slot["role_id"])) if slot else None


@_serialised_per_member
async def equip_role(bot: discord.Client, guild_id: int, user_id: int,
                      item_name: str, reason: str = "Equipped",
                      *, entry_key: str | None = None,
                      expected_slot=_SLOT_UNSPECIFIED) -> dict:
    """
    Swaps the member's single equipped role to an ownership entry the member
    already holds — a permanent `inventory_items` row (item_type 'role' or
    'temp_role') or an unexpired timed holding. Never raises; every failure
    mode returns {"success": False, "error": "..."}.

    `entry_key` identifies WHICH entry when several share a display name. When
    it is omitted and the name is ambiguous, this refuses rather than guessing:
    equipping the wrong "Ocean Blue" would put the wrong expiry on the member.
    """
    from utils.inventory import get_inventory_entries, find_entry

    if expected_slot is not _SLOT_UNSPECIFIED and slot_identity(await get_equipped(guild_id, user_id)) != expected_slot:
        return {"success": False, "error": "The equipped role changed. Open the item again."}
    items = await get_inventory_entries(guild_id, user_id)
    owned = find_entry(items, entry_key=entry_key, item_name=item_name)
    if owned and owned["item_name"] != item_name:
        owned = None
    if not owned:
        if entry_key:
            return {"success": False,
                    "error": "That item is no longer in your inventory."}
        ambiguous = sum(1 for it in items if it["item_name"] == item_name)
        if ambiguous > 1:
            return {"success": False,
                    "error": (f"You own more than one **{item_name}** — "
                              f"open the entry you want from `/inventory`.")}
        return {"success": False, "error": f"You don't own **{item_name}**."}
    if owned["item_type"] not in ("role", "temp_role"):
        return {"success": False,
                "error": f"**{item_name}** isn't an equippable role item."}

    meta = owned.get("metadata") or {}
    role_id = meta.get("role_id") or owned.get("role_id")
    if not role_id:
        return {"success": False,
                "error": (f"**{item_name}** has no role attached — "
                          f"ask an admin to check its setup.")}
    role_id = int(role_id)
    owned_key = owned.get("entry_key") or entry_key

    guild = bot.get_guild(guild_id)
    if not guild:
        return {"success": False, "error": "Bot is not in that guild."}
    member = guild.get_member(user_id)
    if not member:
        return {"success": False, "error": "Member not found in guild."}
    role = guild.get_role(role_id)
    if not role:
        return {"success": False,
                "error": f"**{item_name}**'s role no longer exists on this server."}

    can_assign, warning = check_bot_role_position(guild, role)
    if not can_assign:
        return {"success": False, "error": warning}

    # Is the role really absent? The cached member only says what the gateway
    # last echoed. An absence is confirmed against Discord before anything is
    # added, because the add is also what the slot records as "this operation
    # added the role" — and a later Unequip acts on that. A failed fetch is a
    # clean failure here: nothing is sent to Discord and the slot is untouched.
    role_was_present, presence_failure = await _role_presence_for_add(
        guild, member, role, user_id)
    if presence_failure is not None:
        return presence_failure

    # ── Discord + slot write: one unit that a cancellation cannot split ──────
    # Everything above only reads. From here on a Discord role is added and
    # then the slot row is written; a caller cancelled between the two would
    # leave the member wearing a role no record accounts for. So the pair runs
    # as its own task and the caller only waits for it:
    #   * caller cancelled  -> the pair still runs to its normal end (equipped,
    #     or reverted by its own error handling); the cancellation is then
    #     re-raised. Nothing is retried and no second Discord add is sent.
    #   * the pair ITSELF cancelled (event loop shutting down) -> it takes back
    #     the role it added before propagating; if that cannot be confirmed the
    #     outcome is logged as UNCERTAIN, never as a success or a clean failure.
    async def _apply() -> dict:
        try:
            return await _add_and_record(role_was_present)
        except Exception as unexpected:
            # Backstop: every known failure after the role step is handled in
            # _add_and_record. Anything else must still end as a result, not
            # as an exception with a role possibly left on. Nothing is removed
            # here, because how far the operation got is not known.
            role_on = (not role_was_present
                       and role in (getattr(member, "roles", None) or ()))
            if role_on:
                _log_uncertain(
                    "unexpected_error", guild_id=guild_id, user_id=user_id,
                    item_name=item_name, role_id=role_id, entry_key=owned_key,
                    detail=(f"unexpected {unexpected!r} after the role was "
                            f"added; the role is on the member and the slot "
                            f"may not record it. Nothing was retried or "
                            f"removed."))
                return {"success": False, "role_added": True, "uncertain": True,
                        "error": (f"The role was assigned but the equip could "
                                  f"not be completed ({unexpected}). An admin "
                                  f"may need to reconcile the role.")}
            print(f"[EQUIP] Equip of {item_name!r} (role {role_id}) for "
                  f"{user_id} in {guild_id} failed unexpectedly: {unexpected!r}")
            return {"success": False, "role_added": False, "uncertain": False,
                    "error": "The equip could not be completed. Please try again."}
        except BaseException:
            # CancelledError (or interpreter shutdown) inside the unit. The
            # add may have reached Discord even if it never returned.
            if not role_was_present:
                cleanup = asyncio.ensure_future(_take_back_unsaved_role(
                    bot, guild_id, user_id, member, role, role_id))
                await _finish(cleanup)
                state = ("left_on" if cleanup.cancelled() or cleanup.exception()
                         else cleanup.result())
                if state == "left_on":
                    print(f"[EQUIP][UNCERTAIN] Equip of {item_name!r} (role "
                          f"{role_id}) for {user_id} in {guild_id} was "
                          f"cancelled after the Discord role step and the "
                          f"role could not be confirmed removed. The member "
                          f"may be wearing a role the slot does not record; "
                          f"an admin needs to reconcile it.")
                elif state == "recorded":
                    print(f"[EQUIP][UNCERTAIN] Equip of {item_name!r} (role "
                          f"{role_id}) for {user_id} in {guild_id} was "
                          f"cancelled after the slot was saved. The item is "
                          f"equipped; a previously equipped role may still "
                          f"be on the member and need removing.")
                elif state == "entitled":
                    print(f"[EQUIP] Equip of {item_name!r} (role {role_id}) "
                          f"for {user_id} in {guild_id} was cancelled; the "
                          f"role is kept because another entitlement covers "
                          f"it, and the slot is unchanged.")
                else:
                    print(f"[EQUIP] Equip of {item_name!r} for {user_id} in "
                          f"{guild_id} was cancelled; the role it added was "
                          f"taken back and the slot is unchanged.")
            raise

    async def _fail_after_role_step(stage: str, cause: str, *,
                                    role_was_present: bool, clean_error: str,
                                    uncertain_error: str,
                                    force_uncertain: bool = False) -> dict:
        """Failure result for an equip that cannot be recorded after the
        Discord step, compensating for the role THIS call added.

        A role the member already wore is never touched. A role this call
        added is taken back unless the slot records it or another system
        entitles the member to it. Only when the role is confirmed off (or
        accounted for by that other entitlement) is this a clean failure;
        otherwise it is reported and logged as uncertain.
        """
        if role_was_present:
            if force_uncertain:
                # Nothing of this call's own to take back, but the previous
                # role's fate is unknown: never a clean failure.
                print(f"[EQUIP][UNCERTAIN] operation=equip stage={stage} "
                      f"guild={guild_id} "
                      f"user={user_id} item={item_name!r} role={role_id} "
                      f"entry={owned_key} - {cause}; the role this equip found "
                      f"already worn was left alone, but the previous equipped "
                      f"role may no longer be on the member. Nothing was "
                      f"retried.")
                return {"success": False, "error": uncertain_error,
                        "role_added": False, "uncertain": True}
            return {"success": False, "error": clean_error,
                    "role_added": False, "uncertain": False}
        state = await _take_back_unsaved_role(
            bot, guild_id, user_id, member, role, role_id)
        if force_uncertain:
            # The role this call added was handled, but the PREVIOUS role may be
            # gone while the slot still records it. A clean failure here would
            # hide a lost role behind a successful take-back.
            print(f"[EQUIP][UNCERTAIN] operation=equip stage={stage} "
                  f"guild={guild_id} "
                  f"user={user_id} item={item_name!r} role={role_id} "
                  f"entry={owned_key} - {cause}; the previous equipped role may "
                  f"no longer be on the member while the equip slot still "
                  f"records it. Nothing was retried."
                  + ("" if state == "removed" else
                     " The role this equip added is still on the member."))
            return {"success": False, "error": uncertain_error,
                    "role_added": state not in ("removed", "entitled"),
                    "uncertain": True}
        if state == "removed":
            return {"success": False, "error": clean_error,
                    "role_added": False, "uncertain": False}
        if state == "entitled":
            print(f"[EQUIP] Equip of {item_name!r} (role {role_id}) for "
                  f"{user_id} in {guild_id} failed at {stage} ({cause}); the "
                  f"role is kept because another entitlement covers it, and "
                  f"the slot is unchanged.")
            return {"success": False, "error": clean_error,
                    "role_added": False, "uncertain": False,
                    "role_kept_by_entitlement": True}
        _log_uncertain(
            stage, guild_id=guild_id, user_id=user_id, item_name=item_name,
            role_id=role_id, entry_key=owned_key,
            detail=(f"{cause}; the role this equip added is still on the "
                    f"member ("
                    + ("the slot names this role"
                       if state == "recorded" else
                       "it could not be removed, or the slot or other "
                       "entitlements could not be verified")
                    + "). Nothing was retried."))
        return {"success": False, "error": uncertain_error,
                "role_added": True, "uncertain": True}

    async def _add_and_record(role_was_present: bool) -> dict:
        if not role_was_present:
            try:
                await member.add_roles(role, reason=reason)
            except Exception as e:
                from utils.timed_ownership import is_ambiguous_discord_role_error
                ambiguous = is_ambiguous_discord_role_error(
                    e, member=member, role=role)
                role_now_present = role in (getattr(member, "roles", None) or ())
                uncertain = bool(ambiguous or role_now_present)
                error = f"Discord error assigning role: {e}"
                if uncertain:
                    # Discord may have applied the role although the request
                    # did not return cleanly. Not retried and not removed:
                    # the real state is unknown here, and the role may be on
                    # for a reason this call cannot see.
                    _log_uncertain(
                        "discord_add", guild_id=guild_id, user_id=user_id,
                        item_name=item_name, role_id=role_id,
                        entry_key=owned_key,
                        detail=(f"Discord did not confirm adding the role "
                                f"({e!r}); role visible on the member: "
                                f"{bool(role_now_present)}; slot unchanged. "
                                f"Nothing was retried or removed."))
                    error += (" The role may or may not have been applied - "
                              "open the item again to see its current state.")
                return {"success": False, "error": error,
                        "role_added": bool(role_now_present),
                        "uncertain": uncertain}

        # Non-role inventory writers may run while Discord is awaited. Never
        # record a slot for an entry that disappeared, expired, or changed type/role.
        try:
            live = find_entry(await get_inventory_entries(guild_id, user_id), entry_key=owned_key)
        except Exception as e:
            # Ownership could not be re-read, so the slot must not be written.
            return await _fail_after_role_step(
                "ownership_recheck", f"ownership could not be re-read ({e!r})",
                role_was_present=role_was_present,
                clean_error=("The equip could not be completed, so nothing "
                             "was changed. Please try again."),
                uncertain_error=(f"The role was assigned but the equip could "
                                 f"not be completed ({e}). An admin may need "
                                 f"to reconcile the role."))
        if (not live or live.get("item_name") != item_name or
                live.get("item_type") != owned.get("item_type") or
                (live.get("metadata") or {}).get("role_id") != (owned.get("metadata") or {}).get("role_id") or
                live.get("role_id") != owned.get("role_id")):
            return await _fail_after_role_step(
                "ownership_changed",
                "the ownership entry changed or disappeared during the "
                "Discord update",
                role_was_present=role_was_present,
                clean_error=("That item changed while it was being equipped, "
                             "so nothing was changed. Open your inventory "
                             "again."),
                uncertain_error=("The ownership entry changed during the "
                                 "Discord update; an admin may need to "
                                 "reconcile the role."))

        try:
            swap = await swap_and_record_equipped(
                bot, guild_id, user_id,
                item_name=item_name, role_id=role_id, entry_key=owned_key,
                guild=guild, member=member, reason=reason,
                role_added=not role_was_present)
        except Exception as e:
            # The slot write raised: this call's role is on the member and the
            # previous slot row is unchanged. Left like that, a later Equip of
            # a different item finds no such slot, swaps nothing out, and the
            # member wears two roles. Take back the role THIS call added (never
            # one the member already wore, never one the slot does record). If
            # that fails too, the outcome stays uncertain and is reported.
            slot_outcome_unknown = isinstance(e, _SlotWriteOutcomeUnknown)
            cause = e.cause if slot_outcome_unknown else e
            return await _fail_after_role_step(
                "slot_write", f"the slot could not be saved ({cause!r})",
                role_was_present=role_was_present,
                clean_error=("The equip could not be saved, so nothing was "
                             "changed. Please try again."),
                uncertain_error=(f"The role was assigned but the equip could "
                                 f"not be saved ({cause}) and the previous role "
                                 f"could not be reconciled, so an admin may "
                                 f"need to check both roles."),
                force_uncertain=slot_outcome_unknown)

        if isinstance(swap, dict) and swap.get("success") is False:
            # The PREVIOUS role could not be released, so the swap was refused
            # before the slot was written: the old slot is intact and the old
            # role is still on the member. Take back the role this call added
            # (never one the member already wore, never one the old slot
            # records) and report the equip as failed, with the real reason.
            reason_text = swap.get("error") or (
                "The previously equipped role could not be removed, so "
                "nothing was changed.")
            prev_uncertain = bool(swap.get("previous_role_uncertain"))
            return await _fail_after_role_step(
                "previous_role",
                f"the previously equipped role could not be released "
                f"({reason_text})",
                role_was_present=role_was_present,
                clean_error=reason_text,
                uncertain_error=(
                    f"{reason_text} An admin may need to reconcile the roles."
                    if prev_uncertain else
                    f"{reason_text} The role this equip added could not be "
                    f"taken back either; an admin may need to reconcile the "
                    f"roles."),
                force_uncertain=prev_uncertain)

        return {"success": True, "item_name": item_name,
                "role_id": role_id, "role_name": role.name,
                "entry_key": owned_key, "role_added": not role_was_present}



    unit = asyncio.ensure_future(_apply())
    try:
        return await asyncio.shield(unit)
    except asyncio.CancelledError:
        await _finish(unit)
        if not unit.cancelled():
            failure = unit.exception()
            outcome = {"success": False, "uncertain": True,
                       "error": repr(failure)} if failure else unit.result()
            if outcome.get("uncertain"):
                print(f"[EQUIP][UNCERTAIN] Equip of {item_name!r} for "
                      f"{user_id} in {guild_id}: caller cancelled and the "
                      f"outcome is uncertain: {outcome.get('error')}")
            else:
                print(f"[EQUIP] Equip of {item_name!r} for {user_id} in "
                      f"{guild_id} ran to its end after its caller was "
                      f"cancelled: "
                      f"{'equipped' if outcome.get('success') else outcome.get('error')}")
        raise


@_serialised_per_member
async def unequip_role(bot: discord.Client, guild_id: int, user_id: int,
                       item_name: str | None = None,
                       *, entry_key: str | None = None) -> dict:
    """
    Remove the currently equipped role (or the one matching item_name
    if specified) from both Discord and the equipped_roles bookkeeping
    table. Returns {success: True/False, error: ...} like equip_role
    does, so Wallet callers never need to handle exceptions.

    Scoped by item_name to mirror unequip_title: a stale button that
    says "Unequip Flame" must not clear a *different* role the member
    equipped between renders.
    """
    current = await get_equipped(guild_id, user_id)
    if not current:
        return {"success": False, "error": "You do not have a role equipped."}
    current_key = current.get("entry_key")
    if entry_key is not None:
        from utils.inventory import get_inventory_entries, find_entry
        target = find_entry(await get_inventory_entries(guild_id, user_id), entry_key=entry_key)
        try:
            target_role_id = int((target.get("metadata") or {}).get("role_id") or target.get("role_id") or 0) if target else 0
        except (TypeError, ValueError):
            target_role_id = 0
        if (not target or target.get("item_type") not in ("role", "temp_role") or
                (item_name is not None and target.get("item_name") != item_name) or
                target_role_id != int(current["role_id"])):
            return {"success": False, "error": "That role entry is no longer owned."}
    if current_key is not None:
        if entry_key is not None:
            if current_key != entry_key:
                return {"success": False,
                        "error": "That is not the currently equipped role."}
        elif item_name is not None and current["item_name"] != item_name:
            return {"success": False,
                    "error": "That is not the currently equipped role."}
    else:
        # Legacy equipped_roles row (entry_key IS NULL): represents a
        # permanent inventory_items row identified by item_name.
        if entry_key is not None:
            if not str(entry_key).startswith("i:"):
                return {"success": False,
                        "error": "That is not the currently equipped role."}
            from utils.inventory import get_inventory_entries, find_entry
            inv = await get_inventory_entries(guild_id, user_id)
            target = find_entry(inv, entry_key=entry_key)
            resolved_name = target["item_name"] if target else None
            if resolved_name != current["item_name"]:
                return {"success": False,
                        "error": "That is not the currently equipped role."}
        elif item_name is not None and current["item_name"] != item_name:
            return {"success": False,
                    "error": "That is not the currently equipped role."}

    guild = bot.get_guild(guild_id)
    if not guild:
        return {"success": False, "error": "Bot is not in that guild."}
    member = guild.get_member(user_id)
    role_id = int(current["role_id"])
    removed_name = current["item_name"]

    # ── Discord step + slot clear: one unit a cancellation cannot split ─────
    # The removal and the row delete are one task. A caller cancelled between
    # them would leave the member without the role while the slot still says
    # "equipped"; the unit runs to its end and the cancellation is re-raised
    # afterwards. If the unit ITSELF is cancelled at the Discord step (loop
    # shutdown), the outcome is logged as uncertain and never as success.
    journal = {"discord_step": "none"}

    async def _clear_row() -> int:
        # Conditional delete carrying the identity that was validated.
        # Everything above can suspend on the Discord round-trip, and a
        # legitimate equip that lands in that window owns the slot by now;
        # deleting by member alone would erase it and report the NEW item as
        # removed. The row must still be exactly the one read at the top: same
        # entry, same role, same item name.
        async with aiosqlite.connect(DB_PATH) as db:
            if current_key:
                cursor = await db.execute(
                    "DELETE FROM equipped_roles"
                    " WHERE guild_id=? AND user_id=? AND entry_key=?"
                    " AND role_id=? AND item_name=?",
                    (guild_id, user_id, current_key, role_id, removed_name))
            else:
                # Legacy row: its identity is "no entry_key, this role".
                cursor = await db.execute(
                    "DELETE FROM equipped_roles"
                    " WHERE guild_id=? AND user_id=?"
                    " AND (entry_key IS NULL OR entry_key='') AND role_id=?"
                    " AND item_name=?",
                    (guild_id, user_id, role_id, removed_name))
            cleared = int(getattr(cursor, "rowcount", 0) or 0)
            if cleared:
                from utils.inventory_activity import record_activity
                await record_activity(db, guild_id=guild_id, user_id=user_id,
                                      event_type="unequipped",
                                      item_name=removed_name,
                                      item_type="role", quantity=1,
                                      source="equipment")
            await db.commit()
            return cleared

    async def _apply() -> dict:
        try:
            resolution = await _release_role(
                bot, guild_id, user_id, current, guild=guild, member=member,
                reason="Unequipped from wallet", journal=journal)
            if resolution["action"] == "unresolved":
                return {"success": False,
                        "uncertain": bool(resolution["uncertain"]),
                        "role_kept": True,
                        "error": resolution["error"]}
            role_taken_off = (resolution["role"]
                              if resolution["action"] == "removed" else None)
            try:
                cleared = await _clear_row()
            except Exception as e:
                # Wallet callers rely on a result, not an exception. The
                # Discord role may already be off while the slot row is still
                # there: the item keeps showing as equipped, and pressing
                # Unequip again finishes the job (the role is gone by then, so
                # only the row is cleared).
                print(f"[EQUIP] Slot row for {user_id} in {guild_id} was not "
                      f"cleared after the Discord role step: {e}")
                return {"success": False, "uncertain": True,
                        "error": ("The change could not be saved. Press "
                                  "Unequip again to finish.")}

            if not cleared:
                # The slot moved on. Say so instead of claiming an equip state
                # change this call did not make; the newer slot is left exactly
                # as it is. If that newer slot wears the very role this call
                # just took off Discord, put the role back - otherwise the slot
                # it preserved would name a role the member no longer has.
                slot_read_error = None
                if role_taken_off is not None:
                    try:
                        newer = await get_equipped(guild_id, user_id)
                    except Exception as read_exc:
                        # The slot moved while the role was being removed and
                        # the new record cannot be read: whether this role is
                        # still needed is unknown, so this is not a settled
                        # failure. Nothing is restored (that role may belong to
                        # the new slot) and nothing is retried.
                        slot_read_error = read_exc
                        newer = None
                        print(f"[EQUIP][UNCERTAIN] operation=unequip "
                              f"guild={guild_id} user={user_id} "
                              f"item={removed_name!r} role={role_id} "
                              f"entry={entry_key} - the slot changed while the "
                              f"role was removed and could not be read back "
                              f"({read_exc!r}). Whether the role is still "
                              f"needed is unknown. Nothing was retried.")
                    if newer is not None and int(newer["role_id"]) == role_id:
                        try:
                            await member.add_roles(
                                role_taken_off,
                                reason=("Equipped role changed during "
                                        "unequip - restored"))
                        except Exception as e:
                            print(f"[EQUIP][UNCERTAIN] Role {role_id} was "
                                  f"removed from {user_id} in {guild_id} while "
                                  f"the slot changed to an item that wears it, "
                                  f"and could not be restored: {e}")
                            return {"success": False, "uncertain": True,
                                    "error": ("The equipped role changed "
                                              "while it was being unequipped "
                                              "and its Discord role could not "
                                              "be restored. Equip the item "
                                              "again to fix it.")}
                if slot_read_error is not None:
                    return {"success": False, "uncertain": True,
                            "role_kept": False,
                            "error": ("The equipped role changed while it was "
                                      "being unequipped and the current record "
                                      "could not be read, so it is not known "
                                      "whether this role is still needed. "
                                      "Nothing was retried; press Unequip "
                                      "again to check.")}
                return {"success": False,
                        "error": ("The equipped role changed while it was "
                                  "being unequipped; nothing was cleared.")}

            return {"success": True, "item_name": removed_name,
                    "role_id": role_id,
                    "role_removed": role_taken_off is not None,
                    "role_kept": resolution["action"] == "kept"}
        except BaseException:
            # Cancelled (or shutting down) somewhere in this unit. If the
            # Discord step had started, the member may have changed while the
            # slot still records the role; say so instead of letting the
            # cancellation look like a clean no-op.
            if journal["discord_step"] != "none":
                print(f"[EQUIP][UNCERTAIN] operation=unequip guild={guild_id} "
                      f"user={user_id} item={removed_name!r} role={role_id} - "
                      f"interrupted at the Discord step "
                      f"({journal['discord_step']}); the member may no longer "
                      f"wear the role while the slot still records it. "
                      f"Nothing was retried.")
            raise

    unit = asyncio.ensure_future(_apply())
    try:
        return await asyncio.shield(unit)
    except asyncio.CancelledError:
        await _finish(unit)
        if not unit.cancelled():
            failure = unit.exception()
            outcome = ({"success": False, "uncertain": True,
                        "error": repr(failure)} if failure else unit.result())
            if outcome.get("uncertain"):
                print(f"[EQUIP][UNCERTAIN] Unequip of {removed_name!r} (role "
                      f"{role_id}) for {user_id} in {guild_id}: caller "
                      f"cancelled and the outcome is uncertain: "
                      f"{outcome.get('error')}")
            else:
                print(f"[EQUIP] Unequip of {removed_name!r} for {user_id} in "
                      f"{guild_id} ran to its end after its caller was "
                      f"cancelled: "
                      f"{'unequipped' if outcome.get('success') else outcome.get('error')}")
        raise
    except Exception as unexpected:
        # Backstop. Wallet callers rely on a result, not an exception, and the
        # Discord role may already be off: never report a clean failure.
        print(f"[EQUIP][UNCERTAIN] operation=unequip guild={guild_id} "
              f"user={user_id} item={removed_name!r} role={role_id} - "
              f"unexpected error {unexpected!r}; the member may or may not "
              f"still wear the role and the slot was not confirmed cleared. "
              f"Nothing was retried.")
        return {"success": False, "uncertain": True,
                "error": "The unequip could not be completed. Please try again."}


async def cleanup_expired_role_item(guild_id: int, user_id: int, role_id: int):
    """
    Called by cogs/shop.py's temp_role_cleanup right after it has
    successfully removed an expired temp role's underlying Discord
    role. Finds the matching inventory_items row (item_type=
    'temp_role', metadata.role_id == role_id) and zeroes it out, and
    clears equipped_roles if this was the equipped one — since the
    Discord role is already gone by the time this runs, there's
    nothing left to "unequip" on Discord, only bookkeeping to catch
    up so /inventory and the future rank card stop showing an item
    the member no longer has.
    """
    from utils.inventory import get_inventory, remove_item, InsufficientItems
    from utils.timed_ownership import is_expired

    items = await get_inventory(guild_id, user_id, include_empty=False)
    match = next(
        (it for it in items
         # Only legacy permanent-table rows: a timed holding is owned and
         # retired by utils.timed_ownership, and remove_item() could not
         # affect it anyway. Matching one here used to be impossible (the
         # table did not exist) and must stay impossible.
         if not it.get("timed")
         and it["item_type"] == "temp_role"
         and (it.get("metadata") or {}).get("role_id") == role_id
         and (
             (it.get("metadata") or {}).get("expires_at") is None
             or is_expired((it.get("metadata") or {}).get("expires_at"))
         )),
        None)
    if not match:
        return

    match_key = f"i:{match['id']}"
    try:
        await remove_item(guild_id, user_id, match["item_name"],
                          quantity=match["quantity"], expected_id=match["id"],
                          expected_type="temp_role", expected_metadata=match.get("metadata") or {})
    except InsufficientItems:
        return

    current = await get_equipped(guild_id, user_id)
    if current and current["role_id"] == role_id:
        cur_key = current.get("entry_key")
        if cur_key is None or cur_key == match_key:
            # Conditional delete: the check above reads a snapshot, and the slot
            # can be replaced between that read and this write (a purchase, a
            # swap, another cleanup). Deleting by member alone would erase the
            # newer entry; matching the identity we verified leaves it alone.
            async with aiosqlite.connect(DB_PATH) as db:
                if cur_key is None:
                    await db.execute(
                        "DELETE FROM equipped_roles"
                        " WHERE guild_id=? AND user_id=?"
                        " AND (entry_key IS NULL OR entry_key='') AND role_id=?",
                        (guild_id, user_id, int(role_id)))
                else:
                    await db.execute(
                        "DELETE FROM equipped_roles"
                        " WHERE guild_id=? AND user_id=? AND entry_key=?",
                        (guild_id, user_id, cur_key))
                await db.commit()
