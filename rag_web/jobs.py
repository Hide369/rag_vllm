"""バックグラウンドジョブと共有リソース操作の排他ロック。

IIS ARR は素の HTTP リクエストを 120 秒で切るため、長い操作はジョブとして
別スレッドで走らせ、ブラウザが get でポーリングする。ジョブはプロセス内
メモリにだけ置き、結果は一度取得したか、終了から ttl_s 秒経つと捨てる。

取り込み・アップロード・テンプレート登録削除のように共有リソースを変える
操作は、全ユーザーを通して同時に1つしか走らせない。ロックは待たず、
取れなければ即座に SharedOperationBusy を出す（API 層で 409 にする）。
"""

import logging
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

logger = logging.getLogger(__name__)


class SharedOperationBusy(Exception):
    """別の共有操作が実行中で、ロックを取れなかった。"""


@dataclass
class _Job:
    state: str = "running"
    result: dict | None = None
    error: str | None = None
    finished_at: float | None = None
    finished: threading.Event = field(default_factory=threading.Event)


class JobStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl_s: float = 600):
        self._clock = clock
        self._ttl_s = ttl_s
        self._jobs: dict[str, _Job] = {}
        self._jobs_lock = threading.Lock()
        self._shared_lock = threading.Lock()

    def shared_busy(self) -> bool:
        return self._shared_lock.locked()

    @contextmanager
    def shared_operation(self) -> Iterator[None]:
        """共有ロックを待たずに取る。取れなければ SharedOperationBusy。例外時も解放する。"""
        self._acquire_shared()
        try:
            yield
        finally:
            self._shared_lock.release()

    def _acquire_shared(self) -> None:
        if not self._shared_lock.acquire(blocking=False):
            raise SharedOperationBusy("別の共有操作が実行中です")

    def start(self, work: Callable[[], dict], *, shared: bool) -> str:
        # ジョブ生成を先に済ませ、ロック取得後に失敗しうる処理を挟まない。
        job_id = secrets.token_urlsafe(24)
        job = _Job()
        # ロックはスレッド起動前に取り、busy 判定を呼び出し側で同期的に行う。
        if shared:
            self._acquire_shared()
        try:
            with self._jobs_lock:
                self._jobs[job_id] = job
            threading.Thread(
                target=self._run, args=(job, work, shared), daemon=True
            ).start()
        except BaseException:
            # スレッドを起動できなかった場合にロックもジョブも残さない。
            with self._jobs_lock:
                self._jobs.pop(job_id, None)
            if shared:
                self._shared_lock.release()
            raise
        return job_id

    def _run(self, job: _Job, work: Callable[[], dict], shared: bool) -> None:
        # どの経路でも終端状態を記録する（running のまま残るとクライアントが
        # 永久にポーリングする）。SystemExit 等も failed として記録し、
        # スレッドからは再送出しない（未処理例外の警告になるため）。
        outcome: tuple[str, dict | None, str | None] = (
            "failed", None, "ジョブが異常終了しました",
        )
        try:
            outcome = ("done", work(), None)
        except Exception as e:
            logger.exception("ジョブが失敗しました")
            outcome = ("failed", None, str(e))
        except BaseException as e:
            logger.exception("ジョブが例外で中断されました")
            outcome = ("failed", None, str(e) or type(e).__name__)
        finally:
            if shared:
                self._shared_lock.release()
            with self._jobs_lock:
                job.state, job.result, job.error = outcome
                job.finished_at = self._clock()
            job.finished.set()

    def wait_finished(self, job_id: str, timeout_s: float) -> bool:
        """ジョブの終了を最大 timeout_s 秒待つ。

        結果は消費しない（get と違い取り出さない）。テストの同期や終了処理に使える。
        未知の id は False。
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        return job is not None and job.finished.wait(timeout_s)

    def wait_idle(self, timeout_s: float) -> bool:
        """走っているジョブがすべて終わるまで最大 timeout_s 秒待つ。

        終了処理（テストの teardown で DB 接続を閉じる前など）が、ジョブの
        スレッドの書き込みと競合しないために使う。時間内に終われば True、
        timeout_s を超えれば False。待っている間に新しく始まったジョブは対象外。
        """
        # 注入された clock は TTL 用で止まりうるため、待ちの締め切りは実時間で測る。
        deadline = time.monotonic() + timeout_s
        with self._jobs_lock:
            running = [job.finished for job in self._jobs.values() if not job.finished.is_set()]
        for finished in running:
            # 各待ちは残り時間で区切る。全体で timeout_s を超えて待たない。
            if not finished.wait(max(0.0, deadline - time.monotonic())):
                return False
        return True

    def get(self, job_id: str) -> dict | None:
        with self._jobs_lock:
            self._purge_expired()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state == "running":
                return {"state": "running"}
            del self._jobs[job_id]
            if job.state == "done":
                return {"state": "done", "result": job.result}
            return {"state": "failed", "error": job.error}

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > self._ttl_s
        ]
        for job_id in expired:
            del self._jobs[job_id]
