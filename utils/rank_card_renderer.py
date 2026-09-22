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

# The checkout keeps the fonts / mailbox at the repository root; the
# assets/rank_card layout is the deployed one. Prefer the asset root,
# fall back to the repo root so the renderer works in both layouts.
# NOTE: this module lives in utils/ — one level below the repo root —
# so the repo root is TWO dirnames up, not one. (When the renderer sat
# at the repo root the single dirname was correct; moving the file
# without adjusting this would have made every fallback look inside
# utils/ and silently break all font/mailbox loading.)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _asset_path(asset_sub: str, root_fallback: str) -> str:
    p = os.path.join(_ASSET_ROOT, asset_sub)
    return p if os.path.isfile(p) else os.path.join(_REPO_ROOT, root_fallback)


MAILBOX_PNG_PATH = _asset_path("mailbox.png", "mailbox_trimmed.png")

# Supplied avatar-ring artwork (source of truth -- not redrawn/regenerated).
# The file's own inner circle (where the avatar sits) is off-center within
# the PNG and smaller than the file's full bounding box, since the tendrils
# spray out past it -- these were measured directly off the asset itself
# (radial scan from its opaque-pixel centroid to the first opaque pixel at
# each angle, median radius) and are used to align the artwork's hole with
# the avatar circle exactly.
AVATAR_RING_PNG_PATH = _asset_path("avatar_ring.png", "Discord ring.png")
AVATAR_RING_INNER_CENTER = (490, 509)   # px, in the source PNG's own pixel space
AVATAR_RING_INNER_RADIUS = 265          # px, in the source PNG's own pixel space

FONT_PATHS = {
    "cinzel": _asset_path(os.path.join("fonts", "Cinzel-Variable.ttf"), "Cinzel-Variable.ttf"),
    "zilla_bold": _asset_path(os.path.join("fonts", "ZillaSlab-Bold.ttf"), "ZillaSlab-Bold.ttf"),
    "outfit": _asset_path(os.path.join("fonts", "Outfit-Variable.ttf"), "Outfit-Variable.ttf"),
    "amiri_regular": _asset_path(os.path.join("fonts", "Amiri-Regular.ttf"), "Amiri-Regular.ttf"),
    "amiri_bold": _asset_path(os.path.join("fonts", "Amiri-Bold.ttf"), "Amiri-Bold.ttf"),
    "tajawal_regular": _asset_path(os.path.join("fonts", "Tajawal-Regular.ttf"), "Tajawal-Regular.ttf"),
    "tajawal_medium": _asset_path(os.path.join("fonts", "Tajawal-Medium.ttf"), "Tajawal-Medium.ttf"),
    "tajawal_bold": _asset_path(os.path.join("fonts", "Tajawal-Bold.ttf"), "Tajawal-Bold.ttf"),
}

TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/"

# ─────────────────────────────────────────────────────────────────────────
# COLOR PALETTE
# ─────────────────────────────────────────────────────────────────────────

COLORS = {
    # Pass 4: sampled off the reference (1024x682) instead of invented.
    "bg_top": (11, 6, 20),
    "bg_bottom": (23, 11, 36),
    "nebula_a": (78, 26, 122),
    "nebula_b": (120, 52, 168),
    "panel": (22, 12, 44, 80),          # reference panels barely lift the bg
    "panel_border": (150, 100, 210, 34),  # reference borders are a ~+15 lift
    "panel_glow": (130, 80, 190, 16),
    "purple_glow": (170, 100, 235),
    "text_primary": (232, 226, 242),
    "text_muted": (128, 112, 158),      # muted label tone (e.g. "TOP", "/2,000 XP")
    "text_bright": (196, 178, 226),     # brighter tone for emphasized values
    "accent": (176, 120, 235),
    "pip_filled": (214, 120, 240),
    "pip_empty": (88, 66, 118),
    "xp_bar_bg": (26, 14, 70),
    "xp_bar_fill_a": (109, 21, 214),    # gradient start (left)  -- sampled
    "xp_bar_fill_b": (219, 77, 225),    # gradient end (right)   -- sampled
    "ring": (150, 90, 207),
    "line_icon": (178, 126, 226),       # purple line-art icon stroke
    "name_text": (215, 191, 232),       # SHADOW lettering -- sampled
    "rank_number_a": (198, 161, 234),   # #3 gradient top     -- sampled
    "rank_number_b": (146, 78, 232),    # #3 gradient bottom  -- sampled
    "xp_value": (186, 120, 232),        # "1,450" purple      -- sampled
    "label_purple": (140, 100, 180),    # INVENTORY / TOTAL XP headers
    "gold_label": (168, 142, 116),      # reference's COINS label tint
}

# ─────────────────────────────────────────────────────────────────────────
# LAYOUT — fixed canvas 1280x853, measured off the reference (x1.25 scale)
# ─────────────────────────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 1280, 853

LAYOUT = {
    # Pass 4: re-measured off the reference at 1024x682 and scaled x1.25.
    "avatar": (47, 71, 243, 243),
    "avatar_level_badge_r": 30,
    "avatar_badge_center": (199, 232),   # relative to avatar origin

    "name": (333, 98, 600, 63),
    "title_pill": (327, 196, 245, 41),
    "member_since": (334, 270, 500, 26),

    "rank_prestige_panel": (706, 57, 215, 258),

    "level_panel": (39, 366, 183, 185),
    "xp_totalxp_panel": (225, 390, 461, 146),
    "xp_divider_x": 275,                 # offset inside the xp panel (=500 abs)

    "stats_row_y": 589,
    "stats_row_h": 148,
    "stats_card_w": 116,
    "stats_gap": 17,
    "stats_start_x": 39,

    # 3 columns x 4 rows (reference-confirmed), tall right column.
    "inventory_panel": (710, 314, 308, 422),
    "inventory_grid_origin": (731, 388),
    "inventory_slot": (76, 76),
    "inventory_slot_gap_x": 8,
    "inventory_slot_gap_y": 8,
    "inventory_cols": 3,
    "inventory_rows": 4,

    # Visible-art bounds of the mailbox in the reference (x1.25): the art
    # runs from near the top-right down to the canvas bottom edge.
    "mailbox_right_x": 1278,
    "mailbox_bottom_y": 851,
    "mailbox_art_h": 755,

    "footer_y": 762,
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


def _text_size(draw, text, font, tracking=0):
    if tracking <= 0:
        b = draw.textbbox((0, 0), text, font=font)
        return b[2] - b[0], b[3] - b[1]
    w = sum(draw.textbbox((0, 0), ch, font=font)[2] for ch in text) + tracking * (len(text) - 1)
    b = draw.textbbox((0, 0), text, font=font)
    return w, b[3] - b[1]


def _draw_glow_layer(img, painter, blur=6, offset=(0, 0)):
    """Run painter(draw) on a transparent layer, blur it, composite under
    nothing -- caller composites. Used for the reference's soft blooms."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    painter(ImageDraw.Draw(layer))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(layer, offset)
    return layer


def _draw_condensed(img, xy, painter, ratio=1.0, glow=None, blur=6,
                    glow_alpha=0.55):
    """The reference's numerals/labels are narrower than the bundled fonts'
    natural advance (AI-rendered condensed look). Render to a scratch
    layer, squeeze horizontally, composite -- reproduces the reference
    glyph proportions without swapping fonts. glow tints a soft bloom of
    the SAME scaled crop behind it."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    painter(ImageDraw.Draw(layer))
    bb = layer.getbbox()
    if not bb:
        return (0, 0)
    crop = layer.crop(bb)
    if ratio != 1.0:
        crop = crop.resize((max(1, int(round(crop.width * ratio))), crop.height),
                           Image.LANCZOS)
    if glow:
        gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gl.paste(crop, (int(xy[0]), int(xy[1])), crop)
        # tint the bloom
        tint = Image.new("RGBA", img.size, (*glow, 255))
        gl = Image.composite(tint, Image.new("RGBA", img.size, (0, 0, 0, 0)),
                             gl.split()[3])
        gl.putalpha(gl.split()[3].point(lambda a: int(a * glow_alpha)))
        gl = gl.filter(ImageFilter.GaussianBlur(blur))
        img.alpha_composite(gl)
    img.alpha_composite(crop, (int(xy[0]), int(xy[1])))
    return crop.size


def _draw_stencil_number(img, draw, xy, text, font, fill, cut_color=(16, 8, 30),
                         glow=None):
    """The reference renders its big slab numerals (Level 84, badge 84) with
    thin vertical stencil notches cut out of the top/bottom of each glyph
    stem. We draw the number, optionally bloom it, then cut a narrow
    vertical slot per glyph at the top and bottom -- matching the
    reference's distinctive cut-stencil treatment without needing a
    stencil font."""
    x, y = xy
    if glow:
        _draw_glow_layer(img, lambda d: d.text((x, y), text, font=font, fill=(*glow, 110)),
                         blur=6)
    # Glyphs on a scratch layer so the stencil notches only erase ink,
    # never painting dark ticks into the glow/background.
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((x, y), text, font=font, fill=(*fill, 255))
    cursor = x
    ascent, descent = font.getmetrics()
    cap_h = int((ascent) * 0.72)
    cut_w = max(2, int(cap_h * 0.055))
    cut_d = int(cap_h * 0.24)
    for ch in text:
        if ch.isdigit():
            adv = ld.textbbox((0, 0), ch, font=font)[2]
            cx = cursor + adv / 2
            top_y = y + (ascent - cap_h)
            ld.rectangle((cx - cut_w / 2, top_y, cx + cut_w / 2, top_y + cut_d),
                         fill=(0, 0, 0, 0))
            ld.rectangle((cx - cut_w / 2, y + ascent - cut_d, cx + cut_w / 2, y + ascent),
                         fill=(0, 0, 0, 0))
        cursor += ld.textbbox((0, 0), ch, font=font)[2]
    img.alpha_composite(layer)


def _draw_gradient_text(img, draw, xy, text, font, color_top, color_bottom,
                        tracking=0, glow=None, ratio=1.0):
    """Vertical two-tone fill (the reference's #3 brightens toward the top)
    plus an optional soft bloom behind it; ratio<1 reproduces the
    reference's condensed glyph proportions."""
    x, y = xy
    tmp = Image.new("RGBA", img.size, (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    _draw_tracked_text(td, (x, y), text, font, (255, 255, 255, 255), tracking=tracking)
    bb = tmp.getbbox()
    if not bb:
        return
    grad = Image.new("RGBA", img.size, (0, 0, 0, 0))
    y0, y1 = bb[1], bb[3]
    for yy in range(y0, y1 + 1):
        t = (yy - y0) / max(y1 - y0, 1)
        col = tuple(int(color_top[c] + (color_bottom[c] - color_top[c]) * t) for c in range(3))
        ImageDraw.Draw(grad).line([(bb[0], yy), (bb[2], yy)], fill=(*col, 255))
    mask = tmp.split()[3]
    grad.putalpha(mask)
    crop = grad.crop(bb)
    if ratio != 1.0:
        crop = crop.resize((max(1, int(round(crop.width * ratio))), crop.height),
                           Image.LANCZOS)
    if glow:
        gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gl.paste(crop, (x, y), crop)
        gl.putalpha(gl.split()[3].point(lambda a: int(a * 0.45)))
        gl = gl.filter(ImageFilter.GaussianBlur(5))
        img.alpha_composite(gl)
    img.alpha_composite(crop, (x, y))


def _draw_sparkle_star(img, draw, cx, cy, r, color, glow=True):
    """The reference's 4-point sparkle (pips, footer stars, ring accents)."""
    if glow:
        _draw_glow_layer(img, lambda d: d.polygon(
            [(cx, cy - r * 1.5), (cx + r * 0.5, cy), (cx, cy + r * 1.5), (cx - r * 0.5, cy),
             ], fill=(*color, 110)), blur=3)
    pts = [
        (cx, cy - r), (cx + r * 0.32, cy - r * 0.32), (cx + r, cy),
        (cx + r * 0.32, cy + r * 0.32), (cx, cy + r), (cx - r * 0.32, cy + r * 0.32),
        (cx - r, cy), (cx - r * 0.32, cy - r * 0.32),
    ]
    draw.polygon(pts, fill=(*color, 255))


def _draw_five_point_star(draw, cx, cy, r, color, rot=0):
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.45
        a = math.radians(-90 + rot + i * 36)
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    draw.polygon(pts, fill=(*color, 255))


def _draw_potion_icon(img, size=27):
    """The little XP potion flask the reference shows next to TOTAL XP --
    hand-drawn (round flask, cork, purple liquid, soft bloom)."""
    s = size
    im = Image.new("RGBA", (s, int(s * 1.25)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w, h = im.size
    cx = w / 2
    # bloom
    bloom = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(bloom).ellipse((w * 0.12, h * 0.38, w * 0.88, h * 0.98), fill=(160, 80, 220, 120))
    bloom = bloom.filter(ImageFilter.GaussianBlur(3))
    im = Image.alpha_composite(im, bloom)
    d = ImageDraw.Draw(im)
    # cork
    d.rounded_rectangle((cx - w * 0.10, 0, cx + w * 0.10, h * 0.16), radius=1,
                        fill=(196, 156, 120))
    # neck
    d.rectangle((cx - w * 0.13, h * 0.14, cx + w * 0.13, h * 0.34), fill=(150, 130, 190, 200))
    # round body
    d.ellipse((w * 0.10, h * 0.30, w * 0.90, h * 0.96), outline=(190, 170, 230, 230), width=2)
    # liquid
    d.pieslice((w * 0.14, h * 0.34, w * 0.86, h * 0.92), start=0, end=180,
               fill=(168, 84, 224))
    d.ellipse((w * 0.14, h * 0.56, w * 0.86, h * 0.92), fill=(168, 84, 224))
    # highlight
    d.ellipse((w * 0.24, h * 0.42, w * 0.40, h * 0.55), fill=(240, 220, 255, 150))
    return im


def _draw_paw_icon(size=40):
    """The pink paw the reference footer tag shows next to MAILBOX."""
    s = size
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cream = (247, 216, 216)
    pink = (240, 178, 190)
    # main pad
    d.ellipse((s * 0.26, s * 0.46, s * 0.74, s * 0.92), fill=cream)
    # toes
    for cx, cy, r in [(0.18, 0.34, 0.13), (0.40, 0.20, 0.14), (0.62, 0.20, 0.14),
                      (0.84, 0.34, 0.13)]:
        d.ellipse((s * cx - s * r, s * cy - s * r, s * cx + s * r, s * cy + s * r), fill=pink)
    return im


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


def _rounded_panel(img, draw, box, radius=20, fill=None, outline=None, width=2, glow=False):
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
    # The reference header glyph is a FILLED purple cube, not line art.
    im, d = _icon_canvas(size)
    s = size
    d.polygon([(s*0.5, s*0.06), (s*0.92, s*0.27), (s*0.5, s*0.48), (s*0.08, s*0.27)],
              fill=(150, 90, 220))
    d.polygon([(s*0.08, s*0.27), (s*0.5, s*0.48), (s*0.5, s*0.94), (s*0.08, s*0.72)],
              fill=(105, 50, 170))
    d.polygon([(s*0.92, s*0.27), (s*0.5, s*0.48), (s*0.5, s*0.94), (s*0.92, s*0.72)],
              fill=(78, 34, 138))
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

    line_icons_30 = _build_line_icons(68)
    line_icons_16 = _build_line_icons(68)

    grid = data.get("inventory_grid") or []

    async def _no_icon():
        return None

    async with aiohttp.ClientSession() as session:
        avatar_im, coin_icon_im, diamond_icon_im = await asyncio.gather(
            _fetch_avatar(session, data.get("avatar_url")),
            _resolve_currency_icon(session, data["currency"]["coins"]["emoji"], 32),
            _resolve_currency_icon(session, data["currency"]["diamonds"]["emoji"], 32),
        )
        item_icons = list(await asyncio.gather(*[
            _fetch_image(session, it["icon_url"]) if it.get("icon_url") else _no_icon()
            for it in grid
        ]))
    mailbox_im = await _load_mailbox()
    ring_im = await _load_avatar_ring()

    _draw_name_block(img, draw, data, line_icons_16)
    _draw_rank_prestige_panel(img, draw, data, line_icons_16)
    _draw_level_xp_panels(img, draw, data)
    _draw_stat_cards(img, draw, data, coin_icon_im, diamond_icon_im, line_icons_30)
    _draw_inventory(img, draw, data, line_icons_16, item_icons)
    if mailbox_im is not None:
        _draw_mailbox(img, mailbox_im)
    _draw_footer(img, draw)

    # avatar (and its ring) pasted after every panel, but the placeholder/
    # fetch needs to happen before drawing -- done via the resolved
    # avatar_im/ring_im captured above.
    _paste_avatar(img, data, avatar_im, ring_im)

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


async def _load_avatar_ring() -> Image.Image | None:
    if not os.path.isfile(AVATAR_RING_PNG_PATH):
        log.warning("rank_card: avatar ring asset not found at %s", AVATAR_RING_PNG_PATH)
        return None
    try:
        im = Image.open(AVATAR_RING_PNG_PATH)
        im.load()
        return im.convert("RGBA")
    except Exception as e:
        log.warning("rank_card: failed to load avatar ring asset: %s", e)
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


def _paste_avatar_ring(img, ax, ay, aw, ah, ring_im):
    """Paste the supplied ring artwork behind the avatar, scaled purely so
    the artwork's own inner circle (AVATAR_RING_INNER_CENTER/_RADIUS)
    lines up with the avatar's circle -- the artwork itself is never
    redrawn, recolored or cropped, only resized and positioned."""
    if ring_im is None:
        return
    cx, cy = ax + aw / 2, ay + ah / 2
    avatar_d = (aw + ah) / 2
    scale = avatar_d / (AVATAR_RING_INNER_RADIUS * 2)
    scaled_w = max(1, round(ring_im.width * scale))
    scaled_h = max(1, round(ring_im.height * scale))
    scaled = ring_im.resize((scaled_w, scaled_h), Image.LANCZOS)
    ring_cx = AVATAR_RING_INNER_CENTER[0] * scale
    ring_cy = AVATAR_RING_INNER_CENTER[1] * scale
    paste_x = round(cx - ring_cx)
    paste_y = round(cy - ring_cy)
    img.paste(scaled, (paste_x, paste_y), scaled)


def _paste_avatar(img, data, avatar_im, ring_im=None):
    draw = ImageDraw.Draw(img)
    ax, ay, aw, ah = LAYOUT["avatar"]
    cx, cy = ax + aw / 2, ay + ah / 2
    r = aw / 2

    # Supplied ring artwork goes down first so it sits behind the avatar.
    _paste_avatar_ring(img, ax, ay, aw, ah, ring_im)

    _circle_mask_paste(img, avatar_im, (int(ax), int(ay), int(aw), int(ah)))

    # Level badge -- attached to the avatar's lower right, stencil numerals
    # like the reference.
    badge_r = LAYOUT["avatar_level_badge_r"]
    bx = ax + LAYOUT["avatar_badge_center"][0]
    by = ay + LAYOUT["avatar_badge_center"][1]
    draw.ellipse((bx - badge_r, by - badge_r, bx + badge_r, by + badge_r),
                 fill=(14, 8, 22, 235), outline=(170, 130, 220, 200), width=2)
    lvl_text = str(data["level"])
    lvl_font = zilla_bold(44)
    bbox = draw.textbbox((0, 0), lvl_text, font=lvl_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    _draw_stencil_number(img, draw, (bx - tw / 2 - bbox[0], by - th / 2 - bbox[1]),
                         lvl_text, lvl_font, (230, 220, 240), cut_color=(14, 8, 22))


def _draw_name_block(img, draw, data, icons16):
    x, y, w, h = LAYOUT["name"]
    username = data.get("username") or f"User {data['user_id']}"
    if username.isascii() and username.replace(" ", "").isalpha():
        # Reference drop-cap treatment: oversized first letter + smaller
        # caps for the rest, condensed to the reference's ~234px width, on
        # a subtle blurred dark plate (both measured off the reference).
        text = username.upper()
        plate = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(plate).rounded_rectangle(
            (x - 11, y - 14, x + 250, y + 67), radius=10, fill=(6, 3, 14, 90))
        plate = plate.filter(ImageFilter.GaussianBlur(6))
        img.alpha_composite(plate)

        def _name_painter(d):
            _draw_dropcap_heading(d, (20, 20), text, "cinzel", 72, 52,
                                  COLORS["name_text"], tracking=2, weight="Bold")
        _draw_condensed(img, (x, y + 2), _name_painter, ratio=0.835,
                        glow=(140, 85, 200), blur=5, glow_alpha=0.9)
    else:
        name_font = cinzel_bold(56) if username.isascii() else tajawal(48, "bold")
        _draw_tracked_text(draw, (x, y), username, name_font, COLORS["name_text"], tracking=1)

    title = data.get("equipped_title")
    if title:
        px, py, pw, ph = LAYOUT["title_pill"]
        # outer pill: dark fill + faint border; inner pill hugs the content
        draw.rounded_rectangle((px, py, px + pw, py + ph), radius=ph // 2,
                               fill=(10, 5, 26, 230), outline=(90, 60, 140, 70), width=1)
        label = title["item_name"].upper()
        tf = outfit(17, "SemiBold")
        tl_w = _text_size(draw, label, tf, tracking=3)[0]
        icon_w = 16
        inner_x0 = px + 15
        inner_x1 = min(px + pw - 6, inner_x0 + 10 + icon_w + 6 + tl_w + 12)
        draw.rounded_rectangle((inner_x0, py + 7, inner_x1, py + ph - 7),
                               radius=(ph - 14) // 2, fill=(26, 20, 40, 210))
        if icons16.get("crown") is not None:
            cr = icons16["crown"].resize((icon_w, icon_w))
            img.paste(cr, (int(inner_x0 + 10), int(py + ph / 2 - icon_w / 2)), cr)
        text_x = inner_x0 + 10 + icon_w + 6
        _draw_tracked_text(draw, (text_x, py + ph / 2), label, tf,
                           (150, 100, 190), tracking=3, anchor="lm")

    ms_x, ms_y, _, _ = LAYOUT["member_since"]
    member_since = data.get("member_since")
    if member_since:
        date_str = member_since.strftime("%b %d, %Y")
        text_x = ms_x
        if icons16.get("calendar") is not None:
            ic = icons16["calendar"].resize((19, 19))
            img.paste(ic, (ms_x, ms_y + 2), ic)
            text_x = ms_x + 26
        mf = outfit(22)
        draw.text((text_x, ms_y), "Member since  ·  ", font=mf, fill=(125, 112, 158))
        w0 = draw.textbbox((0, 0), "Member since  ·  ", font=mf)[2]
        draw.text((text_x + w0, ms_y), date_str, font=mf, fill=(196, 186, 220))


def _draw_rank_prestige_panel(img, draw, data, icons16):
    x, y, w, h = LAYOUT["rank_prestige_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16)

    _draw_tracked_text(draw, (x + 28, y + 20), "RANK", outfit(21, "SemiBold"),
                       COLORS["text_muted"], tracking=3)
    # The reference's rank number is big, bold and purple (gradient +
    # bloom) -- part of the accent hierarchy, not white text.
    _draw_gradient_text(img, draw, (x + 28, y + 40), f"#{data['rank']}",
                        outfit(76, "ExtraBold"), COLORS["rank_number_a"],
                        COLORS["rank_number_b"], glow=(150, 80, 230), ratio=0.78)

    # Two-tone: "TOP" muted, the percentage itself brighter -- matches the
    # reference's emphasis treatment.
    ty = y + 114
    draw.text((x + 28, ty), "TOP ", font=outfit(18, "Medium"), fill=COLORS["text_muted"])
    tw = draw.textbbox((0, 0), "TOP ", font=outfit(18, "Medium"))[2]
    draw.text((x + 28 + tw, ty), f"{data['percentile']:.2f}%",
              font=outfit(18, "SemiBold"), fill=(170, 140, 210))

    draw.line((x + 28, y + 142, x + w - 28, y + 142), fill=(*COLORS["accent"], 40), width=1)
    draw.ellipse((x + 30, y + 140, x + 34, y + 144), fill=(*COLORS["accent"], 120))
    draw.ellipse((x + w - 34, y + 140, x + w - 30, y + 144), fill=(*COLORS["accent"], 120))

    tier = data["effective_prestige"]
    roman = ["0", "I", "II", "III", "IV", "V", "VI"][tier] if 0 <= tier <= 6 else str(tier)
    label = f"PRESTIGE {roman}" if tier else "PRESTIGE"
    big_font = _font("cinzel", 32, "SemiBold")
    small_font = _font("cinzel", 23, "SemiBold")
    first_w = draw.textbbox((0, 0), label[0], font=big_font)[2]
    rest_w = sum(draw.textbbox((0, 0), ch, font=small_font)[2] + 2 for ch in label[1:])
    natural_w = first_w + 2 + rest_w
    target_w = 119
    ratio = min(1.0, target_w / natural_w)

    def _prestige_painter(d):
        _draw_dropcap_heading(d, (20, 20), label, "cinzel", 32, 23, (169, 140, 207),
                              tracking=2, weight="SemiBold")
    _draw_condensed(img, (x + w / 2 - natural_w * ratio / 2, y + 163),
                    _prestige_painter, ratio=ratio)

    # Pips: glowing 4-point sparkles when filled, plain outline diamonds
    # when empty -- as in the reference.
    pip_r = 11
    pitch = 29
    total_pips = 6
    start_x = x + w / 2 - (total_pips - 1) * pitch / 2
    pip_y = y + 205
    for i in range(total_pips):
        cx = start_x + i * pitch
        filled = i < tier
        if filled:
            _draw_sparkle_star(img, draw, cx, pip_y, pip_r, COLORS["pip_filled"])
        else:
            _draw_diamond_pip(draw, cx, pip_y, pip_r, COLORS["pip_empty"], filled)

    if int(data.get("effective_prestige") or 0) == 6:
        by = y + 218
        bw = 150
        bh = 23
        bx = x + w / 2 - bw / 2
        pts = [(bx + 7, by), (bx + bw - 7, by), (bx + bw, by + bh / 2),
               (bx + bw - 7, by + bh), (bx + 7, by + bh), (bx, by + bh / 2)]
        draw.polygon(pts, fill=(60, 20, 90, 200))
        draw.line(pts + [pts[0]], fill=(150, 90, 210, 150), width=1)
        icon = icons16.get("crown")
        lf2 = outfit(11, "SemiBold")
        label2 = "BOOSTER PRESTIGE"
        lw = _text_size(draw, label2, lf2, tracking=2)[0]
        icon_w = 15 if icon is not None else 0
        block_w = icon_w + (4 if icon_w else 0) + lw
        start = bx + (bw - block_w) / 2
        if icon is not None:
            ic = icon.resize((15, 15))
            img.paste(ic, (int(start), int(by + 4)), ic)
        _draw_tracked_text(draw, (start + icon_w + (4 if icon_w else 0), by + bh / 2),
                           label2, lf2, (200, 180, 220), tracking=2, anchor="lm")


def _draw_diamond_pip(draw, cx, cy, r, color, filled):
    pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
    if filled:
        draw.polygon(pts, fill=color)
    else:
        draw.polygon(pts, outline=color, width=2)


def _draw_level_xp_panels(img, draw, data):
    x, y, w, h = LAYOUT["level_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16)
    # LEVEL is a small drop-cap serif heading in the reference, not a
    # tracked sans label.
    bf = _font("cinzel", 32, "Bold")
    sf = _font("cinzel", 25, "Bold")
    nat_w = draw.textbbox((0, 0), "L", font=bf)[2] + 1 + sum(
        draw.textbbox((0, 0), ch, font=sf)[2] + 1 for ch in "EVEL")
    ratio = min(1.0, 81 / nat_w)

    def _level_painter(d):
        _draw_dropcap_heading(d, (20, 20), "LEVEL", "cinzel", 32, 25,
                              (200, 190, 215), tracking=1, weight="Bold")
    _draw_condensed(img, (x + 31, y + 22), _level_painter, ratio=ratio)

    # Big stencil-slab number with the reference's purple bloom.
    lvl_font = zilla_bold(98)
    a, _d = lvl_font.getmetrics()
    digit_h = int(98 * 0.67)
    ty = y + 69 - (a - digit_h)
    _draw_stencil_number(img, draw, (x + 30, ty), str(data["level"]), lvl_font,
                         (224, 208, 230), cut_color=(16, 8, 30),
                         glow=(150, 80, 220))

    x, y, w, h = LAYOUT["xp_totalxp_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16)

    _draw_tracked_text(draw, (x + 14, y + 29), "XP PROGRESS", outfit(19, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    cur, needed = data["xp_current"], max(data["xp_needed"], 1)
    # Two-tone: current XP purple/emphasized (condensed, as measured off
    # the reference), "/ needed XP" muted.
    cur_txt = f"{cur:,}"
    vf = zilla_bold(45)
    cur_w = draw.textbbox((0, 0), cur_txt, font=vf)[2]

    def _xpval_painter(d):
        d.text((20, 20), cur_txt, font=vf, fill=COLORS["xp_value"])
    cw, _ = _draw_condensed(img, (x + 14, y + 50), _xpval_painter, ratio=0.64,
                            glow=(150, 70, 210), blur=4)
    draw.text((x + 20 + cw, y + 58), f"/ {needed:,} XP", font=outfit(22),
              fill=COLORS["text_muted"])

    div_x = x + LAYOUT["xp_divider_x"]
    draw.line((div_x, y + 14, div_x, y + h - 14), fill=(*COLORS["accent"], 30), width=1)

    bar_x, bar_y, bar_w, bar_h = x + 8, y + 86, w - 20, 21
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                           radius=bar_h // 2, fill=COLORS["xp_bar_bg"])
    frac = min(cur / needed, 1.0)
    if frac > 0:
        fill_w = bar_w * frac
        # soft bloom around the filled portion only
        _draw_glow_layer(img, lambda d: d.rounded_rectangle(
            (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h), radius=bar_h // 2,
            fill=(150, 60, 220, 80)), blur=4)
        _draw_gradient_bar(img, bar_x, bar_y, fill_w, bar_h,
                           COLORS["xp_bar_fill_a"], COLORS["xp_bar_fill_b"])
        # Subtle highlight at the leading edge of the FILL itself (not the
        # bar's outer end) -- per the reference. Small soft dot, nothing more.
        if 4 < fill_w < bar_w - 2:
            hx, hy = bar_x + fill_w - 4, bar_y + bar_h / 2
            _draw_soft_dot(img, hx, hy, 5, (255, 235, 250, 200))
    draw.text((bar_x + 6, bar_y + bar_h + 6), f"{frac * 100:.1f}% to next level",
              font=outfit(19), fill=COLORS["text_muted"])

    tx = div_x + 22
    _draw_tracked_text(draw, (tx, y + 25), "TOTAL XP", outfit(20, "SemiBold"),
                       COLORS["label_purple"], tracking=2)
    potion = _draw_potion_icon(27)
    img.paste(potion, (int(div_x + 19), int(y + 42)), potion)
    total_txt = f"{data['xp_total']:,}"
    tf2 = zilla_bold(38)

    def _total_painter(d):
        d.text((20, 20), total_txt, font=tf2, fill=(205, 200, 215))
    _draw_condensed(img, (div_x + 52, y + 44), _total_painter, ratio=0.80)


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

    # The reference tints the default coin label warm/gold while every
    # other label stays muted lavender.
    coin_label_color = (COLORS["gold_label"] if coins_cfg["emoji"] == "🪙"
                        else COLORS["text_muted"])

    cards = [
        (icons30.get("messages"), "MESSAGES", f"{data['messages_count']:,}",
         COLORS["text_muted"]),
        (icons30.get("voice"), "VOICE TIME", _fmt_minutes(data["voice_minutes"]),
         COLORS["text_muted"]),
        (coin_icon_im, coins_cfg["name"].upper(), f"{data['balance']:,}",
         coin_label_color),
        (diamond_icon_im, diamonds_cfg["name"].upper(), f"{data['diamonds']:,}",
         COLORS["text_muted"]),
        (icons30.get("games"), "GAMES WON", f"{data['minigame_wins']:,}",
         COLORS["text_muted"]),
    ]

    y = LAYOUT["stats_row_y"]
    h = LAYOUT["stats_row_h"]
    w = LAYOUT["stats_card_w"]
    gap = LAYOUT["stats_gap"]
    x0 = LAYOUT["stats_start_x"]

    for i, (icon_im, label, value, label_color) in enumerate(cards):
        x = x0 + i * (w + gap)
        _rounded_panel(img, draw, (x, y, x + w, y + h), radius=14)
        if icon_im is not None:
            ic = icon_im if icon_im.width == 34 else icon_im.resize((34, 34))
            # reference icons carry a soft bloom
            gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
            gl.paste(ic, (int(x + w / 2 - 17), y + 23), ic)
            gl.putalpha(gl.split()[3].point(lambda a: int(a * 0.75)))
            gl = gl.filter(ImageFilter.GaussianBlur(3))
            img.alpha_composite(gl)
            img.paste(ic, (int(x + w / 2 - 17), y + 23), ic)
        vf = zilla_bold(32)
        vb = draw.textbbox((0, 0), value, font=vf)
        nat_w = vb[2] - vb[0]

        def _val_painter(d, _v=value, _f=vf):
            d.text((20, 20), _v, font=_f, fill=(215, 215, 222))
        _draw_condensed(img, (x + w / 2 - nat_w * 0.72 / 2, y + 82), _val_painter,
                        ratio=0.72)
        lf = outfit(21, "Medium")
        lw_nat = _text_size(draw, label, lf, tracking=2)[0]

        def _lab_painter(d, _l=label, _f=lf, _c=label_color):
            _draw_tracked_text(d, (20, 20), _l, _f, _c, tracking=2)
        _draw_condensed(img, (x + w / 2 - lw_nat * 0.66 / 2, y + 116), _lab_painter,
                        ratio=0.66)


def _fmt_minutes(total_minutes) -> str:
    total_minutes = int(total_minutes or 0)
    h = total_minutes // 60
    return f"{h}h" if h else f"{total_minutes}m"


def _draw_inventory(img, draw, data, icons16, item_icons=None):
    x, y, w, h = LAYOUT["inventory_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16)
    label_x = x + 30
    if icons16.get("inventory") is not None:
        ic = icons16["inventory"].resize((25, 25))
        img.paste(ic, (label_x, y + 21), ic)
        label_x += 35
    _draw_tracked_text(draw, (label_x, y + 24), "INVENTORY", outfit(21, "SemiBold"),
                       COLORS["label_purple"], tracking=3)
    # "owned / catalog total" at the right of the header, as in the
    # reference (12 / 48): owned bright, the rest muted.
    owned = data.get("owned_count")
    total = data.get("inventory_total")
    if owned is not None and total is not None:
        f_c = outfit(18, "SemiBold")
        t1, t2 = f"{owned}", f" / {total}"
        w1 = draw.textbbox((0, 0), t1, font=f_c)[2]
        w2 = draw.textbbox((0, 0), t2, font=f_c)[2]
        sx = x + w - 20 - (w1 + w2)
        draw.text((sx, y + 24), t1, font=f_c, fill=(200, 195, 215))
        draw.text((sx + w1, y + 24), t2, font=f_c, fill=COLORS["text_muted"])

    ox, oy = LAYOUT["inventory_grid_origin"]
    sw, sh = LAYOUT["inventory_slot"]
    gx, gy = LAYOUT["inventory_slot_gap_x"], LAYOUT["inventory_slot_gap_y"]
    cols, rows = LAYOUT["inventory_cols"], LAYOUT["inventory_rows"]
    items = data.get("inventory_grid") or []
    item_icons = item_icons or []

    for i in range(cols * rows):
        col, row = i % cols, i // cols
        sx = ox + col * (sw + gx)
        sy = oy + row * (sh + gy)
        draw.rounded_rectangle((sx, sy, sx + sw, sy + sh), radius=12,
                               fill=(30, 24, 46, 170), outline=(*COLORS["accent"], 22), width=1)
        if i < len(items):
            icon_im = item_icons[i] if i < len(item_icons) else None
            _draw_inventory_item(img, draw, sx, sy, sw, sh, items[i], icon_im)


def _draw_inventory_item(img, draw, sx, sy, sw, sh, item, icon_im=None):
    """Item art comes from utils.item_catalog icon_url (a remote asset,
    fetched the same way currency icons are); without one, a neutral
    placeholder tile stands in. Only the REAL owned quantity is ever
    shown, and only when it's actually more than 1 -- never a placeholder
    or invented number."""
    if icon_im is not None:
        ic = ImageOps.fit(icon_im, (56, 56), Image.LANCZOS)
        img.paste(ic, (int(sx + sw / 2 - 28), int(sy + sh / 2 - 28)), ic)
    else:
        draw.rounded_rectangle((sx + 7, sy + 7, sx + sw - 7, sy + sh - 7), radius=8,
                               fill=(58, 38, 88, 190))
    qty = item.get("quantity", 1)
    if qty and qty > 1:
        draw.text((sx + sw - 7, sy + sh - 7), f"x{qty}",
                  font=outfit(13, "Bold"), fill=COLORS["text_primary"], anchor="rs")


def _draw_mailbox(img, mailbox_im):
    """The reference's mailbox artwork runs from near the top-right edge
    (flag tip ~y90) down to the canvas bottom (~y851), right-aligned. The
    source PNG's transparent margins must not shrink it: scale by the
    measured visible-art height instead."""
    bottom_y = LAYOUT["mailbox_bottom_y"]
    right_x = LAYOUT["mailbox_right_x"]
    art_h = LAYOUT["mailbox_art_h"]

    aspect = mailbox_im.height / mailbox_im.width
    new_h = art_h
    new_w = int(new_h / aspect)

    mb = mailbox_im.resize((new_w, new_h), Image.LANCZOS)
    left = right_x - new_w
    top = bottom_y - new_h

    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = new_w // 2
    cx = left + new_w / 2
    gd.ellipse((cx - gr, bottom_y - gr // 4, cx + gr, bottom_y + gr // 4),
               fill=(*COLORS["purple_glow"], 45))
    glow = glow.filter(ImageFilter.GaussianBlur(26))
    img.alpha_composite(glow)

    img.alpha_composite(mb, (left, top))


def _draw_footer(img, draw):
    fy = LAYOUT["footer_y"]
    # Left tag: paw + tracked MAILBOX, pointed right end -- as in the
    # reference's lower-left corner.
    tag = [(62, fy), (250, fy), (270, fy + 22), (250, fy + 44), (62, fy + 44),
           (40, fy + 22)]
    draw.polygon(tag, fill=(12, 6, 26, 200))
    draw.line(tag + [tag[0]], fill=(60, 40, 90, 70), width=1)
    paw = _draw_paw_icon(40)
    img.paste(paw, (46, fy + 3), paw)
    _draw_tracked_text(draw, (100, fy + 22), "MAILBOX", outfit(16, "SemiBold"),
                       (120, 95, 150), tracking=6, anchor="lm")

    # Centered arabic line flanked by 4-point sparkles (the reference has
    # no tofu boxes -- the stars are drawn, not typed). Pillow has no
    # arabic shaping of its own; reshape + bidi give correct joined
    # visual-order text when the (pure-python) libs are installed, and we
    # fall back to the raw string otherwise.
    text = "عالمنا صغير، ولكن الإلهام فيه بلا حدود"
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        text = get_display(arabic_reshaper.reshape(text))
    except Exception:
        pass
    f = amiri(30)
    cx = 602
    bbox = draw.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    fplate = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(fplate).rounded_rectangle(
        (cx - tw / 2 - 48, fy - 2, cx + tw / 2 + 48, fy + 44), radius=10,
        fill=(6, 3, 14, 90))
    fplate = fplate.filter(ImageFilter.GaussianBlur(6))
    img.alpha_composite(fplate)
    fglow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(fglow).text((cx - tw / 2, fy + 6), text, font=f,
                               fill=(150, 100, 220, 255))
    fglow.putalpha(fglow.split()[3].point(lambda a: int(a * 0.75)))
    fglow = fglow.filter(ImageFilter.GaussianBlur(4))
    img.alpha_composite(fglow)
    draw.text((cx - tw / 2, fy + 6), text, font=f, fill=(170, 150, 210))
    _draw_sparkle_star(img, draw, cx - tw / 2 - 32, fy + 22, 12, (200, 170, 230))
    _draw_sparkle_star(img, draw, cx + tw / 2 + 32, fy + 22, 12, (200, 170, 230))
