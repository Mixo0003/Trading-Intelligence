"""Ukrainian diagnostic message builders for admin health Telegram/UI."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config.settings import Settings
from app.config.timezones import load_timezone
from app.health.incident_keys import correlation_code
from app.health.models import ComponentStatus
from app.health.registry import (
    SAFE_GENERIC_LABEL_UK,
    STATUS_LABEL_UK,
    get_component_spec,
    label_uk_for,
    product_area_uk_for,
)
from app.health.sanitize import assert_no_private_path, redact_string, sanitize_mapping

# Explicit allowlist for interpolated evidence snippets in Telegram/UI.
_EVIDENCE_KEYS = frozenset(
    {
        "book_failure_ratio",
        "books_valid",
        "books_failed",
        "history_failure_ratio",
        "baselines_incomplete",
        "watchlist",
        "providers_ok",
        "providers_required",
        "delivery_pending",
        "delivery_retry",
        "delivery_dead_letter",
        "cycle_age_seconds",
        "skipped_overlaps",
        "storage_level",
        "disk_free_percent",
        "consecutive_failures",
        "failure_threshold",
        "markets_discovered",
        "outcomes_shortlisted",
        "observations_eligible",
    }
)


def _fmt_admin_time(settings: Settings, iso_ts: str | None) -> str:
    if not iso_ts:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz = load_timezone(settings.report_timezone)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _safe_evidence(metrics: dict[str, Any] | None) -> str:
    clean = sanitize_mapping(metrics)
    parts: list[str] = []
    for key in sorted(_EVIDENCE_KEYS):
        if key not in clean:
            continue
        value = clean[key]
        if value is None or value == "":
            continue
        text = redact_string(str(value))
        if not assert_no_private_path(text):
            continue
        if any(tok in text.lower() for tok in ("traceback", "exception", "token", "secret")):
            continue
        parts.append(f"{key}={text}")
        if len(parts) >= 4:
            break
    return ", ".join(parts) if parts else "немає додаткових метрик"


def _verified_reason_uk(comp: ComponentStatus, reason_code: str) -> str:
    detail = redact_string(str(comp.detail or "")).strip()
    if not assert_no_private_path(detail):
        detail = ""
    # Strip accidental internal ids from detail lines.
    if comp.component and comp.component in detail:
        detail = detail.replace(comp.component, "").strip(" :-—")
    metrics = comp.metrics or {}
    if comp.component == "market_opportunity":
        price = metrics.get("wti_price") if isinstance(metrics.get("wti_price"), dict) else {}
        price_detail = redact_string(str(price.get("detail") or ""))
        if reason_code == "eia_price_down":
            return price_detail or "Первинне джерело EIA RWTC недоступне або застаріле"
        if reason_code in {"fred_optional", "warming"}:
            return detail or "Опційне джерело/прогрів; основна перевірка ще м’яка"
    if reason_code == "zero_valid_books":
        return "Після спроб читання немає жодної валідної executable книги ордерів"
    if reason_code == "reduced_book_coverage":
        ratio = metrics.get("book_failure_ratio")
        if isinstance(ratio, (int, float)):
            return f"Зменшене покриття книг ордерів (частка збоїв {ratio:.0%})"
        return "Зменшене, але ще придатне покриття книг ордерів"
    if reason_code == "readonly_violation":
        return "Порушено інваріант режиму лише читання"
    if reason_code == "poller_conflict":
        return "Виявлено конфлікт Telegram getUpdates / дубль poller у процесі"
    if detail:
        return detail
    return f"Підтверджена причина: {reason_code}"


def contains_forbidden_telegram_text(text: str, *, component_id: str | None = None) -> bool:
    lowered = text.lower()
    if "traceback" in lowered or "exception:" in lowered:
        return True
    if component_id and component_id in text:
        return True
    if not assert_no_private_path(text):
        return True
    return False


def format_hard_event_alert(
    settings: Settings,
    *,
    component_id: str,
    reason_code: str,
    what_happened_uk: str,
    consecutive_failures: int | None = None,
    incident_id: int = 0,
    opened_at: str | None = None,
    severity_uk: str | None = None,
) -> str:
    """Structured Ukrainian hard-failure alert for any registered component."""
    spec = get_component_spec(component_id)
    label = label_uk_for(component_id)
    area = product_area_uk_for(component_id)
    if severity_uk is None:
        severity_uk = (
            "Критично"
            if (spec and spec.criticality == "hard_critical")
            else "Важливо"
        )
    impact = spec.impact_down_uk if spec else "Користувацька можливість недоступна"
    fail_safe = spec.fail_safe_uk if spec else "Система лишається fail-closed"
    action = (
        spec.admin_action_down_uk if spec else "Перевірте System Health як адміністратор"
    )
    threshold = settings.health_failure_threshold
    failures = (
        consecutive_failures
        if consecutive_failures is not None
        else threshold
    )
    code = correlation_code(incident_id=incident_id or 0, reason_code=reason_code)
    reason = redact_string(what_happened_uk)
    if not assert_no_private_path(reason) or contains_forbidden_telegram_text(
        reason, component_id=component_id
    ):
        reason = "Підтверджений збій можливості (деталі лише в журналах)"
    lines = [
        f"⚠ {label}",
        f"Серйозність: {severity_uk}",
        f"Зона: {area}",
        f"Що сталося: {reason}",
        f"Вплив: {impact}",
        f"Безпека: {fail_safe}",
        f"Підтверджено: {failures}/{threshold}",
        f"Початок: {_fmt_admin_time(settings, opened_at)}",
        f"Дія: {action}",
        f"Код: {code}",
    ]
    text = "\n".join(lines)
    if contains_forbidden_telegram_text(text, component_id=component_id):
        return "\n".join(
            [
                f"⚠ {SAFE_GENERIC_LABEL_UK}",
                f"Серйозність: {severity_uk}",
                f"Зона: {area}",
                "Що сталося: Підтверджений збій можливості",
                f"Вплив: {impact}",
                f"Безпека: {fail_safe}",
                f"Підтверджено: {failures}/{threshold}",
                f"Початок: {_fmt_admin_time(settings, opened_at)}",
                f"Дія: {action}",
                f"Код: {code}",
            ]
        )
    return text


def format_down_alert(
    settings: Settings,
    comp: ComponentStatus,
    *,
    reason_code: str,
    consecutive_failures: int,
    incident_id: int,
    opened_at: str | None,
) -> str:
    return format_hard_event_alert(
        settings,
        component_id=comp.component,
        reason_code=reason_code,
        what_happened_uk=_verified_reason_uk(comp, reason_code),
        consecutive_failures=consecutive_failures,
        incident_id=incident_id,
        opened_at=opened_at,
    )


def format_recovery_alert(
    settings: Settings,
    comp: ComponentStatus,
    *,
    reason_code: str,
    incident_id: int,
    opened_at: str | None,
    resolved_at: str | None,
    backlog_remaining: bool = False,
) -> str:
    label = label_uk_for(comp.component)
    spec = get_component_spec(comp.component)
    capability = spec.capability_uk if spec else label
    duration = "—"
    try:
        if opened_at and resolved_at:
            start = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            seconds = max(0, int((end - start).total_seconds()))
            minutes = seconds // 60
            duration = f"{minutes} хв" if minutes else f"{seconds} с"
    except ValueError:
        duration = "—"
    backlog = "так" if backlog_remaining else "ні"
    code = correlation_code(incident_id=incident_id, reason_code=reason_code)
    text = "\n".join(
        [
            f"✅ Відновлення: {label}",
            f"Можливість: {capability}",
            "Функціональність знову доступна після підтверджених успішних перевірок.",
            f"Тривалість інциденту: {duration}",
            "Валідована обробка відновлена: так",
            f"Залишок черги/backlog: {backlog}",
            f"Код: {code}",
        ]
    )
    if contains_forbidden_telegram_text(text, component_id=comp.component):
        return "\n".join(
            [
                f"✅ Відновлення: {SAFE_GENERIC_LABEL_UK}",
                "Валідована обробка відновлена: так",
                f"Тривалість інциденту: {duration}",
                f"Код: {code}",
            ]
        )
    return text


def format_component_ui_row(comp: ComponentStatus, *, last_success_at: str | None = None) -> str:
    label = label_uk_for(comp.component)
    status_uk = STATUS_LABEL_UK.get(comp.status, comp.status)
    reason = _verified_reason_uk(comp, reason_code_guess(comp))
    evidence = _safe_evidence(comp.metrics)
    spec = get_component_spec(comp.component)
    lines = [
        f"• {label}: {status_uk}",
        f"  Причина: {reason}",
    ]
    if last_success_at:
        lines.append(f"  Остання успішна перевірка: {last_success_at}")
    elif comp.checked_at:
        lines.append(f"  Перевірено: {comp.checked_at}")
    if comp.status == "DEGRADED":
        impact = spec.impact_degraded_uk if spec else "Можливість обмежена"
        action = spec.admin_action_degraded_uk if spec else "Втручання не потрібне"
        still = "Основна можливість лишається доступною в обмеженому режимі"
        suppressed = spec.fail_safe_uk if spec else "Небезпечні дії заблоковано"
        lines.extend(
            [
                f"  Зменшено: {impact}",
                f"  Доказ: {evidence}",
                f"  Що працює: {still}",
                f"  Що придушено: {suppressed}",
                f"  Втручання: {action}",
            ]
        )
    elif comp.status == "DOWN":
        impact = spec.impact_down_uk if spec else "Можливість недоступна"
        lines.append(f"  Вплив: {impact}")
    return "\n".join(lines)


def reason_code_guess(comp: ComponentStatus) -> str:
    from app.health.policy import reason_code_for

    return reason_code_for(comp)


def format_grouped_summary(overall, *, groups_order: tuple[str, ...]) -> str:
    from collections import defaultdict

    from app.health.registry import STATUS_LABEL_UK, ui_group_for

    lines = [
        "📊 Стан системи",
        f"Загалом: {STATUS_LABEL_UK.get(overall.status, overall.status)}",
        overall.summary,
        f"Перевірено: {overall.checked_at}",
        "",
    ]
    by_group: dict[str, list[ComponentStatus]] = defaultdict(list)
    for comp in overall.components:
        if comp.status == "DISABLED":
            continue
        by_group[ui_group_for(comp.component)].append(comp)
    for group in groups_order:
        rows = by_group.get(group) or []
        if not rows:
            continue
        lines.append(f"— {group} —")
        for comp in rows:
            lines.append(format_component_ui_row(comp))
        lines.append("")
    # Any leftover groups not in order
    for group, rows in by_group.items():
        if group in groups_order:
            continue
        lines.append(f"— {group} —")
        for comp in rows:
            lines.append(format_component_ui_row(comp))
        lines.append("")
    return "\n".join(lines).rstrip()
