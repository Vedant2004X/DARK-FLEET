from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "backend" / "worker" / "grid_ingestion_worker.py"
APP_PATH = ROOT / "backend" / "app" / "main.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = load_module("df_worker_test", WORKER_PATH)
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.worker.RECENT_DIR = base / "recent_grid"
        self.worker.DB_PATH = self.worker.RECENT_DIR / "recent_grid_local_index.sqlite3"
        self.worker.AIS_MASTER = self.worker.RECENT_DIR / "recent_grid_ais_presence.csv"
        self.worker.SAR_MASTER = self.worker.RECENT_DIR / "recent_grid_sar_presence.csv"
        self.worker.RUN_JSON = self.worker.RECENT_DIR / "last_grid_ingestion_run.json"
        self.worker.MANIFEST = self.worker.RECENT_DIR / "grid_worker_manifest.csv"
        self.worker.PARTS_DIR = self.worker.RECENT_DIR / "parts"

    def tearDown(self):
        self.temp.cleanup()

    def test_backfill_plan_is_1160_monthly_reports(self):
        tasks = self.worker.build_backfill_tasks("2022-01-01", "2026-10-29")
        self.assertEqual(len(tasks), 1160)
        self.assertEqual(tasks[0][0], "ais")
        self.assertEqual(tasks[0][1]["iso3"], "IDN")
        self.assertEqual(tasks[0][2:], ("2022-01-01", "2022-02-01"))
        idn_tasks = self.worker.build_backfill_tasks("2022-01-01", "2026-10-29", "IDN")
        self.assertEqual(len(idn_tasks), 116)


    def test_report_params_keep_highres_hourly_flag_grid_semantics(self):
        params = self.worker._report_params("ais", "2026-10-26", "2026-10-29", "HOURLY")
        self.assertEqual(params["group-by"], "FLAG")
        self.assertEqual(params["spatial-aggregation"], "false")
        self.assertEqual(params["spatial-resolution"], "HIGH")
        self.assertEqual(params["temporal-resolution"], "HOURLY")
        sar_params = self.worker._report_params("sar", "2026-10-26", "2026-10-29", "HOURLY")
        self.assertEqual(sar_params["filters[0]"], "matched='false'")

    def test_v5_nested_grid_payload_flattens_without_identity(self):
        payload = {"entries": [{"public-global-presence:v5.0": [
            {"date": "2026-10-27 05:00", "flag": "BLZ", "hours": 1, "lat": -7.17, "lon": 112.72, "vesselIDs": 1}
        ]}]}
        rows = list(self.worker._iter_nested_records(payload))
        self.assertEqual(len(rows), 1)
        version, raw = rows[0]
        region = self.worker.REGIONS[0]
        normalized = self.worker._normalise_record("ais", raw, version, region, "HOURLY")
        self.assertEqual(normalized["record_granularity"], "GRID_CELL_FLAG_HOUR")
        self.assertEqual(normalized["identity_available"], "false")
        self.assertEqual(normalized["flag"], "BLZ")


    def test_recent_watermarks_seed_from_verified_run_json(self):
        self.worker._ensure_db()
        payload = {
            "safe_end_date_exclusive_utc": "2026-10-29",
            "task_results": [
                {"kind": "ais", "region": "IDN", "status": "success"},
                {"kind": "sar", "region": "IDN", "status": "success"},
            ],
        }
        self.worker.RUN_JSON.write_text(json.dumps(payload), encoding="utf-8")
        rows = self.worker._bootstrap_recent_watermarks([], __import__("datetime").date(2026, 11, 5))
        self.assertEqual(len(rows), 2)
        watermarks = self.worker._success_watermarks(rows, "recent")
        self.assertEqual(watermarks[("ais", "IDN")].isoformat(), "2026-10-29")
        self.assertEqual(watermarks[("sar", "IDN")].isoformat(), "2026-10-29")

    def test_part_import_is_idempotent(self):
        self.worker._ensure_db()
        part = self.worker.PARTS_DIR / "recent" / "hourly" / "ais_IDN_test.csv"
        part.parent.mkdir(parents=True, exist_ok=True)
        row = self.worker._normalise_record(
            "ais",
            {"date": "2026-10-27 05:00", "flag": "BLZ", "hours": 1, "lat": -7.17, "lon": 112.72, "vesselIDs": 1},
            "public-global-presence:v5.0", self.worker.REGIONS[0], "HOURLY"
        )
        with part.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.worker.AIS_COLUMNS)
            w.writeheader(); w.writerow(row)
        first = self.worker._import_part_to_database("ais", part)
        second = self.worker._import_part_to_database("ais", part)
        self.assertEqual(first[0], 1)
        self.assertEqual(first[1], 1)
        self.assertEqual(second[1], 0)
        self.assertTrue(self.worker.AIS_MASTER.exists())
        with closing(__import__("sqlite3").connect(self.worker.DB_PATH)) as db:
            count = db.execute("SELECT COUNT(*) FROM recent_grid_records WHERE kind='ais'").fetchone()[0]
            managed = db.execute("SELECT value FROM recent_grid_meta WHERE key='worker_managed'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(managed, "true")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.api = load_module("df_api_test", APP_PATH)
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "recent_grid.sqlite3"
        import sqlite3
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("""CREATE TABLE recent_grid_records (
                kind TEXT, record_key TEXT, day TEXT, date_text TEXT, lat REAL, lon REAL, flag TEXT,
                hours REAL, vessel_ids REAL, detections REAL, region_iso3 TEXT, region_id TEXT,
                region_label TEXT, spatial_resolution TEXT, temporal_resolution TEXT,
                source_dataset TEXT, sample_key INTEGER, PRIMARY KEY(kind,record_key)) WITHOUT ROWID""")
            db.execute("CREATE TABLE recent_grid_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.executemany("INSERT INTO recent_grid_meta(key,value) VALUES (?,?)", [("worker_managed","true"),("ais_rows","1"),("sar_rows","1"),("built_utc","2026-11-05T00:00:00Z")])
            db.execute("INSERT INTO recent_grid_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("ais","IDN|2026-10-27 05:00|-7.17000|112.72000|BLZ","2026-10-27","2026-10-27 05:00",-7.17,112.72,"BLZ",1,1,None,"IDN","8492","Indonesia","HIGH","HOURLY","public-global-presence:v5.0",123))
            db.execute("INSERT INTO recent_grid_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("sar","IDN|2026-10-28 11:00|-4.98000|106.61000|","2026-10-28","2026-10-28 11:00",-4.98,106.61,"",None,1,1,"IDN","8492","Indonesia","HIGH","HOURLY","public-global-sar-presence:v5.0",456))
            db.commit()
        self.api.RECENT_GRID_INDEX_DB = self.db_path
        self.api.RECENT_GRID_AIS_CSV = Path(self.temp.name) / "ais.csv"
        self.api.RECENT_GRID_SAR_CSV = Path(self.temp.name) / "sar.csv"

    def tearDown(self):
        self.temp.cleanup()

    def test_recent_api_connection_accepts_worker_managed_database(self):
        with closing(self.api._recent_grid_connection()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM recent_grid_records").fetchone()[0]
        self.assertEqual(count, 2)


    def test_worker_managed_status_does_not_require_csv_signature_match(self):
        self.api.RECENT_GRID_AIS_CSV = Path(self.temp.name) / "missing_ais.csv"
        self.api.RECENT_GRID_SAR_CSV = Path(self.temp.name) / "missing_sar.csv"
        payload = self.api._recent_grid_status_payload()
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["worker_managed"])
        self.assertEqual(payload["rows_loaded"], 2)

    def test_unified_data_range_merges_source_windows(self):
        with patch.object(self.api, "get_data_source", return_value=Path("historical.csv")), \
             patch.object(self.api, "get_cached_data_range", return_value={"start_date":"2022-01-01","end_date":"2022-01-05","total_observations":2326571,"region_count":10}):
            payload = self.api.data_range()
        self.assertEqual(payload["start_date"], "2022-01-01")
        self.assertEqual(payload["end_date"], "2026-10-28")
        self.assertEqual(len(payload["source_windows"]), 3)
        self.assertEqual(payload["data_mode"], "UNIFIED_MULTI_SOURCE_CALENDAR")
        self.assertEqual(payload["grid_totals"], {"ais_grid_records": 1, "sar_grid_records": 1})

    def test_recent_grid_overview_returns_temporal_resolution_window(self):
        payload = self.api.recent_grid_overview(start_date=None, end_date=None)
        self.assertEqual(payload["ais_grid_records"], 1)
        self.assertEqual(payload["sar_grid_records"], 1)
        self.assertEqual(payload["temporal_resolution_windows"][0]["temporal_resolution"], "HOURLY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
