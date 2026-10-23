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
)
_ASSET_SELECT = ",".join(_ASSET_FIELDS)
_ASSET_SELECT_E = ",".join("e." + field for field in _ASSET_FIELDS)
_startup_tasks: set[asyncio.Task] = set()
_preparation_locks: dict[str, asyncio.Lock] = {}


async def ensure_shop_emoji_schema(db):
    """Initialize and additively migrate emoji accounting during normal init_db."""
    for statement in SHOP_EMOJI_SCHEMA:
        await db.execute(statement)
    cur = await db.execute("PRAGMA table_info(shop_emoji_assets)")
    columns = {row[1] for row in await cur.fetchall()}
    if "creator_guild_id" not in columns:
        await db.execute("ALTER TABLE shop_emoji_assets ADD COLUMN creator_guild_id INTEGER")
    cur = await db.execute("PRAGMA table_info(shop_emoji_url_aliases)")
    alias_columns = {row[1] for row in await cur.fetchall()}
    if "recovery_ack_required" not in alias_columns:
        await db.execute(
            "ALTER TABLE shop_emoji_url_aliases "
            "ADD COLUMN recovery_ack_required INTEGER NOT NULL DEFAULT 0")
    if "recovery_not_before" not in alias_columns:
        await db.execute("ALTER TABLE shop_emoji_url_aliases ADD COLUMN recovery_not_before TEXT")
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
                 http_status=None, uncertain=False, retry_after=None):
        super().__init__(message, code=code, status=status)
        self.http_status = http_status
        self.uncertain = uncertain
        self.retry_after = retry_after


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


def _valid_emoji_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isdigit() and int(value) > 0


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
    except Exception:
        await db.rollback()
        raise
    if not cur.rowcount:
        return None
    return await _get_asset(db, asset["application_id"], asset["content_hash"])


async def _insert_asset_or_claim(db, application_id, guild_id, content_hash, remote, bot_user_id):
    name = deterministic_emoji_name(content_hash)
    synthetic = {"application_id": application_id, "content_hash": content_hash,
                 "emoji_name": name, "animated": 0}
    matches = _matching_name(synthetic, remote)
    duplicates = _duplicates(synthetic, matches)
    await db.execute("BEGIN IMMEDIATE")
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
        await db.execute(
            "INSERT INTO shop_emoji_assets (application_id,content_hash,emoji_name,emoji_id,animated,"
            "creator_guild_id,pipeline_version,status,attempt_id,attempt_started_at,quarantine_until,last_error,created_at,updated_at) "
            "VALUES (?,?,?,?,0,?,?,?,?,?,NULL,?,?,?) ON CONFLICT(application_id,content_hash) DO NOTHING",
            (application_id, content_hash, name, emoji_id, guild_id, PIPELINE_VERSION, status,
             attempt, now if attempt else None, error, now, now))
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    current = await _get_asset(db, application_id, content_hash)
    claimed = current if (current and status == "preparing"
                          and current.get("attempt_id") == attempt) else None
    return current, claimed, duplicates


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


async def _refresh_ready_asset(db, asset, remote, bot_user_id):
    if asset["status"] != "ready":
        return asset
    if not _valid_emoji_id(asset.get("emoji_id")):
        return await _mark_failed(db, asset, "The ready asset has no usable bound emoji ID.")
    bound = next((emoji for emoji in remote if str(emoji.get("id")) == str(asset["emoji_id"])), None)
    if bound is None:
        return await _mark_missing(db, asset)
    if (bound.get("name") != asset["emoji_name"]
            or bool(bound.get("animated", False)) != bool(asset["animated"])
            or not _ownership_supported(bound, asset["application_id"], bot_user_id)):
        return await _mark_failed(db, asset, "The bound Discord emoji ID has conflicting metadata.")
    return asset


async def attach_prepared_emojis(db, guild_id, products):
    """Mutate product dicts with prepared markup using only batched DB reads.

    Preview/publication rows already carry icon_url. The lightweight catalog
    omits it from its response, so fetch those source URLs transiently by the
    same guild-scoped product IDs without adding icon_url to the API payload.
    """
    products = list(products or [])
    for product in products:
        product["app_emoji"] = ""
    application_id = configured_application_id()
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
        if _valid_emoji_id(emoji_id):
            markup[source_url] = build_custom_emoji(emoji_id, name, bool(animated))
    for index, product in enumerate(products):
        try:
            product["app_emoji"] = markup.get(canonicalize_url(sources[index] or ""), "")
        except SourceImageError:
            product["app_emoji"] = ""
    return products


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
                detail, retry_after = raw.decode("utf-8", "replace")[:1000], None
                try:
                    body = json.loads(raw.decode("utf-8"))
                    detail = str(body.get("message") or detail)
                    retry_after = body.get("retry_after")
                except (UnicodeDecodeError, ValueError, AttributeError):
                    pass
                uncertain = mutating and (response.status >= 500 or response.status == 429)
                # Definitive 4xx responses (including 401/403/404) keep their
                # status and are not retryable. Only 429/5xx/transport failures
                # use retryable/uncertain semantics.
                error_status = (response.status if 400 <= response.status < 500 else 502)
                raise DiscordAPIError(
                    f"Discord API {response.status}: {detail}",
                    code="create_uncertain" if uncertain and method == "POST" else "discord_api_error",
                    status=error_status,
                    http_status=response.status, uncertain=uncertain, retry_after=retry_after,
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


async def _create_claimed(db, discord, asset, final_png, bot_user_id):
    remote = await discord.list_emojis()
    reconciled, duplicates = await _reconcile_asset(db, asset, remote, bot_user_id, stale_after=10**9)
    if duplicates:
        return _result(reconciled, code="deterministic_name_conflict", duplicates=duplicates)
    if reconciled["status"] != "preparing" or reconciled.get("attempt_id") != asset.get("attempt_id"):
        return _result(reconciled, duplicates=duplicates)
    if len(remote) >= APPLICATION_EMOJI_LIMIT:
        failed = await _mark_failed(db, asset, "The application's total emoji capacity is full.")
        if failed["status"] == "ready":
            return _result(failed, code="reconciled")
        return _result(failed, code="emoji_capacity", message=failed["last_error"])
    try:
        created = await discord.create_emoji(asset["emoji_name"], final_png, bot_user_id)
    except DiscordAPIError as exc:
        if exc.uncertain:
            uncertain = await _mark_uncertain(db, asset, str(exc))
            if uncertain["status"] == "ready":
                return _result(
                    uncertain, code="create_uncertain",
                    message=("The create request had an uncertain outcome while this asset was bound by another operation. "
                             "Reconcile application emojis to check for a possible duplicate."))
            return _result(uncertain, code="create_uncertain", message=str(exc))
        failed = await _mark_failed(db, asset, str(exc))
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
                                final_png, remote, bot_user_id):
    """Prepare a new identity and clear recovery state only through this flow."""
    changed_hash = content_identity(final_png)
    new_asset, new_claimed, new_duplicates = await _insert_asset_or_claim(
        db, discord.application_id, guild_id, changed_hash, remote, bot_user_id)
    if new_claimed is None and new_asset["status"] in {"preparing", "uncertain"}:
        new_asset, new_duplicates = await _reconcile_asset(db, new_asset, remote, bot_user_id)
    if new_asset["status"] == "ready":
        if not any(str(e.get("id")) == str(new_asset.get("emoji_id")) for e in remote):
            remote = await discord.list_emojis()
        new_asset = await _refresh_ready_asset(db, new_asset, remote, bot_user_id)
    await _upsert_alias(db, discord.application_id, guild_id, source_url, changed_hash)
    if new_claimed is not None and new_asset["status"] == "preparing":
        outcome = await _create_claimed(db, discord, new_claimed, final_png, bot_user_id)
    else:
        outcome = _result(new_asset, code="remapped_changed_source", duplicates=new_duplicates)
        if outcome["status"] == "ready" and new_duplicates:
            outcome["code"] = "deterministic_name_conflict"
            outcome["message"] = (
                "Multiple Discord emojis share the deterministic name: "
                + ",".join(new_duplicates))
    outcome["remapped_from_content_hash"] = old_asset["content_hash"]
    if (outcome.get("status") == "ready"
            and outcome.get("code") in {"created", "ready", "reconciled", "remapped_changed_source"}):
        outcome["code"] = "remapped_changed_source"
        outcome["message"] = (
            "The source changed; the alias now uses the newly prepared hash. "
            "The previous asset was left unchanged.")
    return outcome, remote


async def _prepare_one(db, discord, guild_id, source_url, remote, bot_user_id,
                       retry_uncertain, acknowledgement):
    alias_asset = await _get_alias_asset(db, discord.application_id, guild_id, source_url)
    if alias_asset and alias_asset["status"] in {"preparing", "uncertain"}:
        original_asset = alias_asset
        recovery_allowed = False
        # Persist changed-source recovery before reconciliation can turn the
        # old asset ready. A later request must not inherit an unmarked ready
        # alias after an error, crash, or unacknowledged preparation attempt.
        final_png = await _download_and_normalize(source_url)
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
            return await _remap_changed_source(
                db, discord, guild_id, source_url, alias_asset, final_png, remote, bot_user_id)
        if alias_asset["status"] == "ready":
            return _result(alias_asset, code="reconciled"), remote
        claimed = await _claim_existing(db, alias_asset, guild_id, allow_uncertain=True)
        if claimed is None:
            current = await _get_asset(db, discord.application_id, alias_asset["content_hash"])
            return _result(current), remote
        return await _create_claimed(db, discord, claimed, final_png, bot_user_id), remote

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
        alias_asset = await _refresh_ready_asset(db, alias_asset, remote, bot_user_id)

    final_png = await _download_and_normalize(source_url)
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
            return await _remap_changed_source(
                db, discord, guild_id, source_url, old_alias_asset, final_png, remote, bot_user_id)
    asset = await _get_asset(db, discord.application_id, content_hash)
    duplicates = []
    claimed = None
    if asset is None:
        asset, claimed, duplicates = await _insert_asset_or_claim(
            db, discord.application_id, guild_id, content_hash, remote, bot_user_id)
    elif asset["status"] in {"preparing", "uncertain"}:
        asset, duplicates = await _reconcile_asset(db, asset, remote, bot_user_id)
    elif asset["status"] == "ready":
        if not any(str(e.get("id")) == str(asset.get("emoji_id")) for e in remote):
            remote = await discord.list_emojis()
        asset = await _refresh_ready_asset(db, asset, remote, bot_user_id)

    # Aliases may point at a non-ready asset, but only ready assets resolve.
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
        if (asset["status"] == "failed" and _valid_emoji_id(asset.get("emoji_id"))
                and any(str(e.get("id")) == str(asset["emoji_id"]) for e in remote)):
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
    return await _create_claimed(db, discord, claimed, final_png, bot_user_id), remote


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
                    by_url, order = {}, []
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
                        by_url[pid] = url
                    url_results = {}
                    for url in order:
                        try:
                            result, remote = await _prepare_one(
                                db, discord, guild_id, url, remote, bot_user_id,
                                retry_uncertain, acknowledge_duplicate_risk)
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
                            asset = await _refresh_ready_asset(db, asset, remote, bot_user_id)
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
                    if str(asset["emoji_id"]) == str(emoji_id):
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
