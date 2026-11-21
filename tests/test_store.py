"""Tests for store.py and enrich.py."""

import asyncio
import csv
import json
from pathlib import Path
import threading
import time
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


# ── Store Tests ───────────────────────────────────────────────────────────────


def test_store_init_and_schema(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    store = Store(db_file)
    try:
        # Check WAL mode
        res = store.fetchall("PRAGMA journal_mode;")
        assert res[0]["journal_mode"].lower() == "wal"

        # Check all tables exist and columns match exactly
        tables_expected = {
            "runs": RUNS_COLUMNS,
            "sessions": SESSIONS_COLUMNS,
            "requests": REQUESTS_COLUMNS,
            "places": PLACES_COLUMNS,
            "ip_observations": IP_OBSERVATIONS_COLUMNS,
            "events": EVENTS_COLUMNS,
        }

        for table_name, expected_cols in tables_expected.items():
            cols_info = store.fetchall(f"PRAGMA table_info({table_name});")
            actual_cols = [c["name"] for c in cols_info]
            assert actual_cols == expected_cols, f"Mismatch in columns for {table_name}"
    finally:
        store.close()


def test_store_crud_and_upsert(tmp_path: Path) -> None:
    db_file = tmp_path / "crud.db"
    store = Store(db_file)
    try:
        # 1. Run insert
        r1 = store.insert_run(
            run_id="run_001",
            started_at=1000.0,
            status="running",
            config_json={"depth": 1},
        )
        assert r1 == 1

        # Duplicate run_id updates existing
        r2 = store.insert_run(
            run_id="run_001",
            status="completed",
            finished_at=1020.0,
        )
        assert r2 >= 1

        runs = store.fetchall("SELECT * FROM runs WHERE run_id = ?", ("run_001",))
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"
        assert runs[0]["finished_at"] == 1020.0
        assert json.loads(runs[0]["config_json"]) == {"depth": 1}

        # 2. Session upsert and update
        s1 = store.upsert_session(
            run_id="run_001",
            session_id="sess_a",
            arm="residential",
            pool="residential",
            requests=5,
            places=10,
        )
        assert s1 == 1

        # Upsert with new place count
        s2 = store.upsert_session(
            run_id="run_001",
            session_id="sess_a",
            places=20,
            status="done",
        )
        assert s2 >= 1

        # update_session by fields
        u1 = store.update_session(
            session_id="sess_a",
            run_id="run_001",
            stop_reason="limit",
            traffic_bytes=1048576,
            ip_set_json=["1.1.1.1", "2.2.2.2"],
        )
        assert u1 == 1

        sess = store.fetchall("SELECT * FROM sessions WHERE session_id = ?", ("sess_a",))
        assert len(sess) == 1
        assert sess[0]["places"] == 20
        assert sess[0]["status"] == "done"
        assert sess[0]["stop_reason"] == "limit"
        assert sess[0]["traffic_bytes"] == 1048576
        assert json.loads(sess[0]["ip_set_json"]) == ["1.1.1.1", "2.2.2.2"]

        # 3. Request insert (single + batch)
        req_count_1 = store.insert_request(
            run_id="run_001",
            session_id="sess_a",
            request_index=1,
            url="https://google.com/maps?q=tea",
            status=200,
            outcome="ok",
            attempt=0,
            is_final=1,
        )
        assert req_count_1 == 1

        req_batch = [
            {
                "run_id": "run_001",
                "session_id": "sess_a",
                "request_index": 2,
                "url": "https://google.com/maps?q=coffee",
                "status": 200,
                "outcome": "ok",
                "attempt": 0,
                "is_final": 1,
            },
            {
                "run_id": "run_001",
                "session_id": "sess_a",
                "request_index": 3,
                "url": "https://google.com/maps?q=bakery",
                "status": 429,
                "outcome": "429",
                "attempt": 0,
                "is_final": 1,
            },
        ]
        req_count_batch = store.insert_request(req_batch)
        assert req_count_batch == 2

        all_reqs = store.fetchall("SELECT * FROM requests WHERE run_id = ?", ("run_001",))
        assert len(all_reqs) == 3
        # Check url_hash was automatically populated
        assert all_reqs[0]["url_hash"] is not None and len(all_reqs[0]["url_hash"]) > 0

        # 4. Places insert (single + batch)
        p_count_1 = store.insert_place(
            run_id="run_001",
            session_id="sess_a",
            query="tea",
            rank=1,
            place_id="ChIJ123",
            title="Good Tea",
        )
        assert p_count_1 == 1

        p_batch = [
            {
                "run_id": "run_001",
                "session_id": "sess_a",
                "query": "tea",
                "rank": 2,
                "place_id": "ChIJ456",
                "title": "Better Tea",
            },
            {
                "run_id": "run_001",
                "session_id": "sess_a",
                "query": "tea",
                "rank": 3,
                "place_id": "ChIJ789",
                "title": "Best Tea",
            },
        ]
        p_count_batch = store.insert_place(p_batch)
        assert p_count_batch == 2

        places = store.fetchall("SELECT * FROM places WHERE run_id = ?", ("run_001",))
        assert len(places) == 3

        # 5. IP observation insert
        ip_count_1 = store.insert_ip_observation(
            run_id="run_001",
            session_id="sess_a",
            ts=1001.0,
            witness="ipify_v4",
            family="v4",
            ip="123.45.67.89",
        )
        assert ip_count_1 == 1

        ip_batch = [
            {
                "run_id": "run_001",
                "session_id": "sess_a",
                "ts": 1002.0,
                "witness": "ipify_v6",
                "family": "v6",
                "ip": "2001:db8::1",
            }
        ]
        assert store.insert_ip_observation(ip_batch) == 1

        obs = store.fetchall("SELECT * FROM ip_observations WHERE run_id = ?", ("run_001",))
        assert len(obs) == 2

        # 6. Events insert
        ev_count = store.insert_event(
            run_id="run_001",
            session_id="sess_a",
            ts=1005.0,
            event_type="ip_change",
            detail_json={"old": "123.45.67.89", "new": "98.76.54.32"},
        )
        assert ev_count == 1
        events = store.fetchall("SELECT * FROM events WHERE run_id = ?", ("run_001",))
        assert len(events) == 1
        assert events[0]["event_type"] == "ip_change"
    finally:
        store.close()


def test_store_concurrent_threads(tmp_path: Path) -> None:
    db_file = tmp_path / "concurrent.db"
    store = Store(db_file)
    try:
        store.insert_run(run_id="conc_run", status="running")

        def worker(worker_id: int) -> None:
            sess_id = f"sess_{worker_id}"
            store.upsert_session(run_id="conc_run", session_id=sess_id, requests=0)
            for i in range(10):
                store.insert_request(
                    run_id="conc_run",
                    session_id=sess_id,
                    request_index=i,
                    query="test",
                    status=200,
                )
                store.insert_place(
                    run_id="conc_run",
                    session_id=sess_id,
                    rank=i,
                    title=f"Place {worker_id}-{i}",
                )
            store.update_session(sess_id, "conc_run", requests=10)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        sessions = store.fetchall("SELECT * FROM sessions WHERE run_id = 'conc_run'")
        assert len(sessions) == 5
        requests = store.fetchall("SELECT * FROM requests WHERE run_id = 'conc_run'")
        assert len(requests) == 50
        places = store.fetchall("SELECT * FROM places WHERE run_id = 'conc_run'")
        assert len(places) == 50
    finally:
        store.close()


def test_export_raw_counts_and_csv_columns(tmp_path: Path) -> None:
    db_file = tmp_path / "export_test.db"
    out_dir = tmp_path / "raw_export"
    store = Store(db_file)
    try:
        # Populate run 1
        store.insert_run(run_id="run_1", status="done")
        store.upsert_session(run_id="run_1", session_id="s1", country="IN", requests=2)
        store.insert_request(run_id="run_1", session_id="s1", query="tea", status=200)
        store.insert_request(run_id="run_1", session_id="s1", query="coffee", status=200)
        store.insert_place(run_id="run_1", session_id="s1", title="Tea Stall 1")
        store.insert_ip_observation(run_id="run_1", session_id="s1", ip="1.2.3.4")
        store.insert_event(run_id="run_1", session_id="s1", event_type="start")

        # Populate run 2 (should not be in run 1's export)
        store.insert_run(run_id="run_2", status="done")
        store.upsert_session(run_id="run_2", session_id="s2", country="US", requests=1)
        store.insert_request(run_id="run_2", session_id="s2", query="pizza", status=200)

        # Export run 1
        counts = store.export_raw(run_id="run_1", out_dir=out_dir)

        assert counts == {
            "sessions": 1,
            "requests": 2,
            "places": 1,
            "ip_observations": 1,
            "events": 1,
        }

        # Check files and exact columns
        expected_files = {
            "sessions.csv": (SESSIONS_COLUMNS, 1),
            "requests.csv": (REQUESTS_COLUMNS, 2),
            "places.csv": (PLACES_COLUMNS, 1),
            "ip_observations.csv": (IP_OBSERVATIONS_COLUMNS, 1),
            "events.csv": (EVENTS_COLUMNS, 1),
        }

        for filename, (expected_cols, expected_rows) in expected_files.items():
            csv_path = out_dir / filename
            assert csv_path.exists(), f"{filename} does not exist"
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                assert header == expected_cols, f"Columns in {filename} do not match schema"
                rows = list(reader)
                assert len(rows) == expected_rows, f"Row count in {filename} mismatch"

        # Check export all (run_id=None)
        out_all = tmp_path / "raw_export_all"
        counts_all = store.export_raw(run_id=None, out_dir=out_all)
        assert counts_all["sessions"] == 2
        assert counts_all["requests"] == 3
    finally:
        store.close()


# ── Enrich Tests ─────────────────────────────────────────────────────────────


def test_parse_asn() -> None:
    assert parse_asn("AS55836 Reliance Jio Infocomm Limited") == "AS55836"
    assert parse_asn("AS7018 AT&T Services, Inc.") == "AS7018"
    assert parse_asn("as12345 lowercase test") == "AS12345"
    assert parse_asn("No asn here") == ""
    assert parse_asn("") == ""
    assert parse_asn(None) == ""


def test_classify_network_type() -> None:
    assert (
        classify_network_type(
            org="Reliance Jio Infocomm", isp="Jio", asname="JIO"
        )
        == "mobile"
    )
    assert classify_network_type(org="Bharti Airtel Limited") == "mobile"
    assert classify_network_type(org="Vodafone Idea Limited", isp="Vi") == "mobile"
    assert classify_network_type(asname="T-Mobile USA, Inc.") == "mobile"
    assert classify_network_type(isp="Verizon Wireless") == "mobile"
    assert classify_network_type(org="AT&T Mobility LLC") == "mobile"
    assert classify_network_type(org="Fast 5G Cellular Networks") == "mobile"
    assert classify_network_type(org="Local Wireless ISP") == "mobile"

    # Non-mobile (residential / datacenters / enterprise)
    assert (
        classify_network_type(
            org="Comcast Cable Communications", isp="Xfinity", asname="Comcast"
        )
        == "residential_or_other"
    )
    assert (
        classify_network_type(
            org="Google LLC", isp="Google", asname="GOOGLE"
        )
        == "residential_or_other"
    )


def test_enrich_disabled(tmp_path: Path) -> None:
    async def _run() -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(
            json.dumps(
                {
                    "1.1.1.1": {
                        "asn": "AS13335",
                        "org": "Cloudflare",
                        "isp": "Cloudflare",
                        "city": "Brisbane",
                        "country": "Australia",
                        "network_type_guess": "residential_or_other",
                    }
                }
            ),
            encoding="utf-8",
        )

        enricher = IpEnricher(cache_path=cache_path, enabled=False)
        # 1.1.1.1 is cached, 2.2.2.2 is uncached
        res = await enricher.enrich(["1.1.1.1", "2.2.2.2"])
        assert res["1.1.1.1"]["asn"] == "AS13335"
        assert res["2.2.2.2"] == {}

    asyncio.run(_run())


def test_enrich_no_network_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        cache_path = tmp_path / "cache.json"
        enricher = IpEnricher(cache_path=cache_path, enabled=True)

        # Simulate network crash
        async def mock_post_batch(session: Any, batch: Any) -> Any:
            raise aiohttp.ClientConnectorError(
                connection_key=None,  # type: ignore
                os_error=OSError("Network unreachable"),
            )

        monkeypatch.setattr(enricher, "_post_batch", mock_post_batch)

        # Must NEVER raise and return {} per IP
        res = await enricher.enrich(["8.8.8.8", "9.9.9.9"])
        assert res == {"8.8.8.8": {}, "9.9.9.9": {}}

    asyncio.run(_run())


def test_enrich_batching_and_rate_limiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        cache_path = tmp_path / "cache.json"
        enricher = IpEnricher(cache_path=cache_path, enabled=True)

        calls: List[List[str]] = []
        call_timestamps: List[float] = []

        async def mock_post_batch(session: Any, batch_ips: List[str]) -> List[Dict[str, Any]]:
            # Enforce the same rate limit pacing logic inside mock or test enricher's pacing
            now = time.monotonic()
            elapsed = now - enricher._last_request_time
            if enricher._last_request_time > 0 and elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
            enricher._last_request_time = time.monotonic()
            call_timestamps.append(time.monotonic())
            calls.append(list(batch_ips))

            response = []
            for ip in batch_ips:
                response.append(
                    {
                        "query": ip,
                        "status": "success",
                        "as": f"AS{ip.replace('.', '')} Jio Mobile",
                        "asname": "JIO",
                        "org": "Reliance Jio",
                        "isp": "Jio",
                        "city": "Mumbai",
                        "country": "India",
                    }
                )
            return response

        monkeypatch.setattr(enricher, "_post_batch", mock_post_batch)

        # Provide 150 IPs to test batching > 100
        ips = [f"10.0.{i // 256}.{i % 256}" for i in range(150)]
        results = await enricher.enrich(ips)

        # Should have made exactly 2 batches: 100 and 50
        assert len(calls) == 2
        assert len(calls[0]) == 100
        assert len(calls[1]) == 50

        # Verify rate limit pacing (at least 0.9s between calls)
        assert len(call_timestamps) == 2
        time_diff = call_timestamps[1] - call_timestamps[0]
        assert time_diff >= 0.9, f"Batch requests too fast: {time_diff}s"

        # Verify enrichment classification
        assert len(results) == 150
        for ip in ips:
            assert ip in results
            assert results[ip]["asn"].startswith("AS")
            assert results[ip]["network_type_guess"] == "mobile"
            assert results[ip]["city"] == "Mumbai"

        # Verify atomic cache persistence
        assert cache_path.exists()
        with open(cache_path, "r", encoding="utf-8") as f:
            saved_cache = json.load(f)
        assert len(saved_cache) == 150

    asyncio.run(_run())


def test_enrich_observations_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        db_file = tmp_path / "obs.db"
        cache_path = tmp_path / "cache.json"
        store = Store(db_file)
        enricher = IpEnricher(cache_path=cache_path, enabled=True)

        try:
            store.insert_run(run_id="run_enrich", status="running")
            store.upsert_session(run_id="run_enrich", session_id="s1")
            store.insert_ip_observation(
                run_id="run_enrich",
                session_id="s1",
                ip="49.32.1.1",
                witness="ipify_v4",
                family="v4",
            )
            store.insert_ip_observation(
                run_id="run_enrich",
                session_id="s1",
                ip="49.32.1.1",
                witness="amazon",
                family="v4",
            )

            async def mock_post_batch(session: Any, batch_ips: List[str]) -> List[Dict[str, Any]]:
                return [
                    {
                        "query": "49.32.1.1",
                        "status": "success",
                        "as": "AS55836 Reliance Jio",
                        "asname": "JIO",
                        "org": "Reliance Jio Infocomm",
                        "isp": "Jio",
                        "city": "Delhi",
                        "country": "India",
                    }
                ]

            monkeypatch.setattr(enricher, "_post_batch", mock_post_batch)

            updated = await enrich_observations(store, run_id="run_enrich", enricher=enricher)
            assert updated == 2

            obs = store.fetchall(
                "SELECT * FROM ip_observations WHERE run_id = 'run_enrich' ORDER BY id ASC"
            )
            assert len(obs) == 2
            for r in obs:
                assert r["asn"] == "AS55836"
                assert r["org"] == "Reliance Jio Infocomm"
                assert r["city"] == "Delhi"
                assert r["country"] == "India"
        finally:
            store.close()

    asyncio.run(_run())
