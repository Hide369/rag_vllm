"""外部 I/O の束。

Ollama・VLM・リランカー・DB・取り込みなど、プロセスの外に触れる関数を1か所に
まとめる。テストは Services の欄を偽物に差し替えるだけで済み、monkeypatch で
モジュールの属性を書き換えずに済む。
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ingest import chat, embedder, parsers, reranker, retrieval, store, vector_store, vlm
from scripts import ingest_source


def _default_db_paths() -> dict[str, str]:
    # パスの組み立ては ingest/store.py に任せる。ここで綴り直すと、別のDBを
    # 開いても例外が出ないまま食い違う。
    return {"internal": str(store.DB_PATH), "docs": str(store.DOCS_DB_PATH)}


@dataclass(frozen=True)
class Services:
    stream_chat: Callable[[str, list[dict], float], Iterator[str]] = chat.stream_chat
    # (model, prompt)。出力の上限は指定せず、サーバーの最大長に任せる。
    ask_json: Callable[..., str] = chat.ask_json
    ask_text: Callable[..., str] = chat.ask_text
    # (path, caption_image=)。Cowork の添付から本文を取り出す。
    parse: Callable[..., list] = parsers.parse
    # (collection, query, index=, rerank=, rerank_floor=)
    search: Callable[..., list] = retrieval.search
    build_index: Callable = retrieval.build_index
    open_store: Callable[[str], object] = vector_store.open_store
    check_ollama: Callable[[], None] = embedder.check_ollama
    check_vlm: Callable[[], None] = vlm.check_vlm
    caption_image: Callable = vlm.caption_image
    check_reranker: Callable[[], None] = reranker.check_reranker
    rerank: Callable = reranker.rerank
    ingest_directory: Callable = ingest_source.ingest_directory
    ingest_uploads: Callable = ingest_source.ingest_uploads
    source_dir: Path = ingest_source.DEFAULT_SOURCE_DIR
    db_paths: Mapping[str, str] = field(default_factory=_default_db_paths)
    # None なら docgen.templates の既定（templates/）。
    templates_dir: Path | None = None


def caption_image_or_reason(services: Services) -> tuple[Callable | None, str | None]:
    """図表の説明文化に使う関数と、使えないときの理由。

    VLMが無いことは取り込みを止める理由にしない。図表の説明が付かないだけで、
    本文は取り込めるためである。かといって caption_image を渡したまま失敗させる
    のも取らない。画像1枚ごとに4回のリトライ（1+2+4秒）が走り、モデル未pullの
    環境では取り込みが極端に遅くなる（ingest/vlm.py の _MAX_ATTEMPTS）。
    先に1回だけ確かめて、駄目なら渡さない。
    """
    try:
        services.check_vlm()
    except vlm.VlmError as error:
        return None, f"図表の説明文化は行いません: {error}"
    return services.caption_image, None
