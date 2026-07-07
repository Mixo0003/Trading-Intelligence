import hashlib
from pathlib import Path

from app.signals.generator import WeatherSignal


DEDUP_FILE = Path("data/sent_signals.txt")


def signal_fingerprint(signal: WeatherSignal) -> str:
    raw = "|".join([
        signal.decision,
        f"{signal.probability:.4f}",
        f"{signal.confidence_score:.4f}",
        signal.message,
    ])

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def was_signal_sent(signal: WeatherSignal) -> bool:
    DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)

    fingerprint = signal_fingerprint(signal)

    if not DEDUP_FILE.exists():
        return False

    sent = {
        line.strip()
        for line in DEDUP_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }

    return fingerprint in sent


def mark_signal_sent(signal: WeatherSignal) -> None:
    DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)

    fingerprint = signal_fingerprint(signal)

    with DEDUP_FILE.open("a", encoding="utf-8") as file:
        file.write(fingerprint + "\n")
