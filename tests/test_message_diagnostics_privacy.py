from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_verbose_per_message_diagnostics_and_content_dumps_are_absent():
    sources = {
        name: (ROOT / name).read_text()
        for name in ("main.py", "cogs/command_aliases.py", "utils/message_router.py")
    }
    combined = "\n".join(sources.values())
    for marker in ("[DIAG-GATEWAY]", "[DIAG-ALIAS:", "[DIAG-ROUTER]"):
        assert marker not in combined
    for unsafe in ("repr(message.content)", "repr(raw_content)", "content={repr("):
        assert unsafe not in combined


def test_useful_error_reporting_remains():
    assert "[ON_ERROR]" in (ROOT / "main.py").read_text()
    assert "[ALIASES] router error:" in (ROOT / "cogs/command_aliases.py").read_text()
    assert "[router] custom_commands has no" in (ROOT / "utils/message_router.py").read_text()
