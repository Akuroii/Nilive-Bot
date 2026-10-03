"""
Discord message payload validation (pure Python: no Flask, no Discord, no DB).

    validate_message(doc, attachments=None) -> (ok: bool, errors: list[dict])

`doc` is a whole message {"content": str, "embeds": [...]}. Each error is
{"path": <dotted Discord field path>, "message": <human text>}, e.g.
{"path": "embeds.0.title", "message": "Embed 1 title is 300 characters; Discord's limit is 256."}

Paths match the ones utils/shop_publisher.py walks for token resolution.
"""

CONTENT_MAX = 2000
EMBEDS_MAX = 10
EMBED_TOTAL_MAX = 6000          # across ALL embeds in one message
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELDS_MAX = 25
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
FOOTER_TEXT_MAX = 2048
AUTHOR_NAME_MAX = 256


def _err(errors, path, message):
    errors.append({"path": path, "message": message})


def _check_len(errors, text, path, label, limit):
    if isinstance(text, str) and len(text) > limit:
        _err(errors, path,
             f"{label} is {len(text)} characters; Discord's limit is {limit}.")


def _embed_chars(embed):
    total = 0
    for key in ("title", "description"):
        if isinstance(embed.get(key), str):
            total += len(embed[key])
    footer = embed.get("footer")
    if isinstance(footer, dict) and isinstance(footer.get("text"), str):
        total += len(footer["text"])
    author = embed.get("author")
    if isinstance(author, dict) and isinstance(author.get("name"), str):
        total += len(author["name"])
    for field in embed.get("fields") or []:
        if isinstance(field, dict):
            for key in ("name", "value"):
                if isinstance(field.get(key), str):
                    total += len(field[key])
    return total


def validate_message(doc, attachments=None):
    errors = []
    if not isinstance(doc, dict):
        _err(errors, "", "Message must be an object.")
        return False, errors

    content = doc.get("content")
    if content is not None and not isinstance(content, str):
        _err(errors, "content", "Message content must be text.")
    _check_len(errors, content, "content", "Message content", CONTENT_MAX)

    embeds = doc.get("embeds")
    if embeds is None:
        embeds = []
    if not isinstance(embeds, list):
        _err(errors, "embeds", "Embeds must be a list.")
        return False, errors
    if len(embeds) > EMBEDS_MAX:
        _err(errors, "embeds",
             f"Message has {len(embeds)} embeds; Discord's limit is {EMBEDS_MAX}.")

    grand_total = 0
    for i, embed in enumerate(embeds):
        prefix = f"embeds.{i}"
        n = i + 1
        if not isinstance(embed, dict):
            _err(errors, prefix, f"Embed {n} must be an object.")
            continue
        _check_len(errors, embed.get("title"), f"{prefix}.title",
                   f"Embed {n} title", TITLE_MAX)
        _check_len(errors, embed.get("description"), f"{prefix}.description",
                   f"Embed {n} description", DESCRIPTION_MAX)
        footer = embed.get("footer")
        if isinstance(footer, dict):
            _check_len(errors, footer.get("text"), f"{prefix}.footer.text",
                       f"Embed {n} footer text", FOOTER_TEXT_MAX)
        author = embed.get("author")
        if isinstance(author, dict):
            _check_len(errors, author.get("name"), f"{prefix}.author.name",
                       f"Embed {n} author name", AUTHOR_NAME_MAX)
        fields = embed.get("fields")
        if fields is not None and not isinstance(fields, list):
            _err(errors, f"{prefix}.fields", f"Embed {n} fields must be a list.")
            fields = []
        fields = fields or []
        if len(fields) > FIELDS_MAX:
            _err(errors, f"{prefix}.fields",
                 f"Embed {n} has {len(fields)} fields; Discord's limit is {FIELDS_MAX}.")
        for j, field in enumerate(fields):
            if not isinstance(field, dict):
                continue
            _check_len(errors, field.get("name"), f"{prefix}.fields.{j}.name",
                       f"Embed {n} field {j + 1} name", FIELD_NAME_MAX)
            _check_len(errors, field.get("value"), f"{prefix}.fields.{j}.value",
                       f"Embed {n} field {j + 1} value", FIELD_VALUE_MAX)
        grand_total += _embed_chars(embed)

    if grand_total > EMBED_TOTAL_MAX:
        _err(errors, "embeds",
             f"Embeds total {grand_total} characters; Discord's combined limit is {EMBED_TOTAL_MAX}.")

    return (not errors), errors
