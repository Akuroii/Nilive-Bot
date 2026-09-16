#!/usr/bin/env python3
"""
Dashboard currency-icon rendering + Mission check emoji — verification suite.

Two regressions this locks down:

  A. Mission completion line. `CHECK_EMOJI` is an APPLICATION emoji and is
     the single source of truth (utils/emoji.py). When the constant points
     at an id the application no longer owns, Discord 404s, the probe
     verdict becomes MISSING and the panel degrades to the unicode ✅. The
     constant must therefore track the live application emoji
     (`<a:Check:1549831102078787744>`), and the completion line must stay
     `⤷` → `reward claimed` → check → amount → name → currency emoji with
     every one of those last three resolved from the Economy config.

  B. Currency icons in the dashboard. A configured coin/diamond icon may be
     a Discord custom emoji (`<:name:id>` / the animated `<a:name:id>`).
     That markup is a Discord *message* feature, and both ways of dropping
     it into a page are broken:

       * Jinja autoescapes it, so the page shows the literal text
         `<a:gold:1549…>`;
       * an f-string partial does not escape it, so the HTML tokenizer
         reads `<a:gold:1549…>` as the start of an `<a>` tag and swallows
         it — the icon disappears entirely.

     Every currency surface now renders the icon through
     dashboard/utils/currency_ctx.py:icon_html (a CDN `<img>`, the same
     mechanism dashboard/utils/check_icon.py already used for the success
     indicator) or its text twin icon_text where HTML cannot help — an
     `<option>` may only contain text.

Covered here:
   1.  CHECK_EMOJI is the current application emoji, and the Mission
       completion line renders it in the locked order.
   2.  icon_html(): unicode, static custom, animated custom, empty,
       literal text, ID-only placeholder, and what a browser's tokenizer
       actually does with each result.
   3.  icon_text(): the same inputs, for `<option>` sinks.
   4.  End-to-end through the real Flask app — Economy page, Members,
       member profile, Leveling, Shop, Ledger, Missions, Tag Missions,
       Tag Partners, and the htmx partials (both leaderboards, shop items,
       purchase history, members search) — under four configurations:
       defaults, unicode, static custom, animated custom; for BOTH
       currencies.
   5.  `<option>` sinks carry text, never an `<img>` and never a raw token.
   6.  Numeric-only currency names survive the Economy save unchanged
       (they used to be normalised into `<:_:2024>`).
   7.  No surface anywhere emits a raw Discord token into HTML.

Run:
  python3 scripts/test_currency_icon.py
(needs the bot's own deps — discord.py, flask, aiosqlite — same as
requirements.txt; no network and no Discord token: the user resolver is
stubbed and every emoji image is asserted by URL, never fetched.)
"""
import io
import os
import re
import sys
import time
import sqlite3
import asyncio
import tempfile
import contextlib
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="currency_icon_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "currency_icon.db")
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

import database  # noqa: E402
from database import DB_PATH  # noqa: E402

GUILD = 6600
USER = 600
MEMBER = 9001

_passed = 0
_failed = 0
_failures = []


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  \033[92mPASS\033[0m  {label}")
    else:
        _failed += 1
        _failures.append(label)
        print(f"  \033[91mFAIL\033[0m  {label}" + (f"  — {detail}" if detail else ""))


def section(title):
    print(f"\n\033[1m{title}\033[0m")


# ═══════════════════════════════════════════════════════════════════
# What a browser actually does with a fragment
# ═══════════════════════════════════════════════════════════════════

# A Discord custom-emoji token swallowed as markup becomes a start tag
# whose name is the token itself, e.g. `<a:gold:1549…>`.
_TOKEN_TAG_RE = re.compile(r"^a?:\w+:\d+$")


def browser_view(frag: str):
    """(start_tag_names, visible_text) as an HTML tokenizer sees it.

    This is the assertion that matters: it does not ask what the server
    *intended* to send, it asks what a parser makes of what was sent.
    """
    tags, text = [], []

    class _P(HTMLParser):
        def handle_starttag(self, t, attrs):
            tags.append(t)

        def handle_data(self, d):
            text.append(d)

    p = _P(convert_charrefs=True)
    p.feed(frag or "")
    return tags, "".join(text)


def no_token_as_tag(frag: str) -> bool:
    """True when no Discord token was consumed as an HTML tag."""
    tags, _ = browser_view(frag)
    return not any(_TOKEN_TAG_RE.match(t) for t in tags)


def no_token_as_text(frag: str) -> bool:
    """True when no raw `<a:name:id>` / `<:name:id>` is visible as text."""
    _, text = browser_view(frag)
    return "<a:" not in text and "<:" not in text


def has_cdn_img(frag: str, emoji_id: str, animated: bool) -> bool:
    ext = "gif" if animated else "png"
    return (f'cdn.discordapp.com/emojis/{emoji_id}.{ext}' in frag
            and "<img" in frag)


# ═══════════════════════════════════════════════════════════════════
# The four configurations under test
# ═══════════════════════════════════════════════════════════════════

STATIC_ID = "1549831102078787001"
ANIM_ID = "1549831102078787002"

CONFIGS = {
    # name -> (coin_name, coin_emoji, diamond_name, diamond_emoji)
    # as the Economy form submits them (blank == "use the default")
    "defaults": ("", "", "", ""),
    "unicode": ("Moon", "🌙", "Gems", "💠"),
    "static custom": ("Moon", f"<:moon:{STATIC_ID}>",
                      "Gems", f"<:gem:{STATIC_ID}>"),
    "animated custom": ("Moon", f"<a:moon:{ANIM_ID}>",
                        "Gems", f"<a:gem:{ANIM_ID}>"),
}


async def set_config(name: str):
    """Write one configuration through the real writer."""
    from utils.currency import set_currency_config
    cn, ce, dn, de = CONFIGS[name]
    return await set_currency_config(
        GUILD, currency_name=cn, coin_emoji_id=ce,
        diamond_name=dn, diamond_emoji_id=de)


def expected(cfg_name: str):
    """(coin_emoji, diamond_emoji) that must be live for a configuration."""
    from utils.currency import DEFAULT_COIN_EMOJI, DEFAULT_DIAMOND_EMOJI
    _cn, ce, _dn, de = CONFIGS[cfg_name]
    return (ce or DEFAULT_COIN_EMOJI, de or DEFAULT_DIAMOND_EMOJI)


# ═══════════════════════════════════════════════════════════════════
# 1. Mission check emoji + completion line
# ═══════════════════════════════════════════════════════════════════
def mission_tests():
    section("1. Mission check emoji is the current application emoji")
    from utils.emoji import (
        CHECK_EMOJI, CHECK_EMOJI_ID, CHECK_EMOJI_FALLBACK, parse_emoji_input,
    )
    check("CHECK_EMOJI is the new application emoji token",
          CHECK_EMOJI == "<a:Check:1549831102078787744>", CHECK_EMOJI)
    check("CHECK_EMOJI_ID is the new id",
          CHECK_EMOJI_ID == 1549831102078787744, CHECK_EMOJI_ID)
    check("the old id 1549593658867712090 is gone from utils/emoji.py",
          "1549593658867712090" not in open(
              os.path.join(os.path.dirname(os.path.dirname(
                  os.path.abspath(__file__))), "utils", "emoji.py"),
              encoding="utf-8").read())
    parsed = parse_emoji_input(CHECK_EMOJI)
    check("it parses as an ANIMATED custom emoji named Check",
          parsed == (str(CHECK_EMOJI_ID), "Check", True), str(parsed))
    check("the unicode fallback is untouched (still last-resort only)",
          CHECK_EMOJI_FALLBACK == "✅")
    check("the resolver / state machine / probe are all still present",
          all(hasattr(__import__("utils.emoji", fromlist=["x"]), n) for n in (
              "resolve_check_emoji", "verify_check_emoji",
              "check_emoji_state", "check_emoji_detail",
              "CHECK_STATE_CONFIRMED", "CHECK_STATE_MISSING",
              "CHECK_STATE_UNKNOWN", "CHECK_REPROBE_SECONDS")))


async def mission_line_tests():
    section("1b. The completion line, rendered by the real _mission_block")
    import utils.emoji as E
    from utils.emoji import CHECK_EMOJI
    from cogs import missions as cog

    class _G:
        id = GUILD

    def line(reward_type, reward_value, cur, check_emoji=CHECK_EMOJI):
        block = cog._mission_block(
            {"name": "Chatter", "type": "messages", "target": 1,
             "progress": 1, "completed": True, "description": None,
             "reward_type": reward_type, "reward_value": reward_value},
            _G(), cur, check_emoji)
        for l in block.splitlines():
            if "reward claimed" in l:
                return l.strip()
        return ""

    # Unconfigured guild -> the historical defaults.
    default_cfg = await set_config("defaults")
    got = line("diamonds", 1, default_cfg)
    check("the exact line the spec asks for",
          got == "⤷ `reward claimed` <a:Check:1549831102078787744> 1 Diamonds 💎",
          got)

    # Order is locked: ⤷ → label → check → amount → name → currency emoji.
    m = re.match(r"^⤷ `reward claimed` (\S+) (.+)$", got)
    check("order: ⤷ → `reward claimed` → check → reward", m is not None, got)
    check("the check glyph is the application emoji, never ✅",
          m and m.group(1) == CHECK_EMOJI and "✅" not in got, got)

    # Amount / name / emoji stay dynamic and follow the Economy config.
    for cfg_name in CONFIGS:
        cfg = await set_config(cfg_name)
        coin_e, gem_e = expected(cfg_name)
        want_c = f"⤷ `reward claimed` {CHECK_EMOJI} 1,250 Moon {coin_e}" \
            if cfg_name != "defaults" else \
            f"⤷ `reward claimed` {CHECK_EMOJI} 1,250 Coins {coin_e}"
        want_d = f"⤷ `reward claimed` {CHECK_EMOJI} 12 Diamonds {gem_e}" \
            if cfg_name == "defaults" else \
            f"⤷ `reward claimed` {CHECK_EMOJI} 12 Gems {gem_e}"
        check(f"[{cfg_name}] a coins reward resolves amount+name+emoji "
              f"from config", line("coins", 1250, cfg) == want_c,
              line("coins", 1250, cfg))
        check(f"[{cfg_name}] a diamonds reward does too",
              line("diamonds", 12, cfg) == want_d, line("diamonds", 12, cfg))

    # Non-currency rewards keep their own text.
    check("an XP reward carries no currency emoji",
          line("xp", 50, default_cfg) ==
          f"⤷ `reward claimed` {CHECK_EMOJI} 50 XP", line("xp", 50, default_cfg))

    # A degraded probe must still be the ONLY thing that can produce ✅.
    E._check_state["state"] = E.CHECK_STATE_MISSING
    import utils.emoji as _E
    check("✅ can still only come from a MISSING verdict (fallback intact)",
          _E.resolve_check_emoji() == "✅")
    E._check_state["state"] = E.CHECK_STATE_UNKNOWN
    E._check_state["token"] = CHECK_EMOJI
    check("and an inconclusive verdict keeps the application emoji",
          _E.resolve_check_emoji() == CHECK_EMOJI)

    await set_config("defaults")


# ═══════════════════════════════════════════════════════════════════
# 2. icon_html() — the renderer
# ═══════════════════════════════════════════════════════════════════
def icon_html_tests():
    section("2. icon_html() — unicode / static / animated / edge cases")
    from dashboard.utils.currency_ctx import icon_html

    cases = [
        ("unicode 🪙", "🪙", None, False),
        ("unicode 🌙", "🌙", None, False),
        ("arabic text name-as-icon", "قمر", None, False),
        ("static custom", f"<:moon:{STATIC_ID}>", STATIC_ID, False),
        ("animated custom", f"<a:moon:{ANIM_ID}>", ANIM_ID, True),
    ]
    for label, raw, emoji_id, animated in cases:
        out = str(icon_html(raw))
        if emoji_id is None:
            check(f"{label} -> passes through as text, no <img>",
                  "<img" not in out and raw in out, out)
        else:
            check(f"{label} -> a CDN <img>, not raw markup",
                  has_cdn_img(out, emoji_id, animated)
                  and "<a:" not in out and "<:" not in out, out)
            check(f"{label} -> uses the "
                  f"{'.gif' if animated else '.png'} asset",
                  f"{emoji_id}.{ 'gif' if animated else 'png' }" in out, out)
        check(f"{label} -> a browser does not swallow it as a tag",
              no_token_as_tag(out), out)
        check(f"{label} -> no raw token text is visible",
              no_token_as_text(out), out)

    check("empty -> empty string (the currency name still carries the row)",
          str(icon_html("")) == "" and str(icon_html(None)) == "")
    check("empty + fallback -> the fallback, escaped",
          str(icon_html("", "🪙")) == "🪙")
    check("literal text is escaped, not treated as markup",
          "&lt;b&gt;" in str(icon_html("<b>")) and "<b>" not in str(icon_html("<b>")))
    check("an ID-only emoji (placeholder `_` name) still renders an image",
          has_cdn_img(str(icon_html(f"<:_:{STATIC_ID}>")), STATIC_ID, False))
    check("icon_css ships the alignment rule only (no sizing/colour change)",
          "nero-currency-icon" in str(
              __import__("dashboard.utils.currency_ctx",
                         fromlist=["x"]).icon_css())
          and "vertical-align" in str(
              __import__("dashboard.utils.currency_ctx",
                         fromlist=["x"]).icon_css()))


# ═══════════════════════════════════════════════════════════════════
# 3. icon_text() — the <option> twin
# ═══════════════════════════════════════════════════════════════════
def icon_text_tests():
    section("3. icon_text() — plain text for <option> sinks")
    from dashboard.utils.currency_ctx import icon_text

    check("unicode passes through", icon_text("🪙") == "🪙")
    check("static custom -> its NAME, never the token",
          icon_text(f"<:moon:{STATIC_ID}>") == "moon")
    check("animated custom -> its NAME, never the token",
          icon_text(f"<a:moon:{ANIM_ID}>") == "moon")
    check("the `_` placeholder name reads as 'no icon'",
          icon_text(f"<:_:{STATIC_ID}>") == "")
    check("empty -> empty", icon_text("") == "" and icon_text(None) == "")
    check("it returns a plain str, so Jinja escapes it",
          type(icon_text("🪙")).__name__ == "str")
    for raw in (f"<:moon:{STATIC_ID}>", f"<a:moon:{ANIM_ID}>"):
        check(f"no markup leaks from {raw[:12]}…",
              "<" not in icon_text(raw) and ">" not in icon_text(raw),
              icon_text(raw))


# ═══════════════════════════════════════════════════════════════════
# 4. End-to-end through the real Flask app
# ═══════════════════════════════════════════════════════════════════

# Pages that render a currency icon somewhere an <img> is allowed.
ICON_PAGES = ["/economy", "/members", "/members/9001", "/leveling", "/shop"]
# Pages whose ONLY currency sites are the currency pickers, which are
# <option> elements — an <img> inside one is dropped by every browser, so
# these must carry the icon's NAME as text instead.
OPTION_ONLY_PAGES = ["/ledger", "/missions", "/tag-missions", "/tag-partners"]
PAGES = ICON_PAGES + OPTION_ONLY_PAGES
PARTIALS = ["/api/economy/leaderboard", "/api/economy/leaderboard-diamonds",
            "/api/shop/items", "/api/shop/purchase-history",
            "/api/members/search"]


def _tab_label(page: str, tab: str) -> str:
    m = re.search(r'id="tab-%s"[^>]*>(.*?)</button>' % tab, page, re.S)
    return m.group(1).strip() if m else ""


def e2e_tests(client, csrf):
    section("4. End-to-end: every currency surface, four configurations")

    for cfg_name in CONFIGS:
        coin_e, gem_e = expected(cfg_name)
        is_custom = cfg_name.endswith("custom")
        is_animated = cfg_name == "animated custom"
        emoji_id = ANIM_ID if is_animated else (STATIC_ID if is_custom else None)

        r = client.post("/api/economy/currency", json={
            "currency_name": CONFIGS[cfg_name][0],
            "coin_emoji_id": CONFIGS[cfg_name][1],
            "diamond_name": CONFIGS[cfg_name][2],
            "diamond_emoji_id": CONFIGS[cfg_name][3],
        }, headers={"X-CSRF-Token": csrf,
                    "Content-Type": "application/json"})
        j = r.get_json() or {}
        check(f"[{cfg_name}] the Economy save succeeds",
              r.status_code == 200 and j.get("success"), f"{r.status_code} {j}")
        res = j.get("resolved") or {}
        check(f"[{cfg_name}] coins resolve to the configured emoji",
              (res.get("coins") or {}).get("emoji") == coin_e,
              (res.get("coins") or {}).get("emoji"))
        check(f"[{cfg_name}] diamonds resolve to the configured emoji",
              (res.get("diamonds") or {}).get("emoji") == gem_e,
              (res.get("diamonds") or {}).get("emoji"))

        # ── pages ────────────────────────────────────────────────────
        for path in PAGES:
            resp = client.get(path)
            page = resp.get_data(as_text=True)
            check(f"[{cfg_name}] {path} renders 200",
                  resp.status_code == 200 and bool(page), resp.status_code)
            check(f"[{cfg_name}] {path}: no Discord token swallowed as a tag",
                  no_token_as_tag(page),
                  [t for t in browser_view(page)[0] if _TOKEN_TAG_RE.match(t)][:3])
            # The Economy form intentionally pre-fills the stored token into
            # an <input value>, and its help text documents the accepted
            # `<:name:id>` format inside <code>. Both are escaped data/docs,
            # not a rendered currency label, so the visible-text scan
            # excludes those two shapes.
            scan = re.sub(r'<input[^>]*id="cur-(coin|diamond)-emoji"[^>]*>',
                          '', page)
            scan = re.sub(r"<code>.*?</code>", "", scan, flags=re.S)
            # <script>/<style> bodies are not rendered text either; the
            # Economy page's own JS carries `<:name:id>` in a comment.
            scan = re.sub(r"<script\b.*?</script>", "", scan, flags=re.S)
            scan = re.sub(r"<style\b.*?</style>", "", scan, flags=re.S)
            check(f"[{cfg_name}] {path}: no raw token text is visible",
                  no_token_as_text(scan),
                  [s for s in re.findall(r"&lt;a?:?\w*:?[^&]{0,24}", scan)][:3])
            if is_custom and path in ICON_PAGES:
                check(f"[{cfg_name}] {path}: the icon is a CDN image",
                      has_cdn_img(page, emoji_id, is_animated),
                      f"looking for {emoji_id}.{'gif' if is_animated else 'png'}")
            if is_custom and path in OPTION_ONLY_PAGES:
                # Every currency site here is a picker, so the icon must
                # have degraded to its NAME inside the <option> — never an
                # <img> (browsers drop those) and never the raw token.
                opts = re.findall(r"<option\b[^>]*>(.*?)</option>", page, re.S)
                cur_opts = [o for o in opts if "Moon" in o or "Gems" in o]
                check(f"[{cfg_name}] {path}: the picker options carry the "
                      f"icon NAME as text",
                      bool(cur_opts)
                      and all("<img" not in o and no_token_as_text(o)
                              for o in cur_opts),
                      cur_opts[:2])

        # The Economy tab labels are the most visible surface of all.
        econ = client.get("/economy").get_data(as_text=True)
        for tab, emoji, name in (("coins", coin_e, res["coins"]["name"]),
                                 ("diamonds", gem_e, res["diamonds"]["name"])):
            label = _tab_label(econ, tab)
            check(f"[{cfg_name}] /economy tab '{tab}' shows the icon+name",
                  name in label and no_token_as_tag(label)
                  and no_token_as_text(label), label)
            if is_custom:
                check(f"[{cfg_name}] /economy tab '{tab}' icon is an <img>",
                      has_cdn_img(label, emoji_id, is_animated), label)

        # ── htmx partials (these were the unescaped ones) ────────────
        for path in PARTIALS:
            frag = client.get(path).get_data(as_text=True)
            check(f"[{cfg_name}] {path}: no Discord token swallowed as a tag",
                  no_token_as_tag(frag),
                  [t for t in browser_view(frag)[0] if _TOKEN_TAG_RE.match(t)][:3])
            check(f"[{cfg_name}] {path}: no raw token text",
                  no_token_as_text(frag), frag[:120])
            if is_custom and "economy/leaderboard" in path:
                check(f"[{cfg_name}] {path}: icon is a CDN image",
                      has_cdn_img(frag, emoji_id, is_animated), frag[:160])

    asyncio.run(set_config("defaults"))


# ═══════════════════════════════════════════════════════════════════
# 5. <option> sinks must carry text
# ═══════════════════════════════════════════════════════════════════
def option_tests(client, csrf):
    section("5. <option> sinks carry text, never an <img> or a raw token")
    client.post("/api/economy/currency", json={
        "currency_name": "Moon", "coin_emoji_id": f"<a:moon:{ANIM_ID}>",
        "diamond_name": "Gems", "diamond_emoji_id": f"<a:gem:{ANIM_ID}>",
    }, headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})

    opt = re.compile(r"<option\b[^>]*>(.*?)</option>", re.S)
    for path in ("/ledger", "/leveling", "/missions",
                 "/tag-missions", "/tag-partners", "/minigames"):
        page = client.get(path).get_data(as_text=True)
        options = opt.findall(page)
        if not options:
            continue
        bad_img = [o for o in options if "<img" in o]
        bad_tok = [o for o in options if not no_token_as_text(o)
                   or not no_token_as_tag(o)]
        check(f"{path}: no <option> holds an <img> (browsers drop it)",
              not bad_img, bad_img[:1])
        check(f"{path}: no <option> holds a raw Discord token",
              not bad_tok, bad_tok[:1])
        # The custom emoji degrades to its NAME inside an option.
        named = [o for o in options if "moon" in o or "gem" in o]
        if path in ("/ledger", "/leveling", "/missions",
                    "/tag-missions", "/tag-partners"):
            check(f"{path}: the custom icon degrades to its emoji name",
                  bool(named), [o for o in options if "Moon" in o or "Gems" in o][:2])

    asyncio.run(set_config("defaults"))


# ═══════════════════════════════════════════════════════════════════
# 6. Numeric-only currency names
# ═══════════════════════════════════════════════════════════════════
def numeric_name_tests(client, csrf):
    section("6. A numeric-only currency NAME is not turned into an emoji")
    from utils.currency import get_currency_config_raw, get_currency_config

    r = client.post("/api/economy/currency", json={
        "currency_name": "2024", "coin_emoji_id": "",
        "diamond_name": "100", "diamond_emoji_id": "",
    }, headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})
    j = r.get_json() or {}
    check("the save succeeds", r.status_code == 200 and j.get("success"),
          f"{r.status_code} {j}")

    raw = asyncio.run(get_currency_config_raw(GUILD))
    check("currency_name is stored as '2024', not '<:_:2024>'",
          raw["currency_name"] == "2024", raw["currency_name"])
    check("diamond_name is stored as '100', not '<:_:100>'",
          raw["diamond_name"] == "100", raw["diamond_name"])

    cfg = asyncio.run(get_currency_config(GUILD))
    check("the resolved coin name is '2024'",
          cfg["coins"]["name"] == "2024", cfg["coins"]["name"])
    check("a blank coin emoji still falls back to the default",
          cfg["coins"]["emoji"] == "🪙", cfg["coins"]["emoji"])

    # An emoji field must still be normalised — a bare ID is the one form
    # Discord cannot render on its own.
    r2 = client.post("/api/economy/currency", json={
        "coin_emoji_id": ANIM_ID},
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"})
    raw2 = asyncio.run(get_currency_config_raw(GUILD))
    check("a bare EMOJI id is still normalised into a token",
          raw2["coin_emoji_id"].startswith("<")
          and raw2["coin_emoji_id"].endswith(f":{ANIM_ID}>"),
          raw2["coin_emoji_id"])
    check("and the numeric name was untouched by that partial save",
          raw2["currency_name"] == "2024", raw2["currency_name"])

    asyncio.run(set_config("defaults"))


# ═══════════════════════════════════════════════════════════════════
# 7. Repository-wide: nothing emits a raw token into HTML
# ═══════════════════════════════════════════════════════════════════
def repo_scan_tests():
    section("7. Repository-wide scans")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # No template interpolates the raw emoji any more.
    bad = []
    tdir = os.path.join(root, "dashboard", "templates")
    for dirpath, _dirs, files in os.walk(tdir):
        for fn in files:
            if not fn.endswith(".html"):
                continue
            p = os.path.join(dirpath, fn)
            src = open(p, encoding="utf-8").read()
            for m in re.finditer(
                    r"(?<!currency_icon\()(?<!currency_icon_text\()"
                    r"currency\.(coins|diamonds)\.emoji", src):
                bad.append(f"{os.path.relpath(p, root)}: …{m.group(0)}")
    check("no template interpolates currency.*.emoji raw", not bad, bad[:4])

    # No f-string HTML partial interpolates the raw emoji any more.
    bad = []
    for rel in ("dashboard/api/economy_shop.py", "dashboard/api/core.py",
                "dashboard/api/leveling.py", "dashboard/api/misc.py",
                "dashboard/api/trade.py", "dashboard/api/minigames.py"):
        p = os.path.join(root, rel)
        if not os.path.exists(p):
            continue
        for i, line in enumerate(open(p, encoding="utf-8"), 1):
            if re.search(r"\{[^}]*\['emoji'\][^}]*\}", line) and "<" in line:
                bad.append(f"{rel}:{i}")
    check("no HTML partial interpolates ['emoji'] raw", not bad, bad[:4])

    # The JS helpers route the icon through the HTML twin.
    js = open(os.path.join(root, "dashboard", "static", "js", "dashboard.js"),
              encoding="utf-8").read()
    check("currencyLabel() renders the icon via currencyEmojiHtml",
          "currencyEmojiHtml(info.emoji)" in js)
    check("currencyAmount() renders the icon via currencyEmojiHtml",
          js.count("currencyEmojiHtml(info.emoji)") >= 2)
    check("a text twin exists for <option> sinks",
          "function currencyLabelText(" in js
          and "function currencyEmojiText(" in js)
    check("the JS CDN url matches the Python one (gif for animated)",
          "cdn.discordapp.com/emojis/" in js and "'gif' : 'png'" in js)

    # The old check-emoji id is gone from every non-doc file. This suite is
    # excluded because it names the id in order to assert its absence.
    hits = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for fn in files:
            if not fn.endswith((".py", ".html", ".js")):
                continue
            if fn == os.path.basename(__file__):
                continue
            p = os.path.join(dirpath, fn)
            if "1549593658867712090" in open(p, encoding="utf-8",
                                             errors="ignore").read():
                hits.append(os.path.relpath(p, root))
    check("the old application-emoji id appears in no .py/.html/.js file",
          not hits, hits)


# ═══════════════════════════════════════════════════════════════════
def seed():
    """Rows so the leaderboards / shop / members surfaces have something
    to render — an empty table would prove nothing."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO dashboard_users "
                 "(guild_id, user_id, permission_level, enabled) "
                 "VALUES (?,?,?,1)", (GUILD, USER, "admin"))
    conn.execute("INSERT OR REPLACE INTO economy "
                 "(guild_id, user_id, balance, diamonds) VALUES (?,?,?,?)",
                 (GUILD, MEMBER, 1250, 12))
    conn.execute("INSERT OR REPLACE INTO levels "
                 "(guild_id, user_id, xp, level) VALUES (?,?,?,?)",
                 (GUILD, MEMBER, 500, 4))
    conn.execute("INSERT INTO shop_items "
                 "(guild_id, name, description, price, type, enabled, "
                 " price_diamonds) VALUES (?,?,?,?,?,1,?)",
                 (GUILD, "Role", "d", 100, "role", 25))
    conn.execute("INSERT INTO purchase_history "
                 "(guild_id, user_id, user_display_name, item_id, item_name, "
                 " price_paid, currency_paid) VALUES (?,?,?,?,?,?,?)",
                 (GUILD, MEMBER, "Someone", 1, "Role", 100, "diamonds"))
    conn.commit()
    conn.close()


def main():
    asyncio.run(database.init_db())
    seed()

    # The Discord user resolver would hit the network; the surfaces under
    # test only need it to not raise.
    import utils.discord_user_cache as duc

    async def _offline(guild_id, ids):
        return {}
    duc.resolve_users = _offline

    import dashboard.app as dapp
    app = dapp.app
    app.config["TESTING"] = True
    csrf = "testcsrf"
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = {"id": USER, "username": "t", "avatar": None}
        s["guild_id"] = GUILD
        s["expires_at"] = time.time() + 7200
        s["csrf_token"] = csrf

    # Keep the noisy startup banners out of the report.
    buf, real = io.StringIO(), sys.stdout
    try:
        sys.stdout = buf
        mission_tests()
    finally:
        sys.stdout = real
    print(buf.getvalue(), end="")

    asyncio.run(mission_line_tests())
    icon_html_tests()
    icon_text_tests()
    e2e_tests(c, csrf)
    option_tests(c, csrf)
    numeric_name_tests(c, csrf)
    repo_scan_tests()

    print(f"\n{'=' * 60}")
    if _failed:
        print(f"RESULT: {_passed} passed, {_failed} FAILED")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"RESULT: all {_passed} checks passed")


if __name__ == "__main__":
    main()
