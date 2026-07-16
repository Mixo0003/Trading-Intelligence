"""Bounded live read-only proof for V2 Phase 2 trader discovery."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.copy_intel.discovery import run_discovery
from app.copy_intel.risk import assert_read_only
from app.database.storage import initialize_database
from tests.conftest import make_test_settings


def main() -> int:
    db_path = Path("data/copy_intel_discovery_proof.db")
    settings = make_test_settings(
        database_path=str(db_path),
        copy_intel_enabled=True,
        copy_intel_read_only=True,
    )
    assert_read_only(settings)
    initialize_database(settings)

    print("=== Live discovery (public leaderboard + wallet history) ===")
    candidates, scores = run_discovery(settings, telegram_id=0, analyze_limit=6)
    rejected = [s for s in scores if s.rejected or s.decision == "AVOID"]
    follow = [s for s in scores if s.decision == "FOLLOW"]
    watch = [s for s in scores if s.decision == "WATCH"]

    print(f"candidates_discovered={len(candidates)}")
    print(f"analyzed={len(scores)} follow={len(follow)} watch={len(watch)} avoid/rejected={len(rejected)}")
    sample = []
    for s in scores[:5]:
        sample.append(
            {
                "label": s.label,
                "wallet_prefix": s.wallet[:8],
                "decision": s.decision,
                "final_score": s.final_score,
                "performance": s.performance_score,
                "risk": s.risk_score,
                "confidence": s.data_confidence,
                "resolved": s.metrics.resolved_count,
                "incomplete": list(s.metrics.incomplete_metrics)[:4],
            }
        )
    print(json.dumps(sample, indent=2))
    if len(candidates) < 2:
        print("NO-GO: insufficient candidates from public leaderboard")
        return 1
    print("GO: discovery path verified (read-only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
