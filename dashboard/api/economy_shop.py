import os
import json
import csv
import io
import datetime
import requests as _req
import aiosqlite
from flask import jsonify, request, session, abort, Response
from markupsafe import escape as _esc
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.auth import login_required, current_user_id, current_user
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission,
    LEVEL_OWNER, LEVEL_ADMIN, LEVEL_MODERATOR,
)
from dashboard.api import api_bp

# ── Economy / Shop ────────────────────────────────────────────────────────────

# dark-fixes pass #18 (username resolver rollout, task #3 of 6): the two
# htmx-loaded leaderboard partials below previously rendered raw
# `<code>{{user_id}}</code>` — no snapshot table backs Economy (unlike
# moderation_logs/purchase_history/etc), so a live resolve is correct
# here. Both partials batch-resolve every ID on the page in ONE
# resolve_users() call, not per-row, then render through the same
# dashboard/utils/user_identity.py helper used everywhere else the
# "Username (big) / User ID (small)" pattern appears.


@api_bp.route("/economy/leaderboard")
@require_api_permission(LEVEL_ADMIN)
def economy_leaderboard_partial():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, balance FROM economy
                WHERE guild_id = ? ORDER BY balance DESC LIMIT 50
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        return await resolve_users(guild_id, [r[0] for r in rows])

    user_map = run_async(resolve()) if rows else {}

    from dashboard.utils.user_identity import render_user_identity_html
    from dashboard.utils.currency_ctx import resolved as _currency, icon_html
    _cc = _currency(guild_id)["coins"]
    # A configured custom emoji is Discord message markup, not HTML, so it
    # is rendered as a CDN <img> (see currency_ctx.icon_html) — dropped
    # straight into this f-string it would be swallowed as an <a> tag.
    _cc_icon = icon_html(_cc["emoji"])
    html = ""
    for i, r in enumerate(rows, 1):
        u = user_map.get(r[0], {})
        identity_html = render_user_identity_html(
            r[0], u.get("display_name"), u.get("username"), u.get("avatar_url"))
        html += (
            f"<tr><td>#{i}</td>"
            f"<td>{identity_html}</td>"
            f"<td><strong>{_cc_icon} {r[1]:,}</strong></td></tr>"
        )
    return html or "<tr><td colspan='3' class='empty'>No data yet</td></tr>"


@api_bp.route("/economy/leaderboard-diamonds")
@require_api_permission(LEVEL_ADMIN)
def economy_leaderboard_diamonds_partial():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, diamonds FROM economy
                WHERE guild_id = ? AND diamonds > 0
                ORDER BY diamonds DESC LIMIT 50
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        return await resolve_users(guild_id, [r[0] for r in rows])

    user_map = run_async(resolve()) if rows else {}

    from dashboard.utils.user_identity import render_user_identity_html
    from dashboard.utils.currency_ctx import resolved as _currency, icon_html
    _cd = _currency(guild_id)["diamonds"]
    _cd_icon = icon_html(_cd["emoji"])
    html = ""
    for i, r in enumerate(rows, 1):
        u = user_map.get(r[0], {})
        identity_html = render_user_identity_html(
            r[0], u.get("display_name"), u.get("username"), u.get("avatar_url"))
        html += (
            f"<tr><td>#{i}</td>"
            f"<td>{identity_html}</td>"
            f"<td><strong>{_cd_icon} {r[1]:,}</strong></td></tr>"
        )
    return html or f"<tr><td colspan='3' class='empty'>No {_cd['name']} held yet</td></tr>"


# ── Currency display configuration ─────────────────────────────────────
# Economy owns the currency configuration; the rest of the project consumes
# it via utils/currency.py. These two routes are the ONLY way the four
# display columns are written from any UI — the General Settings form used
# to write them too, and that is exactly what let the two surfaces drift.
#
# Both fields per currency are independent and optional: leaving a name
# blank keeps (or restores) the default name, leaving the emoji blank keeps
# the default emoji. Nothing here requires both.

def _known_emojis_for_currency_form(guild_id: int) -> list[dict]:
    """Every custom emoji the bot can name, so a pasted BARE ID can be
    normalised into a proper `<:name:id>` token instead of the anonymous
    `<:_:id>` placeholder.

    Sources, cheapest first, and all of them already existed:
      * the bot's own application emojis (utils/app_emoji_cache — these
        render in EVERY guild, which is the only place a currency icon is
        guaranteed to work),
      * the current guild's emoji list (the same Discord endpoint
        /api/guild/emojis already uses for the Embed Builder picker).

    Best-effort by design: any failure just means a pasted bare ID keeps
    the placeholder name, which still renders. Nothing here may block a
    save.
    """
    known: list[dict] = []
    try:
        from utils import app_emoji_cache

        async def _app():
            await app_emoji_cache.ensure_table()
            return await app_emoji_cache.list_all()

        # Already {id, name, animated} — the exact shape this helper wants.
        known.extend(run_async(_app()) or [])
    except Exception:
        pass

    try:
        token = os.getenv("DISCORD_TOKEN", "")
        if token:
            resp = _req.get(
                f"https://discord.com/api/v10/guilds/{guild_id}/emojis",
                headers={"Authorization": f"Bot {token}"}, timeout=8)
            if resp.status_code == 200:
                for e in resp.json():
                    if e.get("id") and e.get("name"):
                        known.append({"id": str(e["id"]), "name": e["name"],
                                      "animated": bool(e.get("animated"))})
    except Exception:
        pass

    return [k for k in known if k["id"]]


@api_bp.route("/economy/currency", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_currency_config_api():
    """Prefill payload for the Economy → Currency tab.

    Returns the STORED values (`stored`, blank when the owner has never set
    a field, so the "leave blank for the default" affordance is truthful)
    alongside the RESOLVED values (`resolved`, what is actually rendered
    everywhere today) and the defaults, so the tab can show a real preview
    without client-side guessing.
    """
    guild_id = get_session_guild_id()

    async def fetch():
        from utils.currency import (
            get_currency_config, get_currency_config_raw,
            DEFAULT_COIN_NAME, DEFAULT_COIN_EMOJI,
            DEFAULT_DIAMOND_NAME, DEFAULT_DIAMOND_EMOJI,
        )
        return {
            "stored": await get_currency_config_raw(guild_id),
            "resolved": await get_currency_config(guild_id),
            "defaults": {
                "coin_name": DEFAULT_COIN_NAME,
                "coin_emoji": DEFAULT_COIN_EMOJI,
                "diamond_name": DEFAULT_DIAMOND_NAME,
                "diamond_emoji": DEFAULT_DIAMOND_EMOJI,
            },
        }

    return jsonify(run_async(fetch()))


@api_bp.route("/economy/currency", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_currency_config_api():
    guild_id = get_session_guild_id()
    data = request.json or {}

    from utils.currency import set_currency_config
    from utils.emoji import normalize_currency_emoji

    known = _known_emojis_for_currency_form(guild_id)

    # An absent key means "leave this field alone"; a present-but-blank key
    # means "revert to the default". that distinction is what lets the form
    # submit a partial payload without blanking fields it did not render.
    #
    # Emoji normalisation applies to the EMOJI fields only. A name is free
    # text — routing it through normalize_currency_emoji() turned a purely
    # numeric name ("2024") into the Discord token `<:_:2024>`, because a
    # bare digit string is exactly the form that helper resolves as an
    # emoji ID. Names are stored as typed.
    def field(key):
        if key not in data:
            return None
        if key in ("coin_emoji_id", "diamond_emoji_id"):
            return normalize_currency_emoji(data.get(key), known)
        return str(data.get(key) or "").strip()

    async def save():
        return await set_currency_config(
            guild_id,
            currency_name=field("currency_name"),
            coin_emoji_id=field("coin_emoji_id"),
            diamond_name=field("diamond_name"),
            diamond_emoji_id=field("diamond_emoji_id"),
        )

    resolved = run_async(save())

    # Audit log records the RESOLVED labels, because "which name is live
    # now" is the question anyone reading the log actually has.
    log_action(
        guild_id,
        f"Updated currency: primary = {resolved['coins']['name']} "
        f"{resolved['coins']['emoji']}, diamonds = "
        f"{resolved['diamonds']['name']} {resolved['diamonds']['emoji']}",
        "economy")
    return jsonify({"success": True, "resolved": resolved})


@api_bp.route("/economy/exchange-rate", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_exchange_rate_api():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT diamond_exchange_rate FROM guild_settings WHERE guild_id=?",
                (guild_id,))
            row = await cursor.fetchone()
        return row[0] if row and row[0] else 500

    return jsonify({"rate": run_async(fetch())})


@api_bp.route("/economy/exchange-rate", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def save_exchange_rate_api():
    guild_id = get_session_guild_id()
    data     = request.json or {}
    try:
        rate = int(data.get("rate", 500))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Rate must be a whole number"})
    if rate <= 0:
        return jsonify({"success": False, "error": "Rate must be positive"})

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO guild_settings (guild_id, diamond_exchange_rate)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    diamond_exchange_rate = excluded.diamond_exchange_rate,
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, rate))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Set diamond exchange rate to {rate}:1", "economy")
    return jsonify({"success": True, "rate": rate})


@api_bp.route("/shop/items")
@require_api_permission(LEVEL_ADMIN)
def shop_items_partial():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, name, description, price, type,
                       role_id, duration_hours, featured, enabled,
                       price_diamonds, icon_url, rarity, prestige_tier
                FROM shop_items WHERE guild_id = ?
                ORDER BY featured DESC, created_at DESC
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    # Rank Card foundation: rarity badge colors on the admin table —
    # matches the tier ordering in utils/item_catalog.py, using the
    # dashboard's existing badge classes (no new CSS needed).
    RARITY_BADGE = {
        "common": "badge", "rare": "badge-accent", "epic": "badge-accent",
        "legendary": "badge-warning", "mythical": "badge-danger",
        "secret": "badge-danger",
    }
    from dashboard.utils.currency_ctx import (
        resolved as _currency, icon_html as _currency_icon_html,
    )
    _cur = _currency(guild_id)
    # Aliased on import: `icon_html` is already taken below by the shop
    # item's own icon image.
    _coin_icon = _currency_icon_html(_cur['coins']['emoji'])
    _gem_icon = _currency_icon_html(_cur['diamonds']['emoji'])
    html = ""
    for r in rows:
        status = "badge-success" if r[8] else "badge-danger"
        label  = "Active" if r[8] else "Disabled"
        dur    = f"{r[6]}h" if r[6] else "Permanent"
        price_diamonds = r[9]
        # Price badge uses the guild's configured icon for whichever
        # currency the item is priced in.
        price_str = (f"{_gem_icon} {price_diamonds:,}"
                     if price_diamonds else f"{_coin_icon} {r[3]:,}")
        rarity = r[11] or "common"
        rarity_class = RARITY_BADGE.get(rarity, "badge")
        icon_html = (
            f"<img src='{_esc(r[10])}' style='width:20px;height:20px;"
            f"border-radius:4px;vertical-align:middle;margin-right:6px;' "
            f"onerror=\"this.style.display='none';\">"
            if r[10] else ""
        )
        # Prestige shop items carry a permanent tier (r[12]). The render
        # uses the tier label solely for display; the source of Prestige
        # state is levels.prestige + booster status, never the shop item.
        type_display = _esc(r[4])
        if (r[4] == "prestige" and r[12]):
            try:
                from utils.prestige import tier_label
                type_display = f"Prestige {tier_label(r[12])}"
            except Exception:
                type_display = "Prestige"

        # r[1] (name) and r[2] (description) are admin-entered but
        # still free text — escaped so a mischievous/compromised admin
        # account can't plant stored XSS for the next admin to view.
        html  += (
            f"<tr>"
            f"<td>{icon_html}<strong>{_esc(r[1])}</strong>{'⭐' if r[7] else ''} "
            f"<span class='badge {rarity_class}'>{_esc(rarity.title())}</span></td>"
            f"<td class='text-muted'>{_esc(r[2]) if r[2] else '—'}</td>"
            f"<td>{price_str}</td>"
            f"<td>{type_display}</td>"
            f"<td>{dur}</td>"
            f"<td><span class='badge {status}'>{label}</span></td>"
            f"<td><button class='btn btn-sm btn-danger' "
            f"hx-delete='/api/shop/item/{r[0]}' "
            f"hx-confirm='Delete this item?' "
            f"hx-target='closest tr' "
            f"hx-swap='outerHTML'>Delete</button></td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='7' class='empty'>No shop items yet</td></tr>"


@api_bp.route("/shop/item/<int:item_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_shop_item(item_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM shop_items WHERE id = ? AND guild_id = ?",
                (item_id, guild_id))
            await db.commit()

    run_async(delete())
    return ""


@api_bp.route("/shop/item", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_shop_item():
    guild_id = get_session_guild_id()
    data     = request.json or request.form.to_dict()

    max_stock_val = data.get("max_stock")
    try:
        max_stock_val = int(max_stock_val) if max_stock_val not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        max_stock_val = None
    current_stock_val = max_stock_val  # a brand-new item starts full

    price_diamonds_val = data.get("price_diamonds")
    try:
        price_diamonds_val = (
            int(price_diamonds_val)
            if price_diamonds_val not in (None, "", 0, "0") else None)
    except (TypeError, ValueError):
        price_diamonds_val = None

    price_val = int(data.get("price", 0) or 0)
    item_name = data.get("name")
    icon_url  = (data.get("icon_url") or "").strip() or None

    # Finalized Prestige: a shop item of type='prestige' carries the
    # permanent tier (1..5) it grants. Must be coins-priced; a diamond
    # price is rejected by the backend at purchase time.
    prestige_tier_val = data.get("prestige_tier")
    try:
        prestige_tier_val = (
            int(prestige_tier_val)
            if prestige_tier_val not in (None, "", 0, "0") else None)
    except (TypeError, ValueError):
        prestige_tier_val = None
    if prestige_tier_val is not None and prestige_tier_val not in (1, 2, 3, 4, 5, 6):
        return jsonify({"success": False,
                        "error": "Prestige tier must be 1–6"})
    # Finalized Prestige: Prestige is Coins-only. The UI guards this, but the
    # API must reject a `prestige` item carrying a diamond price server-side
    # (the purchase path in cogs/shop.py also rejects it at buy time).
    if (data.get("type") or "role") == "prestige" and price_diamonds_val:
        return jsonify({"success": False,
                        "error": "Prestige items are purchased with Coins and "
                                 "cannot have a diamond price."})

    # Wallet pass: a potion is delivered into the buyer's inventory and
    # applies its effect only when used, so an unusable one is worse
    # than a refused save — the member discovers it's broken only after
    # paying. It reuses xp_boost_multiplier + duration_hours (no new
    # columns), and both are required here, mirroring the same check
    # cogs/shop.py performs at purchase time. Titles need no extra
    # validation: name + icon + rarity is the whole item.
    item_type_val = (data.get("type") or "role")
    try:
        xp_boost_multiplier_val = (
            float(data.get("xp_boost_multiplier"))
            if data.get("xp_boost_multiplier") not in (None, "", 0, "0")
            else None)
    except (TypeError, ValueError):
        xp_boost_multiplier_val = None

    if item_type_val == "potion":
        potion_mult = xp_boost_multiplier_val or 0.0
        try:
            potion_hours = int(data.get("duration_hours") or 0)
        except (TypeError, ValueError):
            potion_hours = 0
        if potion_mult <= 1.0:
            return jsonify({"success": False,
                            "error": "Potions need an effect multiplier "
                                     "greater than 1."})
        if potion_hours <= 0:
            return jsonify({"success": False,
                            "error": "Potions need an effect duration in "
                                     "hours."})

    # Rank Card foundation: rarity is admin-set at creation time,
    # same place price/icon are already captured. Falls back to
    # 'common' on anything unrecognized rather than rejecting the
    # save — a typo'd rarity shouldn't block adding the item.
    from utils.item_catalog import is_valid_rarity, upsert_catalog_entry
    rarity = (data.get("rarity") or "common").strip().lower()
    if not is_valid_rarity(rarity):
        rarity = "common"

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO shop_items
                    (guild_id, name, description, price, type,
                     role_id, duration_hours, featured,
                     required_level, required_role_id,
                     max_stock, current_stock, enabled, price_diamonds,
                     icon_url, rarity, prestige_tier, xp_boost_multiplier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """, (
                guild_id,
                item_name,
                data.get("description"),
                price_val,
                data.get("type", "role"),
                data.get("role_id") or None,
                data.get("duration_hours") or None,
                int(data.get("featured", 0)),
                int(data.get("required_level", 0)),
                data.get("required_role_id") or None,
                max_stock_val,
                current_stock_val,
                price_diamonds_val,
                icon_url,
                rarity,
                prestige_tier_val,
                # PRE-EXISTING BUG FIX (found during the Wallet pass):
                # this column was in the form, in the JS payload, and
                # read back by cogs/shop.py's purchase path — but was
                # never actually part of this INSERT, so it always
                # persisted as NULL. Every XP Boost item created from
                # the dashboard was therefore permanently unbuyable
                # ("isn't configured correctly (missing or invalid
                # multiplier)"). Potions reuse the same column, so this
                # had to be corrected rather than worked around.
                xp_boost_multiplier_val,
            ))
            await db.commit()

        value_currency = "diamonds" if price_diamonds_val else ("balance" if price_val else None)
        value_amount = price_diamonds_val if price_diamonds_val else (price_val or None)
        await upsert_catalog_entry(
            guild_id, item_name, icon_url=icon_url, rarity=rarity,
            value_currency=value_currency, value_amount=value_amount)

    run_async(save())
    log_action(guild_id, f"Added shop item: {data.get('name')}", "shop")
    return jsonify({"success": True})


@api_bp.route("/shop/purchase-history")
@require_api_permission(LEVEL_ADMIN)
def shop_purchase_history():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_display_name, item_name,
                       price_paid, purchased_at, expires_at,
                       currency_paid
                FROM purchase_history
                WHERE guild_id = ?
                ORDER BY purchased_at DESC LIMIT 50
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())
    from dashboard.utils.currency_ctx import (
        resolved as _currency, icon_html as _currency_icon_html,
    )
    _cur = _currency(guild_id)
    _coin_icon = _currency_icon_html(_cur['coins']['emoji'])
    _gem_icon = _currency_icon_html(_cur['diamonds']['emoji'])
    html = ""
    for r in rows:
        exp   = r[4][:10] if r[4] else "Permanent"
        # 'currency_paid' stores the stable key; the glyph comes from the
        # guild's configured icon for that key.
        currency_icon = (_gem_icon
                         if (len(r) > 5 and r[5] == "diamonds")
                         else _coin_icon)
        # r[0] (user_display_name — a Discord nickname) and r[1]
        # (item_name — admin-entered but still free text) are both
        # escaped below; same stored-XSS class as moderation_logs_partial.
        html += (
            f"<tr>"
            f"<td>{_esc(r[0])}</td>"
            f"<td><strong>{_esc(r[1])}</strong></td>"
            f"<td>{currency_icon} {r[2]:,}</td>"
            f"<td class='text-muted'>{str(r[3])[:10] if r[3] else '—'}</td>"
            f"<td class='text-muted'>{exp}</td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='5' class='empty'>No purchases yet</td></tr>"


@api_bp.route("/shop/temp-roles")
@require_api_permission(LEVEL_ADMIN)
def shop_temp_roles():
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, role_id, expires_at, source
                FROM temp_roles WHERE guild_id = ?
                ORDER BY expires_at ASC
            """, (guild_id,))
            return await cursor.fetchall()

    rows = run_async(fetch())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        return await resolve_users(guild_id, [r[0] for r in rows])

    user_map = run_async(resolve()) if rows else {}

    from dashboard.utils.user_identity import render_user_identity_html
    html = ""
    for r in rows:
        u = user_map.get(r[0], {})
        identity_html = render_user_identity_html(
            r[0], u.get("display_name"), u.get("username"), u.get("avatar_url"))
        html += (
            f"<tr>"
            f"<td>{identity_html}</td>"
            f"<td><code>{r[1]}</code></td>"
            f"<td class='text-muted'>{str(r[2])[:16] if r[2] else '—'}</td>"
            f"<td><span class='badge badge-accent'>{_esc(r[3])}</span></td>"
            f"</tr>"
        )
    return html or "<tr><td colspan='4' class='empty'>No active temp roles</td></tr>"
