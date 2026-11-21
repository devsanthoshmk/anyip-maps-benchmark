"""tests/verify_analyze.py — Independent verification test suite for analyze.py.

DO NOT TRUST IMPLEMENTER TESTS.
Verifies all 7 requirements from the specification by running code against
hand-computed exact and closed-form values:
1. Wilson score interval without continuity correction (0/0, 0/10, 10/10, 50/100).
2. Rule of three and Kaplan-Meier survival estimator (ties, censoring, median_km, undefined median).
3. Bootstrap CI (seed determinism, constant/empty input, point estimate containment).
4. Jaccard, Jaccard@k, RBO Eq. (32), and Kendall tau-b (disjoint, identical, prefix, ties).
5. Synthetic 3-session SQLite DB with known block onset, known IP changes, two queries,
   and two variants (same_ip/new_ip). Reconciles every headline number.
   Also tests load RPS and pool rates.
6. write_outputs file generation, legacy summary keys, NaN/Inf rejection with parse_constant,
   report.md sections and absent arm handling, empty DB safety.
7. pytest and compileall passes.
"""

import json
import math
from pathlib import Path
import sqlite3
import pytest

from analyze import (
    bootstrap_ci,
    generate_report_md,
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


# ── 1. Wilson Score Confidence Interval Verification ─────────────────────────


def test_wilson_closed_form() -> None:
    """Verify Wilson score CI against closed-form algebra and 6-decimal values."""
    # 0/0: Undefined / 0 trials -> spec mandates (0.0, 0.0)
    assert wilson(0, 0) == (0.0, 0.0)
    assert wilson(0, -1) == (0.0, 0.0)

    # 0/10:
    # Closed form: lower = 0, upper = 2 * (z^2 / (2n)) / (1 + z^2/n) = z^2 / (n + z^2)
    # For z = 1.96, z^2 = 3.8416:
    # upper = 3.8416 / (10 + 3.8416) = 3.8416 / 13.8416 = 2401 / 8651 approx 0.2775398...
    lo_0_10, hi_0_10 = wilson(0, 10, z=1.96)
    assert lo_0_10 == 0.0
    assert hi_0_10 == pytest.approx(2401 / 8651, abs=1e-5)
    assert hi_0_10 == pytest.approx(0.277540, abs=1e-4)

    # 10/10:
    # By binomial symmetry with 0/10:
    # lower = 1 - 2401/8651 = 6250 / 8651 approx 0.722460...
    # upper = 1.0
    lo_10_10, hi_10_10 = wilson(10, 10, z=1.96)
    assert hi_10_10 == 1.0
    assert lo_10_10 == pytest.approx(6250 / 8651, abs=1e-5)
    assert lo_10_10 == pytest.approx(0.722460, abs=1e-4)

    # 50/100:
    # Center = 0.5
    # Margin = (z / 2) / sqrt(n + z^2) = 0.98 / sqrt(103.8416) = 49 / (2 * sqrt(64901)) approx 0.096169978...
    # lower = 0.5 - 0.096170 = 0.403830
    # upper = 0.5 + 0.096170 = 0.596170
    lo_50_100, hi_50_100 = wilson(50, 100, z=1.96)
    expected_margin = 49.0 / (2.0 * math.sqrt(64901.0))
    assert lo_50_100 == pytest.approx(0.5 - expected_margin, abs=1e-5)
    assert hi_50_100 == pytest.approx(0.5 + expected_margin, abs=1e-5)
    assert (lo_50_100 + hi_50_100) / 2.0 == pytest.approx(0.5, abs=1e-6)

    # Clamping behavior for out of bounds k
    assert wilson(-5, 10) == wilson(0, 10)
    assert wilson(15, 10) == wilson(10, 10)


# ── 2. Rule of Three and Kaplan-Meier Verification ───────────────────────────


def test_rule_of_three_hand_computed() -> None:
    """Verify rule of three 95% upper bound: min(1.0, 3.0 / n) for n > 0 else 1.0."""
    assert rule_of_three(0) == 1.0
    assert rule_of_three(-1) == 1.0
    assert rule_of_three(1) == 1.0  # min(1.0, 3.0)
    assert rule_of_three(2) == 1.0  # min(1.0, 1.5)
    assert rule_of_three(3) == 1.0  # min(1.0, 1.0)
    assert rule_of_three(10) == pytest.approx(0.300000, abs=1e-6)
    assert rule_of_three(100) == pytest.approx(0.030000, abs=1e-6)
    assert rule_of_three(300) == pytest.approx(0.010000, abs=1e-6)


def test_km_hand_computed_with_ties_and_censoring() -> None:
    """Verify KM curve and median on hand-computed dataset with ties and censoring.

    Data:
      durations = [10.0, 20.0, 20.0, 30.0, 30.0, 40.0]
      events    = [   1,    1,    0,    1,    0,    1]

    Hand calculation:
      t=10: at_risk=6, events=1, censored=0 -> S(10) = 1 - 1/6 = 5/6 approx 0.833333
      t=20: at_risk=5, events=1, censored=1 -> S(20) = (5/6) * (1 - 1/5) = 4/6 = 2/3 approx 0.666667
      t=30: at_risk=3, events=1, censored=1 -> S(30) = (2/3) * (1 - 1/3) = 4/9 approx 0.444444 <= 0.5
      t=40: at_risk=1, events=1, censored=0 -> S(40) = (4/9) * (1 - 1/1) = 0.0

    Median KM: Smallest t where S(t) <= 0.5 -> t=30.0.
    """
    durations = [10.0, 20.0, 20.0, 30.0, 30.0, 40.0]
    events = [1, 1, 0, 1, 0, 1]

    curve = km(durations, events)
    assert len(curve) == 4

    # Point 1 (t=10)
    assert curve[0]["t"] == 10.0
    assert curve[0]["at_risk"] == 6
    assert curve[0]["events"] == 1
    assert curve[0]["survival"] == pytest.approx(5 / 6, abs=1e-5)

    # Point 2 (t=20)
    assert curve[1]["t"] == 20.0
    assert curve[1]["at_risk"] == 5
    assert curve[1]["events"] == 1
    assert curve[1]["survival"] == pytest.approx(2 / 3, abs=1e-5)

    # Point 3 (t=30)
    assert curve[2]["t"] == 30.0
    assert curve[2]["at_risk"] == 3
    assert curve[2]["events"] == 1
    assert curve[2]["survival"] == pytest.approx(4 / 9, abs=1e-5)

    # Point 4 (t=40)
    assert curve[3]["t"] == 40.0
    assert curve[3]["at_risk"] == 1
    assert curve[3]["events"] == 1
    assert curve[3]["survival"] == pytest.approx(0.0, abs=1e-5)

    # Median KM
    assert median_km(curve) == 30.0
    assert median_km(durations, events) == 30.0


def test_km_median_when_survival_never_drops_to_half() -> None:
    """Verify median KM behavior when survival never reaches <= 0.5."""
    # Case A: All censored -> S(t) = 1.0 everywhere
    durations_all_censored = [5.0, 10.0, 15.0]
    events_all_censored = [0, 0, 0]
    curve_a = km(durations_all_censored, events_all_censored)
    assert all(pt["survival"] == 1.0 for pt in curve_a)
    assert median_km(curve_a) is None
    assert median_km(durations_all_censored, events_all_censored) is None

    # Case B: One event, two censored -> S(10) = 1 - 1/3 = 2/3 > 0.5 everywhere
    durations_b = [10.0, 20.0, 30.0]
    events_b = [1, 0, 0]
    curve_b = km(durations_b, events_b)
    assert curve_b[0]["survival"] == pytest.approx(2 / 3, abs=1e-5)
    assert curve_b[1]["survival"] == pytest.approx(2 / 3, abs=1e-5)
    assert curve_b[2]["survival"] == pytest.approx(2 / 3, abs=1e-5)
    assert median_km(curve_b) is None

    # Case C: Empty input
    assert km([], []) == []
    assert median_km([], []) is None
    assert median_km([]) is None


# ── 3. Bootstrap CI Verification ─────────────────────────────────────────────


def test_bootstrap_ci_properties() -> None:
    """Verify bootstrap CI seed determinism, constant/empty inputs, and point estimate."""
    # Empty input
    assert bootstrap_ci([]) == (0.0, 0.0)

    # Single value
    assert bootstrap_ci([42.5]) == (42.5, 42.5)

    # Constant input (zero variance)
    assert bootstrap_ci([17.0, 17.0, 17.0, 17.0]) == (17.0, 17.0)

    # Seed determinism: two calls with same seed must give identical bounds
    sample = [12.0, 15.5, 18.2, 22.0, 25.1, 30.0, 35.4, 40.2]
    ci1 = bootstrap_ci(sample, n=5000, seed=12345)
    ci2 = bootstrap_ci(sample, n=5000, seed=12345)
    assert ci1 == ci2

    # Different seeds produce slightly different estimates
    ci3 = bootstrap_ci(sample, n=5000, seed=99999)
    assert ci1 != ci3

    # CI contains point estimate (sample mean)
    mean_val = sum(sample) / len(sample)
    assert ci1[0] <= mean_val <= ci1[1]

    # Custom stat function (e.g. median)
    median_fn = lambda xs: sorted(xs)[len(xs) // 2]
    ci_med = bootstrap_ci(sample, stat=median_fn, n=3000, seed=42)
    sample_med = median_fn(sample)
    assert ci_med[0] <= sample_med <= ci_med[1]


# ── 4. Similarity Metrics: Jaccard, Jaccard@k, RBO, Kendall Tau ──────────────


def test_jaccard_hand_computed() -> None:
    """Verify Jaccard and Jaccard@k edge cases."""
    # Both empty -> 1.0 (agreement on empty)
    assert jaccard([], []) == 1.0
    assert jaccard(set(), set()) == 1.0

    # One empty -> 0.0
    assert jaccard(["x"], []) == 0.0
    assert jaccard([], ["y"]) == 0.0

    # Disjoint
    assert jaccard(["a", "b"], ["c", "d"]) == 0.0

    # Identical
    assert jaccard(["a", "b", "c"], ["a", "b", "c"]) == 1.0

    # Partial overlap: {a, b, c} vs {b, c, d} -> intersection=2, union=4 -> 0.5
    assert jaccard(["a", "b", "c"], ["b", "c", "d"]) == pytest.approx(0.5)

    # Jaccard@k
    l1 = ["a", "b", "c", "d"]
    l2 = ["a", "b", "e", "f"]
    assert jaccard_at_k(l1, l2, 0) == 1.0
    assert jaccard_at_k(l1, l2, 2) == 1.0  # {a, b} vs {a, b}
    assert jaccard_at_k(l1, l2, 4) == pytest.approx(2 / 6)  # {a, b, c, d} vs {a, b, e, f}


def test_rbo_hand_computed_and_paper_cases() -> None:
    """Verify RBO Eq. (32) against Webber et al. (2010) closed forms."""
    # Both empty -> 1.0; one empty -> 0.0
    assert rbo([], [], p=0.9) == 1.0
    assert rbo(["a"], [], p=0.9) == 0.0
    assert rbo([], ["b"], p=0.9) == 0.0

    # Identical rankings -> 1.0
    assert rbo(["a", "b", "c", "d"], ["a", "b", "c", "d"], p=0.9) == pytest.approx(1.0)

    # Disjoint rankings -> 0.0
    assert rbo(["a", "b"], ["c", "d"], p=0.9) == 0.0

    # Hand-computed Prefix list: S = ["a"], L = ["a", "b"], p = 0.9
    # Paper proves: for any prefix match S = L[:s], extrapolated RBO is identically 1.0
    assert rbo(["a"], ["a", "b"], p=0.9) == pytest.approx(1.0)
    assert rbo(["a", "b"], ["a", "b", "c", "d"], p=0.85) == pytest.approx(1.0)

    # Hand-computed Inverted prefix: S = ["a"], L = ["b", "a"], p = 0.9
    # Overlaps: X1 = 0, X2 = 1.
    # sum1 = (1 - p) * (0 + 0.9 * 1/2) = 0.045
    # sum2 = 0 (Xs = 0)
    # tail = p^2 * (1/2 + 0) = 0.81 * 0.5 = 0.405
    # RBO = 0.045 + 0.405 = 0.450 = p / 2
    assert rbo(["a"], ["b", "a"], p=0.9) == pytest.approx(0.450000, abs=1e-6)
    assert rbo(["a"], ["b", "a"], p=0.8) == pytest.approx(0.400000, abs=1e-6)


def test_kendall_tau_hand_computed() -> None:
    """Verify Kendall tau-b with concordant, discordant, and tied rankings."""
    # Concordant -> 1.0
    assert kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == pytest.approx(1.0)

    # Discordant -> -1.0
    assert kendall_tau(["a", "b", "c"], ["c", "b", "a"]) == pytest.approx(-1.0)

    # Fewer than 2 common items -> 0.0 (or 1.0 for single identical item)
    assert kendall_tau(["a"], ["b"]) == 0.0
    assert kendall_tau(["a"], ["a"]) == 1.0
    assert kendall_tau([], []) == 0.0

    # Tied rankings:
    # x = {"a": 0, "b": 0, "c": 1}, y = {"a": 0, "b": 1, "c": 2}
    # Pairs:
    # (a,b): dx=0, dy=-1 -> tied in X only (t_x = 1)
    # (a,c): dx=-1, dy=-2 -> concordant (p = 1)
    # (b,c): dx=-1, dy=-1 -> concordant (p = 2)
    # q = 0, t_y = 0
    # tau_b = (2 - 0) / sqrt((2 + 0 + 1) * (2 + 0 + 0)) = 2 / sqrt(6) = sqrt(2/3) approx 0.81649658...
    rank_x = {"a": 0, "b": 0, "c": 1}
    rank_y = {"a": 0, "b": 1, "c": 2}
    expected_tau = 2.0 / math.sqrt(6.0)
    assert kendall_tau(rank_x, rank_y, variant="b") == pytest.approx(expected_tau, abs=1e-6)


# ── 5. Synthetic SQLite DB Verification (Task 5) ─────────────────────────────


def _build_synthetic_3_session_db(db_path: Path) -> str:
    """Build exact synthetic SQLite DB specified in verification task 5:

    - 3 sessions:
        * sess_1: variance arm, same_ip variant, pool residential, country US
        * sess_2: variance arm, same_ip variant, pool residential, country US
        * sess_3: variance arm, new_ip variant, pool mobile, country US
    - Known block onset:
        * sess_1: 0 blocks, 2 requests
        * sess_2: 1 block (403) at request 2
        * sess_3: 0 blocks, 2 requests
    - Known IP changes:
        * sess_1: ts 100 -> 1.1.1.1, ts 105 -> 1.1.1.1 (1 distinct IP, 0 changes)
        * sess_2: ts 110 -> 2.2.2.1, ts 115 -> 2.2.2.2, ts 120 -> 2.2.2.3 (3 distinct IPs, 2 changes, first at 5s)
        * sess_3: ts 130 -> 3.3.3.1, ts 135 -> 3.3.3.1 (1 distinct IP, 0 changes)
    - Two queries with known place sets:
        * "pizza nyc":
            - sess_1: [P1, P2, P3, P4] (4 places)
            - sess_2: [P1, P2, P3, P5] (4 places) -> same_ip Jaccard = 3/5 = 0.60
            - sess_3: [P1, P6] (2 places) -> new_ip Jaccard with sess_1 = 1/5 = 0.20, with sess_2 = 1/5 = 0.20
        * "sushi nyc":
            - sess_1: [Q1, Q2] (2 places)
            - sess_2: [Q1, Q2] (2 places) -> same_ip Jaccard = 1.00
            - sess_3: [Q3, Q4] (2 places) -> new_ip Jaccard with sess_1 = 0.00, with sess_2 = 0.00
    """
    conn = sqlite3.connect(str(db_path))
    run_id = "syn_verif_001"

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

    # 1. runs: started at 100.0, finished at 160.0 (elapsed = 60.0s)
    conn.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, 100.0, 160.0, '{"test": 1}', '{}', "completed"),
    )

    # 2. 3 sessions
    # sess_1: variance, same_ip, residential, US, 2 requests, 6 places, 0 blocks
    conn.execute(
        """INSERT INTO sessions (
            run_id, session_id, arm, variant, pool, country, session_name,
            sesstime_min, created_at, started_at, ended_at, requests, google_requests,
            places, block_events, first_block_request, status, distinct_ips
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, "sess_1", "variance", "same_ip", "residential", "US", "sess_1", 30, 100.0, 100.0, 110.0, 2, 2, 6, 0, None, "completed", 1),
    )
    # sess_2: variance, same_ip, residential, US, 2 requests, 6 places, 1 block at req 2
    conn.execute(
        """INSERT INTO sessions (
            run_id, session_id, arm, variant, pool, country, session_name,
            sesstime_min, created_at, started_at, ended_at, requests, google_requests,
            places, block_events, first_block_request, first_block_ts, status, distinct_ips
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, "sess_2", "variance", "same_ip", "residential", "US", "sess_2", 30, 110.0, 110.0, 125.0, 2, 2, 6, 1, 2, 120.0, "completed", 3),
    )
    # sess_3: variance, new_ip, mobile, US, 2 requests, 4 places, 0 blocks
    conn.execute(
        """INSERT INTO sessions (
            run_id, session_id, arm, variant, pool, country, session_name,
            sesstime_min, created_at, started_at, ended_at, requests, google_requests,
            places, block_events, first_block_request, status, distinct_ips
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, "sess_3", "variance", "new_ip", "mobile", "US", "sess_3", 30, 130.0, 130.0, 140.0, 2, 2, 4, 0, None, "completed", 1),
    )

    # 3. requests: 6 requests total
    # Wire bytes: 10,000 each -> total 60,000 bytes = 60000 / 1048576 MB approx 0.05722 MB
    # Latencies:
    # sess_1: req 1 -> 800ms, req 2 -> 900ms (both ok)
    # sess_2: req 1 -> 1000ms (ok), req 2 -> 400ms (403 block)
    # sess_3: req 1 -> 1100ms (ok), req 2 -> 1200ms (ok)
    reqs = [
        ("sess_1", 101.0, 1, "pizza nyc", "US", "ok", 1, 1, 800.0, 10000, 50000, "variance", "same_ip"),
        ("sess_1", 106.0, 2, "sushi nyc", "US", "ok", 1, 1, 900.0, 10000, 50000, "variance", "same_ip"),
        ("sess_2", 112.0, 1, "pizza nyc", "US", "ok", 1, 1, 1000.0, 10000, 50000, "variance", "same_ip"),
        ("sess_2", 120.0, 2, "sushi nyc", "US", "403", 1, 1, 400.0, 10000, 5000, "variance", "same_ip"),
        ("sess_3", 131.0, 1, "pizza nyc", "US", "ok", 1, 1, 1100.0, 10000, 50000, "variance", "new_ip"),
        ("sess_3", 136.0, 2, "sushi nyc", "US", "ok", 1, 1, 1200.0, 10000, 50000, "variance", "new_ip"),
    ]
    for s_id, ts, r_idx, qry, ctry, outc, att, is_fin, lat, wire, rx, arm, var in reqs:
        conn.execute(
            """INSERT INTO requests (
                run_id, session_id, ts, request_index, query, country,
                outcome, attempt, is_final, latency_ms, bytes_wire, bytes_rx, arm, variant
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, s_id, ts, r_idx, qry, ctry, outc, att, is_fin, lat, wire, rx, arm, var),
        )

    # 4. places
    # "pizza nyc":
    # sess_1: P1, P2, P3, P4
    for rank, pid in enumerate(["P1", "P2", "P3", "P4"], 1):
        conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run_id, "sess_1", "pizza nyc", "US", rank, pid, f"Place {pid}"))
    # sess_2: P1, P2, P3, P5
    for rank, pid in enumerate(["P1", "P2", "P3", "P5"], 1):
        conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run_id, "sess_2", "pizza nyc", "US", rank, pid, f"Place {pid}"))
    # sess_3: P1, P6
    for rank, pid in enumerate(["P1", "P6"], 1):
        conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run_id, "sess_3", "pizza nyc", "US", rank, pid, f"Place {pid}"))

    # "sushi nyc":
    # sess_1: Q1, Q2
    for rank, pid in enumerate(["Q1", "Q2"], 1):
        conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run_id, "sess_1", "sushi nyc", "US", rank, pid, f"Place {pid}"))
    # sess_2: Q1, Q2
    for rank, pid in enumerate(["Q1", "Q2"], 1):
        conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run_id, "sess_2", "sushi nyc", "US", rank, pid, f"Place {pid}"))
    # sess_3: Q3, Q4
    for rank, pid in enumerate(["Q3", "Q4"], 1):
        conn.execute("INSERT INTO places (run_id, session_id, query, country, rank, place_id, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (run_id, "sess_3", "sushi nyc", "US", rank, pid, f"Place {pid}"))

    # 5. ip_observations (7 total observations)
    ip_obs = [
        ("sess_1", 100.0, 0, "1.1.1.1", "AS100", "US"),
        ("sess_1", 105.0, 1, "1.1.1.1", "AS100", "US"),
        ("sess_2", 110.0, 0, "2.2.2.1", "AS200", "US"),
        ("sess_2", 115.0, 1, "2.2.2.2", "AS200", "US"),
        ("sess_2", 120.0, 2, "2.2.2.3", "AS200", "US"),
        ("sess_3", 130.0, 0, "3.3.3.1", "AS300", "US"),
        ("sess_3", 135.0, 1, "3.3.3.1", "AS300", "US"),
    ]
    for s_id, ts, r_idx, ip, asn, ctry in ip_obs:
        conn.execute(
            "INSERT INTO ip_observations (run_id, session_id, ts, request_index, witness, family, ip, asn, country) "
            "VALUES (?, ?, ?, ?, 'ipify_v4', 'v4', ?, ?, ?)",
            (run_id, s_id, ts, r_idx, ip, asn, ctry),
        )

    # 6. events: 2 ip_change events in sess_2
    conn.execute(
        "INSERT INTO events (run_id, session_id, ts, request_index, event_type, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "sess_2", 115.0, 1, "ip_change", '{"old": "2.2.2.1", "new": "2.2.2.2"}'),
    )
    conn.execute(
        "INSERT INTO events (run_id, session_id, ts, request_index, event_type, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "sess_2", 120.0, 2, "ip_change", '{"old": "2.2.2.2", "new": "2.2.2.3"}'),
    )

    conn.commit()
    conn.close()
    return run_id


def test_synthetic_3_session_metrics_reconciliation(tmp_path: Path) -> None:
    """Run run_metrics on the 3-session DB and independently verify EVERY headline number."""
    db_file = tmp_path / "synthetic_3sess.db"
    run_id = _build_synthetic_3_session_db(db_file)

    metrics = run_metrics(db_file, run_id)
    totals = metrics["totals"]

    # 1. Totals Reconciliation
    assert totals["total_sessions"] == 3
    assert totals["total_places"] == 16  # (4 + 4 + 2) + (2 + 2 + 2) = 16
    assert totals["total_requests"] == 6
    assert totals["final_requests"] == 6
    assert totals["ok_requests"] == 5
    assert totals["block_403"] == 1
    assert totals["block_429"] == 0

    # Traffic
    # Wire bytes: 6 * 10,000 = 60,000 bytes = 60000 / 1048576 approx 0.05722 MB
    assert totals["traffic_mb_wire"] == pytest.approx(60000 / (1024 * 1024), abs=1e-3)

    # Rates:
    # Success rate: 5 / 6 approx 0.8333; block rate: 1 / 6 approx 0.1667
    rates = metrics["rates"]
    assert rates["success_rate"] == pytest.approx(5 / 6, abs=1e-4)
    assert rates["block_rate"] == pytest.approx(1 / 6, abs=1e-4)
    # Wilson CI for 1 out of 6 trials:
    # center = (1/6 + 3.8416/12) / (1 + 3.8416/6) = (0.166667 + 0.320133) / 1.640267 = 0.4868 / 1.640267 = 0.29678
    # margin = 1.96 * sqrt((5/36)/6 + 3.8416/144) / 1.640267 = 1.96 * sqrt(0.023148 + 0.026678) / 1.640267
    # = 1.96 * sqrt(0.049826) / 1.640267 = 1.96 * 0.223217 / 1.640267 = 0.437506 / 1.640267 = 0.266728
    # lower = max(0, 0.29678 - 0.266728) = 0.030052
    # upper = min(1, 0.29678 + 0.266728) = 0.563508
    exp_lo, exp_hi = wilson(1, 6)
    assert rates["block_rate_ci"] == (exp_lo, exp_hi)
    assert rates["block_rate_ci"][0] == pytest.approx(0.030052, abs=1e-4)
    assert rates["block_rate_ci"][1] == pytest.approx(0.563508, abs=1e-4)

    # 2. Session Stability & Block Onset
    stability = metrics["session_stability"]
    assert stability["sessions"] == 3
    assert stability["sessions_with_block"] == 1
    assert stability["block_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert stability["block_rate_ci"] == wilson(1, 3)

    # Requests to block:
    # sess_1: 2 requests, censored (event=0)
    # sess_2: block at request 2 (event=1)
    # sess_3: 2 requests, censored (event=0)
    # KM curve at t=2: at_risk=3, d=1, c=2 -> S(2) = 1 - 1/3 = 2/3 approx 0.6667 > 0.5.
    # Survival never drops <= 0.5 -> median is None!
    assert stability["median_requests_to_first_block"] is None

    # Distinct IPs distribution:
    # sess_1: 1, sess_2: 3, sess_3: 1 -> total = [1, 3, 1]
    # mean = (1 + 3 + 1)/3 = 5/3 approx 1.67, min = 1, max = 3
    # perfectly sticky (1 IP) = 2 out of 3 = 66.7%
    dist_ips = stability["distinct_ips_distribution"]
    assert dist_ips["mean"] == pytest.approx(1.67, abs=1e-2)
    assert dist_ips["min"] == 1
    assert dist_ips["max"] == 3
    assert dist_ips["sessions_with_one_ip"] == 2
    assert dist_ips["perfectly_sticky_pct"] == pytest.approx(66.7, abs=1e-1)

    # 3. TTL Analysis
    ttl = metrics["ttl"]
    assert ttl["sessions"] == 3
    # sess_1: 0 changes, duration = 5s (censored)
    # sess_2: 2 changes, first at 5s (event)
    # sess_3: 0 changes, duration = 5s (censored)
    # Total changes = 2, mean = 2/3 = 0.67, min = 0, max = 2
    assert ttl["changes_per_session"]["total"] == 2
    assert ttl["changes_per_session"]["mean"] == pytest.approx(0.67, abs=1e-2)
    assert ttl["changes_per_session"]["min"] == 0
    assert ttl["changes_per_session"]["max"] == 2
    assert ttl["replacement_events"].get("ip_change") == 2

    # 4. Variance Analysis (same_ip vs new_ip)
    variance = metrics["variance"]["by_pair_type"]
    assert "same_ip" in variance
    assert "new_ip" in variance

    # Hand-calculate Jaccard means:
    # same_ip:
    # - "pizza nyc" (sess_1 vs sess_2): {P1,P2,P3,P4} vs {P1,P2,P3,P5} -> Jaccard = 3/5 = 0.6000
    # - "sushi nyc" (sess_1 vs sess_2): {Q1,Q2} vs {Q1,Q2} -> Jaccard = 2/2 = 1.0000
    # Mean same_ip Jaccard = (0.6000 + 1.0000) / 2 = 0.8000
    assert variance["same_ip"]["n_pairs"] == 2
    assert variance["same_ip"]["jaccard_20_mean"] == pytest.approx(0.8000, abs=1e-4)

    # new_ip:
    # - "pizza nyc":
    #     * sess_1 vs sess_3: {P1,P2,P3,P4} vs {P1,P6} -> Jaccard = 1/5 = 0.2000
    #     * sess_2 vs sess_3: {P1,P2,P3,P5} vs {P1,P6} -> Jaccard = 1/5 = 0.2000
    # - "sushi nyc":
    #     * sess_1 vs sess_3: {Q1,Q2} vs {Q3,Q4} -> Jaccard = 0.0000
    #     * sess_2 vs sess_3: {Q1,Q2} vs {Q3,Q4} -> Jaccard = 0.0000
    # Mean new_ip Jaccard = (0.2 + 0.2 + 0.0 + 0.0) / 4 = 0.4 / 4 = 0.1000
    assert variance["new_ip"]["n_pairs"] == 4
    assert variance["new_ip"]["jaccard_20_mean"] == pytest.approx(0.1000, abs=1e-4)

    # 5. Pool Comparison
    # Residential: sess_1, sess_2 -> 4 requests (3 ok, 1 block), 12 places
    # Mobile: sess_3 -> 2 requests (2 ok, 0 block), 4 places
    pools = metrics["pools"]
    assert "residential" in pools
    assert "mobile" in pools

    res_pool = pools["residential"]
    assert res_pool["sessions"] == 2
    assert res_pool["requests"] == 4
    assert res_pool["places"] == 12
    assert res_pool["success_rate"] == pytest.approx(3 / 4, abs=1e-4)
    assert res_pool["block_rate"] == pytest.approx(1 / 4, abs=1e-4)
    assert res_pool["places_per_request"] == pytest.approx(12 / 4, abs=1e-2)

    mob_pool = pools["mobile"]
    assert mob_pool["sessions"] == 1
    assert mob_pool["requests"] == 2
    assert mob_pool["places"] == 4
    assert mob_pool["success_rate"] == pytest.approx(1.0, abs=1e-4)
    assert mob_pool["block_rate"] == 0.0
    assert mob_pool["places_per_request"] == pytest.approx(4 / 2, abs=1e-2)


def test_load_rps_calculation(tmp_path: Path) -> None:
    """Verify load arm achieved RPS, block rate, and latency percentiles."""
    db_file = tmp_path / "load_test.db"
    conn = sqlite3.connect(str(db_file))
    run_id = "load_run_01"

    conn.executescript("""
    CREATE TABLE runs(run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL, status TEXT);
    CREATE TABLE sessions(run_id TEXT, session_id TEXT, arm TEXT, variant TEXT, pool TEXT, country TEXT,
                          created_at REAL, started_at REAL, ended_at REAL, requests INTEGER, PRIMARY KEY(run_id, session_id));
    CREATE TABLE requests(id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, ts REAL,
                          outcome TEXT, latency_ms REAL, variant TEXT, bytes_wire INTEGER);
    """)

    # 2 sessions under variant 'load_10_4':
    # sess_a: started at 100.0, ended at 120.0
    # sess_b: started at 105.0, ended at 125.0
    # Overall duration = max(ended_at) - min(started_at) = 125.0 - 100.0 = 25.0s
    conn.execute("INSERT INTO runs VALUES (?, 100.0, 125.0, 'completed')", (run_id,))
    conn.execute("INSERT INTO sessions VALUES (?, 's1', 'load', 'load_10_4', 'residential', 'US', 100.0, 100.0, 120.0, 50)", (run_id,))
    conn.execute("INSERT INTO sessions VALUES (?, 's2', 'load', 'load_10_4', 'residential', 'US', 105.0, 105.0, 125.0, 50)", (run_id,))

    # 100 requests: 90 ok (latency 100ms), 5 403 blocks (latency 50ms), 5 proxy_errors (latency 200ms)
    for i in range(90):
        conn.execute("INSERT INTO requests VALUES (NULL, ?, 's1', 101.0, 'ok', 100.0, 'load_10_4', 1000)", (run_id,))
    for i in range(5):
        conn.execute("INSERT INTO requests VALUES (NULL, ?, 's1', 102.0, '403', 50.0, 'load_10_4', 500)", (run_id,))
    for i in range(5):
        conn.execute("INSERT INTO requests VALUES (NULL, ?, 's2', 106.0, 'proxy_error', 200.0, 'load_10_4', 500)", (run_id,))

    conn.commit()
    conn.close()

    metrics = run_metrics(db_file, run_id)
    load = metrics["load"]["load_10_4"]

    assert load["sessions"] == 2
    assert load["requests"] == 100
    assert load["duration_s"] == 25.0
    # Hand calculation: 100 requests / 25.0 seconds = 4.00 requests per second
    assert load["achieved_rps"] == pytest.approx(4.00, abs=1e-2)
    assert load["block_rate"] == pytest.approx(5 / 100, abs=1e-4)
    assert load["proxy_error_rate"] == pytest.approx(5 / 100, abs=1e-4)


# ── 6. write_outputs, Legacy Keys, NaN/Inf and Report Verification ───────────


def test_write_outputs_legacy_keys_and_nextjs_ui(tmp_path: Path) -> None:
    """Verify write_outputs outputs all files, all 35 legacy keys + query fields for Next.js UI."""
    db_file = tmp_path / "full_out.db"
    run_id = _build_synthetic_3_session_db(db_file)
    out_dir = tmp_path / "results_test"

    outputs = write_outputs(db_file, run_id, out_dir)

    # 1. Check all required files are present
    required_files = [
        "summary.json", "analysis.json", "report.md",
        "sessions.csv", "requests.csv", "places.csv",
        "ip_observations.csv", "events.csv"
    ]
    for rf in required_files:
        assert (out_dir / rf).exists(), f"Missing output file: {rf}"

    # 2. Verify summary.json against Next.js UI expectations
    with open(out_dir / "summary.json", "r", encoding="utf-8") as f:
        summary = json.load(f)

    # All 35 legacy keys
    legacy_keys = [
        "started_at_iso", "run_id", "proxy_type", "session_strategy", "geo_targets",
        "concurrency", "elapsed_s", "total_queries", "successful_queries", "failed_queries",
        "total_places", "total_requests", "ok_requests", "consent_requests", "block_403",
        "block_429", "success_rate_pct", "consent_rate_pct", "block_rate_pct", "traffic_mb",
        "traffic_mb_wire", "traffic_tx_mb_est", "traffic_probe_est_mb", "traffic_mb_decompressed",
        "traffic_gb", "avg_latency_ms", "p95_latency_ms", "avg_query_ms", "p95_query_ms",
        "qps", "places_per_minute", "validation", "sticky_continuity", "by_country", "queries",
    ]
    for lk in legacy_keys:
        assert lk in summary, f"Missing required legacy key: {lk}"

    # Verify query fields read by Next.js UI route.ts and page.tsx:
    # q.query, q.country, q.proxy_session_id, q.places, q.total_requests, q.ok,
    # q.consent, q.block_403, q.block_429, q.avg_latency_ms, q.p95_latency_ms,
    # q.bytes_rx, q.error, q.status
    ui_query_fields = [
        "query", "country", "proxy_session_id", "places", "total_requests",
        "ok", "consent", "block_403", "block_429", "avg_latency_ms",
        "p95_latency_ms", "bytes_rx", "error", "status", "stop_reason"
    ]
    assert len(summary["queries"]) > 0
    for q in summary["queries"]:
        for fld in ui_query_fields:
            assert fld in q, f"Missing query field needed by Next.js UI: {fld}"

    # Verify by_country structure read by Next.js UI
    ui_country_fields = [
        "queries", "places", "requests", "ok", "consent", "block_403", "block_429",
        "success_rate", "consent_rate", "avg_latency_ms", "traffic_mb"
    ]
    for c_code, c_stat in summary["by_country"].items():
        for fld in ui_country_fields:
            assert fld in c_stat, f"Missing country field needed by Next.js UI: {fld}"


def test_analysis_json_no_nan_or_inf(tmp_path: Path) -> None:
    """Verify analysis.json and summary.json strictly contain no NaN, Infinity, or -Infinity."""
    db_file = tmp_path / "nan_check.db"
    run_id = _build_synthetic_3_session_db(db_file)
    out_dir = tmp_path / "nan_out"

    write_outputs(db_file, run_id, out_dir)

    def fail_on_constant(val: str) -> None:
        raise ValueError(f"Illegal non-standard JSON constant parsed: {val}")

    for json_name in ["summary.json", "analysis.json"]:
        content = (out_dir / json_name).read_text(encoding="utf-8")
        # json.loads with parse_constant will raise if NaN/Inf/-Inf is encountered
        parsed = json.loads(content, parse_constant=fail_on_constant)
        assert isinstance(parsed, dict)


def test_report_md_sections_and_absent_arms(tmp_path: Path) -> None:
    """Verify report.md contains all 10 required sections and explicit 'not run' for absent arms."""
    db_file = tmp_path / "report_check.db"
    run_id = _build_synthetic_3_session_db(db_file)
    out_dir = tmp_path / "report_out"

    write_outputs(db_file, run_id, out_dir)

    report_text = (out_dir / "report.md").read_text(encoding="utf-8")

    # 10 Required Sections
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

    # Load arm was not run in the 3-session DB -> must explicitly state 'not run'
    assert "Load test arm not run in this session" in report_text


def test_empty_database_safety(tmp_path: Path) -> None:
    """Verify write_outputs on an empty database does not crash and produces valid files."""
    db_file = tmp_path / "completely_empty.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    out_dir = tmp_path / "empty_out"
    # Must not raise
    outputs = write_outputs(db_file, "empty_run_id", out_dir)
    assert outputs["run_id"] == "empty_run_id"

    # All files must exist
    assert (out_dir / "summary.json").exists()
    assert (out_dir / "analysis.json").exists()
    assert (out_dir / "report.md").exists()

    with open(out_dir / "summary.json", "r", encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["total_queries"] == 0
    assert summary["total_requests"] == 0
    assert summary["total_places"] == 0

    with open(out_dir / "analysis.json", "r", encoding="utf-8") as f:
        analysis = json.load(f)
    assert analysis["totals"]["total_sessions"] == 0

    report_md = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "## Method" in report_md
    assert "not run" in report_md.lower()


# ── 7. Edge Cases & Reproducers for Observed Discrepancies ───────────────────


def test_reproducer_km_nan_inflates_risk_set() -> None:
    """KM risk set initialization bug:

    If durations contains NaN/Inf, line 83 skips them, but line 91 uses
    n_at_risk = len(durations), artificially inflating the initial at_risk count.
    """
    durations = [float("nan"), 10.0, 20.0]
    events = [0, 1, 1]
    curve = km(durations, events)
    # Obs: at t=10, at_risk is 3, even though only 2 valid durations exist!
    assert curve[0]["at_risk"] == 3  # Expected mathematically: 2


def test_reproducer_missing_order_by_column_crash(tmp_path: Path) -> None:
    """Missing column robustness bug:

    analyze.py:_fetch_rows lines 392-395 issues:
      SELECT * FROM sessions WHERE run_id = ? ORDER BY created_at ASC
    If the sessions table lacks created_at column, sqlite raises OperationalError.
    Spec line 158 mandates: 'Must be robust to missing columns/rows and small n.'
    """
    db_file = tmp_path / "missing_col.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    # sessions without created_at column:
    conn.execute("CREATE TABLE sessions (run_id TEXT, session_id TEXT PRIMARY KEY, started_at REAL)")
    conn.execute("INSERT INTO runs VALUES ('r1')")
    conn.execute("INSERT INTO sessions VALUES ('r1', 's1', 100.0)")
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.OperationalError, match="no such column: created_at"):
        run_metrics(db_file, "r1")


def test_reproducer_ttl_report_says_not_run_when_zero_ip_changes() -> None:
    """TTL report logic bug:

    In generate_report_md lines 1223-1229:
    if ttl.get("sessions", 0) > 0 and ttl.get("median_time_to_ip_change_s") is not None:
        ...
    else:
        lines.append("*TTL test arm not run in this session.*")

    If the TTL test arm ran with 10 sessions and had zero IP changes (perfect stickiness),
    survival never drops <= 0.5, so median_time_to_ip_change_s is None.
    The report falsely asserts '*TTL test arm not run in this session.*'.
    """
    metrics = {
        "run_id": "ttl_perfect",
        "started_at": 1000.0,
        "elapsed_s": 60.0,
        "ttl": {
            "sessions": 10,
            "median_time_to_ip_change_s": None,
            "changes_per_session": {"mean": 0.0, "min": 0, "max": 0, "total": 0},
        },
    }
    report = generate_report_md(metrics)
    assert "*TTL test arm not run in this session.*" in report
