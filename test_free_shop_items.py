"""Free Shop items (price = 0): API, purchase-engine, receipt and gate contracts.

A normal item is Free exactly when its coin `price` is 0 AND price_diamonds is
not positive. It must ride the SAME engine, the SAME handle
(`shop_buy_<id>`) and the SAME reward path as a paid item; the only economic
difference is that no balance is read or written.

Run: python -m pytest test_free_shop_items.py
     python test_free_shop_items.py
"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from phase1_support import (
    GUILD, USER, execute, rows, reset_database, seed_member, member,
    mutation_snapshot, all_embed_text,
)
import dashboard.app as dashboard
from cogs.shop import process_purchase
from utils import economy_safe as _economy_safe
from utils.shop_publication import _PRODUCT_COLUMNS, _PRODUCT_KEYS
from utils import shop_publisher as SP

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

CURRENCY = {
    "coins": {"key": "balance", "name": "Coins", "emoji": "🪙"},
    "diamonds": {"key": "diamonds", "name": "Diamonds", "emoji": "💎"},
}


# ── fixtures ────────────────────────────────────────────────────────────────

def shop_item(kind="custom", price=0, name="Free Item", role_id=None,
              duration_hours=None, multiplier=None, stock=None, level=0,
              required_role=None, diamonds=None, enabled=1, guild=GUILD):
    """Insert a shop listing directly: the engine, not the API, is under test."""
    return execute(
        "INSERT INTO shop_items (guild_id,name,type,price,role_id,duration_hours,"
        "max_stock,current_stock,enabled,required_level,required_role_id,"
        "price_diamonds,xp_boost_multiplier) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (guild, name, kind, price, role_id, duration_hours,
         stock, stock, enabled, level, required_role, diamonds, multiplier))


def role_stub(role_id, name="Cape", position=1):
    return SimpleNamespace(id=role_id, name=name, position=position,
                           mention=f"<@&{role_id}>")


def purchase_context(roles_held=(), granted_role=None, guild_roles=()):
    """A Discord-shaped interaction whose guild/bot resolve like the real ones.

    `guild_roles` are roles the GUILD knows about (so a required-role gate can
    see them); `roles_held` are the member's. They are deliberately separable:
    a requirement must block when the guild has the role but the member does not.
    """
    person = member()
    person.roles = list(roles_held)
    person.add_roles = AsyncMock()
    person.remove_roles = AsyncMock()
    known = list(guild_roles) + ([granted_role] if granted_role is not None else [])

    def get_role(rid):
        rid = int(rid)
        return next((r for r in known if r.id == rid), None)

    guild = SimpleNamespace(
        id=GUILD, owner_id=99,
        me=SimpleNamespace(top_role=SimpleNamespace(position=10)),
        get_role=get_role,
        get_member=lambda uid: person if uid == person.id else None,
    )
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD else None)
    itx = SimpleNamespace(
        user=person, guild=guild, client=bot,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                 is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    return itx, person


def last_embed(itx):
    return itx.response.send_message.call_args.kwargs.get("embed")


def reply_text(itx):
    """Whatever the handler answered with — embed text or plain content."""
    call = itx.response.send_message.call_args
    embed = call.kwargs.get("embed")
    content = call.kwargs.get("content")
    if content is None and call.args:
        content = call.args[0] if isinstance(call.args[0], str) else None
    return " ".join(part for part in (
        all_embed_text(embed) if embed is not None else "",
        content or "",
    ) if part)


def spy_deduct():
    """Observe safe_deduct while keeping its real, zero-rejecting behaviour.

    A bare AsyncMock would happily accept a 0 amount and hide the very bug this
    suite exists to prevent; wrapping the real function means a stray call with
    zero raises ValueError inside the handler instead of passing silently.
    """
    return patch("cogs.shop.safe_deduct", new=AsyncMock(wraps=_economy_safe.safe_deduct))


def history():
    return rows("SELECT item_name, price_paid, currency_paid FROM purchase_history")


# ── purchase engine: free path ──────────────────────────────────────────────

class FreePurchaseEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=0)

    async def test_free_role_purchase_grants_role_without_touching_a_zero_balance(self):
        item = shop_item("role", price=0, role_id=55, name="Free Cape")
        itx, person = purchase_context(granted_role=role_stub(55, "Cape"))
        with spy_deduct() as deduct:
            await process_purchase(itx, item)
        deduct.assert_not_awaited()                    # no deduction call at all
        self.assertEqual(rows("SELECT balance, diamonds FROM economy"), [(0, 138)])
        embed = last_embed(itx)
        self.assertIsNotNone(embed, "free purchase must answer with the normal success embed")
        self.assertIn("Free", all_embed_text(embed))
        self.assertNotIn("0 🪙", all_embed_text(embed))
        person.add_roles.assert_awaited()              # reward delivered
        self.assertEqual(rows("SELECT item_name, item_type FROM inventory_items"),
                         [("Free Cape", "role")])
        self.assertEqual(history(), [("Free Cape", 0, "balance")])
        self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger"), [(0,)])
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(None,)])

    async def test_every_supported_item_type_reaches_its_normal_reward_path_for_free(self):
        cases = [
            ("role", dict(role_id=55), "inventory", "role"),
            ("temp_role", dict(role_id=55, duration_hours=2), "temp", "temp_role"),
            ("title", {}, "inventory", "title"),
            ("custom", {}, "inventory", "shop_custom"),
            ("potion", dict(multiplier=2, duration_hours=2), "inventory", "potion"),
            ("xp_boost", dict(multiplier=2, duration_hours=2), "boost", "xp_boost"),
        ]
        for kind, extra, artefact, expected_type in cases:
            with self.subTest(kind=kind):
                await reset_database()
                seed_member(balance=0)
                item = shop_item(kind, price=0, name=f"Free {kind}", **extra)
                itx, _ = purchase_context(granted_role=role_stub(55, "Cape"))
                with spy_deduct() as deduct:
                    await process_purchase(itx, item)
                deduct.assert_not_awaited()
                self.assertEqual(rows("SELECT balance FROM economy"), [(0,)],
                                 f"{kind}: balance must be untouched")
                self.assertEqual(history(), [(f"Free {kind}", 0, "balance")],
                                 f"{kind}: normal zero-value receipt expected")
                self.assertEqual(rows("SELECT COUNT(*) FROM transaction_ledger"), [(0,)],
                                 f"{kind}: no financial ledger row for a free grant")
                if artefact == "inventory":
                    self.assertEqual(
                        rows("SELECT item_name, item_type FROM inventory_items"),
                        [(f"Free {kind}", expected_type)], f"{kind}: reward not delivered")
                elif artefact == "temp":
                    self.assertEqual(
                        rows("SELECT item_type FROM inventory_items"), [(expected_type,)])
                    self.assertEqual(rows("SELECT COUNT(*) FROM temp_roles"), [(1,)],
                                     "temp_role: the normal expiry row must still be written")
                else:
                    self.assertEqual(
                        rows("SELECT COUNT(*) FROM leveling_active_boosts"), [(1,)],
                        "xp_boost: the normal boost row must still be written")
                self.assertIsNotNone(last_embed(itx), f"{kind}: no success embed")

    async def test_finite_stock_is_consumed_and_exhausted_stock_still_blocks(self):
        item = shop_item("title", price=0, name="Limited Free", stock=1)
        itx, _ = purchase_context()
        await process_purchase(itx, item)
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(0,)])
        self.assertEqual(len(history()), 1)

        itx2, _ = purchase_context()
        before = mutation_snapshot()
        await process_purchase(itx2, item)
        self.assertIn("out of stock", reply_text(itx2).lower())
        self.assertEqual(mutation_snapshot(), before,
                         "an out-of-stock free purchase must not mutate anything")
        self.assertEqual(len(history()), 1, "no second receipt for a refused purchase")

    async def test_zero_coin_price_with_a_diamond_price_is_paid_not_free(self):
        # Diamond precedence: 0 coins + 5 💎 is a PAID item, charged in
        # Diamonds. With no Diamonds the charge must fail exactly like any
        # other paid purchase — it must NOT be waved through as free.
        item = shop_item("title", price=0, diamonds=5, name="Diamond Title")
        execute("UPDATE economy SET diamonds=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        itx, _ = purchase_context()
        with spy_deduct() as deduct:
            await process_purchase(itx, item)
        deduct.assert_awaited_once()                  # it IS charged
        self.assertEqual(len(history()), 0, "a refused paid purchase writes no receipt")
        self.assertEqual(rows("SELECT COUNT(*) FROM inventory_items"), [(0,)],
                         "nothing may be granted when the charge failed")
        self.assertEqual(rows("SELECT diamonds FROM economy"), [(0,)],
                         "balance untouched by the failed charge")

    async def test_diamond_charged_item_deducts_diamonds_and_records_that_currency(self):
        item = shop_item("title", price=0, diamonds=5, name="Diamond Title")
        itx, _ = purchase_context()
        await process_purchase(itx, item)
        self.assertEqual(rows("SELECT diamonds FROM economy"), [(133,)])
        self.assertEqual(history(), [("Diamond Title", 5, "diamonds")])

    async def test_failed_paid_deduction_still_releases_the_stock_claim(self):
        # The existing rollback path for a paid item must be untouched by the
        # free branch: finite stock, no balance, diamond charge -> refuse and give
        # the unit back.
        item = shop_item("title", price=0, diamonds=5, stock=1, name="Diamond Limited")
        execute("UPDATE economy SET diamonds=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        itx, _ = purchase_context()
        await process_purchase(itx, item)
        self.assertEqual(rows("SELECT current_stock FROM shop_items"), [(1,)],
                         "a refused paid purchase must release its stock claim")
        self.assertEqual(len(history()), 0)

    async def test_negative_legacy_price_is_still_refused(self):
        item = shop_item("title", price=-5, name="Negative")
        itx, _ = purchase_context()
        before = mutation_snapshot()
        await process_purchase(itx, item)
        self.assertEqual(mutation_snapshot(), before,
                         "a negative price must never be treated as free-or-anything")
        self.assertEqual(len(history()), 0)

    async def test_free_purchase_is_repeatable_when_stock_is_unlimited(self):
        # Documented existing semantics: no invented one-time claim restriction.
        item = shop_item("title", price=0, name="Repeatable Free")
        for _ in range(2):
            itx, _ = purchase_context()
            await process_purchase(itx, item)
        self.assertEqual(len(history()), 2)
        self.assertEqual(rows("SELECT quantity FROM inventory_items"), [(2,)])

    async def test_unlimited_free_temp_role_stacks_exactly_like_a_paid_one(self):
        item = shop_item("temp_role", price=0, role_id=55, duration_hours=2,
                         name="Free Temp")
        for _ in range(2):
            itx, _ = purchase_context(granted_role=role_stub(55, "Cape"))
            await process_purchase(itx, item)
        self.assertEqual(rows("SELECT COUNT(*) FROM temp_roles"), [(2,)])
        self.assertEqual(len(history()), 2)


# ── purchase engine: eligibility gates unchanged ────────────────────────────

class FreePurchaseGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=0)
        execute("UPDATE levels SET level=0 WHERE guild_id=? AND user_id=?", (GUILD, USER))
        self.guild_roles = []

    async def assert_refused_without_mutation(self, item, fragment):
        before = mutation_snapshot()
        itx, _ = purchase_context(guild_roles=self.guild_roles)
        await process_purchase(itx, item)
        self.assertEqual(mutation_snapshot(), before, f"{fragment}: refusal mutated state")
        self.assertIn(fragment, reply_text(itx).lower())
        self.assertTrue(itx.response.send_message.call_args.kwargs.get("ephemeral"),
                        "refusals stay private")

    async def test_level_gate_still_blocks_a_free_item(self):
        await self.assert_refused_without_mutation(
            shop_item("title", price=0, level=50, name="Level Gated"), "level 50")

    async def test_role_gate_still_blocks_a_free_item(self):
        # The guild knows the role, the member does not hold it.
        self.guild_roles = [role_stub(77, "Required", position=2)]
        await self.assert_refused_without_mutation(
            shop_item("title", price=0, required_role=77, name="Role Gated"), "need")

    async def test_disabled_free_item_is_still_unavailable(self):
        await self.assert_refused_without_mutation(
            shop_item("title", price=0, enabled=0, name="Hidden"), "not found")

    async def test_misconfigured_free_potion_and_xp_boost_are_still_refused(self):
        await self.assert_refused_without_mutation(
            shop_item("potion", price=0, multiplier=None, duration_hours=2, name="Bad Potion"),
            "isn't configured correctly")
        await self.assert_refused_without_mutation(
            shop_item("xp_boost", price=0, multiplier=2, duration_hours=None, name="Bad Boost"),
            "isn't configured correctly")


# ── API: validation contract ────────────────────────────────────────────────

class FreeListingApiTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'admin',1)", (GUILD, USER))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "Free Test", "avatar": None},
                           guild_id=GUILD, expires_at=time.time() + 7200,
                           csrf_token="free-csrf")

    def post(self, payload):
        return self.client.post("/api/shop/item", json=payload,
                                headers={"X-CSRF-Token": "free-csrf"})

    def test_normal_item_with_price_zero_is_accepted_and_stored_as_zero(self):
        for kind, extra in (("title", {}), ("custom", {}), ("role", {"role_id": 55}),
                            ("temp_role", {"role_id": 55, "duration_hours": 2}),
                            ("potion", {"xp_boost_multiplier": 2, "duration_hours": 2}),
                            ("xp_boost", {"xp_boost_multiplier": 2, "duration_hours": 2})):
            with self.subTest(kind=kind):
                payload = dict(name=f"Free {kind}", type=kind, price=0, **extra)
                response = self.post(payload)
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertIs(response.get_json()["success"], True)
                self.assertEqual(
                    rows("SELECT price, price_diamonds FROM shop_items WHERE name=?",
                         (f"Free {kind}",)), [(0, None)])

    def test_invalid_prices_are_still_rejected_without_persisting(self):
        invalid = [("negative", -1), ("null", None), ("fractional", 2.5),
                   ("boolean", True), ("malformed", "garbage"), ("string_id", "free")]
        for label, value in invalid:
            with self.subTest(label=label):
                before = mutation_snapshot()
                response = self.post(dict(name=f"Bad {label}", type="title", price=value))
                self.assertEqual(response.status_code, 400, (label, response.get_json()))
                body = response.get_json()
                self.assertIs(body["success"], False)
                self.assertIn("price", str(body.get("error", "")).lower(),
                              f"{label}: must be refused BECAUSE of the price")
                self.assertEqual(mutation_snapshot(), before, f"{label} was persisted")

    def test_missing_price_is_rejected(self):
        before = mutation_snapshot()
        response = self.post(dict(name="No price", type="title"))
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("price", str(response.get_json().get("error", "")).lower())
        self.assertEqual(mutation_snapshot(), before)

    def test_zero_coin_price_with_a_diamond_price_stays_a_paid_diamond_item(self):
        response = self.post(dict(name="Diamond only", type="title", price=0,
                                  price_diamonds=5))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(rows("SELECT price, price_diamonds FROM shop_items"), [(0, 5)])

    def test_diamond_price_zero_keeps_its_normalization_and_reads_as_free(self):
        # Existing rule: a zero Diamond price is "no Diamond price at all"
        # (normalized to NULL). It must not turn a Free coin listing into a
        # paid one, and it must not become a charge of zero Diamonds.
        response = self.post(dict(name="Free with loose zero", type="custom",
                                  price=0, price_diamonds=0))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(rows("SELECT price, price_diamonds FROM shop_items"), [(0, None)])

    def test_unknown_item_types_are_still_governed_by_existing_validation(self):
        before = mutation_snapshot()
        response = self.post(dict(name="Nope", type="free_money", price=0))
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(mutation_snapshot(), before)

    def test_prestige_rules_are_untouched_by_the_general_free_rule(self):
        # I–V still require a positive Coin price; VI is its own explicit zero.
        for tier in range(1, 6):
            before = mutation_snapshot()
            response = self.post(dict(name=f"P{tier}", type="prestige",
                                      prestige_tier=tier, price=0))
            self.assertEqual(response.status_code, 400, (tier, response.get_json()))
            self.assertEqual(mutation_snapshot(), before, f"tier {tier} was persisted")
        self.assertEqual(self.post(dict(name="PVI", type="prestige",
                                        prestige_tier=6, price=0)).status_code, 200)
        self.assertEqual(rows("SELECT price, prestige_tier FROM shop_items"), [(0, 6)])

    def test_free_listing_renders_free_in_the_admin_page_and_catalog(self):
        self.assertEqual(self.post(dict(name="Free Booster", type="custom", price=0)).status_code, 200)
        self.assertEqual(self.post(dict(name="Paid Cape", type="custom", price=250)).status_code, 200)
        for url in ("/shop", "/api/shop/items"):
            with self.subTest(url=url):
                html = self.client.get(url).get_data(as_text=True)
                start = html.index("Free Booster")
                row = html[start:html.index("</tr>", start)]
                self.assertIn("Free", row, "a zero-charge normal item must render as Free")
                self.assertNotIn("While boosting", row,
                                 "a normal free item must not borrow Prestige VI's duration text")
                paid = html[html.index("Paid Cape"):]
                self.assertIn("250", paid[:paid.index("</tr>")],
                              "paid rendering is unchanged")

    def test_admin_purchase_history_shows_a_free_receipt_as_free(self):
        execute("INSERT INTO purchase_history (guild_id,user_id,user_display_name,item_id,"
                "item_name,price_paid,currency_paid) VALUES (?,?,'Tester',1,'Free Cape',0,'balance')",
                (GUILD, USER))
        html = self.client.get("/api/shop/purchase-history").get_data(as_text=True)
        self.assertIn("<td>Free</td>", html)


# ── published design: same handle, same engine ──────────────────────────────

class PublishedFreeProductTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member(balance=0)

    async def load_rows(self):
        """The same row shape the publish path builds (shop_publication)."""
        import aiosqlite
        from database import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                f"SELECT {_PRODUCT_COLUMNS} FROM shop_items WHERE guild_id=?", (GUILD,))
            return {int(r[0]): dict(zip(_PRODUCT_KEYS, r)) for r in await cur.fetchall()}

    async def test_a_published_design_with_a_free_product_purchases_through_the_same_handle(self):
        item = shop_item("custom", price=0, name="Free Sticker")
        design = {
            "products": [item],
            "presentation": {
                "mode": "per_product",
                "content": "Buy {{product.name}} for {{product.price_display}}!",
                "embeds": [],
            },
            "action": {"kind": "buttons", "entries": [{"product_id": item}]},
        }
        rows_by_id = await self.load_rows()
        resolved = SP.preview_design(design, rows_by_id, CURRENCY)
        self.assertEqual(SP.validate_design(design, rows_by_id), [],
                         "a free product must be a valid roster member")
        entry = resolved["action"]["entries"][0]
        self.assertEqual(entry["custom_id"], f"shop_buy_{item}",
                         "the purchase handle is the existing shop_buy_<id>")
        self.assertIs(entry["free"], True)
        self.assertEqual(entry["label"], SP.FREE_LABEL)
        self.assertIn("𝐅𝐫𝐞𝐞", resolved["content"])

        # The published button value IS the engine's item id.
        purchased = int(entry["custom_id"].replace("shop_buy_", ""))
        itx, _ = purchase_context()
        await process_purchase(itx, purchased)
        self.assertEqual(history(), [("Free Sticker", 0, "balance")])
        self.assertEqual(rows("SELECT item_name FROM inventory_items"), [("Free Sticker",)])
        self.assertEqual(rows("SELECT balance FROM economy"), [(0,)])

    async def test_free_option_rows_resolve_and_purchase_through_the_same_engine(self):
        root = shop_item("custom", price=0, name="Free Bundle")
        free_option = shop_item("custom", price=0, name="Free Add-on")
        paid_option = shop_item("custom", price=150, name="Paid Add-on")
        execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (root, free_option))
        execute("UPDATE shop_items SET option_of_id=? WHERE id=?", (root, paid_option))
        design = {
            "products": [root],
            "presentation": {"mode": "per_product", "content": "{{product.name}}", "embeds": []},
            # A select needs 2–25 entries (existing rule); both options belong to
            # the same rostered root.
            "action": {"kind": "option_select",
                       "entries": [{"product_id": paid_option},
                                   {"product_id": free_option}]},
        }
        rows_by_id = await self.load_rows()
        self.assertEqual(SP.validate_design(design, rows_by_id), [],
                         "the existing design validation rules are unchanged")
        resolved = SP.preview_design(design, rows_by_id, CURRENCY)
        entries = resolved["action"]["entries"]
        by_id = {e["product_id"]: e for e in entries}
        self.assertEqual(resolved["action"]["component_custom_id"], "shop_buy_sel_0")
        self.assertEqual(by_id[free_option]["custom_id"], f"shop_buy_{free_option}")
        self.assertIs(by_id[free_option]["free"], True)
        self.assertIs(by_id[paid_option]["free"], False)
        self.assertNotEqual(by_id[paid_option]["description"], SP.FREE_LABEL)

        # Selecting the free option runs the engine on the free row only.
        itx, _ = purchase_context()
        await process_purchase(itx, int(by_id[free_option]["custom_id"].replace("shop_buy_", "")))
        self.assertEqual(history(), [("Free Add-on", 0, "balance")])
        self.assertEqual(rows("SELECT item_name FROM inventory_items"), [("Free Add-on",)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
