from app.ev.types import ConfidenceLevel


def confidence_label(score: float) -> str:
    if score >= 0.80:
        return "HIGH"
    if score >= 0.55:
        return "MEDIUM"
    return "LOW"


def confidence_score_to_level(score: float) -> ConfidenceLevel:
    return ConfidenceLevel(confidence_label(score))


def calculate_basic_confidence(
    has_station: bool,
    has_resolution_source: bool,
    has_forecast: bool,
    has_threshold: bool,
) -> float:
    score = 0.0

    if has_station:
        score += 0.25
    if has_resolution_source:
        score += 0.25
    if has_forecast:
        score += 0.30
    if has_threshold:
        score += 0.20

    return min(score, 1.0)
