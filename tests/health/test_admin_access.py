"""Admin-only System Health visibility and access control."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.storage import initialize_database
from app.telegram.auth import ACCESS_DENIED, is_admin, require_admin
from app.telegram.health.buttons import HEALTH_CALLBACKS, health_inline_keyboard
from app.telegram.inline_menus import main_inline_keyboard
from tests.conftest import make_test_settings


@pytest.fixture
def settings(tmp_path):
    s = make_test_settings(
        database_path=str(tmp_path / "admin.db"),
        admin_ids=[1001, 1002],
    )
    initialize_database(s)
    return s


def _labels(markup) -> list[str]:
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            out.append(btn.text)
    return out


class TestAdminVisibility:
    def test_health_button_admin_only(self):
        assert "🩺 System Health" not in _labels(main_inline_keyboard(is_admin=False))
        assert "🩺 System Health" in _labels(main_inline_keyboard(is_admin=True))

    def test_health_menu_structure(self):
        labels = _labels(health_inline_keyboard())
        assert "📊 Зведення" in labels
        assert "📦 Storage" in labels
        assert "🧪 Self-Test" in labels
        assert "⬅️ Main Menu" in labels

    def test_is_admin(self, settings):
        assert is_admin(settings, 1001)
        assert not is_admin(settings, 9999)
        assert not is_admin(settings, None)


@pytest.mark.asyncio
async def test_unauthorized_command_access(settings):
    update = MagicMock()
    update.effective_user = MagicMock(id=9999)
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    ok = await require_admin(update, settings, action="cmd:/health")
    assert ok is False
    update.effective_message.reply_text.assert_awaited_with(ACCESS_DENIED)


@pytest.mark.asyncio
async def test_unauthorized_callback_access(settings):
    update = MagicMock()
    update.effective_user = MagicMock(id=42)
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    ok = await require_admin(update, settings, action="cb:health:summary")
    assert ok is False


def test_health_callbacks_registered():
    assert "nav:health" in HEALTH_CALLBACKS
    assert "health:selftest" in HEALTH_CALLBACKS
