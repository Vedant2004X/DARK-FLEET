"""Standard-library tests for DARK-FLEET v0.9 analytics."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import v09_analytics as analytics


class V09AnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.global_db = self.root / "global.sqlite3"
        self.phase3_db = self.root / "phase3.sqlite3"
        self.recent_db = self.root / "recent.sqlite3"
        self._make_global_db()
        self._make_phase3_db()
        self._make_recent_db()
        self.patches = [
            patch.object(analytics, "GLOBAL_DB", self.global_db),
            patch.object(analytics, "PHASE3_DB", self.phase3_db),
            patch.object(analytics, "RECENT_GRID_DB", self.recent_db),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_global_db(self) -> None:
        with closing(sqlite3.connect(self.global_db)) as conn:
            conn.execute("""CREATE TABLE observations (
                id INTEGER PRIMARY KEY, date_day TEXT NOT NULL, date TEXT, mmsi TEXT,
                lat REAL, lon REAL, shipName TEXT, vesselType TEXT,
                speed_knots TEXT, speed_change_knots TEXT, time_gap_hours TEXT,
                anomaly_score TEXT, anomaly_flag TEXT, anomaly_status TEXT,
                region_iso3 TEXT, region_id TEXT, region_label TEXT, source_dataset TEXT
            )""")
            rows = [
                (1, "2022-01-01", "2022-01-01T01:00:00Z", "111", 1, 2, "A", "CARGO", "10", "1", "1", "-0.2", "1", "Normal baseline", "IND", "1", "India EEZ", "test"),
                (2, "2022-01-01", "2022-01-01T02:00:00Z", "222", 2, 3, "B", "CARGO", "11", "2", "2", "0.4", "-1", "Flagged for review", "IND", "1", "India EEZ", "test"),
                (3, "2022-01-02", "2022-01-02T01:00:00Z", "333", 3, 4, "C", "FISHING", "5", "0.5", "3", "0.1", "-1", "Flagged for review", "CHL", "2", "Chile EEZ", "test"),
            ]
            conn.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()

    def _make_phase3_db(self) -> None:
        with closing(sqlite3.connect(self.phase3_db)) as conn:
            conn.execute("""CREATE TABLE candidates (
                sar_record_id TEXT, candidate_rank INTEGER, candidate_mmsi TEXT,
                distance_km REAL, time_gap_min REAL, ranking_score REAL, row_json TEXT
            )""")
            conn.executemany(
                "INSERT INTO candidates VALUES (?,?,?,?,?,?,?)",
                [("sar1", 1, "111", 5.0, 20.0, 0.8, "{}"), ("sar1", 2, "222", 9.0, 40.0, 0.5, "{}")],
            )
            conn.commit()

    def _make_recent_db(self) -> None:
        with closing(sqlite3.connect(self.recent_db)) as conn:
            conn.execute("""CREATE TABLE recent_grid_records (
                kind TEXT, day TEXT, region_iso3 TEXT
            )""")
            conn.executemany(
                "INSERT INTO recent_grid_records VALUES (?,?,?)",
                [("ais", "2026-10-27", "IND"), ("ais", "2026-10-28", "CHL"), ("sar", "2026-10-28", "IND")],
            )
            conn.commit()

    def test_overview_date_filter(self) -> None:
        result = analytics.v09_overview(start_date=analytics.date(2022, 1, 1), end_date=analytics.date(2022, 1, 1))
        self.assertEqual(result["total_observations"], 2)
        self.assertEqual(result["flagged_observations"], 1)
        self.assertEqual(result["unique_vessels"], 2)

    def test_review_queue_orders_saved_scores(self) -> None:
        result = analytics.v09_review_queue(start_date=None, end_date=None, region_iso3=None, limit=10, offset=0, search="")
        self.assertEqual(result["total_matching"], 2)
        self.assertEqual(result["observations"][0]["mmsi"], "222")

    def test_explanation_uses_cohort_or_global_baseline(self) -> None:
        result = analytics.v09_explain_observation(2)
        self.assertEqual(result["observation"]["mmsi"], "222")
        self.assertEqual(result["comparison_cohort"]["basis"], "all_vessels")
        self.assertEqual(len(result["feature_deviations"]), 3)
        self.assertIn("not SHAP", result["methodology_note"])

    def test_association_summary_does_not_claim_confirmed_matches(self) -> None:
        result = analytics.v09_association_overview()
        self.assertEqual(result["candidate_rows"], 2)
        self.assertIn("not identity proof", result["interpretation"])

    def test_status_reports_recent_grid_as_identity_unavailable(self) -> None:
        result = analytics.v09_status()
        self.assertTrue(result["historical_anomaly_index"]["ready"])
        self.assertTrue(result["recent_grid"]["ready"])
        self.assertFalse(result["recent_grid"]["identity_available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
