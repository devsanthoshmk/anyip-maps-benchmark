"""
Benchmark report: aggregates scraper results, prints terminal tables,
and saves summary JSON + per-query/places CSVs to results/.

The saved *_summary.json is what the UI reads — keep to_dict() in sync
with the fields the UI expects (ui/src/page.tsx types mirror this).
"""

from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box

from scraper import QueryResult, _RESP_OVERHEAD_BYTES

console = Console()

MB = 1024 * 1024
GB = 1024 * MB


@dataclass
class BenchmarkReport:
    run_id: str
    proxy_type: str
    geo_targets: list[str]
    concurrency: int
    session_strategy: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    results: list[QueryResult] = field(default_factory=list)

    # ── Aggregates ──────────────────────────────────────────────────────────

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    @property
    def total_queries(self) -> int:
        return len(self.results)

    @property
    def successful_queries(self) -> int:
        return sum(1 for r in self.results if r.query_status == "success")

    @property
    def partial_queries(self) -> int:
        return sum(1 for r in self.results if r.query_status == "partial")

    @property
    def blocked_queries(self) -> int:
        return sum(1 for r in self.results if r.query_status == "blocked")

    @property
    def empty_queries(self) -> int:
        return sum(1 for r in self.results if r.query_status == "empty")

    @property
    def error_queries(self) -> int:
        return sum(1 for r in self.results if r.query_status == "error")

    @property
    def failed_queries(self) -> int:
        return self.total_queries - self.successful_queries

    @property
    def query_success_rate_pct(self) -> float:
        return round((self.successful_queries / self.total_queries * 100), 2) if self.total_queries else 0.0

    @property
    def total_places(self) -> int:
        return sum(len(r.places) for r in self.results)

    @property
    def total_requests(self) -> int:
        return sum(r.total_requests for r in self.results)

    @property
    def ok_requests(self) -> int:
        return sum(r.ok_count for r in self.results)

    @property
    def consent_requests(self) -> int:
        return sum(r.consent_count for r in self.results)

    @property
    def block_403(self) -> int:
        return sum(r.block_403 for r in self.results)

    @property
    def block_429(self) -> int:
        return sum(r.block_429 for r in self.results)

    @property
    def sorry_requests(self) -> int:
        return sum(r.sorry_count for r in self.results)

    @property
    def soft_block_requests(self) -> int:
        return sum(r.soft_block_count for r in self.results)

    @property
    def proxy_error_requests(self) -> int:
        return sum(r.proxy_error_count for r in self.results)

    @property
    def google_error_requests(self) -> int:
        return sum(r.google_error_count for r in self.results)

    @property
    def session_count(self) -> int:
        return len({r.proxy_session_id for r in self.results if r.proxy_session_id})

    @property
    def ip_change_count(self) -> int:
        return sum(
            1 for r in self.results
            if r.exit_ip_start and r.exit_ip_end and r.exit_ip_start != r.exit_ip_end
        )

    @property
    def success_rate(self) -> float:
        return self.ok_requests / self.total_requests if self.total_requests else 0.0

    @property
    def consent_rate(self) -> float:
        return self.consent_requests / self.total_requests if self.total_requests else 0.0

    @property
    def block_rate(self) -> float:
        bad = sum(r.block_count for r in self.results)
        return bad / self.total_requests if self.total_requests else 0.0

    @property
    def proxy_error_rate(self) -> float:
        return self.proxy_error_requests / self.total_requests if self.total_requests else 0.0

    @property
    def proxy_error_rate_pct(self) -> float:
        return round(self.proxy_error_rate * 100, 2)

    @property
    def traffic_mb(self) -> float:
        """Wire traffic (compressed responses) — closest to what AnyIP bills."""
        return sum(r.bytes_wire_total for r in self.results) / MB

    @property
    def traffic_tx_mb_est(self) -> float:
        """Estimated request bytes sent through the proxy."""
        return sum(r.bytes_tx_est_total for r in self.results) / MB

    @property
    def billed_bytes_est(self) -> int:
        """Estimated total AnyIP-billable bytes: compressed responses + request
        bytes (est.) + response headers (est.) + TLS handshakes + probes."""
        return (sum(r.bytes_wire_total for r in self.results)
                + sum(r.bytes_tx_est_total for r in self.results)
                + _RESP_OVERHEAD_BYTES * self.total_requests
                + sum(r.probe_bytes_billed for r in self.results)
                + sum(r.tls_handshake_bytes for r in self.results))

    @property
    def probe_bytes_est(self) -> int:
        """Probe traffic + TLS handshake overhead combined (est.)."""
        return (sum(r.probe_bytes_billed for r in self.results)
                + sum(r.tls_handshake_bytes for r in self.results))

    @property
    def traffic_billed_mb_est(self) -> float:
        return self.billed_bytes_est / MB

    @property
    def traffic_mb_decompressed(self) -> float:
        """Decompressed payload (the old, inflated metric — kept for analysis)."""
        return sum(r.bytes_rx_total for r in self.results) / MB

    @property
    def avg_latency_ms(self) -> float:
        """Mean per-request latency (HTTP level) — separate from query wall time."""
        lat = [req.latency_ms for r in self.results for req in r.requests if req.outcome == "ok"]
        return sum(lat) / len(lat) if lat else 0.0

    @property
    def avg_query_ms(self) -> float:
        walls = [r.query_wall_ms for r in self.results if r.query_wall_ms]
        return sum(walls) / len(walls) if walls else 0.0

    @property
    def p95_query_ms(self) -> float:
        walls = sorted(r.query_wall_ms for r in self.results if r.query_wall_ms)
        if not walls:
            return 0.0
        idx = min(len(walls) - 1, math.ceil(len(walls) * 0.95) - 1)
        return walls[idx]

    @property
    def p95_latency_ms(self) -> float:
        lat = sorted(req.latency_ms for r in self.results for req in r.requests if req.outcome == "ok")
        if not lat:
            return 0.0
        idx = min(len(lat) - 1, math.ceil(len(lat) * 0.95) - 1)
        return lat[idx]

    @property
    def qps(self) -> float:
        return self.total_queries / self.elapsed_seconds if self.elapsed_seconds else 0.0

    @property
    def places_per_minute(self) -> float:
        return self.total_places / (self.elapsed_seconds / 60) if self.elapsed_seconds else 0.0

    # ── Per-country breakdown ───────────────────────────────────────────────

    def by_country(self) -> dict[str, dict]:
        groups: dict[str, list[QueryResult]] = {}
        for r in self.results:
            groups.setdefault(r.country, []).append(r)

        summary = {}
        for country, results in groups.items():
            reqs = [req for r in results for req in r.requests]
            ok = [req for req in reqs if req.outcome == "ok"]
            walls = [r.query_wall_ms for r in results if r.query_wall_ms]
            validation = {}
            for r in results:
                for k, v in r.validation.items():
                    validation[k] = validation.get(k, 0) + v
            summary[country] = {
                "queries": len(results),
                "places": sum(len(r.places) for r in results),
                "requests": len(reqs),
                "ok": len(ok),
                "consent": sum(1 for req in reqs if req.outcome == "consent"),
                "403": sum(1 for req in reqs if req.outcome == "403"),
                "429": sum(1 for req in reqs if req.outcome == "429"),
                "sorry": sum(1 for req in reqs if req.outcome == "sorry"),
                "soft_block": sum(1 for req in reqs if req.outcome == "soft_block"),
                "proxy_error": sum(1 for req in reqs if req.outcome == "proxy_error"),
                "success_rate": len(ok) / len(reqs) if reqs else 0.0,
                "consent_rate": (
                    sum(1 for req in reqs if req.outcome == "consent") / len(reqs) if reqs else 0.0
                ),
                "avg_latency_ms": sum(r.latency_ms for r in ok) / len(ok) if ok else 0.0,
                "avg_query_ms": sum(walls) / len(walls) if walls else 0.0,
                "traffic_mb": (
                    sum(r.bytes_wire_total for r in results)
                    + sum(r.bytes_tx_est_total for r in results)
                ) / MB,
            }
        return summary

    # ── Validation aggregation ──────────────────────────────────────────────

    def validation_totals(self) -> dict:
        totals: dict[str, int] = {}
        for r in self.results:
            for k, v in r.validation.items():
                totals[k] = totals.get(k, 0) + v
        total = sum(totals.values())
        flagged = sum(v for k, v in totals.items() if k != "valid")
        flagged_queries = [
            r.query for r in self.results
            if r.validation_flagged() > 0
        ]
        return {
            **totals,
            "flagged_places": flagged,
            "flagged_queries": len(flagged_queries),
            "flagged_query_names": flagged_queries[:10],
            "validation_rate_pct": round(
                (totals.get("valid", 0) / total * 100.0) if total else 100.0, 2
            ),
        }

    # ── Sticky-session continuity ────────────────────────────────────────────

    def sticky_continuity(self) -> dict:
        """Did sessions hold the same exit IP within a query — and across
        queries that shared one session id (per-country / shared / resumed)?"""
        per_query_matched = 0
        per_query_rotated = 0
        per_query_unknown = 0
        for r in self.results:
            if not r.exit_ip_start or not r.exit_ip_end:
                per_query_unknown += 1
            elif r.exit_ip_start == r.exit_ip_end:
                per_query_matched += 1
            else:
                per_query_rotated += 1

        sessions: dict[str, dict] = {}
        for r in self.results:
            s = sessions.setdefault(r.proxy_session_id, {
                "session_id": r.proxy_session_id,
                "queries": 0,
                "countries": [],
                "exit_ips": set(),
                "rotated": False,
            })
            s["queries"] += 1
            if r.country not in s["countries"]:
                s["countries"].append(r.country)
            for ip in (r.exit_ip_start, r.exit_ip_end):
                if ip:
                    s["exit_ips"].add(ip)
            if r.exit_ip_start and r.exit_ip_end and r.exit_ip_start != r.exit_ip_end:
                s["rotated"] = True

        shared = [
            {
                "session_id": s["session_id"],
                "countries": s["countries"],
                "queries": s["queries"],
                "distinct_exit_ips": len(s["exit_ips"]),
                "exit_ips": sorted(s["exit_ips"]),
                "rotated_mid_query": s["rotated"],
            }
            for s in sorted(sessions.values(), key=lambda x: -x["queries"])
            if s["queries"] > 0
        ]
        return {
            "query_ip_matched": per_query_matched,
            "query_ip_rotated": per_query_rotated,
            "query_ip_unknown": per_query_unknown,
            "sessions": shared,
        }

    # ── JSON structure for the UI (results/*_summary.json) ──────────────────

    def to_dict(self) -> dict:
        billed = self.traffic_billed_mb_est
        return {
            "started_at_iso": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
            "run_id": self.run_id,
            "proxy_type": self.proxy_type,
            "session_strategy": self.session_strategy,
            "geo_targets": self.geo_targets,
            "concurrency": self.concurrency,
            "elapsed_s": round(self.elapsed_seconds, 2),
            "total_queries": self.total_queries,
            "successful_queries": self.successful_queries,
            "failed_queries": self.failed_queries,
            "partial_queries": self.partial_queries,
            "blocked_queries": self.blocked_queries,
            "empty_queries": self.empty_queries,
            "error_queries": self.error_queries,
            "query_success_rate_pct": self.query_success_rate_pct,
            "total_places": self.total_places,
            "total_requests": self.total_requests,
            "ok_requests": self.ok_requests,
            "consent_requests": self.consent_requests,
            "block_403": self.block_403,
            "block_429": self.block_429,
            "sorry_requests": self.sorry_requests,
            "soft_block_requests": self.soft_block_requests,
            "proxy_error_requests": self.proxy_error_requests,
            "google_error_requests": self.google_error_requests,
            "session_count": self.session_count,
            "ip_change_count": self.ip_change_count,
            "success_rate_pct": round(self.success_rate * 100, 2),
            "consent_rate_pct": round(self.consent_rate * 100, 2),
            "block_rate_pct": round(self.block_rate * 100, 2),
            "proxy_error_rate_pct": self.proxy_error_rate_pct,
            # ── Traffic: wire/billed is what AnyIP charges; decompressed for analysis
            "traffic_mb": round(billed, 3),
            "traffic_mb_wire": round(self.traffic_mb, 3),
            "traffic_tx_mb_est": round(self.traffic_tx_mb_est, 3),
            "traffic_probe_est_mb": round(self.probe_bytes_est / MB, 4),
            "traffic_mb_decompressed": round(self.traffic_mb_decompressed, 3),
            "traffic_gb": round(billed / 1024, 4),
            # ── Latency: query wall time separate from per-request latency
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "avg_query_ms": round(self.avg_query_ms, 1),
            "p95_query_ms": round(self.p95_query_ms, 1),
            "qps": round(self.qps, 4),
            "places_per_minute": round(self.places_per_minute, 2),
            "validation": self.validation_totals(),
            "sticky_continuity": self.sticky_continuity(),
            "by_country": self.by_country(),
            "queries": [
                {
                    "query": r.query,
                    "country": r.country,
                    "session": r.proxy_session_id,
                    "proxy_session_id": r.proxy_session_id,
                    "proxy_type": r.proxy_type,
                    "exit_ip_start": r.exit_ip_start,
                    "exit_ip_end": r.exit_ip_end,
                    "ip_stable": bool(
                        r.exit_ip_start and r.exit_ip_start == r.exit_ip_end
                    ),
                    "places": len(r.places),
                    "requests": r.total_requests,
                    "total_requests": r.total_requests,
                    "ok": r.ok_count,
                    "consent": r.consent_count,
                    "block_403": r.block_403,
                    "block_429": r.block_429,
                    "sorry": r.sorry_count,
                    "soft_block": r.soft_block_count,
                    "avg_latency_ms": round(r.avg_latency_ms, 1),
                    "p95_latency_ms": round(r.p95_latency_ms, 1),
                    "query_wall_ms": round(r.query_wall_ms, 1),
                    "bytes_rx": r.bytes_rx_total,
                    "bytes_wire": r.bytes_wire_total,
                    "bytes_tx_est": r.bytes_tx_est_total,
                    "bytes_probe_est": r.probe_bytes_billed,
                    "places_flagged": r.validation_flagged(),
                    "error": r.error,
                    "status": r.query_status,
                    "stop_reason": r.stop_reason,
                    "block_at_request": r.block_at_request,
                    "block_at_page": r.block_at_page,
                    "partial": r.partial,
                    "proxy_errors": r.proxy_error_count,
                }
                for r in self.results
            ],
        }


# ── Terminal output (rich tables) ──────────────────────────────────────────────

def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _sticky_note(report: BenchmarkReport) -> str:
    s = report.sticky_continuity()
    shared = s["sessions"]
    reused = [x for x in shared if x["queries"] > 1]
    note = f"{s['query_ip_matched']} / {s['query_ip_rotated']}"
    if s["query_ip_unknown"]:
        note += f" (+{s['query_ip_unknown']} unknown)"
    if reused:
        bad = [x for x in reused if x["distinct_exit_ips"] > 1]
        note += f"  · {len(reused)} shared sessions, {len(bad)} with >1 exit IP"
    return note


def print_summary(report: BenchmarkReport) -> None:
    console.rule(f"[bold cyan]AnyIP Benchmark — run {report.run_id}")

    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
    t.add_column("Metric", style="dim", min_width=28)
    t.add_column("Value", justify="right")
    for label, val in [
        ("Proxy type", report.proxy_type),
        ("Geo targets", ", ".join(report.geo_targets)),
        ("Concurrency", str(report.concurrency)),
        ("Elapsed", f"{report.elapsed_seconds:.1f}s"),
        ("Queries total", str(report.total_queries)),
        ("Queries succeeded", str(report.successful_queries)),
        ("Queries partial", str(report.partial_queries)),
        ("Queries blocked", str(report.blocked_queries)),
        ("Queries empty", str(report.empty_queries)),
        ("Queries error", str(report.error_queries)),
        ("Query success rate", f"{report.query_success_rate_pct:.1f}%"),
        ("Places scraped", str(report.total_places)),
        ("HTTP requests", str(report.total_requests)),
        ("OK", str(report.ok_requests)),
        ("Consent pages (200)", str(report.consent_requests)),
        ("403 blocks", str(report.block_403)),
        ("429 rate-limits", str(report.block_429)),
        ("Sorry / captcha (200)", str(report.sorry_requests)),
        ("Soft blocks (200)", str(report.soft_block_requests)),
        ("Proxy errors", str(report.proxy_error_requests)),
        ("Google 5xx", str(report.google_error_requests)),
        ("Success rate", _pct(report.success_rate)),
        ("Consent rate", _pct(report.consent_rate)),
        ("Overall block rate", _pct(report.block_rate)),
        ("Proxy error rate", _pct(report.proxy_error_rate)),
        ("Distinct sessions", str(report.session_count)),
        ("Traffic billed (est.)", f"{report.traffic_billed_mb_est:.2f} MB"),
        ("Traffic wire / req-est",
         f"{report.traffic_mb:.2f} MB + {report.traffic_tx_mb_est:.3f} MB"),
        ("Traffic decompressed (old metric)", f"{report.traffic_mb_decompressed:.2f} MB"),
        ("Avg request latency", f"{report.avg_latency_ms:.0f} ms"),
        ("p95 request latency", f"{report.p95_latency_ms:.0f} ms"),
        ("Avg query wall time", f"{report.avg_query_ms:.0f} ms"),
        ("p95 query wall time", f"{report.p95_query_ms:.0f} ms"),
        ("Throughput", f"{report.qps:.3f} q/s  |  {report.places_per_minute:.1f} places/min"),
        ("Validation", f"{report.validation_totals().get('valid', 0)} valid, "
                       f"{report.validation_totals().get('flagged_places', 0)} flagged"
                       f" ({report.validation_totals().get('validation_rate_pct', 0):.1f}% valid)"),
        ("Sticky exits: stable / rotated", _sticky_note(report)),
    ]:
        t.add_row(label, val)
    console.print(t)

    by_country = report.by_country()
    if len(by_country) > 1:
        console.rule("[bold]Per-country breakdown")
        ct = Table(box=box.SIMPLE_HEAD, header_style="bold blue")
        ct.add_column("Country")
        ct.add_column("Queries", justify="right")
        ct.add_column("Places", justify="right")
        ct.add_column("OK", justify="right")
        ct.add_column("Consent", justify="right")
        ct.add_column("403", justify="right")
        ct.add_column("429", justify="right")
        ct.add_column("Success%", justify="right")
        ct.add_column("Avg ms", justify="right")
        ct.add_column("Traffic MB", justify="right")
        for country, s in by_country.items():
            ct.add_row(
                country,
                str(s["queries"]),
                str(s["places"]),
                str(s["ok"]),
                str(s["consent"]),
                str(s["403"]),
                str(s["429"]),
                f"{s['success_rate'] * 100:.1f}%",
                f"{s['avg_latency_ms']:.0f}",
                f"{s['traffic_mb']:.2f}",
            )
        console.print(ct)

    console.rule("[bold]Per-query detail")
    qt = Table(box=box.SIMPLE, header_style="bold")
    qt.add_column("Query", min_width=30)
    qt.add_column("Country")
    qt.add_column("Places", justify="right")
    qt.add_column("Reqs", justify="right")
    qt.add_column("Consent", justify="right")
    qt.add_column("403", justify="right")
    qt.add_column("429", justify="right")
    qt.add_column("Avg ms", justify="right")
    qt.add_column("Status")
    for r in report.results:
        status_label = r.query_status.upper()
        qt.add_row(
            r.query[:40],
            r.country,
            str(len(r.places)),
            str(r.total_requests),
            str(r.consent_count),
            str(r.block_403),
            str(r.block_429),
            f"{r.avg_latency_ms:.0f}",
            status_label,
        )
    console.print(qt)


# ── File output ────────────────────────────────────────────────────────────────

def save_results(report: BenchmarkReport, output_dir: str = "results") -> None:
    ts = time.strftime("%Y%m%d_%H%M%S")
    run_dir = Path(output_dir) / f"{report.run_id}_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    json_path = run_dir / "summary.json"
    with open(json_path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)
    console.print(f"\n[green]Summary saved →[/green] {json_path}")

    csv_path = run_dir / "queries.csv"
    fieldnames = [
        "query", "country", "proxy_session_id", "proxy_type", "places",
        "total_requests", "ok", "consent", "403", "429",
        "avg_latency_ms", "p95_latency_ms", "query_wall_ms",
        "bytes_rx", "bytes_wire", "bytes_tx_est", "bytes_probe_est",
        "exit_ip_start", "exit_ip_end", "places_flagged", "error",
        "status", "stop_reason", "block_at_request", "block_at_page", "partial",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in report.results:
            writer.writerow({
                "query": r.query,
                "country": r.country,
                "proxy_session_id": r.proxy_session_id,
                "proxy_type": r.proxy_type,
                "places": len(r.places),
                "total_requests": r.total_requests,
                "ok": r.ok_count,
                "consent": r.consent_count,
                "403": r.block_403,
                "429": r.block_429,
                "avg_latency_ms": round(r.avg_latency_ms, 1),
                "p95_latency_ms": round(r.p95_latency_ms, 1),
                "query_wall_ms": round(r.query_wall_ms, 1),
                "bytes_rx": r.bytes_rx_total,
                "bytes_wire": r.bytes_wire_total,
                "bytes_tx_est": r.bytes_tx_est_total,
                "bytes_probe_est": r.probe_bytes_billed,
                "exit_ip_start": r.exit_ip_start,
                "exit_ip_end": r.exit_ip_end,
                "places_flagged": r.validation_flagged(),
                "error": r.error or "",
                "status": r.query_status,
                "stop_reason": r.stop_reason,
                "block_at_request": r.block_at_request,
                "block_at_page": r.block_at_page,
                "partial": r.partial,
            })
    console.print(f"[green]Query CSV saved →[/green] {csv_path}")

    places_path = run_dir / "places.csv"
    place_fields = [
        "query", "country", "place_id", "title", "category",
        "address", "phone", "website", "stars", "reviews", "lat", "lng",
        "validation",
    ]
    with open(places_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=place_fields, extrasaction="ignore")
        writer.writeheader()
        for r in report.results:
            for p in r.places:
                row = dict(p)
                row["query"] = r.query
                row["country"] = r.country
                writer.writerow(row)
    console.print(f"[green]Places CSV saved →[/green] {places_path}")
