"""
Unit tests for the experiment runner and CLI integration.
Tests cover TOML loading, panel parsing and validation, request estimation math,
dry-run guarantees (no disk writes), error handling, cumulative block indexing,
TTL primary family stability, local variance proxy bypass, quota stop status,
and legacy bench integrations.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import AsyncMock, patch

import pytest

from bench import load_query_tasks, print_legacy_dry_run
from experiment import (
    ArmConfig,
    RunConfig,
    SessionWorker,
    build_manifest,
    estimate_requests,
    load_config,
    load_panel,
    record_query_result,
    reconcile_session,
    run_experiment,
    run_variance,
)
from scraper import IpObservation, QueryResult, RequestRecord
from store import Store


def test_load_config_valid() -> None:
    """Verify loading valid TOML files and dataclass unpacking."""
    run_cfg = load_config("experiments/pilot.toml")
    assert run_cfg.id == "pilot"
    assert run_cfg.country == "IN"
    assert run_cfg.quota_cap_mb == 100.0
    assert len(run_cfg.arms) == 3

    # Tuple unpacking support
    cfg, arms = load_config("experiments/pilot.toml")
    assert cfg.id == "pilot"
    assert len(arms) == 3

    arm_names = [a.name for a in arms]
    assert "ttl_pilot" in arm_names
    assert "block_pilot" in arm_names
    assert "variance_pilot" in arm_names


def test_load_config_smoke_and_stability() -> None:
    """Verify smoke_local.toml and stability.toml parse cleanly."""
    smoke_cfg = load_config("experiments/smoke_local.toml")
    assert smoke_cfg.id == "smoke_local"
    assert len(smoke_cfg.arms) == 1
    assert smoke_cfg.arms[0].pool == "local"

    stab_cfg = load_config("experiments/stability.toml")
    assert stab_cfg.id == "stability_full"
    assert len(stab_cfg.arms) == 7
    assert {a.name for a in stab_cfg.arms} >= {
        "ttl_residential", "ttl_mobile", "block_residential", "block_mobile",
        "result_variance", "load_matrix", "pool_comparison",
    }


def test_load_config_missing_file() -> None:
    """Verify FileNotFoundError is raised for non-existent config."""
    with pytest.raises(FileNotFoundError):
        load_config("experiments/non_existent_file.toml")


def test_explicit_sessions_zero_error(tmp_path: Path) -> None:
    """Verify explicit sessions=0 is not coerced to 1 and raises ValueError."""
    bad_toml = tmp_path / "zero_sessions.toml"
    bad_toml.write_text(
        """
        [run]
        id = "zero_sess"
        country = "US"

        [[arms]]
        name = "bad_arm"
        kind = "block_onset"
        pool = "residential"
        sessions = 0
        """,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="sessions must be >= 1"):
        load_config(bad_toml)


def test_invalid_pool_fail_fast_no_db(tmp_path: Path) -> None:
    """Verify invalid pool raises ValueError before any output directory/DB is created."""
    out_dir = tmp_path / "should_not_exist"
    bad_toml = tmp_path / "bad_pool.toml"
    bad_toml.write_text(
        f"""
        [run]
        id = "bad_pool_run"
        country = "US"
        output_dir = "{out_dir}"

        [[arms]]
        name = "bad_arm"
        kind = "ttl"
        pool = "quantum_vpn"
        duration_min = 1.0
        """,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid pool 'quantum_vpn'"):
        run_experiment(bad_toml, output_dir=out_dir)

    # Output directory must not have been created
    assert not out_dir.exists()


def test_header_only_csv_fail_fast_no_db(tmp_path: Path) -> None:
    """Verify a CSV panel with only a header raises ValueError before any output directory/DB is created."""
    out_dir = tmp_path / "header_only_results"
    empty_csv = tmp_path / "header_only.csv"
    empty_csv.write_text("query,country,city,expected_min_results,category\n", encoding="utf-8")

    bad_toml = tmp_path / "header_only.toml"
    bad_toml.write_text(
        f"""
        [run]
        id = "header_only_run"
        country = "US"
        query_panel = "{empty_csv}"
        output_dir = "{out_dir}"

        [[arms]]
        name = "block_arm"
        kind = "block_onset"
        pool = "local"
        sessions = 1
        """,
        encoding="utf-8",
    )

    # Directly calling load_config must fail fast with ValueError
    with pytest.raises(ValueError, match="No queries found in panel|has no valid rows"):
        load_config(bad_toml)

    # Output directory must not have been created by load_config
    assert not out_dir.exists()

    with pytest.raises(ValueError, match="No queries found in panel|has no valid rows"):
        run_experiment(bad_toml, output_dir=out_dir)

    # Output directory must not have been created
    assert not out_dir.exists()


def test_load_config_header_only_panel_raises_and_no_dir(tmp_path: Path) -> None:
    """Regression test: when a header-only CSV is provided as a query panel, load_config raises ValueError and NO results directory is created."""
    out_dir = tmp_path / "header_only_results_dir"
    empty_csv = tmp_path / "header_only.csv"
    empty_csv.write_text("query,country,city,expected_min_results,category\n", encoding="utf-8")

    bad_toml = tmp_path / "bad_panel_config.toml"
    bad_toml.write_text(
        f"""
        [run]
        id = "header_only_regression"
        country = "US"
        query_panel = "{empty_csv}"
        output_dir = "{out_dir}"

        [[arms]]
        name = "test_arm"
        kind = "block_onset"
        pool = "local"
        sessions = 1
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="No queries found in panel|has no valid rows"):
        load_config(bad_toml)

    # Must fail fast before any output directory is created on disk
    assert not out_dir.exists()


def test_load_config_header_only_arm_panel_raises_and_no_dir(tmp_path: Path) -> None:
    """Verify load_config raises ValueError when an arm overrides query_panel with a header-only CSV."""
    out_dir = tmp_path / "arm_header_only_dir"
    empty_csv = tmp_path / "arm_header_only.csv"
    empty_csv.write_text("query,country,city,expected_min_results,category\n", encoding="utf-8")

    bad_toml = tmp_path / "bad_arm_panel_config.toml"
    bad_toml.write_text(
        f"""
        [run]
        id = "arm_header_only_run"
        country = "US"
        output_dir = "{out_dir}"

        [[arms]]
        name = "custom_panel_arm"
        kind = "block_onset"
        pool = "local"
        query_panel = "{empty_csv}"
        sessions = 1
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="No queries found in panel|has no valid rows"):
        load_config(bad_toml)

    assert not out_dir.exists()


def test_load_panel_valid() -> None:
    """Verify loading a valid query panel CSV."""
    rows = load_panel("queries/pilot.csv")
    assert len(rows) == 3
    first = rows[0]
    assert "query" in first
    assert first["country"] == "IN"
    assert first["expected_min_results"] == 10
    assert isinstance(first["expected_min_results"], int)


def test_load_panel_country_filter() -> None:
    """Verify country filtering in load_panel."""
    in_rows = load_panel("queries/panel_all.csv", country="IN")
    assert len(in_rows) > 0
    assert all(r["country"] == "IN" for r in in_rows)


def test_load_panel_errors(tmp_path: Path) -> None:
    """Verify error conditions on panel reading."""
    with pytest.raises(FileNotFoundError):
        load_panel(tmp_path / "missing.csv")

    with pytest.raises(ValueError, match="No queries found in panel"):
        load_panel("queries/pilot.csv", country="FR")


def test_estimate_requests_math() -> None:
    """Verify estimation math for each arm type."""
    cfg = RunConfig(
        id="calc_test",
        country="US",
        witness_mode="dual",
        depth_pages=1,
    )
    arms = [
        ArmConfig(
            name="ttl_arm",
            kind="ttl",
            sessions=3,
            duration_min=1.0,
            probe_interval_s=20.0,
        ),
        ArmConfig(
            name="block_arm",
            kind="block_onset",
            sessions=2,
            requests_per_session=10,
        ),
        ArmConfig(
            name="var_arm",
            kind="variance",
            queries_per_session=1,
            same_ip_repeats=2,
            new_ip_repeats=3,
            forced_rotate_repeats=1,
            include_local=True,
            depth_pages=1,
        ),
    ]
    cfg.arms = arms
    estimates = estimate_requests(cfg)
    assert len(estimates) == 3

    ttl_est = estimates[0]
    assert ttl_est["google_requests"] == 0
    assert ttl_est["probe_requests"] == 24

    block_est = estimates[1]
    assert block_est["google_requests"] == 20
    assert block_est["probe_requests"] == 8

    var_est = estimates[2]
    # Seven repeats, each with an SPA request, a data page, and two dual-witness probes.
    assert var_est["google_requests"] == 14
    assert var_est["probe_requests"] == 28


def test_estimate_variance_periodic_probes() -> None:
    cfg = RunConfig(
        witness_mode="dual",
        ip_probe_every=2,
        arms=[ArmConfig(
            name="var_arm",
            kind="variance",
            queries_per_session=2,
            same_ip_repeats=2,
            new_ip_repeats=1,
            forced_rotate_repeats=0,
            include_local=False,
            depth_pages=4,
        )],
    )
    estimate, = estimate_requests(cfg)
    assert estimate["google_requests"] == 30  # Six repeats, five Google requests each.
    assert estimate["probe_requests"] == 48  # Start, two periodic, and end per repeat, two witnesses each.
    assert estimate["total_requests"] == 78


def test_estimate_pool_compare_includes_spa_per_query_and_pool() -> None:
    cfg = RunConfig(
        witness_mode="dual",
        depth_pages=2,
        arms=[ArmConfig(
            name="pool_arm",
            kind="pool_compare",
            pools=["residential", "mobile"],
            include_local=True,
            queries_per_session=2,
        )],
    )
    estimate, = estimate_requests(cfg)
    assert estimate["sessions"] == 6  # Two queries across three pools, including local.
    assert estimate["google_requests"] == 18  # One SPA request plus two data pages per session.
    assert estimate["probe_requests"] == 24


def test_dry_run_no_writes(tmp_path: Path) -> None:
    """Verify that --dry-run writes nothing to disk."""
    out_dir = tmp_path / "dry_results"
    res = run_experiment("experiments/smoke_local.toml", dry_run=True, output_dir=out_dir)
    assert res.get("dry_run") is True
    assert "plan" in res
    assert not out_dir.exists()


def test_cumulative_first_block_request(tmp_path: Path) -> None:
    """Verify first_block_request accumulates across multiple queries in a session."""
    db_file = tmp_path / "test_cum_block.db"
    store = Store(db_file)
    store.init()

    # Query 1: 2 successful requests
    qr1 = QueryResult(
        query="query one",
        country="IN",
        proxy_session_id="sess_cum",
        requests=[
            RequestRecord(url="http://g1", status=200, outcome="ok", latency_ms=100.0, bytes_rx=100, request_index=1),
            RequestRecord(url="http://g2", status=200, outcome="ok", latency_ms=100.0, bytes_rx=100, request_index=2),
        ],
    )
    # Query 2: blocked on request 2 (page 1)
    qr2 = QueryResult(
        query="query two",
        country="IN",
        proxy_session_id="sess_cum",
        requests=[
            RequestRecord(url="http://g3", status=200, outcome="ok", latency_ms=100.0, bytes_rx=100, request_index=1),
            RequestRecord(url="http://g4", status=403, outcome="403", latency_ms=100.0, bytes_rx=100, request_index=2),
        ],
    )

    reconcile_session(
        store,
        run_id="run_cum",
        session_id="sess_cum",
        arm_name="block_arm",
        variant_name="default",
        pool="residential",
        country="IN",
        session_name="sess_cum",
        sesstime_min=20,
        started_at=1000.0,
        ended_at=1020.0,
        status="completed",
        stop_reason="block",
        results=[qr1, qr2],
    )

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    sess = conn.execute("SELECT * FROM sessions WHERE session_id = 'sess_cum'").fetchone()

    # Query 1 had 2 requests. Query 2 blocked at request_index 2.
    # Cumulative index = 2 + 2 = 4.
    assert sess["first_block_request"] == 4
    assert sess["block_events"] == 1
    assert sess["requests"] == 4
    assert sess["google_requests"] == 4
    conn.close()
    store.close()


def test_ttl_v4_stable_vs_v4_change(tmp_path: Path) -> None:
    """Verify TTL arm evaluates primary family only and ignores v4 vs v6 false alarms."""
    import asyncio
    async def _run():
        db_file = tmp_path / "test_ttl.db"
        store = Store(db_file)
        store.init()

        run_cfg = RunConfig(id="ttl_test", country="IN", witness_mode="dual")
        arm = ArmConfig(
            name="ttl_eval",
            kind="ttl",
            pool="residential",
            sessions=1,
            sesstime_min=1,
            duration_min=0.05,
            probe_interval_s=0.01,
        )

        # 1. Stable v4 case (v4 constant, v6 present): should not trigger change
        stable_probes = [
            [
                IpObservation(ts=1000.0, request_index=0, witness="ipify_v4", family="v4", ip="103.1.1.1"),
                IpObservation(ts=1000.0, request_index=0, witness="ipify_v6", family="v6", ip="2405:200::1"),
            ],
            [
                IpObservation(ts=1001.0, request_index=1, witness="ipify_v4", family="v4", ip="103.1.1.1"),
                IpObservation(ts=1001.0, request_index=1, witness="ipify_v6", family="v6", ip="2405:200::2"),
            ],
        ]

        worker = SessionWorker(store, "ttl_test", run_cfg, asyncio.Event())
        call_count = 0

        async def fake_stable_exit_ips(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(stable_probes) - 1)
            call_count += 1
            return stable_probes[idx]

        with patch("experiment._exit_ips", side_effect=fake_stable_exit_ips):
            res = await worker.run_ttl_session(arm, 0)

        # Stable v4 should conclude with stop_reason="limit", NOT ip_change
        assert res["stop_reason"] == "limit"
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        sess = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (res["session_id"],)).fetchone()
        assert sess["distinct_ips"] == 1
        assert sess["stop_reason"] == "limit"
        conn.close()

        # 2. Changed v4 case: v4 changes from 103.1.1.1 to 103.1.1.2
        changed_probes = [
            [
                IpObservation(ts=1000.0, request_index=0, witness="ipify_v4", family="v4", ip="103.1.1.1"),
            ],
            [
                IpObservation(ts=1002.0, request_index=1, witness="ipify_v4", family="v4", ip="103.1.1.2"),
            ],
        ]
        call_count2 = 0

        async def fake_changed_exit_ips(*args, **kwargs):
            nonlocal call_count2
            idx = min(call_count2, len(changed_probes) - 1)
            call_count2 += 1
            return changed_probes[idx]

        with patch("experiment._exit_ips", side_effect=fake_changed_exit_ips):
            res2 = await worker.run_ttl_session(arm, 1)

        assert res2["stop_reason"] in ("ip_change", "ttl_expiry")
        conn2 = sqlite3.connect(db_file)
        conn2.row_factory = sqlite3.Row
        sess2 = conn2.execute("SELECT * FROM sessions WHERE session_id = ?", (res2["session_id"],)).fetchone()
        assert sess2["distinct_ips"] == 2
        conn2.close()
        store.close()

    asyncio.run(_run())


def test_local_variance_no_proxy(tmp_path: Path) -> None:
    """Verify local variance executes with use_proxy=False and proxy_type=None, without proxy credentials."""
    import asyncio
    async def _run():
        db_file = tmp_path / "test_var_local.db"
        store = Store(db_file)
        store.init()

        run_cfg = RunConfig(id="var_local_test", country="IN", query_panel="queries/pilot.csv")
        arm = ArmConfig(
            name="var_arm",
            kind="variance",
            pool="local",
            queries_per_session=1,
            same_ip_repeats=1,
            new_ip_repeats=1,
            repeat_delay_s=0.0,
        )

        recorded_kwargs = []

        async def fake_scrape_query(*args, **kwargs):
            recorded_kwargs.append(kwargs)
            return QueryResult(
                query="hotels in Delhi",
                country="IN",
                proxy_session_id=kwargs.get("session_id", "local_sess"),
                places=[{"place_id": "p1", "title": "Place 1"}],
                requests=[RequestRecord(url="http://maps", status=200, outcome="ok", latency_ms=50.0, bytes_rx=100)],
            )

        with patch("experiment.scrape_query", side_effect=fake_scrape_query):
            await run_variance(store, "var_local_test", run_cfg, arm, asyncio.Event())

        # Ensure every scrape_query call had use_proxy=False and proxy_type=None
        assert len(recorded_kwargs) >= 2
        for kw in recorded_kwargs:
            assert kw.get("use_proxy") is False
            assert kw.get("proxy_type") is None
        store.close()

    asyncio.run(_run())


def test_quota_stop_reason_and_status(tmp_path: Path) -> None:
    """Verify that when quota_exceeded event is set, stop_reason and run status are 'quota'."""
    import asyncio
    async def _run():
        db_file = tmp_path / "test_quota_stop.db"
        store = Store(db_file)
        store.init()

        run_cfg = RunConfig(id="quota_run", country="IN")
        arm = ArmConfig(
            name="ttl_arm",
            kind="ttl",
            pool="local",
            sessions=1,
            duration_min=1.0,
            probe_interval_s=0.1,
        )

        stop_event = asyncio.Event()
        quota_exceeded = asyncio.Event()
        quota_exceeded.set()
        stop_event.set()

        worker = SessionWorker(store, "quota_run", run_cfg, stop_event, quota_exceeded)
        with patch("experiment._exit_ips", new_callable=AsyncMock, return_value=[IpObservation(ts=1.0, request_index=0, witness="ipify_v4", family="v4", ip="1.1.1.1")]):
            res = await worker.run_ttl_session(arm, 0)

        assert res["stop_reason"] == "quota"
        assert res["status"] == "quota"
        store.close()

    asyncio.run(_run())


def test_csv_query_parsing(tmp_path: Path) -> None:
    """Verify load_query_tasks correctly parses CSV headers and query/country columns."""
    csv_file = tmp_path / "test_queries.csv"
    csv_file.write_text(
        "query,country,city,expected_min_results,category\n"
        "coffee in Seattle,US,Seattle,10,coffee\n"
        "tea in Darjeeling,IN,Darjeeling,10,tea\n",
        encoding="utf-8",
    )

    import argparse
    args = argparse.Namespace(
        query=None,
        queries_file=str(csv_file),
        countries="US,IN",
    )
    tasks = load_query_tasks(args)
    assert len(tasks) == 2
    assert tasks[0] == ("coffee in Seattle", "US")
    assert tasks[1] == ("tea in Darjeeling", "IN")


def test_legacy_dry_run_no_network(capsys: pytest.CaptureFixture) -> None:
    """Verify legacy --dry-run prints plan and exits without network calls."""
    import argparse
    args = argparse.Namespace(
        query="tea in Pune",
        queries_file="queries/smoke_test.txt",
        countries="IN",
        concurrency=2,
        limit=20,
        no_proxy=True,
        proxy_type="none",
        rotation=False,
    )
    print_legacy_dry_run(args)
    captured = capsys.readouterr().out
    assert "Legacy Benchmark — Dry Run Plan" in captured
    assert "Dry run only" in captured


def test_local_repeats_estimate_and_execution(tmp_path: Path) -> None:
    """Verify that local_repeats=2 estimates 2 local runs and executes at least 2 local sessions per query."""
    arm = ArmConfig(
        name="var_arm",
        kind="variance",
        pool="residential",
        queries_per_session=1,
        same_ip_repeats=1,
        new_ip_repeats=1,
        local_repeats=2,
        include_local=True,
        depth_pages=1,
    )
    cfg = RunConfig(id="var_test", country="US", witness_mode="dual", depth_pages=1, arms=[arm])
    estimates = estimate_requests(cfg)
    # Four repeats, each with an SPA request, a data page, and two dual-witness probes.
    assert estimates[0]["google_requests"] == 8
    assert estimates[0]["probe_requests"] == 16

    # Test execution with mocked scrape_query
    import asyncio
    async def _run():
        db_file = tmp_path / "test_local_repeats.db"
        store = Store(db_file)
        store.init()

        async def fake_scrape_query(*args, **kwargs):
            return QueryResult(
                query="hotels in Delhi",
                country="IN",
                proxy_session_id=kwargs.get("session_id", "sid"),
                places=[{"place_id": "p1", "title": "Place 1"}],
                requests=[RequestRecord(url="http://maps", status=200, outcome="ok", latency_ms=50.0, bytes_rx=100)],
            )

        with patch("experiment.scrape_query", side_effect=fake_scrape_query):
            outcomes = await run_variance(store, "var_test", cfg, arm, asyncio.Event())

        local_outcomes = [o for o in outcomes if o.get("variant") == "local"]
        assert len(local_outcomes) == 2

        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        local_sessions = conn.execute("SELECT * FROM sessions WHERE variant = 'local'").fetchall()
        assert len(local_sessions) == 2
        conn.close()
        store.close()

    asyncio.run(_run())


def test_invalid_local_repeats_error(tmp_path: Path) -> None:
    """Verify local_repeats < 1 when include_local=True raises ValueError."""
    bad_toml = tmp_path / "bad_local_repeats.toml"
    bad_toml.write_text(
        """
        [run]
        id = "bad_local_rep"
        country = "IN"
        query_panel = "queries/pilot.csv"

        [[arms]]
        name = "bad_arm"
        kind = "variance"
        pool = "residential"
        include_local = true
        local_repeats = 0
        """,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="local_repeats must be >= 1"):
        load_config(bad_toml)
