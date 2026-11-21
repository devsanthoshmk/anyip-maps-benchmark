"""IP enrichment module using ip-api.com batch API with atomic caching and rate limiting.

Spec requirements:
- class IpEnricher(cache_path="cache/ip_enrich.json", enabled=True)
- async def enrich(ips) -> dict[str, dict] with keys:
  asn, org, isp, city, country, network_type_guess
- Cache JSON, atomic write
- Batch via http://ip-api.com/batch?fields=status,country,city,isp,org,as,asname,query
  (max 100/batch, serialize batches, 1 request/sec delay)
- On failure return {} per IP; never raise
- Parse `as` "AS55836 Reliance Jio..." -> asn "AS55836"
- network_type_guess: "mobile" if org/isp/asname contains mobile/cellular/4g/5g/wireless
  or known carriers (jio, airtel, vodafone, vi, t-mobile, verizon wireless, at&t mobility);
  else "residential_or_other"
- async def enrich_observations(store, run_id) updates ip_observations rows with asn/org/city/country
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import aiohttp

logger = logging.getLogger(__name__)

IP_API_BATCH_URL = "http://ip-api.com/batch?fields=status,country,city,isp,org,as,asname,query"
BATCH_SIZE = 100
MIN_REQUEST_INTERVAL_S = 1.0

MOBILE_CARRIERS = (
    "jio",
    "airtel",
    "vodafone",
    "t-mobile",
    "verizon wireless",
    "at&t mobility",
)

MOBILE_KEYWORDS = (
    "mobile",
    "cellular",
    "4g",
    "5g",
    "wireless",
)

VI_WORD_REGEX = re.compile(r"\bvi\b", re.IGNORECASE)
ASN_REGEX = re.compile(r"\b(AS\d+)\b", re.IGNORECASE)


def parse_asn(as_text: Optional[str]) -> str:
    """Extract ASN identifier (e.g. 'AS55836') from ip-api `as` field."""
    if not as_text:
        return ""
    text = str(as_text).strip()
    match = ASN_REGEX.search(text)
    if match:
        return match.group(1).upper()
    tokens = text.split()
    if tokens and tokens[0].upper().startswith("AS"):
        return tokens[0].upper()
    return ""


def classify_network_type(org: str = "", isp: str = "", asname: str = "") -> str:
    """Classify IP as 'mobile' or 'residential_or_other'."""
    combined = f"{org} {isp} {asname}".lower()

    for kw in MOBILE_KEYWORDS:
        if kw in combined:
            return "mobile"

    for carrier in MOBILE_CARRIERS:
        if carrier in combined:
            return "mobile"

    if VI_WORD_REGEX.search(combined):
        return "mobile"

    return "residential_or_other"


def _safe_exc_str(e: BaseException) -> str:
    try:
        return str(e)
    except Exception:
        try:
            return repr(e)
        except Exception:
            return e.__class__.__name__


class IpEnricher:
    """Thread-safe, rate-limited, caching IP enricher."""

    def __init__(
        self,
        cache_path: Union[str, Path] = "cache/ip_enrich.json",
        enabled: bool = True,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.enabled = enabled
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_request_time: float = 0.0
        self._async_lock = asyncio.Lock()
        self._load_cache()

    def _load_cache(self) -> None:
        """Load cache from disk if available."""
        if not self.cache_path.exists():
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self._cache = data
        except Exception as e:
            logger.warning(f"Could not load IP enrich cache from {self.cache_path}: {_safe_exc_str(e)}")
            self._cache = {}

    def _save_cache(self) -> None:
        """Atomically persist cache to disk."""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.cache_path.with_suffix(
                f".tmp.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
            )
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.cache_path)
        except Exception as e:
            logger.warning(f"Failed to atomically write cache to {self.cache_path}: {_safe_exc_str(e)}")

    async def _post_batch(
        self,
        session: aiohttp.ClientSession,
        batch_ips: List[str],
    ) -> List[Dict[str, Any]]:
        """Serialize HTTP batch request with 1 req/sec pacing. Never raises."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if self._last_request_time > 0 and elapsed < MIN_REQUEST_INTERVAL_S:
            await asyncio.sleep(MIN_REQUEST_INTERVAL_S - elapsed)

        self._last_request_time = time.monotonic()

        try:
            async with session.post(
                IP_API_BATCH_URL,
                json=batch_ips,
                timeout=aiohttp.ClientTimeout(total=15.0),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        return data
                logger.warning(f"ip-api batch returned HTTP status {resp.status}")
                return []
        except Exception as e:
            logger.warning(f"ip-api batch request failed: {_safe_exc_str(e)}")
            return []

    async def enrich(self, ips: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Enrich IP addresses with geo/ASN/ISP data and network_type_guess.

        Never raises on network error. Returns {} per IP on failure.
        """
        ip_list = [str(ip).strip() for ip in ips if ip and str(ip).strip()]
        result: Dict[str, Dict[str, Any]] = {}

        if not ip_list:
            return result

        if not self.enabled:
            for ip in ip_list:
                result[ip] = dict(self._cache.get(ip, {}))
            return result

        async with self._async_lock:
            # Check cache first
            uncached: List[str] = []
            for ip in dict.fromkeys(ip_list):
                if ip in self._cache:
                    result[ip] = dict(self._cache[ip])
                else:
                    uncached.append(ip)

            if not uncached:
                for ip in ip_list:
                    result[ip] = dict(self._cache.get(ip, {}))
                return result

            cache_modified = False

            # Batch max 100 IPs
            try:
                async with aiohttp.ClientSession() as session:
                    for i in range(0, len(uncached), BATCH_SIZE):
                        batch = uncached[i : i + BATCH_SIZE]
                        resp_items = await self._post_batch(session, batch)

                        received_ips = set()
                        for item in resp_items:
                            if not isinstance(item, dict):
                                continue
                            query_ip = item.get("query", "")
                            if not query_ip:
                                continue
                            received_ips.add(query_ip)

                            if item.get("status") == "success":
                                org = str(item.get("org") or "")
                                isp = str(item.get("isp") or "")
                                asname = str(item.get("asname") or "")
                                as_raw = str(item.get("as") or "")
                                city = str(item.get("city") or "")
                                country = str(item.get("country") or "")

                                parsed_asn = parse_asn(as_raw)
                                net_type = classify_network_type(org, isp, asname)

                                enriched_data = {
                                    "asn": parsed_asn,
                                    "org": org,
                                    "isp": isp,
                                    "city": city,
                                    "country": country,
                                    "network_type_guess": net_type,
                                }
                                self._cache[query_ip] = enriched_data
                                result[query_ip] = enriched_data
                                cache_modified = True
                            else:
                                result[query_ip] = {}

                        # Any IP in batch not received in response gets {}
                        for ip in batch:
                            if ip not in received_ips and ip not in result:
                                result[ip] = {}
            except Exception as e:
                logger.warning(f"Unexpected error during IP enrichment: {_safe_exc_str(e)}")
                for ip in uncached:
                    if ip not in result:
                        result[ip] = {}

            if cache_modified:
                self._save_cache()

            # Ensure all requested IPs have entries in result
            for ip in ip_list:
                if ip not in result:
                    result[ip] = dict(self._cache.get(ip, {}))

            return result


# Global default enricher
_DEFAULT_ENRICHER: Optional[IpEnricher] = None


def get_default_enricher() -> IpEnricher:
    global _DEFAULT_ENRICHER
    if _DEFAULT_ENRICHER is None:
        _DEFAULT_ENRICHER = IpEnricher()
    return _DEFAULT_ENRICHER


async def enrich(ips: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Module-level convenience wrapper for default enricher."""
    return await get_default_enricher().enrich(ips)


async def enrich_observations(
    store: Any,
    run_id: Optional[str] = None,
    enricher: Optional[IpEnricher] = None,
) -> int:
    """Update ip_observations rows in store with asn, org, city, country.

    Returns the number of rows updated.
    """
    if enricher is None:
        enricher = get_default_enricher()

    if hasattr(store, "get_ip_observations"):
        rows = store.get_ip_observations(run_id)
    elif hasattr(store, "fetchall"):
        if run_id is not None:
            rows = store.fetchall(
                "SELECT * FROM ip_observations WHERE run_id = ?",
                (run_id,),
            )
        else:
            rows = store.fetchall("SELECT * FROM ip_observations")
    else:
        logger.warning("Store does not provide query interface for ip_observations")
        return 0

    if not rows:
        return 0

    ips = [r["ip"] for r in rows if r.get("ip")]
    if not ips:
        return 0

    enriched = await enricher.enrich(ips)
    updated_count = 0

    for row in rows:
        row_id = row.get("id")
        ip = row.get("ip")
        if not ip or ip not in enriched:
            continue

        info = enriched[ip]
        if not info:
            continue

        fields: Dict[str, Any] = {}
        if info.get("asn"):
            fields["asn"] = info["asn"]
        if info.get("org"):
            fields["org"] = info["org"]
        if info.get("city"):
            fields["city"] = info["city"]
        if info.get("country"):
            fields["country"] = info["country"]

        if fields:
            if hasattr(store, "update_ip_observation"):
                store.update_ip_observation(row_id, **fields)
                updated_count += 1
            elif hasattr(store, "execute"):
                set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
                params = list(fields.values()) + [row_id]
                store.execute(f"UPDATE ip_observations SET {set_clause} WHERE id = ?", params)
                updated_count += 1

    return updated_count
