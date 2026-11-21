"""
Verification test suite for AnyIP experiment benchmark.
Created by independent verifier in accordance with anyip-spec.md.
"""

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from analyze import run_metrics, write_outputs
from config import ProxyConfig
from experiment import (
    ArmConfig,
    RunConfig,
    _quota_monitor_task,
    build_manifest,
    load_config,
    load_panel,
    reconcile_session,
    record_query_result,
    run_experiment,
)
from scraper import QueryResult, RequestRecord, IpObservation
from store import Store, export_raw


class TestVerificationDefects:
    """Test suite reproducing and documenting discovered defects and verifications."""

    def test_first_block_request_query_reset_bug(self, tmp_path):
        """
        Bug: in experiment.py lines 505-511, first_block_req is set from req.request_index.
        However, req.request_index resets to 1 for every query in scraper.py.
        If a session runs Query 1 (2 requests, OK) and Query 2 (blocked on request 2),
        cumulative session requests = 4, but first_block_request is recorded as 2!
        """
        db_path = tmp_path / "test_block.db"
        store = Store(db_path)
        store.init()
        run_id = "test_run"
        session_id = "sess_1"

        store.upsert_session({
            "run_id": run_id,
            "session_id": session_id,
            "arm": "block_pilot",
            "variant": "default",
            "pool": "residential",
            "country": "US",
            "session_name": session_id,
            "status": "running",
        })

        # Query 1: 2 requests, both OK
        res1 = QueryResult(
            query="q1",
            country="US",
            proxy_session_id="sess_1",
            requests=[
                RequestRecord(url="u1", status=200, outcome="ok", latency_ms=10.0, bytes_rx=100, request_index=1, is_final=True),
                RequestRecord(url="u2", status=200, outcome="ok", latency_ms=10.0, bytes_rx=100, request_index=2, is_final=True),
            ],
            places=[{"id": "p1"}],
        )

        # Query 2: 2 requests, request 2 is blocked (403)
        res2 = QueryResult(
            query="q2",
            country="US",
            proxy_session_id="sess_1",
            requests=[
                RequestRecord(url="u3", status=200, outcome="ok", latency_ms=10.0, bytes_rx=100, request_index=1, is_final=True),
                RequestRecord(url="u4", status=403, outcome="403", latency_ms=10.0, bytes_rx=100, request_index=2, is_final=True),
            ],
            places=[],
            stop_reason="block",
            block_at_request=2,
        )

        # Reconcile session
        reconcile_session(
            store=store,
            run_id=run_id,
            session_id=session_id,
            arm_name="block_pilot",
            variant_name="default",
            pool="residential",
            country="US",
            session_name=session_id,
            sesstime_min=20,
            started_at=100.0,
            ended_at=110.0,
            status="completed",
            stop_reason="block",
            results=[res1, res2],
        )

        row = store._conn.execute("SELECT requests, google_requests, block_events, first_block_request FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        store.close()

        # Cumulative requests made in this session: 2 + 2 = 4
        print(f"Observed first_block_request: {row['first_block_request']}, Expected cumulative: 4")
        assert row["block_events"] == 1
        assert row["first_block_request"] == 4

    def test_variance_pool_local_crash(self, tmp_path):
        """
        Bug: run_variance hardcodes use_proxy=True and passes proxy_type=arm.pool.
        When arm.pool == 'local', ProxyConfig.sticky_url raises ValueError:
        'Invalid proxy_type: local'.
        """
        cfg = ProxyConfig(proxy_user="test", proxy_pass="pass")
        with pytest.raises(ValueError, match="Invalid proxy_type: 'local'"):
            cfg.sticky_url(session_id="abc123", country="US", proxy_type="local")

    def test_query_success_rate_mismatch_in_summary(self, tmp_path):
        """
        Bug: in analyze.py line 1518, summary.json['query_success_rate_pct'] is assigned
        rates.get('success_rate_pct', 0.0), which is the request success rate, NOT query success rate!
        """
        db_path = tmp_path / "test_rate.db"
        store = Store(db_path)
        store.init()
        run_id = "test_rate_run"

        store.insert_run({
            "run_id": run_id,
            "started_at": 100.0,
            "finished_at": 110.0,
            "config_json": json.dumps({"output_dir": str(tmp_path)}),
            "manifest_json": "{}",
            "status": "completed",
        })

        store.upsert_session({
            "run_id": run_id,
            "session_id": "s1",
            "arm": "arm1",
            "variant": "v1",
            "pool": "local",
            "country": "US",
            "session_name": "s1",
            "status": "completed",
            "stop_reason": "limit",
        })

        # Query 1: 1 request, OK (Successful query)
        store.insert_request({
            "run_id": run_id,
            "session_id": "s1",
            "ts": 101.0,
            "request_index": 1,
            "query_index": 1,
            "query": "query 1",
            "country": "US",
            "kind": "spa",
            "status": 200,
            "outcome": "ok",
            "attempt": 0,
            "is_final": 1,
            "latency_ms": 100.0,
            "bytes_wire": 100,
            "bytes_rx": 500,
            "arm": "arm1",
            "variant": "v1",
        })
        store.insert_place({
            "run_id": run_id,
            "session_id": "s1",
            "query": "query 1",
            "country": "US",
            "rank": 1,
            "place_id": "p1",
            "title": "Title 1",
            "validation": "valid",
            "arm": "arm1",
            "variant": "v1",
        })

        # Query 2: 4 requests: 3 OK, 1 blocked with 0 places (Failed query)
        for req_idx, outc in enumerate(["ok", "ok", "ok", "403"], start=1):
            store.insert_request({
                "run_id": run_id,
                "session_id": "s1",
                "ts": 102.0 + req_idx,
                "request_index": req_idx,
                "query_index": 2,
                "query": "query 2",
                "country": "US",
                "kind": "page",
                "status": 200 if outc == "ok" else 403,
                "outcome": outc,
                "block_type": "403" if outc == "403" else None,
                "attempt": 0,
                "is_final": 1,
                "latency_ms": 100.0,
                "bytes_wire": 100,
                "bytes_rx": 500,
                "arm": "arm1",
                "variant": "v1",
            })

        store.close()

        # Run analysis & write outputs
        out_dir = tmp_path / "out"
        write_outputs(str(db_path), run_id, out_dir=str(out_dir))

        summary = json.loads((out_dir / "summary.json").read_text())
        # Queries: total=2, successful=1 -> Query success rate should be 50.0%
        # Requests: total=5, ok=4 -> Request success rate is 4/5 = 80.0%
        assert summary["total_queries"] == 2
        assert summary["successful_queries"] == 1
        assert summary["total_requests"] == 5
        assert summary["ok_requests"] == 4
        assert summary["success_rate_pct"] == 80.0

        # Fixed in analyze.py: query_success_rate_pct is correctly 50.0%
        print(f"Observed query_success_rate_pct: {summary['query_success_rate_pct']}")
        assert summary["query_success_rate_pct"] == 50.0

    def test_store_export_raw(self, tmp_path):
        """
        Verify store.export_raw exports all 5 CSV files with matching row counts.
        """
        db_path = tmp_path / "test_exp.db"
        s = Store(db_path)
        s.init()
        s.upsert_session({
            "run_id": "r1",
            "session_id": "s1",
            "arm": "arm1",
            "variant": "v1",
            "pool": "local",
            "country": "US",
            "session_name": "s1",
            "status": "completed",
        })
        s.close()

        csv_dir = tmp_path / "csv_out"
        counts = export_raw(run_id="r1", out_dir=csv_dir, db_path=db_path)
        assert (csv_dir / "sessions.csv").exists()
        assert (csv_dir / "requests.csv").exists()
        assert (csv_dir / "places.csv").exists()
        assert (csv_dir / "ip_observations.csv").exists()
        assert (csv_dir / "events.csv").exists()
        assert counts["sessions"] == 1

    def test_quota_monitor_simulation(self):
        """
        Simulate quota monitor trigger when remaining bytes < 200MB.
        Confirms stop_event is set gracefully and no crash occurs.
        """
        async def _run():
            stop_event = asyncio.Event()
            quota_exceeded = asyncio.Event()

            fake_usage = {
                "quota_bytes": 5000000000,
                "consumption_bytes": 4900000000,
                "remaining_bytes": 100000000,  # 100MB < 200MB threshold
            }

            with patch("config.PROXY.has_api", return_value=True), \
                 patch("quota.get_usage", AsyncMock(return_value=fake_usage)):
                monitor_task = asyncio.create_task(
                    _quota_monitor_task(
                        stop_event=stop_event,
                        quota_exceeded=quota_exceeded,
                        cap_mb=500.0,
                        start_consumption_bytes=0,
                        interval_s=0.01,
                    )
                )
                await asyncio.sleep(0.05)
                assert stop_event.is_set()
                assert quota_exceeded.is_set()
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_run())

    def test_manifest_contains_no_secrets(self, monkeypatch):
        """
        Confirm manifest contains only environment variable names (keys),
        not secret values such as passwords or API keys.
        """
        monkeypatch.setenv("ANYIP_PROXY_PASS", "SUPER_SECRET_PASSWORD_12345")
        monkeypatch.setenv("ANYIP_API_KEY", "SECRET_KEY_99999")

        run_cfg = RunConfig(
            id="test_manifest_run",
            country="US",
            query_panel="queries/panel_us.csv",
            raw_toml="[run]\nid='test'",
        )

        manifest = build_manifest(
            run_config=run_cfg,
            quota_before={"quota_bytes": 100},
            quota_after={"quota_bytes": 100},
        )

        manifest_str = json.dumps(manifest)
        assert "SUPER_SECRET_PASSWORD_12345" not in manifest_str
        assert "SECRET_KEY_99999" not in manifest_str
        assert "ANYIP_PROXY_PASS" in manifest["env_keys"]
        assert "ANYIP_API_KEY" in manifest["env_keys"]

    def test_dry_run_creates_no_files(self, tmp_path):
        """
        Verify that dry_run=True in run_experiment produces plan and touches no files.
        """
        test_dir = tmp_path / "dry_run_test"
        cfg_path = tmp_path / "test.toml"
        cfg_path.write_text(f"""[run]
id = "dry_test"
country = "US"
query_panel = "queries/panel_us.csv"
output_dir = "{test_dir}"

[[arms]]
name = "arm1"
kind = "block_onset"
pool = "local"
sessions = 1
requests_per_session = 1
""")

        res = run_experiment(str(cfg_path), dry_run=True)
        assert res.get("dry_run") is True
        assert not test_dir.exists()
