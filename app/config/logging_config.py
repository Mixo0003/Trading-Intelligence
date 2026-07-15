import logging
import re


_TOKEN_IN_URL = re.compile(r"(/bot)([^/\s]+)(/)")


class TelegramTokenRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_URL.sub(r"\1***REDACTED***\3", record.msg)
        if record.args:
            record.args = tuple(
                _TOKEN_IN_URL.sub(r"\1***REDACTED***\3", arg)
                if isinstance(arg, str)
                else arg
                for arg in record.args
            )
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        )
    token_filter = TelegramTokenRedactingFilter()
    for handler in root.handlers:
        handler.addFilter(token_filter)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
