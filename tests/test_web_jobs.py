"""rag_web.jobs のテスト。スレッドの同期は threading.Event と有限の待ちだけで行う。"""

import threading

import pytest

from rag_web.jobs import JobStore, SharedOperationBusy

WAIT_S = 5


def _wait_until_finished(store, job_id):
    """ジョブの終了を有限時間だけ待ち、get の結果（消費される）を返す。"""
    assert store.wait_finished(job_id, WAIT_S), "ジョブが時間内に終わらなかった"
    return store.get(job_id)


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _blocking_work():
    """(work, started, release) を返す。release が set されるまで work は終わらない。"""
    started = threading.Event()
    release = threading.Event()

    def work():
        started.set()
        assert release.wait(WAIT_S)
        return {"ok": True}

    return work, started, release


def test_job_runs_and_result_is_returned_once():
    store = JobStore()
    work, started, release = _blocking_work()
    job_id = store.start(work, shared=False)
    assert started.wait(WAIT_S)
    assert store.get(job_id) == {"state": "running"}
    release.set()
    assert _wait_until_finished(store, job_id) == {"state": "done", "result": {"ok": True}}
    assert store.get(job_id) is None


def test_failed_job_reports_error():
    store = JobStore()

    def work():
        raise RuntimeError("boom")

    job_id = store.start(work, shared=False)
    assert _wait_until_finished(store, job_id) == {"state": "failed", "error": "boom"}
    assert store.get(job_id) is None


def test_second_shared_job_is_rejected_while_first_runs():
    store = JobStore()
    work, started, release = _blocking_work()
    store.start(work, shared=True)
    assert started.wait(WAIT_S)
    assert store.shared_busy() is True
    with pytest.raises(SharedOperationBusy):
        store.start(lambda: {}, shared=True)
    release.set()


def test_non_shared_jobs_run_concurrently():
    store = JobStore()
    work1, started1, release1 = _blocking_work()
    work2, started2, release2 = _blocking_work()
    store.start(work1, shared=False)
    store.start(work2, shared=False)
    assert started1.wait(WAIT_S)
    assert started2.wait(WAIT_S)
    assert store.shared_busy() is False
    release1.set()
    release2.set()


def test_shared_lock_released_after_failed_job():
    store = JobStore()

    def work():
        raise RuntimeError("boom")

    first = store.start(work, shared=True)
    _wait_until_finished(store, first)
    assert store.shared_busy() is False
    second = store.start(lambda: {"n": 2}, shared=True)
    assert _wait_until_finished(store, second) == {"state": "done", "result": {"n": 2}}


def test_shared_lock_released_after_successful_job():
    store = JobStore()
    first = store.start(lambda: {}, shared=True)
    _wait_until_finished(store, first)
    assert store.shared_busy() is False


def test_finished_job_expires_after_ttl():
    clock = _FakeClock()
    store = JobStore(clock=clock)
    job_id = store.start(lambda: {"x": 1}, shared=False)
    # 完了を get で消費しないよう、内部状態が finished になるまで待つ。
    assert store.wait_finished(job_id, WAIT_S)
    clock.now += 601
    assert store.get(job_id) is None


def test_finished_job_survives_within_ttl():
    clock = _FakeClock()
    store = JobStore(clock=clock)
    job_id = store.start(lambda: {"x": 1}, shared=False)
    assert store.wait_finished(job_id, WAIT_S)
    clock.now += 599
    assert store.get(job_id) == {"state": "done", "result": {"x": 1}}


def test_unknown_job_returns_none():
    assert JobStore().get("nope") is None


def test_job_ids_are_unguessable():
    store = JobStore()
    first = store.start(lambda: {}, shared=False)
    second = store.start(lambda: {}, shared=False)
    assert first != second
    assert len(first) >= 32 and len(second) >= 32


def test_shared_operation_rejected_while_shared_job_runs():
    store = JobStore()
    work, started, release = _blocking_work()
    store.start(work, shared=True)
    assert started.wait(WAIT_S)
    with pytest.raises(SharedOperationBusy):
        with store.shared_operation():
            pytest.fail("ロックが取れてはいけない")
    release.set()


def test_start_shared_rejected_while_shared_operation_held():
    store = JobStore()
    with store.shared_operation():
        assert store.shared_busy() is True
        with pytest.raises(SharedOperationBusy):
            store.start(lambda: {}, shared=True)
    assert store.shared_busy() is False


def test_shared_operation_released_after_exception():
    store = JobStore()
    with pytest.raises(ValueError):
        with store.shared_operation():
            raise ValueError("x")
    assert store.shared_busy() is False
    with store.shared_operation():
        pass
