#!/usr/bin/env python3
"""Aggregate-only diagnostics for Phase 3B direct shadow intake."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from challenge_shadow_common import ro_connection


def diagnostics(path: Path) -> dict[str, object]:
    conn = ro_connection(path)
    try:
        outcomes = {str(r[0]): int(r[1]) for r in conn.execute(
            "SELECT outcome,COUNT(*) FROM challenge_intake_request_audit GROUP BY outcome"
        )}
        states = {str(r[0]): int(r[1]) for r in conn.execute(
            """SELECT reconciliation_state,COUNT(*)
                 FROM (
                     SELECT r.reconciliation_state
                       FROM challenge_observation_reconciliation r
                      WHERE r.direct_submission_id IS NOT NULL
                     UNION ALL
                     SELECT 'GOOGLE_ONLY_NEW'
                       FROM challenge_observation_reconciliation r
                       JOIN challenge_submissions s ON s.submission_id=r.google_submission_id
                      WHERE r.reconciliation_state='GOOGLE_ONLY_NEW'
                        AND r.direct_submission_id IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM challenge_submissions newer
                             WHERE newer.supersedes_submission_id=s.submission_id
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM challenge_observation_reconciliation_links l
                             WHERE l.google_submission_id=s.submission_id
                        )
                 ) current_states
                GROUP BY reconciliation_state"""
        )}
        delays = conn.execute(
            """SELECT AVG((julianday(g.first_observed_at)-julianday(d.first_observed_at))*86400.0),
                      COUNT(*)
                 FROM challenge_observation_reconciliation_links r
                 JOIN challenge_submissions d ON d.submission_id=r.direct_submission_id
                 JOIN challenge_submissions g ON g.submission_id=r.google_submission_id
                WHERE r.match_class='MATCH'
                  AND julianday(g.first_observed_at)>=julianday(d.first_observed_at)"""
        ).fetchone()
        ordered_delays = [float(r[0]) for r in conn.execute(
            """SELECT (julianday(g.first_observed_at)-julianday(d.first_observed_at))*86400.0 delay
                 FROM challenge_observation_reconciliation_links r
                 JOIN challenge_submissions d ON d.submission_id=r.direct_submission_id
                 JOIN challenge_submissions g ON g.submission_id=r.google_submission_id
                WHERE r.match_class='MATCH'
                  AND julianday(g.first_observed_at)>=julianday(d.first_observed_at)
                ORDER BY delay"""
        )]
        median = None
        if ordered_delays:
            mid = len(ordered_delays)//2
            median = ordered_delays[mid] if len(ordered_delays)%2 else (ordered_delays[mid-1]+ordered_delays[mid])/2
        return {
            "award_mode": conn.execute("SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'").fetchone()[0],
            "direct_intake_mode": conn.execute("SELECT setting_value FROM challenge_settings WHERE setting_key='direct_intake_mode'").fetchone()[0],
            "direct_approvals_received": int(conn.execute("SELECT COUNT(*) FROM challenge_direct_intake_metadata").fetchone()[0]),
            "reconciliation_links": int(conn.execute(
                "SELECT COUNT(*) FROM challenge_observation_reconciliation_links"
            ).fetchone()[0]),
            "direct_duplicate_deliveries_blocked": outcomes.get("idempotent",0)+outcomes.get("duplicate_direct",0),
            "authentication_failures": outcomes.get("auth_failure",0),
            "validation_failures": outcomes.get("validation_failure",0)+outcomes.get("conflict",0),
            "reconciliation_states": states,
            "direct_awaiting_google": states.get("DIRECT_WAITING_FOR_GOOGLE",0),
            "matched": states.get("MATCH",0),
            "mismatches": {key: states.get(key,0) for key in (
                "GOOGLE_VALUE_DIFFERENCE","IDENTITY_DIFFERENCE","TIER_DIFFERENCE","PARTY_DIFFERENCE"
            )},
            "google_only_new": states.get("GOOGLE_ONLY_NEW",0),
            "average_google_mirror_delay_seconds": float(delays[0]) if delays[0] is not None else None,
            "median_google_mirror_delay_seconds": median,
            "identity_resolution_failures": int(conn.execute(
                """SELECT COUNT(*) FROM challenge_submission_participants p
                   JOIN challenge_submissions s ON s.submission_id=p.submission_id
                  WHERE s.source_system='discord_direct' AND p.member_id IS NULL"""
            ).fetchone()[0]),
            "manual_review_required_awards": int(conn.execute(
                "SELECT COUNT(*) FROM challenge_award_reviews WHERE review_state='manual_review_required'"
            ).fetchone()[0]),
            "legacy_baseline_rows": int(conn.execute("SELECT COUNT(*) FROM challenge_legacy_baselines").fetchone()[0]),
            "queued": int(conn.execute("SELECT COUNT(*) FROM challenge_tier_awards WHERE award_state='queued'").fetchone()[0]),
            "delivered": int(conn.execute("SELECT COUNT(*) FROM challenge_tier_awards WHERE award_state='delivered'").fetchone()[0]),
            "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        conn.close()


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--challenges-db",type=Path,default=Path("/srv/projects/database/Challenges.db"))
    args=p.parse_args()
    print(json.dumps(diagnostics(args.challenges_db),indent=2,sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
