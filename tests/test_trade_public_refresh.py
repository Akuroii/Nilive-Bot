import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OWNER_ID", "704453350384730237")
os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.gettempdir(), "nilive-trade-test-scratch.db"))

import pytest

from cogs import trade


CURRENCY = {
    "coins": {"key": "balance", "name": "Coins", "emoji": "🪙"},
    "diamonds": {"key": "diamonds", "name": "Diamonds", "emoji": "💎"},
}


class InteractionResponse:
    def __init__(self):
        self.done = False
        self.deferred_ephemeral = False
        self.messages = []
        self.modals = []

    def is_done(self):
        return self.done

    async def defer(self, *, ephemeral=False, thinking=False):
        self.done = True
        self.deferred_ephemeral = ephemeral

    async def send_message(self, content, *, ephemeral=False):
        self.done = True
        self.messages.append((content, ephemeral))

    async def send_modal(self, modal):
        self.done = True
        self.modals.append(modal)


class FakeMessage:
    id = 550

    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


class FakeInteraction:
    def __init__(self, user_id, guild_id=44, message=None):
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.message = message
        self.response = InteractionResponse()
        self.followup = SimpleNamespace(send=AsyncMock())


def make_trade():
    a = SimpleNamespace(id=11, display_name="A", mention="<@11>")
    b = SimpleNamespace(id=22, display_name="B", mention="<@22>")
    session = trade.TradeSession(44, a, b)
    view = trade.TradeView(session, CURRENCY)
    message = FakeMessage()
    session.message = message
    trade._active_trades[session.key()] = session
    return session, view, message


@pytest.fixture(autouse=True)
def clean_active_trades():
    trade._active_trades.clear()
    yield
    trade._active_trades.clear()


@pytest.mark.asyncio
async def test_ephemeral_picker_modal_edits_stored_public_trade(monkeypatch):
    session, view, public_message = make_trade()
    session.ready.update({11, 22})
    modal = trade.ItemQuantityModal(view, "Moonstone", 5)
    modal.quantity._value = "3"
    monkeypatch.setattr(trade, "has_item", AsyncMock(return_value=True))
    # The modal's source is intentionally not the shared/public Trade message.
    interaction = FakeInteraction(11, message=SimpleNamespace(id=999))

    await modal.on_submit(interaction)

    assert interaction.response.deferred_ephemeral
    assert interaction.response.messages == []
    assert len(public_message.edits) == 1
    embed = public_message.edits[0]["embed"]
    assert "Moonstone ×3" in embed.fields[0].value
    assert session.offers[11]["items"] == {"Moonstone": 3}
    assert session.ready == set()
    assert public_message.edits[0]["view"] is view


@pytest.mark.asyncio
async def test_coin_diamond_manual_fallback_and_clear_are_public_and_reset_ready(monkeypatch):
    session, view, public_message = make_trade()
    session.offers[11]["coins"] = 20
    session.offers[11]["diamonds"] = 2
    session.offers[11]["items"]["Old item"] = 1
    session.ready.update({11, 22})

    await view.refresh_public_trade(
        FakeInteraction(22), lambda: session.offers[22].update(coins=80))
    await view.refresh_public_trade(
        FakeInteraction(22), lambda: session.offers[22].update(diamonds=9))

    manual = trade.AddItemModal(view)
    manual.item_name._value = "Manual item"
    manual.quantity._value = "4"
    monkeypatch.setattr(trade, "has_item", AsyncMock(return_value=True))
    await manual.on_submit(FakeInteraction(22))

    assert len(public_message.edits) == 3
    final_embed = public_message.edits[-1]["embed"]
    assert "80 Coins" in final_embed.fields[1].value
    assert "9 Diamonds" in final_embed.fields[1].value
    assert "Manual item ×4" in final_embed.fields[1].value
    assert session.ready == set()

    await view.refresh_public_trade(
        FakeInteraction(11), lambda: session.offers.__setitem__(11, trade._empty_offer()))
    assert session.offers[11] == trade._empty_offer()
    assert len(public_message.edits) == 4


@pytest.mark.asyncio
async def test_more_than_25_items_opens_manual_fallback_then_refreshes_public_trade(monkeypatch):
    session, view, public_message = make_trade()
    inventory = [{"item_name": f"Item {i}", "quantity": 1} for i in range(26)]
    monkeypatch.setattr(trade, "get_inventory", AsyncMock(return_value=inventory))
    monkeypatch.setattr(trade, "has_item", AsyncMock(return_value=True))
    button = next(item for item in view.children if getattr(item, "label", None) == "Offer Item")
    picker_interaction = FakeInteraction(11, message=public_message)

    await button.callback(picker_interaction)
    assert len(picker_interaction.response.modals) == 1
    modal = picker_interaction.response.modals[0]
    assert isinstance(modal, trade.AddItemModal)
    modal.item_name._value = "Item 25"
    modal.quantity._value = "1"
    await modal.on_submit(FakeInteraction(11))

    assert session.offers[11]["items"] == {"Item 25": 1}
    assert len(public_message.edits) == 1
    assert "Item 25 ×1" in public_message.edits[0]["embed"].fields[0].value


@pytest.mark.asyncio
async def test_ready_change_refreshes_public_message_and_concurrent_updates_do_not_overwrite():
    session, view, public_message = make_trade()
    first = FakeInteraction(11, message=public_message)
    second = FakeInteraction(22, message=public_message)

    async def set_coin(interaction, amount):
        def mutate():
            session.offers[interaction.user.id].update(coins=amount)
            session.reset_ready()
        await view.refresh_public_trade(interaction, mutate)

    await asyncio.gather(set_coin(first, 12), set_coin(second, 31))
    assert session.offers[11]["coins"] == 12
    assert session.offers[22]["coins"] == 31
    assert len(public_message.edits) == 2

    ready_button = next(item for item in view.children if getattr(item, "label", None) == "Ready")
    await ready_button.callback(FakeInteraction(11, message=public_message))
    assert session.ready == {11}
    assert len(public_message.edits) == 3
    assert "offer" in public_message.edits[-1]["embed"].fields[0].name


@pytest.mark.asyncio
async def test_duplicate_ready_clicks_do_not_settle_twice(monkeypatch):
    session, view, public_message = make_trade()
    execute = AsyncMock(return_value={"success": True, "trade_id": 41})
    monkeypatch.setattr(trade, "execute_trade", execute)
    button = next(item for item in view.children if getattr(item, "label", None) == "Ready")

    await asyncio.gather(
        button.callback(FakeInteraction(11, message=public_message)),
        button.callback(FakeInteraction(22, message=public_message)),
    )

    execute.assert_awaited_once()
    assert session.finished
    assert trade._active_trades.get(session.key()) is None
    assert "Trade complete" in public_message.edits[-1]["embed"].title


@pytest.mark.asyncio
async def test_expired_trade_rejects_modal_without_mutating_offer(monkeypatch):
    session, view, public_message = make_trade()
    await view.on_timeout()
    modal = trade.ItemQuantityModal(view, "Moonstone", 5)
    modal.quantity._value = "2"
    has_item = AsyncMock(return_value=True)
    monkeypatch.setattr(trade, "has_item", has_item)
    interaction = FakeInteraction(11)

    await modal.on_submit(interaction)

    assert interaction.response.messages == [("This trade has already ended.", True)]
    has_item.assert_not_awaited()
    assert session.offers[11]["items"] == {}
    assert len(public_message.edits) == 1
    assert "Trade timed out" in public_message.edits[0]["content"]
