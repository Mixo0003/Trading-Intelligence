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
CB_HEALTH_RETENTION = "health:retention"
CB_HEALTH_BACKUP = "health:backup"
CB_HEALTH_BACKUP_CREATE = "health:backup_create"
CB_HEALTH_BACKUP_CONFIRM = "health:backup_confirm"
CB_HEALTH_BACKUP_CANCEL = "health:backup_cancel"
CB_HEALTH_REBUILD_BASELINES = "health:rebuild_baselines"
CB_HEALTH_REBUILD_CONFIRM = "health:rebuild_confirm"
CB_HEALTH_REBUILD_CANCEL = "health:rebuild_cancel"

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
        CB_HEALTH_RETENTION,
        CB_HEALTH_BACKUP,
        CB_HEALTH_BACKUP_CREATE,
        CB_HEALTH_BACKUP_CONFIRM,
        CB_HEALTH_BACKUP_CANCEL,
        CB_HEALTH_REBUILD_BASELINES,
        CB_HEALTH_REBUILD_CONFIRM,
        CB_HEALTH_REBUILD_CANCEL,
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
                InlineKeyboardButton(
                    "📋 Retention Dry-Run", callback_data=CB_HEALTH_RETENTION
                ),
                InlineKeyboardButton("💾 Backup Status", callback_data=CB_HEALTH_BACKUP),
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


def backup_health_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💾 Create Backup",
                    callback_data=CB_HEALTH_BACKUP_CREATE,
                )
            ],
            [InlineKeyboardButton("⬅️ System Health", callback_data=CB_HEALTH)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def backup_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Backup",
                    callback_data=CB_HEALTH_BACKUP_CONFIRM,
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=CB_HEALTH_BACKUP_CANCEL,
                )
            ],
            [InlineKeyboardButton("⬅️ System Health", callback_data=CB_HEALTH)],
        ]
    )


def copy_health_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Rebuild Copy Baselines",
                    callback_data=CB_HEALTH_REBUILD_BASELINES,
                )
            ],
            [InlineKeyboardButton("⬅️ System Health", callback_data=CB_HEALTH)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def rebuild_baselines_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Rebuild",
                    callback_data=CB_HEALTH_REBUILD_CONFIRM,
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=CB_HEALTH_REBUILD_CANCEL,
                )
            ],
            [InlineKeyboardButton("⬅️ System Health", callback_data=CB_HEALTH)],
        ]
    )
