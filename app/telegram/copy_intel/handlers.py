"""Telegram handlers for copy-trading intelligence (inline-menu UX)."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.config.settings import Settings
from app.copy_intel.discovery import filter_scores, run_discovery
from app.copy_intel.risk import assert_read_only
from app.copy_intel.scoring import score_trader
from app.copy_intel.settings_profiles import get_user_settings, set_profile, update_setting
from app.copy_intel.watchlist import (
    add_wallet,
    can_manage_entry,
    list_entries,
    list_entries_for_user,
    profile_url,
    remove_wallet,
    set_monitoring,
    toggle_wallet,
)
from app.database import copy_storage
from app.database.copy_storage import get_recent_recommendations
from app.telegram.auth import is_admin
from app.telegram.copy_intel.formatting import (
    format_copy_status,
    format_paper_portfolio,
    format_recent_alerts,
    format_settings_card,
    format_subscription_card,
    format_trader_card,
    format_unsubscribe_confirm,
    format_watchlist,
    format_watchlist_empty,
)
from app.telegram.inline_menus import (
    CB_COPY,
    CB_DISC_BACK,
    CB_DISC_NEXT,
    CB_DISC_PREV,
    CB_SET_ALERT_BOTH,
    CB_SET_ALERT_FOLLOW,
    CB_SET_BALANCED,
    CB_SET_CONSERVATIVE,
    CB_SET_EXPLORATORY,
    CB_WL_BACK,
    CB_WL_NEXT,
    CB_WL_PREV,
    COPY_MENU_TEXT,
    DISCOVERY_MENU_TEXT,
    copy_inline_keyboard,
    copy_settings_inline_keyboard,
    discovery_inline_keyboard,
    empty_watchlist_keyboard,
    trader_actions_keyboard,
    watchlist_card_keyboard,
    watchlist_unsubscribe_confirm_keyboard,
)
from app.telegram.nav import edit_or_reply

logger = logging.getLogger(__name__)

_DISCOVERY_CACHE_KEY = "copy_discovery_scores"
_DISCOVERY_VIEW_KEY = "copy_discovery_view"
_DISCOVERY_INDEX_KEY = "copy_discovery_index"
_WATCHLIST_INDEX_KEY = "copy_watchlist_index"


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def copy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        COPY_MENU_TEXT,
        reply_markup=copy_inline_keyboard(),
    )


async def copy_discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        DISCOVERY_MENU_TEXT,
        reply_markup=discovery_inline_keyboard(),
    )


async def copy_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_watchlist(update, context, edit=False)


async def copy_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    assert_read_only(settings)
    if not context.args:
        await update.effective_message.reply_text("Usage: /copy_add 0x… [label]")
        return
    address = context.args[0]
    label = " ".join(context.args[1:]) if len(context.args) > 1 else None
    user_id = update.effective_user.id if update.effective_user else None
    try:
        wallet = add_wallet(settings, address, label=label, added_by=user_id)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(f"Added {wallet}" + (f" ({label})" if label else ""))


async def copy_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if not context.args:
        await update.effective_message.reply_text("Usage: /copy_remove 0x…")
        return
    entry = _require_manageable_entry(update, context, context.args[0])
    if entry is None:
        await update.effective_message.reply_text("Wallet not on watchlist.")
        return
    actor = str(update.effective_user.id if update.effective_user else "system")
    try:
        removed = remove_wallet(settings, context.args[0], actor=actor)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text("Removed." if removed else "Wallet not on watchlist.")


async def copy_toggle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    if not context.args:
        await update.effective_message.reply_text("Usage: /copy_toggle 0x…")
        return
    entry = _require_manageable_entry(update, context, context.args[0])
    if entry is None:
        await update.effective_message.reply_text("Wallet not on watchlist.")
        return
    actor = str(update.effective_user.id if update.effective_user else "system")
    try:
        enabled = toggle_wallet(settings, context.args[0], actor=actor)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    if enabled is None:
        await update.effective_message.reply_text("Wallet not on watchlist.")
        return
    await update.effective_message.reply_text("Monitoring enabled." if enabled else "Monitoring disabled.")


async def copy_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_copy_status(update, context, edit=False)


async def copy_alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_copy_alerts(update, context, edit=False)


async def copy_paper_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_paper(update, context, edit=False)


async def copy_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_copy_settings(update, context, edit=False)


def _watchlist_text(settings: Settings) -> str:
    """Legacy compact list for slash-command fallback."""
    entries = list_entries(settings)
    metrics_by_wallet: dict[str, float] = {}
    for entry in entries:
        raw = copy_storage.load_trader_metrics(settings, entry.wallet)
        if not raw:
            continue
        from app.copy_intel.models import TraderMetrics

        metrics = TraderMetrics(
            wallet=entry.wallet,
            trade_count=raw.get("trade_count", 0),
            resolved_count=raw.get("resolved_count", 0),
            win_rate=raw.get("win_rate", 0.0),
            realized_pnl=raw.get("realized_pnl", 0.0),
            unrealized_pnl=raw.get("unrealized_pnl", 0.0),
            avg_entry_price=raw.get("avg_entry_price", 0.0),
            avg_holding_hours=raw.get("avg_holding_hours", 0.0),
            max_drawdown=raw.get("max_drawdown", 0.0),
            category_concentration=raw.get("category_concentration", 0.0),
            recent_pnl_30d=raw.get("recent_pnl_30d", 0.0),
            lifetime_pnl=raw.get("lifetime_pnl", 0.0),
            wash_score=raw.get("wash_score", 0.0),
            eligible=raw.get("eligible", False),
            ineligible_reason=raw.get("ineligible_reason"),
        )
        metrics_by_wallet[entry.wallet] = score_trader(metrics)
    return format_watchlist(entries, metrics_by_wallet)


def _user_id(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


def _user_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return is_admin(_settings(context), _user_id(update))


def _managed_entries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return list_entries_for_user(
        _settings(context),
        _user_id(update),
        is_admin=_user_is_admin(update, context),
    )


def _require_manageable_entry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    wallet: str,
):
    settings = _settings(context)
    entry = copy_storage.get_watchlist_entry(settings, wallet)
    if not can_manage_entry(entry, _user_id(update), is_admin=_user_is_admin(update, context)):
        return None
    return entry


async def show_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    entries = _managed_entries(update, context)
    if not entries:
        text = format_watchlist_empty()
        markup = empty_watchlist_keyboard()
        if edit:
            await edit_or_reply(update, text, reply_markup=markup)
        else:
            await update.effective_message.reply_text(text, reply_markup=markup)
        return

    index = int(context.user_data.get(_WATCHLIST_INDEX_KEY) or 0)
    index = max(0, min(index, len(entries) - 1))
    context.user_data[_WATCHLIST_INDEX_KEY] = index
    await _render_watchlist_card(update, context, entries=entries, index=index, edit=edit)


async def _render_watchlist_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    entries: list | None = None,
    index: int | None = None,
    edit: bool = True,
) -> None:
    settings = _settings(context)
    entries = entries if entries is not None else _managed_entries(update, context)
    if not entries:
        text = format_watchlist_empty()
        markup = empty_watchlist_keyboard()
        if edit:
            await edit_or_reply(update, text, reply_markup=markup)
        else:
            await update.effective_message.reply_text(text, reply_markup=markup)
        return

    idx = int(context.user_data.get(_WATCHLIST_INDEX_KEY) or 0) if index is None else index
    idx = max(0, min(idx, len(entries) - 1))
    context.user_data[_WATCHLIST_INDEX_KEY] = idx
    entry = entries[idx]
    state = copy_storage.get_wallet_monitor_state(settings, entry.wallet)
    user = get_user_settings(settings, _user_id(update) or 0)
    text = format_subscription_card(
        label=entry.label,
        wallet=entry.wallet,
        enabled=entry.enabled,
        added_at=entry.created_at,
        baseline_status=state.baseline_status if state else "PENDING",
        last_poll_at=state.last_polled_at if state else None,
        last_new_event_at=copy_storage.get_latest_alertable_event_at(settings, entry.wallet),
        alert_decisions=user.alert_decisions,
        profile_url=profile_url(entry.wallet),
        index=idx,
        total=len(entries),
    )
    markup = watchlist_card_keyboard(
        entry.wallet,
        index=idx,
        total=len(entries),
        enabled=entry.enabled,
        profile_url=profile_url(entry.wallet),
    )
    if edit:
        await edit_or_reply(update, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def watchlist_nav(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    if data == CB_WL_BACK:
        await edit_or_reply(update, COPY_MENU_TEXT, reply_markup=copy_inline_keyboard())
        return
    entries = _managed_entries(update, context)
    if not entries:
        await show_watchlist(update, context, edit=True)
        return
    index = int(context.user_data.get(_WATCHLIST_INDEX_KEY) or 0)
    if data == CB_WL_PREV:
        context.user_data[_WATCHLIST_INDEX_KEY] = max(0, index - 1)
    elif data == CB_WL_NEXT:
        context.user_data[_WATCHLIST_INDEX_KEY] = min(len(entries) - 1, index + 1)
    await _render_watchlist_card(update, context, entries=entries, edit=True)


async def watchlist_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    """Handle pause / resume / unsubscribe confirm flow callbacks."""
    settings = _settings(context)
    actor = str(_user_id(update) or "system")

    if data.startswith("copy:wl:pause:"):
        wallet = data.removeprefix("copy:wl:pause:")
        entry = _require_manageable_entry(update, context, wallet)
        if entry is None:
            await edit_or_reply(
                update,
                "Not authorized to manage this subscription.",
                reply_markup=empty_watchlist_keyboard(),
            )
            return
        result = set_monitoring(settings, wallet, enabled=False, actor=actor)
        if result == "missing":
            await show_watchlist(update, context, edit=True)
            return
        await _render_watchlist_card(update, context, edit=True)
        return

    if data.startswith("copy:wl:resume:"):
        wallet = data.removeprefix("copy:wl:resume:")
        entry = _require_manageable_entry(update, context, wallet)
        if entry is None:
            await edit_or_reply(
                update,
                "Not authorized to manage this subscription.",
                reply_markup=empty_watchlist_keyboard(),
            )
            return
        result = set_monitoring(settings, wallet, enabled=True, actor=actor)
        if result == "missing":
            await show_watchlist(update, context, edit=True)
            return
        await _render_watchlist_card(update, context, edit=True)
        return

    if data.startswith("copy:wl:rmq:"):
        wallet = data.removeprefix("copy:wl:rmq:")
        entry = _require_manageable_entry(update, context, wallet)
        if entry is None:
            await edit_or_reply(
                update,
                "Not authorized to manage this subscription.",
                reply_markup=empty_watchlist_keyboard(),
            )
            return
        await edit_or_reply(
            update,
            format_unsubscribe_confirm(label=entry.label, wallet=entry.wallet),
            reply_markup=watchlist_unsubscribe_confirm_keyboard(entry.wallet),
        )
        return

    if data.startswith("copy:wl:rmy:"):
        wallet = data.removeprefix("copy:wl:rmy:")
        entry = _require_manageable_entry(update, context, wallet)
        if entry is None:
            # Already removed or not owned — safe no-op into empty/list view.
            await show_watchlist(update, context, edit=True)
            return
        remove_wallet(settings, wallet, actor=actor)
        entries = _managed_entries(update, context)
        if not entries:
            context.user_data[_WATCHLIST_INDEX_KEY] = 0
            await show_watchlist(update, context, edit=True)
            return
        index = int(context.user_data.get(_WATCHLIST_INDEX_KEY) or 0)
        context.user_data[_WATCHLIST_INDEX_KEY] = min(index, len(entries) - 1)
        await _render_watchlist_card(update, context, entries=entries, edit=True)
        return

    if data.startswith("copy:wl:rmn:"):
        wallet = data.removeprefix("copy:wl:rmn:")
        entry = _require_manageable_entry(update, context, wallet)
        if entry is None:
            await show_watchlist(update, context, edit=True)
            return
        await _render_watchlist_card(update, context, edit=True)
        return


async def show_copy_status(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    settings = _settings(context)
    poll_state = {
        "last_poll_at": copy_storage.get_poll_state(settings, "last_poll_at"),
        "last_event_count": copy_storage.get_poll_state(settings, "last_event_count"),
        "last_error": copy_storage.get_poll_state(settings, "last_error"),
        "last_discovery_count": copy_storage.get_poll_state(settings, "last_discovery_count"),
    }
    text = format_copy_status(settings, poll_state)
    disc = poll_state.get("last_discovery_count")
    if disc:
        text += f"\nLast discovery candidates: {disc}"
    markup = copy_inline_keyboard()
    if edit:
        await edit_or_reply(update, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def show_copy_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    rows = get_recent_recommendations(_settings(context), limit=10)
    text = format_recent_alerts(rows)
    markup = copy_inline_keyboard()
    if edit:
        await edit_or_reply(update, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def show_paper(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    settings = _settings(context)
    exposure = copy_storage.paper_exposure_totals(settings)
    text = format_paper_portfolio(settings.copy_paper_bankroll_usd, exposure)
    markup = copy_inline_keyboard()
    if edit:
        await edit_or_reply(update, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def show_copy_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    settings = _settings(context)
    telegram_id = update.effective_user.id if update.effective_user else 0
    user = get_user_settings(settings, telegram_id)
    text = format_settings_card(user)
    markup = copy_settings_inline_keyboard()
    if edit:
        await edit_or_reply(update, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def apply_settings_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    settings = _settings(context)
    telegram_id = update.effective_user.id if update.effective_user else 0
    if data == CB_SET_CONSERVATIVE:
        user = set_profile(settings, telegram_id, "Conservative")
    elif data == CB_SET_BALANCED:
        user = set_profile(settings, telegram_id, "Balanced")
    elif data == CB_SET_EXPLORATORY:
        user = set_profile(settings, telegram_id, "Exploratory")
    elif data == CB_SET_ALERT_FOLLOW:
        user = update_setting(settings, telegram_id, "alert_decisions", "FOLLOW_ONLY")
    else:
        user = update_setting(settings, telegram_id, "alert_decisions", "FOLLOW_AND_WATCH")
    await edit_or_reply(
        update,
        format_settings_card(user),
        reply_markup=copy_settings_inline_keyboard(),
    )


async def _ensure_discovery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cached = context.user_data.get(_DISCOVERY_CACHE_KEY)
    if cached:
        return cached
    settings = _settings(context)
    assert_read_only(settings)
    await edit_or_reply(update, "Searching traders…", reply_markup=discovery_inline_keyboard())
    telegram_id = update.effective_user.id if update.effective_user else 0
    _candidates, scores = run_discovery(settings, telegram_id=telegram_id, analyze_limit=10)
    context.user_data[_DISCOVERY_CACHE_KEY] = scores
    context.user_data["copy_discovery_candidates"] = len(_candidates)
    return scores


async def show_discovery_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    title: str,
    decision: str | None = None,
    rising_only: bool = False,
    high_risk_only: bool = False,
) -> None:
    try:
        scores = await _ensure_discovery(update, context)
    except Exception as exc:
        logger.exception("Discovery failed")
        await edit_or_reply(
            update,
            f"Discovery failed: {exc}",
            reply_markup=discovery_inline_keyboard(),
        )
        return
    filtered = filter_scores(
        scores,
        decision=decision,
        rising_only=rising_only,
        high_risk_only=high_risk_only,
    )
    if not filtered:
        filtered = scores[:5]
    else:
        filtered = filtered[:8]
    context.user_data[_DISCOVERY_VIEW_KEY] = filtered
    context.user_data[_DISCOVERY_INDEX_KEY] = 0
    context.user_data["copy_discovery_title"] = title
    await _render_discovery_card(update, context)


async def _render_discovery_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    scores = context.user_data.get(_DISCOVERY_VIEW_KEY) or []
    index = int(context.user_data.get(_DISCOVERY_INDEX_KEY) or 0)
    title = context.user_data.get("copy_discovery_title") or "Traders"
    if not scores:
        await edit_or_reply(
            update,
            f"{title}\nNo traders match this filter.",
            reply_markup=discovery_inline_keyboard(),
        )
        return
    index = max(0, min(index, len(scores) - 1))
    context.user_data[_DISCOVERY_INDEX_KEY] = index
    score = scores[index]
    text = f"{title} ({index + 1}/{len(scores)})\n\n{format_trader_card(score)}"
    await edit_or_reply(
        update,
        text,
        reply_markup=trader_actions_keyboard(
            score.wallet,
            index=index,
            total=len(scores),
            profile_url=score.metrics.profile_url,
        ),
    )


async def discovery_nav(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    if data == CB_DISC_BACK:
        await edit_or_reply(update, DISCOVERY_MENU_TEXT, reply_markup=discovery_inline_keyboard())
        return
    index = int(context.user_data.get(_DISCOVERY_INDEX_KEY) or 0)
    if data == CB_DISC_PREV:
        context.user_data[_DISCOVERY_INDEX_KEY] = max(0, index - 1)
    elif data == CB_DISC_NEXT:
        scores = context.user_data.get(_DISCOVERY_VIEW_KEY) or []
        context.user_data[_DISCOVERY_INDEX_KEY] = min(len(scores) - 1, index + 1)
    await _render_discovery_card(update, context)


async def add_watchlist_from_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    wallet: str,
) -> None:
    settings = _settings(context)
    assert_read_only(settings)
    scores = context.user_data.get(_DISCOVERY_VIEW_KEY) or []
    label = None
    for score in scores:
        if score.wallet == wallet.lower() or score.wallet == wallet:
            label = score.label
            wallet = score.wallet
            break
    user_id = update.effective_user.id if update.effective_user else None
    try:
        add_wallet(settings, wallet, label=label, added_by=user_id)
        await edit_or_reply(
            update,
            f"Added to watchlist: {label or wallet[:10]}",
            reply_markup=copy_inline_keyboard(),
        )
    except ValueError as exc:
        await edit_or_reply(update, str(exc), reply_markup=discovery_inline_keyboard())


# Legacy reply-keyboard handler kept as no-op redirect for tests importing the symbol.
async def copy_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await copy_command(update, context)
