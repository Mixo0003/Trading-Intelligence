"""Ukrainian Live Edge Telegram cards. Market titles stay in original language."""

from __future__ import annotations

import json
from decimal import Decimal

from app.opportunity_intel.live_edge.models import (
    DECISION_LABELS_UK,
    LiveEdgeDecision,
    LiveEdgeDecisionResult,
)
from app.telegram.opportunity_intel.formatting import (
    market_title_plain,
    sanitize_plain,
)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
POLYMARKET_OPEN_LABEL = "🔗 Відкрити на Polymarket"


def resolve_polymarket_link(
    *,
    event_slug: str | None = None,
    market_slug: str | None = None,
) -> str | None:
    """Build a public Polymarket URL from Gamma slugs only. Never guess from title."""
    from app.polymarket.links import build_validated_market_link

    return build_validated_market_link(event_slug) or build_validated_market_link(
        market_slug
    )


def polymarket_open_lines(
    *,
    event_slug: str | None = None,
    market_slug: str | None = None,
    payload: dict | None = None,
) -> list[str]:
    source = payload if isinstance(payload, dict) else {}
    link = resolve_polymarket_link(
        event_slug=event_slug if event_slug is not None else source.get("event_slug"),
        market_slug=market_slug if market_slug is not None else source.get("market_slug"),
    )
    if link is None:
        return []
    return [POLYMARKET_OPEN_LABEL, link]


def _card_payload_dict(row) -> dict:
    raw = getattr(row, "card_json", "") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_CATEGORY_CONTEXT = {
    "soccer": "футбол",
    "football": "футбол",
    "baseball": "бейсбол",
    "basketball": "баскетбол",
    "tennis": "теніс",
    "esports_lol": "LoL",
    "esports_cs": "CS2",
    "esports_dota": "Dota",
    "esports": "esports",
}

_CATEGORY_EMOJI = {
    "soccer": "⚽",
    "football": "⚽",
    "baseball": "⚾",
    "basketball": "🏀",
    "tennis": "🎾",
    "esports_lol": "🎮",
    "esports_cs": "🎮",
    "esports_dota": "🎮",
    "esports": "🎮",
}


def format_live_edge_card(result: LiveEdgeDecisionResult) -> str:
    evaluation = result.evaluation
    quote = result.quote
    title = market_title_plain(evaluation.market.question, maximum=400)
    live_state = _live_score_text(evaluation.live_state)
    price = str(quote.best_ask) if quote is not None else None
    context = _context_line(
        category=evaluation.identity.category.value,
        sport_label=evaluation.identity.sport_label,
    )
    link_lines = polymarket_open_lines(
        event_slug=evaluation.market.event_slug,
        market_slug=evaluation.market.market_slug,
    )
    if result.decision == LiveEdgeDecision.WATCH:
        return _finish(
            _watch_lines(
                title=title,
                context=context,
                live_state=live_state,
                price=price,
                reasons=result.decision_reasons,
                link_lines=link_lines,
            )
        )
    if result.decision == LiveEdgeDecision.WAIT:
        trigger = (
            result.wait_trigger.description_uk
            if result.wait_trigger is not None
            else None
        )
        return _finish(
            _wait_lines(
                title=title,
                live_state=live_state,
                price=price,
                trigger=trigger,
                link_lines=link_lines,
            )
        )
    if result.decision == LiveEdgeDecision.TOO_LATE:
        return _finish(
            _too_late_lines(title=title, price=price, link_lines=link_lines)
        )
    if result.decision == LiveEdgeDecision.SKIP:
        return _finish(["🔴 ПРОПУСТИТИ", title, *link_lines])
    reasons = result.decision_reasons or evaluation.reasons
    return _finish(
        _buy_lines(
            title=title,
            side=result.side,
            price=str(quote.vwap) if quote is not None else price,
            allocation=str(quote.allocation_usd) if quote is not None else None,
            slippage=_slippage_text(quote.slippage if quote is not None else None),
            reasons=reasons,
            link_lines=link_lines,
        )
    )


def format_wait_trigger_alert(result: LiveEdgeDecisionResult) -> str:
    title = market_title_plain(result.evaluation.market.question, maximum=400)
    trigger = (
        result.wait_trigger.description_uk if result.wait_trigger is not None else None
    )
    lines = ["🚨 УМОВА ДЛЯ ВХОДУ ВИКОНАНА", title]
    if trigger:
        lines.extend(["Зайти, якщо:", f"🎯 {sanitize_plain(trigger, maximum=240)}"])
    lines.extend(
        polymarket_open_lines(
            event_slug=result.evaluation.market.event_slug,
            market_slug=result.evaluation.market.market_slug,
        )
    )
    return _finish(lines)


def format_live_edge_history_block(rows: tuple[object, ...]) -> str:
    if not rows:
        return ""
    lines = ["🔥 Live Edge", ""]
    for row in rows:
        title = market_title_plain(getattr(row, "question", "Market"), maximum=120)
        decision = getattr(row, "decision", "")
        try:
            label = DECISION_LABELS_UK[LiveEdgeDecision(decision)]
        except ValueError:
            label = decision
        lines.append(f'"{title}"')
        lines.append(
            f"{label} | {getattr(row, 'signal_type', '')} | "
            f"first {getattr(row, 'first_detected_at', '')}"
        )
        if getattr(row, "missed", 0):
            lines.append(
                f"ПРОПУЩЕНА МОЖЛИВІСТЬ: {getattr(row, 'missed_reason', '')}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def card_payload_json(result: LiveEdgeDecisionResult) -> str:
    evaluation = result.evaluation
    state = evaluation.live_state
    provenance = evaluation.provenance
    quote = result.quote
    return json.dumps(
        {
            "question": evaluation.market.question,
            "decision": result.decision.value,
            "side": result.side,
            "signal_type": evaluation.signal_type.value,
            "category": evaluation.identity.category.value,
            "has_live_state": state is not None,
            "provider": provenance.provider if provenance is not None else None,
            "live_score": _live_score_text(state),
            "freshness_seconds": (
                int(evaluation.live_data_age_seconds)
                if evaluation.live_data_age_seconds is not None
                else None
            ),
            "watch_reasons": list(result.decision_reasons),
            "wait_trigger_uk": (
                result.wait_trigger.description_uk
                if result.wait_trigger is not None
                else None
            ),
            "sport_label": evaluation.identity.sport_label,
            "allocation_usd": (
                str(quote.allocation_usd) if quote is not None else None
            ),
            "slippage": (
                str(quote.slippage)
                if quote is not None and quote.slippage is not None
                else None
            ),
            "vwap": str(quote.vwap) if quote is not None else None,
            "match_status": evaluation.match.status.value,
            "best_ask": str(quote.best_ask) if quote is not None else None,
            "watch_fingerprint": _watch_fingerprint(evaluation, quote),
            "event_slug": evaluation.market.event_slug,
            "market_slug": evaluation.market.market_slug,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def format_watch_telegram(row) -> str:
    return format_decision_telegram(row)


def format_decision_telegram(row) -> str:
    payload = _card_payload_dict(row)
    title = market_title_plain(
        payload.get("question") or getattr(row, "question", ""),
        maximum=400,
    )
    decision = payload.get("decision") or getattr(row, "decision", "")
    live_state = payload.get("live_score")
    price = payload.get("best_ask") or getattr(row, "executable_price", None)
    price = str(price) if price not in {None, ""} else None
    link_lines = polymarket_open_lines(payload=payload)
    if decision == LiveEdgeDecision.WAIT.value or decision == "WAIT":
        return _finish(
            _wait_lines(
                title=title,
                live_state=live_state,
                price=price,
                trigger=_stored_wait_trigger(row, payload),
                link_lines=link_lines,
            )
        )
    if decision == LiveEdgeDecision.BUY_NOW.value or decision == "BUY_NOW":
        entry = payload.get("vwap") or price
        return _finish(
            _buy_lines(
                title=title,
                side=payload.get("side") or getattr(row, "side", None),
                price=str(entry) if entry not in {None, ""} else None,
                allocation=payload.get("allocation_usd"),
                slippage=_slippage_text(payload.get("slippage")),
                reasons=payload.get("watch_reasons") or (),
                link_lines=link_lines,
            )
        )
    if decision == LiveEdgeDecision.TOO_LATE.value or decision == "TOO_LATE":
        return _finish(_too_late_lines(title=title, price=price, link_lines=link_lines))
    return _finish(
        _watch_lines(
            title=title,
            context=_context_line(
                category=payload.get("category") or getattr(row, "category", ""),
                sport_label=payload.get("sport_label"),
            ),
            live_state=live_state,
            price=price,
            reasons=payload.get("watch_reasons") or (),
            link_lines=link_lines,
        )
    )


def _live_score_text(state) -> str | None:
    if state is None:
        return None
    parts: list[str] = []
    if state.score is not None:
        parts.append(f"{state.score[0]}–{state.score[1]}")
    if state.maps_won is not None:
        parts.append(f"maps {state.maps_won[0]}–{state.maps_won[1]}")
    if state.round_score is not None:
        parts.append(f"rounds {state.round_score[0]}–{state.round_score[1]}")
    if state.minute is not None:
        parts.append(f"{state.minute}'")
    if state.period:
        parts.append(str(state.period))
    if state.inning is not None:
        half = state.top_bottom or ""
        parts.append(f"inn {state.inning} {half}".strip())
    return " ".join(parts) if parts else state.status or None


def _watch_fingerprint(evaluation, quote) -> str:
    import hashlib

    state = evaluation.live_state
    parts = [
        evaluation.market.market_id,
        _live_score_text(state) or "",
        str(quote.best_ask) if quote is not None else "noquote",
        evaluation.match.status.value,
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def _watch_lines(
    *,
    title: str,
    context: str | None,
    live_state: str | None,
    price: str | None,
    reasons,
    link_lines: list[str],
) -> list[str]:
    lines = ["⚠️ LIVE WATCH"]
    if context:
        lines.append(context)
    if title:
        lines.append(title)
    if live_state:
        lines.append(f"Рахунок: {live_state}")
    if price:
        lines.append(f"💵 Ціна: {price}")
    lines.append("🧠 ВЕРДИКТ: ПОКИ СПОСТЕРІГАЄМО")
    cleaned = _clean_reasons(reasons)
    if cleaned:
        lines.append("Причина:")
        lines.append(f"✅ {cleaned[0]}")
        if len(cleaned) > 1:
            lines.append(f"⚠️ {cleaned[1]}")
    lines.extend(link_lines)
    return lines


def _wait_lines(
    *,
    title: str,
    live_state: str | None,
    price: str | None,
    trigger: str | None,
    link_lines: list[str],
) -> list[str]:
    lines = ["🟡 МОЖЛИВІСТЬ ПІД НАГЛЯДОМ"]
    if title:
        lines.append(title)
    if live_state:
        lines.append(f"Live: {live_state}")
    if price:
        lines.append(f"💵 Ціна: {price}")
    lines.append("🧠 ВЕРДИКТ: ЧЕКАТИ")
    if trigger:
        lines.extend(
            ["Зайти, якщо:", f"🎯 {_trigger_body(str(trigger))}"]
        )
    lines.extend(link_lines)
    return lines


def _buy_lines(
    *,
    title: str,
    side: str | None,
    price: str | None,
    allocation: str | None,
    slippage: str | None,
    reasons,
    link_lines: list[str],
) -> list[str]:
    lines = ["🚀 ГАРЯЧИЙ СИГНАЛ"]
    if title:
        lines.append(title)
    if side:
        lines.append(f"🎯 Ставка: {sanitize_plain(str(side), maximum=80)}")
    if price:
        lines.append(f"💵 Вхід: {price}")
    if allocation:
        lines.append(f"💰 Бюджет: ${allocation}")
    if slippage:
        lines.append(f"📉 Slippage: {slippage}")
    lines.append("🧠 ВЕРДИКТ: ЗАХОДИТИ")
    cleaned = _clean_reasons(reasons)[:3]
    if cleaned:
        lines.append("Чому:")
        for reason in cleaned:
            lines.append(f"✅ {reason}")
    lines.extend(link_lines)
    return lines


def _too_late_lines(
    *,
    title: str,
    price: str | None,
    link_lines: list[str],
) -> list[str]:
    lines = ["⚪ ВЖЕ ПІЗНО"]
    if title:
        lines.append(title)
    if price:
        lines.append(f"💵 Ціна: {price}")
    lines.extend(
        [
            "🧠 ВЕРДИКТ: НЕ ЗАХОДИТИ",
            "Причина:",
            "ринок уже врахував майже весь потенціал.",
        ]
    )
    lines.extend(link_lines)
    return lines


def _stored_wait_trigger(row, payload: dict) -> str | None:
    text = payload.get("wait_trigger_uk")
    if text:
        return str(text)
    raw = getattr(row, "wait_trigger_json", None) or ""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("description_uk")
    return str(value) if value else None


def _trigger_body(raw: str) -> str:
    text = sanitize_plain(raw, maximum=240).strip()
    for prefix in ("Зайти, якщо:", "Зайти, якщо"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text


def _context_line(*, category: str | None, sport_label: str | None) -> str | None:
    key = (category or "").strip().lower()
    emoji = _CATEGORY_EMOJI.get(key, "⚡")
    label = (sport_label or "").strip() or _CATEGORY_CONTEXT.get(key)
    if not label:
        return None
    return f"{emoji} {label}"


def _slippage_text(value) -> str | None:
    if value in {None, ""}:
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return sanitize_plain(str(value), maximum=40)
    scaled = (amount * Decimal("100")).quantize(Decimal("0.01"))
    return f"{scaled}%"


def _clean_reasons(reasons) -> list[str]:
    cleaned: list[str] = []
    for reason in reasons or ():
        text = sanitize_plain(str(reason), maximum=180).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _finish(lines: list[str]) -> str:
    text = "\n".join(line for line in lines if line is not None)
    if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
        return text[: TELEGRAM_MAX_MESSAGE_LENGTH - 3] + "..."
    return text
