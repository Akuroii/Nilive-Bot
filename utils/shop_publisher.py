"""
Shop Publisher — the fixed token resolver and preview assembly (Phase 1).

Phase 1 scope is exactly: Template + Product -> token resolution -> Publisher
preview. This module owns the middle of that pipeline and is the ONE place a
token is ever interpreted. Phase 2's publish path must call the same functions,
so "preview == publish" is an identity, not a promise.

THE TOKEN RESOLVER IS FIXED, DETERMINISTIC AND NON-PROGRAMMABLE (locked):

  * A FIXED catalog (TOKEN_CATALOG below). Every resolvable name exists there,
    with a description; nothing outside it ever resolves.
  * DETERMINISTIC: pure functions, no clock, no randomness, no IO, no imports
    beyond the standard library + the read-only validators/labels it reuses.
    The same template + product + currency always produce byte-identical
    output, and repeated resolution never differs.
  * NON-PROGRAMMABLE: the syntax is a bare lookup `{{product.name}}` (optional
    inner whitespace). There are no expressions, no nesting, no conditions, no
    filters, no pipes, no arithmetic, no function calls. A resolved VALUE is
    never re-scanned for tokens, so product text containing `{{...}}` can never
    inject or escalate a lookup. Anything that is not exactly a catalog token
    is left verbatim in the output and reported as an unknown-token warning.

FREE IS NOT PART OF PHASE 1 (locked): `{{product.price}}` renders the
mechanical price for every row, including zero-price rows (e.g. "0 🪙 Coins").
The `𝐅𝐫𝐞𝐞` rendering of no-price / zero-price is Phase 3 and lands as a
deliberate edit to format_price() + that test, never as an accident. Paid-product
validation (utils/shop_validation.py) is untouched by this module.

PURCHASE ACTION (locked): the preview must show the purchase action that will
actually be published. purchase_action() is the canonical descriptor Phase 2
publishes verbatim: a green button labelled "Buy <name>" carrying the EXISTING
`shop_buy_<id>` custom id, which cogs/shop.py's on_interaction already routes to
process_purchase(). No new purchase engine, no new mechanism.

Template shape: embed_templates rows as loaded by the Embed Builder API — a
whole message {"content": str, "embeds": [...]}. Legacy rows that predate that
shape (a bare embed dict) normalize through normalize_template_doc(), the same
rule dashboard/api/embedbuilder.py applies on read, so both readers agree.

Pure Python: no Flask, no Discord, no database access — the dashboard route and
scripts/test_shop_publisher.py both exercise this module directly.
"""

import re
from copy import deepcopy

from utils.embed_schema import validate_message

# ── The fixed token catalog (insertion order = display order) ───────────────
# One entry per resolvable name. The UI's token reference panel renders exactly
# this, so an admin and the resolver can never disagree about what is available.
TOKEN_CATALOG = {
    "product.id": "The product's shop id (shop_items.id).",
    "product.name": "The product name.",
    "product.description": "The product description (empty when unset).",
    "product.price": ("The formatted price with its currency icon and name, "
                      "e.g. \"1,000 🪙 Coins\"."),
    "product.price_amount": "The raw price number as text.",
    "product.currency_name": "Display name of the currency this product charges in.",
    "product.currency_emoji": "Icon of the currency this product charges in.",
    "product.type": "The stored item type (role, temp_role, xp_boost, prestige, potion, title, custom).",
    "product.duration": "Duration in hours as text (e.g. \"72h\"), empty when permanent.",
    "product.stock": "Stock as text (e.g. \"12/20\"), empty when unlimited.",
    "product.required_level": "Required level as text, empty when there is no requirement.",
    "product.rarity": "The item rarity (common, rare, epic, legendary, mythical, secret).",
    "product.icon_url": "The item icon URL, empty when unset.",
    "product.prestige_tier": "The Prestige tier in roman numerals (I–VI), empty for other items.",
}

# {{ name }} with optional inner whitespace. Deliberately NOT a template
# language: the pattern cannot express anything but a dotted identifier.
TOKEN_RE_PATTERN = r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}"
TOKEN_RE = re.compile(TOKEN_RE_PATTERN)

# V1 picker grouping reuses the existing `type` column only — this is the
# display order of the groups, matching the Shop admin form's type order. No
# new category system is introduced anywhere in the Publisher.
TYPE_GROUP_ORDER = ("role", "temp_role", "xp_boost", "prestige", "potion", "title", "custom")


def type_group_key(item_type: str):
    """Sort key placing the known `type` groups first, in shop-form order."""
    try:
        return (0, TYPE_GROUP_ORDER.index(item_type), item_type)
    except ValueError:
        return (1, 0, item_type or "")


def token_catalog_payload() -> list:
    """The fixed catalog in display order, for the picker's token reference."""
    return [
        {"token": "{{" + key + "}}", "key": key, "description": description}
        for key, description in TOKEN_CATALOG.items()
    ]


def normalize_template_doc(doc) -> dict:
    """embed_templates.data -> {"content": str, "embeds": [dict, ...]}.

    The same normalization dashboard/api/embedbuilder.py applies on read:
    legacy rows (a bare embed dict, no "content"/"embeds" keys) become a
    one-embed message; missing content becomes ""; non-dict embeds drop out.
    """
    if not isinstance(doc, dict):
        return {"content": "", "embeds": []}
    if "embeds" not in doc and "content" not in doc:
        return {"content": "", "embeds": [deepcopy(doc)]}
    content = doc.get("content")
    embeds = doc.get("embeds")
    return {
        "content": content if isinstance(content, str) else "",
        "embeds": [deepcopy(e) for e in (embeds or []) if isinstance(e, dict)],
    }


def charging_amount(product: dict):
    """(column_key, amount) — the same rule cogs/shop.py charges with:
    a diamond price takes precedence; otherwise the coin price applies."""
    price_diamonds = product.get("price_diamonds")
    if price_diamonds:
        return "diamonds", int(price_diamonds)
    return "balance", int(product.get("price") or 0)


def format_price(amount: int, currency_name: str, currency_emoji: str) -> str:
    """Mechanical price rendering — thousands separators + icon + name.

    PHASE 1 DELIBERATELY DOES NOT SPECIAL-CASE ZERO. A zero-price row renders
    e.g. "0 🪙 Coins"; the locked Free rendering (`𝐅𝐫𝐞𝐞` for no-price /
    zero-price) is Phase 3 and changes this function on purpose.
    """
    return f"{int(amount):,} {currency_emoji} {currency_name}"


def product_token_values(product: dict, currency: dict) -> dict:
    """Every catalog token -> the exact string it resolves to.

    `product` is one shop_items row as a dict; `currency` is the
    utils/currency.get_currency_config() shape. Absent fields resolve to empty
    strings rather than the literal text "None", so a template token over an
    unset column renders clean and is reported as an empty-value warning.
    """
    product = product or {}
    currency = currency or {}
    column_key, amount = charging_amount(product)
    bucket = currency.get("diamonds" if column_key == "diamonds" else "coins") or {}
    currency_name = bucket.get("name") or ("Diamonds" if column_key == "diamonds" else "Coins")
    currency_emoji = bucket.get("emoji") or ("💎" if column_key == "diamonds" else "🪙")

    duration_hours = product.get("duration_hours")
    max_stock = product.get("max_stock")
    current_stock = product.get("current_stock")
    required_level = product.get("required_level")
    prestige_tier = product.get("prestige_tier")

    if prestige_tier:
        # Read-only reuse of the single Prestige label source; no Prestige
        # behavior is implemented or modified here.
        from utils.prestige import tier_label
        tier_text = tier_label(int(prestige_tier))
    else:
        tier_text = ""

    return {
        "product.id": str(int(product.get("id") or 0)),
        "product.name": str(product.get("name") or ""),
        "product.description": str(product.get("description") or ""),
        "product.price": format_price(amount, currency_name, currency_emoji),
        "product.price_amount": str(amount),
        "product.currency_name": currency_name,
        "product.currency_emoji": currency_emoji,
        "product.type": str(product.get("type") or ""),
        "product.duration": f"{int(duration_hours)}h" if duration_hours else "",
        "product.stock": (
            f"{int(current_stock or 0)}/{int(max_stock)}" if max_stock else ""),
        "product.required_level": (
            str(int(required_level)) if required_level else ""),
        "product.rarity": str(product.get("rarity") or "common"),
        "product.icon_url": str(product.get("icon_url") or ""),
        "product.prestige_tier": tier_text,
    }


# The documented text surfaces a token may appear in. Resolution is confined to
# these paths (mirroring utils/embed_schema.py's error paths) so the walk can
# never wander into an unknown key and quietly start interpreting editor data.

def _embed_text_leaves(embed: dict, prefix: str, visit) -> None:
    for key in ("title", "description", "url", "timestamp"):
        if isinstance(embed.get(key), str):
            visit(embed, key, f"{prefix}.{key}")
    for group, keys in (("author", ("name", "url", "icon_url")),
                        ("footer", ("text", "icon_url"))):
        node = embed.get(group)
        if isinstance(node, dict):
            for key in keys:
                if isinstance(node.get(key), str):
                    visit(node, key, f"{prefix}.{group}.{key}")
    for key in ("image", "thumbnail"):
        node = embed.get(key)
        if isinstance(node, dict) and isinstance(node.get("url"), str):
            visit(node, "url", f"{prefix}.{key}.url")
    fields = embed.get("fields")
    if isinstance(fields, list):
        for i, field in enumerate(fields):
            if not isinstance(field, dict):
                continue
            for key in ("name", "value"):
                if isinstance(field.get(key), str):
                    visit(field, key, f"{prefix}.fields.{i}.{key}")


def resolve_message(template_doc, values: dict):
    """Substitute catalog tokens through the template's text surfaces.

    Returns (resolved_doc, used):
      resolved_doc — a NEW normalized message dict; the input is never mutated.
      used — one entry per token occurrence, in document order:
             {"token", "key", "path", "resolved", "value"}
             resolved=False means the name is not in the catalog (left verbatim);
             value is the substituted string ("" when unknown).
    """
    doc = normalize_template_doc(template_doc)
    used = []

    def resolve_text(text: str, path: str) -> str:
        if "{{" not in text:
            return text

        def repl(match):
            key = match.group(1)
            resolved = key in values
            value = values.get(key, "")
            used.append({
                "token": match.group(0), "key": key, "path": path,
                "resolved": resolved, "value": value if resolved else "",
            })
            # Unknown tokens stay verbatim; known values are emitted once and
            # never re-scanned (non-programmable: no nesting, no escalation).
            return value if resolved else match.group(0)

        return TOKEN_RE.sub(repl, text)

    if isinstance(doc["content"], str):
        doc["content"] = resolve_text(doc["content"], "content")
    for i, embed in enumerate(doc["embeds"]):
        _embed_text_leaves(embed, f"embeds.{i}",
                           lambda node, key, path: node.__setitem__(
                               key, resolve_text(node[key], path)))
    return doc, used


def purchase_action(product: dict) -> dict:
    """THE purchase action Phase 2 publishes verbatim.

    A green "Buy <name>" button carrying `shop_buy_<id>` — the custom-id family
    cogs/shop.py's on_interaction already dispatches to process_purchase(). The
    label follows /shop's existing button convention; Phase 3 (Free + Prestige
    presentation) revisits the label for free-tier items on purpose.
    """
    product = product or {}
    item_id = int(product.get("id") or 0)
    name = product.get("name") or ""
    return {
        "type": "button",
        "custom_id": f"shop_buy_{item_id}",
        "label": f"Buy {name}",
        "style": "green",
        "emoji": "🛒",
    }


def collect_warnings(product: dict, used: list, resolved_doc: dict) -> list:
    """Deterministic, pathed preview warnings. Order is fixed:

    1. product state (disabled, out of stock),
    2. token occurrences in document order (unknown token, empty value),
    3. Discord-rule breaches of the RESOLVED payload (utils/embed_schema),
       which Phase 2 must treat as publish blockers — surfaced here as
       warnings because Phase 1 has no publish to block.
    """
    warnings = []
    product = product or {}

    if not product.get("enabled", 1):
        warnings.append({
            "code": "product_disabled", "path": "",
            "message": "This product is disabled — the purchase button would refuse purchases.",
        })
    max_stock = product.get("max_stock")
    if max_stock and not product.get("current_stock"):
        warnings.append({
            "code": "product_out_of_stock", "path": "",
            "message": (f"This product is out of stock (0/{int(max_stock)}) — "
                        "the purchase button would refuse purchases."),
        })

    for entry in used:
        if not entry["resolved"]:
            warnings.append({
                "code": "unknown_token", "path": entry["path"],
                "message": (f"Unknown token {entry['token']} at {entry['path']} "
                            "was left as typed — it is not in the token catalog."),
            })
        elif entry["value"] == "":
            warnings.append({
                "code": "empty_value", "path": entry["path"],
                "message": (f"Token {entry['token']} at {entry['path']} "
                            "resolved to an empty value for this product."),
            })

    _ok, errors = validate_message(resolved_doc, [])
    for error in errors:
        warnings.append({
            "code": "validation", "path": error.get("path") or "",
            "message": error.get("message") or "Invalid message payload.",
        })
    return warnings


def preview_message(template_doc, product: dict, currency: dict) -> dict:
    """Template + Product -> resolved presentation + purchase action + warnings.

    The returned message (content/embeds) is EXACTLY what Phase 2 publishes,
    and purchase_action is EXACTLY the button Phase 2 attaches to it.
    """
    values = product_token_values(product, currency)
    resolved, used = resolve_message(template_doc, values)
    return {
        "content": resolved["content"],
        "embeds": resolved["embeds"],
        "purchase_action": purchase_action(product),
        "warnings": collect_warnings(product, used, resolved),
        "tokens": {"values": values, "used": used},
    }
