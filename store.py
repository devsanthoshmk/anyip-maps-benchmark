"""SQLite benchmark storage with WAL mode, thread-safety, and raw CSV export.

Spec requirements:
- stdlib sqlite3
- WAL mode, check_same_thread=False, threading.Lock
- Exact tables: runs, sessions, requests, places, ip_observations, events
- Methods: init(), insert_run, upsert_session, update_session, insert_request,
  insert_place, insert_ip_observation, insert_event, close(), export_raw
- Return row counts; use dicts (no ORM); deduplicate/update safe
- Export files: sessions.csv, requests.csv, places.csv, ip_observations.csv, events.csv
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union, cast

logger = logging.getLogger(__name__)

RUNS_COLUMNS: list[str] = [
    "run_id",
    "started_at",
    "finished_at",
    "config_json",
    "manifest_json",
    "status",
]

SESSIONS_COLUMNS: list[str] = [
    "run_id",
    "session_id",
    "arm",
    "variant",
    "pool",
    "country",
    "session_name",
    "strict_replace",
    "sessasn_strict",
    "sessipcollision_strict",
    "sesstime_min",
    "created_at",
    "started_at",
    "ended_at",
    "requests",
    "google_requests",
    "places",
    "block_events",
    "first_block_request",
    "first_block_ts",
    "status",
    "stop_reason",
    "distinct_ips",
    "ip_set_json",
    "asn_set_json",
    "city_set_json",
    "traffic_bytes",
    "error",
]

REQUESTS_COLUMNS: list[str] = [
    "id",
    "run_id",
    "session_id",
    "ts",
    "request_index",
    "query_index",
    "query",
    "country",
    "kind",
    "page_start",
    "url_hash",
    "status",
    "outcome",
    "block_type",
    "anyip_error",
    "attempt",
    "is_final",
    "latency_ms",
    "bytes_wire",
    "bytes_rx",
    "content_encoding",
    "content_type",
    "server",
    "error",
    "arm",
    "variant",
]

PLACES_COLUMNS: list[str] = [
    "id",
    "run_id",
    "session_id",
    "query_index",
    "query",
    "country",
    "request_id",
    "rank",
    "place_id",
    "title",
    "category",
    "address",
    "phone",
    "website",
    "stars",
    "reviews",
    "lat",
    "lng",
    "validation",
    "arm",
    "variant",
]

IP_OBSERVATIONS_COLUMNS: list[str] = [
    "id",
    "run_id",
    "session_id",
    "ts",
    "request_index",
    "witness",
    "family",
    "ip",
    "asn",
    "org",
    "city",
    "country",
    "latency_ms",
    "error",
    "arm",
    "variant",
]

EVENTS_COLUMNS: list[str] = [
    "id",
    "run_id",
    "session_id",
    "ts",
    "request_index",
    "event_type",
    "detail_json",
    "arm",
    "variant",
]

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at REAL,
    finished_at REAL,
    config_json TEXT,
    manifest_json TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    run_id TEXT,
    session_id TEXT,
    arm TEXT,
    variant TEXT,
    pool TEXT,
    country TEXT,
    session_name TEXT,
    strict_replace INTEGER,
    sessasn_strict INTEGER,
    sessipcollision_strict INTEGER,
    sesstime_min INTEGER,
    created_at REAL,
    started_at REAL,
    ended_at REAL,
    requests INTEGER,
    google_requests INTEGER,
    places INTEGER,
    block_events INTEGER,
    first_block_request INTEGER,
    first_block_ts REAL,
    status TEXT,
    stop_reason TEXT,
    distinct_ips INTEGER,
    ip_set_json TEXT,
    asn_set_json TEXT,
    city_set_json TEXT,
    traffic_bytes INTEGER,
    error TEXT,
    PRIMARY KEY(run_id, session_id)
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    session_id TEXT,
    ts REAL,
    request_index INTEGER,
    query_index INTEGER,
    query TEXT,
    country TEXT,
    kind TEXT,
    page_start INTEGER,
    url_hash TEXT,
    status INTEGER,
    outcome TEXT,
    block_type TEXT,
    anyip_error TEXT,
    attempt INTEGER,
    is_final INTEGER,
    latency_ms REAL,
    bytes_wire INTEGER,
    bytes_rx INTEGER,
    content_encoding TEXT,
    content_type TEXT,
    server TEXT,
    error TEXT,
    arm TEXT,
    variant TEXT
);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    session_id TEXT,
    query_index INTEGER,
    query TEXT,
    country TEXT,
    request_id INTEGER,
    rank INTEGER,
    place_id TEXT,
    title TEXT,
    category TEXT,
    address TEXT,
    phone TEXT,
    website TEXT,
    stars TEXT,
    reviews TEXT,
    lat REAL,
    lng REAL,
    validation TEXT,
    arm TEXT,
    variant TEXT
);

CREATE TABLE IF NOT EXISTS ip_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    session_id TEXT,
    ts REAL,
    request_index INTEGER,
    witness TEXT,
    family TEXT,
    ip TEXT,
    asn TEXT,
    org TEXT,
    city TEXT,
    country TEXT,
    latency_ms REAL,
    error TEXT,
    arm TEXT,
    variant TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    session_id TEXT,
    ts REAL,
    request_index INTEGER,
    event_type TEXT,
    detail_json TEXT,
    arm TEXT,
    variant TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_run_id ON sessions(run_id);
CREATE INDEX IF NOT EXISTS idx_requests_run_sess ON requests(run_id, session_id);
CREATE INDEX IF NOT EXISTS idx_places_run_sess ON places(run_id, session_id);
CREATE INDEX IF NOT EXISTS idx_ip_obs_run_sess ON ip_observations(run_id, session_id);
CREATE INDEX IF NOT EXISTS idx_events_run_sess ON events(run_id, session_id);
"""


def _serialize_value(val: Any) -> Any:
    """Normalize Python types for SQLite storage."""
    if val is None:
        return None
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, (dict, list, tuple, set, frozenset)):
        if isinstance(val, (set, frozenset)):
            return json.dumps(list(val))
        return json.dumps(val)
    return val


def _hash_url(url: str) -> str:
    """Deterministic hash for url storage."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class Store:
    """Thread-safe SQLite store for gmap-bench benchmarks using stdlib sqlite3 and WAL."""

    def __init__(self, db_path: Union[str, Path] = "benchmark.db") -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    def _open(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self.init()

    def init(self) -> "Store":
        """Initialize tables and configure WAL mode."""
        with self._lock:
            if self._conn is None:
                self._open()
            assert self._conn is not None
            # WAL mode
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.executescript(SCHEMA_DDL)
            if not any(row[1] == "query_index" for row in self._conn.execute("PRAGMA table_info(places)")):
                self._conn.execute("ALTER TABLE places ADD COLUMN query_index INTEGER")
            self._conn.commit()
        return self

    def insert_run(self, data: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
        """Insert or replace a run record. Returns rows affected (1)."""
        record: Dict[str, Any] = dict(cast(Dict[str, Any], data or {}))
        record.update(kwargs)
        if "run_id" not in record or not record["run_id"]:
            raise ValueError("run_id is required for insert_run")

        keys = [c for c in RUNS_COLUMNS if c in record]
        vals = [_serialize_value(record[c]) for c in keys]
        placeholders = ", ".join(["?"] * len(keys))
        col_names = ", ".join(keys)

        update_keys = [c for c in keys if c != "run_id"]
        if update_keys:
            update_clause = ", ".join([f"{c} = excluded.{c}" for c in update_keys])
            sql = (
                f"INSERT INTO runs ({col_names}) VALUES ({placeholders}) "
                f"ON CONFLICT(run_id) DO UPDATE SET {update_clause}"
            )
        else:
            sql = f"INSERT INTO runs ({col_names}) VALUES ({placeholders}) ON CONFLICT(run_id) DO NOTHING"

        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, vals)
            self._conn.commit()
            return cur.rowcount if cur.rowcount > 0 else 1

    def upsert_session(self, data: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
        """Upsert a session record on (run_id, session_id). Returns rows affected (1)."""
        record: Dict[str, Any] = dict(data or {})
        record.update(kwargs)
        if "run_id" not in record or "session_id" not in record:
            raise ValueError("run_id and session_id are required for upsert_session")

        keys = [c for c in SESSIONS_COLUMNS if c in record]
        vals = [_serialize_value(record[c]) for c in keys]
        placeholders = ", ".join(["?"] * len(keys))
        col_names = ", ".join(keys)

        update_keys = [c for c in keys if c not in ("run_id", "session_id")]
        if update_keys:
            update_clause = ", ".join([f"{c} = excluded.{c}" for c in update_keys])
            sql = (
                f"INSERT INTO sessions ({col_names}) VALUES ({placeholders}) "
                f"ON CONFLICT(run_id, session_id) DO UPDATE SET {update_clause}"
            )
        else:
            sql = f"INSERT INTO sessions ({col_names}) VALUES ({placeholders}) ON CONFLICT(run_id, session_id) DO NOTHING"

        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, vals)
            self._conn.commit()
            return cur.rowcount if cur.rowcount > 0 else 1

    def update_session(
        self,
        session_id: str,
        run_id: Optional[str] = None,
        *args: Any,
        **fields: Any,
    ) -> int:
        """Update specific fields of a session. Returns row count updated."""
        payload: Dict[str, Any] = {}
        if args and isinstance(args[0], dict):
            payload.update(args[0])
        payload.update(fields)

        if not payload:
            return 0

        if run_id is None and "run_id" in payload:
            run_id = payload.pop("run_id")

        valid_keys = [
            k for k in payload.keys() if k in SESSIONS_COLUMNS and k not in ("session_id", "run_id")
        ]
        if not valid_keys:
            return 0

        set_clause = ", ".join([f"{k} = ?" for k in valid_keys])
        params = [_serialize_value(payload[k]) for k in valid_keys]
        params.append(session_id)

        where = "WHERE session_id = ?"
        if run_id is not None:
            where += " AND run_id = ?"
            params.append(run_id)

        sql = f"UPDATE sessions SET {set_clause} {where}"

        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def insert_request(
        self,
        data: Optional[Union[Dict[str, Any], Sequence[Dict[str, Any]]]] = None,
        **kwargs: Any,
    ) -> int:
        """Insert one or multiple request rows. Returns number of rows inserted."""
        if isinstance(data, (list, tuple)):
            count = 0
            for item in data:
                count += self.insert_request(item)
            return count

        record: Dict[str, Any] = dict(data or {})
        record.update(kwargs)

        if "url" in record and ("url_hash" not in record or not record.get("url_hash")):
            record["url_hash"] = _hash_url(str(record["url"]))

        keys = [c for c in REQUESTS_COLUMNS if c in record]
        # Skip id if None so autoincrement takes over
        if "id" in keys and record["id"] is None:
            keys.remove("id")

        vals = [_serialize_value(record[c]) for c in keys]
        col_names = ", ".join(keys)
        placeholders = ", ".join(["?"] * len(keys))

        if "id" in keys:
            update_keys = [c for c in keys if c != "id"]
            if update_keys:
                update_clause = ", ".join([f"{c} = excluded.{c}" for c in update_keys])
                sql = (
                    f"INSERT INTO requests ({col_names}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {update_clause}"
                )
            else:
                sql = f"INSERT INTO requests ({col_names}) VALUES ({placeholders}) ON CONFLICT(id) DO NOTHING"
        else:
            sql = f"INSERT INTO requests ({col_names}) VALUES ({placeholders})"

        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, vals)
            self._conn.commit()
            return cur.rowcount if cur.rowcount > 0 else 1

    def insert_place(
        self,
        data: Optional[Union[Dict[str, Any], Sequence[Dict[str, Any]]]] = None,
        **kwargs: Any,
    ) -> int:
        """Insert one or multiple places rows. Returns number of rows inserted."""
        if isinstance(data, (list, tuple)):
            count = 0
            for item in data:
                count += self.insert_place(item)
            return count

        record: Dict[str, Any] = dict(data or {})
        record.update(kwargs)

        keys = [c for c in PLACES_COLUMNS if c in record]
        if "id" in keys and record["id"] is None:
            keys.remove("id")

        vals = [_serialize_value(record[c]) for c in keys]
        col_names = ", ".join(keys)
        placeholders = ", ".join(["?"] * len(keys))

        if "id" in keys:
            update_keys = [c for c in keys if c != "id"]
            if update_keys:
                update_clause = ", ".join([f"{c} = excluded.{c}" for c in update_keys])
                sql = (
                    f"INSERT INTO places ({col_names}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {update_clause}"
                )
            else:
                sql = f"INSERT INTO places ({col_names}) VALUES ({placeholders}) ON CONFLICT(id) DO NOTHING"
        else:
            sql = f"INSERT INTO places ({col_names}) VALUES ({placeholders})"

        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, vals)
            self._conn.commit()
            return cur.rowcount if cur.rowcount > 0 else 1

    def insert_ip_observation(
        self,
        data: Optional[Union[Dict[str, Any], Sequence[Dict[str, Any]]]] = None,
        **kwargs: Any,
    ) -> int:
        """Insert one or multiple ip_observation rows. Returns number of rows inserted."""
        if isinstance(data, (list, tuple)):
            count = 0
            for item in data:
                count += self.insert_ip_observation(item)
            return count

        record: Dict[str, Any] = dict(data or {})
        record.update(kwargs)

        keys = [c for c in IP_OBSERVATIONS_COLUMNS if c in record]
        if "id" in keys and record["id"] is None:
            keys.remove("id")

        vals = [_serialize_value(record[c]) for c in keys]
        col_names = ", ".join(keys)
        placeholders = ", ".join(["?"] * len(keys))

        if "id" in keys:
            update_keys = [c for c in keys if c != "id"]
            if update_keys:
                update_clause = ", ".join([f"{c} = excluded.{c}" for c in update_keys])
                sql = (
                    f"INSERT INTO ip_observations ({col_names}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {update_clause}"
                )
            else:
                sql = f"INSERT INTO ip_observations ({col_names}) VALUES ({placeholders}) ON CONFLICT(id) DO NOTHING"
        else:
            sql = f"INSERT INTO ip_observations ({col_names}) VALUES ({placeholders})"

        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, vals)
            self._conn.commit()
            return cur.rowcount if cur.rowcount > 0 else 1

    def insert_event(
        self,
        data: Optional[Union[Dict[str, Any], Sequence[Dict[str, Any]]]] = None,
        **kwargs: Any,
    ) -> int:
        """Insert one or multiple event rows. Returns number of rows inserted."""
        if isinstance(data, (list, tuple)):
            count = 0
            for item in data:
                count += self.insert_event(item)
            return count

        record: Dict[str, Any] = dict(data or {})
        record.update(kwargs)

        keys = [c for c in EVENTS_COLUMNS if c in record]
        if "id" in keys and record["id"] is None:
            keys.remove("id")

        vals = [_serialize_value(record[c]) for c in keys]
        col_names = ", ".join(keys)
        placeholders = ", ".join(["?"] * len(keys))

        if "id" in keys:
            update_keys = [c for c in keys if c != "id"]
            if update_keys:
                update_clause = ", ".join([f"{c} = excluded.{c}" for c in update_keys])
                sql = (
                    f"INSERT INTO events ({col_names}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {update_clause}"
                )
            else:
                sql = f"INSERT INTO events ({col_names}) VALUES ({placeholders}) ON CONFLICT(id) DO NOTHING"
        else:
            sql = f"INSERT INTO events ({col_names}) VALUES ({placeholders})"

        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, vals)
            self._conn.commit()
            return cur.rowcount if cur.rowcount > 0 else 1

    def export_raw(
        self,
        run_id: Optional[str] = None,
        out_dir: Union[str, Path] = "results",
    ) -> Dict[str, int]:
        """Export raw tables to CSV: sessions.csv, requests.csv, places.csv, ip_observations.csv, events.csv.

        Returns dict of row counts per export file.
        """
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        tables_to_export: list[Tuple[str, str, list[str]]] = [
            ("sessions", "sessions.csv", SESSIONS_COLUMNS),
            ("requests", "requests.csv", REQUESTS_COLUMNS),
            ("places", "places.csv", PLACES_COLUMNS),
            ("ip_observations", "ip_observations.csv", IP_OBSERVATIONS_COLUMNS),
            ("events", "events.csv", EVENTS_COLUMNS),
        ]

        row_counts: Dict[str, int] = {}

        with self._lock:
            assert self._conn is not None
            for table_name, csv_filename, columns in tables_to_export:
                cols_str = ", ".join(columns)
                if run_id is not None:
                    sql = f"SELECT {cols_str} FROM {table_name} WHERE run_id = ?"
                    cursor = self._conn.execute(sql, (run_id,))
                else:
                    sql = f"SELECT {cols_str} FROM {table_name}"
                    cursor = self._conn.execute(sql)

                target_file = out_path / csv_filename
                count = 0
                with open(target_file, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(columns)
                    for row in cursor:
                        # row is a sqlite3.Row
                        writer.writerow([row[c] if row[c] is not None else "" for c in columns])
                        count += 1
                row_counts[table_name] = count

        return row_counts

    def get_ip_observations(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Query ip_observations rows as dicts."""
        with self._lock:
            assert self._conn is not None
            if run_id is not None:
                cur = self._conn.execute(
                    "SELECT * FROM ip_observations WHERE run_id = ? ORDER BY id ASC",
                    (run_id,),
                )
            else:
                cur = self._conn.execute("SELECT * FROM ip_observations ORDER BY id ASC")
            return [dict(row) for row in cur.fetchall()]

    def update_ip_observation(self, observation_id: int, **fields: Any) -> int:
        """Update an ip_observations row by its ID."""
        valid_keys = [k for k in fields.keys() if k in IP_OBSERVATIONS_COLUMNS and k != "id"]
        if not valid_keys:
            return 0
        set_clause = ", ".join([f"{k} = ?" for k in valid_keys])
        params = [_serialize_value(fields[k]) for k in valid_keys]
        params.append(observation_id)
        sql = f"UPDATE ip_observations SET {set_clause} WHERE id = ?"
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def update_ip_observations_by_ip(
        self,
        ip: str,
        run_id: Optional[str] = None,
        **fields: Any,
    ) -> int:
        """Update all ip_observations rows matching an IP."""
        valid_keys = [k for k in fields.keys() if k in IP_OBSERVATIONS_COLUMNS and k != "id"]
        if not valid_keys:
            return 0
        set_clause = ", ".join([f"{k} = ?" for k in valid_keys])
        params = [_serialize_value(fields[k]) for k in valid_keys]
        params.append(ip)
        where = "WHERE ip = ?"
        if run_id is not None:
            where += " AND run_id = ?"
            params.append(run_id)
        sql = f"UPDATE ip_observations SET {set_clause} {where}"
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Execute a raw SQL query with thread lock."""
        with self._lock:
            assert self._conn is not None
            return self._conn.execute(sql, params)

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Execute query and return list of dicts."""
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        """Close database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


# Module-level convenience functions using a default store
_DEFAULT_STORE: Optional[Store] = None


def get_default_store(db_path: Union[str, Path] = "benchmark.db") -> Store:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None or _DEFAULT_STORE.db_path != str(db_path):
        _DEFAULT_STORE = Store(db_path)
    return _DEFAULT_STORE


def init(db_path: Union[str, Path] = "benchmark.db") -> Store:
    return get_default_store(db_path).init()


def insert_run(data: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
    return get_default_store().insert_run(data, **kwargs)


def upsert_session(data: Optional[Dict[str, Any]] = None, **kwargs: Any) -> int:
    return get_default_store().upsert_session(data, **kwargs)


def update_session(
    session_id: str,
    run_id: Optional[str] = None,
    *args: Any,
    **fields: Any,
) -> int:
    return get_default_store().update_session(session_id, run_id, *args, **fields)


def insert_request(
    data: Optional[Union[Dict[str, Any], Sequence[Dict[str, Any]]]] = None,
    **kwargs: Any,
) -> int:
    return get_default_store().insert_request(data, **kwargs)


def insert_place(
    data: Optional[Union[Dict[str, Any], Sequence[Dict[str, Any]]]] = None,
    **kwargs: Any,
) -> int:
    return get_default_store().insert_place(data, **kwargs)


def insert_ip_observation(
    data: Optional[Union[Dict[str, Any], Sequence[Dict[str, Any]]]] = None,
    **kwargs: Any,
) -> int:
    return get_default_store().insert_ip_observation(data, **kwargs)


def insert_event(
    data: Optional[Union[Dict[str, Any], Sequence[Dict[str, Any]]]] = None,
    **kwargs: Any,
) -> int:
    return get_default_store().insert_event(data, **kwargs)


def export_raw(
    run_id: Optional[str] = None,
    out_dir: Union[str, Path] = "results",
    db_path: Optional[Union[str, Path]] = None,
) -> Dict[str, int]:
    if db_path is not None:
        store = Store(db_path)
        try:
            return store.export_raw(run_id, out_dir)
        finally:
            store.close()
    return get_default_store().export_raw(run_id, out_dir)


def close() -> None:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is not None:
        _DEFAULT_STORE.close()
        _DEFAULT_STORE = None
