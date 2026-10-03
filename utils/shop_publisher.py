"""
Shop Publisher — the fixed token resolver and design-draft preview assembly.

This module owns the middle of the Template/Design -> Discord message pipeline
and is the ONE place a token is ever interpreted. The publish path must call
the same functions, so "preview == publish" is an identity, not a promise.

THE TOKEN RESOLVER IS FIXED, DETERMINISTIC AND NON-PROGRAMMABLE (locked):

  * A FIXED catalog (TOKEN_CATALOG below). Every resolvable name exists there,
    with a label, a group and a description; nothing outside it ever resolves.
    The user-facing short spellings ({{name}}, {{price}}, ...) are a FIXED
    alias table (TOKEN_ALIASES) over the collision-safe canonical namespace
    ({{product.name}} ...) — still just lookups, never a language.
  * DETERMINISTIC: pure functions, no clock, no randomness, no IO, no imports
    beyond the standard library + the read-only validators/labels it reuses.
    The same design draft + rows + currency always produce byte-identical
    output, and repeated resolution never differs.
  * NON-PROGRAMMABLE: the syntax is a bare lookup `{{product.name}}` /
    `{{name}}` (optional inner whitespace). There are no expressions, no
    nesting, no conditions, no filters, no pipes, no arithmetic, no function
    calls, no loops. A resolved VALUE is never re-scanned for tokens, so
    product text containing `{{...}}` can never inject or escalate a lookup.
    Anything that is not exactly a catalog token (or alias) is left verbatim
    in the output and reported as an unknown-token warning.

THE DESIGN DRAFT (Step 0 contract) is what preview_design() consumes:

    {
      "id":            int | 0,            # optional, seeds select custom_ids
      "presentation":  {"mode", "content", "embeds"},
      "products":      [root_product_id, ...],   # THE ROSTER — ROOTS ONLY
      "action":        {"kind", "entries", ...}, # see build_purchase_action
    }

Locked roster / purchase-option invariants (option_of_id on shop_items rows;
the field is synthetic until the column lands — validators read it via .get()
so a missing column simply means "every row is a root"):

  * products[] is the Design's explicit ROOT-PRODUCT roster: only rows with
    option_of_id IS NULL may appear. A purchase-option row in products[] is
    rejected (option_in_roster) — options are never independent products.
  * An action entry may reference a purchase-option row only when that row's
    ROOT product is present in products[] (option_outside_roster otherwise).
  * product_select entries are ROOT-PRODUCT ONLY; the entry purchases that
    exact row (the product's default purchasable form).
  * option_select entries resolve to exactly ONE product family:
    root(e1) == root(e2) == ... — the root itself and/or its DIRECT option
    rows; option-of-option chains are rejected.
  * buttons entries may reference a root product or a purchase-option row.
  * There is no is_purchasable flag: a row is reachable through the shop only
    when a published Design action references it.

PURCHASE ACTIONS resolve to the EXISTING purchase mechanism — never a second
engine. Every entry's custom_id is `shop_buy_<row_id>` — the family
cogs/shop.py's on_interaction already dispatches to process_purchase(). A
select control carries `shop_buy_sel_<context_id>` and puts `shop_buy_<row_id>`
in its option values; the dispatch extension that reads those values lands
with the select-enablement step and routes into the SAME process_purchase().

FREE rendering is presentation-only (locked): a free row (zero price, no
diamond price — e.g. Prestige VI) renders its price slot as FREE_LABEL
(exact `𝐅𝐫𝐞𝐞` glyphs) and an empty button label defaults to exactly FREE_LABEL.
No purchase-flow or paid-product-validation change happens here — that is a
later isolated step. Paid-product validation (utils/shop_validation.py) is
untouched by this module.

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
from utils.discord_limits import (
    BUTTON_LABEL_MAX,
    CUSTOM_ID_MAX,
    SELECT_OPTION_DESCRIPTION_MAX,
    SELECT_OPTION_LABEL_MAX,
    SELECT_OPTIONS_MAX,
    SELECT_PLACEHOLDER_MAX,
)

# The exact Free glyphs (mathematical bold): U+1D405 U+1D42B U+1D41E U+1D41E
# (𝐅𝐫𝐞𝐞). ONE constant — the purchase-action label rule and the
# price_display token are the only writers of this string.
FREE_LABEL = "\U0001D405\U0001D42B\U0001D41E\U0001D41E"

# ── The fixed token catalog (insertion order = display order) ───────────────
# One entry per resolvable canonical name. The UI's "Insert Dynamic Field"
# menu renders exactly this (label + group), so an admin and the resolver can
# never disagree about what is available.
TOKEN_CATALOG = {
    "product.id": {
        "label": "ID", "group": "Product",
        "description": "The product's shop id (shop_items.id).",
    },
    "product.name": {
        "label": "Name", "group": "Product",
        "description": "The product name.",
    },
    "product.description": {
        "label": "Description", "group": "Product",
        "description": "The product description (empty when unset).",
    },
    "product.type": {
        "label": "Type", "group": "Product",
        "description": ("The stored item type (role, temp_role, xp_boost, "
                        "prestige, potion, title, custom)."),
    },
    "product.rarity": {
        "label": "Rarity", "group": "Product",
        "description": ("The item rarity (common, rare, epic, legendary, "
                        "mythical, secret)."),
    },
    "product.tier": {
        "label": "Prestige tier", "group": "Product",
        "description": ("The Prestige tier in roman numerals (I–VI), empty "
                        "for other items. Same value as {{product.prestige_tier}}."),
    },
    "product.prestige_tier": {
        "label": "Prestige tier", "group": "Product",
        "description": "The Prestige tier in roman numerals (I–VI), empty for other items.",
    },
    "product.duration": {
        "label": "Duration", "group": "Product",
        "description": "Duration in hours as text (e.g. \"72h\"), empty when permanent.",
    },
    "product.stock": {
        "label": "Stock", "group": "Product",
        "description": "Stock as text (e.g. \"12/20\"), empty when unlimited.",
    },
    "product.required_level": {
        "label": "Required level", "group": "Product",
        "description": "Required level as text, empty when there is no requirement.",
    },
    "product.image": {
        "label": "Image", "group": "Product",
        "description": ("The item icon URL, empty when unset. Same value as "
                        "{{product.icon_url}}."),
    },
    "product.icon_url": {
        "label": "Image URL", "group": "Product",
        "description": "The item icon URL, empty when unset.",
    },
    "product.price": {
        "label": "Price (amount)", "group": "Price",
        "description": ("The price amount in grouped digits (e.g. \"1,500\"), "
                        "\"0\" when free. Combine with {{product.currency}}."),
    },
    "product.price_display": {
        "label": "Price (full)", "group": "Price",
        "description": ("The all-in-one price line \"1,500 🪙 Coins\" — or "
                        "exactly 𝐅𝐫𝐞𝐞 for a free product."),
    },
    "product.price_amount": {
        "label": "Price (raw)", "group": "Price",
        "description": "The raw price number as plain digits.",
    },
    "product.currency": {
        "label": "Currency", "group": "Price",
        "description": ("Display name of the currency this product charges in "
                        "(e.g. \"Coins\"). Same value as {{product.currency_name}}."),
    },
    "product.currency_name": {
        "label": "Currency name", "group": "Price",
        "description": "Display name of the currency this product charges in.",
    },
    "product.currency_emoji": {
        "label": "Currency icon", "group": "Price",
        "description": "Icon of the currency this product charges in.",
    },
}

# The user-facing short spellings (locked V1 field list) are FIXED aliases of
# the canonical namespace — a lookup table, never a template language. The
# Insert Dynamic Field menu always inserts the canonical spelling.
TOKEN_ALIASES = {
    "name": "product.name",
    "description": "product.description",
    "duration": "product.duration",
    "stock": "product.stock",
    "price": "product.price",
    "currency": "product.currency",
    "image": "product.image",
    "type": "product.type",
    "rarity": "product.rarity",
    "tier": "product.tier",
}

# {{ name }} / {{product.name }} with optional inner whitespace. Deliberately
# NOT a template language: the pattern cannot express anything but a dotted
# identifier.
TOKEN_RE_PATTERN = r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}"
TOKEN_RE = re.compile(TOKEN_RE_PATTERN)

# V1 picker grouping reuses the existing `type` column only — the display
# order of the groups, matching the Shop admin form's type order. This is a
# transitional grouping until the Category model lands (Step 1); nothing here
# invents a category system.
TYPE_GROUP_ORDER = ("role", "temp_role", "xp_boost", "prestige", "potion",
                    "title", "custom")

# The two fixed presentation modes (server policy — NOT a template language):
#   per_product — the presentation's EMBEDS fan out once per roster product
#                 (product tokens resolve against that product). Message
#                 content resolves against the product only when the roster
#                 has exactly one product; otherwise product tokens in the
#                 content stay verbatim and are reported.
#   frame       — the presentation resolves ONCE as the shop frame; product
#                 tokens anywhere in it stay verbatim and are reported.
#                 The roster products are presented by the purchase action.
PRESENTATION_MODES = ("per_product", "frame")

ACTION_KINDS = ("buttons", "product_select", "option_select")


def type_group_key(item_type: str):
    """Sort key placing the known `type` groups first, in shop-form order."""
    try:
        return (0, TYPE_GROUP_ORDER.index(item_type), item_type)
    except ValueError:
        return (1, 0, item_type or "")


def canonical_token(key: str) -> str:
    """The catalog key a typed name resolves through (aliases included)."""
    if key in TOKEN_CATALOG:
        return key
    return TOKEN_ALIASES.get(key, "")


def token_catalog_payload() -> list:
    """The fixed catalog in display order, for the Insert Dynamic Field menu."""
    aliases_by_key = {}
    for alias, target in TOKEN_ALIASES.items():
        aliases_by_key.setdefault(target, []).append(alias)
    return [
        {
            "token": "{{" + key + "}}",
            "key": key,
            "label": entry["label"],
            "group": entry["group"],
            "description": entry["description"],
            "aliases": ["{{" + a + "}}" for a in aliases_by_key.get(key, [])],
        }
        for key, entry in TOKEN_CATALOG.items()
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
    price_diamonds = (product or {}).get("price_diamonds")
    if price_diamonds:
        return "diamonds", int(price_diamonds)
    return "balance", int((product or {}).get("price") or 0)


def is_free(product: dict) -> bool:
    """A free product: zero charge (no coin price, no diamond price)."""
    _column_key, amount = charging_amount(product)
    return amount == 0


def format_price(amount: int, currency_name: str, currency_emoji: str) -> str:
    """Mechanical price rendering — thousands separators + icon + name.

    Never special-cases zero: the FREE_LABEL presentation happens in
    display_price() and the purchase-action label rules, deliberately in
    exactly two places.
    """
    return f"{int(amount):,} {currency_emoji} {currency_name}"


def display_price(amount: int, currency_name: str, currency_emoji: str,
                  free: bool) -> str:
    """The all-in-one price line: exactly FREE_LABEL when free, else the
    mechanical format_price() form."""
    if free:
        return FREE_LABEL
    return format_price(amount, currency_name, currency_emoji)


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
    free = is_free(product)

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
        "product.type": str(product.get("type") or ""),
        "product.rarity": str(product.get("rarity") or "common"),
        "product.tier": tier_text,
        "product.prestige_tier": tier_text,
        "product.duration": f"{int(duration_hours)}h" if duration_hours else "",
        "product.stock": (
            f"{int(current_stock or 0)}/{int(max_stock)}" if max_stock else ""),
        "product.required_level": (
            str(int(required_level)) if required_level else ""),
        "product.image": str(product.get("icon_url") or ""),
        "product.icon_url": str(product.get("icon_url") or ""),
        "product.price": f"{int(amount):,}",
        "product.price_display": display_price(
            amount, currency_name, currency_emoji, free),
        "product.price_amount": str(amount),
        "product.currency": currency_name,
        "product.currency_name": currency_name,
        "product.currency_emoji": currency_emoji,
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


def resolve_string(text: str, path: str, values: dict, used: list) -> str:
    """Substitute catalog tokens through ONE documented text surface.

    Occurrences are appended to `used` in document order as
    {"token", "key", "canonical", "path", "resolved", "known", "value"}:
      resolved=False + known=True  -> a catalog token with no product context
                                      (left verbatim; reported)
      resolved=False + known=False -> an unknown name (left verbatim)
    """
    if not isinstance(text, str) or "{{" not in text:
        return text

    def repl(match):
        key = match.group(1)
        canonical = canonical_token(key)
        resolved = canonical in values if canonical else False
        value = values.get(canonical, "") if resolved else ""
        used.append({
            "token": match.group(0),
            "key": key,
            "canonical": canonical,
            "path": path,
            "resolved": resolved,
            "known": bool(canonical),
            "value": value if resolved else "",
        })
        # Unknown / context-less tokens stay verbatim; known values are
        # emitted once and never re-scanned (no nesting, no escalation).
        return value if resolved else match.group(0)

    return TOKEN_RE.sub(repl, text)


def resolve_message(template_doc, values: dict):
    """Substitute catalog tokens through the template's text surfaces.

    Returns (resolved_doc, used):
      resolved_doc — a NEW normalized message dict; the input is never mutated.
      used — one entry per token occurrence, in document order.
    """
    doc = normalize_template_doc(template_doc)
    used = []

    if isinstance(doc["content"], str):
        doc["content"] = resolve_string(doc["content"], "content", values, used)
    for i, embed in enumerate(doc["embeds"]):
        _embed_text_leaves(
            embed, f"embeds.{i}",
            lambda node, key, path: node.__setitem__(
                key, resolve_string(node[key], path, values, used)))
    return doc, used


# ── Purchase actions (config-driven; one existing engine) ───────────────────

DEFAULT_ENTRY_LABELS = {
    "buttons": "Buy {{product.name}}",
    "product_select": "{{product.name}}",
    "option_select": "{{product.name}} — {{product.price}} {{product.currency}}",
}

DEFAULT_PLACEHOLDERS = {
    "product_select": "Select…",
    "option_select": "Select…",
}


def root_of(row: dict) -> int:
    """The root product id of a row: its parent when it is a purchase-option
    row (option_of_id set), else its own id. The field is synthetic until the
    column lands — a missing key simply means the row is a root."""
    row = row or {}
    parent = row.get("option_of_id")
    return int(parent) if parent else int(row.get("id") or 0)


def is_option_row(row: dict) -> bool:
    """True for purchase-option rows (option_of_id set)."""
    return bool((row or {}).get("option_of_id"))


def entry_custom_id(row: dict) -> str:
    """The custom-id family cogs/shop.py already dispatches — for buttons the
    button's custom_id, for select options the option VALUE."""
    return f"shop_buy_{int((row or {}).get('id') or 0)}"


def build_purchase_action(action_cfg: dict, rows_by_id: dict, currency: dict,
                          context_id: int = 0, used: list = None) -> dict:
    """Action config + rows -> THE purchase action Phase 2+ publishes.

    Every entry resolves to the EXISTING `shop_buy_<row_id>` mechanism; a
    select control additionally carries `shop_buy_sel_<context_id>` as its
    component custom_id (its option values carry the row custom_ids). Labels
    and descriptions run through the same fixed resolver against the ENTRY's
    product (so `{{product.duration}} — {{product.price}} {{product.currency}}`
    is truthful per option).

    Free rows (locked presentation rule): an empty BUTTON label becomes
    exactly FREE_LABEL; an empty select label/description gets FREE_LABEL in
    the price slot (description). Token labels that use
    {{product.price_display}} render the same glyphs.

    Pass `used` to collect label occurrences into the shared token report.
    """
    cfg = action_cfg or {}
    kind = cfg.get("kind") if cfg.get("kind") in ACTION_KINDS else "buttons"
    entries_out = []

    for i, entry in enumerate(cfg.get("entries") or []):
        entry = entry if isinstance(entry, dict) else {}
        product_id = entry.get("product_id")
        row = rows_by_id.get(int(product_id)) if isinstance(product_id, int) \
            and not isinstance(product_id, bool) else None
        if row is None:
            continue
        values = product_token_values(row, currency)
        free = is_free(row)

        label_tpl = entry.get("label") or ""
        if not label_tpl:
            label_tpl = FREE_LABEL if (free and kind == "buttons") \
                else DEFAULT_ENTRY_LABELS[kind]
        desc_tpl = entry.get("description") or ""
        if not desc_tpl and free and kind != "buttons":
            desc_tpl = FREE_LABEL

        label = resolve_string(label_tpl, f"action.entries.{i}.label", values,
                               used if used is not None else [])
        description = resolve_string(
            desc_tpl, f"action.entries.{i}.description", values,
            used if used is not None else [])

        built = {
            "product_id": int(row.get("id") or 0),
            "custom_id": entry_custom_id(row),
            "label": label,
            "description": description,
            "emoji": str(entry.get("emoji") or ""),
            "style": str(entry.get("style") or ("green" if kind == "buttons" else "")),
            "free": free,
        }
        entries_out.append(built)

    action = {"kind": kind, "entries": entries_out}
    if kind == "buttons":
        action["style"] = "green"   # /shop's existing button convention
    else:
        action["component_custom_id"] = f"shop_buy_sel_{int(context_id or 0)}"
        placeholder_tpl = cfg.get("placeholder") or DEFAULT_PLACEHOLDERS[kind]
        action["placeholder"] = resolve_string(
            placeholder_tpl, "action.placeholder", {}, used if used is not None else [])
    return action


def validate_action_entries(action_cfg: dict, rows_by_id: dict,
                            roster_ids=None) -> list:
    """Structural + locked-invariant checks for one action config.

    Returns a list of {"code", "path", "message"} problems (empty = valid):
      unknown_action_kind, action_entry_count, invalid_product_ref,
      unknown_product, duplicate_entry, option_outside_roster,
      entry_not_in_roster, option_in_product_select, orphan_option,
      option_chain, option_select_multi_family.

    roster_ids (the Design's products[]) enforces the locked roster rule: an
    entry's ROOT product must be in the roster. Pass None to skip that rule.
    """
    problems = []
    cfg = action_cfg if isinstance(action_cfg, dict) else {}
    kind = cfg.get("kind")
    if kind not in ACTION_KINDS:
        return [{"code": "unknown_action_kind", "path": "action.kind",
                 "message": (f"Unknown purchase action kind {kind!r} — "
                             f"expected one of {', '.join(ACTION_KINDS)}.")}]

    entries = cfg.get("entries")
    if not isinstance(entries, list):
        entries = []
        problems.append({"code": "action_entry_count", "path": "action.entries",
                         "message": "action.entries must be a list."})

    if kind == "buttons":
        count_ok = 1 <= len(entries) <= 25
        count_message = "A button action needs 1–25 entries."
    else:
        count_ok = 2 <= len(entries) <= SELECT_OPTIONS_MAX
        count_message = "A select action needs 2–25 entries."
    if not count_ok:
        problems.append({"code": "action_entry_count", "path": "action.entries",
                         "message": count_message})

    seen = set()
    family_roots = []
    for i, entry in enumerate(entries):
        path = f"action.entries.{i}"
        entry = entry if isinstance(entry, dict) else {}
        product_id = entry.get("product_id")
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            problems.append({"code": "invalid_product_ref",
                             "path": f"{path}.product_id",
                             "message": "entry product_id must be a product id (an integer)."})
            continue
        row = rows_by_id.get(product_id)
        if row is None:
            problems.append({"code": "unknown_product", "path": f"{path}.product_id",
                             "message": f"No product {product_id} in this guild."})
            continue
        if product_id in seen:
            problems.append({"code": "duplicate_entry", "path": f"{path}.product_id",
                             "message": f"Product {product_id} is already an entry of this action."})
        seen.add(product_id)

        root = root_of(row)
        family_roots.append(root)

        # Locked roster rule: the entry's ROOT must be in the Design roster.
        if roster_ids is not None and root not in roster_ids:
            if is_option_row(row):
                problems.append({
                    "code": "option_outside_roster", "path": path,
                    "message": (f"Purchase-option row {product_id} belongs to root "
                                f"product {root}, which is not in products[] — "
                                "add the root product to the roster first."),
                })
            else:
                problems.append({
                    "code": "entry_not_in_roster", "path": path,
                    "message": (f"Product {product_id} is not in the Design's "
                                "products[] roster."),
                })

        if kind == "option_select" and not is_option_row(row):
            problems.append({
                "code": "root_in_option_select", "path": path,
                "message": ("option_select entries must be direct options of the "
                            "selected root; the root itself cannot be an entry."),
            })

        if is_option_row(row):
            parent = rows_by_id.get(root)
            if parent is None:
                problems.append({"code": "orphan_option", "path": path,
                                 "message": (f"Purchase-option row {product_id} points "
                                             f"at root {root}, which does not exist.")})
            elif is_option_row(parent):
                problems.append({"code": "option_chain", "path": path,
                                 "message": ("Purchase options must sit directly under "
                                             "a root product — option-of-option chains "
                                             "are not allowed.")})
            if kind == "product_select":
                problems.append({
                    "code": "option_in_product_select", "path": path,
                    "message": ("product_select entries must be ROOT products "
                                "(option_of_id IS NULL) — purchase-option rows are "
                                "not independent products."),
                })

    if kind == "option_select" and family_roots:
        first = family_roots[0]
        for i, root in enumerate(family_roots):
            if root != first:
                problems.append({
                    "code": "option_select_multi_family",
                    "path": f"action.entries.{i}",
                    "message": (f"option_select entries must all belong to ONE product "
                                f"family (root {first}); entry {i} resolves to root {root}."),
                })
    return problems


def validate_design(design: dict, rows_by_id: dict) -> list:
    """The locked Design-draft contract (Step 0). Empty list = valid.

    products[] is the ROOT-PRODUCT roster: only rows with option_of_id IS NULL
    may appear; a purchase-option row there is rejected (option_in_roster).
    Action entries then follow validate_action_entries()' rules, including
    "an option row may appear in entries only when its root is in products[]".
    """
    problems = []
    design = design if isinstance(design, dict) else {}
    products = design.get("products")
    if not isinstance(products, list) or not products:
        problems.append({"code": "empty_roster", "path": "products",
                         "message": "products[] must list at least one root product."})
        products = []

    roster_ids = set()
    for i, pid in enumerate(products):
        path = f"products[{i}]"
        if isinstance(pid, bool) or not isinstance(pid, int):
            problems.append({"code": "invalid_product_ref", "path": path,
                             "message": "products[] entries must be product ids (integers)."})
            continue
        if pid in roster_ids:
            problems.append({"code": "duplicate_product", "path": path,
                             "message": "products[] must not contain duplicate root product ids."})
            continue
        row = rows_by_id.get(pid)
        if row is None:
            problems.append({"code": "unknown_product", "path": path,
                             "message": f"No product {pid} in this guild."})
            continue
        if is_option_row(row):
            problems.append({
                "code": "option_in_roster", "path": path,
                "message": (f"products[] is the ROOT-product roster — purchase-option "
                            f"row {pid} cannot appear as an independent product "
                            f"(it belongs to root {root_of(row)})."),
            })
            continue
        roster_ids.add(pid)

    problems.extend(validate_action_entries(
        design.get("action"), rows_by_id, roster_ids))
    return problems


# ── Preview warnings (deterministic, pathed) ────────────────────────────────

def _state_warnings(rows: list) -> list:
    """Per-product state warnings (disabled / out of stock), once per distinct
    product, in first-appearance order."""
    warnings = []
    seen = set()
    for row in rows:
        row = row or {}
        pid = int(row.get("id") or 0)
        if pid in seen:
            continue
        seen.add(pid)
        if not row.get("enabled", 1):
            warnings.append({
                "code": "product_disabled", "path": f"products[{pid}]",
                "message": (f"Product {row.get('name') or pid} is disabled — the "
                            "purchase action would refuse purchases."),
            })
        max_stock = row.get("max_stock")
        if max_stock and not row.get("current_stock"):
            warnings.append({
                "code": "product_out_of_stock", "path": f"products[{pid}]",
                "message": (f"Product {row.get('name') or pid} is out of stock "
                            f"(0/{int(max_stock)}) — the purchase action would "
                            "refuse purchases."),
            })
    return warnings


def _occurrence_warnings(used: list) -> list:
    warnings = []
    for entry in used:
        if not entry["resolved"]:
            if entry.get("known"):
                warnings.append({
                    "code": "unresolved_product_token", "path": entry["path"],
                    "message": (f"Token {entry['token']} at {entry['path']} needs a "
                                "product context — this surface is not rendered per "
                                "product (frame mode / shared content). It was left "
                                "as typed."),
                })
            else:
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
    return warnings


def _action_limit_warnings(action: dict) -> list:
    """Discord component limits (from the ONE limits table) on the BUILT
    action — publish blockers later, warnings now."""
    warnings = []
    kind = action.get("kind")
    label_max = BUTTON_LABEL_MAX if kind == "buttons" else SELECT_OPTION_LABEL_MAX
    for i, entry in enumerate(action.get("entries") or []):
        path = f"action.entries.{i}"
        if len(entry.get("label") or "") > label_max:
            warnings.append({"code": "action_limit", "path": f"{path}.label",
                             "message": (f"Entry label is {len(entry['label'])} "
                                         f"characters; Discord's limit is {label_max}.")})
        if len(entry.get("description") or "") > SELECT_OPTION_DESCRIPTION_MAX:
            warnings.append({
                "code": "action_limit", "path": f"{path}.description",
                "message": (f"Entry description is {len(entry['description'])} "
                            f"characters; Discord's limit is "
                            f"{SELECT_OPTION_DESCRIPTION_MAX}.")})
        if len(entry.get("custom_id") or "") > CUSTOM_ID_MAX:
            warnings.append({"code": "action_limit", "path": f"{path}.custom_id",
                             "message": "custom_id exceeds Discord's limit."})
    if kind != "buttons":
        placeholder = action.get("placeholder") or ""
        if len(placeholder) > SELECT_PLACEHOLDER_MAX:
            warnings.append({"code": "action_limit", "path": "action.placeholder",
                             "message": (f"Placeholder is {len(placeholder)} "
                                         f"characters; Discord's limit is "
                                         f"{SELECT_PLACEHOLDER_MAX}.")})
    return warnings


def collect_warnings(state_rows: list, used: list, resolved_doc: dict,
                     action: dict = None, extra_limits: list = None) -> list:
    """Deterministic, pathed preview warnings. Order is fixed:

    1. product state (disabled, out of stock) per distinct product,
    2. token occurrences in recorded order (unknown token, unresolved product
       token, empty value) — presentation surfaces first, then action labels,
    3. message/action limit breaches (presentation_limit, action_limit),
    4. Discord-rule breaches of the RESOLVED payload (utils/embed_schema),
       which the publish path must treat as blockers — surfaced here as
       warnings because there is no publish yet.
    """
    warnings = []
    warnings.extend(_state_warnings(state_rows))
    warnings.extend(_occurrence_warnings(used))
    if extra_limits:
        warnings.extend(extra_limits)
    if action:
        warnings.extend(_action_limit_warnings(action))

    _ok, errors = validate_message(resolved_doc, [])
    for error in errors:
        warnings.append({
            "code": "validation", "path": error.get("path") or "",
            "message": error.get("message") or "Invalid message payload.",
        })
    return warnings


# ── Design preview (the ONE assembly the publish path reuses) ───────────────

def preview_design(design: dict, rows_by_id: dict, currency: dict) -> dict:
    """Design draft + rows -> resolved presentation + purchase action.

    Returns the exact dict the publish path must reuse:
      {mode, content, embeds, action, warnings, tokens}

    Fixed mode policy (server policy, NOT a template language):
      per_product — embeds fan out once per roster product (paths
                    products.<i>.… for >1 product); content resolves against
                    the product only for a single-product roster.
      frame       — the presentation resolves once with no product context.
    """
    design = design if isinstance(design, dict) else {}
    presentation = design.get("presentation")
    presentation = presentation if isinstance(presentation, dict) else {}
    mode = presentation.get("mode")
    if mode not in PRESENTATION_MODES:
        mode = "per_product"
    # `mode` is draft metadata, not message content: strip it before the
    # template normalization so a legacy bare-embed presentation (neither
    # "content" nor "embeds" key) still becomes a one-embed message.
    doc_src = {k: v for k, v in presentation.items() if k != "mode"}
    doc = normalize_template_doc(doc_src)

    roster = [pid for pid in (design.get("products") or [])
              if isinstance(pid, int) and not isinstance(pid, bool)
              and pid in rows_by_id]
    used = []
    extra_limits = []

    if mode == "frame":
        resolved, frame_used = resolve_message(doc, {})
        used.extend(frame_used)
        content, embeds = resolved["content"], resolved["embeds"]
    elif len(roster) == 1:
        values = product_token_values(rows_by_id[roster[0]], currency)
        resolved, single_used = resolve_message(doc, values)
        used.extend(single_used)
        content, embeds = resolved["content"], resolved["embeds"]
    else:
        # per_product fan-out: shared content without product context, one
        # resolved card per roster product (order preserved).
        resolved_content, content_used = resolve_message(
            {"content": doc["content"], "embeds": []}, {})
        used.extend(content_used)
        content = resolved_content["content"]
        embeds = []
        for i, pid in enumerate(roster):
            values = product_token_values(rows_by_id[pid], currency)
            card, card_used = resolve_message(
                {"content": "", "embeds": doc["embeds"]}, values)
            for occurrence in card_used:
                occurrence["path"] = f"products.{i}." + occurrence["path"]
            used.extend(card_used)
            embeds.extend(card["embeds"])
        if len(embeds) > 10:
            extra_limits.append({
                "code": "presentation_limit", "path": "embeds",
                "message": (f"The fan-out renders {len(embeds)} embeds; a Discord "
                            "message allows up to 10."),
            })

    action = build_purchase_action(
        design.get("action"), rows_by_id, currency,
        context_id=design.get("id") or 0, used=used)

    state_rows = [rows_by_id[pid] for pid in roster]
    entry_ids = [e["product_id"] for e in action["entries"]]
    state_rows.extend(rows_by_id[pid] for pid in entry_ids if pid not in roster)

    values_out = {}
    if len(roster) == 1:
        values_out = product_token_values(rows_by_id[roster[0]], currency)

    return {
        "mode": mode,
        "content": content,
        "embeds": embeds,
        "action": action,
        "warnings": collect_warnings(state_rows, used,
                                     {"content": content, "embeds": embeds},
                                     action, extra_limits),
        "tokens": {"values": values_out, "used": used},
    }


def preview_message(template_doc, product: dict, currency: dict) -> dict:
    """Single-product convenience over preview_design() — kept so the
    "preview == publish" identity has one obvious per-product call shape."""
    product = product or {}
    pid = int(product.get("id") or 0)
    design = {
        "id": 0,
        "presentation": {"mode": "per_product", **normalize_template_doc(template_doc)},
        "products": [pid],
        "action": {"kind": "buttons", "entries": [{"product_id": pid}]},
    }
    return preview_design(design, {pid: product}, currency)
