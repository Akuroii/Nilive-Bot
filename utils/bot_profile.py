import os
import base64
import ipaddress
import socket
from urllib.parse import urljoin, urlparse
import aiosqlite
import requests
from requests.adapters import HTTPAdapter
import urllib3.connection
import urllib3.connectionpool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from database import DB_PATH

DISCORD_API = "https://discord.com/api/v10"
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)

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


def _is_unsafe_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        not ip_obj.is_global
        or ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or getattr(ip_obj, "scope_id", None) is not None
    ):
        return True
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if (
            ip_obj.ipv4_mapped is not None
            or ip_obj.sixtofour is not None
            or ip_obj.teredo is not None
            or any(ip_obj in net for net in _NAT64_NETWORKS)
        ):
            return True
    return False


def _resolve_public_image_target(url: str) -> tuple[dict | None, str | None]:
    """
    Validate an HTTP(S) image URL, resolve DNS once, ensure EVERY resolved IP
    in the answer set is globally routable, and return pinned connection info
    so the subsequent socket connect never re-resolves DNS (mitigating DNS
    rebinding / TOCTOU while keeping original Host + TLS SNI/hostname checks).
    """
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        return None, "invalid image URL"
    raw = url.strip()
    if "\\" in raw or any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in raw):
        return None, "invalid image URL"
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        return None, "invalid image URL"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        return None, "image URL must use http:// or https://"
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return None, "image URL cannot contain credentials or fragments"
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if not (1 <= effective_port <= 65535):
        return None, "invalid image URL port"

    raw_host = parsed.hostname.strip().rstrip(".")
    lower_host = raw_host.lower()
    if lower_host in ("localhost", "ip6-localhost") or lower_host.endswith((".local", ".internal", ".localhost")):
        return None, "private or local hosts are not allowed"

    try:
        literal_ip = ipaddress.ip_address(raw_host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if _is_unsafe_ip(literal_ip):
            return None, "private or non-public IP addresses are not allowed"
        return {
            "url": raw,
            "scheme": scheme,
            "host": str(literal_ip),
            "port": effective_port,
            "pinned_ips": [str(literal_ip)],
        }, None

    try:
        ascii_host = raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None, "invalid image URL host"

    try:
        infos = socket.getaddrinfo(ascii_host, effective_port, type=socket.SOCK_STREAM)
    except Exception:
        return None, "couldn't resolve image host"
    if not infos:
        return None, "couldn't resolve image host"

    pinned_ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            resolved_ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return None, "invalid resolved address"
        if _is_unsafe_ip(resolved_ip):
            return None, "private or non-public IP addresses are not allowed"
        ip_str = str(resolved_ip)
        if ip_str not in pinned_ips:
            pinned_ips.append(ip_str)

    if not pinned_ips:
        return None, "couldn't resolve image host"

    return {
        "url": raw,
        "scheme": scheme,
        "host": ascii_host,
        "port": effective_port,
        "pinned_ips": pinned_ips,
    }, None


def _validate_public_image_url(url: str) -> str | None:
    """Reject non-HTTP(S), credentialed, localhost, or non-public IP targets (SSRF guard)."""
    _, err = _resolve_public_image_target(url)
    return err


def _build_pinned_adapter(expected_host: str, expected_port: int, pinned_ips: list[str]) -> HTTPAdapter:
    """
    Create a single-hop HTTPAdapter whose underlying urllib3 connection classes
    dial ONLY the pre-validated `pinned_ips` while keeping `self.host` set to
    `expected_host` so HTTP Host, TLS SNI, and certificate hostname verification
    all validate against the original hostname without a second DNS lookup.
    """
    norm_expected = expected_host.strip("[]").lower().rstrip(".")

    def _dial_pinned(conn) -> socket.socket:
        conn_host = (conn.host or "").strip("[]").lower().rstrip(".")
        if conn_host != norm_expected or int(conn.port) != int(expected_port):
            raise NewConnectionError(
                conn,
                f"Refused unpinned connection target {conn.host}:{conn.port}",
            )
        last_err: Exception | None = None
        for ip_str in pinned_ips:
            ip_obj = ipaddress.ip_address(ip_str)
            af = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
            sock: socket.socket | None = None
            try:
                sock = socket.socket(af, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if conn.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT and conn.timeout is not None:
                    sock.settimeout(conn.timeout)
                if conn.source_address:
                    sock.bind(conn.source_address)
                target_addr = (ip_str, int(conn.port), 0, 0) if af == socket.AF_INET6 else (ip_str, int(conn.port))
                sock.connect(target_addr)
                return sock
            except socket.timeout:
                if sock is not None:
                    sock.close()
                last_err = ConnectTimeoutError(
                    conn,
                    f"Connection to {conn.host} ({ip_str}) timed out.",
                )
            except OSError as exc:
                if sock is not None:
                    sock.close()
                last_err = NewConnectionError(
                    conn,
                    f"Failed to establish connection to {conn.host} ({ip_str}): {exc}",
                )
        assert last_err is not None
        raise last_err

    class _PinnedHTTPConnection(urllib3.connection.HTTPConnection):
        def _new_conn(self) -> socket.socket:
            return _dial_pinned(self)

    class _PinnedHTTPSConnection(urllib3.connection.HTTPSConnection):
        def _new_conn(self) -> socket.socket:
            return _dial_pinned(self)

    class _PinnedHTTPPool(urllib3.connectionpool.HTTPConnectionPool):
        ConnectionCls = _PinnedHTTPConnection

    class _PinnedHTTPSPool(urllib3.connectionpool.HTTPSConnectionPool):
        ConnectionCls = _PinnedHTTPSConnection

    class _PinnedHostAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
            self.poolmanager.pool_classes_by_scheme = {
                "http": _PinnedHTTPPool,
                "https": _PinnedHTTPSPool,
            }

    return _PinnedHostAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)


def _download_image_as_data_uri(
    url: str,
    max_bytes: int = MAX_IMAGE_BYTES,
    verify_tls: bool | str = True,
) -> tuple[str | None, str | None]:
    """
    Downloads an image and returns (data_uri, error). Validates public
    HTTP(S) targets on every hop, pins resolved public IPs per hop to prevent
    DNS rebinding (preserving Host, TLS SNI, and hostname verification),
    streams with a hard byte cap, and closes responses/sockets deterministically.
    """
    current_url = (url or "").strip()
    for _ in range(4):
        target, url_err = _resolve_public_image_target(current_url)
        if url_err or target is None:
            return None, url_err or "invalid image URL"

        sess = requests.Session()
        sess.trust_env = False
        adapter = _build_pinned_adapter(target["host"], target["port"], target["pinned_ips"])
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)

        resp = None
        try:
            try:
                resp = sess.get(
                    target["url"],
                    timeout=(5, 10),
                    stream=True,
                    allow_redirects=False,
                    verify=verify_tls,
                    headers={"User-Agent": "NiliveBot-ProfileImage/1.0"},
                )
            except Exception as e:
                return None, f"couldn't reach that URL ({e})"

            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if not loc:
                    return None, "redirect missing Location header"
                current_url = urljoin(current_url, loc)
                continue

            if resp.status_code != 200:
                return None, f"URL returned HTTP {resp.status_code}"

            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                return None, f"that URL isn't an image (Content-Type: {content_type or 'unknown'})"

            cl_header = resp.headers.get("Content-Length")
            if cl_header:
                try:
                    if int(cl_header) > max_bytes:
                        return None, f"image is too large — keep it under {max_bytes // (1024*1024)}MB"
                except ValueError:
                    pass

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
        finally:
            if resp is not None:
                resp.close()
            sess.close()

    return None, "too many redirects"


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
