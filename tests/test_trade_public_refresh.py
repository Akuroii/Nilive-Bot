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

import discord
import pytest

from cogs import trade


CURRENCY = {
    "coins": {"key": "balance", "name": "Coins", "emoji": "🪙"},
    "diamonds": {"key": "diamonds", "name": "Diamonds", "emoji": "💎"},
}


class InteractionResponse:
    """Models discord.py's one-shot response and a thinking defer separately."""
    def __init__(self, interaction_type):
        self.interaction_type = interaction_type
        self.acknowledged = False
        self.deferred_thinking = False
        self.thinking_pending = False
        self.response_type = None
        self.direct_messages = []
        self.modals = []

    def is_done(self):
        # This means ACKed in discord.py; it does NOT imply thinking completed.
        return self.acknowledged

    async def defer(self, *, ephemeral=False, thinking=False):
        assert not self.acknowledged
        assert self.interaction_type in (
            discord.InteractionType.component,
            discord.InteractionType.modal_submit,
        )
        self.acknowledged = True
        self.deferred_thinking = bool(ephemeral and thinking)
        self.thinking_pending = bool(thinking)
        self.response_type = (
            discord.InteractionResponseType.deferred_channel_message
            if thinking else discord.InteractionResponseType.deferred_message_update
        )

    async def send_message(self, content, *, ephemeral=False):
        assert not self.acknowledged
        self.acknowledged = True
        self.response_type = discord.InteractionResponseType.channel_message
        self.direct_messages.append({"content": content, "ephemeral": ephemeral})

    async def send_modal(self, modal):
        assert not self.acknowledged
        self.acknowledged = True
        self.response_type = discord.InteractionResponseType.modal
        self.modals.append(modal)


class InteractionFollowup:
    def __init__(self, response):
        self.response = response
        self.messages = []
        self.send = AsyncMock(side_effect=self._send)

    async def _send(self, content, *, ephemeral=False):
        assert self.response.acknowledged
        assert self.response.thinking_pending, "follow-up should resolve the pending thinking response"
        self.messages.append({"content": content, "ephemeral": ephemeral})
        self.response.thinking_pending = False


class FakeMessage:
    id = 550

    def __init__(self, fail_on_edit_number=None, edit_error=None):
        self.edits = []
        self.attempts = 0
        self.fail_on_edit_number = fail_on_edit_number
        self.edit_error = edit_error

    async def edit(self, **kwargs):
        self.attempts += 1
        if self.edit_error is not None and self.attempts == self.fail_on_edit_number:
            raise self.edit_error
        self.edits.append(kwargs)
        return self


class FakeHTTPException(discord.HTTPException):
    def __init__(self):
        Exception.__init__(self, "mock Discord message edit failure")


class FakeNotFound(discord.NotFound):
    def __init__(self):
        Exception.__init__(self, "mock public Trade message missing")


class FakeInteraction:
    def __init__(self, user_id, guild_id=44, message=None,
                 interaction_type=discord.InteractionType.component):
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.guild_id = guild_id
        self.message = message
        self.type = interaction_type
        self.response = InteractionResponse(interaction_type)
        self.followup = InteractionFollowup(self.response)

    async def edit_original_response(self, **kwargs):
        raise AssertionError("Trade must finish thinking defers with the documented follow-up")


def make_trade(message=None):
    a = SimpleNamespace(id=11, display_name="A", mention="<@11>")
    b = SimpleNamespace(id=22, display_name="B", mention="<@22>")
    session = trade.TradeSession(44, a, b)
    view = trade.TradeView(session, CURRENCY)
    message = message or FakeMessage()
    session.message = message
    trade._active_trades[session.key()] = session
    return session, view, message


def assert_deferred_completed(interaction, expected_text=None):
    assert interaction.response.is_done()
    assert interaction.response.deferred_thinking
    assert interaction.response.response_type == discord.InteractionResponseType.deferred_channel_message
    assert not interaction.response.thinking_pending
    assert interaction.response.direct_messages == []
    assert len(interaction.followup.messages) == 1
    assert interaction.followup.messages[0]["ephemeral"] is True
    if expected_text is not None:
        assert interaction.followup.messages[0]["content"] == expected_text
    interaction.followup.send.assert_awaited_once()


@pytest.fixture(autouse=True)
def clean_active_trades():
    trade._active_trades.clear()
    yield
    trade._active_trades.clear()


@pytest.mark.asyncio
async def test_ephemeral_picker_modal_updates_public_message_and_completes_defer(monkeypatch):
    session, view, public_message = make_trade()
    session.ready.update({11, 22})
    modal = trade.ItemQuantityModal(view, "Moonstone", 5)
    modal.quantity._value = "3"
    monkeypatch.setattr(trade, "has_item", AsyncMock(return_value=True))
    # The modal source is intentionally not the shared/public Trade message.
    interaction = FakeInteraction(
        11, message=SimpleNamespace(id=999),
        interaction_type=discord.InteractionType.modal_submit)

    await modal.on_submit(interaction)

    assert len(public_message.edits) == 1
    assert "Moonstone ×3" in public_message.edits[0]["embed"].fields[0].value
    assert public_message.edits[0]["view"] is view
    assert session.offers[11]["items"] == {"Moonstone": 3}
    assert session.ready == set()
    assert_deferred_completed(interaction, "Trade offer updated.")


@pytest.mark.asyncio
async def test_currency_manual_item_and_clear_successes_complete_each_defer(monkeypatch):
    session, view, public_message = make_trade()
    session.offers[11]["coins"] = 20
    session.offers[11]["diamonds"] = 2
    session.offers[11]["items"]["Old item"] = 1
    session.ready.update({11, 22})
    interactions = []

    monkeypatch.setattr(trade, "get_balance", AsyncMock(return_value=1000))
    coins_modal = trade.AddCoinsModal(view)
    coins_modal.amount._value = "80"
    coin_interaction = FakeInteraction(22, interaction_type=discord.InteractionType.modal_submit)
    interactions.append(coin_interaction)
    await coins_modal.on_submit(coin_interaction)
    diamonds_modal = trade.AddDiamondsModal(view)
    diamonds_modal.amount._value = "9"
    diamond_interaction = FakeInteraction(22, interaction_type=discord.InteractionType.modal_submit)
    interactions.append(diamond_interaction)
    await diamonds_modal.on_submit(diamond_interaction)

    manual = trade.AddItemModal(view)
    manual.item_name._value = "Manual item"
    manual.quantity._value = "4"
    monkeypatch.setattr(trade, "has_item", AsyncMock(return_value=True))
    manual_interaction = FakeInteraction(22)
    interactions.append(manual_interaction)
    await manual.on_submit(manual_interaction)

    final_embed = public_message.edits[-1]["embed"]
    assert "80 Coins" in final_embed.fields[1].value
    assert "9 Diamonds" in final_embed.fields[1].value
    assert "Manual item ×4" in final_embed.fields[1].value
    assert session.ready == set()

    clear_interaction = FakeInteraction(11, message=public_message)
    interactions.append(clear_interaction)
    clear_button = next(item for item in view.children if getattr(item, "label", None) == "Clear My Offer")
    await clear_button.callback(clear_interaction)
    assert session.offers[11] == trade._empty_offer()
    assert len(public_message.edits) == 4
    for interaction in interactions:
        assert_deferred_completed(interaction, "Trade offer updated.")


@pytest.mark.asyncio
async def test_more_than_25_items_manual_fallback_updates_public_and_completes_defer(monkeypatch):
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
    submit_interaction = FakeInteraction(11, interaction_type=discord.InteractionType.modal_submit)
    await modal.on_submit(submit_interaction)

    assert session.offers[11]["items"] == {"Item 25": 1}
    assert len(public_message.edits) == 1
    assert "Item 25 ×1" in public_message.edits[0]["embed"].fields[0].value
    assert_deferred_completed(submit_interaction, "Trade offer updated.")


@pytest.mark.asyncio
async def test_near_simultaneous_offers_and_ready_complete_each_defer():
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
    assert_deferred_completed(first, "Trade offer updated.")
    assert_deferred_completed(second, "Trade offer updated.")

    ready_button = next(item for item in view.children if getattr(item, "label", None) == "Ready")
    ready_interaction = FakeInteraction(11, message=public_message)
    await ready_button.callback(ready_interaction)
    assert session.ready == {11}
    assert len(public_message.edits) == 3
    assert_deferred_completed(ready_interaction, "Ready status updated.")


@pytest.mark.asyncio
async def test_duplicate_ready_clicks_settle_once_and_complete_both_interactions(monkeypatch):
    session, view, public_message = make_trade()
    execute = AsyncMock(return_value={"success": True, "trade_id": 41})
    monkeypatch.setattr(trade, "execute_trade", execute)
    button = next(item for item in view.children if getattr(item, "label", None) == "Ready")
    first = FakeInteraction(11, message=public_message)
    second = FakeInteraction(22, message=public_message)

    await asyncio.gather(button.callback(first), button.callback(second))

    execute.assert_awaited_once()
    assert session.finished
    assert trade._active_trades.get(session.key()) is None
    assert "Trade complete" in public_message.edits[-1]["embed"].title
    assert_deferred_completed(first, "Ready status updated.")
    assert_deferred_completed(second, "Trade completed.")


@pytest.mark.asyncio
async def test_failed_settlement_updates_public_message_and_completes_second_ready_defer(monkeypatch):
    session, view, public_message = make_trade()
    execute = AsyncMock(return_value={"success": False, "error": "Balance changed."})
    monkeypatch.setattr(trade, "execute_trade", execute)
    button = next(item for item in view.children if getattr(item, "label", None) == "Ready")
    first = FakeInteraction(11, message=public_message)
    second = FakeInteraction(22, message=public_message)

    await button.callback(first)
    await button.callback(second)

    execute.assert_awaited_once()
    assert session.finished
    assert "Trade failed" in public_message.edits[-1]["embed"].title
    assert "Balance changed." in public_message.edits[-1]["embed"].description
    assert_deferred_completed(first, "Ready status updated.")
    assert_deferred_completed(second, "Trade failed. Review the shared message for details.")


@pytest.mark.asyncio
async def test_cancel_updates_public_message_and_completes_defer():
    session, view, public_message = make_trade()
    button = next(item for item in view.children if getattr(item, "label", None) == "Cancel")
    interaction = FakeInteraction(11, message=public_message)

    await button.callback(interaction)

    assert session.finished
    assert trade._active_trades.get(session.key()) is None
    assert len(public_message.edits) == 1
    assert "cancelled" in public_message.edits[0]["embed"].description
    assert_deferred_completed(interaction, "Trade cancelled.")


@pytest.mark.asyncio
async def test_validation_failure_after_defer_completes_privately_without_public_edit(monkeypatch):
    session, view, public_message = make_trade()
    modal = trade.ItemQuantityModal(view, "Missing item", 0)
    modal.quantity._value = "1"
    monkeypatch.setattr(trade, "has_item", AsyncMock(return_value=False))
    interaction = FakeInteraction(11, interaction_type=discord.InteractionType.modal_submit)

    await modal.on_submit(interaction)

    assert public_message.edits == []
    assert session.offers[11]["items"] == {}
    assert_deferred_completed(interaction, "You don't have 1x **Missing item** to offer.")


@pytest.mark.asyncio
async def test_currency_validation_and_database_failures_complete_deferred_responses(monkeypatch):
    session, view, public_message = make_trade()
    coin_modal = trade.AddCoinsModal(view)
    coin_modal.amount._value = "200"
    diamond_modal = trade.AddDiamondsModal(view)
    diamond_modal.amount._value = "2"
    balance_check = AsyncMock(side_effect=[100, RuntimeError("scratch db unavailable")])
    monkeypatch.setattr(trade, "get_balance", balance_check)
    coin_interaction = FakeInteraction(11, interaction_type=discord.InteractionType.modal_submit)
    diamond_interaction = FakeInteraction(11, interaction_type=discord.InteractionType.modal_submit)

    await coin_modal.on_submit(coin_interaction)
    await diamond_modal.on_submit(diamond_interaction)

    assert public_message.edits == []
    assert session.offers[11]["coins"] == 0
    assert session.offers[11]["diamonds"] == 0
    assert_deferred_completed(coin_interaction)
    assert "cannot offer 200" in coin_interaction.followup.messages[0]["content"]
    assert_deferred_completed(
        diamond_interaction, "Could not verify your diamond balance. Please try again.")


@pytest.mark.asyncio
async def test_invalid_modal_input_uses_one_direct_ephemeral_response():
    session, view, public_message = make_trade()
    modal = trade.ItemQuantityModal(view, "Moonstone", 1)
    modal.quantity._value = "not-a-number"
    interaction = FakeInteraction(11, interaction_type=discord.InteractionType.modal_submit)

    await modal.on_submit(interaction)

    assert interaction.response.is_done()
    assert interaction.response.direct_messages == [
        {"content": "Quantity must be a whole number.", "ephemeral": True}
    ]
    assert not interaction.response.deferred_thinking
    assert not interaction.response.thinking_pending
    interaction.followup.send.assert_not_awaited()
    assert public_message.edits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type, expected_text", [
    (FakeHTTPException, "Could not update the shared Trade message. Please try again."),
    (FakeNotFound, "The shared Trade message no longer exists."),
])
async def test_public_message_edit_failure_rolls_back_and_completes_defer(error_type, expected_text):
    failed_message = FakeMessage(fail_on_edit_number=1, edit_error=error_type())
    session, view, _ = make_trade(message=failed_message)
    interaction = FakeInteraction(11)

    result = await view.refresh_public_trade(
        interaction, lambda: session.offers[11].update(coins=45))

    assert result is False
    assert session.offers[11]["coins"] == 0
    assert failed_message.edits == []
    assert_deferred_completed(interaction, expected_text)


@pytest.mark.asyncio
async def test_ready_message_edit_failure_restores_ready_and_completes_defer():
    failed_message = FakeMessage(fail_on_edit_number=1, edit_error=FakeHTTPException())
    session, view, _ = make_trade(message=failed_message)
    button = next(item for item in view.children if getattr(item, "label", None) == "Ready")
    interaction = FakeInteraction(11, message=failed_message)

    await button.callback(interaction)

    assert session.ready == set()
    assert failed_message.edits == []
    assert_deferred_completed(
        interaction, "Could not update the shared Trade message. Please try again.")


@pytest.mark.asyncio
async def test_cancel_message_edit_failure_still_completes_defer():
    failed_message = FakeMessage(fail_on_edit_number=1, edit_error=FakeHTTPException())
    session, view, _ = make_trade(message=failed_message)
    button = next(item for item in view.children if getattr(item, "label", None) == "Cancel")
    interaction = FakeInteraction(11, message=failed_message)

    await button.callback(interaction)

    assert session.finished
    assert failed_message.edits == []
    assert_deferred_completed(
        interaction, "Trade was cancelled, but the shared message could not be updated.")


@pytest.mark.asyncio
async def test_settlement_receipt_edit_failure_completes_defer_after_settlement(monkeypatch):
    failed_message = FakeMessage(fail_on_edit_number=2, edit_error=FakeHTTPException())
    session, view, _ = make_trade(message=failed_message)
    execute = AsyncMock(return_value={"success": True, "trade_id": 42})
    monkeypatch.setattr(trade, "execute_trade", execute)
    button = next(item for item in view.children if getattr(item, "label", None) == "Ready")
    first = FakeInteraction(11, message=failed_message)
    second = FakeInteraction(22, message=failed_message)

    await button.callback(first)
    await button.callback(second)

    execute.assert_awaited_once()
    assert session.finished
    assert_deferred_completed(first, "Ready status updated.")
    assert_deferred_completed(
        second,
        "Trade processing finished, but the shared message could not be updated. Check trade history.")


@pytest.mark.asyncio
async def test_expired_trade_rejects_modal_without_deferring_or_mutating(monkeypatch):
    session, view, public_message = make_trade()
    await view.on_timeout()
    modal = trade.ItemQuantityModal(view, "Moonstone", 5)
    modal.quantity._value = "2"
    has_item = AsyncMock(return_value=True)
    monkeypatch.setattr(trade, "has_item", has_item)
    interaction = FakeInteraction(11, interaction_type=discord.InteractionType.modal_submit)

    await modal.on_submit(interaction)

    assert interaction.response.direct_messages == [
        {"content": "This trade has already ended.", "ephemeral": True}
    ]
    assert not interaction.response.thinking_pending
    interaction.followup.send.assert_not_awaited()
    has_item.assert_not_awaited()
    assert session.offers[11]["items"] == {}
    assert len(public_message.edits) == 1
