"""Tests for Telegram cron action callbacks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import _reply_anchor_for_event
from plugins.platforms.telegram.adapter import TelegramAdapter


class _FakeMessage:
    message_id = 649
    chat_id = 24978334
    text = "## Opportunity 1\n\nUseful visibility item"
    caption = None
    message_thread_id = None

    chat = SimpleNamespace(type="private", full_name="Nico", title=None)


class _FakeQuery:
    data = "cr:draft"
    message = _FakeMessage()
    from_user = SimpleNamespace(id=24978334, first_name="Nico", full_name="Nico")

    def __init__(self):
        self.answer = AsyncMock()


@pytest.mark.asyncio
async def test_cron_action_draft_uses_real_telegram_message_id_for_reply_anchor(tmp_path, monkeypatch):
    """Synthetic callback ids must not be used as Telegram reply_to ids.

    Telegram send() casts reply anchors to int. A callback event id like
    "649:cr:draft" therefore makes the later assistant response fail with
    "invalid literal for int()". The event may still encode the action in text,
    but its platform message id must stay the real Telegram message id.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = object.__new__(TelegramAdapter)
    from gateway.config import Platform

    adapter.platform = Platform.TELEGRAM
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    adapter.build_source = TelegramAdapter.build_source.__get__(adapter, TelegramAdapter)
    adapter.handle_message = AsyncMock()

    query = _FakeQuery()

    await adapter._handle_cron_action_callback(
        query,
        "cr:draft",
        query_chat_id=24978334,
        query_chat_type="private",
        query_thread_id=None,
        query_user_name="Nico",
    )

    query.answer.assert_awaited_once_with(text="Drafting reply angle…")
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.message_id == "649:cr:draft"
    assert event.reply_to_message_id == "649"
    assert _reply_anchor_for_event(event) == "649"
    assert "Draft a concise reply" in event.text
