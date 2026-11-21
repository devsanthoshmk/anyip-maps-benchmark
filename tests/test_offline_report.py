import json

import pytest

from analyze import run_metrics, write_outputs
from offline_report import generate
from store import Store


def test_offline_fixture_reports_known_outcomes(tmp_path):
    folder = tmp_path / "offline"
    generate(folder)
    summary = json.loads((folder / "summary.json").read_text())
    assert summary["total_queries"] == 5
    assert summary["successful_queries"] == 2
    assert summary["consent_requests"] == summary["block_403"] == summary["block_429"] == 1
    states = {q["session"]: q["ip_stable"] for q in summary["queries"]}
    assert states == {"same": True, "changed": False, "missing": None, "denied": None, "limited": None}
    assert "**Status:** `stopped`" in (folder / "report.md").read_text()

    analysis = json.loads((folder / "analysis.json").read_text())
    assert analysis["status"] == "stopped"
    assert summary["total_requests"] == 5
    assert summary["failed_queries"] == 3
    assert sum(analysis["request_outcome_counts"].values()) == 5
    assert analysis["sticky_continuity"]["query_ip_matched"] == 1
    assert analysis["sticky_continuity"]["query_ip_rotated"] == 1
    queries = {q["session"]: q for q in summary["queries"]}
    assert queries["same"]["exit_ip_start"] == queries["same"]["exit_ip_end"] == "192.0.2.1"
    assert queries["changed"]["exit_ip_end"] == "192.0.2.2"
    assert queries["missing"]["exit_ip_start"] == "192.0.2.1"
    assert queries["missing"]["exit_ip_end"] is None


@pytest.mark.parametrize("status", ["stopped", "failed"])
def test_interrupted_report_keeps_all_request_outcomes(tmp_path, status):
    """Retries remain request evidence even when the run does not finish."""
    database = tmp_path / "run.db"
    outcomes = ["ok", "consent", "403", "429", "sorry", "soft_block",
                "proxy_error", "google_5xx", "network_error", "other_error"]
    store = Store(database)
    try:
        store.insert_run(run_id="interrupted", started_at=100, finished_at=200, status=status)
        store.upsert_session(run_id="interrupted", session_id="s", arm="load", status=status)
        for index, outcome in enumerate(outcomes, 1):
            store.insert_request(run_id="interrupted", session_id="s", query="tea", country="IN",
                                 query_index=1, request_index=index, ts=100 + index,
                                 outcome=outcome, is_final=int(index != 7),
                                 bytes_wire=1024, bytes_rx=2048, latency_ms=100)
    finally:
        store.close()

    write_outputs(database, "interrupted", tmp_path / "output")
    analysis = json.loads((tmp_path / "output" / "analysis.json").read_text())
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())
    report = (tmp_path / "output" / "report.md").read_text()
    assert analysis["status"] == status
    assert f"**Status:** `{status}`" in report
    assert analysis["request_outcome_counts"] == dict.fromkeys(outcomes, 1)
    assert analysis["totals"]["total_requests"] == summary["total_requests"] == 10
    assert analysis["totals"]["final_requests"] == 9
    assert analysis["rates"]["success_rate"] == pytest.approx(0.1)
    assert analysis["rates"]["block_rate"] == pytest.approx(0.5)
    assert analysis["rates"]["proxy_error_rate"] == pytest.approx(0.1)
    assert analysis["traffic"]["traffic_mb_wire"] == round(10 * 1024 / 1024**2, 3)
    assert sum(q["requests"] for q in summary["queries"]) == 10


@pytest.mark.parametrize("probes,expected", [
    ([], None),
    ([(109, "192.0.2.1")], None),
    ([(111, "192.0.2.1")], None),
    ([(109, "192.0.2.1"), (111, "192.0.2.1")], True),
    ([(109, "192.0.2.1"), (111, "192.0.2.2")], False),
])
def test_query_ip_evidence_requires_both_sides(tmp_path, probes, expected):
    database = tmp_path / "run.db"
    store = Store(database)
    try:
        store.insert_run(run_id="evidence", started_at=100, finished_at=120, status="stopped")
        store.upsert_session(run_id="evidence", session_id="s", arm="variance")
        store.insert_request(run_id="evidence", session_id="s", query="tea", country="IN",
                             query_index=1, ts=110, outcome="ok", is_final=1)
        for ts, ip in probes:
            store.insert_ip_observation(run_id="evidence", session_id="s", ts=ts,
                                        family="v4", witness="ipify_v4", ip=ip)
    finally:
        store.close()
    metrics = run_metrics(database, "evidence")
    assert metrics["queries"][0]["ip_stable"] is expected
    assert metrics["sticky_continuity"]["query_ip_matched"] == int(expected is True)
    assert metrics["sticky_continuity"]["query_ip_rotated"] == int(expected is False)
