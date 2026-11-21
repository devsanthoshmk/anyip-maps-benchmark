"""
Experiment runner for AnyIP benchmarks.

Executes structured benchmark arms defined in TOML configuration files:
- TTL stability (exit IP continuity over time, no Google traffic)
- Block onset (requests to first Google block under sustained scraping)
- Variance (result rank similarity: same-IP vs new-IP vs local vs forced_rotate)
- Load testing (throughput & error rate scaling across session counts and concurrencies)
- Pool comparison (head-to-head metrics: residential vs mobile vs mixed vs local)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import tomllib
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
import uuid

from rich.console import Console
from rich.table import Table

from config import PROXY, ProxyConfig
from scraper import (
    BLOCK_OUTCOMES,
    IpObservation,
    ProxySession,
    QueryResult,
    RequestRecord,
    RequestGuard,
    request_guard,
    _exit_ips,
    scrape_query,
)
from store import Store, init as store_init
import quota

logger = logging.getLogger(__name__)
console = Console()

VALID_ARM_KINDS = {"ttl", "block_onset", "variance", "load", "pool_compare"}
VALID_POOLS = {"residential", "mobile", "mixed", "local"}


@dataclass
class ArmConfig:
    name: str = ""
    kind: str = ""  # ttl | block_onset | variance | load | pool_compare
    pool: str = "residential"  # residential | mobile | mixed | local
    country: Optional[str] = None
    sessions: int = 1
    sesstime_min: int = 20
    strict_replace: bool = False
    sessasn_strict: bool = False
    sessipcollision_strict: bool = False
    query_panel: Optional[str] = None
    depth_pages: Optional[int] = None
    requests_per_session: int = 0
    queries_per_session: int = 0
    concurrency_per_session: int = 1
    delay_s: float = 0.0
    duration_min: float = 0.0
    probe_interval_s: float = 20.0
    same_ip_repeats: int = 2
    new_ip_repeats: int = 2
    forced_rotate_repeats: int = 0
    local_repeats: int = 1
    repeat_delay_s: float = 60.0
    parallel_variance_queries: int = 1
    load_levels: list[int] = field(default_factory=list)
    load_concurrency: list[int] = field(default_factory=list)
    max_pages: int = 0
    stop_on_block: bool = True
    variant: str = ""
    pools: list[str] = field(default_factory=list)
    include_local: bool = False


@dataclass
class RunConfig:
    id: str = ""
    country: str = "US"
    query_panel: str = "queries/panel_all.csv"
    depth_pages: int = 1
    quota_cap_mb: float = 100.0
    price_usd_per_gb: Optional[float] = None
    stop_block_rate: float = 0.5
    stop_proxy_error_rate: float = 0.5
    seed: int = 42
    output_dir: str = "results"
    max_parallel_sessions: int = 5
    rate_limit_rps: float = 0.0
    ip_probe_every: int = 0
    witness_mode: str = "dual"
    enrich: bool = False
    arms: list[ArmConfig] = field(default_factory=list)
    raw_toml: str = ""

    def __iter__(self):
        yield self
        yield self.arms


def load_config(path: Union[str, Path]) -> RunConfig:
    """Load and strictly validate an experiment TOML file using stdlib tomllib."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Experiment config file not found: {path}")

    raw_text = p.read_text(encoding="utf-8")
    data = tomllib.loads(raw_text)

    run_dict = data.get("run") or data.get("experiment") or {}
    for key, val in data.items():
        if key not in ("run", "experiment", "arms", "arm") and key not in run_dict:
            run_dict[key] = val

    # Validate global depth_pages
    raw_depth = run_dict.get("depth_pages")
    depth_pages = int(raw_depth) if raw_depth is not None else 1
    if depth_pages < 1:
        raise ValueError(f"Run config depth_pages must be >= 1, got {depth_pages}")

    # Validate global quota_cap_mb
    raw_cap = run_dict.get("quota_cap_mb")
    quota_cap_mb = float(raw_cap) if raw_cap is not None else 100.0
    if quota_cap_mb <= 0:
        raise ValueError(f"Run config quota_cap_mb must be > 0, got {quota_cap_mb}")

    raw_parallel = run_dict.get("max_parallel_sessions")
    max_parallel = int(raw_parallel) if raw_parallel is not None else 5
    if max_parallel < 1:
        raise ValueError(f"Run config max_parallel_sessions must be >= 1, got {max_parallel}")

    query_panel = str(run_dict.get("query_panel") or "queries/panel_all.csv")
    raw_price = run_dict.get("price_usd_per_gb")
    price = float(raw_price) if raw_price is not None else None
    if price is not None and (not math.isfinite(price) or price < 0):
        raise ValueError("price_usd_per_gb must be finite and nonnegative")

    run_cfg = RunConfig(
        id=str(run_dict.get("id") or ""),
        country=str(run_dict.get("country") or "US").upper(),
        query_panel=query_panel,
        depth_pages=depth_pages,
        quota_cap_mb=quota_cap_mb,
        price_usd_per_gb=price,
        stop_block_rate=float(run_dict.get("stop_block_rate", 0.5)),
        stop_proxy_error_rate=float(run_dict.get("stop_proxy_error_rate", 0.5)),
        seed=int(run_dict.get("seed") or 42),
        output_dir=str(run_dict.get("output_dir") or "results"),
        max_parallel_sessions=max_parallel,
        rate_limit_rps=float(run_dict.get("rate_limit_rps") or 0.0),
        ip_probe_every=int(run_dict.get("ip_probe_every") or 0),
        witness_mode=str(run_dict.get("witness_mode") or "dual"),
        enrich=bool(run_dict.get("enrich", False)),
        raw_toml=raw_text,
    )

    raw_arms = data.get("arms") or data.get("arm") or []
    if not raw_arms:
        raise ValueError(f"Experiment config must contain at least one arm under [[arms]], found none in {path}")

    # Validate top-level query panel if explicitly provided and not used by any non-ttl arm
    if "query_panel" in run_dict and not any(a.get("kind") != "ttl" or a.get("query_panel") for a in raw_arms):
        panel_p = Path(query_panel)
        if not panel_p.is_file():
            raise FileNotFoundError(f"Run config: query panel file not found: {query_panel}")
        if panel_p.stat().st_size == 0:
            raise ValueError(f"Run config: query panel file is empty: {query_panel}")
        try:
            panel_rows = load_panel(query_panel, country=run_cfg.country)
            if not panel_rows:
                raise ValueError(
                    f"Run config: query panel '{query_panel}' has no valid rows for country {run_cfg.country}"
                )
        except ValueError as exc:
            raise ValueError(f"Run config: {exc}") from exc

    arm_list: list[ArmConfig] = []
    for idx, raw_arm in enumerate(raw_arms):
        name = str(raw_arm.get("name") or f"arm_{idx+1}")

        # 1. Kind validation
        kind = str(raw_arm.get("kind") or "").strip().lower()
        if kind not in VALID_ARM_KINDS:
            raise ValueError(
                f"Arm '{name}': invalid kind {kind!r}. Must be one of {sorted(VALID_ARM_KINDS)}"
            )

        # 2. Pool validation
        pool = str(raw_arm.get("pool") or "residential").strip().lower()
        if pool not in VALID_POOLS:
            raise ValueError(
                f"Arm '{name}': invalid pool {pool!r}. Must be one of {sorted(VALID_POOLS)}"
            )

        # 3. Sessions validation: explicit 0 is an error, not coerced!
        raw_sessions = raw_arm.get("sessions")
        sessions = int(raw_sessions) if raw_sessions is not None else 1
        if sessions < 1:
            raise ValueError(f"Arm '{name}': sessions must be >= 1, got {sessions}")

        # 4. Depth pages validation
        arm_depth = None
        if "depth_pages" in raw_arm:
            arm_depth = int(raw_arm["depth_pages"])
            if arm_depth < 1:
                raise ValueError(f"Arm '{name}': depth_pages must be >= 1, got {arm_depth}")

        # 5. Positive durations and intervals validation
        if kind == "ttl":
            dur_min = float(raw_arm.get("duration_min") or 0.0)
            if dur_min <= 0:
                raise ValueError(f"Arm '{name}' (ttl): duration_min must be > 0, got {dur_min}")
            interval_s = float(raw_arm.get("probe_interval_s") or 20.0)
            if interval_s <= 0:
                raise ValueError(f"Arm '{name}' (ttl): probe_interval_s must be > 0, got {interval_s}")
            sesstime_m = int(raw_arm.get("sesstime_min") or 20)
            if sesstime_m < 1:
                raise ValueError(f"Arm '{name}' (ttl): sesstime_min must be >= 1, got {sesstime_m}")
        else:
            dur_min = float(raw_arm.get("duration_min") or 0.0)
            interval_s = float(raw_arm.get("probe_interval_s") or 20.0)
            sesstime_m = int(raw_arm.get("sesstime_min") or 20)

        if kind == "block_onset" and "requests_per_session" in raw_arm:
            reqs = int(raw_arm["requests_per_session"])
            if reqs < 1:
                raise ValueError(f"Arm '{name}' (block_onset): requests_per_session must be >= 1, got {reqs}")

        country_override = raw_arm.get("country")
        country = str(country_override).upper() if country_override else None
        effective_country = country or run_cfg.country

        # 6. Panel file validation: must exist and contain valid query rows
        panel_ref = raw_arm.get("query_panel")
        panel_to_check = panel_ref or query_panel
        if kind != "ttl" or panel_ref:
            panel_p = Path(panel_to_check)
            if not panel_p.is_file():
                raise FileNotFoundError(f"Arm '{name}': query panel file not found: {panel_to_check}")
            if panel_p.stat().st_size == 0:
                raise ValueError(f"Arm '{name}': query panel file is empty: {panel_to_check}")
            try:
                panel_rows = load_panel(panel_to_check, country=effective_country)
                if not panel_rows:
                    raise ValueError(
                        f"Arm '{name}': query panel '{panel_to_check}' has no valid rows for country {effective_country}"
                    )
            except ValueError as exc:
                raise ValueError(f"Arm '{name}': {exc}") from exc

        raw_local = raw_arm.get("local_repeats")
        local_repeats = int(raw_local) if raw_local is not None else 1
        include_local = bool(raw_arm.get("include_local", False))
        parallel_variance_queries = int(raw_arm.get("parallel_variance_queries", 1))
        if parallel_variance_queries < 1:
            raise ValueError(f"Arm '{name}': parallel_variance_queries must be >= 1")
        if include_local and local_repeats < 1:
            raise ValueError(f"Arm '{name}': local_repeats must be >= 1 when include_local is true, got {local_repeats}")

        arm = ArmConfig(
            name=name,
            kind=kind,
            pool=pool,
            country=country,
            sessions=sessions,
            sesstime_min=sesstime_m,
            strict_replace=bool(raw_arm.get("strict_replace", False)),
            sessasn_strict=bool(raw_arm.get("sessasn_strict", False)),
            sessipcollision_strict=bool(raw_arm.get("sessipcollision_strict", False)),
            query_panel=raw_arm.get("query_panel"),
            depth_pages=arm_depth,
            requests_per_session=int(raw_arm.get("requests_per_session") or 0),
            queries_per_session=int(raw_arm.get("queries_per_session") or 0),
            concurrency_per_session=int(raw_arm.get("concurrency_per_session") or 1),
            delay_s=float(raw_arm.get("delay_s") or 0.0),
            duration_min=dur_min,
            probe_interval_s=interval_s,
            same_ip_repeats=int(raw_arm.get("same_ip_repeats") or 2),
            new_ip_repeats=int(raw_arm.get("new_ip_repeats") or 2),
            forced_rotate_repeats=int(raw_arm.get("forced_rotate_repeats") or 0),
            local_repeats=local_repeats,
            repeat_delay_s=float(raw_arm.get("repeat_delay_s") or 60.0),
            parallel_variance_queries=parallel_variance_queries,
            load_levels=[int(x) for x in raw_arm.get("load_levels", [])],
            load_concurrency=[int(x) for x in raw_arm.get("load_concurrency", [])],
            max_pages=int(raw_arm.get("max_pages") or 0),
            stop_on_block=bool(raw_arm.get("stop_on_block", True)),
            variant=str(raw_arm.get("variant") or ""),
            pools=[str(p).strip().lower() for p in raw_arm.get("pools", [])],
            include_local=include_local,
        )
        arm_list.append(arm)

    run_cfg.arms = arm_list
    return run_cfg


def load_panel(path: Union[str, Path], country: Optional[str] = None) -> list[dict]:
    """Load query panel CSV and optionally filter by country."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Query panel file not found: {path}")

    rows: list[dict] = []
    with p.open(mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue
            q_text = (row.get("query") or "").strip()
            c_text = (row.get("country") or "").strip().upper()
            if not q_text or not c_text:
                continue

            # Validate country
            if len(c_text) != 2:
                raise ValueError(f"Invalid country code in panel {path}: {c_text!r}")

            if country and c_text != country.upper():
                continue

            try:
                min_res = int(row.get("expected_min_results") or 10)
            except ValueError:
                min_res = 10

            rows.append({
                "query": q_text,
                "country": c_text,
                "city": (row.get("city") or "").strip(),
                "expected_min_results": min_res,
                "category": (row.get("category") or "").strip(),
            })

    if not rows:
        raise ValueError(f"No queries found in panel {path}" + (f" for country {country}" if country else ""))

    return rows


def estimate_requests(config: Union[RunConfig, Tuple[RunConfig, list[ArmConfig]], str, Path, dict]) -> list[dict]:
    """Estimate Google requests and exit IP probe requests for dry-run."""
    if isinstance(config, (str, Path)):
        run_cfg = load_config(config)
        arm_list = run_cfg.arms
    elif isinstance(config, tuple):
        run_cfg = config[0]
        arm_list = config[1]
    elif isinstance(config, RunConfig):
        run_cfg = config
        arm_list = config.arms
    elif isinstance(config, dict):
        run_cfg = RunConfig(**{k: v for k, v in config.items() if k != "arms"})
        arm_list = config.get("arms", [])
    else:
        raise TypeError(f"Unsupported config type: {type(config)}")

    witnesses_per_probe = 5 if run_cfg.witness_mode == "multi" else 2
    estimates: list[dict] = []

    for arm in arm_list:
        kind = arm.kind
        pool = arm.pool
        depth = arm.depth_pages or run_cfg.depth_pages or 1
        query_reqs = 1 + depth
        probe_every = run_cfg.ip_probe_every
        # The final page breaks before the periodic probe; the end probe covers it.
        periodic_probes = depth // probe_every - 1 // probe_every if probe_every > 0 else 0
        query_probes = (2 + periodic_probes) * witnesses_per_probe
        sessions = arm.sessions

        google_reqs = 0
        probe_reqs = 0

        if kind == "ttl":
            # No Google traffic; only probes
            probes_per_session = int((arm.duration_min * 60) / max(1.0, arm.probe_interval_s)) + 1
            probe_reqs = sessions * probes_per_session * witnesses_per_probe
            google_reqs = 0

        elif kind == "block_onset":
            reqs_per_sess = arm.requests_per_session if arm.requests_per_session > 0 else 10
            google_reqs = sessions * reqs_per_sess
            # Start and end probes per session
            probe_reqs = sessions * 2 * witnesses_per_probe

        elif kind == "variance":
            n_local_runs = arm.local_repeats if (pool == "local" or arm.include_local) else 0
            n_repeats = arm.same_ip_repeats + arm.new_ip_repeats + arm.forced_rotate_repeats + n_local_runs
            panel_size = len(load_panel(arm.query_panel or run_cfg.query_panel, country=arm.country or run_cfg.country))
            n_queries = min(arm.queries_per_session or panel_size, panel_size)
            google_reqs = n_queries * n_repeats * query_reqs
            probe_reqs = n_queries * n_repeats * query_probes
            sessions = n_queries * (int(arm.same_ip_repeats > 0) + arm.new_ip_repeats + arm.forced_rotate_repeats + n_local_runs)

        elif kind == "load":
            lvls = arm.load_levels or [sessions]
            concs = arm.load_concurrency or [arm.concurrency_per_session]
            total_load_sessions = sum(lvl for lvl in lvls) * len(concs)
            reqs_per_sess = arm.requests_per_session if arm.requests_per_session > 0 else depth * 5
            google_reqs = total_load_sessions * reqs_per_sess
            probe_reqs = total_load_sessions * 2 * witnesses_per_probe
            sessions = total_load_sessions

        elif kind == "pool_compare":
            pool_set = arm.pools or ["residential", "mobile", "mixed"]
            if arm.include_local and "local" not in pool_set:
                pool_set = list(pool_set) + ["local"]
            panel_size = len(load_panel(arm.query_panel or run_cfg.query_panel, country=arm.country or run_cfg.country))
            n_queries = min(arm.queries_per_session or panel_size, panel_size)
            google_reqs = n_queries * len(pool_set) * query_reqs
            sess_count = n_queries * len(pool_set)
            probe_reqs = sess_count * query_probes
            sessions = sess_count

        total_reqs = google_reqs + probe_reqs
        est_wire_mb = round((google_reqs * 50_000 + probe_reqs * 1024) / (1024 * 1024), 3)

        estimates.append({
            "arm": arm.name,
            "kind": kind,
            "pool": pool,
            "sessions": sessions,
            "google_requests": google_reqs,
            "probe_requests": probe_reqs,
            "total_requests": total_reqs,
            "est_wire_mb": est_wire_mb,
        })

    return estimates


def _print_dry_run(run_cfg: RunConfig, estimates: list[dict]) -> None:
    """Print the dry-run execution plan."""
    console.rule(f"[bold cyan]Dry Run Plan: {run_cfg.id or 'Experiment'}")
    console.print(f"  Target Country : {run_cfg.country}")
    console.print(f"  Query Panel    : {run_cfg.query_panel}")
    console.print(f"  Quota Cap      : {run_cfg.quota_cap_mb} MB")
    console.print(f"  Max Parallel   : {run_cfg.max_parallel_sessions} sessions")
    console.print(f"  Witness Mode   : {run_cfg.witness_mode}")
    console.print()

    table = Table(title="Arm Request Estimates", show_header=True, header_style="bold magenta")
    table.add_column("Arm Name", style="cyan")
    table.add_column("Kind", style="white")
    table.add_column("Pool", style="green")
    table.add_column("Sessions", justify="right")
    table.add_column("Google Reqs", justify="right")
    table.add_column("Probe Reqs", justify="right")
    table.add_column("Total Reqs", justify="right")
    table.add_column("Est. Wire MB", justify="right")

    tot_google = 0
    tot_probe = 0
    tot_all = 0
    tot_mb = 0.0

    for est in estimates:
        tot_google += est["google_requests"]
        tot_probe += est["probe_requests"]
        tot_all += est["total_requests"]
        tot_mb += est["est_wire_mb"]
        table.add_row(
            est["arm"],
            est["kind"],
            est["pool"],
            str(est["sessions"]),
            str(est["google_requests"]),
            str(est["probe_requests"]),
            str(est["total_requests"]),
            f"{est['est_wire_mb']:.2f}",
        )

    table.add_section()
    table.add_row(
        "TOTAL",
        "",
        "",
        "",
        str(tot_google),
        str(tot_probe),
        str(tot_all),
        f"{tot_mb:.2f}",
        style="bold",
    )
    console.print(table)
    console.print("\n[yellow]Dry run only: no network calls made, no database written.[/yellow]\n")


def record_query_result(
    store: Store,
    run_id: str,
    session_id: str,
    arm_name: str,
    variant_name: str,
    result: QueryResult,
    query_index: int = 1,
) -> None:
    """Insert requests, places with rank, and IP observations into SQLite store."""
    # 1. Requests: attempts and final records
    records = result.attempts if result.attempts else result.requests
    for rec in records:
        outcome = rec.outcome or "other_error"
        block_type = outcome if outcome in BLOCK_OUTCOMES else ""
        store.insert_request({
            "run_id": run_id,
            "session_id": session_id,
            "ts": rec.ts or time.time(),
            "request_index": rec.request_index,
            "query_index": query_index,
            "query": result.query,
            "country": result.country,
            "kind": rec.kind,
            "page_start": rec.page_start,
            "url_hash": rec.response_hash or (hashlib.sha256(rec.url.encode("utf-8", errors="replace")).hexdigest()[:16] if rec.url else ""),
            "status": rec.status,
            "outcome": outcome,
            "block_type": block_type,
            "anyip_error": rec.anyip_error,
            "attempt": rec.attempt,
            "is_final": 1 if rec.is_final else 0,
            "latency_ms": rec.latency_ms,
            "bytes_wire": rec.bytes_wire,
            "bytes_rx": rec.bytes_rx,
            "content_encoding": rec.content_encoding,
            "content_type": rec.content_type,
            "server": rec.server,
            "error": rec.error,
            "arm": arm_name,
            "variant": variant_name,
        })

    # 2. Places with 1-based rank
    for rank, p in enumerate(result.places, start=1):
        store.insert_place({
            "run_id": run_id,
            "session_id": session_id,
            "query_index": query_index,
            "query": result.query,
            "country": result.country,
            "request_id": None,
            "rank": rank,
            "place_id": p.get("place_id") or "",
            "title": p.get("title") or "",
            "category": p.get("category") or "",
            "address": p.get("address") or "",
            "phone": p.get("phone") or "",
            "website": p.get("website") or "",
            "stars": str(p.get("stars") or ""),
            "reviews": str(p.get("reviews") or ""),
            "lat": p.get("lat"),
            "lng": p.get("lng"),
            "validation": p.get("validation") or "",
            "arm": arm_name,
            "variant": variant_name,
        })

    # 3. IP observations
    for obs in result.ip_observations:
        store.insert_ip_observation({
            "run_id": run_id,
            "session_id": session_id,
            "ts": obs.ts,
            "request_index": obs.request_index,
            "witness": obs.witness,
            "family": obs.family,
            "ip": obs.ip,
            "latency_ms": obs.latency_ms,
            "error": obs.error,
            "arm": arm_name,
            "variant": variant_name,
        })


def reconcile_session(
    store: Store,
    run_id: str,
    session_id: str,
    arm_name: str,
    variant_name: str,
    pool: str,
    country: str,
    session_name: str,
    sesstime_min: int,
    started_at: float,
    ended_at: float,
    status: str,
    stop_reason: str,
    results: Sequence[QueryResult],
    extra_observations: Optional[Sequence[IpObservation]] = None,
    error: str = "",
    strict_replace: bool = False,
    sessasn_strict: bool = False,
    sessipcollision_strict: bool = False,
) -> None:
    """Calculate aggregate counters and reconcile session in store.
    
    Ensures:
    - first_block_request is cumulative across all queries in the session.
    - distinct_ips counts distinct primary IPs (v4 if present, else v6).
    - requests (attempts) and google_requests (final) reconcile exactly with DB.
    """
    total_req_attempts = 0
    google_requests = 0
    places_count = 0
    block_events = 0
    first_block_req: Optional[int] = None
    first_block_ts: Optional[float] = None
    traffic_bytes = 0

    all_obs: list[IpObservation] = []
    if extra_observations:
        all_obs.extend(extra_observations)

    req_offset = 0
    for r in results:
        attempts_len = len(r.attempts) if r.attempts else len(r.requests)
        total_req_attempts += attempts_len
        google_requests += len(r.requests)
        places_count += len(r.places)
        traffic_bytes += r.bytes_wire_total + r.bytes_tx_est_total + r.probe_bytes_billed

        # Cumulative first_block_request across queries in the session
        for req in r.requests:
            if req.outcome in BLOCK_OUTCOMES:
                block_events += 1
                if first_block_req is None:
                    first_block_req = req_offset + req.request_index
                    first_block_ts = req.ts

        req_offset += len(r.requests)
        all_obs.extend(r.ip_observations)

    # Distinct primary IPs: IPv4 if any IPv4 exists, else IPv6
    v4_ips = {o.ip for o in all_obs if o.family == "v4" and o.ip}
    v6_ips = {o.ip for o in all_obs if o.family == "v6" and o.ip}
    primary_ips = v4_ips if v4_ips else v6_ips
    distinct_primary_count = len(primary_ips)

    all_observed_ips = sorted({o.ip for o in all_obs if o.ip})

    store.upsert_session({
        "run_id": run_id,
        "session_id": session_id,
        "arm": arm_name,
        "variant": variant_name,
        "pool": pool,
        "country": country,
        "session_name": session_name,
        "strict_replace": int(strict_replace),
        "sessasn_strict": int(sessasn_strict),
        "sessipcollision_strict": int(sessipcollision_strict),
        "sesstime_min": sesstime_min,
        "started_at": started_at,
        "ended_at": ended_at,
        "requests": total_req_attempts if results else len(all_obs),
        "google_requests": google_requests,
        "places": places_count,
        "block_events": block_events,
        "first_block_request": first_block_req,
        "first_block_ts": first_block_ts,
        "status": status,
        "stop_reason": stop_reason,
        "distinct_ips": distinct_primary_count,
        "ip_set_json": json.dumps(all_observed_ips),
        "asn_set_json": json.dumps([]),
        "city_set_json": json.dumps([]),
        "traffic_bytes": traffic_bytes if results else len(all_obs) * 1024,
        "error": error,
    })


class SessionWorker:
    """Worker executing individual sessions for TTL, Block Onset, and Load arms."""

    def __init__(
        self,
        store: Store,
        run_id: str,
        run_config: RunConfig,
        stop_event: asyncio.Event,
        quota_exceeded: Optional[asyncio.Event] = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.run_config = run_config
        self.stop_event = stop_event
        self.quota_exceeded = quota_exceeded or asyncio.Event()

    async def run_ttl_session(self, arm: ArmConfig, session_idx: int) -> dict:
        """Run one TTL stability session: probes exit IP over time without Google traffic.
        
        Evaluates primary family continuity (IPv4 if any IPv4 exists, else IPv6).
        Does not falsely report an IP change across v4 vs v6 witnesses.
        """
        session_id = uuid.uuid4().hex[:12]
        country = (arm.country or self.run_config.country).upper()
        pool = arm.pool or "residential"
        is_local = (pool == "local")
        use_proxy = False if is_local else PROXY.is_configured()
        proxy_type = None if is_local else (pool if use_proxy else None)

        started_at = time.time()
        self.store.upsert_session({
            "run_id": self.run_id,
            "session_id": session_id,
            "arm": arm.name,
            "variant": arm.variant or "ttl_default",
            "pool": pool,
            "country": country,
            "session_name": session_id,
            "strict_replace": int(arm.strict_replace),
            "sessasn_strict": int(arm.sessasn_strict),
            "sessipcollision_strict": int(arm.sessipcollision_strict),
            "sesstime_min": arm.sesstime_min,
            "created_at": started_at,
            "started_at": started_at,
            "status": "running",
        })

        duration_s = max(1.0, arm.duration_min * 60)
        sesstime_s = max(1.0, arm.sesstime_min * 60)
        probe_interval_s = max(1.0, arm.probe_interval_s)

        all_obs: list[IpObservation] = []
        primary_family: Optional[str] = None
        first_primary_ip: Optional[str] = None
        first_primary_ts: Optional[float] = None
        primary_ips_seen: Set[str] = set()

        ip_changed = False
        change_event_type = ""
        t0 = time.time()
        probe_idx = 0

        async with ProxySession(
            country=country,
            use_proxy=use_proxy,
            proxy_type=proxy_type,
            session_duration_min=arm.sesstime_min,
            session_id=session_id,
            strict_session=arm.strict_replace,
            sessasn_strict=arm.sessasn_strict,
            sessipcollision_strict=arm.sessipcollision_strict,
        ) as proxy_session:
            while (time.time() - t0 < duration_s) and not self.stop_event.is_set():
                obs_list = await _exit_ips(proxy_session, request_index=probe_idx, mode=self.run_config.witness_mode)
                all_obs.extend(obs_list)

                for obs in obs_list:
                    self.store.insert_ip_observation({
                        "run_id": self.run_id,
                        "session_id": session_id,
                        "ts": obs.ts,
                        "request_index": obs.request_index,
                        "witness": obs.witness,
                        "family": obs.family,
                        "ip": obs.ip,
                        "latency_ms": obs.latency_ms,
                        "error": obs.error,
                        "arm": arm.name,
                        "variant": arm.variant or "ttl_default",
                    })

                # Determine primary family (IPv4 if present, else IPv6)
                valid_v4 = [o for o in obs_list if o.family == "v4" and o.ip]
                valid_v6 = [o for o in obs_list if o.family == "v6" and o.ip]

                if primary_family is None:
                    if valid_v4:
                        primary_family = "v4"
                        first_primary_ip = valid_v4[0].ip
                        first_primary_ts = valid_v4[0].ts
                        primary_ips_seen.add(first_primary_ip)
                    elif valid_v6:
                        primary_family = "v6"
                        first_primary_ip = valid_v6[0].ip
                        first_primary_ts = valid_v6[0].ts
                        primary_ips_seen.add(first_primary_ip)

                # Check for change ONLY within the primary family
                if primary_family and first_primary_ip and not ip_changed:
                    current_primary_obs = valid_v4 if primary_family == "v4" else valid_v6
                    for obs in current_primary_obs:
                        primary_ips_seen.add(obs.ip)
                        if obs.ip != first_primary_ip:
                            ip_changed = True
                            elapsed = obs.ts - (first_primary_ts or t0)
                            change_event_type = "ttl_expiry" if elapsed >= sesstime_s else "ip_change"
                            self.store.insert_event({
                                "run_id": self.run_id,
                                "session_id": session_id,
                                "ts": obs.ts,
                                "request_index": probe_idx,
                                "event_type": change_event_type,
                                "detail_json": json.dumps({
                                    "family": primary_family,
                                    "old_ip": first_primary_ip,
                                    "new_ip": obs.ip,
                                    "elapsed_s": round(elapsed, 2),
                                    "sesstime_s": sesstime_s,
                                }),
                                "arm": arm.name,
                                "variant": arm.variant or "ttl_default",
                            })
                            break

                if ip_changed or self.stop_event.is_set():
                    break

                probe_idx += 1
                sleep_elapsed = 0.0
                while sleep_elapsed < probe_interval_s and not self.stop_event.is_set() and (time.time() - t0 < duration_s):
                    chunk = min(0.5, probe_interval_s - sleep_elapsed)
                    await asyncio.sleep(chunk)
                    sleep_elapsed += chunk

        # Determine definitive stop reason and status
        if self.quota_exceeded.is_set():
            stop_reason = "quota"
            sess_status = "quota"
        elif self.stop_event.is_set():
            stop_reason = "stopped"
            sess_status = "stopped"
        elif ip_changed:
            stop_reason = change_event_type
            sess_status = "completed"
        else:
            stop_reason = "limit"
            sess_status = "completed"

        ended_at = time.time()
        distinct_primary_count = len(primary_ips_seen)
        all_observed_ips = sorted({o.ip for o in all_obs if o.ip})

        self.store.update_session(
            session_id=session_id,
            run_id=self.run_id,
            ended_at=ended_at,
            requests=len(all_obs),
            google_requests=0,
            places=0,
            block_events=0,
            first_block_request=None,
            first_block_ts=None,
            status=sess_status,
            stop_reason=stop_reason,
            distinct_ips=distinct_primary_count,
            ip_set_json=json.dumps(all_observed_ips),
            asn_set_json=json.dumps([]),
            city_set_json=json.dumps([]),
            traffic_bytes=len(all_obs) * 1024,
            error="",
        )
        return {"session_id": session_id, "status": sess_status, "stop_reason": stop_reason}

    async def run_block_onset_session(
        self,
        arm: ArmConfig,
        session_idx: int,
        panel: list[dict],
    ) -> dict:
        """Run one block onset session: repeatedly scrape queries until block or request budget."""
        session_id = uuid.uuid4().hex[:12]
        country = (arm.country or self.run_config.country).upper()
        pool = arm.pool or "residential"
        is_local = (pool == "local")
        use_proxy = False if is_local else PROXY.is_configured()
        proxy_type = None if is_local else (pool if use_proxy else None)

        started_at = time.time()
        variant_name = arm.variant or "onset_default"

        self.store.upsert_session({
            "run_id": self.run_id,
            "session_id": session_id,
            "arm": arm.name,
            "variant": variant_name,
            "pool": pool,
            "country": country,
            "session_name": session_id,
            "strict_replace": int(arm.strict_replace),
            "sessasn_strict": int(arm.sessasn_strict),
            "sessipcollision_strict": int(arm.sessipcollision_strict),
            "sesstime_min": arm.sesstime_min,
            "created_at": started_at,
            "started_at": started_at,
            "status": "running",
        })

        budget = arm.requests_per_session if arm.requests_per_session > 0 else 10
        depth = arm.depth_pages or self.run_config.depth_pages or 1
        results: list[QueryResult] = []
        stop_reason = "limit"
        first_block_found = False

        query_cursor = session_idx % len(panel)
        google_req_count = 0

        while google_req_count < budget and not self.stop_event.is_set():
            q_data = panel[query_cursor % len(panel)]
            query_cursor += 1

            res = await scrape_query(
                query=q_data["query"],
                country=country,
                limit=depth * 20,
                use_proxy=use_proxy,
                proxy_type=proxy_type,
                session_duration_min=arm.sesstime_min,
                sticky=True,
                strict_session=arm.strict_replace,
                session_id=session_id,
                session_name=session_id,
                max_pages=depth,
                ip_probe_every=self.run_config.ip_probe_every,
                witness_mode=self.run_config.witness_mode,
                sessasn_strict=arm.sessasn_strict,
                sessipcollision_strict=arm.sessipcollision_strict,
                expected_min_results=int(q_data.get("expected_min_results", 10)),
            )
            results.append(res)
            record_query_result(
                self.store,
                self.run_id,
                session_id,
                arm.name,
                variant_name,
                res,
                query_index=len(results),
            )
            google_req_count += len(res.requests)

            if res.block_count > 0 or res.stop_reason == "block":
                first_block_found = True
                stop_reason = "block"
                block_outc = res.requests[-1].outcome if res.requests else "block"
                self.store.insert_event({
                    "run_id": self.run_id,
                    "session_id": session_id,
                    "ts": time.time(),
                    "request_index": res.block_at_request or 1,
                    "event_type": "block",
                    "detail_json": json.dumps({"block_type": block_outc, "query": res.query}),
                    "arm": arm.name,
                    "variant": variant_name,
                })
                if arm.stop_on_block:
                    break

            # Documented overshoot policy:
            # Google requests are executed in query chunks (1 SPA request + pages, typically 2 requests for depth=1).
            # The session stops as soon as google_req_count >= budget, allowing at most one query overshoot
            # (e.g. 6 requests for a budget of 5 when each query executes 2 requests).
            if google_req_count >= budget:
                break

        if self.quota_exceeded.is_set():
            stop_reason = "quota"
            sess_status = "quota"
        elif self.stop_event.is_set() and not first_block_found:
            stop_reason = "stopped"
            sess_status = "stopped"
        else:
            sess_status = "completed"

        ended_at = time.time()
        reconcile_session(
            self.store,
            self.run_id,
            session_id,
            arm.name,
            variant_name,
            pool,
            country,
            session_id,
            arm.sesstime_min,
            started_at,
            ended_at,
            status=sess_status,
            stop_reason=stop_reason,
            results=results,
            strict_replace=arm.strict_replace,
            sessasn_strict=arm.sessasn_strict,
            sessipcollision_strict=arm.sessipcollision_strict,
        )
        return {"session_id": session_id, "status": sess_status, "stop_reason": stop_reason}

    async def run_load_session(
        self,
        arm: ArmConfig,
        session_idx: int,
        variant_name: str,
        concurrency: int,
        panel: list[dict],
    ) -> dict:
        """Run one load test session under specified concurrency and variant."""
        session_id = uuid.uuid4().hex[:12]
        country = (arm.country or self.run_config.country).upper()
        pool = arm.pool or "residential"
        is_local = (pool == "local")
        use_proxy = False if is_local else PROXY.is_configured()
        proxy_type = None if is_local else (pool if use_proxy else None)

        started_at = time.time()
        duration_s = (arm.duration_min * 60) if arm.duration_min > 0 else 60.0
        budget = arm.requests_per_session if arm.requests_per_session > 0 else 50
        depth = arm.depth_pages or self.run_config.depth_pages or 1

        self.store.upsert_session({
            "run_id": self.run_id,
            "session_id": session_id,
            "arm": arm.name,
            "variant": variant_name,
            "pool": pool,
            "country": country,
            "session_name": session_id,
            "strict_replace": int(arm.strict_replace),
            "sessasn_strict": int(arm.sessasn_strict),
            "sessipcollision_strict": int(arm.sessipcollision_strict),
            "sesstime_min": arm.sesstime_min,
            "created_at": started_at,
            "started_at": started_at,
            "status": "running",
        })

        results: list[QueryResult] = []
        lock = asyncio.Lock()
        google_req_count = 0
        stop_reason = "limit"

        async def worker_loop(w_idx: int):
            nonlocal google_req_count, stop_reason
            cursor = session_idx * 10 + w_idx
            while not self.stop_event.is_set():
                async with lock:
                    if google_req_count >= budget or (time.time() - started_at >= duration_s):
                        break
                    q_data = panel[cursor % len(panel)]
                    cursor += 1

                res = await scrape_query(
                    query=q_data["query"],
                    country=country,
                    limit=depth * 20,
                    use_proxy=use_proxy,
                    proxy_type=proxy_type,
                    session_duration_min=arm.sesstime_min,
                    sticky=True,
                    strict_session=arm.strict_replace,
                    session_id=session_id,
                    session_name=session_id,
                    max_pages=depth,
                    ip_probe_every=self.run_config.ip_probe_every,
                    witness_mode=self.run_config.witness_mode,
                    sessasn_strict=arm.sessasn_strict,
                    sessipcollision_strict=arm.sessipcollision_strict,
                    expected_min_results=int(q_data.get("expected_min_results", 10)),
                )

                async with lock:
                    results.append(res)
                    google_req_count += len(res.requests)
                    record_query_result(
                        self.store,
                        self.run_id,
                        session_id,
                        arm.name,
                        variant_name,
                        res,
                        query_index=len(results),
                    )
                    if res.block_count > 0:
                        stop_reason = "block"

        workers = [worker_loop(w) for w in range(max(1, concurrency))]
        async with asyncio.TaskGroup() as group:
            for worker_coro in workers:
                group.create_task(worker_coro)

        ended_at = time.time()
        if self.quota_exceeded.is_set():
            stop_reason = "quota"
            sess_status = "quota"
        elif self.stop_event.is_set():
            stop_reason = "stopped"
            sess_status = "stopped"
        else:
            sess_status = "completed"

        reconcile_session(
            self.store,
            self.run_id,
            session_id,
            arm.name,
            variant_name,
            pool,
            country,
            session_id,
            arm.sesstime_min,
            started_at,
            ended_at,
            status=sess_status,
            stop_reason=stop_reason,
            results=results,
            strict_replace=arm.strict_replace,
            sessasn_strict=arm.sessasn_strict,
            sessipcollision_strict=arm.sessipcollision_strict,
        )
        return {"session_id": session_id, "status": sess_status}


async def run_variance(
    store: Store,
    run_id: str,
    run_config: RunConfig,
    arm: ArmConfig,
    stop_event: asyncio.Event,
    quota_exceeded: Optional[asyncio.Event] = None,
) -> list[dict]:
    """Execute variance arm: same-IP repeats, new-IP runs, local baseline, and optional forced_rotate."""
    country = (arm.country or run_config.country).upper()
    panel_path = arm.query_panel or run_config.query_panel
    panel = load_panel(panel_path, country=country)
    pool = arm.pool or "residential"
    is_local_pool = (pool == "local")
    depth = arm.depth_pages or run_config.depth_pages or 1

    n_queries = arm.queries_per_session or len(panel)
    queries_to_test = panel[:n_queries]

    outcomes: list[dict] = []

    async def run_query_repeats(q_idx, q_data):
        if stop_event.is_set():
            return

        query = q_data["query"]

        # ── 1. Same-IP repeats ────────────────────────────────────────────────
        base_proxy_sess = uuid.uuid4().hex[:12]
        for rep in range(arm.same_ip_repeats):
            if stop_event.is_set():
                break

            store_sess_id = f"var_same_{base_proxy_sess}_{rep+1}"
            started_at = time.time()

            store.upsert_session({
                "run_id": run_id,
                "session_id": store_sess_id,
                "arm": arm.name,
                "variant": "same_ip",
                "pool": pool,
                "country": country,
                "session_name": base_proxy_sess,
                "strict_replace": int(arm.strict_replace),
                "sessasn_strict": int(arm.sessasn_strict),
                "sessipcollision_strict": int(arm.sessipcollision_strict),
                "sesstime_min": arm.sesstime_min,
                "created_at": started_at,
                "started_at": started_at,
                "status": "running",
            })

            res = await scrape_query(
                query=query,
                country=country,
                limit=depth * 20,
                use_proxy=False if is_local_pool else PROXY.is_configured(),
                proxy_type=None if is_local_pool else pool,
                session_duration_min=arm.sesstime_min,
                sticky=True,
                strict_session=arm.strict_replace,
                session_id=base_proxy_sess,
                session_name=base_proxy_sess,
                max_pages=depth,
                ip_probe_every=run_config.ip_probe_every,
                witness_mode=run_config.witness_mode,
                expected_min_results=int(q_data.get("expected_min_results", 10)),
            )

            record_query_result(store, run_id, store_sess_id, arm.name, "same_ip", res, query_index=q_idx+1)

            if quota_exceeded and quota_exceeded.is_set():
                stop_r = "quota"
                s_stat = "quota"
            elif stop_event.is_set():
                stop_r = "stopped"
                s_stat = "stopped"
            else:
                stop_r = res.stop_reason or "limit"
                s_stat = "completed"

            reconcile_session(
                store,
                run_id,
                store_sess_id,
                arm.name,
                "same_ip",
                pool,
                country,
                base_proxy_sess,
                arm.sesstime_min,
                started_at,
                time.time(),
                status=s_stat,
                stop_reason=stop_r,
                results=[res],
            )
            outcomes.append({"variant": "same_ip", "session_id": store_sess_id, "query": query})

            if rep < arm.same_ip_repeats - 1 and not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=arm.repeat_delay_s)
                except TimeoutError:
                    pass

        # ── 2. New-IP runs ───────────────────────────────────────────────────
        for rep in range(arm.new_ip_repeats):
            if stop_event.is_set():
                break

            new_proxy_sess = uuid.uuid4().hex[:12]
            store_sess_id = f"var_new_{new_proxy_sess}"
            started_at = time.time()

            store.upsert_session({
                "run_id": run_id,
                "session_id": store_sess_id,
                "arm": arm.name,
                "variant": "new_ip",
                "pool": pool,
                "country": country,
                "session_name": new_proxy_sess,
                "strict_replace": int(arm.strict_replace),
                "sessasn_strict": int(arm.sessasn_strict),
                "sessipcollision_strict": int(arm.sessipcollision_strict),
                "sesstime_min": arm.sesstime_min,
                "created_at": started_at,
                "started_at": started_at,
                "status": "running",
            })

            res = await scrape_query(
                query=query,
                country=country,
                limit=depth * 20,
                use_proxy=False if is_local_pool else PROXY.is_configured(),
                proxy_type=None if is_local_pool else pool,
                session_duration_min=arm.sesstime_min,
                sticky=True,
                strict_session=arm.strict_replace,
                session_id=new_proxy_sess,
                session_name=new_proxy_sess,
                max_pages=depth,
                ip_probe_every=run_config.ip_probe_every,
                witness_mode=run_config.witness_mode,
                expected_min_results=int(q_data.get("expected_min_results", 10)),
            )

            record_query_result(store, run_id, store_sess_id, arm.name, "new_ip", res, query_index=q_idx+1)

            if quota_exceeded and quota_exceeded.is_set():
                stop_r = "quota"
                s_stat = "quota"
            elif stop_event.is_set():
                stop_r = "stopped"
                s_stat = "stopped"
            else:
                stop_r = res.stop_reason or "limit"
                s_stat = "completed"

            reconcile_session(
                store,
                run_id,
                store_sess_id,
                arm.name,
                "new_ip",
                pool,
                country,
                new_proxy_sess,
                arm.sesstime_min,
                started_at,
                time.time(),
                status=s_stat,
                stop_reason=stop_r,
                results=[res],
            )
            outcomes.append({"variant": "new_ip", "session_id": store_sess_id, "query": query})

        # ── 3. Forced rotate runs (if configured) ────────────────────────────
        for rep in range(arm.forced_rotate_repeats):
            if stop_event.is_set():
                break

            forced_sess = uuid.uuid4().hex[:12]
            store_sess_id = f"var_forced_{forced_sess}"
            started_at = time.time()

            store.upsert_session({
                "run_id": run_id,
                "session_id": store_sess_id,
                "arm": arm.name,
                "variant": "forced_rotate",
                "pool": pool,
                "country": country,
                "session_name": forced_sess,
                "strict_replace": 0,
                "sessasn_strict": 0,
                "sessipcollision_strict": 0,
                "sesstime_min": 0,
                "created_at": started_at,
                "started_at": started_at,
                "status": "running",
            })

            res = await scrape_query(
                query=query,
                country=country,
                limit=depth * 20,
                use_proxy=False if is_local_pool else PROXY.is_configured(),
                proxy_type=None if is_local_pool else pool,
                sticky=False,
                force_close=True,
                session_id=forced_sess,
                session_name=forced_sess,
                max_pages=depth,
                ip_probe_every=run_config.ip_probe_every,
                witness_mode=run_config.witness_mode,
                expected_min_results=int(q_data.get("expected_min_results", 10)),
            )

            record_query_result(store, run_id, store_sess_id, arm.name, "forced_rotate", res, query_index=q_idx+1)

            if quota_exceeded and quota_exceeded.is_set():
                stop_r = "quota"
                s_stat = "quota"
            elif stop_event.is_set():
                stop_r = "stopped"
                s_stat = "stopped"
            else:
                stop_r = res.stop_reason or "limit"
                s_stat = "completed"

            reconcile_session(
                store,
                run_id,
                store_sess_id,
                arm.name,
                "forced_rotate",
                pool,
                country,
                forced_sess,
                0,
                started_at,
                time.time(),
                status=s_stat,
                stop_reason=stop_r,
                results=[res],
            )
            outcomes.append({"variant": "forced_rotate", "session_id": store_sess_id, "query": query})

        # ── 4. Local runs (never uses proxy, proxy_type=None) ─────────────────
        if (pool == "local" or arm.include_local) and not stop_event.is_set():
            n_local = max(1, arm.local_repeats)
            for rep in range(n_local):
                if stop_event.is_set():
                    break
                local_sess_id = f"var_local_{uuid.uuid4().hex[:8]}_{rep+1}"
                started_at = time.time()

                store.upsert_session({
                    "run_id": run_id,
                    "session_id": local_sess_id,
                    "arm": arm.name,
                    "variant": "local",
                    "pool": "local",
                    "country": country,
                    "session_name": local_sess_id,
                    "strict_replace": 0,
                    "sessasn_strict": 0,
                    "sessipcollision_strict": 0,
                    "sesstime_min": 0,
                    "created_at": started_at,
                    "started_at": started_at,
                    "status": "running",
                })

                res = await scrape_query(
                    query=query,
                    country=country,
                    limit=depth * 20,
                    use_proxy=False,
                    proxy_type=None,
                    session_duration_min=0,
                    sticky=False,
                    session_id=local_sess_id,
                    session_name=local_sess_id,
                    max_pages=depth,
                    expected_min_results=int(q_data.get("expected_min_results", 10)),
                )

                record_query_result(store, run_id, local_sess_id, arm.name, "local", res, query_index=q_idx+1)

                if quota_exceeded and quota_exceeded.is_set():
                    stop_r = "quota"
                    s_stat = "quota"
                elif stop_event.is_set():
                    stop_r = "stopped"
                    s_stat = "stopped"
                else:
                    stop_r = res.stop_reason or "limit"
                    s_stat = "completed"

                reconcile_session(
                    store,
                    run_id,
                    local_sess_id,
                    arm.name,
                    "local",
                    "local",
                    country,
                    local_sess_id,
                    0,
                    started_at,
                    time.time(),
                    status=s_stat,
                    stop_reason=stop_r,
                    results=[res],
                )
                outcomes.append({"variant": "local", "session_id": local_sess_id, "query": query})

    width = arm.parallel_variance_queries
    for start in range(0, len(queries_to_test), width):
        if stop_event.is_set():
            break
        if width == 1:
            await run_query_repeats(start, queries_to_test[start])
        else:
            async with asyncio.TaskGroup() as group:
                for idx in range(start, min(start + width, len(queries_to_test))):
                    group.create_task(run_query_repeats(idx, queries_to_test[idx]))
    return outcomes


async def run_pool_compare(
    store: Store,
    run_id: str,
    run_config: RunConfig,
    arm: ArmConfig,
    stop_event: asyncio.Event,
    quota_exceeded: Optional[asyncio.Event] = None,
) -> list[dict]:
    """Execute pool comparison arm: run each query across configured pools."""
    country = (arm.country or run_config.country).upper()
    panel_path = arm.query_panel or run_config.query_panel
    panel = load_panel(panel_path, country=country)
    depth = arm.depth_pages or run_config.depth_pages or 1

    pools_to_test = arm.pools or ["residential", "mobile", "mixed"]
    if arm.include_local and "local" not in pools_to_test:
        pools_to_test = list(pools_to_test) + ["local"]

    n_queries = arm.queries_per_session or len(panel)
    queries_to_test = panel[:n_queries]

    outcomes: list[dict] = []

    for q_idx, q_data in enumerate(queries_to_test):
        if stop_event.is_set():
            break

        query = q_data["query"]

        for pool_name in pools_to_test:
            if stop_event.is_set():
                break

            is_local = (pool_name == "local")
            use_proxy = False if is_local else PROXY.is_configured()
            proxy_type = None if is_local else pool_name

            proxy_sess_id = uuid.uuid4().hex[:12]
            store_sess_id = f"pool_{pool_name}_{proxy_sess_id}"
            started_at = time.time()

            store.upsert_session({
                "run_id": run_id,
                "session_id": store_sess_id,
                "arm": arm.name,
                "variant": pool_name,
                "pool": pool_name,
                "country": country,
                "session_name": proxy_sess_id,
                "strict_replace": int(arm.strict_replace),
                "sessasn_strict": int(arm.sessasn_strict),
                "sessipcollision_strict": int(arm.sessipcollision_strict),
                "sesstime_min": arm.sesstime_min,
                "created_at": started_at,
                "started_at": started_at,
                "status": "running",
            })

            res = await scrape_query(
                query=query,
                country=country,
                limit=depth * 20,
                use_proxy=use_proxy,
                proxy_type=proxy_type,
                session_duration_min=arm.sesstime_min,
                sticky=True,
                strict_session=arm.strict_replace,
                session_id=proxy_sess_id,
                session_name=proxy_sess_id,
                max_pages=depth,
                ip_probe_every=run_config.ip_probe_every,
                witness_mode=run_config.witness_mode,
                expected_min_results=int(q_data.get("expected_min_results", 10)),
            )

            record_query_result(store, run_id, store_sess_id, arm.name, pool_name, res, query_index=q_idx+1)

            if quota_exceeded and quota_exceeded.is_set():
                stop_r = "quota"
                s_stat = "quota"
            elif stop_event.is_set():
                stop_r = "stopped"
                s_stat = "stopped"
            else:
                stop_r = res.stop_reason or "limit"
                s_stat = "completed"

            reconcile_session(
                store,
                run_id,
                store_sess_id,
                arm.name,
                pool_name,
                pool_name,
                country,
                proxy_sess_id,
                arm.sesstime_min,
                started_at,
                time.time(),
                status=s_stat,
                stop_reason=stop_r,
                results=[res],
            )
            outcomes.append({"pool": pool_name, "session_id": store_sess_id, "query": query})

    return outcomes


def build_manifest(run_config: RunConfig, quota_before: dict, quota_after: dict) -> dict:
    """Build run manifest containing environment details, versions, and config."""
    # Masked env key names only — never expose secrets/values
    masked_env_keys = sorted(os.environ.keys())

    py_version = sys.version
    pkg_versions: dict[str, str] = {}
    for pkg in ["aiohttp", "rich", "pytest"]:
        try:
            pkg_versions[pkg] = importlib.metadata.version(pkg)
        except Exception:
            pkg_versions[pkg] = "unknown"

    git_hash = None
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            git_hash = res.stdout.strip()
    except Exception:
        git_hash = None

    return {
        "run_id": run_config.id,
        "config_toml": run_config.raw_toml,
        "env_keys": masked_env_keys,
        "python_version": py_version,
        "package_versions": pkg_versions,
        "git_hash": git_hash,
        "quota_before": quota_before,
        "quota_after": quota_after,
        "created_at_iso": datetime.now(timezone.utc).isoformat(),
    }


async def _quota_monitor_task(
    stop_event: asyncio.Event,
    quota_exceeded: asyncio.Event,
    cap_mb: float,
    start_consumption_bytes: int,
    interval_s: float = 60.0,
) -> None:
    """Periodic background task monitoring AnyIP quota."""
    if not PROXY.has_api():
        return

    cap_bytes = cap_mb * 1024 * 1024
    min_remaining_bytes = 200 * 1024 * 1024  # 200 MB threshold

    while not stop_event.is_set():
        try:
            await asyncio.sleep(interval_s)
            if stop_event.is_set():
                break

            usage = await quota.get_usage()
            quota_total = usage.get("quota_bytes", 0)
            current_consumption = usage.get("consumption_bytes", 0)
            remaining = usage.get("remaining_bytes", quota_total - current_consumption)

            session_spent = current_consumption - start_consumption_bytes

            if cap_bytes > 0 and session_spent >= cap_bytes:
                logger.warning(
                    f"Quota monitor: reached run quota cap of {cap_mb} MB ({session_spent / (1024*1024):.1f} MB spent). Stopping."
                )
                console.print(f"\n[red]⚠ Quota cap of {cap_mb} MB reached. Halting workers.[/red]\n")
                quota_exceeded.set()
                stop_event.set()
                break

            if remaining < min_remaining_bytes:
                logger.warning(
                    f"Quota monitor: under 200 MB remaining ({remaining / (1024*1024):.1f} MB). Stopping."
                )
                console.print("\n[red]⚠ Critical: Under 200 MB quota remaining. Halting workers.[/red]\n")
                quota_exceeded.set()
                stop_event.set()
                break

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"Quota check skipped: {exc}")


async def _run_experiment_async(
    config_path: Union[str, Path],
    *,
    dry_run: bool = False,
    db_path: Optional[Union[str, Path]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    run_id: Optional[str] = None,
    quota_cap_mb: Optional[float] = None,
) -> dict:
    """Main asynchronous execution pipeline for experiments."""
    # Fail fast inside load_config before creating any output directory or DB
    run_cfg = load_config(config_path)

    if quota_cap_mb is not None:
        if quota_cap_mb <= 0:
            raise ValueError(f"quota_cap_mb override must be > 0, got {quota_cap_mb}")
        run_cfg.quota_cap_mb = quota_cap_mb

    if run_id:
        run_cfg.id = run_id
    if not run_cfg.id:
        run_cfg.id = f"exp_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    estimates = estimate_requests(run_cfg)

    if dry_run:
        _print_dry_run(run_cfg, estimates)
        return {
            "plan": estimates,
            "config": asdict(run_cfg),
            "dry_run": True,
            "run_id": run_cfg.id,
        }

    needs_proxy = any(
        any(pool != "local" for pool in (arm.pools or ["residential", "mobile", "mixed"]))
        if arm.kind == "pool_compare" else arm.pool != "local"
        for arm in run_cfg.arms
    )
    if needs_proxy and not PROXY.is_configured():
        raise ValueError("AnyIP proxy credentials are required for non-local experiment arms")

    # Setup directories and DB path ONLY after config is validated and dry_run is false
    out_base = Path(output_dir or run_cfg.output_dir)
    run_dir = out_base / run_cfg.id

    actual_db_path = str(run_dir / "run.db") if db_path is None else str(db_path)

    # Initial quota check
    quota_before: dict = {}
    start_consumption = 0
    if PROXY.has_api():
        try:
            quota_before = await quota.get_usage()
            start_consumption = quota_before.get("consumption_bytes", 0)
        except Exception as exc:
            if needs_proxy:
                raise RuntimeError("Could not verify AnyIP quota; experiment not started") from exc
            logger.warning(f"Initial quota check failed: {exc}")
    if needs_proxy:
        remaining = quota_before.get("remaining_bytes", quota_before.get("quota_bytes", 0) - start_consumption)
        if remaining < 200 * 1024**2 + run_cfg.quota_cap_mb * 1024**2:
            raise RuntimeError("Insufficient verified AnyIP quota for the run cap plus 200 MB reserve")

    # Initialize SQLite store
    run_dir.mkdir(parents=True, exist_ok=False)
    store = Store(actual_db_path)
    store.init()
    started_at = time.time()

    store.insert_run({
        "run_id": run_cfg.id,
        "started_at": started_at,
        "config_json": json.dumps(asdict(run_cfg), default=str),
        "manifest_json": "{}",
        "status": "running",
    })

    stop_event = asyncio.Event()
    quota_exceeded = asyncio.Event()

    # Setup SIGINT handler
    loop = asyncio.get_running_loop()
    installed_signal = False
    try:
        loop.add_signal_handler(signal.SIGINT, lambda: (
            console.print("\n[yellow]SIGINT received. Completing active requests...[/yellow]\n"),
            stop_event.set(),
        ))
        installed_signal = True
    except (NotImplementedError, RuntimeError):
        pass

    # Start quota monitor
    monitor_task = asyncio.create_task(
        _quota_monitor_task(stop_event, quota_exceeded, run_cfg.quota_cap_mb, start_consumption, interval_s=60.0)
    )

    console.rule(f"[bold cyan]Running Experiment: {run_cfg.id}")
    console.print(f"  Target Country : {run_cfg.country}")
    console.print(f"  Output Dir     : {run_dir}")
    console.print(f"  Database       : {actual_db_path}")
    console.print(f"  Arms to Run    : {len(run_cfg.arms)}")
    console.print()

    sem = asyncio.Semaphore(max(1, run_cfg.max_parallel_sessions))
    worker = SessionWorker(store, run_cfg.id, run_cfg, stop_event, quota_exceeded)
    guard = RequestGuard(stop_event, run_cfg.rate_limit_rps, run_cfg.stop_block_rate, run_cfg.stop_proxy_error_rate)
    token = request_guard.set(guard)
    failure = None

    async def run_workers(*coroutines):
        async with asyncio.TaskGroup() as group:
            for coroutine in coroutines:
                group.create_task(coroutine)

    try:
        for arm_idx, arm in enumerate(run_cfg.arms, start=1):
            if stop_event.is_set():
                break

            console.rule(f"[bold blue]Arm {arm_idx}/{len(run_cfg.arms)}: {arm.name} ({arm.kind})")

            if arm.kind == "ttl":
                async def bounded_ttl(s_idx: int):
                    async with sem:
                        return await worker.run_ttl_session(arm, s_idx)

                await run_workers(*(bounded_ttl(i) for i in range(arm.sessions)))

            elif arm.kind == "block_onset":
                panel = load_panel(arm.query_panel or run_cfg.query_panel, country=arm.country or run_cfg.country)

                async def bounded_block(s_idx: int):
                    async with sem:
                        return await worker.run_block_onset_session(arm, s_idx, panel)

                await run_workers(*(bounded_block(i) for i in range(arm.sessions)))

            elif arm.kind == "variance":
                await run_variance(store, run_cfg.id, run_cfg, arm, stop_event, quota_exceeded)

            elif arm.kind == "load":
                panel = load_panel(arm.query_panel or run_cfg.query_panel, country=arm.country or run_cfg.country)
                lvls = arm.load_levels or [arm.sessions]
                concs = arm.load_concurrency or [arm.concurrency_per_session]

                for lvl in lvls:
                    for conc in concs:
                        if stop_event.is_set():
                            break
                        var_name = f"load_{lvl}_{conc}"
                        console.print(f"  Running load level: {lvl} sessions, concurrency {conc} ({var_name})")

                        # A load level explicitly sets its simultaneous session count.
                        await run_workers(*(worker.run_load_session(arm, i, var_name, conc, panel) for i in range(lvl)))

            elif arm.kind == "pool_compare":
                await run_pool_compare(store, run_cfg.id, run_cfg, arm, stop_event, quota_exceeded)

    except (Exception, asyncio.CancelledError) as exc:
        failure = exc
        stop_event.set()
    finally:
        request_guard.reset(token)
        if installed_signal:
            loop.remove_signal_handler(signal.SIGINT)
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)

    # Final quota check
    quota_after: dict = {}
    if PROXY.has_api():
        try:
            quota_after = await quota.get_usage()
        except Exception as exc:
            logger.warning(f"Final quota check failed: {exc}")

    manifest = build_manifest(run_cfg, quota_before, quota_after)
    manifest["stop_trigger"] = guard.reason

    # Optional IP enrichment
    if run_cfg.enrich and failure is None and not stop_event.is_set():
        try:
            console.print("[dim]Enriching IP observations...[/dim]")
            from enrich import enrich_observations
            await enrich_observations(store, run_id=run_cfg.id)
        except Exception as exc:
            logger.warning(f"IP enrichment error: {exc}")

    # Determine final run status
    finished_at = time.time()
    if failure is not None:
        final_status = "failed"
    elif quota_exceeded.is_set():
        final_status = "quota"
    elif stop_event.is_set():
        final_status = "stopped"
    else:
        final_status = "completed"

    # Finalize run status in store before writing outputs so reports reflect correct status
    store.insert_run({
        "run_id": run_cfg.id,
        "started_at": started_at,
        "finished_at": finished_at,
        "config_json": json.dumps(asdict(run_cfg), default=str),
        "manifest_json": json.dumps(manifest, default=str),
        "status": final_status,
    })

    # Generate analytical reports and CSV exports
    console.print(f"[dim]Writing analysis and summary outputs to {run_dir}...[/dim]")
    from analyze import write_outputs
    try:
        outputs = write_outputs(db_path=actual_db_path, run_id=run_cfg.id, out_dir=run_dir)
    finally:
        store.close()
    if failure is not None:
        raise failure

    console.rule(f"[bold green]Experiment {run_cfg.id} {final_status.upper()}")
    console.print(f"  Elapsed Time : {finished_at - started_at:.2f}s")
    console.print(f"  Outputs      : {run_dir}\n")

    return {
        "run_id": run_cfg.id,
        "status": final_status,
        "output_dir": str(run_dir),
        "db_path": actual_db_path,
        "manifest": manifest,
        "outputs": outputs,
    }


def run_experiment(
    config_path: Union[str, Path],
    *,
    dry_run: bool = False,
    db_path: Optional[Union[str, Path]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    run_id: Optional[str] = None,
    quota_cap_mb: Optional[float] = None,
) -> dict:
    """Synchronous entry point to run an experiment."""
    return asyncio.run(
        _run_experiment_async(
            config_path,
            dry_run=dry_run,
            db_path=db_path,
            output_dir=output_dir,
            run_id=run_id,
            quota_cap_mb=quota_cap_mb,
        )
    )


def main() -> None:
    """CLI entrypoint for experiment.py."""
    parser = argparse.ArgumentParser(description="Run an AnyIP experiment defined in a TOML config")
    parser.add_argument("config_pos", nargs="?", default=None, help="Path to experiment TOML config file")
    parser.add_argument("--config", "-c", default=None, help="Path to experiment TOML config file")
    parser.add_argument("--quota-cap-mb", type=float, default=None, help="Override quota cap in MB")
    parser.add_argument("--dry-run", action="store_true", help="Print plan and estimated requests without running")
    parser.add_argument("--output-dir", default=None, help="Directory for results")
    parser.add_argument("--db-path", default=None, help="Custom SQLite DB path")
    parser.add_argument("--run-id", default=None, help="Custom run identifier")
    args = parser.parse_args()

    cfg_path = args.config or args.config_pos
    if not cfg_path:
        parser.error("A config file must be provided either as positional argument or with --config")

    run_experiment(
        cfg_path,
        dry_run=args.dry_run,
        output_dir=args.output_dir,
        db_path=args.db_path,
        run_id=args.run_id,
        quota_cap_mb=args.quota_cap_mb,
    )


if __name__ == "__main__":
    main()
