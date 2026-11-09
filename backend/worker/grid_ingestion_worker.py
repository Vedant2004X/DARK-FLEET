#!/usr/bin/env python3
"""DARK-FLEET v0.8.3/v0.8.4 grid data worker.

- Historical backfill: monthly HIGH-resolution FLAG-grouped HOURLY reports.
- Recent refresh: serial HIGH-resolution FLAG-grouped HOURLY reports with a 7-day safety lag.
- Writes into the same SQLite index used by the FastAPI app; CSV parts are immutable audit artifacts.
- Does not create MMSI identities or vessel-level tracks.

Required environment variable: GFW_API_ACCESS_TOKEN
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import json
import math
import os
import re
import sqlite3
import sys
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

API_BASE = "https://gateway.api.globalfishingwatch.org/v3"
AIS_DATASET = "public-global-presence:latest"
SAR_DATASET = "public-global-sar-presence:latest"
GROUP_BY = "FLAG"
SPATIAL_AGGREGATION = "false"
SPATIAL_RESOLUTION = "HIGH"
SAR_FILTER = "matched='false'"
BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data" / "processed"
RECENT_DIR = DATA_DIR / "recent_grid"
DB_PATH = RECENT_DIR / "recent_grid_local_index.sqlite3"
AIS_MASTER = RECENT_DIR / "recent_grid_ais_presence.csv"
SAR_MASTER = RECENT_DIR / "recent_grid_sar_presence.csv"
RUN_JSON = RECENT_DIR / "last_grid_ingestion_run.json"
MANIFEST = RECENT_DIR / "grid_worker_manifest.csv"
PARTS_DIR = RECENT_DIR / "parts"

# The ten pilot EEZs already used by DARK-FLEET. Keep this list versioned with the project.
REGIONS = [
    {"iso3": "IDN", "region_id": "8492", "region_label": "Indonesian Exclusive Economic Zone"},
    {"iso3": "ARG", "region_id": "8466", "region_label": "Argentinian Exclusive Economic Zone"},
    {"iso3": "AUS", "region_id": "8323", "region_label": "Australian Exclusive Economic Zone"},
    {"iso3": "BRA", "region_id": "8464", "region_label": "Brazilian Exclusive Economic Zone"},
    {"iso3": "CHL", "region_id": "8465", "region_label": "Chilean Exclusive Economic Zone"},
    {"iso3": "IND", "region_id": "8480", "region_label": "Indian Exclusive Economic Zone"},
    {"iso3": "JPN", "region_id": "8487", "region_label": "Japanese Exclusive Economic Zone"},
    {"iso3": "NOR", "region_id": "5686", "region_label": "Norwegian Exclusive Economic Zone"},
    {"iso3": "PER", "region_id": "8432", "region_label": "Peruvian Exclusive Economic Zone"},
    {"iso3": "ZAF", "region_id": "8396", "region_label": "South African Exclusive Economic Zone"},
]

AIS_COLUMNS = [
    "date", "lat", "lon", "flag", "hours", "vesselIDs", "record_granularity",
    "identity_available", "source_dataset", "region_iso3", "region_id", "region_label",
    "dataset_requested", "dataset_returned", "spatial_resolution", "temporal_resolution",
    "group_by", "spatial_aggregation", "time_interpretation",
]
SAR_COLUMNS = [
    "date", "lat", "lon", "flag", "hours", "vesselIDs", "detections", "record_granularity",
    "identity_available", "source_dataset", "region_iso3", "region_id", "region_label",
    "dataset_requested", "dataset_returned", "spatial_resolution", "temporal_resolution",
    "group_by", "spatial_aggregation", "matched_filter", "time_interpretation",
]
MANIFEST_COLUMNS = [
    "task_key", "mode", "kind", "iso3", "region_id", "region_label", "dataset_requested",
    "dataset_returned", "group_by", "spatial_aggregation", "spatial_resolution",
    "temporal_resolution", "start_date", "end_date_exclusive", "status", "rows",
    "valid_coordinate_rows", "invalid_coordinate_rows", "part_file", "http_status",
    "message", "completed_utc",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _normal_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        # GFW grid reports often return UTC-convention naive strings like YYYY-MM-DD HH:MM.
        return text


def _valid_date(value: Any) -> bool:
    text = str(value or "").strip()
    if len(text) < 10:
        return False
    try:
        date.fromisoformat(text[:10])
        return True
    except ValueError:
        return False


def _valid_coords(row: dict[str, Any]) -> bool:
    lat, lon = _safe_float(row.get("lat")), _safe_float(row.get("lon"))
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180


def _get_token() -> str:
    token = os.environ.get("GFW_API_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "GFW_API_ACCESS_TOKEN is not set. Configure it as a user/system environment variable; "
            "never paste it into source code or commit it."
        )
    return token


def _dataset_for(kind: str) -> str:
    return AIS_DATASET if kind == "ais" else SAR_DATASET


def _report_params(kind: str, start: str, end_exclusive: str, temporal_resolution: str) -> dict[str, str]:
    params = {
        "spatial-resolution": SPATIAL_RESOLUTION,
        "temporal-resolution": temporal_resolution,
        "group-by": GROUP_BY,
        "spatial-aggregation": SPATIAL_AGGREGATION,
        "datasets[0]": _dataset_for(kind),
        "date-range": f"{start},{end_exclusive}",
        "format": "JSON",
    }
    if kind == "sar":
        params["filters[0]"] = SAR_FILTER
    return params


def _poll_last_report(session: requests.Session, headers: dict[str, str], timeout_seconds: int = 900) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    url = f"{API_BASE}/4wings/last-report"
    while time.monotonic() < deadline:
        response = session.get(url, headers=headers, timeout=(15, 60))
        if response.status_code == 404:
            time.sleep(5)
            continue
        response.raise_for_status()
        payload = response.json()
        status = str(payload.get("status", "")).lower()
        if status == "running":
            time.sleep(10)
            continue
        if "entries" in payload:
            return payload
        if status in {"failed", "error"}:
            raise RuntimeError(f"GFW last-report ended with {status}: {str(payload)[:600]}")
        time.sleep(5)
    raise TimeoutError("Timed out waiting for the GFW report to finish.")


def _fetch_report(
    session: requests.Session, headers: dict[str, str], region: dict[str, str],
    kind: str, start: str, end_exclusive: str, temporal_resolution: str,
) -> tuple[dict[str, Any], str, int]:
    url = f"{API_BASE}/4wings/report"
    params = _report_params(kind, start, end_exclusive, temporal_resolution)
    body = {"region": {"dataset": "public-eez-areas", "id": int(region["region_id"])} }
    attempts = 4
    for attempt in range(1, attempts + 1):
        response = session.post(url, params=params, headers=headers, json=body, timeout=(20, 300))
        status_code = response.status_code
        if status_code == 200:
            payload = response.json()
            if str(payload.get("status", "")).lower() == "running":
                payload = _poll_last_report(session, headers)
            if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
                raise ValueError(f"GFW returned HTTP 200 without a valid entries array: {str(payload)[:500]}")
            state = str(payload.get("status", "")).lower()
            if state in {"failed", "error"}:
                raise RuntimeError(f"GFW report state={state}: {str(payload)[:500]}")
            returned = response.headers.get("x-datasets", "")
            if not returned and payload["entries"] and isinstance(payload["entries"][0], dict):
                for key, value in payload["entries"][0].items():
                    if isinstance(value, list):
                        returned = key
                        break
            return payload, returned or _dataset_for(kind), status_code
        message = f"HTTP {status_code}: {response.text[:700]}"
        if status_code == 404:
            raise RuntimeError(message)
        if status_code == 429:
            time.sleep(20 * attempt)
            continue
        if status_code in {500, 502, 503, 504, 524} and attempt < attempts:
            time.sleep(15 * attempt)
            continue
        raise RuntimeError(message)
    raise RuntimeError("GFW report did not complete after retry attempts.")


def _iter_nested_records(payload: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("GFW response is missing entries[].")
    for outer in entries:
        if not isinstance(outer, dict):
            continue
        emitted_flat = False
        for dataset_version, value in outer.items():
            if isinstance(value, list):
                for record in value:
                    if isinstance(record, dict):
                        yield str(dataset_version), record
                        emitted_flat = True
            elif isinstance(value, dict) and any(key in value for key in ("lat", "lon", "date")):
                yield str(dataset_version), value
                emitted_flat = True
        if not emitted_flat and any(key in outer for key in ("lat", "lon", "date")):
            yield str(outer.get("dataset", "unknown")), outer


def _normalise_record(
    kind: str, record: dict[str, Any], dataset_version: str, region: dict[str, str],
    temporal_resolution: str,
) -> dict[str, Any]:
    date_text = str(record.get("date", "") or "").strip()
    row = {
        "date": date_text,
        "lat": record.get("lat", ""),
        "lon": record.get("lon", ""),
        "flag": str(record.get("flag", "") or "").strip().upper(),
        "hours": record.get("hours", ""),
        "vesselIDs": record.get("vesselIDs", ""),
        "detections": record.get("detections", "") if kind == "sar" else "",
        "record_granularity": "GRID_CELL_FLAG_HOUR",
        "identity_available": "false",
        "source_dataset": dataset_version or _dataset_for(kind),
        "region_iso3": region["iso3"],
        "region_id": region["region_id"],
        "region_label": region["region_label"],
        "dataset_requested": _dataset_for(kind),
        "dataset_returned": dataset_version or _dataset_for(kind),
        "spatial_resolution": SPATIAL_RESOLUTION,
        "temporal_resolution": temporal_resolution,
        "group_by": GROUP_BY,
        "spatial_aggregation": SPATIAL_AGGREGATION,
        "matched_filter": SAR_FILTER if kind == "sar" else "",
        "time_interpretation": "GFW source date; naive timestamps follow the source UTC convention.",
    }
    # Keep each export schema exact: AIS has no SAR detections/matched-filter fields.
    allowed = set(_master_columns(kind))
    return {key: value for key, value in row.items() if key in allowed}


def _row_key(kind: str, row: dict[str, Any]) -> str:
    lat, lon = _safe_float(row.get("lat")), _safe_float(row.get("lon"))
    date_text = str(row.get("date", "") or "").strip()
    region = str(row.get("region_iso3", "")).strip().upper()
    flag = str(row.get("flag", "") or "").strip().upper()
    if lat is None or lon is None or not date_text or not region:
        raise ValueError("Cannot create grid natural key for row without region/date/coordinates.")
    return f"{region}|{date_text}|{lat:.5f}|{lon:.5f}|{flag}"


def _ensure_db() -> None:
    RECENT_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=60)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("""CREATE TABLE IF NOT EXISTS recent_grid_records (
            kind TEXT NOT NULL, record_key TEXT NOT NULL, day TEXT NOT NULL, date_text TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL, flag TEXT NOT NULL, hours REAL, vessel_ids REAL,
            detections REAL, region_iso3 TEXT NOT NULL, region_id TEXT, region_label TEXT,
            spatial_resolution TEXT, temporal_resolution TEXT, source_dataset TEXT, sample_key INTEGER NOT NULL,
            PRIMARY KEY(kind, record_key)
        ) WITHOUT ROWID""")
        conn.execute("CREATE TABLE IF NOT EXISTS recent_grid_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recent_kind_day_region ON recent_grid_records(kind, day, region_iso3)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recent_kind_region_day ON recent_grid_records(kind, region_iso3, day)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recent_kind_region_day_sample ON recent_grid_records(kind, region_iso3, day, sample_key)")
        conn.commit()


def _master_columns(kind: str) -> list[str]:
    return SAR_COLUMNS if kind == "sar" else AIS_COLUMNS


def _import_part_to_database(kind: str, part_path: Path) -> tuple[int, int, int]:
    """Idempotently append records to SQLite and master CSV. Returns (rows, inserted, invalid)."""
    _ensure_db()
    inserted_rows: list[dict[str, Any]] = []
    total = inserted = invalid = 0
    with part_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        with closing(sqlite3.connect(DB_PATH, timeout=60)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=60000")
            batch: list[tuple[Any, ...]] = []
            batch_rows: list[dict[str, Any]] = []
            def flush() -> None:
                nonlocal inserted
                if not batch:
                    return
                keys = [item[1] for item in batch]
                existing: set[str] = set()
                for offset in range(0, len(keys), 400):
                    chunk = keys[offset:offset + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    for (key,) in conn.execute(
                        f"SELECT record_key FROM recent_grid_records WHERE kind=? AND record_key IN ({placeholders})",
                        [kind, *chunk],
                    ).fetchall():
                        existing.add(str(key))
                new_rows = []
                seen_in_batch: set[str] = set()
                for row in batch_rows:
                    row_key = _row_key(kind, row)
                    if row_key not in existing and row_key not in seen_in_batch:
                        new_rows.append(row)
                        seen_in_batch.add(row_key)
                before = conn.total_changes
                conn.executemany(
                    "INSERT OR IGNORE INTO recent_grid_records "
                    "(kind,record_key,day,date_text,lat,lon,flag,hours,vessel_ids,detections,region_iso3,region_id,region_label,spatial_resolution,temporal_resolution,source_dataset,sample_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch,
                )
                inserted += conn.total_changes - before
                conn.commit()
                if new_rows:
                    master = AIS_MASTER if kind == "ais" else SAR_MASTER
                    master.parent.mkdir(parents=True, exist_ok=True)
                    file_exists = master.exists() and master.stat().st_size > 0
                    with master.open("a", encoding="utf-8", newline="") as out:
                        writer = csv.DictWriter(out, fieldnames=_master_columns(kind), extrasaction="ignore")
                        if not file_exists:
                            writer.writeheader()
                        writer.writerows(new_rows)
                batch.clear()
                batch_rows.clear()
            for raw in reader:
                total += 1
                position = (_safe_float(raw.get("lat")), _safe_float(raw.get("lon")))
                date_text = str(raw.get("date", "") or "").strip()
                day = date_text[:10]
                region = str(raw.get("region_iso3", "") or "").strip().upper()
                if position[0] is None or position[1] is None or not (-90 <= position[0] <= 90 and -180 <= position[1] <= 180) or not _valid_date(date_text) or not region:
                    invalid += 1
                    continue
                lat, lon = position
                row = dict(raw)
                row["region_iso3"] = region
                row["date"] = date_text
                key = _row_key(kind, row)
                sample_key = zlib.crc32(f"{kind}|{key}".encode("utf-8")) & 0xFFFFFFFF
                batch.append((
                    kind, key, day, date_text, lat, lon, str(row.get("flag", "") or "").upper(),
                    _safe_float(row.get("hours")), _safe_float(row.get("vesselIDs")), _safe_float(row.get("detections")),
                    region, str(row.get("region_id", "") or ""), str(row.get("region_label", "") or ""),
                    str(row.get("spatial_resolution", "HIGH") or "HIGH"),
                    str(row.get("temporal_resolution", "HOURLY") or "HOURLY"),
                    str(row.get("source_dataset", "") or ""), sample_key,
                ))
                batch_rows.append(row)
                if len(batch) >= 2000:
                    flush()
            flush()
    with closing(sqlite3.connect(DB_PATH, timeout=60)) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        counts = {k: int(conn.execute("SELECT COUNT(*) FROM recent_grid_records WHERE kind=?", (k,)).fetchone()[0]) for k in ("ais", "sar")}
        meta = {
            "worker_managed": "true",
            "ais_rows": str(counts["ais"]),
            "sar_rows": str(counts["sar"]),
            "built_utc": utc_iso(),
            "last_worker_version": "0.8.5",
        }
        conn.executemany("INSERT INTO recent_grid_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", list(meta.items()))
        conn.commit()
    return total, inserted, invalid


def _read_manifest() -> list[dict[str, str]]:
    if not MANIFEST.exists():
        return []
    with MANIFEST.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _append_manifest(row: dict[str, Any]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    exists = MANIFEST.exists() and MANIFEST.stat().st_size > 0
    with MANIFEST.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _latest_manifest_by_task(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        latest[str(row.get("task_key", ""))] = row
    return latest


def _task_key(mode: str, kind: str, iso3: str, start: str, end: str, temporal: str) -> str:
    return "|".join((mode, kind, iso3, start, end, temporal))


def _task_rows(mode: str, kind: str, region: dict[str, str], start: str, end: str, temporal: str) -> dict[str, Any]:
    return {
        "task_key": _task_key(mode, kind, region["iso3"], start, end, temporal),
        "mode": mode, "kind": kind, "iso3": region["iso3"], "region_id": region["region_id"],
        "region_label": region["region_label"], "dataset_requested": _dataset_for(kind),
        "group_by": GROUP_BY, "spatial_aggregation": SPATIAL_AGGREGATION,
        "spatial_resolution": SPATIAL_RESOLUTION, "temporal_resolution": temporal,
        "start_date": start, "end_date_exclusive": end,
    }


def _write_report_part(
    session: requests.Session, headers: dict[str, str], region: dict[str, str], kind: str,
    start: str, end: str, temporal: str, mode: str,
) -> dict[str, Any]:
    payload, returned, http_status = _fetch_report(session, headers, region, kind, start, end, temporal)
    part_dir = PARTS_DIR / mode / temporal.lower()
    part_dir.mkdir(parents=True, exist_ok=True)
    part_path = part_dir / f"{kind}_{region['iso3']}_{start}_{end}_{temporal}.csv"
    columns = _master_columns(kind)
    tmp = part_path.with_suffix(part_path.suffix + ".tmp")
    rows = valid_coords = invalid_coords = 0
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for dataset_version, raw in _iter_nested_records(payload):
            row = _normalise_record(kind, raw, returned or dataset_version, region, temporal)
            rows += 1
            if _valid_coords(row) and _valid_date(row.get("date")):
                valid_coords += 1
            else:
                invalid_coords += 1
            writer.writerow(row)
    os.replace(tmp, part_path)
    imported, inserted, invalid = _import_part_to_database(kind, part_path)
    result = {
        "rows": rows,
        "valid_coordinate_rows": valid_coords,
        "invalid_coordinate_rows": invalid_coords,
        "part_file": str(part_path),
        "dataset_returned": returned or _dataset_for(kind),
        "http_status": http_status,
        "database_rows_read": imported,
        "database_rows_inserted": inserted,
        "database_invalid_rows": invalid,
    }
    return result


def _record_manifest(task: dict[str, Any], status: str, result: dict[str, Any] | None = None, message: str = "") -> None:
    result = result or {}
    row = {
        **task,
        "dataset_returned": result.get("dataset_returned", ""),
        "status": status,
        "rows": result.get("rows", 0),
        "valid_coordinate_rows": result.get("valid_coordinate_rows", 0),
        "invalid_coordinate_rows": result.get("invalid_coordinate_rows", 0),
        "part_file": result.get("part_file", ""),
        "http_status": result.get("http_status", 0),
        "message": message or result.get("message", ""),
        "completed_utc": utc_iso(),
    }
    _append_manifest(row)


def _bootstrap_recent_watermarks(manifest_rows: list[dict[str, str]], safe_end: date) -> list[dict[str, str]]:
    """Seed recent watermarks from the verified v0.8.1d run JSON, then SQLite.

    The run JSON preserves the successful report end-exclusive watermark even when a
    particular region/dataset had zero rows on the final day. SQLite is the fallback
    for installations where the run JSON was not copied locally.
    """
    if any(row.get("mode") == "recent" for row in manifest_rows):
        return manifest_rows
    seeded: list[dict[str, str]] = []

    if RUN_JSON.exists():
        try:
            payload = json.loads(RUN_JSON.read_text(encoding="utf-8"))
            end_text = str(payload.get("safe_end_date_exclusive_utc", ""))
            task_results = payload.get("task_results") or []
            seen = {
                (str(item.get("kind", "")).lower(), str(item.get("region", item.get("iso3", ""))).upper())
                for item in task_results if str(item.get("status", "")).lower() == "success"
            }
            if end_text and seen:
                end_date = date.fromisoformat(end_text)
                start_date = end_date - timedelta(days=3)
                for region in REGIONS:
                    for kind in ("ais", "sar"):
                        if (kind, region["iso3"]) not in seen:
                            continue
                        task = _task_rows("recent", kind, region, start_date.isoformat(), end_text, "HOURLY")
                        row = {
                            **task,
                            "dataset_returned": "public-global-presence:v5.0" if kind == "ais" else "public-global-sar-presence:v5.0",
                            "status": "baseline_success", "rows": 0, "valid_coordinate_rows": 0,
                            "invalid_coordinate_rows": 0, "part_file": "", "http_status": 200,
                            "message": "Watermark seeded from the verified v0.8.1d successful report manifest; source rows already exist in the local grid index.",
                            "completed_utc": utc_iso(),
                        }
                        _append_manifest(row)
                        seeded.append({k: str(v) for k, v in row.items()})
                if seeded:
                    manifest_rows.extend(seeded)
                    print(f"Seeded recent watermarks for {len(seeded)} region/dataset streams through {end_text} from the verified run JSON.")
                    return manifest_rows
        except Exception as exc:
            print(f"WARNING: Could not seed recent watermarks from run JSON: {exc}")

    # Fallback: infer an end-exclusive watermark from the last day represented in SQLite.
    try:
        if DB_PATH.exists():
            with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
                grouped = conn.execute(
                    "SELECT kind, region_iso3, MIN(day), MAX(day), COUNT(*) "
                    "FROM recent_grid_records GROUP BY kind, region_iso3"
                ).fetchall()
            by_key = {
                (str(kind).lower(), str(iso3).upper()): (str(min_day), str(max_day), int(n))
                for kind, iso3, min_day, max_day, n in grouped if n and min_day and max_day
            }
            for region in REGIONS:
                for kind in ("ais", "sar"):
                    values = by_key.get((kind, region["iso3"]))
                    if not values:
                        continue
                    min_day, max_day, n = values
                    end_exclusive = (date.fromisoformat(max_day) + timedelta(days=1)).isoformat()
                    task = _task_rows("recent", kind, region, min_day, end_exclusive, "HOURLY")
                    row = {
                        **task,
                        "dataset_returned": "public-global-presence:v5.0" if kind == "ais" else "public-global-sar-presence:v5.0",
                        "status": "baseline_success", "rows": n, "valid_coordinate_rows": n,
                        "invalid_coordinate_rows": 0, "part_file": "", "http_status": 200,
                        "message": "Watermark inferred from maximum date in the existing local v0.8.2 grid SQLite index.",
                        "completed_utc": utc_iso(),
                    }
                    _append_manifest(row)
                    seeded.append({k: str(v) for k, v in row.items()})
            manifest_rows.extend(seeded)
            if seeded:
                print(f"Seeded recent watermarks for {len(seeded)} region/dataset streams from SQLite.")
    except Exception as exc:
        print(f"WARNING: Could not seed recent watermarks from SQLite: {exc}")
    return manifest_rows


def _success_watermarks(rows: list[dict[str, str]], mode: str) -> dict[tuple[str, str], date]:
    watermarks: dict[tuple[str, str], date] = {}
    for row in rows:
        if row.get("mode") != mode or row.get("status") not in {"success", "baseline_success"}:
            continue
        try:
            end_date = date.fromisoformat(row.get("end_date_exclusive", ""))
        except ValueError:
            continue
        key = (row.get("kind", ""), row.get("iso3", ""))
        watermarks[key] = max(watermarks.get(key, date.min), end_date)
    return watermarks


def run_recent_update(max_reports: int = 20, safe_lag_days: int = 7, overlap_days: int = 7,
                      max_new_days: int = 7, pause_seconds: float = 3.0) -> dict[str, Any]:
    token = _get_token()
    _ensure_db()
    safe_end = utc_now().date() - timedelta(days=safe_lag_days)
    rows = _bootstrap_recent_watermarks(_read_manifest(), safe_end)
    latest = _latest_manifest_by_task(rows)
    watermarks = _success_watermarks(rows, "recent")
    tasks: list[tuple[str, dict[str, str], str, str]] = []
    for region in REGIONS:
        for kind in ("ais", "sar"):
            watermark = watermarks.get((kind, region["iso3"]))
            if watermark is None:
                end = safe_end
                start = end - timedelta(days=3)
            else:
                if safe_end <= watermark:
                    continue
                start = watermark - timedelta(days=overlap_days)
                end = min(safe_end, watermark + timedelta(days=max_new_days))
            if start < end:
                tasks.append((kind, region, start.isoformat(), end.isoformat()))
    if not tasks:
        result = {"pipeline_version": "0.8.5", "mode": "recent", "status": "up_to_date", "reports_total": 0,
                  "successful_reports_this_run": 0, "failed_reports_this_run": 0, "skipped_reports": 20,
                  "safe_lag_days": safe_lag_days, "safe_end_date_exclusive_utc": safe_end.isoformat(),
                  "identity_available": False,
                  "note": "All recent grid source watermarks are already at the current safe end date."}
        RUN_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return result
    session = requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    successful = failed = 0
    task_results: list[dict[str, Any]] = []
    for index, (kind, region, start, end) in enumerate(tasks[:max_reports], start=1):
        task = _task_rows("recent", kind, region, start, end, "HOURLY")
        task_key = task["task_key"]
        prior = latest.get(task_key)
        if prior and prior.get("status") == "success" and prior.get("part_file") and Path(prior["part_file"]).exists():
            continue
        print(f"[{index}/{min(len(tasks), max_reports)}] RECENT {kind.upper()} {region['iso3']} {start} to {end} (exclusive)")
        try:
            result = _write_report_part(session, headers, region, kind, start, end, "HOURLY", "recent")
            _record_manifest(task, "success", result)
            successful += 1
            task_results.append({"kind": kind, "region": region["iso3"], **result, "status": "success"})
            print(f"  SUCCESS: {result['rows']:,} rows; {result['database_rows_inserted']:,} new grid keys")
        except Exception as exc:
            status_match = re.search(r"HTTP (\d{3})", str(exc))
            _record_manifest(task, "failed", {"http_status": int(status_match.group(1)) if status_match else 0}, str(exc)[:1200])
            failed += 1
            task_results.append({"kind": kind, "region": region["iso3"], "status": "failed", "error": str(exc)[:400]})
            print(f"  FAILED: {str(exc)[:500]}")
        if index < min(len(tasks), max_reports):
            time.sleep(pause_seconds)
    result = {
        "pipeline_version": "0.8.5", "mode": "recent", "safe_lag_days": safe_lag_days,
        "safe_end_date_exclusive_utc": safe_end.isoformat(), "overlap_days": overlap_days,
        "successful_reports_this_run": successful, "failed_reports_this_run": failed,
        "remaining_region_dataset_tasks": max(0, len(tasks) - min(len(tasks), max_reports)),
        "identity_available": False, "record_granularity": "GRID_CELL_FLAG_HOUR",
        "task_results": task_results, "completed_utc": utc_iso(),
    }
    RUN_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\nRECENT UPDATE SUMMARY\n" + json.dumps(result, indent=2))
    return result


def _month_windows(start: date, end_exclusive: date) -> Iterator[tuple[date, date]]:
    cursor = start
    while cursor < end_exclusive:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        stop = min(next_month, end_exclusive)
        yield cursor, stop
        cursor = stop


def build_backfill_tasks(start_date: str, end_date_exclusive: str, region_filter: str = "") -> list[tuple[str, dict[str, str], str, str]]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date_exclusive)
    if start >= end:
        raise ValueError("Backfill start must be earlier than end-exclusive date.")
    regions = [r for r in REGIONS if not region_filter or r["iso3"] == region_filter.upper()]
    tasks = []
    for month_start, month_end in _month_windows(start, end):
        for region in regions:
            for kind in ("ais", "sar"):
                tasks.append((kind, region, month_start.isoformat(), month_end.isoformat()))
    return tasks


def run_backfill_batch(start_date: str = "2022-01-01", end_date_exclusive: str | None = "2026-10-29",
                       max_reports: int = 10, region_filter: str = "", pause_seconds: float = 3.0,
                       plan_only: bool = False) -> dict[str, Any]:
    token = None if plan_only else _get_token()
    # Fixed cutoff matches the already-collected recent pilot watermark. The daily recent
    # worker advances beyond it, so monthly task keys remain stable and resumable.
    end_date_exclusive = end_date_exclusive or "2026-10-29"
    tasks = build_backfill_tasks(start_date, end_date_exclusive, region_filter)
    manifest_rows = _read_manifest()
    latest = _latest_manifest_by_task(manifest_rows)
    pending: list[tuple[str, dict[str, str], str, str]] = []
    for item in tasks:
        kind, region, start, end = item
        task = _task_rows("backfill", kind, region, start, end, "HOURLY")
        row = latest.get(task["task_key"])
        if row and row.get("status") == "success" and row.get("part_file") and Path(row["part_file"]).exists():
            continue
        pending.append(item)
    summary = {"mode": "backfill", "start_date": start_date, "end_date_exclusive": end_date_exclusive,
               "region_filter": region_filter or "ALL", "planned_monthly_reports": len(tasks),
               "completed_reports": len(tasks) - len(pending), "remaining_reports": len(pending),
               "batch_limit": max_reports, "temporal_resolution": "HOURLY", "spatial_resolution": "HIGH",
               "identity_available": False}
    if plan_only:
        print(json.dumps(summary, indent=2))
        return summary
    _ensure_db()
    session = requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    successful = failed = 0
    task_results = []
    for index, (kind, region, start, end) in enumerate(pending[:max_reports], start=1):
        task = _task_rows("backfill", kind, region, start, end, "HOURLY")
        print(f"[{index}/{min(len(pending), max_reports)}] BACKFILL {kind.upper()} {region['iso3']} {start} to {end} (HOURLY, end-exclusive)")
        try:
            result = _write_report_part(session, headers, region, kind, start, end, "HOURLY", "backfill")
            _record_manifest(task, "success", result)
            successful += 1
            task_results.append({"kind": kind, "region": region["iso3"], **result, "status": "success"})
            print(f"  SUCCESS: {result['rows']:,} hourly grid rows; {result['database_rows_inserted']:,} inserted")
        except Exception as exc:
            status_match = re.search(r"HTTP (\d{3})", str(exc))
            _record_manifest(task, "failed", {"http_status": int(status_match.group(1)) if status_match else 0}, str(exc)[:1200])
            failed += 1
            task_results.append({"kind": kind, "region": region["iso3"], "status": "failed", "error": str(exc)[:400]})
            print(f"  FAILED: {str(exc)[:500]}")
        if index < min(len(pending), max_reports):
            time.sleep(pause_seconds)
    summary.update({"successful_this_batch": successful, "failed_this_batch": failed,
                    "remaining_after_batch": len(pending) - min(len(pending), max_reports),
                    "task_results": task_results, "completed_utc": utc_iso()})
    (RECENT_DIR / "last_backfill_batch.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\nBACKFILL BATCH SUMMARY\n" + json.dumps(summary, indent=2))
    return summary


def run_daemon(interval_hours: float = 24.0, max_reports: int = 20) -> None:
    interval = max(1.0, interval_hours) * 3600
    print(f"DARK-FLEET v0.8.5 ingestion worker running; recent update interval={interval_hours:g} hours.")
    while True:
        started = time.monotonic()
        try:
            run_recent_update(max_reports=max_reports)
        except Exception as exc:
            print(f"DAILY UPDATE ERROR: {exc}")
        elapsed = time.monotonic() - started
        sleep_for = max(60.0, interval - elapsed)
        print(f"Next recent update in approximately {sleep_for/3600:.2f} hours.")
        time.sleep(sleep_for)



def _acquire_worker_lock() -> Path:
    RECENT_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = RECENT_DIR / "grid_ingestion_worker.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "started_utc": utc_iso()}))
        return lock_path
    except FileExistsError:
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > 12 * 3600:
                lock_path.unlink(missing_ok=True)
                return _acquire_worker_lock()
        except OSError:
            pass
        raise RuntimeError("Another DARK-FLEET ingestion worker is already running. Wait for it to finish before starting another report job.")

def main() -> int:
    parser = argparse.ArgumentParser(description="DARK-FLEET resumable GFW gridded-data ingestion worker")
    parser.add_argument("--mode", choices=["plan", "backfill", "recent", "scheduled", "daemon"], required=True)
    parser.add_argument("--start-date", default="2022-01-01")
    parser.add_argument("--end-date-exclusive", default="2026-10-29")
    parser.add_argument("--max-reports", type=int, default=10)
    parser.add_argument("--region", default="", help="Optional ISO3 filter for backfill, e.g. IDN")
    parser.add_argument("--pause-seconds", type=float, default=3.0)
    parser.add_argument("--interval-hours", type=float, default=24.0)
    parser.add_argument("--backfill-reports", type=int, default=20)
    parser.add_argument("--safe-lag-days", type=int, default=7)
    parser.add_argument("--overlap-days", type=int, default=7)
    parser.add_argument("--max-new-days", type=int, default=7)
    args = parser.parse_args()
    if args.mode == "plan":
        run_backfill_batch(args.start_date, args.end_date_exclusive, args.max_reports, args.region, plan_only=True)
        return 0
    lock_path = _acquire_worker_lock()
    try:
        if args.mode == "backfill":
            run_backfill_batch(args.start_date, args.end_date_exclusive, args.max_reports, args.region, args.pause_seconds)
        elif args.mode == "recent":
            run_recent_update(args.max_reports, args.safe_lag_days, args.overlap_days, args.max_new_days, args.pause_seconds)
        elif args.mode == "scheduled":
            # Backfill batch and daily refresh run serially under one lock/token.
            run_backfill_batch(args.start_date, args.end_date_exclusive, args.backfill_reports, args.region, args.pause_seconds)
            run_recent_update(20, args.safe_lag_days, args.overlap_days, args.max_new_days, args.pause_seconds)
        elif args.mode == "daemon":
            run_daemon(args.interval_hours, args.max_reports)
    finally:
        lock_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped by user. Completed report parts and manifest checkpoints are preserved.")
        raise SystemExit(130)
