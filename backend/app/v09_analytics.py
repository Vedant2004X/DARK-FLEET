"""DARK-FLEET v0.9 read-only analytics and review API.

This module deliberately reuses the existing historical anomaly outputs. It does
not retrain or overwrite the Isolation Forest results, phase-3 candidates, or
recent grid database. It uses only Python's standard library plus FastAPI.
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(tags=["DARK-FLEET v0.9 analytics"])

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data" / "processed"
GLOBAL_DB = DATA_DIR / "dark_fleet_global_anomaly_index.sqlite3"
PHASE3_DB = DATA_DIR / "phase3" / "phase3_candidates.sqlite3"
RECENT_GRID_DB = DATA_DIR / "recent_grid" / "recent_grid_local_index.sqlite3"

EXPECTED_OBSERVATION_COLUMNS = {
    "id", "date_day", "date", "mmsi", "lat", "lon", "shipName",
    "vesselType", "speed_knots", "speed_change_knots", "time_gap_hours",
    "anomaly_score", "anomaly_flag", "anomaly_status", "region_iso3",
    "region_id", "region_label", "source_dataset",
}
FEATURE_COLUMNS = ("speed_knots", "speed_change_knots", "time_gap_hours")
MAX_LIMIT = 100
MAX_OFFSET = 100_000


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database read-only; never create or mutate it."""
    if not path.exists():
        raise FileNotFoundError(path)
    # URI mode prevents SQLite from silently creating a missing database.
    uri_path = quote(path.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _require_observations(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "observations"):
        raise HTTPException(status_code=503, detail="Historical anomaly index has no observations table.")
    missing = sorted(EXPECTED_OBSERVATION_COLUMNS - _table_columns(connection, "observations"))
    if missing:
        raise HTTPException(
            status_code=503,
            detail={"message": "Historical anomaly index schema is not compatible with v0.9.", "missing_columns": missing},
        )


def _date_filter(start_date: date | None, end_date: date | None) -> tuple[str, list[str]]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date.")
    clauses: list[str] = []
    params: list[str] = []
    if start_date:
        clauses.append("date_day >= ?")
        params.append(start_date.isoformat())
    if end_date:
        clauses.append("date_day <= ?")
        params.append(end_date.isoformat())
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _feature_expressions() -> dict[str, str]:
    return {
        name: f"CAST(NULLIF(TRIM({name}), '') AS REAL)"
        for name in FEATURE_COLUMNS
    }


def _cohort_statistics(connection: sqlite3.Connection, vessel_type: str | None) -> dict[str, dict[str, float | int | None]]:
    """Compute one-pass mean/std statistics for the three saved AIS features."""
    expressions = _feature_expressions()
    select_parts: list[str] = []
    for name, expression in expressions.items():
        select_parts.extend([
            f"COUNT({expression}) AS {name}_n",
            f"AVG({expression}) AS {name}_mean",
            f"AVG(({expression}) * ({expression})) AS {name}_mean_square",
        ])
    sql = "SELECT " + ", ".join(select_parts) + " FROM observations"
    params: tuple[str, ...] = ()
    if vessel_type:
        sql += " WHERE vesselType = ?"
        params = (vessel_type,)
    row = connection.execute(sql, params).fetchone()
    result: dict[str, dict[str, float | int | None]] = {}
    for name in FEATURE_COLUMNS:
        n = int(row[f"{name}_n"] or 0)
        mean = _number(row[f"{name}_mean"])
        mean_square = _number(row[f"{name}_mean_square"])
        std = None
        if mean is not None and mean_square is not None:
            std = math.sqrt(max(0.0, mean_square - mean * mean))
        result[name] = {"n": n, "mean": mean, "std": std}
    return result


def _feature_explanation(
    observation: sqlite3.Row,
    stats: dict[str, dict[str, float | int | None]],
) -> list[dict[str, Any]]:
    labels = {
        "speed_knots": "Speed",
        "speed_change_knots": "Speed change",
        "time_gap_hours": "Transmission time gap",
    }
    explanations: list[dict[str, Any]] = []
    for name in FEATURE_COLUMNS:
        value = _number(observation[name])
        baseline = stats[name]
        mean = baseline["mean"]
        std = baseline["std"]
        z_score = None
        if value is not None and mean is not None and std is not None and std > 1e-9:
            z_score = (value - mean) / std
        magnitude = abs(z_score) if z_score is not None else None
        if magnitude is None:
            level = "not_available"
        elif magnitude >= 3:
            level = "very_high_deviation"
        elif magnitude >= 2:
            level = "elevated_deviation"
        elif magnitude >= 1:
            level = "moderate_deviation"
        else:
            level = "near_cohort_baseline"
        explanations.append({
            "feature": name,
            "label": labels[name],
            "observed_value": value,
            "cohort_mean": mean,
            "cohort_std": std,
            "cohort_n": baseline["n"],
            "z_score": round(z_score, 4) if z_score is not None else None,
            "deviation_level": level,
        })
    return explanations


def _global_observation_connection() -> sqlite3.Connection:
    try:
        connection = _connect_readonly(GLOBAL_DB)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Historical anomaly index is not available.") from exc
    try:
        _require_observations(connection)
    except Exception:
        connection.close()
        raise
    return connection


@router.get("/api/v09/status")
def v09_status() -> dict[str, Any]:
    """Report data-artifact readiness and source-specific record coverage."""
    payload: dict[str, Any] = {
        "api_module_version": "0.9.0",
        "mode": "READ_ONLY_ANALYTICS",
        "historical_anomaly_index": {"ready": False, "path": str(GLOBAL_DB), "rows": 0},
        "phase3_candidates": {"ready": False, "path": str(PHASE3_DB), "rows": 0},
        "recent_grid": {"ready": False, "path": str(RECENT_GRID_DB), "by_kind": []},
        "dependencies": {"additional_ml_packages_required": False},
        "notes": [
            "Existing anomaly scores are reused; v0.9 does not retrain or overwrite the model.",
            "Recent AIS/SAR grid rows remain aggregate activity records without vessel identity.",
            "Phase 3 proximity candidates are not confirmed SAR/AIS vessel associations.",
        ],
    }

    if GLOBAL_DB.exists():
        try:
            with closing(_connect_readonly(GLOBAL_DB)) as connection:
                _require_observations(connection)
                row = connection.execute(
                    "SELECT COUNT(*) AS n, MIN(date_day) AS first_day, MAX(date_day) AS last_day "
                    "FROM observations"
                ).fetchone()
                payload["historical_anomaly_index"].update({
                    "ready": True, "rows": int(row["n"] or 0),
                    "start_date": row["first_day"], "end_date": row["last_day"],
                    "size_mb": round(GLOBAL_DB.stat().st_size / (1024 * 1024), 2),
                })
        except (sqlite3.Error, HTTPException) as exc:
            payload["historical_anomaly_index"]["error"] = str(exc)

    if PHASE3_DB.exists():
        try:
            with closing(_connect_readonly(PHASE3_DB)) as connection:
                if _table_exists(connection, "candidates"):
                    row = connection.execute(
                        "SELECT COUNT(*) AS n, COUNT(DISTINCT sar_record_id) AS sar_records "
                        "FROM candidates"
                    ).fetchone()
                    payload["phase3_candidates"].update({
                        "ready": True, "rows": int(row["n"] or 0),
                        "sar_records_with_candidates": int(row["sar_records"] or 0),
                        "size_mb": round(PHASE3_DB.stat().st_size / (1024 * 1024), 2),
                    })
        except sqlite3.Error as exc:
            payload["phase3_candidates"]["error"] = str(exc)

    if RECENT_GRID_DB.exists():
        try:
            with closing(_connect_readonly(RECENT_GRID_DB)) as connection:
                if _table_exists(connection, "recent_grid_records"):
                    rows = connection.execute(
                        "SELECT kind, COUNT(*) AS n, MIN(day) AS first_day, MAX(day) AS last_day, "
                        "COUNT(DISTINCT region_iso3) AS regions "
                        "FROM recent_grid_records GROUP BY kind ORDER BY kind"
                    ).fetchall()
                    payload["recent_grid"].update({
                        "ready": True,
                        "by_kind": [dict(row) for row in rows],
                        "size_mb": round(RECENT_GRID_DB.stat().st_size / (1024 * 1024), 2),
                        "identity_available": False,
                        "record_granularity": "GRID_CELL_FLAG_HOUR",
                    })
        except sqlite3.Error as exc:
            payload["recent_grid"]["error"] = str(exc)
    return payload


@router.get("/api/v09/overview")
def v09_overview(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> dict[str, Any]:
    """Summarize the saved historical model outputs for a requested date range."""
    where, params = _date_filter(start_date, end_date)
    with closing(_global_observation_connection()) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total, COUNT(DISTINCT mmsi) AS vessels, "
            "SUM(CASE WHEN anomaly_flag='-1' THEN 1 ELSE 0 END) AS flagged, "
            "SUM(CASE WHEN anomaly_flag='1' THEN 1 ELSE 0 END) AS baseline, "
            "AVG(CAST(NULLIF(TRIM(anomaly_score), '') AS REAL)) AS mean_score, "
            "MIN(CAST(NULLIF(TRIM(anomaly_score), '') AS REAL)) AS min_score, "
            "MAX(CAST(NULLIF(TRIM(anomaly_score), '') AS REAL)) AS max_score, "
            "MIN(date_day) AS actual_start_date, MAX(date_day) AS actual_end_date "
            f"FROM observations{where}", params,
        ).fetchone()

        def grouped(column: str, limit: int = 12) -> list[dict[str, Any]]:
            allowed = {"anomaly_status", "vesselType", "region_iso3"}
            if column not in allowed:
                raise ValueError("Unsupported aggregation column")
            return [dict(item) for item in connection.execute(
                f"SELECT COALESCE(NULLIF({column}, ''), 'Unknown') AS label, COUNT(*) AS n "
                f"FROM observations{where} GROUP BY {column} ORDER BY n DESC LIMIT ?",
                [*params, limit],
            ).fetchall()]

        return {
            "data_mode": "EXISTING_HISTORICAL_MODEL_OUTPUTS",
            "requested_start_date": start_date.isoformat() if start_date else None,
            "requested_end_date": end_date.isoformat() if end_date else None,
            "actual_start_date": row["actual_start_date"],
            "actual_end_date": row["actual_end_date"],
            "total_observations": int(row["total"] or 0),
            "unique_vessels": int(row["vessels"] or 0),
            "flagged_observations": int(row["flagged"] or 0),
            "baseline_observations": int(row["baseline"] or 0),
            "mean_saved_anomaly_score": _number(row["mean_score"]),
            "min_saved_anomaly_score": _number(row["min_score"]),
            "max_saved_anomaly_score": _number(row["max_score"]),
            "by_status": grouped("anomaly_status"),
            "by_vessel_type": grouped("vesselType"),
            "by_region": grouped("region_iso3"),
            "interpretation": (
                "These are summaries of the already-saved model outputs. A flagged row is a statistical "
                "review signal, not a finding of illegal activity."
            ),
        }


@router.get("/api/v09/review-queue")
def v09_review_queue(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    region_iso3: str | None = Query(default=None, min_length=3, max_length=3),
    limit: int = Query(default=25, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    search: str = Query(default="", max_length=100),
) -> dict[str, Any]:
    """Return saved flagged observations, ordered by the existing anomaly score."""
    where, params = _date_filter(start_date, end_date)
    clauses = [where[7:]] if where else []
    values: list[Any] = list(params)
    clauses.append("anomaly_flag = '-1'")
    if region_iso3:
        clauses.append("region_iso3 = ?")
        values.append(region_iso3.upper())
    if search.strip():
        pattern = f"%{search.strip()}%"
        clauses.append("(mmsi LIKE ? OR shipName LIKE ? OR vesselType LIKE ?)")
        values.extend([pattern, pattern, pattern])
    where_sql = " WHERE " + " AND ".join(clauses)

    with closing(_global_observation_connection()) as connection:
        total = connection.execute(
            f"SELECT COUNT(*) FROM observations{where_sql}", values
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT id, date_day, date, mmsi, shipName, vesselType, lat, lon, "
            "speed_knots, speed_change_knots, time_gap_hours, anomaly_score, "
            "anomaly_flag, anomaly_status, region_iso3, region_label "
            f"FROM observations{where_sql} "
            "ORDER BY CAST(NULLIF(TRIM(anomaly_score), '') AS REAL) DESC, date_day DESC, id DESC "
            "LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
        return {
            "data_mode": "EXISTING_FLAGGED_MODEL_OUTPUTS",
            "status_filter": "REVIEW",
            "total_matching": int(total),
            "limit": limit,
            "offset": offset,
            "observations": [dict(row) for row in rows],
            "note": "Sorted by the saved anomaly score; score is not a probability or a legal conclusion.",
        }


@router.get("/api/v09/explain/{observation_id}")
def v09_explain_observation(observation_id: int) -> dict[str, Any]:
    """Provide cohort-deviation context for a saved observation.

    This is a transparent statistical context aid, not SHAP and not a claim about
    the internal feature contributions of the pre-existing Isolation Forest.
    """
    with closing(_global_observation_connection()) as connection:
        observation = connection.execute(
            "SELECT id, date_day, date, mmsi, shipName, vesselType, lat, lon, "
            "speed_knots, speed_change_knots, time_gap_hours, anomaly_score, "
            "anomaly_flag, anomaly_status, region_iso3, region_label "
            "FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if observation is None:
            raise HTTPException(status_code=404, detail="Observation ID was not found.")

        vessel_type = str(observation["vesselType"] or "").strip()
        cohort = _cohort_statistics(connection, vessel_type or None)
        sample_n = min(int(cohort[name]["n"] or 0) for name in FEATURE_COLUMNS)
        cohort_basis = "vessel_type" if vessel_type and sample_n >= 30 else "all_vessels"
        if cohort_basis == "all_vessels":
            cohort = _cohort_statistics(connection, None)
            sample_n = min(int(cohort[name]["n"] or 0) for name in FEATURE_COLUMNS)

        return {
            "observation": dict(observation),
            "saved_model_signal": {
                "anomaly_flag": observation["anomaly_flag"],
                "anomaly_status": observation["anomaly_status"],
                "anomaly_score": _number(observation["anomaly_score"]),
                "score_origin": "pre-existing saved model output",
            },
            "feature_deviations": _feature_explanation(observation, cohort),
            "comparison_cohort": {
                "basis": cohort_basis,
                "vessel_type": vessel_type or None,
                "minimum_feature_sample_count": sample_n,
            },
            "methodology_note": (
                "Feature deviations are absolute z-scores, not SHAP values. They use a vessel-type cohort when at least "
                "30 feature observations are available; otherwise all vessels are used. They are "
                "contextual indicators, not causal explanations, and they do not "
                "reconstruct the original Isolation Forest decision path."
            ),
        }


@router.get("/api/v09/association-overview")
def v09_association_overview() -> dict[str, Any]:
    """Summarize the existing Phase 3 candidate index without confirming matches."""
    if not PHASE3_DB.exists():
        raise HTTPException(status_code=503, detail="Phase 3 candidate index is not available.")
    with closing(_connect_readonly(PHASE3_DB)) as connection:
        if not _table_exists(connection, "candidates"):
            raise HTTPException(status_code=503, detail="Phase 3 database has no candidates table.")
        row = connection.execute(
            "SELECT COUNT(*) AS candidate_rows, COUNT(DISTINCT sar_record_id) AS sar_records, "
            "COUNT(DISTINCT candidate_mmsi) AS unique_candidate_vessels, "
            "AVG(distance_km) AS mean_distance_km, MIN(distance_km) AS min_distance_km, "
            "MAX(distance_km) AS max_distance_km, AVG(time_gap_min) AS mean_time_gap_min, "
            "AVG(ranking_score) AS mean_ranking_score, MIN(ranking_score) AS min_ranking_score, "
            "MAX(ranking_score) AS max_ranking_score FROM candidates"
        ).fetchone()
        rank_rows = [dict(item) for item in connection.execute(
            "SELECT candidate_rank, COUNT(*) AS n FROM candidates "
            "GROUP BY candidate_rank ORDER BY candidate_rank LIMIT 10"
        ).fetchall()]
    return {
        "data_mode": "PHASE3_PROXIMITY_CANDIDATES",
        "candidate_rows": int(row["candidate_rows"] or 0),
        "sar_records_with_candidates": int(row["sar_records"] or 0),
        "unique_candidate_vessels": int(row["unique_candidate_vessels"] or 0),
        "mean_distance_km": _number(row["mean_distance_km"]),
        "min_distance_km": _number(row["min_distance_km"]),
        "max_distance_km": _number(row["max_distance_km"]),
        "mean_time_gap_min": _number(row["mean_time_gap_min"]),
        "mean_ranking_score": _number(row["mean_ranking_score"]),
        "min_ranking_score": _number(row["min_ranking_score"]),
        "max_ranking_score": _number(row["max_ranking_score"]),
        "candidate_rank_distribution": rank_rows,
        "interpretation": (
            "Ranking scores order nearby AIS observations inside the configured search envelope. "
            "Proximity is not identity proof; no candidate does not prove vessel absence."
        ),
    }
