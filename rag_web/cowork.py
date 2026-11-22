"""Cowork（雛形からの文書生成・雛形なしの生成）を、画面に依存しない関数にする。

rag_chat_app.py の _collect_evidence / _generate_document / _generate_freeform と
生成ブロックの Cowork 分岐を移したもの。順序・分岐・利用者に見せる文言は元のまま
揃え、st.session_state.cowork_result へ積んでいた結果を戻り値の dict にした。
描き方（警告を畳む、空のヒットを出さない等）は呼び出し側の仕事である。

rag_web.app を import しない。app がこのモジュールを使うため、循環する。モデル名は
CoworkRequest.model で受け取る。
"""

import base64
import logging
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import docgen
from docgen import filling, freeform, markdown_document, source_code
from docgen import templates as docgen_templates
from ingest import query_translation
from ingest.chat import ChatError
from ingest.embedder import EmbeddingError
from ingest.retrieval import DOCS_RERANK_FLOOR

from .answering import hit_view
from .corpora import CorpusCache
from .services import Services, caption_image_or_reason

logger = logging.getLogger(__name__)

# 雛形を選ばない選択肢。雛形が0件でも画面が成立するよう、常に存在させる。
NO_TEMPLATE_LABEL = "（フォーマットファイルなし）"

# 貼り付けたテキストが「参照したファイル」に並ぶときの名前。ファイル名を持たない
# ため、こちらで付ける。画面のラベルと成果物の一覧で呼び方を揃えるために定数にする。
PASTED_TEXT_NAME = "貼り付けたテキスト"

CORPUS_INTERNAL = "社内資料"
CORPUS_DOCS = "技術ドキュメント"

NO_EVIDENCE_MESSAGE = "参照する資料も添付ファイルもありません。根拠が無いため生成しません。"


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes


@dataclass(frozen=True)
class CoworkRequest:
    model: str
    question: str
    template: str | None  # templates_dir 直下のファイル名。None は雛形なし
    output_suffix: str  # 雛形なしのときだけ使う
    use_internal: bool
    use_docs: bool
    pasted: str
    attachments: list[Attachment]


class _Stop(Exception):
    """利用者に伝えて生成を止める。message がそのままエラー結果になる。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def output_suffixes() -> list[str]:
    """雛形なしで選べる出力形式。文書の形式を先、ソースコードを後に並べる。"""
    return list(markdown_document.OUTPUT_SUFFIXES + source_code.OUTPUT_SUFFIXES)


def is_source_suffix(suffix: str) -> bool:
    return suffix.lower() in source_code.OUTPUT_SUFFIXES


def template_path(services: Services, name: str) -> Path:
    """登録済みの雛形を名前で引く。

    一覧から引くのは、利用者が送ってきた名前をパスとして組み立てないためである
    （templates/ の外を指す名前を通さない）。
    """
    for path in docgen_templates.templates(services.templates_dir):
        if path.name == name:
            return path
    raise FileNotFoundError(f"フォーマットファイルが見つかりません: {name}")


def _empty_result() -> dict:
    return {"errors": [], "warnings": [], "infos": [], "download": None, "sources": []}


def _error_result(message: str) -> dict:
    result = _empty_result()
    result["errors"].append(message)
    return result


def _has_no_evidence(request: CoworkRequest) -> bool:
    """根拠が1つも無いかを判定する。

    雛形あり・なしの両方の分岐が同じ条件で止める。判定を2箇所に書くと、根拠の
    種類を足すたびに片方だけ直して取り残す恐れがあるため1つにまとめる。
    """
    return (
        not request.use_internal
        and not request.use_docs
        and not request.attachments
        and not request.pasted.strip()
    )


def generate_cowork(
    request: CoworkRequest, services: Services, corpora: CorpusCache, today: date
) -> dict:
    """Cowork 1回分の結果を返す。想定外の例外もエラー結果にする（ジョブを失敗にしない）。

    返り値:
        {"errors": [str], "warnings": [str], "infos": [str],
         "download": {"file_name": str, "content_b64": str} | None,
         "sources": [{"kind": "社内資料"|"技術ドキュメント", "hits": [hit_view(...)]}]}
    """
    try:
        return _generate(request, services, corpora, today)
    except _Stop as stop:
        return _error_result(stop.message)
    except Exception as error:
        logger.exception("文書の生成中に想定外のエラーが発生しました")
        return _error_result(f"文書を生成できませんでした: {error}")


def _generate(
    request: CoworkRequest, services: Services, corpora: CorpusCache, today: date
) -> dict:
    if request.template is None:
        # 雛形なしを選んだ回。根拠が1つも無ければ、呼んでも中身の無い文書が出る
        # だけなので、その場合だけ止める。
        if _has_no_evidence(request):
            raise _Stop(NO_EVIDENCE_MESSAGE)
        return _generate_freeform(request, services, corpora, today)

    name = request.template
    try:
        # 生成待ちの間に雛形が消されることがある。例外ではなく結果で伝える。
        path = template_path(services, name)
        names = docgen.placeholders(path)
    except Exception as error:
        # templates.register は拡張子しか見ないため、中身が壊れたファイルも
        # 登録できてしまう（誤操作として起こりうる）。
        raise _Stop(f"{name} を開けませんでした: {error}") from error
    if not names:
        raise _Stop(f"{name} に {{{{印}}}} がありません。埋める欄が無いため生成しません。")
    if _has_no_evidence(request):
        # 根拠が1つも無ければ、呼んでも全欄が埋まらない。LLMを呼ぶ前に止める。
        raise _Stop(NO_EVIDENCE_MESSAGE)
    return _generate_document(path, names, request, services, corpora, today)


def _collect_evidence(
    request: CoworkRequest, services: Services, corpora: CorpusCache, result: dict
):
    """検索結果・添付・貼り付けたテキストの本文を集める。

    雛形あり・なしの両方がこれを呼ぶ。どちらか一方にだけ検索の修正が入る状態を
    作らないため1つにまとめてある。どのコーパスを引くかはここが決め、生成側へは
    (種類の名前, ヒット) の並びで渡す（生成側にコーパスの知識を持たせない）。
    """
    question = request.question
    sources = []
    # リランカーの確認は1回の生成で1度だけ行う。失敗は覚えず呼ぶたびに再確認する
    # ため、コーパスごとに呼ぶと確認の失敗を二重に払うことになる（Streamlit 版も
    # 1回の実行で1度だけ評価していた）。
    rerank = corpora.rerank_callable() if request.use_internal or request.use_docs else None
    try:
        if request.use_internal:
            sources.append((
                CORPUS_INTERNAL,
                services.search(
                    corpora.collection("internal"),
                    question,
                    index=corpora.index("internal"),
                    rerank=rerank,
                ),
            ))
        if request.use_docs:
            documents = corpora.collection("docs")
            if documents.count() == 0:
                # 取り込み前でも生成は止めない。社内資料と添付だけで埋める。
                result["infos"].append(
                    "技術ドキュメントが取り込まれていません。この検索は飛ばしました。"
                )
            else:
                # 英語のコーパスなので日本語のままでは当たらない。依頼文は訳しても
                # 依頼のまま届き、リランカーに全件切られるため、translate_query では
                # なく「調べるべき話題」を作らせる（query_translation.topic_query）。
                # 英訳は文書生成ではないので、文脈長は既定のまま呼ぶ。
                english = query_translation.topic_query(
                    question, lambda prompt: services.ask_json(request.model, prompt)
                )
                sources.append((
                    CORPUS_DOCS,
                    services.search(
                        documents,
                        english,
                        index=corpora.index("docs"),
                        rerank=rerank,
                        rerank_floor=DOCS_RERANK_FLOOR,
                    ),
                ))
    except (EmbeddingError, ChatError) as error:
        # 埋め込み（Ollama）も英訳（LLM）も、落ちたら伝えて止める。
        raise _Stop(str(error)) from error

    texts = []
    if request.attachments:
        # VLMが無いことは止める理由にしないが、caption_image を渡さずに parse を
        # 呼ぶと図やスクリーンショットに説明が付かない。理由は補足に残す。
        caption_image, reason = caption_image_or_reason(services)
        if reason:
            result["warnings"].append(reason)
        with tempfile.TemporaryDirectory() as workspace:
            for attachment in request.attachments:
                try:
                    path = _attachment_path(Path(workspace), attachment.name)
                    path.write_bytes(attachment.data)
                    # 添付はDBに入れない。取り出した本文をその場で使うだけである。
                    units = services.parse(path, caption_image=caption_image)
                except Exception as error:
                    # 拡張子だけでは中身の壊れたファイルを弾けない。
                    raise _Stop(f"{attachment.name} を開けませんでした: {error}") from error
                text = "\n".join(unit.text for unit in units)
                if not text:
                    # 空の見出しだけがプロンプトに載ると、利用者には何も伝わらない。
                    result["warnings"].append(
                        f"{attachment.name} から本文を取り出せませんでした。"
                    )
                texts.append((attachment.name, text))
    if request.pasted.strip():
        # 貼り付けたテキストは添付と同じ扱いにする。名前を付けるのは「参照した
        # ファイル」に出すためで、何を根拠にしたかの記録から抜けると辿れない。
        texts.append((PASTED_TEXT_NAME, request.pasted.strip()))
    return sources, texts


def _attachment_path(workspace: Path, name: str) -> Path:
    """添付を書く一時パス。

    名前はブラウザーから来る。Streamlit の UploadedFile と違い区切りや ".." を
    含みうるため、一時フォルダの外へ書かないよう、ファイル名でなければ拒む。
    """
    if name != Path(name).name or name in ("", ".", ".."):
        raise ValueError("ファイル名ではありません")
    return workspace / name


def _download(data: bytes, file_name: str) -> dict:
    return {"file_name": file_name, "content_b64": base64.b64encode(data).decode("ascii")}


def _source_views(sources) -> list[dict]:
    return [
        {"kind": kind, "hits": [hit_view(hit) for hit in hits]} for kind, hits in sources
    ]


def _generate_document(
    path: Path,
    names: list[str],
    request: CoworkRequest,
    services: Services,
    corpora: CorpusCache,
    today: date,
) -> dict:
    """雛形を埋める。"""
    result = _empty_result()

    def ask(prompt: str) -> str:
        return services.ask_json(
            request.model, prompt, num_ctx=filling.GENERATION_NUM_CTX
        )

    sources, texts = _collect_evidence(request, services, corpora, result)

    try:
        values = filling.fill_values(names, request.question, sources, texts, ask)
    except (filling.PromptTooLongError, ChatError) as error:
        raise _Stop(str(error)) from error

    undrawn = []
    try:
        data = docgen.fill(path, values, lambda name, reason: undrawn.append((name, reason)))
    except Exception as error:
        raise _Stop(f"{path.name} を開けませんでした: {error}") from error
    missing = [name for name in names if name not in values]
    if missing:
        result["warnings"].append(
            f"埋まらなかった欄: {'、'.join(missing)}（フォーマットファイルの {{{{印}}}} が残ります）"
        )
    if undrawn:
        # 図にできなかったことは開く前に伝える。黙って Mermaid のテキストが
        # 入っていると、利用者は成果物を開くまで気づけない。
        result["warnings"].append(
            "図にできなかった欄: "
            + "、".join(f"{name}（{reason}）" for name, reason in undrawn)
        )
    result["download"] = _download(
        data, f"{path.stem}_{today.isoformat()}{path.suffix}"
    )
    result["sources"] = _source_views(sources)
    return result


def _generate_freeform(
    request: CoworkRequest, services: Services, corpora: CorpusCache, today: date
) -> dict:
    """雛形なしで文書（またはソースコード）を作る。"""
    result = _empty_result()
    suffix = request.output_suffix

    def ask_text(prompt: str) -> str:
        return services.ask_text(
            request.model, prompt, num_ctx=filling.GENERATION_NUM_CTX
        )

    sources, texts = _collect_evidence(request, services, corpora, result)

    paths = [name for name, _ in texts]
    citations = [hit.citation for _, hits in sources for hit in hits]
    # ソースコードは Markdown の経路に乗せない。あちらは見出し・表・箇条書きへ
    # 解析してから組む作りで、コードを通すと `//` コメントが見出しに、`|` を
    # 含む行が表になる。
    is_source = is_source_suffix(suffix)

    try:
        written_text = (
            source_code.write_source(request.question, sources, texts, suffix, ask_text)
            if is_source
            else freeform.write_markdown(request.question, sources, texts, suffix, ask_text)
        )
    except (filling.PromptTooLongError, ChatError) as error:
        raise _Stop(str(error)) from error

    undrawn = []
    if is_source:
        data, warnings = source_code.build(written_text, paths, citations, suffix)
    else:
        blocks = markdown_document.parse(written_text)
        blocks.append(markdown_document.References(paths=paths, citations=citations))
        data, warnings = markdown_document.build(
            blocks, suffix, lambda name, reason: undrawn.append((name, reason))
        )
    result["warnings"].extend(warnings)
    if undrawn:
        result["warnings"].append(
            "図にできなかった箇所: "
            + "、".join(f"{name}（{reason}）" for name, reason in undrawn)
        )
    # 雛形が無いと元にする名前が無いので、固定の名前に日付を付ける。
    result["download"] = _download(data, f"文書_{today.isoformat()}{suffix}")
    result["sources"] = _source_views(sources)
    return result
