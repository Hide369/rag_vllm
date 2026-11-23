"""rag_web/cowork.py（雛形からの文書生成・雛形なしの生成）。

Ollama・VLM・リランカーには触れない。DB は tmp_path の本物の sqlite で、
開いた接続はフィクスチャの後始末で閉じる（-W error で ResourceWarning を
落とさないため）。
"""
import base64
import json
import logging
from dataclasses import replace
from datetime import date

import pytest

from docgen import filling, markdown_document, source_code
from ingest import store, vector_store
from ingest.chat import ChatError
from ingest.embedder import EMBED_DIM, EmbeddingError
from ingest.models import Chunk, ParsedUnit
from ingest.reranker import RerankError
from ingest.retrieval import DOCS_RERANK_FLOOR, Hit
from ingest.vlm import VlmError
from rag_web import cowork
from rag_web.answering import hit_view
from rag_web.cowork import (
    PASTED_TEXT_NAME,
    Attachment,
    CoworkRequest,
    generate_cowork,
    is_source_suffix,
    output_suffixes,
    template_path,
)
from rag_web.corpora import CorpusCache
from rag_web.services import Services

TODAY = date(2026, 11, 22)
NO_EVIDENCE = "参照する資料も添付ファイルもありません。根拠が無いため生成しません。"


def _hit(text="第5回 AI活用検討会を開催した。", source="議事録.docx"):
    return Hit(
        text=text,
        distance=0.1,
        occurrences=[{"source": source, "location_type": "page", "location": 1}],
    )


def _write_one(collection, source="go-spec.md"):
    chunk = Chunk(
        id=f"{source}::page1::0",
        text="goroutines",
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
    """外部 I/O の偽物。呼び出しを記録する。"""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.templates_dir = tmp_path / "templates"
        self.hits = [_hit()]
        self.ask_json_reply = json.dumps(
            {"会議名": "第5回 AI活用検討会", "決定事項": "継続する"}, ensure_ascii=False
        )
        self.ask_text_reply = "# 文書\n\n本文\n"
        self.parse_result = [ParsedUnit(text="添付の本文", location_type="page", location=1)]
        self.parse_error = None
        self.search_error = None
        self.vlm_error = None
        self.ask_json_calls = []
        self.ask_text_calls = []
        self.search_calls = []
        self.parse_calls = []
        self.opened = []

    def ask_json(self, model, prompt):
        self.ask_json_calls.append((model, prompt))
        if prompt.startswith("あなたは技術ドキュメント検索のためのクエリ作成者です。"):
            return json.dumps({"query": "goroutines"})
        return self.ask_json_reply

    def ask_text(self, model, prompt):
        self.ask_text_calls.append((model, prompt))
        return self.ask_text_reply

    def search(self, collection, query, **kwargs):
        self.search_calls.append((collection, query, kwargs))
        if self.search_error:
            raise self.search_error
        return self.hits

    def parse(self, path, caption_image=None):
        self.parse_calls.append((path.name, path.read_bytes(), caption_image))
        if self.parse_error:
            raise self.parse_error
        return self.parse_result

    def check_vlm(self):
        if self.vlm_error:
            raise self.vlm_error

    def caption_image(self, *args, **kwargs):
        return "説明"

    def open_store(self, path):
        collection = vector_store.open_store(path)
        self.opened.append(collection)
        return collection

    def close_stores(self):
        # 開いた接続を閉じる（GC 時の ResourceWarning を避ける）。
        for collection in self.opened:
            collection.close()

    def services(self):
        return Services(
            ask_json=self.ask_json,
            ask_text=self.ask_text,
            search=self.search,
            parse=self.parse,
            check_vlm=self.check_vlm,
            caption_image=self.caption_image,
            open_store=self.open_store,
            build_index=lambda collection: "index",
            check_reranker=lambda: None,
            rerank=self.rerank,
            templates_dir=self.templates_dir,
            db_paths={
                "internal": str(self.tmp_path / "internal.sqlite3"),
                "docs": str(self.tmp_path / "docs.sqlite3"),
            },
        )

    @staticmethod
    def rerank(query, texts):
        return [9.0] * len(texts)


@pytest.fixture
def recorder(tmp_path):
    recorder = Recorder(tmp_path)
    yield recorder
    recorder.close_stores()


@pytest.fixture
def services(recorder):
    return recorder.services()


@pytest.fixture
def corpora(services):
    return CorpusCache(services)


def _template(recorder, name="議事録.md", text="# {{会議名}}\n\n{{決定事項}}\n"):
    recorder.templates_dir.mkdir(parents=True, exist_ok=True)
    (recorder.templates_dir / name).write_text(text, encoding="utf-8")
    return name


def _request(**overrides):
    values = {
        "model": "m",
        "question": "第5回会議の議事録を作って",
        "template": None,
        "output_suffix": ".md",
        "use_internal": True,
        "use_docs": False,
        "pasted": "",
        "attachments": [],
    }
    values.update(overrides)
    return CoworkRequest(**values)


def _run(request, services, corpora):
    return generate_cowork(request, services, corpora, TODAY)


def _downloaded(result):
    return base64.b64decode(result["download"]["content_b64"])


def test_template_fills_and_offers_download(recorder, services, corpora):
    name = _template(recorder)
    result = _run(_request(template=name), services, corpora)
    assert result["errors"] == []
    assert result["download"]["file_name"] == "議事録_2026-11-22.md"
    text = _downloaded(result).decode("utf-8")
    assert "第5回 AI活用検討会" in text
    assert "継続する" in text
    assert recorder.ask_json_calls[0][0] == "m"


def test_unfilled_marks_are_listed_as_warning(recorder, services, corpora):
    name = _template(recorder)
    recorder.ask_json_reply = json.dumps({"会議名": "第5回"}, ensure_ascii=False)
    result = _run(_request(template=name), services, corpora)
    assert result["warnings"] == [
        "埋まらなかった欄: 決定事項（フォーマットファイルの {{印}} が残ります）"
    ]
    assert result["download"] is not None


def test_generation_does_not_cap_output_tokens(recorder, services, corpora):
    """出力の上限を指定しない。思考も上限を食うため、決め打ちすると本文が途中で切れる。
    recorder の ask_json / ask_text は (model, prompt) しか受けないので、上限などの
    追加引数を渡すと TypeError になる。"""
    name = _template(recorder)
    _run(_request(template=name), services, corpora)
    _run(_request(), services, corpora)
    assert [call[0] for call in recorder.ask_json_calls] == ["m"]
    assert [call[0] for call in recorder.ask_text_calls] == ["m"]


def test_docs_not_searched_unless_requested(recorder, services, corpora):
    _write_one(corpora.collection("docs"))
    _run(_request(use_docs=False), services, corpora)
    assert len(recorder.search_calls) == 1
    assert recorder.ask_json_calls == []  # topic_query も呼ばれない


def test_docs_searched_with_topic_query_and_floor(recorder, services, corpora):
    _write_one(corpora.collection("docs"))
    result = _run(_request(use_internal=False, use_docs=True), services, corpora)
    assert result["errors"] == []
    collection, query, kwargs = recorder.search_calls[0]
    assert collection is corpora.collection("docs")
    assert query == "goroutines"
    assert kwargs["rerank_floor"] == DOCS_RERANK_FLOOR
    assert kwargs["rerank"] is services.rerank
    assert [source["kind"] for source in result["sources"]] == ["技術ドキュメント"]


def test_reranker_is_checked_once_per_generation_for_both_corpora(recorder, services):
    checks = []

    def failing_check():
        checks.append(1)
        raise RerankError("モデルが無い")

    failing = replace(services, check_reranker=failing_check)
    corpora = CorpusCache(failing)
    _write_one(corpora.collection("docs"))
    result = _run(_request(use_internal=True, use_docs=True), failing, corpora)
    assert result["errors"] == []
    assert len(recorder.search_calls) == 2
    assert len(checks) == 1
    assert all(call[2]["rerank"] is None for call in recorder.search_calls)


def test_empty_docs_corpus_is_skipped_with_info(recorder, services, corpora):
    result = _run(_request(use_docs=True), services, corpora)
    assert result["infos"] == ["技術ドキュメントが取り込まれていません。この検索は飛ばしました。"]
    assert len(recorder.search_calls) == 1
    assert result["download"] is not None


def test_empty_docs_search_adds_no_note(recorder, services, corpora):
    """技術ドキュメントを引いて0件でも、補足には何も出さない。

    以前は0件になった理由を切り分ける3行（使ったクエリ・床を外したときの件数・最も
    近かった出典）を出していたが、原因が分かって役目を終えた。何を根拠にしたかは成果物
    末尾の「参照したファイル」から読める。
    """
    _write_one(corpora.collection("docs"))
    recorder.hits = []
    result = _run(_request(use_internal=False, use_docs=True), services, corpora)
    assert result["errors"] == []
    assert result["infos"] == []
    assert result["warnings"] == []
    assert result["download"] is not None
    assert [source["hits"] for source in result["sources"]] == [[]]


def test_broken_template_reports_error(recorder, services, corpora, monkeypatch):
    name = _template(recorder, name="x.md")

    def broken(path):
        raise ValueError("壊れています")

    monkeypatch.setattr(cowork.docgen, "placeholders", broken)
    result = _run(_request(template=name), services, corpora)
    assert result["errors"][0].startswith("x.md を開けませんでした: ")
    assert result["download"] is None
    assert recorder.ask_json_calls == []


def test_missing_template_reports_error(recorder, services, corpora):
    _template(recorder)
    result = _run(_request(template="消えた.md"), services, corpora)
    assert result["errors"][0].startswith("消えた.md を開けませんでした: ")
    assert result["download"] is None


def test_template_without_marks_stops(recorder, services, corpora):
    name = _template(recorder, name="空.md", text="印の無い雛形\n")
    result = _run(_request(template=name), services, corpora)
    assert result["errors"] == [
        "空.md に {{印}} がありません。埋める欄が無いため生成しません。"
    ]
    assert recorder.ask_json_calls == []


@pytest.mark.parametrize("with_template", [True, False])
def test_no_evidence_stops_without_calling_llm(recorder, services, corpora, with_template):
    template = _template(recorder) if with_template else None
    result = _run(
        _request(template=template, use_internal=False, use_docs=False),
        services,
        corpora,
    )
    assert result["errors"] == [NO_EVIDENCE]
    assert result["download"] is None
    assert recorder.ask_json_calls == []
    assert recorder.ask_text_calls == []
    assert recorder.search_calls == []


def test_whitespace_only_paste_is_not_evidence(recorder, services, corpora):
    result = _run(
        _request(use_internal=False, pasted=" \n\t "), services, corpora
    )
    assert result["errors"] == [NO_EVIDENCE]


def test_pasted_text_alone_is_enough_and_named_in_references(recorder, services, corpora):
    result = _run(
        _request(use_internal=False, pasted="  貼った内容  "), services, corpora
    )
    assert result["errors"] == []
    assert PASTED_TEXT_NAME in _downloaded(result).decode("utf-8")
    assert "貼った内容" in recorder.ask_text_calls[0][1]
    assert recorder.search_calls == []


def test_broken_attachment_stops_with_error(recorder, services, corpora):
    recorder.parse_error = ValueError("読めません")
    result = _run(
        _request(attachments=[Attachment(name="a.pdf", data=b"%PDF")]),
        services,
        corpora,
    )
    assert result["errors"] == ["a.pdf を開けませんでした: 読めません"]
    assert result["download"] is None
    assert recorder.ask_text_calls == []


def test_attachment_name_with_path_is_not_written_outside(recorder, services, corpora):
    result = _run(
        _request(attachments=[Attachment(name="..", data=b"x")]), services, corpora
    )
    assert result["errors"][0].startswith(".. を開けませんでした: ")
    assert recorder.parse_calls == []


def test_empty_attachment_warns_and_continues(recorder, services, corpora):
    recorder.parse_result = []
    result = _run(
        _request(attachments=[Attachment(name="a.pdf", data=b"%PDF")]),
        services,
        corpora,
    )
    assert "a.pdf から本文を取り出せませんでした。" in result["warnings"]
    assert result["download"] is not None


def test_attachments_are_captioned_through_vlm(recorder, services, corpora):
    attachment = Attachment(name="a.png", data=b"png-bytes")
    _run(_request(attachments=[attachment]), services, corpora)
    assert recorder.parse_calls[0] == ("a.png", b"png-bytes", services.caption_image)

    recorder.vlm_error = VlmError("VLMが見つかりません")
    result = _run(_request(attachments=[attachment]), services, corpora)
    assert recorder.parse_calls[1][2] is None
    assert "図表の説明文化は行いません: VLMが見つかりません" in result["warnings"]


def test_freeform_markdown_output(recorder, services, corpora):
    result = _run(_request(output_suffix=".docx"), services, corpora)
    assert result["errors"] == []
    assert result["download"]["file_name"] == "文書_2026-11-22.docx"
    assert _downloaded(result)[:2] == b"PK"


def test_source_code_output_skips_markdown_path(recorder, services, corpora, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("markdown_document.build は呼ばれないはず")

    monkeypatch.setattr(markdown_document, "build", unexpected)
    recorder.ask_text_reply = "```go\npackage main\n```"
    result = _run(_request(output_suffix=".go"), services, corpora)
    assert result["errors"] == []
    assert result["download"]["file_name"] == "文書_2026-11-22.go"
    data = _downloaded(result).decode("utf-8")
    assert "package main" in data
    assert source_code.REFERENCES_HEADING in data


def test_diagram_failures_are_reported(recorder, services, corpora, monkeypatch):
    def build(blocks, suffix, on_diagram_error=None):
        on_diagram_error("図1", "mmdc がありません")
        return b"data", []

    monkeypatch.setattr(markdown_document, "build", build)
    result = _run(_request(), services, corpora)
    assert "図にできなかった箇所: 図1（mmdc がありません）" in result["warnings"]

    name = _template(recorder)

    def fill(path, values, on_diagram_error=None):
        on_diagram_error("決定事項", "構文エラー")
        return b"data"

    monkeypatch.setattr(cowork.docgen, "fill", fill)
    result = _run(_request(template=name), services, corpora)
    assert "図にできなかった欄: 決定事項（構文エラー）" in result["warnings"]


def test_search_error_becomes_result_error(recorder, services, corpora):
    recorder.search_error = EmbeddingError("vLLM（埋め込み）に接続できません")
    result = _run(_request(), services, corpora)
    assert result["errors"] == ["vLLM（埋め込み）に接続できません"]
    assert result["download"] is None


def test_chat_error_during_generation_becomes_result_error(recorder, services, corpora):
    def failing(*args, **kwargs):
        raise ChatError("生成に失敗しました")

    result = _run(
        _request(), replace(services, ask_text=failing), corpora
    )
    assert result["errors"] == ["生成に失敗しました"]
    assert result["download"] is None


def test_prompt_too_long_becomes_result_error(recorder, services, corpora, monkeypatch):
    name = _template(recorder)

    def too_long(*args, **kwargs):
        raise filling.PromptTooLongError("添付と検索結果が長すぎます")

    monkeypatch.setattr(filling, "fill_values", too_long)
    result = _run(_request(template=name), services, corpora)
    assert result["errors"] == ["添付と検索結果が長すぎます"]


def test_unexpected_error_becomes_result_error_and_is_logged(
    recorder, services, corpora, caplog
):
    recorder.search_error = RuntimeError("想定外")
    with caplog.at_level(logging.ERROR, logger="rag_web.cowork"):
        result = _run(_request(), services, corpora)
    assert result["errors"] == ["文書を生成できませんでした: 想定外"]
    assert result["download"] is None
    assert any(record.exc_info for record in caplog.records)


def test_sources_carry_hit_views(recorder, services, corpora):
    result = _run(_request(), services, corpora)
    assert result["sources"] == [{"kind": "社内資料", "hits": [hit_view(recorder.hits[0])]}]
    collection, query, kwargs = recorder.search_calls[0]
    assert query == "第5回会議の議事録を作って"
    assert kwargs["rerank"] is services.rerank
    assert "rerank_floor" not in kwargs


def test_output_suffixes_lists_documents_then_sources():
    assert output_suffixes() == list(
        markdown_document.OUTPUT_SUFFIXES + source_code.OUTPUT_SUFFIXES
    )
    assert is_source_suffix(".GO")
    assert not is_source_suffix(".md")


def test_template_path_resolves_by_name(recorder, services):
    name = _template(recorder)
    assert template_path(services, name) == recorder.templates_dir / name
    with pytest.raises(FileNotFoundError):
        template_path(services, "無い.md")


def test_existing_services_constructions_keep_working():
    services = Services()
    assert services.templates_dir is None
