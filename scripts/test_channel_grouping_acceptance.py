import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ.setdefault("NERO_ENVIRONMENT", "staging")
os.environ.setdefault("STAGING_DATABASE_PATH", "/tmp/test_staging_grouping.db")
os.environ.setdefault("DATABASE_PATH", "/tmp/test_prod_grouping.db")
os.environ.setdefault("OWNER_ID", "999999999")
os.environ.setdefault("SECRET_KEY", "testing_secret_key_12345678901234567890")

import unittest
import json
from unittest.mock import patch, MagicMock
from flask import session
from dashboard.app import app
from dashboard.api.core import get_guild_channels

class TestChannelGroupingAcceptance(unittest.TestCase):
    def test_channels_api_grouping_and_svg_icon_keys(self):
        mock_raw_channels = [
            {"id": "200", "name": "Voice Lounges", "type": 4, "position": 1, "parent_id": None},
            {"id": "201", "name": "General Voice", "type": 2, "position": 0, "parent_id": "200"},
            {"id": "101", "name": "rules", "type": 0, "position": 0, "parent_id": "100"},
            {"id": "100", "name": "Welcome & Info", "type": 4, "position": 0, "parent_id": None},
            {"id": "301", "name": "sandbox", "type": 0, "position": 0, "parent_id": None},
            {"id": "102", "name": "announcements", "type": 5, "position": 1, "parent_id": "100"},
            {"id": "401", "name": "stage-talk", "type": 13, "position": 0, "parent_id": "200"},
            {"id": "501", "name": "forum-discussions", "type": 15, "position": 2, "parent_id": "100"},
        ]
        
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_raw_channels

        with app.test_request_context():
            session["user"] = {"id": "999999999", "username": "Admin"}
            session["expires_at"] = 9999999999
            session["guild_id"] = "123456"
            session["guild_permissions"] = 0x20 # MANAGE_GUILD

            with patch.dict(os.environ, {"DISCORD_TOKEN": "mock_token"}):
                with patch('requests.get', return_value=mock_resp):
                    response = get_guild_channels()
                    data = response.get_json() if hasattr(response, 'get_json') else json.loads(response[0].get_data(as_text=True))
                    
                    self.assertIn("results", data)
                    channels = data["results"]
                    
                    # 1. Categories themselves (type 4) must NOT be in the channel selectable rows
                    cat_ids = [c["id"] for c in channels]
                    self.assertNotIn("100", cat_ids)
                    self.assertNotIn("200", cat_ids)
                    
                    # 2. Check total selectable channels count
                    self.assertEqual(len(channels), 6)
                    
                    # 3. Check Discord category & channel hierarchy ordering:
                    # Welcome & Info (pos 0): rules (pos 0), announcements (pos 1), forum (pos 2)
                    self.assertEqual(channels[0]["name"], "rules")
                    self.assertEqual(channels[0]["category"], "Welcome & Info")
                    self.assertEqual(channels[0]["type_icon"], "#")
                    
                    self.assertEqual(channels[1]["name"], "announcements")
                    self.assertEqual(channels[1]["category"], "Welcome & Info")
                    self.assertEqual(channels[1]["type_icon"], "megaphone")
                    
                    self.assertEqual(channels[2]["name"], "forum-discussions")
                    self.assertEqual(channels[2]["category"], "Welcome & Info")
                    self.assertEqual(channels[2]["type_icon"], "forum")
                    
                    # Voice Lounges (pos 1): General Voice (pos 0), stage-talk (pos 0)
                    self.assertEqual(channels[3]["name"], "General Voice")
                    self.assertEqual(channels[3]["category"], "Voice Lounges")
                    self.assertEqual(channels[3]["type_icon"], "speaker")

                    self.assertEqual(channels[4]["name"], "stage-talk")
                    self.assertEqual(channels[4]["category"], "Voice Lounges")
                    self.assertEqual(channels[4]["type_icon"], "mic")

                    # Uncategorized (cat_pos 9999)
                    self.assertEqual(channels[5]["name"], "sandbox")
                    self.assertEqual(channels[5]["category"], "")
                    self.assertEqual(channels[5]["type_icon"], "#")

    def test_channels_api_flat_server_and_missing_metadata(self):
        # Server with no categories and some missing optional fields
        mock_raw_channels = [
            {"id": "1", "name": "general", "type": 0, "position": 0},
            {"id": "2", "name": "random", "type": 0, "position": 1, "parent_id": None},
            {"id": "3", "type": 2, "position": 2}, # missing name
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_raw_channels

        with app.test_request_context():
            session["user"] = {"id": "999999999", "username": "Admin"}
            session["expires_at"] = 9999999999
            session["guild_id"] = "123456"
            session["guild_permissions"] = 0x20

            with patch.dict(os.environ, {"DISCORD_TOKEN": "mock_token"}):
                with patch('requests.get', return_value=mock_resp):
                    response = get_guild_channels()
                    data = response.get_json() if hasattr(response, 'get_json') else json.loads(response[0].get_data(as_text=True))
                    
                    self.assertIn("results", data)
                    channels = data["results"]
                    self.assertEqual(len(channels), 3)
                    self.assertEqual(channels[0]["name"], "general")
                    self.assertEqual(channels[1]["name"], "random")
                    self.assertEqual(channels[2]["type_icon"], "speaker")

if __name__ == '__main__':
    unittest.main()
