import aiosqlite
from datetime import datetime, timezone
from database import DB_PATH

# ═══════════════════════════════════════════════════════════════════════
# POTION ENGINE — Wallet pass
#
# A Potion is a CONSUMABLE inventory item: it is never equipped, it is
# used, and using it decrements the stack by one and applies an effect.
#
# Deliberately NOT a new effects framework. The project already has
# exactly one timed-effect mechanism — leveling_active_boosts, granted
# by utils.xp_calculator.grant_xp_boost() and read on every message by
# calculate_message_xp() — and the whole point of a potion is "a timed
# effect you can hold in your bag and trigger later". So a potion is
# defined as: the existing xp_boost effect, deferred.
#
# The difference from the existing shop `xp_boost` item type is purely
# WHEN the effect fires. An xp_boost item is consumed at the moment of
# purchase (cogs/shop.py calls grant_xp_boost inline and nothing ever
# reaches the buyer's inventory). A potion item is delivered into
# inventory_items instead, carrying its effect parameters in the row's
# existing `metadata` JSON column, and grant_xp_boost() only runs when
# the member clicks Use in their Wallet. No schema change was needed for
# that: inventory_items.metadata already exists and is already used the
# same way for role items ({"role_id":...}).
#
# EFFECT_XP_BOOST is the only effect kind implemented, on purpose. The
# metadata carries an explicit "effect" discriminator so a second kind
# can be added later without a migration or a re-read of every existing
# potion row — unknown kinds are rejected with a clear message rather
# than silently consuming the item.
# ═══════════════════════════════════════════════════════════════════════

POTION_ITEM_TYPE = "potion"
EFFECT_XP_BOOST = "xp_boost"
SUPPORTED_EFFECTS = (EFFECT_XP_BOOST,)


class PotionError(Exception):
    pass


def build_metadata(effect: str, multiplier: float,
                   duration_hours: int) -> dict:
    """Shape written onto inventory_items.metadata when a potion is granted."""
    return {
        "effect": effect,
        "multiplier": float(multiplier),
        "duration_hours": int(duration_hours),
    }


def describe(metadata: dict | None) -> str:
    """Human-readable one-liner for a potion's effect, for embeds."""
    meta = metadata or {}
    if meta.get("effect") == EFFECT_XP_BOOST:
        mult = meta.get("multiplier")
        hours = meta.get("duration_hours")
        if mult and hours:
            return f"{mult:g}× XP for {hours}h"
        if mult:
            return f"{mult:g}× XP"
    return "Unknown effect — ask an admin to check this item's setup."


def is_usable(metadata: dict | None) -> bool:
    meta = metadata or {}
    if meta.get("effect") not in SUPPORTED_EFFECTS:
        return False
    try:
        return (float(meta.get("multiplier") or 0) > 1.0
                and int(meta.get("duration_hours") or 0) > 0)
    except (TypeError, ValueError):
        return False


async def use_potion(guild_id: int, user_id: int, item_name: str) -> dict:
    """
    Consumes one copy of a potion and applies its effect.

    Order matters: the stack is decremented FIRST (remove_item runs
    inside its own BEGIN IMMEDIATE, so two concurrent Use clicks on the
    last potion can't both pass the ownership check), and only then is
    the effect granted. A double-click therefore burns at most the
    quantity actually owned. If the effect grant itself fails after the
    decrement, the copy is refunded so the member is never charged for
    nothing — that refund is the only place this module writes back to
    inventory.

    Never raises for user-facing failures; returns {"success": False,
    "error": ...} like equip_engine/title_engine do.
    """
    from utils.inventory import (
        get_inventory, remove_item, give_item, InsufficientItems,
    )

    items = await get_inventory(guild_id, user_id, include_empty=False)
    owned = next((it for it in items if it["item_name"] == item_name), None)
    if not owned:
        return {"success": False, "error": f"You don't own **{item_name}**."}
    if owned["item_type"] != POTION_ITEM_TYPE:
        return {"success": False,
                "error": f"**{item_name}** isn't a potion."}

    meta = owned.get("metadata") or {}
    if not is_usable(meta):
        return {"success": False,
                "error": (f"**{item_name}** has no valid effect configured "
                          f"— ask an admin to check its setup.")}

    try:
        remaining = await remove_item(guild_id, user_id, item_name, quantity=1)
    except InsufficientItems:
        return {"success": False,
                "error": f"You don't have any **{item_name}** left."}

    multiplier = float(meta["multiplier"])
    duration_hours = int(meta["duration_hours"])

    from utils.xp_calculator import grant_xp_boost
    try:
        expires_at = await grant_xp_boost(
            guild_id, user_id, multiplier, duration_hours, source="potion")
    except Exception as e:
        # Refund the consumed copy — the member clicked Use and got
        # nothing. give_item's upsert restores the exact stack size.
        try:
            await give_item(
                guild_id, user_id, item_name, quantity=1,
                item_type=POTION_ITEM_TYPE, metadata=meta, source="refund")
        except Exception as refund_error:
            print(f"[POTION] Effect grant AND refund both failed "
                  f"(guild={guild_id} user={user_id} item={item_name}): "
                  f"{e} / {refund_error}")
        return {"success": False,
                "error": f"Couldn't apply the effect ({e}). Nothing was used."}

    return {
        "success": True,
        "item_name": item_name,
        "effect": meta["effect"],
        "multiplier": multiplier,
        "duration_hours": duration_hours,
        "expires_at": expires_at,
        "remaining": remaining,
    }


async def get_active_effects(guild_id: int, user_id: int) -> list[dict]:
    """
    Currently-running potion/boost effects, newest expiry last. Reads
    leveling_active_boosts directly (the same table
    xp_calculator.get_active_boost_multiplier aggregates) because the
    Wallet wants to list each effect and when it ends, not just the
    combined multiplier that XP math needs.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT multiplier, expires_at, source
            FROM leveling_active_boosts
            WHERE guild_id=? AND user_id=? AND expires_at > ?
            ORDER BY expires_at ASC
        """, (guild_id, user_id, now))
        rows = await cursor.fetchall()
    return [{"multiplier": r[0], "expires_at": r[1], "source": r[2]}
            for r in rows]
