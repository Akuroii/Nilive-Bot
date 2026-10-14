"""
Server-side validation for a Discord message payload (the Embed Builder send
path).

Why this exists: the send route used to check three things — embeds is a
list, ≤10 embeds, attachments ≤10 with a ≤25MB total — and forwarded
everything else straight to Discord. Every other rule lived only in the
browser, so anything that reached the route without going through the page
(curl, an old tab, a future client, a bug) was validated by Discord with
`{"code": 50035, "errors": {"embeds.0.title": ...}}` — an error the admin
cannot act on, after a full file upload.

The rules here are Discord's own, from utils/discord_limits.py (which cites
the docs for every number). The shape is Discord's too:

    ok, errors = validate_message(payload, files)
    errors == [{"path": "embeds.0.title",
                "message": "Embed 1 title is 300 characters; Discord's limit is 256."}]

...so the browser can highlight the exact field (the review's requirement:
mirror Discord's path-keyed 400 shape), while the first message doubles as
the plain-text `error` string older clients already display.

Deliberatly strict about the things Discord punishes, deliberately quiet
about anything else: unknown top-level keys are ignored (Discord rejects
them itself if it cares), and `attachment://name` references are rejected
unless a matching uploaded file exists in THIS request — a dangling
attachment reference is exactly the "silently broken embed" the rebuild is
supposed to make impossible.

Pure Python: no Flask, no Discord, no imports beyond the limits table and
the standard library, so scripts/test_embed_schema.py can exercise it
directly.
"""

from urllib.parse import urlparse

from utils.discord_limits import (
    ATTACHMENTS_MAX,
    EMBED_AUTHOR_NAME_MAX,
    EMBED_COLOR_MAX,
    EMBED_DESCRIPTION_MAX,
    EMBED_FIELD_NAME_MAX,
    EMBED_FIELD_VALUE_MAX,
    EMBED_FIELDS_MAX,
    EMBED_FOOTER_TEXT_MAX,
    EMBED_TITLE_MAX,
    EMBED_URL_MAX,
    MESSAGE_CONTENT_MAX,
    MESSAGE_EMBEDS_MAX,
    MESSAGE_EMBED_TOTAL_CHARS_MAX,
    attachment_max_file_bytes,
    attachment_total_bytes_max,
    human_bytes,
)

# Keys Discord itself accepts on an embed. Anything else is dropped before
# the payload leaves this module (see strip_unknown_embed_keys) so an
# editor-only helper key can never be uploaded as part of an embed.
EMBED_KEYS = {
    "title", "description", "url", "timestamp", "color",
    "footer", "image", "thumbnail", "author", "fields",
}
IMAGE_KEYS = ("image", "thumbnail")
ICON_URL_KEYS = ("author.icon_url", "footer.icon_url")


def _err(errors: list, path: str, message: str) -> None:
    errors.append({"path": path, "message": message})


def _is_http_url(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _is_attachment_ref(value) -> bool:
    return isinstance(value, str) and value.startswith("attachment://")


def _text_len(value) -> int:
    return len(value) if isinstance(value, str) else 0


def embed_char_count(embed: dict) -> int:
    """
    Discord's "sum of all characters in an embed structure must not exceed
    6000" — title, description, every field name/value, footer text and
    author name. Counted the way Discord counts it so a payload that passes
    here cannot be rejected for size at the API.
    """
    total = _text_len(embed.get("title")) + _text_len(embed.get("description"))
    footer = embed.get("footer")
    if isinstance(footer, dict):
        total += _text_len(footer.get("text"))
    author = embed.get("author")
    if isinstance(author, dict):
        total += _text_len(author.get("name"))
    for field in embed.get("fields") or []:
        if isinstance(field, dict):
            total += _text_len(field.get("name")) + _text_len(field.get("value"))
    return total


def _valid_timestamp(value) -> bool:
    from datetime import datetime
    if not isinstance(value, str) or not value.strip():
        return False
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        datetime.fromisoformat(raw)
        return True
    except ValueError:
        return False


def validate_embed(embed, index: int, files_by_name: dict) -> list:
    """One embed → list of {path, message}. `embeds.<i>` paths, 0-based."""
    errors: list = []
    prefix = f"embeds.{index}"
    label = f"Embed {index + 1}"

    if not isinstance(embed, dict):
        _err(errors, prefix, f"{label} is not an object.")
        return errors

    title = embed.get("title")
    if title is not None and not isinstance(title, str):
        _err(errors, f"{prefix}.title", f"{label} title must be text.")
    elif _text_len(title) > EMBED_TITLE_MAX:
        _err(errors, f"{prefix}.title",
             f"{label} title is {_text_len(title)} characters; Discord's limit is {EMBED_TITLE_MAX}.")

    description = embed.get("description")
    if description is not None and not isinstance(description, str):
        _err(errors, f"{prefix}.description", f"{label} description must be text.")
    elif _text_len(description) > EMBED_DESCRIPTION_MAX:
        _err(errors, f"{prefix}.description",
             f"{label} description is {_text_len(description)} characters; "
             f"Discord's limit is {EMBED_DESCRIPTION_MAX}.")

    url = embed.get("url")
    if url not in (None, ""):
        if not isinstance(url, str) or len(url) > EMBED_URL_MAX:
            _err(errors, f"{prefix}.url",
                 f"{label} URL must be a link of at most {EMBED_URL_MAX} characters.")
        elif not (_is_http_url(url) or _is_attachment_ref(url)):
            _err(errors, f"{prefix}.url",
                 f"{label} URL must start with http:// or https://.")

    timestamp = embed.get("timestamp")
    if timestamp not in (None, "") and not _valid_timestamp(timestamp):
        _err(errors, f"{prefix}.timestamp",
             f"{label} timestamp must be an ISO 8601 date/time (e.g. 2026-09-23T18:00:00+00:00).")

    color = embed.get("color")
    if color not in (None, ""):
        if isinstance(color, bool) or not isinstance(color, int):
            _err(errors, f"{prefix}.color", f"{label} colour must be an integer.")
        elif not 0 <= color <= EMBED_COLOR_MAX:
            _err(errors, f"{prefix}.color",
                 f"{label} colour must be between 0 and {EMBED_COLOR_MAX}.")

    author = embed.get("author")
    if author not in (None, ""):
        if not isinstance(author, dict):
            _err(errors, f"{prefix}.author", f"{label} author must be an object.")
        else:
            name = author.get("name")
            if _text_len(name) > EMBED_AUTHOR_NAME_MAX:
                _err(errors, f"{prefix}.author.name",
                     f"{label} author name is {_text_len(name)} characters; "
                     f"Discord's limit is {EMBED_AUTHOR_NAME_MAX}.")
            needs_name = author.get("url") or author.get("icon_url")
            if needs_name and not _text_len(name):
                # Discord requires author.name whenever url/icon_url is set.
                _err(errors, f"{prefix}.author.name",
                     f"{label} author name is required when an author icon or link is set.")
            a_url = author.get("url")
            if a_url not in (None, "") and not _is_http_url(a_url):
                _err(errors, f"{prefix}.author.url",
                     f"{label} author link must start with http:// or https://.")
            a_icon = author.get("icon_url")
            if a_icon not in (None, ""):
                _check_media_url(errors, f"{prefix}.author.icon_url", a_icon,
                                 f"{label} author icon", files_by_name)

    footer = embed.get("footer")
    if footer not in (None, ""):
        if not isinstance(footer, dict):
            _err(errors, f"{prefix}.footer", f"{label} footer must be an object.")
        else:
            text = footer.get("text")
            if _text_len(text) > EMBED_FOOTER_TEXT_MAX:
                _err(errors, f"{prefix}.footer.text",
                     f"{label} footer text is {_text_len(text)} characters; "
                     f"Discord's limit is {EMBED_FOOTER_TEXT_MAX}.")
            if footer.get("icon_url") not in (None, "") and not _text_len(text):
                _err(errors, f"{prefix}.footer.text",
                     f"{label} footer text is required when a footer icon is set.")
            icon = footer.get("icon_url")
            if icon not in (None, ""):
                _check_media_url(errors, f"{prefix}.footer.icon_url", icon,
                                 f"{label} footer icon", files_by_name)

    for key, label_word in (("image", "image"), ("thumbnail", "thumbnail")):
        media = embed.get(key)
        if media in (None, ""):
            continue
        if isinstance(media, str):
            _err(errors, f"{prefix}.{key}",
                 f"{label} {label_word} must be an object with a url.")
            continue
        if not isinstance(media, dict):
            _err(errors, f"{prefix}.{key}", f"{label} {label_word} must be an object with a url.")
            continue
        media_url = media.get("url")
        if not media_url:
            _err(errors, f"{prefix}.{key}.url",
                 f"{label} {label_word} is missing its url.")
        else:
            _check_media_url(errors, f"{prefix}.{key}.url", media_url,
                             f"{label} {label_word}", files_by_name)

    fields = embed.get("fields")
    if fields not in (None, ""):
        if not isinstance(fields, list):
            _err(errors, f"{prefix}.fields", f"{label} fields must be a list.")
        else:
            if len(fields) > EMBED_FIELDS_MAX:
                _err(errors, f"{prefix}.fields",
                     f"{label} has {len(fields)} fields; Discord's limit is {EMBED_FIELDS_MAX}.")
            for fi, field in enumerate(fields):
                fpath = f"{prefix}.fields.{fi}"
                if not isinstance(field, dict):
                    _err(errors, fpath, f"{label} field {fi + 1} is not an object.")
                    continue
                fname, fvalue = field.get("name"), field.get("value")
                if not _text_len(fname):
                    _err(errors, f"{fpath}.name",
                         f"{label} field {fi + 1} needs a name.")
                elif len(fname) > EMBED_FIELD_NAME_MAX:
                    _err(errors, f"{fpath}.name",
                         f"{label} field {fi + 1} name is {len(fname)} characters; "
                         f"Discord's limit is {EMBED_FIELD_NAME_MAX}.")
                if isinstance(fvalue, str) and len(fvalue) > EMBED_FIELD_VALUE_MAX:
                    _err(errors, f"{fpath}.value",
                         f"{label} field {fi + 1} value is {len(fvalue)} characters; "
                         f"Discord's limit is {EMBED_FIELD_VALUE_MAX}.")
                elif fvalue is not None and not isinstance(fvalue, str):
                    _err(errors, f"{fpath}.value", f"{label} field {fi + 1} value must be text.")

    total_chars = embed_char_count(embed)
    if total_chars > MESSAGE_EMBED_TOTAL_CHARS_MAX:
        _err(errors, prefix,
             f"{label} holds {total_chars} characters in total; Discord's per-embed limit "
             f"across all of its text is {MESSAGE_EMBED_TOTAL_CHARS_MAX}.")

    return errors


def _check_media_url(errors, path, value, label, files_by_name) -> None:
    """http(s) (and for images/author/footer icons) or a matching attachment."""
    if _is_attachment_ref(value):
        name = value[len("attachment://"):]
        if not name:
            _err(errors, path, f"{label} references an attachment with no file name.")
        elif name not in files_by_name:
            _err(errors, path,
                 f"{label} points at the attachment \"{name}\", but no file with that name "
                 f"is being uploaded — reattach the file before sending.")
        return
    if not _is_http_url(value):
        _err(errors, path, f"{label} must start with http:// or https://.")


def validate_message(payload, files=None):
    """
    Validate a whole Create Message payload.

    payload: {"content": str|None, "embeds": [embed, ...]}
    files:   [{"name": str, "size": int}, ...] — the uploaded files, or None.

    Returns (ok, errors). `errors` is ordered by importance (message-level
    first, then embed by embed) and each entry is {"path", "message"}.
    """
    errors: list = []
    files = list(files or [])
    files_by_name = {}
    for f in files:
        name = (f or {}).get("name") or ""
        if name:
            files_by_name[name] = f
            # Discord matches attachment:// against the file name only.
            files_by_name[name.split("/")[-1].split("\\")[-1]] = f

    if not isinstance(payload, dict):
        return False, [{"path": "", "message": "Message payload must be an object."}]

    content = payload.get("content")
    content_is_text = not (content is not None and not isinstance(content, str))
    if not content_is_text:
        _err(errors, "content", "Message content must be text.")
    if _text_len(content) > MESSAGE_CONTENT_MAX:
        _err(errors, "content",
             f"Message content is {_text_len(content)} characters; Discord's limit is "
             f"{MESSAGE_CONTENT_MAX}.")
    has_content = (not content_is_text) or _text_len(content) > 0

    embeds = payload.get("embeds")
    if embeds in (None, ""):
        embeds = []
    has_embeds = bool(embeds)
    if not isinstance(embeds, list):
        _err(errors, "embeds", "Embeds must be a list.")
        embeds = []
    elif len(embeds) > MESSAGE_EMBEDS_MAX:
        # Discord rejects the whole message; reporting every field of every
        # embed on top of that would bury the one error that matters.
        _err(errors, "embeds",
             f"A message can carry at most {MESSAGE_EMBEDS_MAX} embeds; this one has {len(embeds)}.")
        embeds = []

    for i, embed in enumerate(embeds):
        errors.extend(validate_embed(embed, i, files_by_name))

    # ── Attachments ───────────────────────────────────────────────────
    if len(files) > ATTACHMENTS_MAX:
        _err(errors, "files",
             f"A message can carry at most {ATTACHMENTS_MAX} attachments; this one has {len(files)}.")
    total = 0
    total_max = attachment_total_bytes_max()
    oversized_single = None
    for f in files:
        size = f.get("size")
        if not isinstance(size, int) or size < 0:
            continue
        total += size
        if size > total_max and oversized_single is None:
            oversized_single = f
    if oversized_single is not None:
        _err(errors, "files",
             f"\"{oversized_single.get('name') or 'file'}\" is "
             f"{human_bytes(oversized_single.get('size'))} — over the {human_bytes(total_max)} "
             f"maximum this dashboard can upload in one message.")
    elif total > total_max:
        _err(errors, "files",
             f"Attachments total {human_bytes(total)}; the maximum is {human_bytes(total_max)}.")

    if not has_content and not has_embeds and not files:
        _err(errors, "", "Nothing to send — add content, an embed, or an attachment.")

    return (len(errors) == 0), errors


def strip_unknown_embed_keys(embed: dict) -> dict:
    """
    Drop editor-only / unknown keys from one embed.

    The browser's editor carries local bookkeeping on some objects; Discord
    rejects unknown fields with a 400, so the send path strips anything it
    does not recognise instead of trusting the client to have cleaned up.
    """
    if not isinstance(embed, dict):
        return {}
    return {k: v for k, v in embed.items() if k in EMBED_KEYS}


def first_error_message(errors) -> str:
    """The message older clients display as `error` (they only read a string)."""
    for e in errors or []:
        if e.get("message"):
            return e["message"]
    return "Invalid message payload"


def validate_for_discord(payload, files=None):
    """
    validate_message + the cleanup Discord needs. Returns
    (clean_payload, ok, errors) so the route can send exactly what was
    validated rather than the raw body.
    """
    ok, errors = validate_message(payload, files)
    clean = {}
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, str) and content:
            clean["content"] = content
        embeds = payload.get("embeds")
        if isinstance(embeds, list):
            clean["embeds"] = [strip_unknown_embed_keys(e) for e in embeds if isinstance(e, dict)]
    return clean, ok, errors


def advisory_file_warnings(files) -> list:
    """
    Per-file advisory notes (never blocking): a file above the free-tier
    per-file cap may be rejected by Discord on a non-boosted server. Returned
    separately so the route can surface them without failing the send.
    """
    limit = attachment_max_file_bytes()
    notes = []
    for f in files or []:
        size = (f or {}).get("size")
        if isinstance(size, int) and size > limit:
            notes.append(
                f"\"{(f or {}).get('name') or 'file'}\" is {human_bytes(size)} — above the "
                f"{human_bytes(limit)} per-file limit on non-boosted servers. Boosted servers "
                f"and Nitro accounts allow more; Discord will refuse it if this one does not.")
    return notes
