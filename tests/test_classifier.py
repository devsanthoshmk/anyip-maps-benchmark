"""
Tests for response classification (_classify_response in scraper.py).
Pure fixtures; no network calls.
"""

import pytest
from scraper import _classify_response


def test_status_zero_network_error():
    assert _classify_response(0) == "network_error"
    assert _classify_response(0, b"Some error", {"some": "header"}) == "network_error"


def test_status_403_and_429():
    assert _classify_response(403, b"Forbidden") == "403"
    assert _classify_response(429, b"Too Many Requests") == "429"


def test_proxy_errors():
    # Proxy auth required
    assert _classify_response(407) == "proxy_error"

    # AnyIP client error codes
    assert _classify_response(400) == "proxy_error"
    assert _classify_response(409) == "proxy_error"
    assert _classify_response(413) == "proxy_error"

    # Gateway errors (502/503/504) even without any header
    assert _classify_response(502) == "proxy_error"
    assert _classify_response(503) == "proxy_error"
    assert _classify_response(504) == "proxy_error"

    # Non-200 with X-Anyip-Error header (case-insensitive)
    assert _classify_response(404, b"Not found", {"X-Anyip-Error": "Rate limit exceeded"}) == "proxy_error"
    assert _classify_response(418, b"Teapot", {"x-anyip-error": "Session closed"}) == "proxy_error"


def test_headers_on_200_do_not_override_200():
    # "use X-Anyip-Error/x-anyip-error to set anyip_error in the record, not to change a 200"
    headers = {"X-Anyip-Error": "Some warning note"}
    assert _classify_response(200, b"normal maps payload", headers) == "ok"


def test_google_5xx_and_other_errors():
    # Google internal errors
    assert _classify_response(500) == "google_5xx"
    assert _classify_response(501) == "google_5xx"
    assert _classify_response(505) == "google_5xx"
    assert _classify_response(506) == "google_5xx"

    # Redirects and other 4xx
    assert _classify_response(301) == "other_error"
    assert _classify_response(302) == "other_error"
    assert _classify_response(404) == "other_error"
    assert _classify_response(422) == "other_error"


def test_consent_signals():
    signals = [
        b"Welcome to consent.google.com to continue",
        b"Before You Continue to Google Search",
        b"Please agree to Google terms of service",
        b"To use Google Maps, please review the notice",
    ]
    for body in signals:
        assert _classify_response(200, body) == "consent"


def test_sorry_signals():
    signals = [
        b"Redirecting to /sorry/index?continue=...",
        b"Our systems have detected unusual traffic from your computer network",
        b"Detected Unusual Traffic",
        b"Our systems have detected unusual activity",
        b"Please solve the recaptcha below",
        b"Blocked due to automated queries",
    ]
    for body in signals:
        assert _classify_response(200, body) == "sorry"


def test_soft_block_signals():
    # Lacks tbm=map link -> soft_block
    assert _classify_response(200, b"Why did this happen? See details.") == "soft_block"
    assert _classify_response(200, b"Troubleshooting connection issues") == "soft_block"

    # Contains tbm=map link -> not a soft block, returns ok
    assert _classify_response(200, b"Troubleshooting with link <a href='/search?tbm=map'>Maps</a>") == "ok"
    assert _classify_response(200, b"Why did this happen <link href='/search?tbm=map&pb=1'>") == "ok"


def test_precedence_order_on_200():
    # Consent > Sorry > Soft block
    consent_and_sorry = b"consent.google.com with unusual traffic"
    assert _classify_response(200, consent_and_sorry) == "consent"

    sorry_and_soft = b"unusual traffic why did this happen"
    assert _classify_response(200, sorry_and_soft) == "sorry"

    # Completely normal body
    assert _classify_response(200, b"window.APP_INITIALIZATION_STATE=[1, 2, 3]") == "ok"
