"""tests/test_analyze.py — Unit and integration tests for analyze.py.

Verifies:
- Wilson score CI for known values (e.g. 0/10 upper bound, symmetric 50/100)
- Rule of three upper bounds
- Kaplan-Meier estimator on tiny datasets and median_km
- Bootstrap confidence interval reproducibility
- Jaccard ({a,b,c} vs {b,c,d} = 0.5), Jaccard@k
- RBO finite formula point estimate
- Kendall tau rank correlation
- Synthetic SQLite run with all arms, verifying run_metrics and write_outputs
- Handling empty / missing data without crashing and zero NaN/Inf in JSON
"""

import json
import math
from pathlib import Path
import sqlite3
import pytest

from analyze import (
    bootstrap_ci,
    generate_report_md,
    get_primary_ip,
    jaccard,
    jaccard_at_k,
    kendall_tau,
    km,
    median_km,
    rbo,
    rule_of_three,
    run_metrics,
    sanitize_floats,
    wilson,
    write_outputs,
)


# ── 1. Statistical & Mathematical Tests ──────────────────────────────────────


def test_wilson_known_values() -> None:
    # 0 out of 0 trials
    assert wilson(0, 0) == (0.0, 0.0)

    # 0 out of 10 trials: lower is 0.0, upper bound is ~0.2775 (3.8416 / 13.8416)
    lo, hi = wilson(0, 10, z=1.96)
    assert lo == 0.0
    assert hi == pytest.approx(0.277539, abs=1e-4)

    # 10 out of 10 trials: upper is 1.0, lower is ~0.7225
    lo10, hi10 = wilson(10, 10, z=1.96)
    assert hi10 == 1.0
    assert lo10 == pytest.approx(1.0 - 0.277539, abs=1e-4)

    # 50 out of 100 trials: symmetric around 0.50
    lo50, hi50 = wilson(50, 100, z=1.96)
    assert lo50 == pytest.approx(0.403831, abs=1e-4)
    assert hi50 == pytest.approx(0.596168, abs=1e-4)
    assert (lo50 + hi50) / 2 == pytest.approx(0.50, abs=1e-4)

    # 0 out of 100 trials: upper is ~0.037 (close to rule of three 0.03)
    lo0_100, hi0_100 = wilson(0, 100, z=1.96)
    assert lo0_100 == 0.0
    assert hi0_100 == pytest.approx(0.036995, abs=1e-4)


def test_rule_of_three() -> None:
    assert rule_of_three(0) == 1.0
    assert rule_of_three(-5) == 1.0
    assert rule_of_three(1) == 1.0  # min(1.0, 3/1)
    assert rule_of_three(2) == 1.0  # min(1.0, 3/2)
    assert rule_of_three(10) == pytest.approx(0.30)
    assert rule_of_three(100) == pytest.approx(0.03)
    assert rule_of_three(300) == pytest.approx(0.01)


def test_km_tiny_dataset() -> None:
    # 4 items with distinct event times [1, 2, 3, 4], all events (no censoring)
    # S(1) = 1 - 1/4 = 0.75
    # S(2) = 0.75 * (1 - 1/3) = 0.50 -> median at t=2
    # S(3) = 0.50 * (1 - 1/2) = 0.25
    # S(4) = 0.25 * (1 - 1/1) = 0.0
    durations = [1.0, 2.0, 3.0, 4.0]
    events = [1, 1, 1, 1]
    curve = km(durations, events)
    assert len(curve) == 4
    assert curve[0] == {"t": 1.0, "at_risk": 4, "events": 1, "survival": 0.75}
    assert curve[1] == {"t": 2.0, "at_risk": 3, "events": 1, "survival": 0.50}
    assert curve[2] == {"t": 3.0, "at_risk": 2, "events": 1, "survival": 0.25}
    assert curve[3] == {"t": 4.0, "at_risk": 1, "events": 1, "survival": 0.0}

    assert median_km(curve) == 2.0
    assert median_km(durations, events) == 2.0


def test_km_with_censoring_and_ties() -> None:
    # durations: [2, 3, 5, 5, 7], events: [1, 0, 1, 1, 0]
    # t=2: at_risk=5, d=1 -> S = 4/5 = 0.80
    # t=3: at_risk=4, d=0 (censored), S remains 0.80
    # t=5: at_risk=3, d=2 -> S = 0.80 * (1 - 2/3) = 0.80 * 1/3 = 0.266667 <= 0.5 -> median at t=5
    # t=7: at_risk=1, d=0 (censored), S remains 0.266667
    durations = [2.0, 3.0, 5.0, 5.0, 7.0]
    events = [1, 0, 1, 1, 0]
    curve = km(durations, events)
    assert len(curve) == 4
    assert curve[0]["t"] == 2.0
    assert curve[0]["survival"] == pytest.approx(0.80)
    assert curve[1]["t"] == 3.0
    assert curve[1]["survival"] == pytest.approx(0.80)
    assert curve[2]["t"] == 5.0
    assert curve[2]["events"] == 2
    assert curve[2]["survival"] == pytest.approx(0.266667, abs=1e-5)
    assert curve[3]["t"] == 7.0
    assert curve[3]["survival"] == pytest.approx(0.266667, abs=1e-5)

    assert median_km(curve) == 5.0


def test_km_all_censored() -> None:
    # All censored: survival stays 1.0, median is None
    durations = [1.0, 2.0, 3.0]
    events = [0, 0, 0]
    curve = km(durations, events)
    assert all(pt["survival"] == 1.0 for pt in curve)
    assert median_km(curve) is None
    assert km([], []) == []
    assert median_km([]) is None


def test_bootstrap_ci() -> None:
    assert bootstrap_ci([]) == (0.0, 0.0)
    assert bootstrap_ci([42.0]) == (42.0, 42.0)
    assert bootstrap_ci([10.0, 10.0, 10.0]) == (10.0, 10.0)

    # Deterministic test with fixed seed
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    lo, hi = bootstrap_ci(data, n=2000, seed=42)
    mean_val = sum(data) / len(data)
    assert lo <= mean_val <= hi
    assert 2.0 <= lo <= 4.5
    assert 6.5 <= hi <= 9.0


def test_jaccard_and_at_k() -> None:
    # Known test case from spec: {a, b, c} vs {b, c, d} = 2/4 = 0.5
    assert jaccard({"a", "b", "c"}, {"b", "c", "d"}) == pytest.approx(0.5)
    assert jaccard(["a", "b", "c"], ["b", "c", "d"]) == pytest.approx(0.5)

    # Identical
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    # Both empty -> 1.0
    assert jaccard([], []) == 1.0
    # One empty -> 0.0
    assert jaccard(["a"], []) == 0.0
    assert jaccard([], ["b"]) == 0.0

    # Jaccard@k
    list_a = ["x", "y", "a", "b"]
    list_b = ["x", "y", "c", "d"]
    assert jaccard_at_k(list_a, list_b, 2) == 1.0
    assert jaccard_at_k(list_a, list_b, 4) == pytest.approx(2 / 6)
    assert jaccard_at_k(list_a, list_b, 0) == 1.0


def test_rbo() -> None:
    # Identical rankings -> 1.0
    ranks = ["a", "b", "c", "d", "e"]
    assert rbo(ranks, ranks, p=0.9) == pytest.approx(1.0)

    # Disjoint rankings -> 0.0
    assert rbo(["a", "b"], ["c", "d"], p=0.9) == 0.0

    # Both empty -> 1.0; one empty -> 0.0
    assert rbo([], [], p=0.9) == 1.0
    assert rbo(["a"], [], p=0.9) == 0.0

    # Partial overlap
    score = rbo(["a", "b", "c"], ["a", "c", "b"], p=0.9)
    assert 0.8 <= score < 1.0


def test_kendall_tau() -> None:
    # Perfect concordance -> 1.0
    assert kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == pytest.approx(1.0)
    # Perfect discordance -> -1.0
    assert kendall_tau(["a", "b", "c"], ["c", "b", "a"]) == pytest.approx(-1.0)

    # Rank dicts
    dict_a = {"item1": 0, "item2": 1, "item3": 2}
    dict_b = {"item1": 0, "item2": 1, "item3": 2}
    assert kendall_tau(dict_a, dict_b) == pytest.approx(1.0)

    # Fewer than 2 common items -> 0.0
    assert kendall_tau(["a"], ["b"]) == 0.0
    assert kendall_tau(["a"], ["a"]) == 1.0
    assert kendall_tau([], []) == 0.0


def test_sanitize_floats() -> None:
    bad_obj = {
        "nan": float("nan"),
        "inf": float("inf"),
        "neg_inf": float("-inf"),
        "nested": [float("nan"), 123.45],
        "normal": 1.0,
    }
    clean = sanitize_floats(bad_obj)
    assert clean["nan"] == 0.0
    assert clean["inf"] == 0.0
    assert clean["neg_inf"] == 0.0
    assert clean["nested"] == [0.0, 123.45]
    assert clean["normal"] == 1.0
    # Must dump without throwing ValueError
    json_str = json.dumps(clean, allow_nan=False)
    assert "NaN" not in json_str
    assert "Infinity" not in json_str


# ── 2. Synthetic Database & Full Pipeline Tests ──────────────────────────────


def _create_synthetic_db(db_path: Path) -> str:
    """Create a synthetic database matching the schema from anyip-spec.md."""
    conn = sqlite3.connect(str(db_path))
    run_id = "test_run_001"

    schema = """
    CREATE TABLE runs(
        run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL,
        config_json TEXT, manifest_json TEXT, status TEXT
    );
    CREATE TABLE sessions(
        run_id TEXT, session_id TEXT, arm TEXT, variant TEXT, pool TEXT, country TEXT,
        session_name TEXT, strict_replace INTEGER, sessasn_strict INTEGER,
        sessipcollision_strict INTEGER, sesstime_min INTEGER, created_at REAL,
        started_at REAL, ended_at REAL, requests INTEGER, google_requests INTEGER,
        places INTEGER, block_events INTEGER, first_block_request INTEGER,
        first_block_ts REAL, status TEXT, stop_reason TEXT, distinct_ips INTEGER,
        ip_set_json TEXT, asn_set_json TEXT, city_set_json TEXT, traffic_bytes INTEGER,
        error TEXT, PRIMARY KEY(run_id, session_id)
    );
    CREATE TABLE requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, session_id TEXT, ts REAL,
        request_index INTEGER, query_index INTEGER, query TEXT, country TEXT,
        kind TEXT, page_start INTEGER, url_hash TEXT, status INTEGER, outcome TEXT,
        block_type TEXT, anyip_error TEXT, attempt INTEGER, is_final INTEGER,
        latency_ms REAL, bytes_wire INTEGER, bytes_rx INTEGER, content_encoding TEXT,
        content_type TEXT, server TEXT, error TEXT, arm TEXT, variant TEXT
    );
    CREATE TABLE places(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, session_id TEXT, query TEXT,
        country TEXT, request_id INTEGER, rank INTEGER, place_id TEXT, title TEXT,
        category TEXT, address TEXT, phone TEXT, website TEXT, stars TEXT, reviews TEXT,
        lat REAL, lng REAL, validation TEXT, arm TEXT, variant TEXT
    );
    CREATE TABLE ip_observations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, session_id TEXT, ts REAL,
        request_index INTEGER, witness TEXT, family TEXT, ip TEXT, asn TEXT, org TEXT,
        city TEXT, country TEXT, latency_ms REAL, error TEXT, arm TEXT, variant TEXT
    );
    CREATE TABLE events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, session_id TEXT, ts REAL,
        request_index INTEGER, event_type TEXT, detail_json TEXT, arm TEXT, variant TEXT
    );
    """
    conn.executescript(schema)

    # 1. runs
    conn.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, 1700000000.0, 1700000100.0, '{"depth": 1}', '{}', "completed"),
    )

    # 2. sessions across arms: ttl, block_onset, variance, load
    sessions_data = [
        # session_id, arm, variant, pool, country, sesstime_min, reqs, g_reqs, places, blk_ev, fb_req, status
        ("sess_ttl_1", "ttl", "ttl_default", "residential", "US", 10, 0, 0, 0, 0, None, "completed"),
        ("sess_block_1", "block_onset", "onset_default", "residential", "US", 30, 10, 10, 20, 1, 8, "completed"),
        ("sess_var_same1", "variance", "same_ip", "residential", "IN", 30, 2, 2, 20, 0, None, "completed"),
        ("sess_var_same2", "variance", "same_ip", "residential", "IN", 30, 2, 2, 20, 0, None, "completed"),
        ("sess_var_new1", "variance", "new_ip", "residential", "IN", 30, 2, 2, 20, 0, None, "completed"),
        ("sess_load_1", "load", "load_5_2", "residential", "US", 30, 5, 5, 40, 0, None, "completed"),
    ]
    for s_id, arm, var, pool, ctry, stime, reqs, greqs, plcs, blk, fb, stat in sessions_data:
        conn.execute(
            """INSERT INTO sessions (
                run_id, session_id, arm, variant, pool, country, session_name,
                sesstime_min, started_at, ended_at, requests, google_requests,
                places, block_events, first_block_request, status, distinct_ips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, s_id, arm, var, pool, ctry, s_id,
                stime, 1700000000.0, 1700000050.0, reqs, greqs,
                plcs, blk, fb, stat, 1,
            ),
        )

    # 3. requests (including attempts and outcomes)
    requests_data = [
        # sess_id, ts, req_idx, query, ctry, outcome, attempt, is_final, lat, wire, rx, arm, var
        ("sess_block_1", 1700000010.0, 1, "hotels in ny", "US", "ok", 1, 1, 800.0, 50000, 200000, "block_onset", "onset_default"),
        ("sess_block_1", 1700000015.0, 8, "hotels in ny", "US", "403", 1, 1, 300.0, 2000, 5000, "block_onset", "onset_default"),
        ("sess_var_same1", 1700000020.0, 1, "tea in Pune", "IN", "ok", 1, 1, 1200.0, 40000, 180000, "variance", "same_ip"),
        ("sess_var_same2", 1700000025.0, 1, "tea in Pune", "IN", "ok", 1, 1, 1100.0, 42000, 185000, "variance", "same_ip"),
        ("sess_var_new1", 1700000030.0, 1, "tea in Pune", "IN", "ok", 1, 1, 1300.0, 41000, 182000, "variance", "new_ip"),
        ("sess_load_1", 1700000040.0, 1, "cafes in Austin", "US", "ok", 1, 1, 950.0, 45000, 190000, "load", "load_5_2"),
        # Attempt with retry
        ("sess_load_1", 1700000042.0, 2, "cafes in Austin", "US", "proxy_error", 1, 0, 1500.0, 1000, 1000, "load", "load_5_2"),
        ("sess_load_1", 1700000043.0, 2, "cafes in Austin", "US", "ok", 2, 1, 900.0, 46000, 192000, "load", "load_5_2"),
    ]
    for s_id, ts, r_idx, qry, ctry, outc, att, is_fin, lat, wire, rx, arm, var in requests_data:
        conn.execute(
            """INSERT INTO requests (
                run_id, session_id, ts, request_index, query, country,
                outcome, attempt, is_final, latency_ms, bytes_wire, bytes_rx, arm, variant
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, s_id, ts, r_idx, qry, ctry, outc, att, is_fin, lat, wire, rx, arm, var),
        )

    # 4. places
    # Query "tea in Pune" in sess_var_same1, sess_var_same2, sess_var_new1
    places_pune_1 = [f"ChIJ_pune_{i}" for i in range(1, 21)]
    # same2 shares 18 out of 20 with same1
    places_pune_2 = [f"ChIJ_pune_{i}" for i in range(1, 19)] + ["ChIJ_pune_alt1", "ChIJ_pune_alt2"]
    # new1 shares 10 out of 20 with same1
    places_pune_new = [f"ChIJ_pune_{i}" for i in range(1, 11)] + [f"ChIJ_pune_other_{i}" for i in range(11, 21)]

    for rank, pid in enumerate(places_pune_1, start=1):
        conn.execute(
            "INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, "sess_var_same1", "tea in Pune", "IN", rank, pid, f"Place {pid}"),
        )
    for rank, pid in enumerate(places_pune_2, start=1):
        conn.execute(
            "INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, "sess_var_same2", "tea in Pune", "IN", rank, pid, f"Place {pid}"),
        )
    for rank, pid in enumerate(places_pune_new, start=1):
        conn.execute(
            "INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, "sess_var_new1", "tea in Pune", "IN", rank, pid, f"Place {pid}"),
        )

    # 5. ip_observations for TTL arm
    conn.execute(
        "INSERT INTO ip_observations (run_id, session_id, ts, request_index, witness, family, ip, asn, country) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, "sess_ttl_1", 1700000000.0, 0, "ipify_v4", "v4", "100.1.1.1", "AS12345", "US"),
    )
    conn.execute(
        "INSERT INTO ip_observations (run_id, session_id, ts, request_index, witness, family, ip, asn, country) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, "sess_ttl_1", 1700000020.0, 1, "ipify_v4", "v4", "100.1.1.2", "AS12345", "US"),
    )

    # 6. events
    conn.execute(
        "INSERT INTO events (run_id, session_id, ts, event_type, detail_json) VALUES (?, ?, ?, ?, ?)",
        (run_id, "sess_ttl_1", 1700000020.0, "ip_change", '{"old": "100.1.1.1", "new": "100.1.1.2"}'),
    )

    conn.commit()
    conn.close()
    return run_id


def test_synthetic_run_metrics(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    run_id = _create_synthetic_db(db_file)

    metrics = run_metrics(db_file, run_id)
    assert metrics["run_id"] == run_id
    assert metrics["status"] == "completed"
    assert metrics["elapsed_s"] == 100.0

    totals = metrics["totals"]
    assert totals["total_sessions"] == 6
    assert totals["total_places"] == 60
    assert totals["total_requests"] == 8
    assert totals["final_requests"] == 7
    assert totals["ok_requests"] == 6
    assert totals["block_403"] == 1

    rates = metrics["rates"]
    assert rates["success_rate"] == pytest.approx(6 / 8, abs=1e-3)
    assert rates["block_rate"] == pytest.approx(1 / 8, abs=1e-3)
    assert rates["proxy_error_rate"] == pytest.approx(1 / 8, abs=1e-3)

    # Variance comparisons
    variance = metrics["variance"]["by_pair_type"]
    assert "same_ip" in variance
    assert "new_ip" in variance
    assert variance["same_ip"]["n_sessions"] == 2
    assert variance["same_ip"]["n_pairs"] == 1
    # same_ip overlap is 18/22 approx 0.818
    assert variance["same_ip"]["jaccard_20_mean"] > 0.80

    # single session in new_ip -> n_pairs = 0 and None mean
    assert variance["new_ip"]["n_sessions"] == 1
    assert variance["new_ip"]["n_pairs"] == 0
    assert variance["new_ip"]["jaccard_20_mean"] is None

    # TTL metrics
    ttl = metrics["ttl"]
    assert ttl["sessions"] == 1
    assert ttl["median_time_to_ip_change_s"] == pytest.approx(20.0)

    # Load metrics
    load = metrics["load"]
    assert "load_5_2" in load
    assert load["load_5_2"]["requests"] == 3


def test_write_outputs_full_pipeline(tmp_path: Path) -> None:
    db_file = tmp_path / "bench.db"
    run_id = _create_synthetic_db(db_file)
    out_dir = tmp_path / "results"

    outputs = write_outputs(db_file, run_id, out_dir)
    assert outputs["run_id"] == run_id

    # 1. Verify CSV files exist and have content
    for csv_name in ["sessions.csv", "requests.csv", "places.csv", "ip_observations.csv", "events.csv"]:
        csv_file = out_dir / csv_name
        assert csv_file.exists(), f"Missing CSV: {csv_name}"
        lines = csv_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 2, f"CSV {csv_name} has no header/data"

    # 2. Verify summary.json has all legacy keys specified in spec
    summary_file = out_dir / "summary.json"
    assert summary_file.exists()
    with open(summary_file, "r", encoding="utf-8") as f:
        summary = json.load(f)

    legacy_keys = [
        "started_at_iso", "run_id", "proxy_type", "session_strategy", "geo_targets",
        "concurrency", "elapsed_s", "total_queries", "successful_queries", "failed_queries",
        "total_places", "total_requests", "ok_requests", "consent_requests", "block_403",
        "block_429", "success_rate_pct", "consent_rate_pct", "block_rate_pct", "traffic_mb",
        "traffic_mb_wire", "traffic_tx_mb_est", "traffic_probe_est_mb", "traffic_mb_decompressed",
        "traffic_gb", "avg_latency_ms", "p95_latency_ms", "avg_query_ms", "p95_query_ms",
        "qps", "places_per_minute", "validation", "sticky_continuity", "by_country", "queries",
    ]
    for key in legacy_keys:
        assert key in summary, f"Missing legacy key in summary.json: {key}"

    # Verify query items
    assert len(summary["queries"]) > 0
    q0 = summary["queries"][0]
    for q_field in ["query", "country", "session", "status", "places", "requests", "stop_reason"]:
        assert q_field in q0, f"Missing field in query record: {q_field}"

    # 3. Verify analysis.json
    analysis_file = out_dir / "analysis.json"
    assert analysis_file.exists()
    with open(analysis_file, "r", encoding="utf-8") as f:
        analysis = json.load(f)
    assert "session_stability" in analysis
    assert "arm_breakdown" in analysis
    assert "variance" in analysis

    # 4. Verify report.md contains all 10 required sections
    report_file = out_dir / "report.md"
    assert report_file.exists()
    report_text = report_file.read_text(encoding="utf-8")
    required_sections = [
        "## Method",
        "## Environment / Manifest",
        "## Data Quality",
        "## Session Stability",
        "## Block Onset",
        "## Result Variance",
        "## Load",
        "## Pool Comparison",
        "## Limitations",
        "## Recommendations",
    ]
    for sec in required_sections:
        assert sec in report_text, f"Missing section in report.md: {sec}"


def test_write_outputs_empty_database(tmp_path: Path) -> None:
    """Must not crash on completely empty database or non-existent run_id."""
    db_file = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs(run_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    out_dir = tmp_path / "out_empty"
    # Should not throw any exception
    outputs = write_outputs(db_file, "unknown_run", out_dir)
    assert outputs["summary_file"] == str(out_dir / "summary.json")

    # Verify JSON files are created and valid
    with open(out_dir / "summary.json", "r", encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["total_queries"] == 0
    assert summary["total_requests"] == 0

    with open(out_dir / "analysis.json", "r", encoding="utf-8") as f:
        analysis = json.load(f)
    assert analysis["totals"]["total_sessions"] == 0

    report_text = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "## Method" in report_text
    assert "not run" in report_text.lower()


# ── 3. Regression Tests for Fixed Defects ────────────────────────────────────


def test_regression_missing_column_partial_schema(tmp_path: Path) -> None:
    """Defect 1: run_metrics must not crash on partial schema with missing columns (e.g. created_at)."""
    db_file = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE sessions (run_id TEXT, session_id TEXT PRIMARY KEY, started_at REAL)")
    conn.execute("INSERT INTO runs VALUES ('r1')")
    conn.execute("INSERT INTO sessions VALUES ('r1', 's1', 100.0)")
    conn.commit()
    conn.close()

    # Must return a dict and not raise sqlite3.OperationalError
    res = run_metrics(db_file, "r1")
    assert isinstance(res, dict)
    assert res["run_id"] == "r1"
    assert res["totals"]["total_sessions"] == 1

    # write_outputs must also succeed on partial schema
    out_dir = tmp_path / "partial_out"
    outputs = write_outputs(db_file, "r1", out_dir)
    assert Path(outputs["summary_file"]).exists()
    assert Path(outputs["report_file"]).exists()


def test_regression_km_nan_durations_risk_set() -> None:
    """Defect 2: km must calculate at_risk using only finite durations when skipping NaNs."""
    durations = [float("nan"), 10.0, 20.0]
    events = [0, 1, 1]
    curve = km(durations, events)

    assert len(curve) == 2
    # At t=10.0, at_risk should be 2 (not 3)
    assert curve[0]["t"] == 10.0
    assert curve[0]["at_risk"] == 2
    assert curve[0]["events"] == 1
    assert curve[0]["survival"] == pytest.approx(0.5)

    # At t=20.0, at_risk should be 1
    assert curve[1]["t"] == 20.0
    assert curve[1]["at_risk"] == 1
    assert curve[1]["events"] == 1
    assert curve[1]["survival"] == 0.0

    # Also check with multiple non-finite values
    curve2 = km([float("nan"), float("inf"), float("-inf"), 5.0, 15.0], [1, 0, 1, 1, 0])
    assert len(curve2) == 2
    assert curve2[0]["at_risk"] == 2


def test_regression_ttl_report_100pct_stickiness() -> None:
    """Defect 3: report.md must not say 'TTL test arm not run' when arm ran with 100% stickiness."""
    metrics_with_sticky_ttl = {
        "run_id": "ttl_run_01",
        "status": "completed",
        "totals": {"total_sessions": 2},
        "ttl": {
            "sessions": 2,
            "observed_ip_changes": 0,
            "median_time_to_ip_change_s": None,  # never reached 50% drop
            "changes_per_session": {"mean": 0.0, "min": 0, "max": 0, "total": 0},
        },
    }
    report = generate_report_md(metrics_with_sticky_ttl)

    # Must contain explicit statement of 0 IP changes / 100% stickiness
    assert "100% observed stickiness" in report
    assert "**IP Changes Observed:** 0 IP changes" in report
    assert "TTL stability was measured" in report
    # Under TTL section, it must NOT claim TTL arm was not run
    assert "TTL test arm not run in this session" not in report

    # In contrast, when ttl sessions is 0, it must say not run
    metrics_no_ttl = {
        "run_id": "no_ttl_run",
        "status": "completed",
        "totals": {"total_sessions": 1},
        "ttl": {
            "sessions": 0,
            "median_time_to_ip_change_s": None,
            "changes_per_session": {"mean": 0.0, "total": 0},
        },
    }
    report_no_ttl = generate_report_md(metrics_no_ttl)
    assert "*TTL test arm not run in this session.*" in report_no_ttl


def test_regression_dual_stack_v4_same_v6_different(tmp_path: Path) -> None:
    """Requirement 8a: v4 same/v6 different -> not rotated."""
    db_file = tmp_path / "dual_stack.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL, status TEXT)")
    conn.execute("CREATE TABLE sessions (run_id TEXT, session_id TEXT PRIMARY KEY, arm TEXT, variant TEXT, pool TEXT, country TEXT, started_at REAL, ended_at REAL)")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, query TEXT, country TEXT, outcome TEXT, is_final INTEGER, latency_ms REAL, bytes_wire INTEGER, bytes_rx INTEGER)")
    conn.execute("CREATE TABLE places (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, query TEXT, country TEXT, rank INTEGER, place_id TEXT, title TEXT)")
    conn.execute("CREATE TABLE ip_observations (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, witness TEXT, family TEXT, ip TEXT, asn TEXT, country TEXT)")

    run_id = "dual_test"
    conn.execute("INSERT INTO runs VALUES ('dual_test', 100.0, 110.0, 'completed')")
    conn.execute("INSERT INTO sessions VALUES ('dual_test', 'sess_dual_1', 'variance_pilot', 'same_ip', 'residential', 'IN', 100.0, 110.0)")
    conn.execute("INSERT INTO requests VALUES (1, 'dual_test', 'sess_dual_1', 102.0, 1, 'tea in Pune', 'IN', 'ok', 1, 500.0, 1000, 2000)")
    conn.execute("INSERT INTO places VALUES (1, 'dual_test', 'sess_dual_1', 'tea in Pune', 'IN', 1, 'p1', 'Place 1')")

    # Start probe: v4 1.2.3.4, v6 2001:db8::1
    conn.execute("INSERT INTO ip_observations VALUES (1, 'dual_test', 'sess_dual_1', 100.0, 0, 'ipify_v4', 'v4', '1.2.3.4', 'AS1', 'IN')")
    conn.execute("INSERT INTO ip_observations VALUES (2, 'dual_test', 'sess_dual_1', 100.0, 0, 'ipify_v6', 'v6', '2001:db8::1', 'AS1', 'IN')")
    # End probe: v4 1.2.3.4, v6 2001:db8::2 (v6 changed, v4 same)
    conn.execute("INSERT INTO ip_observations VALUES (3, 'dual_test', 'sess_dual_1', 106.0, 2, 'ipify_v4', 'v4', '1.2.3.4', 'AS1', 'IN')")
    conn.execute("INSERT INTO ip_observations VALUES (4, 'dual_test', 'sess_dual_1', 106.0, 2, 'ipify_v6', 'v6', '2001:db8::2', 'AS1', 'IN')")
    conn.commit()
    conn.close()

    m = run_metrics(db_file, run_id)
    assert m["sticky_continuity"]["query_ip_matched"] == 1
    assert m["sticky_continuity"]["query_ip_rotated"] == 0
    assert m["queries"][0]["ip_stable"] is True
    assert m["queries"][0]["exit_ip_start"] == "1.2.3.4"
    assert m["queries"][0]["exit_ip_end"] == "1.2.3.4"


def test_regression_ttl_primary_v4_stable_three_probes(tmp_path: Path) -> None:
    """Requirement 8b: TTL primary v4 stable 3 probes -> 0 changes."""
    db_file = tmp_path / "ttl_stable.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL, status TEXT)")
    conn.execute("CREATE TABLE sessions (run_id TEXT, session_id TEXT PRIMARY KEY, arm TEXT, variant TEXT, pool TEXT, country TEXT, started_at REAL, ended_at REAL)")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, query TEXT, country TEXT, outcome TEXT, is_final INTEGER, latency_ms REAL, bytes_wire INTEGER, bytes_rx INTEGER)")
    conn.execute("CREATE TABLE ip_observations (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, witness TEXT, family TEXT, ip TEXT, asn TEXT, country TEXT)")

    run_id = "ttl_stable_run"
    conn.execute("INSERT INTO runs VALUES ('ttl_stable_run', 0.0, 60.0, 'completed')")
    conn.execute("INSERT INTO sessions VALUES ('ttl_stable_run', 'ttl_sess', 'ttl_pilot', 'ttl_default', 'residential', 'IN', 0.0, 60.0)")

    # 3 dual probes over 60 seconds where v4 is constant 1.1.1.1
    conn.execute("INSERT INTO ip_observations VALUES (1, 'ttl_stable_run', 'ttl_sess', 0.0, 0, 'ipify_v4', 'v4', '1.1.1.1', 'AS1', 'IN')")
    conn.execute("INSERT INTO ip_observations VALUES (2, 'ttl_stable_run', 'ttl_sess', 0.0, 0, 'ipify_v6', 'v6', '2400::1', 'AS1', 'IN')")
    conn.execute("INSERT INTO ip_observations VALUES (3, 'ttl_stable_run', 'ttl_sess', 20.0, 1, 'ipify_v4', 'v4', '1.1.1.1', 'AS1', 'IN')")
    conn.execute("INSERT INTO ip_observations VALUES (4, 'ttl_stable_run', 'ttl_sess', 20.0, 1, 'ipify_v6', 'v6', '2400::2', 'AS1', 'IN')")
    conn.execute("INSERT INTO ip_observations VALUES (5, 'ttl_stable_run', 'ttl_sess', 40.0, 2, 'ipify_v4', 'v4', '1.1.1.1', 'AS1', 'IN')")
    conn.execute("INSERT INTO ip_observations VALUES (6, 'ttl_stable_run', 'ttl_sess', 40.0, 2, 'ipify_v6', 'v6', '2400::3', 'AS1', 'IN')")
    conn.commit()
    conn.close()

    m = run_metrics(db_file, run_id)
    ttl = m["ttl"]
    assert ttl["sessions"] == 1
    assert ttl["sessions_with_change"] == 0
    assert ttl["observed_ip_changes"] == 0
    assert ttl["changes_per_session"]["total"] == 0
    assert ttl["median_time_to_ip_change_s"] is None
    # Censored at last observation or ended_at
    assert ttl["km_curve"][0]["events"] == 0


def test_regression_variance_excludes_block_sessions(tmp_path: Path) -> None:
    """Requirement 8c: variance with block sessions same query -> block sessions excluded; new_ip n_pairs = C(4,2)=6 for 4 sessions."""
    db_file = tmp_path / "var_excl.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL, status TEXT)")
    conn.execute("CREATE TABLE sessions (run_id TEXT, session_id TEXT PRIMARY KEY, arm TEXT, variant TEXT, pool TEXT, country TEXT, started_at REAL, ended_at REAL)")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, query TEXT, country TEXT, outcome TEXT, is_final INTEGER, latency_ms REAL, bytes_wire INTEGER, bytes_rx INTEGER)")
    conn.execute("CREATE TABLE places (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, query TEXT, country TEXT, rank INTEGER, place_id TEXT, title TEXT)")

    run_id = "var_run"
    conn.execute("INSERT INTO runs VALUES ('var_run', 100.0, 200.0, 'completed')")

    # 4 sessions in arm="variance", variant="new_ip"
    for i in range(1, 5):
        s_id = f"var_new_{i}"
        conn.execute("INSERT INTO sessions VALUES ('var_run', ?, 'variance', 'new_ip', 'residential', 'US', 100.0, 150.0)", (s_id,))
        conn.execute("INSERT INTO requests VALUES (?, 'var_run', ?, 110.0, 1, 'coffee in seattle', 'US', 'ok', 1, 200.0, 1000, 2000)", (i, s_id))
        for r in range(1, 11):
            conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES ('var_run', ?, 'coffee in seattle', 'US', ?, ?, ?)",
                         (s_id, r, f"p_{r}", f"Place {r}"))

    # 2 sessions in arm="block_onset", variant="onset_default" for the exact same query
    for i in [5, 6]:
        s_id = f"block_{i}"
        conn.execute("INSERT INTO sessions VALUES ('var_run', ?, 'block_onset', 'onset_default', 'residential', 'US', 100.0, 150.0)", (s_id,))
        conn.execute("INSERT INTO requests VALUES (?, 'var_run', ?, 110.0, 1, 'coffee in seattle', 'US', 'ok', 1, 200.0, 1000, 2000)", (i, s_id))
        for r in range(1, 11):
            conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES ('var_run', ?, 'coffee in seattle', 'US', ?, ?, ?)",
                         (s_id, r, f"p_{r}", f"Place {r}"))

    conn.commit()
    conn.close()

    m = run_metrics(db_file, run_id)
    var = m["variance"]["by_pair_type"]
    assert "new_ip" in var
    assert "onset_default" not in var
    # Must only pair within variance arm -> 4 sessions choose 2 = 6 pairs!
    assert var["new_ip"]["n_sessions"] == 4
    assert var["new_ip"]["n_pairs"] == 6


def test_regression_query_success_rate_calculation(tmp_path: Path) -> None:
    """Requirement 8d: query success rate 9/10 -> 90.0."""
    db_file = tmp_path / "q_rate.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL, status TEXT)")
    conn.execute("CREATE TABLE sessions (run_id TEXT, session_id TEXT PRIMARY KEY, arm TEXT, variant TEXT, pool TEXT, country TEXT, started_at REAL, ended_at REAL)")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, query TEXT, country TEXT, outcome TEXT, is_final INTEGER, latency_ms REAL, bytes_wire INTEGER, bytes_rx INTEGER)")
    conn.execute("CREATE TABLE places (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, query TEXT, country TEXT, rank INTEGER, place_id TEXT, title TEXT)")

    run_id = "q_rate_run"
    conn.execute("INSERT INTO runs VALUES ('q_rate_run', 100.0, 200.0, 'completed')")

    # 10 queries across 10 sessions: 9 success, 1 blocked
    for i in range(1, 11):
        s_id = f"sess_{i}"
        conn.execute("INSERT INTO sessions VALUES ('q_rate_run', ?, 'block_onset', 'onset_default', 'residential', 'US', 100.0, 150.0)", (s_id,))
        if i <= 9:
            # 2 ok requests + places -> success
            conn.execute("INSERT INTO requests VALUES (?, 'q_rate_run', ?, 100.0, 1, ?, 'US', 'ok', 1, 200.0, 1000, 2000)", (i*2, s_id, f"query {i}"))
            conn.execute("INSERT INTO requests VALUES (?, 'q_rate_run', ?, 101.0, 2, ?, 'US', 'ok', 1, 200.0, 1000, 2000)", (i*2+1, s_id, f"query {i}"))
            conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES ('q_rate_run', ?, ?, 'US', 1, 'p1', 'P1')", (s_id, f"query {i}"))
        else:
            # query 10 blocked on request 1 with 0 places
            conn.execute("INSERT INTO requests VALUES (?, 'q_rate_run', ?, 100.0, 1, ?, 'US', '403', 1, 200.0, 1000, 2000)", (i*2, s_id, f"query {i}"))

    conn.commit()
    conn.close()

    out_dir = tmp_path / "q_rate_out"
    outputs = write_outputs(db_file, run_id, out_dir)

    with open(outputs["summary_file"]) as f:
        summary = json.load(f)

    assert summary["total_queries"] == 10
    assert summary["successful_queries"] == 9
    assert summary["query_success_rate_pct"] == 90.0

    report_text = Path(outputs["report_file"]).read_text()
    assert "9 / 10" in report_text
    assert "90.0%" in report_text


def test_regression_real_pilot_db_if_available() -> None:
    """Requirement 8e: regression test on real pilot DB if present."""
    pilot_db = Path("results/pilot/run.db")
    if not pilot_db.exists():
        pytest.skip("results/pilot/run.db not present")

    m = run_metrics(str(pilot_db), "pilot")
    # 1. sticky continuity: all 10 queries matched
    assert m["sticky_continuity"]["query_ip_matched"] == 10
    assert m["sticky_continuity"]["query_ip_rotated"] == 0

    # 2. ttl: 0 changes observed
    assert m["ttl"]["sessions"] == 2
    assert m["ttl"]["observed_ip_changes"] == 0
    assert m["ttl"]["sessions_with_change"] == 0
    assert m["ttl"]["median_time_to_ip_change_s"] is None

    # 3. variance: clean 1 pair for same_ip and 1 pair for new_ip
    var = m["variance"]["by_pair_type"]
    assert var["same_ip"]["n_sessions"] == 2
    assert var["same_ip"]["n_pairs"] == 1
    assert var["new_ip"]["n_sessions"] == 2
    assert var["new_ip"]["n_pairs"] == 1

    # 4. query success rate: all 10 queries succeeded after non-final retry fix
    assert m["rates"]["query_success_rate_pct"] == 100.0
    assert m["query_status_counts"]["success"] == 10

    # 5. stale event rows ignored when observations show no primary change
    assert m["ttl"]["replacement_events"] == {}


def test_regression_conditional_recommendations_no_blocks_no_mobile() -> None:
    """Requirement 1: Recommendations must not overclaim rotation intervals or mobile pools when not observed."""
    # Keep this unit test independent of optional local result artifacts.
    m = {
        "run_id": "test_pilot",
        "totals": {"total_sessions": 2, "total_blocks": 0, "successful_queries": 2, "total_queries": 2},
        "rates": {"block_rate": 0.0, "block_rate_ci": (0.0, 0.0), "success_rate": 1.0, "success_rate_ci": (1.0, 1.0)},
        "session_stability": {"sessions": 2, "sessions_with_block": 0, "first_block_distribution": {"count": 0}},
        "ttl": {"sessions": 2, "observed_ip_changes": 0, "median_time_to_ip_change_s": None},
        "pools": {"residential": {"sessions": 2}},
        "load": {},
        "variance": {},
    }
    report = generate_report_md(m)

    # Must NOT claim arbitrary rotation or mobile recommendation
    assert "every 10" not in report
    assert "Use mobile pools" not in report

    # Must explicitly state not measured / no block observed
    assert "no block" in report.lower()
    assert "not measured" in report.lower()


def test_regression_stale_event_rows_ignored_when_no_primary_change(tmp_path: Path) -> None:
    """Requirement 2: Stale historical event rows must be ignored when primary observations show no change."""
    db_file = tmp_path / "stale_event.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL, status TEXT)")
    conn.execute("CREATE TABLE sessions (run_id TEXT, session_id TEXT PRIMARY KEY, arm TEXT, variant TEXT, pool TEXT, country TEXT, started_at REAL, ended_at REAL)")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, query TEXT, country TEXT, outcome TEXT, is_final INTEGER, latency_ms REAL, bytes_wire INTEGER, bytes_rx INTEGER)")
    conn.execute("CREATE TABLE ip_observations (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, witness TEXT, family TEXT, ip TEXT, asn TEXT, country TEXT)")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, request_index INTEGER, event_type TEXT, detail_json TEXT, arm TEXT, variant TEXT)")

    run_id = "stale_run"
    conn.execute("INSERT INTO runs VALUES ('stale_run', 0.0, 60.0, 'completed')")
    conn.execute("INSERT INTO sessions VALUES ('stale_run', 'sess_stale_1', 'ttl_pilot', 'ttl_default', 'residential', 'IN', 0.0, 60.0)")

    # Session observations have constant IPv4 across all probes -> 0 primary changes
    conn.execute("INSERT INTO ip_observations VALUES (1, 'stale_run', 'sess_stale_1', 0.0, 0, 'ipify_v4', 'v4', '1.1.1.1', 'AS1', 'IN')")
    conn.execute("INSERT INTO ip_observations VALUES (2, 'stale_run', 'sess_stale_1', 20.0, 1, 'ipify_v4', 'v4', '1.1.1.1', 'AS1', 'IN')")

    # BUT events table has a stale/erroneous ip_change row (e.g. from v4 vs v6 bug)
    conn.execute("INSERT INTO events VALUES (1, 'stale_run', 'sess_stale_1', 0.1, 0, 'ip_change', '{\"old\": \"1.1.1.1\", \"new\": \"2400::1\"}', 'ttl_pilot', 'ttl_default')")
    conn.commit()
    conn.close()

    m = run_metrics(db_file, run_id)
    assert m["ttl"]["observed_ip_changes"] == 0
    assert m["ttl"]["sessions_with_change"] == 0
    # Stale event row must be ignored:
    assert m["ttl"]["replacement_events"] == {}


def test_regression_pilot_fixed_db_metrics() -> None:
    """Verify pilot_fixed has ttl_expiry: 2 and conditional recommendations."""
    pilot_fixed_db = Path("results/pilot_fixed/run.db")
    if not pilot_fixed_db.exists():
        pytest.skip("results/pilot_fixed/run.db not present")

    m = run_metrics(str(pilot_fixed_db), "pilot_fixed")
    # In pilot_fixed, primary IPv4 changed after 65s -> 2 ttl_expiry events
    assert m["ttl"]["observed_ip_changes"] == 2
    assert m["ttl"]["replacement_events"] == {"ttl_expiry": 2}

    # Verify recommendations in report.md
    report = generate_report_md(m)
    assert "no block" in report.lower()
    assert "not measured" in report.lower()
    assert "every 10" not in report
    assert "Use mobile pools" not in report


def test_regression_query_status_terminal_scenarios(tmp_path: Path) -> None:
    """Regression tests covering terminal request evaluation in query-status loop:
    a. proxy_error with is_final=0 followed by ok with is_final=1 and places returned -> query status is 'success'.
    b. final proxy_error (is_final=1 or terminal is_final=None) with no places -> query status is 'error'.
    c. page 1 ok + page 2 proxy_error with places -> query status is 'partial'.
    """
    db_file = tmp_path / "terminal_status_scenarios.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL, status TEXT)")
    conn.execute("CREATE TABLE sessions (run_id TEXT, session_id TEXT PRIMARY KEY, arm TEXT, variant TEXT, pool TEXT, country TEXT, started_at REAL, ended_at REAL)")
    conn.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL, "
        "request_index INTEGER, query TEXT, country TEXT, outcome TEXT, attempt INTEGER, "
        "is_final INTEGER, latency_ms REAL, bytes_wire INTEGER, bytes_rx INTEGER)"
    )
    conn.execute("CREATE TABLE places (id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, query TEXT, country TEXT, rank INTEGER, place_id TEXT, title TEXT)")

    run_id = "term_run"
    conn.execute("INSERT INTO runs VALUES ('term_run', 1000.0, 1050.0, 'completed')")
    conn.execute("INSERT INTO sessions VALUES ('term_run', 'sess_term', 'block_onset', 'onset_default', 'residential', 'US', 1000.0, 1050.0)")

    # a. proxy_error with is_final=0 followed by ok with is_final=1 and places returned -> query status is 'success'
    conn.execute("INSERT INTO requests VALUES (1, 'term_run', 'sess_term', 1001.0, 1, 'query_a', 'US', 'proxy_error', 0, 0, 1100.0, 500, 500)")
    conn.execute("INSERT INTO requests VALUES (2, 'term_run', 'sess_term', 1002.0, 1, 'query_a', 'US', 'ok', 1, 1, 850.0, 40000, 150000)")
    conn.execute("INSERT INTO places VALUES (1, 'term_run', 'sess_term', 'query_a', 'US', 1, 'pl_a1', 'Place A1')")
    conn.execute("INSERT INTO places VALUES (2, 'term_run', 'sess_term', 'query_a', 'US', 2, 'pl_a2', 'Place A2')")

    # b1. final proxy_error with is_final=1 and no places -> query status is 'error'
    conn.execute("INSERT INTO requests VALUES (3, 'term_run', 'sess_term', 1010.0, 2, 'query_b1', 'US', 'proxy_error', 0, 1, 1400.0, 600, 600)")

    # b2. final proxy_error with is_final=None (legacy/terminal) and no places -> query status is 'error'
    conn.execute("INSERT INTO requests VALUES (4, 'term_run', 'sess_term', 1015.0, 3, 'query_b2', 'US', 'proxy_error', 0, NULL, 1300.0, 600, 600)")

    # c. page 1 ok + page 2 proxy_error with places -> query status is 'partial'
    conn.execute("INSERT INTO requests VALUES (5, 'term_run', 'sess_term', 1020.0, 4, 'query_c', 'US', 'ok', 0, 1, 900.0, 35000, 120000)")
    conn.execute("INSERT INTO requests VALUES (6, 'term_run', 'sess_term', 1022.0, 4, 'query_c', 'US', 'proxy_error', 0, 1, 1250.0, 700, 700)")
    conn.execute("INSERT INTO places VALUES (3, 'term_run', 'sess_term', 'query_c', 'US', 1, 'pl_c1', 'Place C1')")

    conn.commit()
    conn.close()

    m = run_metrics(db_file, run_id)
    queries = {q["query"]: q for q in m["queries"]}

    # Scenario a assertions
    qa = queries["query_a"]
    assert qa["status"] == "success"
    assert qa["partial"] is False
    assert qa["stop_reason"] == "limit"
    assert qa["places"] == 2
    assert qa["requests"] == 2
    assert qa["ok"] == 1
    assert qa["proxy_errors"] == 1

    # Scenario b1 assertions (is_final=1, 0 places)
    qb1 = queries["query_b1"]
    assert qb1["status"] == "error"
    assert qb1["partial"] is False
    assert qb1["stop_reason"] == "proxy_error"
    assert qb1["places"] == 0

    # Scenario b2 assertions (is_final=None, 0 places)
    qb2 = queries["query_b2"]
    assert qb2["status"] == "error"
    assert qb2["partial"] is False
    assert qb2["stop_reason"] == "proxy_error"
    assert qb2["places"] == 0

    # Scenario c assertions (page 1 ok + page 2 proxy_error with places)
    qc = queries["query_c"]
    assert qc["status"] == "partial"
    assert qc["partial"] is True
    assert qc["stop_reason"] == "proxy_error"
    assert qc["places"] == 1

    # Verify request-level metrics include non-final retry (is_final=0)
    assert m["request_outcome_counts"]["proxy_error"] == 4
    assert m["request_outcome_counts"]["ok"] == 2
    assert m["query_status_counts"]["success"] == 1
    assert m["query_status_counts"]["error"] == 2
    assert m["query_status_counts"]["partial"] == 1
    assert m["traffic"]["traffic_mb_wire"] > 0
    assert m["latency_by_outcome"]["proxy_error"]["n"] == 4
    assert m["latency_by_outcome"]["ok"]["n"] == 2


def test_regression_pilot15_ttl_report() -> None:
    """Requirement: pilot15 (5 sessions, 2 changes, median None) report text check."""
    pilot15_db = Path("results/pilot15/run.db")
    if not pilot15_db.exists():
        pytest.skip("results/pilot15/run.db not present")

    m = run_metrics(str(pilot15_db), "pilot15")
    report = generate_report_md(m)

    # 1. TTL section checks
    assert "2 IP changes" in report
    assert "0 IP changes observed" not in report
    assert "100% observed stickiness" not in report
    assert "survival 60.0% at end" in report or "median not reached" in report

    # 2. Recommendations checks
    assert "TTL stability was not measured" not in report
    assert "TTL stability was measured" in report
    assert "do not extrapolate" in report.lower()



