"""Recompute the headline results without credentials, dependencies, or network access."""

from collections import Counter, defaultdict
from itertools import combinations
import json
from pathlib import Path
from statistics import mean


def main():
    data = json.loads(Path(__file__).with_name("evidence.json").read_text())
    queries = data["queries"]
    assert len(queries) == 100
    outcomes = Counter(a[0] for q in queries for a in q["attempts"])
    assert outcomes == {"ok": 284, "network_error": 5, "soft_block": 5}, outcomes
    places = sum(len(q["ranked_places"]) for q in queries)
    assert places == 3489
    assert data["place_validation"]["ok"] == 3483
    assert data["account_usage_delta_bytes"] == 22_069_790
    print(f"{len(queries)} searches, {sum(outcomes.values())} attempts, {places} place records")
    print("Attempts:", dict(outcomes))

    for country, expected in {"IN": (44, 47, 1, 2, 9, 1831.4), "US": (41, 49, 0, 1, 6, 1499.5)}.items():
        runs = [q for q in queries if q["country"] == country]
        assert len(runs) == 50
        endpoints = Counter(
            q["endpoints"][0] == q["endpoints"][1] if q["comparable_endpoints"] else None for q in runs
        )
        sessions = defaultdict(list)
        for q in runs:
            if q["variant"] == "same_ip":
                sessions[q["sticky_session"]].append(q)
        assert len(sessions) == 10 and all(len(repeats) == 2 for repeats in sessions.values())
        same = sum(
            all(q["comparable_endpoints"] for q in repeats)
            and len({ip for q in repeats for ip in q["endpoints"]}) == 1 for repeats in sessions.values()
        )
        ok_ms = round(mean(a[2] for q in runs for a in q["attempts"] if a[0] == "ok"), 1)
        result = (sum(q["status"] == "success" for q in runs), endpoints[True], endpoints[False], endpoints[None], same, ok_ms)
        assert result == expected, (country, result)
        print(f"{country}: {result[0]}/50 success; IP matched/changed/unknown {result[1:4]}; {same}/10 reused IDs matched; {ok_ms} ms mean OK latency")

    groups = defaultdict(list)
    for q in queries:
        if q["ranked_places"]:
            groups[(q["query"], q["variant"])].append(q)
    similarities = defaultdict(list)
    for (_, variant), repeats in groups.items():
        for a, b in combinations(repeats, 2):
            common = [([x for x in a["witness_labels"] if x.startswith(family)],
                       [x for x in b["witness_labels"] if x.startswith(family)]) for family in ("v4-", "v6-")]
            common = [(x, y) for x, y in common if x and y]
            if not common or not all(len(x) == len(y) == 1 and (x == y) == (variant == "same_ip") for x, y in common):
                continue
            x, y = set(a["ranked_places"][:20]), set(b["ranked_places"][:20])
            similarities[variant].append(len(x & y) / len(x | y))
    for variant, expected in {"same_ip": (13, 0.8385), "new_ip": (30, 0.6413)}.items():
        scores = similarities[variant]
        result = len(scores), round(mean(scores), 4)
        assert result == expected, (variant, result)
        print(f"{variant}: {result[0]} comparable pairs; mean Jaccard@20 {result[1]:.4f}")


if __name__ == "__main__":
    main()
