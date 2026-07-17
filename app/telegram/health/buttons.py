"""Inline keyboards for System Health (admin-only)."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.telegram.inline_menus import CB_MAIN

CB_HEALTH = "nav:health"
CB_HEALTH_SUMMARY = "health:summary"
CB_HEALTH_WEATHER = "health:weather"
CB_HEALTH_COPY = "health:copy"
CB_HEALTH_JOBS = "health:jobs"
CB_HEALTH_DB = "health:db"
CB_HEALTH_INCIDENTS = "health:incidents"
CB_HEALTH_SELFTEST = "health:selftest"
CB_HEALTH_SETTINGS = "health:settings"

HEALTH_MENU_TEXT = "🩺 System Health\nAdmin diagnostics — choose a section:"

HEALTH_CALLBACKS = frozenset(
    {
        CB_HEALTH,
        CB_HEALTH_SUMMARY,
        CB_HEALTH_WEATHER,
        CB_HEALTH_COPY,
        CB_HEALTH_JOBS,
        CB_HEALTH_DB,
        CB_HEALTH_INCIDENTS,
        CB_HEALTH_SELFTEST,
        CB_HEALTH_SETTINGS,
    }
)


def health_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Health Summary", callback_data=CB_HEALTH_SUMMARY)],
            [
                InlineKeyboardButton("🌦 Weather Providers", callback_data=CB_HEALTH_WEATHER),
                InlineKeyboardButton("👥 Copy Intelligence", callback_data=CB_HEALTH_COPY),
            ],
            [
                InlineKeyboardButton("⏱ Jobs & Scheduler", callback_data=CB_HEALTH_JOBS),
                InlineKeyboardButton("🗄 Database & Cache", callback_data=CB_HEALTH_DB),
            ],
            [
                InlineKeyboardButton("🚨 Recent Incidents", callback_data=CB_HEALTH_INCIDENTS),
                InlineKeyboardButton("🧪 Run Self-Test", callback_data=CB_HEALTH_SELFTEST),
            ],
            [InlineKeyboardButton("⚙️ Health Settings", callback_data=CB_HEALTH_SETTINGS)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def health_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ System Health", callback_data=CB_HEALTH)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )
