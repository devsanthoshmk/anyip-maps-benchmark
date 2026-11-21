"""analyze.py — Statistical analysis and report generation for AnyIP gmap-bench.

Pure Python (standard library only; no scipy/lifelines/statsmodels/numpy).
Implements:
- Wilson score interval, rule of three
- Kaplan-Meier survival curve + median KM
- Bootstrap confidence interval (percentile method)
- Jaccard similarity, Jaccard@k
- Rank-Biased Overlap (RBO)
- Kendall tau rank correlation
- run_metrics(db_path, run_id) -> dict
- write_outputs(db_path, run_id, out_dir) -> dict
"""

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import sqlite3
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

# Block outcomes for analysis as specified in anyip-spec.md
BLOCK_OUTCOMES: Set[str] = {"403", "429", "consent", "sorry", "soft_block"}
MB = 1024 * 1024


# ── Statistical & Mathematical Algorithms ───────────────────────────────────


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion without continuity correction.

    k: Number of successes / events
    n: Total number of trials
    z: Normal distribution quantile (1.96 for two-sided 95% CI)

    Returns (lower, upper). If n <= 0, returns (0.0, 0.0). Clamped to [0.0, 1.0].
    """
    if n <= 0:
        return (0.0, 0.0)

    k_clamped = max(0, min(k, n))
    p_hat = k_clamped / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    margin = (z * math.sqrt((p_hat * (1.0 - p_hat)) / n + z2 / (4.0 * n * n))) / denom

    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return (round(lower, 6), round(upper, 6))


def rule_of_three(n: int) -> float:
    """Rule of three: 95% upper confidence bound on event rate when zero events
    occurred in n trials. Returns 3.0 / n if n > 0 else 1.0, clamped to [0.0, 1.0].
    """
    if n <= 0:
        return 1.0
    return min(1.0, 3.0 / n)


def km(
    durations: Sequence[float],
    events: Sequence[Union[int, bool]],
) -> List[Dict[str, Any]]:
    """Kaplan-Meier survival estimator.

    durations: Observation times.
    events: Binary indicators (1/True = event occurred, 0/False = censored).

    Returns list of dicts: [{'t': float, 'at_risk': int, 'events': int, 'survival': float}]
    """
    if not durations or len(durations) != len(events):
        return []

    valid_pairs = [
        (float(d), bool(e))
        for d, e in zip(durations, events)
        if not (math.isnan(d) or math.isinf(d))
    ]
    if not valid_pairs:
        return []

    event_counts: Dict[float, int] = defaultdict(int)
    censor_counts: Dict[float, int] = defaultdict(int)
    for d, e in valid_pairs:
        if e:
            event_counts[d] += 1
        else:
            censor_counts[d] += 1

    unique_times = sorted(set(event_counts.keys()) | set(censor_counts.keys()))
    n_at_risk = len(valid_pairs)
    survival = 1.0
    curve: List[Dict[str, Any]] = []

    for t in unique_times:
        d_i = event_counts[t]
        c_i = censor_counts[t]
        at_risk = n_at_risk

        if at_risk > 0 and d_i > 0:
            survival *= (1.0 - d_i / at_risk)

        curve.append({
            "t": float(t),
            "at_risk": int(at_risk),
            "events": int(d_i),
            "survival": round(max(0.0, min(1.0, survival)), 6),
        })

        n_at_risk -= (d_i + c_i)

    return curve


def median_km(
    km_curve_or_durations: Any,
    events: Optional[Sequence[Union[int, bool]]] = None,
) -> Optional[float]:
    """Smallest t where survival drops <= 0.5.

    Returns None if survival never drops <= 0.5 or input is empty.
    Accepts either median_km(curve) or median_km(durations, events).
    """
    if events is not None:
        curve = km(km_curve_or_durations, events)
    elif isinstance(km_curve_or_durations, list):
        if not km_curve_or_durations:
            return None
        if isinstance(km_curve_or_durations[0], dict):
            curve = km_curve_or_durations
        else:
            return None
    else:
        return None

    for pt in curve:
        if pt["survival"] <= 0.5:
            return float(pt["t"])
    return None


def _default_mean(data: Sequence[float]) -> float:
    return sum(data) / len(data) if data else 0.0


def bootstrap_ci(
    values: Sequence[float],
    stat: Optional[Callable[[Sequence[float]], float]] = None,
    n: int = 10000,
    seed: int = 42,
    alpha: float = 0.05,
) -> Tuple[float, float]:
    """Bootstrap percentile confidence interval using isolated PRNG.

    values: Input sample
    stat: Statistic function (default: mean)
    n: Number of bootstrap iterations (default: 10,000)
    seed: PRNG seed for deterministic reproducibility
    alpha: Significance level (0.05 for 95% CI)
    """
    clean = [float(v) for v in values if not (math.isnan(v) or math.isinf(v))]
    if not clean:
        return (0.0, 0.0)
    if len(clean) == 1:
        val = clean[0]
        return (val, val)
    if min(clean) == max(clean):
        val = clean[0]
        return (val, val)

    fn = stat if stat is not None else _default_mean
    rng = random.Random(seed)
    N = len(clean)

    boot_stats = [fn(rng.choices(clean, k=N)) for _ in range(n)]
    boot_stats.sort()

    low_idx = int(round((alpha / 2.0) * (n - 1)))
    high_idx = int(round((1.0 - alpha / 2.0) * (n - 1)))
    return (round(boot_stats[low_idx], 6), round(boot_stats[high_idx], 6))


def jaccard(a: Any, b: Any) -> float:
    """Jaccard similarity between two sets/sequences.

    If both empty -> 1.0 (agreement on empty result set).
    If one empty -> 0.0.
    """
    sa = set(a) if not isinstance(a, set) else a
    sb = set(b) if not isinstance(b, set) else b

    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0

    intersection = len(sa & sb)
    union = len(sa | sb)
    return intersection / union


def jaccard_at_k(a: Sequence[Any], b: Sequence[Any], k: int) -> float:
    """Jaccard similarity evaluated at rank depth k."""
    if k <= 0:
        return 1.0
    return jaccard(list(a)[:k], list(b)[:k])


def rbo(
    a: Sequence[Any],
    b: Sequence[Any],
    p: float = 0.9,
    k: Optional[int] = None,
) -> float:
    """Rank-Biased Overlap (RBO) point estimate (extrapolated).
    Webber, Moffat, Zobel (ACM TOIS 2010), Eq. (32).

    a, b: Ranked sequences of items (1st element is top rank).
    p: Persistence parameter in (0, 1). Default 0.9.
    k: Optional evaluation depth limit.
    """
    if not (0.0 < p < 1.0):
        raise ValueError("p must be in (0, 1)")

    if k is not None and k > 0:
        a = list(a)[:k]
        b = list(b)[:k]

    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    if len(a) <= len(b):
        S, L = list(a), list(b)
    else:
        S, L = list(b), list(a)

    s, l = len(S), len(L)
    seen_s: Set[Any] = set()
    seen_l: Set[Any] = set()

    sum1 = 0.0
    x_s = 0
    x_l = 0

    for d in range(1, l + 1):
        if d <= s:
            seen_s.add(S[d - 1])
        seen_l.add(L[d - 1])

        overlap = len(seen_s & seen_l)
        a_d = overlap / d
        sum1 += (p ** (d - 1)) * a_d

        if d == s:
            x_s = overlap
        if d == l:
            x_l = overlap

    sum2 = 0.0
    for d in range(s + 1, l + 1):
        sum2 += (p ** (d - 1)) * (x_s * (d - s) / (s * d))

    term1 = (1.0 - p) * (sum1 + sum2)
    term2 = (p ** l) * ((x_l - x_s) / l + x_s / s)
    res = term1 + term2
    return max(0.0, min(1.0, res))


def kendall_tau(
    rank_a: Any,
    rank_b: Any,
    variant: str = "b",
) -> float:
    """Kendall tau rank correlation on common items.

    rank_a, rank_b: Either rank dicts {item: rank_score} or ordered sequences [item, ...].
    Returns 0.0 if fewer than 2 items in common (prevents aggregation crashes).
    """
    map_a: Mapping[Any, float] = (
        rank_a if isinstance(rank_a, Mapping) else {item: i for i, item in enumerate(rank_a)}
    )
    map_b: Mapping[Any, float] = (
        rank_b if isinstance(rank_b, Mapping) else {item: i for i, item in enumerate(rank_b)}
    )

    common = [item for item in map_a if item in map_b]
    n = len(common)
    if n < 2:
        return 1.0 if map_a == map_b and n > 0 else 0.0

    p = 0
    q = 0
    t_x = 0
    t_y = 0

    for i in range(n):
        u = common[i]
        x_u, y_u = map_a[u], map_b[u]
        for j in range(i + 1, n):
            v = common[j]
            x_v, y_v = map_a[v], map_b[v]

            dx = (x_u > x_v) - (x_u < x_v)
            dy = (y_u > y_v) - (y_u < y_v)

            prod = dx * dy
            if prod > 0:
                p += 1
            elif prod < 0:
                q += 1
            elif dx == 0 and dy != 0:
                t_x += 1
            elif dx != 0 and dy == 0:
                t_y += 1

    if variant == "a":
        denom = n * (n - 1) / 2.0
        return (p - q) / denom if denom > 0 else 0.0
    else:
        # tau-b
        denom = math.sqrt((p + q + t_x) * (p + q + t_y))
        return (p - q) / denom if denom > 0 else 0.0


def percentile(values: Sequence[float], p: float) -> float:
    """Compute p-th percentile (0 <= p <= 100) using linear interpolation."""
    clean = sorted([float(x) for x in values if not (math.isnan(x) or math.isinf(x))])
    if not clean:
        return 0.0
    if len(clean) == 1:
        return clean[0]
    p_clamped = max(0.0, min(100.0, p))
    k = (len(clean) - 1) * (p_clamped / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return clean[int(k)]
    d0 = clean[int(f)] * (c - k)
    d1 = clean[int(c)] * (k - f)
    return d0 + d1


def safe_float(val: Any, default: float = 0.0) -> float:
    """Convert value to float, ensuring it is finite (never NaN or Inf)."""
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def sanitize_floats(obj: Any) -> Any:
    """Recursively convert NaNs and Infs to finite numbers for valid JSON output."""
    if isinstance(obj, float):
        return safe_float(obj)
    if isinstance(obj, dict):
        return {k: sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_floats(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_floats(v) for v in obj)
    return obj


# ── Database Extraction & Metric Calculations ────────────────────────────────


def _open_db(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _get_existing_tables(conn: sqlite3.Connection) -> Set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in cur.fetchall()}


def _get_table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in cur.fetchall()}


def _fetch_rows(
    conn: sqlite3.Connection,
    table: str,
    run_id: str,
    existing_tables: Set[str],
    order_by: str = "",
) -> List[Dict[str, Any]]:
    if table not in existing_tables:
        return []
    columns = _get_table_columns(conn, table)
    where_clause = ""
    params: List[Any] = []
    if "run_id" in columns:
        where_clause = " WHERE run_id = ?"
        params.append(run_id)

    order_clause = ""
    if order_by:
        # Check if the primary sort column exists in this table
        sort_col = order_by.split()[0].strip()
        if sort_col in columns:
            order_clause = f" ORDER BY {order_by}"

    sql = f"SELECT * FROM {table}{where_clause}{order_clause}"
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []


def run_metrics(db_path: Union[str, Path], run_id: str) -> Dict[str, Any]:
    """Extract and calculate comprehensive benchmark metrics for run_id.

    Robust to missing columns, missing tables, or empty data. Never returns NaN or Inf.
    """
    conn = _open_db(db_path)
    try:
        tables = _get_existing_tables(conn)

        # 1. Fetch records
        run_rows = _fetch_rows(conn, "runs", run_id, tables)
        session_rows = _fetch_rows(conn, "sessions", run_id, tables, order_by="created_at ASC")
        request_rows = _fetch_rows(conn, "requests", run_id, tables, order_by="ts ASC")
        place_rows = _fetch_rows(conn, "places", run_id, tables, order_by="id ASC")
        ip_obs_rows = _fetch_rows(conn, "ip_observations", run_id, tables, order_by="ts ASC")
        event_rows = _fetch_rows(conn, "events", run_id, tables, order_by="ts ASC")

        run_info = run_rows[0] if run_rows else {}
        try:
            run_config = json.loads(run_info.get("config_json") or "{}")
            arm_kinds = {a["name"]: a["kind"] for a in run_config.get("arms", [])}
        except (ValueError, TypeError, KeyError, AttributeError):
            run_config = {}
            arm_kinds = {}
        price = run_config.get("price_usd_per_gb")
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
            price = None

        # 2. Timing & Execution Boundaries
        started_at = safe_float(run_info.get("started_at"))
        finished_at = safe_float(run_info.get("finished_at"))
        all_ts = [
            safe_float(r.get("ts")) for r in request_rows if r.get("ts")
        ] + [
            safe_float(s.get("started_at")) for s in session_rows if s.get("started_at")
        ]
        if not started_at and all_ts:
            started_at = min(all_ts)
        if not finished_at and all_ts:
            finished_at = max(all_ts)
        elapsed_s = max(0.0, finished_at - started_at) if (started_at and finished_at) else 0.0

        # 3. Requests and Outcomes
        total_requests_all = len(request_rows)
        final_requests = [r for r in request_rows if r.get("is_final") in (1, None)]
        total_final_requests = len(final_requests)
        # Evaluate all requests so block/proxy/network counts reconcile with total requests
        eval_requests = request_rows

        outcome_counts: Dict[str, int] = defaultdict(int)
        for r in eval_requests:
            outcome = r.get("outcome") or "other_error"
            outcome_counts[outcome] += 1

        ok_requests = outcome_counts["ok"]
        consent_requests = outcome_counts["consent"]
        block_403 = outcome_counts["403"]
        block_429 = outcome_counts["429"]
        sorry_requests = outcome_counts["sorry"]
        soft_block_requests = outcome_counts["soft_block"]
        google_5xx = outcome_counts["google_5xx"]
        proxy_errors = outcome_counts["proxy_error"]
        other_errors = outcome_counts["other_error"]
        network_errors = outcome_counts["network_error"]

        total_blocks = block_403 + block_429 + consent_requests + sorry_requests + soft_block_requests
        n_req = len(eval_requests)

        # Rates & Wilson CIs
        block_rate = total_blocks / n_req if n_req else 0.0
        block_rate_ci = wilson(total_blocks, n_req)
        success_rate = ok_requests / n_req if n_req else 0.0
        success_rate_ci = wilson(ok_requests, n_req)
        consent_rate = consent_requests / n_req if n_req else 0.0
        consent_rate_ci = wilson(consent_requests, n_req)
        proxy_error_rate = proxy_errors / n_req if n_req else 0.0
        proxy_error_rate_ci = wilson(proxy_errors, n_req)

        # Traffic aggregation (all wire bytes, including retries)
        bytes_wire_sum = sum(int(r.get("bytes_wire") or 0) for r in request_rows)
        bytes_rx_sum = sum(int(r.get("bytes_rx") or 0) for r in request_rows)
        # Estimate probe bytes from IP observations if not in requests
        bytes_probe_sum = len(ip_obs_rows) * 1024  # standard ~1KB per exit IP probe
        bytes_tx_sum = sum(int(r.get("bytes_tx_est") or 500) for r in request_rows)

        traffic_mb_wire = bytes_wire_sum / MB
        traffic_mb_decompressed = bytes_rx_sum / MB
        traffic_tx_mb_est = bytes_tx_sum / MB
        traffic_probe_est_mb = bytes_probe_sum / MB
        traffic_mb = traffic_mb_wire + traffic_tx_mb_est + traffic_probe_est_mb
        traffic_gb = traffic_mb / 1024.0

        # Latencies grouped by outcome
        latency_by_outcome: Dict[str, Dict[str, Any]] = {}
        outcomes_list = sorted(set(r.get("outcome") or "other_error" for r in eval_requests))
        for outc in ["all", "ok"] + outcomes_list:
            if outc == "all":
                lats = [safe_float(r.get("latency_ms")) for r in eval_requests if safe_float(r.get("latency_ms")) > 0]
            elif outc == "ok":
                lats = [safe_float(r.get("latency_ms")) for r in eval_requests if r.get("outcome") == "ok" and safe_float(r.get("latency_ms")) > 0]
            else:
                lats = [safe_float(r.get("latency_ms")) for r in eval_requests if r.get("outcome") == outc and safe_float(r.get("latency_ms")) > 0]

            if lats:
                latency_by_outcome[outc] = {
                    "n": len(lats),
                    "mean_ms": round(sum(lats) / len(lats), 1),
                    "p50_ms": round(percentile(lats, 50), 1),
                    "p90_ms": round(percentile(lats, 90), 1),
                    "p95_ms": round(percentile(lats, 95), 1),
                    "p99_ms": round(percentile(lats, 99), 1),
                }
            else:
                latency_by_outcome[outc] = {
                    "n": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p90_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0
                }

        avg_latency_ms = latency_by_outcome["ok"]["mean_ms"] if latency_by_outcome["ok"]["n"] else latency_by_outcome["all"]["mean_ms"]
        p95_latency_ms = latency_by_outcome["ok"]["p95_ms"] if latency_by_outcome["ok"]["n"] else latency_by_outcome["all"]["p95_ms"]

        # 4. Queries Extraction & Status
        # Keep repeated executions of the same query separate.
        queries_dict: Dict[tuple, Dict[str, Any]] = {}
        execution_indices: Dict[tuple, set] = defaultdict(set)
        for r in request_rows:
            q_text = r.get("query") or ""
            c_text = r.get("country") or "US"
            s_id = r.get("session_id") or ""
            base = (q_text, c_text, s_id)
            key = (*base, r.get("query_index"))
            execution_indices[base].add(r.get("query_index"))
            if key not in queries_dict:
                queries_dict[key] = {
                    "query": q_text,
                    "country": c_text,
                    "session": s_id,
                    "requests": [],
                    "places": [],
                }
            queries_dict[key]["requests"].append(r)

        for p in place_rows:
            q_text = p.get("query") or ""
            c_text = p.get("country") or "US"
            s_id = p.get("session_id") or ""
            base = (q_text, c_text, s_id)
            index = p.get("query_index")
            if index is None and len(execution_indices[base]) == 1:
                index = next(iter(execution_indices[base]))
            if index is None and len(execution_indices[base]) > 1:
                continue  # Historical rows cannot be assigned to a repeat reliably.
            key = (*base, index)
            if key in queries_dict:
                queries_dict[key]["places"].append(p)
            else:
                queries_dict[key] = {
                    "query": q_text,
                    "country": c_text,
                    "session": s_id,
                    "requests": [],
                    "places": [p],
                }

        # IP observations map per session
        obs_by_sess: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for obs in ip_obs_rows:
            obs_by_sess[obs.get("session_id") or ""].append(obs)

        # Build session IP summaries (primary IP, primary family, distinct counts)
        session_ip_summaries: Dict[str, Dict[str, Any]] = {}
        for s in session_rows:
            s_id = s.get("session_id") or ""
            session_ip_summaries[s_id] = get_session_ip_summary(obs_by_sess.get(s_id, []))

        # Build query items and compute query-level status
        query_items: List[Dict[str, Any]] = []
        query_status_counts: Dict[str, int] = defaultdict(int)
        query_durations: List[float] = []

        for (q_text, c_text, s_id, _query_index), q_data in queries_dict.items():
            reqs = q_data["requests"]
            places = q_data["places"]
            n_places = len(places)
            n_req_q = len(reqs)

            # Determine stop reason & block position
            stop_reason = "limit"
            block_at_req: Optional[int] = None
            block_at_page: Optional[int] = None
            has_block = False
            has_error = False

            # In the query-status loop, only evaluate terminal requests: is_final in (1, None)
            terminal_reqs = [r for r in reqs if r.get("is_final") in (1, None)]
            eval_q_reqs = terminal_reqs if terminal_reqs else reqs

            for req in eval_q_reqs:
                outc = req.get("outcome") or ""
                if outc in BLOCK_OUTCOMES:
                    has_block = True
                    if block_at_req is None:
                        block_at_req = req.get("request_index") or 1
                        block_at_page = (req.get("page_start") or 0) // 20 + 1
                    stop_reason = "block"
                    break
                elif outc in {"network_error", "proxy_error", "google_5xx", "other_error"}:
                    has_error = True
                    stop_reason = "network" if outc == "network_error" else ("proxy_error" if outc == "proxy_error" else outc)

            # Query Result Status according to spec rules (lines 32-38)
            if not has_block and not has_error:
                status = "success" if n_places > 0 else "empty"
            elif n_places > 0:
                status = "partial"
            elif has_block:
                status = "blocked"
            else:
                status = "error"

            query_status_counts[status] += 1

            # Latency and timing
            req_lats = [safe_float(r.get("latency_ms")) for r in reqs if safe_float(r.get("latency_ms")) > 0]
            avg_q_lat = sum(req_lats) / len(req_lats) if req_lats else 0.0
            p95_q_lat = percentile(req_lats, 95) if req_lats else 0.0

            q_ts_list = [safe_float(r.get("ts")) for r in reqs if safe_float(r.get("ts")) > 0]
            query_wall_ms = (max(q_ts_list) - min(q_ts_list)) * 1000.0 if len(q_ts_list) > 1 else (sum(req_lats) or 0.0)
            if query_wall_ms > 0:
                query_durations.append(query_wall_ms)

            # Query start and end primary IP extraction
            s_obs = obs_by_sess.get(s_id, [])
            if q_ts_list and s_obs:
                min_q_ts = min(q_ts_list)
                max_q_ts = max(q_ts_list)
                cand_start = [o for o in s_obs if safe_float(o.get("ts")) < min_q_ts]
                if cand_start:
                    max_start_ts = max(safe_float(o.get("ts")) for o in cand_start)
                    start_probes = [o for o in cand_start if abs(safe_float(o.get("ts")) - max_start_ts) < 0.1]
                else:
                    start_probes = []

                cand_end = [o for o in s_obs if safe_float(o.get("ts")) > max_q_ts]
                if cand_end:
                    min_end_ts = min(safe_float(o.get("ts")) for o in cand_end)
                    end_probes = [o for o in cand_end if abs(safe_float(o.get("ts")) - min_end_ts) < 0.1]
                else:
                    end_probes = []
            else:
                start_probes = end_probes = []

            start_ip, start_fam = get_primary_ip(start_probes)
            end_ip, end_fam = get_primary_ip(end_probes)

            exit_ip_start = start_ip if start_ip else None
            exit_ip_end = end_ip if end_ip else None

            if exit_ip_start and exit_ip_end and start_fam == end_fam:
                ip_stable = (exit_ip_start == exit_ip_end)
            else:
                ip_stable = None

            q_bytes_wire = sum(int(r.get("bytes_wire") or 0) for r in reqs)
            q_bytes_rx = sum(int(r.get("bytes_rx") or 0) for r in reqs)

            query_items.append({
                "query": q_text,
                "country": c_text,
                "session": s_id,
                "proxy_session_id": s_id,
                "proxy_type": "residential",
                "status": status,
                "places": n_places,
                "requests": n_req_q,
                "total_requests": n_req_q,
                "ok": sum(1 for r in reqs if r.get("outcome") == "ok"),
                "consent": sum(1 for r in reqs if r.get("outcome") == "consent"),
                "block_403": sum(1 for r in reqs if r.get("outcome") == "403"),
                "block_429": sum(1 for r in reqs if r.get("outcome") == "429"),
                "sorry": sum(1 for r in reqs if r.get("outcome") == "sorry"),
                "soft_block": sum(1 for r in reqs if r.get("outcome") == "soft_block"),
                "proxy_errors": sum(1 for r in reqs if r.get("outcome") == "proxy_error"),
                "block_at_request": block_at_req,
                "block_at_page": block_at_page,
                "stop_reason": stop_reason,
                "partial": status == "partial",
                "exit_ip_start": exit_ip_start,
                "exit_ip_end": exit_ip_end,
                "ip_stable": ip_stable,
                "observed_ips": sorted(set(o.get("ip") for o in s_obs if o.get("ip"))),
                "avg_latency_ms": round(avg_q_lat, 1),
                "p95_latency_ms": round(p95_q_lat, 1),
                "query_wall_ms": round(query_wall_ms, 1),
                "bytes_rx": q_bytes_rx,
                "bytes_wire": q_bytes_wire,
                "bytes_tx_est": int(n_req_q * 500),
                "bytes_probe_est": 2048 if s_obs else 0,
                "places_flagged": 0,
                "error": None if status in ("success", "empty") else stop_reason,
            })

        total_queries = len(query_items)
        successful_queries = query_status_counts["success"]
        failed_queries = total_queries - successful_queries
        total_places = len(place_rows)
        validation_flags = [set((p.get("validation") or "").split(",")) for p in place_rows]
        valid_places = sum(flags == {"ok"} for flags in validation_flags)
        unvalidated_places = sum(flags == {""} for flags in validation_flags)
        validated_places = total_places - unvalidated_places

        avg_query_ms = sum(query_durations) / len(query_durations) if query_durations else 0.0
        p95_query_ms = percentile(query_durations, 95) if query_durations else 0.0
        qps = (total_queries / elapsed_s) if elapsed_s > 0 else 0.0
        places_per_minute = (total_places / (elapsed_s / 60.0)) if elapsed_s > 0 else 0.0

        # 5. Session Stability & Block Onset
        total_sessions = len(session_rows)
        sessions_with_block = 0
        block_cohort_sessions = 0
        km_durations: List[float] = []
        km_events: List[int] = []
        first_block_reqs: List[float] = []
        distinct_ip_counts: List[int] = []
        v4_counts: List[int] = []
        v6_counts: List[int] = []

        for s in session_rows:
            s_id = s.get("session_id") or ""
            # Block detection
            b_events = int(s.get("block_events") or 0)
            fb_req = s.get("first_block_request")
            req_count = int(s.get("google_requests") or 0)
            in_cohort = req_count > 0 and (not arm_kinds or arm_kinds.get(s.get("arm")) == "block_onset")
            block_cohort_sessions += int(in_cohort)

            if in_cohort and fb_req is not None:
                sessions_with_block += 1
                event_req = float(fb_req) if fb_req else 1.0
                km_durations.append(event_req)
                km_events.append(1)
                first_block_reqs.append(event_req)
            elif in_cohort:
                km_durations.append(float(req_count))
                km_events.append(0)  # censored

            # Distinct IPs based on primary family
            summary = session_ip_summaries.get(s_id, {})
            dist_ips = summary.get("distinct_primary_ips", 0)
            if dist_ips > 0:
                distinct_ip_counts.append(dist_ips)
            v4_counts.append(summary.get("distinct_v4", 0))
            v6_counts.append(summary.get("distinct_v6", 0))

        sess_block_rate = (sessions_with_block / block_cohort_sessions) if block_cohort_sessions else 0.0
        sess_block_ci = wilson(sessions_with_block, block_cohort_sessions)

        km_curve = km(km_durations, km_events)
        med_req_to_block = median_km(km_curve)

        first_block_dist: Dict[str, Any] = {
            "count": len(first_block_reqs),
            "mean": round(sum(first_block_reqs) / len(first_block_reqs), 1) if first_block_reqs else 0.0,
            "min": min(first_block_reqs) if first_block_reqs else 0.0,
            "p25": round(percentile(first_block_reqs, 25), 1),
            "median": round(percentile(first_block_reqs, 50), 1),
            "p75": round(percentile(first_block_reqs, 75), 1),
            "p95": round(percentile(first_block_reqs, 95), 1),
            "max": max(first_block_reqs) if first_block_reqs else 0.0,
            "buckets": {
                "1": sum(1 for x in first_block_reqs if x == 1),
                "2_5": sum(1 for x in first_block_reqs if 2 <= x <= 5),
                "6_10": sum(1 for x in first_block_reqs if 6 <= x <= 10),
                "11_20": sum(1 for x in first_block_reqs if 11 <= x <= 20),
                "21_50": sum(1 for x in first_block_reqs if 21 <= x <= 50),
                "51+": sum(1 for x in first_block_reqs if x > 50),
            },
        }

        distinct_ips_dist: Dict[str, Any] = {
            "mean": round(sum(distinct_ip_counts) / len(distinct_ip_counts), 2) if distinct_ip_counts else 0.0,
            "min": min(distinct_ip_counts) if distinct_ip_counts else 0,
            "max": max(distinct_ip_counts) if distinct_ip_counts else 0,
            "sessions_with_one_ip": distinct_ip_counts.count(1),
            "sessions_without_witness": total_sessions - len(distinct_ip_counts),
            "perfectly_sticky_pct": round(
                (distinct_ip_counts.count(1) / len(distinct_ip_counts) * 100.0), 1
            ) if distinct_ip_counts else None,
            "distinct_v4": {
                "mean": round(sum(v4_counts) / len(v4_counts), 2) if v4_counts else 0.0,
                "min": min(v4_counts) if v4_counts else 0,
                "max": max(v4_counts) if v4_counts else 0,
            },
            "distinct_v6": {
                "mean": round(sum(v6_counts) / len(v6_counts), 2) if v6_counts else 0.0,
                "min": min(v6_counts) if v6_counts else 0,
                "max": max(v6_counts) if v6_counts else 0,
            },
        }

        # 6. TTL Analysis (Exit IP Stability)
        ttl_sessions = [
            s for s in session_rows
            if s.get("arm") in ("ttl", "ttl_pilot") or "ttl" in (s.get("arm") or "").lower()
        ]
        if not ttl_sessions and not any(s.get("arm") for s in session_rows) and ip_obs_rows:
            ttl_sessions = session_rows

        ttl_deltas: List[float] = []
        ttl_events: List[int] = []
        changes_per_session: List[int] = []
        sessions_with_change = 0
        session_change_map: Dict[str, int] = {}

        for s in ttl_sessions:
            s_id = s.get("session_id") or ""
            obs_list = obs_by_sess.get(s_id, [])
            if not obs_list:
                continue

            init_ip, prim_fam = get_primary_ip(obs_list)
            if not init_ip:
                continue

            # Keep only observations matching primary family
            fam_obs = [
                o for o in obs_list
                if o.get("ip") and (":" not in o.get("ip") if prim_fam == "v4" else ":" in o.get("ip"))
            ]
            if not fam_obs:
                continue

            # Group fam_obs into probe events (probes within 0.5s are same probe event)
            probe_batches: List[List[Dict[str, Any]]] = []
            for o in fam_obs:
                if not probe_batches or (safe_float(o.get("ts")) - safe_float(probe_batches[-1][-1].get("ts"))) > 0.5:
                    probe_batches.append([o])
                else:
                    probe_batches[-1].append(o)

            probe_points: List[Tuple[float, str]] = []
            for B in probe_batches:
                p_ip, _ = get_primary_ip(B)
                if p_ip:
                    probe_points.append((safe_float(B[0].get("ts")), p_ip))

            if not probe_points:
                continue

            t0, ip0 = probe_points[0]
            prev_ip = ip0
            changed = False
            session_changes = 0

            for t_k, ip_k in probe_points[1:]:
                if ip_k != prev_ip:
                    session_changes += 1
                    if not changed:
                        ttl_deltas.append(max(0.1, t_k - t0))
                        ttl_events.append(1)
                        changed = True
                    prev_ip = ip_k

            session_change_map[s_id] = session_changes
            changes_per_session.append(session_changes)
            if session_changes > 0:
                sessions_with_change += 1

            if not changed:
                duration = max(0.1, probe_points[-1][0] - t0)
                ended_at = safe_float(s.get("ended_at"))
                if ended_at > t0:
                    duration = max(duration, ended_at - t0)
                ttl_deltas.append(duration)
                ttl_events.append(0)  # censored

        ttl_km_curve = km(ttl_deltas, ttl_events)
        median_time_to_ip_change = median_km(ttl_km_curve)

        # Replacement events derived only from primary-family change detection
        replacement_counts: Dict[str, int] = defaultdict(int)
        for s in ttl_sessions:
            s_id = s.get("session_id") or ""
            sess_changes = session_change_map.get(s_id, 0)
            if sess_changes > 0:
                sess_events = [
                    ev for ev in event_rows
                    if ev.get("session_id") == s_id and ev.get("event_type") in ("ttl_expiry", "ip_change")
                ]
                if sess_events:
                    for ev in sess_events[:sess_changes]:
                        replacement_counts[ev.get("event_type") or "ip_change"] += 1
                else:
                    replacement_counts["ip_change"] += sess_changes

        ttl_metrics = {
            "sessions": len(ttl_sessions),
            "sessions_with_change": sessions_with_change,
            "observed_ip_changes": sum(changes_per_session),
            "median_time_to_ip_change_s": median_time_to_ip_change,
            "km_curve": ttl_km_curve[:15],
            "changes_per_session": {
                "mean": round(sum(changes_per_session) / len(changes_per_session), 2) if changes_per_session else 0.0,
                "min": min(changes_per_session) if changes_per_session else 0,
                "max": max(changes_per_session) if changes_per_session else 0,
                "total": sum(changes_per_session),
            },
            "replacement_events": dict(replacement_counts),
        }

        # 7. Arm Breakdown
        arm_groups: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "sessions": 0, "requests": 0, "places": 0, "ok": 0, "consent": 0,
            "403": 0, "429": 0, "sorry": 0, "soft_block": 0, "proxy_error": 0,
            "latencies": [],
        })

        for s in session_rows:
            arm_name = s.get("arm") or "unspecified"
            arm_groups[arm_name]["sessions"] += 1
            arm_groups[arm_name]["places"] += int(s.get("places") or 0)

        for r in eval_requests:
            arm_name = r.get("arm") or "unspecified"
            outc = r.get("outcome") or "other_error"
            arm_groups[arm_name]["requests"] += 1
            if outc == "ok":
                arm_groups[arm_name]["ok"] += 1
            elif outc == "consent":
                arm_groups[arm_name]["consent"] += 1
            elif outc == "403":
                arm_groups[arm_name]["403"] += 1
            elif outc == "429":
                arm_groups[arm_name]["429"] += 1
            elif outc == "sorry":
                arm_groups[arm_name]["sorry"] += 1
            elif outc == "soft_block":
                arm_groups[arm_name]["soft_block"] += 1
            elif outc == "proxy_error":
                arm_groups[arm_name]["proxy_error"] += 1

            lat = safe_float(r.get("latency_ms"))
            if lat > 0:
                arm_groups[arm_name]["latencies"].append(lat)

        arm_breakdown: Dict[str, Any] = {}
        for arm_name, data in arm_groups.items():
            req_cnt = data["requests"]
            blk_cnt = data["403"] + data["429"] + data["consent"] + data["sorry"] + data["soft_block"]
            lats = data["latencies"]
            arm_breakdown[arm_name] = {
                "sessions": data["sessions"],
                "requests": req_cnt,
                "places": data["places"],
                "ok": data["ok"],
                "block_rate": round(blk_cnt / req_cnt, 4) if req_cnt else 0.0,
                "block_rate_ci": wilson(blk_cnt, req_cnt),
                "success_rate": round(data["ok"] / req_cnt, 4) if req_cnt else 0.0,
                "success_rate_ci": wilson(data["ok"], req_cnt),
                "proxy_error_rate": round(data["proxy_error"] / req_cnt, 4) if req_cnt else 0.0,
                "proxy_error_rate_ci": wilson(data["proxy_error"], req_cnt),
                "latency_p50_ms": round(percentile(lats, 50), 1),
                "latency_p95_ms": round(percentile(lats, 95), 1),
            }

        # 8. Variance Analysis (Rank and Overlap Metrics Across Repeats)
        ALLOWED_VARIANTS = {"same_ip", "new_ip", "local", "forced_rotate"}

        # Only include sessions whose variant is in ALLOWED_VARIANTS and belong to a variance arm
        variance_sessions = [
            s for s in session_rows
            if (s.get("variant") or "") in ALLOWED_VARIANTS
            and not any(x in (s.get("arm") or "").lower() for x in ("ttl", "block", "onset", "load", "pool"))
        ]
        variance_sess_ids = {s.get("session_id") for s in variance_sessions if s.get("session_id")}
        session_arm_map = {s.get("session_id"): (s.get("arm") or "") for s in variance_sessions}
        session_variant_map = {s.get("session_id"): (s.get("variant") or "") for s in variance_sessions}

        # Track session count per variant
        sessions_by_variant: Dict[str, Set[str]] = defaultdict(set)
        for s in variance_sessions:
            s_id = s.get("session_id")
            v = s.get("variant")
            if s_id and v:
                sessions_by_variant[v].add(s_id)

        places_by_qs: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for p in place_rows:
            s_id = p.get("session_id") or ""
            if s_id in variance_sess_ids:
                q_text = p.get("query") or ""
                pid = p.get("place_id") or p.get("title") or ""
                if pid:
                    places_by_qs[(q_text, s_id)].append(pid)

        queries_with_places = sorted(set(q for q, _ in places_by_qs.keys()))
        pair_metrics_by_type: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: {
            "jaccard_20": [], "jaccard_40": [], "rbo": [], "kendall_tau": [], "count_delta": []
        })

        for q in queries_with_places:
            for p_type in sorted(sessions_by_variant.keys()):
                sess_for_qv = [
                    s_id for (q_k, s_id) in places_by_qs.keys()
                    if q_k == q and session_variant_map.get(s_id) == p_type
                ]
                sess_sorted = sorted(set(sess_for_qv))
                for i in range(len(sess_sorted)):
                    for j in range(i + 1, len(sess_sorted)):
                        s1, s2 = sess_sorted[i], sess_sorted[j]
                        if session_arm_map.get(s1) != session_arm_map.get(s2):
                            continue
                        if arm_kinds and p_type in {"same_ip", "new_ip"}:
                            one = session_ip_summaries.get(s1, {})
                            two = session_ip_summaries.get(s2, {})
                            families = [
                                (one.get(family, []), two.get(family, []))
                                for family in ("v4_ips", "v6_ips")
                            ]
                            common = [(a, b) for a, b in families if a and b]
                            if not common or not all(
                                len(a) == len(b) == 1 and (a == b) == (p_type == "same_ip")
                                for a, b in common
                            ):
                                continue
                        list1 = places_by_qs[(q, s1)]
                        list2 = places_by_qs[(q, s2)]

                        j20 = jaccard_at_k(list1, list2, 20)
                        j40 = jaccard_at_k(list1, list2, 40)
                        rbo_val = rbo(list1, list2, p=0.9)
                        tau_val = kendall_tau(list1, list2)
                        c_delta = abs(len(list1) - len(list2))

                        pair_metrics_by_type[p_type]["jaccard_20"].append(j20)
                        pair_metrics_by_type[p_type]["jaccard_40"].append(j40)
                        pair_metrics_by_type[p_type]["rbo"].append(rbo_val)
                        pair_metrics_by_type[p_type]["kendall_tau"].append(tau_val)
                        pair_metrics_by_type[p_type]["count_delta"].append(float(c_delta))

        variance_summary: Dict[str, Any] = {}
        outputs_by_session = defaultdict(list)
        for item in query_items:
            outputs_by_session[item["session"]].append(item)
        for p_type in sorted(sessions_by_variant.keys()):
            n_sess = len(sessions_by_variant[p_type])
            metric_dict = pair_metrics_by_type.get(p_type, {
                "jaccard_20": [], "jaccard_40": [], "rbo": [], "kendall_tau": [], "count_delta": []
            })
            n_pairs = len(metric_dict["jaccard_20"])
            if n_pairs > 0:
                variance_summary[p_type] = {
                    "n_sessions": n_sess,
                    "n_pairs": n_pairs,
                    "jaccard_20_mean": round(_default_mean(metric_dict["jaccard_20"]), 4),
                    "jaccard_20_ci": bootstrap_ci(metric_dict["jaccard_20"]),
                    "jaccard_40_mean": round(_default_mean(metric_dict["jaccard_40"]), 4),
                    "jaccard_40_ci": bootstrap_ci(metric_dict["jaccard_40"]),
                    "rbo_mean": round(_default_mean(metric_dict["rbo"]), 4),
                    "rbo_ci": bootstrap_ci(metric_dict["rbo"]),
                    "kendall_tau_mean": round(_default_mean(metric_dict["kendall_tau"]), 4),
                    "kendall_tau_ci": bootstrap_ci(metric_dict["kendall_tau"]),
                    "count_delta_mean": round(_default_mean(metric_dict["count_delta"]), 2),
                }
            else:
                variance_summary[p_type] = {
                    "n_sessions": n_sess,
                    "n_pairs": 0,
                    "jaccard_20_mean": None,
                    "jaccard_20_ci": None,
                    "jaccard_40_mean": None,
                    "jaccard_40_ci": None,
                    "rbo_mean": None,
                    "rbo_ci": None,
                    "kendall_tau_mean": None,
                    "kendall_tau_ci": None,
                    "count_delta_mean": None,
                }
            repeats = [outputs_by_session[s] for s in sessions_by_variant[p_type]]
            variance_summary[p_type]["empty_repeats"] = sum(any(q["status"] == "empty" for q in items) for items in repeats)
            variance_summary[p_type]["failed_or_incomplete_repeats"] = sum(
                not items or any(q["status"] not in {"success", "empty"} for q in items) for items in repeats
            )

        # 9. Load Analysis
        load_variants = sorted(set(s.get("variant") for s in session_rows if s.get("variant") and "load" in s.get("variant")))
        load_summary: Dict[str, Any] = {}
        for l_var in load_variants:
            l_sessions = [s for s in session_rows if s.get("variant") == l_var]
            l_reqs = [r for r in eval_requests if r.get("variant") == l_var]

            req_count = len(l_reqs)
            blk_count = sum(1 for r in l_reqs if r.get("outcome") in BLOCK_OUTCOMES)
            proxy_count = sum(1 for r in l_reqs if r.get("outcome") == "proxy_error")
            lats = [safe_float(r.get("latency_ms")) for r in l_reqs if safe_float(r.get("latency_ms")) > 0]

            s_times = [safe_float(s.get("started_at")) for s in l_sessions if safe_float(s.get("started_at")) > 0]
            e_times = [safe_float(s.get("ended_at")) for s in l_sessions if safe_float(s.get("ended_at")) > 0]
            dur_s = max(1.0, max(e_times) - min(s_times)) if (s_times and e_times) else 1.0

            achieved_rps = req_count / dur_s

            load_summary[l_var] = {
                "sessions": len(l_sessions),
                "requests": req_count,
                "duration_s": round(dur_s, 2),
                "achieved_rps": round(achieved_rps, 2),
                "block_rate": round(blk_count / req_count, 4) if req_count else 0.0,
                "block_rate_ci": wilson(blk_count, req_count),
                "proxy_error_rate": round(proxy_count / req_count, 4) if req_count else 0.0,
                "latency_p50_ms": round(percentile(lats, 50), 1),
                "latency_p95_ms": round(percentile(lats, 95), 1),
            }

        # 10. Pool Comparison
        pool_sessions = [s for s in session_rows if not arm_kinds or arm_kinds.get(s.get("arm")) == "pool_compare"]
        pools_list = sorted({s["pool"] for s in pool_sessions if s.get("pool")})
        pool_summary: Dict[str, Any] = {}

        # ASN extraction per session
        asn_by_sess: Dict[str, List[str]] = defaultdict(list)
        for obs in ip_obs_rows:
            s_id = obs.get("session_id") or ""
            asn = obs.get("asn") or ""
            if asn and asn not in asn_by_sess[s_id]:
                asn_by_sess[s_id].append(asn)

        for p_name in pools_list:
            p_sessions = [s for s in pool_sessions if s.get("pool") == p_name]
            p_reqs = [r for r in eval_requests if any(s.get("session_id") == r.get("session_id") for s in p_sessions)]
            p_places = [p for p in place_rows if any(s.get("session_id") == p.get("session_id") for s in p_sessions)]

            p_req_count = len(p_reqs)
            p_ok = sum(1 for r in p_reqs if r.get("outcome") == "ok")
            p_blk = sum(1 for r in p_reqs if r.get("outcome") in BLOCK_OUTCOMES)
            p_consent = sum(1 for r in p_reqs if r.get("outcome") == "consent")
            p_proxy = sum(1 for r in p_reqs if r.get("outcome") == "proxy_error")
            p_lats = [safe_float(r.get("latency_ms")) for r in p_reqs if safe_float(r.get("latency_ms")) > 0]

            p_wire_bytes = sum(int(r.get("bytes_wire") or 0) for r in p_reqs)
            p_wire_mb = p_wire_bytes / MB

            # ASN mix
            asn_counts: Dict[str, int] = defaultdict(int)
            for s in p_sessions:
                for a in asn_by_sess.get(s.get("session_id") or "", []):
                    asn_counts[a] += 1

            pool_summary[p_name] = {
                "sessions": len(p_sessions),
                "requests": p_req_count,
                "places": len(p_places),
                "success_rate": round(p_ok / p_req_count, 4) if p_req_count else 0.0,
                "success_rate_ci": wilson(p_ok, p_req_count),
                "block_rate": round(p_blk / p_req_count, 4) if p_req_count else 0.0,
                "block_rate_ci": wilson(p_blk, p_req_count),
                "consent_rate": round(p_consent / p_req_count, 4) if p_req_count else 0.0,
                "consent_rate_ci": wilson(p_consent, p_req_count),
                "proxy_error_rate": round(p_proxy / p_req_count, 4) if p_req_count else 0.0,
                "proxy_error_rate_ci": wilson(p_proxy, p_req_count),
                "latency_p50_ms": round(percentile(p_lats, 50), 1),
                "latency_p95_ms": round(percentile(p_lats, 95), 1),
                "places_per_request": round(len(p_places) / p_req_count, 2) if p_req_count else 0.0,
                "traffic_mb_per_1k_places": round((p_wire_mb / len(p_places) * 1000.0), 3) if p_places else 0.0,
                "asn_mix": dict(asn_counts),
            }

        # 11. Country Breakdown (legacy UI compatibility)
        by_country_dict: Dict[str, Dict[str, Any]] = {}
        proxied_sessions = {s["session_id"] for s in session_rows if s.get("pool") in {"residential", "mobile", "mixed"}}
        countries_list = sorted({r["country"] for r in request_rows if r.get("country")} |
                                {q["country"] for q in query_items if q.get("country")})
        for c_code in countries_list:
            c_reqs = [r for r in eval_requests if r.get("country") == c_code]
            c_places = [p for p in place_rows if p.get("country") == c_code]
            c_queries = [q for q in query_items if q.get("country") == c_code]
            c_successful = sum(q.get("status") == "success" for q in c_queries)
            c_ip_states = Counter(q.get("ip_stable") for q in c_queries)

            c_ok = sum(1 for r in c_reqs if r.get("outcome") == "ok")
            c_req_count = len(c_reqs)
            c_lats = [safe_float(r.get("latency_ms")) for r in c_reqs if r.get("outcome") == "ok"]
            c_q_walls = [safe_float(q.get("query_wall_ms")) for q in c_queries if safe_float(q.get("query_wall_ms")) > 0]
            c_wire = sum(int(r.get("bytes_wire") or 0) for r in c_reqs) / MB
            proxy_successes = sum(q.get("status") == "success" and q.get("session") in proxied_sessions for q in c_queries)
            proxy_wire = sum(int(r.get("bytes_wire") or 0) for r in c_reqs if r.get("session_id") in proxied_sessions)

            by_country_dict[c_code] = {
                "queries": len(c_queries),
                "successful_queries": c_successful,
                "query_success_rate_pct": round(100 * c_successful / len(c_queries), 2) if c_queries else None,
                "ip_matched_queries": c_ip_states[True],
                "ip_changed_queries": c_ip_states[False],
                "ip_unknown_queries": c_ip_states[None],
                "places": len(c_places),
                "requests": c_req_count,
                "ok": c_ok,
                "consent": sum(1 for r in c_reqs if r.get("outcome") == "consent"),
                "403": sum(1 for r in c_reqs if r.get("outcome") == "403"),
                "429": sum(1 for r in c_reqs if r.get("outcome") == "429"),
                "block_403": sum(1 for r in c_reqs if r.get("outcome") == "403"),
                "block_429": sum(1 for r in c_reqs if r.get("outcome") == "429"),
                "sorry": sum(1 for r in c_reqs if r.get("outcome") == "sorry"),
                "soft_block": sum(1 for r in c_reqs if r.get("outcome") == "soft_block"),
                "proxy_error": sum(1 for r in c_reqs if r.get("outcome") == "proxy_error"),
                "success_rate": round(c_ok / c_req_count, 4) if c_req_count else 0.0,
                "consent_rate": round(
                    sum(1 for r in c_reqs if r.get("outcome") == "consent") / c_req_count, 4
                ) if c_req_count else 0.0,
                "avg_latency_ms": round(sum(c_lats) / len(c_lats), 1) if c_lats else 0.0,
                "avg_query_ms": round(sum(c_q_walls) / len(c_q_walls), 1) if c_q_walls else 0.0,
                "traffic_mb": round(c_wire, 3),
                "estimated_usd_per_successful_proxy_query": (
                    price * proxy_wire / 1_000_000_000 / proxy_successes if price is not None and proxy_successes else None
                ),
            }

        # 12. Sticky Continuity (legacy UI compatibility)
        query_ip_matched = sum(1 for q in query_items if q.get("ip_stable") is True)
        query_ip_rotated = sum(1 for q in query_items if q.get("exit_ip_start") and q.get("exit_ip_end") and q.get("ip_stable") is False)
        query_ip_unknown = sum(1 for q in query_items if not q.get("exit_ip_start") or not q.get("exit_ip_end") or q.get("ip_stable") is None)

        sticky_sessions_list: List[Dict[str, Any]] = []
        for s in session_rows:
            s_id = s.get("session_id") or ""
            summary = session_ip_summaries.get(s_id, {})
            s_queries = [q for q in query_items if q.get("session") == s_id]
            # TTL sessions with 0 queries must not count as rotated
            rotated_mid_query = (
                any(q.get("exit_ip_start") and q.get("exit_ip_end") and q.get("ip_stable") is False for q in s_queries)
                if s_queries else False
            )
            distinct_exit_ips = summary.get("distinct_primary_ips", 1) or 1
            primary_ips = summary.get("primary_ips", [])
            sticky_sessions_list.append({
                "session_id": s_id,
                "countries": sorted(set(q.get("country") for q in s_queries if q.get("country"))),
                "queries": len(s_queries),
                "distinct_exit_ips": distinct_exit_ips,
                "exit_ips": sorted(primary_ips),
                "distinct_v4": summary.get("distinct_v4", 0),
                "distinct_v6": summary.get("distinct_v6", 0),
                "rotated_mid_query": rotated_mid_query,
            })

        # Assemble full result metrics dict
        q_succ_rate = round(successful_queries / total_queries, 4) if total_queries else 0.0
        q_succ_ci = wilson(successful_queries, total_queries)
        q_succ_pct = round(q_succ_rate * 100.0, 2)

        results: Dict[str, Any] = {
            "run_id": run_id,
            "status": run_info.get("status") or "completed",
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_s": round(elapsed_s, 2),
            "totals": {
                "total_queries": total_queries,
                "successful_queries": successful_queries,
                "failed_queries": failed_queries,
                "total_places": total_places,
                "total_requests": total_requests_all,
                "final_requests": total_final_requests,
                "ok_requests": ok_requests,
                "consent_requests": consent_requests,
                "block_403": block_403,
                "block_429": block_429,
                "total_sessions": total_sessions,
                "traffic_mb": round(traffic_mb, 3),
                "traffic_mb_wire": round(traffic_mb_wire, 3),
                "traffic_gb": round(traffic_gb, 4),
            },
            "query_status_counts": dict(query_status_counts),
            "request_outcome_counts": dict(outcome_counts),
            "rates": {
                "success_rate": round(success_rate, 4),
                "success_rate_ci": success_rate_ci,
                "success_rate_pct": round(success_rate * 100.0, 2),
                "query_success_rate": q_succ_rate,
                "query_success_rate_ci": q_succ_ci,
                "query_success_rate_pct": q_succ_pct,
                "block_rate": round(block_rate, 4),
                "block_rate_ci": block_rate_ci,
                "block_rate_pct": round(block_rate * 100.0, 2),
                "consent_rate": round(consent_rate, 4),
                "consent_rate_ci": consent_rate_ci,
                "consent_rate_pct": round(consent_rate * 100.0, 2),
                "proxy_error_rate": round(proxy_error_rate, 4),
                "proxy_error_rate_ci": proxy_error_rate_ci,
                "proxy_error_rate_pct": round(proxy_error_rate * 100.0, 2),
            },
            "traffic": {
                "traffic_mb": round(traffic_mb, 3),
                "traffic_mb_wire": round(traffic_mb_wire, 3),
                "traffic_tx_mb_est": round(traffic_tx_mb_est, 3),
                "traffic_probe_est_mb": round(traffic_probe_est_mb, 4),
                "traffic_mb_decompressed": round(traffic_mb_decompressed, 3),
                "traffic_gb": round(traffic_gb, 4),
            },
            "latency": {
                "avg_latency_ms": round(avg_latency_ms, 1),
                "p95_latency_ms": round(p95_latency_ms, 1),
                "avg_query_ms": round(avg_query_ms, 1),
                "p95_query_ms": round(p95_query_ms, 1),
                "qps": round(qps, 4),
                "places_per_minute": round(places_per_minute, 2),
            },
            "latency_by_outcome": latency_by_outcome,
            "session_stability": {
                "sessions": total_sessions,
                "sessions_with_block": sessions_with_block,
                "block_onset_cohort_sessions": block_cohort_sessions,
                "block_rate": round(sess_block_rate, 4),
                "block_rate_ci": sess_block_ci,
                "median_requests_to_first_block": med_req_to_block,
                "first_block_distribution": first_block_dist,
                "distinct_ips_distribution": distinct_ips_dist,
            },
            "arm_breakdown": arm_breakdown,
            "ttl": ttl_metrics,
            "variance": {
                "by_pair_type": variance_summary,
            },
            "load": load_summary,
            "pools": pool_summary,
            "by_country": by_country_dict,
            "sticky_continuity": {
                "query_ip_matched": query_ip_matched,
                "query_ip_rotated": query_ip_rotated,
                "query_ip_unknown": query_ip_unknown,
                "sessions": sticky_sessions_list,
            },
            "validation": {
                "valid": valid_places,
                "unvalidated": unvalidated_places,
                **{key: sum(key in flags for flags in validation_flags) for key in (
                    "bad_place_id", "missing_title", "missing_address", "bad_geo", "out_of_bbox"
                )},
                "parse_error": 0,
                "flagged_places": validated_places - valid_places,
                "flagged_queries": 0,
                "flagged_query_names": [],
                "validation_rate_pct": round(100 * valid_places / validated_places, 2) if validated_places else None,
            },
            "queries": query_items,
        }

        return sanitize_floats(results)

    finally:
        conn.close()


# ── Markdown Report Generation ──────────────────────────────────────────────


def _format_ci(val: Optional[float], ci: Optional[Tuple[float, float]], pct: bool = True) -> str:
    if val is None or ci is None:
        return "N/A"
    if pct:
        return f"{val * 100:.1f}% [{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]"
    return f"{val:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]"


def get_primary_ip(observations: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    """Extract primary egress IP and its family ('v4' or 'v6').

    Shared rule:
    Primary egress IP = first non-empty IPv4 (":" not in ip) from observations,
    preferring witness 'ipify_v4'; if no IPv4 at all, the first IPv6 (":" in ip).
    Never compare a v4 to a v6 as an IP change.
    Returns (ip, family). If no valid IP, returns ("", "").
    """
    if not observations:
        return ("", "")

    # 1. Prefer witness 'ipify_v4' with non-empty IPv4 (":" not in ip)
    for obs in observations:
        ip = (obs.get("ip") or "").strip()
        witness = (obs.get("witness") or "").strip()
        if witness == "ipify_v4" and ip and ":" not in ip:
            return (ip, "v4")

    # 2. First non-empty IPv4 (":" not in ip) from any witness
    for obs in observations:
        ip = (obs.get("ip") or "").strip()
        if ip and ":" not in ip:
            return (ip, "v4")

    # 3. If no IPv4 at all, the first non-empty IPv6 (":" in ip)
    for obs in observations:
        ip = (obs.get("ip") or "").strip()
        if ip and ":" in ip:
            return (ip, "v6")

    return ("", "")


def get_session_ip_summary(observations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute primary IP, family, distinct primary IPs, and raw v4/v6 counts for a session."""
    primary_ip, family = get_primary_ip(observations)

    v4_ips: List[str] = []
    v6_ips: List[str] = []

    for obs in observations:
        ip = (obs.get("ip") or "").strip()
        if not ip:
            continue
        if ":" not in ip:
            if ip not in v4_ips:
                v4_ips.append(ip)
        else:
            if ip not in v6_ips:
                v6_ips.append(ip)

    if family == "v4":
        primary_ips = list(v4_ips)
    elif family == "v6":
        primary_ips = list(v6_ips)
    else:
        primary_ips = []

    return {
        "primary_ip": primary_ip,
        "primary_family": family,
        "primary_ips": primary_ips,
        "distinct_primary_ips": len(primary_ips),
        "v4_ips": v4_ips,
        "v6_ips": v6_ips,
        "distinct_v4": len(v4_ips),
        "distinct_v6": len(v6_ips),
        "all_observed_ips": sorted(set(v4_ips + v6_ips)),
    }


def generate_report_md(metrics: Dict[str, Any]) -> str:
    """Generate comprehensive benchmark report markdown with all 10 required sections."""
    totals = metrics.get("totals", {})
    rates = metrics.get("rates", {})
    latency = metrics.get("latency", {})
    stability = metrics.get("session_stability", {})
    first_block = stability.get("first_block_distribution", {})
    distinct_ips = stability.get("distinct_ips_distribution", {})
    ttl = metrics.get("ttl", {})
    arms = metrics.get("arm_breakdown", {})
    variance = metrics.get("variance", {}).get("by_pair_type", {})
    load = metrics.get("load", {})
    pools = metrics.get("pools", {})
    run_id = metrics.get("run_id", "unknown")
    started_at = metrics.get("started_at", 0.0)
    iso_time = (
        datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
        if started_at > 0 else "N/A"
    )

    total_blocks_count = (
        totals.get("block_403", 0)
        + totals.get("block_429", 0)
        + totals.get("consent_requests", 0)
        + metrics.get("request_outcome_counts", {}).get("sorry", 0)
        + metrics.get("request_outcome_counts", {}).get("soft_block", 0)
    )
    q_succ = totals.get("successful_queries", 0)
    q_tot = totals.get("total_queries", 0)
    q_rate = (q_succ / q_tot) if q_tot > 0 else 0.0
    q_ci = wilson(q_succ, q_tot)

    lines: List[str] = [
        f"# AnyIP Google Maps Benchmark Report — {run_id}",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  ",
        f"**Run ID:** `{run_id}` | **Status:** `{metrics.get('status', 'completed')}`",
        "",
        "---",
        "",
        "## Method",
        "This benchmark measures Google Maps scraping resilience, session stickiness, exit IP stability, "
        "and result variance under AnyIP proxy infrastructure. "
        "All statistical intervals use explicit non-parametric and exact formulations:",
        "- **Binomial Rates:** 95% Wilson score confidence interval without continuity correction.",
        "- **Zero-Event Thresholds:** Rule of Three 95% upper confidence bound ($3 / n$).",
        "- **Block Onset & IP Survival:** Non-parametric Kaplan-Meier estimator with tie resolution.",
        "- **Result Set Similarity:** Rank-Biased Overlap (RBO, $p=0.9$), Jaccard similarity at depths 20 and 40, "
        "and Kendall's $\\tau_b$ rank correlation on overlapping places.",
        "- **Variance Differences:** 10,000-iteration percentile bootstrap confidence intervals.",
        "",
        "## Environment / Manifest",
        f"- **Benchmark Start:** `{iso_time}`",
        f"- **Wall Elapsed Time:** {metrics.get('elapsed_s', 0.0):.2f}s",
        f"- **Total Sessions:** {totals.get('total_sessions', 0)}",
        f"- **Total Queries Executed:** {totals.get('total_queries', 0)}",
        f"- **Total HTTP Requests (attempts included):** {totals.get('total_requests', 0)}",
        f"- **Total Places Scraped:** {totals.get('total_places', 0)}",
        f"- **Estimated HTTP Wire Traffic:** {totals.get('traffic_mb_wire', 0.0):.3f} MiB (not provider billing)",
        "",
        "## Data Quality",
        "Validation status of scraped place records and query outputs:",
        "",
        "| Metric | Count | Rate (95% CI) |",
        "| :--- | :--- | :--- |",
        f"| Valid Places | {metrics.get('validation', {}).get('valid', 0)} | "
        + (f"{validation_pct:.2f}% of validated places |"
           if (validation_pct := metrics.get('validation', {}).get('validation_rate_pct')) is not None
           else "Not measured |"),
        f"| Successful Queries | {q_succ} / {q_tot} | {_format_ci(q_rate, q_ci)} |",
        f"| Blocked Requests | {total_blocks_count} | {_format_ci(rates.get('block_rate', 0.0), rates.get('block_rate_ci', (0.0, 0.0)))} |",
        f"| Proxy Errors | {metrics.get('request_outcome_counts', {}).get('proxy_error', 0)} | {_format_ci(rates.get('proxy_error_rate', 0.0), rates.get('proxy_error_rate_ci', (0.0, 0.0)))} |",
        "",
        "## Session Stability",
        "Sticky session continuity and IP retention across queries:",
        "",
        f"- **Sessions Evaluated:** {stability.get('sessions', 0)}",
        (f"- **Block-onset cohort sessions with at least one block:** {stability.get('sessions_with_block', 0)} / {stability.get('block_onset_cohort_sessions')}"
         if stability.get('block_onset_cohort_sessions') else "- **Dedicated block-onset cohort:** Not measured"),
        (f"- **Sessions with one observed exit IP (among witnessed sessions):** {sticky_pct:.1f}%"
         if (sticky_pct := distinct_ips.get('perfectly_sticky_pct')) is not None
         else "- **Exit IP continuity:** Unknown (no usable witnesses)"),
        f"- **Distinct Exit IPs per Session:** Mean {distinct_ips.get('mean', 1.0):.2f} (Min: {distinct_ips.get('min', 1)}, Max: {distinct_ips.get('max', 1)})",
        f"- **Median Requests to First Block (Kaplan-Meier):** {stability.get('median_requests_to_first_block') or 'Undefined (> max requests)'}",
        "",
        "### TTL & Exit IP Duration",
    ]

    if ttl.get("sessions", 0) > 0:
        ttl_s_count = ttl.get("sessions", 0)
        total_changes = ttl.get("observed_ip_changes", ttl.get("changes_per_session", {}).get("total", 0))
        median_s = ttl.get("median_time_to_ip_change_s")
        km_curve = ttl.get("km_curve", [])
        end_survival = (km_curve[-1].get("survival", 1.0) * 100.0) if km_curve else 100.0

        if total_changes == 0:
            lines.extend([
                f"- **IP Changes Observed:** 0 IP changes over the measured window (100% observed stickiness); median undefined because survival never reached 50%.",
                f"- **Mean IP Changes per Session:** {ttl.get('changes_per_session', {}).get('mean', 0.0):.2f}",
            ])
        elif median_s is not None:
            lines.extend([
                f"- **Median Time to First IP Change:** {median_s:.1f}s",
                f"- **IP Changes Observed:** {total_changes} IP changes across {ttl_s_count} sessions; median time to first change {median_s:.1f}s (95% KM).",
                f"- **Mean IP Changes per Session:** {ttl.get('changes_per_session', {}).get('mean', 0.0):.2f}",
            ])
        else:
            lines.extend([
                f"- **IP Changes Observed:** {total_changes} IP changes across {ttl_s_count} sessions; median not reached within the measured window (survival {end_survival:.1f}% at end).",
                f"- **Mean IP Changes per Session:** {ttl.get('changes_per_session', {}).get('mean', 0.0):.2f}",
            ])
    else:
        lines.append("*TTL test arm not run in this session.*")

    lines.extend([
        "",
        "## Block Onset",
    ])

    if not stability.get("block_onset_cohort_sessions"):
        lines.append("*No dedicated block-onset cohort was measured. Request-level block outcomes are reported above.*")
        if not total_blocks_count:
            lines.append("*No block responses were recorded in the observed requests.*")
    elif first_block.get("count", 0) > 0:
        lines.extend([
            "Distribution of request index at which Google issued the first block:",
            "",
            "| Metric | Value |",
            "| :--- | :--- |",
            f"| Minimum Request Index | {first_block.get('min', 0.0)} |",
            f"| 25th Percentile | {first_block.get('p25', 0.0)} |",
            f"| Median Request Index | {first_block.get('median', 0.0)} |",
            f"| 75th Percentile | {first_block.get('p75', 0.0)} |",
            f"| 95th Percentile | {first_block.get('p95', 0.0)} |",
            f"| Maximum Request Index | {first_block.get('max', 0.0)} |",
            "",
            "**Onset Histogram:** "
            + ", ".join(f"[{k}]: {v}" for k, v in first_block.get("buckets", {}).items()),
        ])
    else:
        lines.append("*No blocks encountered during session execution (zero onset events).*")

    country_metrics = metrics.get("by_country", {})
    if country_metrics:
        lines.extend([
            "", "## Country Results", "",
            "IP counts compare exit-IP witness observations, not the IP seen by the destination.", "",
            "| Country | Successful queries / observed | Places | Consent | 403 | 429 | Avg OK latency (ms) | Traffic (MiB, estimated) | IP matched / changed / unknown |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for country, row in country_metrics.items():
            lines.append(
                f"| {country} | {row['successful_queries']} / {row['queries']} | {row['places']} | "
                f"{row['consent']} | {row['403']} | {row['429']} | {row['avg_latency_ms']} | "
                f"{row['traffic_mb']} | {row['ip_matched_queries']} / {row['ip_changed_queries']} / {row['ip_unknown_queries']} |"
            )
        lines.extend(["", "Estimated cost per successful proxy query (request traffic only; excludes witness traffic and is not an invoice):"])
        for country, row in country_metrics.items():
            cost = row["estimated_usd_per_successful_proxy_query"]
            lines.append(f"- {country}: " + (f"${cost:.8f}" if cost is not None else "not available (no configured price or no successful proxy queries)"))

    lines.extend([
        "",
        "## Result Variance",
        "Place ranking similarity across repeats (same IP vs new IP vs local):",
        "",
    ])

    if variance:
        for p_type, data in variance.items():
            lines.append(f"- `{p_type}`: {data.get('empty_repeats', 0)} empty; {data.get('failed_or_incomplete_repeats', 0)} failed or incomplete. Similarity is conditional on comparable ranked outputs.")
        lines.append("")
        lines.extend([
            "| Pair Type | Pairs ($n$) | Jaccard@20 (95% CI) | Jaccard@40 (95% CI) | RBO $p=0.9$ (95% CI) | Kendall $\\tau_b$ (95% CI) |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for p_type, v_data in variance.items():
            lines.append(
                f"| `{p_type}` | {v_data.get('n_pairs', 0)} | "
                f"{_format_ci(v_data.get('jaccard_20_mean', 0.0), v_data.get('jaccard_20_ci', (0.0, 0.0)), pct=False)} | "
                f"{_format_ci(v_data.get('jaccard_40_mean', 0.0), v_data.get('jaccard_40_ci', (0.0, 0.0)), pct=False)} | "
                f"{_format_ci(v_data.get('rbo_mean', 0.0), v_data.get('rbo_ci', (0.0, 0.0)), pct=False)} | "
                f"{_format_ci(v_data.get('kendall_tau_mean', 0.0), v_data.get('kendall_tau_ci', (0.0, 0.0)), pct=False)} |"
            )
    else:
        lines.append("*Result variance arm not run in this session.*")

    lines.extend([
        "",
        "## Load",
        "Performance and error rates under concurrent request stress:",
        "",
    ])

    if load:
        lines.extend([
            "| Variant | Sessions | Requests | Achieved RPS | Block Rate (95% CI) | Proxy Error Rate | Latency p50 | Latency p95 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for l_var, l_data in load.items():
            lines.append(
                f"| `{l_var}` | {l_data.get('sessions', 0)} | {l_data.get('requests', 0)} | {l_data.get('achieved_rps', 0.0):.2f} | "
                f"{_format_ci(l_data.get('block_rate', 0.0), l_data.get('block_rate_ci', (0.0, 0.0)))} | "
                f"{l_data.get('proxy_error_rate', 0.0) * 100:.1f}% | "
                f"{l_data.get('latency_p50_ms', 0.0):.1f} ms | {l_data.get('latency_p95_ms', 0.0):.1f} ms |"
            )
    else:
        lines.append("*Load test arm not run in this session.*")

    lines.extend([
        "",
        "## Pool Comparison",
        "Comparison of proxy pool architectures (Residential vs Mobile vs Mixed vs Local):",
        "",
    ])

    if pools:
        lines.extend([
            "| Pool | Sessions | Requests | Success % (95% CI) | Block % (95% CI) | Places / Req | Wire MB / 1k Places | Latency p50 / p95 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for p_name, p_data in pools.items():
            lines.append(
                f"| `{p_name}` | {p_data.get('sessions', 0)} | {p_data.get('requests', 0)} | "
                f"{_format_ci(p_data.get('success_rate', 0.0), p_data.get('success_rate_ci', (0.0, 0.0)))} | "
                f"{_format_ci(p_data.get('block_rate', 0.0), p_data.get('block_rate_ci', (0.0, 0.0)))} | "
                f"{p_data.get('places_per_request', 0.0):.2f} | "
                f"{p_data.get('traffic_mb_per_1k_places', 0.0):.3f} MB | "
                f"{p_data.get('latency_p50_ms', 0.0):.0f} / {p_data.get('latency_p95_ms', 0.0):.0f} ms |"
            )
    else:
        lines.append("*Pool comparison arm not run in this session.*")

    lines.extend([
        "",
        "## Limitations",
        "- **Probe Witness Overhead:** Dual IPv4/IPv6 witness probing adds ~2KB wire traffic per query.",
        "- **Captive Portal / Consent Interception:** Headless HTTP scrapers cannot solve dynamic recaptcha challenges.",
        "- **Sample Boundaries:** Confidence intervals reflect sample size ($n$); low-n runs produce wide Wilson intervals.",
    ])

    # Generate evidence-based recommendations conditional on observed data
    recs: List[str] = []

    # 1. Block onset / session rotation
    total_blocks_seen = (
        totals.get("block_403", 0)
        + totals.get("block_429", 0)
        + totals.get("consent_requests", 0)
        + metrics.get("request_outcome_counts", {}).get("sorry", 0)
        + metrics.get("request_outcome_counts", {}).get("soft_block", 0)
        + stability.get("sessions_with_block", 0)
    )
    med_block = stability.get("median_requests_to_first_block")
    if not stability.get("block_onset_cohort_sessions"):
        recs.append("1. **Session Longevity:** Block onset was not measured; this run does not establish a session lifetime under sustained traffic.")
    elif total_blocks_seen == 0 or first_block.get("count", 0) == 0:
        recs.append(
            "1. **Session Longevity & Rotation Policy:** No block onset was observed in this run; "
            "no safe rotation interval can be inferred. Recommend running the dedicated `block_onset` arm "
            "with higher request depth before setting a rotation policy."
        )
    elif med_block is not None:
        recs.append(
            f"1. **Session Longevity:** Median block onset occurred at request {med_block:.0f}. "
            f"Rotate sticky sessions before reaching {max(1, int(med_block * 0.8))} requests."
        )
    else:
        recs.append(
            "1. **Session Longevity:** Block events occurred but survival remained above 50%; "
            "rotate sessions conservatively upon encountering Google 403, 429, or consent challenges."
        )

    # 2. TTL lifetime constraint (do not extrapolate query count)
    ttl_s_count = ttl.get("sessions", 0)
    ttl_changes = ttl.get("observed_ip_changes", ttl.get("changes_per_session", {}).get("total", 0))
    ttl_med = ttl.get("median_time_to_ip_change_s")
    km_curve = ttl.get("km_curve", [])
    end_survival = (km_curve[-1].get("survival", 1.0) * 100.0) if km_curve else 100.0

    if ttl_s_count > 0:
        if ttl_med is not None:
            recs.append(
                f"2. **Session Lifetime Constraint:** TTL stability was measured across {ttl_s_count} sessions with an "
                f"observed median exit IP lifetime of {ttl_med:.1f}s. This establishes a hard session-lifetime constraint; "
                "recreate sessions within this window and do not extrapolate this duration into a fixed query count."
            )
        elif ttl_changes > 0:
            recs.append(
                f"2. **Session Lifetime Constraint:** TTL stability was measured across {ttl_s_count} sessions with "
                f"{ttl_changes} IP changes observed, but median time to first change was not reached within the measured window "
                f"(survival {end_survival:.1f}% at end). Enforce session recreation within the observed change bounds; "
                "do not extrapolate to a fixed query count."
            )
        else:
            recs.append(
                f"2. **Exit IP Stability:** TTL stability was measured across {ttl_s_count} sessions with 0 IP changes "
                "observed over the measured window (100% observed stickiness). Note that this is not proof of indefinite "
                "stickiness beyond the tested duration."
            )
    else:
        recs.append(
            "2. **Exit IP Stability:** TTL stability was not measured in this run; verify IP retention with a dedicated TTL arm."
        )

    # 3. Pool allocation
    pools_measured = set(pools.keys())
    has_mobile_or_mixed = any(p in ("mobile", "mixed") for p in pools_measured)
    if not has_mobile_or_mixed:
        recs.append(
            "3. **Pool Allocation:** Only residential pool was measured in this run. Comparison with mobile or "
            "mixed pools was not measured; evaluate mobile pools in a comparative arm before allocating traffic across pools."
        )
    else:
        recs.append(
            "3. **Pool Allocation:** Use mobile pools for high-scrutiny geo targets where residential blocks occur, "
            "and residential pools for cost-effective bulk throughput."
        )

    # 4. Load concurrency
    if not load:
        recs.append(
            "4. **Load & Concurrency:** Concurrent load behavior was not measured in this run; "
            "validate concurrency limits and RPS capacity in a dedicated `load` arm before production scaling."
        )
    else:
        recs.append(
            "4. **Load & Concurrency:** Concurrency limits and achieved RPS from the load arm should govern "
            "worker scaling limits to avoid proxy gateway saturation."
        )

    lines.extend([
        "",
        "## Recommendations",
        *recs,
        "",
    ])

    return "\n".join(lines)


# ── Raw CSV Export & Output Writing ──────────────────────────────────────────


def _export_csvs_direct(
    conn: sqlite3.Connection,
    run_id: str,
    out_dir: Path,
    existing_tables: Set[str],
) -> Dict[str, int]:
    """Fallback exporter that writes CSVs directly from sqlite3."""
    table_defs = [
        ("sessions", "sessions.csv", [
            "run_id", "session_id", "arm", "variant", "pool", "country", "session_name",
            "strict_replace", "sessasn_strict", "sessipcollision_strict", "sesstime_min",
            "created_at", "started_at", "ended_at", "requests", "google_requests", "places",
            "block_events", "first_block_request", "first_block_ts", "status", "stop_reason",
            "distinct_ips", "ip_set_json", "asn_set_json", "city_set_json", "traffic_bytes", "error",
        ]),
        ("requests", "requests.csv", [
            "id", "run_id", "session_id", "ts", "request_index", "query_index", "query",
            "country", "kind", "page_start", "url_hash", "status", "outcome", "block_type",
            "anyip_error", "attempt", "is_final", "latency_ms", "bytes_wire", "bytes_rx",
            "content_encoding", "content_type", "server", "error", "arm", "variant",
        ]),
        ("places", "places.csv", [
            "id", "run_id", "session_id", "query_index", "query", "country", "request_id", "rank", "place_id",
            "title", "category", "address", "phone", "website", "stars", "reviews", "lat", "lng",
            "validation", "arm", "variant",
        ]),
        ("ip_observations", "ip_observations.csv", [
            "id", "run_id", "session_id", "ts", "request_index", "witness", "family", "ip",
            "asn", "org", "city", "country", "latency_ms", "error", "arm", "variant",
        ]),
        ("events", "events.csv", [
            "id", "run_id", "session_id", "ts", "request_index", "event_type", "detail_json",
            "arm", "variant",
        ]),
    ]

    row_counts: Dict[str, int] = {}
    for table_name, csv_filename, columns in table_defs:
        target_file = out_dir / csv_filename
        count = 0
        with open(target_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            if table_name in existing_tables:
                t_cols = _get_table_columns(conn, table_name)
                where = " WHERE run_id = ?" if "run_id" in t_cols else ""
                params = (run_id,) if "run_id" in t_cols else ()
                try:
                    cur = conn.execute(f"SELECT * FROM {table_name}{where}", params)
                    for row in cur:
                        writer.writerow([row[c] if c in row.keys() and row[c] is not None else "" for c in columns])
                        count += 1
                except sqlite3.OperationalError:
                    pass
        row_counts[table_name] = count
    return row_counts


def write_outputs(
    db_path: Union[str, Path],
    run_id: str,
    out_dir: Union[str, Path],
) -> Dict[str, Any]:
    """Write summary.json, analysis.json, report.md, and raw CSV files to out_dir.

    Never crashes on missing/empty data; omits absent arms or marks 'not run';
    never emits NaN/Inf in JSON.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # 1. Compute comprehensive metrics
    metrics = run_metrics(db_path, run_id)

    # 2. Export raw CSV files (attempt via store.export_raw, fallback to sqlite)
    csv_counts: Dict[str, int] = {}
    exported_via_store = False
    try:
        import store
        if hasattr(store, "export_raw"):
            csv_counts = store.export_raw(run_id=run_id, out_dir=out_path, db_path=db_path)
            exported_via_store = True
    except Exception:
        exported_via_store = False

    if not exported_via_store:
        conn = _open_db(db_path)
        try:
            tables = _get_existing_tables(conn)
            csv_counts = _export_csvs_direct(conn, run_id, out_path, tables)
        finally:
            conn.close()

    # 3. Build summary.json matching existing UI fields
    totals = metrics.get("totals", {})
    rates = metrics.get("rates", {})
    traffic = metrics.get("traffic", {})
    latency = metrics.get("latency", {})
    started_at = metrics.get("started_at", 0.0)
    iso_started = (
        datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
        if started_at > 0 else datetime.now(timezone.utc).isoformat()
    )

    summary_json: Dict[str, Any] = {
        "started_at_iso": iso_started,
        "run_id": run_id,
        "proxy_type": "residential",
        "session_strategy": "resume-last",
        "geo_targets": sorted(metrics.get("by_country", {}).keys()) or ["US"],
        "concurrency": 1,
        "elapsed_s": metrics.get("elapsed_s", 0.0),
        "total_queries": totals.get("total_queries", 0),
        "successful_queries": totals.get("successful_queries", 0),
        "failed_queries": totals.get("failed_queries", 0),
        "total_places": totals.get("total_places", 0),
        "total_requests": totals.get("total_requests", 0),
        "ok_requests": totals.get("ok_requests", 0),
        "consent_requests": totals.get("consent_requests", 0),
        "block_403": totals.get("block_403", 0),
        "block_429": totals.get("block_429", 0),
        "success_rate_pct": rates.get("success_rate_pct", 0.0),
        "consent_rate_pct": rates.get("consent_rate_pct", 0.0),
        "block_rate_pct": rates.get("block_rate_pct", 0.0),
        "traffic_mb": traffic.get("traffic_mb", 0.0),
        "traffic_mb_wire": traffic.get("traffic_mb_wire", 0.0),
        "traffic_tx_mb_est": traffic.get("traffic_tx_mb_est", 0.0),
        "traffic_probe_est_mb": traffic.get("traffic_probe_est_mb", 0.0),
        "traffic_mb_decompressed": traffic.get("traffic_mb_decompressed", 0.0),
        "traffic_gb": traffic.get("traffic_gb", 0.0),
        "avg_latency_ms": latency.get("avg_latency_ms", 0.0),
        "p95_latency_ms": latency.get("p95_latency_ms", 0.0),
        "avg_query_ms": latency.get("avg_query_ms", 0.0),
        "p95_query_ms": latency.get("p95_query_ms", 0.0),
        "qps": latency.get("qps", 0.0),
        "places_per_minute": latency.get("places_per_minute", 0.0),
        "validation": metrics.get("validation", {}),
        "sticky_continuity": metrics.get("sticky_continuity", {}),
        "by_country": metrics.get("by_country", {}),
        "queries": metrics.get("queries", []),
        # Additional compatibility keys
        "query_success_rate_pct": rates.get("query_success_rate_pct", 0.0),
        "partial_queries": metrics.get("query_status_counts", {}).get("partial", 0),
        "blocked_queries": metrics.get("query_status_counts", {}).get("blocked", 0),
        "empty_queries": metrics.get("query_status_counts", {}).get("empty", 0),
        "error_queries": metrics.get("query_status_counts", {}).get("error", 0),
        "proxy_error_requests": metrics.get("request_outcome_counts", {}).get("proxy_error", 0),
        "google_error_requests": metrics.get("request_outcome_counts", {}).get("google_5xx", 0),
        "sorry_requests": metrics.get("request_outcome_counts", {}).get("sorry", 0),
        "soft_block_requests": metrics.get("request_outcome_counts", {}).get("soft_block", 0),
        "session_count": totals.get("total_sessions", 0),
        "ip_change_count": metrics.get("ttl", {}).get("changes_per_session", {}).get("total", 0),
        "proxy_error_rate_pct": rates.get("proxy_error_rate_pct", 0.0),
    }

    # Ensure clean floats with no NaNs/Infs
    clean_summary = sanitize_floats(summary_json)
    clean_analysis = sanitize_floats(metrics)

    summary_file = out_path / "summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(clean_summary, f, indent=2, allow_nan=False)

    analysis_file = out_path / "analysis.json"
    with open(analysis_file, "w", encoding="utf-8") as f:
        json.dump(clean_analysis, f, indent=2, allow_nan=False)

    report_file = out_path / "report.md"
    report_content = generate_report_md(clean_analysis)
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_content)

    return {
        "run_id": run_id,
        "out_dir": str(out_path),
        "csv_counts": csv_counts,
        "summary_file": str(summary_file),
        "analysis_file": str(analysis_file),
        "report_file": str(report_file),
        "summary": clean_summary,
        "analysis": clean_analysis,
    }
