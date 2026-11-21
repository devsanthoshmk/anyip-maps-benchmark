"""Generate a dashboard-ready report from fictional records, without network calls."""

import argparse
from pathlib import Path

from analyze import write_outputs
from store import Store


def generate(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=False)
    database = out_dir / "run.db"
    run_id = "offline_fixture_interrupted"
    store = Store(database)
    try:
        store.init()
        store.insert_run(run_id=run_id, started_at=100, finished_at=200, status="stopped")
        cases = [
            ("same", "IN", "ok", 200, "192.0.2.1"),
            ("changed", "IN", "ok", 200, "192.0.2.2"),
            ("missing", "US", "consent", 200, None),
            ("denied", "US", "403", 403, None),
            ("limited", "US", "429", 429, None),
        ]
        for index, (case, country, outcome, status, end_ip) in enumerate(cases):
            t = 105 + index * 10
            session = dict(run_id=run_id, session_id=case)
            query = dict(**session, country=country, query=f"Fixture: {case}", query_index=1)
            store.upsert_session(**session, country=country, pool="residential", arm="offline_fixture",
                                 started_at=t - 2, ended_at=t + 3, status="completed")
            store.insert_request(**query, ts=t, request_index=1, outcome=outcome, status=status,
                                 is_final=1, latency_ms=100, bytes_wire=1024, bytes_rx=512)
            if outcome == "ok":
                store.insert_place(**query, rank=1, place_id="fixture-place", title="Fictional place")
            store.insert_ip_observation(**session, ts=t - 1, witness="ipify_v4", family="v4", ip="192.0.2.1")
            if end_ip:
                store.insert_ip_observation(**session, ts=t + 1, witness="ipify_v4", family="v4", ip=end_ip)
    finally:
        store.close()
    return write_outputs(database, run_id, out_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    print(generate(parser.parse_args().output_dir)["report_file"])
