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

# Footer "Mail Box" tag (lower-left). Every piece is supplied artwork and is
# used as-is -- nothing is redrawn, traced or typeset:
#   * frame    -- the supplied SVG (an Affinity export: one embedded raster
#                 plus an embedded luminance mask; parsed directly, see
#                 _load_mailbox_frame_svg)
#   * Nero     -- the supplied PNG, only ever scaled down
#   * wordmark -- the supplied MAIL BOX wordmark (pixel letters, gradient,
#                 outline and glow all baked into the artwork; 936x217). It is
#                 only ever scaled uniformly and positioned -- never typeset,
#                 recoloured or glowed again. (The supplied .svg is a thin
#                 wrapper around this very same PNG, so the PNG is the asset.)
MAILBOX_FRAME_SVG_PATH = _asset_path("mailbox_frame.svg", "Mailbox_frame.svg")
NERO_ICON_PNG_PATH = _asset_path("nero_icon.png", "Nero_icon_3d.png")
MAILBOX_WORDMARK_PNG_PATH = _asset_path("mailbox_wordmark.png", "mailbox_wordmark.png")

# Supplied sparkle artwork (source of truth) flanking the footer Arabic line.
# Used as-is: only trimmed to its own alpha bounding box and scaled down --
# never redrawn, recoloured or glowed. Footer position/size were fitted to the
# supplied reference crop (see _get_footer_sparkle / _draw_footer).
SPARKLE_EMOJI_PNG_PATH = _asset_path("sparkle_emoji.png", "sparkle_emoji.png")

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
# The supplied INACTIVE artwork contains one stray detached blob (a pale grey
# dot, ~88x101 source px, lower-left corner) that is not part of the diamond.
# Measured by connected-component analysis of the cropped art: the diamond
# spans y 59..1473, the stray dot y 1630..1731. This box (in cropped-source
# px) is cleared so the dot no longer shows beside every inactive pip. The
# crop box above is deliberately NOT changed, so every pip's size, centring
# and position stay exactly as they were. Set to None to disable.
INACTIVE_CRYSTAL_STRAY_BOX = (0, 1560, 140, 1765)
# Working resolution for the pips, as a multiple of the final slot height:
# 2 -> 64 px tall (50x64 / 46x64 px) for the 32 px slots, so the last step is
# an exact 2:1 average.
PRESTIGE_PIP_WORK_SCALE = 2

# Supplied XP-potion artwork (source of truth -- replaces the old hand-drawn
# flask). Content box measured off the asset itself (alpha>10 threshold) to
# trim the transparent margin around the round flask before it's resized
# into the TOTAL XP slot.
POTION_PNG_PATH = _asset_path("xp_potion.png", "xp_potion.png")
POTION_CONTENT_BOX = (139, 125, 796, 785)   # x0,y0,x1,y1 in source px

# Supplied stat-row icon artwork (source of truth -- not redrawn/regenerated).
# Replaces the hand-drawn line icons below for these three keys only; see
# _load_stat_icon_asset / LINE_ICON_BUILDERS.
STAT_ICON_MESSAGES_PNG_PATH = _asset_path("stat_icon_messages.png", "stat_icon_messages.png")
STAT_ICON_VOICE_PNG_PATH = _asset_path("stat_icon_voice.png", "stat_icon_voice.png")
STAT_ICON_GAMES_PNG_PATH = _asset_path("stat_icon_games.png", "stat_icon_games.png")

FONT_PATHS = {
    "cinzel": _asset_path(os.path.join("fonts", "Cinzel-Variable.ttf"), "Cinzel-Variable.ttf"),
    "zilla_bold": _asset_path(os.path.join("fonts", "ZillaSlab-Bold.ttf"), "ZillaSlab-Bold.ttf"),
    # XP numerals ONLY (XP PROGRESS value / "of needed" / TOTAL XP value).
    # Supplied Fortuner Heavy, used unmodified; not used anywhere else on the card.
    "fortuner": _asset_path(os.path.join("fonts", "FortunerHeavyPersonalUse.otf"),
                            "FortunerHeavyPersonalUse.otf"),
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
    "rank_number_a": (198, 161, 234),   # "RANK" label color -- sampled
    "rank_number_top": (232, 208, 250),    # #1/#3 gradient top -- brighter, more pop
    "rank_number_bottom": (122, 46, 208),  # #1/#3 gradient bottom -- deeper, more contrast
    "xp_value": (186, 120, 232),        # "1,450" purple      -- sampled
    "label_purple": (140, 100, 180),    # INVENTORY / TOTAL XP headers
    # "XP PROGRESS" / "TOTAL XP" labels only. Was #483A65 -- too dark on the
    # panel. Lighter lavender, still clearly below the XP numerals.
    "xp_panel_label": (178, 162, 220),
    "xp_panel_label_glow": (196, 140, 236),  # very subtle pink-lavender tint
    # TOTAL XP number only. Was #7A3D97; lifted so the heavy stencil face
    # holds together at its smaller size, still well under the hero value.
    "xp_total_value": (150, 88, 194),
    # "/ needed XP" readout: lavender-grey, quieter than the hero but
    # legible in the stencil face at ~24px.
    "xp_suffix": (154, 138, 190),
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


def fortuner(size):
    """Supplied Fortuner Heavy, used only for the XP numerals. Falls back to
    Zilla Slab Bold if the file is missing so a bad deploy can never take
    /rank down. Cap height is 0.639 em (digits and capitals share it), so
    the sizes used for the XP numerals are the previous Varsity sizes
    scaled by 0.700/0.639 -- that keeps the cap heights (and therefore the
    XP hierarchy) exactly what they were."""
    try:
        return ImageFont.truetype(FONT_PATHS["fortuner"], size)
    except OSError:
        log.warning("rank_card_renderer: Fortuner font missing, using Zilla")
        return zilla_bold(size)


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


def _format_compact_xp(n: int) -> str:
    """Display formatting ONLY -- the underlying xp_total the caller passes
    in is never touched, this just decides how to print it. Deterministic,
    single-value output (never a range): under 100k, plain comma grouping;
    100k-999,999 -> whole-number K (floor, not rounded, so 999,999 reads
    as "999K" rather than rounding up into "1000K"); 1,000,000+ -> M with
    one decimal place, and >=1B the same in B -- with a trailing ".0"
    stripped so exact millions/billions print as "1M"/"1B" rather than
    "1.0M"/"1.0B", matching round K's bare-integer look."""
    n = int(n)
    if n < 0:
        return f"-{_format_compact_xp(-n)}"
    if n < 100_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n // 1_000}K"
    if n < 1_000_000_000:
        val = f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{val}M"
    val = f"{n / 1_000_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{val}B"


def _format_compact_progress(n: int) -> str:
    """Display-only compact form for the XP PROGRESS pair ("23.4K" /
    "31.2K"). Separate from _format_compact_xp on purpose: TOTAL XP keeps
    its own established formatting. Under 1,000 prints plain; K / M / B
    use one decimal with a trailing ".0" stripped. Rounds to nearest and
    promotes when rounding reaches the next unit (999,960 -> "1M", not
    "1000K")."""
    n = int(n)
    if n < 0:
        return f"-{_format_compact_progress(-n)}"
    if n < 1_000:
        return str(n)
    for div, suffix, nxt in ((1_000, "K", 1_000_000),
                             (1_000_000, "M", 1_000_000_000),
                             (1_000_000_000, "B", None)):
        if nxt is None or n < nxt:
            val = round(n / div, 1)
            if nxt is not None and val * div >= nxt:
                continue
            txt = f"{val:.1f}".rstrip("0").rstrip(".")
            return f"{txt}{suffix}"
    return f"{n:,}"


def _draw_numeral_ss(img, x, baseline_y, text, font_fn, size, fill, ss=4):
    """Draws `text` at `size`pt (from `font_fn`) on its own ss-x
    supersampled local layer, anchored at (x, baseline_y), then
    downsamples once with LANCZOS -- same one-shared-canvas /
    one-final-downsample approach as the XP bar and _rounded_panel.

    Why this exists specifically for the TOTAL XP value: at the ~24-32px
    sizes this row renders at, FreeType's small-size autohinter can snap
    each glyph's stems/curves to the pixel grid a little differently
    glyph-to-glyph -- round-bowl digits (3/6/8/9/0) in particular can
    end up a fractional pixel higher/lower-looking than flat-top/bottom
    digits, even though every glyph in the string is mathematically
    drawn from the same anchor="ls" baseline (there's no per-glyph
    positioning logic here to "fix" -- one draw.text call, one font, one
    baseline). Rendering at 4x first means that hinting snap happens on
    a grid 4x finer, so the rounding error shrinks to a quarter-pixel at
    final size instead of a whole one, and the whole string reads as one
    optically even line instead of individual digits looking adrift.

    Returns the drawn text's tight bbox in `img`'s coordinate space
    (left, top, right, bottom), matching draw.textbbox(..., anchor="ls")
    for the same call, so callers doing layout math (e.g. centering the
    potion icon on this row) don't need to change."""
    f_big = font_fn(max(1, round(size * ss)))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bx0, by0, bx1, by1 = probe.textbbox((0, 0), text, font=f_big, anchor="ls")
    pad = ss * 4
    lw = max(1, (bx1 - bx0) + pad * 2)
    lh = max(1, (by1 - by0) + pad * 2)
    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    origin_x, origin_y = pad - bx0, pad - by0
    ImageDraw.Draw(layer).text((origin_x, origin_y), text, font=f_big, fill=fill,
                               anchor="ls")
    sw, sh = max(1, round(lw / ss)), max(1, round(lh / ss))
    layer = layer.resize((sw, sh), Image.LANCZOS)
    paste_x = x - origin_x / ss
    paste_y = baseline_y - origin_y / ss
    img.alpha_composite(layer, (round(paste_x), round(paste_y)))
    return (paste_x + pad / ss, paste_y + pad / ss,
            paste_x + (lw - pad) / ss, paste_y + (lh - pad) / ss)


def _ss_words_width(draw, words, font, gap):
    """Total width of `words` set with an explicit `gap` between them
    (Varsity's own word space is too wide for a compact readout)."""
    return sum(draw.textbbox((0, 0), w, font=font)[2] for w in words) \
        + gap * (len(words) - 1)


def _draw_ss_words(img, x, baseline_y, words, font_fn, size, fill, gap, ss=4):
    """Draws each word via _draw_numeral_ss on one shared baseline with an
    explicit gap instead of the font's word-space. Returns the right edge."""
    cursor = x
    right = x
    for i, wd in enumerate(words):
        bb = _draw_numeral_ss(img, cursor, baseline_y, wd, font_fn, size, fill, ss=ss)
        right = bb[2]
        cursor = right + gap
    return right


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


def _bigger_font(font, scale):
    """Returns a copy of `font` scaled up by `scale`x for supersampled
    rendering. font_variant(size=...) alone is NOT enough for a variable
    font (Outfit/Cinzel here): PIL resets a variable font to its default
    instance when handed a new size, silently discarding whatever weight
    axis was set on the original (e.g. an ExtraBold instance would come
    back as Thin -- this was a real bug caught by visual QA: the RANK
    number rendered as hollow/thin outlines instead of its actual bold
    weight). Reapplying the original's own style name after resizing
    fixes it; wrapped in try/except since static (non-variable) fonts
    like zilla_bold/stencil have no variation axis to reapply and should
    just pass through unchanged."""
    big = font.font_variant(size=max(1, round(font.size * scale)))
    try:
        style = font.getname()[1]
        if style:
            big.set_variation_by_name(style)
    except Exception:
        pass
    return big


def _draw_stencil_number(img, draw, xy, text, font, fill, glow=None, ss=4):
    """Big slab numerals (Level number, badge number) drawn with the repo's
    actual STENCIL.TTF -- the font's own cut notches provide the stencil
    look, so this just draws glyphs (with an optional soft bloom behind
    them), no manual notch-carving. `font` is expected to already be a
    stencil() instance; kept as a parameter (rather than hardcoded) so
    callers control size.

    Rendered at 4x supersample (via font.font_variant, same face/size
    scaled up) then LANCZOS-downsampled once, same approach used for the
    XP bar / rounded panels / TOTAL XP value elsewhere in this file. At
    native size FreeType's small-size hinting can snap individual glyphs'
    stems/curves a fractional pixel differently from one another (round
    digits like 0/3/6/8/9 especially), which is what reads as digits not
    quite sharing one clean line even though they're all one draw call on
    one baseline. `xy` keeps the exact same meaning (draw.text's own
    top-left convention) as the previous native-resolution version, so
    call sites are unaffected."""
    x, y = xy
    f_big = _bigger_font(font, ss)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bx0, by0, bx1, by1 = probe.textbbox((0, 0), text, font=f_big)
    if bx1 <= bx0 or by1 <= by0:
        return
    pad = ss * (30 if glow else 6)
    lw, lh = (bx1 - bx0) + pad * 2, (by1 - by0) + pad * 2
    origin_x, origin_y = pad - bx0, pad - by0
    paste_x, paste_y = round(x - origin_x / ss), round(y - origin_y / ss)

    if glow:
        gl = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
        ImageDraw.Draw(gl).text((origin_x, origin_y), text, font=f_big, fill=(*glow, 130))
        gl = gl.resize((max(1, round(lw / ss)), max(1, round(lh / ss))), Image.LANCZOS)
        gl = gl.filter(ImageFilter.GaussianBlur(8))
        img.alpha_composite(gl, (paste_x, paste_y))

    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((origin_x, origin_y), text, font=f_big, fill=(*fill, 255))
    small = layer.resize((max(1, round(lw / ss)), max(1, round(lh / ss))), Image.LANCZOS)
    img.alpha_composite(small, (paste_x, paste_y))


def _draw_gradient_text(img, draw, xy, text, font, color_top, color_bottom,
                        tracking=0, glow=None, ratio=1.0, ss=4):
    """Vertical two-tone fill (the reference's #3 brightens toward the top)
    plus an optional soft bloom behind it; ratio<1 reproduces the
    reference's condensed glyph proportions.

    Rendered at 4x supersample (font.font_variant of the same face/size)
    then downsampled once, for the same reason as _draw_stencil_number --
    this draws the RANK number ("#1"/"#3"/etc.), and native-resolution
    FreeType hinting was the actual source of digits looking like they
    don't share a line, not any per-glyph positioning in this function.

    Preserves the exact positioning behavior of the previous version bit
    for bit (just antialiased better): text is measured/cropped to its
    own tight ink bbox and that crop is pasted at `xy` directly, the same
    order of operations the original did at native res -- callers (the
    one call site's x+28/y+40 offsets) are tuned against that, so this
    keeps it rather than "fixing" it into a different position."""
    x, y = xy
    f_big = _bigger_font(font, ss)
    track_big = tracking * ss
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    rough_w = sum(probe.textbbox((0, 0), ch, font=f_big)[2] + track_big for ch in text)
    pad = ss * 40
    scratch_w = max(10, int(rough_w) + pad * 2 + 200)
    scratch_h = f_big.size * 3 + pad * 2
    origin_x, origin_y = pad, pad

    tmp = Image.new("RGBA", (scratch_w, scratch_h), (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    _draw_tracked_text(td, (origin_x, origin_y), text, f_big, (255, 255, 255, 255),
                       tracking=track_big)
    bb = tmp.getbbox()
    if not bb:
        return
    grad = Image.new("RGBA", (scratch_w, scratch_h), (0, 0, 0, 0))
    gy0, gy1 = bb[1], bb[3]
    for yy in range(gy0, gy1 + 1):
        t = (yy - gy0) / max(gy1 - gy0, 1)
        col = tuple(int(color_top[c] + (color_bottom[c] - color_top[c]) * t) for c in range(3))
        ImageDraw.Draw(grad).line([(bb[0], yy), (bb[2], yy)], fill=(*col, 255))
    grad.putalpha(tmp.split()[3])
    crop_big = grad.crop(bb)
    final_w = max(1, round(crop_big.width / ss * ratio))
    final_h = max(1, round(crop_big.height / ss))
    crop = crop_big.resize((final_w, final_h), Image.LANCZOS)

    if glow:
        gl = Image.new("RGBA", crop.size, (0, 0, 0, 0))
        gl.paste(crop, (0, 0), crop)
        gl.putalpha(gl.split()[3].point(lambda a: int(a * 0.45)))
        gl = gl.filter(ImageFilter.GaussianBlur(5))
        img.alpha_composite(gl, (x, y))
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

    # Drawn on its own supersampled layer (4x) then downsampled once,
    # rather than straight onto the final-resolution `draw` -- every
    # panel on the card (RANK, XP/TOTAL XP, INVENTORY, the stat cards)
    # shares this one function, and PIL's rounded_rectangle has no
    # antialiasing of its own: at final resolution a ~16-20px corner
    # radius only has a handful of pixels to place its curve across, and
    # comes out visibly stair-stepped. Same fix as the XP bar's own
    # rendering pass -- one shared local canvas, one final LANCZOS
    # downsample.
    x0, y0, x1, y1 = box
    SS = 4
    pad = width + 2  # keeps the outline stroke from being clipped by the layer edge
    w, h = (x1 - x0), (y1 - y0)
    lw, lh = int(round(w + pad * 2)), int(round(h + pad * 2))
    layer = Image.new("RGBA", (max(1, lw * SS), max(1, lh * SS)), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (pad * SS, pad * SS, (w + pad) * SS, (h + pad) * SS), radius=radius * SS,
        fill=fill, outline=outline, width=width * SS)
    layer = layer.resize((lw, lh), Image.LANCZOS)
    img.alpha_composite(layer, (int(round(x0 - pad)), int(round(y0 - pad))))


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

# Display box for ALL five stat icons (the three supplied assets and the two
# currency icons are each loaded straight from source at this size, so there
# is no bitmap up-scaling). 47 = the previous 40 + 17.5%.
_STAT_ICON_DISPLAY_SIZE = 47
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
    potion_im = await _load_potion_icon()

    _draw_name_block(img, draw, data, line_icons_16)
    _draw_rank_prestige_panel(img, draw, data, line_icons_16,
                              active_crystal_im, inactive_crystal_im)
    _draw_level_xp_panels(img, draw, data, potion_im)
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


async def _load_potion_icon() -> Image.Image | None:
    """The real XP-potion artwork, trimmed to its measured content box. On
    any failure this returns None and the caller falls back to the old
    hand-drawn flask (_draw_potion_icon) -- same missing-asset pattern as
    mailbox/avatar-ring/crystals above."""
    if not os.path.isfile(POTION_PNG_PATH):
        log.warning("rank_card: xp potion asset not found at %s -- falling back to the "
                    "hand-drawn flask.", POTION_PNG_PATH)
        return None
    try:
        im = Image.open(POTION_PNG_PATH)
        im.load()
        return im.convert("RGBA").crop(POTION_CONTENT_BOX)
    except Exception as e:
        log.warning("rank_card: failed to load xp potion asset: %s", e)
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
    # Gradient-rim badge with its own soft magenta glow -- an echo of the
    # avatar ring it sits on, instead of the flat single-tone outline this
    # replaces (see _draw_level_badge_ring).
    _draw_level_badge_ring(img, bx, by, badge_r)
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


def _draw_level_badge_ring(img, cx, cy, r, ss=4):
    """The level badge as a small echo of the avatar ring it sits on top
    of, instead of a flat single-color outline: a soft magenta glow behind
    it (same family of color as the ring's own inner glow, sampled off
    assets/rank_card/avatar_ring.png) and a gradient stroke sweeping
    through the ring's pink/violet range rather than one flat purple.
    Built on its own supersampled (4x) local canvas and downsampled once,
    same approach used for the XP bar / rounded panels elsewhere in this
    file, so the circle's curve is smooth rather than stepped at this
    small a radius. Fill stays dark/flat so the level number inside stays
    legible against it -- only the rim picks up the ring's vibrancy."""
    margin = r * 0.45
    W = H = max(1, int(round((r + margin) * 2 * ss)))
    cxl, cyl = W / 2, H / 2
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (cxl - r * 1.3 * ss, cyl - r * 1.3 * ss, cxl + r * 1.3 * ss, cyl + r * 1.3 * ss),
        fill=(235, 70, 200, 130))
    glow = glow.filter(ImageFilter.GaussianBlur(5 * ss))
    canvas.alpha_composite(glow)

    ImageDraw.Draw(canvas).ellipse(
        (cxl - r * ss, cyl - r * ss, cxl + r * ss, cyl + r * ss), fill=(15, 8, 23, 240))

    border_w = 3 * ss
    stops = [(0.0, (235, 150, 235)), (0.5, (190, 90, 225)), (1.0, (120, 40, 170))]
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for xcol in range(W):
        col = _lerp_stops(stops, xcol / max(W - 1, 1))
        gd.line([(xcol, 0), (xcol, H)], fill=(*col, 255))
    stroke_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(stroke_mask).ellipse(
        (cxl - r * ss, cyl - r * ss, cxl + r * ss, cyl + r * ss), outline=255, width=border_w)
    grad.putalpha(stroke_mask)
    canvas.alpha_composite(grad)

    small = canvas.resize((max(1, round(W / ss)), max(1, round(H / ss))), Image.LANCZOS)
    img.alpha_composite(small, (round(cx - small.width / 2), round(cy - small.height / 2)))


def _aa_rounded_rect(img, box, radius, fill=None, outline=None, width=1, ss=8):
    """Antialiased drop-in for ImageDraw.rounded_rectangle on the final
    canvas (same inclusive-box geometry, radius and outline width). The
    plain call is aliased, so a pill's curved ends stair-step. Drawn on a
    local supersampled layer and reduced with an exact-coverage BOX filter
    (no ringing, no blur of the interior). Colours are used at full opacity:
    the card is flattened with convert("RGB"), which discards alpha, so the
    pixels that have always been output are exactly these RGB values --
    this only adds smooth coverage at the edge."""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    pad = 2
    w, h = (x1 - x0 + 1), (y1 - y0 + 1)
    opaque = lambda c: None if c is None else (c[0], c[1], c[2], 255)
    layer = Image.new("RGBA", ((w + 2 * pad) * ss, (h + 2 * pad) * ss), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (pad * ss, pad * ss, (w + pad) * ss - 1, (h + pad) * ss - 1),
        radius=radius * ss, fill=opaque(fill), outline=opaque(outline), width=width * ss)
    layer = layer.resize((w + 2 * pad, h + 2 * pad), Image.BOX)
    img.alpha_composite(layer, (x0 - pad, y0 - pad))


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
        _aa_rounded_rect(img, (px, py, px + pw, py + ph), ph // 2,
                         fill=(10, 5, 26, 230), outline=(90, 60, 140, 70), width=1)
        label = title["item_name"].upper()
        tf = outfit(17, "SemiBold")
        tl_w = _text_size(draw, label, tf, tracking=3)[0]
        icon_w = 16
        inner_x0 = px + 15
        inner_x1 = min(px + pw - 6, inner_x0 + 10 + icon_w + 6 + tl_w + 12)
        _aa_rounded_rect(img, (inner_x0, py + 7, inner_x1, py + ph - 7),
                         (ph - 14) // 2, fill=(26, 20, 40, 210))
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


_PRESTIGE_PIP_CACHE = {}


def _prestige_pip_sprite(kind, src, cw, ch):
    """Final-size pip sprite from the supplied high-resolution crystal art.

    Two exact area-average (BOX) reductions: source -> a 64px-tall working
    render -> the final slot size. The previous single LANCZOS step from
    ~3000px art overshot edges by up to ~22/255 and was 5-8% over-sharpened
    relative to the true geometry (uneven, stair-stepped-looking outlines on
    the thin inactive diamonds); area averaging reproduces the art's actual
    edge coverage with no ringing and no blur. Pillow premultiplies alpha for
    RGBA resizes, so the glow falloff keeps its colour. The art is static, so
    the result is cached per (kind, size)."""
    key = (kind, cw, ch)
    hit = _PRESTIGE_PIP_CACHE.get(key)
    if hit is not None:
        return hit
    if kind == "inactive" and INACTIVE_CRYSTAL_STRAY_BOX:
        src = src.copy()
        a = src.getchannel("A")
        ImageDraw.Draw(a).rectangle(INACTIVE_CRYSTAL_STRAY_BOX, fill=0)
        src.putalpha(a)
    k = PRESTIGE_PIP_WORK_SCALE
    work = src.resize((cw * k, ch * k), Image.BOX)
    out = work.resize((cw, ch), Image.BOX)
    _PRESTIGE_PIP_CACHE[key] = out
    return out


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
    # y nudged from y+40 to y+52: _draw_gradient_text crops its render to
    # the glyphs' own tight ink bbox and pastes that crop directly at the
    # xy given here (see that function's docstring) -- which means this y
    # is effectively where the numeral's ink TOP lands, not a normal
    # top-anchored draw.text position that would still carry the font's
    # own ascent "leading" above the ink. At y+40 that put the "1"/"3"'s
    # ink almost flush against the "RANK" label above it (~1px gap,
    # visually touching). +12px restores real breathing room between the
    # label and the number.
    _draw_gradient_text(img, draw, (x + 28, y + 52), rank_txt,
                        rank_font, COLORS["rank_number_top"],
                        COLORS["rank_number_bottom"], glow=(150, 80, 230))

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
            crystal = _prestige_pip_sprite("active" if filled else "inactive", src, cw, crystal_h)
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


def _draw_level_xp_panels(img, draw, data, potion_im=None):
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

    # Both column headers share one label row -- previously "XP PROGRESS"
    # sat 4px lower than "TOTAL XP" (y+29 vs y+25), a small but visible
    # misalignment between the two columns' top edges.
    # Shared row grid for BOTH columns (panel-relative): label -> hero
    # value on one baseline -> bar -> supporting line. Rebalanced so top
    # and bottom padding match (the old layout ended ~3px above the
    # panel's bottom edge).
    label_y = y + 15
    hero_baseline = y + 71
    label_font = outfit(19, "SemiBold")
    lbl_glow = (*COLORS["xp_panel_label_glow"], 46)

    def _label(lx, text, font):
        # Faint tinted bloom first, crisp lavender text on top.
        _draw_glow_layer(img, lambda d: _draw_tracked_text(
            d, (lx, label_y), text, font, lbl_glow, tracking=2), blur=3)
        _draw_tracked_text(draw, (lx, label_y), text, font,
                           COLORS["xp_panel_label"], tracking=2)

    _label(x + 14, "XP PROGRESS", label_font)
    div_x = x + LAYOUT["xp_divider_x"]
    cur, needed = data["xp_current"], max(data["xp_needed"], 1)

    # Hierarchy (unchanged): current XP is the hero; "/ needed XP" is the
    # same face but much smaller and muted, on the hero's baseline. One
    # family (Fortuner) for the whole numeric treatment. Sizes keep the cap
    # heights of the previous treatment (hero ~29px, suffix ~15px); only the
    # face changed. Display formatting only: cur / needed / frac below are
    # untouched.
    cur_txt = _format_compact_progress(cur)
    suffix_words = ["/", _format_compact_progress(needed), "XP"]
    HERO_SIZE, SUF_SIZE, SUF_GAP, HERO_GAP = 45, 24, 7, 14
    suffix_font = fortuner(SUF_SIZE)
    suffix_w = _ss_words_width(draw, suffix_words, suffix_font, SUF_GAP)
    available = (div_x - 10) - (x + 14) - HERO_GAP - suffix_w
    vf, cur_w = _fit_numeral_font(draw, cur_txt, fortuner, max(available, 40), HERO_SIZE,
                                  min_size=26)
    _draw_glow_layer(img, lambda d: d.text((x + 14, hero_baseline), cur_txt, font=vf,
                                            fill=(150, 70, 210, 140), anchor="ls"),
                     blur=3)
    hero_bb = _draw_numeral_ss(img, x + 14, hero_baseline, cur_txt, fortuner, vf.size,
                               COLORS["xp_value"])
    _draw_ss_words(img, hero_bb[2] + HERO_GAP, hero_baseline, suffix_words, fortuner,
                   SUF_SIZE, COLORS["xp_suffix"], SUF_GAP)

    # Ends at the numerals' baseline, leaving a small gap above the XP bar
    # (bar top = y + 84) instead of running through/past it. Top edge, x,
    # width and colour unchanged.
    draw.line((div_x, y + 14, div_x, y + 72), fill=(*COLORS["accent"], 30), width=1)

    # Bar: unchanged (size/shape/design and the same _draw_xp_bar call).
    bar_x, bar_y, bar_w, bar_h = x + 8, y + 84, w - 20, 21
    frac = min(cur / needed, 1.0)
    _draw_xp_bar(img, bar_x, bar_y, bar_w, bar_h, frac)
    draw.text((bar_x + 6, bar_y + bar_h + 8), f"{frac * 100:.1f}% to next level",
              font=outfit(17), fill=COLORS["text_muted"])

    tx = div_x + 22
    _label(tx, "TOTAL XP", outfit(20, "SemiBold"))

    # Compact display formatting only -- data["xp_total"] itself is never
    # touched, see _format_compact_xp. Keeps "23,398" as-is today but
    # keeps a future 1,200,000 from ever reaching the raw comma-grouped
    # width this row was actually breaking on.
    total_txt = _format_compact_xp(data["xp_total"])
    total_x = div_x + 52
    total_available = (x + w) - total_x - 12

    # Baseline-anchored: a fixed BASELINE position means the glyphs sit on
    # the same line regardless of which font size _fit_numeral_font ends
    # up choosing (a top-anchored draw would put the text's nominal box
    # top at a fixed y, but the glyphs' actual distance below that top
    # depends on the font's internal leading at whatever size got chosen
    # -- smaller size, smaller leading -- so the row would visibly rise
    # or fall with value length). Drawn via _draw_numeral_ss (supersampled
    # + one downsample) rather than a direct draw.text: every glyph in
    # "582,520" is already produced by one draw.text call on one shared
    # baseline (anchor="ls") -- there's no per-glyph position to "fix" --
    # but FreeType's small-size autohinter can still snap round-bowl
    # digits (3/6/8/9/0) a fractional pixel differently than flat-edged
    # ones at native ~30px rendering, which is what reads as digits not
    # quite sharing the line. Rendering at 4x first shrinks that snapping
    # error to a quarter-pixel before the one final downsample.
    #
    # Max size trimmed from 38 to 30 -- large enough to stay the clear
    # focal point of its column (matches TOTAL XP's own scale relative to
    # the label/icon beside it), without outweighing the panel the way a
    # 38-42px value did.
    total_baseline_y = hero_baseline
    tf2, _tw = _fit_numeral_font(draw, total_txt, fortuner, max(total_available, 40), 36,
                                 min_size=20)
    total_bbox = _draw_numeral_ss(img, total_x, total_baseline_y, total_txt, fortuner,
                                  tf2.size, COLORS["xp_total_value"])

    # Potion icon: the supplied artwork (potion_im, pre-trimmed to its
    # content box by _load_potion_icon), scaled to a size that reads at
    # the same visual weight as the old hand-drawn flask and centered on
    # the TOTAL XP value's own vertical center -- "sits beside the
    # number" instead of being independently positioned the way the old
    # fixed (div_x+19, y+42) offset was. Falls back to the old hand-drawn
    # flask if the asset is missing, same as every other optional asset
    # in this renderer.
    icon_cy = (total_bbox[1] + total_bbox[3]) / 2
    if potion_im is not None:
        # 27px matches the old hand-drawn flask's own `size` -- "preserve
        # the intended size of the existing slot" -- scaled by LANCZOS for
        # a clean downsize from the much larger source asset.
        target_h = 27
        aspect = potion_im.width / potion_im.height
        target_w = max(1, round(target_h * aspect))
        potion = potion_im.resize((target_w, target_h), Image.LANCZOS)
    else:
        potion = _draw_potion_icon(27)
    icon_x = div_x + 15
    icon_y = icon_cy - potion.height / 2
    img.alpha_composite(potion, (round(icon_x), round(icon_y)))


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


def _lerp_stops(stops, t):
    """stops: [(pos0..1, (r,g,b)), ...] sorted by pos. Piecewise-linear
    interpolate a color at t. Used for the outer frame's horizontal
    white -> lavender -> violet/pink sweep."""
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p0 <= t <= p1:
            local_t = (t - p0) / max(p1 - p0, 1e-6)
            return tuple(int(c0[c] + (c1[c] - c0[c]) * local_t) for c in range(3))
    return stops[-1][1]


def _draw_xp_wave_fill(canvas, ox, oy, fw, track_h, radius, color_a, color_b):
    """Paints the filled portion of the XP bar's INNER track directly onto
    `canvas` at (ox, oy): a horizontal purple gradient with three
    translucent, broad, low-frequency wave ribbons on top -- a darker
    violet wave, a brighter lavender wave (different wavelength/phase so
    they overlap asymmetrically, not identical sine copies), and a softer
    translucent highlight riding near the top -- so the fill reads as a
    few large flowing liquid curves rather than tight repeating water
    texture.

    Takes no resolution decisions of its own: the caller (_draw_xp_bar)
    already supersamples the whole bar before calling this, so fw/track_h
    here are already at that larger scale, and every coordinate this
    function draws is antialiased for free by the caller's single final
    downsample -- this only draws, it never resizes."""
    fw_i = max(1, int(round(fw)))
    th_i = max(1, int(round(track_h)))

    base = Image.new("RGBA", (fw_i, th_i), (0, 0, 0, 0))
    bd = ImageDraw.Draw(base)
    for i in range(fw_i):
        t = i / max(fw_i - 1, 1)
        col = tuple(int(color_a[c] + (color_b[c] - color_a[c]) * t) for c in range(3))
        bd.line([(i, 0), (i, th_i)], fill=(*col, 255))

    def _ribbon(wavelength_px, amplitude_px, phase, thickness_px, color, alpha,
               y_bias_px=0.0):
        layer = Image.new("RGBA", (fw_i, th_i), (0, 0, 0, 0))
        cy = th_i / 2 + y_bias_px
        top, bottom = [], []
        for x in range(fw_i):
            yy = cy + amplitude_px * math.sin(2 * math.pi * x / wavelength_px + phase)
            top.append((x, yy - thickness_px / 2))
            bottom.append((x, yy + thickness_px / 2))
        if len(top) >= 2:
            ImageDraw.Draw(layer).polygon(top + bottom[::-1], fill=(*color, alpha))
        return layer

    # Wavelength/amplitude/thickness are all given in the SAME (already
    # supersampled) px space as fw/track_h, so they stay proportional to
    # the bar regardless of the supersampling factor the caller picked.
    deep = _ribbon(wavelength_px=track_h * 11, amplitude_px=track_h * 0.30, phase=0.6,
                   thickness_px=track_h * 0.68, color=(58, 14, 96), alpha=70)
    bright = _ribbon(wavelength_px=track_h * 17, amplitude_px=track_h * 0.24, phase=2.9,
                     thickness_px=track_h * 0.58, color=(216, 176, 255), alpha=60)
    sheen_wave = _ribbon(wavelength_px=track_h * 13, amplitude_px=track_h * 0.14, phase=5.1,
                         thickness_px=track_h * 0.34, color=(255, 255, 255), alpha=35,
                         y_bias_px=-track_h * 0.20)
    base.alpha_composite(deep)
    base.alpha_composite(bright)
    base.alpha_composite(sheen_wave)

    mask = Image.new("L", (fw_i, th_i), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, fw_i, th_i), radius=radius, fill=255)
    base.putalpha(ImageChops.multiply(base.split()[3], mask))
    canvas.alpha_composite(base, (int(round(ox)), int(round(oy))))


def _draw_xp_bar(img, bar_x, bar_y, bar_w, bar_h, frac):
    """Two nested pill frames, matching the reference's "glass capsule"
    construction rather than one rounded rectangle:

      outer glow -> outer luminous gradient border -> small recessed gap
      -> inner track rim -> dark inner track -> wave fill (clipped to the
      INNER track only, empty track stays visibly dark after the fill) ->
      leading orb, sized/positioned to stay inside the inner track.

    Rendering approach: every one of those pieces -- both pill masks, the
    border strokes, the wave fill, every glow, the orb -- is drawn on ONE
    local canvas supersampled at SS=8x, using SS-scaled coordinates
    throughout, and NOTHING is resized until the single final LANCZOS
    downsample back to (bar_w, bar_h) at the very end. That single
    downsample is the only anti-aliasing step. Earlier passes drew the
    outline/mask/orb geometry straight at the bar's native ~21px-tall
    resolution (and, for the wave fill, supersampled only that one piece
    in isolation before pasting it back into a native-res composite) --
    at that size, PIL's rounded_rectangle/ellipse have only a handful of
    pixels to place a curve across and produce visibly stair-stepped
    edges, and compositing a supersampled piece into an otherwise
    native-res frame doesn't fix the frame's own hard edges. Building the
    entire bar in one oversized space first (so every curve has 8x the
    pixels to fall across) and downsampling exactly once is what actually
    removes the stair-stepping instead of blurring over it.

    frac is the already-computed, real XP fraction (0..1) -- no
    hardcoded percentage."""
    SS = 8
    bar_w, bar_h = int(bar_w), int(bar_h)
    W, H = bar_w * SS, bar_h * SS

    outer_radius = (bar_h // 2) * SS
    # Gap between the outer frame and the inner track -- the reference's
    # "small visible depth separation" between the two borders. Computed
    # at native scale first (so the proportions match the earlier pass
    # exactly) then scaled up.
    pad_native = max(2, min(3, bar_h // 2 - 3))
    pad = pad_native * SS
    inner_w = W - pad * 2
    inner_h = H - pad * 2
    inner_radius = max(1, inner_h // 2)
    inner_x = pad
    inner_y = pad

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # Soft outer glow behind the frame -- restrained, a blurred stroke
    # rather than a filled halo, so it reads as glass edge-light.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(
        (0, 0, W - 1, H - 1), radius=outer_radius, outline=(200, 175, 235, 50),
        width=4 * SS)
    glow = glow.filter(ImageFilter.GaussianBlur(4 * SS))
    canvas.alpha_composite(glow)

    # Recessed ring between the two frames: fill the OUTER footprint with
    # a slightly darker tone first, so the inner track (painted next,
    # inset by `pad`) reads as sitting inward from it.
    outer_fill = tuple(max(0, c - 10) for c in COLORS["xp_bar_bg"])
    outer_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(outer_mask).rounded_rectangle((0, 0, W, H), radius=outer_radius, fill=255)
    outer_plate = Image.new("RGBA", (W, H), (*outer_fill, 255))
    canvas.paste(outer_plate, (0, 0), outer_mask)

    # Inner track: recessed vertical gradient, confined to the inset
    # inner footprint.
    track = Image.new("RGBA", (inner_w, inner_h), (0, 0, 0, 0))
    tg = ImageDraw.Draw(track)
    base_col = COLORS["xp_bar_bg"]
    top_shadow = tuple(max(0, c - 14) for c in base_col)
    for row in range(inner_h):
        t = row / max(inner_h - 1, 1)
        col = tuple(int(top_shadow[c] + (base_col[c] - top_shadow[c]) * t) for c in range(3))
        tg.line([(0, row), (inner_w, row)], fill=(*col, 255))
    track_mask = Image.new("L", (inner_w, inner_h), 0)
    ImageDraw.Draw(track_mask).rounded_rectangle(
        (0, 0, inner_w, inner_h), radius=inner_radius, fill=255)
    canvas.paste(track, (inner_x, inner_y), track_mask)
    # Thin inner rim -- subtler than the outer frame, just enough to
    # separate the track from its recess.
    ImageDraw.Draw(canvas).rounded_rectangle(
        (inner_x, inner_y, inner_x + inner_w, inner_y + inner_h), radius=inner_radius,
        outline=(*COLORS["accent"], 55), width=max(1, SS // 4))

    fill_w = inner_w * frac if frac > 0 else 0

    if fill_w > 0:
        # Soft outer purple bloom around the filled portion only, kept
        # inside the outer frame's footprint.
        bloom = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(bloom).rounded_rectangle(
            (inner_x, inner_y, inner_x + fill_w, inner_y + inner_h), radius=inner_radius,
            fill=(160, 70, 225, 95))
        bloom = bloom.filter(ImageFilter.GaussianBlur(5 * SS))
        canvas.alpha_composite(bloom)

        # Layered fluid/sine-wave fill (gradient + broad flowing ribbons),
        # painted straight onto the shared canvas -- see _draw_xp_wave_fill.
        _draw_xp_wave_fill(canvas, inner_x, inner_y, fill_w, inner_h, inner_radius,
                           COLORS["xp_bar_fill_a"], COLORS["xp_bar_fill_b"])

        # Glassy top sheen band on top of the wave lighting -- dimensional/
        # glass look rather than a flat gradient.
        if fill_w > 6 * SS:
            sheen = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sd = ImageDraw.Draw(sheen)
            s_inset = max(2, inner_h // 5)
            sd.rounded_rectangle(
                (inner_x + s_inset, inner_y + SS, inner_x + fill_w - s_inset,
                 inner_y + inner_h * 0.48),
                radius=(inner_h * 0.46) / 2, fill=(255, 255, 255, 40))
            sheen = sheen.filter(ImageFilter.GaussianBlur(1.3 * SS))
            canvas.alpha_composite(sheen)

    # Outer luminous frame -- drawn after the fill so it always sits on
    # top, framing the whole capsule. Gradient sweep: white/silver at the
    # start, soft lavender through the middle, violet -> pink toward the
    # end. Painted as a horizontal color sweep masked down to just the
    # pill's outline.
    border_w = 2 * SS
    stops = [(0.0, (248, 248, 255)), (0.45, (206, 182, 236)), (1.0, (214, 116, 196))]
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for x in range(W):
        col = _lerp_stops(stops, x / max(W - 1, 1))
        gd.line([(x, 0), (x, H)], fill=(*col, 255))
    stroke_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(stroke_mask).rounded_rectangle(
        (0, 0, W - 1, H - 1), radius=outer_radius, outline=255, width=border_w)
    grad.putalpha(stroke_mask)
    canvas.alpha_composite(grad)

    # Thumb: bright glowing circular marker at the current progress
    # position, sized and clamped to stay INSIDE the inner track (its
    # soft bloom may extend past the track, the solid orb itself does
    # not).
    thumb_r = inner_h * 0.40
    inset_r = thumb_r * 0.7
    thumb_cx = max(inner_x + inset_r, min(inner_x + fill_w, inner_x + inner_w - inset_r))
    thumb_cy = inner_y + inner_h / 2

    # Layered glow, largest/softest first -- mirrors the reference's
    # stacked box-shadow (wide soft violet halo, tighter bright halo,
    # crisp white core), all still on the shared supersampled canvas so
    # the final circle comes out smoothly antialiased rather than jagged.
    outer_halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(outer_halo).ellipse(
        (thumb_cx - thumb_r * 2.3, thumb_cy - thumb_r * 2.3,
         thumb_cx + thumb_r * 2.3, thumb_cy + thumb_r * 2.3),
        fill=(190, 110, 255, 120))
    outer_halo = outer_halo.filter(ImageFilter.GaussianBlur(6 * SS))
    canvas.alpha_composite(outer_halo)

    inner_halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(inner_halo).ellipse(
        (thumb_cx - thumb_r * 1.4, thumb_cy - thumb_r * 1.4,
         thumb_cx + thumb_r * 1.4, thumb_cy + thumb_r * 1.4),
        fill=(255, 255, 255, 190))
    inner_halo = inner_halo.filter(ImageFilter.GaussianBlur(2.5 * SS))
    canvas.alpha_composite(inner_halo)

    ImageDraw.Draw(canvas).ellipse(
        (thumb_cx - thumb_r, thumb_cy - thumb_r, thumb_cx + thumb_r, thumb_cy + thumb_r),
        fill=(255, 255, 255, 255))

    # The single final downsample -- every mask/gradient/border/orb/glow
    # above was drawn at 8x, so this one LANCZOS pass is where all of it
    # gets antialiased at once, instead of each piece being resized (or
    # not resized at all) separately.
    final = canvas.resize((bar_w, bar_h), Image.LANCZOS)
    img.alpha_composite(final, (int(bar_x), int(bar_y)))


def _optical_icon_offset(ic, max_shift=3):
    """(dx, dy) in px that moves an icon's OPTICAL centre onto the centre of
    its display box. Optical centre = midpoint of the opaque bounding-box
    centre and the alpha-weighted centroid, so wide-but-top-heavy or
    narrow-and-tall glyphs (crown, microphone, custom emoji) sit visually
    centred rather than merely bbox-centred. Clamped so an odd image can
    never wander far from its slot."""
    a = ic.getchannel("A")
    w, h = ic.size
    bb = a.point(lambda v: 255 if v > 24 else 0).getbbox()
    cols = list(a.resize((w, 1), Image.BOX).getdata())
    rows = list(a.resize((1, h), Image.BOX).getdata())
    tx, ty = sum(cols), sum(rows)
    if not bb or tx <= 0 or ty <= 0:
        return 0, 0
    cx = sum((i + 0.5) * v for i, v in enumerate(cols)) / tx
    cy = sum((j + 0.5) * v for j, v in enumerate(rows)) / ty
    ox = ((bb[0] + bb[2]) / 2 + cx) / 2
    oy = ((bb[1] + bb[3]) / 2 + cy) / 2
    clamp = lambda v: max(-max_shift, min(max_shift, int(round(v))))
    return clamp(w / 2 - ox), clamp(h / 2 - oy)


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
            odx, ody = _optical_icon_offset(ic)
            icon_x, icon_y = icon_x + odx, icon_y + ody
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
        # Rendered via _draw_stencil_number (supersampled 4x + one
        # downsample, despite the name it just draws with whatever font
        # it's given) rather than a direct draw.text -- at this row's
        # ~16-32px size, native-resolution hinting could snap round-bowl
        # digits (e.g. "36"'s 6) a fractional pixel differently from
        # flat-edged ones, reading as if they don't share the line.
        _draw_stencil_number(img, draw, (x + w / 2 - nat_w / 2, y + 72), value, vf,
                             (215, 215, 222))
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
        # Antialiased (same box/radius/colours as the plain call it replaces,
        # which stair-stepped the corners) -- see _aa_rounded_rect.
        _aa_rounded_rect(img, (sx, sy, sx + sw, sy + sh), 12,
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
        _aa_rounded_rect(img, (sx + 7, sy + 7, sx + sw - 7, sy + sh - 7), 8,
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


# ─────────────────────────────────────────────────────────────────────────
# FOOTER "MAIL BOX" TAG  (supplied SVG frame + Nero PNG + MAIL BOX wordmark)
# ─────────────────────────────────────────────────────────────────────────

MAILBOX_TAG = {
    # Placement on the 1280x853 canvas. The OUTER RING's left edge and its
    # vertical centre are what get pinned (the ring is the tag's visual
    # anchor); the rest follows from the frame's own geometry.
    "ring_left": 24,
    "ring_cy": 795.5,
    # Uniform scale of the supplied frame art (never stretched):
    # 1.0 = the SVG's native 877x346 raster.
    "scale": 0.28,
    # Nero, as fractions of the disc diameter: width of Nero's solid body
    # relative to the disc, and an optical offset of its centre from the
    # disc centre (+y = down). Nero's ears give it top-weight, so a small
    # downward nudge keeps them off the inner ring.
    "nero_fill": 0.865,
    "nero_dx": 0.0,
    "nero_dy": 0.03,
    # Wordmark: width of the LETTERS (outline included, glow excluded) in card
    # px; height follows from the artwork's own aspect ratio. 150 = the
    # previous 135.5 + 10.7%. This is the largest size that keeps >= ~3.8px
    # between the letters and the frame's ring / rounded end (measured on the
    # real pixels): +13.7% already leaves ~1.9px, +15% ~0.9px, +17.5% touches.
    "wordmark_ink_w": 150.0,
    # Horizontal shift from the plate's geometric centre. The plate's right
    # end is a tight rounded cap while the left side opens toward the disc, so
    # moving the lettering left maximises clearance on the binding (right) side.
    "wordmark_dx": -2.5,
    # Optical vertical nudge of the wordmark (card px, +down).
    "wordmark_dy": 0.0,
}

# Geometry measured directly off the supplied frame art (the 877x346
# embedded raster; alpha-run scans). Rings + disc are concentric.
_TAG_ART_W, _TAG_ART_H = 877, 346
_TAG_ART_CENTER = (165.0, 165.0)    # shared centre of rings and disc
_TAG_ART_OUTER_R = 165.0            # outer ring radius (art x 1..329)
_TAG_ART_DISC_D = 260.0             # flat disc diameter (art 35..295)
_TAG_ART_PLATE_X = (329.0, 870.0)   # ring's right edge -> plate's inner right end
_TAG_ART_PLATE_CY = 164.5           # plate (pill) vertical centre

# Wordmark geometry, in the artwork's own 936x217 px: the letters' box
# INCLUDING their dark outline (measured; max-channel <= 30). The artwork's
# soft glow lies outside this box (~14 px margin), which is why placement is
# based on the letters and not on the image bounds.
_WM_VIEWBOX = (936.0, 217.0)
_WM_INK = (14.0, 25.0, 908.0, 187.0)    # x0, y0, x1, y1

_MAILBOX_TAG_CACHE = None   # None = not built yet; False = unavailable; else (sprite, x, y)


def _load_mailbox_frame_svg(path: str) -> Image.Image:
    """Return the supplied SVG frame as a native-resolution RGBA image.

    The SVG is an Affinity export that wraps ONE embedded raster (the
    artwork) in a luminance <mask> (a second embedded raster) at a
    fractional offset. Parsed here rather than rasterised by an SVG library
    so no new dependency is needed; the maths is exactly SVG's (verified
    against resvg: mean per-channel difference 0.006/255). Only
    translate-type transforms are supported -- anything else raises, and
    the caller falls back to the old tag instead of drawing it wrong.
    The SVG's clip rect only trims ~0.2px off the art's bottom edge and is
    ignored."""
    import re
    import base64
    from xml.etree import ElementTree as ET

    root = ET.parse(path).getroot()
    SVG = "{http://www.w3.org/2000/svg}"
    XL = "{http://www.w3.org/1999/xlink}"
    parent = {c: p for p in root.iter() for c in p}

    def translation(el):
        tx = ty = 0.0
        while el is not None:
            tr = el.get("transform")
            if tr:
                m = re.fullmatch(r"\s*matrix\(([^)]*)\)\s*", tr)
                if not m:
                    raise ValueError(f"unsupported transform {tr!r}")
                a, b, c, d, e, f = [float(v) for v in re.split(r"[\s,]+", m.group(1).strip())]
                if (a, b, c, d) != (1.0, 0.0, 0.0, 1.0):
                    raise ValueError(f"non-translate transform {tr!r}")
                tx += e
                ty += f
            el = parent.get(el)
        return tx, ty

    def decode(img_el):
        href = img_el.get(XL + "href") or img_el.get("href") or ""
        m = re.match(r"data:image/png;base64,(.*)", href, re.S)
        if not m:
            raise ValueError("image is not an embedded PNG")
        return Image.open(io.BytesIO(base64.b64decode(m.group(1))))

    by_id = {el.get("id"): el for el in root.iter(SVG + "image") if el.get("id")}
    use = next(root.iter(SVG + "use"))
    art_el = by_id[(use.get(XL + "href") or use.get("href")).lstrip("#")]
    mask_el = next(next(root.iter(SVG + "mask")).iter(SVG + "image"))

    art = decode(art_el).convert("RGBA")
    mask_src = decode(mask_el)
    if (float(use.get("width", "0").rstrip("px")), float(use.get("height", "0").rstrip("px"))) \
            != (float(art.width), float(art.height)):
        raise ValueError("<use> rescales the artwork; not supported")
    if mask_src.mode == "RGBA":
        mask = ImageChops.multiply(mask_src.convert("L"), mask_src.getchannel("A"))
    else:
        mask = mask_src.convert("L")           # luminance mask

    ux, uy = translation(use)
    ox = float(use.get("x", 0)) + ux           # art origin, in viewBox space
    oy = float(use.get("y", 0)) + uy
    mx, my = translation(mask_el)              # mask origin, in viewBox space
    # Sample the mask at the art's (fractional) position, bilinear.
    mask_on_art = mask.transform(art.size, Image.AFFINE,
                                 (1, 0, ox - mx, 0, 1, oy - my), Image.BILINEAR)
    art.putalpha(ImageChops.multiply(art.getchannel("A"), mask_on_art))
    return art


def _build_mailbox_tag():
    """Compose the static tag sprite once (it has no per-user data).
    Returns (sprite, x, y) in canvas px, or None if any asset is missing/bad."""
    cfg = MAILBOX_TAG
    try:
        frame = _load_mailbox_frame_svg(MAILBOX_FRAME_SVG_PATH)
        nero = Image.open(NERO_ICON_PNG_PATH).convert("RGBA")
        wordmark = Image.open(MAILBOX_WORDMARK_PNG_PATH).convert("RGBA")
    except Exception as e:
        log.warning("rank_card: Mail Box tag assets unavailable (%s) -- using the "
                    "fallback tag.", e)
        return None

    sc = cfg["scale"]
    sw, sh = round(_TAG_ART_W * sc), round(_TAG_ART_H * sc)
    sprite = frame.resize((sw, sh), Image.LANCZOS)          # alpha-aware resize

    cx, cy = _TAG_ART_CENTER[0] * sc, _TAG_ART_CENTER[1] * sc
    disc_d = _TAG_ART_DISC_D * sc

    # --- Nero: the supplied PNG, scaled only. Sized off its solid body
    # (alpha>30) so the PNG's transparent margin doesn't shrink it.
    solid = nero.getchannel("A").point(lambda v: 255 if v > 30 else 0).getbbox()
    nero = nero.crop(solid)
    nw = max(1, round(disc_d * cfg["nero_fill"]))
    nh = max(1, round(nw * nero.height / nero.width))
    nero = nero.resize((nw, nh), Image.LANCZOS)
    nx = round(cx + cfg["nero_dx"] * disc_d - nw / 2)
    ny = round(cy + cfg["nero_dy"] * disc_d - nh / 2)

    # --- Wordmark: the supplied artwork, scaled uniformly (never stretched)
    # so its letters fill the plate between the pads. Gradient, outline and
    # glow are baked into the artwork and left exactly as supplied --
    # nothing is added over it.
    ink_x0, ink_y0, ink_x1, ink_y1 = _WM_INK
    plate_cx = (_TAG_ART_PLATE_X[0] + _TAG_ART_PLATE_X[1]) / 2 * sc + cfg["wordmark_dx"]
    k = cfg["wordmark_ink_w"] / (ink_x1 - ink_x0)         # card px per artwork px
    wm_w = max(1, round(_WM_VIEWBOX[0] * k))
    wm_h = max(1, round(_WM_VIEWBOX[1] * k))
    wordmark = wordmark.resize((wm_w, wm_h), Image.LANCZOS)   # Pillow premultiplies RGBA
    # Place so the LETTER box is centred on (plate_cx, plate centre y); the
    # artwork carries a glow margin that must not shift the lettering.
    ink_cx, ink_cy = (ink_x0 + ink_x1) / 2 * k, (ink_y0 + ink_y1) / 2 * k
    wx = round(plate_cx - ink_cx)
    wy = round(_TAG_ART_PLATE_CY * sc + cfg["wordmark_dy"] - ink_cy)
    sprite.alpha_composite(wordmark, (wx, wy))
    sprite.alpha_composite(nero, (nx, ny))

    x0 = round(cfg["ring_left"] - 1 * sc)                     # ring's left edge = art x 1
    y0 = round(cfg["ring_cy"] - cy)
    return sprite, x0, y0


def _get_mailbox_tag():
    global _MAILBOX_TAG_CACHE
    if _MAILBOX_TAG_CACHE is None:
        built = _build_mailbox_tag()
        _MAILBOX_TAG_CACHE = built if built is not None else False
    return _MAILBOX_TAG_CACHE or None


# Footer sparkle: visible-art size in card px, fitted to the reference crop
# (art content box ~32x34 px). Built once from the supplied PNG and cached.
FOOTER_SPARKLE_SIZE = (32, 34)
_FOOTER_SPARKLE_CACHE = None   # None = not built yet; False = unavailable; else RGBA sprite


def _get_footer_sparkle():
    """Final-size footer sparkle from the supplied high-resolution artwork.

    Same two-step exact area-average (BOX) reduction the prestige pips use:
    trimmed art -> 2x working render -> final size. Pillow premultiplies alpha
    for RGBA resizes, so the soft orbit rings keep their colour. Cached."""
    global _FOOTER_SPARKLE_CACHE
    if _FOOTER_SPARKLE_CACHE is None:
        sprite = False
        try:
            if os.path.isfile(SPARKLE_EMOJI_PNG_PATH):
                src = Image.open(SPARKLE_EMOJI_PNG_PATH)
                src.load()
                src = src.convert("RGBA")
                box = src.getchannel("A").getbbox()
                if box:
                    src = src.crop(box)
                    fw, fh = FOOTER_SPARKLE_SIZE
                    work = src.resize((fw * 2, fh * 2), Image.BOX)
                    sprite = work.resize((fw, fh), Image.BOX)
            else:
                log.warning("rank_card: sparkle asset not found at %s", SPARKLE_EMOJI_PNG_PATH)
        except Exception as e:
            log.warning("rank_card: failed to load sparkle asset %s: %s",
                        SPARKLE_EMOJI_PNG_PATH, e)
            sprite = False
        _FOOTER_SPARKLE_CACHE = sprite
    return _FOOTER_SPARKLE_CACHE or None


def _draw_footer(img, draw):
    fy = LAYOUT["footer_y"]
    # Left tag: the supplied Mail Box frame + Nero + MAIL BOX wordmark (built
    # once, cached). If any of those assets is missing the previous paw tag is
    # drawn instead so /rank never breaks.
    mb_tag = _get_mailbox_tag()
    if mb_tag is not None:
        img.alpha_composite(mb_tag[0], (mb_tag[1], mb_tag[2]))
    else:
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
    # The supplied sparkle artwork flanks the line (reference crop). If the
    # asset is missing/unreadable the previous drawn 4-point stars are used,
    # so /rank never breaks.
    sparkle = _get_footer_sparkle()
    if sparkle is not None:
        for sx in (cx - tw / 2 - 33, cx + tw / 2 + 33):
            img.alpha_composite(sparkle, (round(sx - sparkle.width / 2),
                                          round(fy + 22 - sparkle.height / 2)))
    else:
        _draw_sparkle_star(img, draw, cx - tw / 2 - 32, fy + 22, 12, (200, 170, 230))
        _draw_sparkle_star(img, draw, cx + tw / 2 + 32, fy + 22, 12, (200, 170, 230))
