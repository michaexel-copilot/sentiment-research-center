"""DuckDB storage. One file (data/src.duckdb); all writes go through a process-wide lock.
The dashboard reads a snapshot copy (data/src_dashboard.duckdb) published after each run."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    key VARCHAR PRIMARY KEY, symbol VARCHAR, name VARCHAR, asset_class VARCHAR,
    coingecko_id VARCHAR, sector VARCHAR, universes VARCHAR, rank INTEGER, updated_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS prices (
    asset_key VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE,
    PRIMARY KEY (asset_key, date)
);
CREATE TABLE IF NOT EXISTS texts (
    text_id VARCHAR, asset_key VARCHAR, source VARCHAR, kind VARCHAR, title VARCHAR, body VARCHAR,
    url VARCHAR, published_at TIMESTAMP, engagement DOUBLE, author_label VARCHAR, fetched_at TIMESTAMP,
    PRIMARY KEY (text_id, asset_key)
);
CREATE TABLE IF NOT EXISTS text_scores (
    text_id VARCHAR, asset_key VARCHAR, relevant BOOLEAN, sentiment DOUBLE, confidence DOUBLE,
    stance VARCHAR, horizon VARCHAR, tags VARCHAR, sarcasm BOOLEAN, model VARCHAR,
    prompt_version VARCHAR, scored_at TIMESTAMP,
    PRIMARY KEY (text_id, asset_key)
);
CREATE TABLE IF NOT EXISTS snapshots (
    asset_key VARCHAR, date DATE, kind VARCHAR, data VARCHAR,
    PRIMARY KEY (asset_key, date, kind)
);
CREATE TABLE IF NOT EXISTS signals (
    asset_key VARCHAR, date DATE, signal VARCHAR, composite DOUBLE, confidence DOUBLE,
    subscores VARCHAR, drivers VARCHAR, flags VARCHAR, facts VARCHAR, narrative VARCHAR,
    created_at TIMESTAMP,
    PRIMARY KEY (asset_key, date)
);
CREATE TABLE IF NOT EXISTS llm_usage (
    ts TIMESTAMP, run_id VARCHAR, stage VARCHAR, model VARCHAR, batch BOOLEAN,
    input_tokens BIGINT, output_tokens BIGINT, cache_read_tokens BIGINT, cache_write_tokens BIGINT,
    cost_usd DOUBLE
);
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY, started_at TIMESTAMP, finished_at TIMESTAMP, scope VARCHAR,
    status VARCHAR, n_assets INTEGER, n_failed INTEGER, errors VARCHAR
);
"""

_lock = threading.RLock()
_conn: duckdb.DuckDBPyConnection | None = None


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if hasattr(o, "item"):  # numpy scalars
        return o.item()
    return str(o)


def dumps(o: Any) -> str:
    return json.dumps(o, default=_json_default)


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Process-wide connection for the pipeline. The dashboard opens short-lived read-only ones instead."""
    global _conn
    if read_only:
        return duckdb.connect(str(config.path("db")), read_only=True)
    with _lock:
        if _conn is None:
            _conn = duckdb.connect(str(config.path("db")))
            _conn.execute(SCHEMA)
        return _conn


def snapshot_path() -> Path:
    db_path = config.path("db")
    return db_path.with_name(f"{db_path.stem}_dashboard{db_path.suffix}")


def publish_snapshot(retries: int = 20) -> Path:
    """Copy the database to the read-only snapshot the dashboard uses.

    DuckDB on Windows lets no other process open the file while a writer has it open (not even read-only),
    so the dashboard never reads the live database. Written to a temp file, then swapped in; the swap is
    retried because a dashboard query may have the old snapshot open for a moment.
    """
    target = snapshot_path()
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    with _lock:
        conn = connect()
        conn.execute("CHECKPOINT")
        source = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(f"ATTACH '{tmp.as_posix()}' AS dashboard_snapshot")
        try:
            conn.execute(f"COPY FROM DATABASE {source} TO dashboard_snapshot")
        finally:
            conn.execute("DETACH dashboard_snapshot")
    for attempt in range(retries):
        try:
            os.replace(tmp, target)
            return target
        except PermissionError:
            time.sleep(0.5)
    raise RuntimeError(f"could not replace {target} (dashboard holding it open?); new snapshot left at {tmp}")


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


@contextmanager
def cursor():
    with _lock:
        yield connect().cursor()


def query_df(sql: str, params: Iterable[Any] | None = None) -> pd.DataFrame:
    with cursor() as cur:
        return cur.execute(sql, list(params or [])).df()


def execute(sql: str, params: Iterable[Any] | None = None) -> None:
    with cursor() as cur:
        cur.execute(sql, list(params or []))


APPEND_ONLY = {"llm_usage"}  # tables without a primary key


def upsert_df(table: str, df: pd.DataFrame) -> int:
    """INSERT OR REPLACE a DataFrame whose columns match the table's columns (plain INSERT for append-only tables)."""
    if df is None or df.empty:
        return 0
    verb = "INSERT" if table in APPEND_ONLY else "INSERT OR REPLACE"
    with cursor() as cur:
        cur.register("_upsert_df", df)
        cols = ", ".join(df.columns)
        cur.execute(f"{verb} INTO {table} ({cols}) SELECT {cols} FROM _upsert_df")
        cur.unregister("_upsert_df")
    return len(df)


# --- typed helpers -------------------------------------------------------------------------------

def save_snapshot(asset_key: str, day: date, kind: str, data: dict[str, Any]) -> None:
    execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?)", [asset_key, day, kind, dumps(data)])


def load_snapshot(asset_key: str, kind: str, day: date | None = None) -> dict[str, Any] | None:
    sql = "SELECT data FROM snapshots WHERE asset_key = ? AND kind = ?"
    params: list[Any] = [asset_key, kind]
    if day is not None:
        sql += " AND date <= ?"
        params.append(day)
    sql += " ORDER BY date DESC LIMIT 1"
    df = query_df(sql, params)
    return json.loads(df.iloc[0]["data"]) if not df.empty else None


def load_snapshot_history(asset_key: str, kind: str, days: int = 120) -> pd.DataFrame:
    df = query_df(
        "SELECT date, data FROM snapshots WHERE asset_key = ? AND kind = ? "
        "AND date >= current_date - CAST(? AS INTEGER) ORDER BY date",
        [asset_key, kind, days],
    )
    if df.empty:
        return df
    return pd.concat([df[["date"]], pd.DataFrame([json.loads(d) for d in df["data"]])], axis=1)


def prices(asset_key: str) -> pd.DataFrame:
    df = query_df("SELECT * FROM prices WHERE asset_key = ? ORDER BY date", [asset_key])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df


def texts_with_scores(asset_key: str, days: int = 90) -> pd.DataFrame:
    return query_df(
        """SELECT t.*, s.relevant, s.sentiment, s.confidence, s.stance, s.horizon, s.tags, s.sarcasm, s.model
           FROM texts t LEFT JOIN text_scores s USING (text_id, asset_key)
           WHERE t.asset_key = ? AND t.published_at >= now() - to_days(CAST(? AS INTEGER))
           ORDER BY t.published_at DESC""",
        [asset_key, days],
    )


def unscored_texts(asset_keys: list[str], limit_per_asset: int) -> pd.DataFrame:
    """Newest unscored texts per asset, alternating news and social so a busy social feed can't crowd out news.
    Within each kind: most engaged first, then newest."""
    if not asset_keys:
        return pd.DataFrame()
    return query_df(
        f"""SELECT * EXCLUDE (rn_kind) FROM (
              SELECT *, row_number() OVER (PARTITION BY asset_key ORDER BY rn_kind, kind) AS rn FROM (
                SELECT t.*, row_number() OVER (PARTITION BY t.asset_key, t.kind
                       ORDER BY t.engagement DESC, t.published_at DESC) AS rn_kind
                FROM texts t LEFT JOIN text_scores s USING (text_id, asset_key)
                WHERE s.text_id IS NULL AND t.asset_key IN ({",".join("?" * len(asset_keys))})
                  AND t.published_at >= now() - INTERVAL 35 DAY))
            WHERE rn <= ?""",
        [*asset_keys, limit_per_asset],
    )
