"""Build the public evidence file from a local run.db and analysis.json."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3


def export(run):
    analysis = json.loads((run / "analysis.json").read_text())
    ids = defaultdict(dict)

    def label(kind, value):
        if not value:
            return None
        mapping = ids[kind]
        return mapping.setdefault(value, len(mapping) + 1)

    with sqlite3.connect(run / "run.db") as db:
        manifest, = db.execute("SELECT manifest_json FROM runs").fetchone()
        manifest = json.loads(manifest)
        sessions = {sid: (name, variant) for sid, name, variant in db.execute(
            "SELECT session_id, session_name, variant FROM sessions"
        )}
        requests, places, witnesses = (defaultdict(list) for _ in range(3))
        place_validation = defaultdict(int)
        for sid, outcome, final, ms, n in db.execute(
            "SELECT session_id, outcome, is_final, latency_ms, bytes_wire FROM requests ORDER BY id"
        ):
            requests[sid].append([outcome, final, ms, n])
        for sid, pid, title, validation in db.execute(
            "SELECT session_id, place_id, title, validation FROM places ORDER BY id"
        ):
            places[sid].append(label("place", pid or title))
            place_validation[validation or "unclassified"] += 1
        for sid, ip in db.execute("SELECT session_id, ip FROM ip_observations ORDER BY id"):
            if ip:
                family = "v6" if ":" in ip else "v4"
                witnesses[sid].append(f"{family}-{label('ip', ip)}")

    queries = []
    for row in analysis["queries"]:
        sid = row["session"]
        name, variant = sessions[sid]
        queries.append({
            "country": row["country"], "query": label("query", (row["country"], row["query"])),
            "sticky_session": label("session", name), "variant": variant,
            "status": row["status"], "endpoints": [label("ip", row[key]) for key in ("exit_ip_start", "exit_ip_end")],
            "comparable_endpoints": row["ip_stable"] is not None,
            "witness_labels": sorted(set(witnesses[sid])), "attempts": requests[sid],
            "ranked_places": places[sid],
        })
    return {
        "run_id": analysis["run_id"], "elapsed_s": analysis["elapsed_s"],
        "account_usage_delta_bytes": manifest["quota_after"]["consumption_bytes"] - manifest["quota_before"]["consumption_bytes"],
        "place_validation": dict(place_validation), "queries": queries,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(export(args.run), separators=(",", ":")) + "\n")
