"""Shop Product Management — Add / Edit / Delete CRUD + finalized type rules.

Covers the decisions locked for this pass (dashboard API surface only; the
purchase engine, ledger, timed ownership, cogs/shop.py and wallet/inventory are
out of scope and untouched):

  * the five admin-facing product types — Item (internal `custom`), Color
    (internal `color`), Potion, Title, Prestige — plus their rules:
      - Color grants a role, so it requires a role_id (like role/temp_role);
      - Title and Prestige are permanent (a submitted duration is normalized
        away); Item/Color keep duration semantics (0 = permanent, >0 = timed).
  * NEW XP Boost creation is refused at the API, while EXISTING boosts (and
    role/temp_role) stay editable on their safe fields and keep their type.
  * the real update endpoint PUT /api/shop/item/<id>: permission + CSRF +
    validation, `type`/`category_id`/`option_of_id` immutability, the stock
    rules (current_stock is ALWAYS preserved — an explicit current_stock key is
    ignored; editing the maximum never restocks; the maximum can't drop below
    the preserved current stock),
    and the explicit icon_url clear (absent key keeps, present-but-blank clears).
  * the item_catalog COALESCE review: an edit keeps the rank-card catalog in
    step, but a blanked shop icon does NOT wipe the catalog's cached icon.
  * the admin table renders the correct type display names and an Edit button,
    and the root-with-options delete guard is not weakened by editing.

Real Flask routes + permissions + CSRF + scratch SQLite; no HTTP server.
Run:  python -m pytest test_shop_product_management.py
      python test_shop_product_management.py
"""
import asyncio
import time
import unittest

from phase1_support import (
    GUILD, USER, execute, reset_database, rows, mutation_snapshot,
)
import dashboard.app as dashboard

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

OTHER_GUILD = 99901   # a second guild — proves cross-guild isolation
CSRF = "pm-csrf"


def seed_item(guild=GUILD, name="VIP", kind="role", price=1500, role_id=99,
              duration=None, max_stock=None, current_stock=None, icon=None,
              rarity="common", category_id=None, option_of_id=None,
              prestige_tier=None, diamonds=None, multiplier=None,
              required_level=0, description=None, featured=0, enabled=1):
    """Insert a listing directly (bypassing the API) so a test can start from
    an exact stored state — including legacy types the create form no longer
    offers."""
    return execute(
        "INSERT INTO shop_items (guild_id,name,type,price,description,role_id,"
        "duration_hours,max_stock,current_stock,required_level,required_role_id,"
        "price_diamonds,icon_url,rarity,prestige_tier,xp_boost_multiplier,"
        "category_id,option_of_id,featured,enabled) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (guild, name, kind, price, description, role_id, duration, max_stock,
         current_stock, required_level, None, diamonds, icon, rarity,
         prestige_tier, multiplier, category_id, option_of_id, featured, enabled))


class ProductManagementTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'admin',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "PM", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token=CSRF)

    # ── request helpers ──────────────────────────────────────────────────
    def post(self, payload, csrf=True):
        return self.client.post("/api/shop/item", json=payload,
                                headers={"X-CSRF-Token": CSRF} if csrf else {})

    def put(self, item_id, payload, csrf=True):
        return self.client.put(f"/api/shop/item/{item_id}", json=payload,
                               headers={"X-CSRF-Token": CSRF} if csrf else {})

    def get(self, item_id):
        return self.client.get(f"/api/shop/item/{item_id}")

    def delete(self, item_id, csrf=True):
        return self.client.delete(f"/api/shop/item/{item_id}",
                                  headers={"X-CSRF-Token": CSRF} if csrf else {})

    def one(self, sql, args=()):
        r = rows(sql, args)
        return r[0] if r else None

    def field(self, item_id, column):
        return self.one(f"SELECT {column} FROM shop_items WHERE id=?", (item_id,))[0]

    # ══ ADD — the five current types + the XP Boost block ═══════════════
    def test_add_item_custom_succeeds(self):
        r = self.post(dict(name="Cape", type="custom", price=250))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIs(r.get_json()["success"], True)
        self.assertEqual(self.one("SELECT type,price FROM shop_items WHERE name='Cape'"),
                         ("custom", 250))

    def test_add_color_succeeds_and_stores_internal_value(self):
        r = self.post(dict(name="Red Name", type="color", price=300, role_id=4242))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.one("SELECT type,price,role_id FROM shop_items WHERE name='Red Name'"),
            ("color", 300, 4242))

    def test_add_color_requires_role_id(self):
        before = mutation_snapshot()
        r = self.post(dict(name="No Role Colour", type="color", price=300))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIs(r.get_json()["success"], False)
        self.assertIn("role", str(r.get_json()["error"]).lower())
        self.assertEqual(mutation_snapshot(), before, "an invalid color was persisted")

    def test_add_xp_boost_is_blocked_but_not_persisted(self):
        before = mutation_snapshot()
        r = self.post(dict(name="Boost", type="xp_boost", price=100,
                           xp_boost_multiplier=2, duration_hours=4))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIs(r.get_json()["success"], False)
        self.assertIn("xp boost", str(r.get_json()["error"]).lower())
        self.assertEqual(mutation_snapshot(), before, "a blocked XP Boost was persisted")

    def test_add_legacy_role_type_still_accepted_for_backward_compatibility(self):
        # role/temp_role are no longer OFFERED by the create form, but the API
        # keeps accepting them so nothing that already integrates breaks; only
        # XP Boost creation is explicitly refused.
        r = self.post(dict(name="Roll", type="role", price=100, role_id=123))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.field(rows("SELECT id FROM shop_items WHERE name='Roll'")[0][0], "type"),
                         "role")

    def test_add_title_with_a_duration_is_rejected_as_permanent(self):
        before = mutation_snapshot()
        r = self.post(dict(name="Hero", type="title", price=0, duration_hours=8))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn("permanent", str(r.get_json()["error"]).lower())
        self.assertEqual(mutation_snapshot(), before)
        # a permanent Title (no duration) is still accepted
        self.assertEqual(self.post(dict(name="Hero", type="title", price=0)).status_code, 200)
        self.assertIsNone(self.field(rows("SELECT id FROM shop_items WHERE name='Hero'")[0][0],
                                     "duration_hours"))

    def test_add_prestige_i_to_v_with_a_duration_is_rejected_as_permanent(self):
        before = mutation_snapshot()
        r = self.post(dict(name="Prestige III", type="prestige", price=500,
                           prestige_tier=3, duration_hours=8))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(mutation_snapshot(), before)
        # permanent Prestige (no duration) still persists its tier and price
        self.assertEqual(
            self.post(dict(name="Prestige III", type="prestige", price=500,
                           prestige_tier=3)).status_code, 200)
        self.assertEqual(
            self.one("SELECT duration_hours,price,prestige_tier FROM shop_items "
                     "WHERE name='Prestige III'"), (None, 500, 3))

    # ══ GET — edit prefill ══════════════════════════════════════════════
    def test_get_returns_the_full_row_for_prefill(self):
        item = seed_item(kind="custom", name="Cape", price=250, icon="https://x/i.png",
                         rarity="epic", max_stock=20, current_stock=12)
        r = self.get(item)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertIs(body["success"], True)
        self.assertEqual(body["item"]["id"], item)
        self.assertEqual(body["item"]["type"], "custom")
        self.assertEqual(body["item"]["max_stock"], 20)
        self.assertEqual(body["item"]["current_stock"], 12)
        self.assertEqual(body["item"]["icon_url"], "https://x/i.png")

    def test_get_is_guild_scoped(self):
        other = seed_item(guild=OTHER_GUILD, name="Foreign", kind="custom", price=1)
        self.assertEqual(self.get(other).status_code, 404)
        self.assertEqual(self.get(123456).status_code, 404)

    # ══ EDIT — safe fields, immutability, stock, icon ═══════════════════
    def test_edit_updates_safe_fields(self):
        item = seed_item(kind="custom", name="Cape", price=250, description="old",
                         rarity="common")
        r = self.put(item, dict(name="Royal Cape", price=400, description="new",
                                rarity="legendary"))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIs(r.get_json()["success"], True)
        self.assertEqual(
            self.one("SELECT name,price,description,rarity FROM shop_items WHERE id=?", (item,)),
            ("Royal Cape", 400, "new", "legendary"))

    def test_edit_cannot_change_the_type(self):
        item = seed_item(kind="custom", name="Cape", price=250)
        r = self.put(item, dict(type="title"))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.field(item, "type"), "custom")

    def test_edit_cannot_smuggle_in_a_new_xp_boost_by_retyping(self):
        item = seed_item(kind="custom", name="Cape", price=250)
        r = self.put(item, dict(type="xp_boost", xp_boost_multiplier=2, duration_hours=4))
        # `type` is immutable, so this is a no-op re-type: the row STAYS a custom
        # Item and never becomes an XP Boost. (A stray xp_boost_multiplier is
        # inert on a non-boost type — the purchase engine only reads it for
        # potion/xp_boost — and the create/edit form never sends one for Item,
        # exactly as the add path already behaves.)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.field(item, "type"), "custom")
        self.assertEqual(self.field(item, "duration_hours"), 4)

    def test_edit_preserves_category_id(self):
        cat = execute("INSERT INTO shop_categories (guild_id,name,emoji,enabled,sort_order) "
                      "VALUES (?, 'Boosts', '⚡', 1, 0)", (GUILD,))
        item = seed_item(kind="custom", name="Cape", price=250, category_id=cat)
        r = self.put(item, dict(name="Cape v2"))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.field(item, "category_id"), cat)

    def test_edit_preserves_option_of_id_and_keeps_the_family_intact(self):
        root = seed_item(kind="custom", name="Bundle", price=100)
        child = seed_item(kind="custom", name="Option A", price=50, option_of_id=root)
        r = self.put(child, dict(name="Option A v2", price=60))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.field(child, "option_of_id"), root)
        # the root still has its option, so the delete guard must still hold
        self.assertEqual(self.delete(root).status_code, 409)

    def test_edit_max_stock_does_not_restock_current(self):
        item = seed_item(kind="custom", name="Cape", price=250, max_stock=20, current_stock=12)
        r = self.put(item, dict(max_stock=30))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.one("SELECT max_stock,current_stock FROM shop_items WHERE id=?",
                                  (item,)), (30, 12))

    def test_edit_max_stock_below_current_is_rejected(self):
        item = seed_item(kind="custom", name="Cape", price=250, max_stock=20, current_stock=12)
        before = mutation_snapshot()
        r = self.put(item, dict(max_stock=5))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn("stock", str(r.get_json()["error"]).lower())
        self.assertEqual(mutation_snapshot(), before, "a rejected stock edit changed the row")

    def test_edit_unlimited_product_stays_unlimited_through_unrelated_edits(self):
        # Unlimited products store (max_stock=NULL, current_stock=NULL).
        # Unrelated edits — including an explicit max_stock null, which is
        # what the UI sends for an unlimited listing — must succeed and keep
        # BOTH stock columns NULL: editing never initializes stock.
        item = seed_item(kind="custom", name="Endless Cape", price=250)
        r = self.put(item, dict(name="Endless Cape v2", price=300, max_stock=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.one("SELECT name,price,max_stock,current_stock FROM shop_items WHERE id=?",
                     (item,)),
            ("Endless Cape v2", 300, None, None))

    def test_edit_unlimited_to_finite_stock_transition_is_rejected(self):
        # STOCK-INTEGRITY GUARD: an unlimited product (current_stock=NULL)
        # must not be given a finite max_stock by editing. PUT never writes
        # current_stock, so allowing it would produce (max_stock=N,
        # current_stock=NULL) — a row safe_decrement_stock() can never
        # decrement (NULL - 1 stays NULL in SQLite) nor exhaust, i.e. an
        # infinitely-selling "finite" product.
        item = seed_item(kind="custom", name="Endless Cape", price=250,
                         icon="https://x/cape.png")
        before = mutation_snapshot()
        r = self.put(item, dict(max_stock=10))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn("unlimited", str(r.get_json()["error"]).lower())
        # the rejected request changed NOTHING: stock fields, the other
        # stored values, and every mutation-tracked table are untouched
        self.assertEqual(mutation_snapshot(), before,
                         "a rejected unlimited->finite edit changed state")
        self.assertEqual(
            self.one("SELECT name,price,icon_url,max_stock,current_stock "
                     "FROM shop_items WHERE id=?", (item,)),
            ("Endless Cape", 250, "https://x/cape.png", None, None))
        # ... even when smuggled alongside otherwise-valid field changes
        r = self.put(item, dict(name="New Name", max_stock=10))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(self.field(item, "name"), "Endless Cape")

    def test_edit_legacy_title_with_stored_duration_is_grandfathered(self):
        # Rows created before the Title/Prestige permanence rule could legally
        # store a positive duration. They must stay safe-field-editable while
        # the stored duration is preserved value-for-value — never silently
        # normalized to NULL.
        t = seed_item(kind="title", name="Old Timed Title", price=100, duration=48)
        # 1. body OMITS duration -> unrelated edit allowed, duration kept at 48
        r = self.put(t, dict(price=150))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.one("SELECT duration_hours,price FROM shop_items WHERE id=?",
                                  (t,)), (48, 150))
        # 2. the UI resubmits the SAME stored duration (number and form-string)
        #    -> allowed, duration still exactly 48
        r = self.put(t, dict(price=175, duration_hours=48))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.one("SELECT duration_hours,price FROM shop_items WHERE id=?",
                                  (t,)), (48, 175))
        r = self.put(t, dict(price=180, duration_hours="48"))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.one("SELECT duration_hours,price FROM shop_items WHERE id=?",
                                  (t,)), (48, 180))
        # 4. changing to a DIFFERENT positive duration is still rejected,
        #    and the rejected request changes nothing
        before = mutation_snapshot()
        r = self.put(t, dict(duration_hours=72))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn("permanent", str(r.get_json()["error"]).lower())
        self.assertEqual(mutation_snapshot(), before)
        self.assertEqual(self.field(t, "duration_hours"), 48)
        # 3. an EXPLICIT clear with 0 works (the supported permanent form)
        r = self.put(t, dict(duration_hours=0))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(self.field(t, "duration_hours"))

    def test_edit_legacy_prestige_with_stored_duration_is_grandfathered(self):
        p = seed_item(kind="prestige", name="Old Prestige II", price=500,
                      prestige_tier=2, duration=24)
        # unrelated edit allowed; duration and tier preserved exactly
        r = self.put(p, dict(price=600))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.one("SELECT duration_hours,price,prestige_tier FROM shop_items "
                     "WHERE id=?", (p,)), (24, 600, 2))
        # a different positive duration is still rejected
        r = self.put(p, dict(duration_hours=12))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(self.field(p, "duration_hours"), 24)
        # the SAME tier resubmitted as a number or a form-string is not a tier
        # change, so the grandfathered value is still preserved
        for same_tier in (2, "2"):
            r = self.put(p, dict(prestige_tier=same_tier, price=650))
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(
                self.one("SELECT duration_hours,prestige_tier FROM shop_items "
                         "WHERE id=?", (p,)), (24, 2))

    def test_legacy_prestige_tier_change_cannot_carry_the_old_duration(self):
        # THE TIER-CHANGE HOLE: the grandfather clause keys off a stored
        # positive duration, but it must NOT carry that duration across a
        # CHANGE OF TIER. A legacy VI(48) turned into a V used to become a
        # timed Prestige V; a legacy V(24) turned into a VI became a product
        # claiming to be permanent and unmetered while holding a duration.
        v = seed_item(kind="prestige", name="Old VI", price=0,
                      prestige_tier=6, duration=48)
        before = mutation_snapshot()
        # 1. the old duration is still present -> REJECTED, nothing written
        r = self.put(v, dict(prestige_tier=5, price=500, duration_hours=48))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(mutation_snapshot(), before)
        self.assertEqual(
            self.one("SELECT duration_hours,price,prestige_tier FROM shop_items "
                     "WHERE id=?", (v,)), (48, 0, 6))
        # 1b. omitted duration is not an escape hatch either: the clause steps
        #     aside entirely, so the still-stored positive value fails the
        #     permanence rule
        r = self.put(v, dict(prestige_tier=5, price=500))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(self.field(v, "prestige_tier"), 6)
        # 2. an EXPLICIT clear first, then the tier change, is allowed and
        #    stores NULL — never the old 48
        r = self.put(v, dict(duration_hours=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(self.field(v, "duration_hours"))
        r = self.put(v, dict(prestige_tier=5, price=500))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.one("SELECT duration_hours,price,prestige_tier FROM shop_items "
                     "WHERE id=?", (v,)), (None, 500, 5))
        # 3. V(24) -> VI must end up genuinely free, unmetered and permanent
        p = seed_item(kind="prestige", name="Old V", price=500,
                      prestige_tier=5, duration=24)
        r = self.put(p, dict(prestige_tier=6, price=0, duration_hours=24))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(
            self.one("SELECT duration_hours,price,prestige_tier FROM shop_items "
                     "WHERE id=?", (p,)), (24, 500, 5))
        r = self.put(p, dict(prestige_tier=6, price=0))
        self.assertEqual(r.status_code, 400, r.get_json())
        # ... and only an explicit clear lets the VI through, with VI terms
        r = self.put(p, dict(prestige_tier=6, price=0, duration_hours=0))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.one("SELECT duration_hours,price,prestige_tier,max_stock,"
                     "required_level,role_id FROM shop_items WHERE id=?", (p,)),
            (None, 0, 6, None, 0, None))

    def test_legacy_title_edit_is_tier_blind(self):
        # The tier guard is Prestige-only. A Title carries no tier, so its
        # safe-field edits and its grandfathered duration must behave exactly
        # as before — the new comparison must not accidentally fire on Title's
        # NULL tier (None == None must stay true).
        t = seed_item(kind="title", name="Old Title", price=100, duration=48)
        for payload in (dict(price=110), dict(price=120, duration_hours=48),
                        dict(price=130, duration_hours="48"),
                        dict(price=140, prestige_tier=None),
                        dict(price=150, prestige_tier="")):
            r = self.put(t, payload)
            self.assertEqual(r.status_code, 200, (payload, r.get_json()))
            self.assertEqual(self.field(t, "duration_hours"), 48, payload)
        self.assertEqual(self.field(t, "price"), 150)
        # a Title still cannot gain a DIFFERENT positive duration
        r = self.put(t, dict(duration_hours=72))
        self.assertEqual(r.status_code, 400, r.get_json())
        # and an explicit clear still works
        r = self.put(t, dict(duration_hours=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(self.field(t, "duration_hours"))

    def test_ui_duration_clear_payload_null_clears_legacy_duration(self):
        # THE EXACT UI CLEAR PAYLOAD: the edit form computes
        #   duration_hours: parseInt(input.value) || null
        # so typing 0 sends JSON null (key PRESENT, value null) — not numeric
        # 0. An explicit null is an administrator's clear, NOT an "unchanged"
        # resubmission: the grandfather clause must step aside and the stored
        # legacy duration must actually become NULL.
        t = seed_item(kind="title", name="Old Timed Title", price=100, duration=48)
        r = self.put(t, dict(price=200, duration_hours=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        # cleared in the DB, and the unrelated field change persisted
        self.assertEqual(self.one("SELECT duration_hours,price FROM shop_items WHERE id=?",
                                  (t,)), (None, 200))
        # the PUT response reports no stale/temporary product fields: it is a
        # bare success envelope (never the 0 used as validation representation)
        self.assertEqual(r.get_json(), {"success": True})
        p = seed_item(kind="prestige", name="Old Prestige II", price=500,
                      prestige_tier=2, duration=24)
        r = self.put(p, dict(price=700, duration_hours=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.one("SELECT duration_hours,price,prestige_tier FROM shop_items "
                     "WHERE id=?", (p,)), (None, 700, 2))
        # Contrast check: a GRANDFATHERED edit (duration omitted) must report
        # the REAL stored value (48) on the GET prefill — never the temporary
        # validation representation (0).
        t2 = seed_item(kind="title", name="Timed Title Two", price=100, duration=48)
        self.assertEqual(self.put(t2, dict(price=120)).status_code, 200)
        body = self.get(t2).get_json()
        self.assertEqual(body["item"]["duration_hours"], 48)
        self.assertEqual(body["item"]["price"], 120)

    def test_prestige_vi_with_positive_duration_is_rejected(self):
        # LOCKED RULE: Title and ALL Prestige tiers reject positive durations.
        # The VI unmetered branch used to force-null duration_hours BEFORE the
        # permanence check, silently accepting (and discarding) a positive
        # duration instead of rejecting it.
        from utils.shop_validation import shop_input, ShopValidationError
        # 1. the validator itself rejects VI + positive duration
        with self.assertRaises(ShopValidationError):
            shop_input({"name": "VI", "type": "prestige", "price": 0,
                        "prestige_tier": 6, "duration_hours": 48})
        # 2. the real POST API rejects the same request, mutating nothing
        before = mutation_snapshot()
        r = self.post(dict(name="Prestige VI", type="prestige", price=0,
                           prestige_tier=6, duration_hours=48))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn("permanent", str(r.get_json()["error"]).lower())
        self.assertEqual(mutation_snapshot(), before)
        # 3. valid VI requests are unaffected: duration omitted, null and 0
        #    all still create a free, unmetered, permanent VI listing
        for suffix, extra in (("A", {}), ("B", {"duration_hours": None}),
                              ("C", {"duration_hours": 0})):
            resp = self.post(dict(name=f"Prestige VI {suffix}", type="prestige",
                                  price=0, prestige_tier=6, **extra))
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertEqual(
                self.one("SELECT duration_hours,price,prestige_tier,max_stock "
                         "FROM shop_items WHERE name=?", (f"Prestige VI {suffix}",)),
                (None, 0, 6, None))
        # 4. other VI rules unweakened: a paid VI is still refused
        r = self.post(dict(name="Paid VI", type="prestige", price=100,
                           prestige_tier=6))
        self.assertEqual(r.status_code, 400, r.get_json())
        # 5. the legacy-duration grandfather still covers an OLD VI row with a
        #    stored positive duration: unrelated edits stay possible and the
        #    stored value is preserved exactly
        v = seed_item(kind="prestige", name="Old VI", price=0,
                      prestige_tier=6, duration=48)
        r = self.put(v, dict(featured=1))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.one("SELECT duration_hours,featured FROM shop_items WHERE id=?",
                     (v,)), (48, 1))

    def test_grandfather_clause_does_not_leak_to_other_types_or_creation(self):
        # A custom item's duration is NOT grandfathered: it follows normal
        # duration semantics and an edit can change it freely.
        c = seed_item(kind="custom", name="Timed Cape", price=100, duration=48)
        r = self.put(c, dict(duration_hours=72))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.field(c, "duration_hours"), 72)
        # POST creation of a timed Title is still rejected (rule unchanged).
        r = self.post(dict(name="New Timed Title", type="title", price=0,
                           duration_hours=8))
        self.assertEqual(r.status_code, 400, r.get_json())
        # A clean (no-duration) title gains nothing from the clause: trying to
        # SET a positive duration on it is still rejected.
        t = seed_item(kind="title", name="Plain Title", price=100)
        r = self.put(t, dict(duration_hours=5))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIsNone(self.field(t, "duration_hours"))

    def test_edit_ignores_an_explicit_current_stock_key(self):
        # Finalized: stock quantities are NOT managed by Product Management.
        # PUT always preserves current_stock, even when a client sends the key
        # explicitly — it is ignored, not applied and not an error. The UI's
        # current-stock field is read-only to match.
        item = seed_item(kind="custom", name="Cape", price=250, max_stock=20, current_stock=12)
        r = self.put(item, dict(current_stock=18, price=300))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.one("SELECT current_stock,price FROM shop_items WHERE id=?",
                                  (item,)), (12, 300))
        # even junk in the ignored key must not block or corrupt the edit
        r = self.put(item, dict(current_stock="garbage", name="Cape v2"))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.one("SELECT current_stock,name FROM shop_items WHERE id=?",
                                  (item,)), (12, "Cape v2"))
        # the max>=current guard still compares against the PRESERVED stock:
        # sending current_stock=0 alongside max_stock=5 must not bypass it.
        before = mutation_snapshot()
        r = self.put(item, dict(current_stock=0, max_stock=5))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(mutation_snapshot(), before)
        self.assertEqual(self.field(item, "current_stock"), 12)

    def test_edit_icon_absent_keeps_present_blank_clears(self):
        item = seed_item(kind="custom", name="Cape", price=250, icon="https://x/i.png")
        # absent key -> kept
        self.assertEqual(self.put(item, dict(name="Cape v2")).status_code, 200)
        self.assertEqual(self.field(item, "icon_url"), "https://x/i.png")
        # present-but-blank -> explicit clear
        self.assertEqual(self.put(item, dict(icon_url="")).status_code, 200)
        self.assertIsNone(self.field(item, "icon_url"))

    def test_edit_rejects_invalid_price_without_persisting(self):
        item = seed_item(kind="custom", name="Cape", price=250)
        before = mutation_snapshot()
        r = self.put(item, dict(price="garbage"))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(mutation_snapshot(), before)

    def test_edit_color_cannot_drop_its_required_role(self):
        item = seed_item(kind="color", name="Red Name", price=300, role_id=4242)
        before = mutation_snapshot()
        r = self.put(item, dict(role_id=None))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn("role", str(r.get_json()["error"]).lower())
        self.assertEqual(mutation_snapshot(), before)
        self.assertEqual(self.field(item, "role_id"), 4242)

    def test_edit_legacy_xp_boost_safe_fields_keeps_type_and_effect(self):
        item = seed_item(kind="xp_boost", name="Boost", price=100, multiplier=2, duration=4)
        r = self.put(item, dict(name="Boost v2", price=150))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.one("SELECT name,type,price,xp_boost_multiplier,duration_hours "
                     "FROM shop_items WHERE id=?", (item,)),
            ("Boost v2", "xp_boost", 150, 2, 4))

    def test_edit_legacy_role_item_keeps_type(self):
        item = seed_item(kind="role", name="VIP", price=1500, role_id=77)
        r = self.put(item, dict(price=1200))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.one("SELECT type,price,role_id FROM shop_items WHERE id=?",
                                  (item,)), ("role", 1200, 77))

    def test_edit_is_guild_scoped_and_404s(self):
        other = seed_item(guild=OTHER_GUILD, name="Foreign", kind="custom", price=1)
        self.assertEqual(self.put(other, dict(name="Hijack")).status_code, 404)
        self.assertEqual(self.put(123456, dict(name="Ghost")).status_code, 404)
        self.assertEqual(self.field(other, "name"), "Foreign")

    def test_edit_requires_admin_permission(self):
        item = seed_item(kind="custom", name="Cape", price=250)
        execute("UPDATE dashboard_users SET permission_level='moderator' "
                "WHERE guild_id=? AND user_id=?", (GUILD, USER))
        r = self.put(item, dict(name="Hacked"))
        self.assertEqual(r.status_code, 403, r.get_json())
        self.assertEqual(self.field(item, "name"), "Cape")

    def test_edit_requires_csrf(self):
        item = seed_item(kind="custom", name="Cape", price=250)
        r = self.put(item, dict(name="No CSRF"), csrf=False)
        self.assertEqual(r.status_code, 403, r.get_json())
        self.assertEqual(self.field(item, "name"), "Cape")

    # ══ item_catalog COALESCE review ════════════════════════════════════
    def test_edit_syncs_the_catalog_value_and_rarity(self):
        item = seed_item(kind="custom", name="Cape", price=250, rarity="common")
        self.assertEqual(self.put(item, dict(price=300, rarity="epic")).status_code, 200)
        self.assertEqual(
            self.one("SELECT rarity,value_currency,value_amount FROM item_catalog "
                     "WHERE guild_id=? AND item_name='Cape'", (GUILD,)),
            ("epic", "balance", 300))

    def test_blank_icon_clears_the_listing_but_coalesce_keeps_the_catalog_icon(self):
        # Create through the API so the catalog is populated with the icon.
        self.assertEqual(
            self.post(dict(name="Cog", type="custom", price=100,
                           icon_url="https://x/cog.png")).status_code, 200)
        item = rows("SELECT id FROM shop_items WHERE name='Cog'")[0][0]
        self.assertEqual(
            self.one("SELECT icon_url FROM item_catalog WHERE guild_id=? AND item_name='Cog'",
                     (GUILD,))[0], "https://x/cog.png")
        # Explicit clear on the listing...
        self.assertEqual(self.put(item, dict(icon_url="")).status_code, 200)
        self.assertIsNone(self.field(item, "icon_url"))
        # ...but upsert_catalog_entry's COALESCE deliberately PRESERVES the
        # cached rank-card icon rather than wiping it. This is the documented
        # COALESCE impact of the edit path.
        self.assertEqual(
            self.one("SELECT icon_url FROM item_catalog WHERE guild_id=? AND item_name='Cog'",
                     (GUILD,))[0], "https://x/cog.png")

    # ══ admin table render (the authoritative htmx partial) ═════════════
    def test_partial_renders_display_type_names_and_edit_button(self):
        seed_item(kind="custom", name="Cape", price=250)
        seed_item(kind="color", name="Red Name", price=300, role_id=1)
        seed_item(kind="potion", name="Elixir", price=50, multiplier=2, duration=2)
        seed_item(kind="title", name="Hero", price=0)
        seed_item(kind="prestige", name="P3", price=500, prestige_tier=3, role_id=None)
        seed_item(kind="role", name="VIP", price=1500, role_id=2)
        seed_item(kind="xp_boost", name="Boost", price=100, multiplier=2, duration=4, role_id=None)
        html = self.client.get("/api/shop/items").get_data(as_text=True)
        for label in ("Item", "Color", "Potion", "Title", "Prestige III",
                      "Permanent Role", "XP Boost"):
            self.assertIn(label, html, f"missing display label {label}")
        # the raw internal values must not leak as the display type
        self.assertNotIn(">custom<", html)
        self.assertNotIn(">temp_role<", html)
        self.assertIn("editItem(", html)
        self.assertIn(">Edit<", html)

    # ══ legacy tier change, with the EXACT payloads the real form now sends ══
    # scripts/test_vi_legacy_edit_form.js executes the real template script and
    # asserts these payload shapes; this class proves what the API and the DB do
    # with them. (V(24) -> VI used to send duration_hours: null on its own.)
    @staticmethod
    def ui_payload(**over):
        base = dict(name="Old V", price=500, price_diamonds=None, type="prestige",
                    role_id=None, prestige_tier=5, xp_boost_multiplier=None,
                    duration_hours=24, required_level=0, description="",
                    icon_url=None, rarity="common", featured=0, max_stock=None)
        base.update(over)
        return base

    def vi_row(self, p):
        return self.one(
            "SELECT duration_hours,price,prestige_tier,max_stock,required_level,"
            "role_id,price_diamonds,current_stock FROM shop_items WHERE id=?", (p,))

    def test_ui_legacy_v24_to_vi_without_clear_is_rejected_and_unchanged(self):
        p = seed_item(kind="prestige", name="Old V", price=500, role_id=None,
                      prestige_tier=5, duration=24)
        before = mutation_snapshot()
        before_row = self.vi_row(p)
        # What the repaired form sends for V(24) -> VI with NO explicit clear:
        # the stored 24 travels with the VI-locked values.
        r = self.put(p, self.ui_payload(prestige_tier=6, price=0, duration_hours=24))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIs(r.get_json()["success"], False)
        self.assertIn("duration", str(r.get_json()["error"]).lower())
        self.assertEqual(mutation_snapshot(), before, "a rejected tier change wrote")
        self.assertEqual(self.vi_row(p), before_row)
        self.assertEqual(before_row[:3], (24, 500, 5))

    def test_ui_legacy_v24_to_vi_after_explicit_clear_stores_null_free_vi(self):
        p = seed_item(kind="prestige", name="Old V", price=500, role_id=None,
                      prestige_tier=5, duration=24, max_stock=None,
                      current_stock=None, required_level=0)
        # What the form sends after "Clear legacy duration": null + VI terms.
        r = self.put(p, self.ui_payload(prestige_tier=6, price=0, duration_hours=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        # (duration, price, tier, max_stock, level, role, diamonds, current_stock)
        self.assertEqual(self.vi_row(p), (None, 0, 6, None, 0, None, None, None))

    def test_ui_normal_v_without_legacy_duration_to_vi_still_works(self):
        p = seed_item(kind="prestige", name="Normal V", price=500, role_id=None,
                      prestige_tier=5, duration=None)
        r = self.put(p, self.ui_payload(name="Normal V", prestige_tier=6, price=0,
                                         duration_hours=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.vi_row(p), (None, 0, 6, None, 0, None, None, None))
        # also via a stored 0 (older rows): same outcome
        q = seed_item(kind="prestige", name="Zero V", price=500, role_id=None,
                      prestige_tier=5, duration=0)
        r = self.put(q, self.ui_payload(name="Zero V", prestige_tier=6, price=0,
                                         duration_hours=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(self.field(q, "duration_hours"))

    def test_ui_legacy_v24_unrelated_edit_keeps_24_and_tier(self):
        p = seed_item(kind="prestige", name="Old V", price=500, role_id=None,
                      prestige_tier=5, duration=24)
        r = self.put(p, self.ui_payload(price=650, duration_hours=24))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.vi_row(p)[:3], (24, 650, 5))

    def test_ui_legacy_v24_to_other_tier_without_clear_is_rejected(self):
        p = seed_item(kind="prestige", name="Old V", price=500, role_id=None,
                      prestige_tier=5, duration=24)
        before = mutation_snapshot()
        r = self.put(p, self.ui_payload(prestige_tier=4, duration_hours=24))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(mutation_snapshot(), before)

    def test_ui_legacy_vi48_paths_still_hold(self):
        v = seed_item(kind="prestige", name="Old VI", price=0, role_id=None,
                      prestige_tier=6, duration=48)
        vi = dict(name="Old VI", prestige_tier=6, price=0, duration_hours=48)
        # unrelated edit resends 48 -> preserved exactly
        r = self.put(v, self.ui_payload(**vi, description="edited"))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.field(v, "duration_hours"), 48)
        # VI -> V with the 48 still present -> rejected, row untouched
        before = mutation_snapshot()
        r = self.put(v, self.ui_payload(name="Old VI", prestige_tier=5, price=500,
                                         duration_hours=48))
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(mutation_snapshot(), before)
        # explicit Clear (stays VI) -> NULL
        r = self.put(v, self.ui_payload(**{**vi, "duration_hours": None}))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(self.field(v, "duration_hours"))
        # explicit clear followed by VI -> V works and stays permanent
        r = self.put(v, self.ui_payload(name="Old VI", prestige_tier=5, price=500,
                                         duration_hours=None))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.vi_row(v)[:3], (None, 500, 5))

    # ══ delete guard is not weakened ════════════════════════════════════
    def test_delete_root_with_options_is_still_blocked(self):
        root = seed_item(kind="custom", name="Bundle", price=100)
        seed_item(kind="custom", name="Option A", price=50, option_of_id=root)
        r = self.delete(root)
        self.assertEqual(r.status_code, 409)
        self.assertIsNotNone(self.one("SELECT id FROM shop_items WHERE id=?", (root,)))

    def test_delete_leaf_succeeds(self):
        item = seed_item(kind="custom", name="Cape", price=250)
        r = self.delete(item)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.one("SELECT id FROM shop_items WHERE id=?", (item,)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
