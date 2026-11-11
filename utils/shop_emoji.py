"""Explicit Shop product-image preparation into application-owned emoji.

Preview and publication use :func:`attach_prepared_emojis`, a read-only DB
lookup. Source fetching, image decoding, Discord creation, and recovery live
only in the explicit preparation/reconciliation paths below.

Discord creation is not exactly-once. The asset key and attempt fence stop
concurrent workers from claiming the same content independently; an ambiguous
create remains quarantined until read-only reconciliation or an informed,
explicit owner retry. Changed source bytes can instead be explicitly remapped
to a new content identity while the old uncertain attempt remains untouched.

Shared-pool accounting is durable: each asset permanently records its creator
guild. Ready, preparing, and uncertain assets count toward that guild's
100-asset allowance even when no alias remains; failed and confirmed-missing
assets do not. Reusing an existing ready asset does not consume a new slot.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
import aiosqlite

from database import DB_PATH
from utils.emoji import build_custom_emoji, parse_emoji_input

log = logging.getLogger(__name__)

PIPELINE_VERSION = "rgba-128-png-v1"
MAX_URL_LENGTH = 4096
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 4096
MAX_EMOJI_BYTES = 256 * 1024
MAX_REDIRECTS = 3
DNS_TIMEOUT_SECONDS = 5.0
FETCH_TIMEOUT_SECONDS = 15.0
IMAGE_TIMEOUT_SECONDS = 10.0
DISCORD_TIMEOUT_SECONDS = 15.0
BATCH_TIMEOUT_SECONDS = 55.0
PREPARING_STALE_SECONDS = 60
QUARANTINE_SECONDS = 15 * 60
STARTUP_RECONCILE_LIMIT = 25
MAX_PRODUCTS_PER_REQUEST = 25
APPLICATION_EMOJI_LIMIT = 2000
# Mutating-request outcomes Discord documents as a definitive rejection (the
# request was understood and refused). Every OTHER non-2xx status of a mutating
# request (1xx/3xx, 408/409/413/422, 5xx, anything unrecognised) is ambiguous:
# it must preserve duplicate protection rather than be treated as "nothing
# was created". 429 is handled separately (documented retry instruction).
DEFINITIVE_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 405})
DISCORD_UNKNOWN_EMOJI_CODE = 10014      # JSON error code "Unknown Emoji"
DEFAULT_RATE_LIMIT_SECONDS = 60.0       # only when Discord supplied no delay
MAX_RATE_LIMIT_SECONDS = 24 * 3600.0    # sanity cap on a server-provided delay
# Durable shared-pool safeguard: ready/preparing/uncertain assets count against
# their creator guild even after every alias has moved away. Failed and
# confirmed-missing assets do not consume this per-guild allowance.
GUILD_ASSET_LIMIT = 100
SUPPORTED_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP")
DISCORD_API = "https://discord.com/api/v10"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_NAT64_NETWORKS = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)

# database.py executes these statements and additive migrations via
# ensure_shop_emoji_schema() during normal init_db(). Preview/loaders never DDL.
SHOP_EMOJI_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shop_emoji_assets (
        application_id       TEXT NOT NULL,
        content_hash         TEXT NOT NULL,
        emoji_name           TEXT NOT NULL,
        emoji_id             TEXT,
        animated             INTEGER NOT NULL DEFAULT 0,
        creator_guild_id     INTEGER,
        pipeline_version     TEXT NOT NULL,
        status               TEXT NOT NULL CHECK (
            status IN ('preparing', 'ready', 'uncertain', 'missing', 'failed')
        ),
        attempt_id           TEXT,
        attempt_started_at   TEXT,
        quarantine_until     TEXT,
        last_error           TEXT,
        managed_name         INTEGER NOT NULL DEFAULT 0,
        created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (application_id, content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shop_emoji_url_aliases (
        application_id   TEXT NOT NULL,
        guild_id         INTEGER NOT NULL,
        source_url       TEXT NOT NULL,
        content_hash     TEXT NOT NULL,
        recovery_ack_required INTEGER NOT NULL DEFAULT 0,
        recovery_not_before TEXT,
        created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (application_id, guild_id, source_url),
        FOREIGN KEY (application_id, content_hash)
            REFERENCES shop_emoji_assets(application_id, content_hash)
            ON UPDATE CASCADE ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_shop_emoji_assets_status
    ON shop_emoji_assets(application_id, status, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_shop_emoji_aliases_hash
    ON shop_emoji_url_aliases(application_id, content_hash)
    """,
)

_ASSET_FIELDS = (
    "application_id", "content_hash", "emoji_name", "emoji_id", "animated",
    "creator_guild_id", "pipeline_version", "status", "attempt_id", "attempt_started_at",
    "quarantine_until", "last_error", "created_at", "updated_at",
    # Set once this asset's Discord name is managed by the readable-name policy.
    # It marks "a rename may be in flight or may have completed", so a restart
    # between PATCH and the local write reconciles from Discord instead of
    # treating the name difference as conflicting metadata.
    "managed_name",
)
_ASSET_SELECT = ",".join(_ASSET_FIELDS)
_ASSET_SELECT_E = ",".join("e." + field for field in _ASSET_FIELDS)
_startup_tasks: set[asyncio.Task] = set()
_preparation_locks: dict[str, asyncio.Lock] = {}


async def _add_column(db, table, column, ddl, known_columns):
    """Add a column once, tolerating a concurrent initialiser winning the race.

    ``PRAGMA table_info`` + ``ALTER TABLE`` is check-then-act: the Bot and the
    Dashboard can start together against one SQLite file, both observe the
    column as missing, and both issue the ALTER — the loser raises
    ``duplicate column name``. Re-checking on failure makes the migration safe
    for every process, and the outcome is identical either way.
    """
    if column in known_columns:
        return False
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True
    except Exception:  # noqa: BLE001 - another process may have just added it
        cur = await db.execute(f"PRAGMA table_info({table})")
        if column in {row[1] for row in await cur.fetchall()}:
            return False
        raise


async def ensure_shop_emoji_schema(db):
    """Initialize and additively migrate emoji accounting during normal init_db."""
    for statement in SHOP_EMOJI_SCHEMA:
        await db.execute(statement)
    # The durable preparation queue lives beside the aliases: one DB file means
    # one restart-survivable source of truth for pending work.
    from utils.shop_emoji_queue import ensure_schema as _ensure_queue_schema
    await _ensure_queue_schema(db)
    cur = await db.execute("PRAGMA table_info(shop_emoji_assets)")
    columns = {row[1] for row in await cur.fetchall()}
    await _add_column(db, "shop_emoji_assets", "creator_guild_id", "INTEGER", columns)
    await _add_column(db, "shop_emoji_assets", "managed_name",
                      "INTEGER NOT NULL DEFAULT 0", columns)
    cur = await db.execute("PRAGMA table_info(shop_emoji_url_aliases)")
    alias_columns = {row[1] for row in await cur.fetchall()}
    await _add_column(db, "shop_emoji_url_aliases", "recovery_ack_required",
                      "INTEGER NOT NULL DEFAULT 0", alias_columns)
    await _add_column(db, "shop_emoji_url_aliases", "recovery_not_before",
                      "TEXT", alias_columns)
    # Existing aliased rows retain their original guild where possible. A
    # legacy unaliased NULL remains unattributed and cannot claim a new POST.
    await db.execute(
        "UPDATE shop_emoji_assets SET creator_guild_id=( "
        "SELECT a.guild_id FROM shop_emoji_url_aliases a "
        "WHERE a.application_id=shop_emoji_assets.application_id "
        "AND a.content_hash=shop_emoji_assets.content_hash "
        "ORDER BY a.created_at ASC,a.guild_id ASC LIMIT 1) "
        "WHERE creator_guild_id IS NULL")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_emoji_assets_creator "
        "ON shop_emoji_assets(application_id,creator_guild_id,status)")
    await db.commit()


class ShopEmojiError(Exception):
    def __init__(self, message, *, code="shop_emoji_error", status=400, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


class SourceImageError(ShopEmojiError):
    pass


class DiscordAPIError(ShopEmojiError):
    def __init__(self, message, *, code="discord_api_error", status=502,
                 http_status=None, uncertain=False, retry_after=None,
                 discord_code=None):
        super().__init__(message, code=code, status=status)
        self.http_status = http_status
        self.uncertain = uncertain
        self.retry_after = retry_after
        self.discord_code = discord_code


class DiscordRateLimited(DiscordAPIError):
    """HTTP 429. ``retry_after`` is the seconds (float) Discord told us to wait.

    Discord documents ``Retry-After`` / ``retry_after`` as the time to wait
    before retrying, but does not state that a rate-limited request was left
    unprocessed. So for a MUTATING request ``uncertain`` stays True (duplicate
    protection, exactly as before); the rate limit only adds a schedule. A
    non-mutating request that is rate limited is simply retried later."""

    def __init__(self, message, *, retry_after, scope=None, is_global=False,
                 retry_after_provided=True, uncertain=False, method="GET"):
        super().__init__(message,
                         code="create_uncertain" if uncertain and method == "POST" else "discord_rate_limited",
                         status=429, http_status=429, uncertain=uncertain,
                         retry_after=retry_after)
        self.scope = scope
        self.is_global = is_global
        self.retry_after_provided = retry_after_provided


def _parse_retry_after(*values):
    """Largest finite, non-negative delay among the server-provided values.

    Both the ``Retry-After`` header and the JSON ``retry_after`` field are
    honoured; taking the maximum guarantees the next attempt is never earlier
    than ANY delay Discord supplied. ``None`` means Discord supplied none.
    """
    found = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            found.append(number)
    return min(max(found), MAX_RATE_LIMIT_SECONDS) if found else None


class EmojiLookup:
    """Result of Get Application Emoji: ``present`` | ``absent`` | ``unknown``.

    ``absent`` is returned ONLY for a 404 carrying Discord's "Unknown Emoji"
    code for the exact, validated application and emoji ID. Anything else that
    is not a 200 for that ID (403, 5xx, timeout, connection failure, malformed
    or mismatching body) is ``unknown`` and must never be read as absence.
    """
    __slots__ = ("state", "emoji", "detail")

    def __init__(self, state, emoji=None, detail=""):
        self.state, self.emoji, self.detail = state, emoji, detail


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(value=None):
    return (value or _utcnow()).isoformat(timespec="milliseconds")


def _parse_time(value):
    if not value:
        return None
    try:
        text = str(value).replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _asset(row):
    return dict(zip(_ASSET_FIELDS, row)) if row else None


def configured_application_id():
    return os.getenv("DISCORD_CLIENT_ID", "").strip()


def content_identity(final_png: bytes) -> str:
    """Identity is the exact byte string uploaded, not source pixels or version."""
    return hashlib.sha256(final_png).hexdigest()


def deterministic_emoji_name(content_hash: str) -> str:
    if not _HEX_64.fullmatch(content_hash or ""):
        raise ValueError("A lowercase SHA-256 content hash is required")
    return "shop_" + content_hash[:24]


# ── Readable emoji names ────────────────────────────────────────────────────
# Discord application-emoji names accept ASCII word characters only, so an
# Arabic product name cannot be used verbatim. The slug below is a best-effort
# transliteration for HUMAN legibility; the emoji's IDENTITY is still the
# content hash, which is why the name always ends in a hash-derived suffix.
# Renaming is therefore cosmetic and can never change an asset's identity.
_EMOJI_NAME_LIMIT = 32          # Discord's documented maximum
# 8 hex characters = 32 bits of the content hash inside the name. The suffix is
# only a human-readable tie-breaker, NOT the identity: identity is (and remains)
# the full SHA-256 in shop_emoji_assets, and adoption additionally verifies the
# suffix against that full hash (see _name_matches_content). A longer suffix
# makes an accidental name collision between two different images negligible
# while still leaving room for a readable slug.
_SUFFIX_LENGTH = 8
_NAME_SLUG_BUDGET = _EMOJI_NAME_LIMIT - 1 - _SUFFIX_LENGTH

# Digraphs are replaced first (as whole strings) so they win over their parts;
# single letters then go through one translate table.
_ARABIC_DIGRAPHS = (("ال", "al"), ("خ", "kh"), ("ش", "sh"), ("ث", "th"),
                    ("ذ", "dh"), ("غ", "gh"), ("أ", "a"), ("إ", "e"), ("آ", "aa"))
_ARABIC_LETTERS = {
    "ا": "a", "ب": "b", "ت": "t", "ة": "a", "ج": "j", "ح": "h",
    "د": "d", "ر": "r", "ز": "z", "س": "s", "ص": "s", "ض": "d",
    "ط": "t", "ظ": "z", "ع": "a", "ف": "f", "ق": "q", "ك": "k",
    "ل": "l", "م": "m", "ن": "n", "ه": "h", "و": "u", "ى": "a",
    "ي": "i", "ئ": "e", "ؤ": "o", "ء": "",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
}
# Arabic diacritics/tatweel carry no lexical weight in a slug.
_ARABIC_DROP = set("\u0610\u0611\u0612\u0613\u0614\u0615\u061a\u0640"
                   "\u064b\u064c\u064d\u064e\u064f\u0650\u0651\u0652"
                   "\u0653\u0654\u0655\u0656\u0657\u0658\u0659\u065e\u0670")
_TRANSLITERATE = str.maketrans(_ARABIC_LETTERS)


def slugify_name(value: str) -> str:
    """ASCII slug for a product name; empty when nothing usable remains.

    Arabic transliterates to Latin, other non-ASCII is dropped, runs of
    separators collapse, and the result is bounded so a suffix still fits.
    """
    text = str(value or "")
    for char in _ARABIC_DROP:
        text = text.replace(char, "")
    for source, replacement in _ARABIC_DIGRAPHS:
        text = text.replace(source, replacement)
    text = text.translate(_TRANSLITERATE).lower()
    out, last_was_sep = [], True
    for char in text:
        if char.isalnum() and char.isascii():
            out.append(char)
            last_was_sep = False
        elif not last_was_sep:
            out.append("_")
            last_was_sep = True
    slug = "".join(out).strip("_")
    return slug[:_NAME_SLUG_BUDGET].strip("_")


def name_suffix(content_hash: str) -> str:
    """6-char deterministic suffix derived from the content hash.

    Same image -> same suffix, so a rename of an existing asset is stable and
    two different images can never collide on the readable part alone.
    """
    if not _HEX_64.fullmatch(content_hash or ""):
        raise ValueError("A lowercase SHA-256 content hash is required")
    return content_hash[:_SUFFIX_LENGTH]


def readable_emoji_name(item_name: str, content_hash: str) -> str:
    """``aseer_almajarat_3ef701`` — readable slug + deterministic suffix.

    Falls back to the hash-only name when the product name yields no ASCII
    slug at all, so a name Discord would reject is never sent.
    """
    suffix = name_suffix(content_hash)
    slug = slugify_name(item_name)
    if not slug:
        return deterministic_emoji_name(content_hash)
    name = f"{slug}_{suffix}"[:_EMOJI_NAME_LIMIT]
    return name if len(name) >= _SUFFIX_LENGTH + 2 else deterministic_emoji_name(content_hash)


_VALID_EMOJI_NAME = re.compile(r"[A-Za-z0-9_]{2,32}")


def safe_emoji_name(candidate, content_hash) -> str:
    """A name Discord will accept, or the deterministic fallback.

    Guards the create path: a readable name is only used when it is plain ASCII
    word characters within Discord's 2-32 length window. Anything else falls
    back to the hash-only name rather than risking a rejected create.
    """
    if isinstance(candidate, str) and _VALID_EMOJI_NAME.fullmatch(candidate):
        return candidate
    return deterministic_emoji_name(content_hash)


def name_slug_part(name: str) -> str:
    """The readable part of an emoji name (everything before the suffix)."""
    if not isinstance(name, str):
        return ""
    if "_" not in name:
        return "" if name.startswith("shop_") else name
    return name.rsplit("_", 1)[0]


def _name_matches_content(name: str, content_hash: str) -> bool:
    """Is this Discord emoji name a legitimate name for THIS content hash?

    Guards adoption: a name match must never attach an emoji that belongs to a
    different image. Accepts

    * the deterministic legacy form ``shop_<hash24>``, or
    * a readable ``<slug>_<hash8>`` whose suffix equals this hash's suffix.

    Anything else - including a readable name built from a different hash that
    happens to share the slug - is rejected, so a collision can never be
    adopted.
    """
    if not _HEX_64.fullmatch(content_hash or "") or not isinstance(name, str):
        return False
    if name == deterministic_emoji_name(content_hash):
        return True
    return name.endswith("_" + name_suffix(content_hash)) and bool(name_slug_part(name))


def canonical_product_label(names) -> str | None:
    """The one deterministic product label for a shared image.

    Content-based deduplication means several products can share a single
    application emoji. Its name must not depend on which product happened to be
    processed first, so the label is the lexicographically smallest usable
    product name among everything bound to that content. Pure: the same set of
    names always yields the same label, in any order, across restarts.
    """
    usable = [str(n).strip() for n in (names or [])
              if isinstance(n, str) and str(n).strip() and not str(n).strip().isdigit()]
    return min(usable) if usable else None


async def canonical_label_for_asset(db, application_id, content_hash) -> str | None:
    """Canonical product label for a prepared asset, read from the alias table.

    Every product bound to this asset contributes its listing name; the
    deterministic minimum wins. Returned label is what the emoji should be
    called, independent of queue order.
    """
    if not content_hash:
        return None
    return canonical_product_label(
        await product_names_for_asset(db, application_id, content_hash))


def _canonical_or_raw(raw):
    """Canonicalize when possible; otherwise keep the raw string for comparison.

    ``shop_items.icon_url`` is stored exactly as the owner wrote it, while
    ``shop_emoji_url_aliases.source_url`` is canonicalized. Comparing the two
    with SQL string equality therefore misses legitimate matches (an uppercase
    host, an explicit ``:443``). Both sides go through the same function here.
    """
    try:
        return canonicalize_url(raw) if isinstance(raw, str) else None
    except SourceImageError:
        return raw if isinstance(raw, str) and raw else None


async def product_names_for_asset(db, application_id, content_hash) -> list[str]:
    """Every listing name this asset's content is bound to, canonically matched.

    The alias table gives the (guild, canonical URL) pairs the asset serves; the
    listing names are then resolved by canonicalizing ``shop_items.icon_url`` in
    Python, so a stored URL that differs only in host case or an explicit
    default port still contributes its product name. This is what keeps the
    readable name independent of which product happened to be processed first.
    """
    if not content_hash:
        return []
    cur = await db.execute(
        "SELECT a.guild_id, a.source_url, e.creator_guild_id "
        "FROM shop_emoji_url_aliases a "
        "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
        "  AND e.content_hash=a.content_hash "
        "WHERE a.application_id=? AND a.content_hash=?",
        (application_id, content_hash))
    aliases = await cur.fetchall()
    if not aliases:
        return []
    wanted = {(int(gid), _canonical_or_raw(url)) for gid, url, _owner in aliases
              if gid is not None}
    if not wanted:
        return []
    cur = await db.execute(
        "SELECT guild_id, name, icon_url FROM shop_items "
        "WHERE guild_id IN (" + ",".join("?" for _ in {g for g, _ in wanted}) + ")",
        tuple({g for g, _ in wanted}))
    names = set()
    for gid, name, icon_url in await cur.fetchall():
        if (int(gid), _canonical_or_raw(icon_url)) in wanted and isinstance(name, str):
            names.add(name)
    return sorted(names)


async def resolve_asset_name(db, asset) -> str:
    """The name this asset should carry, for its own content identity."""
    content_hash = (asset or {}).get("content_hash") or ""
    label = await canonical_label_for_asset(db, (asset or {}).get("application_id"), content_hash)
    return readable_emoji_name(label or "", content_hash)


async def emoji_name_conflict(db, application_id, emoji_id, content_hash):
    """The other content hash already bound to this emoji ID, if any.

    A Discord emoji ID belongs to exactly one image. This returns a DIFFERENT
    hash than ``content_hash`` when this emoji is already spoken for, so a
    colliding name or ID can never point two images at one emoji.
    """
    if not _valid_emoji_id(emoji_id):
        return None
    cur = await db.execute(
        "SELECT content_hash FROM shop_emoji_assets WHERE application_id=? AND emoji_id=? "
        "AND content_hash<>?", (application_id, str(emoji_id), content_hash))
    row = await cur.fetchone()
    return row[0] if row else None


def rename_target_name(asset, label) -> str | None:
    """The name a *ready* asset should carry, or None when it is already fine.

    ``label`` must be the CANONICAL product label (see
    ``canonical_product_label``), not whichever product the caller happens to
    be processing - that is what keeps a shared emoji's name stable instead of
    oscillating with queue order.

    Only a ready, bound asset is a rename candidate: renaming anything else
    would change what reconciliation expects while a create may still be in
    flight. Returns None when the current name already matches, so repeat
    passes issue zero PATCHes.
    """
    if not isinstance(asset, dict) or asset.get("status") != "ready":
        return None
    if not _valid_emoji_id(asset.get("emoji_id")):
        return None
    content_hash = asset.get("content_hash") or ""
    if not _HEX_64.fullmatch(content_hash):
        return None
    current = asset.get("emoji_name") or ""
    target = readable_emoji_name(label or "", content_hash)
    return None if current == target else target


def canonicalize_url(raw: str) -> str:
    """Canonicalize without changing signed path/query semantics or doing I/O."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_URL_LENGTH:
        raise SourceImageError("A bounded HTTPS image URL is required.", code="invalid_source_url")
    if "\\" in raw or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in raw):
        raise SourceImageError("The image URL contains unsupported whitespace or controls.", code="invalid_source_url")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise SourceImageError("The image URL is malformed.", code="invalid_source_url") from exc
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise SourceImageError("Only HTTPS image URLs are supported.", code="invalid_source_url")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise SourceImageError("Image URL credentials and fragments are not allowed.", code="invalid_source_url")
    if port is not None and port != 443:
        raise SourceImageError("Image URLs may use only HTTPS port 443.", code="invalid_source_port")
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
        host_text = literal.compressed.lower()
    except ValueError:
        literal = None
        try:
            host_text = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise SourceImageError("The image URL hostname is invalid.", code="invalid_source_url") from exc
    if literal is not None:
        _validate_public_ip(literal)
    if ":" in host_text:
        host_text = f"[{host_text}]"
    default_port = port is None or port == 443
    netloc = host_text if default_port else f"{host_text}:{port}"
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


def _validate_public_ip(value):
    ip = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(value)
    forbidden = (
        not ip.is_global, ip.is_private, ip.is_loopback, ip.is_link_local,
        ip.is_multicast, ip.is_reserved, ip.is_unspecified,
        getattr(ip, "scope_id", None) is not None,
    )
    if any(forbidden):
        raise SourceImageError("The image URL resolves to a non-public address.", code="unsafe_source_address")
    if isinstance(ip, ipaddress.IPv6Address):
        # Do not trust is_global alone for transition/translation mechanisms.
        if (ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None
                or any(ip in network for network in _NAT64_NETWORKS)):
            raise SourceImageError("Translated or tunnelled source addresses are not allowed.", code="unsafe_source_address")
    return ip


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Resolve every answer, reject mixed/public-private results, and pin IPs."""

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            _validate_public_ip(literal)
            infos = [(literal.version == 4 and socket.AF_INET or socket.AF_INET6,
                      socket.SOCK_STREAM, 6, "", (str(literal), port))]
        else:
            loop = asyncio.get_running_loop()
            try:
                infos = await asyncio.wait_for(
                    loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM),
                    timeout=DNS_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, socket.gaierror, OSError) as exc:
                raise SourceImageError("The image hostname could not be resolved safely.",
                                       code="source_dns_failed", status=502) from exc
        answers, output = [], []
        for family_, type_, proto, _canon, sockaddr in infos:
            ip = _validate_public_ip(sockaddr[0])
            key = (family_, str(ip), sockaddr[1])
            if key in answers:
                continue
            answers.append(key)
            output.append({
                "hostname": host,
                "host": str(ip),
                "port": sockaddr[1],
                "family": family_,
                "proto": proto,
                "flags": socket.AI_NUMERICHOST,
            })
        if not output:
            raise SourceImageError("The image hostname returned no usable public address.",
                                   code="unsafe_source_address")
        return output

    async def close(self):
        return None


async def fetch_source_image(url: str) -> bytes:
    """Fetch a public HTTPS image with pinned addresses and bounded redirects."""
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS, sock_connect=5, sock_read=10)
    connector = aiohttp.TCPConnector(resolver=_PinnedResolver())
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        async def run():
            current = canonicalize_url(url)
            for redirect_count in range(MAX_REDIRECTS + 1):
                current = canonicalize_url(current)
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        if redirect_count >= MAX_REDIRECTS:
                            raise SourceImageError("Too many image redirects.", code="source_redirect_limit")
                        location = response.headers.get("Location", "")
                        if not location:
                            raise SourceImageError("The image redirect has no destination.", code="invalid_source_url")
                        current = canonicalize_url(urljoin(current, location))
                        continue
                    if response.status != 200:
                        raise SourceImageError(f"The image server returned HTTP {response.status}.",
                                               code="source_http_error", status=502)
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type and not content_type.startswith("image/"):
                        raise SourceImageError("The source did not return an image response.",
                                               code="unsupported_source_type")
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > MAX_SOURCE_BYTES:
                        raise SourceImageError("The source image exceeds the download limit.",
                                               code="source_too_large")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                        if len(data) > MAX_SOURCE_BYTES:
                            raise SourceImageError("The source image exceeds the download limit.",
                                                   code="source_too_large")
                    if not data:
                        raise SourceImageError("The source image is empty.", code="invalid_source_image")
                    return bytes(data)
            raise SourceImageError("The image redirect chain could not be completed.",
                                   code="source_redirect_limit")
        try:
            return await asyncio.wait_for(run(), timeout=FETCH_TIMEOUT_SECONDS + 1)
        except asyncio.TimeoutError as exc:
            raise SourceImageError("The source image request timed out.",
                                   code="source_timeout", status=504) from exc
        except SourceImageError:
            raise
        except (aiohttp.ClientError, OSError) as exc:
            raise SourceImageError("The source image could not be fetched safely.",
                                   code="source_fetch_failed", status=502) from exc


def normalize_image(data: bytes) -> bytes:
    """Decode one static image and return an exact 128x128 RGBA PNG."""
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise SourceImageError("The source image exceeds the download limit.", code="source_too_large")
    try:
        from PIL import Image, UnidentifiedImageError, __version__ as pillow_version
    except ImportError as exc:
        raise SourceImageError("Pillow is unavailable.", code="image_decoder_unavailable", status=503) from exc
    try:
        if int(pillow_version.split(".", 1)[0]) < 13:
            raise SourceImageError("Pillow 13.0.0 or newer is required for untrusted image decoding.",
                                   code="image_decoder_unsupported", status=503)
    except ValueError as exc:
        raise SourceImageError("The Pillow version could not be verified.",
                               code="image_decoder_unsupported", status=503) from exc
    try:
        with Image.open(io.BytesIO(data), formats=list(SUPPORTED_IMAGE_FORMATS)) as image:
            if image.format not in SUPPORTED_IMAGE_FORMATS:
                raise SourceImageError("The source image format is unsupported.", code="unsupported_source_type")
            width, height = image.size
            if (width <= 0 or height <= 0 or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE
                    or width * height > MAX_IMAGE_PIXELS):
                raise SourceImageError("The source image exceeds the pixel/dimension limits.",
                                       code="image_pixel_limit")
            try:
                frames = int(getattr(image, "n_frames", 1))
            except Exception as exc:
                raise SourceImageError("The image frame count could not be validated.",
                                       code="invalid_source_image") from exc
            if bool(getattr(image, "is_animated", False)) or frames != 1:
                raise SourceImageError("Animated or multi-frame images are not supported; no frame was discarded.",
                                       code="animated_source")
            image.load()  # verify() alone is not a decode and is not sufficient.
            rgba = image.convert("RGBA")
            rgba.thumbnail((128, 128), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
            position = ((128 - rgba.width) // 2, (128 - rgba.height) // 2)
            canvas.alpha_composite(rgba, dest=position)
            output = io.BytesIO()
            canvas.save(output, format="PNG", optimize=False, compress_level=9)
            final = output.getvalue()
    except SourceImageError:
        raise
    except UnidentifiedImageError as exc:
        raise SourceImageError("Only PNG, JPEG, and WebP source images are supported.",
                               code="unsupported_source_type") from exc
    except Exception as exc:
        raise SourceImageError("The source image could not be decoded safely.",
                               code="invalid_source_image") from exc
    if len(final) > MAX_EMOJI_BYTES:
        raise SourceImageError("The normalized emoji exceeds Discord's 256 KiB limit.",
                               code="normalized_emoji_too_large")
    return final


async def _download_and_normalize(url):
    data = await fetch_source_image(url)
    try:
        return await asyncio.wait_for(asyncio.to_thread(normalize_image, data), IMAGE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise SourceImageError("Image decoding exceeded the time limit.",
                               code="image_decode_timeout", status=504) from exc


def _ownership_supported(emoji, application_id, bot_user_id=None):
    """Require positive proof that the bot API identity uploaded the emoji."""
    if not isinstance(emoji, dict) or not bot_user_id:
        return False
    if emoji.get("application_id") is not None and str(emoji["application_id"]) != str(application_id):
        return False
    user = emoji.get("user")
    if not isinstance(user, dict) or user.get("id") is None:
        return False
    return str(user["id"]) == str(bot_user_id)


# Strict ASCII snowflake syntax. ``str.isdigit()`` accepts characters such as
# "²" (and int() then raises) or Arabic-Indic / fullwidth digits (which pass but
# are not Discord IDs), so syntax is decided by an explicit [0-9] pattern.
_EMOJI_ID_RE = re.compile(r"[1-9][0-9]{0,19}")

# SQLite equivalent of the same syntax (GLOB classes are ASCII-only). Interpolate
# with the column expression, e.g. SQL_VALID_EMOJI_ID.format(col="e.emoji_id").
SQL_VALID_EMOJI_ID = (
    "({col} IS NOT NULL AND typeof({col})='text' AND length({col}) BETWEEN 1 AND 20 "
    "AND {col} GLOB '[1-9]*' AND {col} NOT GLOB '*[^0-9]*')")


def _valid_emoji_id(value):
    """True only for a syntactically valid snowflake. Never raises."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return False
    return _EMOJI_ID_RE.fullmatch(text) is not None


def _matching_name(asset, remote):
    return [emoji for emoji in remote
            if isinstance(emoji, dict) and emoji.get("name") == asset["emoji_name"]
            and bool(emoji.get("animated", False)) == bool(asset["animated"])]


def _duplicates(asset, matches):
    return sorted({str(emoji["id"]) for emoji in matches if _valid_emoji_id(emoji.get("id"))})


async def _get_asset(db, application_id, content_hash):
    cur = await db.execute(
        f"SELECT {_ASSET_SELECT} FROM shop_emoji_assets WHERE application_id=? AND content_hash=?",
        (application_id, content_hash))
    return _asset(await cur.fetchone())


async def _get_alias_asset(db, application_id, guild_id, source_url):
    cur = await db.execute(
        f"SELECT {_ASSET_SELECT_E},a.recovery_ack_required,a.recovery_not_before "
        "FROM shop_emoji_url_aliases a "
        "JOIN shop_emoji_assets e ON e.application_id=a.application_id "
        "AND e.content_hash=a.content_hash "
        "WHERE a.application_id=? AND a.guild_id=? AND a.source_url=?",
        (application_id, guild_id, source_url))
    row = await cur.fetchone()
    asset = _asset(row)
    if asset:
        asset["recovery_ack_required"] = bool(row[len(_ASSET_FIELDS)])
        asset["recovery_not_before"] = row[len(_ASSET_FIELDS) + 1]
    return asset


async def _upsert_alias(db, application_id, guild_id, source_url, content_hash):
    now = _iso()
    await db.execute(
        "INSERT INTO shop_emoji_url_aliases "
        "(application_id,guild_id,source_url,content_hash,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(application_id,guild_id,source_url) DO UPDATE SET "
        "content_hash=excluded.content_hash,recovery_ack_required=0,recovery_not_before=NULL,"
        "updated_at=excluded.updated_at",
        (application_id, guild_id, source_url, content_hash, now, now))
    await db.commit()


async def _set_alias_recovery_required(db, application_id, guild_id, source_url, asset):
    not_before = None
    for candidate in (asset.get("quarantine_until"), asset.get("recovery_not_before")):
        if _parse_time(candidate) is not None:
            not_before = candidate
            break
    if not_before is None:
        not_before = _iso(_utcnow() + timedelta(seconds=QUARANTINE_SECONDS))
    await db.execute(
        "UPDATE shop_emoji_url_aliases SET recovery_ack_required=1,recovery_not_before=?,updated_at=? "
        "WHERE application_id=? AND guild_id=? AND source_url=? AND content_hash=?",
        (not_before, _iso(), application_id, guild_id, source_url, asset["content_hash"]))
    await db.commit()
    return not_before


async def _clear_alias_recovery_required(db, application_id, guild_id, source_url, content_hash):
    await db.execute(
        "UPDATE shop_emoji_url_aliases SET recovery_ack_required=0,recovery_not_before=NULL,updated_at=? "
        "WHERE application_id=? AND guild_id=? AND source_url=? AND content_hash=?",
        (_iso(), application_id, guild_id, source_url, content_hash))
    await db.commit()


async def _adopt_asset(db, asset, candidate):
    if not _valid_emoji_id(candidate.get("id")):
        return asset
    # A Discord emoji ID may be bound to exactly one content hash. If it is
    # already bound elsewhere, adopting it here would silently point two
    # different images at the same emoji — the collision failure this guards.
    bound_elsewhere = await emoji_name_conflict(
        db, asset["application_id"], candidate.get("id"), asset["content_hash"])
    if bound_elsewhere:
        return await _set_last_error(
            db, asset,
            "This Discord emoji ID is already bound to different content; refusing to adopt it.")
    cur = await db.execute(
        "UPDATE shop_emoji_assets SET status='ready',emoji_id=?,attempt_id=NULL,"
        "attempt_started_at=NULL,quarantine_until=NULL,last_error=NULL,updated_at=? "
        "WHERE application_id=? AND content_hash=? AND status IN ('preparing','uncertain','failed','missing') "
        "AND COALESCE(attempt_id,'')=COALESCE(?, '')",
        (str(candidate["id"]), _iso(), asset["application_id"], asset["content_hash"], asset.get("attempt_id")))
    await db.commit()
    # A conditional loser must observe the winner's current state, not return
    # its stale pre-update snapshot.
    return await _get_asset(db, asset["application_id"], asset["content_hash"])


async def _set_last_error(db, asset, message):
    await db.execute(
        "UPDATE shop_emoji_assets SET last_error=?,updated_at=? "
        "WHERE application_id=? AND content_hash=? AND status=? "
        "AND COALESCE(attempt_id,'')=COALESCE(?, '')",
        (message, _iso(), asset["application_id"], asset["content_hash"], asset["status"], asset.get("attempt_id")))
    await db.commit()
    return await _get_asset(db, asset["application_id"], asset["content_hash"])


async def _mark_uncertain(db, asset, message, *, start_quarantine=True):
    quarantine = _iso(_utcnow() + timedelta(seconds=QUARANTINE_SECONDS)) if start_quarantine else asset.get("quarantine_until")
    cur = await db.execute(
        "UPDATE shop_emoji_assets SET status='uncertain',quarantine_until=?,last_error=?,updated_at=? "
        "WHERE application_id=? AND content_hash=? AND status IN ('preparing','uncertain') "
        "AND attempt_id=?",
        (quarantine, message, _iso(), asset["application_id"], asset["content_hash"], asset.get("attempt_id")))
    await db.commit()
    return await _get_asset(db, asset["application_id"], asset["content_hash"])


async def _mark_failed(db, asset, message):
    cur = await db.execute(
        "UPDATE shop_emoji_assets SET status='failed',attempt_id=NULL,attempt_started_at=NULL,"
        "quarantine_until=NULL,last_error=?,updated_at=? WHERE application_id=? AND content_hash=? "
        "AND status IN ('preparing','uncertain','failed','missing','ready') "
        "AND COALESCE(attempt_id,'')=COALESCE(?, '')",
        (message, _iso(), asset["application_id"], asset["content_hash"], asset.get("attempt_id")))
    await db.commit()
    return await _get_asset(db, asset["application_id"], asset["content_hash"])


async def _mark_missing(db, asset):
    cur = await db.execute(
        "UPDATE shop_emoji_assets SET status='missing',attempt_id=NULL,attempt_started_at=NULL,"
        "quarantine_until=NULL,last_error=?,updated_at=? WHERE application_id=? AND content_hash=? "
        "AND status='ready' AND emoji_id=?",
        ("Discord confirmed the previously bound emoji ID is absent.", _iso(),
         asset["application_id"], asset["content_hash"], asset.get("emoji_id")))
    await db.commit()
    return await _get_asset(db, asset["application_id"], asset["content_hash"])


INVALID_READY_PREFIX = "invalid_ready_asset:"


async def _mark_ready_unreconciled(db, asset, message):
    """Withdraw trust from a ``ready`` row whose bound emoji ID is unusable.

    The row is moved to ``uncertain`` (never ``failed``/``missing``): an earlier
    create may really have succeeded and only the stored ID is damaged, so a
    replacement must never be created blindly. It keeps its damaged ``emoji_id``
    as evidence, gets a fresh attempt id (so the ordinary uncertain-recovery
    compare-and-swaps apply) and a quarantine, and is then handled by the existing
    read-only reconcile (ownership + content-hash name evidence) or by an
    explicit, acknowledged owner recovery. Compare-and-swap on ``status='ready'``
    and the exact stored ID.
    """
    now = _iso()
    quarantine = _iso(_utcnow() + timedelta(seconds=QUARANTINE_SECONDS))
    await db.execute(
        "UPDATE shop_emoji_assets SET status='uncertain',attempt_id=?,attempt_started_at=?,"
        "quarantine_until=?,last_error=?,updated_at=? WHERE application_id=? AND content_hash=? "
        "AND status='ready' AND COALESCE(emoji_id,'')=COALESCE(?, '')",
        (uuid.uuid4().hex, now, quarantine, f"{INVALID_READY_PREFIX} {message}", now,
         asset["application_id"], asset["content_hash"],
         None if asset.get("emoji_id") is None else str(asset.get("emoji_id"))))
    await db.commit()
    return await _get_asset(db, asset["application_id"], asset["content_hash"])


async def _fail_claim(db, asset, message):
    """Settle a claim after a definite failure of THIS request (rejection / full).

    A claim taken from ``uncertain`` returns to ``uncertain``: a later definite
    rejection does not prove an EARLIER ambiguous POST failed, and ``failed`` is
    a retry state that skips the duplicate-risk acknowledgement. Claims from a
    fresh row or from ``failed``/``missing`` become ``failed`` as before.
    """
    if asset.get("claim_origin") == "uncertain":
        return await _mark_uncertain(
            db, asset,
            f"{message} (an earlier ambiguous creation may still have succeeded; "
            "this rejection does not prove it did not)")
    return await _mark_failed(db, asset, message)


async def _guild_asset_usage(db, application_id, guild_id):
    cur = await db.execute(
        "SELECT COUNT(*) FROM shop_emoji_assets WHERE application_id=? AND creator_guild_id=? "
        "AND status IN ('ready','preparing','uncertain')",
        (application_id, guild_id))
    return int((await cur.fetchone())[0])


async def _enforce_guild_quota(db, application_id, guild_id):
    if guild_id is None:
        raise ShopEmojiError("This legacy asset has no creator-guild attribution; recovery is blocked.",
                             code="asset_attribution_required", status=409)
    used = await _guild_asset_usage(db, application_id, guild_id)
    if used >= GUILD_ASSET_LIMIT:
        raise ShopEmojiError(
            f"This server already accounts for {used} active or recovering emoji assets; "
            f"the durable per-server limit is {GUILD_ASSET_LIMIT}. Old unaliased assets still count.",
            code="guild_emoji_quota", status=409,
            details={"used": used, "limit": GUILD_ASSET_LIMIT})


async def _claim_existing(db, asset, guild_id, *, allow_uncertain=False):
    allowed = ("uncertain",) if allow_uncertain else ("failed", "missing")
    if asset["status"] not in allowed:
        return None
    await db.execute("BEGIN IMMEDIATE")
    attempt = None
    try:
        current = await _get_asset(db, asset["application_id"], asset["content_hash"])
        if (current is None or current["status"] != asset["status"]
                or current.get("attempt_id") != asset.get("attempt_id")):
            await db.commit()
            return None
        creator = current.get("creator_guild_id")
        if creator is None:
            raise ShopEmojiError("This legacy asset has no creator-guild attribution; recovery is blocked.",
                                 code="asset_attribution_required", status=409)
        if int(creator) != int(guild_id):
            raise ShopEmojiError("Only the asset's creator server can recover this non-ready asset.",
                                 code="asset_creator_mismatch", status=409)
        if current["status"] in {"failed", "missing"}:
            await _enforce_guild_quota(db, current["application_id"], int(creator))
        attempt = uuid.uuid4().hex
        now = _iso()
        cur = await db.execute(
            "UPDATE shop_emoji_assets SET status='preparing',attempt_id=?,attempt_started_at=?,"
            "quarantine_until=NULL,last_error=NULL,updated_at=? WHERE application_id=? AND content_hash=? "
            "AND status=? AND COALESCE(attempt_id,'')=COALESCE(?, '')",
            (attempt, now, now, current["application_id"], current["content_hash"],
             current["status"], current.get("attempt_id")))
        await db.commit()
        if not cur.rowcount:
            return None
        # `claim_origin` is in-memory only (never persisted, never returned by
        # _result): it tells _release_unposted_claim whether an earlier create may
        # already have been dispatched for this asset.
        return dict(await _get_asset(db, asset["application_id"], asset["content_hash"]),
                    claim_origin="uncertain" if current["status"] == "uncertain" else "recovered")
    except BaseException as exc:    # incl. CancelledError: the caller never gets the claim
        if isinstance(exc, Exception):
            await db.rollback()
        if attempt is not None:
            await _abandon_new_attempt(
                db, asset["application_id"], asset["content_hash"], attempt,
                "uncertain" if asset["status"] == "uncertain" else "recovered")
        raise


async def _insert_asset_or_claim(db, application_id, guild_id, content_hash, remote,
                                bot_user_id, emoji_name=None):
    # `emoji_name` may be a plain string or a resolver taking the content hash.
    # A resolver is what lets the readable suffix be derived from the REAL hash
    # without downloading the image twice.
    candidate = emoji_name(content_hash) if callable(emoji_name) else emoji_name
    name = safe_emoji_name(candidate, content_hash)
    synthetic = {"application_id": application_id, "content_hash": content_hash,
                 "emoji_name": name, "animated": 0}
    matches = _matching_name(synthetic, remote)
    duplicates = _duplicates(synthetic, matches)
    await db.execute("BEGIN IMMEDIATE")
    written, attempt = False, None
    try:
        existing = await _get_asset(db, application_id, content_hash)
        if existing is not None:
            await db.commit()
            return existing, None, duplicates
        now, attempt = _iso(), uuid.uuid4().hex
        status, emoji_id, error = "preparing", None, None
        if len(matches) > 1:
            # No POST has been claimed for a newly discovered asset: this is a
            # definite conflict, not evidence of an uncertain creation attempt.
            status, error = "failed", "Multiple Discord emojis share the deterministic name: " + ",".join(duplicates)
            attempt = None
        elif len(matches) == 1:
            candidate = matches[0]
            if _valid_emoji_id(candidate.get("id")) and _ownership_supported(candidate, application_id, bot_user_id):
                status, emoji_id, attempt = "ready", str(candidate["id"]), None
            else:
                status, error, attempt = "failed", "The deterministic-name match lacks supported ownership metadata.", None
        if status == "preparing":
            await _enforce_guild_quota(db, application_id, guild_id)
        written = status == "preparing"      # from the next statement on, our claim may exist
        await db.execute(
            "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,animated,"
            "creator_guild_id,pipeline_version,status,attempt_id,attempt_started_at,quarantine_until,last_error,created_at,updated_at) "
            "VALUES (?,?,?,?,0,?,?,?,?,?,NULL,?,?,?) ON CONFLICT(application_id,content_hash) DO NOTHING",
            (application_id, content_hash, name, emoji_id, guild_id, PIPELINE_VERSION, status,
             attempt, now if attempt else None, error, now, now))
        await db.commit()
        current = await _get_asset(db, application_id, content_hash)
        claimed = (dict(current, claim_origin="new")
                   if (current and status == "preparing"
                       and current.get("attempt_id") == attempt) else None)
        return current, claimed, duplicates
    except BaseException as exc:    # incl. CancelledError: the caller never gets the claim
        if isinstance(exc, Exception):
            await db.rollback()
        if written:
            await _abandon_new_attempt(db, application_id, content_hash, attempt, "new")
        raise


async def _reconcile_asset(db, asset, remote, bot_user_id, *, stale_after=PREPARING_STALE_SECONDS):
    """Read-only Discord recovery; absence is never treated as failed creation."""
    matches = _matching_name(asset, remote)
    duplicates = _duplicates(asset, matches)
    if len(matches) > 1:
        message = "Multiple Discord emojis share the deterministic name: " + ",".join(duplicates)
        if asset["status"] == "preparing":
            asset = await _mark_uncertain(db, asset, message)
        else:
            asset = await _set_last_error(db, asset, message)
        return asset, duplicates
    if len(matches) == 1:
        candidate = matches[0]
        if (_valid_emoji_id(candidate.get("id"))
                and _ownership_supported(candidate, asset["application_id"], bot_user_id)):
            # Ownership is not enough: the name must also be a legitimate name
            # for THIS content hash, otherwise a slug collision would adopt an
            # emoji that belongs to a different image.
            if not _name_matches_content(candidate.get("name"), asset["content_hash"]):
                message = ("The emoji name match does not correspond to this content hash; "
                           "refusing to adopt it.")
                if asset["status"] == "preparing":
                    return await _mark_uncertain(db, asset, message), []
                return await _set_last_error(db, asset, message), []
            return await _adopt_asset(db, asset, candidate), []
        # A name match without positive bot-uploader provenance is a collision,
        # not absence. Keep ambiguous creation uncertain and return its ID so
        # every pre-create/retry path blocks instead of falling through to POST.
        message = "The deterministic-name match lacks supported ownership metadata."
        if asset["status"] == "preparing":
            asset = await _mark_uncertain(db, asset, message)
        else:
            asset = await _set_last_error(db, asset, message)
        return asset, duplicates
    if asset["status"] == "preparing":
        started = _parse_time(asset.get("attempt_started_at"))
        if started is None or (_utcnow() - started).total_seconds() >= stale_after:
            asset = await _mark_uncertain(
                db, asset, "Creation outcome is uncertain; absence from one list is not proof of failure.")
    return asset, []


async def _refresh_ready_asset(db, asset, remote, bot_user_id, discord=None):
    """Re-check a ``ready`` asset against Discord.

    * Unusable bound ID: not trusted, moved to ``uncertain`` (never recreated).
    * Bound ID absent from ``remote``: a list omission is NOT proof. The exact
      Get Application Emoji endpoint decides: present -> checked below, absent
      (404 Unknown Emoji) -> ``missing``, unknown (403/5xx/timeout/...) -> the
      asset is left untouched. Without a ``discord`` client nothing can be
      confirmed, so nothing changes.
    """
    if asset["status"] != "ready":
        return asset
    if not _valid_emoji_id(asset.get("emoji_id")):
        return await _mark_ready_unreconciled(
            db, asset,
            "The ready asset has no usable bound emoji ID; reconcile before relying on it "
            "(no replacement is created automatically).")
    bound = next((emoji for emoji in remote if str(emoji.get("id")) == str(asset["emoji_id"])), None)
    if bound is None:
        if discord is None:
            return asset
        lookup = await discord.get_emoji(asset["emoji_id"])
        if lookup.state == "absent":
            return await _mark_missing(db, asset)
        if lookup.state == "unknown":
            log.warning("bound emoji %s presence is unknown (%s); asset left unchanged",
                        asset.get("emoji_id"), lookup.detail)
            return asset
        bound = lookup.emoji
    animated_mismatch = bool(bound.get("animated", False)) != bool(asset["animated"])
    owned = _ownership_supported(bound, asset["application_id"], bot_user_id)
    same_name = bound.get("name") == asset["emoji_name"]
    if owned and not animated_mismatch and not same_name and asset.get("managed_name"):
        # This asset's name is managed by the readable-name policy, so a name
        # difference is expected while a rename is in flight or after one
        # completed but the local write was interrupted. The bound ID is still
        # proven ours, which is what actually matters for resolution.
        return asset
    if not same_name or animated_mismatch or not owned:
        return await _mark_failed(db, asset, "The bound Discord emoji ID has conflicting metadata.")
    return asset


async def attach_prepared_emojis(db, guild_id, products, *, application_id=None):
    """Mutate product dicts with prepared markup using only batched DB reads.

    Preview/publication rows already carry icon_url. The lightweight catalog
    omits it from its response, so fetch those source URLs transiently by the
    same guild-scoped product IDs without adding icon_url to the API payload.

    Bot callers pass the authenticated client's application_id. DISCORD_CLIENT_ID
    remains the dashboard/preparation default, but a bot read must not depend on
    that dashboard variable being present (or still current) in its process.
    This parameter only selects which existing alias to read; it never creates,
    reconciles, or mutates an asset.
    """
    products = list(products or [])
    for product in products:
        product["app_emoji"] = ""
    application_id = (str(application_id).strip() if application_id is not None
                      else configured_application_id())
    if not application_id or not products:
        return products
    sources = [product.get("icon_url") if "icon_url" in product else None
               for product in products]
    missing_indexes = [index for index, product in enumerate(products)
                       if "icon_url" not in product and isinstance(product.get("id"), int)]
    if missing_indexes:
        product_ids = [products[index]["id"] for index in missing_indexes]
        marks = ",".join("?" for _ in product_ids)
        cur = await db.execute(
            f"SELECT id,icon_url FROM shop_items WHERE guild_id=? AND id IN ({marks})",
            (guild_id, *product_ids))
        found = {int(product_id): raw for product_id, raw in await cur.fetchall()}
        for index in missing_indexes:
            sources[index] = found.get(products[index]["id"])
    canonical = {}
    for raw in sources:
        if not raw:
            continue
        try:
            canonical[canonicalize_url(raw)] = None
        except SourceImageError:
            continue
    if not canonical:
        return products
    urls = list(canonical)
    marks = ",".join("?" for _ in urls)
    cur = await db.execute(
        "SELECT a.source_url,e.emoji_id,e.emoji_name,e.animated FROM shop_emoji_url_aliases a "
        "JOIN shop_emoji_assets e ON e.application_id=a.application_id AND e.content_hash=a.content_hash "
        f"WHERE a.application_id=? AND a.guild_id=? AND e.status='ready' AND a.source_url IN ({marks})",
        (application_id, guild_id, *urls))
    markup = {}
    for source_url, emoji_id, name, animated in await cur.fetchall():
        # One damaged row must cost one icon, never the whole batch. It is logged
        # (not silent) and surfaced by the report / worker as an invalid asset.
        try:
            if _valid_emoji_id(emoji_id):
                markup[source_url] = build_custom_emoji(emoji_id, name, bool(animated))
            else:
                log.warning("ready emoji asset for %s has an unusable bound ID; icon omitted", source_url)
        except Exception as exc:  # noqa: BLE001 - isolate a malformed row
            log.warning("could not build emoji markup for %s: %r", source_url, exc)
    for index, product in enumerate(products):
        try:
            product["app_emoji"] = markup.get(canonicalize_url(sources[index] or ""), "")
        except SourceImageError:
            product["app_emoji"] = ""
    return products


async def find_listing_icon_urls(db, guild_id, names):
    """Read-only lookup of shop listing icon URLs by exact product name.

    Inventory rows are keyed by item name and carry no Shop product id. The
    item_catalog row is written when a Shop item is SAVED, so a listing that
    predates that write path has no catalog icon even though its emoji may
    already be prepared against the listing's own icon_url.

    Only an unambiguous, exact, guild-scoped match is accepted: one single
    `shop_items` row for that exact name in this guild, carrying an
    http(s) icon_url. Two listings sharing a name (or a blank match) return
    nothing, so another product's emoji can never be attached to this item.
    No writes, no preparation, no backfill.
    """
    names = sorted({n for n in names if isinstance(n, str) and n})
    if not names:
        return {}
    marks = ",".join("?" for _ in names)
    cur = await db.execute(
        "SELECT name, MIN(icon_url), COUNT(*) FROM shop_items "
        f"WHERE guild_id=? AND name IN ({marks}) GROUP BY name",
        (guild_id, *names))
    rows = await cur.fetchall()
    found = {}
    for name, icon_url, count in rows:
        url = (icon_url or "").strip()
        if count != 1 or not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        found[name] = url
    return found


def product_emoji_references(resolved):
    """Return only IDs emitted by product.emoji, not user-typed emoji text."""
    references, seen = [], set()
    for occurrence in (((resolved or {}).get("tokens") or {}).get("used") or []):
        if occurrence.get("canonical") != "product.emoji" or not occurrence.get("resolved"):
            continue
        parsed = parse_emoji_input(occurrence.get("value") or "")
        if not parsed:
            continue
        emoji_id, name, animated = parsed
        key = (str(emoji_id), name, bool(animated))
        if key not in seen:
            seen.add(key)
            references.append({"id": str(emoji_id), "name": name, "animated": bool(animated)})
    return references


class DiscordAPI:
    def __init__(self, application_id=None, token=None):
        self.application_id = (application_id or configured_application_id()).strip()
        self.token = (token if token is not None else os.getenv("DISCORD_TOKEN", "")).strip()
        if not self.application_id or not self.token:
            raise ShopEmojiError("Discord application credentials are not configured.",
                                 code="discord_not_configured", status=503)
        self._session = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=DISCORD_TIMEOUT_SECONDS), trust_env=False)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session is not None:
            await self._session.close()

    async def _request(self, method, path, *, payload=None, mutating=False):
        headers = {"Authorization": f"Bot {self.token}"}
        try:
            async with self._session.request(
                method, DISCORD_API + path, headers=headers, json=payload,
                allow_redirects=False,
            ) as response:
                if response.status == 204:
                    return None
                raw = await response.read()
                if 200 <= response.status < 300:
                    if not raw:
                        if mutating:
                            raise DiscordAPIError("Discord returned an empty success response.",
                                                  code="create_uncertain" if method == "POST" else "discord_uncertain",
                                                  uncertain=True)
                        return None
                    try:
                        return json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise DiscordAPIError("Discord returned an unreadable success response.",
                                              code="create_uncertain" if method == "POST" else "discord_uncertain",
                                              uncertain=mutating) from exc
                detail, body_retry, discord_code = raw.decode("utf-8", "replace")[:1000], None, None
                try:
                    body = json.loads(raw.decode("utf-8"))
                    detail = str(body.get("message") or detail)
                    body_retry = body.get("retry_after")
                    discord_code = body.get("code")
                except (UnicodeDecodeError, ValueError, AttributeError):
                    pass
                if response.status == 429:
                    # Documented: wait Retry-After / retry_after before retrying. Not
                    # a business failure, so the caller schedules (never counts) it.
                    # A mutating request stays ambiguous: Discord does not say a
                    # rate-limited request was left unprocessed.
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"), body_retry)
                    provided = retry_after is not None
                    if retry_after is None:
                        retry_after = DEFAULT_RATE_LIMIT_SECONDS
                    scope = response.headers.get("X-RateLimit-Scope")
                    is_global = (str(response.headers.get("X-RateLimit-Global", "")).lower() == "true"
                                 or scope == "global")
                    log.warning(
                        "Discord rate limited %s %s: retry_after=%.2fs scope=%s global=%s "
                        "retry_after_from_discord=%s", method, path, retry_after, scope, is_global, provided)
                    raise DiscordRateLimited(
                        f"Discord API 429: {detail}", retry_after=retry_after, scope=scope,
                        is_global=is_global, retry_after_provided=provided,
                        uncertain=mutating, method=method)
                # Only documented, definitive rejections of a MUTATING request
                # prove nothing was changed. Any other non-2xx outcome (3xx we
                # refuse to follow, 5xx, 408/409/413/422, anything unrecognised)
                # is ambiguous and must preserve duplicate protection.
                uncertain = mutating and response.status not in DEFINITIVE_REJECTION_STATUSES
                error_status = (response.status if 400 <= response.status < 500 else 502)
                raise DiscordAPIError(
                    f"Discord API {response.status}: {detail}",
                    code="create_uncertain" if uncertain and method == "POST" else "discord_api_error",
                    status=error_status,
                    http_status=response.status, uncertain=uncertain, retry_after=body_retry,
                    discord_code=discord_code,
                )
        except DiscordAPIError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise DiscordAPIError(
                "Discord transport did not confirm the operation.",
                code="create_uncertain" if mutating and method == "POST" else "discord_transport_error",
                status=504, uncertain=mutating,
            ) from exc

    async def verify_application(self):
        result = await self._request("GET", "/oauth2/applications/@me")
        if not isinstance(result, dict) or str(result.get("id")) != self.application_id:
            raise ShopEmojiError("The configured application ID does not match the bot token.",
                                 code="application_id_mismatch", status=503)

    async def bot_user_id(self):
        result = await self._request("GET", "/users/@me")
        if not isinstance(result, dict) or not _valid_emoji_id(result.get("id")):
            raise ShopEmojiError("Discord did not return a usable bot identity.",
                                 code="discord_identity_failed", status=502)
        return str(result["id"])

    async def list_emojis(self):
        result = await self._request("GET", f"/applications/{self.application_id}/emojis")
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ShopEmojiError("Discord returned an invalid application-emoji list.",
                                 code="emoji_list_invalid", status=502)
        # A malformed entry makes absence unreliable; fail the entire preflight.
        if any(not _valid_emoji_id(item.get("id")) or not isinstance(item.get("name"), str) for item in items):
            raise ShopEmojiError("Discord returned an incomplete application-emoji list.",
                                 code="emoji_list_invalid", status=502)
        return items

    async def get_emoji(self, emoji_id):
        """Get Application Emoji for one bound ID (read-only; never raises on 404).

        Returns an :class:`EmojiLookup`. Only a 200 whose body is that exact ID is
        ``present``; only a 404 with Discord's Unknown Emoji code is ``absent``.
        A 429 propagates as :class:`DiscordRateLimited` so the caller can schedule
        the retry; every other inconclusive outcome is ``unknown``.
        """
        if not _valid_emoji_id(emoji_id):
            return EmojiLookup("unknown", None, "invalid emoji ID; nothing was requested")
        emoji_id = str(emoji_id)
        try:
            result = await self._request(
                "GET", f"/applications/{self.application_id}/emojis/{emoji_id}")
        except DiscordRateLimited:
            raise
        except DiscordAPIError as exc:
            if exc.http_status == 404 and exc.discord_code == DISCORD_UNKNOWN_EMOJI_CODE:
                return EmojiLookup("absent", None, str(exc))
            return EmojiLookup("unknown", None, str(exc))
        if (isinstance(result, dict) and str(result.get("id")) == emoji_id
                and isinstance(result.get("name"), str)):
            return EmojiLookup("present", result, "")
        return EmojiLookup("unknown", None, "Discord returned an unexpected emoji body")

    async def create_emoji(self, name, final_png, bot_user_id):
        data_uri = "data:image/png;base64," + base64.b64encode(final_png).decode("ascii")
        result = await self._request(
            "POST", f"/applications/{self.application_id}/emojis",
            payload={"name": name, "image": data_uri}, mutating=True)
        if (not isinstance(result, dict) or not _valid_emoji_id(result.get("id"))
                or result.get("name") != name or bool(result.get("animated", False))
                or not _ownership_supported(result, self.application_id, bot_user_id)):
            raise DiscordAPIError("Discord's create response did not confirm the expected emoji.",
                                  code="create_uncertain", uncertain=True)
        return result

    async def rename_emoji(self, emoji_id, name):
        """PATCH an existing application emoji's name. Never creates, never deletes.

        Discord supports renaming in place (Modify Application Emoji), so a
        cosmetic rename can reuse the bound ID and leave every alias, hash and
        ownership record untouched.
        """
        result = await self._request(
            "PATCH", f"/applications/{self.application_id}/emojis/{emoji_id}",
            payload={"name": name}, mutating=True)
        if not isinstance(result, dict) or str(result.get("id")) != str(emoji_id) \
                or result.get("name") != name:
            raise DiscordAPIError(
                "Discord's rename response did not confirm the expected name.",
                code="rename_uncertain", uncertain=True)
        return result

    async def delete_emoji(self, emoji_id):
        return await self._request(
            "DELETE", f"/applications/{self.application_id}/emojis/{emoji_id}", mutating=True)


async def preflight_prepared_emoji_ids(guild_id, references):
    """Read-only final validation of prepared IDs emitted by a resolved payload.

    Discord/API/timeout/429 errors propagate without marking assets missing.
    Confirmed absence blocks for owner recovery and likewise performs no write.
    """
    wanted = {}
    for reference in references or []:
        if not isinstance(reference, dict) or not _valid_emoji_id(reference.get("id")):
            raise ShopEmojiError("A resolved product.emoji reference is invalid.",
                                 code="product_emoji_reference_invalid", status=409)
        emoji_id = str(reference["id"])
        wanted[emoji_id] = {
            "name": str(reference.get("name") or ""),
            "animated": bool(reference.get("animated", False)),
        }
    if not wanted:
        return {"checked": 0}
    if len(wanted) > 100:
        raise ShopEmojiError("Too many prepared emoji references require preflight.",
                             code="product_emoji_reference_limit", status=409)
    async with DiscordAPI() as discord:
        await discord.verify_application()
        bot_user_id = await discord.bot_user_id()
        async with aiosqlite.connect(DB_PATH) as db:
            marks = ",".join("?" for _ in wanted)
            cur = await db.execute(
                "SELECT DISTINCT e.emoji_id,e.emoji_name,e.animated FROM shop_emoji_assets e "
                "JOIN shop_emoji_url_aliases a ON a.application_id=e.application_id "
                "AND a.content_hash=e.content_hash "
                f"WHERE e.application_id=? AND a.guild_id=? AND e.status='ready' "
                f"AND e.emoji_id IN ({marks})",
                (discord.application_id, guild_id, *wanted))
            bound = {str(emoji_id): (name, bool(animated))
                     for emoji_id, name, animated in await cur.fetchall()}
        unprepared = sorted(set(wanted) - set(bound))
        if unprepared:
            raise ShopEmojiError(
                "A final product.emoji reference is not a ready prepared asset for this server: "
                + ",".join(unprepared), code="product_emoji_not_prepared", status=409)
        remote = await discord.list_emojis()
        remote_by_id = {str(emoji["id"]): emoji for emoji in remote}
        missing = sorted(emoji_id for emoji_id in wanted if emoji_id not in remote_by_id)
        if missing:
            raise ShopEmojiError(
                "Discord confirmed these prepared emoji IDs are absent; recover them before publishing: "
                + ",".join(missing), code="product_emoji_missing", status=409,
                details={"emoji_ids": missing})
        conflicts = []
        for emoji_id, (name, animated) in bound.items():
            emoji = remote_by_id[emoji_id]
            if (emoji.get("name") != name
                    or bool(emoji.get("animated", False)) != animated
                    or not _ownership_supported(emoji, discord.application_id, bot_user_id)):
                conflicts.append(emoji_id)
        if conflicts:
            raise ShopEmojiError(
                "Prepared emoji metadata conflicts with Discord; recover these IDs before publishing: "
                + ",".join(sorted(conflicts)), code="product_emoji_conflict", status=409,
                details={"emoji_ids": sorted(conflicts)})
        return {"checked": len(wanted)}


def _result(asset, *, code=None, message=None, duplicates=None):
    status = asset["status"] if asset else "failed"
    emoji = ""
    if asset and status == "ready" and _valid_emoji_id(asset.get("emoji_id")):
        emoji = build_custom_emoji(asset["emoji_id"], asset["emoji_name"], bool(asset["animated"]))
    if code is None:
        code = ("deterministic_name_conflict" if duplicates
                else "create_uncertain" if status in {"uncertain", "preparing"} else status)
    return {
        "status": status,
        "code": code,
        "message": message or (asset or {}).get("last_error") or "",
        "content_hash": (asset or {}).get("content_hash"),
        "emoji_name": (asset or {}).get("emoji_name"),
        "animated": bool((asset or {}).get("animated", False)),
        "emoji_id": (asset or {}).get("emoji_id") if status == "ready" else None,
        "emoji": emoji,
        "retry_after": (asset or {}).get("quarantine_until") if status == "uncertain" else None,
        "duplicates": duplicates or [],
    }


async def _retry_allowed(asset, retry_uncertain, acknowledgement, *, quarantine_until=None):
    if not retry_uncertain:
        return False
    if acknowledgement is not True:
        raise ShopEmojiError(
            "Retrying an uncertain creation requires explicit acknowledgement that a duplicate is still possible.",
            code="duplicate_risk_ack_required", status=409)
    until = _parse_time(asset.get("quarantine_until") if quarantine_until is None else quarantine_until)
    if until is None or _utcnow() < until:
        return False
    return True


async def _release_unposted_claim(db, asset, reason):
    """Hand back a claim whose create request was provably never dispatched.

    Only the claimant may POST, and every caller invokes this strictly before
    ``create_emoji()`` is awaited, so nothing can exist on Discord for this
    attempt. Leaving the row ``preparing`` would be wrong twice over: it keeps
    consuming the per-server quota, and a later pass ages it into ``uncertain``,
    demanding an owner duplicate-risk acknowledgement for an emoji that was
    never sent.

    * claim taken from a fresh row, or from ``failed``/``missing`` -> ``failed``:
      the existing ordinary-retry state (no quota, no acknowledgement; the retry
      still goes through name reconciliation and the quota check).
    * claim taken from ``uncertain`` -> back to ``uncertain``: an EARLIER create
      may really have been sent, so the duplicate guard must survive.

    Both transitions are compare-and-swap on this attempt's ``attempt_id``, so a
    row another process has since adopted, re-claimed or marked is left alone.
    Nothing is deleted, locally or on Discord, and no other asset or alias that
    shares the content is touched.
    """
    try:
        if asset.get("claim_origin") == "uncertain":
            return await _mark_uncertain(db, asset, reason)
        return await _mark_failed(db, asset, reason)
    except Exception:  # noqa: BLE001 - releasing must never mask the original error
        log.warning("could not release unposted claim %s", asset.get("content_hash"),
                    exc_info=True)
        return asset


async def _finish_cleanup(coro):
    """Run ``coro`` to completion even if this task is cancelled meanwhile.

    ``await asyncio.shield(x)`` alone is not enough: a second cancellation makes
    the awaiting task leave at once, its caller then closes the database
    connection, and the shielded work finishes (or fails) against a closed
    connection. Here every cancellation that arrives during the cleanup is
    remembered, the cleanup is awaited to its end, and only then is the
    cancellation re-raised, so it is delayed but never swallowed. The wait is
    bounded by the cleanup itself (one indexed UPDATE under SQLite's busy timeout).
    """
    task = asyncio.ensure_future(coro)
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.cancelled():                 # the cleanup itself was cancelled
                raise
            cancelled = exc
        except BaseException:
            if cancelled is not None:
                raise cancelled
            raise
    if cancelled is not None:
        raise cancelled
    return result


class _PreDispatch:
    """Release ``claimed`` if this block exits by ANY exception before dispatch.

    That includes ``asyncio.CancelledError`` (a ``BaseException``, so an
    ``except Exception`` never sees it) and ``StalePreparation``.

    "Before dispatch" is not inferred, it is recorded: ``_create_claimed`` sets
    ``claimed["dispatch_started"]`` synchronously (no await in between)
    immediately before ``create_emoji()`` is called. From that instant a request
    may have reached Discord (a cancelled or timed-out POST proves nothing), so
    the claim is never released here; the existing uncertain/reconcile recovery
    owns it. The release is awaited to completion even under repeated cancellation
    (``_finish_cleanup``) and is marked so nested guards release only once.
    """

    def __init__(self, db, claimed):
        self.db, self.claimed = db, claimed

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        claimed = self.claimed
        if (exc_type is None or claimed is None or claimed.get("dispatch_started")
                or claimed.get("released")):
            return False
        if issubclass(exc_type, StalePreparation):
            reason = str(exc)
        elif issubclass(exc_type, asyncio.CancelledError):
            reason = "Preparation was cancelled before any create request was sent."
        else:
            reason = f"Creation was abandoned before any request was sent: {exc}"
        claimed["released"] = True
        await _finish_cleanup(_release_unposted_claim(self.db, claimed, reason))
        return False


async def _abandon_new_attempt(db, application_id, content_hash, attempt, origin):
    """Compensate a claim whose acquisition was interrupted after it was written.

    The caller never received the claim, so only the attempt id it generated can
    identify it. The release is a compare-and-swap on that id: if the write never
    happened it matches nothing.
    """
    stub = {"application_id": application_id, "content_hash": content_hash,
            "attempt_id": attempt, "claim_origin": origin}
    await _finish_cleanup(_release_unposted_claim(
        db, stub, "Preparation was interrupted before any create request was sent."))


async def _create_claimed(db, discord, asset, final_png, bot_user_id,
                          validate_source=None):
    """Create the claimed emoji, revalidating AT the dispatch boundary.

    The serialization boundary this function defines is deliberately narrow:

        [final list + reconcile awaits] -> validate_source() -> POST

    Everything that can suspend (``list_emojis``, ``_reconcile_asset``) is
    finished BEFORE the validator runs, and nothing is awaited between the
    validator returning True and ``create_emoji()`` being dispatched. A fence
    that ran earlier — before this function — leaves exactly that window open:
    the product can change while the list is in flight, after the earlier check
    has already passed.

    Cost of the check is one indexed SQLite read against the same connection the
    caller already holds, so it is a cross-process fence: the Dashboard's write
    is committed before its HTTP response returns, and this read sees it.
    """
    async with _PreDispatch(db, asset):
        remote = await discord.list_emojis()
        reconciled, duplicates = await _reconcile_asset(db, asset, remote, bot_user_id, stale_after=10**9)
        if duplicates:
            return _result(reconciled, code="deterministic_name_conflict", duplicates=duplicates)
        if reconciled["status"] != "preparing" or reconciled.get("attempt_id") != asset.get("attempt_id"):
            return _result(reconciled, duplicates=duplicates)
        # Capacity is an APPLICATION-wide fact, not a per-guild or per-request
        # one: Discord documents "an application can own up to 2000 emojis",
        # owned by the app and usable only by it, whether uploaded through the
        # API or through the app's settings in the portal. ``remote`` is the
        # List Application Emojis snapshot for that application (one complete
        # ``items`` array - the route documents no pagination), so its length is
        # the only authoritative count available; nothing here is derived from
        # rate-limit headers, which Discord documents as unreliable for emoji
        # routes. Request limits are handled where they belong: a 429 schedules
        # (never fails) the work, and a rejection at this boundary is a definite
        # answer that releases the claim.
        if len(remote) >= APPLICATION_EMOJI_LIMIT:
            failed = await _fail_claim(db, asset, "The application's total emoji capacity is full.")
            if failed["status"] == "ready":
                return _result(failed, code="reconciled")
            return _result(failed, code="emoji_capacity", message=failed["last_error"])
        # ── POST BOUNDARY ── every suspendable await is behind us. This is the last
        # check before an irreversible, never-deleted Discord emoji is created.
        if not await _guard_source(validate_source):
            raise StalePreparation()
    # From here a request may reach Discord. Recorded synchronously, with no await
    # between this line and the call, so no cancellation can fall in between.
    asset["dispatch_started"] = True
    try:
        created = await discord.create_emoji(asset["emoji_name"], final_png, bot_user_id)
    except DiscordAPIError as exc:
        if exc.uncertain:
            uncertain = await _mark_uncertain(db, asset, str(exc))
            if isinstance(exc, DiscordRateLimited):
                # 429 on a create is still ambiguous (see DiscordRateLimited); it only
                # adds the server-provided schedule for the next recheck.
                limited = _result(uncertain, code="create_uncertain", message=str(exc))
                limited["rate_limit_retry_after"] = float(exc.retry_after)
                return limited
            if uncertain["status"] == "ready":
                return _result(
                    uncertain, code="create_uncertain",
                    message=("The create request had an uncertain outcome while this asset was bound by another operation. "
                             "Reconcile application emojis to check for a possible duplicate."))
            return _result(uncertain, code="create_uncertain", message=str(exc))
        failed = await _fail_claim(db, asset, str(exc))
        if failed["status"] == "ready":
            return _result(failed, code="reconciled")
        return _result(failed, code="discord_rejected", message=str(exc))
    adopted = await _adopt_asset(db, asset, created)
    if adopted["status"] == "ready" and str(adopted.get("emoji_id")) != str(created["id"]):
        # Another worker completed first. Our successful response is now a
        # confirmed, detectable duplicate; never issue another POST or delete it.
        return _result(
            adopted, code="duplicate_detected",
            message="Another completion was bound first; this create returned a duplicate ID requiring review.",
            duplicates=sorted({str(adopted["emoji_id"]), str(created["id"])}))
    if adopted["status"] != "ready":
        if (adopted.get("attempt_id") == asset.get("attempt_id")
                and adopted["status"] in {"preparing", "uncertain"}):
            uncertain = await _mark_uncertain(
                db, adopted, "The asset changed before creation could be recorded; reconciliation is required.")
            return _result(uncertain, code="create_uncertain")
        # A newer attempt/state won the fence. This late completion is evidence
        # for recovery, not permission to mutate that newer attempt.
        return _result(
            adopted, code="late_completion",
            message="A late Discord create completed after the asset attempt changed; reconcile the returned ID.",
            duplicates=[str(created["id"])])
    return _result(adopted, code="created")


async def _remap_changed_source(db, discord, guild_id, source_url, old_asset,
                                final_png, remote, bot_user_id, emoji_name=None,
                                validate_source=None):
    """Prepare a new identity and clear recovery state only through this flow."""
    changed_hash = content_identity(final_png)
    new_asset, new_claimed, new_duplicates = await _insert_asset_or_claim(
        db, discord.application_id, guild_id, changed_hash, remote, bot_user_id,
        emoji_name=emoji_name)
    if new_claimed is None and new_asset["status"] in {"preparing", "uncertain"}:
        new_asset, new_duplicates = await _reconcile_asset(db, new_asset, remote, bot_user_id)
    if new_asset["status"] == "ready":
        if not any(str(e.get("id")) == str(new_asset.get("emoji_id")) for e in remote):
            remote = await discord.list_emojis()
            new_duplicates = _duplicates(new_asset, _matching_name(new_asset, remote))
        new_asset = await _refresh_ready_asset(db, new_asset, remote, bot_user_id, discord=discord)
    async with _PreDispatch(db, new_claimed):    # no-op while `new_claimed` is None
        await _upsert_alias(db, discord.application_id, guild_id, source_url, changed_hash)
    if new_claimed is not None and new_asset["status"] == "preparing":
        outcome = await _create_claimed(db, discord, new_claimed, final_png, bot_user_id,
                                        validate_source=validate_source)
    else:
        outcome = _result(new_asset, duplicates=new_duplicates)
        if outcome["status"] == "ready":
            bound_id = str(outcome.get("emoji_id") or "")
            if any(emoji_id != bound_id for emoji_id in new_duplicates):
                outcome["code"] = "deterministic_name_conflict"
                outcome["message"] = (
                    "Multiple Discord emojis share the deterministic name: "
                    + ",".join(new_duplicates))
            else:
                outcome["code"] = "remapped_changed_source"
    outcome["remapped_from_content_hash"] = old_asset["content_hash"]
    if (outcome.get("status") == "ready"
            and outcome.get("code") in {"created", "ready", "reconciled", "remapped_changed_source"}):
        outcome["code"] = "remapped_changed_source"
        outcome["message"] = (
            "The source changed; the alias now uses the newly prepared hash. "
            "The previous asset was left unchanged.")
    return outcome, remote


class StalePreparation(ShopEmojiError):
    """The image stopped being what the product currently uses.

    Raised by the injected ``validate_source`` hook after the download and
    BEFORE any Discord creation, so an obsolete worker cannot leave a durable,
    undeletable emoji behind for an image the storefront no longer shows.
    """

    def __init__(self, message="The product's image changed during preparation."):
        super().__init__(message, code="stale_preparation", status=409)


async def _guard_source(validate_source):
    """Run the caller's staleness fence, if any, exactly where it belongs."""
    if validate_source is None:
        return True
    return bool(await validate_source())


async def _prepare_one(db, discord, guild_id, source_url, remote, bot_user_id,
                       retry_uncertain, acknowledgement, emoji_name=None,
                       validate_source=None):
    """Prepare one product image, with a caller-supplied staleness fence.

    ``validate_source`` is awaited after the image has been downloaded and
    normalised, immediately before any creation request. It must return True
    only while the product's current image is still the one being prepared.
    A False return raises :class:`StalePreparation` before the POST, so no
    external side effect can be produced for an obsolete image. All creation
    paths (fresh create, uncertain recovery, remap) are covered.
    """
    alias_asset = await _get_alias_asset(db, discord.application_id, guild_id, source_url)
    if alias_asset and alias_asset["status"] in {"preparing", "uncertain"}:
        original_asset = alias_asset
        recovery_allowed = False
        # Persist changed-source recovery before reconciliation can turn the
        # old asset ready. A later request must not inherit an unmarked ready
        # alias after an error, crash, or unacknowledged preparation attempt.
        final_png = await _download_and_normalize(source_url)
        if not await _guard_source(validate_source):
            raise StalePreparation()
        changed_hash = content_identity(final_png)
        source_changed = changed_hash != original_asset["content_hash"]
        recovery_not_before = original_asset.get("recovery_not_before")
        if source_changed:
            recovery_not_before = await _set_alias_recovery_required(
                db, discord.application_id, guild_id, source_url, original_asset)
        alias_asset, duplicates = await _reconcile_asset(db, alias_asset, remote, bot_user_id)
        if alias_asset["status"] != "ready":
            if not await _retry_allowed(
                    alias_asset, retry_uncertain, acknowledgement,
                    quarantine_until=recovery_not_before if source_changed else None):
                return _result(alias_asset, duplicates=duplicates), remote
            recovery_allowed = True
            remote = await discord.list_emojis()
            alias_asset, duplicates = await _reconcile_asset(db, alias_asset, remote, bot_user_id)
            if alias_asset["status"] != "ready" and (alias_asset["status"] != "uncertain" or duplicates):
                return _result(alias_asset, duplicates=duplicates), remote
        # Reconciliation may confirm the old emoji, but that alone does not make
        # it the prepared result for different current source bytes.
        if source_changed:
            if not recovery_allowed:
                # The initial reconciliation confirmed the old asset. It stays
                # valid, but only the acknowledged recovery flow may move the
                # URL away from it; never return that old hash as new content.
                if not await _retry_allowed(
                        original_asset, retry_uncertain, acknowledgement,
                        quarantine_until=recovery_not_before):
                    raise ShopEmojiError(
                        "The current source differs from the confirmed old asset; "
                        "use acknowledged recovery after quarantine to remap it.",
                        code="source_changed_recovery_required", status=409)
                remote = await discord.list_emojis()
            # Only this acknowledged flow may move the alias and clear its
            # durable recovery requirement. The old hash is never re-POSTed.
            if not await _guard_source(validate_source):
                raise StalePreparation()
            return await _remap_changed_source(
                db, discord, guild_id, source_url, alias_asset, final_png, remote, bot_user_id,
                emoji_name=emoji_name, validate_source=validate_source)
        if alias_asset["status"] == "ready":
            return _result(alias_asset, code="reconciled"), remote
        claimed = await _claim_existing(db, alias_asset, guild_id, allow_uncertain=True)
        if claimed is None:
            current = await _get_asset(db, discord.application_id, alias_asset["content_hash"])
            return _result(current), remote
        return await _create_claimed(db, discord, claimed, final_png, bot_user_id,
                                     validate_source=validate_source), remote

    old_alias_asset = None
    recovery_not_before = None
    if alias_asset and alias_asset["status"] == "ready":
        old_alias_asset = alias_asset
        recovery_not_before = alias_asset.get("recovery_not_before")
        # Reuse a bound ID already confirmed in this snapshot (including a
        # creation earlier in this batch). Absence requires a fresh list after
        # the ready DB read before it can be treated as confirmed deletion.
        if not any(str(e.get("id")) == str(alias_asset.get("emoji_id")) for e in remote):
            remote = await discord.list_emojis()
        alias_asset = await _refresh_ready_asset(db, alias_asset, remote, bot_user_id, discord=discord)

    final_png = await _download_and_normalize(source_url)
    # ── FENCE ── the download is the long operation. If the product's image
    # changed while it was in flight, stop HERE: no asset row (which would
    # consume quota), no alias, and no Discord request may be produced for an
    # image the storefront no longer shows.
    if not await _guard_source(validate_source):
        raise StalePreparation()
    content_hash = content_identity(final_png)
    if old_alias_asset and old_alias_asset.get("recovery_ack_required"):
        if content_hash == old_alias_asset["content_hash"]:
            # The source reverted to the confirmed old identity; no remap is
            # needed, so ordinary reuse may clear the recovered-alias marker.
            await _clear_alias_recovery_required(
                db, discord.application_id, guild_id, source_url, old_alias_asset["content_hash"])
        else:
            if not await _retry_allowed(
                    old_alias_asset, retry_uncertain, acknowledgement,
                    quarantine_until=recovery_not_before):
                raise ShopEmojiError(
                    "The current source differs from the confirmed old asset; "
                    "use acknowledged recovery after quarantine to remap it.",
                    code="source_changed_recovery_required", status=409)
            remote = await discord.list_emojis()
            if not await _guard_source(validate_source):
                raise StalePreparation()
            return await _remap_changed_source(
                db, discord, guild_id, source_url, old_alias_asset, final_png, remote, bot_user_id,
                emoji_name=emoji_name, validate_source=validate_source)
    asset = await _get_asset(db, discord.application_id, content_hash)
    duplicates = []
    claimed = None
    if asset is None:
        asset, claimed, duplicates = await _insert_asset_or_claim(
            db, discord.application_id, guild_id, content_hash, remote, bot_user_id,
            emoji_name=emoji_name)
    elif asset["status"] in {"preparing", "uncertain"}:
        asset, duplicates = await _reconcile_asset(db, asset, remote, bot_user_id)
    elif asset["status"] == "ready":
        if not any(str(e.get("id")) == str(asset.get("emoji_id")) for e in remote):
            remote = await discord.list_emojis()
        asset = await _refresh_ready_asset(db, asset, remote, bot_user_id, discord=discord)

    # Aliases may point at a non-ready asset, but only ready assets resolve.
    async with _PreDispatch(db, claimed):        # no-op while `claimed` is None
        await _upsert_alias(db, discord.application_id, guild_id, source_url, content_hash)
    if asset["status"] == "ready":
        return _result(asset, code="reused",
                       duplicates=_duplicates(asset, _matching_name(asset, remote))), remote

    # A newly inserted claim proceeds directly to creation. Only an already
    # existing non-ready asset enters these recovery/retry branches.
    if claimed is None and asset["status"] in {"preparing", "uncertain"}:
        if not await _retry_allowed(asset, retry_uncertain, acknowledgement):
            return _result(asset, duplicates=duplicates), remote
        remote = await discord.list_emojis()
        asset, duplicates = await _reconcile_asset(db, asset, remote, bot_user_id)
        if asset["status"] == "ready":
            return _result(asset, code="reconciled"), remote
        if asset["status"] == "uncertain" and not duplicates:
            claimed = await _claim_existing(db, asset, guild_id, allow_uncertain=True)
    elif claimed is None and asset["status"] in {"failed", "missing"}:
        # A present bound ID with conflicting metadata needs owner recovery;
        # only a confirmed missing bound ID may be recreated normally.
        if _valid_emoji_id(asset.get("emoji_id")):
            # A list omission is not proof of absence: ask for this exact ID.
            bound = next((e for e in remote if str(e.get("id")) == str(asset["emoji_id"])), None)
            if bound is None:
                lookup = await discord.get_emoji(asset["emoji_id"])
                if lookup.state == "unknown":
                    return _result(
                        asset, code="emoji_state_unknown",
                        message=("Discord did not confirm whether the previously bound emoji still exists "
                                 f"({lookup.detail}); no replacement will be created until it does.")), remote
                bound = lookup.emoji if lookup.state == "present" else None
            if bound is not None:
                if (asset["status"] == "missing"
                        and _ownership_supported(bound, discord.application_id, bot_user_id)
                        and bool(bound.get("animated", False)) == bool(asset["animated"])
                        and _name_matches_content(bound.get("name"), asset["content_hash"])):
                    # The emoji reappeared (the earlier absence was wrong): re-bind it.
                    asset = await _adopt_asset(db, asset, bound)
                    if asset["status"] == "ready":
                        return _result(asset, code="reconciled"), remote
                return _result(asset, code="bound_emoji_conflict",
                               message=asset.get("last_error") or "The bound emoji requires owner recovery."), remote
        # Known deletion of a bound ID is different from an uncertain create
        # with no known ID and never requires the uncertain-retry acknowledgement.
        matches = _matching_name(asset, remote)
        duplicates = _duplicates(asset, matches)
        if len(matches) > 1:
            asset = await _set_last_error(db, asset, "Multiple Discord emojis share the deterministic name: " + ",".join(duplicates))
        elif len(matches) == 1:
            if _ownership_supported(matches[0], discord.application_id, bot_user_id):
                asset = await _adopt_asset(db, asset, matches[0])
            else:
                asset = await _set_last_error(db, asset, "The deterministic-name match lacks supported ownership metadata.")
        else:
            claimed = await _claim_existing(db, asset, guild_id)
    if asset["status"] == "ready":
        return _result(asset, code="reconciled"), remote
    if claimed is None:
        return _result(asset, duplicates=duplicates), remote
    if content_identity(final_png) != claimed["content_hash"]:
        return _result(claimed, code="content_changed", message="The normalized content changed before creation."), remote
    # ── FENCE ── the download is finished; the product may have changed while it
    # was in flight. Verify against the caller's authority before creating.
    # The claim exists by now, so a rejection must hand it back (nothing was sent).
    async with _PreDispatch(db, claimed):
        if not await _guard_source(validate_source):
            raise StalePreparation()
    return await _create_claimed(db, discord, claimed, final_png, bot_user_id,
                                 validate_source=validate_source), remote


def _preparation_lock(application_id):
    lock = _preparation_locks.get(application_id)
    if lock is None:
        lock = asyncio.Lock()
        _preparation_locks[application_id] = lock
    return lock


async def _load_products(db, guild_id, product_ids):
    marks = ",".join("?" for _ in product_ids)
    cur = await db.execute(
        f"SELECT id,icon_url FROM shop_items WHERE guild_id=? AND id IN ({marks})",
        (guild_id, *product_ids))
    return {int(row[0]): row[1] for row in await cur.fetchall()}


def _fence_for_url(db, guild_id, source_url, product_ids):
    """Build a fence re-confirming ONE URL against the current listings.

    The explicit owner path is authoritative on product IDs, never on URLs: the
    URL list is derived once, when the request arrives, and the batch may run
    for a long time afterwards. Before any image is uploaded, re-read the
    listings this URL came from and require that at least one of them still
    points at it — otherwise the emoji would be created for an image the
    storefront no longer shows.

    "At least one" is deliberate: several products can share one icon URL, and
    the batch prepares each URL once. If every listing moves away, the
    preparation is obsolete; if one keeps it, the artefact is still wanted.

    Returns an awaitable predicate for ``_prepare_one``'s ``validate_source``.
    """
    async def still_current():
        marks = ",".join("?" for _ in product_ids)
        cur = await db.execute(
            f"SELECT icon_url FROM shop_items WHERE guild_id=? AND id IN ({marks})",
            (guild_id, *product_ids))
        for row in await cur.fetchall():
            try:
                if canonicalize_url(row[0]) == source_url:
                    return True
            except SourceImageError:
                continue
        return False
    return still_current


def _validate_product_ids(product_ids):
    if (not isinstance(product_ids, list) or not product_ids
            or len(product_ids) > MAX_PRODUCTS_PER_REQUEST
            or any(not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 for pid in product_ids)):
        raise ShopEmojiError("Send 1–25 valid product IDs.", code="invalid_product_ids")
    return list(dict.fromkeys(product_ids))


async def prepare_product_emojis(guild_id, product_ids, *, retry_uncertain=False,
                                 acknowledge_duplicate_risk=False):
    """Explicit owner action. Product IDs, never browser-supplied URLs, are authoritative."""
    product_ids = _validate_product_ids(product_ids)
    if retry_uncertain and acknowledge_duplicate_risk is not True:
        raise ShopEmojiError(
            "Retrying uncertain creation requires explicit duplicate-risk acknowledgement.",
            code="duplicate_risk_ack_required", status=409)

    async def run():
        async with aiosqlite.connect(DB_PATH) as db:
            products = await _load_products(db, guild_id, product_ids)
            missing = [pid for pid in product_ids if pid not in products]
            if missing:
                raise ShopEmojiError("One or more products were not found in this server.",
                                     code="unknown_product", status=404,
                                     details={"product_ids": missing})
            async with DiscordAPI() as discord:
                async with _preparation_lock(discord.application_id):
                    await discord.verify_application()
                    bot_user_id = await discord.bot_user_id()
                    remote = await discord.list_emojis()
                    by_url, order, url_products = {}, [], {}
                    for pid in product_ids:
                        raw = products[pid]
                        if not raw:
                            by_url.setdefault(pid, None)
                            continue
                        try:
                            url = canonicalize_url(raw)
                        except SourceImageError as exc:
                            by_url[pid] = exc
                            continue
                        if url not in order:
                            order.append(url)
                        url_products.setdefault(url, []).append(pid)
                        by_url[pid] = url
                    url_results = {}
                    rate_limited = None     # seconds Discord asked us to wait (first 429 of the batch)
                    for url in order:
                        if rate_limited is not None:
                            # Discord told us to wait: do not keep sending requests.
                            url_results[url] = {
                                "status": "failed", "code": "rate_limited", "emoji": "", "duplicates": [],
                                "message": "Discord rate limited this batch; try again later.",
                                "rate_limit_retry_after": float(rate_limited)}
                            continue
                        try:
                            result, remote = await _prepare_one(
                                db, discord, guild_id, url, remote, bot_user_id,
                                retry_uncertain, acknowledge_duplicate_risk,
                                # This path is an explicit owner request, so the
                                # URL is re-confirmed per product: a listing that
                                # was edited while this batch ran must not get an
                                # emoji for the image it no longer uses.
                                validate_source=_fence_for_url(
                                    db, guild_id, url, url_products[url]))
                            if result.get("rate_limit_retry_after") is not None:
                                # A create was rate limited (the asset is uncertain): stop here.
                                rate_limited = float(result["rate_limit_retry_after"])
                            if result.get("status") == "ready" and result.get("emoji_id"):
                                # Later URLs in this batch must see creations or
                                # adoptions made earlier in that same batch.
                                remote[:] = [e for e in remote if str(e.get("id")) != str(result["emoji_id"])]
                                remote.append({
                                    "id": str(result["emoji_id"]),
                                    "name": result["emoji_name"],
                                    "animated": bool(result.get("animated")),
                                    "application_id": discord.application_id,
                                    "user": {"id": bot_user_id},
                                })
                        except DiscordRateLimited as exc:
                            rate_limited = float(exc.retry_after)
                            result = {"status": "failed", "code": "rate_limited",
                                      "message": str(exc), "emoji": "", "duplicates": [],
                                      "rate_limit_retry_after": float(exc.retry_after)}
                        except (SourceImageError, ShopEmojiError) as exc:
                            result = {"status": "failed", "code": exc.code,
                                      "message": str(exc), "emoji": "", "duplicates": [],
                                      "retry_after": getattr(exc, "retry_after", None)}
                        url_results[url] = result
                    results = []
                    for pid in product_ids:
                        target = by_url[pid]
                        if target is None:
                            result = {"status": "failed", "code": "icon_url_unset",
                                      "message": "This product has no icon_url.", "emoji": "", "duplicates": []}
                        elif isinstance(target, Exception):
                            result = {"status": "failed", "code": target.code,
                                      "message": str(target), "emoji": "", "duplicates": []}
                        else:
                            result = url_results[target]
                        results.append({"product_id": pid, **result})
                    return {"success": True, "results": results,
                            "uncertain": any(r["status"] in {"uncertain", "preparing"} for r in results)}
    try:
        return await asyncio.wait_for(run(), timeout=BATCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ShopEmojiError("Preparation exceeded the batch deadline; any in-flight create requires reconciliation.",
                             code="preparation_deadline", status=504) from exc


async def reconcile_product_emojis(guild_id, product_ids):
    """Explicit read-only Discord reconciliation for stored product icon aliases."""
    product_ids = _validate_product_ids(product_ids)

    async def run():
        async with DiscordAPI() as discord:
            async with _preparation_lock(discord.application_id):
                await discord.verify_application()
                bot_user_id = await discord.bot_user_id()
                async with aiosqlite.connect(DB_PATH) as db:
                    products = await _load_products(db, guild_id, product_ids)
                    missing = [pid for pid in product_ids if pid not in products]
                    if missing:
                        raise ShopEmojiError("One or more products were not found in this server.",
                                             code="unknown_product", status=404,
                                             details={"product_ids": missing})
                    pending = []
                    for pid in product_ids:
                        raw = products[pid]
                        if not raw:
                            pending.append((pid, None, {"status": "failed", "code": "icon_url_unset",
                                                        "message": "This product has no icon_url."}))
                            continue
                        try:
                            url = canonicalize_url(raw)
                            asset = await _get_alias_asset(db, discord.application_id, guild_id, url)
                        except SourceImageError as exc:
                            pending.append((pid, None, {"status": "failed", "code": exc.code,
                                                        "message": str(exc)}))
                            continue
                        pending.append((pid, asset, None if asset else {
                            "status": "unprepared", "code": "not_prepared",
                            "message": "No prepared asset is aliased to the current icon_url."}))
                    # The list postdates all asset reads, so a ready ID absent
                    # here is a confirmed deletion rather than a stale snapshot.
                    remote = await discord.list_emojis()
                    results = []
                    for pid, asset, initial in pending:
                        if asset is None:
                            results.append({"product_id": pid, "emoji": "", "duplicates": [], **initial})
                            continue
                        duplicates = _duplicates(asset, _matching_name(asset, remote))
                        if asset["status"] in {"preparing", "uncertain", "failed", "missing"}:
                            asset, duplicates = await _reconcile_asset(db, asset, remote, bot_user_id)
                        elif asset["status"] == "ready":
                            asset = await _refresh_ready_asset(db, asset, remote, bot_user_id, discord=discord)
                            duplicates = _duplicates(asset, _matching_name(asset, remote))
                        results.append({"product_id": pid,
                                        **_result(asset, duplicates=duplicates, code="reconciled")})
                    return {"success": True, "results": results,
                            "uncertain": any(r["status"] in {"uncertain", "preparing"} for r in results)}
    return await asyncio.wait_for(run(), timeout=BATCH_TIMEOUT_SECONDS)


async def reconcile_uncertain_at_startup():
    """Best-effort read-only Discord recovery; never creates or deletes emoji."""
    if not configured_application_id() or not os.getenv("DISCORD_TOKEN", "").strip():
        return
    async with DiscordAPI() as discord:
        async with _preparation_lock(discord.application_id):
            await discord.verify_application()
            bot_user_id = await discord.bot_user_id()
            remote = await discord.list_emojis()
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    f"SELECT {_ASSET_SELECT} FROM shop_emoji_assets WHERE application_id=? "
                    "AND status IN ('uncertain','preparing') ORDER BY updated_at ASC LIMIT ?",
                    (discord.application_id, STARTUP_RECONCILE_LIMIT))
                rows = await cur.fetchall()
                for row in rows:
                    await _reconcile_asset(db, _asset(row), remote, bot_user_id)


def schedule_startup_reconciliation():
    """Schedule after schema init; callers do not await network work at startup."""
    if not configured_application_id() or not os.getenv("DISCORD_TOKEN", "").strip():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    async def guarded():
        try:
            await reconcile_uncertain_at_startup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Shop emoji startup reconciliation was inconclusive: %s", exc)

    task = loop.create_task(guarded(), name="shop-emoji-startup-reconciliation")
    _startup_tasks.add(task)
    task.add_done_callback(_startup_tasks.discard)
    return task


async def delete_confirmed_duplicate(guild_id, content_hash, emoji_id, *, confirm=False):
    """Delete only a freshly confirmed, unbound duplicate selected by the owner."""
    if confirm is not True:
        raise ShopEmojiError("Explicit duplicate-deletion confirmation is required.",
                             code="delete_confirmation_required", status=409)
    if not _HEX_64.fullmatch(content_hash or "") or not _valid_emoji_id(emoji_id):
        raise ShopEmojiError("A valid asset hash and confirmed emoji ID are required.",
                             code="invalid_duplicate_target")
    async with DiscordAPI() as discord:
        async with _preparation_lock(discord.application_id):
            await discord.verify_application()
            bot_user_id = await discord.bot_user_id()
            async with aiosqlite.connect(DB_PATH) as db:
                asset = await _get_asset(db, discord.application_id, content_hash)
                cur = await db.execute(
                    "SELECT 1 FROM shop_emoji_url_aliases WHERE application_id=? AND guild_id=? AND content_hash=? LIMIT 1",
                    (discord.application_id, guild_id, content_hash))
                if asset is None or await cur.fetchone() is None:
                    raise ShopEmojiError("The asset is not referenced by this server.",
                                         code="unknown_emoji_asset", status=404)
                if asset["status"] == "preparing":
                    raise ShopEmojiError("An attempt is active; reconcile its late completion before deleting duplicates.",
                                         code="duplicate_attempt_active", status=409)
                if asset["status"] == "ready":
                    if not _valid_emoji_id(asset.get("emoji_id")):
                        raise ShopEmojiError("The ready asset has no usable bound emoji ID.",
                                             code="duplicate_bound_id_required", status=409)
                # Any status, not only ``ready``: a row that still records this
                # exact emoji ID (a ``missing`` row keeps its ID as evidence, a
                # ``failed`` row may carry one from an earlier life) is the only
                # local proof of what that ID was created for. Deleting it would
                # destroy that evidence and could not be undone, so it is
                # protected exactly like a ready binding.
                if (_valid_emoji_id(asset.get("emoji_id"))
                        and str(asset["emoji_id"]) == str(emoji_id)):
                    raise ShopEmojiError("The bound emoji ID cannot be deleted as a duplicate.",
                                         code="bound_emoji_protected", status=409)
                remote = await discord.list_emojis()
                matches = _matching_name(asset, remote)
                ids = {str(emoji["id"]) for emoji in matches}
                bound_missing = asset["status"] == "ready" and str(asset["emoji_id"]) not in ids
                if len(matches) < 2 or bound_missing or str(emoji_id) not in ids:
                    raise ShopEmojiError("The selected duplicate is no longer confirmed by a fresh Discord list.",
                                         code="duplicate_not_confirmed", status=409)
                target = next(emoji for emoji in matches if str(emoji["id"]) == str(emoji_id))
                if not _ownership_supported(target, discord.application_id, bot_user_id):
                    raise ShopEmojiError("The duplicate's ownership metadata does not permit deletion.",
                                         code="duplicate_ownership_conflict", status=409)
                # ── APPLICATION-WIDE binding check ── An application emoji is a
                # SHARED resource: it is owned by the application, not by the
                # guild that asked for the deletion, and nothing in Discord ties
                # it to one server. The alias row checked above proves only that
                # THIS content hash is referenced here; it says nothing about
                # another guild's asset row that may hold this exact emoji ID.
                # ``shop_emoji_assets`` is keyed by (application_id, content_hash)
                # but the emoji_id it stores is application-scoped, so the
                # question "is this ID still bound?" must be asked across every
                # content hash and every creator guild - never guild-scoped.
                # Deleting a still-bound ID would silently break the other
                # consumer's icon, so this runs immediately before DELETE (the
                # fresh list above has already proven the ID still exists).
                bound_elsewhere = await emoji_name_conflict(
                    db, discord.application_id, emoji_id, content_hash)
                if bound_elsewhere:
                    raise ShopEmojiError(
                        "This Discord emoji is still bound to another prepared asset "
                        f"({bound_elsewhere[:12]}...); deleting it would break that consumer.",
                        code="emoji_in_use", status=409,
                        details={"content_hash": bound_elsewhere})
                try:
                    await discord.delete_emoji(str(emoji_id))
                except DiscordAPIError as exc:
                    if exc.uncertain:
                        raise ShopEmojiError("Duplicate deletion is uncertain; do not retry until Discord is rechecked.",
                                             code="delete_uncertain", status=202) from exc
                    if exc.http_status == 404:
                        # The fresh list and provenance check established this
                        # exact unbound target immediately before DELETE. A
                        # definitive 404 therefore confirms absence; do not
                        # issue another DELETE or turn this into a retry loop.
                        return {"success": True, "deleted_emoji_id": str(emoji_id),
                                "bound_emoji_id": (str(asset["emoji_id"])
                                                   if _valid_emoji_id(asset.get("emoji_id")) else None),
                                "content_hash": content_hash}
                    raise
                return {"success": True, "deleted_emoji_id": str(emoji_id),
                        "bound_emoji_id": (str(asset["emoji_id"])
                                           if _valid_emoji_id(asset.get("emoji_id")) else None),
                        "content_hash": content_hash}
