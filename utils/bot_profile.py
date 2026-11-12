import os
import base64
import ipaddress
import socket
from urllib.parse import urljoin, urlparse
import aiosqlite
import requests
from database import DB_PATH

DISCORD_API = "https://discord.com/api/v10"

# ═══════════════════════════════════════════════════════════════════════
# PER-SERVER BOT PROFILE
#
# Discord's "Modify Current Member" endpoint
# (PATCH /guilds/{guild.id}/members/@me) currently accepts FOUR fields
# for whichever token calls it — including a bot's own token:
#
#   nick    — string, this server's nickname
#   avatar  — base64 data-URI image, this server's member avatar
#   banner  — base64 data-URI image, this server's member banner
#   bio     — string (~190 chars, same ballpark as Discord's own
#             profile bio), this server's member "about me"
#
# All four are genuinely scoped to ONE guild — none of this touches the
# bot's global identity, and none of it is visible in any other server.
# Confirmed directly against Discord's current API docs
# (docs.discord.com/developers/resources/guild) before building this.
#
# avatar/banner are NOT accepted as plain URLs by this endpoint (unlike
# some other Discord upload fields) — they must be base64-encoded image
# data. _download_image_as_data_uri() does that conversion so the
# dashboard can still offer a simple "paste a URL" field, matching every
# other image field already in this codebase (embed builder, welcome
# embeds), instead of needing new file-upload infrastructure.
# ═══════════════════════════════════════════════════════════════════════

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # conservative cap; Discord's own limit may differ by format


async def get_guild_bot_profile(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT nickname, avatar_url, banner_url, bio, updated_at
            FROM guild_bot_profile WHERE guild_id = ?
        """, (guild_id,))
        row = await cursor.fetchone()
    if not row:
        return {"guild_id": guild_id, "nickname": None, "avatar_url": None,
                "banner_url": None, "bio": None, "updated_at": None}
    return {
        "guild_id": guild_id, "nickname": row[0], "avatar_url": row[1],
        "banner_url": row[2], "bio": row[3], "updated_at": row[4],
    }


async def save_guild_bot_profile(guild_id: int, nickname: str | None,
                                  avatar_url: str | None, banner_url: str | None,
                                  bio: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO guild_bot_profile
                (guild_id, nickname, avatar_url, banner_url, bio, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                nickname   = excluded.nickname,
                avatar_url = excluded.avatar_url,
                banner_url = excluded.banner_url,
                bio        = excluded.bio,
                updated_at = CURRENT_TIMESTAMP
        """, (guild_id, nickname or None, avatar_url or None,
              banner_url or None, bio or None))
        await db.commit()


def _validate_public_image_url(url: str) -> str | None:
    """Reject non-HTTP(S), credentialed, localhost, or non-public IP targets (SSRF guard)."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        return "invalid image URL"
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return "invalid image URL"
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "image URL must use http:// or https://"
    if parsed.username or parsed.password:
        return "image URL cannot contain credentials"
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in ("localhost", "ip6-localhost") or host.endswith((".local", ".internal", ".localhost")):
        return "private or local hosts are not allowed"

    def _is_unsafe_ip(ip_obj) -> bool:
        return (
            not ip_obj.is_global
            or ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )

    try:
        literal_ip = ipaddress.ip_address(host)
        if _is_unsafe_ip(literal_ip):
            return "private or non-public IP addresses are not allowed"
        return None
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except Exception:
        return "couldn't resolve image host"
    if not infos:
        return "couldn't resolve image host"
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            resolved_ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return "invalid resolved address"
        if _is_unsafe_ip(resolved_ip):
            return "private or non-public IP addresses are not allowed"
    return None


def _download_image_as_data_uri(url: str, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[str | None, str | None]:
    """
    Downloads an image and returns (data_uri, error). Validates public
    HTTP(S) targets across redirects and streams with a hard byte cap.
    """
    current_url = (url or "").strip()
    resp = None
    for _ in range(4):
        url_err = _validate_public_image_url(current_url)
        if url_err:
            return None, url_err
        try:
            resp = requests.get(current_url, timeout=10, stream=True, allow_redirects=False)
        except Exception as e:
            return None, f"couldn't reach that URL ({e})"
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            if not loc:
                return None, "redirect missing Location header"
            current_url = urljoin(current_url, loc)
            continue
        break
    else:
        return None, "too many redirects"

    if resp is None or resp.status_code != 200:
        return None, f"URL returned HTTP {resp.status_code if resp else 'unknown'}"

    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        return None, f"that URL isn't an image (Content-Type: {content_type or 'unknown'})"

    chunks = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                return None, f"image is too large — keep it under {max_bytes // (1024*1024)}MB"
            chunks.append(chunk)
    except Exception as e:
        return None, f"download failed ({e})"

    data = b"".join(chunks)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{b64}", None


def apply_bot_profile_via_rest(guild_id: int, nickname: str | None,
                                avatar_url: str | None, banner_url: str | None,
                                bio: str | None) -> dict:
    """
    Applies nickname/avatar/banner/bio for the bot's OWN guild member
    row in ONE guild, in a single PATCH call. Fields left empty/None
    are explicitly cleared — this mirrors how every other settings form
    in this dashboard round-trips its FULL current state on save, not a
    diff — EXCEPT when an avatar/banner URL was given but couldn't be
    downloaded/encoded: in that case the field is left out of the
    request entirely (whatever's currently live stays live) and the
    specific failure is reported back, rather than wiping a working
    avatar/banner because of an unrelated typo in the other field.

    Returns {"success": bool, "applied": [field names], "errors": {field: message}}.
    """
    bot_token = os.getenv("DISCORD_TOKEN", "")
    if not bot_token:
        return {"success": False, "applied": [], "errors": {"_general": "Bot token not configured"}}

    body: dict = {
        "nick": nickname or None,
        "bio": bio or None,
    }
    errors: dict = {}

    if avatar_url:
        data_uri, err = _download_image_as_data_uri(avatar_url)
        if err:
            errors["avatar"] = err
        else:
            body["avatar"] = data_uri
    else:
        body["avatar"] = None

    if banner_url:
        data_uri, err = _download_image_as_data_uri(banner_url)
        if err:
            errors["banner"] = err
        else:
            body["banner"] = data_uri
    else:
        body["banner"] = None

    try:
        resp = requests.patch(
            f"{DISCORD_API}/guilds/{guild_id}/members/@me",
            headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
    except Exception as e:
        errors["_general"] = str(e)
        return {"success": False, "applied": [], "errors": errors}

    if resp.status_code == 200:
        applied = [k for k in ("nick", "bio", "avatar", "banner") if k in body]
        return {"success": True, "applied": applied, "errors": errors}

    try:
        detail = resp.json().get("message", resp.text)
    except Exception:
        detail = resp.text
    errors["_general"] = f"Discord API error {resp.status_code}: {detail}"
    return {"success": False, "applied": [], "errors": errors}


def get_live_bot_member(guild_id: int) -> dict | None:
    """
    Reads the bot's own CURRENT guild member object straight from
    Discord — the real, live nick/avatar/banner/bio, plus the bot's
    global (non-guild-specific) username/avatar for comparison. Used
    by the dashboard's "Current State" panel so it can never drift
    from what Discord itself is actually showing right now.

    guild_banner_url's CDN path follows the same
    /guilds/{guild_id}/users/{user_id}/{kind}/{hash} pattern confirmed
    for guild member avatars; Discord's docs don't spell out the
    banner path explicitly, so this is a well-supported inference from
    their naming convention, not a directly-confirmed URL. The
    dashboard template has an onerror fallback so a wrong guess here
    just hides the preview instead of showing a broken image.
    """
    bot_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not bot_token:
        return None
    try:
        from dashboard.utils.role_validation import resolve_bot_user_id

        bot_user_id, id_err = resolve_bot_user_id(bot_token)
        if id_err or not bot_user_id:
            return None
        resp = requests.get(
            f"{DISCORD_API}/guilds/{guild_id}/members/{bot_user_id}",
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        user = data.get("user", {}) or {}
        bot_user_id = str(user.get("id") or bot_user_id)

        def _guild_asset_url(kind: str, hash_: str | None) -> str | None:
            if not hash_ or not bot_user_id:
                return None
            ext = "gif" if hash_.startswith("a_") else "png"
            return (f"https://cdn.discordapp.com/guilds/{guild_id}/users/"
                    f"{bot_user_id}/{kind}/{hash_}.{ext}")

        global_avatar_hash = user.get("avatar")
        global_avatar_url = None
        if global_avatar_hash and bot_user_id:
            ext = "gif" if global_avatar_hash.startswith("a_") else "png"
            global_avatar_url = f"https://cdn.discordapp.com/avatars/{bot_user_id}/{global_avatar_hash}.{ext}"

        return {
            "nick": data.get("nick"),
            "username": user.get("username"),
            "global_avatar_url": global_avatar_url,
            "guild_avatar_url": _guild_asset_url("avatars", data.get("avatar")),
            "guild_banner_url": _guild_asset_url("banners", data.get("banner")),
            "bio": data.get("bio"),
        }
    except Exception as e:
        print(f"[BOTPROFILE] get_live_bot_member error: {e}")
        return None
