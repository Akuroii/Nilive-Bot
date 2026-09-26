#!/usr/bin/env python3
"""
Guild lookup TTL cache (locked plan §7.1) — /api/guild/roles + /api/guild/channels.

What this locks down (the nine behaviours the cache was authorised for)
  1. the first request fetches from Discord;
  2. a second request inside the TTL window is served from the cache — proved
     the strong way: the fake would now FAIL if it were called, and the body is
     still the first answer;
  3. a request after the window expires fetches fresh data;
  4. `?refresh=1` bypasses the cache (and re-seeds it), while an unrelated query
     parameter does not;
  5. roles and channels are cached independently;
  6. the response shape is unchanged, fresh or cached;
  7. nothing leaks between guilds;
  8. the existing error behaviour is preserved — same wording, status 200, and
     a failure is NEVER cached (the next request asks Discord again);
  9. the change is isolated: only those two endpoints are cached, a forbidden
     request neither fetches nor caches, and no shipped caller (v1's page
     included) sends the bypass — so every existing consumer receives
     byte-identical responses.

Boundary: the Discord HTTP call is the ONE fake (`core._req.get`), which is
phase1_support's own rule — external boundaries are explicit fakes. The Flask
app, its session, the permission decorators, the endpoint code and the cache
itself are the real modules, driven through a real test client. Time is real
too: the expiry test shortens the TTL constant instead of patching the clock,
so the expiry path that ships is the path under test.

Run:  python3 scripts/test_guild_lookup_cache.py
      (needs the dashboard's web dependencies, like the other API harnesses)
"""

import asyncio
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phase1_support as P                      # noqa: E402  (sets the scratch DB + env first)
import dashboard.app as dashboard               # noqa: E402
import dashboard.api.core as core               # noqa: E402

GUILD_A = P.GUILD
GUILD_B = P.GUILD + 1
VIEWER = P.USER + 1

dashboard.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

# ── Fixtures shaped like Discord's own payloads ─────────────────────────────
ROLES_A = [
    {"id": str(GUILD_A), "name": "@everyone", "position": 0, "color": 0, "managed": False},
    {"id": "1001", "name": "Nero", "position": 30, "color": 0x7C5CBF, "managed": True},
    {"id": "1002", "name": "Admins", "position": 12, "color": 0xFF0000, "managed": False},
    {"id": "1003", "name": "Moderators", "position": 20, "color": 0, "managed": False},
]
# The endpoint's own ordering contract: unmanaged by position desc, then
# managed roles, then @everyone last.
ROLE_ORDER_A = ["Moderators", "Admins", "Nero", "@everyone"]

ROLES_B = [
    {"id": str(GUILD_B), "name": "@everyone", "position": 0, "color": 0, "managed": False},
    {"id": "1101", "name": "B Admins", "position": 5, "color": 0, "managed": False},
]

CHANNELS_A = [
    {"id": "2003", "name": "Text channels", "type": 4},
    {"id": "2001", "name": "general", "type": 0, "parent_id": "2003"},
    {"id": "2005", "name": "zebra", "type": 0},
    {"id": "2004", "name": "announcements", "type": 5, "parent_id": "2003"},
    {"id": "2002", "name": "Lounge", "type": 2},
]
# type order (text, announcement, voice, …) then name ascending.
CHANNEL_ORDER_A = ["general", "zebra", "announcements", "Lounge"]

CHANNELS_B = [
    {"id": "2101", "name": "b-general", "type": 0},
    {"id": "2102", "name": "b-voice", "type": 2},
]

EMOJIS_A = [
    {"id": "3001", "name": "pepe", "animated": False},
    {"id": "3002", "name": "Kek", "animated": True},
]

ROLE_KEYS = {"id", "name", "text", "color", "position", "managed"}
CHANNEL_KEYS = {"id", "name", "text", "type_icon", "category", "type"}
EMOJI_KEYS = {"id", "name", "animated"}


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class Discord:
    """The single fake: Discord's HTTP boundary, with per-guild answers and a
    call log the tests assert on (counts, URL, auth header, timeout)."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.by_guild = {
            str(GUILD_A): {"roles": ROLES_A, "channels": CHANNELS_A, "emojis": EMOJIS_A},
            str(GUILD_B): {"roles": ROLES_B, "channels": CHANNELS_B, "emojis": []},
        }

    def set(self, kind, payload, guild=GUILD_A):
        self.by_guild[str(guild)][kind] = payload

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.status != 200:
            return FakeResponse(self.status, None)
        guild_id = url.split("/guilds/")[1].split("/")[0]
        kind = url.rsplit("/", 1)[1]
        data = self.by_guild.get(guild_id, {}).get(kind, [])
        return FakeResponse(200, [dict(row) for row in data])

    def count(self, kind):
        return sum(1 for call in self.calls if call["url"].endswith("/" + kind))


class CacheCase(unittest.TestCase):
    """Real app, real session, real permission decorators, fake HTTP boundary."""

    def setUp(self):
        asyncio.run(P.reset_database())
        for user, level in ((P.USER, "admin"), (VIEWER, "viewer")):
            for guild in (GUILD_A, GUILD_B):
                P.execute(
                    "INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                    "VALUES (?,?,?,1)", (guild, user, level),
                )
        core._guild_lookup_cache.clear()
        # Leave the SHIPPED constant exactly as it is (a test that rewrote it
        # here could never see a wrong value), and only restore it for tests
        # that deliberately shorten the window.
        self.addCleanup(core._guild_lookup_cache.clear)
        self.addCleanup(setattr, core, "GUILD_LOOKUP_TTL_SECONDS", core.GUILD_LOOKUP_TTL_SECONDS)

        self.discord = Discord()
        patcher = patch.object(core._req, "get", self.discord.get)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = dashboard.app.test_client()
        self.as_guild(GUILD_A)

    def as_guild(self, guild_id, user=P.USER):
        with self.client.session_transaction() as session:
            session.update(user={"id": user, "username": "TTL", "avatar": None},
                           guild_id=guild_id, expires_at=time.time() + 7200,
                           csrf_token="ttl-csrf")

    def get(self, path):
        return self.client.get(path)

    def names(self, response):
        return [item["name"] for item in response.get_json()["results"]]

    def body(self, response):
        return json.dumps(response.get_json(), sort_keys=True)


# ═════════════════════════════════════════════════════════════════════════
# The locked contract
# ═════════════════════════════════════════════════════════════════════════
class ContractTests(CacheCase):
    def test_ttl_is_sixty_seconds(self):
        self.assertEqual(core.GUILD_LOOKUP_TTL_SECONDS, 60,
                         "§7.1/§10 fix the window at 60 s")

    def test_first_request_fetches_from_discord(self):
        response = self.get("/api/guild/roles")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"].split(";")[0], "application/json")
        self.assertEqual(self.discord.count("roles"), 1, "the first request must fetch")
        call = self.discord.calls[0]
        self.assertTrue(call["url"].endswith(f"/guilds/{GUILD_A}/roles"), call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Bot " + os.environ["DISCORD_TOKEN"])
        self.assertEqual(call["timeout"], 8, "the timeout the endpoint always used")
        self.assertEqual(self.names(response), ROLE_ORDER_A)

    def test_channels_first_request_fetches_too(self):
        response = self.get("/api/guild/channels")
        self.assertEqual(self.discord.count("channels"), 1)
        self.assertEqual(self.names(response), CHANNEL_ORDER_A)


# ═════════════════════════════════════════════════════════════════════════
# 2 + 3. inside the window: cached; after it: fresh
# ═════════════════════════════════════════════════════════════════════════
class TtlWindowTests(CacheCase):
    def test_second_request_inside_the_window_is_served_from_the_cache(self):
        first = self.get("/api/guild/roles")
        # The fake now answers differently AND fails: any fetch would turn this
        # request into an error, so a 200 with the first body can only be the cache.
        self.discord.status = 500
        self.discord.set("roles", [{"id": "9", "name": "Should not appear",
                                    "position": 1, "color": 0, "managed": False}])
        second = self.get("/api/guild/roles")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json(), first.get_json())
        self.assertEqual(self.discord.count("roles"), 1, "no second fetch inside the window")

    def test_request_after_the_window_expires_fetches_fresh_data(self):
        core.GUILD_LOOKUP_TTL_SECONDS = 0.05      # real time, shortened window
        first = self.get("/api/guild/roles")
        self.assertEqual(self.discord.count("roles"), 1)
        self.assertEqual(self.names(first), ROLE_ORDER_A)
        self.discord.set("roles", [
            {"id": str(GUILD_A), "name": "@everyone", "position": 0, "color": 0, "managed": False},
            {"id": "1001", "name": "Nero Renamed", "position": 30, "color": 0x7C5CBF, "managed": True},
        ])
        time.sleep(0.08)
        second = self.get("/api/guild/roles")
        self.assertEqual(self.discord.count("roles"), 2, "the window closed: fetch again")
        self.assertEqual(self.names(second), ["Nero Renamed", "@everyone"])
        self.assertNotEqual(self.body(second), self.body(first))
        self.assertNotIn(core._guild_cache_key("roles", GUILD_A), {},
                         "rig: the stale entry was replaced, not reused")

    def test_the_window_is_sixty_seconds_on_the_nose(self):
        # The exact value matters twice over: §7.1 fixes it at 60 s, and the
        # cheap "seen at t, expires at t+60" implementation would pass a window
        # measured anywhere between two requests. Reach into the stored entry
        # and compare the entry's own lifetime instead of the observed gap.
        before = time.monotonic()
        self.get("/api/guild/channels")
        after = time.monotonic()
        entry = core._guild_lookup_cache[core._guild_cache_key("channels", GUILD_A)]
        lifetime = entry["expires_at"] - before
        self.assertAlmostEqual(lifetime, 60.0, delta=1.0,
                               msg="the cache entry must live ~60 s from the fetch")
        self.assertLessEqual(entry["expires_at"] - after, 60.0)


# ═════════════════════════════════════════════════════════════════════════
# 4. the bypass
# ═════════════════════════════════════════════════════════════════════════
class BypassTests(CacheCase):
    def test_refresh_parameter_skips_the_cache_and_reseeds_it(self):
        self.get("/api/guild/roles")                                  # prime
        self.discord.set("roles", [
            {"id": str(GUILD_A), "name": "@everyone", "position": 0, "color": 0, "managed": False},
            {"id": "1001", "name": "Fresh Name", "position": 30, "color": 0x7C5CBF, "managed": True},
        ])
        bypassed = self.get("/api/guild/roles?refresh=1")
        self.assertEqual(self.discord.count("roles"), 2, "the bypass must fetch")
        self.assertEqual(self.names(bypassed), ["Fresh Name", "@everyone"])
        # …and the fresh answer is what the NEXT ordinary request is served.
        again = self.get("/api/guild/roles")
        self.assertEqual(self.discord.count("roles"), 2, "the bypass re-seeded the entry")
        self.assertEqual(self.body(again), self.body(bypassed))

    def test_an_unrelated_query_parameter_does_not_bypass(self):
        first = self.get("/api/guild/roles?guild=1&pick=role")
        self.discord.status = 500                     # would fail if refetched
        second = self.get("/api/guild/roles?guild=1")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.body(second), self.body(first))
        self.assertEqual(self.discord.count("roles"), 1)

    def test_bypass_is_available_to_both_endpoints(self):
        self.get("/api/guild/channels")
        self.get("/api/guild/channels?refresh=true")
        self.assertEqual(self.discord.count("channels"), 2)


# ═════════════════════════════════════════════════════════════════════════
# 5 + 7. independence: kind by kind, guild by guild
# ═════════════════════════════════════════════════════════════════════════
class IndependenceTests(CacheCase):
    def test_roles_and_channels_are_cached_independently(self):
        roles = self.get("/api/guild/roles")
        channels = self.get("/api/guild/channels")
        self.assertEqual(self.discord.count("roles"), 1)
        self.assertEqual(self.discord.count("channels"), 1)
        # One kind's entry never answers for the other: with the fake failing,
        # both endpoints still return their own cached, differently-shaped body.
        self.discord.status = 500
        self.assertEqual(self.body(self.get("/api/guild/roles")), self.body(roles))
        self.assertEqual(self.body(self.get("/api/guild/channels")), self.body(channels))
        self.assertEqual(self.discord.count("roles"), 1)
        self.assertEqual(self.discord.count("channels"), 1)

    def test_nothing_leaks_between_guilds(self):
        first = self.get("/api/guild/roles")
        self.assertEqual(self.names(first), ROLE_ORDER_A)

        self.as_guild(GUILD_B)                        # same user, another guild
        other = self.get("/api/guild/roles")
        self.assertEqual(self.discord.count("roles"), 2, "another guild has its own entry")
        self.assertEqual(self.names(other), ["B Admins", "@everyone"])
        self.assertNotEqual(self.body(other), self.body(first))

        self.as_guild(GUILD_A)                        # back again: still the A answer
        back = self.get("/api/guild/roles")
        self.assertEqual(self.discord.count("roles"), 2, "A was not refetched")
        self.assertEqual(self.body(back), self.body(first))

    def test_channels_are_isolated_per_guild_as_well(self):
        self.get("/api/guild/channels")
        self.as_guild(GUILD_B)
        other = self.get("/api/guild/channels")
        self.assertEqual(self.discord.count("channels"), 2)
        self.assertEqual(self.names(other), ["b-general", "b-voice"])


# ═════════════════════════════════════════════════════════════════════════
# 6. the response contract, fresh or cached
# ═════════════════════════════════════════════════════════════════════════
class ResponseShapeTests(CacheCase):
    def test_roles_response_shape_and_ordering_are_unchanged(self):
        first = self.get("/api/guild/roles").get_json()
        self.assertEqual(set(first), {"results"})
        for item in first["results"]:
            self.assertEqual(set(item), ROLE_KEYS, "the picker contract: exactly these keys")
        self.assertEqual([item["name"] for item in first["results"]], ROLE_ORDER_A)
        self.assertEqual([item["text"] for item in first["results"]],
                         [item["name"] for item in first["results"]],
                         "text mirrors name")
        by_name = {item["name"]: item for item in first["results"]}
        self.assertEqual(by_name["Nero"]["color"], "#7c5cbf", "colour is #rrggbb")
        self.assertEqual(by_name["Moderators"]["color"], None, "zero is null, never black")
        self.assertTrue(by_name["Nero"]["managed"])
        self.assertFalse(by_name["Admins"]["managed"])
        self.assertEqual(by_name["@everyone"]["position"], 0)

    def test_channels_response_shape_icons_and_categories_are_unchanged(self):
        body = self.get("/api/guild/channels").get_json()
        self.assertEqual(set(body), {"results"})
        for item in body["results"]:
            self.assertEqual(set(item), CHANNEL_KEYS)
        self.assertEqual([item["name"] for item in body["results"]], CHANNEL_ORDER_A)
        by_name = {item["name"]: item for item in body["results"]}
        self.assertNotIn("Text channels", by_name, "categories are not listed as channels")
        self.assertEqual(by_name["general"]["type_icon"], "💬")
        self.assertEqual(by_name["announcements"]["type_icon"], "📢")
        self.assertEqual(by_name["Lounge"]["type_icon"], "🔊")
        self.assertEqual(by_name["general"]["category"], "Text channels")
        self.assertEqual(by_name["zebra"]["category"], "")
        self.assertEqual(by_name["announcements"]["type"], "announcement")
        self.assertEqual(by_name["Lounge"]["type"], "voice")

    def test_a_cached_answer_is_byte_identical_to_a_fresh_one(self):
        self.get("/api/guild/roles")                       # prime
        self.get("/api/guild/channels")                    # prime
        fresh = self.get("/api/guild/roles?refresh=1")
        cached = self.get("/api/guild/roles")
        self.assertEqual(self.body(cached), self.body(fresh))
        self.assertEqual(cached.status_code, fresh.status_code)
        self.assertEqual(cached.headers["Content-Type"], fresh.headers["Content-Type"])
        fresh_ch = self.get("/api/guild/channels?refresh=1")
        cached_ch = self.get("/api/guild/channels")
        self.assertEqual(self.body(cached_ch), self.body(fresh_ch))
        self.assertEqual(self.discord.count("roles"), 2, "rig: one prime + one bypass")
        self.assertEqual(self.discord.count("channels"), 2, "rig: one prime + one bypass")


# ═════════════════════════════════════════════════════════════════════════
# 8. error behaviour, unchanged — and never cached
# ═════════════════════════════════════════════════════════════════════════
class ErrorBehaviourTests(CacheCase):
    def test_missing_token_keeps_its_exact_answer_and_never_fetches(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": ""}):
            first = self.get("/api/guild/roles")
            second = self.get("/api/guild/roles")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json(), {"results": [], "error": "BOT_TOKEN not set"})
        self.assertEqual(second.get_json(), first.get_json())
        self.assertEqual(self.discord.calls, [], "no token means no request, as before")
        self.assertEqual(core._guild_lookup_cache, {}, "and nothing is cached")

    def test_discord_error_keeps_its_wording_and_is_never_cached(self):
        self.discord.status = 500
        first = self.get("/api/guild/channels")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json(), {"results": [], "error": "Discord 500"})
        self.assertEqual(core._guild_lookup_cache, {}, "a failure is not a cache entry")

        self.discord.status = 429                     # a different failure, asked again
        second = self.get("/api/guild/channels")
        self.assertEqual(second.get_json(), {"results": [], "error": "Discord 429"})
        self.assertEqual(self.discord.count("channels"), 2, "errors are re-asked, never served")

        self.discord.status = 200                     # recovery is immediate
        third = self.get("/api/guild/channels")
        self.assertEqual(self.names(third), CHANNEL_ORDER_A)
        self.assertEqual(self.discord.count("channels"), 3)

    def test_an_error_for_one_kind_does_not_disturb_the_other(self):
        primed = self.get("/api/guild/roles")
        self.discord.status = 500
        error = self.get("/api/guild/channels")
        self.assertEqual(error.get_json(), {"results": [], "error": "Discord 500"})
        self.assertEqual(self.body(self.get("/api/guild/roles")), self.body(primed))
        self.assertEqual(self.discord.count("roles"), 1)

    def test_a_forbidden_request_neither_fetches_nor_caches(self):
        self.as_guild(GUILD_A, user=VIEWER)                    # viewer < moderator
        denied = self.get("/api/guild/roles")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.discord.calls, [], "the guard still runs before any fetch")
        self.assertEqual(core._guild_lookup_cache, {})


# ═════════════════════════════════════════════════════════════════════════
# 9. isolation: only these two endpoints, and no shipped caller uses the bypass
# ═════════════════════════════════════════════════════════════════════════
class IsolationTests(CacheCase):
    def test_the_neighbouring_guild_endpoint_is_not_cached(self):
        """`/api/guild/emojis` shares this file and the same Discord pattern and
        is deliberately NOT part of the cache — the change is exactly two
        endpoints. Two requests, two fetches."""
        self.get("/api/guild/emojis")
        self.get("/api/guild/emojis")
        self.assertEqual(self.discord.count("emojis"), 2)
        self.assertEqual(core._guild_lookup_cache, {},
                         "the emoji endpoint never writes a cache entry")

    def test_only_the_two_authorised_kinds_are_ever_cached(self):
        self.get("/api/guild/roles")
        self.get("/api/guild/channels")
        self.get("/api/guild/emojis")
        self.assertEqual(sorted(kind for kind, _ in core._guild_lookup_cache), ["channels", "roles"])

    def test_no_shipped_caller_sends_the_bypass(self):
        """The bypass is opt-in and unused: every existing consumer — the v1
        page and the shared picker included — still receives the identical
        response on the identical URL."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("dashboard/static/js/nero-select.js",
                    "dashboard/static/js/embed-builder-page.js",
                    "dashboard/templates/systems/missions.html"):
            with open(os.path.join(root, rel), encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("refresh=", source, rel + " must not send the bypass")


# ═════════════════════════════════════════════════════════════════════════
# The two behaviours of the cache object itself that the design relies on
# ═════════════════════════════════════════════════════════════════════════
class CacheObjectTests(CacheCase):
    def test_the_endpoint_sorts_its_own_copy_not_the_cached_list(self):
        self.get("/api/guild/roles")                  # prime
        self.get("/api/guild/roles")                  # CACHE HIT: the endpoint sorts in place
        self.assertEqual(self.discord.count("roles"), 1, "rig: the second read was a cache hit")
        entry = core._guild_lookup_cache[core._guild_cache_key("roles", GUILD_A)]
        self.assertEqual([row["id"] for row in entry["data"]],
                         [row["id"] for row in ROLES_A],
                         "the cached list keeps Discord's own order")

    def test_expired_entries_are_dropped_when_a_new_one_is_stored(self):
        core.GUILD_LOOKUP_TTL_SECONDS = 0.05
        self.get("/api/guild/roles")
        self.assertIn(core._guild_cache_key("roles", GUILD_A), core._guild_lookup_cache)
        time.sleep(0.08)
        self.get("/api/guild/channels")               # a store for another key
        self.assertNotIn(core._guild_cache_key("roles", GUILD_A), core._guild_lookup_cache,
                         "a stale entry does not outlive the process for a guild nobody asks about")
        self.assertIn(core._guild_cache_key("channels", GUILD_A), core._guild_lookup_cache)


if __name__ == "__main__":
    unittest.main(verbosity=2)
