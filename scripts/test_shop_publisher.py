#!/usr/bin/env python3
"""
Shop Publisher — Phase 1: the FIXED token resolver (utils/shop_publisher.py).

What this locks down
  1.  The token catalog is FIXED: one exact set of resolvable names, rendered
      for the UI from the same table the resolver uses.
  2.  Resolution is DETERMINISTIC: same template + product + currency →
      byte-identical output; the input document is never mutated; repeating
      the resolution never differs.
  3.  Resolution is NON-PROGRAMMABLE: bare `{{name}}` lookups only — no
      nesting, no expressions, resolved values are never re-scanned, and
      anything outside the catalog stays verbatim and is reported.
  4.  The preview shows the resolved product presentation PLUS the purchase
      action that will actually be published (`shop_buy_<id>` — the existing
      purchase mechanism), byte-for-byte the descriptor Phase 2 publishes.
  5.  Preview warnings are deterministic and pathed (unknown token, empty
      value, product state, Discord-rule breaches of the RESOLVED payload).
  6.  Phase 1 does NOT implement Free behavior: a zero-price row renders the
      mechanical price. This suite pins that current behavior so Phase 3's
      `𝐅𝐫𝐞𝐞` change is a deliberate, reviewed test edit — and asserts that
      paid-product validation is untouched.

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
        "content": "Buy {{product.name}} for {{product.price}}!",
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
        "product.price_amount", "product.currency_name", "product.currency_emoji",
        "product.type", "product.duration", "product.stock",
        "product.required_level", "product.rarity", "product.icon_url",
        "product.prestige_tier",
    }
    check(set(SP.TOKEN_CATALOG) == expected,
          "catalog holds exactly the V1 token set", str(set(SP.TOKEN_CATALOG) ^ expected))
    payload = SP.token_catalog_payload()
    check([e["key"] for e in payload] == list(SP.TOKEN_CATALOG),
          "UI catalog payload preserves the fixed display order")
    check(all(e["token"] == "{{" + e["key"] + "}}" for e in payload),
          "every catalog entry exposes its exact token spelling")
    check(all(e["description"] for e in payload),
          "every catalog entry is documented")

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
          "content resolves with the mechanical price", one["content"])
    check(one["embeds"][0]["fields"][0]["value"] == "role", "field values resolve")
    check(one["embeds"][0]["footer"]["text"] == "Rarity: epic", "footer resolves")


# ── C. Non-programmable ────────────────────────────────────────────────────

def non_programmable_tests():
    section("C. Non-programmable: lookups only")
    resolved, used = SP.resolve_message(
        {"content": "{{ 1 + 1 }} {{product.name}} {{missing.thing}} {{}} {{ product.name }}"},
        SP.product_token_values(product(), CURRENCY))
    check(resolved["content"].startswith("{{ 1 + 1 }} "),
          "expressions are not evaluated — left verbatim", resolved["content"])
    check("VIP Role" in resolved["content"], "whitespace-tolerant known token resolves")
    check(resolved["content"].count("VIP Role") == 2,
          "both spellings of the known token resolve")
    check("{{missing.thing}}" in resolved["content"], "unknown token stays verbatim")
    check(resolved["content"].count("{{}}") == 1 and " {{}} " in resolved["content"],
          "an empty brace group is not a token — left verbatim", resolved["content"])
    check([u["key"] for u in used if not u["resolved"]] == ["missing.thing"],
          "only the unknown dotted name is reported as unknown", str(used))

    injection = SP.resolve_message(
        {"content": "{{product.name}}"},
        SP.product_token_values(product(name="{{product.id}} 7"), CURRENCY))
    check(injection[0]["content"] == "{{product.id}} 7",
          "a resolved value containing braces is never re-scanned (no nesting/injection)")
    check(len(injection[1]) == 1, "exactly one occurrence recorded for one token")

    resolved, used = SP.resolve_message(
        {"content": "{{product.name}}{{product.name}}{{product.name}}"},
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


# ── D. Price formatting (Phase 1 has NO Free behavior) ─────────────────────

def price_tests():
    section("D. Price formatting — mechanical, no Free behavior")
    values = SP.product_token_values(product(), CURRENCY)
    check(values["product.price"] == "1,500 🪙 Coins", "coin price renders with icon + name",
          values["product.price"])
    check(values["product.price_amount"] == "1500", "raw amount is plain digits")
    check(values["product.currency_name"] == "Coins" and
          values["product.currency_emoji"] == "🪙", "coin currency tokens follow the config")

    values = SP.product_token_values(product(price_diamonds=25), CURRENCY)
    check(values["product.price"] == "25 💎 Diamonds", "a diamond price takes precedence",
          values["product.price"])
    check(values["product.price_amount"] == "25", "diamond amount is plain digits")
    check(values["product.currency_name"] == "Diamonds", "charging currency switches to diamonds")

    renamed = {
        "coins": {"key": "balance", "name": "Moons", "emoji": "<:moon:1>"},
        "diamonds": {"key": "diamonds", "name": "Stars", "emoji": "<:star:2>"},
    }
    values = SP.product_token_values(product(), renamed)
    check(values["product.price"] == "1,500 <:moon:1> Moons",
          "custom currency display flows through", values["product.price"])

    # PHASE 1 DELIBERATE STATE — Phase 3 (Free + Prestige) changes this on
    # purpose: zero price / no price must render 𝐅𝐫𝐞𝐞. Until then the
    # renderer stays mechanical so paid-product semantics are untouched.
    values = SP.product_token_values(product(price=0, price_diamonds=None), CURRENCY)
    check(values["product.price"] == "0 🪙 Coins",
          "PHASE 1: zero price renders mechanically (not the Phase 3 Free form)",
          values["product.price"])


# ── E. The purchase action that will be published ──────────────────────────

def purchase_action_tests():
    section("E. The purchase action is the real, existing mechanism")
    action = SP.purchase_action(product())
    check(action == {
        "type": "button",
        "custom_id": "shop_buy_12",
        "label": "Buy VIP Role",
        "style": "green",
        "emoji": "🛒",
    }, "purchase action is exactly the descriptor Phase 2 publishes", str(action))
    check(action["custom_id"].startswith("shop_buy_"),
          "custom_id is the family cogs/shop.py's on_interaction already dispatches")
    preview = SP.preview_message(template(), product(), CURRENCY)
    check(preview["purchase_action"] == action,
          "the preview's purchase action IS purchase_action(product)")


# ── F. Token value edges ───────────────────────────────────────────────────

def value_tests():
    section("F. Token values on real product shapes")
    values = SP.product_token_values(product(), CURRENCY)
    check(values["product.id"] == "12", "id resolves")
    check(values["product.duration"] == "", "no duration → empty")
    check(values["product.stock"] == "12/20", "finite stock renders current/max")
    check(values["product.required_level"] == "5", "required level renders")
    check(values["product.prestige_tier"] == "", "no tier → empty")

    values = SP.product_token_values(product(duration_hours=72, max_stock=None,
                                             current_stock=None, required_level=0), CURRENCY)
    check(values["product.duration"] == "72h", "duration renders with h suffix")
    check(values["product.stock"] == "", "unlimited stock → empty")
    check(values["product.required_level"] == "", "no requirement → empty")

    values = SP.product_token_values(product(prestige_tier=4), CURRENCY)
    check(values["product.prestige_tier"] == "IV",
          "prestige tier reuses the single roman label source (read-only)",
          values["product.prestige_tier"])

    values = SP.product_token_values(product(description=None, icon_url=None, rarity=None), CURRENCY)
    check(values["product.description"] == "", "absent description → empty, not 'None'")
    check(values["product.icon_url"] == "", "absent icon → empty")
    check(values["product.rarity"] == "common", "absent rarity falls back to common")

    values = SP.product_token_values({**product(), "id": 3, "name": "VI", "price": 0,
                                      "type": "prestige", "prestige_tier": 6}, CURRENCY)
    check(values["product.prestige_tier"] == "VI", "tier VI renders as VI")


# ── G. Warnings ────────────────────────────────────────────────────────────

def warning_tests():
    section("G. Preview warnings (deterministic, pathed)")
    preview = SP.preview_message(template(), product(), CURRENCY)
    check(preview["warnings"] == [], "a clean preview raises no warnings", str(preview["warnings"]))

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

    # Fixed order: product state → occurrences → resolved-payload validation.
    preview = SP.preview_message(
        {"content": "{{nope}}", "embeds": [{"title": "x" * 300}]},
        product(enabled=0, current_stock=0), CURRENCY)
    codes = [w["code"] for w in preview["warnings"]]
    check(codes == ["product_disabled", "product_out_of_stock", "unknown_token", "validation"],
          "warning order is the documented deterministic order", str(codes))


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


def main():
    catalog_tests()
    determinism_tests()
    non_programmable_tests()
    price_tests()
    purchase_action_tests()
    value_tests()
    warning_tests()
    normalize_tests()
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
