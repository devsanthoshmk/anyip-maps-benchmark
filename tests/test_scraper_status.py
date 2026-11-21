"""
Tests for QueryResult status, stop reasons, partial blocks, and dataclasses in scraper.py.
Pure fixtures; no network calls.
"""

import json
import pytest
from scraper import QueryResult, RequestRecord, IpObservation


def _make_req(outcome: str, status: int = 200, kind: str = "google", request_index: int = 1) -> RequestRecord:
    return RequestRecord(
        url="https://www.google.com/test",
        status=status,
        outcome=outcome,
        latency_ms=120.0,
        bytes_rx=1500,
        bytes_wire=800,
        kind=kind,
        request_index=request_index,
    )


def test_ip_observation_dataclass():
    obs = IpObservation(
        ts=1700000000.0,
        request_index=0,
        witness="ipify_v4",
        family="v4",
        ip="198.51.100.1",
        latency_ms=45.2,
        error="",
    )
    assert obs.witness == "ipify_v4"
    assert obs.family == "v4"
    assert obs.ip == "198.51.100.1"
    assert obs.latency_ms == 45.2
    assert obs.error == ""


def test_request_record_spec_fields():
    rec = RequestRecord(
        url="https://www.google.com/maps/search/coffee",
        status=502,
        outcome="proxy_error",
        latency_ms=250.0,
        bytes_rx=0,
        bytes_wire=0,
        kind="spa",
        request_index=1,
        page_start=0,
        attempt=1,
        anyip_error="Session expired",
        content_type="text/plain",
        server="nginx",
        retry_after="5",
        response_hash="abcdef1234567890",
        error="Bad Gateway",
        is_final=True,
    )
    assert rec.kind == "spa"
    assert rec.attempt == 1
    assert rec.anyip_error == "Session expired"
    assert rec.server == "nginx"
    assert rec.is_final is True


def test_block_counters_and_properties():
    res = QueryResult(
        query="coffee in Seattle",
        country="US",
        proxy_session_id="s123",
        requests=[
            _make_req("ok", 200),
            _make_req("403", 403),
            _make_req("429", 429),
            _make_req("consent", 200),
            _make_req("sorry", 200),
            _make_req("soft_block", 200),
            _make_req("proxy_error", 502),
            _make_req("google_5xx", 500),
        ],
    )
    assert res.ok_count == 1
    assert res.block_403 == 1
    assert res.block_429 == 1
    assert res.consent_count == 1
    assert res.sorry_count == 1
    assert res.soft_block_count == 1
    # 403 + 429 + consent + sorry + soft_block = 5
    assert res.block_count == 5
    assert res.proxy_error_count == 1
    assert res.google_error_count == 1


def test_query_status_success():
    # Places >= expected_min_results (default 1), no block, stop_reason in {limit, no_new}
    res = QueryResult(
        query="hotels in Paris",
        country="FR",
        proxy_session_id="s1",
        places=[{"place_id": "p1"}, {"place_id": "p2"}],
        requests=[_make_req("ok", 200), _make_req("ok", 200)],
        stop_reason="limit",
        expected_min_results=1,
    )
    assert res.query_status == "success"

    res_no_new = QueryResult(
        query="hotels in Paris",
        country="FR",
        proxy_session_id="s1",
        places=[{"place_id": "p1"}],
        requests=[_make_req("ok", 200)],
        stop_reason="no_new",
        expected_min_results=1,
    )
    assert res_no_new.query_status == "success"


def test_query_status_target_depth_reached_fewer_than_expected():
    # Target depth reached (stop_reason="limit"), success even if fewer than expected_min_results
    res = QueryResult(
        query="rare query",
        country="US",
        proxy_session_id="s1",
        places=[{"place_id": "p1"}],
        requests=[_make_req("ok", 200)],
        stop_reason="limit",
        expected_min_results=10,
    )
    assert res.query_status == "success"


def test_query_status_blocked():
    # places == 0 and first Google request / stop reason is a block
    res_403 = QueryResult(
        query="coffee",
        country="US",
        proxy_session_id="s1",
        places=[],
        requests=[_make_req("403", 403)],
        stop_reason="block",
        block_at_request=1,
        block_at_page=1,
    )
    assert res_403.query_status == "blocked"

    res_consent = QueryResult(
        query="coffee",
        country="US",
        proxy_session_id="s1",
        places=[],
        requests=[_make_req("consent", 200)],
        stop_reason="block",
        block_at_request=1,
        block_at_page=1,
    )
    assert res_consent.query_status == "blocked"


def test_query_status_partial_on_pagination_block():
    # Non-negotiable rule 18:
    # "success is query-level and requires reaching the intended depth with no block.
    # A pagination block after 20 places is partial, not success."
    res = QueryResult(
        query="coffee in Seattle",
        country="US",
        proxy_session_id="s1",
        places=[{"place_id": f"p{i}"} for i in range(20)],
        requests=[_make_req("ok", 200, request_index=1), _make_req("429", 429, request_index=2)],
        stop_reason="block",
        block_at_request=2,
        block_at_page=1,
        partial=True,
    )
    assert res.query_status == "partial"
    assert res.partial is True
    assert res.block_at_request == 2
    assert res.block_at_page == 1


def test_query_status_partial_on_errors():
    # places > 0 and stop_reason in {parse_error, network, session_lost, other_error, proxy_error, quota, stopped}
    for err_reason in ["parse_error", "network", "session_lost", "other_error", "proxy_error", "quota", "stopped"]:
        res = QueryResult(
            query="cafes",
            country="US",
            proxy_session_id="s1",
            places=[{"place_id": "p1"}],
            requests=[_make_req("ok", 200)],
            stop_reason=err_reason,
            partial=True,
        )
        assert res.query_status == "partial"


def test_query_status_empty():
    # places == 0, no block, stop_reason in {no_new, limit}
    res_no_new = QueryResult(
        query="obscure place in antarctica",
        country="US",
        proxy_session_id="s1",
        places=[],
        requests=[_make_req("ok", 200)],
        stop_reason="no_new",
    )
    assert res_no_new.query_status == "empty"

    res_limit = QueryResult(
        query="obscure place in antarctica",
        country="US",
        proxy_session_id="s1",
        places=[],
        requests=[_make_req("ok", 200)],
        stop_reason="limit",
    )
    assert res_limit.query_status == "empty"


def test_query_status_error():
    # places == 0, no block, stop_reason in {parse_error, network, session_lost, other_error, proxy_error, quota, stopped}
    for err_reason in ["parse_error", "network", "session_lost", "other_error", "proxy_error", "quota", "stopped"]:
        res = QueryResult(
            query="test",
            country="US",
            proxy_session_id="s1",
            places=[],
            requests=[_make_req("other_error" if err_reason == "other_error" else "ok", 500)],
            stop_reason=err_reason,
        )
        assert res.query_status == "error"


def test_query_status_quota_and_stopped_reproducer():
    r1 = QueryResult(
        query="coffee",
        country="US",
        proxy_session_id="s1",
        places=[{"place_id": f"p{i}"} for i in range(5)],
        stop_reason="quota",
    )
    assert r1.query_status == "partial"

    r2 = QueryResult(
        query="coffee",
        country="US",
        proxy_session_id="s1",
        places=[],
        stop_reason="stopped",
    )
    assert r2.query_status == "error"


def test_benchmark_report_keys_and_metrics():
    from report import BenchmarkReport

    r_success = QueryResult(
        query="coffee",
        country="US",
        proxy_session_id="sess_1",
        places=[{"place_id": "p1"}],
        requests=[_make_req("ok", 200)],
        stop_reason="limit",
        exit_ip_start="1.1.1.1",
        exit_ip_end="1.1.1.1",
    )
    r_partial = QueryResult(
        query="tea",
        country="IN",
        proxy_session_id="sess_2",
        places=[{"place_id": "p2"}],
        requests=[_make_req("ok", 200), _make_req("403", 403)],
        stop_reason="block",
        partial=True,
        block_at_request=2,
        block_at_page=1,
        exit_ip_start="2.2.2.1",
        exit_ip_end="2.2.2.2",  # rotated
    )
    r_blocked = QueryResult(
        query="juice",
        country="US",
        proxy_session_id="sess_3",
        places=[],
        requests=[_make_req("429", 429)],
        stop_reason="block",
        block_at_request=1,
        block_at_page=1,
    )

    report = BenchmarkReport(
        run_id="test_run",
        proxy_type="residential",
        geo_targets=["US", "IN"],
        concurrency=5,
        results=[r_success, r_partial, r_blocked],
    )

    assert report.total_queries == 3
    assert report.successful_queries == 1
    assert report.partial_queries == 1
    assert report.blocked_queries == 1
    assert report.failed_queries == 2
    assert report.session_count == 3
    assert report.ip_change_count == 1
    assert report.query_success_rate_pct == round(1 / 3 * 100, 2)

    d = report.to_dict()
    # Check new spec keys in to_dict
    assert "query_success_rate_pct" in d
    assert "partial_queries" in d
    assert "blocked_queries" in d
    assert "empty_queries" in d
    assert "error_queries" in d
    assert "proxy_error_requests" in d
    assert "google_error_requests" in d
    assert "sorry_requests" in d
    assert "soft_block_requests" in d
    assert "session_count" in d
    assert "ip_change_count" in d

    # Check legacy UI keys preserved
    assert "started_at_iso" in d
    assert "run_id" in d
    assert "proxy_type" in d
    assert "session_strategy" in d
    assert "geo_targets" in d
    assert "concurrency" in d
    assert "elapsed_s" in d
    assert "total_queries" in d
    assert "successful_queries" in d
    assert "failed_queries" in d
    assert "total_places" in d
    assert "total_requests" in d
    assert "ok_requests" in d
    assert "consent_requests" in d
    assert "block_403" in d
    assert "block_429" in d
    assert "success_rate_pct" in d
    assert "consent_rate_pct" in d
    assert "block_rate_pct" in d
    assert "traffic_mb" in d
    assert "avg_latency_ms" in d
    assert "p95_latency_ms" in d
    assert "queries" in d

    # Check queries item fields
    q0 = d["queries"][0]
    assert q0["status"] == "success"
    assert "stop_reason" in q0
    assert "block_at_request" in q0
    assert "block_at_page" in q0
    assert "partial" in q0
    assert "proxy_errors" in q0


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


def test_timed_get_retries_on_proxy_error_and_records_attempts():
    import asyncio
    from scraper import _timed_get

    async def run():
        mock_session = _MockSession([
            _MockResponse(502, b"Bad Gateway"),
            _MockResponse(200, b"maps content"),
        ])
        res = QueryResult(query="test", country="US", proxy_session_id="s1")
        rec, body = await _timed_get(
            mock_session,  # type: ignore
            "https://www.google.com/test",
            retries=2,
            result=res,
        )
        assert len(res.attempts) == 2
        assert res.attempts[0].status == 502
        assert res.attempts[0].outcome == "proxy_error"
        assert res.attempts[0].attempt == 0
        assert res.attempts[1].status == 200
        assert res.attempts[1].outcome == "ok"
        assert res.attempts[1].attempt == 1
        assert rec.status == 200
        assert rec.outcome == "ok"

    asyncio.run(run())


def test_timed_get_never_retries_blocks():
    import asyncio
    from scraper import _timed_get

    async def run():
        mock_session = _MockSession([
            _MockResponse(403, b"Forbidden"),
            _MockResponse(200, b"should not be reached"),
        ])
        res = QueryResult(query="test", country="US", proxy_session_id="s1")
        rec, body = await _timed_get(
            mock_session,  # type: ignore
            "https://www.google.com/test",
            retries=2,
            result=res,
        )
        # Never retries 403
        assert len(res.attempts) == 1
        assert res.attempts[0].status == 403
        assert res.attempts[0].outcome == "403"
        assert rec.status == 403
        assert rec.outcome == "403"
        assert mock_session.calls == 1

    asyncio.run(run())


def test_timed_get_exhausted_exceptions_network_error():
    import asyncio
    from scraper import _timed_get

    async def run():
        mock_session = _MockSession([
            ConnectionResetError("Connection lost"),
            ConnectionResetError("Connection lost again"),
        ])
        res = QueryResult(query="test", country="US", proxy_session_id="s1")
        rec, body = await _timed_get(
            mock_session,  # type: ignore
            "https://www.google.com/test",
            retries=1,
            result=res,
        )
        assert len(res.attempts) == 2
        assert rec.status == 0
        assert rec.outcome == "network_error"
        assert "Connection lost" in rec.error

    asyncio.run(run())


def test_exit_ips_and_legacy_wrapper():
    import asyncio
    from scraper import _exit_ips, _exit_ip

    class _MockProbeSession:
        async def get(self, url, **kwargs):
            if "api64.ipify.org" in url:
                return _MockResponse(200, b'{"ip": "2001:db8::1"}')
            elif "ipify.org/?format=json" in url:
                return _MockResponse(200, b'{"ip": "1.2.3.4"}')
            elif "checkip.amazonaws.com" in url:
                return _MockResponse(200, b"1.2.3.4\n")
            elif "httpbin.org/ip" in url:
                return _MockResponse(200, b'{"origin": "1.2.3.4, 5.6.7.8"}')
            elif "ifconfig.co/ip" in url:
                return _MockResponse(200, b"1.2.3.4")
            return _MockResponse(404, b"Not Found")

    async def run():
        mock_sess = _MockProbeSession()
        # Test dual mode
        obs_dual = await _exit_ips(mock_sess, request_index=0, mode="dual")  # type: ignore
        assert len(obs_dual) == 2
        assert obs_dual[0].witness == "ipify_v4"
        assert obs_dual[0].ip == "1.2.3.4"
        assert obs_dual[0].family == "v4"
        assert obs_dual[1].witness == "ipify_v6"
        assert obs_dual[1].ip == "2001:db8::1"
        assert obs_dual[1].family == "v6"

        # Test multi mode
        obs_multi = await _exit_ips(mock_sess, request_index=1, mode="multi")  # type: ignore
        assert len(obs_multi) == 5
        witness_names = [o.witness for o in obs_multi]
        assert witness_names == ["ipify_v4", "ipify_v6", "amazon", "httpbin", "ifconfig"]
        # httpbin parsed origin
        httpbin_obs = next(o for o in obs_multi if o.witness == "httpbin")
        assert httpbin_obs.ip == "1.2.3.4"

        # Test legacy _exit_ip wrapper
        ip, billed = await _exit_ip(mock_sess)  # type: ignore
        assert ip == "1.2.3.4"
        assert billed > 0

    asyncio.run(run())


def test_exit_ips_api64_family_derivation():
    import asyncio
    from scraper import _exit_ips

    class _MockApi64Ipv4Session:
        async def get(self, url, **kwargs):
            if "api64.ipify.org" in url:
                return _MockResponse(200, b'{"ip": "1.2.3.4"}')
            elif "ipify.org/?format=json" in url:
                return _MockResponse(200, b'{"ip": "1.2.3.4"}')
            return _MockResponse(404, b"Not Found")

    class _MockApi64Ipv6Session:
        async def get(self, url, **kwargs):
            if "api64.ipify.org" in url:
                return _MockResponse(200, b'{"ip": "2607:f8b0:4005:805::200e"}')
            elif "ipify.org/?format=json" in url:
                return _MockResponse(200, b'{"ip": "1.2.3.4"}')
            return _MockResponse(404, b"Not Found")

    async def run():
        # api64 returning an IPv4 address -> derived family must be "v4", not "v6"
        obs_v4 = await _exit_ips(_MockApi64Ipv4Session(), request_index=0, mode="dual")  # type: ignore
        api64_v4_obs = next(o for o in obs_v4 if o.witness == "ipify_v6")
        assert api64_v4_obs.ip == "1.2.3.4"
        assert api64_v4_obs.family == "v4"

        # api64 returning a real IPv6 address -> derived family must be "v6"
        obs_v6 = await _exit_ips(_MockApi64Ipv6Session(), request_index=0, mode="dual")  # type: ignore
        api64_v6_obs = next(o for o in obs_v6 if o.witness == "ipify_v6")
        assert api64_v6_obs.ip == "2607:f8b0:4005:805::200e"
        assert api64_v6_obs.family == "v6"

    asyncio.run(run())


def test_scrape_into_spa_block():
    import asyncio
    from scraper import _scrape_into

    async def run():
        mock_session = _MockSession([
            _MockResponse(403, b"Forbidden"),
        ])
        res = QueryResult(query="coffee", country="US", proxy_session_id="s1")
        res = await _scrape_into(
            mock_session,  # type: ignore
            res,
            lang="en",
            limit=10,
        )
        assert res.stop_reason == "block"
        assert res.block_at_request == 1
        assert res.block_at_page == 1
        assert res.partial is False
        assert res.query_status == "blocked"

    asyncio.run(run())


def test_scrape_into_spa_parse_fail():
    import asyncio
    from scraper import _scrape_into

    async def run():
        # 200 OK but no tbm=map link in SPA
        mock_session = _MockSession([
            _MockResponse(200, b"<html>No map link here</html>"),
        ])
        res = QueryResult(query="coffee", country="US", proxy_session_id="s1")
        res = await _scrape_into(
            mock_session,  # type: ignore
            res,
            lang="en",
            limit=10,
        )
        assert res.stop_reason == "parse_error"
        assert "extract tbm=map" in (res.error or "")
        assert res.partial is False
        assert res.query_status == "error"

    asyncio.run(run())


def test_scrape_into_page_block_after_places():
    import asyncio
    from scraper import _scrape_into

    spa_html = b'<html><link href="/search?tbm=map&amp;pb=1"></html>'
    # Page 1 JSON with 1 place
    # Item structure: data[64] -> list of items. item[1] -> d.
    # d[78] = place_id, d[11] = title
    item = [None, {78: "ChIJ1234567890", 11: "Best Coffee", 13: ["Cafe"], 39: "123 Main St", 9: [0, 0, 47.6, -122.3]}]
    data = [None] * 65
    data[64] = [item]
    page1_body = b")]}'\n" + json.dumps(data).encode("utf-8")

    async def run():
        mock_session = _MockSession([
            _MockResponse(200, spa_html),       # SPA ok
            _MockResponse(200, page1_body),     # Page 1 ok
            _MockResponse(429, b"Too Many"),    # Page 2 blocked
        ])
        res = QueryResult(query="coffee", country="US", proxy_session_id="s1")
        res = await _scrape_into(
            mock_session,  # type: ignore
            res,
            lang="en",
            limit=40,
        )
        assert len(res.places) == 1
        assert res.stop_reason == "block"
        assert res.block_at_request == 3  # SPA is 1, page1 is 2, page2 is 3
        assert res.block_at_page == 2
        assert res.partial is True
        assert res.query_status == "partial"

    asyncio.run(run())




