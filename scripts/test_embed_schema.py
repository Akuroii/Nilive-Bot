#!/usr/bin/env python3
"""
Embed Builder — server-side payload validation (utils/embed_schema.py) and
the limits table it reads (utils/discord_limits.py).

What this locks down
  1.  A payload the builder can actually produce passes untouched.
  2.  Every Discord rule the review found unenforced server-side now fails
      with a FIELD-PATH error (the same shape Discord's own 400 uses), so
      the page can point at the offending input instead of showing one
      opaque string.
  3.  `attachment://name` is only accepted when a file with that name is in
      the same request — the "never send a silently broken embed" rule.
  4.  Limits come from ONE table: the JSON the client receives and the
      checks the server runs cannot drift apart.
  5.  The advisory per-file cap warns and never blocks (Discord raises it
      for boosted servers / Nitro; a hard block would refuse sendable files).

Run:  python3 scripts/test_embed_schema.py
No dependencies — the two modules are pure standard library on purpose, so
this runs in CI before requirements are even installed.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.discord_limits as L              # noqa: E402
import utils.embed_schema as S                # noqa: E402

_passed = 0
_failed = 0
_failures: list[str] = []


def check(cond, name, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS {name}")
    else:
        _failed += 1
        _failures.append(name + (f" — {extra}" if extra else ""))
        print(f"  FAIL {name} {extra}")


def section(title):
    print(f"\n== {title} ==")


def paths(errors):
    return [e["path"] for e in errors]


def good_payload():
    return {
        "content": "Hello **world**",
        "embeds": [{
            "title": "Title",
            "description": "Body",
            "color": 0x7C5CBF,
            "author": {"name": "Auth", "icon_url": "https://example.com/a.png",
                       "url": "https://example.com"},
            "footer": {"text": "Foot", "icon_url": "https://example.com/f.png"},
            "image": {"url": "https://example.com/i.png"},
            "thumbnail": {"url": "https://example.com/t.png"},
            "url": "https://example.com/embed",
            "timestamp": "2026-09-23T18:00:00+00:00",
            "fields": [{"name": "n", "value": "v", "inline": True}],
        }],
    }


FILES = [{"name": "pic.png", "size": 1024}]


def happy_path_tests():
    section("A payload the builder produces")
    ok, errors = S.validate_message(good_payload(), FILES)
    check(ok and errors == [], "full valid payload passes", str(errors))

    ok, errors = S.validate_message({"content": "hi"}, [])
    check(ok, "content-only payload passes", str(errors))

    ok, errors = S.validate_message({"embeds": [{}]}, [])
    check(ok, "an empty embed object passes (the editor sends blanks)", str(errors))

    ok, errors = S.validate_message({}, FILES)
    check(ok, "attachment-only payload passes", str(errors))

    clean, ok, errors = S.validate_for_discord(
        {"content": "hi", "embeds": [{**good_payload()["embeds"][0], "editorOnly": 1}]}, FILES)
    check(ok, "editor-only keys do not fail validation", str(errors))
    check("editorOnly" not in clean["embeds"][0], "editor-only keys are stripped from the payload")
    check(clean["content"] == "hi" and len(clean["embeds"]) == 1, "clean payload keeps content + embeds")


def limit_tests():
    section("Discord's limits, one table")
    # title
    _, errors = S.validate_message({"embeds": [{"title": "x" * (L.EMBED_TITLE_MAX + 1)}]}, [])
    check(paths(errors) == ["embeds.0.title"], "title over limit fails on its own path", str(paths(errors)))
    ok, _ = S.validate_message({"embeds": [{"title": "x" * L.EMBED_TITLE_MAX}]}, [])
    check(ok, "title exactly at the limit passes")

    # description
    _, errors = S.validate_message(
        {"embeds": [{"description": "x" * (L.EMBED_DESCRIPTION_MAX + 1)}]}, [])
    check(paths(errors) == ["embeds.0.description"], "description over limit", str(paths(errors)))

    # field count
    fields = [{"name": f"n{i}", "value": "v"} for i in range(L.EMBED_FIELDS_MAX + 1)]
    _, errors = S.validate_message({"embeds": [{"fields": fields}]}, [])
    check(paths(errors) == ["embeds.0.fields"], "26 fields rejected", str(paths(errors)))
    ok, _ = S.validate_message(
        {"embeds": [{"fields": [{"name": f"n{i}", "value": "v"} for i in range(L.EMBED_FIELDS_MAX)]}]}, [])
    check(ok, "25 fields accepted")

    # field name / value
    _, errors = S.validate_message(
        {"embeds": [{"fields": [{"name": "x" * (L.EMBED_FIELD_NAME_MAX + 1), "value": "v"}]}]}, [])
    check(paths(errors) == ["embeds.0.fields.0.name"], "field name over limit", str(paths(errors)))
    _, errors = S.validate_message(
        {"embeds": [{"fields": [{"value": "x" * (L.EMBED_FIELD_VALUE_MAX + 1)}]}]}, [])
    check(set(paths(errors)) == {"embeds.0.fields.0.name", "embeds.0.fields.0.value"},
          "missing name AND oversized value both reported", str(paths(errors)))

    # footer / author
    _, errors = S.validate_message(
        {"embeds": [{"footer": {"text": "x" * (L.EMBED_FOOTER_TEXT_MAX + 1)}}]}, [])
    check(paths(errors) == ["embeds.0.footer.text"], "footer text over limit", str(paths(errors)))
    _, errors = S.validate_message(
        {"embeds": [{"footer": {"icon_url": "https://example.com/f.png"}}]}, [])
    check(paths(errors) == ["embeds.0.footer.text"],
          "footer icon without footer text is rejected (Discord requires text)", str(paths(errors)))
    _, errors = S.validate_message(
        {"embeds": [{"author": {"icon_url": "https://example.com/a.png"}}]}, [])
    check(paths(errors) == ["embeds.0.author.name"],
          "author icon without author name is rejected", str(paths(errors)))
    ok, _ = S.validate_message({"embeds": [{"author": {"name": "ok"}}]}, [])
    check(ok, "author name alone passes")

    # combined 6000 per embed
    big = {"title": "t" * 256, "description": "d" * 4000,
           "fields": [{"name": "n", "value": "v" * 1024}] * 2}
    ok, errors = S.validate_message({"embeds": [big]}, [])
    check(not ok and paths(errors) == ["embeds.0"], "over 6000 characters in one embed is rejected",
          str(paths(errors)))
    check(S.embed_char_count({"title": "ab", "description": "cd",
                              "fields": [{"name": "e", "value": "f"}]}) == 6,
          "character count covers title/description/fields")

    # embeds count
    _, errors = S.validate_message({"embeds": [{}] * (L.MESSAGE_EMBEDS_MAX + 1)}, [])
    check(paths(errors) == ["embeds"], "11 embeds rejected", str(paths(errors)))

    # content
    _, errors = S.validate_message({"content": "x" * (L.MESSAGE_CONTENT_MAX + 1)}, [])
    check(paths(errors) == ["content"], "content over 2000", str(paths(errors)))
    _, errors = S.validate_message({"content": 42}, [])
    check(paths(errors) == ["content"], "non-string content rejected", str(paths(errors)))


def url_and_timestamp_tests():
    section("URLs, timestamps, colours, shapes")
    for bad in ["javascript:alert(1)", "data:image/png;base64,AAAA", "/relative", "example.com"]:
        ok, errors = S.validate_message({"embeds": [{"url": bad}]}, [])
        check(not ok and paths(errors) == ["embeds.0.url"], f"embed url '{bad}' rejected")

    ok, _ = S.validate_message({"embeds": [{"url": "https://example.com/x"}]}, [])
    check(ok, "https embed url accepted")

    for bad in ["2026-13-45", "yesterday", "1700000000"]:
        ok, errors = S.validate_message({"embeds": [{"timestamp": bad}]}, [])
        check(not ok and paths(errors) == ["embeds.0.timestamp"], f"timestamp '{bad}' rejected")
    ok, _ = S.validate_message({"embeds": [{"timestamp": "2026-09-23T18:00:00Z"}]}, [])
    check(ok, "ISO timestamp with Z accepted")

    ok, errors = S.validate_message({"embeds": [{"color": 0xFFFFFF + 1}]}, [])
    check(not ok and paths(errors) == ["embeds.0.color"], "colour above 0xFFFFFF rejected")
    ok, errors = S.validate_message({"embeds": [{"color": "#7c5cbf"}]}, [])
    check(not ok and paths(errors) == ["embeds.0.color"],
          "hex string colour rejected (Discord wants an int)", str(errors))

    ok, errors = S.validate_message({"embeds": [{"image": "https://example.com/i.png"}]}, [])
    check(not ok and paths(errors) == ["embeds.0.image"],
          "image as a bare string rejected (must be an object with url)", str(errors))
    ok, errors = S.validate_message({"embeds": [{"thumbnail": {}}]}, [])
    check(not ok and paths(errors) == ["embeds.0.thumbnail.url"],
          "thumbnail without a url rejected", str(paths(errors)))
    _, errors = S.validate_message({"embeds": ["not an object"]}, [])
    check(paths(errors) == ["embeds.0"], "a non-object embed is reported on its index")
    _, errors = S.validate_message({"embeds": "nope"}, [])
    check(paths(errors) == ["embeds"], "non-list embeds rejected")
    _, errors = S.validate_message("nope", [])
    check(paths(errors) == [""] and "object" in errors[0]["message"],
          "non-object payload rejected")


def attachment_reference_tests():
    section("attachment:// references (no dangling files)")
    ok, _ = S.validate_message(
        {"embeds": [{"image": {"url": "attachment://pic.png"}}]}, FILES)
    check(ok, "attachment://pic.png matches the uploaded file")

    ok, errors = S.validate_message(
        {"embeds": [{"image": {"url": "attachment://ghost.png"}}]}, FILES)
    check(not ok and paths(errors) == ["embeds.0.image.url"], "unknown attachment name rejected",
          str(paths(errors)))
    check("reattach" in errors[0]["message"], "the message says what to do about it",
          errors[0]["message"])

    ok, errors = S.validate_message(
        {"embeds": [{"image": {"url": "attachment://pic.png"}}]}, [])
    check(not ok, "attachment reference with NO files in the request rejected")

    ok, _ = S.validate_message(
        {"embeds": [{"author": {"name": "a", "icon_url": "attachment://pic.png"},
                     "footer": {"text": "f", "icon_url": "attachment://pic.png"}}]}, FILES)
    check(ok, "attachment:// works for author + footer icons too")

    ok, errors = S.validate_message(
        {"embeds": [{"image": {"url": "attachment://"}}]}, FILES)
    check(not ok and paths(errors) == ["embeds.0.image.url"], "empty attachment name rejected")


def attachment_limit_tests():
    section("Attachment counts and sizes")
    files = [{"name": f"f{i}.png", "size": 1000} for i in range(L.ATTACHMENTS_MAX + 1)]
    _, errors = S.validate_message({"content": "x"}, files)
    check(paths(errors) == ["files"], "11 attachments rejected", str(paths(errors)))

    total_max = L.attachment_total_bytes_max()
    _, errors = S.validate_message({"content": "x"}, [{"name": "huge.bin", "size": total_max + 1}])
    check(paths(errors) == ["files"], "a single file over the request maximum is rejected",
          str(paths(errors)))
    _, errors = S.validate_message(
        {"content": "x"},
        [{"name": "a.bin", "size": total_max // 2 + 1}, {"name": "b.bin", "size": total_max // 2 + 1}])
    check(paths(errors) == ["files"], "two files over the total are rejected", str(paths(errors)))
    ok, _ = S.validate_message({"content": "x"}, [{"name": "a.bin", "size": total_max}])
    check(ok, "files exactly at the total pass")

    # advisory: warn, never block
    advisory = L.attachment_max_file_bytes()
    warn = S.advisory_file_warnings([{"name": "big.png", "size": advisory + 1}])
    check(len(warn) == 1 and "big.png" in warn[0], "per-file advisory produces a warning")
    ok, errors = S.validate_message({"content": "x"}, [{"name": "big.png", "size": advisory + 1}])
    check(ok, "per-file advisory never blocks a send", str(errors))
    check(S.advisory_file_warnings([]) == [] and S.advisory_file_warnings(None) == [],
          "no warnings for an empty/None file list")

    # empty message
    _, errors = S.validate_message({}, [])
    check(paths(errors) == [""] and "Nothing to send" in errors[0]["message"],
          "an empty message is rejected with the friendly line")


def limits_table_tests():
    section("limits_payload (what the client receives)")
    payload = L.limits_payload()
    check(payload["message"]["content_max"] == L.MESSAGE_CONTENT_MAX
          and payload["message"]["embeds_max"] == L.MESSAGE_EMBEDS_MAX,
          "message limits present")
    check(payload["embed"]["fields_max"] == 25 and payload["embed"]["title_max"] == 256,
          "embed limits present")
    check(payload["attachments"]["file_advisory_is_hard"] is False,
          "the per-file cap is advertised as advisory")
    check(payload["attachments"]["total_bytes_max"] < L.MESSAGE_REQUEST_BYTES_MAX,
          "the file budget leaves room for the multipart envelope inside 25 MiB")
    check(payload["components"]["buttons_per_row_max"] == 5
          and payload["components"]["select_options_max"] == 25,
          "component limits published for Phase 3")

    # env override, and it must never crash on junk
    os.environ[L.ATTACHMENT_FILE_BYTES_ENV] = "1234567"
    check(L.attachment_max_file_bytes() == 1234567, "env override is honoured")
    os.environ[L.ATTACHMENT_FILE_BYTES_ENV] = "not-a-number"
    check(L.attachment_max_file_bytes() == L.ATTACHMENT_FILE_BYTES_ADVISORY,
          "junk env value falls back to the default")
    del os.environ[L.ATTACHMENT_FILE_BYTES_ENV]

    check(S.first_error_message([{"path": "a", "message": "boom"}]) == "boom",
          "first_error_message returns the first message")
    check(S.first_error_message([]) == "Invalid message payload",
          "first_error_message has a fallback")

    # ── The client's half of the same contract (phase 1 step 6a) ──────
    # dashboard/static/js/embed/validate.js refuses to run without the keys it
    # lists in REQUIRED_LIMITS (a missing one is an explicit issue, never a
    # silent "no limit"), and the page is handed this very payload through
    # data-limits. So the two lists are read from the two files and pinned
    # together here: renaming a key on either side fails in CI instead of
    # shipping a page that validates nothing.
    validate_js = (root := os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "dashboard", "static", "js", "embed", "validate.js"))
    with open(validate_js, encoding="utf-8") as fh:
        js_source = fh.read()
    js_keys = re.findall(r"\['([a-z_]+)',\s*'([a-z_]+)'\]", js_source)
    check(len(js_keys) >= 10,
          f"the v2 client declares the limits it needs ({len(js_keys)} keys read from validate.js)",
          str(js_keys))
    missing = [f"{block}.{key}" for block, key in js_keys if key not in payload.get(block, {})]
    check(not missing,
          "every limit the v2 client requires is served by limits_payload()",
          ", ".join(missing))
    non_numeric = [f"{block}.{key}" for block, key in js_keys
                   if not isinstance(payload.get(block, {}).get(key), int)]
    check(not non_numeric,
          "and every one of them is an integer (the client compares sizes, not strings)",
          ", ".join(non_numeric))

    # 6b: the counters and the add caps read numbers too, and they read them from
    # the same table. Every `limits.<block>.<key>` the client mentions — the
    # measurement code as well as the rules — must exist in the payload, so a
    # counter can never measure against a limit the server does not have.
    used = sorted(set(re.findall(r"limits\.([a-z_]+)\.([a-z_]+)", js_source)))
    unknown = [f"{b}.{k}" for b, k in used if k not in payload.get(b, {})]
    check(not unknown,
          f"every limit the client MEASURES against is served ({len(used)} referenced)",
          ", ".join(unknown))
    counter_keys = {"message.content_max", "message.embeds_max", "message.embed_total_chars_max",
                    "embed.title_max", "embed.description_max", "embed.fields_max",
                    "embed.field_name_max", "embed.field_value_max", "embed.footer_text_max",
                    "embed.author_name_max"}
    check(counter_keys.issubset({f"{b}.{k}" for b, k in used}),
          "including every key the 6b counters and caps display",
          ", ".join(sorted(counter_keys - {f"{b}.{k}" for b, k in used})))


def main():
    print("Embed Builder — embed_schema / discord_limits verification")
    print("=" * 60)
    happy_path_tests()
    limit_tests()
    url_and_timestamp_tests()
    attachment_reference_tests()
    attachment_limit_tests()
    limits_table_tests()

    print("\n" + "=" * 60)
    if _failed:
        print(f"RESULT: {_passed} passed, {_failed} FAILED")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"RESULT: all {_passed} checks passed")


if __name__ == "__main__":
    main()
