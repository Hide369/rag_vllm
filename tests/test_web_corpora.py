import threading
from dataclasses import replace

import pytest

from ingest import store, vector_store
from ingest.embedder import EMBED_DIM
from ingest.models import Chunk
from ingest.reranker import RerankError
from ingest.vlm import VlmError
from rag_web.corpora import CORPORA, CorpusCache
from rag_web.services import Services, caption_image_or_reason


def _write_one(collection, source="a.txt"):
    chunk = Chunk(
        id=f"{source}::page1::0",
        text="本文",
        metadata={
            "source": source,
            "file_hash": "h",
            "location_type": "page",
            "location": 1,
            "ocr": False,
            "chunk_index": 0,
            "indexed_at": "2026-08-11",
        },
    )
    store.replace_source(collection, source, [chunk], [[0.1] * EMBED_DIM])


class Recorder:
    """open_store / build_index の呼び出しを数える偽物。"""

    def __init__(self):
        self.open_calls = []
        self.build_calls = []
        self.opened = []

    def open_store(self, path):
        self.open_calls.append(path)
        collection = vector_store.open_store(path)
        self.opened.append(collection)
        return collection

    def close_stores(self):
        # 開いた接続を閉じないと
        # GC 時に sqlite3 の ResourceWarning が出て、-W error で落ちる。
        for collection in self.opened:
            collection.close()

    def build_index(self, collection):
        self.build_calls.append(collection)
        return object()


@pytest.fixture
def recorder():
    recorder = Recorder()
    yield recorder
    recorder.close_stores()


@pytest.fixture
def services(tmp_path, recorder):
    return Services(
        open_store=recorder.open_store,
        build_index=recorder.build_index,
        db_paths={
            "internal": str(tmp_path / "internal.sqlite3"),
            "docs": str(tmp_path / "docs.sqlite3"),
        },
    )


def test_collection_opened_once_per_corpus(services, recorder):
    cache = CorpusCache(services)
    for _ in range(2):
        for corpus in CORPORA:
            cache.collection(corpus)
    assert len(recorder.open_calls) == 2


def test_index_rebuilt_when_revision_changes(services, recorder):
    cache = CorpusCache(services)
    first = cache.index("internal")
    assert cache.index("internal") is first
    assert len(recorder.build_calls) == 1
    _write_one(cache.collection("internal"))
    cache.index("internal")
    assert len(recorder.build_calls) == 2


def test_index_not_shared_between_corpora_with_equal_revision(services):
    cache = CorpusCache(services)
    assert cache.collection("internal").revision() == cache.collection("docs").revision()
    assert cache.index("internal") is not cache.index("docs")


def _write_numeric(collection, source, value):
    chunk = Chunk(
        id=f"{source}::page1::0",
        text="本文",
        metadata={
            "source": source,
            "file_hash": "h",
            "location_type": "page",
            "location": 1,
            "ocr": False,
            "chunk_index": 0,
            "indexed_at": "2026-08-11",
            "amount": value,
        },
    )
    store.replace_source(collection, source, [chunk], [[0.1] * EMBED_DIM])


def test_schema_follows_revision_and_is_not_shared_between_corpora(services):
    cache = CorpusCache(services)
    assert cache.schema("internal") == {}
    assert cache.schema("docs") == {}
    _write_numeric(cache.collection("internal"), "a.txt", 5)
    updated = cache.schema("internal")
    assert "amount" in updated
    assert cache.schema("docs") == {}


def test_unknown_corpus_raises_key_error(services):
    cache = CorpusCache(services)
    for method in (cache.collection, cache.index, cache.schema):
        with pytest.raises(KeyError):
            method("unknown")


def test_reranker_success_is_remembered(services):
    calls = []
    cache = CorpusCache(replace(services, check_reranker=lambda: calls.append(1)))
    assert cache.reranker_status() == {"state": "unchecked", "reason": None}
    assert cache.rerank_callable() is services.rerank
    assert cache.rerank_callable() is services.rerank
    assert len(calls) == 1
    assert cache.reranker_status() == {"state": "ready", "reason": None}


def test_reranker_failure_retried_next_time(services):
    outcomes = [RerankError("x"), None]

    def check():
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome

    cache = CorpusCache(replace(services, check_reranker=check))
    assert cache.rerank_callable() is None
    status = cache.reranker_status()
    assert status["state"] == "failed"
    assert status["reason"] == "Rerankerで並べ替えません: x"
    assert cache.rerank_callable() is services.rerank
    assert cache.reranker_status()["state"] == "ready"


class _ObservedLock:
    """acquire の試行を数え、2回目の試行（待たされる側）を Event で知らせる。"""

    def __init__(self, second_attempt):
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self._attempts = 0
        self._second_attempt = second_attempt

    def __enter__(self):
        with self._guard:
            self._attempts += 1
            if self._attempts == 2:
                self._second_attempt.set()
        self._lock.acquire()

    def __exit__(self, *exc_info):
        self._lock.release()


def test_reranker_check_not_run_concurrently(services):
    entered = threading.Event()
    release = threading.Event()
    second_attempt = threading.Event()
    calls = []

    def check():
        calls.append(1)
        entered.set()
        assert release.wait(timeout=10)

    cache = CorpusCache(replace(services, check_reranker=check))
    # 直列化用のロックを観測用に差し替え、2本目が待ち始めたことを確かめる。
    cache._rerank_check_lock = _ObservedLock(second_attempt)
    results = []
    first = threading.Thread(target=lambda: results.append(cache.rerank_callable()))
    first.start()
    assert entered.wait(timeout=10)
    assert cache.reranker_status()["state"] == "checking"
    second = threading.Thread(target=lambda: results.append(cache.rerank_callable()))
    second.start()
    assert second_attempt.wait(timeout=10)
    assert len(calls) == 1
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert len(calls) == 1
    assert results == [services.rerank, services.rerank]


def test_caption_image_or_reason_reports_vlm_error(services):
    def failing():
        raise VlmError("x")

    caption, reason = caption_image_or_reason(replace(services, check_vlm=failing))
    assert caption is None
    assert reason == "図表の説明文化は行いません: x"


def test_caption_image_or_reason_returns_captioner_when_available(services):
    caption, reason = caption_image_or_reason(replace(services, check_vlm=lambda: None))
    assert caption is services.caption_image
    assert reason is None


def test_collection_refuses_a_db_built_with_another_embedding_model(
    services, recorder, tmp_path
):
    """画面の検索も、読むだけで開く入口である。食い違いのまま開くと、検索語の
    ベクトルだけが別のモデルになり、距離が静かに狂う。"""
    seed = vector_store.open_store(services.db_paths["internal"])
    seed.set_meta_if_absent("embedding_model", "qwen3-embedding")
    seed.set_meta_if_absent("embedding_dim", "2560")
    seed.close()

    cache = CorpusCache(services)
    with pytest.raises(store.EmbeddingMismatchError) as raised:
        cache.collection("internal")

    assert "qwen3-embedding" in str(raised.value)
    assert "VLLM_EMBED_MODEL" in str(raised.value)
