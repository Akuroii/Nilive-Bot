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
import functools

import aiohttp
import uharfbuzz as hb
import freetype
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageChops

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

# Re-measured independently: a least-squares circle fit (120 boundary
# samples every 3 degrees, alpha>60 threshold) to the ring PNG's actual
# inner-hole boundary gives a true center of ~(509, 509), not (490, 509) --
# the hole itself is asymmetric (tendrils reach ~40px further in on the
# left than the right), which the single scalar radius above averages out
# reasonably (measured ~266 vs the stored 265) but the X center does not.
# Rather than move the ring (and disturb the level badge / everything else
# anchored to the avatar box), this raw source-space delta is scaled by
# _paste_avatar (same scale factor used for the ring) and applied to the
# avatar image only, so it sits concentric with where the ring's hole
# actually is instead of where the old constant assumed it was.
AVATAR_RING_INNER_CENTER_X_TRUE = 509   # px, source-space, see comment above

# Supplied prestige-crystal artwork (source of truth). Each PNG carries a
# large transparent glow-falloff margin; these content boxes (alpha > ~10)
# were measured directly off the two assets so the crystals can be trimmed
# to their visible art before being laid out as six equal slots.
ACTIVE_CRYSTAL_PNG_PATH = _asset_path("prestige_crystal_active.png", "active_crystal.png")
INACTIVE_CRYSTAL_PNG_PATH = _asset_path("prestige_crystal_inactive.png", "inactve_crystal.png")
ACTIVE_CRYSTAL_CONTENT_BOX = (0, 691, 2776, 4280)     # x0,y0,x1,y1 in source px
INACTIVE_CRYSTAL_CONTENT_BOX = (0, 468, 1257, 2233)   # x0,y0,x1,y1 in source px

# Supplied stat-row icon artwork (source of truth -- not redrawn/regenerated).
# Replaces the hand-drawn line icons below for these three keys only; see
# _load_stat_icon_asset / LINE_ICON_BUILDERS.
STAT_ICON_MESSAGES_PNG_PATH = _asset_path("stat_icon_messages.png", "stat_icon_messages.png")
STAT_ICON_VOICE_PNG_PATH = _asset_path("stat_icon_voice.png", "stat_icon_voice.png")
STAT_ICON_GAMES_PNG_PATH = _asset_path("stat_icon_games.png", "stat_icon_games.png")

FONT_PATHS = {
    "cinzel": _asset_path(os.path.join("fonts", "Cinzel-Variable.ttf"), "Cinzel-Variable.ttf"),
    "zilla_bold": _asset_path(os.path.join("fonts", "ZillaSlab-Bold.ttf"), "ZillaSlab-Bold.ttf"),
    "outfit": _asset_path(os.path.join("fonts", "Outfit-Variable.ttf"), "Outfit-Variable.ttf"),
    "amiri_regular": _asset_path(os.path.join("fonts", "Amiri-Regular.ttf"), "Amiri-Regular.ttf"),
    "amiri_bold": _asset_path(os.path.join("fonts", "Amiri-Bold.ttf"), "Amiri-Bold.ttf"),
    # Real Amira Typo face (user-supplied), used ONLY for the dynamic
    # currency-name label (see currency_name_style below). Arabic-script
    # glyphs only -- no Latin coverage -- so it is paired with zilla_bold
    # for non-Arabic currency names rather than used on its own for both.
    "amira_typo": _asset_path(os.path.join("fonts", "Amira-Typo.ttf"), "Amira-Typo.ttf"),
    "tajawal_regular": _asset_path(os.path.join("fonts", "Tajawal-Regular.ttf"), "Tajawal-Regular.ttf"),
    "tajawal_medium": _asset_path(os.path.join("fonts", "Tajawal-Medium.ttf"), "Tajawal-Medium.ttf"),
    "tajawal_bold": _asset_path(os.path.join("fonts", "Tajawal-Bold.ttf"), "Tajawal-Bold.ttf"),
    "stencil": _asset_path(os.path.join("fonts", "STENCIL.TTF"), "STENCIL.TTF"),
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
    "gold_label": (168, 142, 116),      # unused now -- currency labels use
                                         # the slot colors below instead
    # Currency-slot colors (deliberately outside the purple/magenta family
    # used everywhere else on the card, and deliberately not gold). Applied
    # by SLOT ("coins" = primary/first-configured currency, "diamonds" =
    # secondary), never by the currency's configured display name, since
    # both are rename-able by the guild owner.
    "currency_pearl": (0xDD, 0xF7, 0xFF),       # coins slot  -- icy pearl
    "currency_pearl_glow": (0x9D, 0xEB, 0xFF),  # coins slot  -- subtle glow
    "currency_shell": (0xD6, 0xB4, 0xFC),       # diamonds slot -- label color per latest request (was coral shell 0xFFB8A8)
    "currency_shell_glow": (0xFF, 0x8F, 0xA3),  # diamonds slot -- subtle glow
}

# ─────────────────────────────────────────────────────────────────────────
# LAYOUT — fixed canvas 1280x853, measured off the reference (x1.25 scale)
# ─────────────────────────────────────────────────────────────────────────

CANVAS_W, CANVAS_H = 1280, 853

LAYOUT = {
    # Pass 5: avatar/ring shrunk to a medium size per the avatar position
    # reference + client feedback -- the supplied ring artwork flares ~1.6x
    # past its own inner circle, so it needs a smaller avatar circle than
    # the reference's own (tighter) ring to clear the username/title
    # column. Badge center/radius measured as a ratio of the avatar radius
    # off the avatar position reference ((dx,dy)=(1.0r,1.18r) from the
    # avatar's own center, radius=0.23r) and reapplied at the new size.
    "avatar": (47, 71, 185, 185),
    "avatar_level_badge_r": 21,
    "avatar_level_badge_font": 31,
    "avatar_badge_center": (185, 202),   # relative to avatar origin

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
    # Left edge realigned to match rank_prestige_panel's x (was 710 vs 706 --
    # a 4px mismatch that broke the shared-column look); small positive gap
    # added below the rank panel instead of a 1px overlap.
    "inventory_panel": (706, 318, 308, 422),
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


def stencil(size):
    return ImageFont.truetype(FONT_PATHS["stencil"], size)


def outfit(size, weight="Regular"):
    return _font("outfit", size, weight)


def amiri(size, bold=False):
    return ImageFont.truetype(FONT_PATHS["amiri_bold" if bold else "amiri_regular"], size)


def amira_typo(size):
    # RAQM layout engine so glyph shaping/joining goes through the font's
    # own GSUB tables directly (it has full Arabic shaping rules), rather
    # than PIL's plain layout + a manual reshape-to-presentation-forms pass.
    # The manual-reshape path (used for amiri() above, which lacks GSUB)
    # produces isolated-form codepoints this font doesn't carry for every
    # letter (e.g. isolated teh marbuta) -- raqm avoids that entirely by
    # shaping the real base codepoints.
    #
    # If raqm isn't available in THIS process (PIL.ImageFont.core.HAVE_RAQM
    # is False -- e.g. Pillow built/installed without libraqm on this
    # platform), requesting layout_engine=RAQM here does not fail: Pillow
    # silently downgrades to Layout.BASIC and draws the raw logical-order
    # string with no bidi reordering and no shaping, which for RTL text
    # reads back-to-front (confirmed: reproduces exactly as the reported
    # "لؤلؤة" -> "ةؤلؤل" symptom). Detect that condition explicitly here
    # rather than let it happen silently -- see currency_name_style, which
    # pre-reorders the text with get_display() ONLY on this branch so the
    # two stay in sync and RAQM-available rendering below is unchanged.
    if not ImageFont.core.HAVE_RAQM:
        log.warning(
            "rank_card_renderer: raqm unavailable in this process -- Arabic "
            "currency labels will use the bidi-reorder-only BASIC-layout "
            "fallback (letters render correctly ordered but not cursively "
            "joined, since BASIC applies no GSUB shaping). Install "
            "libraqm/libfribidi in this environment for full shaping."
        )
        return ImageFont.truetype(FONT_PATHS["amira_typo"], size,
                                  layout_engine=ImageFont.Layout.BASIC)
    return ImageFont.truetype(FONT_PATHS["amira_typo"], size,
                              layout_engine=ImageFont.Layout.RAQM)


def tajawal(size, weight="regular"):
    return ImageFont.truetype(FONT_PATHS[f"tajawal_{weight}"], size)


# Arabic block ranges covering the configurable currency-name case (Arabic,
# Arabic Supplement, Arabic Presentation Forms A/B) -- same detection surface
# python-bidi/arabic_reshaper are meant for.
_ARABIC_RANGES = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
                  (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


def _is_arabic_text(text: str) -> bool:
    return any(any(lo <= ord(ch) <= hi for lo, hi in _ARABIC_RANGES) for ch in text)


def _shape_arabic(text: str) -> str:
    """Reshape + apply bidi so Arabic renders as joined, correct-visual-order
    glyphs instead of isolated forms/boxes. Mirrors the existing footer-tagline
    approach (Pillow has no Arabic shaping of its own); falls back to the raw
    string if the (pure-python) shaping libs aren't available."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


# Bump applied to the Arabic font size so a configured Arabic currency name
# reads with similar visual weight/presence to the Latin default -- Amira
# Typo at the same point size as Zilla Slab Bold sits visually smaller/
# lighter (measured the same way the earlier Amiri/Outfit bump was).
_ARABIC_CURRENCY_SIZE_BUMP = 1.08


def _fit_currency_label(draw, name, base_font_fn, base_size, max_width,
                        condense_ratio, min_size=12):
    """Like _fit_numeral_font, but for a dynamic currency-name label that
    also goes through currency_name_style's font/shaping choice and the
    row's usual condense_ratio squeeze. Shrinks base_size until the label,
    AFTER the condense squeeze, fits max_width -- names are admin-typed
    (utils/currency.py has no length cap), so an unfit long name would
    otherwise overflow the stat card / overlap its neighbor.
    Returns (font, text_to_draw, is_arabic, natural_width)."""
    size = base_size
    while size > min_size:
        lf, label_text, label_is_arabic, label_mask = currency_name_style(
            name, base_font_fn, size)
        if label_mask is not None:
            lw_nat = label_mask.width
        else:
            lw_nat = _text_size(draw, label_text, lf,
                                tracking=(0 if label_is_arabic else 2))[0]
        if lw_nat * condense_ratio <= max_width:
            return lf, label_text, label_is_arabic, lw_nat, label_mask
        size -= 1
    lf, label_text, label_is_arabic, label_mask = currency_name_style(
        name, base_font_fn, min_size)
    if label_mask is not None:
        lw_nat = label_mask.width
    else:
        lw_nat = _text_size(draw, label_text, lf,
                            tracking=(0 if label_is_arabic else 2))[0]
    return lf, label_text, label_is_arabic, lw_nat, label_mask


@functools.lru_cache(maxsize=8)
def _hb_face(font_path: str):
    with open(font_path, "rb") as f:
        return hb.Face(f.read())


def shape_arabic_mask(text: str, font_path: str, size_px: int):
    """RAQM-independent Arabic shaping: real HarfBuzz (uharfbuzz -- its own
    bundled HarfBuzz, unrelated to whatever Pillow's _imagingft was built
    with) applies the font's own GSUB init/medi/fina/rlig rules directly to
    the base codepoints (no presentation-form substitution, no reshaping
    library, no manual reversal), then FreeType rasterizes the shaped glyph
    run by glyph index. Returns (alpha_mask_image, advance_width_px); the
    mask is tight-cropped to its own bbox, "L" mode, fully antialiased.
    """
    face_hb = _hb_face(font_path)
    font_hb = hb.Font(face_hb)
    upem = face_hb.upem
    font_hb.scale = (upem, upem)

    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font_hb, buf)

    ft_face = freetype.Face(font_path)
    ft_face.set_pixel_sizes(0, size_px)
    scale = size_px / upem

    pen_x, pen_y = 0.0, 0.0
    glyphs = []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        ft_face.load_glyph(info.codepoint, freetype.FT_LOAD_RENDER)
        bmp = ft_face.glyph.bitmap
        left, top = ft_face.glyph.bitmap_left, ft_face.glyph.bitmap_top
        x = pen_x + pos.x_offset * scale
        y = pen_y - pos.y_offset * scale
        if bmp.width and bmp.rows:
            # Plain frombytes off FreeType's own buffer -- no numpy needed.
            glyph_img = Image.frombytes("L", (bmp.width, bmp.rows), bytes(bmp.buffer))
            glyphs.append((glyph_img, left, top, x, y))
        pen_x += pos.x_advance * scale
        pen_y += pos.y_advance * scale

    if not glyphs:
        return Image.new("L", (1, 1), 0), 0
    canvas = Image.new("L", (int(pen_x) + size_px, size_px * 3), 0)
    baseline_y = size_px * 2
    for g, left, top, x, y in glyphs:
        canvas.paste(g, (int(x) + left, int(baseline_y - top - y)), g)
    bbox = canvas.getbbox()
    if bbox:
        canvas = canvas.crop(bbox)
    return canvas, int(pen_x)


def _draw_condensed_mask(img, xy, mask, color, ratio=1.0, glow=None, blur=4,
                         glow_alpha=0.35):
    """Composite an already-rendered alpha mask (from shape_arabic_mask)
    with a solid color, condensed by `ratio` -- the mask-based counterpart
    to _draw_condensed (which takes a painter(draw) callback instead).
    Kept separate rather than folded into _draw_condensed: ImageDraw.bitmap()
    does not treat an 'L'-mode mask as antialiased alpha (verified -- it
    hard-thresholds), so a mask needs a direct Image.paste compositing path
    that _draw_condensed's painter(draw)-only callback can't reach; this
    function does not modify _draw_condensed or any of its call sites."""
    if mask.getbbox() is None:
        return (0, 0)
    if ratio != 1.0:
        mask = mask.resize((max(1, int(round(mask.width * ratio))), mask.height),
                           Image.LANCZOS)
    if glow:
        gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gl.paste(Image.new("RGBA", mask.size, (*glow, 255)),
                (int(xy[0]), int(xy[1])), mask)
        gl.putalpha(gl.split()[3].point(lambda a: int(a * glow_alpha)))
        gl = gl.filter(ImageFilter.GaussianBlur(blur))
        img.alpha_composite(gl)
    solid = Image.new("RGBA", mask.size, (*color, 255))
    img.paste(solid, (int(xy[0]), int(xy[1])), mask)
    return mask.size


def currency_name_style(name: str, base_font_fn, base_size: int):
    """Single reusable place to decide how a *configurable* currency name
    gets drawn, since utils/currency.py puts no restriction on what an admin
    types in. Any renderer in this module that draws a dynamic currency name
    should go through this instead of hardcoding a font.

    Amira Typo (user-supplied) is the correct face for this label, but the
    file itself carries Arabic-script glyphs only -- no Latin letters at all
    (checked its cmap directly) -- so it can only be used on the Arabic
    branch; an English name set in it would render as empty .notdef boxes.
    It is paired with base_font_fn (Zilla Slab Bold at this call site) for
    the non-Arabic branch so both scripts are covered:

      - Arabic name -> Amira Typo via raqm (the font's own GSUB shaping,
        not the manual-reshape helper other Arabic text in this module
        uses), sized ~8% up so it carries the same visual weight as the
        Latin branch.
      - Non-Arabic name -> unchanged shape: base_font_fn(base_size), raw
        text, no shaping.

    Returns (font, text_to_draw, is_arabic). is_arabic tells the caller the
    text is Arabic (drawn via raqm, which needs no per-glyph tracking --
    raqm already handles inter-glyph spacing/joining correctly, and manual
    tracking would insert gaps into shaped ligatures).
    """
    if _is_arabic_text(name):
        size = round(base_size * _ARABIC_CURRENCY_SIZE_BUMP)
        if not ImageFont.core.HAVE_RAQM:
            # raqm unavailable: BASIC layout cannot join Arabic glyphs no
            # matter what text it's given (no GSUB shaping happens at all),
            # so instead of drawing text via Pillow, shape with an
            # independent HarfBuzz binding (uharfbuzz -- bundles its own
            # HarfBuzz, decoupled from whatever Pillow's _imagingft was
            # built with) and rasterize with FreeType, applying this
            # font's own init/medi/fina/rlig GSUB rules directly to the
            # base codepoints. Returns a pre-rendered alpha mask instead
            # of a (font, text) pair; caller composites it directly (see
            # _draw_condensed_mask) rather than calling d.text().
            mask, _adv = shape_arabic_mask(name, FONT_PATHS["amira_typo"], size)
            return None, None, True, mask
        return amira_typo(size), name, True, None
    return base_font_fn(base_size), name, False, None


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


def _fit_numeral_font(draw, text, font_fn, max_width, start_size, min_size=14):
    """Pick the largest integer point size (<= start_size) at which `text`
    fits within max_width -- i.e. fit numerals by adjusting the actual font
    size, the normal typesetting way. This replaces render-at-fixed-size
    then squeeze-the-raster-horizontally (_draw_condensed with ratio<1 on a
    plain number): a non-uniform horizontal squash thins vertical stems
    without thinning horizontal ones, which is exactly what reads as
    distorted/squeezed/uneven numerals. Sizing down keeps every glyph's
    proportions intact -- softer on width than the old squeeze ratios, but
    actually clean at 100% zoom. Returns (font, natural_width)."""
    size = start_size
    while size > min_size:
        f = font_fn(size)
        w = draw.textbbox((0, 0), text, font=f)[2]
        if w <= max_width:
            return f, w
        size -= 1
    f = font_fn(min_size)
    return f, draw.textbbox((0, 0), text, font=f)[2]


def _draw_stencil_number(img, draw, xy, text, font, fill, glow=None):
    """Big slab numerals (Level number, badge number) drawn with the repo's
    actual STENCIL.TTF -- the font's own cut notches provide the stencil
    look, so this just draws glyphs (with an optional soft bloom behind
    them), no manual notch-carving. `font` is expected to already be a
    stencil() instance; kept as a parameter (rather than hardcoded) so
    callers control size."""
    x, y = xy
    if glow:
        _draw_glow_layer(img, lambda d: d.text((x, y), text, font=font, fill=(*glow, 130)),
                         blur=8)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((x, y), text, font=font, fill=(*fill, 255))
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


# ─────────────────────────────────────────────────────────────────────────
# STAT-ROW ICON ASSETS — Messages / Voice Time / Games Won now use the
# supplied artwork below instead of the hand-drawn line icons above. Each
# source PNG is a large canvas with a variable transparent margin, so it's
# trimmed to its opaque content first, then re-centered with a consistent
# margin (matching the ~14-24% margin the hand-drawn icons above carried)
# before being downsampled. _draw_stat_cards always displays these three
# at a fixed 34px regardless of the supersample `size` this builder is
# invoked with (68, needed only so the hand-drawn AA-less icons below look
# smooth) -- resizing straight from the source's native resolution to that
# 34px display target in one LANCZOS pass keeps these sharper than routing
# through the intermediate 68px step would. Cached per (path, size) since
# the files are static across renders.
# ─────────────────────────────────────────────────────────────────────────

_STAT_ICON_DISPLAY_SIZE = 40
_STAT_ICON_MARGIN = 0.14  # fraction of the trimmed content's longer side


@functools.lru_cache(maxsize=None)
def _load_stat_icon_asset(path: str, size: int) -> Image.Image | None:
    if not os.path.isfile(path):
        log.warning("rank_card: stat icon asset not found at %s -- falling back "
                    "to the hand-drawn line icon.", path)
        return None
    try:
        im = Image.open(path)
        im.load()
        im = im.convert("RGBA")
    except Exception as e:
        log.warning("rank_card: failed to load stat icon %s: %s", path, e)
        return None

    bbox = im.split()[3].getbbox()
    if bbox is not None:
        im = im.crop(bbox)

    side = max(im.width, im.height)
    canvas_side = max(1, round(side * (1 + _STAT_ICON_MARGIN)))
    canvas = Image.new("RGBA", (canvas_side, canvas_side), (0, 0, 0, 0))
    canvas.paste(im, ((canvas_side - im.width) // 2, (canvas_side - im.height) // 2), im)

    return canvas.resize((size, size), Image.LANCZOS)


def _line_icon_messages_asset(size):
    return (_load_stat_icon_asset(STAT_ICON_MESSAGES_PNG_PATH, _STAT_ICON_DISPLAY_SIZE)
            or _line_icon_messages(size))


def _line_icon_voice_asset(size):
    return (_load_stat_icon_asset(STAT_ICON_VOICE_PNG_PATH, _STAT_ICON_DISPLAY_SIZE)
            or _line_icon_voice(size))


def _line_icon_games_asset(size):
    return (_load_stat_icon_asset(STAT_ICON_GAMES_PNG_PATH, _STAT_ICON_DISPLAY_SIZE)
            or _line_icon_games(size))


LINE_ICON_BUILDERS = {
    "messages": _line_icon_messages_asset,
    "voice": _line_icon_voice_asset,
    "games": _line_icon_games_asset,
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
    # The mask itself was drawn at 1:1 (185x185ish) with ImageDraw's plain
    # (non-antialiased) ellipse -- confirmed with a high-contrast test
    # pattern to produce a visibly hard-stepped circular edge. Drawing the
    # mask at 4x and downsampling with LANCZOS gives a properly antialiased
    # edge; this only changes the mask, not the avatar pixels themselves.
    ss = 4
    mask_big = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask_big).ellipse((0, 0, w * ss, h * ss), fill=255)
    mask = mask_big.resize((w, h), Image.LANCZOS)
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
            _resolve_currency_icon(session, data["currency"]["coins"]["emoji"],
                                   _STAT_ICON_DISPLAY_SIZE),
            _resolve_currency_icon(session, data["currency"]["diamonds"]["emoji"],
                                   _STAT_ICON_DISPLAY_SIZE),
        )
        item_icons = list(await asyncio.gather(*[
            _fetch_image(session, it["icon_url"]) if it.get("icon_url") else _no_icon()
            for it in grid
        ]))
    mailbox_im = await _load_mailbox()
    ring_im = await _load_avatar_ring()
    active_crystal_im, inactive_crystal_im = await _load_prestige_crystals()

    _draw_name_block(img, draw, data, line_icons_16)
    _draw_rank_prestige_panel(img, draw, data, line_icons_16,
                              active_crystal_im, inactive_crystal_im)
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


async def _load_prestige_crystals():
    """Returns (active_im, inactive_im), each trimmed to its own measured
    content box, or None for whichever asset is missing/unreadable."""
    def _load_one(path, box):
        if not os.path.isfile(path):
            log.warning("rank_card: prestige crystal asset not found at %s", path)
            return None
        try:
            im = Image.open(path)
            im.load()
            return im.convert("RGBA").crop(box)
        except Exception as e:
            log.warning("rank_card: failed to load prestige crystal asset %s: %s", path, e)
            return None
    return (_load_one(ACTIVE_CRYSTAL_PNG_PATH, ACTIVE_CRYSTAL_CONTENT_BOX),
            _load_one(INACTIVE_CRYSTAL_PNG_PATH, INACTIVE_CRYSTAL_CONTENT_BOX))


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

    # Avatar goes down first, ring artwork composited on top of it. The
    # ring's inner glow/edge has its own soft alpha falloff before it
    # reaches full opacity; with the avatar underneath, that falloff
    # blends against the avatar instead of exposing bare background,
    # which is what was reading as a gap between the two.
    avatar_d = (aw + ah) / 2
    ring_scale = avatar_d / (AVATAR_RING_INNER_RADIUS * 2)
    avatar_offset_x = round((AVATAR_RING_INNER_CENTER_X_TRUE - AVATAR_RING_INNER_CENTER[0])
                            * ring_scale)
    _circle_mask_paste(img, avatar_im,
                       (int(ax + avatar_offset_x), int(ay), int(aw), int(ah)))

    _paste_avatar_ring(img, ax, ay, aw, ah, ring_im)

    # Level badge -- attached to the avatar's lower right, stencil numerals
    # like the reference.
    badge_r = LAYOUT["avatar_level_badge_r"]
    bx = ax + LAYOUT["avatar_badge_center"][0]
    by = ay + LAYOUT["avatar_badge_center"][1]
    draw.ellipse((bx - badge_r, by - badge_r, bx + badge_r, by + badge_r),
                 fill=(14, 8, 22, 235), outline=(170, 130, 220, 200), width=2)
    lvl_text = str(data["level"])
    # Sized to fit inside the badge circle -- levels can run to 3 digits,
    # and the badge font was previously a fixed size regardless of digit
    # count, which pushed "905"-style levels outside the circle entirely.
    lvl_font, _ = _fit_numeral_font(draw, lvl_text, stencil,
                                    2 * badge_r - 10,
                                    LAYOUT["avatar_level_badge_font"], min_size=13)
    bbox = draw.textbbox((0, 0), lvl_text, font=lvl_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    _draw_stencil_number(img, draw, (bx - tw / 2 - bbox[0], by - th / 2 - bbox[1]),
                         lvl_text, lvl_font, (230, 220, 240))


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


def _draw_rank_prestige_panel(img, draw, data, icons16,
                              active_crystal_im=None, inactive_crystal_im=None):
    x, y, w, h = LAYOUT["rank_prestige_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16)

    # RANK heading is a bright light-purple label in the reference, not the
    # same muted gray as "TOP" -- sampled off the reference directly.
    _draw_tracked_text(draw, (x + 28, y + 20), "RANK", outfit(21, "SemiBold"),
                       COLORS["rank_number_a"], tracking=3)
    # The reference's rank number is big, bold and purple (gradient +
    # bloom) -- part of the accent hierarchy, not white text. Sized to fit
    # the panel width directly (rank can run to 3 digits on large servers)
    # instead of a fixed size + horizontal squeeze.
    rank_txt = f"#{data['rank']}"
    rank_font, _rw = _fit_numeral_font(draw, rank_txt, lambda s: outfit(s, "ExtraBold"),
                                       w - 56, 76, min_size=34)
    _draw_gradient_text(img, draw, (x + 28, y + 40), rank_txt,
                        rank_font, COLORS["rank_number_a"],
                        COLORS["rank_number_b"], glow=(150, 80, 230))

    # Two-tone: "TOP" muted, the percentage itself brighter -- matches the
    # reference's emphasis treatment.
    ty = y + 114
    draw.text((x + 28, ty), "TOP ", font=outfit(18, "Medium"), fill=COLORS["text_muted"])
    tw = draw.textbbox((0, 0), "TOP ", font=outfit(18, "Medium"))[2]
    # Saturated purple accent (sampled off the reference), not the pale
    # near-white the value was drifting toward.
    draw.text((x + 28 + tw, ty), f"{data['percentile']:.2f}%",
              font=outfit(18, "SemiBold"), fill=COLORS["accent"])

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

    # Six individual prestige-crystal slots: the supplied active/inactive
    # crystal artwork, unmodified apart from a uniform resize, one slot per
    # prestige level -- identical slot size, shared vertical center, equal
    # pitch. Falls back to the old vector sparkle/diamond pips if either
    # asset failed to load.
    crystal_h = 32
    pitch = 30
    total_pips = 6
    start_x = x + w / 2 - (total_pips - 1) * pitch / 2
    pip_y = y + 205
    for i in range(total_pips):
        cx = start_x + i * pitch
        filled = i < tier
        src = active_crystal_im if filled else inactive_crystal_im
        if src is not None:
            aspect = src.width / src.height
            cw = max(1, round(crystal_h * aspect))
            crystal = src.resize((cw, crystal_h), Image.LANCZOS)
            img.paste(crystal, (round(cx - cw / 2), round(pip_y - crystal_h / 2)), crystal)
        elif filled:
            _draw_sparkle_star(img, draw, cx, pip_y, 11, COLORS["pip_filled"])
        else:
            _draw_diamond_pip(draw, cx, pip_y, 11, COLORS["pip_empty"], filled)

    if int(data.get("effective_prestige") or 0) == 6:
        by = y + 224  # nudged down 6px to clear the taller crystal-slot pips
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
    bf = _font("cinzel", 36, "Bold")
    sf = _font("cinzel", 28, "Bold")
    nat_w = draw.textbbox((0, 0), "L", font=bf)[2] + 1 + sum(
        draw.textbbox((0, 0), ch, font=sf)[2] + 1 for ch in "EVEL")
    ratio = min(1.0, 91 / nat_w)

    def _level_painter(d):
        _draw_dropcap_heading(d, (20, 20), "LEVEL", "cinzel", 36, 28,
                              (200, 190, 215), tracking=1, weight="Bold")
    _draw_condensed(img, (x + 31, y + 20), _level_painter, ratio=ratio)

    # Big stencil-slab number, drawn with the repo's real STENCIL.TTF (the
    # font's own cut notches give the faceted look) plus the reference's
    # purple bloom. Positioned by the glyphs' actual bbox rather than an
    # ascent-ratio guess, since that guess was tuned to ZillaSlab's metrics
    # and STENCIL.TTF's are different (smaller internal leading above the
    # cap) -- this keeps the same visual cap-top target (y+66) regardless
    # of which font supplies the glyphs.
    lvl_text = str(data["level"])
    # Sized to fit the panel width -- same overflow problem as the badge
    # number: a fixed size regardless of digit count let 3-digit levels
    # spill out of the card's left edge.
    lvl_font, _ = _fit_numeral_font(draw, lvl_text, stencil, w - 45, 104, min_size=44)
    bbox0 = draw.textbbox((0, 0), lvl_text, font=lvl_font)
    ty = (y + 66) - bbox0[1]
    _draw_stencil_number(img, draw, (x + 30, ty), lvl_text, lvl_font,
                         (228, 214, 235), glow=(160, 85, 225))

    x, y, w, h = LAYOUT["xp_totalxp_panel"]
    _rounded_panel(img, draw, (x, y, x + w, y + h), radius=16)

    _draw_tracked_text(draw, (x + 14, y + 29), "XP PROGRESS", outfit(19, "SemiBold"),
                       COLORS["text_muted"], tracking=2)
    div_x = x + LAYOUT["xp_divider_x"]
    cur, needed = data["xp_current"], max(data["xp_needed"], 1)
    # Two-tone: current XP purple/emphasized, "/ needed XP" muted. The value
    # is fit to the space actually available before the divider (rather
    # than drawn at a fixed size and squeezed) so large XP totals don't
    # distort -- see _fit_numeral_font.
    cur_txt = f"{cur:,}"
    suffix_txt = f"/ {needed:,} XP"
    suffix_font = outfit(22)
    suffix_w = draw.textbbox((0, 0), suffix_txt, font=suffix_font)[2]
    # Budget leaves extra room (14px, not just a hairline) before the
    # suffix -- at full/near-full glyph size there's less natural slack
    # than the old squeezed version had, and the glow's blur bleeds a few
    # px past the glyph edge, so a tight gap read as the value and the
    # "/ needed XP" text overlapping.
    available = (div_x - 10) - (x + 14) - 14 - suffix_w
    vf, cur_w = _fit_numeral_font(draw, cur_txt, zilla_bold, max(available, 40), 45,
                                  min_size=22)
    _draw_glow_layer(img, lambda d: d.text((x + 14, y + 50), cur_txt, font=vf,
                                            fill=(150, 70, 210, 140)), blur=3)
    draw.text((x + 14, y + 50), cur_txt, font=vf, fill=COLORS["xp_value"])
    draw.text((x + 28 + cur_w, y + 58), suffix_txt, font=outfit(22),
              fill=COLORS["text_muted"])

    draw.line((div_x, y + 14, div_x, y + h - 14), fill=(*COLORS["accent"], 30), width=1)

    bar_x, bar_y, bar_w, bar_h = x + 8, y + 86, w - 20, 21
    frac = min(cur / needed, 1.0)
    _draw_xp_bar(img, bar_x, bar_y, bar_w, bar_h, frac)
    draw.text((bar_x + 6, bar_y + bar_h + 6), f"{frac * 100:.1f}% to next level",
              font=outfit(19), fill=COLORS["text_muted"])

    tx = div_x + 22
    _draw_tracked_text(draw, (tx, y + 25), "TOTAL XP", outfit(20, "SemiBold"),
                       COLORS["label_purple"], tracking=2)
    potion = _draw_potion_icon(27)
    img.paste(potion, (int(div_x + 19), int(y + 42)), potion)
    total_txt = f"{data['xp_total']:,}"
    total_x = div_x + 52
    total_available = (x + w) - total_x - 12
    tf2, _tw = _fit_numeral_font(draw, total_txt, zilla_bold, max(total_available, 40), 38,
                                 min_size=18)
    draw.text((total_x, y + 44), total_txt, font=tf2, fill=(205, 200, 215))


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


def _draw_xp_wave_fill(fw, bar_h, radius, color_a, color_b):
    """Builds the filled portion of the XP pill as one masked RGBA layer:
    a horizontal purple gradient with two translucent sine-wave ribbons
    layered on top (a bright one and a darker one, different wavelength/
    phase) so the fill reads as flowing liquid rather than a flat tint --
    the reference's "fluid" character -- instead of the two static
    corner-blob highlights this replaces. frac/width already resolved by
    the caller; this only ever draws the actual fw>0 case.

    Rendered supersampled then LANCZOS-downsampled (same trick used
    elsewhere in this file for AA masks, see _fit_avatar/_load_crystals
    comments) so the ribbons' diagonal edges stay crisp at the bar's
    native ~21px height instead of coming out jagged/pixelated."""
    fw_i = max(1, int(round(fw)))
    SS = 4
    fw_s, bh_s = fw_i * SS, bar_h * SS

    base = Image.new("RGBA", (fw_s, bh_s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(base)
    for i in range(fw_s):
        t = i / max(fw_s - 1, 1)
        col = tuple(int(color_a[c] + (color_b[c] - color_a[c]) * t) for c in range(3))
        bd.line([(i, 0), (i, bh_s)], fill=(*col, 255))

    def _ribbon(wavelength_px, amplitude_px, phase, thickness_px, color, alpha):
        layer = Image.new("RGBA", (fw_s, bh_s), (0, 0, 0, 0))
        cy = bh_s / 2
        top, bottom = [], []
        for x in range(0, fw_s + SS, SS):
            yy = cy + (amplitude_px * SS) * math.sin(
                2 * math.pi * x / (wavelength_px * SS) + phase)
            top.append((x, yy - thickness_px * SS / 2))
            bottom.append((x, yy + thickness_px * SS / 2))
        if len(top) >= 2:
            ImageDraw.Draw(layer).polygon(top + bottom[::-1], fill=(*color, alpha))
        return layer

    # Wavelengths are fixed in real px (not scaled to fw) so the pattern
    # reads as one continuous flow whatever the current fill width is --
    # a short bar at 1% shows a small slice of it, a long bar at 99%
    # shows several cycles, rather than the same two cycles stretched or
    # squeezed to fit (which is what looks unnatural at very low/high %).
    bright = _ribbon(wavelength_px=82, amplitude_px=bar_h * 0.30, phase=0.5,
                     thickness_px=bar_h * 0.60, color=(255, 235, 255), alpha=58)
    shadow = _ribbon(wavelength_px=150, amplitude_px=bar_h * 0.24, phase=3.3,
                     thickness_px=bar_h * 0.55, color=(58, 12, 92), alpha=50)
    base.alpha_composite(shadow)
    base.alpha_composite(bright)

    fill = base.resize((fw_i, bar_h), Image.LANCZOS)
    mask = Image.new("L", (fw_i, bar_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, fw_i, bar_h), radius=radius, fill=255)
    fill.putalpha(ImageChops.multiply(fill.split()[3], mask))
    return fill


def _draw_xp_bar(img, bar_x, bar_y, bar_w, bar_h, frac):
    """Dark recessed pill track, purple gradient fill with soft diagonal
    internal lighting, and a bright glowing circular thumb at the current
    progress position -- matches the reference's visual character (dark
    track / gradient fill / wave lighting / glow thumb / pill geometry)
    using the existing Nero purple palette rather than the reference's own
    hex values. frac is the already-computed, real XP fraction (0..1) --
    no hardcoded percentage."""
    radius = bar_h // 2

    # Track: a vertical gradient (slightly darker at the top inner edge)
    # instead of a flat fill gives a recessed/inset look cheaply.
    track = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, 0))
    tg = ImageDraw.Draw(track)
    base = COLORS["xp_bar_bg"]
    top_shadow = tuple(max(0, c - 14) for c in base)
    for row in range(bar_h):
        t = row / max(bar_h - 1, 1)
        col = tuple(int(top_shadow[c] + (base[c] - top_shadow[c]) * t) for c in range(3))
        tg.line([(0, row), (bar_w, row)], fill=(*col, 255))
    track_mask = Image.new("L", (bar_w, bar_h), 0)
    ImageDraw.Draw(track_mask).rounded_rectangle((0, 0, bar_w, bar_h), radius=radius, fill=255)
    img.paste(track, (int(bar_x), int(bar_y)), track_mask)
    # Thin luminous outline on the empty track so its edge reads clearly
    # against the panel instead of blending into it.
    ImageDraw.Draw(img).rounded_rectangle(
        (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=radius,
        outline=(*COLORS["accent"], 90), width=1)

    fill_w = bar_w * frac if frac > 0 else 0

    if fill_w > 0:
        # Soft outer purple bloom around the filled portion only.
        _draw_glow_layer(img, lambda d: d.rounded_rectangle(
            (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h), radius=radius,
            fill=(160, 70, 225, 110)), blur=6)

        # Layered fluid/sine-wave fill (gradient + two flowing ribbons),
        # built and masked to the pill shape as one layer, then composited
        # in a single paste -- see _draw_xp_wave_fill.
        wave_fill = _draw_xp_wave_fill(fill_w, bar_h, radius,
                                       COLORS["xp_bar_fill_a"], COLORS["xp_bar_fill_b"])
        img.alpha_composite(wave_fill, (int(bar_x), int(bar_y)))

        # Glassy top sheen band on top of the wave lighting -- dimensional/
        # glass look rather than a flat gradient.
        if fill_w > 6:
            sheen = Image.new("RGBA", img.size, (0, 0, 0, 0))
            sd = ImageDraw.Draw(sheen)
            inset = max(2, bar_h // 5)
            sd.rounded_rectangle(
                (bar_x + inset, bar_y + 2, bar_x + fill_w - inset, bar_y + bar_h * 0.48),
                radius=(bar_h * 0.46) / 2, fill=(255, 255, 255, 45))
            sheen = sheen.filter(ImageFilter.GaussianBlur(1.5))
            img.alpha_composite(sheen)

    # Thumb: bright glowing circular marker at the current progress
    # position. Clamped so its glow never clips outside the pill's rounded
    # caps at either 0% or 100%.
    thumb_r = bar_h * 0.62
    inset_r = thumb_r * 0.55
    thumb_cx = max(bar_x + inset_r, min(bar_x + fill_w, bar_x + bar_w - inset_r))
    thumb_cy = bar_y + bar_h / 2

    # Layered glow, largest/softest first -- mirrors the reference's
    # stacked box-shadow (wide soft violet halo, tighter bright halo,
    # crisp white core).
    _draw_glow_layer(img, lambda d: d.ellipse(
        (thumb_cx - thumb_r * 2.1, thumb_cy - thumb_r * 2.1,
         thumb_cx + thumb_r * 2.1, thumb_cy + thumb_r * 2.1),
        fill=(190, 110, 255, 130)), blur=7)
    _draw_glow_layer(img, lambda d: d.ellipse(
        (thumb_cx - thumb_r * 1.3, thumb_cy - thumb_r * 1.3,
         thumb_cx + thumb_r * 1.3, thumb_cy + thumb_r * 1.3),
        fill=(255, 255, 255, 200)), blur=3)
    ImageDraw.Draw(img).ellipse(
        (thumb_cx - thumb_r, thumb_cy - thumb_r, thumb_cx + thumb_r, thumb_cy + thumb_r),
        fill=(255, 255, 255, 255))


def _draw_stat_cards(img, draw, data, coin_icon_im, diamond_icon_im, icons30):
    coins_cfg = data["currency"]["coins"]
    diamonds_cfg = data["currency"]["diamonds"]

    # Slot-based currency colors (NOT name-based -- both currencies are
    # rename-able by the guild owner, so the color must stay tied to which
    # slot a currency occupies, never to what its configured name happens to
    # say). Primary/first-configured slot ("coins") gets the icy pearl tint;
    # secondary slot ("diamonds") gets the warm coral shell tint. Distinct
    # from the purple/magenta palette used everywhere else on the card, and
    # deliberately not gold.
    coin_label_color = COLORS["currency_pearl"]
    diamond_label_color = COLORS["currency_shell"]

    cards = [
        (icons30.get("messages"), "MESSAGES", f"{data['messages_count']:,}",
         COLORS["text_muted"]),
        (icons30.get("voice"), "VOICE TIME", _fmt_minutes(data["voice_minutes"]),
         COLORS["text_muted"]),
        (coin_icon_im, coins_cfg["name"].upper(), f"{data['balance']:,}",
         coin_label_color),
        (diamond_icon_im, diamonds_cfg["name"].upper(), f"{data['diamonds']:,}",
         diamond_label_color),
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
            isz = _STAT_ICON_DISPLAY_SIZE
            ic = icon_im if icon_im.width == isz else icon_im.resize((isz, isz))
            # Icon box kept centered on the same vertical point the old
            # fixed 34px box was (top 23 + half of 34 = 40) so growing the
            # box grows outward from that same center rather than shifting
            # the row down.
            icon_x, icon_y = int(x + w / 2 - isz / 2), int(y + 40 - isz / 2)
            # reference icons carry a soft bloom
            gl = Image.new("RGBA", img.size, (0, 0, 0, 0))
            gl.paste(ic, (icon_x, icon_y), ic)
            gl.putalpha(gl.split()[3].point(lambda a: int(a * 0.75)))
            gl = gl.filter(ImageFilter.GaussianBlur(3))
            img.alpha_composite(gl)
            img.paste(ic, (icon_x, icon_y), ic)
        # Sized to fit the card width directly rather than drawn big and
        # squeezed -- keeps stroke weight even on both short values (138)
        # and long ones (34,725) instead of the squeeze warping wide values
        # more than narrow ones.
        vf, nat_w = _fit_numeral_font(draw, value, zilla_bold, w - 16, 32, min_size=16)
        # Nudged up from the original y+82 -- the value's ink sits directly
        # above its label (not below), and the two were touching with
        # effectively no gap between them. The label below stays put; only
        # the number moves, opening a small, subtle gap above the label
        # without disturbing the label's own position or the icon above.
        draw.text((x + w / 2 - nat_w / 2, y + 72), value, font=vf, fill=(215, 215, 222))
        # Configurable currency names can be Arabic (utils/currency.py puts
        # no restriction on what an admin types) -- MESSAGES/VOICE TIME/
        # GAMES WON are fixed English labels and never go through this,
        # only the two currency labels can be dynamic/Arabic.
        is_currency_label = i in (2, 3)
        if is_currency_label:
            # Amira Typo (Arabic-only glyph set) for Arabic names, Zilla
            # Slab Bold -- same family as the value numerals below it -- for
            # English/Latin names. Names stay fully dynamic; nothing here is
            # hardcoded to a specific currency name. Sized down (like the
            # numeral above) if the admin's name would otherwise overflow
            # the card at the default size.
            lf, label_text, label_is_arabic, lw_nat, label_mask = _fit_currency_label(
                draw, label, zilla_bold, 21, w - 16, 0.66)
        else:
            lf, label_text, label_is_arabic = outfit(21, "Medium"), label, False
            lw_nat = _text_size(draw, label_text, lf, tracking=2)[0]
            label_mask = None

        # Subtle glow behind the two currency labels only, using each slot's
        # paired glow tone (_draw_condensed's existing glow= param -- same
        # soft-bloom mechanism already used elsewhere on the card, e.g. the
        # row icons above, so this matches the card's visual language rather
        # than introducing a new effect). Kept gentle (low alpha, tight
        # blur) so it reads as a glow, not a colored halo that competes with
        # the purple palette.
        glow_kwargs = {}
        if is_currency_label:
            glow_color = (COLORS["currency_pearl_glow"] if i == 2
                         else COLORS["currency_shell_glow"])
            glow_kwargs = dict(glow=glow_color, blur=4, glow_alpha=0.35)

        if label_mask is not None:
            # raqm-unavailable Arabic fallback: label_text/lf are None (see
            # currency_name_style) -- draw the pre-shaped HarfBuzz/FreeType
            # mask directly instead of going through the painter(draw)
            # callback _draw_condensed expects (see _draw_condensed_mask's
            # docstring for why: ImageDraw.bitmap() doesn't antialias an
            # 'L'-mode mask correctly). _draw_condensed itself is untouched.
            _draw_condensed_mask(img, (x + w / 2 - lw_nat * 0.66 / 2, y + 116),
                                 label_mask, label_color, ratio=0.66, **glow_kwargs)
        else:
            def _lab_painter(d, _l=label_text, _f=lf, _c=label_color, _ar=label_is_arabic):
                if _ar:
                    # Per-glyph tracking would re-isolate the shaped
                    # ligatures -- draw the shaped run as a single string.
                    d.text((20, 20), _l, font=_f, fill=_c)
                else:
                    _draw_tracked_text(d, (20, 20), _l, _f, _c, tracking=2)
            _draw_condensed(img, (x + w / 2 - lw_nat * 0.66 / 2, y + 116), _lab_painter,
                            ratio=0.66, **glow_kwargs)


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
    text = _shape_arabic("عالمنا صغير، ولكن الإلهام فيه بلا حدود")
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
