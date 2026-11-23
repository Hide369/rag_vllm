"""保存済みベクトルと vLLM の埋め込みを比べるスクリプト。偽の埋め込みを使う。"""
import numpy as np
import pytest

from ingest.vector_store import open_store
from scripts import check_embedding_compat

TEXTS = ["あ", "い", "う", "え"]


def _unit(angle: float) -> list[float]:
    return [float(np.cos(angle)), float(np.sin(angle)), 0.0, 0.0]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "s.sqlite3"
    collection = open_store(str(path))
    collection.add(
        ids=[str(i) for i in range(len(TEXTS))],
        documents=TEXTS,
        metadatas=[{"source": "a.md"}] * len(TEXTS),
        embeddings=[_unit(0.0)] * len(TEXTS),
    )
    collection.close()
    return path


def _fake_embedder(monkeypatch, angle, seen=None):
    def embed(texts, session=None):
        if seen is not None:
            seen.extend(texts)
        # 長さを変えても結果が変わらないことを確かめるため、わざと 5 倍にする。
        return [[5 * v for v in _unit(angle)] for _ in texts]

    monkeypatch.setattr(check_embedding_compat.embedder, "embed_texts", embed)


def test_identical_vectors_pass_and_the_summary_is_printed(db_path, monkeypatch, capsys):
    seen = []
    _fake_embedder(monkeypatch, 0.0, seen)

    code = check_embedding_compat.main(["--db", str(db_path), "--limit", "3"])

    out = capsys.readouterr().out
    assert code == 0
    assert len(seen) == 3, "--limit の件数だけを埋め込む"
    assert "件数: 3" in out
    assert "最小: 1.000000" in out
    assert "平均: 1.000000" in out


def test_default_limit_is_300():
    assert check_embedding_compat.DEFAULT_LIMIT == 300
    assert check_embedding_compat.DEFAULT_THRESHOLD == 0.99


def test_a_minimum_below_the_threshold_exits_with_1(db_path, monkeypatch, capsys):
    _fake_embedder(monkeypatch, 0.5)  # cos(0.5) = 0.8776

    code = check_embedding_compat.main(["--db", str(db_path)])

    assert code == 1
    assert "0.877" in capsys.readouterr().out


def test_the_threshold_can_be_lowered(db_path, monkeypatch):
    _fake_embedder(monkeypatch, 0.5)
    assert check_embedding_compat.main(["--db", str(db_path), "--threshold", "0.8"]) == 0


def test_an_empty_db_says_so_and_exits_with_2(tmp_path, monkeypatch, capsys):
    _fake_embedder(monkeypatch, 0.0)
    empty = tmp_path / "empty.sqlite3"
    open_store(str(empty)).close()

    code = check_embedding_compat.main(["--db", str(empty)])

    assert code == 2
    assert "空" in capsys.readouterr().out


def test_a_missing_db_exits_with_2_without_creating_a_file(tmp_path, monkeypatch, capsys):
    """存在しないパスを開くと空の DB が黙って作られ、誤ったパスが「空」に見えてしまう。"""
    _fake_embedder(monkeypatch, 0.0)
    missing = tmp_path / "typo.sqlite3"

    code = check_embedding_compat.main(["--db", str(missing)])

    assert code == 2
    assert "見つかりません" in capsys.readouterr().err
    assert not missing.exists()


def test_the_session_is_closed(db_path, monkeypatch):
    closed = []

    class _Session:
        def close(self):
            closed.append(True)

    _fake_embedder(monkeypatch, 0.0)
    monkeypatch.setattr(check_embedding_compat, "new_session", lambda settings: _Session())

    assert check_embedding_compat.main(["--db", str(db_path)]) == 0
    assert closed == [True]


def test_an_embedding_failure_exits_with_2(db_path, monkeypatch, capsys):
    def boom(texts, session=None):
        raise check_embedding_compat.embedder.EmbeddingError("vLLM が落ちています")

    monkeypatch.setattr(check_embedding_compat.embedder, "embed_texts", boom)

    assert check_embedding_compat.main(["--db", str(db_path)]) == 2
    assert "vLLM が落ちています" in capsys.readouterr().err


def test_a_legacy_setting_stops_before_measuring(db_path, monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_HOST", "http://x")
    assert check_embedding_compat.main(["--db", str(db_path)]) == 2
    assert "VLLM_*" in capsys.readouterr().err
