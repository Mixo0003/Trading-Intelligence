import re

from app.ev.calculations import calculate_binary_ev


def select_best_outcome(model_probability: float, yes_price: float | None) -> tuple[str, float | None]:
    if yes_price is None:
        return "N/A", None

    no_price = 1.0 - yes_price
    model_no = 1.0 - model_probability

    try:
        ev_yes = calculate_binary_ev(model_probability, yes_price)
        ev_no = calculate_binary_ev(model_no, no_price)
    except Exception:
        return "Yes", None

    if ev_no > ev_yes:
        return "No", ev_no
    return "Yes", ev_yes


def is_bracket_market(text: str) -> bool:
    lower = (text or "").lower()
    if "temperature range" in lower:
        return True
    return bool(re.search(r"\bbe\s+\d+\s*°?\s*c\b", lower))
