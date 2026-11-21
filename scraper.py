"""
Google Maps scraper through AnyIP proxies.

One scrape_query() call = one sticky proxy session:
  1. GET the Maps SPA -> extract the tbm=map JSON URL
  2. Paginate that JSON -> collect places

_classify_response() peeks at the body and headers:
  Outcomes: ok | consent | 403 | 429 | sorry | soft_block | google_5xx | proxy_error | other_error | network_error

AnyIP username format:
    user_<ID>,type_<type>,country_<ISO>,session_<id>,sesstime_<min>
Docs: https://anyip.io/docs/guides/configuration.md
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import ipaddress
import json
import math
import re
import time
import uuid
import zlib
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Optional, Mapping, Any
from urllib.parse import quote, urlencode

import aiohttp

try:
    import brotli  # noqa: F401  (needed to decode "Content-Encoding: br" ourselves)
except ImportError:
    brotli = None

from config import PROXY

BLOCK_OUTCOMES = {"403", "429", "consent", "sorry", "soft_block"}


class RequestGuard:
    """Shared admission and circuit breaker for an experiment's Google attempts."""

    def __init__(self, stop, rps, block_rate, proxy_error_rate, min_samples=5):
        self.stop = stop
        self.interval = 1 / rps if rps else 0
        self.limits = (block_rate, proxy_error_rate)
        self.min_samples = min_samples
        self.lock = asyncio.Lock()
        self.next_start = 0.0
        self.total = self.blocks = self.proxy_errors = 0
        self.reason = None

    async def admit(self):
        async with self.lock:
            delay = self.next_start - time.monotonic()
            if delay > 0 and not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), delay)
                except TimeoutError:
                    pass
            if self.stop.is_set():
                return False
            self.next_start = time.monotonic() + self.interval
            return True

    def observe(self, outcome):
        self.total += 1
        self.blocks += outcome in BLOCK_OUTCOMES
        self.proxy_errors += outcome == "proxy_error"
        if self.total >= self.min_samples and not self.stop.is_set():
            for count, limit, reason in zip((self.blocks, self.proxy_errors), self.limits, ("block_rate", "proxy_error_rate")):
                if count and count / self.total >= limit:
                    self.reason = reason
                    self.stop.set()
                    break


request_guard: ContextVar[RequestGuard | None] = ContextVar("request_guard", default=None)


# ── Response classification ────────────────────────────────────────────────────

_CONSENT_SIGNALS = [
    b"consent.google.com",
    b"before you continue",
    b"agree to google",
    b"to use google maps",
]

_SORRY_SIGNALS = [
    b"/sorry/index",
    b"unusual traffic",
    b"detected unusual traffic",
    b"our systems have detected",
    b"recaptcha",
    b"automated queries",
]

_SOFT_BLOCK_SIGNALS = [
    b"why did this happen",
    b"troubleshooting",
]


def _classify_response(
    status: int,
    body: bytes | str = b"",
    headers: Mapping[str, Any] | None = None,
) -> str:
    """Classify an HTTP response into one of the 10 spec outcomes:
    ok | consent | 403 | 429 | sorry | soft_block | google_5xx | proxy_error | other_error | network_error
    """
    if status == 0:
        return "network_error"

    if status == 403:
        return "403"
    if status == 429:
        return "429"

    # AnyIP error headers (mapping may have case-insensitive or mixed keys)
    anyip_err = ""
    if headers:
        for k, v in headers.items():
            if str(k).lower() == "x-anyip-error":
                anyip_err = str(v)
                break

    if status == 407 or status in (400, 409, 413):
        return "proxy_error"
    if status in (502, 503, 504):
        return "proxy_error"
    if anyip_err and status != 200:
        return "proxy_error"

    if status in (500, 501) or status >= 505:
        return "google_5xx"

    if 300 <= status < 400:
        return "other_error"
    if 400 <= status < 500:
        return "other_error"

    if 200 <= status < 300:
        raw_body = (
            body.encode("utf-8", errors="replace") if isinstance(body, str) else (body or b"")
        )
        body_lower = raw_body.lower()

        # 1. Consent signals
        for sig in _CONSENT_SIGNALS:
            if sig in body_lower:
                return "consent"

        # 2. Sorry signals
        for sig in _SORRY_SIGNALS:
            if sig in body_lower:
                return "sorry"

        # 3. Soft-block signals (only when body lacks a tbm=map link)
        if (
            b"why did this happen" in body_lower or b"troubleshooting" in body_lower
        ) and (b"tbm=map" not in body_lower):
            return "soft_block"

        return "ok"

    return "other_error"


# ── Exit-IP Observations & Request Records ────────────────────────────────────

@dataclass
class IpObservation:
    ts: float
    request_index: int
    witness: str
    family: str  # "v4" or "v6"
    ip: str
    latency_ms: float = 0.0
    error: str = ""
    asn: str = ""
    org: str = ""
    city: str = ""
    country: str = ""


@dataclass
class RequestRecord:
    url: str
    status: int
    outcome: str          # ok | consent | 403 | 429 | sorry | soft_block | google_5xx | proxy_error | other_error | network_error
    latency_ms: float
    bytes_rx: int         # decompressed payload (analysis only — NOT what AnyIP bills)
    bytes_wire: int = 0   # compressed bytes on the wire — what AnyIP bills (response)
    bytes_tx_est: int = 0 # estimated request bytes sent through the proxy
    content_encoding: str = ""
    kind: str = "google"  # "spa" | "page" | "probe"
    request_index: int = 0
    page_start: int = 0
    attempt: int = 0
    anyip_error: str = ""
    content_type: str = ""
    server: str = ""
    retry_after: str = ""
    response_hash: str = ""
    error: str = ""
    ts: float = 0.0
    is_final: bool = True


def _wire_report_bytes(r: RequestRecord) -> int:
    """Full round-trip through the proxy: req (est.) + resp headers (est.) + body (wire)."""
    return r.bytes_wire + r.bytes_tx_est + _RESP_OVERHEAD_BYTES


# ── Per-query result ───────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    query: str
    country: str
    proxy_session_id: str
    proxy_type: str = ""          # effective type used, e.g. "residential"
    places: list[dict] = field(default_factory=list)
    requests: list[RequestRecord] = field(default_factory=list)
    error: Optional[str] = None

    # Spec fields
    partial: bool = False
    stop_reason: str = ""         # limit | no_new | block | parse_error | network | session_lost | proxy_error | other_error | quota | stopped
    block_at_request: int | None = None
    block_at_page: int | None = None
    target_pages: int = 0
    expected_min_results: int = 0
    session_started_at: float = 0.0
    session_age_s: float = 0.0
    ip_observations: list[IpObservation] = field(default_factory=list)
    attempts: list[RequestRecord] = field(default_factory=list)

    # ── Measurement separation ────────────────────────────────────────────────
    query_wall_ms: float = 0.0

    # Sticky-session verification (exit IP before vs after the query)
    exit_ip_start: str = ""
    exit_ip_end: str = ""
    # Billed-bytes estimate of the ipify probes (kept inside the accounting)
    probe_bytes_billed: int = 0
    # Fresh-connection TLS handshakes through the tunnel (Google + ipify)
    tls_handshake_bytes: int = 0

    # Result validation issue counters
    validation: dict = field(default_factory=lambda: {
        "valid": 0,
        "bad_place_id": 0,
        "missing_title": 0,
        "missing_address": 0,
        "bad_geo": 0,
        "out_of_bbox": 0,
        "parse_error": 0,
    })

    def validation_flagged(self) -> int:
        return sum(v for k, v in self.validation.items() if k != "valid")

    @property
    def total_requests(self) -> int:
        return len(self.requests)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.requests if r.outcome == "ok")

    @property
    def consent_count(self) -> int:
        return sum(1 for r in self.requests if r.outcome == "consent")

    @property
    def block_403(self) -> int:
        return sum(1 for r in self.requests if r.outcome == "403")

    @property
    def block_429(self) -> int:
        return sum(1 for r in self.requests if r.outcome == "429")

    @property
    def sorry_count(self) -> int:
        return sum(1 for r in self.requests if r.outcome == "sorry")

    @property
    def soft_block_count(self) -> int:
        return sum(1 for r in self.requests if r.outcome == "soft_block")

    @property
    def block_count(self) -> int:
        return sum(1 for r in self.requests if r.outcome in BLOCK_OUTCOMES)

    @property
    def proxy_error_count(self) -> int:
        return sum(1 for r in self.requests if r.outcome == "proxy_error")

    @property
    def google_error_count(self) -> int:
        return sum(1 for r in self.requests if r.outcome == "google_5xx")

    @property
    def avg_latency_ms(self) -> float:
        ok = [r.latency_ms for r in self.requests if r.outcome == "ok"]
        return sum(ok) / len(ok) if ok else 0.0

    @property
    def p95_latency_ms(self) -> float:
        ok = sorted(r.latency_ms for r in self.requests if r.outcome == "ok")
        if not ok:
            return 0.0
        idx = min(len(ok) - 1, math.ceil(len(ok) * 0.95) - 1)
        return ok[idx]

    @property
    def bytes_rx_total(self) -> int:
        return sum(r.bytes_rx for r in self.requests)

    @property
    def bytes_wire_total(self) -> int:
        return sum(r.bytes_wire for r in (self.attempts or self.requests))

    @property
    def bytes_tx_est_total(self) -> int:
        return sum(r.bytes_tx_est for r in (self.attempts or self.requests))

    @property
    def query_status(self) -> str:
        """
        QueryResult.query_status:
        - success: ONLY when no block/error AND stop_reason in {limit, no_new} AND (stop_reason == "limit" or places >= expected_min)
        - partial: places > 0 and stop_reason in {block, parse_error, network, session_lost, other_error, proxy_error, quota, stopped},
                   or when no block/error but places < expected_min and stop_reason not limit/no_new
        - blocked: places == 0 and a block occurred
        - empty: places == 0, no block, stop_reason in {no_new, limit}
        - error: places == 0 and stop_reason in any failure/quota/stopped reason (or error occurred)
        """
        n_places = len(self.places)
        has_block = self.block_count > 0 or self.stop_reason == "block"
        has_error = (
            bool(self.error)
            or self.proxy_error_count > 0
            or self.google_error_count > 0
            or any(r.outcome in ("network_error", "other_error") for r in self.requests)
            or self.stop_reason in (
                "parse_error",
                "network",
                "session_lost",
                "proxy_error",
                "other_error",
                "quota",
                "stopped",
            )
        )

        if n_places == 0:
            if has_block:
                return "blocked"
            if self.stop_reason in ("no_new", "limit") and not has_error:
                return "empty"
            return "error"

        # When n_places > 0:
        if has_block or has_error or self.stop_reason not in ("limit", "no_new"):
            return "partial"

        # n_places > 0, no block, no error, stop_reason in {limit, no_new}
        exp_min = self.expected_min_results if self.expected_min_results > 0 else 1
        if self.stop_reason == "limit" or n_places >= exp_min:
            return "success"
        return "partial"


# ── Sticky proxy session ───────────────────────────────────────────────────────

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br" if brotli is not None else "gzip, deflate",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}


class ProxySession:
    """One aiohttp session pinned to one AnyIP sticky session (same exit IP)."""

    def __init__(
        self,
        country: str = "US",
        use_proxy: bool = True,
        proxy_type: str | None = None,
        session_duration_min: int | None = None,
        sticky: bool = True,
        strict_session: bool = False,
        session_id: str | None = None,   # reuse an existing AnyIP session name
        region: str | None = None,
        city: str | None = None,
        asn: int | str | None = None,
        sessasn_strict: bool = False,
        sessipcollision_strict: bool = False,
        force_close: bool = False,
    ):
        self.country = country.upper()
        self.use_proxy = use_proxy and PROXY.is_configured()
        self.sticky = sticky
        self.strict_session = strict_session
        self.region = region
        self.city = city
        self.asn = asn
        self.sessasn_strict = sessasn_strict
        self.sessipcollision_strict = sessipcollision_strict
        self.force_close = force_close

        raw_type = "none" if not self.use_proxy else (proxy_type or PROXY.proxy_type or "mixed")
        raw_type = str(raw_type).strip().lower()
        self.effective_proxy_type = "mixed" if raw_type in ("", "any") else raw_type

        # Reuse a session name or mint a new one (12 hex chars is valid 1-32 alphanumeric)
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.session_resumed = bool(session_id)
        self.proxy_url: Optional[str] = None
        if self.use_proxy:
            self.proxy_url = PROXY.sticky_url(
                session_id=self.session_id,
                country=self.country,
                proxy_type=proxy_type,
                session_duration_min=session_duration_min,
                sticky=self.sticky,
                strict_session=self.strict_session,
                region=self.region,
                city=self.city,
                asn=self.asn,
                sessasn_strict=self.sessasn_strict,
                sessipcollision_strict=self.sessipcollision_strict,
            )
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "ProxySession":
        connector = aiohttp.TCPConnector(
            ssl=False,
            limit=0,
            force_close=self.force_close,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            auto_decompress=False,
            headers=_BROWSER_HEADERS,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()

    async def get(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        kw = dict(kwargs)
        if self.use_proxy and self.proxy_url:
            kw.setdefault("proxy", self.proxy_url)
        session = self._session
        if session is None:
            raise RuntimeError("ProxySession must be used as an async context manager")
        return await session.get(url, **kw)


# ── Traffic + exit-IP helpers ─────────────────────────────────────────────────────────

_REQ_OVERHEAD_BYTES = 430
_RESP_OVERHEAD_BYTES = 550
_TLS_HANDSHAKE_BYTES = 4600


def _estimate_request_bytes(url: str) -> int:
    return _REQ_OVERHEAD_BYTES + len(url.encode("ascii", errors="replace"))


def _decompress(raw: bytes, encoding: str) -> bytes:
    """Manually inflate the body aiohttp no longer auto-decompresses."""
    enc = (encoding or "").lower().strip()
    try:
        if enc in ("gzip", "x-gzip"):
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if enc == "br" and brotli is not None:
            return brotli.decompress(raw)
    except Exception:
        return raw
    return raw


_DUAL_WITNESSES = [
    ("ipify_v4", "v4", "https://api.ipify.org/?format=json"),
    ("ipify_v6", "v6", "https://api64.ipify.org/?format=json"),
]

_MULTI_WITNESSES = [
    ("ipify_v4", "v4", "https://api.ipify.org/?format=json"),
    ("ipify_v6", "v6", "https://api64.ipify.org/?format=json"),
    ("amazon", "v4", "https://checkip.amazonaws.com"),
    ("httpbin", "v4", "https://httpbin.org/ip"),
    ("ifconfig", "v4", "https://ifconfig.co/ip"),
]


def _ip_family(ip: str, fallback: str = "") -> str:
    """Derive IP family from address: 'v6' iff ':' in ip, 'v4' if ip, else fallback."""
    cleaned = (ip or "").strip()
    if not cleaned:
        return fallback
    return "v6" if ":" in cleaned else "v4"


async def _probe_one(
    session: ProxySession,
    name: str,
    family: str,
    url: str,
    request_index: int,
) -> IpObservation:
    ts = time.time()
    t0 = time.monotonic()
    try:
        async with await session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            raw = await resp.read()
            latency = (time.monotonic() - t0) * 1000
            text = raw.decode("utf-8", errors="replace").strip()
            ip_str = ""
            if text.startswith("{"):
                try:
                    data = json.loads(text)
                    ip_str = str(data.get("ip") or data.get("origin") or "").strip()
                except Exception:
                    ip_str = text.strip("\"' \r\n")
            else:
                ip_str = text.strip("\"' \r\n")
            if "," in ip_str:
                ip_str = ip_str.split(",")[0].strip()
            error = "" if resp.status == 200 else f"HTTP {resp.status}"
            if not error:
                try:
                    ip_str = str(ipaddress.ip_address(ip_str))
                except ValueError:
                    error = "Invalid IP witness response"
            if error:
                ip_str = ""
            resolved_family = _ip_family(ip_str, fallback=family)
            return IpObservation(
                ts=ts,
                request_index=request_index,
                witness=name,
                family=resolved_family,
                ip=ip_str,
                latency_ms=latency,
                error=error,
            )
    except Exception as exc:
        latency = (time.monotonic() - t0) * 1000
        return IpObservation(
            ts=ts,
            request_index=request_index,
            witness=name,
            family=family,
            ip="",
            latency_ms=latency,
            error=str(exc) or type(exc).__name__,
        )


async def _exit_ips(
    session: ProxySession,
    request_index: int = 0,
    mode: str = "dual",
) -> list[IpObservation]:
    """Witness set of exit IP observations (dual: v4 + v6; multi: dual + amazon + httpbin + ifconfig)."""
    witnesses = _MULTI_WITNESSES if mode == "multi" else _DUAL_WITNESSES
    tasks = [
        _probe_one(session, name, family, url, request_index)
        for name, family, url in witnesses
    ]
    return await asyncio.gather(*tasks)


async def _exit_ip(session: ProxySession) -> tuple[str, int]:
    """Legacy wrapper for callers expecting (ip, billed_bytes)."""
    observations = await _exit_ips(session, request_index=0, mode="dual")
    first_v4 = ""
    for obs in observations:
        if obs.family == "v4" and obs.ip and not first_v4:
            first_v4 = obs.ip
    billed = len(observations) * 800
    return first_v4, billed


# ── Validation ────────────────────────────────────────────────────────────────

_PLACE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-_]{10,}$")

_COUNTRY_BBOX = {
    "US": (-171.0, 17.5, -65.0, 72.5),
    "IN": (67.5, 6.0, 98.5, 36.5),
}


def _validate_place(place: dict, country: str) -> list[str]:
    """Structural + geo-sanity checks on one extracted place. Returns issues."""
    issues: list[str] = []
    if not _PLACE_ID_RE.match(str(place.get("place_id") or "")):
        issues.append("bad_place_id")
    if not str(place.get("title") or "").strip():
        issues.append("missing_title")
    if not str(place.get("address") or "").strip():
        issues.append("missing_address")

    lat, lng = place.get("lat"), place.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        issues.append("bad_geo")
    else:
        box = _COUNTRY_BBOX.get(country.upper())
        if box:
            lo_lng, lo_lat, hi_lng, hi_lat = box
            if not (lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng):
                issues.append("out_of_bbox")
    return issues


# ── HTTP helper with retries ───────────────────────────────────────────────────

async def _timed_get(
    session: ProxySession,
    url: str,
    *args,
    kind: str = "google",
    request_index: int = 0,
    page_start: int = 0,
    timeout: int = 30,
    retries: int = 2,
    result: QueryResult | None = None,
    **kwargs,
) -> tuple[RequestRecord, bytes]:
    """Execute timed GET with retries according to spec rules:
    - Every attempt creates a RequestRecord with attempt index and is appended to result.attempts.
    - Retry only on exceptions/timeouts and proxy_error (502/503/504/407).
    - Never retry 403/429/consent/sorry/soft_block.
    - Final record is returned and also appended by caller to result.requests once.
    - On exhausted exceptions: outcome network_error, status 0, error text.
    """
    if args:
        if len(args) >= 1:
            timeout = args[0]
        if len(args) >= 2:
            retries = args[1]

    last_record: RequestRecord | None = None
    last_body: bytes = b""
    guard = request_guard.get()

    for attempt in range(retries + 1):
        if guard is not None and not await guard.admit():
            if last_record is not None:
                last_record.is_final = True
                return last_record, last_body
            return RequestRecord(url=url, status=0, outcome="stopped", latency_ms=0,
                                 bytes_rx=0, kind=kind, request_index=request_index,
                                 page_start=page_start), b""
        ts = time.time()
        t0 = time.monotonic()
        try:
            async with await session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                raw = await resp.read()
                latency = (time.monotonic() - t0) * 1000
                enc = resp.headers.get("Content-Encoding", "")
                body = _decompress(raw, enc)
                headers_dict = dict(resp.headers)
                outcome = _classify_response(resp.status, body, headers=headers_dict)
                if guard is not None:
                    guard.observe(outcome)
                anyip_err = (
                    resp.headers.get("X-Anyip-Error", "")
                    or resp.headers.get("x-anyip-error", "")
                )
                content_type = resp.headers.get("Content-Type", "")
                server = resp.headers.get("Server", "")
                retry_after = resp.headers.get("Retry-After", "")
                resp_hash = hashlib.sha256(raw).hexdigest()[:16]

                is_final_attempt = (attempt == retries) or (outcome != "proxy_error")
                rec = RequestRecord(
                    url=url,
                    status=resp.status,
                    outcome=outcome,
                    latency_ms=latency,
                    bytes_rx=len(body),
                    bytes_wire=len(raw),
                    bytes_tx_est=_estimate_request_bytes(url),
                    content_encoding=enc or "identity",
                    kind=kind,
                    request_index=request_index,
                    page_start=page_start,
                    attempt=attempt,
                    anyip_error=anyip_err,
                    content_type=content_type,
                    server=server,
                    retry_after=retry_after,
                    response_hash=resp_hash,
                    error="",
                    ts=ts,
                    is_final=is_final_attempt,
                )
                if result is not None:
                    result.attempts.append(rec)

                last_record = rec
                last_body = body

                if outcome == "proxy_error" and attempt < retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue

                return rec, body
        except Exception as exc:
            latency = (time.monotonic() - t0) * 1000
            err_str = str(exc) or type(exc).__name__
            is_final_attempt = (attempt == retries)
            proxy_failure = isinstance(exc, aiohttp.ClientHttpProxyError)
            if guard is not None:
                guard.observe("proxy_error" if proxy_failure else "network_error")
            rec = RequestRecord(
                url=url,
                status=exc.status if proxy_failure else 0,
                outcome="proxy_error" if proxy_failure else "network_error",
                latency_ms=latency,
                bytes_rx=0,
                bytes_wire=0,
                bytes_tx_est=_estimate_request_bytes(url),
                content_encoding="identity",
                kind=kind,
                request_index=request_index,
                page_start=page_start,
                attempt=attempt,
                anyip_error=(exc.headers or {}).get("x-anyip-error", "") if proxy_failure else "",
                content_type="",
                server="",
                retry_after="",
                response_hash="",
                error=err_str,
                ts=ts,
                is_final=is_final_attempt,
            )
            if result is not None:
                result.attempts.append(rec)

            last_record = rec
            last_body = b""

            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return rec, b""

    if last_record is None:
        last_record = RequestRecord(
            url=url,
            status=0,
            outcome="network_error",
            latency_ms=0.0,
            bytes_rx=0,
            error="Exhausted retries",
            kind=kind,
            request_index=request_index,
            page_start=page_start,
        )
    return last_record, last_body


# ── Core scraper ───────────────────────────────────────────────────────────────

_PB_URL_RE = re.compile(r'<link href="(/search\?tbm=map[^"]+)"')
_JSON_STRIP = re.compile(r"^\)\]}'")


async def scrape_query(
    query: str,
    country: str = "US",
    lang: str = "en",
    limit: int = 40,
    use_proxy: bool = True,
    proxy_type: str | None = None,
    session_duration_min: int | None = None,
    sticky: bool = True,
    strict_session: bool = False,
    session_id: str | None = None,
    session_name: str | None = None,  # alias of session_id
    max_pages: int = 0,               # 0 = until limit/no_new
    ip_probe_every: int = 0,          # 0 = start/end only
    witness_mode: str = "dual",
    force_close: bool = False,
    sessasn_strict: bool = False,
    sessipcollision_strict: bool = False,
    region: str | None = None,
    city: str | None = None,
    asn: int | str | None = None,
    expected_min_results: int = 0,
) -> QueryResult:
    """Scrape Google Maps for `query` over one sticky proxy session."""
    t_wall0 = time.monotonic()
    effective_session_id = session_id or session_name
    target_pages = max_pages or (math.ceil(limit / 20) if limit > 0 else 0)

    async with ProxySession(
        country=country,
        use_proxy=use_proxy,
        proxy_type=proxy_type,
        session_duration_min=session_duration_min,
        sticky=sticky,
        strict_session=strict_session,
        session_id=effective_session_id,
        region=region,
        city=city,
        asn=asn,
        sessasn_strict=sessasn_strict,
        sessipcollision_strict=sessipcollision_strict,
        force_close=force_close,
    ) as session:
        result = QueryResult(
            query=query,
            country=country,
            proxy_session_id=session.session_id,
            proxy_type=session.effective_proxy_type,
            target_pages=target_pages,
            expected_min_results=expected_min_results,
            session_started_at=time.time(),
        )
        if session.use_proxy:
            result.tls_handshake_bytes = 2 * _TLS_HANDSHAKE_BYTES

        # ── Step 0: start exit IP observations (request_index 0) ───────────
        start_obs = await _exit_ips(session, request_index=0, mode=witness_mode)
        result.ip_observations.extend(start_obs)
        result.probe_bytes_billed += len(start_obs) * 800
        for obs in start_obs:
            if obs.family == "v4" and obs.ip and not result.exit_ip_start:
                result.exit_ip_start = obs.ip
        if not result.exit_ip_start and start_obs:
            result.exit_ip_start = start_obs[0].ip

        # ── Scrape SPA and pages ───────────────────────────────────────────
        result = await _scrape_into(
            session,
            result,
            lang=lang,
            limit=limit,
            max_pages=max_pages,
            ip_probe_every=ip_probe_every,
            witness_mode=witness_mode,
        )

        # ── Step 3: end exit IP observations ───────────────────────────────
        last_req_index = result.requests[-1].request_index if result.requests else 0
        end_obs = await _exit_ips(session, request_index=last_req_index, mode=witness_mode)
        result.ip_observations.extend(end_obs)
        result.probe_bytes_billed += len(end_obs) * 800
        for obs in end_obs:
            if obs.family == "v4" and obs.ip and not result.exit_ip_end:
                result.exit_ip_end = obs.ip
        if not result.exit_ip_end and end_obs:
            result.exit_ip_end = end_obs[0].ip

        result.session_age_s = time.time() - result.session_started_at

    result.query_wall_ms = (time.monotonic() - t_wall0) * 1000
    return result


async def _scrape_into(
    session: ProxySession,
    result: QueryResult,
    lang: str,
    limit: int,
    max_pages: int = 0,
    ip_probe_every: int = 0,
    witness_mode: str = "dual",
) -> QueryResult:
    query = result.query
    country = result.country

    # Logical Google request index starts at 1 for SPA and increments for each page.
    logical_req_index = 1

    # ── Step 1: SPA request (acquire session token) ─────────────────────
    spa_url = (
        f"https://www.google.com/maps/search/{quote(query, safe='')}?"
        + urlencode({"hl": lang, "gl": country.lower()})
    )
    rec, body = await _timed_get(
        session,
        spa_url,
        kind="spa",
        request_index=logical_req_index,
        page_start=0,
        result=result,
    )
    if rec.outcome == "stopped":
        result.stop_reason = "stopped"
        return result
    result.requests.append(rec)

    if rec.outcome in BLOCK_OUTCOMES:
        result.stop_reason = "block"
        result.block_at_request = logical_req_index
        result.block_at_page = 1
        result.partial = False
        result.error = f"SPA blocked: {rec.outcome}"
        return result

    if rec.outcome == "network_error":
        result.stop_reason = "network"
        result.partial = False
        result.error = rec.error or "Network error on SPA"
        return result

    if rec.outcome == "proxy_error":
        result.stop_reason = "proxy_error"
        result.partial = False
        result.error = rec.error or rec.anyip_error or "Proxy error on SPA"
        return result

    if rec.outcome != "ok":
        result.stop_reason = "other_error"
        result.partial = False
        result.error = f"SPA request failed: {rec.outcome}"
        return result

    match = _PB_URL_RE.search(body.decode("utf-8", errors="replace"))
    if not match:
        result.stop_reason = "parse_error"
        result.error = "Could not extract tbm=map URL from SPA"
        result.partial = False
        return result

    base_url = "https://www.google.com" + match.group(1).replace("&amp;", "&")

    # ── Step 2: Paginate tbm=map JSON ────────────────────────────────────
    start = 0
    seen_ids: set[str] = set()
    page_no = 0

    while True:
        page_no += 1
        logical_req_index += 1

        rec, body = await _timed_get(
            session,
            _paginate_url(base_url, start),
            kind="page",
            request_index=logical_req_index,
            page_start=start,
            result=result,
        )
        if rec.outcome == "stopped":
            result.stop_reason = "stopped"
            result.partial = bool(result.places)
            break
        result.requests.append(rec)

        if rec.outcome in BLOCK_OUTCOMES:
            result.stop_reason = "block"
            result.block_at_request = logical_req_index
            result.block_at_page = page_no
            result.partial = bool(result.places)
            result.error = f"Page {page_no} blocked: {rec.outcome}"
            break

        if rec.outcome == "network_error":
            result.stop_reason = "network"
            result.partial = bool(result.places)
            result.error = rec.error or f"Network error on page {page_no}"
            break

        if rec.outcome == "proxy_error":
            result.stop_reason = "proxy_error"
            result.partial = bool(result.places)
            result.error = rec.error or rec.anyip_error or f"Proxy error on page {page_no}"
            break

        if rec.outcome != "ok":
            result.stop_reason = "other_error"
            result.partial = bool(result.places)
            result.error = f"Page {page_no} failed: {rec.outcome}"
            break

        text = _JSON_STRIP.sub("", body.decode("utf-8", errors="replace").strip())
        try:
            data = json.loads(text)
            items = data[64]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            result.stop_reason = "parse_error"
            result.partial = bool(result.places)
            result.error = f"Parse error on page {page_no}"
            break

        if not items:
            result.stop_reason = "no_new"
            break

        new_found = False
        malformed = False
        for item in items:
            try:
                d = item[1]
                place_id = (
                    d[78]
                    if (isinstance(d, list) and len(d) > 78)
                    else (d.get(78) or d.get("78") if isinstance(d, dict) else None)
                )
                if not place_id:
                    malformed = True
                    continue
                if place_id in seen_ids:
                    continue
                seen_ids.add(place_id)
                new_found = True
                place = {
                    "place_id": place_id,
                    "title": _safe(d, 11),
                    "category": _safe(d, 13, 0),
                    "address": _safe(d, 39),
                    "phone": _safe(d, 178, 0, 1, 0, 0),
                    "website": _safe(d, 7, 0),
                    "stars": _safe(d, 4, 7),
                    "reviews": _safe(d, 37, 1),
                    "lat": _safe(d, 9, 2),
                    "lng": _safe(d, 9, 3),
                }
            except Exception:
                malformed = True
                result.validation["parse_error"] += 1
                continue

            issues = _validate_place(place, country)
            result.validation["valid" if not issues else issues[0]] += 1
            for other in issues[1:]:
                result.validation[other] += 1
            place["validation"] = ",".join(issues) if issues else "ok"
            result.places.append(place)
            if limit > 0 and len(result.places) >= limit:
                break

        if not new_found:
            result.stop_reason = "parse_error" if malformed else "no_new"
            if malformed:
                result.partial = bool(result.places)
                result.error = f"Malformed place records on page {page_no}"
            break

        start += 20
        if max_pages > 0 and page_no >= max_pages:
            result.stop_reason = "limit"
            break
        if limit > 0 and len(result.places) >= limit:
            result.stop_reason = "limit"
            break

        if ip_probe_every > 0 and (logical_req_index % ip_probe_every == 0):
            probes = await _exit_ips(session, request_index=logical_req_index, mode=witness_mode)
            result.ip_observations.extend(probes)
            result.probe_bytes_billed += len(probes) * 800

        await asyncio.sleep(0.4)  # polite inter-page delay

    if not result.stop_reason:
        if (max_pages > 0 and page_no >= max_pages) or (limit > 0 and len(result.places) >= limit):
            result.stop_reason = "limit"
        else:
            result.stop_reason = "no_new"

    return result


def _paginate_url(base_url: str, start: int) -> str:
    if start == 0:
        return base_url
    for pat, repl in [
        (r"%218i\d+", f"%218i{start}"),
        (r"!8i\d+", f"!8i{start}"),
    ]:
        new_url, n = re.subn(pat, repl, base_url)
        if n:
            return new_url
    if "%217i20" in base_url:
        return base_url.replace("%217i20", f"%217i20%218i{start}")
    if "!7i20" in base_url:
        return base_url.replace("!7i20", f"!7i20!8i{start}")
    return base_url + f"&start={start}"


def _safe(obj, *keys, default=None):
    for k in keys:
        try:
            if isinstance(obj, dict) and k not in obj:
                if str(k) in obj:
                    obj = obj[str(k)]
                    continue
            obj = obj[k]
        except (KeyError, IndexError, TypeError):
            return default
    return obj


async def async_main():
    from pathlib import Path

    print("=" * 60)
    print("Google Maps Scraper - Local Test (No Proxy)")
    print("=" * 60)

    try:
        query_input = input("Enter search query (default: 'coffee in Seattle'): ").strip()
    except (EOFError, KeyboardInterrupt):
        query_input = ""

    if not query_input:
        query_input = "coffee in Seattle"

    queries = [q.strip() for q in query_input.split(",") if q.strip()]

    concurrency = 3
    semaphore = asyncio.Semaphore(concurrency)
    print(f"\nRunning {len(queries)} query(ies) with low concurrency ({concurrency})...\n")

    async def run_one(q: str) -> QueryResult:
        async with semaphore:
            print(f"[*] Scraping: '{q}'...")
            return await scrape_query(query=q, limit=10, use_proxy=False)

    results = await asyncio.gather(*(run_one(q) for q in queries))

    # Show results
    print("\n" + "=" * 60)
    print("SCRAPE RESULTS")
    print("=" * 60)

    for res in results:
        status = res.query_status.upper()
        print(f"\nQuery: {res.query}")
        print(f"Status: {status} | Places found: {len(res.places)} | Requests: {res.total_requests} (OK: {res.ok_count})")
        if res.error:
            print(f"Error: {res.error}")

        if res.places:
            print("\nPlaces:")
            for i, p in enumerate(res.places, 1):
                stars = f"★ {p['stars']}" if p.get('stars') else "No rating"
                reviews = f"({p['reviews']} reviews)" if p.get('reviews') else ""
                print(f"  {i}. {p.get('title')} [{p.get('category') or 'Unknown'}] - {stars} {reviews}")
                if p.get("address"):
                    print(f"     Address: {p.get('address')}")
                if p.get("phone"):
                    print(f"     Phone: {p.get('phone')}")
                if p.get("website"):
                    print(f"     Website: {p.get('website')}")

    # Save to results/temp/ folder
    results_dir = Path(__file__).resolve().parent / "results" / "temp"
    results_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^\w\-]+", "_", queries[0].lower()).strip("_")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    file_path = results_dir / f"test_{safe_name}_{timestamp}.json"

    payload = {
        "test_name": f"test_{safe_name}_{timestamp}",
        "timestamp": timestamp,
        "concurrency": concurrency,
        "use_proxy": False,
        "queries": [
            {
                "query": r.query,
                "country": r.country,
                "places_count": len(r.places),
                "requests_count": r.total_requests,
                "ok_count": r.ok_count,
                "consent_count": r.consent_count,
                "error": r.error,
                "status": r.query_status,
                "places": r.places,
            }
            for r in results
        ],
    }

    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"[✓] Saved test result to: {file_path}")
    print("=" * 60)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
