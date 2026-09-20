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
    # Pass 5 (fidelity): re-sampled per-region. The reference's page field is
    # BRIGHT at the top (25,7,46) and fades DARK toward the bottom (7,4,20);
    # its panels sit DARKER than the page with a 1px ~+12-lum border lift --
    # all solid values now, because ImageDraw writes RGBA raw (an alpha of 34
    # used to render as a fully opaque line, which made every border harsh).
    "bg_top": (22, 8, 44),
    "bg_bottom": (8, 4, 20),
    "nebula_a": (78, 26, 122),
    "nebula_b": (120, 52, 168),
    "panel": (10, 5, 25),              # reference panels darken the page bg
    "panel_border": (25, 17, 42),      # 1px, ~+12 lum over the panel fill
    "card_fill": (11, 9, 18),          # stats cards: neutral dark fill
    "card_border": (24, 20, 34),
    "slot_fill": (22, 17, 37),         # inventory slots: soft, no outline
    "inv_panel": (10, 6, 23),
    "inv_border": (23, 19, 37),
    "panel_glow": (130, 80, 190, 16),
    "purple_glow": (170, 100, 235),
    "text_primary": (232, 226, 242),
    "text_muted": (128, 112, 158),      # muted label tone (e.g. "TOP", "/2,000 XP")
    "text_bright": (196, 178, 226),     # brighter tone for emphasized values
    "accent": (176, 120, 235),
    "pip_filled": (198, 95, 240),
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
    "avatar": (52, 92, 235, 235),
    "avatar_level_badge_r": 31,
    "avatar_badge_center": (190, 214),   # relative to avatar origin

    "name": (333, 98, 600, 63),
    "title_pill": (327, 196, 245, 41),
    "member_since": (334, 270, 500, 26),

    "rank_prestige_panel": (706, 57, 215, 245),

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
    "mailbox_right_x": 1277,
    "mailbox_bottom_y": 853,
    "mailbox_art_h": 755,
    # Pass 5: the uploaded asset's head/post proportions run ~10% wider than
    # the reference art; a mild anisotropic correction (narrower, a touch
    # taller) converges on the reference silhouette without redrawing art.
    "mailbox_scale_x": 0.90,
    "mailbox_scale_y": 1.04,

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


def _pill_safe(text: str) -> str:
    """Title names can carry emoji/symbols the bundled latin fonts have no
    glyph for (Pillow then draws a tofu box on the card). Keep only
    codepoints the pill can actually render; drop the rest."""
    keep = set("✓✔★♥♦♛⚜")
    return "".join(ch for ch in text
                   if ch in keep or (ord(ch) < 0x2100 and ch.isprintable()))


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
                         glow=None, tracking=0, ratio=1.0, cut_scale=0.045,
                         cut_depth=0.30, glow_blur=6, glow_alpha=110):
    """The reference renders its big slab numerals (Level 84, badge 84) with
    thin vertical stencil notches cut out of the top/bottom of each glyph
    stem. We draw the number (with optional tracking/width ratio measured
    off the reference), cut a FINE vertical slot per glyph at top and
    bottom, bloom it, and composite -- matching the reference's cut-stencil
    treatment without needing a stencil font. Notch width stays ~2px at the
    reference's cap height so digits never read as split halves."""
    x, y = xy
    # Glyphs on a scratch layer so the stencil notches only erase ink,
    # never painting dark ticks into the glow/background.
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    cursor = x
    ascent, descent = font.getmetrics()
    cap_h = int((ascent) * 0.72)
    cut_w = max(1, int(cap_h * cut_scale))
    cut_d = int(cap_h * cut_depth)
    for ch in text:
        ld.text((cursor, y), ch, font=font, fill=(*fill, 255))
        adv = ld.textbbox((0, 0), ch, font=font)[2]
        if ch.isdigit():
            cx = cursor + adv / 2
            top_y = y + (ascent - cap_h)
            ld.rectangle((cx - cut_w / 2, top_y, cx + cut_w / 2, top_y + cut_d),
                         fill=(0, 0, 0, 0))
            ld.rectangle((cx - cut_w / 2, y + ascent - cut_d, cx + cut_w / 2, y + ascent),
                         fill=(0, 0, 0, 0))
        cursor += adv + tracking
    bb = layer.getbbox()
    if not bb:
        return
    crop = layer.crop(bb)
    if ratio != 1.0:
        crop = crop.resize((max(1, int(round(crop.width * ratio))), crop.height),
                           Image.LANCZOS)
    if glow:
        gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gl.paste(crop, (int(xy[0]), int(xy[1])), crop)
        tint = Image.new("RGBA", img.size, (*glow, 255))
        gl = Image.composite(tint, Image.new("RGBA", img.size, (0, 0, 0, 0)),
                             gl.split()[3])
        gl.putalpha(gl.split()[3].point(lambda a: int(a * (glow_alpha / 255))))
        gl = gl.filter(ImageFilter.GaussianBlur(glow_blur))
        img.alpha_composite(gl)
    img.alpha_composite(crop, (int(xy[0]), int(xy[1])))


def _draw_gradient_text(img, draw, xy, text, font, color_top, color_bottom,
                        tracking=0, glow=None, ratio=1.0, glow_alpha=0.45):
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
        gl.putalpha(gl.split()[3].point(lambda a: int(a * glow_alpha)))
        gl = gl.filter(ImageFilter.GaussianBlur(5))
        img.alpha_composite(gl)
    img.alpha_composite(crop, (x, y))


def _draw_sparkle_star(img, draw, cx, cy, r, color, glow=True, core=False,
                       slim=0.32):
    """The reference's 4-point sparkle (pips, footer stars, ring accents).
    Filled pips in the reference carry a wide soft bloom (halo ~2x the star)
    and a hot pale core -- the bloom is a blurred disc, not just the star
    silhouette, otherwise it hides underneath the star itself."""
    if glow:
        def _painter(d):
            d.ellipse((cx - r * 1.6, cy - r * 1.6, cx + r * 1.6, cy + r * 1.6),
                      fill=(*color, 70))
            d.polygon([(cx, cy - r * 1.5), (cx + r * 0.5, cy),
                       (cx, cy + r * 1.5), (cx - r * 0.5, cy)], fill=(*color, 130))
        _draw_glow_layer(img, _painter, blur=6)
    pts = [
        (cx, cy - r), (cx + r * slim, cy - r * slim), (cx + r, cy),
        (cx + r * slim, cy + r * slim), (cx, cy + r), (cx - r * slim, cy + r * slim),
        (cx - r, cy), (cx - r * slim, cy - r * slim),
    ]
    draw.polygon(pts, fill=(*color, 255))
    if core:
        cr = max(1.5, r * 0.22)
        _draw_soft_dot(img, cx, cy, cr, (255, 228, 255, 255))


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
    # Pass 5: the reference's light comes from the TOP of the card (brightest
    # at top-center/top-left, near-black at mid-right and bottom). Blobs are
    # placed where the reference actually measures bright -- not decorative.
    blobs = [
        (int(CANVAS_W * 0.13), int(CANVAS_H * 0.10), 340, COLORS["nebula_a"], 30),
        (int(CANVAS_W * 0.50), int(CANVAS_H * 0.04), 300, COLORS["nebula_a"], 26),
        (int(CANVAS_W * 0.90), int(CANVAS_H * 0.10), 240, COLORS["nebula_b"], 12),
    ]
    for cx, cy, r, color, alpha in blobs:
        nd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*color, alpha))
    nebula = nebula.filter(ImageFilter.GaussianBlur(130))
    img = Image.alpha_composite(img.convert("RGBA"), nebula)

    import random
    rnd = random.Random(1337)
    sparkle = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sparkle)
    for _ in range(60):
        x, y = rnd.randint(0, CANVAS_W), rnd.randint(0, CANVAS_H)
        r = rnd.choice([1, 1, 1, 2])
        a = rnd.randint(24, 80)
        sd.ellipse((x - r, y - r, x + r, y + r), fill=(230, 220, 245, a))
    img = Image.alpha_composite(img, sparkle)
    return img


def _rounded_panel(img, draw, box, radius=20, fill=None, outline=None, width=1, glow=False):
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
    # Pass 5: the reference's stat glyphs are FILLED violet shapes carrying a
    # soft bloom -- not thin line art.
    im, d = _icon_canvas(size)
    s = size
    d.rounded_rectangle((s*0.10, s*0.16, s*0.90, s*0.68), radius=s*0.20,
                        fill=COLORS["line_icon"])
    d.polygon([(s*0.28, s*0.64), (s*0.28, s*0.88), (s*0.50, s*0.64)],
              fill=COLORS["line_icon"])
    for cx in (0.34, 0.5, 0.66):
        r = s * 0.045
        d.ellipse((s*cx - r, s*0.40 - r, s*cx + r, s*0.40 + r), fill=(40, 18, 70))
    return im


def _line_icon_voice(size):
    im, d = _icon_canvas(size)
    s = size
    w = max(2, int(s * 0.07))
    d.rounded_rectangle((s*0.36, s*0.08, s*0.64, s*0.52), radius=s*0.14,
                        fill=COLORS["line_icon"])
    d.arc((s*0.22, s*0.26, s*0.78, s*0.72), start=20, end=160,
          fill=COLORS["line_icon"], width=w)
    d.line((s*0.5, s*0.68, s*0.5, s*0.86), fill=COLORS["line_icon"], width=w)
    d.line((s*0.34, s*0.86, s*0.66, s*0.86), fill=COLORS["line_icon"], width=w)
    return im


def _line_icon_games(size):
    im, d = _icon_canvas(size)
    s = size
    d.rounded_rectangle((s*0.08, s*0.30, s*0.92, s*0.74), radius=s*0.22,
                        fill=COLORS["line_icon"])
    d.line((s*0.24, s*0.52, s*0.40, s*0.52), fill=(40, 18, 70), width=max(2, int(s*0.06)))
    d.line((s*0.32, s*0.44, s*0.32, s*0.60), fill=(40, 18, 70), width=max(2, int(s*0.06)))
    for cx in (0.64, 0.78):
        r = s * 0.055
        d.ellipse((s*cx - r, s*0.50 - r, s*cx + r, s*0.50 + r), fill=(40, 18, 70))
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
    # Filled violet coin stack (reference), drawn bottom-up so the upper
    # coins overlap like a real stack.
    im, d = _icon_canvas(size)
    s = size
    for cy in (0.62, 0.44, 0.26):
        d.ellipse((s*0.20, s*cy, s*0.80, s*cy + s*0.24), fill=(150, 90, 210))
        d.ellipse((s*0.26, s*cy + s*0.03, s*0.74, s*cy + s*0.15), fill=(196, 140, 240))
    return im


def _line_icon_diamond(size):
    im, d = _icon_canvas(size)
    s = size
    pts = [(s*0.5, s*0.08), (s*0.88, s*0.38), (s*0.5, s*0.92), (s*0.12, s*0.38)]
    d.polygon(pts, fill=COLORS["line_icon"])
    d.polygon([(s*0.5, s*0.08), (s*0.68, s*0.38), (s*0.5, s*0.92), (s*0.32, s*0.38)],
              fill=(216, 160, 250))
    d.line((s*0.12, s*0.38, s*0.88, s*0.38), fill=(120, 60, 180), width=1)
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

    _draw_avatar_and_level(img, draw, data)
    _draw_name_block(img, draw, data, line_icons_16)
    _draw_rank_prestige_panel(img, draw, data, line_icons_16)
    _draw_level_xp_panels(img, draw, data)
    _draw_stat_cards(img, draw, data, coin_icon_im, diamond_icon_im, line_icons_30)
    _draw_inventory(img, draw, data, line_icons_16, item_icons)
    if mailbox_im is not None:
        _draw_mailbox(img, mailbox_im)
    _draw_footer(img, draw)

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


def _ring_color(ang):
    """Pass 5: the reference ring is a painterly angular gradient (pale at the
    lower-left, saturated violet under the star, medium on the right) -- not a
    flat neon circle. Anchors sampled off the reference band."""
    anchors = [(0, (125, 45, 200)), (30, (95, 55, 150)), (60, (105, 70, 160)),
               (90, (135, 100, 190)), (120, (160, 90, 215)), (150, (150, 80, 205)),
               (180, (120, 70, 175)), (225, (200, 175, 225)), (270, (150, 110, 200)),
               (300, (185, 150, 215)), (330, (140, 90, 190)), (360, (125, 45, 200))]
    for (a0, c0), (a1, c1) in zip(anchors, anchors[1:]):
        if a0 <= ang <= a1:
            t = (ang - a0) / max(1, a1 - a0)
            return tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
    return anchors[0][1]


def _draw_ring(img, cx, cy, r):
    """Pass 5 ring treatment (option B): one clean gradient band at the
    reference's visual weight (5px, soft edge, faint bloom), a whisper of an
    echo arc, and the reference's ornaments sitting ON the band -- star at
    12h, diamonds at +-26deg, sparkles at 80/180/220/280deg, two tapered
    swoosh arcs with a star at their leading end. Nothing more."""
    band = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    box = (cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1)
    for ang in range(0, 360, 2):
        col = _ring_color(ang)
        bd.arc(box, start=ang - 2, end=ang + 3, fill=(*col, 255), width=5)
    band = band.filter(ImageFilter.GaussianBlur(0.7))
    glow = band.filter(ImageFilter.GaussianBlur(7))
    glow.putalpha(glow.split()[3].point(lambda a: int(a * 0.5)))
    img.alpha_composite(glow)
    img.alpha_composite(band)
    # faint echo arc just outside the band (reference shows one thin ring)
    echo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(echo).ellipse((cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9),
                                 outline=(150, 110, 200, 30), width=2)
    echo = echo.filter(ImageFilter.GaussianBlur(1))
    img.alpha_composite(echo)

    acc = (186, 120, 240)
    draw = ImageDraw.Draw(img)
    # 12h star: two-tone (pale crown over violet body), soft bloom
    sx, sy, sr = cx, cy - r - 11, 19
    _draw_glow_layer(img, lambda d: _draw_five_point_star(d, sx, sy, sr * 1.25,
                                                          (150, 80, 220)), blur=4)
    _draw_five_point_star(draw, sx, sy, sr, (150, 85, 215))
    draw.polygon([(sx, sy - sr), (sx + sr * 0.47, sy - sr * 0.31),
                  (sx, sy - sr * 0.10), (sx - sr * 0.47, sy - sr * 0.31)],
                 fill=(205, 170, 240))
    # diamonds at +-26deg, rotated to the ring tangent
    for sgn in (-1, 1):
        a = math.radians(26 * sgn)
        dx, dy = cx + r * math.sin(a), cy - r * math.cos(a)
        hw, hh = 10, 17
        base = [(0, -hh), (hw, 0), (0, hh), (-hw, 0)]
        ca, sa = math.cos(a), math.sin(a)
        rot = [(px * ca - py * sa, px * sa + py * ca) for px, py in base]
        draw.polygon([(dx + px, dy + py) for px, py in rot], fill=(140, 80, 205, 255))
    # sparkles on the band
    for ang, sr2 in ((80, 9), (280, 9), (200, 9)):
        a = math.radians(ang)
        px_, py_ = cx + r * math.sin(a), cy - r * math.cos(a)
        _draw_sparkle_star(img, draw, px_, py_, sr2, acc)
    # tapered swoosh arcs with a leading star (upper-right / lower-left)
    for a0, a1, star_ang, ssr in ((36, 74, 40, 10), (202, 240, 232, 9)):
        for off, w, al in ((15, 4, 150), (15, 3, 200), (15, 2, 245)):
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            rr = r + off
            lb = (cx - rr, cy - rr, cx + rr, cy + rr)
            seg0, seg1 = (a0 + (4 - w) * 3, a1 - (4 - w) * 3) if w < 3 else (a0, a1)
            ld.arc(lb, start=seg0 - 90, end=seg1 - 90, fill=(*acc, al), width=w)
            layer = layer.filter(ImageFilter.GaussianBlur(0.6))
            img.alpha_composite(layer)
        a = math.radians(star_ang)
        px_, py_ = cx + (r + 15) * math.sin(a), cy - (r + 15) * math.cos(a)
        _draw_sparkle_star(img, draw, px_, py_, ssr, acc)


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
    gd.ellipse((cx - r - 14, cy - r - 14, cx + r + 14, cy + r + 14),
              fill=(*COLORS["ring"], 22))
    glow = glow.filter(ImageFilter.GaussianBlur(12))
    img.alpha_composite(glow)


def _paste_avatar(img, data, avatar_im):
    draw = ImageDraw.Draw(img)
    ax, ay, aw, ah = LAYOUT["avatar"]
    cx, cy = ax + aw / 2, ay + ah / 2
    r = aw / 2

    _circle_mask_paste(img, avatar_im, (int(ax), int(ay), int(aw), int(ah)))

    # Pass 5: gradient band + reference ornaments (see _draw_ring).
    _draw_ring(img, cx, cy, r)

    # Level badge -- its own component (separate typography from the big
    # LEVEL number): dark-plum disc, muted 2px rim, compact stencil numerals
    # with fine notches, optically centered.
    badge_r = LAYOUT["avatar_level_badge_r"]
    bx = ax + LAYOUT["avatar_badge_center"][0]
    by = ay + LAYOUT["avatar_badge_center"][1]
    disc = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(disc)
    dd.ellipse((bx - badge_r, by - badge_r, bx + badge_r, by + badge_r),
               fill=(32, 16, 52, 245))
    dd.ellipse((bx - badge_r, by - badge_r, bx + badge_r, by + badge_r),
               outline=(85, 60, 115, 255), width=2)
    disc = disc.filter(ImageFilter.GaussianBlur(0.4))
    img.alpha_composite(disc)
    lvl_text = str(data["level"])
    lvl_font = zilla_bold(40)
    bbox = draw.textbbox((0, 0), lvl_text, font=lvl_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    _draw_stencil_number(img, draw, (bx - tw / 2, by - th / 2),
                         lvl_text, lvl_font, (216, 198, 229), cut_color=(32, 16, 52),
                         cut_scale=0.05, cut_depth=0.26)


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
                               fill=(10, 5, 26), outline=(52, 36, 80), width=1)
        label = _pill_safe(title["item_name"].upper())
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

    _draw_tracked_text(draw, (x + 28, y + 22), "RANK", outfit(15, "SemiBold"),
                       (140, 120, 165), tracking=3)
    # The reference's rank number is big, bold and purple (gradient +
    # bloom) -- part of the accent hierarchy, not white text. Pass 5: cap
    # measured at 46px (the old 76px font rendered ~61).
    _draw_gradient_text(img, draw, (x + 28, y + 50), f"#{data['rank']}",
                        outfit(61, "ExtraBold"), COLORS["rank_number_a"],
                        COLORS["rank_number_b"], glow=(150, 80, 230), ratio=0.95,
                        glow_alpha=0.28)

    # Two-tone: "TOP" muted, the percentage itself brighter -- matches the
    # reference's emphasis treatment.
    ty = y + 108
    draw.text((x + 28, ty), "TOP ", font=outfit(17, "Medium"), fill=COLORS["text_muted"])
    tw = draw.textbbox((0, 0), "TOP ", font=outfit(17, "Medium"))[2]
    draw.text((x + 28 + tw, ty), f"{data['percentile']:.2f}%",
              font=outfit(17, "SemiBold"), fill=(170, 140, 210))

    # reference divider: a whisper of a line with pin-head dots
    draw.line((x + 28, y + 145, x + w - 28, y + 145), fill=(34, 24, 52), width=1)
    draw.ellipse((x + 29, y + 144, x + 33, y + 148), fill=(70, 48, 105))
    draw.ellipse((x + w - 33, y + 144, x + w - 29, y + 148), fill=(70, 48, 105))

    tier = data["effective_prestige"]
    roman = ["0", "I", "II", "III", "IV", "V", "VI"][tier] if 0 <= tier <= 6 else str(tier)
    label = f"PRESTIGE {roman}" if tier else "PRESTIGE"
    big_font = _font("cinzel", 34, "SemiBold")
    small_font = _font("cinzel", 25, "SemiBold")
    first_w = draw.textbbox((0, 0), label[0], font=big_font)[2]
    rest_w = sum(draw.textbbox((0, 0), ch, font=small_font)[2] + 2 for ch in label[1:])
    natural_w = first_w + 2 + rest_w
    target_w = 119
    ratio = min(1.0, target_w / natural_w)

    def _prestige_painter(d):
        _draw_dropcap_heading(d, (20, 20), label, "cinzel", 34, 25, (190, 168, 222),
                              tracking=2, weight="SemiBold")
    _draw_condensed(img, (x + w / 2 - natural_w * ratio / 2, y + 168),
                    _prestige_painter, ratio=ratio,
                    glow=(140, 90, 200), blur=4, glow_alpha=90)

    # Pips: glowing 4-point sparkles when filled (the reference's shape);
    # empty tiers keep the SAME sparkle language, dimmed and unglossed --
    # hard outline diamonds read as UI geometry the reference never has.
    pip_r = 10
    pitch = 29
    total_pips = 6
    start_x = x + w / 2 - (total_pips - 1) * pitch / 2
    pip_y = y + 204
    for i in range(total_pips):
        cx = start_x + i * pitch
        filled = i < tier
        if filled:
            _draw_sparkle_star(img, draw, cx, pip_y, pip_r, COLORS["pip_filled"],
                               core=True, slim=0.24)
        else:
            _draw_sparkle_star(img, draw, cx, pip_y, 8, (58, 44, 80), glow=False)

    if int(data.get("effective_prestige") or 0) == 6:
        by = y + 218
        bw = 150
        bh = 23
        bx = x + w / 2 - bw / 2
        pts = [(bx + 7, by), (bx + bw - 7, by), (bx + bw, by + bh / 2),
               (bx + bw - 7, by + bh), (bx + 7, by + bh), (bx, by + bh / 2)]
        draw.polygon(pts, fill=(28, 12, 46))
        draw.line(pts + [pts[0]], fill=(95, 60, 140), width=1)
        icon = icons16.get("crown")
        lf2 = outfit(9, "SemiBold")
        label2 = "BOOSTER PRESTIGE"
        lw = _text_size(draw, label2, lf2, tracking=1)[0]
        icon_w = 13 if icon is not None else 0
        block_w = icon_w + (4 if icon_w else 0) + lw
        start = bx + (bw - block_w) / 2
        if icon is not None:
            ic = icon.resize((13, 13))
            img.paste(ic, (int(start), int(by + 5)), ic)
        _draw_tracked_text(draw, (start + icon_w + (4 if icon_w else 0), by + bh / 2),
                           label2, lf2, (190, 175, 215), tracking=1, anchor="lm")


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

    # Big stencil-slab number. Pass 5: measured off the reference -- cap 66px
    # (not 78), ~6px of air between the digits, a touch wider than the font's
    # natural advance, FINE notches, and the reference's wide soft bloom.
    lvl_font = zilla_bold(92)
    _draw_stencil_number(img, draw, (x + 40, y + 71), str(data["level"]),
                         lvl_font, (226, 212, 236), cut_color=(16, 8, 30),
                         glow=(150, 80, 220), tracking=4, ratio=1.0,
                         glow_blur=9, glow_alpha=120)

    x, y, w, h = LAYOUT["xp_totalxp_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16)

    # Pass 5: the reference's dynamic numerals are a clean bold SANS at cap
    # ~20 with NO horizontal squeeze (the old 0.64-0.80 ratios distorted them
    # more the longer they got). Labels are cap ~10.
    _draw_tracked_text(draw, (x + 14, y + 28), "XP PROGRESS", outfit(14, "SemiBold"),
                       (125, 105, 150), tracking=2)
    cur, needed = data["xp_current"], max(data["xp_needed"], 1)
    # Two-tone: current XP purple/emphasized, "/ needed XP" muted.
    cur_txt = f"{cur:,}"
    vf = outfit(28, "Bold")

    def _xpval_painter(d):
        d.text((20, 20), cur_txt, font=vf, fill=COLORS["xp_value"])
    cw, _ = _draw_condensed(img, (x + 14, y + 55), _xpval_painter, ratio=1.0,
                            glow=(150, 70, 210), blur=4, glow_alpha=90)
    draw.text((x + 14 + cw + 8, y + 75), f"/ {needed:,} XP", font=outfit(22),
              fill=(150, 140, 165), anchor="ls")

    div_x = x + LAYOUT["xp_divider_x"]
    draw.line((div_x, y + 14, div_x, y + h - 14), fill=(40, 28, 66), width=1)

    bar_x, bar_y, bar_w, bar_h = x + 8, y + 90, w - 20, 19
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                           radius=bar_h // 2, fill=(20, 11, 60))
    frac = min(cur / needed, 1.0)
    if frac > 0:
        fill_w = bar_w * frac
        # soft luminous bloom around the filled portion only
        _draw_glow_layer(img, lambda d: d.rounded_rectangle(
            (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h), radius=bar_h // 2,
            fill=(150, 60, 220, 90)), blur=6)
        _draw_gradient_bar(img, bar_x, bar_y, fill_w, bar_h,
                           COLORS["xp_bar_fill_a"], COLORS["xp_bar_fill_b"])
        # reference gloss: bright top row, shaded bottom row on the fill
        gloss = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd2 = ImageDraw.Draw(gloss)
        gd2.line((bar_x + 3, bar_y + 2, bar_x + fill_w - 3, bar_y + 2),
                 fill=(255, 255, 255, 55), width=2)
        gd2.line((bar_x + 3, bar_y + bar_h - 3, bar_x + fill_w - 3, bar_y + bar_h - 3),
                 fill=(40, 8, 80, 110), width=2)
        gloss = gloss.filter(ImageFilter.GaussianBlur(1))
        img.alpha_composite(gloss)
        # Subtle highlight at the leading edge of the FILL itself (not the
        # bar's outer end) -- per the reference. Small soft dot, nothing more.
        if 4 < fill_w < bar_w - 2:
            hx, hy = bar_x + fill_w - 4, bar_y + bar_h / 2
            _draw_soft_dot(img, hx, hy, 4, (255, 235, 250, 190))
    draw.text((bar_x + 4, bar_y + bar_h + 6), f"{frac * 100:.1f}% to next level",
              font=outfit(18), fill=COLORS["text_muted"])

    tx = div_x + 22
    _draw_tracked_text(draw, (tx, y + 22), "TOTAL XP", outfit(14, "SemiBold"),
                       (150, 105, 205), tracking=2)
    potion = _draw_potion_icon(34)
    img.paste(potion, (int(div_x + 16), int(y + 35)), potion)
    total_txt = f"{data['xp_total']:,}"
    tf2 = outfit(28, "Bold")

    def _total_painter(d):
        d.text((20, 20), total_txt, font=tf2, fill=(228, 222, 238))
    _draw_condensed(img, (div_x + 56, y + 47), _total_painter, ratio=1.0,
                    glow=(140, 120, 200), blur=4, glow_alpha=70)


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

    lab_violet = (150, 110, 200)   # reference label tone
    cards = [
        (icons30.get("messages"), "MESSAGES", f"{data['messages_count']:,}",
         lab_violet),
        (icons30.get("voice"), "VOICE TIME", _fmt_minutes(data["voice_minutes"]),
         lab_violet),
        (coin_icon_im, coins_cfg["name"].upper(), f"{data['balance']:,}",
         coin_label_color),
        (diamond_icon_im, diamonds_cfg["name"].upper(), f"{data['diamonds']:,}",
         lab_violet),
        (icons30.get("games"), "MINI GAMES", f"{data['minigame_wins']:,}",
         lab_violet),
    ]

    y = LAYOUT["stats_row_y"]
    h = LAYOUT["stats_row_h"]
    w = LAYOUT["stats_card_w"]
    gap = LAYOUT["stats_gap"]
    x0 = LAYOUT["stats_start_x"]

    for i, (icon_im, label, value, label_color) in enumerate(cards):
        x = x0 + i * (w + gap)
        # Pass 5: reference cards are a neutral-dark fill with a 1px border
        # only ~+9 lum above the page -- separation without rigidity.
        _rounded_panel(img, draw, (x, y, x + w, y + h), radius=14,
                       fill=COLORS["card_fill"], outline=COLORS["card_border"])
        if label == coins_cfg["name"].upper() and coins_cfg["emoji"] == "🪙":
            # the reference's coin card carries a faint warm halo from icon
            warm = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(warm).ellipse((x + w / 2 - 22, y + 14, x + w / 2 + 22, y + 62),
                                         fill=(120, 80, 35, 60))
            warm = warm.filter(ImageFilter.GaussianBlur(10))
            img.alpha_composite(warm)
        if icon_im is not None:
            ic = icon_im if icon_im.width == 34 else icon_im.resize((34, 34))
            # reference icons carry a soft bloom
            gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
            gl.paste(ic, (int(x + w / 2 - 17), y + 23), ic)
            gl.putalpha(gl.split()[3].point(lambda a: int(a * 0.75)))
            gl = gl.filter(ImageFilter.GaussianBlur(3))
            img.alpha_composite(gl)
            img.paste(ic, (int(x + w / 2 - 17), y + 23), ic)
        # Pass 5: values are clean bold sans at cap ~17, labels cap ~9 with
        # light tracking -- no horizontal squeezing at any digit count.
        draw.text((x + w / 2, y + 105), value, font=outfit(24, "SemiBold"),
                  fill=(233, 233, 239), anchor="ms")
        _draw_tracked_text(draw, (x + w / 2, y + 130), label,
                           outfit(12, "SemiBold"), label_color,
                           tracking=0, anchor="ms")


def _fmt_minutes(total_minutes) -> str:
    total_minutes = int(total_minutes or 0)
    h = total_minutes // 60
    return f"{h}h" if h else f"{total_minutes}m"


def _draw_inventory(img, draw, data, icons16, item_icons=None):
    x, y, w, h = LAYOUT["inventory_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16,
                   fill=COLORS["inv_panel"], outline=COLORS["inv_border"])
    label_x = x + 30
    if icons16.get("inventory") is not None:
        ic = icons16["inventory"].resize((25, 25))
        img.paste(ic, (label_x, y + 23), ic)
        label_x += 35
    _draw_tracked_text(draw, (label_x, y + 26), "INVENTORY", outfit(15, "SemiBold"),
                       (150, 105, 205), tracking=3)
    # "owned / catalog total" at the right of the header, as in the
    # reference (12 / 48): owned bright, the rest muted.
    owned = data.get("owned_count")
    total = data.get("inventory_total")
    if owned is not None and total is not None:
        f_c = outfit(15, "SemiBold")
        t1, t2 = f"{owned}", f" / {total}"
        w1 = draw.textbbox((0, 0), t1, font=f_c)[2]
        w2 = draw.textbbox((0, 0), t2, font=f_c)[2]
        sx = x + w - 20 - (w1 + w2)
        draw.text((sx, y + 26), t1, font=f_c, fill=(200, 195, 215))
        draw.text((sx + w1, y + 26), t2, font=f_c, fill=COLORS["text_muted"])

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
        # Pass 5: reference slots are soft dark rounded squares with NO
        # outline -- the grid reads from fill contrast alone. Structure
        # (3x4, 12 slots, empties visible) is unchanged.
        draw.rounded_rectangle((sx, sy, sx + sw, sy + sh), radius=14,
                               fill=COLORS["slot_fill"])
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
    new_h = int(art_h * LAYOUT["mailbox_scale_y"])
    new_w = int(new_h / aspect * LAYOUT["mailbox_scale_x"])

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
    draw.polygon(tag, fill=(12, 6, 26))
    draw.line(tag + [tag[0]], fill=(35, 24, 52), width=1)
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
