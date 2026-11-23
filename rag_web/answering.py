"""チャット1回分の処理を、画面に依存しないイベント列として返す。

検索・条件抽出・生成の順序と分岐、利用者に見せる文言をここに集め、描画は
イベントとして返す。イベントの送り出し（NDJSON）は呼び出し側の仕事である。

イベントは次の5種類で、最後は必ず done か error のどちらか1つになる。
    {"type": "note", "text": str}
    {"type": "delta", "text": str}
    {"type": "evidence", "hits": [hit_view(...)], "table": str | None}
    {"type": "error", "message": str}
    {"type": "done", "display": str | None}
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from ingest import answer_text, catalog, conditions, display_mode, query_translation
from ingest.chat import ChatError
from ingest.embedder import EmbeddingError
from ingest.prompting import (
    build_catalog_prompt,
    build_docs_prompt,
    build_prompt,
    format_hit_caption,
)
from ingest.retrieval import DOCS_RERANK_FLOOR, contextual_query

from .corpora import CorpusCache
from .services import Services

logger = logging.getLogger(__name__)

EXTRACTION_FAILED_NOTE = "条件を解釈できませんでした。通常の検索で回答します。"


@dataclass(frozen=True)
class ChatRequest:
    corpus: str  # "internal" | "docs"
    model: str
    temperature: float
    question: str
    # [{"role": "user"|"assistant", "content": str}]。今回の質問は含まない。
    history: list[dict]


def system_prompt(today: date) -> str:
    # 日付を引数で受けるのは、常駐するサーバーでは起動時の日付が翌日以降も
    # 残ってしまうため（リクエストごとに今日の日付で作り直す）。
    return (
        f"あなたは有能なアシスタントです。今日の日付は{today:%Y年%m月%d日}です。\n"
        "日本語で回答して下さい。"
    )


def hit_view(hit) -> dict:
    """根拠1件を画面へ渡す形にする。"""
    return {
        "caption": format_hit_caption(hit),
        "others": hit.all_citations()[1:],
        "text": hit.text,
    }


def answer_events(
    request: ChatRequest, services: Services, corpora: CorpusCache, today: date
) -> Iterator[dict]:
    """質問1つに答えるイベント列。想定外の例外も error イベントで終わらせる。

    ストリーミングの途中で例外が生のまま抜けると、受け手は応答が途切れた理由を
    知る手段を失う。ログには詳細を残し、画面には短いメッセージを返す。
    """
    try:
        yield from _answer_events(request, services, corpora, today)
    except Exception as error:
        logger.exception("回答の生成中に想定外のエラーが発生しました")
        yield {"type": "error", "message": f"回答を生成できませんでした: {error}"}


def _answer_events(
    request: ChatRequest, services: Services, corpora: CorpusCache, today: date
) -> Iterator[dict]:
    question = request.question
    searching_docs = request.corpus == "docs"

    def ask(prompt: str) -> str:
        return services.ask_json(request.model, prompt)

    # 「マークダウンで表示して」と頼まれたら、描画せず記法のまま出す。
    # 判定は質問1つごとに閉じる（ingest/display_mode.py）。前のターンの指定を
    # 持ち越すと、利用者が何も言っていないのにコードブロックで返り続ける。
    display = display_mode.detect(question)

    collection = corpora.collection(request.corpus)
    # 属性一覧は社内資料でしか使わない（技術ドキュメントでは条件抽出をしない）。
    schema = None if searching_docs else corpora.schema(request.corpus)

    # 技術ドキュメントでは条件抽出を走らせない。型番の絞り込みは社内の
    # 製品仕様書に固有の仕組みであり、ここではLLM呼び出しが1回無駄に増えるだけ。
    extraction = (
        conditions.Extraction()
        if searching_docs
        else conditions.extract(question, schema, ask)
    )

    table = None
    hits = []
    user_content = None
    # ベクトル検索は埋め込みAPIを呼ぶ。Ollamaが止まっていればここで
    # EmbeddingError になるため、生成時（下のChatError）と同じ見せ方に揃える。
    try:
        if searching_docs:
            # 検索には直前の質問を継ぎ足す（追質問は単独では引けない）。社内資料側の
            # 検索経路（下の else 節）と同じ判断である。
            query = contextual_query(question, request.history)
            # 技術ドキュメントは英語、質問は日本語のことが多い。継ぎ足した文字列
            # ごと英語の検索クエリへ翻訳する。生成は原文の question のまま行う
            # （build_docs_prompt）。詳細は ingest/query_translation.py 参照。
            query = query_translation.translate_query(query, ask)
            # 技術ドキュメントだけ、1件ごとの採否をリランカーのスコアで決める。
            # このコーパスでは距離のしきい値が関門にならない（実測は
            # ingest/retrieval.py の DOCS_RERANK_FLOOR）。社内資料側には渡さない。
            hits = services.search(
                collection,
                query,
                index=corpora.index(request.corpus),
                rerank=corpora.rerank_callable(),
                rerank_floor=DOCS_RERANK_FLOOR,
            )
            user_content = build_docs_prompt(question, hits)
        elif extraction.conditions:
            # 「最大の洗濯容量は」に答えるための並べ替え。最大・最小を尋ねる語が
            # 無ければLLMは呼ばれない（ingest/conditions.py の _SUPERLATIVES）。
            ranking = conditions.extract_ranking(question, schema, ask)
            matched = catalog.select(collection, extraction.conditions)
            relaxed = catalog.relaxations(collection, extraction.conditions)
            # 条件から外れるが上位の機種は画面にだけ出す。モデルに渡すと、設置
            # できない機種を答えとして挙げる（実測8回中5回）。
            beyond = catalog.exceeding(collection, extraction.conditions, ranking, matched)
            prompt_table = catalog.format_table(
                extraction.conditions, matched, relaxed, ranking
            )
            # 画面（絞り込んだ一覧）には beyond も含めて見せる。
            table = catalog.format_table(
                extraction.conditions, matched, relaxed, ranking, beyond
            )
            user_content = build_catalog_prompt(question, prompt_table)
        else:
            # 検索には直前の質問を継ぎ足す（追質問は単独では引けない）。
            # モデルへ渡す質問は生のままにする。会話履歴は messages で渡しており、
            # 継ぎ足した文字列まで質問として見せると同じ問いが二重になる。
            query = contextual_query(question, request.history)
            hits = services.search(
                collection,
                query,
                index=corpora.index(request.corpus),
                rerank=corpora.rerank_callable(),
            )
            user_content = build_prompt(question, hits)
    except EmbeddingError as error:
        # 条件抽出の失敗は伝えない。Ollamaが止まっていれば条件抽出も検索も同じ
        # 理由で失敗し、「通常の検索で回答します」と告げた直後に検索が落ちるため。
        yield {"type": "error", "message": f"検索できませんでした: {error}"}
        return

    if extraction.failed:
        yield {"type": "note", "text": EXTRACTION_FAILED_NOTE}

    messages = (
        [{"role": "system", "content": system_prompt(today)}]
        + [{"role": m["role"], "content": m["content"]} for m in request.history]
        + [{"role": "user", "content": user_content}]
    )
    try:
        # 「答え：」の言い直しはラベルだけ落とす（ingest/answer_text.py）。
        # 口癖であって記法ではないので、生表示でも落とす。
        stream = answer_text.without_label(
            services.stream_chat(request.model, messages, request.temperature)
        )
        if not display:
            # 表のセル内の <br>・<ul>・<li> は、描画する場合にだけ落とす。
            stream = answer_text.strip_html_tags_stream(stream)
        for chunk in stream:
            yield {"type": "delta", "text": chunk}
    except ChatError as error:
        yield {"type": "error", "message": f"回答を生成できませんでした: {error}"}
        return

    yield {
        "type": "evidence",
        "hits": [hit_view(hit) for hit in hits],
        "table": table,
    }
    yield {"type": "done", "display": display}
