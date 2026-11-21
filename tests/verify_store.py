"""Independent verification tests for store.py and enrich.py.

Covers:
1. Schema: every table/column/index from spec exists; types/defaults sane; WAL enabled; foreign keys not required.
2. CRUD: insert run/session/request/place/ip_observation/event; update session; upsert twice; verify no duplicate/lost rows; concurrent inserts from 4 threads do not corrupt or raise.
3. export_raw: exact filenames, headers, row counts, values, and empty-run behavior. Ensure CSV handles commas/quotes/unicode and None sensibly.
4. SQL safety: parameterized statements; no string interpolation of values.
5. enrich.py: network failure/timeout returns {} and never raises; cache hit avoids network; cache is atomic and valid JSON; ASN parsing from as; network_type_guess for Jio/Airtel/T-Mobile/Comcast; batching max 100; enrich_observations actually updates the DB rows.
"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List
import aiohttp
import pytest

from enrich import (
    IpEnricher,
    classify_network_type,
    enrich,
    enrich_observations,
    parse_asn,
)
from store import (
    EVENTS_COLUMNS,
    IP_OBSERVATIONS_COLUMNS,
    PLACES_COLUMNS,
    REQUESTS_COLUMNS,
    RUNS_COLUMNS,
    SESSIONS_COLUMNS,
    Store,
)


# ── 1. Schema Verification ───────────────────────────────────────────────────


def test_schema_tables_columns_types_indices_wal_fk(tmp_path: Path) -> None:
    db_file = tmp_path / "schema_verify.db"
    store = Store(db_file)
    try:
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row

        # WAL enabled
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal", f"Expected WAL mode, got {journal_mode}"

        # Foreign keys not required (default off)
        fk_status = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk_status == 0

        # Exact schema definition from spec
        spec_schema = {
            "runs": [
                ("run_id", "TEXT", 1),
                ("started_at", "REAL", 0),
                ("finished_at", "REAL", 0),
                ("config_json", "TEXT", 0),
                ("manifest_json", "TEXT", 0),
                ("status", "TEXT", 0),
            ],
            "sessions": [
                ("run_id", "TEXT", 1),
                ("session_id", "TEXT", 2),
                ("arm", "TEXT", 0),
                ("variant", "TEXT", 0),
                ("pool", "TEXT", 0),
                ("country", "TEXT", 0),
                ("session_name", "TEXT", 0),
                ("strict_replace", "INTEGER", 0),
                ("sessasn_strict", "INTEGER", 0),
                ("sessipcollision_strict", "INTEGER", 0),
                ("sesstime_min", "INTEGER", 0),
                ("created_at", "REAL", 0),
                ("started_at", "REAL", 0),
                ("ended_at", "REAL", 0),
                ("requests", "INTEGER", 0),
                ("google_requests", "INTEGER", 0),
                ("places", "INTEGER", 0),
                ("block_events", "INTEGER", 0),
                ("first_block_request", "INTEGER", 0),
                ("first_block_ts", "REAL", 0),
                ("status", "TEXT", 0),
                ("stop_reason", "TEXT", 0),
                ("distinct_ips", "INTEGER", 0),
                ("ip_set_json", "TEXT", 0),
                ("asn_set_json", "TEXT", 0),
                ("city_set_json", "TEXT", 0),
                ("traffic_bytes", "INTEGER", 0),
                ("error", "TEXT", 0),
            ],
            "requests": [
                ("id", "INTEGER", 1),
                ("run_id", "TEXT", 0),
                ("session_id", "TEXT", 0),
                ("ts", "REAL", 0),
                ("request_index", "INTEGER", 0),
                ("query_index", "INTEGER", 0),
                ("query", "TEXT", 0),
                ("country", "TEXT", 0),
                ("kind", "TEXT", 0),
                ("page_start", "INTEGER", 0),
                ("url_hash", "TEXT", 0),
                ("status", "INTEGER", 0),
                ("outcome", "TEXT", 0),
                ("block_type", "TEXT", 0),
                ("anyip_error", "TEXT", 0),
                ("attempt", "INTEGER", 0),
                ("is_final", "INTEGER", 0),
                ("latency_ms", "REAL", 0),
                ("bytes_wire", "INTEGER", 0),
                ("bytes_rx", "INTEGER", 0),
                ("content_encoding", "TEXT", 0),
                ("content_type", "TEXT", 0),
                ("server", "TEXT", 0),
                ("error", "TEXT", 0),
                ("arm", "TEXT", 0),
                ("variant", "TEXT", 0),
            ],
            "places": [
                ("id", "INTEGER", 1),
                ("run_id", "TEXT", 0),
                ("session_id", "TEXT", 0),
                ("query", "TEXT", 0),
                ("country", "TEXT", 0),
                ("request_id", "INTEGER", 0),
                ("rank", "INTEGER", 0),
                ("place_id", "TEXT", 0),
                ("title", "TEXT", 0),
                ("category", "TEXT", 0),
                ("address", "TEXT", 0),
                ("phone", "TEXT", 0),
                ("website", "TEXT", 0),
                ("stars", "TEXT", 0),
                ("reviews", "TEXT", 0),
                ("lat", "REAL", 0),
                ("lng", "REAL", 0),
                ("validation", "TEXT", 0),
                ("arm", "TEXT", 0),
                ("variant", "TEXT", 0),
            ],
            "ip_observations": [
                ("id", "INTEGER", 1),
                ("run_id", "TEXT", 0),
                ("session_id", "TEXT", 0),
                ("ts", "REAL", 0),
                ("request_index", "INTEGER", 0),
                ("witness", "TEXT", 0),
                ("family", "TEXT", 0),
                ("ip", "TEXT", 0),
                ("asn", "TEXT", 0),
                ("org", "TEXT", 0),
                ("city", "TEXT", 0),
                ("country", "TEXT", 0),
                ("latency_ms", "REAL", 0),
                ("error", "TEXT", 0),
                ("arm", "TEXT", 0),
                ("variant", "TEXT", 0),
            ],
            "events": [
                ("id", "INTEGER", 1),
                ("run_id", "TEXT", 0),
                ("session_id", "TEXT", 0),
                ("ts", "REAL", 0),
                ("request_index", "INTEGER", 0),
                ("event_type", "TEXT", 0),
                ("detail_json", "TEXT", 0),
                ("arm", "TEXT", 0),
                ("variant", "TEXT", 0),
            ],
        }

        for table_name, expected_cols in spec_schema.items():
            table_info = conn.execute(f"PRAGMA table_info({table_name});").fetchall()
            actual_cols = [(row["name"], row["type"].upper(), row["pk"]) for row in table_info]
            assert [c[0] for c in actual_cols] == [c[0] for c in expected_cols], f"Column names mismatch for {table_name}"
            assert actual_cols == expected_cols, f"Column types/PK mismatch for {table_name}"

        # Indices
        indices = conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'").fetchall()
        idx_names = {r["name"] for r in indices}
        for expected_idx in [
            "idx_sessions_run_id",
            "idx_requests_run_sess",
            "idx_places_run_sess",
            "idx_ip_obs_run_sess",
            "idx_events_run_sess",
        ]:
            assert expected_idx in idx_names, f"Index {expected_idx} not found in schema"

        conn.close()
    finally:
        store.close()


# ── 2. CRUD, Upsert, and 4-Thread Concurrency ─────────────────────────────────


def test_crud_insert_update_upsert_duplicates(tmp_path: Path) -> None:
    db_file = tmp_path / "crud_verify.db"
    store = Store(db_file)
    try:
        # Run: insert and upsert twice
        assert store.insert_run(run_id="run_crud", started_at=100.0, status="init") == 1
        assert store.insert_run(run_id="run_crud", status="running", finished_at=120.0) >= 1
        assert store.insert_run(run_id="run_crud", status="finished") >= 1

        runs = store.fetchall("SELECT * FROM runs WHERE run_id = 'run_crud'")
        assert len(runs) == 1, "Duplicate run row created on upsert"
        assert runs[0]["status"] == "finished"
        assert runs[0]["started_at"] == 100.0
        assert runs[0]["finished_at"] == 120.0

        # Session: insert and upsert twice
        assert store.upsert_session(run_id="run_crud", session_id="s1", country="IN", requests=1) == 1
        assert store.upsert_session(run_id="run_crud", session_id="s1", requests=5, places=10) >= 1
        assert store.upsert_session(run_id="run_crud", session_id="s1", places=20) >= 1

        sess = store.fetchall("SELECT * FROM sessions WHERE run_id = 'run_crud' AND session_id = 's1'")
        assert len(sess) == 1, "Duplicate session row created on upsert"
        assert sess[0]["country"] == "IN"
        assert sess[0]["requests"] == 5
        assert sess[0]["places"] == 20

        # update_session with session_id and run_id
        assert store.update_session("s1", "run_crud", stop_reason="limit", status="done") == 1
        sess = store.fetchall("SELECT * FROM sessions WHERE session_id = 's1'")
        assert sess[0]["stop_reason"] == "limit"
        assert sess[0]["status"] == "done"

        # update_session without run_id
        assert store.update_session("s1", traffic_bytes=2048) == 1
        sess = store.fetchall("SELECT * FROM sessions WHERE session_id = 's1'")
        assert sess[0]["traffic_bytes"] == 2048

        # Requests (single & batch)
        assert store.insert_request(run_id="run_crud", session_id="s1", request_index=1, query="q1", status=200) == 1
        assert (
            store.insert_request([
                {"run_id": "run_crud", "session_id": "s1", "request_index": 2, "query": "q2", "status": 200},
                {"run_id": "run_crud", "session_id": "s1", "request_index": 3, "query": "q3", "status": 200},
            ])
            == 2
        )
        reqs = store.fetchall("SELECT * FROM requests WHERE run_id = 'run_crud'")
        assert len(reqs) == 3

        # Places (single & batch)
        assert store.insert_place(run_id="run_crud", session_id="s1", rank=1, title="Place 1") == 1
        assert (
            store.insert_place([
                {"run_id": "run_crud", "session_id": "s1", "rank": 2, "title": "Place 2"},
                {"run_id": "run_crud", "session_id": "s1", "rank": 3, "title": "Place 3"},
            ])
            == 2
        )
        places = store.fetchall("SELECT * FROM places WHERE run_id = 'run_crud'")
        assert len(places) == 3

        # IP Observations (single & batch)
        assert store.insert_ip_observation(run_id="run_crud", session_id="s1", ip="1.1.1.1", witness="w1") == 1
        assert (
            store.insert_ip_observation([
                {"run_id": "run_crud", "session_id": "s1", "ip": "2.2.2.2", "witness": "w2"},
                {"run_id": "run_crud", "session_id": "s1", "ip": "3.3.3.3", "witness": "w3"},
            ])
            == 2
        )
        obs = store.fetchall("SELECT * FROM ip_observations WHERE run_id = 'run_crud'")
        assert len(obs) == 3

        # Events (single & batch)
        assert store.insert_event(run_id="run_crud", session_id="s1", event_type="start") == 1
        assert (
            store.insert_event([
                {"run_id": "run_crud", "session_id": "s1", "event_type": "probe"},
                {"run_id": "run_crud", "session_id": "s1", "event_type": "stop"},
            ])
            == 2
        )
        events = store.fetchall("SELECT * FROM events WHERE run_id = 'run_crud'")
        assert len(events) == 3
    finally:
        store.close()


def test_concurrent_inserts_4_threads(tmp_path: Path) -> None:
    db_file = tmp_path / "concurrent_verify.db"
    store = Store(db_file)
    num_threads = 4
    items_per_thread = 50
    exceptions: list[Exception] = []

    try:
        store.insert_run(run_id="conc_run", status="running")

        def worker(tid: int) -> None:
            try:
                sess_id = f"sess_thread_{tid}"
                store.upsert_session(run_id="conc_run", session_id=sess_id, requests=0, places=0)
                for i in range(items_per_thread):
                    store.insert_request(
                        run_id="conc_run",
                        session_id=sess_id,
                        request_index=i,
                        query=f"query_{tid}_{i}",
                        status=200,
                        outcome="ok",
                    )
                    store.insert_place(
                        run_id="conc_run",
                        session_id=sess_id,
                        rank=i,
                        title=f"Place {tid} {i}",
                    )
                    store.insert_ip_observation(
                        run_id="conc_run",
                        session_id=sess_id,
                        ip=f"10.{tid}.0.{i}",
                        witness="probe",
                    )
                    store.insert_event(
                        run_id="conc_run",
                        session_id=sess_id,
                        event_type="heartbeat",
                        detail_json={"tid": tid, "i": i},
                    )
                store.update_session(sess_id, "conc_run", requests=items_per_thread, places=items_per_thread)
            except Exception as e:
                exceptions.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not exceptions, f"Exceptions in concurrent threads: {exceptions}"

        # Verify exact counts and no corrupted rows
        sess_rows = store.fetchall("SELECT * FROM sessions WHERE run_id = 'conc_run'")
        assert len(sess_rows) == num_threads
        for s in sess_rows:
            assert s["requests"] == items_per_thread
            assert s["places"] == items_per_thread

        assert len(store.fetchall("SELECT * FROM requests WHERE run_id = 'conc_run'")) == num_threads * items_per_thread
        assert len(store.fetchall("SELECT * FROM places WHERE run_id = 'conc_run'")) == num_threads * items_per_thread
        assert len(store.fetchall("SELECT * FROM ip_observations WHERE run_id = 'conc_run'")) == num_threads * items_per_thread
        assert len(store.fetchall("SELECT * FROM events WHERE run_id = 'conc_run'")) == num_threads * items_per_thread
    finally:
        store.close()


# ── 3. export_raw Verification ────────────────────────────────────────────────


def test_export_raw_filenames_headers_counts_values(tmp_path: Path) -> None:
    db_file = tmp_path / "export_verify.db"
    out_dir = tmp_path / "export_out"
    store = Store(db_file)
    try:
        # Empty run behavior
        empty_counts = store.export_raw(run_id="empty_run", out_dir=out_dir / "empty")
        assert empty_counts == {
            "sessions": 0,
            "requests": 0,
            "places": 0,
            "ip_observations": 0,
            "events": 0,
        }

        expected_headers = {
            "sessions.csv": SESSIONS_COLUMNS,
            "requests.csv": REQUESTS_COLUMNS,
            "places.csv": PLACES_COLUMNS,
            "ip_observations.csv": IP_OBSERVATIONS_COLUMNS,
            "events.csv": EVENTS_COLUMNS,
        }
        for fname, exp_cols in expected_headers.items():
            fpath = out_dir / "empty" / fname
            assert fpath.exists(), f"File {fname} missing from empty export"
            with open(fpath, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                assert header == exp_cols, f"Header mismatch in {fname}"
                assert len(list(reader)) == 0

        # Populate with unicode, quotes, commas, and None
        store.insert_run(run_id="run_exp", status="completed")
        store.upsert_session(
            run_id="run_exp",
            session_id="s_exp",
            session_name='Session, "quoted" & Café ☕',
            country="FR",
            error=None,
        )
        store.insert_request(
            run_id="run_exp",
            session_id="s_exp",
            query='café, "fine" & 日本語',
            status=200,
            error=None,
        )
        store.insert_place(
            run_id="run_exp",
            session_id="s_exp",
            title='L\'Étoile, Café "Deluxe" München',
            address="123 Rue, Paris, France",
            stars=None,
        )
        store.insert_ip_observation(
            run_id="run_exp",
            session_id="s_exp",
            ip="2001:db8::1",
            city="München, Bayern",
            error=None,
        )
        store.insert_event(
            run_id="run_exp",
            session_id="s_exp",
            event_type="test_event",
            detail_json={"note": 'Special, "note"'},
        )

        counts = store.export_raw(run_id="run_exp", out_dir=out_dir / "data")
        assert counts == {
            "sessions": 1,
            "requests": 1,
            "places": 1,
            "ip_observations": 1,
            "events": 1,
        }

        # Check values and None handling
        with open(out_dir / "data" / "sessions.csv", "r", encoding="utf-8") as f:
            row = list(csv.DictReader(f))[0]
            assert row["session_name"] == 'Session, "quoted" & Café ☕'
            assert row["error"] == ""

        with open(out_dir / "data" / "requests.csv", "r", encoding="utf-8") as f:
            row = list(csv.DictReader(f))[0]
            assert row["query"] == 'café, "fine" & 日本語'
            assert row["error"] == ""

        with open(out_dir / "data" / "places.csv", "r", encoding="utf-8") as f:
            row = list(csv.DictReader(f))[0]
            assert row["title"] == 'L\'Étoile, Café "Deluxe" München'
            assert row["stars"] == ""

        with open(out_dir / "data" / "ip_observations.csv", "r", encoding="utf-8") as f:
            row = list(csv.DictReader(f))[0]
            assert row["city"] == "München, Bayern"
            assert row["error"] == ""

        with open(out_dir / "data" / "events.csv", "r", encoding="utf-8") as f:
            row = list(csv.DictReader(f))[0]
            assert json.loads(row["detail_json"]) == {"note": 'Special, "note"'}
    finally:
        store.close()


# ── 4. SQL Safety Verification ────────────────────────────────────────────────


def test_sql_safety_parameterized(tmp_path: Path) -> None:
    db_file = tmp_path / "sql_safety.db"
    store = Store(db_file)
    try:
        injection_val = "'; DROP TABLE runs; DROP TABLE sessions; --"

        store.insert_run(run_id=injection_val, status=injection_val)
        store.upsert_session(run_id=injection_val, session_id=injection_val, status=injection_val)
        store.update_session(injection_val, injection_val, stop_reason=injection_val)
        store.insert_request(run_id=injection_val, session_id=injection_val, query=injection_val)
        store.insert_place(run_id=injection_val, session_id=injection_val, title=injection_val)
        store.insert_ip_observation(run_id=injection_val, session_id=injection_val, ip=injection_val)
        store.insert_event(run_id=injection_val, session_id=injection_val, event_type=injection_val)

        # Malicious column names in kwargs
        bad_col = "status; DROP TABLE places; --"
        store.insert_run(run_id="run_safe", **{bad_col: "val"})
        store.upsert_session(run_id="run_safe", session_id="s_safe", **{bad_col: "val"})
        store.update_session("s_safe", "run_safe", **{bad_col: "val"})

        # Verify all tables intact
        tables = {r["name"] for r in store.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
        for tbl in ["runs", "sessions", "requests", "places", "ip_observations", "events"]:
            assert tbl in tables, f"Table {tbl} was dropped!"

        # Verify value saved literally
        run_row = store.fetchall("SELECT * FROM runs WHERE run_id = ?", (injection_val,))
        assert len(run_row) == 1
        assert run_row[0]["status"] == injection_val
    finally:
        store.close()


# ── 5. enrich.py Verification ─────────────────────────────────────────────────


def test_enrich_network_failure_and_timeout(tmp_path: Path) -> None:
    async def _run() -> None:
        enricher = IpEnricher(cache_path=tmp_path / "cache_fail.json", enabled=True)

        # Timeout
        async def mock_timeout(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.TimeoutError("Timeout")

        enricher._post_batch = mock_timeout  # type: ignore
        res_timeout = await enricher.enrich(["1.2.3.4"])
        assert res_timeout == {"1.2.3.4": {}}

        # Connector error
        async def mock_conn_err(*args: Any, **kwargs: Any) -> Any:
            raise aiohttp.ClientConnectorError(connection_key=None, os_error=OSError("Unreachable"))  # type: ignore

        enricher._post_batch = mock_conn_err  # type: ignore
        res_conn = await enricher.enrich(["5.6.7.8"])
        assert res_conn == {"5.6.7.8": {}}

        # HTTP error
        async def mock_http_err(*args: Any, **kwargs: Any) -> Any:
            return []

        enricher._post_batch = mock_http_err  # type: ignore
        res_http = await enricher.enrich(["9.10.11.12"])
        assert res_http == {"9.10.11.12": {}}

    asyncio.run(_run())


def test_enrich_cache_hit_avoids_network(tmp_path: Path) -> None:
    async def _run() -> None:
        cache_path = tmp_path / "cache_hit.json"
        cache_path.write_text(
            json.dumps({
                "1.1.1.1": {
                    "asn": "AS13335",
                    "org": "Cloudflare",
                    "isp": "Cloudflare",
                    "city": "Sydney",
                    "country": "Australia",
                    "network_type_guess": "residential_or_other",
                }
            }),
            encoding="utf-8",
        )

        enricher = IpEnricher(cache_path=cache_path, enabled=True)

        async def fail_on_network(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Network called for cached IP")

        enricher._post_batch = fail_on_network  # type: ignore
        res = await enricher.enrich(["1.1.1.1"])
        assert res["1.1.1.1"]["asn"] == "AS13335"

        # Partial hit
        network_calls: list[list[str]] = []

        async def partial_network(session: Any, batch_ips: list[str]) -> list[dict[str, Any]]:
            network_calls.append(list(batch_ips))
            return [{
                "query": "2.2.2.2",
                "status": "success",
                "as": "AS1234 Test",
                "org": "Test",
                "isp": "Test",
                "city": "City",
                "country": "Country",
            }]

        enricher._post_batch = partial_network  # type: ignore
        res_mixed = await enricher.enrich(["1.1.1.1", "2.2.2.2"])
        assert network_calls == [["2.2.2.2"]]
        assert res_mixed["1.1.1.1"]["asn"] == "AS13335"
        assert res_mixed["2.2.2.2"]["asn"] == "AS1234"

    asyncio.run(_run())


def test_enrich_cache_atomic_valid_json(tmp_path: Path) -> None:
    async def _run() -> None:
        cache_path = tmp_path / "cache_atomic.json"
        enricher = IpEnricher(cache_path=cache_path, enabled=True)

        async def mock_batch(session: Any, batch_ips: list[str]) -> list[dict[str, Any]]:
            return [{
                "query": ip,
                "status": "success",
                "as": "AS55836 Jio",
                "org": "Reliance Jio",
                "isp": "Jio",
                "city": "Mumbai",
                "country": "India",
            } for ip in batch_ips]

        enricher._post_batch = mock_batch  # type: ignore
        await enricher.enrich(["49.32.1.1", "49.32.1.2"])

        assert cache_path.exists()
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "49.32.1.1" in data
        assert "49.32.1.2" in data
        assert data["49.32.1.1"]["asn"] == "AS55836"

    asyncio.run(_run())


def test_enrich_asn_parsing() -> None:
    cases = [
        ("AS55836 Reliance Jio Infocomm Limited", "AS55836"),
        ("AS9498 BHARTI Airtel Ltd.", "AS9498"),
        ("AS21928 T-Mobile USA, Inc.", "AS21928"),
        ("AS7922 Comcast Cable Communications, LLC", "AS7922"),
        ("as12345 lowercase test", "AS12345"),
        ("AS9999", "AS9999"),
        ("Unknown ISP Without ASN", ""),
        ("", ""),
        (None, ""),
    ]
    for text, expected in cases:
        assert parse_asn(text) == expected, f"Failed for input: {text!r}"


def test_enrich_network_type_guess() -> None:
    # Jio
    assert classify_network_type(org="Reliance Jio Infocomm", isp="Jio", asname="JIO") == "mobile"
    assert classify_network_type(org="Jio Platforms") == "mobile"

    # Airtel
    assert classify_network_type(org="Bharti Airtel Limited") == "mobile"
    assert classify_network_type(isp="Airtel Broadband") == "mobile"

    # T-Mobile
    assert classify_network_type(org="T-Mobile USA, Inc.") == "mobile"
    assert classify_network_type(isp="T-Mobile") == "mobile"

    # Comcast -> residential_or_other
    assert classify_network_type(org="Comcast Cable Communications, LLC", isp="Xfinity", asname="COMCAST") == "residential_or_other"
    assert classify_network_type(org="Comcast Business") == "residential_or_other"
    assert classify_network_type(isp="Comcast") == "residential_or_other"

    # Vodafone / Vi
    assert classify_network_type(org="Vodafone Idea Limited") == "mobile"
    assert classify_network_type(isp="Vi India") == "mobile"

    # Verizon Wireless
    assert classify_network_type(isp="Verizon Wireless") == "mobile"
    assert classify_network_type(org="Verizon Communications Inc.") == "residential_or_other"

    # AT&T Mobility
    assert classify_network_type(org="AT&T Mobility LLC") == "mobile"
    assert classify_network_type(org="AT&T Services, Inc.") == "residential_or_other"


def test_enrich_batching_max_100(tmp_path: Path) -> None:
    async def _run() -> None:
        enricher = IpEnricher(cache_path=tmp_path / "cache_batch.json", enabled=True)
        batch_sizes: list[int] = []

        async def mock_batch(session: Any, batch_ips: list[str]) -> list[dict[str, Any]]:
            batch_sizes.append(len(batch_ips))
            return [{
                "query": ip,
                "status": "success",
                "as": "AS1 Test",
                "org": "Org",
                "isp": "ISP",
                "city": "City",
                "country": "Country",
            } for ip in batch_ips]

        enricher._post_batch = mock_batch  # type: ignore

        ips = [f"192.168.{i // 256}.{i % 256}" for i in range(250)]
        results = await enricher.enrich(ips)

        assert len(results) == 250
        assert batch_sizes == [100, 100, 50], f"Expected batch sizes [100, 100, 50], got {batch_sizes}"

    asyncio.run(_run())


def test_enrich_observations_updates_db(tmp_path: Path) -> None:
    async def _run() -> None:
        db_file = tmp_path / "enrich_db.db"
        store = Store(db_file)
        enricher = IpEnricher(cache_path=tmp_path / "cache_obs.json", enabled=True)

        try:
            store.insert_run(run_id="run_e", status="running")
            store.upsert_session(run_id="run_e", session_id="s1")

            store.insert_ip_observation(run_id="run_e", session_id="s1", ip="49.32.1.1", witness="ipify_v4")
            store.insert_ip_observation(run_id="run_e", session_id="s1", ip="49.32.1.1", witness="amazon")
            store.insert_ip_observation(run_id="run_e", session_id="s1", ip="24.1.2.3", witness="ipify_v4")

            async def mock_batch(session: Any, batch_ips: list[str]) -> list[dict[str, Any]]:
                resp = []
                for ip in batch_ips:
                    if ip == "49.32.1.1":
                        resp.append({
                            "query": ip,
                            "status": "success",
                            "as": "AS55836 Reliance Jio",
                            "org": "Reliance Jio Infocomm",
                            "city": "Mumbai",
                            "country": "India",
                        })
                    elif ip == "24.1.2.3":
                        resp.append({
                            "query": ip,
                            "status": "success",
                            "as": "AS7922 Comcast Cable Communications",
                            "org": "Comcast Cable Communications",
                            "city": "Chicago",
                            "country": "United States",
                        })
                return resp

            enricher._post_batch = mock_batch  # type: ignore

            updated = await enrich_observations(store, run_id="run_e", enricher=enricher)
            assert updated == 3

            obs = store.fetchall("SELECT * FROM ip_observations WHERE run_id = 'run_e' ORDER BY id ASC")
            assert len(obs) == 3
            assert obs[0]["asn"] == "AS55836"
            assert obs[0]["city"] == "Mumbai"
            assert obs[1]["asn"] == "AS55836"
            assert obs[1]["city"] == "Mumbai"
            assert obs[2]["asn"] == "AS7922"
            assert obs[2]["city"] == "Chicago"
        finally:
            store.close()

    asyncio.run(_run())
