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
from utils.inventory import FUNCTIONAL_ITEM_TYPES

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


def kind_of_inventory_type(item_type: str | None) -> str:
    item_type = item_type or "custom"
    if item_type in ("role", "temp_role"):
        return KIND_ROLE
    if item_type in FUNCTIONAL_ITEM_TYPES:
        return item_type
    return KIND_PLAIN


def kind_of_shop_type(shop_type: str | None) -> str | None:
    """Kind a Shop product becomes in the Inventory; None if it never does.

    Prestige is a rank and a legacy xp_boost is applied at purchase: neither
    is ever an Inventory item, so neither can collide.
    """
    shop_type = (shop_type or "").strip().lower()
    if shop_type in ("prestige", "xp_boost"):
        return None
    if shop_type in ("role", "temp_role", "color"):
        return KIND_ROLE
    if shop_type in (KIND_POTION, KIND_TITLE):
        return shop_type
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
    """
    try:
        cursor = await db.execute(sql, args)
        return await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise


async def _definitions(db, guild_id: int, name: str, *,
                       exclude_shop_item_id: int | None) -> list[tuple[str, str]]:
    """(kind, where) for everything in this guild already using `name`."""
    found: list[tuple[str, str]] = []

    for item_id, shop_type in await _query(
            db, "SELECT id, type FROM shop_items WHERE guild_id=? AND name=?",
            (guild_id, name)):
        if exclude_shop_item_id is not None and int(item_id) == int(exclude_shop_item_id):
            continue
        kind = kind_of_shop_type(shop_type)
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
    for item_type, members in await _query(
            db, "SELECT item_type, COUNT(*) FROM inventory_items "
                "WHERE guild_id=? AND item_name=? AND quantity > 0 "
                "GROUP BY item_type", (guild_id, name)):
        found.append((kind_of_inventory_type(item_type),
                      f"an item {members} member(s) already own"))
    return found


async def find_name_conflict(guild_id: int, name: str, kind: str | None, *,
                             exclude_shop_item_id: int | None = None,
                             db: aiosqlite.Connection | None = None) -> str | None:
    """Admin-facing reason this name cannot be used for `kind`, or None."""
    name = str(name or "").strip()
    if not name or not kind:
        return None

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
