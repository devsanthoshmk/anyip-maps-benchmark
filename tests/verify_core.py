"""
Independent verification test suite for AnyIP gmap-bench core modules:
- config.py
- scraper.py
- report.py
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from unittest.mock import patch, MagicMock

import pytest

from config import ProxyConfig
from scraper import (
    _classify_response,
    _timed_get,
    _exit_ips,
    _exit_ip,
    _ip_family,
    _probe_one,
    _scrape_into,
    scrape_query,
    QueryResult,
    RequestRecord,
    IpObservation,
    BLOCK_OUTCOMES,
)
from report import BenchmarkReport


# ==============================================================================
# 1. config.py Verification
# ==============================================================================

@pytest.fixture
def cfg() -> ProxyConfig:
    return ProxyConfig(
        host="portal.anyip.io",
        port=1080,
        proxy_user="testuser",
        proxy_pass="secretpass",
        proxy_id="acc123",
        rotate_hash="hash456",
        proxy_type="residential",
        session_duration_min=20,
    )


def test_sticky_url_exact_token_order(cfg: ProxyConfig):
    # Residential
    u_res = cfg.sticky_url("s1", country="US", proxy_type="residential")
    assert "user_testuser,type_residential,country_US,session_s1,sesstime_20" in u_res

    # Mobile
    u_mob = cfg.sticky_url("s1", country="IN", proxy_type="mobile")
    assert "user_testuser,type_mobile,country_IN,session_s1,sesstime_20" in u_mob

    # Mixed -> type_ omitted
    u_mix = cfg.sticky_url("s1", country="US", proxy_type="mixed")
    assert "user_testuser,country_US,session_s1,sesstime_20" in u_mix
    assert "type_" not in u_mix

    # "any" and "" normalize to mixed -> type_ omitted
    u_any = cfg.sticky_url("s1", country="US", proxy_type="any")
    assert "user_testuser,country_US,session_s1,sesstime_20" in u_any
    assert "type_" not in u_any

    u_empty = cfg.sticky_url("s1", country="US", proxy_type="")
    assert "user_testuser,country_US,session_s1,sesstime_20" in u_empty
    assert "type_" not in u_empty

    # Region, city, asn ordering
    u_geo = cfg.sticky_url(
        "s1",
        country="us",
        proxy_type="residential",
        region="California",
        city="Los Angeles",
        asn="AS7018",
    )
    expected_geo = (
        "user_testuser,type_residential,country_US,region_california,"
        "city_los angeles,asn_7018,session_s1,sesstime_20"
    )
    assert expected_geo in u_geo

    # Strict flags ordering: sessreplace_false, sessasn_strict, sessipcollision_strict
    u_strict = cfg.sticky_url(
        "s1",
        country="US",
        proxy_type="residential",
        strict_session=True,
        sessasn_strict=True,
        sessipcollision_strict=True,
    )
    expected_strict = "session_s1,sesstime_20,sessreplace_false,sessasn_strict,sessipcollision_strict"
    assert expected_strict in u_strict


def test_sticky_url_session_validation(cfg: ProxyConfig):
    # 1 char -> valid
    cfg.sticky_url("A")
    # 31 chars -> valid
    cfg.sticky_url("A" * 31)
    # 32 chars -> valid
    cfg.sticky_url("A" * 32)

    # 33 chars -> ValueError
    with pytest.raises(ValueError):
        cfg.sticky_url("A" * 33)

    # Punctuation / whitespace -> ValueError
    for invalid in ["sess_1", "sess-1", "sess.1", "sess 1", "sess@1", "", "   "]:
        with pytest.raises(ValueError):
            cfg.sticky_url(invalid)

    # Non-string types -> ValueError
    for non_str in [None, 12345, ["s1"], {"id": "s1"}]:
        with pytest.raises(ValueError):
            cfg.sticky_url(non_str)  # type: ignore


def test_sticky_url_invalid_proxy_types(cfg: ProxyConfig):
    # "none" is invalid and must raise ValueError
    for bad_type in ["none", "NONE", "None", "datacenter", "invalid"]:
        with pytest.raises(ValueError):
            cfg.sticky_url("s1", proxy_type=bad_type)


def test_rotate_url_behavior(cfg: ProxyConfig):
    assert cfg.has_rotate() is True
    r_url = cfg.rotate_url("testSess1")
    assert r_url == "https://dashboard.anyip.io/api/proxy_accounts/acc123/invalidate/hash456/session/testSess1"

    with pytest.raises(ValueError):
        cfg.rotate_url("bad-sess!")

    unconfigured = ProxyConfig(proxy_id="", rotate_hash="")
    assert unconfigured.has_rotate() is False
    assert unconfigured.rotate_url("testSess1") is None


# ==============================================================================
# 2. scraper.py: _classify_response Verification
# ==============================================================================

def test_classify_response_standard_codes():
    assert _classify_response(0) == "network_error"
    assert _classify_response(403) == "403"
    assert _classify_response(429) == "429"
    assert _classify_response(407) == "proxy_error"
    assert _classify_response(400) == "proxy_error"
    assert _classify_response(409) == "proxy_error"
    assert _classify_response(413) == "proxy_error"
    assert _classify_response(502) == "proxy_error"
    assert _classify_response(503) == "proxy_error"
    assert _classify_response(504) == "proxy_error"
    assert _classify_response(500) == "google_5xx"
    assert _classify_response(501) == "google_5xx"
    assert _classify_response(505) == "google_5xx"
    assert _classify_response(506) == "google_5xx"
    assert _classify_response(301) == "other_error"
    assert _classify_response(302) == "other_error"
    assert _classify_response(404) == "other_error"
    assert _classify_response(200, b"normal content") == "ok"


def test_classify_response_signals_and_precedence():
    # Consent signals
    for sig in [
        b"consent.google.com",
        b"before you continue",
        b"agree to google",
        b"To use Google Maps",
    ]:
        assert _classify_response(200, sig) == "consent"

    # Sorry signals
    for sig in [
        b"/sorry/index",
        b"unusual traffic",
        b"detected unusual traffic",
        b"our systems have detected",
        b"recaptcha",
        b"automated queries",
    ]:
        assert _classify_response(200, sig) == "sorry"

    # Soft block signals (only without tbm=map)
    assert _classify_response(200, b"why did this happen") == "soft_block"
    assert _classify_response(200, b"troubleshooting") == "soft_block"
    # With tbm=map -> ok
    assert _classify_response(200, b"why did this happen <a href='/search?tbm=map'>Maps</a>") == "ok"
    assert _classify_response(200, b"troubleshooting /search?tbm=map&pb=1") == "ok"

    # Adversarial precedence: Consent > Sorry > Soft block
    assert _classify_response(200, b"consent.google.com and unusual traffic") == "consent"
    assert _classify_response(200, b"unusual traffic and why did this happen") == "sorry"
    assert _classify_response(200, b"consent.google.com /sorry/index troubleshooting") == "consent"

    # Status 403 / 429 takes precedence over 200 body text
    assert _classify_response(403, b"consent.google.com") == "403"
    assert _classify_response(429, b"unusual traffic") == "429"
    assert _classify_response(502, b"consent.google.com") == "proxy_error"


def test_classify_response_headers():
    # 200 response must NOT be overridden by X-Anyip-Error
    assert _classify_response(200, b"normal", {"X-Anyip-Error": "Rate limit warning"}) == "ok"
    # Non-200 with X-Anyip-Error becomes proxy_error
    assert _classify_response(404, b"Not found", {"X-Anyip-Error": "Session died"}) == "proxy_error"
    assert _classify_response(500, b"Error", {"x-anyip-error": "AnyIP gateway fault"}) == "proxy_error"


# ==============================================================================
# 3. scraper.py: _timed_get Verification
# ==============================================================================

class _MockResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class _MockSession:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_timed_get_retries_and_attempt_records():
    async def run():
        # Retry on 502 -> then 200
        sess = _MockSession([
            _MockResponse(502, b"Bad Gateway", {"X-Anyip-Error": "Gateway timeout"}),
            _MockResponse(200, b"ok maps data"),
        ])
        res = QueryResult(query="test", country="US", proxy_session_id="s1")
        rec, body = await _timed_get(
            sess,  # type: ignore
            "https://www.google.com/test",
            kind="page",
            request_index=2,
            page_start=20,
            retries=2,
            result=res,
        )
        assert sess.calls == 2
        assert len(res.attempts) == 2
        assert res.attempts[0].status == 502
        assert res.attempts[0].outcome == "proxy_error"
        assert res.attempts[0].attempt == 0
        assert res.attempts[0].is_final is False
        assert res.attempts[0].anyip_error == "Gateway timeout"
        assert res.attempts[1].status == 200
        assert res.attempts[1].outcome == "ok"
        assert res.attempts[1].attempt == 1
        assert res.attempts[1].is_final is True
        assert rec.status == 200
        assert body == b"ok maps data"

    asyncio.run(run())


def test_timed_get_never_retries_blocks_or_consent():
    async def run():
        for block_code in [403, 429]:
            sess = _MockSession([
                _MockResponse(block_code, b"Blocked"),
                _MockResponse(200, b"unreachable"),
            ])
            res = QueryResult(query="test", country="US", proxy_session_id="s1")
            rec, _ = await _timed_get(
                sess,  # type: ignore
                "https://www.google.com/test",
                retries=2,
                result=res,
            )
            assert sess.calls == 1
            assert len(res.attempts) == 1
            assert rec.status == block_code
            assert rec.outcome == str(block_code)

        # Consent on 200
        sess_c = _MockSession([
            _MockResponse(200, b"consent.google.com"),
            _MockResponse(200, b"unreachable"),
        ])
        res_c = QueryResult(query="test", country="US", proxy_session_id="s1")
        rec_c, _ = await _timed_get(
            sess_c,  # type: ignore
            "https://www.google.com/test",
            retries=2,
            result=res_c,
        )
        assert sess_c.calls == 1
        assert len(res_c.attempts) == 1
        assert rec_c.outcome == "consent"

    asyncio.run(run())


def test_timed_get_exhausted_network_exceptions():
    async def run():
        sess = _MockSession([
            ConnectionResetError("Socket reset"),
            asyncio.TimeoutError("Timeout error"),
        ])
        res = QueryResult(query="test", country="US", proxy_session_id="s1")
        rec, body = await _timed_get(
            sess,  # type: ignore
            "https://www.google.com/test",
            retries=1,
            result=res,
        )
        assert sess.calls == 2
        assert len(res.attempts) == 2
        assert rec.status == 0
        assert rec.outcome == "network_error"
        assert body == b""
        assert rec.is_final is True

    asyncio.run(run())


# ==============================================================================
# 4. scraper.py: QueryResult.query_status Matrix Verification
# ==============================================================================

def _make_req(outcome: str, status: int = 200, req_idx: int = 1) -> RequestRecord:
    return RequestRecord(
        url="https://google.com",
        status=status,
        outcome=outcome,
        latency_ms=100.0,
        bytes_rx=1000,
        request_index=req_idx,
    )


def test_query_status_success_cases():
    # Places >= expected_min_results (default 1), stop_reason in {limit, no_new}
    r1 = QueryResult(
        query="test", country="US", proxy_session_id="s1",
        places=[{"place_id": "p1"}, {"place_id": "p2"}],
        requests=[_make_req("ok")],
        stop_reason="limit",
    )
    assert r1.query_status == "success"

    r2 = QueryResult(
        query="test", country="US", proxy_session_id="s1",
        places=[{"place_id": "p1"}],
        requests=[_make_req("ok")],
        stop_reason="no_new",
        expected_min_results=1,
    )
    assert r2.query_status == "success"

    # Target depth reached (stop_reason="limit"), success even if places < expected_min_results
    r3 = QueryResult(
        query="test", country="US", proxy_session_id="s1",
        places=[{"place_id": "p1"}],
        requests=[_make_req("ok")],
        stop_reason="limit",
        expected_min_results=10,
    )
    assert r3.query_status == "success"


def test_query_status_partial_cases():
    # Pagination block after places
    r_blk = QueryResult(
        query="test", country="US", proxy_session_id="s1",
        places=[{"place_id": "p1"}],
        requests=[_make_req("ok", req_idx=1), _make_req("403", 403, req_idx=2)],
        stop_reason="block",
        partial=True,
        block_at_request=2,
        block_at_page=1,
    )
    assert r_blk.query_status == "partial"

    # Fewer than expected when stopped on no_new (not limit)
    r_under = QueryResult(
        query="test", country="US", proxy_session_id="s1",
        places=[{"place_id": "p1"}],
        requests=[_make_req("ok")],
        stop_reason="no_new",
        expected_min_results=10,
    )
    assert r_under.query_status == "partial"

    # Errors after places gathered
    for err in ["parse_error", "network", "session_lost", "other_error", "proxy_error", "quota", "stopped"]:
        r_err = QueryResult(
            query="test", country="US", proxy_session_id="s1",
            places=[{"place_id": "p1"}],
            requests=[_make_req("ok")],
            stop_reason=err,
            partial=True,
        )
        assert r_err.query_status == "partial"


def test_query_status_blocked_cases():
    # places == 0 and first Google request/stop reason is a block
    for blk in ["403", "429", "consent", "sorry", "soft_block"]:
        r = QueryResult(
            query="test", country="US", proxy_session_id="s1",
            places=[],
            requests=[_make_req(blk, 403 if blk == "403" else 429 if blk == "429" else 200)],
            stop_reason="block",
            block_at_request=1,
            block_at_page=1,
        )
        assert r.query_status == "blocked"


def test_query_status_empty_cases():
    # places == 0, no block, stop_reason in {no_new, limit}
    r_no_new = QueryResult(
        query="test", country="US", proxy_session_id="s1",
        places=[],
        requests=[_make_req("ok")],
        stop_reason="no_new",
    )
    assert r_no_new.query_status == "empty"

    r_limit = QueryResult(
        query="test", country="US", proxy_session_id="s1",
        places=[],
        requests=[_make_req("ok")],
        stop_reason="limit",
    )
    assert r_limit.query_status == "empty"


def test_query_status_error_cases():
    # places == 0, no block, stop_reason in {parse_error, network, session_lost, other_error, proxy_error, quota, stopped}
    for err in ["parse_error", "network", "session_lost", "other_error", "proxy_error", "quota", "stopped"]:
        r = QueryResult(
            query="test", country="US", proxy_session_id="s1",
            places=[],
            requests=[_make_req("ok")],
            stop_reason=err,
        )
        assert r.query_status == "error"


# ==============================================================================
# 5. scraper.py: scrape_query Simulations Verification
# ==============================================================================

def test_scrape_query_simulations():
    async def run():
        spa_ok = b'<html><link href="/search?tbm=map&amp;pb=1"></html>'

        def make_page_json(place_ids):
            items = []
            for pid in place_ids:
                item = [None, {
                    78: pid,
                    11: f"Title {pid}",
                    13: ["Cafe"],
                    39: "123 Main St",
                    9: [0, 0, 47.6, -122.3],
                }]
                items.append(item)
            data = [None] * 65
            data[64] = items
            return b")]}'\n" + json.dumps(data).encode("utf-8")

        # Simulated 2-page success
        sess1 = _MockSession([
            _MockResponse(200, spa_ok),
            _MockResponse(200, make_page_json([f"p{i}" for i in range(20)])),
            _MockResponse(200, make_page_json([f"p{i}" for i in range(20, 35)])),
        ])
        res1 = QueryResult(query="coffee", country="US", proxy_session_id="s1")
        res1 = await _scrape_into(sess1, res1, lang="en", limit=40, max_pages=2)  # type: ignore
        assert len(res1.places) == 35
        assert len(res1.requests) == 3
        assert res1.requests[0].kind == "spa"
        assert res1.requests[0].request_index == 1
        assert res1.requests[1].kind == "page"
        assert res1.requests[1].request_index == 2
        assert res1.requests[2].kind == "page"
        assert res1.requests[2].request_index == 3
        assert res1.stop_reason == "limit"
        assert res1.query_status == "success"

        # Simulated Page-2 Block
        sess2 = _MockSession([
            _MockResponse(200, spa_ok),
            _MockResponse(200, make_page_json([f"p{i}" for i in range(20)])),
            _MockResponse(403, b"Forbidden"),
        ])
        res2 = QueryResult(query="coffee", country="US", proxy_session_id="s1")
        res2 = await _scrape_into(sess2, res2, lang="en", limit=40, max_pages=2)  # type: ignore
        assert len(res2.places) == 20
        assert res2.stop_reason == "block"
        assert res2.block_at_request == 3
        assert res2.block_at_page == 2
        assert res2.partial is True
        assert res2.query_status == "partial"

        # Simulated SPA Block
        sess3 = _MockSession([
            _MockResponse(429, b"Too Many"),
        ])
        res3 = QueryResult(query="coffee", country="US", proxy_session_id="s1")
        res3 = await _scrape_into(sess3, res3, lang="en", limit=40, max_pages=2)  # type: ignore
        assert len(res3.places) == 0
        assert res3.stop_reason == "block"
        assert res3.block_at_request == 1
        assert res3.block_at_page == 1
        assert res3.partial is False
        assert res3.query_status == "blocked"

        # Simulated SPA Parse Failure
        sess4 = _MockSession([
            _MockResponse(200, b"<html>No link</html>"),
        ])
        res4 = QueryResult(query="coffee", country="US", proxy_session_id="s1")
        res4 = await _scrape_into(sess4, res4, lang="en", limit=40, max_pages=2)  # type: ignore
        assert len(res4.places) == 0
        assert res4.stop_reason == "parse_error"
        assert res4.partial is False
        assert res4.query_status == "error"

    asyncio.run(run())


# ==============================================================================
# 6. report.py Verification
# ==============================================================================

def test_report_dict_compatibility():
    r_success = QueryResult(
        query="coffee", country="US", proxy_session_id="s1",
        places=[{"place_id": "p1"}],
        requests=[_make_req("ok", 200)],
        stop_reason="limit",
        exit_ip_start="1.1.1.1", exit_ip_end="1.1.1.1",
    )
    r_partial = QueryResult(
        query="tea", country="IN", proxy_session_id="s2",
        places=[{"place_id": "p2"}],
        requests=[_make_req("ok", 200, req_idx=1), _make_req("403", 403, req_idx=2)],
        stop_reason="block",
        partial=True,
        block_at_request=2,
        block_at_page=1,
        exit_ip_start="2.2.2.1", exit_ip_end="2.2.2.2",
    )
    rep = BenchmarkReport(
        run_id="run_check",
        proxy_type="residential",
        geo_targets=["US", "IN"],
        concurrency=2,
        results=[r_success, r_partial],
    )
    d = rep.to_dict()

    # Legacy keys
    legacy_keys = [
        "started_at_iso", "run_id", "proxy_type", "session_strategy", "geo_targets",
        "concurrency", "elapsed_s", "total_queries", "successful_queries", "failed_queries",
        "total_places", "total_requests", "ok_requests", "consent_requests", "block_403",
        "block_429", "success_rate_pct", "consent_rate_pct", "block_rate_pct", "traffic_mb",
        "traffic_mb_wire", "traffic_tx_mb_est", "traffic_probe_est_mb", "traffic_mb_decompressed",
        "traffic_gb", "avg_latency_ms", "p95_latency_ms", "avg_query_ms", "p95_query_ms",
        "qps", "places_per_minute", "validation", "sticky_continuity", "by_country", "queries",
    ]
    for k in legacy_keys:
        assert k in d

    # New keys
    new_keys = [
        "query_success_rate_pct", "partial_queries", "blocked_queries", "empty_queries",
        "error_queries", "proxy_error_requests", "google_error_requests", "sorry_requests",
        "soft_block_requests", "session_count", "ip_change_count",
    ]
    for k in new_keys:
        assert k in d

    # Per-query fields
    q_fields = [
        "query", "country", "session", "status", "places", "requests", "stop_reason",
        "block_at_request", "block_at_page", "partial", "proxy_errors"
    ]
    for q in d["queries"]:
        for f in q_fields:
            assert f in q

    assert d["successful_queries"] == 1
    assert d["partial_queries"] == 1
    assert d["failed_queries"] == 1
    assert d["query_success_rate_pct"] == 50.0


# ==============================================================================
# 7. scraper.py: IP Family Derivation Verification
# ==============================================================================

def test_ip_family_derivation():
    async def run():
        # Pure derivation
        assert _ip_family("1.2.3.4", fallback="v6") == "v4"
        assert _ip_family("2001:db8::1", fallback="v4") == "v6"
        assert _ip_family("", fallback="v6") == "v6"
        assert _ip_family(None, fallback="v4") == "v4"
        assert _ip_family("   ", fallback="v6") == "v6"

        # api64 returning IPv4 -> family must be v4, not v6
        sess_v4 = _MockSession([_MockResponse(200, b'{"ip": "198.51.100.1"}')])
        obs1 = await _probe_one(
            sess_v4,  # type: ignore
            name="ipify_v6",
            family="v6",
            url="https://api64.ipify.org/?format=json",
            request_index=0,
        )
        assert obs1.family == "v4"
        assert obs1.ip == "198.51.100.1"

        # real IPv6 -> family v6
        sess_v6 = _MockSession([_MockResponse(200, b'{"ip": "2001:db8::cafe"}')])
        obs2 = await _probe_one(
            sess_v6,  # type: ignore
            name="ipify_v6",
            family="v6",
            url="https://api64.ipify.org/?format=json",
            request_index=0,
        )
        assert obs2.family == "v6"
        assert obs2.ip == "2001:db8::cafe"

        # error / network failure -> fallback sane
        sess_err = _MockSession([ConnectionResetError("Socket dropped")])
        obs3 = await _probe_one(
            sess_err,  # type: ignore
            name="ipify_v6",
            family="v6",
            url="https://api64.ipify.org/?format=json",
            request_index=0,
        )
        assert obs3.family == "v6"
        assert obs3.ip == ""
        assert "Socket dropped" in obs3.error

    asyncio.run(run())
