"""Dashboard integration regressions: Streak V2, Prestige multipliers, MVP.

Drives the real Flask routes against a scratch SQLite DB with two guilds and
checks UI payload -> API -> validation -> guild-scoped storage -> reload.

Run:  PYTHONPATH=. python3 -m unittest tests.test_dashboard_streak_prestige_mvp
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="dash_spm_")
DB = os.path.join(_TMP, "test.db")
os.environ["DATABASE_PATH"] = DB
os.environ["OWNER_ID"] = "999999999"
os.environ.setdefault("SECRET_KEY", "testsecretkey0123456789abcdef0123456789")

import dashboard.app as dapp  # noqa: E402

app = dapp.app
app.config["TESTING"] = True
app.config["PROPAGATE_EXCEPTIONS"] = False   # a 500 must fail an assertion, not crash the runner

G1, G2, G_FRESH, USER, CSRF = 1001, 1002, 1004, 100, "csrf"
_conn = sqlite3.connect(DB)
for g in (G1, G2, G_FRESH):
    _conn.execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) VALUES (?,?,?,1)",
                  (g, USER, "admin"))
_conn.commit()
_conn.close()


def client(guild):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = {"id": USER, "username": "tester", "avatar": None}
        s["guild_id"] = guild
        s["expires_at"] = time.time() + 7200
        s["csrf_token"] = CSRF
    return c


def post(c, path, body):
    raw = body if isinstance(body, str) else json.dumps(body)   # raw str lets tests send NaN/Infinity
    return c.post(path, data=raw, content_type="application/json", headers={"X-CSRF-Token": CSRF})


def rows(sql, *args):
    cn = sqlite3.connect(DB)
    try:
        return cn.execute(sql, args).fetchall()
    finally:
        cn.close()


MVP_OK = {"cycle_hours": 6, "winner_ranks": 3, "rank_rewards": [100, 50, 25],
          "chat_word_weight": 1.0, "voice_minute_weight": 2.0, "min_activity_words": 40}


class StreakDashboard(unittest.TestCase):
    URL = "/api/economy/streak-settings"

    def test_per_guild_isolation_and_reload(self):
        a, b = client(G1), client(G2)
        r = post(a, self.URL, {"stages": [10, 20, 30, 40, 50], "stage6_min": 60, "stage6_max": 90})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(a.get(self.URL).get_json()["settings"]["reward_table"],
                         {"stages": [10, 20, 30, 40, 50], "stage6_min": 60, "stage6_max": 90})
        self.assertIsNone(b.get(self.URL).get_json()["settings"]["reward_table"])   # no silent inheritance
        post(b, self.URL, {"stages": [1, 2, 3, 4, 5], "stage6_min": 6, "stage6_max": 7})
        self.assertEqual(a.get(self.URL).get_json()["settings"]["reward_table"]["stages"], [10, 20, 30, 40, 50])

    def test_validation_errors_are_specific_and_change_nothing(self):
        a = client(G1)
        post(a, self.URL, {"stages": [10, 20, 30, 40, 50], "stage6_min": 60, "stage6_max": 90})
        for body, text in (
            ({"stages": [1, 2, 3, 4], "stage6_min": 1, "stage6_max": 2}, "five"),
            ({"stages": [1, 2, 3, 4, 5], "stage6_min": 9, "stage6_max": 2}, "maximum"),
            ({"stages": [1, 2, 3, 4, "x"], "stage6_min": 1, "stage6_max": 2}, "Day 5"),
            ({"stages": [1, 2, 3, 4, 5], "stage6_min": 1, "stage6_max": ""}, "all six"),
            ({"stages": [0, 2, 3, 4, 5], "stage6_min": 1, "stage6_max": 2}, "Day 1"),
        ):
            r = post(a, self.URL, body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(text, r.get_json()["error"])
        self.assertEqual(a.get(self.URL).get_json()["settings"]["reward_table"]["stages"], [10, 20, 30, 40, 50])

    def test_template_has_no_percentage_controls(self):
        html = open("dashboard/templates/systems/economy.html", encoding="utf-8").read().lower()
        for stale in ("percent", "streak_bonus", "bonus_per_day", "bonus %"):
            self.assertNotIn(stale, html)


class PrestigeDashboard(unittest.TestCase):
    URL = "/api/leveling/prestige-config"

    def test_defaults_show_v_and_vi(self):
        t = client(G2).get(self.URL).get_json()["config"]["tiers"]
        self.assertEqual((t["5"]["coins"], t["5"]["diamonds"]), (1.2, 1.1))
        self.assertEqual((t["6"]["coins"], t["6"]["diamonds"]), (1.3, 1.2))

    def test_save_reload_and_guild_isolation(self):
        a, b = client(G1), client(G2)
        before_b = b.get(self.URL).get_json()["config"]["tiers"]
        body = {"enabled": True, "tiers": {"5": {"coins": 1.5, "diamonds": 1.25}, "6": {"coins": 2, "diamonds": 1.5}}}
        self.assertEqual(post(a, self.URL, body).status_code, 200)
        got = a.get(self.URL).get_json()["config"]["tiers"]
        self.assertEqual((got["5"]["coins"], got["5"]["diamonds"], got["6"]["coins"]), (1.5, 1.25, 2.0))
        self.assertEqual(got["1"], {"coins": 1.0, "diamonds": 1.0})            # other tiers untouched
        self.assertEqual(b.get(self.URL).get_json()["config"]["tiers"], before_b)  # other guild untouched

    def test_omitted_enabled_does_not_reenable(self):
        a = client(G1)
        post(a, self.URL, {"enabled": False})
        self.assertEqual(a.get(self.URL).get_json()["config"]["enabled"], 0)
        post(a, self.URL, {"tiers": {"4": {"coins": 1.1, "diamonds": 1.1}}})
        self.assertEqual(a.get(self.URL).get_json()["config"]["enabled"], 0)
        post(a, self.URL, {"enabled": True})
        self.assertEqual(a.get(self.URL).get_json()["config"]["enabled"], 1)

    def test_invalid_values_rejected_all_or_nothing(self):
        a = client(G1)
        post(a, self.URL, {"enabled": True, "tiers": {"3": {"coins": 1.3, "diamonds": 1.3}}})
        bad = [
            '{"tiers":{"3":{"coins":2,"diamonds":2},"5":{"coins":Infinity,"diamonds":1}}}',
            '{"tiers":{"3":{"coins":2,"diamonds":2},"5":{"coins":NaN,"diamonds":1}}}',
            {"tiers": {"3": {"coins": 2, "diamonds": 2}, "5": {"coins": 0, "diamonds": 1}}},
            {"tiers": {"3": {"coins": 2, "diamonds": 2}, "5": {"coins": -1, "diamonds": 1}}},
            {"tiers": {"3": {"coins": 2, "diamonds": 2}, "5": {"coins": 101, "diamonds": 1}}},
            {"tiers": {"3": {"coins": 2, "diamonds": 2}, "9": {"coins": 2, "diamonds": 2}}},
            {"tiers": {"3": {"coins": 2, "diamonds": 2}, "4": {"coins": "abc", "diamonds": 1}}},
            {"tiers": {"3": {"coins": 2, "diamonds": 2}, "4": {"coins": 2}}},
            {"enabled": "maybe"},
        ]
        for body in bad:
            r = post(a, self.URL, body)
            self.assertEqual(r.status_code, 400, body)
            self.assertFalse(r.get_json()["success"])
        t = a.get(self.URL).get_json()["config"]["tiers"]
        self.assertEqual((t["3"]["coins"], t["3"]["diamonds"]), (1.3, 1.3))        # nothing partially applied
        self.assertEqual(rows("SELECT COUNT(*) FROM prestige_tiers WHERE guild_id=? AND tier=9", G1)[0][0], 0)

    def test_missing_rows_display_the_engine_default(self):
        g = 1003
        _c = sqlite3.connect(DB)
        _c.execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) VALUES (?,?,?,1)", (g, USER, "admin"))
        _c.execute("INSERT INTO prestige_tiers (guild_id,tier,coins_multiplier,diamonds_multiplier) VALUES (?,?,?,?)", (g, 2, 1.0, 1.0))
        _c.commit(); _c.close()
        t = client(g).get(self.URL).get_json()["config"]["tiers"]
        self.assertEqual(sorted(t), ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(t["5"]["coins"], 1.2)       # what get_effective_prestige_multipliers would apply

    def test_page_separates_multipliers_from_shop_prices(self):
        html = open("dashboard/templates/systems/leveling.html", encoding="utf-8").read()
        self.assertIn("Shop Designer", html)


class MvpDashboard(unittest.TestCase):
    URL = "/api/mvp/config"

    def test_unconfigured_guild_returns_empty_not_another_guilds_config(self):
        post(client(G1), self.URL, MVP_OK)
        self.assertEqual(client(G_FRESH).get(self.URL).get_json()["config"], {})

    def test_save_reload_isolation_and_threshold_preview(self):
        a, b = client(G1), client(G2)
        body = dict(MVP_OK, mvp_role_id=None, announce_channel_id="555", chat_word_weight=0.5, voice_minute_weight=4.0)
        self.assertEqual(post(a, self.URL, body).status_code, 200)
        cfg = a.get(self.URL).get_json()["config"]
        self.assertEqual((cfg["winner_ranks"], cfg["rank_rewards"], cfg["cycle_hours"]), (3, [100, 50, 25], 6))
        self.assertEqual(cfg["min_activity_points"], 20.0)               # 40 words x THIS guild's 0.5
        self.assertEqual(cfg["min_activity_voice_minutes"], 5.0)         # 20 points / 4 per minute
        self.assertEqual(cfg["announce_channel_id"], "555")
        post(b, self.URL, dict(MVP_OK, winner_ranks=1, rank_rewards=[7], chat_word_weight=2.0))
        again = a.get(self.URL).get_json()["config"]
        self.assertEqual((again["winner_ranks"], again["rank_rewards"], again["chat_word_weight"]), (3, [100, 50, 25], 0.5))
        self.assertEqual(b.get(self.URL).get_json()["config"]["min_activity_points"], 80.0)

    def test_zero_weights_and_zero_rewards_round_trip(self):
        a = client(G1)
        r = post(a, self.URL, dict(MVP_OK, chat_word_weight=0, voice_minute_weight=0, rank_rewards=[0, 0, 0], min_activity_words=0))
        self.assertEqual(r.status_code, 200)
        cfg = a.get(self.URL).get_json()["config"]
        self.assertEqual((cfg["chat_word_weight"], cfg["voice_minute_weight"], cfg["rank_rewards"]), (0.0, 0.0, [0, 0, 0]))
        self.assertIsNone(cfg["min_activity_voice_minutes"])

    def test_invalid_input_is_a_400_and_stores_nothing(self):
        a = client(G1)
        post(a, self.URL, MVP_OK)
        bad = [
            '{"chat_word_weight":Infinity}', '{"chat_word_weight":NaN}', '{"min_activity_words":NaN}',
            '{"voice_minute_weight":Infinity}', {"chat_word_weight": 2_000_000},
            {"cycle_hours": 0}, {"cycle_hours": 169}, {"cycle_hours": 6.5}, {"cycle_hours": ""},
            {"winner_ranks": 0}, {"winner_ranks": 11}, {"winner_ranks": 2.7},
            {"winner_ranks": 2, "rank_rewards": [1, -5]}, {"winner_ranks": 2, "rank_rewards": [1, "x"]},
            {"winner_ranks": 2, "rank_rewards": [1.5, 2]}, {"rank_rewards": "5"},
            {"min_activity_words": -1}, {"chat_word_weight": -1}, {"chat_word_weight": True},
        ]
        for body in bad:
            r = post(a, self.URL, body)
            self.assertEqual(r.status_code, 400, body)
            self.assertFalse(r.get_json()["success"])
        cfg = a.get(self.URL).get_json()["config"]
        self.assertEqual((cfg["cycle_hours"], cfg["winner_ranks"], cfg["rank_rewards"], cfg["chat_word_weight"]),
                         (6, 3, [100, 50, 25], 1.0))

    def test_partial_request_keeps_role_channel_and_other_fields(self):
        b = client(G2)
        post(b, self.URL, dict(MVP_OK, announce_channel_id="555", chat_word_weight=1.5))
        self.assertEqual(post(b, self.URL, {"cycle_hours": 8}).status_code, 200)
        cfg = b.get(self.URL).get_json()["config"]
        self.assertEqual((cfg["announce_channel_id"], cfg["cycle_hours"], cfg["chat_word_weight"], cfg["rank_rewards"]),
                         ("555", 8, 1.5, [100, 50, 25]))
        # An explicit null still clears the channel.
        post(b, self.URL, dict(MVP_OK, announce_channel_id=None))
        self.assertIsNone(b.get(self.URL).get_json()["config"]["announce_channel_id"])

    def test_page_explains_ties_and_shares_reward(self):
        html = open("dashboard/templates/systems/mvp.html", encoding="utf-8").read()
        self.assertIn("tied member receives that rank", html)
        self.assertIn("mvp-reward-preview", html)


if __name__ == "__main__":
    unittest.main()
