"""
Central config — reads .env and exposes typed constants.

AnyIP username format (from docs):
    user_[ID],type_[residential|mobile],country_[ISO],session_[name],sesstime_[min]

Example:
    user_4c059fdb24,type_residential,country_IN,session_abc123,sesstime_20
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
_VALID_PROXY_TYPES = {"residential", "mobile", "mixed", "any", ""}


@dataclass
class ProxyConfig:
    # Proxy gateway
    host: str = field(default_factory=lambda: os.getenv("ANYIP_HOST", "portal.anyip.io"))
    port: int = field(default_factory=lambda: int(os.getenv("ANYIP_PORT", "1080")))

    # Proxy user credentials (short username — "user_" prefix is added automatically)
    proxy_user: str = field(default_factory=lambda: os.getenv("ANYIP_PROXY_USER", ""))
    proxy_pass: str = field(default_factory=lambda: os.getenv("ANYIP_PROXY_PASS", ""))

    # REST API
    api_key: str = field(default_factory=lambda: os.getenv("ANYIP_API_KEY", ""))
    team_id: str = field(default_factory=lambda: os.getenv("ANYIP_TEAM_ID", ""))

    # Rotation API
    proxy_id: str = field(default_factory=lambda: os.getenv("ANYIP_PROXY_ID", ""))
    rotate_hash: str = field(default_factory=lambda: os.getenv("ANYIP_ROTATE_HASH", ""))

    # Session behaviour
    proxy_type: str = field(default_factory=lambda: os.getenv("PROXY_TYPE", "residential"))
    session_duration_min: int = field(
        default_factory=lambda: int(os.getenv("SESSION_DURATION_MIN", "20"))
    )
    # per-query | per-country | shared | resume-last  (can also be chosen interactively)
    session_strategy: str = field(
        default_factory=lambda: os.getenv("SESSION_STRATEGY", "per-query")
    )
    geo_targets: list[str] = field(
        default_factory=lambda: [
            g.strip() for g in os.getenv("GEO_TARGETS", "IN,US").split(",") if g.strip()
        ]
    )

    def sticky_url(
        self,
        session_id: str,
        country: str = "US",
        proxy_type: str | None = None,        # None → use self.proxy_type
        session_duration_min: int | None = None,  # None → use self.session_duration_min
        sticky: bool = True,
        strict_session: bool = False,
        region: str | None = None,
        city: str | None = None,
        asn: int | str | None = None,
        sessasn_strict: bool = False,
        sessipcollision_strict: bool = False,
    ) -> str:
        """
        Build a sticky-session proxy URL using the AnyIP "smart username" format.

        Docs: https://anyip.io/docs/guides/configuration.md
        Format: http://user_[ID],type_[type],country_[ISO],session_[name],sesstime_[min]:PASS@HOST:PORT
        """
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            raise ValueError(
                f"session_id must be 1-32 alphanumeric characters ([A-Za-z0-9]), got {session_id!r}"
            )

        raw_type = proxy_type if proxy_type is not None else self.proxy_type
        if raw_type is None:
            raw_type = "mixed"
        ptype = str(raw_type).strip().lower()

        if ptype == "none" or ptype not in _VALID_PROXY_TYPES:
            raise ValueError(
                f"Invalid proxy_type: {raw_type!r}. Must be one of residential/mobile/mixed/any/'' ('none' is invalid)."
            )

        if ptype in ("any", ""):
            ptype = "mixed"

        parts = [f"user_{self.proxy_user}"]
        if ptype != "mixed":
            parts.append(f"type_{ptype}")
        parts.append(f"country_{country.upper()}")

        if region:
            parts.append(f"region_{str(region).strip().lower()}")
        if city:
            parts.append(f"city_{str(city).strip().lower()}")
        if asn is not None and str(asn).strip():
            digits = re.sub(r"[^\d]", "", str(asn))
            if not digits:
                raise ValueError(f"asn must contain digits, got {asn!r}")
            parts.append(f"asn_{digits}")

        if sticky:
            dur = (
                session_duration_min
                if session_duration_min is not None
                else self.session_duration_min
            )
            dur = max(1, min(10080, int(dur)))
            parts.append(f"session_{session_id}")
            parts.append(f"sesstime_{dur}")
            if strict_session:
                parts.append("sessreplace_false")
            if sessasn_strict:
                parts.append("sessasn_strict")
            if sessipcollision_strict:
                parts.append("sessipcollision_strict")

        username = ",".join(parts)
        return f"http://{username}:{self.proxy_pass}@{self.host}:{self.port}"

    def has_rotate(self) -> bool:
        return bool(self.proxy_id and self.rotate_hash)

    def rotate_url(self, session_id: str) -> str | None:
        if not self.has_rotate():
            return None
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            raise ValueError(
                f"session_id must be 1-32 alphanumeric characters ([A-Za-z0-9]), got {session_id!r}"
            )
        return f"https://dashboard.anyip.io/api/proxy_accounts/{self.proxy_id}/invalidate/{self.rotate_hash}/session/{session_id}"

    def is_configured(self) -> bool:
        return bool(self.proxy_user and self.proxy_pass and self.host)

    def has_api(self) -> bool:
        return bool(self.api_key and self.team_id)


PROXY = ProxyConfig()
