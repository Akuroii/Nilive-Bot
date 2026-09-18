"""
RANK CARD RENDERER — draws the payload from utils/rank_card_data.py onto a
fixed 1280x853 PNG.

Deliberately kept discord.py-free: it only reads the plain dict that
get_rank_card_data() already resolved (username/avatar_url/member_since/
is_booster are plain values, not a live discord.Member). That means this
module is reusable from a non-bot context later (e.g. a dashboard preview)
without dragging discord.py along.

Layout is data (see LAYOUT below), drawing is code. Moving/resizing a
region means editing LAYOUT, not the draw functions.

LAYOUT was measured directly off the approved reference design (grid-
overlaid at 50px, reference canvas 1024x682, scaled x1.25 to this
module's 1280x853 canvas) rather than eyeballed, so panel groupings and
proportions track the reference deliberately.
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

# The exact transparent mailbox PNG the user supplies -- untouched (only
# ever cropped to its own content bbox at install time, never redrawn or
# recolored), never regenerated. If it isn't present yet, the mailbox
# region is skipped (logged once) rather than substituted with anything.
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
    "panel": (21, 13, 33, 165),          # softer fill, less opaque than before
    "panel_border": (150, 100, 210, 70), # softer, lower-alpha border (was 120)
    "panel_glow": (130, 80, 190, 40),    # faint outer glow behind each panel
    "purple_glow": (170, 100, 235),
    "text_primary": (240, 235, 250),
    "text_muted": (168, 152, 194),
    "accent": (196, 140, 255),
    "pip_filled": (196, 110, 255),
    "pip_empty": (68, 54, 88),
    "xp_bar_bg": (38, 25, 56),
    "xp_bar_fill": (185, 100, 240),
    "ring": (176, 120, 235),
}

# ─────────────────────────────────────────────────────────────────────────
# LAYOUT — fixed canvas 1280x853, measured off the reference (x1.25 scale)
# ─────────────────────────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 1280, 853

LAYOUT = {
    "avatar": (44, 54, 240, 240),
    "avatar_level_badge_r": 30,
    "avatar_ring_pips": 5,          # small star ornaments around the ring

    "name": (327, 90, 600, 60),
    "title_pill": (327, 197, 250, 43),
    "member_since": (327, 272, 500, 28),

    # Rank + Prestige: ONE merged panel, Rank on top, Prestige below.
    "rank_prestige_panel": (706, 56, 213, 256),
    "rank_section_h": 128,          # top portion of the merged panel
    # (bottom portion = rank_prestige_panel height - rank_section_h)

    # Level: its own panel. XP Progress + Total XP: one merged panel to
    # its right (XP progress ~62%, Total XP ~38% of that panel's width).
    "level_panel": (40, 375, 179, 175),
    "xp_totalxp_panel": (225, 375, 475, 175),
    "xp_section_frac": 0.62,

    "stats_row_y": 575,
    "stats_row_h": 165,
    "stats_card_w": 116,
    "stats_gap": 16,
    "stats_start_x": 40,

    # Tall right-side column, same left edge as the Rank/Prestige panel
    # above it, ending at the same bottom edge as the stats row.
    "inventory_panel": (706, 322, 281, 418),
    "inventory_grid_origin": (730, 366),
    "inventory_slot": (56, 56),
    "inventory_slot_gap": 12,
    "inventory_cols": 4,
    "inventory_rows": 3,

    # Mailbox column: clear of the Inventory panel's right edge (987) with
    # a margin, anchored to the bottom, height derives from the real
    # asset's own aspect ratio at render time (see _draw_mailbox).
    "mailbox_column_x": (1007, 1280),
    "mailbox_bottom_y": 830,
    "mailbox_top_min_y": 60,        # won't be pushed higher than this

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
    character-by-character with extra spacing between glyphs -- used for
    the reference's wider-tracked small-caps labels (RANK, LEVEL, stat
    labels). Returns the total rendered width."""
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


# ─────────────────────────────────────────────────────────────────────────
# BACKGROUND — code/vector nebula, kept subtle/concentrated (not a galaxy
# scene) per direction: fewer, softer blobs than the previous pass.
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
    # Fewer, more concentrated blobs than the previous pass (was 3 bright
    # blobs at 40-55 alpha); kept subtle so the mood stays dark/controlled.
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
    for _ in range(80):  # fewer stars -- concentrated, not a galaxy scene
        x, y = rnd.randint(0, CANVAS_W), rnd.randint(0, CANVAS_H)
        r = rnd.choice([1, 1, 1, 2])
        a = rnd.randint(30, 100)
        sd.ellipse((x - r, y - r, x + r, y + r), fill=(230, 220, 245, a))
    img = Image.alpha_composite(img, sparkle)
    return img


def _rounded_panel(img, draw, box, radius=20, fill=None, outline=None, width=2, glow=True):
    """Softer, more atmospheric panel: lower-alpha fill/border plus an
    optional faint blurred glow behind it, instead of a flat bright
    outline."""
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
# NETWORK ASSET FETCH (avatar / emoji) — graceful fallback on any failure
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


_UNICODE_TWEMOJI_OVERRIDES = {
    "🪙": "1fa99",
    "💎": "1f48e",
}


def _twemoji_filename(emoji_str: str) -> str:
    if emoji_str in _UNICODE_TWEMOJI_OVERRIDES:
        return _UNICODE_TWEMOJI_OVERRIDES[emoji_str] + ".png"
    codepoints = "-".join(f"{ord(c):x}" for c in emoji_str if ord(c) != 0xFE0F)
    return f"{codepoints}.png"


async def _resolve_currency_icon(session: aiohttp.ClientSession, emoji_str: str,
                                  size: int) -> Image.Image | None:
    """emoji_str is whatever utils.currency.get_currency_config() returned
    for that currency: either a raw `<:name:id>` / `<a:name:id>` token, or
    a plain unicode emoji."""
    if not emoji_str:
        return None
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
    return await _fetch_unicode_emoji(session, emoji_str, size)


async def _fetch_unicode_emoji(session: aiohttp.ClientSession, emoji_str: str,
                                size: int) -> Image.Image | None:
    url = TWEMOJI_BASE + _twemoji_filename(emoji_str)
    im = await _fetch_image(session, url)
    if im is None:
        return None
    return im.resize((size, size), Image.LANCZOS)


UI_ICONS = {
    "messages": "💬", "voice": "🎙️", "games": "🎮",
    "inventory": "📦", "calendar": "📅", "crown": "👑",
}


async def _fetch_ui_icons(session: aiohttp.ClientSession, size: int) -> dict:
    results = await asyncio.gather(
        *[_fetch_unicode_emoji(session, glyph, size) for glyph in UI_ICONS.values()]
    )
    return dict(zip(UI_ICONS.keys(), results))


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

    async with aiohttp.ClientSession() as session:
        avatar_im, coin_icon_im, diamond_icon_im, mailbox_im, ui_icons_28, ui_icons_16 = \
            await asyncio.gather(
                _fetch_avatar(session, data.get("avatar_url")),
                _resolve_currency_icon(session, data["currency"]["coins"]["emoji"], 32),
                _resolve_currency_icon(session, data["currency"]["diamonds"]["emoji"], 32),
                _load_mailbox(),
                _fetch_ui_icons(session, 30),
                _fetch_ui_icons(session, 16),
            )

    _draw_avatar_and_level(img, draw, data, avatar_im)
    _draw_name_block(img, draw, data, ui_icons_16)
    _draw_rank_prestige_panel(img, draw, data, ui_icons_16)
    _draw_level_xp_panels(img, draw, data)
    _draw_stat_cards(img, draw, data, coin_icon_im, diamond_icon_im, ui_icons_28)
    _draw_inventory(img, draw, data, ui_icons_16)
    if mailbox_im is not None:
        _draw_mailbox(img, mailbox_im)
    _draw_footer(draw)

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
    """A compact 4-point star/sparkle glyph -- the ring ornaments, kept
    simple and controlled (not a new decorative motif, just the star
    shape already used for the pip diamonds, at a smaller scale)."""
    pts = [
        _star_point(cx, cy, r, 0), _star_point(cx, cy, r * 0.35, 45),
        _star_point(cx, cy, r, 90), _star_point(cx, cy, r * 0.35, 135),
        _star_point(cx, cy, r, 180), _star_point(cx, cy, r * 0.35, 225),
        _star_point(cx, cy, r, 270), _star_point(cx, cy, r * 0.35, 315),
    ]
    draw.polygon(pts, fill=color)


def _draw_avatar_and_level(img, draw, data, avatar_im):
    ax, ay, aw, ah = LAYOUT["avatar"]
    cx, cy = ax + aw / 2, ay + ah / 2
    r = aw / 2

    # Soft glow behind the ring
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((cx - r - 16, cy - r - 16, cx + r + 16, cy + r + 16),
              fill=(*COLORS["ring"], 55))
    glow = glow.filter(ImageFilter.GaussianBlur(18))
    img.alpha_composite(glow)

    # Double ring (outer thin + inner slightly thicker), closer to the
    # reference's layered ring treatment than a single flat stroke.
    draw.ellipse((cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10),
                 outline=COLORS["ring"], width=2)
    draw.ellipse((cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4),
                 outline=COLORS["accent"], width=3)

    _circle_mask_paste(img, avatar_im, (int(ax), int(ay), int(aw), int(ah)))

    # Star ornaments spaced around the ring, echoing the reference's
    # "crown of stars" treatment -- kept to small, evenly spaced points,
    # no extra motifs.
    n = LAYOUT["avatar_ring_pips"]
    ring_r = r + 10
    for i in range(n):
        angle = -90 + i * (360 / n)
        sx, sy = _star_point(cx, cy, ring_r, angle)
        star_r = 11 if i == 0 else 7  # slightly larger star at the top
        _draw_small_star(draw, sx, sy, star_r, COLORS["accent"])

    badge_r = LAYOUT["avatar_level_badge_r"]
    bx, by = ax + aw - badge_r * 0.7, ay + ah - badge_r * 0.7
    draw.ellipse((bx - badge_r, by - badge_r, bx + badge_r, by + badge_r),
                 fill=(16, 9, 26, 255), outline=COLORS["accent"], width=3)
    lvl_font = zilla_bold(24)
    lvl_text = str(data["level"])
    bbox = draw.textbbox((0, 0), lvl_text, font=lvl_font)
    draw.text((bx - (bbox[2] - bbox[0]) / 2, by - (bbox[3] - bbox[1]) / 2 - bbox[1]),
              lvl_text, font=lvl_font, fill=COLORS["text_primary"])


def _draw_name_block(img, draw, data, icons16):
    x, y, w, h = LAYOUT["name"]
    username = data.get("username") or f"User {data['user_id']}"
    name_font = cinzel_bold(48) if username.isascii() else tajawal(42, "bold")
    _draw_tracked_text(draw, (x, y), username, name_font, COLORS["text_primary"], tracking=1)

    title = data.get("equipped_title")
    if title:
        px, py, pw, ph = LAYOUT["title_pill"]
        _rounded_panel(img, draw, (px, py, px + pw, py + ph), radius=ph // 2)
        icon_x = px + 16
        if icons16.get("crown") is not None:
            img.paste(icons16["crown"], (icon_x, int(py + (ph - 16) / 2)), icons16["crown"])
            text_x = icon_x + 22
        else:
            text_x = icon_x
        _draw_tracked_text(draw, (text_x, py + ph / 2 - 8), title["item_name"],
                           outfit(15, "SemiBold"), COLORS["accent"], tracking=1)

    ms_x, ms_y, _, _ = LAYOUT["member_since"]
    member_since = data.get("member_since")
    if member_since:
        date_str = member_since.strftime("%b %d, %Y")
        text_x = ms_x
        if icons16.get("calendar") is not None:
            img.paste(icons16["calendar"], (ms_x, ms_y), icons16["calendar"])
            text_x = ms_x + 22
        draw.text((text_x, ms_y), f"Member since  ·  {date_str}",
                  font=outfit(17), fill=COLORS["text_muted"])


def _draw_rank_prestige_panel(img, draw, data, icons16):
    x, y, w, h = LAYOUT["rank_prestige_panel"]
    rank_h = LAYOUT["rank_section_h"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=18)

    # -- Rank section (top) --
    _draw_tracked_text(draw, (x + 18, y + 14), "RANK", outfit(14, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    draw.text((x + 18, y + 34), f"#{data['rank']}", font=zilla_bold(42),
              fill=COLORS["text_primary"])
    draw.text((x + 18, y + rank_h - 26), f"TOP {data['percentile']:.2f}%",
              font=outfit(14, "Medium"), fill=COLORS["accent"])

    # Faint divider between the two sections of the merged panel
    draw.line((x + 18, y + rank_h, x + w - 18, y + rank_h),
              fill=(*COLORS["accent"], 40), width=1)

    # -- Prestige section (bottom) --
    py0 = y + rank_h + 10
    tier = data["effective_prestige"]
    roman = ["0", "I", "II", "III", "IV", "V", "VI"][tier] if 0 <= tier <= 6 else str(tier)
    label = f"PRESTIGE {roman}" if tier else "PRESTIGE"
    lf = cinzel_semibold(15)
    lb = draw.textbbox((0, 0), label, font=lf)
    _draw_tracked_text(draw, (x + w / 2 - (lb[2] - lb[0]) / 2, py0), label, lf,
                       COLORS["accent"], tracking=1)

    pip_r = 10
    gap = 9
    total_pips = 6
    row_w = total_pips * (pip_r * 2) + (total_pips - 1) * gap
    start_x = x + w / 2 - row_w / 2 + pip_r
    pip_y = py0 + 36
    for i in range(total_pips):
        cx = start_x + i * (pip_r * 2 + gap)
        filled = i < tier
        color = COLORS["pip_filled"] if filled else COLORS["pip_empty"]
        _draw_diamond_pip(draw, cx, pip_y, pip_r, color, filled)

    if data.get("is_booster"):
        by = py0 + 58
        bw = w - 36
        bx = x + 18
        _rounded_panel(img, draw, (bx, by, bx + bw, by + 24), radius=12,
                       fill=(88, 30, 128, 190), outline=COLORS["accent"], glow=False)
        icon = icons16.get("crown")
        lf2 = outfit(11, "SemiBold")
        label2 = "BOOSTER PRESTIGE"
        lbbox = draw.textbbox((0, 0), label2, font=lf2)
        lw = lbbox[2] - lbbox[0]
        icon_w = 16 if icon is not None else 0
        block_w = icon_w + lw
        start = bx + (bw - block_w) / 2
        if icon is not None:
            img.paste(icon, (int(start), int(by + 4)), icon)
        draw.text((start + icon_w, by + 12), label2, font=lf2,
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

    # XP Progress (left section of the merged panel)
    _draw_tracked_text(draw, (x + 20, y + 16), "XP PROGRESS", outfit(14, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    cur, needed = data["xp_current"], max(data["xp_needed"], 1)
    draw.text((x + 20, y + 38), f"{cur:,} / {needed:,} XP",
              font=zilla_bold(22), fill=COLORS["text_primary"])
    bar_x, bar_y, bar_w, bar_h = x + 20, y + 78, xp_w - 40, 14
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                           radius=bar_h // 2, fill=COLORS["xp_bar_bg"])
    frac = min(cur / needed, 1.0)
    if frac > 0:
        draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w * frac, bar_y + bar_h),
                               radius=bar_h // 2, fill=COLORS["xp_bar_fill"])
    draw.text((bar_x, bar_y + bar_h + 8), f"{frac * 100:.1f}% to next level",
              font=outfit(13), fill=COLORS["text_muted"])

    # Divider, then Total XP (right section, same merged panel)
    div_x = x + xp_w
    draw.line((div_x, y + 16, div_x, y + h - 16), fill=(*COLORS["accent"], 40), width=1)
    tx = div_x + 20
    _draw_tracked_text(draw, (tx, y + 16), "TOTAL XP", outfit(14, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    draw.text((tx, y + 46), f"{data['xp_total']:,}", font=zilla_bold(26),
              fill=COLORS["text_primary"])


def _draw_stat_cards(img, draw, data, coin_icon_im, diamond_icon_im, icons28):
    coins_cfg = data["currency"]["coins"]
    diamonds_cfg = data["currency"]["diamonds"]

    cards = [
        (icons28.get("messages"), "MESSAGES", f"{data['messages_count']:,}"),
        (icons28.get("voice"), "VOICE TIME", _fmt_minutes(data["voice_minutes"])),
        (coin_icon_im, coins_cfg["name"].upper(), f"{data['balance']:,}"),
        (diamond_icon_im, diamonds_cfg["name"].upper(), f"{data['diamonds']:,}"),
        (icons28.get("games"), "GAMES WON", f"{data['minigame_wins']:,}"),
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
            img.paste(icon_im, (int(x + w / 2 - icon_im.width / 2), y + 16), icon_im)
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

    ox, oy = LAYOUT["inventory_grid_origin"]
    sw, sh = LAYOUT["inventory_slot"]
    gap = LAYOUT["inventory_slot_gap"]
    cols, rows = LAYOUT["inventory_cols"], LAYOUT["inventory_rows"]
    items = data.get("inventory_grid") or []

    for i in range(cols * rows):
        col, row = i % cols, i // cols
        sx = ox + col * (sw + gap)
        sy = oy + row * (sh + gap)
        draw.rounded_rectangle((sx, sy, sx + sw, sy + sh), radius=10,
                               fill=(30, 20, 46, 140), outline=(*COLORS["accent"], 60), width=1)
        if i < len(items):
            _draw_inventory_icon_placeholder(draw, sx, sy, sw, sh, items[i])


def _draw_inventory_icon_placeholder(draw, sx, sy, sw, sh, item):
    # Item icons are remote URLs (utils.item_catalog icon_url) -- fetched
    # the same way currency icons are; left as a labelled placeholder here
    # since item-art fetching is outside this fix's scope.
    draw.rounded_rectangle((sx + 6, sy + 6, sx + sw - 6, sy + sh - 6), radius=6,
                           fill=(58, 38, 88, 190))
    qty = item.get("quantity", 1)
    if qty and qty > 1:
        draw.text((sx + sw - 8, sy + sh - 8), f"x{qty}",
                  font=outfit(11, "Bold"), fill=COLORS["text_primary"], anchor="rs")


def _draw_mailbox(img, mailbox_im):
    """img is the RGBA card canvas. The mailbox art is only ever resized
    (uniform scale, aspect-ratio preserved) -- never recolored, redrawn,
    or recomposited beyond a plain resize."""
    x0, x1 = LAYOUT["mailbox_column_x"]
    bottom_y = LAYOUT["mailbox_bottom_y"]
    top_min_y = LAYOUT["mailbox_top_min_y"]

    col_w = (x1 - x0) - 20  # small side margin
    max_h_by_height = bottom_y - top_min_y
    aspect = mailbox_im.height / mailbox_im.width  # h/w, tall asset so >1

    # Fit by whichever dimension is the binding constraint.
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

    # Soft purple glow beneath the mailbox base
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = new_w // 2
    gd.ellipse((center_x - gr, bottom_y - gr // 4, center_x + gr, bottom_y + gr // 4),
              fill=(*COLORS["purple_glow"], 80))
    glow = glow.filter(ImageFilter.GaussianBlur(26))
    img.alpha_composite(glow)

    # Faint reflection: flipped, heavily faded copy just below the base
    reflection = ImageOps.flip(mb).copy()
    r_alpha = reflection.split()[3].point(lambda p: int(p * 0.18))
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
