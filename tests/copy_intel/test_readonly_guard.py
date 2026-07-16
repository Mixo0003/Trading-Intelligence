import pathlib

import pytest

from app.http.retry import is_permanent_status, is_retryable_status


class TestReadOnlyRepositoryGuard:
    def test_no_execution_symbols_in_app(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        forbidden = ["place_order", "sign_transaction", "private_key", "wallet.sign"]
        hits = []
        for path in root.glob("app/**/*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.relative_to(root)}:{token}")
        assert hits == []


class TestHttpRetryBehavior:
    def test_429_retryable(self):
        assert is_retryable_status(429) is True

    def test_400_permanent(self):
        assert is_permanent_status(400) is True

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_4xx_not_retried_by_policy(self, status):
        assert is_permanent_status(status) is True
