import logging

from app.config.logging_config import TelegramTokenRedactingFilter


def test_token_redacting_filter():
    filt = TelegramTokenRedactingFilter()
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="POST https://api.telegram.org/bot1234567890:ABCDEFghijklmnopQRSTUVwx/getMe",
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "1234567890:ABCDEFghijklmnopQRSTUVwx" not in record.msg
    assert "***REDACTED***" in record.msg
