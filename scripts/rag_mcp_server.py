"""Codex から社内資料を引く stdio MCP サーバ。

取り込み済みのDBを検索し、根拠となる原文をそのまま返す。回答文を作らないのは、
コーディングエージェントに要るのが判断済みの答えではなく、コードや設定へ統合
できる原文だからである（設計書3.2節）。

サーバは常駐し、リクエストのたびに revision() を読んでBM25索引の鮮度を見る。
ストアはプロセスの生存期間で1度だけ開く。開く操作は読み取りではないため
（_get_store の説明を参照）、取り込み中の検索が待たされる可能性は
「サーバの生存期間に最大1回」まで減らせるが、ゼロにはならない（設計書5節）。

ツールは2つある。社内資料の search_documents と、技術ドキュメント
（docs_store.sqlite3）の search_tech_docs である。検索の本体は共通で、違いは
_Corpus の値（DB・リランカーの床・文言）にまとめてある
（docs/superpowers/specs/2026-11-24-tech-docs-mcp-tool-design.md）。
"""
import argparse
import re
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from mcp.server import MCPServer

from ingest import reranker, store
from ingest.embedder import EmbeddingError
from ingest.prompting import format_hit_caption
from ingest.retrieval import (
    DOCS_RERANK_FLOOR,
    SEARCH_RESULT_COUNT,
    build_index,
    search,
)
from ingest.vllm_config import load_vllm_settings, reject_legacy_env
from scripts.fetch_docs import load_sources

# 根拠が無いときの出口。0件でも「ヒットしたが無関係」でも、エージェントが取る
# べき道は同じなので、文面を1か所に持って両方から使う。
#
# 一般知識までは許し、社内固有の事項では止める。この線を引くのは、どちらへ倒し
# ても壊れるからである。「推測で答えるな」だけを言うと、社内資料と無縁の質問
# （ライブラリの使い方など）まで回答が止まる。逆に無条件でフォールバックさせる
# と、検索が就業規則を外したときにモデルが日数を創作する。
#
# 「ウェブ検索で補おうとせず」を明示するのが要である。初版はこれを書かずに
# 「推測で答えず、社内資料からは回答できない旨を伝えてください」とだけ書いて
# いた。自分の知識を封じられたエージェントは代替の根拠を外へ探しにいき、ウェブ
# 検索を数回繰り返した末に諦めのメッセージを出して止まる（2026-09-09、--http
# 構成の Codex での実機報告）。0件を「まだ根拠が見つかっていない」と読ませない
# ために、探索を打ち切ってよいことまで書く必要がある。
_FALLBACK = (
    "再検索やウェブ検索で補おうとせず、次のとおり回答してください。"
    "一般的な知識で答えられる質問なら、社内資料に基づかない一般的な回答である"
    "旨を断ったうえで、通常どおり回答して構いません。"
    "社内固有の事項（規程・議事録・社内の決定・組織固有の運用）については"
    "推測せず、社内資料からは確認できない旨を伝えてください。"
)

# 0件のときに返す文言。空文字や空配列を返してはならない。エージェントは根拠が
# 無いときこそ自分の知識で答えにいくため、「探したが無かった」ことを明示的に
# 伝える必要がある（設計書4.6節）。
_NO_HITS = (
    "社内資料を検索しましたが、この質問に関連する記述は見つかりませんでした。\n"
    + _FALLBACK
)

# 結果の末尾に添える歯止め。build_prompt が持っていたものを引き継ぐ（設計書4.6節）。
# しきい値0.50は関連度の高さまでは保証しないため、距離が閾値内であることを
# 関連性の根拠にしてはならない。AGENTS.md にも同じ規範を書くが、そちらを
# 読んでいない経路でも効くよう、結果自体にも添える。
#
# 無関係だったときの出口は0件と同じ _FALLBACK にする。しきい値をぎりぎり通った
# 無関係な結果は、エージェントから見れば根拠が無いのと変わらない。片方だけ出口を
# 用意すると、こちらの経路にウェブ検索のループが残る。
#
# ここで「資料からは回答できない旨を伝えてください」と言い切らないこと。直後に
# 続く _FALLBACK の「一般知識でなら答えてよい」と正面から矛盾し、どちらに従うか
# をエージェントに委ねてしまう。無関係だったときにどう答えるかは _FALLBACK 側の
# 条件つきの記述だけが持つ。ここが受け持つのは「根拠に使うな」までである。
_CAUTION = (
    "---\n"
    "上記は検索結果です。質問に関連しない場合は根拠に使わず、"
    "下記のとおり扱ってください（距離が閾値内であることは"
    "関連性を保証しません）。資料に書かれていない主体を補わないでください。\n"
    + _FALLBACK
)

# 社内資料のDBが空のとき。取り込み忘れを「関連する記述が無い」と取り違えさせない。
# 取り込みを忘れている利用者は、質問を言い換え続けても永遠に0件を得る（設計書7節）。
_EMPTY_STORE = (
    "ベクトルDBが空です。取り込みが未実行の可能性があります。"
    "python -m scripts.ingest_source を実行してください。"
)

# 技術ドキュメントの出口。社内資料の _FALLBACK と同じ型で、一般知識で答えて
# よいことを先に言い、補う探索を名指しで止める（_FALLBACK の 2026-09-09 の
# ウェブ検索ループと同じ理由）。名指しする相手に context7 を加えるのは、この
# ツールが検索語を社外に出さないために context7 を置き換えるものだからである。
#
# 社内固有の事項の歯止めは持たない。ライブラリの質問に「社内固有の事項は推測
# しない」を付けると、社内の話として扱われて一般知識の線引きが狂う。代わりに、
# 版によって変わる記述（引数名・既定値・非推奨）を推測と断らせる。収録している
# のは特定の版の公式ドキュメントであり、モデルの知識は別の版のものでありうる。
_DOCS_FALLBACK = (
    "context7・ウェブ検索・他のツールで補おうとせず、次のとおり回答してください。"
    "一般的な知識で答えて構いませんが、技術ドキュメントDBでは確認できなかった"
    "旨を断ってください。"
    "バージョンによって変わる記述（引数名・既定値・非推奨かどうかなど）は、"
    "推測であることを明示してください。"
)

_DOCS_NO_HITS = (
    "技術ドキュメントを検索しましたが、この質問に関連する記述は見つかりませんでした。\n"
    + _DOCS_FALLBACK
)

_DOCS_CAUTION = (
    "---\n"
    "上記は検索結果です。質問に関連しない場合は根拠に使わず、"
    "下記のとおり扱ってください。\n"
    + _DOCS_FALLBACK
)

# 技術DBが空のとき。fetch_docs は示さない。オフラインの GB10 では走らせられず、
# docs_source/ は git で追跡済みなので取得し直す必要も無い。
_DOCS_EMPTY_STORE = (
    "技術ドキュメントのDBが空です。取り込みが未実行の可能性があります。"
    f"{store.DOCS_INGEST_SOURCE_COMMAND} を実行してください。"
)


# 日本語の検索語を見分ける。ひらがな・カタカナ・CJK統合漢字（拡張Aを含む）・
# 半角カタカナ。全角スペース・句読点（U+3000-303F）と全角英数・記号
# （U+FF01-FF5E）と、カタカナの中黒「・」（U+30FB。IME が / キーで出す）は
# 含めない。IME で「？」や「・」が混ざった英語の検索語まではねると、モデルは
# 同じ語で呼び直し続ける。
_JAPANESE = re.compile("[\u3040-\u309f\u30a0-\u30fa\u30fc-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")

# 日本語の検索語をはね返すときの文言。0件の文言（_DOCS_NO_HITS）とは混ぜない。
# あちらは「再検索しない」と言うので、混ぜるとモデルは呼び直さなくなる。
_ASK_FOR_ENGLISH = (
    "検索していません。技術ドキュメントは英語で書かれているため、日本語を含む"
    "検索語では当たりません。検索語を英語の疑問文に書き直して、もう一度 "
    "search_tech_docs を呼んでください（例: How do I cache data in Streamlit?）。"
)


def _tech_docs_description(sources: list) -> str:
    """search_tech_docs の説明文を組む（設計書4.3〜4.5）。

    収録ライブラリは docs_sources.toml（唯一の設定）から並べる。DBを開いて
    調べないのは、開く操作が書き込みを伴うため（_get_store の説明）。
    version が "0.0.0" のものは版の記載が無い資料なので、名前だけを出す。
    """
    libraries = ", ".join(
        source.name if source.version == "0.0.0" else f"{source.name} {source.version}"
        for source in sources
    )
    return (
        "OSS ライブラリ・言語仕様の公式ドキュメント（英語）を検索し、根拠となる"
        "原文を出典つきで返す。社内資料はこのツールではなく search_documents で"
        "検索する。\n\n"
        f"収録しているライブラリ: {libraries}\n"
        "この一覧に無いライブラリについては呼ばなくてよい。\n\n"
        "query は英語の疑問文で書く。"
        "良い例: How do I cache data in Streamlit?　"
        "悪い例: Streamlit キャッシュ（日本語）、Write a caching function（依頼文。"
        "関連度の判定で落ちる）。\n"
        "日本語を含む query は検索せずに、英語での呼び直しを求める文を返す。"
    )


@dataclass(frozen=True)
class _Corpus:
    """コーパスごとに違うもの。検索の本体（_search）はこれだけを見て分岐する。

    DBを開く関数はここに持たせない。テストが rag_mcp_server._open を差し替える
    ため、import 時に関数を束縛すると差し替えが効かなくなる。開く関数は
    ツールの入口が呼び出し時にモジュールから引いて渡す。
    """

    label: str
    rerank_floor: float | None
    no_hits: str
    caution: str
    empty_store: str


_INTERNAL = _Corpus(
    label="社内資料",
    rerank_floor=None,
    no_hits=_NO_HITS,
    caution=_CAUTION,
    empty_store=_EMPTY_STORE,
)

# 床を効かせるのは技術ドキュメントだけである。距離が圏内・圏外を分けないため、
# 採否は床が決める（ingest/retrieval.py の DOCS_RERANK_FLOOR の実測）。
_TECH_DOCS = _Corpus(
    label="技術ドキュメント",
    rerank_floor=DOCS_RERANK_FLOOR,
    no_hits=_DOCS_NO_HITS,
    caution=_DOCS_CAUTION,
    empty_store=_DOCS_EMPTY_STORE,
)


def format_results(hits: list, corpus: _Corpus = _INTERNAL) -> str:
    """Hit の並びを Markdown に組む。

    出典は all_citations() で全件並べる。画面表示に使う citation は
    「ほか6資料」と丸めるが、エージェントは出典ファイルを直接開くため、
    丸めるとその経路が塞がる（設計書4.5節）。
    """
    if not hits:
        return corpus.no_hits

    blocks = [f"{corpus.label}から {len(hits)} 件が見つかりました。"]
    for number, hit in enumerate(hits, start=1):
        # スコア部分だけを取る。出典は下で全件並べるため重複させない。
        # 右から刻む。caption は「出典 ／ 距離 ／ BM25 ／ Reranker」の4欄で
        # 固定だが、左から1回で切ると、出典（ファイル名）自身が " ／ " を
        # 含んだときに見出しへその後半が紛れ込む。
        scores = " ／ ".join(format_hit_caption(hit).rsplit(" ／ ", 3)[1:])
        citations = "\n".join(f"- {one}" for one in hit.all_citations())
        blocks.append(f"## [{number}] {scores}\n出典:\n{citations}\n\n{hit.text}")
    blocks.append(corpus.caution)
    return "\n\n".join(blocks)


@dataclass
class _State:
    """1つのコーパスについて、プロセスの生存期間だけ持つ状態。

    サーバは GB10 の rag-mcp.service として常駐し、Claude Code から引かれる。
    常駐する理由はDBを開くのが重いからではない（実測0.40ms）。BM25索引の
    構築が57msかかり、エージェントは1タスク中に何度も検索するためである
    （2026-09-08、570出現・本文512種の実測）。

    コーパスごとに1つ持つ（_state と _docs_state）。片方の取り込みで
    revision が変わっても、もう片方の索引は組み直さない。リランカーの準備
    状況はモデルが共通なので、ここではなくプロセスで1つ（_reranker_ready）。
    """

    store: object | None = None
    index: object | None = None
    index_revision: int | None = None


def _get_index(state: _State, collection):
    """BM25索引を返す。revision が変わっていれば組み直す。

    revision は replace_source() の書き込みトランザクションの内側で加算される
    ため、これだけを見れば鮮度を判定できる。読むコストは0.02msで、57msの
    組み直しを避けられるかの判定には十分に軽い。
    """
    revision = collection.revision()
    if state.index is None or revision != state.index_revision:
        state.index = build_index(collection)
        state.index_revision = revision
    return state.index


mcp = MCPServer("local_docs")
_state = _State()
_docs_state = _State()
_reranker_ready = False

# 検索を直列化する。stdio では Codex が単一クライアントであり、リクエストは
# 直列に届くので競合しなかった。--http で複数PCから引くとその前提が消える。
#
# 守る相手は _state だけではない。リランカーの遅延初期化も無防備である。
# VectorStore 自身の読み書き（_current() の entries と matrix の入れ替えを
# 含む）は 2026-10-24 から VectorStore._lock が直列化しているが、検索1回は
# VectorStore の呼び出しを何度もまたぐ。その間に索引が入れ替わる組み合わせ
# までは VectorStore 側では塞げない。
#
# 全部まとめて塞げるのが、ツールの入口で直列化する理由である。代償は同時アクセスが順番待ちになること。検索1回は約3.5秒（大半が
# リランカー、2026-09-08 の実測）なので、3人が同時に叩けば最後の1人は
# 約10秒待つ。少人数を前提にした割り切りであり、常時の同時利用が増えるなら
# 直列化ではなく読み取り側の作り直しが要る。
_search_lock = threading.Lock()


def _open():
    """DBを開く。

    パスは store.DB_PATH（rag_web/・scripts/ingest_source.py と共通）を
    使う。引数や環境変数で受け取らないのは、3か所で食い違うと open_store が
    例外を出さずに空のDBを新規作成し、検索が黙って全部空になる状態が
    生まれるためである（ingest/store.py のコメント参照）。
    """
    return store.open_store(str(store.DB_PATH))


def _open_docs():
    """技術ドキュメントのDBを開く。パスは store.DOCS_DB_PATH だけを使う（_open と同じ理由）。"""
    return store.open_store(str(store.DOCS_DB_PATH))


def _get_store(state: _State, opener: Callable[[], object]):
    """ストアを返す。開くのはプロセスの生存期間で1度だけ。

    リクエストごとに開き直してはならない。open_store() は読み取りではなく、
    VectorStore.__init__（ingest/vector_store.py:146-150）が
    executescript(_SCHEMA) と commit() を実行する。つまり開くたびにSQLiteの
    書き込みロックを取りにいくため、取り込みの書き込みと重なると busy_timeout
    ぶん待たされ、"database is locked" で終わる。開きっぱなしにするのは
    rag_web/corpora.py のコーパスごとのキャッシュと同じ形である。

    これで消えるのは「毎リクエスト」であって「必ず」ではない。最初の検索は
    今もそのスキーマ確認の書き込みを1回行うので、サーバ起動後の最初の検索が
    たまたま取り込みの書き込みと重なれば、その1回はやはり待たされる。
    """
    if state.store is None:
        state.store = opener()
    return state.store


def _ensure_reranker():
    """初回検索時にだけリランカーを用意する。

    起動時にロードしない。Codex はセッション開始時にサーバを起こすため、
    検索を1度もしないセッションにモデルの取得と読み込みを払わせないためである
    （設計書4.4節は3.3秒・約570MBと見積もっている）。

    代償は初回検索が遅いこと。同一クエリ5回の実測（2026-09-08、i5-1240P、
    570出現・本文512種）で 6.99秒 → 3.55 / 3.41 / 3.48 / 3.53秒。初回の
    上乗せ約3.5秒のうち、この check_reranker() 自体は1.18秒（HuggingFace Hub
    へのHEAD6回）で、残りは初回 rerank() のONNXセッション構築である。

    取得に失敗しても検索は続ける。既存の劣化運転方針に揃える（設計書7節）。

    モデルは2つのコーパスで同じなので、確かめるのはプロセスで1回である
    （_reranker_ready はコーパスごとの _State ではなくモジュールに持つ）。
    """
    global _reranker_ready
    if _reranker_ready:
        return reranker.rerank
    try:
        reranker.check_reranker()
    except reranker.RerankError:
        return None
    _reranker_ready = True
    return reranker.rerank


@mcp.tool()
def search_documents(query: str, n_results: int = SEARCH_RESULT_COUNT) -> str:
    """社内資料を検索し、根拠となる原文を出典つきで返す。

    Args:
        query: 検索したい内容。自然文で書く。
        n_results: 返すチャンク数。
    """
    with _search_lock:
        return _search(_INTERNAL, _state, _open, query, n_results)


# 説明文は起動時に1回だけ組む。docs_sources.toml が読めなければ例外のまま
# 起動を止める。握りつぶして一覧の無い説明文で起動すると、モデルは収録範囲が
# 分からないまま呼び続ける（rag-mcp.service の journalctl に例外が残る）。
@mcp.tool(description=_tech_docs_description(load_sources()))
def search_tech_docs(query: str, n_results: int = SEARCH_RESULT_COUNT) -> str:
    """OSS ライブラリ・言語仕様の公式ドキュメントを検索し、原文を出典つきで返す。

    モデルに見せる説明文は docstring ではなく _tech_docs_description が組む。

    Args:
        query: 英語の疑問文で書く。
        n_results: 返すチャンク数。
    """
    # ロックを取る前にはね返す。検索しない応答を、他の人の検索の列に並べない。
    if _JAPANESE.search(query):
        return _ASK_FOR_ENGLISH
    with _search_lock:
        return _search(_TECH_DOCS, _docs_state, _open_docs, query, n_results)


def _search(
    corpus: _Corpus,
    state: _State,
    opener: Callable[[], object],
    query: str,
    n_results: int,
) -> str:
    """検索の本体。_search_lock を握った状態で呼ばれる。"""
    try:
        collection = _get_store(state, opener)
        if collection.count() == 0:
            # 「関連する記述が無い」と取り違えさせない（_EMPTY_STORE の説明）。
            return corpus.empty_store
        index = _get_index(state, collection)
        hits = search(
            collection,
            query,
            index=index,
            n_results=n_results,
            rerank=_ensure_reranker(),
            rerank_floor=corpus.rerank_floor,
        )
    except (EmbeddingError, store.EmbeddingMismatchError) as error:
        # EmbeddingMismatchError は DB と設定の埋め込みモデルの食い違い。
        # 開く時点（_get_store）で出るので、ここで文言を返せば利用者に届く。
        # 文言をそのまま返す。embedder は「VLLM_EMBED_MODEL を確かめて
        # ください」のように、利用者が次に何をすればよいかを書いている。
        #
        # 拾うのはこの1種類だけにする。広く Exception を拾っていた版は
        # 「サーバを落とさない」ことを理由にしていたが、その心配は要らない。
        # インストール済み mcp 2.2.0 のソースでは、ツール関数が投げた例外は
        # mcp/server/mcpserver/tools/base.py:208-210 の
        # `except Exception as exc: raise UnexpectedToolError(...) from exc`
        # を通り、mcp/server/mcpserver/server.py:447 で is_error=True の
        # CallToolResult になる。ただし**そこに載るのは元の例外文ではない**。
        # 定型文 "Error executing tool search_documents" だけであり、
        # mcp/server/mcpserver/exceptions.py:66-67 が「nothing from the original
        # reaches the client」と明記している（実測ではなく、上記のソースを
        # 読んで確認）。
        # それでも狭めるのは、詳細が失われる代償を払ってでも、バグが正常な
        # 検索結果と見分けのつく形で届くほうが良いからである。広い except では
        # TypeError が「0件でした」や「vLLMが落ちています」と同じ顔をして
        # エージェントに届く。
        #
        # RerankError はここへ来ない。check_reranker の失敗は _ensure_reranker
        # が、rerank の失敗は retrieval._reranked が、それぞれ手前で捕まえる。
        return str(error)
    return format_results(hits, corpus)


def main(argv=None) -> None:
    """引数を読んでサーバを起動する。

    既定は stdio のままにする。Codex が子プロセスとして起こす1台構成が
    現状の標準であり、既定を http に倒すとその構成が黙って壊れる。
    Codex は標準入出力で話しかけて応答を得られず、「サーバが応答しない」と
    しか分からない形で失敗する。

    --http は別のPCから引くための構成である。SDK の既定 host は 127.0.0.1 で
    他のPCから届かないため、--host で明示的に開ける必要がある。
    """
    parser = argparse.ArgumentParser(description="社内資料を引く MCP サーバ")
    parser.add_argument(
        "--http",
        action="store_true",
        help="stdio ではなく streamable-http で待ち受ける（別PCから引く構成）",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="--http のときの bind 先。既定は自機のみ。LANへ出すなら 0.0.0.0",
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="--http のときの待ち受けポート"
    )
    args = parser.parse_args(argv)

    # stdout は stdio の MCP プロトコルが使うため、メッセージは stderr へ出す。
    # .env はここでは読まない（Codex / Claude Code が渡した環境変数だけを見る）。
    try:
        reject_legacy_env()
        load_vllm_settings()
    except ValueError as error:
        print(f"エラー: {error}", file=sys.stderr)
        raise SystemExit(2) from error

    if not args.http:
        mcp.run()
        return
    mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
