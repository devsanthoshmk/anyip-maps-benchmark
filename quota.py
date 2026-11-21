"""
AnyIP dashboard quota check.

python quota.py          — print current trial usage
also called by bench.py before a proxy run.

Docs: https://anyip.io/docs/api/introduction.md
"""

from __future__ import annotations

import asyncio

import aiohttp

from config import PROXY

_BASE = "https://dashboard.anyip.io"
GB = 1024 ** 3
MB = 1024 ** 2


async def get_usage() -> dict:
    """Team quota consumption for the current billing period."""
    headers = {"api-key": PROXY.api_key, "Content-Type": "application/json"}
    url = f"{_BASE}/api/v1/teams/{PROXY.team_id}/usage"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"AnyIP API {resp.status}: {text}")
            return await resp.json()


def fmt_bytes(b: int) -> str:
    if b >= GB:
        return f"{b / GB:.3f} GB"
    if b >= MB:
        return f"{b / MB:.1f} MB"
    return f"{b:,} B"


def print_usage(usage: dict) -> None:
    quota = usage.get("quota_bytes", 0)
    used = usage.get("consumption_bytes", 0)
    remaining = usage.get("remaining_bytes", quota - used)

    pct = used / quota * 100 if quota else 0
    bar_len = 30
    filled = int(bar_len * used / quota) if quota else 0
    bar = "█" * filled + "░" * (bar_len - filled)

    print("\n── AnyIP Trial Usage ──────────────────────────────────")
    print(f"  Quota     : {fmt_bytes(quota)}")
    print(f"  Used      : {fmt_bytes(used)}  ({pct:.1f}%)")
    print(f"  Remaining : {fmt_bytes(remaining)}")
    print(f"  [{bar}] {pct:.0f}%")
    if remaining < 500 * MB:
        print("  WARNING: under 500 MB remaining — keep runs small")
    elif remaining < 1 * GB:
        print("  WARNING: under 1 GB remaining")
    print()


if __name__ == "__main__":
    usage = asyncio.run(get_usage())
    print_usage(usage)
