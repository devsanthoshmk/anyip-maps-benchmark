"""
Tests for ProxyConfig (config.py). Pure fixtures; no network calls.
"""

import pytest
from config import ProxyConfig


@pytest.fixture
def base_config() -> ProxyConfig:
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


def test_session_id_validation(base_config: ProxyConfig):
    # Valid alphanumeric IDs between 1 and 32 characters
    valid_ids = ["a", "1", "session123", "A" * 32, "ValidSessionName99"]
    for sid in valid_ids:
        url = base_config.sticky_url(session_id=sid)
        assert f"session_{sid}" in url

    # Invalid session IDs should raise ValueError
    invalid_ids = [
        "",                 # 0 chars
        "A" * 33,           # > 32 chars
        "sess-1",           # contains hyphen
        "sess_1",           # contains underscore
        "sess 1",           # contains space
        "sess@123",         # contains special char
        None,               # None
        12345,              # not a str
    ]
    for invalid in invalid_ids:
        with pytest.raises(ValueError):
            base_config.sticky_url(session_id=invalid)  # type: ignore


def test_proxy_type_variants(base_config: ProxyConfig):
    # Explicit residential
    url_res = base_config.sticky_url("s1", proxy_type="residential")
    assert "type_residential" in url_res

    # Explicit mobile
    url_mob = base_config.sticky_url("s1", proxy_type="mobile")
    assert "type_mobile" in url_mob

    # Explicit mixed -> omit type_
    url_mix = base_config.sticky_url("s1", proxy_type="mixed")
    assert "type_" not in url_mix

    # "any" and "" normalize to mixed -> omit type_
    url_any = base_config.sticky_url("s1", proxy_type="any")
    assert "type_" not in url_any

    url_empty = base_config.sticky_url("s1", proxy_type="")
    assert "type_" not in url_empty

    # None -> uses self.proxy_type ("residential")
    url_none_arg = base_config.sticky_url("s1", proxy_type=None)
    assert "type_residential" in url_none_arg

    # "none" is invalid -> must raise ValueError
    with pytest.raises(ValueError, match="invalid"):
        base_config.sticky_url("s1", proxy_type="none")

    # Unknown type -> must raise ValueError
    with pytest.raises(ValueError, match="invalid"):
        base_config.sticky_url("s1", proxy_type="datacenter")


def test_sticky_flags_ordering(base_config: ProxyConfig):
    # Test ordering of parts:
    # user_<id>, type_<type>, country_<ISO>, region_<lower>, city_<lower>, asn_<digits>,
    # session_<id>, sesstime_<min>, sessreplace_false, sessasn_strict, sessipcollision_strict
    url = base_config.sticky_url(
        session_id="sessABC",
        country="in",
        proxy_type="mobile",
        session_duration_min=15,
        sticky=True,
        strict_session=True,
        region="Maharashtra",
        city="Mumbai",
        asn="AS55836",
        sessasn_strict=True,
        sessipcollision_strict=True,
    )

    expected_username = (
        "user_testuser,"
        "type_mobile,"
        "country_IN,"
        "region_maharashtra,"
        "city_mumbai,"
        "asn_55836,"
        "session_sessABC,"
        "sesstime_15,"
        "sessreplace_false,"
        "sessasn_strict,"
        "sessipcollision_strict"
    )
    expected_url = f"http://{expected_username}:secretpass@portal.anyip.io:1080"
    assert url == expected_url


def test_non_sticky_url(base_config: ProxyConfig):
    url = base_config.sticky_url(
        session_id="sessABC",
        country="US",
        proxy_type="residential",
        sticky=False,
    )
    expected_username = "user_testuser,type_residential,country_US"
    assert url == f"http://{expected_username}:secretpass@portal.anyip.io:1080"
    assert "session_" not in url
    assert "sesstime_" not in url


def test_asn_handling(base_config: ProxyConfig):
    # Integer ASN
    url_int = base_config.sticky_url("s1", asn=12345)
    assert "asn_12345" in url_int

    # AS prefix
    url_prefix = base_config.sticky_url("s1", asn="AS7018")
    assert "asn_7018" in url_prefix

    # Invalid ASN with no digits raises ValueError
    with pytest.raises(ValueError):
        base_config.sticky_url("s1", asn="invalid")


def test_rotate_url_and_has_rotate(base_config: ProxyConfig):
    assert base_config.has_rotate() is True
    r_url = base_config.rotate_url("testSession1")
    assert r_url == "https://dashboard.anyip.io/api/proxy_accounts/acc123/invalidate/hash456/session/testSession1"

    # Invalid session_id in rotate_url
    with pytest.raises(ValueError):
        base_config.rotate_url("invalid-session!")

    # Missing rotate credentials
    unconfigured = ProxyConfig(proxy_id="", rotate_hash="")
    assert unconfigured.has_rotate() is False
    assert unconfigured.rotate_url("testSession1") is None
