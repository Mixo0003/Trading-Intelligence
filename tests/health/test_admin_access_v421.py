"""V4.2.1 admin access: toast deny and fail-closed alerts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.storage import initialize_database
from app.health.alerts import dispatch_health_alerts
from app.health.models import ComponentStatus, OverallHealth
from app.telegram.auth import ACCESS_DENIED, require_admin
from app.telegram.health.buttons import health_inline_keyboard
from app.telegram.inline_menus import main_inline_keyboard
from tests.conftest import make_test_settings


def _labels(markup) -> list[str]:
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            out.append(btn.text)
    return out


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "admin_v421.db"),
        admin_ids=[1001],
        health_alerts_enabled=True,
    )
    initialize_database(s)
    return s


def test_admin_sees_health_button():
    assert "🩺 System Health" in _labels(main_inline_keyboard(is_admin=True))


def test_non_admin_hides_health_button():
    assert "🩺 System Health" not in _labels(main_inline_keyboard(is_admin=False))


def test_health_menu_has_product_groups():
    labels = _labels(health_inline_keyboard())
    assert "📊 Зведення" in labels
    assert any("Polymarket" in x for x in labels)
    assert any("Storage" in x for x in labels)


@pytest.mark.asyncio
async def test_non_admin_callback_toast_only(settings):
    update = MagicMock()
    update.effective_user = MagicMock(id=42)
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    ok = await require_admin(
        update,
        settings,
        action="cb:health:summary",
        deny_via_callback_toast=True,
    )
    assert ok is False
    update.callback_query.answer.assert_awaited()
    args, kwargs = update.callback_query.answer.await_args
    assert ACCESS_DENIED in (args[0] if args else kwargs.get("text", ""))
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_admin_command_reply_only(settings):
    update = MagicMock()
    update.effective_user = MagicMock(id=42)
    update.callback_query = None
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    ok = await require_admin(update, settings, action="cmd:/health")
    assert ok is False
    update.effective_message.reply_text.assert_awaited_with(ACCESS_DENIED)


@pytest.mark.asyncio
async def test_missing_admin_sends_zero_alerts(tmp_path):
    settings = make_test_settings(
        database_path=str(tmp_path / "no_admin.db"),
        admin_ids=[],
        health_alerts_enabled=True,
    )
    initialize_database(settings)
    overall = OverallHealth(
        status="DOWN",
        components=(
            ComponentStatus(
                "sqlite",
                "DOWN",
                "db unavailable",
                "t",
                {"reason_code": "db_unavailable"},
            ),
        ),
        checked_at="t",
        summary="down",
    )
    send = AsyncMock()
    sent = await dispatch_health_alerts(settings, overall, send)
    assert sent == 0
    send.assert_not_awaited()
