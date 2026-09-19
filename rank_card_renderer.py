"""
RANK CARD RENDERER — draws the payload from utils/rank_card_data.py onto a
fixed 1280x853 PNG.

Deliberately kept discord.py-free: it only reads the plain dict that
get_rank_card_data() already resolved. Reusable from a non-bot context
later without dragging discord.py along.

Layout is data (see LAYOUT below), drawing is code.

LAYOUT was measured directly off the approved reference design (grid-
overlaid at 20px, reference canvas 1024x682, scaled x1.25 to this
module's 1280x853 canvas) -- re-measured in the Pass 3 fidelity pass
after the reference's own 3-column x 4-row inventory grid, two-tone
label coloring, and line-art (not color-emoji) UI icon style were
confirmed by close inspection of the reference file.
"""
from __future__ import annotations

import io
import os
import math
import asyncio
import logging

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

from utils.emoji import parse_emoji_input, is_custom_emoji_token, emoji_cdn_url

log = logging.getLogger("rank_card_renderer")

# ─────────────────────────────────────────────────────────────────────────
# ASSETS
# ─────────────────────────────────────────────────────────────────────────

_ASSET_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "assets", "rank_card")
_FONT_DIR = os.path.join(_ASSET_ROOT, "fonts")

MAILBOX_PNG_PATH = os.path.join(_ASSET_ROOT, "mailbox.png")

FONT_PATHS = {
    "cinzel": os.path.join(_FONT_DIR, "Cinzel-Variable.ttf"),
    "zilla_bold": os.path.join(_FONT_DIR, "ZillaSlab-Bold.ttf"),
    "outfit": os.path.join(_FONT_DIR, "Outfit-Variable.ttf"),
    "amiri_regular": os.path.join(_FONT_DIR, "Amiri-Regular.ttf"),
    "amiri_bold": os.path.join(_FONT_DIR, "Amiri-Bold.ttf"),
    "tajawal_regular": os.path.join(_FONT_DIR, "Tajawal-Regular.ttf"),
    "tajawal_medium": os.path.join(_FONT_DIR, "Tajawal-Medium.ttf"),
    "tajawal_bold": os.path.join(_FONT_DIR, "Tajawal-Bold.ttf"),
}

TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/"

# ─────────────────────────────────────────────────────────────────────────
# COLOR PALETTE
# ─────────────────────────────────────────────────────────────────────────

COLORS = {
    "bg_top": (11, 6, 20),
    "bg_bottom": (23, 11, 36),
    "nebula_a": (78, 26, 122),
    "nebula_b": (120, 52, 168),
    "panel": (21, 13, 33, 165),
    "panel_border": (150, 100, 210, 70),
    "panel_glow": (130, 80, 190, 40),
    "purple_glow": (170, 100, 235),
    "text_primary": (240, 235, 250),
    "text_muted": (150, 134, 178),      # muted label tone (e.g. "TOP", "/2,000 XP")
    "text_bright": (216, 178, 255),     # brighter tone for emphasized values
    "accent": (196, 140, 255),
    "pip_filled": (196, 110, 255),
    "pip_empty": (68, 54, 88),
    "xp_bar_bg": (38, 25, 56),
    "xp_bar_fill_a": (140, 80, 230),    # gradient start (left)
    "xp_bar_fill_b": (230, 110, 220),   # gradient end (right)
    "ring": (176, 120, 235),
    "line_icon": (178, 126, 226),       # purple line-art icon stroke
}

# ─────────────────────────────────────────────────────────────────────────
# LAYOUT — fixed canvas 1280x853, measured off the reference (x1.25 scale)
# ─────────────────────────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 1280, 853

LAYOUT = {
    "avatar": (41, 63, 247, 247),
    "avatar_level_badge_r": 30,

    "name": (327, 97, 600, 63),
    "title_pill": (327, 202, 250, 34),
    "member_since": (327, 275, 500, 28),

    "rank_prestige_panel": (706, 56, 213, 256),

    "level_panel": (40, 375, 179, 175),
    "xp_totalxp_panel": (225, 375, 475, 175),
    "xp_section_frac": 0.62,

    "stats_row_y": 575,
    "stats_row_h": 165,
    "stats_card_w": 116,
    "stats_gap": 16,
    "stats_start_x": 40,

    # 3 columns x 4 rows (reference-confirmed), tall right column.
    "inventory_panel": (706, 322, 294, 418),
    "inventory_grid_origin": (733, 372),
    "inventory_slot": (62, 60),
    "inventory_slot_gap_x": 16,
    "inventory_slot_gap_y": 14,
    "inventory_cols": 3,
    "inventory_rows": 4,

    "mailbox_column_x": (1030, 1280),
    "mailbox_bottom_y": 830,
    "mailbox_top_min_y": 60,

    "footer_y": 812,
}


# ─────────────────────────────────────────────────────────────────────────
# FONT HELPERS
# ─────────────────────────────────────────────────────────────────────────

def _font(family: str, size: int, weight: str | None = None) -> ImageFont.FreeTypeFont:
    path = FONT_PATHS[family]
    f = ImageFont.truetype(path, size)
    if weight:
        try:
            f.set_variation_by_name(weight.encode())
        except Exception:
            try:
                if weight.lower() == "semibold":
                    f.set_variation_by_axes([600])
            except Exception:
                pass
    return f


def cinzel_bold(size):
    return _font("cinzel", size, "Bold")


def cinzel_semibold(size):
    return _font("cinzel", size, "SemiBold")


def zilla_bold(size):
    return _font("zilla_bold", size)


def outfit(size, weight="Regular"):
    return _font("outfit", size, weight)


def amiri(size, bold=False):
    return ImageFont.truetype(FONT_PATHS["amiri_bold" if bold else "amiri_regular"], size)


def tajawal(size, weight="regular"):
    return ImageFont.truetype(FONT_PATHS[f"tajawal_{weight}"], size)


def _draw_tracked_text(draw, xy, text, font, fill, tracking=0, anchor=None):
    """draw.text() has no letter-spacing support. When tracking>0, draws
    character-by-character with extra spacing -- used for the reference's
    wider-tracked small-caps labels. Returns the total rendered width."""
    if tracking <= 0:
        draw.text(xy, text, font=font, fill=fill, anchor=anchor)
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    x, y = xy
    if anchor and anchor[0] in ("m", "r"):
        total_w = sum(draw.textbbox((0, 0), ch, font=font)[2] + tracking for ch in text) - tracking
        if anchor[0] == "m":
            x -= total_w / 2
        elif anchor[0] == "r":
            x -= total_w
    cursor = x
    for ch in text:
        draw.text((cursor, y), ch, font=font, fill=fill,
                  anchor=("l" + anchor[1]) if anchor else None)
        w = draw.textbbox((0, 0), ch, font=font)[2]
        cursor += w + tracking
    return cursor - x


def _draw_dropcap_heading(draw, xy, text, font_family, big_size, small_size,
                          fill, tracking=1, weight="Bold"):
    """The reference renders certain display headers (SHADOW, PRESTIGE VI)
    with an oversized first letter followed by smaller caps -- true
    OpenType small-caps isn't available through Pillow's basic text API,
    so this fakes it: first char at big_size, the rest at small_size,
    baseline-aligned. Returns total rendered width."""
    x, y = xy
    big_font = _font(font_family, big_size, weight)
    small_font = _font(font_family, small_size, weight)

    first, rest = text[0], text[1:]
    big_bbox = draw.textbbox((0, 0), first, font=big_font)
    small_bbox = draw.textbbox((0, 0), "H", font=small_font)
    # Baseline-align: both sit on the same bottom line.
    big_bottom = y + big_bbox[3]
    small_y = big_bottom - small_bbox[3]

    draw.text((x, y), first, font=big_font, fill=fill)
    cursor = x + big_bbox[2] + tracking

    for ch in rest:
        draw.text((cursor, small_y), ch, font=small_font, fill=fill)
        w = draw.textbbox((0, 0), ch, font=small_font)[2]
        cursor += w + tracking
    return cursor - x


# ─────────────────────────────────────────────────────────────────────────
# BACKGROUND
# ─────────────────────────────────────────────────────────────────────────

def _draw_background() -> Image.Image:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), COLORS["bg_top"])
    top, bottom = COLORS["bg_top"], COLORS["bg_bottom"]
    for y in range(CANVAS_H):
        t = y / CANVAS_H
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        ImageDraw.Draw(img).line([(0, y), (CANVAS_W, y)], fill=row)

    nebula = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    nd = ImageDraw.Draw(nebula)
    blobs = [
        (int(CANVAS_W * 0.12), int(CANVAS_H * 0.12), 360, COLORS["nebula_a"], 34),
        (int(CANVAS_W * 0.88), int(CANVAS_H * 0.20), 400, COLORS["nebula_b"], 28),
    ]
    for cx, cy, r, color, alpha in blobs:
        nd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*color, alpha))
    nebula = nebula.filter(ImageFilter.GaussianBlur(130))
    img = Image.alpha_composite(img.convert("RGBA"), nebula)

    import random
    rnd = random.Random(1337)
    sparkle = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sparkle)
    for _ in range(80):
        x, y = rnd.randint(0, CANVAS_W), rnd.randint(0, CANVAS_H)
        r = rnd.choice([1, 1, 1, 2])
        a = rnd.randint(30, 100)
        sd.ellipse((x - r, y - r, x + r, y + r), fill=(230, 220, 245, a))
    img = Image.alpha_composite(img, sparkle)
    return img


def _rounded_panel(img, draw, box, radius=20, fill=None, outline=None, width=2, glow=True):
    fill = fill or COLORS["panel"]
    outline = outline or COLORS["panel_border"]
    if glow:
        x0, y0, x1, y1 = box
        pad = 10
        glow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        gd.rounded_rectangle((x0 - pad, y0 - pad, x1 + pad, y1 + pad),
                             radius=radius + pad, fill=COLORS["panel_glow"])
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(14))
        img.alpha_composite(glow_layer)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


# ─────────────────────────────────────────────────────────────────────────
# LINE-ART UI ICONS — the reference uses thin purple outline icons for all
# fixed UI chrome (messages/voice/games/inventory/calendar/crown) and for
# the DEFAULT unconfigured currency glyphs, not color emoji. These are
# hand-drawn to match that style directly; no network fetch, no risk of
# a blank icon. A genuinely custom emoji (a configured Discord emoji, or
# any non-default unicode emoji an admin picks) is never redrawn -- that
# still goes through the real fetch path below.
# ─────────────────────────────────────────────────────────────────────────

def _icon_canvas(size):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def _line_icon_messages(size):
    im, d = _icon_canvas(size)
    s = size
    d.rounded_rectangle((s*0.12, s*0.18, s*0.88, s*0.68), radius=s*0.16,
                        outline=COLORS["line_icon"], width=max(2, int(s*0.06)))
    d.polygon([(s*0.30, s*0.66), (s*0.30, s*0.86), (s*0.48, s*0.66)],
              fill=COLORS["line_icon"])
    for cx in (0.34, 0.5, 0.66):
        r = s * 0.035
        d.ellipse((s*cx - r, s*0.40 - r, s*cx + r, s*0.40 + r), fill=COLORS["line_icon"])
    return im


def _line_icon_voice(size):
    im, d = _icon_canvas(size)
    s = size
    w = max(2, int(s * 0.06))
    d.rounded_rectangle((s*0.38, s*0.10, s*0.62, s*0.55), radius=s*0.12,
                        outline=COLORS["line_icon"], width=w)
    d.arc((s*0.22, s*0.28, s*0.78, s*0.72), start=20, end=160,
          fill=COLORS["line_icon"], width=w)
    d.line((s*0.5, s*0.68, s*0.5, s*0.86), fill=COLORS["line_icon"], width=w)
    d.line((s*0.36, s*0.86, s*0.64, s*0.86), fill=COLORS["line_icon"], width=w)
    return im


def _line_icon_games(size):
    im, d = _icon_canvas(size)
    s = size
    w = max(2, int(s * 0.06))
    d.rounded_rectangle((s*0.10, s*0.32, s*0.90, s*0.72), radius=s*0.20,
                        outline=COLORS["line_icon"], width=w)
    d.line((s*0.26, s*0.52, s*0.38, s*0.52), fill=COLORS["line_icon"], width=w)
    d.line((s*0.32, s*0.46, s*0.32, s*0.58), fill=COLORS["line_icon"], width=w)
    for cx in (0.64, 0.76):
        r = s * 0.045
        d.ellipse((s*cx - r, s*0.48 - r, s*cx + r, s*0.48 + r), outline=COLORS["line_icon"], width=w)
    return im


def _line_icon_inventory(size):
    im, d = _icon_canvas(size)
    s = size
    w = max(2, int(s * 0.06))
    d.line((s*0.5, s*0.08, s*0.90, s*0.28), fill=COLORS["line_icon"], width=w)
    d.line((s*0.5, s*0.08, s*0.10, s*0.28), fill=COLORS["line_icon"], width=w)
    d.line((s*0.10, s*0.28, s*0.5, s*0.48), fill=COLORS["line_icon"], width=w)
    d.line((s*0.90, s*0.28, s*0.5, s*0.48), fill=COLORS["line_icon"], width=w)
    d.line((s*0.10, s*0.28, s*0.10, s*0.70), fill=COLORS["line_icon"], width=w)
    d.line((s*0.90, s*0.28, s*0.90, s*0.70), fill=COLORS["line_icon"], width=w)
    d.line((s*0.5, s*0.48, s*0.5, s*0.90), fill=COLORS["line_icon"], width=w)
    d.line((s*0.10, s*0.70, s*0.5, s*0.90), fill=COLORS["line_icon"], width=w)
    d.line((s*0.90, s*0.70, s*0.5, s*0.90), fill=COLORS["line_icon"], width=w)
    return im


def _line_icon_calendar(size):
    im, d = _icon_canvas(size)
    s = size
    w = max(1, int(s * 0.09))
    d.rounded_rectangle((s*0.10, s*0.18, s*0.90, s*0.88), radius=s*0.12,
                        outline=COLORS["line_icon"], width=w)
    d.line((s*0.10, s*0.38, s*0.90, s*0.38), fill=COLORS["line_icon"], width=w)
    d.line((s*0.30, s*0.08, s*0.30, s*0.26), fill=COLORS["line_icon"], width=w)
    d.line((s*0.70, s*0.08, s*0.70, s*0.26), fill=COLORS["line_icon"], width=w)
    return im


def _line_icon_crown(size):
    im, d = _icon_canvas(size)
    s = size
    pts = [(s*0.08, s*0.75), (s*0.08, s*0.35), (s*0.30, s*0.55),
           (s*0.5, s*0.15), (s*0.70, s*0.55), (s*0.92, s*0.35),
           (s*0.92, s*0.75)]
    d.polygon(pts, fill=COLORS["line_icon"])
    d.rectangle((s*0.08, s*0.75, s*0.92, s*0.85), fill=COLORS["line_icon"])
    return im


def _line_icon_coin_stack(size):
    im, d = _icon_canvas(size)
    s = size
    w = max(2, int(s * 0.06))
    for i, cy in enumerate((0.30, 0.48, 0.66)):
        d.ellipse((s*0.20, s*cy, s*0.80, s*cy + s*0.20),
                  outline=COLORS["line_icon"], width=w)
    return im


def _line_icon_diamond(size):
    im, d = _icon_canvas(size)
    s = size
    w = max(2, int(s * 0.06))
    pts = [(s*0.5, s*0.10), (s*0.85, s*0.38), (s*0.5, s*0.90), (s*0.15, s*0.38)]
    d.polygon(pts, outline=COLORS["line_icon"], width=w)
    d.line((s*0.15, s*0.38, s*0.85, s*0.38), fill=COLORS["line_icon"], width=w)
    d.line((s*0.5, s*0.10, s*0.5, s*0.90), fill=COLORS["line_icon"], width=1)
    return im


LINE_ICON_BUILDERS = {
    "messages": _line_icon_messages,
    "voice": _line_icon_voice,
    "games": _line_icon_games,
    "inventory": _line_icon_inventory,
    "calendar": _line_icon_calendar,
    "crown": _line_icon_crown,
}

_DEFAULT_CURRENCY_ICON_BUILDERS = {
    "🪙": _line_icon_coin_stack,
    "💎": _line_icon_diamond,
}


def _build_line_icons(size: int) -> dict:
    return {name: fn(size) for name, fn in LINE_ICON_BUILDERS.items()}


# ─────────────────────────────────────────────────────────────────────────
# NETWORK ASSET FETCH (avatar / genuinely-custom emoji only)
# ─────────────────────────────────────────────────────────────────────────

async def _fetch_image(session: aiohttp.ClientSession, url: str) -> Image.Image | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
        im = Image.open(io.BytesIO(data))
        im.load()
        return im.convert("RGBA")
    except Exception as e:
        log.warning("rank_card: failed to fetch %s: %s", url, e)
        return None


def _twemoji_filename(emoji_str: str) -> str:
    codepoints = "-".join(f"{ord(c):x}" for c in emoji_str if ord(c) != 0xFE0F)
    return f"{codepoints}.png"


async def _resolve_currency_icon(session: aiohttp.ClientSession, emoji_str: str,
                                  size: int) -> Image.Image | None:
    """emoji_str is whatever utils.currency.get_currency_config() returned.
    The two DEFAULT glyphs (🪙/💎, unconfigured) render as the reference's
    hand-drawn purple line icons -- everything else (a real configured
    Discord emoji, or any other unicode emoji an admin picked) is fetched
    as the actual asset, never redrawn."""
    if not emoji_str:
        return None
    if emoji_str in _DEFAULT_CURRENCY_ICON_BUILDERS:
        return _DEFAULT_CURRENCY_ICON_BUILDERS[emoji_str](size)
    if is_custom_emoji_token(emoji_str):
        parsed = parse_emoji_input(emoji_str)
        if not parsed:
            return None
        emoji_id, _name, animated = parsed
        url = emoji_cdn_url(emoji_id, animated=animated)
        im = await _fetch_image(session, url)
        if im is None:
            return None
        if getattr(im, "is_animated", False):
            im.seek(0)
            im = im.convert("RGBA")
        return im.resize((size, size), Image.LANCZOS)
    url = TWEMOJI_BASE + _twemoji_filename(emoji_str)
    im = await _fetch_image(session, url)
    if im is None:
        return None
    return im.resize((size, size), Image.LANCZOS)


def _circle_mask_paste(base: Image.Image, im: Image.Image, box):
    x, y, w, h = box
    im = ImageOps.fit(im, (w, h), Image.LANCZOS)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, w, h), fill=255)
    base.paste(im, (x, y), mask)


def _placeholder_avatar(size) -> Image.Image:
    im = Image.new("RGBA", (size, size), (58, 38, 88, 255))
    d = ImageDraw.Draw(im)
    d.ellipse((size * 0.18, size * 0.14, size * 0.82, size * 0.62), fill=(88, 64, 128, 255))
    d.ellipse((size * 0.05, size * 0.62, size * 0.95, size * 1.25), fill=(88, 64, 128, 255))
    return im


# ─────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────

async def render_rank_card(data: dict) -> io.BytesIO:
    img = _draw_background()
    draw = ImageDraw.Draw(img)

    line_icons_30 = _build_line_icons(30)
    line_icons_16 = _build_line_icons(16)

    async with aiohttp.ClientSession() as session:
        avatar_im, coin_icon_im, diamond_icon_im = await asyncio.gather(
            _fetch_avatar(session, data.get("avatar_url")),
            _resolve_currency_icon(session, data["currency"]["coins"]["emoji"], 32),
            _resolve_currency_icon(session, data["currency"]["diamonds"]["emoji"], 32),
        )
    mailbox_im = await _load_mailbox()

    _draw_avatar_and_level(img, draw, data)
    _draw_name_block(img, draw, data, line_icons_16)
    _draw_rank_prestige_panel(img, draw, data, line_icons_16)
    _draw_level_xp_panels(img, draw, data)
    _draw_stat_cards(img, draw, data, coin_icon_im, diamond_icon_im, line_icons_30)
    _draw_inventory(img, draw, data, line_icons_16)
    if mailbox_im is not None:
        _draw_mailbox(img, mailbox_im)
    _draw_footer(draw)

    # avatar pasted after background/glow but the placeholder/fetch needs
    # to happen before drawing -- done inline in _draw_avatar_and_level
    # via the resolved avatar_im captured above.
    _paste_avatar(img, data, avatar_im)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf


async def _fetch_avatar(session, avatar_url) -> Image.Image:
    if avatar_url:
        im = await _fetch_image(session, avatar_url)
        if im is not None:
            return im
    ax, ay, aw, ah = LAYOUT["avatar"]
    return _placeholder_avatar(max(aw, ah))


async def _load_mailbox() -> Image.Image | None:
    if not os.path.isfile(MAILBOX_PNG_PATH):
        log.warning("rank_card: mailbox.png not found at %s -- skipping mailbox region "
                    "until the real asset is added.", MAILBOX_PNG_PATH)
        return None
    try:
        im = Image.open(MAILBOX_PNG_PATH)
        im.load()
        return im.convert("RGBA")
    except Exception as e:
        log.warning("rank_card: failed to load mailbox.png: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────
# REGION DRAWERS
# ─────────────────────────────────────────────────────────────────────────

def _star_point(cx, cy, r, angle_deg):
    a = math.radians(angle_deg)
    return (cx + r * math.sin(a), cy - r * math.cos(a))


def _draw_small_star(draw, cx, cy, r, color):
    pts = [
        _star_point(cx, cy, r, 0), _star_point(cx, cy, r * 0.35, 45),
        _star_point(cx, cy, r, 90), _star_point(cx, cy, r * 0.35, 135),
        _star_point(cx, cy, r, 180), _star_point(cx, cy, r * 0.35, 225),
        _star_point(cx, cy, r, 270), _star_point(cx, cy, r * 0.35, 315),
    ]
    draw.polygon(pts, fill=color)


def _draw_avatar_and_level(img, draw, data):
    """Ring, glow, star ornaments and the level badge -- everything except
    the actual avatar photo, which is pasted in _paste_avatar() after all
    panels are drawn so the ring's glow doesn't get painted over it."""
    ax, ay, aw, ah = LAYOUT["avatar"]
    cx, cy = ax + aw / 2, ay + ah / 2
    r = aw / 2

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((cx - r - 16, cy - r - 16, cx + r + 16, cy + r + 16),
              fill=(*COLORS["ring"], 55))
    glow = glow.filter(ImageFilter.GaussianBlur(18))
    img.alpha_composite(glow)


def _paste_avatar(img, data, avatar_im):
    draw = ImageDraw.Draw(img)
    ax, ay, aw, ah = LAYOUT["avatar"]
    cx, cy = ax + aw / 2, ay + ah / 2
    r = aw / 2

    _circle_mask_paste(img, avatar_im, (int(ax), int(ay), int(aw), int(ah)))

    draw.ellipse((cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9),
                 outline=COLORS["ring"], width=6)
    draw.ellipse((cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9),
                 outline=COLORS["accent"], width=1)

    # Star ornaments -- one larger star at 12 o'clock, four smaller ones
    # spaced around, echoing the reference's "crown of stars" without
    # trying to replicate every one of its irregular accent shapes.
    positions = [(-90, 12), (-18, 7), (54, 7), (126, 7), (198, 7), (270, 7)]
    ring_r = r + 9
    for angle, star_r in positions:
        sx, sy = _star_point(cx, cy, ring_r, angle)
        _draw_small_star(draw, sx, sy, star_r, COLORS["accent"])

    badge_r = LAYOUT["avatar_level_badge_r"]
    bx = ax + aw * 0.78
    by = ay + ah * 0.97
    draw.ellipse((bx - badge_r, by - badge_r, bx + badge_r, by + badge_r),
                 fill=(16, 9, 26, 235), outline=COLORS["accent"], width=3)
    lvl_font = zilla_bold(24)
    lvl_text = str(data["level"])
    bbox = draw.textbbox((0, 0), lvl_text, font=lvl_font)
    draw.text((bx - (bbox[2] - bbox[0]) / 2, by - (bbox[3] - bbox[1]) / 2 - bbox[1]),
              lvl_text, font=lvl_font, fill=COLORS["text_primary"])


def _draw_name_block(img, draw, data, icons16):
    x, y, w, h = LAYOUT["name"]
    username = data.get("username") or f"User {data['user_id']}"
    if username.isascii() and username.isupper() is False and username.replace(" ", "").isalpha():
        # Reference drop-cap treatment: oversized first letter + smaller
        # caps for the rest -- applied only to plain-alphabetic Latin
        # names, matching what the reference actually demonstrates.
        _draw_dropcap_heading(draw, (x, y), username.upper(), "cinzel", 52, 38,
                              COLORS["text_primary"])
    else:
        name_font = cinzel_bold(46) if username.isascii() else tajawal(40, "bold")
        _draw_tracked_text(draw, (x, y), username, name_font, COLORS["text_primary"], tracking=1)

    title = data.get("equipped_title")
    if title:
        px, py, pw, ph = LAYOUT["title_pill"]
        _rounded_panel(img, draw, (px, py, px + pw, py + ph), radius=ph // 2)
        icon_x = px + 14
        if icons16.get("crown") is not None:
            img.paste(icons16["crown"], (icon_x, int(py + (ph - 16) / 2)), icons16["crown"])
            text_x = icon_x + 20
        else:
            text_x = icon_x
        _draw_tracked_text(draw, (text_x, py + ph / 2 - 7), title["item_name"],
                           outfit(13, "SemiBold"), COLORS["accent"], tracking=1)

    ms_x, ms_y, _, _ = LAYOUT["member_since"]
    member_since = data.get("member_since")
    if member_since:
        date_str = member_since.strftime("%b %d, %Y")
        text_x = ms_x
        if icons16.get("calendar") is not None:
            img.paste(icons16["calendar"], (ms_x, ms_y), icons16["calendar"])
            text_x = ms_x + 22
        draw.text((text_x, ms_y), "Member since  ·  ", font=outfit(17), fill=COLORS["text_muted"])
        w0 = draw.textbbox((0, 0), "Member since  ·  ", font=outfit(17))[2]
        draw.text((text_x + w0, ms_y), date_str, font=outfit(17), fill=COLORS["text_bright"])


def _draw_rank_prestige_panel(img, draw, data, icons16):
    x, y, w, h = LAYOUT["rank_prestige_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=18)

    _draw_tracked_text(draw, (x + 18, y + 18), "RANK", outfit(14, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    draw.text((x + 16, y + 40), f"#{data['rank']}", font=zilla_bold(46),
              fill=COLORS["text_primary"])

    # Two-tone: "TOP" muted, the percentage itself brighter -- matches the
    # reference's emphasis treatment.
    ty = y + 102
    draw.text((x + 18, ty), "TOP ", font=outfit(14, "Medium"), fill=COLORS["text_muted"])
    tw = draw.textbbox((0, 0), "TOP ", font=outfit(14, "Medium"))[2]
    draw.text((x + 18 + tw, ty), f"{data['percentile']:.2f}%",
              font=outfit(14, "SemiBold"), fill=COLORS["text_bright"])

    draw.line((x + 18, y + 132, x + w - 18, y + 132), fill=(*COLORS["accent"], 45), width=1)

    tier = data["effective_prestige"]
    roman = ["0", "I", "II", "III", "IV", "V", "VI"][tier] if 0 <= tier <= 6 else str(tier)
    label = f"PRESTIGE {roman}" if tier else "PRESTIGE"
    big_font = cinzel_semibold(20)
    small_font = cinzel_semibold(15)
    first_w = draw.textbbox((0, 0), label[0], font=big_font)[2]
    rest_w = sum(draw.textbbox((0, 0), ch, font=small_font)[2] + 1 for ch in label[1:])
    total_w = first_w + 1 + rest_w
    lx = x + w / 2 - total_w / 2
    ly = y + 146
    _draw_dropcap_heading(draw, (lx, ly), label, "cinzel", 20, 15, COLORS["accent"])

    pip_r = 10
    gap = 9
    total_pips = 6
    row_w = total_pips * (pip_r * 2) + (total_pips - 1) * gap
    start_x = x + w / 2 - row_w / 2 + pip_r
    pip_y = y + 182
    for i in range(total_pips):
        cx = start_x + i * (pip_r * 2 + gap)
        filled = i < tier
        color = COLORS["pip_filled"] if filled else COLORS["pip_empty"]
        _draw_diamond_pip(draw, cx, pip_y, pip_r, color, filled)

    if data.get("is_booster"):
        by = y + 206
        bw = w - 36
        bx = x + 18
        _rounded_panel(img, draw, (bx, by, bx + bw, by + 22), radius=11,
                       fill=(88, 30, 128, 190), outline=COLORS["accent"], glow=False)
        icon = icons16.get("crown")
        lf2 = outfit(10, "SemiBold")
        label2 = "BOOSTER PRESTIGE"
        lbbox = draw.textbbox((0, 0), label2, font=lf2)
        lw = lbbox[2] - lbbox[0]
        icon_w = 20 if icon is not None else 0
        block_w = icon_w + lw
        start = bx + (bw - block_w) / 2
        if icon is not None:
            img.paste(icon, (int(start), int(by + 3)), icon)
        draw.text((start + icon_w, by + 11), label2, font=lf2,
                  fill=COLORS["text_primary"], anchor="lm")


def _draw_diamond_pip(draw, cx, cy, r, color, filled):
    pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    if filled:
        draw.polygon(pts, fill=color)
    else:
        draw.polygon(pts, outline=color, width=2)


def _draw_level_xp_panels(img, draw, data):
    x, y, w, h = LAYOUT["level_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=18)
    _draw_tracked_text(draw, (x + 18, y + 16), "LEVEL", outfit(14, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    draw.text((x + 18, y + 44), str(data["level"]), font=zilla_bold(58),
              fill=COLORS["text_primary"])

    x, y, w, h = LAYOUT["xp_totalxp_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=18)
    xp_w = int(w * LAYOUT["xp_section_frac"])

    _draw_tracked_text(draw, (x + 20, y + 16), "XP PROGRESS", outfit(14, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    cur, needed = data["xp_current"], max(data["xp_needed"], 1)
    # Two-tone: current XP bright/emphasized, "/ needed XP" muted.
    cur_txt = f"{cur:,}"
    vf = zilla_bold(22)
    draw.text((x + 20, y + 38), cur_txt, font=vf, fill=COLORS["text_bright"])
    cur_w = draw.textbbox((0, 0), cur_txt, font=vf)[2]
    draw.text((x + 24 + cur_w, y + 42), f"/ {needed:,} XP", font=outfit(16),
              fill=COLORS["text_muted"])

    bar_x, bar_y, bar_w, bar_h = x + 20, y + 78, xp_w - 40, 14
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                           radius=bar_h // 2, fill=COLORS["xp_bar_bg"])
    frac = min(cur / needed, 1.0)
    if frac > 0:
        fill_w = bar_w * frac
        _draw_gradient_bar(img, bar_x, bar_y, fill_w, bar_h,
                           COLORS["xp_bar_fill_a"], COLORS["xp_bar_fill_b"])
        # Subtle highlight at the leading edge of the FILL itself (not the
        # bar's outer end) -- per the reference's fidelity note. Kept to a
        # small soft dot, no extra ornamentation.
        if 4 < fill_w < bar_w - 2:
            hx, hy = bar_x + fill_w - 3, bar_y + bar_h / 2
            _draw_soft_dot(img, hx, hy, 4, (255, 255, 255, 165))
    draw.text((bar_x, bar_y + bar_h + 8), f"{frac * 100:.1f}% to next level",
              font=outfit(13), fill=COLORS["text_muted"])

    div_x = x + xp_w
    draw.line((div_x, y + 16, div_x, y + h - 16), fill=(*COLORS["accent"], 40), width=1)
    tx = div_x + 20
    _draw_tracked_text(draw, (tx, y + 16), "TOTAL XP", outfit(14, "SemiBold"),
                       COLORS["text_muted"], tracking=3)
    draw.text((tx, y + 46), f"{data['xp_total']:,}", font=zilla_bold(26),
              fill=COLORS["text_primary"])


def _draw_gradient_bar(img, x, y, w, h, color_a, color_b):
    if w <= 0:
        return
    w = int(w)
    grad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for i in range(w):
        t = i / max(w - 1, 1)
        col = tuple(int(color_a[c] + (color_b[c] - color_a[c]) * t) for c in range(3))
        ImageDraw.Draw(grad).line([(i, 0), (i, h)], fill=(*col, 255))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=h // 2, fill=255)
    img.paste(grad, (int(x), int(y)), mask)


def _draw_soft_dot(img, cx, cy, r, color):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    layer = layer.filter(ImageFilter.GaussianBlur(2))
    img.alpha_composite(layer)


def _draw_stat_cards(img, draw, data, coin_icon_im, diamond_icon_im, icons30):
    coins_cfg = data["currency"]["coins"]
    diamonds_cfg = data["currency"]["diamonds"]

    cards = [
        (icons30.get("messages"), "MESSAGES", f"{data['messages_count']:,}"),
        (icons30.get("voice"), "VOICE TIME", _fmt_minutes(data["voice_minutes"])),
        (coin_icon_im, coins_cfg["name"].upper(), f"{data['balance']:,}"),
        (diamond_icon_im, diamonds_cfg["name"].upper(), f"{data['diamonds']:,}"),
        (icons30.get("games"), "GAMES WON", f"{data['minigame_wins']:,}"),
    ]

    y = LAYOUT["stats_row_y"]
    h = LAYOUT["stats_row_h"]
    w = LAYOUT["stats_card_w"]
    gap = LAYOUT["stats_gap"]
    x0 = LAYOUT["stats_start_x"]

    for i, (icon_im, label, value) in enumerate(cards):
        x = x0 + i * (w + gap)
        _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16)
        if icon_im is not None:
            img.paste(icon_im, (int(x + w / 2 - icon_im.width / 2), y + 18), icon_im)
        vf = zilla_bold(21)
        vb = draw.textbbox((0, 0), value, font=vf)
        draw.text((x + w / 2 - (vb[2] - vb[0]) / 2, y + h - 56), value,
                  font=vf, fill=COLORS["text_primary"])
        lf = outfit(10, "Medium")
        _draw_tracked_text(draw, (x + w / 2, y + h - 24), label, lf, COLORS["text_muted"],
                           tracking=1, anchor="mm")


def _fmt_minutes(total_minutes: int) -> str:
    h = total_minutes // 60
    return f"{h}h" if h else f"{total_minutes}m"


def _draw_inventory(img, draw, data, icons16):
    x, y, w, h = LAYOUT["inventory_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=18)
    label_x = x + 20
    if icons16.get("inventory") is not None:
        img.paste(icons16["inventory"], (label_x, y + 18), icons16["inventory"])
        label_x += 22
    _draw_tracked_text(draw, (label_x, y + 18), "INVENTORY", outfit(15, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    draw.line((x + 18, y + 48, x + w - 18, y + 48), fill=(*COLORS["accent"], 35), width=1)

    ox, oy = LAYOUT["inventory_grid_origin"]
    sw, sh = LAYOUT["inventory_slot"]
    gx, gy = LAYOUT["inventory_slot_gap_x"], LAYOUT["inventory_slot_gap_y"]
    cols, rows = LAYOUT["inventory_cols"], LAYOUT["inventory_rows"]
    items = data.get("inventory_grid") or []

    for i in range(cols * rows):
        col, row = i % cols, i // cols
        sx = ox + col * (sw + gx)
        sy = oy + row * (sh + gy)
        draw.rounded_rectangle((sx, sy, sx + sw, sy + sh), radius=10,
                               fill=(30, 20, 46, 140), outline=(*COLORS["accent"], 55), width=1)
        if i < len(items):
            _draw_inventory_item(draw, sx, sy, sw, sh, items[i])


def _draw_inventory_item(draw, sx, sy, sw, sh, item):
    """Item art itself would come from utils.item_catalog icon_url (a
    remote asset, fetched the same way currency icons are) -- left as a
    neutral placeholder tile here since per-item icon fetching is outside
    this fidelity pass's scope. Only the REAL owned quantity is ever
    shown, and only when it's actually more than 1 -- never a placeholder
    or invented number."""
    draw.rounded_rectangle((sx + 6, sy + 6, sx + sw - 6, sy + sh - 6), radius=6,
                           fill=(58, 38, 88, 190))
    qty = item.get("quantity", 1)
    if qty and qty > 1:
        draw.text((sx + sw - 7, sy + sh - 7), f"x{qty}",
                  font=outfit(11, "Bold"), fill=COLORS["text_primary"], anchor="rs")


def _draw_mailbox(img, mailbox_im):
    x0, x1 = LAYOUT["mailbox_column_x"]
    bottom_y = LAYOUT["mailbox_bottom_y"]
    top_min_y = LAYOUT["mailbox_top_min_y"]

    col_w = (x1 - x0) - 20
    max_h_by_height = bottom_y - top_min_y
    aspect = mailbox_im.height / mailbox_im.width

    h_if_width_bound = col_w * aspect
    if h_if_width_bound <= max_h_by_height:
        new_w, new_h = col_w, int(h_if_width_bound)
    else:
        new_h = max_h_by_height
        new_w = int(new_h / aspect)

    mb = mailbox_im.resize((new_w, new_h), Image.LANCZOS)
    center_x = (x0 + x1) / 2
    left = int(center_x - new_w / 2)
    top = bottom_y - new_h

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = new_w // 2
    gd.ellipse((center_x - gr, bottom_y - gr // 4, center_x + gr, bottom_y + gr // 4),
              fill=(*COLORS["purple_glow"], 75))
    glow = glow.filter(ImageFilter.GaussianBlur(26))
    img.alpha_composite(glow)

    reflection = ImageOps.flip(mb).copy()
    r_alpha = reflection.split()[3].point(lambda p: int(p * 0.16))
    reflection.putalpha(r_alpha)
    img.alpha_composite(reflection, (left, bottom_y))

    img.alpha_composite(mb, (left, top))


def _draw_footer(draw):
    text = "✦  عالمنا صغير، ولكن الإلهام فيه بلا حدود  ✦"
    f = amiri(19)
    bbox = draw.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    x = (CANVAS_W - tw) / 2
    draw.text((x, LAYOUT["footer_y"]), text, font=f, fill=COLORS["text_muted"])
