"""取り込みドメインの操作。

差分判定に使う file_hash は各チャンクのメタデータに持たせる。別途マニフェスト
ファイルを置くとDBとファイルで状態が二重管理になり、必ず食い違うため。
信頼できる情報源は常にDBひとつにする。

ストレージの詳細（SQLite・正規化・トランザクション）は ingest/vector_store.py に
置く。このモジュールは「資料単位で入れ替える」「source/ から消えた資料を消す」
という取り込みの都合だけを持つ。
"""
from pathlib import Path

from ingest import vector_store
from ingest.embedder import EMBED_DIM
from ingest.models import Chunk
from ingest.vllm_config import load_vllm_settings

DB_FILENAME = "vector_store.sqlite3"
# パスの単一の情報源。呼び出し側が各自で組み立てると、片方だけ間違えても
# open_store は例外を出さずに空のDBを新規作成するため、件数0で検索が全部空に
# なるだけで、テストも緑のまま通ってしまう。
DB_PATH = Path(__file__).resolve().parent.parent / DB_FILENAME

# 技術ドキュメントの取り込み先。社内資料とはファイルごと分ける。同じDBに入れると、
# 社内規程の質問にライブラリのドキュメントが混ざり、「社内資料に無ければ答えない」
# という歯止めが効かなくなる。
# こちらも単一の情報源をここに置く。画面（rag_web/）と実測スクリプト
# （scripts/check_retrieval.py）が各自で組み立てると、片方だけ綴りを間違えても
# open_store は空のDBを新規作成するだけで、しきい値の実測が「0件のDB」に対して
# 走ったことに気づけない。
DOCS_DB_FILENAME = "docs_store.sqlite3"
DOCS_DB_PATH = Path(__file__).resolve().parent.parent / DOCS_DB_FILENAME


# 記録の無いDBは Ollama 時代に bge-m3・1024次元で作られたものである。
# 記録が無いことを「何でもよい」と読むと、別モデルで入れ直した瞬間から
# 距離だけが黙って狂う。
LEGACY_EMBEDDING_MODEL = "bge-m3"
LEGACY_EMBEDDING_DIM = 1024
_MODEL_KEY = "embedding_model"
_DIM_KEY = "embedding_dim"


class EmbeddingMismatchError(Exception):
    """DBのベクトルを作った埋め込みモデルと、設定のモデルが違う。"""


def _canonical_model(name: str) -> str:
    """モデル名を比べるための形にする。

    vLLM は `BAAI/bge-m3` のように組織名つきで名乗ることがあり、設定は
    `bge-m3` と書かれることが多い。同じモデルを別物と判定して全件の
    入れ直しを求めないよう、組織名を落とし、大文字小文字を区別しない。
    """
    return name.strip().rsplit("/", 1)[-1].lower()


def ensure_embedding_model(collection, model: str, dim: int) -> None:
    """DBの記録と設定が同じ埋め込みモデルであることを確かめる。

    読むだけの利用者（MCPサーバー、画面の検索）も通す。検索語のベクトルを
    別のモデルで作ると、例外は出ずに距離だけが意味を失うため、開く時点で止める。
    """
    recorded_model = collection.get_meta(_MODEL_KEY)
    recorded_dim = collection.get_meta(_DIM_KEY)
    if recorded_model is None and recorded_dim is None and collection.count() == 0:
        # 新しい空のDBには、まだ何のモデルで作ったかという事実が無い。bge-m3 と
        # みなすと、「新しい DB に取り込み直してください」という案内どおりに
        # 作った DB まで止めてしまう。最初の書き込みで設定のモデルを記録する。
        return
    # 中身があるのに記録が無いDBは、記録を持たなかった時代（bge-m3・1024次元）の
    # ものである。
    recorded_model = recorded_model or LEGACY_EMBEDDING_MODEL
    recorded_dim = int(recorded_dim or LEGACY_EMBEDDING_DIM)
    if _canonical_model(recorded_model) == _canonical_model(model) and recorded_dim == dim:
        return
    raise EmbeddingMismatchError(
        f"この DB は {recorded_model}（{recorded_dim}次元）で作られています。"
        f"設定は {model}（{dim}次元）です。"
        "VLLM_EMBED_MODEL を DB に合わせるか、新しい DB に取り込み直してください"
        "（ベクトルは別のモデルのものと比べられません）。"
    )


def record_embedding_model(collection, model: str, dim: int) -> None:
    """最初の書き込みで、使った埋め込みモデルを記録する。2回目以降は書かない。"""
    collection.set_meta_if_absent(_MODEL_KEY, model)
    collection.set_meta_if_absent(_DIM_KEY, str(dim))


def open_store(path: str):
    """DBを開き、設定の埋め込みモデルと合うことまで確かめる。

    開くのと確かめるのを分けると、確かめ忘れた入口が食い違いに気づかない。
    食い違ったときは接続を閉じてから投げる（開きっぱなしで残さない）。
    """
    collection = vector_store.open_store(path)
    try:
        ensure_embedding_model(collection, load_vllm_settings().embed_model, EMBED_DIM)
    except Exception:
        collection.close()
        raise
    return collection


def stored_file_hash(collection, source: str) -> str | None:
    """登録済みならそのファイルのハッシュを返す。未登録ならNone。"""
    found = collection.get(where={"source": source}, limit=1)
    metadatas = found["metadatas"]
    return metadatas[0].get("file_hash") if metadatas else None


def replace_source(collection, source: str, chunks: list[Chunk], embeddings) -> None:
    """1つの資料のチャンクを丸ごと入れ替える。

    削除と追加は vector_store.replace が1トランザクションで行う。別々に呼ぶと、
    間で落ちたときに資料が消えたまま残る。
    """
    if chunks:
        # 空のチャンク列は資料の削除であり、ベクトルを1本も書かない。そこで記録
        # すると、何も入っていないDBに設定のモデル名だけが残る。
        # 書く前に確かめるのは、読み取り側を通らない入口（取り込み）でも
        # 別モデルのベクトルが混ざらないようにするため。
        model = load_vllm_settings().embed_model
        ensure_embedding_model(collection, model, EMBED_DIM)
        record_embedding_model(collection, model, EMBED_DIM)
    collection.replace(
        source,
        ids=[chunk.id for chunk in chunks],
        documents=[chunk.text for chunk in chunks],
        metadatas=[chunk.metadata for chunk in chunks],
        embeddings=list(embeddings),
    )


def indexed_sources(collection) -> set[str]:
    if collection.count() == 0:
        return set()
    return {
        meta["source"] for meta in collection.get()["metadatas"] if "source" in meta
    }


def delete_orphans(collection, known_sources: set[str]) -> list[str]:
    """source/ に存在しなくなった資料のチャンクを削除し、削除した資料名を返す。"""
    orphans = sorted(indexed_sources(collection) - set(known_sources))
    for source in orphans:
        collection.delete(where={"source": source})
    return orphans


def all_documents(collection) -> tuple[list[str], list[str]]:
    """全チャンクのIDと本文を、並びを揃えて返す。

    BM25インデックスをディスクに持たず起動時に組み直すため、その入力を
    ここから供給する。DBを唯一の情報源に保つための経路である。

    本文単位で返す。出現単位で返すと、複数の資料が共有する定型文が上位を
    占め、中身の違う根拠を押し出す（実測では上位6件中5件が同一本文だった）。
    """
    return collection.chunks()
