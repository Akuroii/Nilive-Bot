"""
Discord limits — the ONE place the dashboard reads them from.

Before this file the same numbers were hand-copied into the builder page
(MAX_EMBEDS / MAX_ATTACHMENTS / MAX_CONTENT), the send route
(MAX_EMBEDS / MAX_ATTACHMENTS / MAX_TOTAL_ATTACHMENT_BYTES) and
embed-composer.js (4096, 25 fields...). Copies drift, and a drifted copy is
how a payload that passes every client check dies at Discord with
"Invalid Form Body" and no useful explanation.

Every constant below is quoted from Discord's own documentation (or, where
noted, from a documented *policy* that is not an API constant at all — those
are advisory and configurable, never hard blocks):

  * Create Message — "The maximum request size when sending a message is
    25 MiB", "content: Message contents (up to 2000 characters)",
    "embeds: array of embed objects, Up to 10 rich embeds (up to 6000
    characters)".
      https://docs.discord.com/developers/resources/message
  * Embed object — title 256, description 4096, 25 fields, field name 256,
    field value 1024, footer text 2048, author name 256, "the sum of all
    characters in an embed structure must not exceed 6000 characters";
    image/thumbnail url accept http(s) or "attachment://filename".
      https://docs.discord.com/developers/resources/message#embed-object
  * Message components — 5 buttons XOR 1 select per action row, custom_id
    1-100 chars, button label 80, button url 512, string select 25 options,
    option label/value 100, option description 100, placeholder 150,
    min/max_values 25.
      https://docs.discord.com/developers/interactions/message-components
  * Attachments — per-FILE cap is a platform policy that has moved several
    times (8 MiB -> 25 MiB -> 10 MiB -> 20 MiB for free/non-boosted
    accounts, with 50 MiB / 500 MiB for Nitro tiers and higher caps on
    boosted servers). It is NOT a fixed API constant, so
    `attachment_max_file_bytes()` is advisory + env-overridable and callers
    must WARN, never block, on it.
      https://docs.discord.com/developers/reference (Uploading Files)

Deliberately NOT here: Components V2 (`IS_COMPONENTS_V2`) budgets — that
feature replaces `content`/`embeds` and is out of scope for the first
implementation (see EMBED_BUILDER_PLAN.md §5.4). The constants exist so the
later phase has one place to add them to.

Pure Python, no imports, no side effects: importable from Flask routes,
cogs, and test scripts alike.
"""

import os

# ── Message level ──────────────────────────────────────────────────────
MESSAGE_CONTENT_MAX = 2000
MESSAGE_EMBEDS_MAX = 10
MESSAGE_EMBED_TOTAL_CHARS_MAX = 6000
# "The maximum request size when sending a message is 25 MiB" — this is the
# multipart body ceiling, so it is also the only hard cap on total uploaded
# bytes. The dashboard has always enforced it; it now comes from here, with
# a little headroom for the multipart envelope, exactly as before.
MESSAGE_REQUEST_BYTES_MAX = 25 * 1024 * 1024
# Slack kept for the JSON envelope (boundary strings, field names, the
# payload_json part) so a request that is exactly at the file-bytes limit
# still fits inside Discord's 25 MiB request ceiling.
MESSAGE_REQUEST_ENVELOPE_BYTES = 64 * 1024

# ── Attachments ────────────────────────────────────────────────────────
ATTACHMENTS_MAX = 10
# Advisory only (see module docstring): the free/non-boosted per-file cap
# since Discord's August 2026 change (10 MiB -> 20 MiB). Boosted servers and
# Nitro accounts allow more, so exceeding this NEVER blocks a send — the UI
# warns, and Discord's real answer (413) is what decides.
ATTACHMENT_FILE_BYTES_ADVISORY = 20 * 1024 * 1024
ATTACHMENT_FILE_BYTES_ENV = "NERO_ATTACHMENT_MAX_BYTES"

# ── Embed limits ───────────────────────────────────────────────────────
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_FIELDS_MAX = 25
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_FOOTER_TEXT_MAX = 2048
EMBED_AUTHOR_NAME_MAX = 256
EMBED_COLOR_MAX = 0xFFFFFF
# Discord renders embeds with a url in a different accent and de-duplicates
# identical urls within one message — both worth knowing before Phase 2.
EMBED_URL_MAX = 2048

# ── Components (documented now, enforced in Phase 3) ───────────────────
COMPONENT_ROWS_MAX = 5
BUTTONS_PER_ROW_MAX = 5
BUTTON_LABEL_MAX = 80
BUTTON_URL_MAX = 512
CUSTOM_ID_MAX = 100
SELECT_OPTIONS_MAX = 25
SELECT_OPTION_LABEL_MAX = 100
SELECT_OPTION_VALUE_MAX = 100
SELECT_OPTION_DESCRIPTION_MAX = 100
SELECT_PLACEHOLDER_MAX = 150


def attachment_max_file_bytes() -> int:
    """
    The advisory per-file cap, overridable by deployment.

    Discord's own limit depends on the account's Nitro tier and on whether
    the server is boosted, and it is the SERVER's limit that applies to a
    bot upload — so a hard-coded number here would be wrong for boosted
    servers and would block perfectly sendable files. Set
    NERO_ATTACHMENT_MAX_BYTES to the guild's real cap (in bytes) to make the
    warning exact; unset, the free/non-boosted default is used.
    """
    raw = os.getenv(ATTACHMENT_FILE_BYTES_ENV, "")
    if raw:
        try:
            value = int(str(raw).strip())
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return ATTACHMENT_FILE_BYTES_ADVISORY


def attachment_total_bytes_max() -> int:
    """Hard cap on the SUM of uploaded file bytes (see module docstring)."""
    return MESSAGE_REQUEST_BYTES_MAX - MESSAGE_REQUEST_ENVELOPE_BYTES


def limits_payload() -> dict:
    """
    Machine-readable limits for the client (GET /api/embedbuilder/limits).

    The page reads these instead of its own literals so that when Discord
    moves a number, one edit here updates the counters, the validation
    messages and the server-side checks together.
    """
    return {
        "message": {
            "content_max": MESSAGE_CONTENT_MAX,
            "embeds_max": MESSAGE_EMBEDS_MAX,
            "embed_total_chars_max": MESSAGE_EMBED_TOTAL_CHARS_MAX,
            "request_bytes_max": MESSAGE_REQUEST_BYTES_MAX,
        },
        "attachments": {
            "count_max": ATTACHMENTS_MAX,
            "total_bytes_max": attachment_total_bytes_max(),
            # Advisory: the UI warns above this and still lets the send
            # through, because only Discord knows the real per-server cap.
            "file_bytes_advisory": attachment_max_file_bytes(),
            "file_advisory_is_hard": False,
        },
        "embed": {
            "title_max": EMBED_TITLE_MAX,
            "description_max": EMBED_DESCRIPTION_MAX,
            "fields_max": EMBED_FIELDS_MAX,
            "field_name_max": EMBED_FIELD_NAME_MAX,
            "field_value_max": EMBED_FIELD_VALUE_MAX,
            "footer_text_max": EMBED_FOOTER_TEXT_MAX,
            "author_name_max": EMBED_AUTHOR_NAME_MAX,
        },
        "components": {
            "rows_max": COMPONENT_ROWS_MAX,
            "buttons_per_row_max": BUTTONS_PER_ROW_MAX,
            "button_label_max": BUTTON_LABEL_MAX,
            "button_url_max": BUTTON_URL_MAX,
            "custom_id_max": CUSTOM_ID_MAX,
            "select_options_max": SELECT_OPTIONS_MAX,
            "select_option_label_max": SELECT_OPTION_LABEL_MAX,
            "select_option_description_max": SELECT_OPTION_DESCRIPTION_MAX,
            "select_placeholder_max": SELECT_PLACEHOLDER_MAX,
        },
    }


def human_bytes(n: int | float) -> str:
    """Byte count the way every message in this repo words it (MB, not MiB)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{int(n)}B"
