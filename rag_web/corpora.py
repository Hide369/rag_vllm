"""コーパスごとの DB・BM25索引・属性一覧のキャッシュと、リランカーの可否確認。

Streamlit の @st.cache_resource（get_collection / get_index / get_schema /
ensure_reranker）の置き換え。
"""

import threading
from collections.abc import Callable

from ingest import conditions
from ingest.reranker import RerankError

from .services import Services

CORPORA = ("internal", "docs")


class CorpusCache:
    def __init__(self, services: Services):
        self._services = services
        self._lock = threading.Lock()
        self._collections: dict[str, object] = {}
        # corpus -> ((db_path, revision), 値)。古い世代は上書きして捨てる。
        self._indexes: dict[str, tuple[tuple[str, int], object]] = {}
        self._schemas: dict[str, tuple[tuple[str, int], dict[str, str]]] = {}
        # 確認の直列化用。状態の読み書きは _lock 側で行い、確認中も状態を読める
        # ようにする。
        self._rerank_check_lock = threading.Lock()
        self._rerank_state = "unchecked"
        self._rerank_reason: str | None = None

    def _db_path(self, corpus: str) -> str:
        return self._services.db_paths[corpus]  # 未知の corpus は KeyError

    def collection(self, corpus: str):
        """DB はパスごとに1回だけ開く。"""
        db_path = self._db_path(corpus)
        with self._lock:
            if corpus not in self._collections:
                self._collections[corpus] = self._services.open_store(db_path)
            return self._collections[corpus]

    def _cached(self, cache: dict, corpus: str, build: Callable):
        """(db_path, revision) を鍵にキャッシュする。

        revision を鍵に含めるのは、書き込みと不可分に進む値だけがキャッシュの
        鮮度を正しく判定できるため。チャンク数だと「同数の差し替え」を取りこぼし、
        消えた旧チャンクIDを持ったままの索引が、理由の説明なくヒットを落とす。
        revision だけでは足りない。revision は各DBが自分の meta テーブルに持つ、
        ファイルごとに独立したカウンタであり、社内資料と技術ドキュメントが同じ
        数値になることは普通に起こる。db_path を鍵に加えないと、たまたま一致した
        瞬間に、後から呼ばれた側が先のコーパスの結果をそのまま受け取ってしまう。
        """
        # 構築は単一のロックの下で走る（この規模では許容）。revision() をロックの
        # 外で読むため、読んだ直後に書き込まれても余分な再構築が起きるだけで済む。
        db_path = self._db_path(corpus)
        collection = self.collection(corpus)
        key = (db_path, collection.revision())
        with self._lock:
            cached = cache.get(corpus)
            if cached is not None and cached[0] == key:
                return cached[1]
            value = build(collection)
            cache[corpus] = (key, value)
            return value

    def index(self, corpus: str):
        """BM25索引。ディスクには持たず、DBを唯一の情報源にする。"""
        return self._cached(self._indexes, corpus, self._services.build_index)

    def schema(self, corpus: str) -> dict[str, str]:
        """絞り込みに使える属性の一覧。取り込み後も再起動なしで反映される。"""
        return self._cached(self._schemas, corpus, conditions.available_keys)

    def rerank_callable(self) -> Callable | None:
        """リランカーが使えれば rerank を返す。使えなければ None（並べ替えなし）。

        成功は覚えて以後確認しない（確認のたびに hf_hub_download が走るため）。
        失敗は覚えず、次の呼び出しで再確認する。確認中の呼び出しは待たせる。
        """
        with self._rerank_check_lock:
            with self._lock:
                if self._rerank_state == "ready":
                    return self._services.rerank
                self._rerank_state = "checking"
                self._rerank_reason = None
            try:
                self._services.check_reranker()
            except RerankError as error:
                with self._lock:
                    self._rerank_state = "failed"
                    self._rerank_reason = f"Rerankerで並べ替えません: {error}"
                return None
            except BaseException as error:
                # 想定外の例外でも "checking" のまま残さない。握りつぶさず投げ直す。
                with self._lock:
                    self._rerank_state = "failed"
                    self._rerank_reason = f"Rerankerで並べ替えません: {error}"
                raise
            with self._lock:
                self._rerank_state = "ready"
            return self._services.rerank

    def reranker_status(self) -> dict:
        with self._lock:
            return {"state": self._rerank_state, "reason": self._rerank_reason}
