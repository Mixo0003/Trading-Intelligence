from app.signals.price_validation import (
    extreme_edge_requires_validation,
    format_market_price_display,
    validate_actionable_price,
)


class TestPriceValidation:
    def test_missing_price_produces_skip(self):
        valid, code = validate_actionable_price(None)
        assert valid is False
        assert code == "missing"

    def test_invalid_price_produces_skip(self):
        valid, code = validate_actionable_price(1.5)
        assert valid is False
        assert code == "out_of_range"

    def test_zero_price_invalid(self):
        valid, code = validate_actionable_price(0.0)
        assert valid is False

    def test_tiny_nonzero_price_not_zero_percent(self):
        assert format_market_price_display(0.0015) == "0.15%"
        assert format_market_price_display(0.0575) == "5.8%"
        assert format_market_price_display(0.014) == "1.4%"

    def test_valid_price_in_range(self):
        valid, code = validate_actionable_price(0.0575)
        assert valid is True
        assert code is None

    def test_extreme_edge_triggers_validation(self):
        assert extreme_edge_requires_validation(0.62, 0.001) is True
        assert extreme_edge_requires_validation(0.10, 0.40) is False

    def test_legitimate_low_price_remains_valid(self):
        valid, _ = validate_actionable_price(0.0015)
        assert valid is True
        assert format_market_price_display(0.0015) != "0%"
