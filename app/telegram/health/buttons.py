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

# Phase 5 Storage UX
CB_HEALTH_STORAGE = "health:storage"
CB_HEALTH_STORAGE_METRICS = "health:storage_metrics"
CB_HEALTH_STORAGE_BACKUP = "health:storage_backup"
CB_HEALTH_STORAGE_BACKUP_CREATE = "health:storage_backup_create"
CB_HEALTH_STORAGE_BACKUP_CONFIRM = "health:storage_backup_confirm"
CB_HEALTH_STORAGE_BACKUP_CANCEL = "health:storage_backup_cancel"
CB_HEALTH_STORAGE_CLEANUP_PREVIEW = "health:storage_cleanup_preview"
CB_HEALTH_STORAGE_RETENTION_PREVIEW = "health:storage_retention_preview"
CB_HEALTH_STORAGE_CLEANUP_RUN = "health:storage_cleanup_run"
CB_HEALTH_STORAGE_CLEANUP_CONFIRM = "health:storage_cleanup_confirm"
CB_HEALTH_STORAGE_CLEANUP_CANCEL = "health:storage_cleanup_cancel"
CB_HEALTH_STORAGE_PRUNE_RUN = "health:storage_prune_run"
CB_HEALTH_STORAGE_PRUNE_CONFIRM = "health:storage_prune_confirm"
CB_HEALTH_STORAGE_PRUNE_CANCEL = "health:storage_prune_cancel"
CB_HEALTH_STORAGE_HISTORY = "health:storage_history"
CB_HEALTH_STORAGE_REFRESH = "health:storage_refresh"
CB_HEALTH_STORAGE_RETENTION_PAGE = "health:storage_ret_page"
CB_HEALTH_STORAGE_HISTORY_PAGE = "health:storage_hist_page"

HEALTH_MENU_TEXT = (
    "🩺 System Health\n"
    "Адмін-діагностика Trading Intelligence.\n"
    "Оберіть розділ:"
)
STORAGE_MENU_TITLE = "📦 Storage"

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
        CB_HEALTH_STORAGE,
        CB_HEALTH_STORAGE_METRICS,
        CB_HEALTH_STORAGE_BACKUP,
        CB_HEALTH_STORAGE_BACKUP_CREATE,
        CB_HEALTH_STORAGE_BACKUP_CONFIRM,
        CB_HEALTH_STORAGE_BACKUP_CANCEL,
        CB_HEALTH_STORAGE_CLEANUP_PREVIEW,
        CB_HEALTH_STORAGE_RETENTION_PREVIEW,
        CB_HEALTH_STORAGE_CLEANUP_RUN,
        CB_HEALTH_STORAGE_CLEANUP_CONFIRM,
        CB_HEALTH_STORAGE_CLEANUP_CANCEL,
        CB_HEALTH_STORAGE_PRUNE_RUN,
        CB_HEALTH_STORAGE_PRUNE_CONFIRM,
        CB_HEALTH_STORAGE_PRUNE_CANCEL,
        CB_HEALTH_STORAGE_HISTORY,
        CB_HEALTH_STORAGE_REFRESH,
        CB_HEALTH_STORAGE_RETENTION_PAGE,
        CB_HEALTH_STORAGE_HISTORY_PAGE,
    }
)


def health_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Зведення", callback_data=CB_HEALTH_SUMMARY)],
            [
                InlineKeyboardButton("⚙️ Core Runtime", callback_data=CB_HEALTH_JOBS),
                InlineKeyboardButton("📦 Storage", callback_data=CB_HEALTH_STORAGE),
            ],
            [
                InlineKeyboardButton("🎯 Polymarket", callback_data=CB_HEALTH_SUMMARY),
                InlineKeyboardButton("🌦 Weather", callback_data=CB_HEALTH_WEATHER),
            ],
            [
                InlineKeyboardButton("👥 Copy", callback_data=CB_HEALTH_COPY),
                InlineKeyboardButton("₿ Crypto", callback_data=CB_HEALTH_SUMMARY),
            ],
            [
                InlineKeyboardButton(
                    "📈 Market Opportunities", callback_data=CB_HEALTH_SUMMARY
                ),
                InlineKeyboardButton("📬 Delivery", callback_data=CB_HEALTH_SUMMARY),
            ],
            [
                InlineKeyboardButton(
                    "🌐 External Sources", callback_data=CB_HEALTH_SUMMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Retention Dry-Run", callback_data=CB_HEALTH_RETENTION
                ),
                InlineKeyboardButton("💾 Backup Status", callback_data=CB_HEALTH_BACKUP),
            ],
            [
                InlineKeyboardButton("🚨 Інциденти", callback_data=CB_HEALTH_INCIDENTS),
                InlineKeyboardButton("🧪 Self-Test", callback_data=CB_HEALTH_SELFTEST),
            ],
            [InlineKeyboardButton("⚙️ Налаштування", callback_data=CB_HEALTH_SETTINGS)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def storage_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📈 Storage Metrics", callback_data=CB_HEALTH_STORAGE_METRICS)],
            [
                InlineKeyboardButton("💾 Backup Status", callback_data=CB_HEALTH_STORAGE_BACKUP),
                InlineKeyboardButton("🛡 Create Backup", callback_data=CB_HEALTH_STORAGE_BACKUP_CREATE),
            ],
            [
                InlineKeyboardButton(
                    "🧹 File Cleanup Preview",
                    callback_data=CB_HEALTH_STORAGE_CLEANUP_PREVIEW,
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 Retention Preview",
                    callback_data=CB_HEALTH_STORAGE_RETENTION_PREVIEW,
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Run Safe File Cleanup",
                    callback_data=CB_HEALTH_STORAGE_CLEANUP_RUN,
                )
            ],
            [
                InlineKeyboardButton(
                    "✂ Run Retention Batch",
                    callback_data=CB_HEALTH_STORAGE_PRUNE_RUN,
                )
            ],
            [
                InlineKeyboardButton(
                    "📜 Maintenance History",
                    callback_data=CB_HEALTH_STORAGE_HISTORY,
                )
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=CB_HEALTH_STORAGE_REFRESH),
                InlineKeyboardButton("⬅️ Back to System Health", callback_data=CB_HEALTH),
            ],
        ]
    )


def health_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ System Health", callback_data=CB_HEALTH)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def storage_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Storage", callback_data=CB_HEALTH_STORAGE)],
            [InlineKeyboardButton("⬅️ System Health", callback_data=CB_HEALTH)],
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


def storage_backup_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Backup",
                    callback_data=CB_HEALTH_STORAGE_BACKUP_CONFIRM,
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=CB_HEALTH_STORAGE_BACKUP_CANCEL,
                )
            ],
            [InlineKeyboardButton("⬅️ Storage", callback_data=CB_HEALTH_STORAGE)],
        ]
    )


def storage_cleanup_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Cleanup",
                    callback_data=CB_HEALTH_STORAGE_CLEANUP_CONFIRM,
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=CB_HEALTH_STORAGE_CLEANUP_CANCEL,
                )
            ],
            [InlineKeyboardButton("⬅️ Storage", callback_data=CB_HEALTH_STORAGE)],
        ]
    )


def storage_prune_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Retention Batch",
                    callback_data=CB_HEALTH_STORAGE_PRUNE_CONFIRM,
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=CB_HEALTH_STORAGE_PRUNE_CANCEL,
                )
            ],
            [InlineKeyboardButton("⬅️ Storage", callback_data=CB_HEALTH_STORAGE)],
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
