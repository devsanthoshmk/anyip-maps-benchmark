"""Export reviewed aggregate results only. Run from the repository root."""

import argparse
import json
from pathlib import Path
import re


FIELDS = {
    "totals": "total_queries successful_queries total_places total_requests ok_requests consent_requests block_403 block_429 traffic_mb_wire",
    "country": "queries successful_queries places avg_latency_ms consent 403 429 soft_block proxy_error ip_matched_queries ip_changed_queries ip_unknown_queries traffic_mb",
    "variance": "n_pairs jaccard_20_mean jaccard_40_mean rbo_mean kendall_tau_mean",
    "sticky": "query_ip_matched query_ip_rotated query_ip_unknown",
}


def numbers(row, fields):
    return {key: row[key] for key in FIELDS[fields].split() if type(row.get(key)) in (int, float)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis", type=Path)
    parser.add_argument("title")
    parser.add_argument("--account-usage-delta-bytes", type=int)
    args = parser.parse_args()
    src = json.loads(args.analysis.read_text())
    run_id = src["run_id"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", run_id):
        parser.error("Invalid run ID")
    result = {
        "run_id": run_id, "elapsed_s": float(src["elapsed_s"]),
        "totals": numbers(src["totals"], "totals"),
        "by_country": {key: numbers(row, "country") for key, row in src["by_country"].items() if re.fullmatch(r"[A-Z]{2}", key)},
        "variance": {"by_pair_type": {key: numbers(row, "variance") for key, row in src["variance"]["by_pair_type"].items() if key in {"new_ip", "same_ip", "local", "forced_rotate"}}},
        "sticky_continuity": numbers(src["sticky_continuity"], "sticky"),
    }
    if args.account_usage_delta_bytes is not None:
        result["account_usage_delta_bytes"] = args.account_usage_delta_bytes
    folder = Path(__file__).parent
    index = folder / "index.json"
    runs = json.loads(index.read_text())["runs"] if index.exists() else []
    path = run_id + ".json"
    for file, data in ((folder / path, result), (index, {"runs": [
        {"id": run_id, "title": args.title, "path": path}, *[row for row in runs if row["id"] != run_id],
    ]})):
        file.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
