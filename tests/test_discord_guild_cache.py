import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.gettempdir(), "nilive-guild-cache-test.sqlite"))
os.environ.setdefault("DISCORD_TOKEN", "cache-test-token")

from flask import Flask
from dashboard.api import core
from dashboard.utils import discord_guild_cache as cache

APP = Flask(__name__)


def setup_function():
    cache.clear_cache()


def _call_route(route, path):
    with APP.test_request_context(path):
        response = route.__wrapped__()
        return response.get_json()


def test_cache_hits_expire_and_force_refresh(monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_TTL_SECONDS", 0.02)
    calls = []

    def load():
        calls.append(len(calls) + 1)
        return calls[-1], True

    assert cache.get_or_load("roles", 7, "sensitive-token", load) == 1
    assert cache.get_or_load("roles", 7, "sensitive-token", load) == 1
    assert cache.get_or_load("roles", 7, "sensitive-token", load, refresh=True) == 2
    time.sleep(0.025)
    assert cache.get_or_load("roles", 7, "sensitive-token", load) == 3
    assert calls == [1, 2, 3]
    assert all("sensitive-token" not in repr(key) for key in cache._CACHE)


def test_concurrent_cold_misses_are_coalesced():
    calls = 0
    guard = threading.Lock()
    results = []

    def load():
        nonlocal calls
        with guard:
            calls += 1
        time.sleep(0.03)
        return {"roles": []}, True

    def worker():
        results.append(cache.get_or_load("roles", 8, "token", load))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert results == [{"roles": []}] * 8


def test_failures_are_not_cached_and_later_success_recovers():
    calls = []

    def failure():
        calls.append("failure")
        return {"error": "unavailable"}, False

    def success():
        calls.append("success")
        return {"channels": []}, True

    assert cache.get_or_load("channels", 9, "token", failure) == {"error": "unavailable"}
    assert cache.get_or_load("channels", 9, "token", success) == {"channels": []}
    assert cache.get_or_load("channels", 9, "token", failure) == {"channels": []}
    assert calls == ["failure", "success"]


def test_failed_forced_refresh_drops_old_entry_and_next_call_retries():
    calls = []

    def old_value():
        calls.append("old")
        return "old", True

    def failed_refresh():
        calls.append("refresh-error")
        return "error", False

    def recovered():
        calls.append("recovered")
        return "new", True

    assert cache.get_or_load("roles", 12, "token", old_value) == "old"
    assert cache.get_or_load("roles", 12, "token", failed_refresh, refresh=True) == "error"
    assert cache.get_or_load("roles", 12, "token", recovered) == "new"
    assert calls == ["old", "refresh-error", "recovered"]


def test_channels_endpoint_caches_valid_payload_and_force_refreshes(monkeypatch):
    monkeypatch.setattr(core, "get_session_guild_id", lambda: 9001)
    monkeypatch.setenv("DISCORD_TOKEN", "channels-test-token")
    responses = [
        SimpleNamespace(status_code=200, json=lambda: [
            {"id": "c1", "name": "general", "type": 0, "position": 1}
        ]),
        SimpleNamespace(status_code=200, json=lambda: [
            {"id": "c2", "name": "new", "type": 0, "position": 2}
        ]),
    ]
    calls = []

    def request(*args, **kwargs):
        calls.append(args[0])
        return responses.pop(0)

    monkeypatch.setattr(core._req, "get", request)
    first = _call_route(core.get_guild_channels, "/api/guild/channels")
    second = _call_route(core.get_guild_channels, "/api/guild/channels")
    refreshed = _call_route(core.get_guild_channels, "/api/guild/channels?refresh=1")

    assert len(calls) == 2
    assert first["results"][0]["id"] == "c1"
    assert second == first
    assert refreshed["results"][0]["id"] == "c2"


def test_channels_endpoint_does_not_cache_api_errors(monkeypatch):
    monkeypatch.setattr(core, "get_session_guild_id", lambda: 9002)
    monkeypatch.setenv("DISCORD_TOKEN", "channels-failure-token")
    responses = [
        SimpleNamespace(status_code=429, json=lambda: {}),
        SimpleNamespace(status_code=200, json=lambda: [
            {"id": "c3", "name": "recovered", "type": 0, "position": 0}
        ]),
    ]
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        return responses.pop(0)

    monkeypatch.setattr(core._req, "get", request)
    failed = _call_route(core.get_guild_channels, "/api/guild/channels")
    recovered = _call_route(core.get_guild_channels, "/api/guild/channels")

    assert len(calls) == 2
    assert failed["error"] == "Discord 429"
    assert recovered["results"][0]["name"] == "recovered"


def test_roles_endpoint_caches_raw_roles_and_member_state(monkeypatch):
    monkeypatch.setattr(core, "get_session_guild_id", lambda: 9003)
    monkeypatch.setenv("DISCORD_TOKEN", "roles-test-token")
    roles = [
        {"id": "9003", "name": "@everyone", "position": 0, "color": 0,
         "managed": False, "permissions": "0"},
        {"id": "role1", "name": "Members", "position": 1, "color": 255,
         "managed": False, "permissions": "0"},
    ]
    member = {"user": {"id": "bot1"}, "roles": ["role1"], "permissions": str(1 << 28)}
    calls = []

    def fetch(_guild_id):
        calls.append(_guild_id)
        return roles, member, None

    from dashboard.utils import role_validation
    monkeypatch.setattr(role_validation, "fetch_guild_roles_and_bot_member", fetch)
    first = _call_route(core.get_guild_roles, "/api/guild/roles")
    second = _call_route(core.get_guild_roles, "/api/guild/roles")
    refreshed = _call_route(core.get_guild_roles, "/api/guild/roles?refresh=1")

    assert calls == [9003, 9003]
    assert first == second == refreshed
    assert first["bot_status"] == "present"
    assert [role["id"] for role in first["results"]] == ["role1", "9003"]


def test_unknown_role_member_state_is_not_cached_then_recovers(monkeypatch):
    monkeypatch.setattr(core, "get_session_guild_id", lambda: 9004)
    monkeypatch.setenv("DISCORD_TOKEN", "roles-recovery-token")
    roles = [{"id": "9004", "name": "@everyone", "position": 0, "color": 0,
              "managed": False, "permissions": "0"}]
    unknown = {"_bot_status": "unknown", "_error": "Discord 503"}
    present = {"user": {"id": "bot2"}, "roles": [], "permissions": "0"}
    replies = [(roles, unknown, None), (roles, present, None)]
    calls = []

    def fetch(_guild_id):
        calls.append(_guild_id)
        return replies.pop(0)

    from dashboard.utils import role_validation
    monkeypatch.setattr(role_validation, "fetch_guild_roles_and_bot_member", fetch)
    first = _call_route(core.get_guild_roles, "/api/guild/roles")
    recovered = _call_route(core.get_guild_roles, "/api/guild/roles")

    assert calls == [9004, 9004]
    assert first["bot_status"] == "unknown"
    assert recovered["bot_status"] == "present"
