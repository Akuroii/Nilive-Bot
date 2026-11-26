"""
One answer to "may this guild define an item with this name?".

A member's permanent Inventory holds one stack per item NAME
(inventory_items is UNIQUE on guild, user, item_name), and a grant never
changes what an owned stack is (utils.inventory.give_item refuses instead).
So two definitions in one guild that share a name but are different KINDS
of item cannot both be delivered to the same member. This module refuses
such a definition when an administrator creates or renames it, which is
where the mistake is cheap to fix.

Kinds
  potion  - activated            (Shop potion)
  title   - equipped as a title  (Shop title)
  role    - equipped as a role   (Shop role / temp_role / color)
  plain   - no action            (Shop custom, Mission / Event / Minigame
                                  item rewards)
Same kind may share a name (they stack). Plain items from different sources
may share a name (they stack). Anything else is a conflict.

Scope and naming: per guild, and by the exact stored name - the same
comparison the Inventory itself uses. Level rewards are Shop products (they
reference shop_items by id), so the Shop check covers them. Role rewards of
Missions / Events / Minigames are named after the Discord role at delivery
time, which a definition cannot know; those are covered at delivery instead
(utils.pending_rewards).
"""
import json

import aiosqlite

from database import DB_PATH
from utils.inventory import FUNCTIONAL_ITEM_TYPES, _metadata_role_id

KIND_POTION = "potion"
KIND_TITLE = "title"
KIND_ROLE = "role"
KIND_PLAIN = "plain"

_KIND_LABEL = {
    KIND_POTION: "potion",
    KIND_TITLE: "title",
    KIND_ROLE: "role item",
    KIND_PLAIN: "plain item",
}


class ItemNameConflict(ValueError):
    """A definition would share its name with a different kind of item."""


def _normalize_positive_role_id(role_id) -> int | None:
    if role_id in (None, "", 0, "0") or isinstance(role_id, bool):
        return None
    try:
        parsed = int(role_id)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def kind_of_inventory_type(item_type: str | None, *,
                           metadata=None, role_id=None) -> str:
    item_type = item_type or "custom"
    if item_type in ("role", "temp_role"):
        return KIND_ROLE
    if item_type in FUNCTIONAL_ITEM_TYPES:
        return item_type
    resolved_rid = _normalize_positive_role_id(role_id)
    if resolved_rid is None:
        resolved_rid = _metadata_role_id(metadata)
    if resolved_rid is not None:
        return KIND_ROLE
    return KIND_PLAIN


def kind_of_shop_type(shop_type: str | None, *, role_id=None) -> str | None:
    """Kind a Shop product becomes in the Inventory; None if it never does.

    Prestige is a rank and a legacy xp_boost is applied at purchase: neither
    is ever an Inventory item, so neither can collide. A `custom` Shop Item
    with a `role_id` becomes an equippable role item (`KIND_ROLE`); without a
    `role_id` it is a plain collectible (`KIND_PLAIN`).
    """
    shop_type = (shop_type or "").strip().lower()
    if shop_type in ("prestige", "xp_boost"):
        return None
    if shop_type in ("role", "temp_role", "color"):
        return KIND_ROLE
    if shop_type in (KIND_POTION, KIND_TITLE):
        return shop_type
    if shop_type in ("custom", "shop_custom", "item") and _normalize_positive_role_id(role_id) is not None:
        return KIND_ROLE
    return KIND_PLAIN


def kinds_conflict(first: str | None, second: str | None) -> bool:
    return bool(first and second and first != second)


def _json_item_names(text) -> set[str]:
    """Item-reward names in a stored minigame default-reward list."""
    try:
        rows = json.loads(text) if text else []
    except (TypeError, ValueError):
        return set()
    names = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if (row.get("reward_type") or row.get("type")) != "item":
            continue
        value = row.get("reward_value", row.get("value"))
        if value is not None:
            names.add(str(value))
    return names


async def _query(db, sql: str, args: tuple) -> list:
    """Rows, or none when the table does not exist yet.

    Mission and Minigame tables are created by their own modules on the bot's
    first start, so a fresh database (or the dashboard alone) may lack them.
    A source with no table defines nothing.

    A missing COLUMN is not the same thing: the table exists and its schema is
    older than this code, so the definitions could not be read at all. Treating
    that as "nothing is defined" would silently let a conflicting name through,
    so it is raised as the schema error it is.
    """
    try:
        cursor = await db.execute(sql, args)
        return await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


async def _definitions(db, guild_id: int, name: str, *,
                       exclude_shop_item_id: int | None) -> list[tuple[str, str]]:
    """(kind, where) for everything in this guild already using `name`."""
    found: list[tuple[str, str]] = []

    for item_id, shop_type, shop_role_id in await _query(
            db, "SELECT id, type, role_id FROM shop_items WHERE guild_id=? AND name=?",
            (guild_id, name)):
        if exclude_shop_item_id is not None and int(item_id) == int(exclude_shop_item_id):
            continue
        kind = kind_of_shop_type(shop_type, role_id=shop_role_id)
        if kind:
            found.append((kind, f"Shop product #{item_id}"))

    for mission_id, mission_name in await _query(
            db, "SELECT id, name FROM missions_definitions "
                "WHERE guild_id=? AND reward_type='item' AND reward_value=?",
            (guild_id, name)):
        found.append((KIND_PLAIN, f"reward of mission “{mission_name}” (#{mission_id})"))

    # A finished event is disabled and can never grant again.
    for event_id, title in await _query(
            db, "SELECT id, title FROM events WHERE guild_id=? AND enabled=1 "
                "AND reward_type='item' AND reward_value=?", (guild_id, name)):
        found.append((KIND_PLAIN, f"reward of event “{title}” (#{event_id})"))

    for template_id, template_name in await _query(
            db, "SELECT t.id, t.name FROM minigame_rewards r "
                "JOIN minigame_templates t ON t.id = r.template_id "
                "WHERE t.guild_id=? AND r.reward_type='item' AND r.reward_value=?",
            (guild_id, name)):
        found.append((KIND_PLAIN,
                      f"reward of minigame “{template_name}” (#{template_id})"))
    for category_id, category_name, text in await _query(
            db, "SELECT id, name, default_rewards_json FROM minigame_categories "
                "WHERE guild_id=?", (guild_id,)):
        if name in _json_item_names(text):
            found.append((KIND_PLAIN, "default reward of minigame category "
                                      f"“{category_name}” (#{category_id})"))
    for (text,) in await _query(
            db, "SELECT global_default_rewards_json FROM minigames_config "
                "WHERE guild_id=?", (guild_id,)):
        if name in _json_item_names(text):
            found.append((KIND_PLAIN, "the minigame default rewards"))

    # What members already hold is the collision itself, whatever defined it
    # (a renamed or deleted listing, an old event).
    owned_by_kind: dict[str, int] = {}
    for item_type, metadata in await _query(
            db, "SELECT item_type, metadata FROM inventory_items "
                "WHERE guild_id=? AND item_name=? AND quantity > 0",
            (guild_id, name)):
        k = kind_of_inventory_type(item_type, metadata=metadata)
        owned_by_kind[k] = owned_by_kind.get(k, 0) + 1
    for k, members in owned_by_kind.items():
        found.append((k, f"an item {members} member(s) already own"))

    from utils.timed_ownership import RETENTION_ITEM_TYPE, is_expired
    timed_by_kind: dict[str, int] = {}
    for item_type, h_role_id, h_meta, expires_at in await _query(
            db, "SELECT item_type, role_id, metadata, expires_at "
                "FROM timed_holdings WHERE guild_id=? AND item_name=? "
                "AND item_type != ?",
            (guild_id, name, RETENTION_ITEM_TYPE)):
        if is_expired(expires_at):
            continue
        k = kind_of_inventory_type(item_type, metadata=h_meta, role_id=h_role_id)
        timed_by_kind[k] = timed_by_kind.get(k, 0) + 1
    for k, holdings in timed_by_kind.items():
        found.append((k, f"an active timed item ({holdings} holding(s))"))
    return found


async def find_name_conflict(guild_id: int, name: str, kind: str | None, *,
                             role_id=None,
                             shop_type: str | None = None,
                             exclude_shop_item_id: int | None = None,
                             db: aiosqlite.Connection | None = None) -> str | None:
    """Admin-facing reason this name cannot be used for `kind` / `role_id`, or None."""
    name = str(name or "").strip()
    if not name or not kind:
        return None
    normalized_rid = _normalize_positive_role_id(role_id)
    norm_shop_type = (shop_type or "").strip().lower()
    is_custom_role = norm_shop_type in ("custom", "shop_custom", "item")

    async def check(connection):
        for other_kind, where in await _definitions(
                connection, guild_id, name,
                exclude_shop_item_id=exclude_shop_item_id):
            if kinds_conflict(kind, other_kind):
                return (f"“{name}” is already the name of a "
                        f"{_KIND_LABEL[other_kind]} ({where}). A "
                        f"{_KIND_LABEL[kind]} cannot share a name with a "
                        f"different kind of item, because a member could not "
                        f"hold both. Use a different name.")

        if kind == KIND_ROLE and normalized_rid is not None:
            for other_id, other_type, other_rid_raw in await _query(
                connection,
                "SELECT id, type, role_id FROM shop_items WHERE guild_id=? AND name=?",
                (guild_id, name),
            ):
                if exclude_shop_item_id is not None and int(other_id) == int(exclude_shop_item_id):
                    continue
                if kind_of_shop_type(other_type, role_id=other_rid_raw) != KIND_ROLE:
                    continue
                other_is_custom = (other_type or "").strip().lower() in (
                    "custom", "shop_custom", "item",
                )
                if norm_shop_type and is_custom_role != other_is_custom:
                    return (
                        f"“{name}” is already used by Shop product #{other_id} "
                        f"with a different role-product type. A manual-equip "
                        f"Item and an auto-equip Color/Role cannot share a name. "
                        f"Use a different name."
                    )
                other_rid = _normalize_positive_role_id(other_rid_raw)
                if other_rid != normalized_rid:
                    return (
                        f"“{name}” is already linked to a different Role ID in "
                        f"Shop product #{other_id}. Same-name role items must "
                        f"use the same Role ID, or use a different name."
                    )

            for inv_type, raw_meta in await _query(
                connection,
                "SELECT item_type, metadata FROM inventory_items "
                "WHERE guild_id=? AND item_name=? AND quantity > 0",
                (guild_id, name),
            ):
                if kind_of_inventory_type(inv_type, metadata=raw_meta) != KIND_ROLE:
                    continue
                if is_custom_role and (inv_type or "") in ("role", "temp_role"):
                    return (
                        f"“{name}” is already owned by member(s) as a Color/Role "
                        f"product and cannot stack with a role-linked Item. Use "
                        f"a different name."
                    )
                if norm_shop_type and not is_custom_role and (inv_type or "") not in ("role", "temp_role"):
                    return (
                        f"“{name}” is already owned by member(s) as a role-linked "
                        f"Item and cannot share a name with a Color/Role product. "
                        f"Use a different name."
                    )
                owned_rid = _metadata_role_id(raw_meta)
                if owned_rid != normalized_rid:
                    return (
                        f"“{name}” is already owned by member(s) with a different "
                        f"role setup. Use a different name instead of binding it "
                        f"to another Role ID."
                    )

            from utils.timed_ownership import RETENTION_ITEM_TYPE, is_expired
            for th_type, th_rid_raw, th_meta, expires_at in await _query(
                connection,
                "SELECT item_type, role_id, metadata, expires_at "
                "FROM timed_holdings WHERE guild_id=? AND item_name=? "
                "AND item_type != ?",
                (guild_id, name, RETENTION_ITEM_TYPE),
            ):
                if is_expired(expires_at):
                    continue
                if kind_of_inventory_type(th_type, metadata=th_meta, role_id=th_rid_raw) != KIND_ROLE:
                    continue
                if is_custom_role and (th_type or "") in ("role", "temp_role"):
                    return (
                        f"“{name}” is currently held as an active timed Color/Role "
                        f"and cannot share a name with a role-linked Item. Use a "
                        f"different name."
                    )
                if norm_shop_type and not is_custom_role and (th_type or "") not in ("role", "temp_role"):
                    return (
                        f"“{name}” is currently held as an active timed role-linked "
                        f"Item and cannot share a name with a Color/Role product. "
                        f"Use a different name."
                    )
                live_rid = (
                    _normalize_positive_role_id(th_rid_raw)
                    or _metadata_role_id(th_meta)
                )
                if live_rid != normalized_rid:
                    return (
                        f"“{name}” is currently held as an active timed item with "
                        f"a different Role ID. Wait until active holdings expire "
                        f"or use a different name."
                    )
        return None

    if db is not None:
        return await check(db)
    async with aiosqlite.connect(DB_PATH) as connection:
        return await check(connection)


async def ensure_name_available(guild_id: int, name: str, kind: str | None, *,
                                exclude_shop_item_id: int | None = None) -> None:
    """Raise ItemNameConflict (a ValueError) when the name is taken by another kind."""
    reason = await find_name_conflict(
        guild_id, name, kind, exclude_shop_item_id=exclude_shop_item_id)
    if reason:
        raise ItemNameConflict(reason)


async def ensure_item_rewards_available(guild_id: int, rows) -> None:
    """Check every `item` row of a Mission / Event / Minigame reward list."""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if (row.get("reward_type") or row.get("type")) != "item":
            continue
        value = row.get("reward_value", row.get("value"))
        if value is not None and str(value).strip():
            await ensure_name_available(guild_id, str(value), KIND_PLAIN)
