"""Compact inline keyboards for Trading Intelligence navigation."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

# Callback data prefixes (keep under Telegram 64-byte limit).
CB_MAIN = "nav:main"
CB_WEATHER = "nav:weather"
CB_COPY = "nav:copy"
CB_TOP = "nav:top"
CB_OPP = "nav:opp"
CB_SETTINGS = "nav:settings"
CB_HEALTH = "nav:health"

CB_WX_SCAN_USA = "wx:scan:usa"
CB_WX_SCAN_EUROPE = "wx:scan:europe"
CB_WX_SCAN_ASIA = "wx:scan:asia"
CB_WX_SCAN_ALL = "wx:scan:all"
CB_WX_TOP = "wx:top"
CB_WX_FORECAST = "wx:forecast"

CB_COPY_DISCOVER = "copy:discover"
CB_COPY_WATCHLIST = "copy:watchlist"
CB_COPY_STATUS = "copy:status"
CB_COPY_ALERTS = "copy:alerts"
CB_COPY_PAPER = "copy:paper"
CB_COPY_SETTINGS = "copy:settings"

CB_WL_PREV = "copy:wl:prev"
CB_WL_NEXT = "copy:wl:next"
CB_WL_BACK = "copy:wl:back"
# Dynamic: copy:wl:pause:<wallet>, copy:wl:resume:<wallet>,
# copy:wl:rmq:<wallet>, copy:wl:rmy:<wallet>, copy:wl:rmn:<wallet>

CB_DISC_TOP = "disc:top"
CB_DISC_FIND = "disc:find"
CB_DISC_CAT = "disc:cat"
CB_DISC_RISING = "disc:rising"
CB_DISC_RISK = "disc:risk"
CB_DISC_PREV = "disc:prev"
CB_DISC_NEXT = "disc:next"
CB_DISC_BACK = "disc:back"

CB_SET_CONSERVATIVE = "cset:cons"
CB_SET_BALANCED = "cset:bal"
CB_SET_EXPLORATORY = "cset:exp"
CB_SET_ALERT_FOLLOW = "cset:af"
CB_SET_ALERT_BOTH = "cset:ab"
CB_SET_EDIT = "cset:edit"
CB_SET_RESET = "cset:rst"
CB_SET_CONFIRM = "cset:ok"
CB_SET_CANCEL = "cset:no"
CB_SET_RESET_OK = "cset:rok"
CB_SET_RESET_NO = "cset:rno"
# Dynamic: cset:f:<code>, cset:pv:<code>:<idx>

# Dynamic: disc:add:<wallet> (wallet = 42 chars → ~50 total)

# Market Opportunity Scanner (opp: / oset: — distinct from nav:top / wx:top / cset:)
CB_OPP_TOP = "opp:top"
CB_OPP_ALERTS = "opp:alerts"
CB_OPP_FILTERS = "opp:filters"
CB_OPP_LIVE = "opp:live"
CB_OPP_HISTORY = "opp:history"
CB_OPP_STATUS = "opp:status"
CB_OPP_BACK = "opp:back"
CB_OSET_EDIT = "oset:edit"
CB_OSET_RESET = "oset:rst"
CB_OSET_CONFIRM = "oset:ok"
CB_OSET_CANCEL = "oset:no"
CB_OSET_RESET_OK = "oset:rok"
CB_OSET_RESET_NO = "oset:rno"
CB_OSET_CAT = "oset:cat"
CB_OSET_LIVE = "oset:lm"
CB_OSET_ALERT = "oset:am"
# Legacy combined edit menu; routed to Alert Settings for backwards compatibility.
CB_OSET_EDIT_LEGACY = CB_OSET_EDIT
# Dynamic: oset:f:<code>, oset:pv:<code>:<idx>, oset:ct:<id>, oset:cta, oset:ctc


def remove_reply_keyboard() -> ReplyKeyboardRemove:
    """Clear any leftover persistent reply keyboard in existing chats."""
    return ReplyKeyboardRemove()


def main_inline_keyboard(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🌤 Weather Intelligence", callback_data=CB_WEATHER)],
        [InlineKeyboardButton("📋 Copy Trading Intelligence", callback_data=CB_COPY)],
        [InlineKeyboardButton("🎯 Market Opportunities", callback_data=CB_OPP)],
        [InlineKeyboardButton("📊 Top Opportunities", callback_data=CB_TOP)],
        [InlineKeyboardButton("⚙️ Settings", callback_data=CB_SETTINGS)],
    ]
    if is_admin:
        rows.insert(
            -1,
            [InlineKeyboardButton("🩺 System Health", callback_data=CB_HEALTH)],
        )
    return InlineKeyboardMarkup(rows)


def weather_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔍 Scan USA", callback_data=CB_WX_SCAN_USA),
                InlineKeyboardButton("🌍 Scan Europe", callback_data=CB_WX_SCAN_EUROPE),
            ],
            [
                InlineKeyboardButton("🌏 Scan Asia", callback_data=CB_WX_SCAN_ASIA),
                InlineKeyboardButton("🗺 All Regions", callback_data=CB_WX_SCAN_ALL),
            ],
            [
                InlineKeyboardButton("📈 Top Signals", callback_data=CB_WX_TOP),
                InlineKeyboardButton("📅 Forecasts", callback_data=CB_WX_FORECAST),
            ],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def copy_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏆 Trader Discovery", callback_data=CB_COPY_DISCOVER)],
            [
                InlineKeyboardButton("👀 Watchlist", callback_data=CB_COPY_WATCHLIST),
                InlineKeyboardButton("📡 Copy Status", callback_data=CB_COPY_STATUS),
            ],
            [
                InlineKeyboardButton("🔔 Copy Alerts", callback_data=CB_COPY_ALERTS),
                InlineKeyboardButton("📒 Paper Portfolio", callback_data=CB_COPY_PAPER),
            ],
            [InlineKeyboardButton("⚙️ Copy Settings", callback_data=CB_COPY_SETTINGS)],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data=CB_MAIN)],
        ]
    )


def discovery_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏆 Top Traders", callback_data=CB_DISC_TOP),
                InlineKeyboardButton("🔎 Find Traders", callback_data=CB_DISC_FIND),
            ],
            [
                InlineKeyboardButton("📂 Category Leaders", callback_data=CB_DISC_CAT),
                InlineKeyboardButton("🆕 Rising Traders", callback_data=CB_DISC_RISING),
            ],
            [
                InlineKeyboardButton("⚠️ High Risk", callback_data=CB_DISC_RISK),
                InlineKeyboardButton("⚙️ Copy Settings", callback_data=CB_COPY_SETTINGS),
            ],
            [
                InlineKeyboardButton("👀 Watchlist", callback_data=CB_COPY_WATCHLIST),
                InlineKeyboardButton("⬅️ Copy Menu", callback_data=CB_COPY),
            ],
        ]
    )


def copy_settings_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🛡 Conservative", callback_data=CB_SET_CONSERVATIVE),
                InlineKeyboardButton("⚖ Balanced", callback_data=CB_SET_BALANCED),
            ],
            [InlineKeyboardButton("🚀 Exploratory", callback_data=CB_SET_EXPLORATORY)],
            [
                InlineKeyboardButton("Alerts: FOLLOW only", callback_data=CB_SET_ALERT_FOLLOW),
                InlineKeyboardButton("Alerts: FOLLOW+WATCH", callback_data=CB_SET_ALERT_BOTH),
            ],
            [InlineKeyboardButton("✏️ Edit Settings", callback_data=CB_SET_EDIT)],
            [InlineKeyboardButton("↩ Reset to Balanced", callback_data=CB_SET_RESET)],
            [InlineKeyboardButton("⬅️ Copy Menu", callback_data=CB_COPY)],
        ]
    )


def copy_settings_edit_keyboard() -> InlineKeyboardMarkup:
    from app.copy_intel.settings_bounds import BOUNDS, FIELD_BY_KEY

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, bound in BOUNDS.items():
        code = FIELD_BY_KEY[key]
        row.append(InlineKeyboardButton(bound.label, callback_data=f"cset:f:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Settings", callback_data=CB_COPY_SETTINGS)])
    return InlineKeyboardMarkup(rows)


def copy_settings_field_keyboard(field_key: str) -> InlineKeyboardMarkup:
    from app.copy_intel.settings_bounds import FIELD_BY_KEY, preset_chips

    code = FIELD_BY_KEY[field_key]
    rows: list[list[InlineKeyboardButton]] = []
    chips = preset_chips(field_key)
    chip_row: list[InlineKeyboardButton] = []
    for idx, (label, _value) in enumerate(chips):
        chip_row.append(
            InlineKeyboardButton(label, callback_data=f"cset:pv:{code}:{idx}")
        )
        if len(chip_row) == 3:
            rows.append(chip_row)
            chip_row = []
    if chip_row:
        rows.append(chip_row)
    rows.append([InlineKeyboardButton("⬅️ Edit Settings", callback_data=CB_SET_EDIT)])
    rows.append([InlineKeyboardButton("⬅️ Settings", callback_data=CB_COPY_SETTINGS)])
    return InlineKeyboardMarkup(rows)


def copy_settings_confirm_keyboard(*, reset: bool = False) -> InlineKeyboardMarkup:
    if reset:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirm reset", callback_data=CB_SET_RESET_OK),
                    InlineKeyboardButton("❌ Cancel", callback_data=CB_SET_RESET_NO),
                ],
                [InlineKeyboardButton("⬅️ Settings", callback_data=CB_COPY_SETTINGS)],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm save", callback_data=CB_SET_CONFIRM),
                InlineKeyboardButton("❌ Cancel", callback_data=CB_SET_CANCEL),
            ],
            [InlineKeyboardButton("⬅️ Settings", callback_data=CB_COPY_SETTINGS)],
        ]
    )


def empty_watchlist_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏆 Trader Discovery", callback_data=CB_COPY_DISCOVER)],
            [InlineKeyboardButton("⬅️ Copy Menu", callback_data=CB_COPY)],
        ]
    )


def watchlist_card_keyboard(
    wallet: str,
    *,
    index: int,
    total: int,
    enabled: bool,
    profile_url: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if enabled:
        rows.append(
            [InlineKeyboardButton("⏸ Pause", callback_data=f"copy:wl:pause:{wallet}")]
        )
    else:
        rows.append(
            [InlineKeyboardButton("▶️ Resume", callback_data=f"copy:wl:resume:{wallet}")]
        )
    rows.append(
        [
            InlineKeyboardButton("🗑 Unsubscribe", callback_data=f"copy:wl:rmq:{wallet}"),
            InlineKeyboardButton("🔗 Open Profile", url=profile_url),
        ]
    )
    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(InlineKeyboardButton("Previous", callback_data=CB_WL_PREV))
    if index < total - 1:
        nav.append(InlineKeyboardButton("Next", callback_data=CB_WL_NEXT))
    nav.append(InlineKeyboardButton("Back", callback_data=CB_WL_BACK))
    rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Watchlist", callback_data=CB_COPY_WATCHLIST)])
    return InlineKeyboardMarkup(rows)


def watchlist_unsubscribe_confirm_keyboard(wallet: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Unsubscribe",
                    callback_data=f"copy:wl:rmy:{wallet}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✖️ Cancel",
                    callback_data=f"copy:wl:rmn:{wallet}",
                )
            ],
            [InlineKeyboardButton("⬅️ Watchlist", callback_data=CB_COPY_WATCHLIST)],
        ]
    )


def trader_actions_keyboard(
    wallet: str,
    *,
    index: int,
    total: int,
    profile_url: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("➕ Add to Watchlist", callback_data=f"disc:add:{wallet}"),
            InlineKeyboardButton("🔗 Open Profile", url=profile_url),
        ]
    ]
    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(InlineKeyboardButton("◀️ Previous", callback_data=CB_DISC_PREV))
    if index < total - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=CB_DISC_NEXT))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=CB_DISC_BACK)])
    return InlineKeyboardMarkup(rows)


OPP_MENU_TEXT = (
    "🎯 Market Opportunities\n"
    "Cached scanner results only — analysis mode.\n"
    "Does not place trades."
)


def opportunity_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Top Opportunities", callback_data=CB_OPP_TOP)],
            [
                InlineKeyboardButton("Alert Settings", callback_data=CB_OPP_ALERTS),
                InlineKeyboardButton("Filters", callback_data=CB_OPP_FILTERS),
            ],
            [
                InlineKeyboardButton("Live Mode", callback_data=CB_OPP_LIVE),
                InlineKeyboardButton("Signal History", callback_data=CB_OPP_HISTORY),
            ],
            [InlineKeyboardButton("Scanner Status", callback_data=CB_OPP_STATUS)],
            [InlineKeyboardButton("⬅️ Back", callback_data=CB_MAIN)],
        ]
    )


def opportunity_filters_keyboard() -> InlineKeyboardMarkup:
    """Filter fields only: score, liquidity, volume, spreads, slippage, scope."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Min score", callback_data="oset:f:ms"),
                InlineKeyboardButton("Min liquidity", callback_data="oset:f:ml"),
            ],
            [
                InlineKeyboardButton("Min 24h volume", callback_data="oset:f:mv"),
                InlineKeyboardButton("Max rel spread", callback_data="oset:f:rs"),
            ],
            [
                InlineKeyboardButton("Max abs spread", callback_data="oset:f:as"),
                InlineKeyboardButton("Max slippage", callback_data="oset:f:sl"),
            ],
            [
                InlineKeyboardButton("Categories", callback_data=CB_OSET_CAT),
                InlineKeyboardButton("Live mode", callback_data=CB_OSET_LIVE),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data=CB_OPP)],
        ]
    )


def opportunity_settings_keyboard() -> InlineKeyboardMarkup:
    """Alert-related fields only: mode, cooldown, quiet hours, timezone, paper size."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Alert mode", callback_data=CB_OSET_ALERT),
                InlineKeyboardButton("Cooldown", callback_data="oset:f:cd"),
            ],
            [
                InlineKeyboardButton("Quiet hours", callback_data="oset:f:qh"),
                InlineKeyboardButton("Timezone", callback_data="oset:f:tz"),
            ],
            [InlineKeyboardButton("Paper allocation", callback_data="oset:f:pa")],
            [InlineKeyboardButton("Reset all to Balanced", callback_data=CB_OSET_RESET)],
            [InlineKeyboardButton("⬅️ Back", callback_data=CB_OPP)],
        ]
    )


def opportunity_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=CB_OSET_CONFIRM),
                InlineKeyboardButton("✖️ Cancel", callback_data=CB_OSET_CANCEL),
            ]
        ]
    )


def opportunity_reset_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm Reset", callback_data=CB_OSET_RESET_OK),
                InlineKeyboardButton("✖️ Cancel", callback_data=CB_OSET_RESET_NO),
            ]
        ]
    )


def opportunity_live_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("OFF", callback_data="oset:pv:lm:0"),
                InlineKeyboardButton("STRICT", callback_data="oset:pv:lm:1"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data=CB_OPP_FILTERS)],
        ]
    )


def opportunity_alert_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ON_DEMAND_ONLY", callback_data="oset:pv:am:0"),
                InlineKeyboardButton("INSTANT", callback_data="oset:pv:am:1"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data=CB_OPP_ALERTS)],
        ]
    )


def opportunity_allocation_keyboard() -> InlineKeyboardMarkup:
    presets = ("25", "50", "100", "250", "500", "1000")
    rows = []
    row: list[InlineKeyboardButton] = []
    for idx, preset in enumerate(presets):
        row.append(
            InlineKeyboardButton(f"${preset}", callback_data=f"oset:pv:pa:{idx}")
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=CB_OPP_ALERTS)])
    return InlineKeyboardMarkup(rows)


def opportunity_category_keyboard(
    selected: frozenset[str] | None, *, all_categories: bool
) -> InlineKeyboardMarkup:
    from app.opportunity_intel.categories import CATEGORY_LABELS, KNOWN_CATEGORY_IDS

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for category_id in KNOWN_CATEGORY_IDS:
        enabled = all_categories or (selected is not None and category_id in selected)
        mark = "✅" if enabled else "⬜"
        label = CATEGORY_LABELS.get(category_id, category_id)
        row.append(
            InlineKeyboardButton(
                f"{mark} {label}",
                callback_data=f"oset:ct:{category_id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("Select all", callback_data="oset:cta"),
            InlineKeyboardButton("Clear", callback_data="oset:ctc"),
        ]
    )
    rows.append(
        [InlineKeyboardButton("✅ Preview save", callback_data="oset:ctp")]
    )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=CB_OPP_FILTERS)])
    return InlineKeyboardMarkup(rows)


MAIN_MENU_TEXT = "Trading Intelligence\nChoose a section:"
WEATHER_MENU_TEXT = "🌤 Weather Intelligence\nChoose an action:"
COPY_MENU_TEXT = "📋 Copy Trading\nMode: analysis only (read-only)"
DISCOVERY_MENU_TEXT = "🏆 Trader Discovery\nPublic Polymarket data"
SETTINGS_MENU_TEXT = (
    "⚙️ Settings\n"
    "Mode: ANALYSIS ONLY\n"
    "Copy Intel: read-only\n"
    "Secrets are never shown."
)
