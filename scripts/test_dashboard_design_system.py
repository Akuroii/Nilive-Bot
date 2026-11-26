#!/usr/bin/env python3
"""Design-system guards for the dashboard (tokens.css, both themes).

Pure static checks: no Flask, no database, no browser. They pin the
contract the redesign relies on:

  1. every token declared for dark is also declared for light
  2. text / accent / status / control-border contrast meets WCAG in both themes
  3. tokens.css is linked before main.css in every template that links main.css
  4. a global :focus-visible ring exists
  5. retired fonts are gone and the new families are loaded
  6. the retired overview copy does not come back

Run:  python3 scripts/test_dashboard_design_system.py
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
CSS = DASH / "static" / "css"
TEMPLATES = DASH / "templates"


def _blocks(css: str):
    """Split tokens.css into (shared, dark, light) declaration dicts."""
    def decls(body):
        return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body))
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sel, body = re.sub(r"\s+", "", m.group(1)), m.group(2)
        if sel == ":root":
            out["shared"] = decls(body)
        elif sel == ':root,:root[data-theme="dark"]':
            out["dark"] = decls(body)
        elif sel == ':root[data-theme="light"]':
            out["light"] = decls(body)
    return out


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TOKENS = _blocks((CSS / "tokens.css").read_text(encoding="utf-8"))


class TokenContract(unittest.TestCase):
    def test_all_three_blocks_parsed(self):
        self.assertEqual(set(TOKENS), {"shared", "dark", "light"})

    def test_every_dark_token_has_a_light_value(self):
        missing = sorted(set(TOKENS["dark"]) - set(TOKENS["light"]))
        self.assertEqual(missing, [], f"declared for dark only (leaks into light): {missing}")

    def test_legacy_names_still_defined(self):
        legacy = ["--bg", "--bg2", "--bg3", "--bg4", "--border", "--border-soft",
                  "--text", "--text2", "--text3", "--accent", "--accent2", "--accent3",
                  "--accent-glow", "--success", "--warning", "--danger", "--gold",
                  "--surface-glass"]
        for theme in ("dark", "light"):
            for name in legacy:
                self.assertIn(name, TOKENS[theme], f"{name} missing from {theme}")

    def test_main_css_no_longer_defines_theme_values(self):
        main = (CSS / "main.css").read_text(encoding="utf-8")
        self.assertNotRegex(main, r"--bg3?\s*:\s*#", "main.css must not redefine tokens")


class Contrast(unittest.TestCase):
    SURFACES = ("--bg", "--bg2", "--bg3", "--bg4")

    def test_text_on_every_surface(self):
        for theme in ("dark", "light"):
            t = TOKENS[theme]
            for fg in ("--text", "--text2", "--text3", "--accent-text"):
                for bg in self.SURFACES:
                    r = _ratio(t[fg], t[bg])
                    self.assertGreaterEqual(r, 4.5, f"{theme}: {fg} on {bg} = {r:.2f}")

    def test_status_text_on_cards(self):
        for theme in ("dark", "light"):
            t = TOKENS[theme]
            for fg in ("--success", "--warning", "--danger"):
                for bg in ("--bg3", "--bg2"):
                    r = _ratio(t[fg], t[bg])
                    self.assertGreaterEqual(r, 4.5, f"{theme}: {fg} on {bg} = {r:.2f}")

    def test_white_label_on_filled_controls(self):
        for theme in ("dark", "light"):
            t = TOKENS[theme]
            for fill in ("--accent", "--accent2", "--danger-solid"):
                r = _ratio(t["--on-accent"], t[fill])
                self.assertGreaterEqual(r, 4.5, f"{theme}: on-accent over {fill} = {r:.2f}")

    def test_control_border_is_visible(self):
        for theme in ("dark", "light"):
            t = TOKENS[theme]
            for bg in ("--bg3", "--bg4"):
                r = _ratio(t["--border-control"], t[bg])
                self.assertGreaterEqual(r, 3.0, f"{theme}: border-control on {bg} = {r:.2f}")

    def test_focus_ring_is_visible(self):
        for theme in ("dark", "light"):
            t = TOKENS[theme]
            for bg in self.SURFACES:
                r = _ratio(t["--focus-ring"], t[bg])
                self.assertGreaterEqual(r, 3.0, f"{theme}: focus ring on {bg} = {r:.2f}")


class Loading(unittest.TestCase):
    def test_tokens_linked_before_main_css(self):
        checked = 0
        for path in TEMPLATES.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            if "css/main.css" not in text:
                continue
            checked += 1
            self.assertIn("css/tokens.css", text, f"{path.name} links main.css without tokens.css")
            self.assertLess(text.index("css/tokens.css"), text.index("css/main.css"),
                            f"{path.name}: tokens.css must load before main.css")
        self.assertGreaterEqual(checked, 5)

    def test_new_fonts_loaded_everywhere_main_css_is(self):
        for path in TEMPLATES.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            if "css/main.css" in text:
                self.assertIn("family=Geist", text, path.name)
                self.assertIn("Instrument+Serif", text, path.name)
                self.assertNotIn("family=Inter", text, path.name)

    def test_retired_fonts_gone(self):
        for path in list(CSS.glob("*.css")) + list(TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("JetBrains Mono", text, path.name)
            self.assertNotIn("'Inter'", text, path.name)


class Focus(unittest.TestCase):
    def test_global_focus_visible_ring(self):
        main = (CSS / "main.css").read_text(encoding="utf-8")
        self.assertRegex(main, r":focus-visible:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--focus-ring\)")

    def test_reduced_motion_respected(self):
        main = (CSS / "main.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion: reduce", main)


class Copy(unittest.TestCase):
    RETIRED = [
        "all in one place",
        "Jump straight into the tasks you use most",
        "Open module",
        "NERO CONTROL CENTER",
        "YOUR WORKSPACE",
        "simple, self-serve",
    ]

    def test_overview_copy(self):
        html = (TEMPLATES / "general" / "overview.html").read_text(encoding="utf-8")
        for phrase in self.RETIRED:
            self.assertNotIn(phrase, html, f"retired copy returned: {phrase!r}")
        self.assertIn("overview-hero", html)   # pinned by test_dashboard_redesign.py


if __name__ == "__main__":
    unittest.main(verbosity=2)
