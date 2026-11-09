from __future__ import annotations

import csv
from contextlib import closing
import heapq
import math
import os
import threading
import time
import zlib
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="DARK-FLEET API",
    description=(
        "Maritime anomaly-analysis research API using AIS-derived observations "
        "and provisional SAR/AIS proximity comparisons."
    ),
    version="0.8.5",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data" / "processed"
ANOMALY_CSV = DATA_DIR / "dark_fleet_anomaly_results.csv"
SAR_AIS_CSV = DATA_DIR / "dark_fleet_final_sar_ais_matches.csv"
GLOBAL_ANOMALY_CSV = DATA_DIR / "dark_fleet_global_anomaly_results.csv"
GLOBAL_SAR_CSV = DATA_DIR / "dark_fleet_global_sar_detections.csv"
RECENT_GRID_DIR = DATA_DIR / "recent_grid"
RECENT_GRID_AIS_CSV = RECENT_GRID_DIR / "recent_grid_ais_presence.csv"
RECENT_GRID_SAR_CSV = RECENT_GRID_DIR / "recent_grid_sar_presence.csv"
RECENT_GRID_RUN_JSON = RECENT_GRID_DIR / "last_grid_ingestion_run.json"
RECENT_GRID_INDEX_DB = RECENT_GRID_DIR / "recent_grid_local_index.sqlite3"
PAGE_OFFSET_SAFETY_CAP = 100_000


def iter_csv(path: Path):
    """Yield CSV rows as dictionaries without materializing the whole file."""
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Required data file not found: {path.name}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            yield from csv.DictReader(handle)
    except (OSError, csv.Error) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read {path.name}") from exc


def read_small_csv(path: Path) -> list[dict[str, str]]:
    return list(iter_csv(path))


def stat_key(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def valid_lat_lon(lat_value: Any, lon_value: Any) -> tuple[float, float] | None:
    if lat_value is None or lon_value is None:
        return None
    if str(lat_value).strip() == "" or str(lon_value).strip() == "":
        return None
    lat = safe_float(lat_value, float("nan"))
    lon = safe_float(lon_value, float("nan"))
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def day_text(value: Any) -> str:
    """Return ISO YYYY-MM-DD from ISO or space-separated timestamps."""
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else ""


def validate_date_range(start_date: date | None, end_date: date | None) -> tuple[str, str]:
    start_text = start_date.isoformat() if start_date else ""
    end_text = end_date.isoformat() if end_date else ""
    if start_text and end_text and start_text > end_text:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
    return start_text, end_text


def within_date_range(
    row: dict[str, str], start_text: str = "", end_text: str = "", *, date_key: str = "date"
) -> bool:
    date_value = row.get(date_key)
    if not date_value and date_key == "date":
        # The historic Chile comparison file stores its time as sar_time rather than date.
        date_value = (
            row.get("sar_time")
            or row.get("entryTimestamp")
            or row.get("timestamp")
            or row.get("ais_time")
        )
    row_day = day_text(date_value)
    if not row_day:
        return False if (start_text or end_text) else True
    if start_text and row_day < start_text:
        return False
    if end_text and row_day > end_text:
        return False
    return True


def get_data_source() -> Path:
    return GLOBAL_ANOMALY_CSV if GLOBAL_ANOMALY_CSV.exists() else ANOMALY_CSV


def get_map_region(row: dict[str, str]) -> str:
    value = (
        row.get("region_iso3")
        or row.get("source_region_iso3")
        or row.get("region_label")
        or row.get("source_region_label")
        or "UNKNOWN"
    )
    return value.strip().upper() or "UNKNOWN"


def allocate_even_region_quotas(region_counts: dict[str, int], limit: int) -> dict[str, int]:
    regions = sorted(region for region, count in region_counts.items() if count > 0)
    quotas = {region: 0 for region in regions}
    remaining = min(limit, sum(region_counts[region] for region in regions))
    while remaining > 0:
        active = [region for region in regions if quotas[region] < region_counts[region]]
        if not active:
            break
        share = max(1, remaining // len(active))
        progressed = 0
        for region in active:
            if remaining <= 0:
                break
            add = min(share, region_counts[region] - quotas[region], remaining)
            quotas[region] += add
            remaining -= add
            progressed += add
        if progressed == 0:
            break
    return quotas


def choose_even_indices(count: int, quota: int) -> set[int]:
    if quota <= 0 or count <= 0:
        return set()
    if quota >= count:
        return set(range(count))
    if quota == 1:
        return {count // 2}
    return {round(i * (count - 1) / (quota - 1)) for i in range(quota)}


def sample_map_csv(
    path: Path, limit: int, start_text: str = "", end_text: str = ""
) -> tuple[list[dict[str, str]], int]:
    """Sample valid map records by region and date without reading the full CSV into RAM."""
    region_counts: Counter[str] = Counter()
    for row in iter_csv(path):
        if not within_date_range(row, start_text, end_text):
            continue
        if valid_lat_lon(row.get("lat"), row.get("lon")) is not None:
            region_counts[get_map_region(row)] += 1

    total_valid = sum(region_counts.values())
    if total_valid == 0:
        return [], 0

    quotas = allocate_even_region_quotas(dict(region_counts), limit)
    targets = {
        region: choose_even_indices(region_counts[region], quotas.get(region, 0))
        for region in region_counts
    }
    positions: Counter[str] = Counter()
    chosen: list[dict[str, str]] = []
    for row in iter_csv(path):
        if not within_date_range(row, start_text, end_text):
            continue
        if valid_lat_lon(row.get("lat"), row.get("lon")) is None:
            continue
        region = get_map_region(row)
        position = positions[region]
        positions[region] += 1
        if position in targets.get(region, set()):
            chosen.append(row)

    chosen.sort(key=lambda row: (
        get_map_region(row), safe_float(row.get("lat")), safe_float(row.get("lon")), row.get("date", "")
    ))
    return chosen, total_valid


@lru_cache(maxsize=16)
def _cached_map_sample(
    path_text: str, modified_ns: int, file_size: int, limit: int, start_text: str, end_text: str
) -> tuple[list[dict[str, str]], int]:
    return sample_map_csv(Path(path_text), limit, start_text, end_text)


def get_cached_map_sample(path: Path, limit: int, start_text: str, end_text: str):
    path_text, modified_ns, file_size = stat_key(path)
    return _cached_map_sample(path_text, modified_ns, file_size, limit, start_text, end_text)


@lru_cache(maxsize=32)
def _cached_data_range(path_text: str, modified_ns: int, file_size: int) -> dict[str, Any]:
    start_text = ""
    end_text = ""
    rows = 0
    regions: set[str] = set()
    for row in iter_csv(Path(path_text)):
        value = day_text(row.get("date"))
        if not value:
            continue
        rows += 1
        start_text = min(start_text, value) if start_text else value
        end_text = max(end_text, value) if end_text else value
        region = get_map_region(row)
        if region != "UNKNOWN":
            regions.add(region)
    return {
        "start_date": start_text,
        "end_date": end_text,
        "total_observations": rows,
        "region_count": len(regions),
    }


def get_cached_data_range(path: Path) -> dict[str, Any]:
    path_text, modified_ns, file_size = stat_key(path)
    return _cached_data_range(path_text, modified_ns, file_size)


@lru_cache(maxsize=64)
def _cached_summary(
    path_text: str, modified_ns: int, file_size: int, start_text: str, end_text: str
) -> dict[str, Any]:
    total = 0
    flagged = 0
    vessels: set[str] = set()
    for row in iter_csv(Path(path_text)):
        if not within_date_range(row, start_text, end_text):
            continue
        total += 1
        if str(row.get("anomaly_flag", "")).strip() == "-1":
            flagged += 1
        mmsi = str(row.get("mmsi") or "").strip()
        if mmsi:
            vessels.add(mmsi)
    return {
        "total_observations": total,
        "flagged_observations": flagged,
        "baseline_observations": total - flagged,
        "unique_vessels": len(vessels),
    }


def get_cached_summary(path: Path, start_text: str, end_text: str) -> dict[str, Any]:
    path_text, modified_ns, file_size = stat_key(path)
    return _cached_summary(path_text, modified_ns, file_size, start_text, end_text)


def matches_feed_filters(
    row: dict[str, str], status: str, search_text: str, start_text: str, end_text: str
) -> bool:
    if not within_date_range(row, start_text, end_text):
        return False
    flag = str(row.get("anomaly_flag", "")).strip()
    if status == "REVIEW" and flag != "-1":
        return False
    # Baseline is the complement of the flagged class, matching the summary KPI.
    if status == "NORMAL" and flag == "-1":
        return False
    if search_text:
        fields = ("mmsi", "shipName", "vesselType", "date", "region_iso3", "region_label")
        if search_text not in " ".join(str(row.get(key, "")) for key in fields).lower():
            return False
    return True


@lru_cache(maxsize=32)
def _cached_anomaly_page(
    path_text: str,
    modified_ns: int,
    file_size: int,
    start_text: str,
    end_text: str,
    status: str,
    search_text: str,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, str]], int]:
    total_matching = 0

    def filtered_rows():
        nonlocal total_matching
        for row in iter_csv(Path(path_text)):
            if matches_feed_filters(row, status, search_text, start_text, end_text):
                total_matching += 1
                yield row

    # Pick only the requested page. For ALL, deterministically mix both classes
    # within each timestamp instead of putting every high anomaly score first.
    # This preserves the true dataset prevalence; it is not a 50/50 re-sampling.
    requested = offset + limit
    if status == "ALL":
        def page_order(row: dict[str, str]):
            identity = f"{row.get('mmsi', '')}|{row.get('date', '')}".encode("utf-8", errors="ignore")
            return (row.get("date", ""), zlib.crc32(identity), row.get("mmsi", ""))
    else:
        def page_order(row: dict[str, str]):
            return (row.get("date", ""), safe_float(row.get("anomaly_score")), row.get("mmsi", ""))
    newest = heapq.nlargest(requested, filtered_rows(), key=page_order)
    return newest[offset:offset + limit], total_matching


def get_anomaly_page(path: Path, start_text: str, end_text: str, status: str,
                     search_text: str, offset: int, limit: int):
    path_text, modified_ns, file_size = stat_key(path)
    return _cached_anomaly_page(
        path_text, modified_ns, file_size, start_text, end_text,
        status, search_text, offset, limit,
    )


def validate_range_for_request(start_date: date | None, end_date: date | None) -> tuple[str, str]:
    return validate_date_range(start_date, end_date)


# -----------------------------------------------------------------------------
# v0.6.1 â€” persistent SQLite index for the 2.3M-row global anomaly CSV.
# The source CSV is never modified. The index is rebuilt only when its source
# file signature changes, then reused across API requests and server restarts.
# -----------------------------------------------------------------------------
GLOBAL_CACHE_DB = DATA_DIR / "dark_fleet_global_anomaly_index.sqlite3"
GLOBAL_CACHE_BUILDING_DB = DATA_DIR / "dark_fleet_global_anomaly_index.building.sqlite3"
_GLOBAL_CACHE_BUILD_LOCK = threading.Lock()
_GLOBAL_CACHE_STATE_LOCK = threading.Lock()
_GLOBAL_CACHE_STATE: dict[str, Any] = {
    "state": "not_built", "rows_loaded": 0, "source_bytes_read": 0,
    "source_size_bytes": 0, "started_at": None, "elapsed_seconds": 0.0,
    "error": None,
}
_GLOBAL_CACHE_THREAD: threading.Thread | None = None

_GLOBAL_TEXT_FIELDS = (
    "date", "mmsi", "shipName", "vesselType", "speed_knots",
    "speed_change_knots", "time_gap_hours", "anomaly_score",
    "anomaly_flag", "anomaly_status", "region_iso3", "region_id",
    "region_label", "source_dataset",
)


def _global_source_signature() -> str:
    stat = GLOBAL_ANOMALY_CSV.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _global_cache_signature_matches() -> bool:
    if not GLOBAL_CACHE_DB.exists() or not GLOBAL_ANOMALY_CSV.exists():
        return False
    try:
        with closing(_sqlite3.connect(str(GLOBAL_CACHE_DB), timeout=3)) as conn:
            row = conn.execute("SELECT value FROM cache_metadata WHERE key='source_signature'").fetchone()
            return bool(row and row[0] == _global_source_signature())
    except (_sqlite3.Error, OSError):
        return False


def _set_global_cache_state(**values: Any) -> None:
    with _GLOBAL_CACHE_STATE_LOCK:
        _GLOBAL_CACHE_STATE.update(values)


def _global_cache_status_payload() -> dict[str, Any]:
    if not GLOBAL_ANOMALY_CSV.exists():
        return {"required": False, "ready": True, "state": "not_required", "rows_loaded": 0}
    if _global_cache_signature_matches():
        try:
            with closing(_sqlite3.connect(str(GLOBAL_CACHE_DB), timeout=3)) as conn:
                metadata = dict(conn.execute("SELECT key,value FROM cache_metadata").fetchall())
            return {
                "required": True, "ready": True, "state": "ready",
                "rows_loaded": int(metadata.get("row_count", "0")),
                "database_file": GLOBAL_CACHE_DB.name,
                "database_size_mb": round(GLOBAL_CACHE_DB.stat().st_size / (1024 * 1024), 2),
            }
        except (_sqlite3.Error, OSError):
            pass
    with _GLOBAL_CACHE_STATE_LOCK:
        state = dict(_GLOBAL_CACHE_STATE)
    return {
        "required": True, "ready": state.get("state") == "ready",
        "database_file": GLOBAL_CACHE_DB.name,
        **state,
    }


def _build_global_cache_worker() -> None:
    started = time.time()
    source_size = GLOBAL_ANOMALY_CSV.stat().st_size
    _set_global_cache_state(
        state="building", rows_loaded=0, source_bytes_read=0,
        source_size_bytes=source_size, started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        elapsed_seconds=0.0, error=None,
    )
    conn = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            stale = Path(str(GLOBAL_CACHE_BUILDING_DB) + suffix)
            if stale.exists():
                stale.unlink()
        conn = _sqlite3.connect(str(GLOBAL_CACHE_BUILDING_DB), timeout=120)
        conn.execute("PRAGMA page_size=32768")
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-65536")
        conn.execute("""CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            date_day TEXT NOT NULL,
            date TEXT,
            mmsi TEXT,
            lat REAL,
            lon REAL,
            shipName TEXT,
            vesselType TEXT,
            speed_knots TEXT,
            speed_change_knots TEXT,
            time_gap_hours TEXT,
            anomaly_score TEXT,
            anomaly_flag TEXT,
            anomaly_status TEXT,
            region_iso3 TEXT,
            region_id TEXT,
            region_label TEXT,
            source_dataset TEXT
        )""")
        insert_sql = """INSERT INTO observations (
            date_day,date,mmsi,lat,lon,shipName,vesselType,speed_knots,
            speed_change_knots,time_gap_hours,anomaly_score,anomaly_flag,
            anomaly_status,region_iso3,region_id,region_label,source_dataset
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        batch: list[tuple[Any, ...]] = []
        loaded = 0
        min_day = ""
        max_day = ""
        regions_seen: set[str] = set()
        with GLOBAL_ANOMALY_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"date", "mmsi", "anomaly_flag", "anomaly_score"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"Global anomaly CSV is missing required columns: {sorted(missing)}")
            for row in reader:
                date_value = str(row.get("date") or "").strip()
                date_day = day_text(date_value)
                if date_day:
                    min_day = min(min_day, date_day) if min_day else date_day
                    max_day = max(max_day, date_day) if max_day else date_day
                region_value = str(row.get("region_iso3") or "").strip().upper()
                if region_value:
                    regions_seen.add(region_value)
                lat_pair = valid_lat_lon(row.get("lat"), row.get("lon"))
                lat = lat_pair[0] if lat_pair else None
                lon = lat_pair[1] if lat_pair else None
                values = (
                    date_day, date_value, str(row.get("mmsi") or "").strip(), lat, lon,
                    str(row.get("shipName") or ""), str(row.get("vesselType") or ""),
                    str(row.get("speed_knots") or ""), str(row.get("speed_change_knots") or ""),
                    str(row.get("time_gap_hours") or ""), str(row.get("anomaly_score") or ""),
                    str(row.get("anomaly_flag") or ""), str(row.get("anomaly_status") or ""),
                    str(row.get("region_iso3") or ""), str(row.get("region_id") or ""),
                    str(row.get("region_label") or ""), str(row.get("source_dataset") or ""),
                )
                batch.append(values)
                if len(batch) >= 10000:
                    conn.executemany(insert_sql, batch)
                    loaded += len(batch)
                    batch.clear()
                    _set_global_cache_state(
                        rows_loaded=loaded,
                        source_bytes_read=min(source_size, handle.buffer.tell()),
                        elapsed_seconds=round(time.time() - started, 1),
                    )
            if batch:
                conn.executemany(insert_sql, batch)
                loaded += len(batch)
                batch.clear()
        _set_global_cache_state(
            state="indexing", rows_loaded=loaded, source_bytes_read=source_size,
            elapsed_seconds=round(time.time() - started, 1),
        )
        conn.execute("CREATE INDEX idx_obs_date_id ON observations(date_day, id DESC)")
        conn.execute("CREATE INDEX idx_obs_flag_date_id ON observations(anomaly_flag, date_day, id DESC)")
        conn.execute("CREATE INDEX idx_obs_date_region ON observations(date_day, region_iso3)")
        conn.execute("CREATE INDEX idx_obs_region_id ON observations(region_iso3, id)")
        conn.execute("CREATE INDEX idx_obs_mmsi ON observations(mmsi)")
        conn.execute("CREATE TABLE cache_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        metadata_values = {
            "source_signature": _global_source_signature(),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "row_count": str(loaded),
            "start_date": min_day,
            "end_date": max_day,
            "region_count": str(len(regions_seen)),
        }
        conn.executemany("INSERT INTO cache_metadata(key,value) VALUES(?,?)", metadata_values.items())
        conn.commit()
        conn.close()
        conn = None
        os.replace(GLOBAL_CACHE_BUILDING_DB, GLOBAL_CACHE_DB)
        _set_global_cache_state(
            state="ready", rows_loaded=loaded, source_bytes_read=source_size,
            elapsed_seconds=round(time.time() - started, 1), error=None,
        )
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            if GLOBAL_CACHE_BUILDING_DB.exists():
                GLOBAL_CACHE_BUILDING_DB.unlink()
        except OSError:
            pass
        _set_global_cache_state(
            state="error", elapsed_seconds=round(time.time() - started, 1),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        _GLOBAL_CACHE_BUILD_LOCK.release()


def _start_global_cache_build() -> dict[str, Any]:
    global _GLOBAL_CACHE_THREAD
    if not GLOBAL_ANOMALY_CSV.exists():
        return _global_cache_status_payload()
    if _global_cache_signature_matches():
        return _global_cache_status_payload()
    if _GLOBAL_CACHE_BUILD_LOCK.acquire(blocking=False):
        _GLOBAL_CACHE_THREAD = threading.Thread(
            target=_build_global_cache_worker, name="dark-fleet-global-cache-builder", daemon=True
        )
        _GLOBAL_CACHE_THREAD.start()
    return _global_cache_status_payload()


def _require_global_cache() -> None:
    if not GLOBAL_ANOMALY_CSV.exists():
        return
    status = _global_cache_status_payload()
    if not status.get("ready"):
        if status.get("state") == "error":
            raise HTTPException(status_code=500, detail={"message": "Global data index build failed", "error": status.get("error")})
        raise HTTPException(status_code=503, detail={
            "message": "Global data index is being prepared. Retry after /api/cache-status reports ready.",
            "cache_state": status.get("state"),
        })


def _global_db_connection():
    _require_global_cache()
    conn = _sqlite3.connect(str(GLOBAL_CACHE_DB), timeout=60)
    conn.row_factory = _sqlite3.Row
    return conn


def _sql_date_filter(start_text: str, end_text: str) -> tuple[str, list[Any]]:
    clauses = ["date_day <> ''"]
    params: list[Any] = []
    if start_text:
        clauses.append("date_day >= ?")
        params.append(start_text)
    if end_text:
        clauses.append("date_day <= ?")
        params.append(end_text)
    return " AND ".join(clauses), params


def _sqlite_row_to_observation(row: _sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result.pop("id", None)
    result.pop("date_day", None)
    return result


def _global_data_range() -> dict[str, Any]:
    with closing(_global_db_connection()) as conn:
        metadata = dict(conn.execute("SELECT key,value FROM cache_metadata").fetchall())
    return {
        "start_date": metadata.get("start_date", ""),
        "end_date": metadata.get("end_date", ""),
        "total_observations": int(metadata.get("row_count", "0")),
        "region_count": int(metadata.get("region_count", "0")),
    }


def _global_model_summary(start_text: str, end_text: str) -> dict[str, Any]:
    date_where, params = _sql_date_filter(start_text, end_text)
    with closing(_global_db_connection()) as conn:
        row = conn.execute(f"""SELECT COUNT(*),
            SUM(CASE WHEN anomaly_flag='-1' THEN 1 ELSE 0 END),
            COUNT(DISTINCT NULLIF(mmsi,''))
            FROM observations WHERE {date_where}""", params).fetchone()
    total = int(row[0] or 0)
    flagged = int(row[1] or 0)
    return {
        "total_observations": total, "flagged_observations": flagged,
        "baseline_observations": total - flagged, "unique_vessels": int(row[2] or 0),
    }


def _global_anomaly_page(
    start_text: str, end_text: str, status: str, search_text: str, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    date_where, params = _sql_date_filter(start_text, end_text)
    clauses = [date_where]
    if status == "REVIEW":
        clauses.append("anomaly_flag='-1'")
    elif status == "NORMAL":
        # Keep this exactly aligned with baseline_observations = total - flagged.
        clauses.append("anomaly_flag <> '-1'")
    if search_text:
        clauses.append("lower(mmsi || ' ' || shipName || ' ' || vesselType || ' ' || date || ' ' || region_iso3 || ' ' || region_label) LIKE ?")
        params.append(f"%{search_text.lower()}%")
    where_sql = " AND ".join(f"({clause})" for clause in clauses)
    with closing(_global_db_connection()) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM observations WHERE {where_sql}", params).fetchone()[0])
        if status == "ALL":
            # The source file was score-sorted, which made the ALL tab look like
            # a flagged-only feed. A stable multiplicative hash shuffles records
            # within each timestamp while keeping their real class prevalence.
            order_sql = "date DESC, ((id * 2654435761 + 1013904223) % 2147483647) ASC, id DESC"
        else:
            order_sql = "date DESC, CAST(anomaly_score AS REAL) DESC, mmsi DESC, id DESC"
        rows = conn.execute(f"""SELECT * FROM observations WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?""", [*params, limit, offset]).fetchall()
    return [_sqlite_row_to_observation(row) for row in rows], total


def _global_map_sample(limit: int, start_text: str, end_text: str) -> tuple[list[dict[str, Any]], int]:
    date_where, params = _sql_date_filter(start_text, end_text)
    with closing(_global_db_connection()) as conn:
        region_rows = conn.execute(f"""SELECT COALESCE(NULLIF(region_iso3,''),'UNKNOWN'), COUNT(*)
            FROM observations WHERE {date_where} AND lat IS NOT NULL AND lon IS NOT NULL
            GROUP BY COALESCE(NULLIF(region_iso3,''),'UNKNOWN')""", params).fetchall()
        region_counts = {str(row[0]): int(row[1]) for row in region_rows}
        total_valid = sum(region_counts.values())
        if total_valid == 0:
            return [], 0
        quotas = allocate_even_region_quotas(region_counts, limit)
        chosen: list[dict[str, Any]] = []
        for region, count in region_counts.items():
            quota = quotas.get(region, 0)
            if quota <= 0:
                continue
            targets = sorted(choose_even_indices(count, quota))
            placeholders = ",".join("?" for _ in targets)
            if region == "UNKNOWN":
                region_clause = "(region_iso3 IS NULL OR region_iso3='')"
                region_params: list[Any] = []
            else:
                region_clause = "region_iso3=?"
                region_params = [region]
            query = f"""WITH numbered AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY id)-1 AS position
                FROM observations
                WHERE {date_where} AND {region_clause} AND lat IS NOT NULL AND lon IS NOT NULL
            )
            SELECT o.* FROM numbered n JOIN observations o ON o.id=n.id
            WHERE n.position IN ({placeholders}) ORDER BY o.id"""
            rows = conn.execute(query, [*params, *region_params, *targets]).fetchall()
            chosen.extend(_sqlite_row_to_observation(row) for row in rows)
    chosen.sort(key=lambda row: (
        str(row.get("region_iso3") or "UNKNOWN"), safe_float(row.get("lat")),
        safe_float(row.get("lon")), str(row.get("date") or "")
    ))
    return chosen, total_valid


@app.get("/")
def root():
    return {"name": "DARK-FLEET API", "version": app.version, "docs": "/docs"}


@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "service": "DARK-FLEET API",
        "api_version": app.version,
        "global_anomaly_installed": GLOBAL_ANOMALY_CSV.exists(),
        "global_sar_installed": GLOBAL_SAR_CSV.exists(),
        "global_cache_state": _global_cache_status_payload().get("state"),
    }


@app.get("/api/cache-status")
def cache_status():
    return _global_cache_status_payload()


@app.post("/api/cache/prepare")
def prepare_cache():
    return _start_global_cache_build()


def _recent_grid_source_windows() -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """Read compact date/count metadata from the already-built recent-grid SQLite index."""
    if not RECENT_GRID_INDEX_DB.exists():
        return [], {"ais": 0, "sar": 0}, 0
    try:
        with closing(_sqlite3.connect(str(RECENT_GRID_INDEX_DB), timeout=10)) as conn:
            rows = conn.execute(
                "SELECT kind, temporal_resolution, MIN(day), MAX(day), COUNT(*), COUNT(DISTINCT region_iso3) "
                "FROM recent_grid_records GROUP BY kind, temporal_resolution"
            ).fetchall()
            region_count_row = conn.execute("SELECT COUNT(DISTINCT region_iso3) FROM recent_grid_records").fetchone()
            region_count = int(region_count_row[0] or 0) if region_count_row else 0
        windows: list[dict[str, Any]] = []
        totals = {"ais": 0, "sar": 0}
        regions: set[str] = set()
        for kind, temporal, start, end, count, region_count in rows:
            kind_text = str(kind or "").lower()
            if kind_text not in totals:
                continue
            totals[kind_text] += int(count or 0)
            windows.append({
                "source": f"recent_{kind_text}_grid",
                "label": "Recent AIS grid activity" if kind_text == "ais" else "Recent SAR grid activity",
                "start_date": start,
                "end_date": end,
                "records": int(count or 0),
                "granularity": f"HIGH-resolution grid-cell FLAG aggregates Â· {temporal or 'source temporal resolution'}",
                "identity_available": False,
                "temporal_windows": [{
                    "temporal_resolution": str(temporal or "UNKNOWN"),
                    "start_date": start,
                    "end_date": end,
                    "records": int(count or 0),
                }],
            })
        return windows, totals, region_count
    except Exception:
        # A missing or incompatible recent index must not prevent historical dashboard access.
        return [], {"ais": 0, "sar": 0}, 0


@app.get("/api/data-range")
def data_range():
    path = get_data_source()
    if path == GLOBAL_ANOMALY_CSV:
        _require_global_cache()
        bounds = _global_data_range()
    else:
        bounds = get_cached_data_range(path)
    is_global = path == GLOBAL_ANOMALY_CSV
    historical_window = {
        "source": "historical_vessel_anomaly",
        "label": "Historical vessel-level anomaly data",
        "start_date": bounds.get("start_date"),
        "end_date": bounds.get("end_date"),
        "records": int(bounds.get("total_observations", 0) or 0),
        "granularity": "Historical AIS-derived vessel observations with anomaly outputs",
        "identity_available": True,
        "temporal_windows": [{
            "temporal_resolution": "SOURCE_DATA",
            "start_date": bounds.get("start_date"),
            "end_date": bounds.get("end_date"),
            "records": int(bounds.get("total_observations", 0) or 0),
        }],
    }
    grid_windows, grid_totals, grid_regions = _recent_grid_source_windows()
    source_windows = ([historical_window] if historical_window["start_date"] and historical_window["end_date"] else []) + grid_windows
    starts = [str(item["start_date"]) for item in source_windows if item.get("start_date")]
    ends = [str(item["end_date"]) for item in source_windows if item.get("end_date")]
    unified_start = min(starts) if starts else bounds.get("start_date")
    unified_end = max(ends) if ends else bounds.get("end_date")
    return {
        **bounds,
        "start_date": unified_start,
        "end_date": unified_end,
        "historical_start_date": bounds.get("start_date"),
        "historical_end_date": bounds.get("end_date"),
        "source_windows": source_windows,
        "grid_totals": {"ais_grid_records": grid_totals["ais"], "sar_grid_records": grid_totals["sar"]},
        "grid_region_count": grid_regions,
        "source_file": path.name,
        "data_mode": "UNIFIED_MULTI_SOURCE_CALENDAR" if grid_windows else ("GLOBAL_MULTI_REGION_MODEL_OUTPUT" if is_global else "CURRENT_SAMPLE_MODEL_OUTPUT"),
        "is_global": is_global,
        "note": (
            "Unified calendar spans the collected historical vessel-level and recent grid-activity windows. "
            "Each source is queried and counted separately; dates between source windows may contain no records. "
            "Recent FLAG-grouped grid records do not contain individual MMSI identities and are not vessel-level anomaly scores."
        ),
    }


@app.get("/api/model-summary")
def model_summary(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    start_text, end_text = validate_range_for_request(start_date, end_date)
    path = get_data_source()
    is_global = path == GLOBAL_ANOMALY_CSV
    if is_global:
        summary = _global_model_summary(start_text, end_text)
    else:
        summary = get_cached_summary(path, start_text, end_text)
    return {
        **summary,
        "data_mode": "GLOBAL_MULTI_REGION_MODEL_OUTPUT" if is_global else "CURRENT_SAMPLE_MODEL_OUTPUT",
        "source_file": path.name,
        "start_date": start_text or None,
        "end_date": end_text or None,
        "note": "Anomaly flags are statistical indicators requiring human review, not proof of wrongdoing.",
    }


@app.get("/api/anomalies")
def list_anomalies(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=PAGE_OFFSET_SAFETY_CAP),
    status: Literal["ALL", "REVIEW", "NORMAL"] = "ALL",
    flagged_only: bool = False,
    search: str = "",
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    start_text, end_text = validate_range_for_request(start_date, end_date)
    path = get_data_source()
    if flagged_only:
        status = "REVIEW"
    is_global = path == GLOBAL_ANOMALY_CSV
    if is_global:
        _require_global_cache()
        rows, total = _global_anomaly_page(
            start_text, end_text, status, search.strip(), offset, limit
        )
    else:
        rows, total = get_anomaly_page(
            path, start_text, end_text, status, search.strip().lower(), offset, limit
        )
    return {
        "data_mode": "GLOBAL_MULTI_REGION_MODEL_OUTPUT" if is_global else "CURRENT_SAMPLE_MODEL_OUTPUT",
        "source_file": path.name,
        "total_matching": total,
        "total_returned": len(rows),
        "limit": limit,
        "offset": offset,
        "max_offset": PAGE_OFFSET_SAFETY_CAP,
        "observations": rows,
        "note": (
            "Global observations are ordered newest-first with a deterministic mixed flagged/baseline order "
            "within each timestamp. The feed preserves the dataset's natural class prevalence; use the "
            "Flagged and Baseline filters to isolate either class."
            if is_global and status == "ALL" else
            "Global, region- and date-scoped observations. Filtered rows are ordered newest-first; "
            "search or narrower date ranges are recommended for deeper exploration."
            if is_global else
            "Local sample observations. Anomaly flags are statistical review signals, not proof of wrongdoing."
        ),
    }


@app.get("/api/sar-ais-matches")
def sar_ais_matches(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    start_text, end_text = validate_range_for_request(start_date, end_date)
    rows = [row for row in iter_csv(SAR_AIS_CSV) if within_date_range(row, start_text, end_text)]
    total = len(rows)
    page_rows = rows[offset:offset + limit]
    return {
        "data_mode": "SAR_AIS_COMPARISON",
        "total_matching": total,
        "total_returned": len(page_rows),
        "limit": limit,
        "offset": offset,
        "observations": page_rows,
        "note": "Proximity-based AIS/SAR comparisons are provisional; a detection without a nearby AIS candidate does not establish wrongdoing.",
    }


@app.get("/api/map-observations")
def map_observations(
    limit: int = Query(default=6000, ge=100, le=15000),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    start_text, end_text = validate_range_for_request(start_date, end_date)
    source_path = get_data_source()
    is_global = source_path == GLOBAL_ANOMALY_CSV
    if is_global:
        _require_global_cache()
        selected, total_valid = _global_map_sample(limit, start_text, end_text)
    else:
        selected, total_valid = get_cached_map_sample(source_path, limit, start_text, end_text)
    return {
        "data_mode": "GLOBAL_MULTI_REGION_MODEL_OUTPUT" if is_global else "CURRENT_SAMPLE_MODEL_OUTPUT",
        "source_file": source_path.name,
        "total_matching": total_valid,
        "total_returned": len(selected),
        "sampled_for_map": total_valid > len(selected),
        "limit": limit,
        "start_date": start_text or None,
        "end_date": end_text or None,
        "observations": selected,
        "note": "Map markers are a region-balanced sample of valid coordinates in the selected date range. Coverage is limited to collected regions and dates.",
    }


@app.get("/api/map-sar-detections")
def map_sar_detections(
    limit: int = Query(default=6000, ge=100, le=15000),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
):
    start_text, end_text = validate_range_for_request(start_date, end_date)
    combined: list[dict[str, str]] = list(iter_csv(SAR_AIS_CSV))
    has_global = GLOBAL_SAR_CSV.exists()

    if has_global:
        for raw in iter_csv(GLOBAL_SAR_CSV):
            region = (raw.get("region_iso3") or "").strip().upper()
            # The local Chile comparison is retained because it has candidate distances.
            if region == "CHL":
                continue
            row = dict(raw)
            row["sar_lat"] = row.get("lat", "")
            row["sar_lon"] = row.get("lon", "")
            row["sar_time"] = row.get("entryTimestamp") or row.get("date", "")
            row["association_status"] = "association_not_verified"
            row["nearest_mmsi"] = ""
            row["nearest_ship"] = ""
            row["distance_km"] = ""
            row["time_diff_min"] = ""
            combined.append(row)

    valid_rows = []
    for row in combined:
        if not within_date_range(row, start_text, end_text):
            continue
        lat = row.get("sar_lat") if row.get("sar_lat") not in (None, "") else row.get("lat")
        lon = row.get("sar_lon") if row.get("sar_lon") not in (None, "") else row.get("lon")
        if valid_lat_lon(lat, lon) is not None:
            valid_rows.append(row)

    total_valid = len(valid_rows)
    if total_valid > limit:
        by_region: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in valid_rows:
            by_region[get_map_region(row)].append(row)
        quotas = allocate_even_region_quotas(
            {region: len(items) for region, items in by_region.items()}, limit
        )
        sampled_rows: list[dict[str, str]] = []
        for region in sorted(by_region):
            items = by_region[region]
            chosen_indices = choose_even_indices(len(items), quotas.get(region, 0))
            sampled_rows.extend(items[index] for index in sorted(chosen_indices))
        valid_rows = sampled_rows

    return {
        "data_mode": "LOCAL_COMPARISON_PLUS_GLOBAL_UNVERIFIED_SAR" if has_global else "LOCAL_SAR_AIS_COMPARISON",
        "total_matching": total_valid,
        "total_returned": len(valid_rows),
        "sampled_for_map": total_valid > len(valid_rows),
        "start_date": start_text or None,
        "end_date": end_text or None,
        "observations": valid_rows,
        "note": "Global non-Chile SAR detections are unverified until AIS association is performed. Chile rows use the existing provisional proximity comparison.",
    }


# -----------------------------------------------------------------------------
# Phase 3 â€” high-resolution SAR/AIS candidate screening integration
# The new files are optional: the existing dashboard remains operational when
# the Phase 3 CSVs have not yet been copied into the local project.
# -----------------------------------------------------------------------------
import json as _json
import sqlite3 as _sqlite3
import threading as _threading

PHASE3_DATA_DIR = DATA_DIR / "phase3"
PHASE3_CANDIDATES_CSV = PHASE3_DATA_DIR / "dark_fleet_phase3_sar_ais_candidates.csv"
PHASE3_SUMMARY_CSV = PHASE3_DATA_DIR / "dark_fleet_phase3_sar_ais_summary.csv"
PHASE3_SENSITIVITY_CSV = PHASE3_DATA_DIR / "dark_fleet_phase3_sensitivity_analysis.csv"
PHASE3_INDEX_DB = PHASE3_DATA_DIR / "phase3_candidates.sqlite3"
_PHASE3_INDEX_LOCK = _threading.Lock()

PHASE3_SCENARIOS: dict[str, tuple[float, float]] = {
    "STRICT": (5.0, 30.0),
    "STANDARD": (10.0, 60.0),
    "BROAD": (20.0, 180.0),
}


def _phase3_file_signature(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size


def _phase3_files_ready() -> tuple[bool, list[str]]:
    missing = [
        path.name for path in (PHASE3_CANDIDATES_CSV, PHASE3_SUMMARY_CSV, PHASE3_SENSITIVITY_CSV)
        if not path.exists()
    ]
    return not missing, missing


@lru_cache(maxsize=4)
def _cached_phase3_rows(path_text: str, modified_ns: int, file_size: int) -> tuple[dict[str, str], ...]:
    path = Path(path_text)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return tuple(dict(row) for row in csv.DictReader(handle))


def _read_phase3_cached(path: Path) -> list[dict[str, str]]:
    path_text, modified_ns, file_size = _phase3_file_signature(path)
    return list(_cached_phase3_rows(path_text, modified_ns, file_size))


def _require_phase3_files() -> None:
    ready, missing = _phase3_files_ready()
    if not ready:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Phase 3 result files are not installed in the backend yet.",
                "expected_directory": str(PHASE3_DATA_DIR),
                "missing_files": missing,
            },
        )


def _phase3_summary_filtered(start_text: str = "", end_text: str = "", region_iso3: str = "") -> list[dict[str, str]]:
    rows = _read_phase3_cached(PHASE3_SUMMARY_CSV)
    region_filter = region_iso3.strip().upper()
    output = []
    for row in rows:
        row_date = day_text(row.get("sar_date"))
        if start_text and (not row_date or row_date < start_text):
            continue
        if end_text and (not row_date or row_date > end_text):
            continue
        if region_filter and str(row.get("sar_region_iso3", "")).strip().upper() != region_filter:
            continue
        output.append(row)
    return output


def _phase3_candidate_index_signature() -> str:
    stat = PHASE3_CANDIDATES_CSV.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _ensure_phase3_candidate_index() -> dict[str, Any]:
    """Build a disk-backed indexed copy once; subsequent SAR detail lookups are indexed."""
    PHASE3_DATA_DIR.mkdir(parents=True, exist_ok=True)
    signature = _phase3_candidate_index_signature()
    with _PHASE3_INDEX_LOCK:
        if PHASE3_INDEX_DB.exists():
            try:
                with _sqlite3.connect(str(PHASE3_INDEX_DB)) as existing:
                    row = existing.execute("SELECT value FROM metadata WHERE key='source_signature'").fetchone()
                    count_row = existing.execute("SELECT COUNT(*) FROM candidates").fetchone()
                    if row and row[0] == signature:
                        return {"ready": True, "rows_indexed": int(count_row[0]), "rebuilt": False}
            except (_sqlite3.Error, OSError):
                pass

        building = PHASE3_INDEX_DB.with_name(PHASE3_INDEX_DB.name + ".building")
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(building) + suffix)
            if candidate.exists():
                candidate.unlink()

        conn = _sqlite3.connect(str(building))
        inserted = 0
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("""CREATE TABLE candidates (
                sar_record_id TEXT NOT NULL,
                candidate_rank INTEGER NOT NULL,
                candidate_mmsi TEXT NOT NULL,
                distance_km REAL NOT NULL,
                time_gap_min REAL NOT NULL,
                ranking_score REAL NOT NULL,
                row_json TEXT NOT NULL
            )""")
            batch: list[tuple[Any, ...]] = []
            with PHASE3_CANDIDATES_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                required = {"sar_record_id", "candidate_mmsi", "distance_km", "time_gap_min", "ranking_score"}
                missing = required - set(reader.fieldnames or [])
                if missing:
                    raise ValueError(f"Phase 3 candidate CSV is missing required columns: {sorted(missing)}")
                for row in reader:
                    sar_id = str(row.get("sar_record_id", "")).strip()
                    mmsi = str(row.get("candidate_mmsi", "")).strip()
                    if not sar_id or not mmsi:
                        continue
                    distance = safe_float(row.get("distance_km"), float("nan"))
                    time_gap = safe_float(row.get("time_gap_min"), float("nan"))
                    base_score = safe_float(row.get("ranking_score"), float("nan"))
                    if not all(math.isfinite(x) for x in (distance, time_gap, base_score)):
                        continue
                    rank = int(safe_float(row.get("candidate_rank"), 0))
                    batch.append((sar_id, rank, mmsi, distance, time_gap, base_score,
                                  _json.dumps(row, ensure_ascii=False, separators=(",", ":"))))
                    if len(batch) >= 5000:
                        conn.executemany("INSERT INTO candidates VALUES (?,?,?,?,?,?,?)", batch)
                        inserted += len(batch)
                        batch.clear()
                if batch:
                    conn.executemany("INSERT INTO candidates VALUES (?,?,?,?,?,?,?)", batch)
                    inserted += len(batch)
            conn.execute("CREATE INDEX idx_phase3_sar_id ON candidates(sar_record_id)")
            conn.execute("CREATE INDEX idx_phase3_sar_rank ON candidates(sar_record_id, candidate_rank)")
            conn.execute("INSERT INTO metadata(key,value) VALUES('source_signature',?)", (signature,))
            conn.commit()
        except Exception:
            conn.close()
            if building.exists():
                building.unlink()
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
        # Replace atomically after the database is closed.
        if PHASE3_INDEX_DB.exists():
            PHASE3_INDEX_DB.unlink()
        building.replace(PHASE3_INDEX_DB)
        return {"ready": True, "rows_indexed": inserted, "rebuilt": True}


@app.get("/api/phase3/status")
def phase3_status():
    ready, missing = _phase3_files_ready()
    payload: dict[str, Any] = {
        "ready": ready,
        "data_directory": str(PHASE3_DATA_DIR),
        "missing_files": missing,
        "files": {
            path.name: {
                "exists": path.exists(),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 2) if path.exists() else 0,
            }
            for path in (PHASE3_CANDIDATES_CSV, PHASE3_SUMMARY_CSV, PHASE3_SENSITIVITY_CSV)
        },
        "candidate_index_ready": PHASE3_INDEX_DB.exists(),
        "data_resolution": "HIGH (0.01-degree GFW grid)",
        "pilot_regions": 10,
        "data_window": {"start": "2022-01-01", "end_exclusive": "2022-01-06"},
        "note": "High-resolution grid-cell presence screening. Candidate proximity is not a confirmed vessel identity.",
    }
    if ready:
        try:
            summary_rows = _read_phase3_cached(PHASE3_SUMMARY_CSV)
            payload["sar_presence_records"] = len(summary_rows)
            payload["candidate_pairs_broad"] = sum(
                int(safe_float(row.get("candidate_count_in_search_envelope"), 0)) for row in summary_rows
            )
        except Exception as exc:
            payload["summary_read_error"] = str(exc)
    return payload


@app.get("/api/phase3/overview")
def phase3_overview(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    region_iso3: str = "",
):
    _require_phase3_files()
    start_text, end_text = validate_range_for_request(start_date, end_date)
    rows = _phase3_summary_filtered(start_text, end_text, region_iso3)
    total = len(rows)
    found_rows = [row for row in rows if row.get("association_status") == "independent_candidate_found"]
    no_candidate_rows = [row for row in rows if row.get("association_status") == "no_candidate_within_search_envelope"]
    candidate_pairs = sum(int(safe_float(row.get("candidate_count_in_search_envelope"), 0)) for row in rows)
    regions = Counter(str(row.get("sar_region_iso3", "UNKNOWN")).strip().upper() or "UNKNOWN" for row in rows)
    sensitivity = _read_phase3_cached(PHASE3_SENSITIVITY_CSV)
    return {
        "data_mode": "PHASE3_HIGH_RESOLUTION_INDEPENDENT_CANDIDATE_SCREENING",
        "data_resolution": "HIGH (0.01-degree GFW grid)",
        "total_sar_presence_records": total,
        "records_with_candidates": len(found_rows),
        "records_without_candidates": len(no_candidate_rows),
        "candidate_record_rate_pct": round(100 * len(found_rows) / total, 3) if total else 0,
        "candidate_pairs_broad": candidate_pairs,
        "region_count": len(regions),
        "region_counts": dict(sorted(regions.items())),
        "start_date": start_text or None,
        "end_date": end_text or None,
        "sensitivity": sensitivity,
        "scenarios": {
            name: {"max_distance_km": distance, "max_time_gap_min": gap}
            for name, (distance, gap) in PHASE3_SCENARIOS.items()
        },
        "note": "Candidate record rates and pair counts measure screening availability only, not precision, recall, accuracy, probability, or confirmed identity.",
    }


@app.get("/api/phase3/sar-records")
def phase3_sar_records(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    region_iso3: str = "",
    association_status: Literal["ALL", "HAS_CANDIDATES", "NO_CANDIDATES"] = "ALL",
    search: str = "",
):
    _require_phase3_files()
    start_text, end_text = validate_range_for_request(start_date, end_date)
    rows = _phase3_summary_filtered(start_text, end_text, region_iso3)
    term = search.strip().lower()
    filtered = []
    for row in rows:
        has_candidates = row.get("association_status") == "independent_candidate_found"
        if association_status == "HAS_CANDIDATES" and not has_candidates:
            continue
        if association_status == "NO_CANDIDATES" and has_candidates:
            continue
        if term:
            haystack = " ".join(str(row.get(k, "")) for k in (
                "sar_record_id", "sar_region_iso3", "sar_region_label", "sar_date",
                "nearest_mmsi", "nearest_ship_name", "nearest_region_iso3"
            )).lower()
            if term not in haystack:
                continue
        filtered.append(row)
    filtered.sort(key=lambda row: (str(row.get("sar_date", "")), str(row.get("sar_record_id", ""))), reverse=True)
    page_rows = filtered[offset:offset + limit]
    return {
        "total_matching": len(filtered), "total_returned": len(page_rows),
        "limit": limit, "offset": offset, "observations": page_rows,
        "note": "One row per SAR presence record. The nearest candidate is a ranked proximity candidate, not a confirmed identity.",
    }



def _parse_phase3_utc_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp into UTC. Naive values are treated as UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _phase3_temporal_context(row: dict[str, Any]) -> dict[str, Any]:
    """Describe AIS time relative to the SAR acquisition interval, not as an exact event-time gap."""
    start = _parse_phase3_utc_timestamp(row.get("sar_entry_timestamp"))
    end = _parse_phase3_utc_timestamp(row.get("sar_exit_timestamp"))
    ais_time = _parse_phase3_utc_timestamp(row.get("candidate_observation_time"))

    if start is None and end is not None:
        start = end
    if end is None and start is not None:
        end = start
    if start is None or end is None or ais_time is None:
        return {
            "temporal_relation": "UNKNOWN",
            "acquisition_window_duration_min": None,
            "outside_window_gap_min": None,
            "temporal_interpretation": (
                "The SAR acquisition interval or AIS observation timestamp could not be parsed; "
                "temporal compatibility is unavailable."
            ),
        }

    if end < start:
        start, end = end, start
    duration_min = max(0.0, (end - start).total_seconds() / 60.0)

    if ais_time < start:
        gap_min = (start - ais_time).total_seconds() / 60.0
        relation = "BEFORE_ACQUISITION_WINDOW"
        interpretation = (
            f"AIS presence timestamp is {gap_min:.1f} minutes before the SAR acquisition window. "
            "This is interval offset, not a measured vessel-to-image event-time difference."
        )
    elif ais_time > end:
        gap_min = (ais_time - end).total_seconds() / 60.0
        relation = "AFTER_ACQUISITION_WINDOW"
        interpretation = (
            f"AIS presence timestamp is {gap_min:.1f} minutes after the SAR acquisition window. "
            "This is interval offset, not a measured vessel-to-image event-time difference."
        )
    else:
        gap_min = 0.0
        relation = "WITHIN_ACQUISITION_WINDOW"
        interpretation = (
            "AIS presence timestamp falls inside the SAR acquisition interval. A zero-minute "
            "interval gap means temporal overlap with the window; it does not mean exact "
            "coincidence with an individual SAR detection time."
        )

    return {
        "temporal_relation": relation,
        "acquisition_window_duration_min": round(duration_min, 2),
        "outside_window_gap_min": round(gap_min, 2),
        "temporal_interpretation": interpretation,
    }


@app.get("/api/phase3/sar-records/{sar_record_id}/candidates")
def phase3_sar_record_candidates(
    sar_record_id: str,
    scenario: str = "STANDARD",
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
):
    _require_phase3_files()
    scenario_key = scenario.strip().upper()
    if scenario_key not in PHASE3_SCENARIOS:
        raise HTTPException(status_code=422, detail=f"scenario must be one of {', '.join(PHASE3_SCENARIOS)}")
    max_distance_km, max_time_gap_min = PHASE3_SCENARIOS[scenario_key]
    distance_weight = 0.65
    time_weight = 0.35
    index_info = _ensure_phase3_candidate_index()
    with _sqlite3.connect(str(PHASE3_INDEX_DB)) as conn:
        conn.row_factory = _sqlite3.Row
        where = "sar_record_id=? AND distance_km<=? AND time_gap_min<=?"
        params = (sar_record_id, max_distance_km, max_time_gap_min)
        total = int(conn.execute(f"SELECT COUNT(*) FROM candidates WHERE {where}", params).fetchone()[0])
        score_expr = "(? * (1.0 - distance_km / ?) + ? * (1.0 - time_gap_min / ?))"
        order_sql = f"""{score_expr} DESC, distance_km ASC, time_gap_min ASC, candidate_mmsi ASC"""
        # Fetch the top two independently of pagination so the explanation always
        # describes the true first and second candidates for this SAR record.
        top_rows = conn.execute(
            f"SELECT row_json, {score_expr} AS scenario_score FROM candidates WHERE {where} ORDER BY {order_sql} LIMIT 2",
            (distance_weight, max_distance_km, time_weight, max_time_gap_min,
             sar_record_id, max_distance_km, max_time_gap_min,
             distance_weight, max_distance_km, time_weight, max_time_gap_min),
        ).fetchall()
        query = f"""
            SELECT row_json, {score_expr} AS scenario_score
            FROM candidates WHERE {where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        result_rows = conn.execute(
            query,
            (distance_weight, max_distance_km, time_weight, max_time_gap_min,
             sar_record_id, max_distance_km, max_time_gap_min,
             distance_weight, max_distance_km, time_weight, max_time_gap_min,
             limit, offset),
        ).fetchall()

    def score_parts(distance_value: float, time_value: float) -> tuple[float, float, float]:
        distance_part = distance_weight * max(0.0, 1.0 - distance_value / max_distance_km)
        time_part = time_weight * max(0.0, 1.0 - time_value / max_time_gap_min)
        return distance_part, time_part, distance_part + time_part

    candidates = []
    for item in result_rows:
        row = _json.loads(item["row_json"])
        distance_value = float(row["distance_km"])
        time_value = float(row["time_gap_min"])
        distance_part, time_part, combined_score = score_parts(distance_value, time_value)
        row["scenario"] = scenario_key
        row["scenario_ranking_score"] = round(float(item["scenario_score"]), 4)
        row["distance_score_contribution"] = round(distance_part, 4)
        row["time_score_contribution"] = round(time_part, 4)
        row["distance_limit_utilization_pct"] = round(100.0 * distance_value / max_distance_km, 2)
        row["time_limit_utilization_pct"] = round(100.0 * time_value / max_time_gap_min, 2)
        row["score_formula"] = (
            "0.65 Ã— normalized distance fit + 0.35 Ã— acquisition-window compatibility"
        )
        row.update(_phase3_temporal_context(row))
        candidates.append(row)

    top_score_values = [float(item["scenario_score"]) for item in top_rows]
    top_score = top_score_values[0] if top_score_values else None
    runner_up_score = top_score_values[1] if len(top_score_values) > 1 else None
    top_gap = (top_score - runner_up_score) if top_score is not None and runner_up_score is not None else None
    top_explanation = None
    if top_rows:
        top_row = _json.loads(top_rows[0]["row_json"])
        d_part, t_part, _ = score_parts(float(top_row["distance_km"]), float(top_row["time_gap_min"]))
        top_explanation = {
            "candidate_mmsi": str(top_row.get("candidate_mmsi", "")),
            "distance_km": float(top_row["distance_km"]),
            "time_gap_min": float(top_row["time_gap_min"]),
            "distance_score_contribution": round(d_part, 4),
            "time_score_contribution": round(t_part, 4),
            "scenario_ranking_score": round(float(top_score), 4),
            **_phase3_temporal_context(top_row),
        }

    ranking_diagnostics = {
        "method": "weighted_space_time_screening_score",
        "distance_weight": distance_weight,
        "time_weight": time_weight,
        "score_range": [0.0, 1.0],
        "top_candidate": top_explanation,
        "runner_up_score": round(runner_up_score, 4) if runner_up_score is not None else None,
        "top_two_score_gap": round(top_gap, 4) if top_gap is not None else None,
        "temporal_basis": (
            "Temporal compatibility is measured against the full SAR acquisition interval. "
            "An AIS timestamp inside the interval has a zero interval gap and receives full "
            "temporal contribution; this is window overlap, not exact detection-time coincidence."
        ),
        "interpretation": (
            "The score gap describes separation between the two highest heuristic ranks only. "
            "It is not association confidence, probability, precision, or evidence of vessel identity."
        ),
    }
    return {
        "sar_record_id": sar_record_id,
        "scenario": scenario_key,
        "max_distance_km": max_distance_km,
        "max_time_gap_min": max_time_gap_min,
        "total_matching": total,
        "total_returned": len(candidates),
        "limit": limit,
        "offset": offset,
        "index": index_info,
        "ranking_diagnostics": ranking_diagnostics,
        "candidates": candidates,
        "note": (
            "The scenario score combines spatial fit with compatibility against the SAR acquisition "
            "interval. A zero interval gap means the AIS timestamp falls within the window, not "
            "that it exactly coincides with an individual SAR detection. Scores are not calibrated "
            "probabilities, precision, recall, or confirmed identities."
        ),
    }


# -----------------------------------------------------------------------------
# Recent GFW grid activity (v0.8.2)
# Kept separate from MMSI-level/historical model outputs: these are FLAG-grouped
# grid-cell-hour aggregates and do not carry individual vessel identity.
# -----------------------------------------------------------------------------
_RECENT_GRID_LOCK = threading.Lock()
_RECENT_GRID_STATE_LOCK = threading.Lock()
_RECENT_GRID_STATE: dict[str, Any] = {
    "state": "not_checked", "ready": False, "rows_loaded": 0,
    "ais_rows_loaded": 0, "sar_rows_loaded": 0, "error": "",
    "source_signature": "", "started_utc": None, "completed_utc": None,
}
_RECENT_GRID_WORKER: threading.Thread | None = None


def _recent_grid_source_signature() -> str:
    parts = []
    for path in (RECENT_GRID_AIS_CSV, RECENT_GRID_SAR_CSV):
        if not path.exists():
            return ""
        stat = path.stat()
        parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def _recent_grid_files_ready() -> tuple[bool, list[str]]:
    missing = [p.name for p in (RECENT_GRID_AIS_CSV, RECENT_GRID_SAR_CSV) if not p.exists()]
    return not missing, missing


def _recent_grid_db_meta() -> dict[str, str]:
    if not RECENT_GRID_INDEX_DB.exists():
        return {}
    try:
        with closing(_sqlite3.connect(str(RECENT_GRID_INDEX_DB), timeout=5)) as conn:
            rows = conn.execute("SELECT key, value FROM recent_grid_meta").fetchall()
        return {str(k): str(v) for k, v in rows}
    except Exception:
        return {}


def _recent_grid_status_payload() -> dict[str, Any]:
    files_ready, missing = _recent_grid_files_ready()
    signature = _recent_grid_source_signature() if files_ready else ""
    meta = _recent_grid_db_meta()
    cache_ready = bool(meta.get("worker_managed") == "true" or (signature and meta.get("source_signature") == signature))
    with _RECENT_GRID_STATE_LOCK:
        state = dict(_RECENT_GRID_STATE)
    if cache_ready:
        state.update({"state": "ready", "ready": True, "error": ""})
        state["ais_rows_loaded"] = int(meta.get("ais_rows", "0") or 0)
        state["sar_rows_loaded"] = int(meta.get("sar_rows", "0") or 0)
        state["rows_loaded"] = state["ais_rows_loaded"] + state["sar_rows_loaded"]
        state["completed_utc"] = meta.get("built_utc")
        state["worker_managed"] = meta.get("worker_managed") == "true"
    elif not files_ready:
        state.update({"state": "missing_files", "ready": False})
    elif state.get("state") == "ready":
        state.update({"state": "stale_index", "ready": False})
    last_ingestion: dict[str, Any] = {}
    if RECENT_GRID_RUN_JSON.exists():
        try:
            with RECENT_GRID_RUN_JSON.open("r", encoding="utf-8") as handle:
                run_payload = json.load(handle)
            last_ingestion = {
                "pipeline_version": run_payload.get("pipeline_version"),
                "safe_end_date_exclusive_utc": run_payload.get("safe_end_date_exclusive_utc"),
                "safe_lag_days": run_payload.get("safe_lag_days"),
                "successful_reports_this_run": run_payload.get("successful_reports_this_run"),
                "failed_reports_this_run": run_payload.get("failed_reports_this_run"),
                "identity_available": run_payload.get("identity_available", False),
            }
        except Exception:
            last_ingestion = {"warning": "The optional recent ingestion run summary could not be read."}
    return {
        **state,
        "last_ingestion": last_ingestion,
        "files_ready": files_ready,
        "missing_files": missing,
        "files": {
            p.name: {"exists": p.exists(), "size_mb": round(p.stat().st_size / (1024 * 1024), 2) if p.exists() else 0}
            for p in (RECENT_GRID_AIS_CSV, RECENT_GRID_SAR_CSV)
        },
        "index_file": str(RECENT_GRID_INDEX_DB),
        "identity_available": False,
        "record_granularity": "GRID_CELL_FLAG_HOUR",
        "note": "Recent records are hourly FLAG-grouped grid-cell aggregates, not individual vessel observations or MMSI identities.",
    }


def _recent_grid_build_worker() -> None:
    started = datetime.now(timezone.utc).isoformat()
    with _RECENT_GRID_STATE_LOCK:
        _RECENT_GRID_STATE.update({
            "state": "building", "ready": False, "rows_loaded": 0,
            "ais_rows_loaded": 0, "sar_rows_loaded": 0, "error": "",
            "started_utc": started, "completed_utc": None,
        })
    try:
        files_ready, missing = _recent_grid_files_ready()
        if not files_ready:
            raise FileNotFoundError("Missing recent grid source files: " + ", ".join(missing))
        signature = _recent_grid_source_signature()
        RECENT_GRID_DIR.mkdir(parents=True, exist_ok=True)
        with closing(_sqlite3.connect(str(RECENT_GRID_INDEX_DB), timeout=60)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("DROP TABLE IF EXISTS recent_grid_records")
            conn.execute("DROP TABLE IF EXISTS recent_grid_meta")
            conn.execute("""CREATE TABLE recent_grid_records (
                kind TEXT NOT NULL,
                record_key TEXT NOT NULL,
                day TEXT NOT NULL,
                date_text TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                flag TEXT NOT NULL,
                hours REAL,
                vessel_ids REAL,
                detections REAL,
                region_iso3 TEXT NOT NULL,
                region_id TEXT,
                region_label TEXT,
                spatial_resolution TEXT,
                temporal_resolution TEXT,
                source_dataset TEXT,
                sample_key INTEGER NOT NULL,
                PRIMARY KEY(kind, record_key)
            ) WITHOUT ROWID""")
            conn.execute("CREATE TABLE recent_grid_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            insert_sql = """INSERT OR IGNORE INTO recent_grid_records
                (kind, record_key, day, date_text, lat, lon, flag, hours, vessel_ids,
                 detections, region_iso3, region_id, region_label, spatial_resolution,
                 temporal_resolution, source_dataset, sample_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            counts = {"ais": 0, "sar": 0}
            invalid = {"ais": 0, "sar": 0}
            batch: list[tuple[Any, ...]] = []
            rows_loaded = 0
            for kind, path in (("ais", RECENT_GRID_AIS_CSV), ("sar", RECENT_GRID_SAR_CSV)):
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    required = {"date", "lat", "lon", "region_iso3"}
                    if not required.issubset(set(reader.fieldnames or [])):
                        raise ValueError(f"{path.name} is missing required columns: {sorted(required - set(reader.fieldnames or []))}")
                    for row in reader:
                        date_text = str(row.get("date", "") or "").strip()
                        day = day_text(date_text)
                        position = valid_lat_lon(row.get("lat"), row.get("lon"))
                        region = str(row.get("region_iso3", "") or "").strip().upper()
                        if not day or position is None or not region:
                            invalid[kind] += 1
                            continue
                        try:
                            date.fromisoformat(day)
                        except ValueError:
                            invalid[kind] += 1
                            continue
                        lat, lon = position
                        flag = str(row.get("flag", "") or "").strip().upper()
                        record_key = f"{region}|{date_text}|{lat:.5f}|{lon:.5f}|{flag}"
                        sample_key = zlib.crc32(f"{kind}|{record_key}".encode("utf-8")) & 0xFFFFFFFF
                        batch.append((
                            kind, record_key, day, date_text, lat, lon, flag,
                            safe_float(row.get("hours"), 0.0) if row.get("hours") not in (None, "") else None,
                            safe_float(row.get("vesselIDs"), 0.0) if row.get("vesselIDs") not in (None, "") else None,
                            safe_float(row.get("detections"), 0.0) if row.get("detections") not in (None, "") else None,
                            region, str(row.get("region_id", "") or ""),
                            str(row.get("region_label", "") or ""),
                            str(row.get("spatial_resolution", "HIGH") or "HIGH"),
                            str(row.get("temporal_resolution", "HOURLY") or "HOURLY"),
                            str(row.get("source_dataset", "") or ""), sample_key,
                        ))
                        if len(batch) >= 5000:
                            before = conn.total_changes
                            conn.executemany(insert_sql, batch)
                            inserted = conn.total_changes - before
                            counts[kind] += inserted
                            rows_loaded += len(batch)
                            batch.clear()
                            conn.commit()
                            with _RECENT_GRID_STATE_LOCK:
                                _RECENT_GRID_STATE.update({
                                    "rows_loaded": rows_loaded,
                                    "ais_rows_loaded": counts["ais"],
                                    "sar_rows_loaded": counts["sar"],
                                    "state": "building",
                                })
                if batch:
                    before = conn.total_changes
                    conn.executemany(insert_sql, batch)
                    inserted = conn.total_changes - before
                    counts[kind] += inserted
                    rows_loaded += len(batch)
                    batch.clear()
                    conn.commit()
                    with _RECENT_GRID_STATE_LOCK:
                        _RECENT_GRID_STATE.update({
                            "rows_loaded": rows_loaded,
                            "ais_rows_loaded": counts["ais"],
                            "sar_rows_loaded": counts["sar"],
                            "state": "building",
                        })
            conn.execute("CREATE INDEX idx_recent_kind_day_region ON recent_grid_records(kind, day, region_iso3)")
            conn.execute("CREATE INDEX idx_recent_kind_region_day ON recent_grid_records(kind, region_iso3, day)")
            conn.execute("CREATE INDEX idx_recent_kind_region_day_sample ON recent_grid_records(kind, region_iso3, day, sample_key)")
            built = datetime.now(timezone.utc).isoformat()
            meta_values = {
                "source_signature": signature,
                "ais_rows": str(counts["ais"]),
                "sar_rows": str(counts["sar"]),
                "ais_invalid_rows": str(invalid["ais"]),
                "sar_invalid_rows": str(invalid["sar"]),
                "built_utc": built,
            }
            conn.executemany("INSERT INTO recent_grid_meta(key,value) VALUES (?,?)", list(meta_values.items()))
            conn.commit()
            conn.execute("PRAGMA optimize")
        with _RECENT_GRID_STATE_LOCK:
            _RECENT_GRID_STATE.update({
                "state": "ready", "ready": True, "rows_loaded": counts["ais"] + counts["sar"],
                "ais_rows_loaded": counts["ais"], "sar_rows_loaded": counts["sar"],
                "source_signature": signature, "completed_utc": built, "error": "",
                "invalid_ais_rows": invalid["ais"], "invalid_sar_rows": invalid["sar"],
            })
    except Exception as exc:
        with _RECENT_GRID_STATE_LOCK:
            _RECENT_GRID_STATE.update({"state": "error", "ready": False, "error": str(exc)})


def _recent_grid_connection():
    if not RECENT_GRID_INDEX_DB.exists():
        raise HTTPException(status_code=409, detail="Recent grid index has not been prepared. Use POST /api/recent-grid/prepare.")
    meta = _recent_grid_db_meta()
    if not meta or (meta.get("worker_managed") != "true" and meta.get("source_signature") != _recent_grid_source_signature()):
        raise HTTPException(status_code=409, detail="Recent grid index is missing or stale. Prepare the local index again.")
    conn = _sqlite3.connect(str(RECENT_GRID_INDEX_DB), timeout=30)
    conn.row_factory = _sqlite3.Row
    return conn


def _recent_grid_filters(kind: str | None, start_text: str, end_text: str, region_iso3: str) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if start_text:
        clauses.append("day>=?")
        params.append(start_text)
    if end_text:
        clauses.append("day<=?")
        params.append(end_text)
    if region_iso3:
        clauses.append("region_iso3=?")
        params.append(region_iso3.strip().upper())
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


@app.get("/api/recent-grid/status")
def recent_grid_status():
    return _recent_grid_status_payload()


@app.post("/api/recent-grid/prepare")
def recent_grid_prepare():
    global _RECENT_GRID_WORKER
    files_ready, missing = _recent_grid_files_ready()
    if not files_ready:
        raise HTTPException(status_code=404, detail={
            "message": "Copy the recent grid AIS and SAR CSV exports into backend/data/processed/recent_grid/.",
            "missing_files": missing,
        })
    status = _recent_grid_status_payload()
    if status.get("ready"):
        return status
    with _RECENT_GRID_LOCK:
        if _RECENT_GRID_WORKER is None or not _RECENT_GRID_WORKER.is_alive():
            _RECENT_GRID_WORKER = threading.Thread(target=_recent_grid_build_worker, daemon=True)
            _RECENT_GRID_WORKER.start()
    return _recent_grid_status_payload()


@app.get("/api/recent-grid/overview")
def recent_grid_overview(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    region_iso3: str = "",
):
    start_text, end_text = validate_range_for_request(start_date, end_date)
    with closing(_recent_grid_connection()) as conn:
        where, params = _recent_grid_filters(None, start_text, end_text, region_iso3)
        rows = conn.execute(
            f"SELECT kind, COUNT(*) AS n, MIN(day) AS min_day, MAX(day) AS max_day FROM recent_grid_records{where} GROUP BY kind",
            params,
        ).fetchall()
        by_kind = {r["kind"]: {"count": int(r["n"]), "start_date": r["min_day"], "end_date": r["max_day"]} for r in rows}
        region_rows = conn.execute(
            f"SELECT region_iso3, kind, COUNT(*) AS n FROM recent_grid_records{where} GROUP BY region_iso3, kind ORDER BY region_iso3, kind",
            params,
        ).fetchall()
    return {
        "data_mode": "RECENT_GFW_GRID_ACTIVITY",
        "record_granularity": "GRID_CELL_FLAG_HOUR",
        "identity_available": False,
        "ais_grid_records": by_kind.get("ais", {}).get("count", 0),
        "sar_grid_records": by_kind.get("sar", {}).get("count", 0),
        "start_date": min((v.get("start_date") for v in by_kind.values() if v.get("start_date")), default=None),
        "end_date": max((v.get("end_date") for v in by_kind.values() if v.get("end_date")), default=None),
        "region_count": len({r["region_iso3"] for r in region_rows}),
        "region_counts": [dict(r) for r in region_rows],
        "spatial_resolution": "HIGH (0.01-degree GFW grid)",
        "temporal_resolution": "HOURLY",
        "temporal_resolution_windows": [
            {"kind": str(r["kind"]), "temporal_resolution": "HOURLY", "start_date": str(r["min_day"]), "end_date": str(r["max_day"]), "records": int(r["n"])}
            for r in rows if r["min_day"] and r["max_day"]
        ],
        "group_by": "FLAG",
        "start_filter": start_text or None,
        "end_filter": end_text or None,
        "note": "These are grid-cell-hour activity aggregates grouped by flag. They are not individual vessels, MMSI tracks, or anomaly scores.",
    }


@app.get("/api/recent-grid/map")
def recent_grid_map(
    limit: int = Query(default=6000, ge=100, le=12000),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    region_iso3: str = "",
):
    start_text, end_text = validate_range_for_request(start_date, end_date)
    with closing(_recent_grid_connection()) as conn:
        where, params = _recent_grid_filters(None, start_text, end_text, region_iso3)
        groups = conn.execute(
            f"SELECT kind, region_iso3, COUNT(*) AS n FROM recent_grid_records{where} GROUP BY kind, region_iso3",
            params,
        ).fetchall()
        totals = {"ais": 0, "sar": 0}
        for g in groups:
            totals[g["kind"]] = totals.get(g["kind"], 0) + int(g["n"])
        total = sum(totals.values())
        quotas: dict[tuple[str, str], int] = {}
        if total:
            # Preserve the sparse SAR layer in the map instead of allowing the
            # much larger AIS grid stream to consume every sample slot.
            sar_budget = min(totals.get("sar", 0), max(800, limit // 3))
            ais_budget = min(totals.get("ais", 0), max(0, limit - sar_budget))
            remaining = max(0, limit - sar_budget - ais_budget)
            if remaining:
                add_sar = min(remaining, max(0, totals.get("sar", 0) - sar_budget))
                sar_budget += add_sar
                remaining -= add_sar
                ais_budget += min(remaining, max(0, totals.get("ais", 0) - ais_budget))
            for kind, budget in (("ais", ais_budget), ("sar", sar_budget)):
                kind_groups = [g for g in groups if g["kind"] == kind]
                kind_total = sum(int(g["n"]) for g in kind_groups)
                if not kind_total or not budget:
                    continue
                allocated = 0
                for g in kind_groups:
                    quota = int(budget * int(g["n"]) / kind_total)
                    quotas[(kind, g["region_iso3"])] = quota
                    allocated += quota
                # At least one point per region when the budget permits it.
                ranked = sorted(kind_groups, key=lambda g: int(g["n"]), reverse=True)
                for g in ranked:
                    key = (kind, g["region_iso3"])
                    if quotas.get(key, 0) == 0 and allocated < budget:
                        quotas[key] = 1
                        allocated += 1
                # Distribute any remaining slots to the largest regions.
                for i in range(max(0, budget - allocated)):
                    if ranked:
                        g = ranked[i % len(ranked)]
                        key = (kind, g["region_iso3"])
                        quotas[key] = quotas.get(key, 0) + 1
        output = {"ais_points": [], "sar_points": []}
        sample_fields = "kind, day, date_text, lat, lon, flag, hours, vessel_ids, detections, region_iso3, region_id, region_label, spatial_resolution, temporal_resolution, source_dataset"
        for (kind, region), quota in quotas.items():
            if quota <= 0:
                continue
            local_where, local_params = _recent_grid_filters(kind, start_text, end_text, region)
            rows = conn.execute(
                f"SELECT {sample_fields} FROM recent_grid_records{local_where} ORDER BY sample_key LIMIT ?",
                [*local_params, quota],
            ).fetchall()
            key = "ais_points" if kind == "ais" else "sar_points"
            output[key].extend(dict(r) for r in rows)
    return {
        **output,
        "total_ais_matching": totals.get("ais", 0),
        "total_sar_matching": totals.get("sar", 0),
        "total_returned": len(output["ais_points"]) + len(output["sar_points"]),
        "limit": limit,
        "start_date": start_text or None,
        "end_date": end_text or None,
        "region_iso3": region_iso3.upper() or None,
        "sampled_for_map": total > len(output["ais_points"]) + len(output["sar_points"]),
        "note": "Points are sampled grid-cell-hour aggregates. They do not represent individual vessel locations or identities.",
    }


@app.get("/api/recent-grid/records")
def recent_grid_records(
    kind: Literal["ais", "sar"] = "ais",
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    region_iso3: str = "",
    search: str = "",
):
    start_text, end_text = validate_range_for_request(start_date, end_date)
    with closing(_recent_grid_connection()) as conn:
        where, params = _recent_grid_filters(kind, start_text, end_text, region_iso3)
        term = search.strip().upper()
        if term:
            where += (" AND " if where else " WHERE ") + "(flag LIKE ? OR region_iso3 LIKE ? OR date_text LIKE ?)"
            params.extend([f"%{term}%"] * 3)
        total = int(conn.execute(f"SELECT COUNT(*) FROM recent_grid_records{where}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT kind, day, date_text, lat, lon, flag, hours, vessel_ids, detections, region_iso3, region_id, region_label, spatial_resolution, temporal_resolution, source_dataset FROM recent_grid_records{where} ORDER BY day DESC, date_text DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"kind": kind, "total_matching": total, "total_returned": len(rows), "limit": limit, "offset": offset, "records": [dict(r) for r in rows], "identity_available": False}
# DARK-FLEET v0.9 analytics router
from app.v09_analytics import router as v09_analytics_router
app.include_router(v09_analytics_router)

