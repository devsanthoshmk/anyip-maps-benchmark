#!/usr/bin/env python3
"""
AnyIP proxy benchmark CLI for Google Maps scraping.

Usage
─────
# Local baseline (no proxy — your own IP)
python bench.py --no-proxy --concurrency 2 --limit 10

# Residential, India + US
python bench.py --queries-file queries/india_us_cities.txt \
    --countries IN,US --concurrency 20 --limit 40

# Mobile, India
python bench.py --countries IN --proxy-type mobile --concurrency 30

# Mixed (AnyIP picks residential/mobile per request)
python bench.py --proxy-type mixed --countries IN,US

# Pick the session strategy inline instead of the interactive prompt
python bench.py --session-strategy per-query        # new IP per query
python bench.py --session-strategy per-country      # one IP per country
python bench.py --session-strategy shared           # one IP for the whole bench
python bench.py --session-strategy resume-last      # reuse the last run's sessions

# Just print current AnyIP quota
python bench.py --quota

Session strategies (bench asks which one to use unless --session-strategy is set):
  per-query     : one fresh sticky session (exit IP) per query
  per-country   : queries to the same country share one sticky session
  shared        : the whole bench shares a single sticky session
  resume-last   : continue the most recent run's session names (same user names)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.prompt import Prompt

from config import PROXY
from scraper import scrape_query
from report import BenchmarkReport, print_summary, save_results
from quota import get_usage, print_usage

console = Console()

SESSION_STRATEGIES = ("per-query", "per-country", "shared", "resume-last")


def load_query_tasks(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Build list of (query, country) tasks from CLI args or input file."""
    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    if not countries:
        countries = ["US"]

    if args.query:
        return [(args.query, countries[0])]

    qfile = Path(args.queries_file)
    if not qfile.exists():
        console.print(f"[red]Queries file not found:[/red] {qfile}")
        sys.exit(1)

    if qfile.suffix.lower() == ".csv":
        tasks: list[tuple[str, str]] = []
        with qfile.open(mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for idx, row in enumerate(reader):
                if not row:
                    continue
                q = (row.get("query") or "").strip()
                if not q:
                    continue
                c = (row.get("country") or "").strip().upper()
                if not c:
                    c = countries[idx % len(countries)]
                tasks.append((q, c))
        if not tasks:
            console.print(f"[red]No queries found in {qfile}. Exiting.[/red]")
            sys.exit(1)
        return tasks
    else:
        raw_queries = [
            line.strip() for line in qfile.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not raw_queries:
            console.print(f"[red]No queries found in {qfile}. Exiting.[/red]")
            sys.exit(1)
        return [(q, countries[i % len(countries)]) for i, q in enumerate(raw_queries)]


def print_legacy_dry_run(args: argparse.Namespace) -> None:
    """Print dry run plan for legacy benchmark without performing any I/O."""
    tasks = load_query_tasks(args)
    countries = sorted(set(c for _, c in tasks))
    proxy_type = "none" if args.no_proxy else args.proxy_type
    est_pages = max(1, math.ceil(args.limit / 20))
    est_reqs_per_query = 1 + est_pages
    total_google_reqs = len(tasks) * est_reqs_per_query
    est_wire_mb = round((total_google_reqs * 50_000) / (1024 * 1024), 2)

    console.rule("[bold cyan]Legacy Benchmark — Dry Run Plan")
    console.print(f"  Queries        : {len(tasks)}")
    console.print(f"  Countries      : {', '.join(countries)}")
    console.print(f"  Concurrency    : {args.concurrency}")
    console.print(f"  Limit / Query  : {args.limit} places (~{est_pages} pages)")
    console.print(f"  Proxy          : {'DISABLED (local)' if args.no_proxy else proxy_type}")
    console.print(f"  Sticky Sessions: {not args.rotation}")
    console.print(f"  Force Close TCP: {args.rotation}")
    console.print(f"  Est. Requests  : ~{total_google_reqs}")
    console.print(f"  Est. Wire MB   : ~{est_wire_mb} MB")
    console.print("\n[yellow]Dry run only: no network requests sent, no results saved.[/yellow]\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="AnyIP proxy benchmark for Google Maps scraping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    q_group = p.add_mutually_exclusive_group(required=False)
    q_group.add_argument("--query", help="Single query to benchmark")
    q_group.add_argument(
        "--queries-file",
        default="queries/india_us_cities.txt",
        help="Text file with one query per line (default: queries/india_us_cities.txt)",
    )
    p.add_argument("--countries", default=",".join(PROXY.geo_targets),
                   help="Comma-separated country codes to rotate across (default from .env)")
    p.add_argument("--concurrency", type=int, default=20, help="Parallel queries (default: 20)")
    p.add_argument("--limit", type=int, default=40, help="Max places per query (default: 40)")
    p.add_argument("--proxy-type", choices=["residential", "mobile", "mixed", "none"],
                   default=PROXY.proxy_type, help="Proxy type (default from .env)")
    p.add_argument("--session-strategy", choices=SESSION_STRATEGIES,
                   default=None, help="Session management choice (interactive prompt if omitted)")
    p.add_argument("--no-ask", action="store_true",
                   help="Never prompt; use defaults/flags (for CI / cron)")
    p.add_argument("--no-proxy", action="store_true",
                   help="Run without proxy (local network baseline)")
    p.add_argument("--rotation", action="store_true",
                   help="Force a fresh TCP connection per request; breaks Google session continuity")
    p.add_argument("--strict-sticky", action="store_true",
                   help="Fail the query instead of rotating IP mid-scrape")
    p.add_argument("--lang", default="en", help="Google language code (default: en)")
    p.add_argument("--run-id", default=None, help="Custom run ID (default: auto)")
    p.add_argument("--output-dir", default="results", help="Result directory (default: results/)")
    p.add_argument("--quota", action="store_true", help="Print AnyIP quota usage and exit")
    p.add_argument("--experiment", default=None, help="Path to experiment TOML config file")
    p.add_argument("--dry-run", action="store_true", help="Print plan and estimated requests without running")
    return p.parse_args()


# ── Session strategy: the interactive choice ──────────────────────────────

def load_latest_sessions(output_dir: str) -> dict:
    """Collect successful (query, country) → session_id from the newest run."""
    summaries = sorted(
        Path(output_dir).glob("*/summary.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not summaries:
        return {}
    latest = summaries[-1]
    try:
        data = json.loads(latest.read_text())
    except (json.JSONDecodeError, OSError):
        console.print(f"[yellow]Could not read {latest} — no sessions to resume.[/yellow]")
        return {}

    resume: dict[tuple[str, str], str] = {}
    failed = 0
    for q in data.get("queries", []):
        sid = str(q.get("proxy_session_id") or "")
        if q.get("status") != "success" or not sid:
            failed += 1
            continue
        resume[(q["query"], q.get("country", ""))] = sid

    age_min = 0.0
    started = data.get("started_at_iso")
    if started:
        try:
            dt = datetime.fromisoformat(started)
            age_min = max(0.0, (time.time() - dt.timestamp()) / 60.0)
        except ValueError:
            pass
    return {
        "run_id": str(data.get("run_id", "?")),
        "summary_path": str(latest),
        "sessions": resume,
        "failed_skipped": failed,
        "age_min": age_min,
        "sesstime_min": PROXY.session_duration_min,
    }


def ask_session_strategy(default: str) -> tuple[str, dict]:
    """Ask the user how sessions should be managed for this bench."""
    console.rule("[bold cyan]Session strategy — how should sticky sessions be managed?")
    opts = "\n".join(f"  [bold][{i + 1}][/bold] {name} {_STRATEGY_HELP[name]}"
                     for i, name in enumerate(SESSION_STRATEGIES))
    console.print(opts)
    while True:
        raw = Prompt.ask(
            "Choose 1-4 (or type the strategy name)",
            choices=[str(i + 1) for i in range(len(SESSION_STRATEGIES))] + list(SESSION_STRATEGIES),
            show_choices=False,
            default="1",
        ).strip().lower()
        if raw.isdigit() and 1 <= int(raw) <= len(SESSION_STRATEGIES):
            choice = SESSION_STRATEGIES[int(raw) - 1]
            break
        if raw in SESSION_STRATEGIES:
            choice = raw
            break
        console.print("[red]Not a valid choice — pick 1-4.[/red]")

    resume = {}
    if choice == "resume-last":
        resume = load_latest_sessions("results")
        if not resume.get("sessions"):
            console.print("[yellow]No previous sessions found — falling back to per-query.[/yellow]")
            choice = "per-query"
            resume = {}
        else:
            console.print(
                f"  [bold blue]Resuming run[/bold blue] {resume['run_id']} "
                f"({len(resume['sessions'])} sessions, from {resume['summary_path']})"
            )
            if resume["age_min"] > resume["sesstime_min"]:
                console.print(
                    f"  [yellow]⚠ Sessions are {resume['age_min']:.0f} min old "
                    f"(sesstime is {resume['sesstime_min']} min) — AnyIP may pin NEW exit "
                    "IPs to the same session names. Exit-IP probes will tell you "
                    "whether continuity actually held.[/yellow]"
                )
    return choice, resume


_STRATEGY_HELP = {
    "per-query": "— new session / exit IP per query (clean slate every time)",
    "per-country": "— one sticky session shared by all queries of a country",
    "shared": "— one sticky session for the entire bench",
    "resume-last": "— continue the previous run's sessions",
}


async def run_benchmark(args: argparse.Namespace) -> BenchmarkReport:
    # ── Session strategy (interactive unless a flag/default exists) ───────────
    session_strategy = args.session_strategy
    resume: dict = {}
    if not session_strategy and not args.no_ask and sys.stdin.isatty():
        session_strategy, resume = ask_session_strategy(PROXY.session_strategy)
    if not session_strategy:
        session_strategy = PROXY.session_strategy
    if session_strategy == "resume-last" and not resume:
        resume = load_latest_sessions(args.output_dir)
        if not resume.get("sessions"):
            console.print("[yellow]No previous sessions found — falling back to per-query.[/yellow]")
            session_strategy = "per-query"
            resume = {}
        elif resume["age_min"] > resume["sesstime_min"]:
            console.print(
                f"  [yellow]⚠ Resuming run {resume['run_id']}: sessions are "
                f"{resume['age_min']:.0f} min old (sesstime {resume['sesstime_min']} min) — "
                "AnyIP may pin NEW exit IPs to the same session names. "
                "Exit-IP probes will show whether continuity held.[/yellow]"
            )
    console.print(f"\n  Session strategy: [bold]{session_strategy}[/bold]"
                  + (f" (resuming {len(resume.get('sessions', {}))} sessions)" if resume else ""))

    # ── Build query list ─────────────────────────────────────────────────────
    tasks = load_query_tasks(args)
    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()] or ["US"]

    run_id = args.run_id or uuid.uuid4().hex[:8]
    proxy_type = "none" if args.no_proxy else args.proxy_type

    report = BenchmarkReport(
        run_id=run_id,
        proxy_type=proxy_type,
        geo_targets=countries,
        concurrency=args.concurrency,
        session_strategy=session_strategy,
    )

    # ── Pre-run info ─────────────────────────────────────────────────────────
    console.rule(f"[bold cyan]AnyIP Benchmark  run={run_id}")
    console.print(f"  Queries    : {len(tasks)}")
    console.print(f"  Countries  : {', '.join(countries)}")
    console.print(f"  Concurrency: {args.concurrency}")
    console.print(f"  Limit/query: {args.limit} places")
    console.print(f"  Proxy      : {'DISABLED (local)' if args.no_proxy else proxy_type}")
    console.print(f"  Sticky     : {not args.rotation}")
    console.print(f"  Sessions   : {session_strategy}")
    console.print()

    if not args.no_proxy and not PROXY.is_configured():
        console.print(
            "[red]Proxy credentials are missing. Use --no-proxy to explicitly run locally.[/red]\n"
        )
        raise SystemExit(2)

    # ── Quota check before spending proxy bandwidth ──────────────────────────
    if not args.no_proxy and PROXY.has_api():
        try:
            print_usage(await get_usage())
        except Exception as exc:
            console.print(f"[yellow]Quota check failed: {exc}[/yellow]")

    # ── Semaphore-controlled async run ───────────────────────────────────────
    semaphore = asyncio.Semaphore(args.concurrency)
    use_proxy = not args.no_proxy and PROXY.is_configured()

    # ── Session allocation per chosen strategy ───────────────────────────────
    shared_session_id = uuid.uuid4().hex[:12] if session_strategy == "shared" else None
    country_session_ids: dict[str, str] = {}
    resume_sessions: dict[tuple[str, str], str] = (
        resume.get("sessions", {}) if session_strategy == "resume-last" else {}
    )

    def pick_session(query: str, country: str) -> str | None:
        if session_strategy == "shared":
            return shared_session_id
        if session_strategy == "per-country":
            return country_session_ids.setdefault(country, uuid.uuid4().hex[:12])
        if session_strategy == "resume-last":
            return resume_sessions.get((query, country))
        return None  # per-query → scrape_query mints a fresh session

    async def run_one(query: str, country: str):
        async with semaphore:
            return await scrape_query(
                query=query,
                country=country,
                lang=args.lang,
                limit=args.limit,
                use_proxy=use_proxy,
                proxy_type=None if args.no_proxy else args.proxy_type,
                sticky=not args.rotation,
                strict_session=args.strict_sticky,
                session_id=None if args.rotation else pick_session(query, country),
                force_close=args.rotation,
            )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scraping…", total=len(tasks))
        for coro in asyncio.as_completed([run_one(q, c) for q, c in tasks]):
            report.results.append(await coro)
            progress.advance(task)

    report.finished_at = time.time()
    return report


def main():
    args = parse_args()

    if args.experiment:
        from experiment import run_experiment
        run_experiment(
            args.experiment,
            dry_run=args.dry_run,
            output_dir=args.output_dir,
            run_id=args.run_id,
        )
        return

    if args.dry_run:
        print_legacy_dry_run(args)
        return

    if args.quota:
        usage = asyncio.run(get_usage())
        print_usage(usage)
        return

    report = asyncio.run(run_benchmark(args))
    print_summary(report)
    save_results(report, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
