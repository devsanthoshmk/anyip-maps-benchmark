import asyncio
import json
import sqlite3

import pytest

from analyze import run_metrics
from store import Store
from scraper import RequestGuard


def test_residential_first_plan_has_no_direct_baseline():
    from experiment import load_config

    config = load_config("experiments/residential_first.toml")
    assert {arm.country for arm in config.arms} == {"IN", "US"}
    assert config.rate_limit_rps == 2
    for arm in config.arms:
        assert (arm.kind, arm.pool, arm.include_local) == ("variance", "residential", False)
        assert arm.parallel_variance_queries == 5
        assert arm.repeat_delay_s == 60


@pytest.mark.parametrize("outcome,reason", [("429", "block_rate"), ("proxy_error", "proxy_error_rate")])
def test_guard_stops_queued_request(outcome, reason):
    async def check():
        guard = RequestGuard(asyncio.Event(), 0.01, 0.5, 0.5, min_samples=2)
        assert await guard.admit()
        pending = asyncio.create_task(guard.admit())
        await asyncio.sleep(0)
        guard.observe(outcome)
        assert not guard.stop.is_set()
        guard.observe("ok")
        assert not await asyncio.wait_for(pending, 1)
        assert guard.reason == reason
    asyncio.run(check())


def test_arm_failure_finalizes_database(tmp_path, monkeypatch):
    import experiment

    async def fail(*args):
        raise RuntimeError("mock arm failure")

    monkeypatch.setattr(experiment, "load_config", lambda _: experiment.RunConfig(
        id="failed_test", arms=[experiment.ArmConfig(name="probe", kind="ttl", pool="local")],
    ))
    monkeypatch.setattr(experiment.ProxyConfig, "has_api", lambda self: False)
    monkeypatch.setattr(experiment.SessionWorker, "run_ttl_session", fail)
    with pytest.raises(ExceptionGroup):
        experiment.run_experiment("unused", output_dir=tmp_path)
    folder = tmp_path / "failed_test"
    with sqlite3.connect(folder / "run.db") as conn:
        assert conn.execute("SELECT status FROM runs").fetchone() == ("failed",)
    assert (folder / "analysis.json").exists()


def test_guard_spaces_concurrent_starts():
    async def check():
        loop = asyncio.get_running_loop()
        guard = RequestGuard(asyncio.Event(), 25, 0.5, 0.5)

        async def start():
            assert await guard.admit()
            return loop.time()

        times = sorted(await asyncio.gather(*(start() for _ in range(3))))
        assert all(b - a >= 0.035 for a, b in zip(times, times[1:]))
    asyncio.run(check())


def test_repeated_queries_keep_their_own_places(tmp_path):
    path = tmp_path / "run.db"
    store = Store(str(path))
    store.insert_run(run_id="test")
    for index in (1, 2):
        common = dict(run_id="test", session_id="s", query="tea", country="IN", query_index=index)
        store.insert_request(**common, kind="preview", outcome="ok", is_final=1)
        store.insert_place(**common, place_id=f"p{index}", rank=1, validation="ok")
    store.close()

    queries = run_metrics(path, "test")["queries"]
    assert len(queries) == 2


def test_pool_comparison_does_not_include_load_sessions(tmp_path):
    path = tmp_path / "run.db"
    store = Store(str(path))
    store.insert_run(run_id="test", config_json=json.dumps({"arms": [
        {"name": "stress", "kind": "load"}, {"name": "matched", "kind": "pool_compare"},
    ]}))
    store.upsert_session(run_id="test", session_id="stress", arm="stress", pool="residential")
    store.upsert_session(run_id="test", session_id="matched", arm="matched", pool="mobile")
    store.close()

    assert set(run_metrics(path, "test")["pools"]) == {"mobile"}
