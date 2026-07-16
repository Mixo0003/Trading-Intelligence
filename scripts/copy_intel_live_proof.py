"""Bounded live read-only proof for copy-trading intelligence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.copy_intel.analytics import compute_trader_metrics
from app.copy_intel.data_client import CopyDataClient, normalize_wallet
from app.copy_intel.ingestion import ingest_wallet
from app.copy_intel.recommendation import build_recommendation
from app.copy_intel.scoring import score_trader
from app.database.storage import initialize_database
from app.telegram.copy_intel.formatting import format_recommendation_card

FIXTURE_WALLET = "0xee00ba338c59557141789b127927a55f5cc5cea1"


def _offline_settings(db_path: Path) -> Settings:
    from tests.conftest import make_test_settings

    return make_test_settings(
        database_path=str(db_path),
        copy_intel_enabled=True,
        copy_intel_read_only=True,
        copy_min_resolved_trades=5,
    )


def main() -> int:
    db_path = Path("data/copy_intel_proof.db")
    settings = _offline_settings(db_path)
    initialize_database(settings)

    wallet = normalize_wallet(FIXTURE_WALLET)
    client = CopyDataClient(settings)

    print("=== Live Data API probe ===")
    positions = client.fetch_positions(wallet, limit=3)
    trades = client.fetch_trades(wallet, limit=3)
    print(f"positions={len(positions)} trades={len(trades)}")
    if not positions:
        print("NO-GO: no positions returned")
        return 1

    print("=== Ingestion ===")
    events = ingest_wallet(settings, wallet, client=client)
    print(f"new_events={len(events)} types={[e.event_type for e in events]}")

    print("=== Analytics + scoring ===")
    metrics = compute_trader_metrics(settings, wallet, client=client)
    score = score_trader(metrics)
    print(
        json.dumps(
            {
                "trade_count": metrics.trade_count,
                "resolved_count": metrics.resolved_count,
                "win_rate": round(metrics.win_rate, 3),
                "eligible": metrics.eligible,
                "score": score,
            },
            indent=2,
        )
    )

    if events:
        rec = build_recommendation(settings, events[0], trader_label="S-Works", client=client)
        card = format_recommendation_card(rec)
        print("=== Sample alert card ===")
        print(card)
    else:
        print("No new trade events on first poll (expected after baseline snapshot).")

    print("GO: read-only copy intel path verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
