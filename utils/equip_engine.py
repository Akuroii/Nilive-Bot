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


async def get_equipped(guild_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT item_name, role_id, equipped_at
            FROM equipped_roles WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))
        row = await cursor.fetchone()
    if not row:
        return None
    return {"item_name": row[0], "role_id": row[1], "equipped_at": row[2]}


async def equip_role(bot: discord.Client, guild_id: int, user_id: int,
                      item_name: str, reason: str = "Equipped") -> dict:
    """
    Swaps the member's single equipped role to item_name — an
    inventory_items row the member must already own, with item_type
    'role' or 'temp_role'. Never raises; every failure mode returns
    {"success": False, "error": "..."}.
    """
    from utils.inventory import get_inventory

    items = await get_inventory(guild_id, user_id, include_empty=False)
    owned = next((it for it in items if it["item_name"] == item_name), None)
    if not owned:
        return {"success": False, "error": f"You don't own **{item_name}**."}
    if owned["item_type"] not in ("role", "temp_role"):
        return {"success": False,
                "error": f"**{item_name}** isn't an equippable role item."}

    meta = owned.get("metadata") or {}
    role_id = meta.get("role_id")
    if not role_id:
        return {"success": False,
                "error": (f"**{item_name}** has no role attached — "
                          f"ask an admin to check its setup.")}
    role_id = int(role_id)

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

    current = await get_equipped(guild_id, user_id)
    if current and current["item_name"] != item_name:
        old_role = guild.get_role(current["role_id"])
        if old_role and old_role in member.roles:
            try:
                await member.remove_roles(
                    old_role, reason=f"{reason} — swapped out")
            except Exception as e:
                print(f"[EQUIP] Failed to remove previous role "
                      f"{current['role_id']} from {user_id} in "
                      f"{guild_id}: {e}")

    if role not in member.roles:
        try:
            await member.add_roles(role, reason=reason)
        except Exception as e:
            return {"success": False, "error": f"Discord error assigning role: {e}"}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO equipped_roles
                (guild_id, user_id, item_name, role_id, equipped_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                item_name   = excluded.item_name,
                role_id     = excluded.role_id,
                equipped_at = CURRENT_TIMESTAMP
        """, (guild_id, user_id, item_name, role_id))
        await db.commit()

    return {"success": True, "item_name": item_name,
            "role_id": role_id, "role_name": role.name}


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

    items = await get_inventory(guild_id, user_id, include_empty=False)
    match = next(
        (it for it in items
         if it["item_type"] == "temp_role"
         and (it.get("metadata") or {}).get("role_id") == role_id),
        None)
    if not match:
        return

    try:
        await remove_item(guild_id, user_id, match["item_name"],
                          quantity=match["quantity"])
    except InsufficientItems:
        pass

    current = await get_equipped(guild_id, user_id)
    if current and current["role_id"] == role_id:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM equipped_roles WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            await db.commit()
