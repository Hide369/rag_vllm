import pytest

from ingest import store
from ingest.embedder import EMBED_DIM
from ingest.models import Chunk
from ingest.store import (
    delete_orphans,
    indexed_sources,
    open_store,
    replace_source,
    stored_file_hash,
)


@pytest.fixture
def collection():
    """インメモリのストア。ディスクには触れない。"""
    return open_store(":memory:")


def _chunks(source, file_hash, count=2):
    return [
        Chunk(
            id=f"{source}::page{i}::0",
            text=f"{source}の{i}ページ目",
            metadata={
                "source": source,
                "file_hash": file_hash,
                "location_type": "page",
                "location": i,
                "ocr": False,
                "chunk_index": 0,
                "indexed_at": "2026-08-11",
            },
        )
        for i in range(1, count + 1)
    ]


def _vectors(count):
    return [[0.1] * EMBED_DIM for _ in range(count)]


def _add(collection, source, file_hash, count=2):
    chunks = _chunks(source, file_hash, count)
    replace_source(collection, source, chunks, _vectors(len(chunks)))
    return chunks


def test_stored_hash_is_none_for_unknown_source(collection):
    assert stored_file_hash(collection, "未登録.pdf") is None


def test_stored_hash_is_returned_after_adding(collection):
    _add(collection, "a.pdf", "hash1")
    assert stored_file_hash(collection, "a.pdf") == "hash1"


def test_replacing_a_source_removes_its_old_chunks(collection):
    _add(collection, "a.pdf", "hash1", count=5)
    _add(collection, "a.pdf", "hash2", count=2)
    assert collection.count() == 2
    assert stored_file_hash(collection, "a.pdf") == "hash2"


def test_replacing_a_source_leaves_other_sources_intact(collection):
    _add(collection, "a.pdf", "hash1", count=3)
    _add(collection, "b.pptx", "hash2", count=2)
    _add(collection, "a.pdf", "hash3", count=1)
    assert collection.count() == 3
    assert stored_file_hash(collection, "b.pptx") == "hash2"


def test_indexed_sources_lists_every_source(collection):
    _add(collection, "a.pdf", "hash1")
    _add(collection, "b.pptx", "hash2")
    assert indexed_sources(collection) == {"a.pdf", "b.pptx"}


def test_orphans_are_deleted(collection):
    """source/ から資料を消したら、DB側も追従しないと幽霊の出典が出る。"""
    _add(collection, "a.pdf", "hash1")
    _add(collection, "消した.pptx", "hash2")
    removed = delete_orphans(collection, known_sources={"a.pdf"})
    assert removed == ["消した.pptx"]
    assert indexed_sources(collection) == {"a.pdf"}


def test_nothing_is_deleted_when_all_sources_are_known(collection):
    _add(collection, "a.pdf", "hash1")
    assert delete_orphans(collection, known_sources={"a.pdf"}) == []
    assert collection.count() == 2


def test_metadata_survives_a_round_trip(collection):
    _add(collection, "a.pdf", "hash1", count=1)
    stored = collection.get(include=["metadatas"])["metadatas"][0]
    assert stored["location"] == 1
    assert stored["location_type"] == "page"
    assert stored["ocr"] is False


def test_all_documents_returns_ids_and_texts_in_the_same_order(collection):
    """BM25インデックスはIDと本文の並びが一致していることに依存する。

    本文単位になったため、返るIDは渡した occurrence ID（"x"/"y"）ではなく
    本文のハッシュである。ここでは決め打ちのIDと比べる代わりに、
    chunks_by_ids（occurrences経由の別経路）で引き直しても同じ本文が
    同じ位置に戻ることを確かめ、並びの対応を独立に検証する。
    """
    collection.add(
        ids=["x", "y"],
        documents=["本文エックス", "本文ワイ"],
        embeddings=[[0.1], [0.2]],
        metadatas=[{"source": "a.pptx"}, {"source": "b.pptx"}],
    )
    ids, documents = store.all_documents(collection)
    assert set(documents) == {"本文エックス", "本文ワイ"}
    found = collection.chunks_by_ids(ids)
    assert [found[chunk_id][0] for chunk_id in ids] == documents


def test_all_documents_on_an_empty_collection_returns_two_empty_lists(collection):
    assert store.all_documents(collection) == ([], [])


# --- 埋め込みモデルの記録（設計書: DB に作ったモデルを残し、食い違えば止める） ---------


def test_an_unrecorded_store_is_treated_as_bge_m3_1024(collection):
    _add(collection, "a.md", "h1")  # 中身がある（記録を持たない時代のDB）
    store.ensure_embedding_model(collection, "bge-m3", 1024)
    with pytest.raises(store.EmbeddingMismatchError) as raised:
        store.ensure_embedding_model(collection, "qwen3-embedding", 2560)
    assert "bge-m3" in str(raised.value)
    assert "qwen3-embedding" in str(raised.value)


def test_a_different_dimension_stops_even_with_the_same_name(collection):
    _add(collection, "a.md", "h1")
    with pytest.raises(store.EmbeddingMismatchError):
        store.ensure_embedding_model(collection, "bge-m3", 768)


def test_an_org_prefix_and_case_do_not_make_a_different_model(collection):
    collection.set_meta_if_absent("embedding_model", "BAAI/bge-m3")
    collection.set_meta_if_absent("embedding_dim", "1024")
    store.ensure_embedding_model(collection, "bge-m3", 1024)
    store.ensure_embedding_model(collection, "BGE-M3", 1024)


def test_a_recorded_other_model_stops_the_default(collection):
    collection.set_meta_if_absent("embedding_model", "qwen3-embedding")
    collection.set_meta_if_absent("embedding_dim", "2560")
    with pytest.raises(store.EmbeddingMismatchError):
        store.ensure_embedding_model(collection, "bge-m3", 1024)


def test_the_first_write_records_the_model_and_the_second_does_not_overwrite(
    collection, monkeypatch
):
    monkeypatch.setenv("VLLM_EMBED_MODEL", "BAAI/bge-m3")
    _add(collection, "a.md", "h1")
    assert collection.get_meta("embedding_model") == "BAAI/bge-m3"
    assert collection.get_meta("embedding_dim") == str(EMBED_DIM)

    monkeypatch.setenv("VLLM_EMBED_MODEL", "bge-m3")  # 同じモデルの別表記
    _add(collection, "b.md", "h2")
    assert collection.get_meta("embedding_model") == "BAAI/bge-m3"


def test_a_write_with_a_mismatched_setting_is_refused_before_anything_is_written(
    collection, monkeypatch
):
    collection.set_meta_if_absent("embedding_model", "qwen3-embedding")
    collection.set_meta_if_absent("embedding_dim", "2560")
    with pytest.raises(store.EmbeddingMismatchError):
        _add(collection, "a.md", "h1")
    assert collection.count() == 0


def test_deleting_from_an_empty_store_records_nothing(collection):
    replace_source(collection, "a.md", [], [])
    assert collection.get_meta("embedding_model") is None


def test_open_store_stops_a_mismatched_db_and_closes_it(tmp_path, monkeypatch):
    path = str(tmp_path / "s.sqlite3")
    first = store.vector_store.open_store(path)
    first.set_meta_if_absent("embedding_model", "qwen3-embedding")
    first.set_meta_if_absent("embedding_dim", "2560")
    first.close()
    with pytest.raises(store.EmbeddingMismatchError):
        open_store(path)


def test_an_empty_unrecorded_store_adopts_the_configured_model_on_first_write(
    collection, monkeypatch
):
    """新しい空のDBは何のモデルでも受け入れ、最初の書き込みで記録する。
    止めると「新しい DB に取り込み直してください」の案内が実行できない。"""
    monkeypatch.setenv("VLLM_EMBED_MODEL", "other-1024")
    store.ensure_embedding_model(collection, "qwen3-embedding", 2560)

    _add(collection, "a.md", "h1")

    assert collection.get_meta("embedding_model") == "other-1024"
    assert collection.get_meta("embedding_dim") == str(EMBED_DIM)
    store.ensure_embedding_model(collection, "other-1024", EMBED_DIM)
    with pytest.raises(store.EmbeddingMismatchError):
        store.ensure_embedding_model(collection, "bge-m3", 1024)


def test_a_non_empty_unrecorded_store_still_stops_a_non_bge_m3_setting(collection):
    _add(collection, "a.md", "h1")
    with pytest.raises(store.EmbeddingMismatchError):
        store.ensure_embedding_model(collection, "other-1024", 1024)


def test_open_store_accepts_a_brand_new_path_with_another_model(tmp_path, monkeypatch):
    monkeypatch.setenv("VLLM_EMBED_MODEL", "other-1024")
    open_store(str(tmp_path / "new.sqlite3")).close()


def test_the_docs_ingest_command_is_defined_once():
    """技術DBの取り込みコマンドは store にだけ書く。

    画面・実測スクリプト・MCP サーバが各自で持つと、片方だけ直したときに
    案内どおり実行しても別のDBへ取り込まれる。MCP サーバは rag_web.app
    （FastAPI の画面一式）を読み込めないので、共通の置き場所が要る。
    """
    from rag_web import app
    from scripts import check_retrieval

    assert store.DOCS_INGEST_COMMAND == (
        "python -m scripts.fetch_docs のあと "
        "python -m scripts.ingest_source --source-dir docs_source "
        "--db docs_store.sqlite3 --keep-code-blocks"
    )
    assert store.DOCS_INGEST_COMMAND.endswith(store.DOCS_INGEST_SOURCE_COMMAND)
    assert store.DOCS_DB_FILENAME in store.DOCS_INGEST_SOURCE_COMMAND
    # オフラインの GB10 では fetch_docs を走らせられない。MCP の案内はこちらを使う。
    assert "fetch_docs" not in store.DOCS_INGEST_SOURCE_COMMAND
    assert app.DOCS_INGEST_COMMAND is store.DOCS_INGEST_COMMAND
    assert check_retrieval.DOCS.ingest_command is store.DOCS_INGEST_COMMAND
