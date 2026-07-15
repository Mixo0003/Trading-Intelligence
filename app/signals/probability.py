def estimate_probability_from_temperature_edge(
    forecast_temperature_f: float,
    threshold_f: float,
    *,
    below_threshold: bool = False,
) -> float:
    edge_f = forecast_temperature_f - threshold_f
    if below_threshold:
        edge_f = -edge_f

    if edge_f >= 5:
        return 0.85
    if edge_f >= 3:
        return 0.75
    if edge_f >= 1:
        return 0.62
    if edge_f >= 0:
        return 0.55
    if edge_f >= -1:
        return 0.45
    if edge_f >= -3:
        return 0.30

    return 0.18
