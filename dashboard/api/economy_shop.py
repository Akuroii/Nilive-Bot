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


# Finalized product-type display names. The five admin-facing types map their
# internal value to a label (Item->custom, Color->color, Potion, Title,
# Prestige); the legacy internal types keep a clear label so EXISTING rows stay
# readable in the admin table without being re-typed. Prestige is special-cased
# with its tier below. Stored `type` values are never renamed.
SHOP_TYPE_DISPLAY = {
    "custom": "Item",
    "color": "Color",
    "potion": "Potion",
    "title": "Title",
    "prestige": "Prestige",
    "role": "Permanent Role",
    "temp_role": "Temporary Role",
    "xp_boost": "XP Boost",
}


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
        vi = r[4] == "prestige" and r[12] == 6
        if vi:
            dur = "While boosting; activation required"
        # Free = zero charge (explicit 0 coin price, no diamond price), plus
        # VI by its own rule. Only the PRICE cell treats a normal Free item as
        # Free; the duration cell below stays VI-only.
        free = vi or (not price_diamonds and r[3] == 0)
        price_str = "Free" if free else (f"{_gem_icon} {price_diamonds:,}"
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
        # Every other type shows its finalized display name (Item/Color/
        # Potion/Title, and readable labels for legacy role/temp_role/xp_boost).
        type_display = _esc(SHOP_TYPE_DISPLAY.get(r[4], r[4]))
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
            f"<td>"
            # r[0] (item id) is an INTEGER from the DB, interpolated raw into
            # the inline onclick exactly like the members partial's
            # openEditModal('{user_id}', ...) — numeric, so not an injection
            # vector. editItem() prefills the Add form in edit mode and PUTs.
            f"<button class='btn btn-sm btn-secondary' "
            f"onclick='editItem({r[0]})' style='margin-right:6px;'>Edit</button>"
            f"<button class='btn btn-sm btn-danger' "
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
            cursor = await db.execute(
                "SELECT 1 FROM shop_items "
                "WHERE guild_id = ? AND option_of_id = ? LIMIT 1",
                (guild_id, item_id))
            if await cursor.fetchone():
                return False
            await db.execute(
                "DELETE FROM shop_items WHERE id = ? AND guild_id = ?",
                (item_id, guild_id))
            await db.commit()
        return True

    try:
        deleted = run_async(delete())
    except aiosqlite.IntegrityError:
        # The database trigger is authoritative if a child was concurrently
        # added after the read above.
        return jsonify({"success": False,
                        "error": "A root product with options cannot be deleted."}), 409
    if not deleted:
        return jsonify({"success": False,
                        "error": "A root product with options cannot be deleted."}), 409
    return ""


@api_bp.route("/shop/item", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def add_shop_item():
    guild_id = get_session_guild_id()
    from utils.shop_validation import shop_input, ShopValidationError
    raw = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    # Finalized decision: no NEW XP Boost products. The XP Boost purchase path
    # and every EXISTING boost listing stay untouched (cogs/shop.py is out of
    # scope); this only closes the creation door, which the dashboard UI no
    # longer offers either. Refused BEFORE validation so any create attempt gets
    # one clear message regardless of the other fields, and so a malformed boost
    # payload can't surface a confusing validation error instead of the block.
    _raw_type = raw.get("type") if isinstance(raw, dict) else None
    if isinstance(_raw_type, str) and _raw_type.strip().lower() == "xp_boost":
        return jsonify({"success": False,
                        "error": "XP Boost products can no longer be created. "
                                 "Existing XP Boost items are unaffected."}), 400
    try:
        data = shop_input(raw)
    except ShopValidationError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    option_parent_id = data["option_of_id"]
    if option_parent_id is not None:
        async def validate_option_parent():
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT id FROM shop_items "
                    "WHERE id = ? AND guild_id = ? AND option_of_id IS NULL",
                    (option_parent_id, guild_id))
                return await cursor.fetchone()
        if not run_async(validate_option_parent()):
            return jsonify({"success": False,
                            "error": "Option parent must be a root product in this guild."}), 400

    max_stock_val = data["max_stock"]
    current_stock_val = max_stock_val
    price_diamonds_val = data["price_diamonds"]
    price_val = data["price"]
    item_name = data["name"]
    icon_url = data["icon_url"] or None
    prestige_tier_val = data["prestige_tier"]
    xp_boost_multiplier_val = data["xp_boost_multiplier"]

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
                     icon_url, rarity, prestige_tier, xp_boost_multiplier,
                     option_of_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
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
                option_parent_id,
            ))
            await db.commit()

        value_currency = "diamonds" if price_diamonds_val else ("balance" if price_val else None)
        value_amount = price_diamonds_val if price_diamonds_val else (price_val or None)
        await upsert_catalog_entry(
            guild_id, item_name, icon_url=icon_url, rarity=rarity,
            value_currency=value_currency, value_amount=value_amount)

    try:
        run_async(save())
    except aiosqlite.IntegrityError:
        return jsonify({"success": False,
                        "error": "Option parent must be a root product in this guild."}), 400
    log_action(guild_id, f"Added shop item: {data.get('name')}", "shop")
    return jsonify({"success": True})


# Columns the Edit form may prefill / the update may write. `type`,
# `category_id` and `option_of_id` are read for prefill and validation but are
# NEVER written by the update (type is immutable; category/option links are
# owned by the Category API and the option-family triggers).
_SHOP_ITEM_EDIT_KEYS = (
    "name", "description", "price", "type", "role_id", "duration_hours",
    "featured", "enabled", "required_level", "required_role_id", "max_stock",
    "current_stock", "price_diamonds", "icon_url", "rarity", "prestige_tier",
    "xp_boost_multiplier", "category_id", "option_of_id",
)
_SHOP_ITEM_EDIT_SELECT = (
    "name, description, price, type, role_id, duration_hours, featured, "
    "enabled, required_level, required_role_id, max_stock, current_stock, "
    "price_diamonds, icon_url, rarity, prestige_tier, xp_boost_multiplier, "
    "category_id, option_of_id"
)


@api_bp.route("/shop/item/<int:item_id>", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_shop_item(item_id: int):
    """Full row for the Edit form prefill.

    Guild-scoped: an unknown id and an id belonging to another guild return the
    SAME 404, so cross-guild existence is never disclosed (matching the
    Category API's convention)."""
    guild_id = get_session_guild_id()

    async def fetch():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                f"SELECT id, {_SHOP_ITEM_EDIT_SELECT} "
                "FROM shop_items WHERE id = ? AND guild_id = ?",
                (item_id, guild_id))
            row = await cursor.fetchone()
            return dict(zip(("id",) + _SHOP_ITEM_EDIT_KEYS, row)) if row else None

    item = run_async(fetch())
    if item is None:
        return jsonify({"success": False, "error": "Item not found."}), 404
    return jsonify({"success": True, "item": item})


@api_bp.route("/shop/item/<int:item_id>", methods=["PUT"])
@require_api_permission(LEVEL_ADMIN)
def update_shop_item(item_id: int):
    """Update the editable fields of ONE product in the session guild.

    True CRUD update, deliberately conservative:
      * `type` is IMMUTABLE — the STORED type always wins over the body, so a
        legacy role/temp_role/xp_boost product can have its safe fields edited
        but can never be re-typed (and a new XP Boost can't be smuggled in by
        editing something else into one).
      * `category_id` and `option_of_id` are PRESERVED — never in the SET
        clause, so the option-family integrity triggers (which fire only on
        UPDATE OF id/option_of_id/guild_id) never run and the root-with-options
        delete guard is untouched.
      * Stock: `current_stock` is ALWAYS preserved — a `current_stock` key in
        the body is deliberately IGNORED (stock quantities are not managed by
        Product Management; the UI shows the value read-only). `max_stock` may
        change but NEVER restocks, and the maximum may not be lowered below the
        stock actually on hand.
      * `icon_url`: an ABSENT key keeps the stored icon; a PRESENT-but-blank key
        is an explicit clear (the same present/absent convention the currency
        -config API in this file already uses).
      * Every other field re-runs the SAME shop_input validator as creation,
        against the STORED type, so an edit can't smuggle in values a create
        would reject (and Title/Prestige permanence, Color's role requirement,
        Prestige pricing etc. all still hold).
    """
    guild_id = get_session_guild_id()
    raw = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    if not isinstance(raw, dict):
        return jsonify({"success": False, "error": "A Shop item object is required."}), 400

    from utils.shop_validation import shop_input, ShopValidationError

    async def fetch_existing():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                f"SELECT {_SHOP_ITEM_EDIT_SELECT} "
                "FROM shop_items WHERE id = ? AND guild_id = ?",
                (item_id, guild_id))
            return await cursor.fetchone()

    existing_row = run_async(fetch_existing())
    if existing_row is None:
        return jsonify({"success": False, "error": "Item not found."}), 404
    existing = dict(zip(_SHOP_ITEM_EDIT_KEYS, existing_row))

    # Merge: stored values, overridden ONLY by keys present in the body. This
    # gives PATCH-style partial updates while still running the full validator.
    merged = dict(existing)
    for key in ("name", "description", "price", "role_id", "duration_hours",
                "featured", "required_level", "required_role_id", "max_stock",
                "price_diamonds", "rarity", "prestige_tier",
                "xp_boost_multiplier"):
        if key in raw:
            merged[key] = raw[key]
    merged["type"] = existing["type"]                  # immutable
    merged["option_of_id"] = existing["option_of_id"]  # preserved
    # icon_url: present -> take the body value (blank == explicit clear);
    # absent -> keep the stored icon.
    if "icon_url" in raw:
        merged["icon_url"] = raw["icon_url"]

    # Legacy compatibility (grandfather clause): Title/Prestige rows created
    # before the permanence rule could legally store a positive duration, and
    # the Edit form resubmits the stored value as-is. Such a row must stay
    # safe-field-editable WITHOUT silently rewriting its duration. When the
    # body OMITS duration_hours, or resubmits the UNCHANGED stored value
    # (int or form-string), we validate the merged record as permanent — the
    # representation the rule expects — but write the ORIGINAL stored value
    # back untouched. Setting a DIFFERENT positive duration still fails the
    # permanence rule, an explicit 0/blank/null is still the supported
    # permanent clear, and POST creation validation is unaffected. The clause
    # cannot apply to any other type and never fires for rows without a
    # stored positive duration.
    legacy_duration_keep = None
    if existing["type"] in ("title", "prestige") and existing["duration_hours"]:
        if "duration_hours" not in raw:
            unchanged = True
        else:
            submitted = raw["duration_hours"]
            unchanged = (
                submitted == existing["duration_hours"]
                or (isinstance(submitted, str) and submitted.strip().isdigit()
                    and int(submitted.strip()) == existing["duration_hours"]))
        if unchanged:
            legacy_duration_keep = existing["duration_hours"]
            merged["duration_hours"] = 0   # validation representation only

    # `enabled` is a safe field the validator doesn't model; normalize it here.
    enabled_val = existing["enabled"]
    if "enabled" in raw:
        _e = raw["enabled"]
        if isinstance(_e, bool):
            enabled_val = 1 if _e else 0
        elif isinstance(_e, int) and _e in (0, 1):
            enabled_val = _e
        elif isinstance(_e, str) and _e.strip() in ("0", "1"):
            enabled_val = int(_e.strip())
        else:
            return jsonify({"success": False, "error": "Enabled must be 0 or 1."}), 400

    try:
        data = shop_input(merged)
    except ShopValidationError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    # Stock reconciliation. `current_stock` is ALWAYS preserved: editing product
    # data never restocks or drains, and a `current_stock` key in the body is
    # deliberately IGNORED — stock quantities are outside Product Management,
    # and the UI's current-stock field is read-only to match. Two rules:
    #   1. A finite maximum requires stock on hand. An unlimited product stores
    #      current_stock = NULL, and editing never initializes stock, so an
    #      unlimited -> finite transition would leave (max_stock=N,
    #      current_stock=NULL) — a row safe_decrement_stock() can never
    #      decrement (NULL - 1 stays NULL) or exhaust: it would sell forever
    #      while looking finite. There is no admin stock-provisioning
    #      mechanism (stock is set once at creation, then only purchase/
    #      refund/expiry move it), so this transition is REJECTED rather than
    #      silently provisioning stock.
    #   2. The maximum may never drop below the stock actually on hand.
    new_max = data["max_stock"]              # normalized: None == unlimited
    current = existing["current_stock"]      # always preserved, never written
    if new_max is not None and current is None:
        return jsonify({"success": False,
                        "error": "This product has unlimited stock. A stock limit "
                                 "cannot be added by editing — create a new "
                                 "product with a stock limit instead."}), 400
    if new_max is not None and current is not None and current > new_max:
        return jsonify({"success": False,
                        "error": "Maximum stock cannot be lower than the current "
                                 f"stock ({current})."}), 400

    # Persisted duration: the grandfathered legacy value wins (written back
    # byte-for-byte); otherwise the validated/normalized value as usual.
    duration_val = (legacy_duration_keep if legacy_duration_keep is not None
                    else (data.get("duration_hours") or None))

    icon_url_val = data["icon_url"] or None     # "" / None -> NULL (explicit clear)
    price_val = data["price"]
    price_diamonds_val = data["price_diamonds"]
    new_name = data["name"]
    from utils.item_catalog import is_valid_rarity, upsert_catalog_entry
    rarity = (data.get("rarity") or "common").strip().lower()
    if not is_valid_rarity(rarity):
        rarity = "common"

    async def write():
        async with aiosqlite.connect(DB_PATH) as db:
            sets = [
                "name = ?", "description = ?", "price = ?", "role_id = ?",
                "duration_hours = ?", "featured = ?", "required_level = ?",
                "required_role_id = ?", "max_stock = ?", "enabled = ?",
                "price_diamonds = ?", "icon_url = ?", "rarity = ?",
                "prestige_tier = ?", "xp_boost_multiplier = ?",
            ]
            params = [
                new_name, data.get("description"), price_val,
                data.get("role_id") or None, duration_val,
                int(data.get("featured", 0)), int(data.get("required_level", 0)),
                data.get("required_role_id") or None, new_max, enabled_val,
                price_diamonds_val, icon_url_val, rarity,
                data.get("prestige_tier"), data.get("xp_boost_multiplier"),
            ]
            # current_stock is intentionally NOT in the SET clause — always
            # preserved (see the stock note above).
            params.extend([item_id, guild_id])
            await db.execute(
                f"UPDATE shop_items SET {', '.join(sets)} "
                "WHERE id = ? AND guild_id = ?", params)
            await db.commit()

        # Keep the rank-card catalog in step with the edited listing, exactly as
        # the add path does. COALESCE inside upsert_catalog_entry means a
        # blanked icon_url (None) PRESERVES the catalog's cached icon rather
        # than wiping it — see the report's "COALESCE impact" note.
        value_currency = "diamonds" if price_diamonds_val else ("balance" if price_val else None)
        value_amount = price_diamonds_val if price_diamonds_val else (price_val or None)
        await upsert_catalog_entry(
            guild_id, new_name, icon_url=icon_url_val, rarity=rarity,
            value_currency=value_currency, value_amount=value_amount)

    try:
        run_async(write())
    except aiosqlite.IntegrityError:
        # The option-family triggers are authoritative. This update never
        # changes id/guild_id/option_of_id, so reaching here is unexpected;
        # fail closed rather than half-write.
        return jsonify({"success": False,
                        "error": "This product could not be updated."}), 409
    log_action(guild_id, f"Edited shop item: {new_name}", "shop")
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
        exp = r[4][:10] if r[4] else ("—" if r[2] == 0 else "Permanent")
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
            f"<td>{'Free' if r[2] == 0 else f'{currency_icon} {r[2]:,}'}</td>"
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
