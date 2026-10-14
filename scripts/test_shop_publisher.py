#!/usr/bin/env python3
"""
Shop Publisher — Step 0: the FIXED token resolver + design-draft contract
(utils/shop_publisher.py).

What this locks down
  1.  The token catalog is FIXED: one exact set of canonical names (with
      friendly labels/groups for the Insert Dynamic Field menu) plus one exact
      set of user-facing aliases — rendered for the UI from the same table the
      resolver uses.
  2.  Resolution is DETERMINISTIC: same draft + rows + currency →
      byte-identical output; the input document is never mutated; repeating
      the resolution never differs.
  3.  Resolution is NON-PROGRAMMABLE: bare lookups only — no nesting, no
      expressions, resolved values are never re-scanned, and anything outside
      the catalog (or its alias table) stays verbatim and is reported.
  4.  The preview shows the resolved presentation PLUS the purchase action
      that will actually be published — entries whose custom_ids are the
      EXISTING `shop_buy_<id>` family, and select kinds whose option values
      carry the same family.
  5.  The Design-draft roster contract (locked): products[] is the ROOT-PRODUCT
      roster — a purchase-option row (option_of_id set) in products[] is
      rejected; an option row may appear in action.entries only when its root
      is in products[]; product_select is root-only; option_select is
      one-family; buttons may reference roots or their options. These run on
      SYNTHETIC rows (option_of_id is a plain dict key here; the DB column
      lands with the Category step).
  6.  Free rendering is presentation-only: zero price renders mechanically in
      {{product.price}} ("0"), while {{product.price_display}} and empty
      action labels render the exact 𝐅𝐫𝐞𝐞 glyphs. Paid-product validation is
      untouched.

Run:  python3 scripts/test_shop_publisher.py
No Flask, no Discord, no network. The resolver is pure; the one non-stdlib
touch is the READ-ONLY utils.prestige.tier_label lookup (lazy-imported, only
for products that carry a prestige tier), so this harness sets the same scratch
environment phase1_support.py uses and never opens a database.
"""

import os
import sys

# Scratch-only environment, before anything imports database.py through
# utils/prestige.py (same contract as scripts/phase1_support.py).
os.environ.setdefault("OWNER_ID", "999999999")
os.environ.setdefault("DATABASE_PATH", os.devnull + "/nilive_test_no_db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.shop_publisher as SP  # noqa: E402

_passed = 0
_failed = 0
_failures: list[str] = []


def check(cond, name, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS {name}")
    else:
        _failed += 1
        _failures.append(name + (f" — {extra}" if extra else ""))
        print(f"  FAIL {name} {extra}")


def section(title):
    print(f"\n== {title} ==")


CURRENCY = {
    "coins": {"key": "balance", "name": "Coins", "emoji": "🪙"},
    "diamonds": {"key": "diamonds", "name": "Diamonds", "emoji": "💎"},
}

PRODUCT = {
    "id": 12, "name": "VIP Role", "description": "The VIP role.",
    "price": 1500, "price_diamonds": None, "type": "role",
    "duration_hours": None, "max_stock": 20, "current_stock": 12,
    "required_level": 5, "rarity": "epic", "icon_url": "https://x/i.png",
    "prestige_tier": None, "enabled": 1, "featured": 1,
}


def product(**over):
    row = dict(PRODUCT)
    row.update(over)
    return row


def template():
    return {
        "content": "Buy {{product.name}} for {{product.price_display}}!",
        "embeds": [{
            "title": "{{product.name}}",
            "description": "{{product.description}}",
            "color": 0x7C5CBF,
            "fields": [
                {"name": "Type", "value": "{{product.type}}"},
                {"name": "Stock", "value": "{{product.stock}}"},
            ],
            "footer": {"text": "Rarity: {{product.rarity}}"},
        }],
    }


# ── A. The fixed catalog ───────────────────────────────────────────────────
def catalog_tests():
    section("A. The token catalog is fixed")
    expected = {
        "product.id", "product.name", "product.description", "product.price",
        "product.price_display", "product.price_amount", "product.currency",
        "product.currency_name", "product.currency_emoji", "product.type",
        "product.duration", "product.stock", "product.required_level",
        "product.rarity", "product.image", "product.icon_url", "product.tier",
        "product.prestige_tier",
    }
    check(set(SP.TOKEN_CATALOG) == expected,
          "catalog holds exactly the canonical token set",
          str(set(SP.TOKEN_CATALOG) ^ expected))
    payload = SP.token_catalog_payload()
    check([e["key"] for e in payload] == list(SP.TOKEN_CATALOG),
          "UI catalog payload preserves the fixed display order")
    check(all(e["token"] == "{{" + e["key"] + "}}" for e in payload),
          "every catalog entry exposes its exact token spelling")
    check(all(e["description"] and e["label"] and e["group"] for e in payload),
          "every catalog entry is documented with a label and a group")
    check({e["group"] for e in payload} == {"Product", "Price"},
          "the Insert Dynamic Field menu has the two locked groups")

    expected_aliases = {
        "name", "description", "duration", "stock", "price", "currency",
        "image", "type", "rarity", "tier",
    }
    check(set(SP.TOKEN_ALIASES) == expected_aliases,
          "the user-facing short spellings are exactly the locked field list",
          str(set(SP.TOKEN_ALIASES) ^ expected_aliases))
    check(all(SP.canonical_token(a) == t for a, t in SP.TOKEN_ALIASES.items()),
          "every alias maps to its canonical catalog name")
    check(all(SP.canonical_token(a) in SP.TOKEN_CATALOG for a in SP.TOKEN_ALIASES),
          "every alias target exists in the catalog")
    alias_payload = {a for e in payload for a in e["aliases"]}
    check(alias_payload == {"{{" + a + "}}" for a in expected_aliases},
          "the catalog payload lists every alias for the insert menu")

    values = SP.product_token_values(product(), CURRENCY)
    check(set(values) == expected,
          "the resolver produces a value for every catalog token and nothing else")


# ── B. Determinism ─────────────────────────────────────────────────────────
def determinism_tests():
    section("B. Resolution is deterministic and pure")
    doc = template()
    import json
    before = json.dumps(doc, sort_keys=True)
    one = SP.preview_message(doc, product(), CURRENCY)
    again = SP.preview_message(doc, product(), CURRENCY)
    check(json.dumps(one, sort_keys=True) == json.dumps(again, sort_keys=True),
          "two runs over the same inputs are byte-identical")
    check(json.dumps(doc, sort_keys=True) == before,
          "the input template document is never mutated")
    check(one["embeds"][0]["title"] == "VIP Role", "title resolves")
    check(one["content"] == "Buy VIP Role for 1,500 🪙 Coins!",
          "content resolves with the full price display", one["content"])
    check(one["embeds"][0]["fields"][0]["value"] == "role", "field values resolve")
    check(one["embeds"][0]["footer"]["text"] == "Rarity: epic", "footer resolves")


# ── C. Non-programmable ────────────────────────────────────────────────────
def non_programmable_tests():
    section("C. Non-programmable: lookups only")
    resolved, used = SP.resolve_message(
        {"content": "{{ 1 + 1 }} {{product.name}} {{missing.thing}} {{}} {{ name }}"},
        SP.product_token_values(product(), CURRENCY))
    check(resolved["content"].startswith("{{ 1 + 1 }} "),
          "expressions are not evaluated — left verbatim", resolved["content"])
    check("VIP Role" in resolved["content"], "whitespace-tolerant known token resolves")
    check(resolved["content"].count("VIP Role") == 2,
          "canonical and alias spellings both resolve", resolved["content"])
    check("{{missing.thing}}" in resolved["content"], "unknown token stays verbatim")
    check(resolved["content"].count("{{}}") == 1 and " {{}} " in resolved["content"],
          "an empty brace group is not a token — left verbatim", resolved["content"])
    check([u["key"] for u in used if not u["known"]] == ["missing.thing"],
          "only the unknown dotted name is unknown", str(used))
    check([u["canonical"] for u in used if u["key"] == "name"] == ["product.name"],
          "the alias occurrence records its canonical catalog name")

    injection = SP.resolve_message(
        {"content": "{{product.name}}"},
        SP.product_token_values(product(name="{{product.id}} 7"), CURRENCY))
    check(injection[0]["content"] == "{{product.id}} 7",
          "a resolved value containing braces is never re-scanned (no nesting/injection)")
    check(len(injection[1]) == 1, "exactly one occurrence recorded for one token")

    resolved, used = SP.resolve_message(
        {"content": "{{product.name}}{{name}}{{product.name}}"},
        SP.product_token_values(product(), CURRENCY))
    check(resolved["content"] == "VIP RoleVIP RoleVIP Role" and len(used) == 3,
          "repeated tokens each resolve and each are recorded")

    # Documented surfaces only — unknown embed keys pass through untouched.
    resolved, used = SP.resolve_message(
        {"embeds": [{"title": "{{product.name}}", "editorOnly": "{{product.id}}"}]},
        SP.product_token_values(product(), CURRENCY))
    check(resolved["embeds"][0]["editorOnly"] == "{{product.id}}",
          "unknown embed keys are not interpreted")
    check(len(used) == 1 and used[0]["path"] == "embeds.0.title",
          "only documented text surfaces are walked", str(used))

    # Known catalog name with NO product context: verbatim + classified.
    resolved, used = SP.resolve_message({"content": "{{product.name}}"}, {})
    check(resolved["content"] == "{{product.name}}",
          "a catalog token without product context stays verbatim")
    check(used[0]["resolved"] is False and used[0]["known"] is True,
          "it is recorded as known-but-unresolved, not unknown")


# ── D. Price tokens + Free presentation ────────────────────────────────────
def price_tests():
    section("D. Price tokens — amount / display split, mechanical Free on price")
    values = SP.product_token_values(product(), CURRENCY)
    check(values["product.price"] == "1,500", "price is the grouped amount only",
          values["product.price"])
    check(values["product.price_display"] == "1,500 🪙 Coins",
          "price_display is the all-in-one price line", values["product.price_display"])
    check(values["product.price_amount"] == "1500", "raw amount is plain digits")
    check(values["product.currency"] == "Coins" and values["product.currency_name"] == "Coins",
          "currency follows the config ({{product.currency}} = name)")
    check(values["product.currency_emoji"] == "🪙", "currency icon follows the config")

    values = SP.product_token_values(product(price_diamonds=25), CURRENCY)
    check(values["product.price"] == "25", "a diamond price takes precedence",
          values["product.price"])
    check(values["product.price_display"] == "25 💎 Diamonds",
          "price_display switches to the diamond currency", values["product.price_display"])
    check(values["product.currency"] == "Diamonds", "charging currency switches to diamonds")

    renamed = {
        "coins": {"key": "balance", "name": "Moons", "emoji": "<:moon:1>"},
        "diamonds": {"key": "diamonds", "name": "Stars", "emoji": "<:star:2>"},
    }
    values = SP.product_token_values(product(), renamed)
    check(values["product.price_display"] == "1,500 <:moon:1> Moons",
          "custom currency display flows through", values["product.price_display"])

    # Free rendering (locked): price stays mechanical ("0"), price_display is
    # exactly FREE_LABEL; the glyphs themselves are pinned here.
    check(SP.FREE_LABEL == "\U0001D405\U0001D42B\U0001D41E\U0001D41E" and
          SP.FREE_LABEL == "𝐅𝐫𝐞𝐞",
          "FREE_LABEL is exactly the locked 𝐅𝐫𝐞𝐞 glyphs")
    values = SP.product_token_values(product(price=0, price_diamonds=None), CURRENCY)
    check(values["product.price"] == "0",
          "zero price renders the mechanical amount in {{product.price}}",
          values["product.price"])
    check(values["product.price_display"] == SP.FREE_LABEL,
          "zero price renders exactly 𝐅𝐫𝐞𝐞 in {{product.price_display}}",
          repr(values["product.price_display"]))
    check(SP.is_free(product(price=0)) and not SP.is_free(product()) and
          not SP.is_free(product(price=0, price_diamonds=5)),
          "is_free matches the charging rule (diamond price makes it paid)")


# ── E. Purchase actions (config-driven, one engine) ────────────────────────
def purchase_action_tests():
    section("E. The purchase action resolves to the existing mechanism")
    rows = {12: product()}
    action = SP.build_purchase_action(
        {"kind": "buttons", "entries": [{"product_id": 12}]}, rows, CURRENCY)
    check(action["kind"] == "buttons", "kind is echoed")
    check(action["entries"] == [{
        "product_id": 12, "custom_id": "shop_buy_12", "label": "Buy VIP Role",
        "description": "", "emoji": "", "style": "green", "free": False,
    }], "a direct-buy button is exactly the descriptor the publish step uses",
        str(action))
    check(action["entries"][0]["custom_id"].startswith("shop_buy_"),
          "custom_id is the family cogs/shop.py's on_interaction already dispatches")

    # Token labels resolve against the ENTRY's product.
    action = SP.build_purchase_action({
        "kind": "buttons",
        "entries": [{"product_id": 12,
                     "label": "Buy {{name}} — {{price}} {{currency}}",
                     "emoji": "⚔️", "style": "blue"}],
    }, rows, CURRENCY)
    entry = action["entries"][0]
    check(entry["label"] == "Buy VIP Role — 1,500 Coins",
          "entry labels resolve tokens (canonical + short spellings)", entry["label"])
    check(entry["emoji"] == "⚔️" and entry["style"] == "blue",
          "per-entry emoji and style pass through")

    # Select kinds: one renderer, option VALUES in the shop_buy_ family.
    family = {
        101: product(id=101, name="Health Potion", price=1500, duration_hours=None),
        102: product(id=102, name="Health Potion — 7 Days", price=500,
                     duration_hours=168, option_of_id=101),
        103: product(id=103, name="Health Potion — 30 Days", price=1000,
                     duration_hours=720, option_of_id=101),
    }
    action = SP.build_purchase_action({
        "kind": "option_select", "placeholder": "Select duration",
        "entries": [{"product_id": 102, "label": "7 Days"},
                    {"product_id": 103, "label": "30 Days"}],
    }, family, CURRENCY, context_id=4)
    check(action["kind"] == "option_select" and
          action["component_custom_id"] == "shop_buy_sel_4",
          "a select carries shop_buy_sel_<context> as its component custom_id")
    check([e["custom_id"] for e in action["entries"]] == ["shop_buy_102", "shop_buy_103"],
          "every option VALUE is an existing shop_buy_<id> purchase handle")
    check(action["placeholder"] == "Select duration", "the placeholder passes through")

    action = SP.build_purchase_action({
        "kind": "product_select",
        "entries": [{"product_id": 12}, {"product_id": 12}],
    }, rows, CURRENCY)
    check(action["entries"][0]["label"] == "VIP Role",
          "product_select defaults to the product name", str(action["entries"][0]))
    check(action["entries"][0]["description"] == "",
          "a paid entry has no free marker")

    # Free labels (locked): empty button label → exactly 𝐅𝐫𝐞𝐞; select entry
    # gets 𝐅𝐫𝐞𝐞 in the price slot (description).
    free_row = product(id=3, name="Welcome Potion", price=0, type="potion")
    action = SP.build_purchase_action(
        {"kind": "buttons", "entries": [{"product_id": 3}]}, {3: free_row}, CURRENCY)
    check(action["entries"][0]["label"] == SP.FREE_LABEL == "𝐅𝐫𝐞𝐞",
          "a free button with no label publishes exactly 𝐅𝐫𝐞𝐞",
          repr(action["entries"][0]["label"]))
    action = SP.build_purchase_action(
        {"kind": "product_select", "entries": [{"product_id": 3}, {"product_id": 12}]},
        {3: free_row, 12: product()}, CURRENCY)
    check(action["entries"][0]["description"] == SP.FREE_LABEL,
          "a free select entry shows 𝐅𝐫𝐞𝐞 in the price slot",
          repr(action["entries"][0]["description"]))
    check(action["entries"][0]["label"] == "Welcome Potion",
          "the free entry keeps its name as the label")

    # The preview's action IS the builder's action (preview == publish).
    preview = SP.preview_message(template(), product(), CURRENCY)
    check(preview["action"] == SP.build_purchase_action(
        {"kind": "buttons", "entries": [{"product_id": 12}]}, {12: product()}, CURRENCY),
        "the preview's purchase action IS build_purchase_action(draft)")


# ── F. Token value edges ───────────────────────────────────────────────────
def value_tests():
    section("F. Token values on real product shapes")
    values = SP.product_token_values(product(), CURRENCY)
    check(values["product.id"] == "12", "id resolves")
    check(values["product.duration"] == "", "no duration → empty")
    check(values["product.stock"] == "12/20", "finite stock renders current/max")
    check(values["product.required_level"] == "5", "required level renders")
    check(values["product.prestige_tier"] == "" and values["product.tier"] == "",
          "no tier → empty")

    values = SP.product_token_values(product(duration_hours=72, max_stock=None,
                                             current_stock=None, required_level=0), CURRENCY)
    check(values["product.duration"] == "72h", "duration renders with h suffix")
    check(values["product.stock"] == "", "unlimited stock → empty")
    check(values["product.required_level"] == "", "no requirement → empty")

    values = SP.product_token_values(product(prestige_tier=4), CURRENCY)
    check(values["product.tier"] == "IV" and values["product.prestige_tier"] == "IV",
          "prestige tier reuses the single roman label source (read-only)",
          values["product.tier"])

    values = SP.product_token_values(product(description=None, icon_url=None, rarity=None), CURRENCY)
    check(values["product.description"] == "", "absent description → empty, not 'None'")
    check(values["product.image"] == "" and values["product.icon_url"] == "",
          "absent icon → empty on both spellings")
    check(values["product.rarity"] == "common", "absent rarity falls back to common")

    values = SP.product_token_values({**product(), "id": 3, "name": "VI", "price": 0,
                                      "type": "prestige", "prestige_tier": 6}, CURRENCY)
    check(values["product.tier"] == "VI", "tier VI renders as VI")
    check(values["product.price_display"] == SP.FREE_LABEL,
          "tier VI's price_display is 𝐅𝐫𝐞𝐞 (the existing free product)")


# ── G. Warnings ────────────────────────────────────────────────────────────
def warning_tests():
    section("G. Preview warnings (deterministic, pathed)")
    preview = SP.preview_message(template(), product(), CURRENCY)
    check(preview["warnings"] == [], "a clean preview raises no warnings",
          str(preview["warnings"]))

    preview = SP.preview_message(template(), product(enabled=0), CURRENCY)
    check([w["code"] for w in preview["warnings"]] == ["product_disabled"],
          "a disabled product warns exactly once", str(preview["warnings"]))

    preview = SP.preview_message(template(), product(current_stock=0), CURRENCY)
    codes = [w["code"] for w in preview["warnings"]]
    check(codes == ["product_out_of_stock"], "an out-of-stock product warns", str(codes))
    check("0/20" in preview["warnings"][0]["message"], "the warning carries the stock line")

    preview = SP.preview_message(
        {"content": "{{nope}} {{product.description}}"},
        product(description=None), CURRENCY)
    codes = [w["code"] for w in preview["warnings"]]
    check(codes == ["unknown_token", "empty_value"],
          "occurrence warnings fire in document order", str(codes))
    check(preview["warnings"][0]["path"] == "content" and
          preview["warnings"][1]["path"] == "content", "occurrence warnings carry their path")

    preview = SP.preview_message(
        {"embeds": [{"title": "x" * 300}]}, product(), CURRENCY)
    codes = [w["code"] for w in preview["warnings"]]
    check(codes == ["validation"], "a resolved payload over a Discord limit warns", str(codes))
    check(preview["warnings"][0]["path"] == "embeds.0.title",
          "limit warnings keep Discord's field path", str(preview["warnings"][0]))

    # Fixed order: product state → occurrences → limits → payload validation.
    preview = SP.preview_message(
        {"content": "{{nope}}", "embeds": [{"title": "x" * 300}]},
        product(enabled=0, current_stock=0), CURRENCY)
    codes = [w["code"] for w in preview["warnings"]]
    check(codes == ["product_disabled", "product_out_of_stock", "unknown_token", "validation"],
          "warning order is the documented deterministic order", str(codes))

    # Frame mode: product tokens need a product context → the dedicated code.
    draft = {
        "presentation": {"mode": "frame",
                         "content": "Welcome to the shop! {{product.name}}"},
        "products": [12],
        "action": {"kind": "buttons", "entries": [{"product_id": 12}]},
    }
    preview = SP.preview_design(draft, {12: product()}, CURRENCY)
    codes = [w["code"] for w in preview["warnings"]]
    check(codes == ["unresolved_product_token"],
          "frame-mode product tokens warn with the dedicated code", str(codes))
    check("{{product.name}}" in preview["content"],
          "the unresolved token stays verbatim in the frame")

    # Action label occurrences join the same deterministic report.
    draft = {
        "presentation": {"mode": "per_product", "content": "x", "embeds": []},
        "products": [12],
        "action": {"kind": "buttons",
                   "entries": [{"product_id": 12, "label": "Buy {{nope}}"}]},
    }
    preview = SP.preview_design(draft, {12: product()}, CURRENCY)
    check([w["code"] for w in preview["warnings"]] == ["unknown_token"],
          "action labels feed the occurrence warnings")
    check(preview["warnings"][0]["path"] == "action.entries.0.label",
          "action occurrences carry their path", str(preview["warnings"][0]))


# ── H. Legacy template normalization ──────────────────────────────────────
def normalize_tests():
    section("H. Template normalization (legacy rows keep working)")
    legacy = {"title": "Only an embed", "description": "old row"}
    doc = SP.normalize_template_doc(legacy)
    check(doc == {"content": "", "embeds": [{"title": "Only an embed", "description": "old row"}]},
          "a legacy bare-embed row becomes a one-embed message")
    preview = SP.preview_message(legacy, product(), CURRENCY)
    check(preview["embeds"][0]["title"] == "Only an embed",
          "legacy rows preview correctly end to end")
    check(SP.normalize_template_doc(None) == {"content": "", "embeds": []},
          "garbage rows normalize to an empty message")
    check(SP.normalize_template_doc({"content": "hi", "embeds": [1, "x", {"ok": 1}]}) ==
          {"content": "hi", "embeds": [{"ok": 1}]},
          "non-dict embeds drop out; content survives")


# ── I. The locked roster / purchase-option contract (synthetic rows) ───────
def roster_tests():
    section("I. Design roster + action-entry contract (synthetic option_of_id)")

    root = product(id=101, name="Health Potion", price=1500)
    opt7 = product(id=102, name="Health Potion", price=500,
                   duration_hours=168, option_of_id=101)
    opt30 = product(id=103, name="Health Potion", price=1000,
                    duration_hours=720, option_of_id=101)
    rows = {101: root, 102: opt7, 103: opt30}

    def codes(problems):
        return [p["code"] for p in problems]

    # ROOT-ONLY ROSTER: an option row in products[] is rejected outright.
    draft = {"products": [102],
             "action": {"kind": "buttons", "entries": [{"product_id": 102}]}}
    problems = SP.validate_design(draft, rows)
    check("option_in_roster" in codes(problems),
          "products: [option_id] is rejected — options are not roster products",
          str(problems))
    check(problems[0]["path"] == "products[0]", "the rejection is pathed")

    # An option entry needs its ROOT in the roster.
    draft = {"products": [101],
             "action": {"kind": "option_select",
                        "entries": [{"product_id": 102}, {"product_id": 103}]}}
    check(SP.validate_design(draft, rows) == [],
          "direct options are admissible when their root is in products[]")
    draft = {"products": [101],
             "action": {"kind": "option_select",
                        "entries": [{"product_id": 101}, {"product_id": 102}]}}
    check("root_in_option_select" in codes(SP.validate_design(draft, rows)),
          "the root itself is forbidden as an Option Select entry")

    draft = {"products": [201], "action": {"kind": "buttons",
                                           "entries": [{"product_id": 102}]}}
    rows2 = {**rows, 201: product(id=201, name="Sword", price=2500)}
    problems = SP.validate_design(draft, rows2)
    check("option_outside_roster" in codes(problems),
          "an option entry WITHOUT its root in products[] is rejected", str(problems))

    # Ordered roots remain valid, but duplicate roots are rejected server-side.
    draft = {"products": [101, 101],
             "action": {"kind": "buttons", "entries": [{"product_id": 101}]}}
    duplicate_problems = SP.validate_design(draft, rows)
    check("duplicate_product" in codes(duplicate_problems),
          "duplicate root product IDs are explicitly rejected")
    draft = {"products": [101, 201],
             "action": {"kind": "buttons", "entries": [{"product_id": 101}]}}
    rows_unique = {**rows, 201: product(id=201, name="Sword", price=2500)}
    check(SP.validate_design(draft, rows_unique) == [],
          "a unique ordered root roster remains valid")

    # product_select is ROOT-PRODUCT ONLY.
    draft = {"products": [101, 102],
             "action": {"kind": "product_select",
                        "entries": [{"product_id": 101}, {"product_id": 102}]}}
    problems = SP.validate_design(draft, rows)
    check("option_in_product_select" in codes(problems),
          "product_select never accepts purchase-option rows", str(problems))
    check("option_in_roster" in codes(problems),
          "and the option row in products[] is also rejected", str(problems))

    # option_select: exactly ONE family, direct options only.
    other = product(id=301, name="Mana Potion", price=800)
    other_opt = product(id=302, name="Mana Potion", price=400, option_of_id=301)
    rows3 = {**rows, 301: other, 302: other_opt}
    draft = {"products": [101, 301],
             "action": {"kind": "option_select",
                        "entries": [{"product_id": 102}, {"product_id": 302}]}}
    problems = SP.validate_design(draft, rows3)
    check("option_select_multi_family" in codes(problems),
          "option_select entries must share one root product", str(problems))

    chained = product(id=104, name="Health Potion", price=250, option_of_id=102)
    rows4 = {**rows, 104: chained}
    draft = {"products": [101],
             "action": {"kind": "option_select",
                        "entries": [{"product_id": 102}, {"product_id": 104}]}}
    problems = SP.validate_design(draft, rows4)
    check("option_chain" in codes(problems),
          "option-of-option chains are rejected (one level only)", str(problems))

    # Buttons may reference a root OR one of its option rows ([ Buy 7 Days ]).
    draft = {"products": [101],
             "action": {"kind": "buttons",
                        "entries": [{"product_id": 102, "label": "Buy 7 Days"}]}}
    check(SP.validate_design(draft, rows) == [],
          "a button on a purchase option is valid when its root is in products[]")

    # Structural rules.
    draft = {"products": [101], "action": {"kind": "menus", "entries": []}}
    check(codes(SP.validate_design(draft, rows)) == ["unknown_action_kind"],
          "unknown action kinds are rejected")
    draft = {"products": [101], "action": {"kind": "buttons", "entries": []}}
    check("action_entry_count" in codes(SP.validate_design(draft, rows)),
          "a button action needs at least one entry")
    draft = {"products": [101],
             "action": {"kind": "option_select",
                        "entries": [{"product_id": 101}, {"product_id": 101}]}}
    check("duplicate_entry" in codes(SP.validate_design(draft, rows)),
          "duplicate entries are rejected")
    draft = {"products": [101],
             "action": {"kind": "buttons", "entries": [{"product_id": 999}]}}
    check("unknown_product" in codes(SP.validate_design(draft, rows)),
          "entries referencing missing rows are rejected")
    draft = {"products": [], "action": {"kind": "buttons", "entries": []}}
    check("empty_roster" in codes(SP.validate_design(draft, rows)),
          "an empty roster is rejected")
    draft = {"products": ["101"],
             "action": {"kind": "buttons", "entries": [{"product_id": 101}]}}
    check("invalid_product_ref" in codes(SP.validate_design(draft, rows)),
          "non-integer roster ids are rejected")

    # Root-in-roster enforcement: entries must belong to the roster.
    draft = {"products": [301],
             "action": {"kind": "buttons", "entries": [{"product_id": 101}]}}
    check("entry_not_in_roster" in codes(SP.validate_design(draft, rows3)),
          "a root entry outside products[] is rejected")


# ── J. Mode policy (fixed fan-out, no template loops) ─────────────────────
def mode_tests():
    section("J. Presentation mode policy (per_product fan-out / frame)")
    card = {"embeds": [{"title": "{{name}}", "description": "{{price}} {{currency}}"}]}
    rows = {
        101: product(id=101, name="Health Potion", price=1500),
        102: product(id=102, name="Mana Potion", price=800),
    }

    draft = {
        "presentation": {"mode": "per_product", **card},
        "products": [101, 102],
        "action": {"kind": "product_select",
                   "entries": [{"product_id": 101}, {"product_id": 102}]},
    }
    preview = SP.preview_design(draft, rows, CURRENCY)
    check([e["title"] for e in preview["embeds"]] == ["Health Potion", "Mana Potion"],
          "per_product renders exactly one card per roster product, in order",
          str(preview["embeds"]))
    check(preview["embeds"][0]["description"] == "1,500 Coins",
          "each card resolves its own product", preview["embeds"][0]["description"])
    paths = [u["path"] for u in preview["tokens"]["used"]]
    check(paths[:6] == ["products.0.embeds.0.title", "products.0.embeds.0.description",
                        "products.0.embeds.0.description",
                        "products.1.embeds.0.title", "products.1.embeds.0.description",
                        "products.1.embeds.0.description"],
          "multi-product occurrences are namespaced per card", str(paths))
    check(paths[6:] == ["action.entries.0.label", "action.entries.1.label"],
          "action-label occurrences follow the presentation ones", str(paths))

    # Content in a multi-product fan-out has no product context.
    draft["presentation"]["content"] = "Buy {{name}}!"
    preview = SP.preview_design(draft, rows, CURRENCY)
    check(preview["content"] == "Buy {{name}}!" and
          [w["code"] for w in preview["warnings"]] == ["unresolved_product_token"],
          "shared content keeps product tokens verbatim and warns", preview["content"])

    # Single product: ordinary paths, content resolves against the product.
    draft = {
        "presentation": {"mode": "per_product", "content": "Buy {{name}}!",
                         "embeds": [{"title": "{{name}}"}]},
        "products": [101],
        "action": {"kind": "buttons", "entries": [{"product_id": 101}]},
    }
    preview = SP.preview_design(draft, rows, CURRENCY)
    check(preview["content"] == "Buy Health Potion!" and
          preview["tokens"]["used"][0]["path"] == "content",
          "a single-product roster resolves content against the product")

    # Fan-out over the embed cap warns (publish will block later).
    big = {"embeds": [{"title": "{{name}}"} for _ in range(3)]}
    draft = {
        "presentation": {"mode": "per_product", **big},
        "products": [101, 102, 101, 102, 101],
        "action": {"kind": "buttons", "entries": [{"product_id": 101}]},
    }
    # (roster repeats are allowed for layout order; cards render per id.)
    preview = SP.preview_design(draft, rows, CURRENCY)
    check(len(preview["embeds"]) == 15 and
          "presentation_limit" in [w["code"] for w in preview["warnings"]],
          "a fan-out past 10 embeds warns with presentation_limit",
          str([w["code"] for w in preview["warnings"]]))

    # Unknown modes fall back deterministically to per_product.
    draft = {"presentation": {"mode": "carousel", "content": "", "embeds": []},
             "products": [101],
             "action": {"kind": "buttons", "entries": [{"product_id": 101}]}}
    preview = SP.preview_design(draft, rows, CURRENCY)
    check(preview["mode"] == "per_product", "unknown modes fall back to per_product")


def main():
    catalog_tests()
    determinism_tests()
    non_programmable_tests()
    price_tests()
    purchase_action_tests()
    value_tests()
    warning_tests()
    normalize_tests()
    roster_tests()
    mode_tests()
    print(f"\n{'=' * 60}")
    if _failed:
        print(f"{_passed}/{_passed + _failed} passed — FAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"{_passed}/{_passed + _failed} passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
