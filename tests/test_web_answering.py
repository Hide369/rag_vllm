from datetime import date

import pytest

from ingest import conditions
from ingest.chat import ChatError
from ingest.embedder import EmbeddingError
from ingest.prompting import format_hit_caption
from ingest.retrieval import DOCS_RERANK_FLOOR, Hit, contextual_query
from rag_web import answering
from rag_web.answering import ChatRequest, answer_events, hit_view, system_prompt
from rag_web.services import Services

TODAY = date(2026, 11, 22)


def _hit(text="本文です", source="a.pdf"):
    return Hit(
        text=text,
        distance=0.1,
        occurrences=[
            {"source": source, "location_type": "page", "location": 1}
        ],
    )


class FakeCorpora:
    """CorpusCache の偽物。DB を開かずに済ませる。"""

    def __init__(self, schema=None):
        self.schema_calls = []
        self._schema = schema if schema is not None else {"幅": "number"}
        self.rerank = object()

    def collection(self, corpus):
        return f"collection:{corpus}"

    def index(self, corpus):
        return f"index:{corpus}"

    def schema(self, corpus):
        self.schema_calls.append(corpus)
        return self._schema

    def rerank_callable(self):
        return self.rerank


class Recorder:
    # strip_html_tags_stream は1文字ずつ流すため、既定の断片も1文字にして
    # delta の数を断片の数と一致させる。
    def __init__(self, chunks=("東", "京"), hits=None, ask_reply="{}"):
        self.chunks = list(chunks)
        self.hits = [_hit()] if hits is None else hits
        self.ask_reply = ask_reply
        self.search_calls = []
        self.chat_calls = []
        self.ask_calls = []

    def search(self, collection, query, **kwargs):
        self.search_calls.append((collection, query, kwargs))
        return self.hits

    def stream_chat(self, model, messages, temperature):
        self.chat_calls.append((model, messages, temperature))
        yield from self.chunks

    def ask_json(self, model, prompt):
        self.ask_calls.append((model, prompt))
        return self.ask_reply

    def services(self):
        return Services(
            stream_chat=self.stream_chat, ask_json=self.ask_json, search=self.search
        )


def _request(corpus="internal", question="首都は？", history=None):
    return ChatRequest(
        corpus=corpus,
        model="m",
        temperature=0.2,
        question=question,
        history=history or [],
    )


def _run(recorder, request, corpora=None):
    return list(
        answer_events(request, recorder.services(), corpora or FakeCorpora(), TODAY)
    )


@pytest.fixture
def no_conditions(monkeypatch):
    monkeypatch.setattr(
        answering.conditions, "extract", lambda q, s, ask: conditions.Extraction()
    )


def test_internal_search_streams_deltas_then_evidence_then_done(no_conditions):
    recorder = Recorder()
    corpora = FakeCorpora()
    events = _run(recorder, _request(), corpora)
    assert [e["type"] for e in events] == ["delta", "delta", "evidence", "done"]
    assert events[2]["hits"][0] == hit_view(recorder.hits[0])
    assert events[2]["table"] is None
    assert [e["text"] for e in events[:2]] == ["東", "京"]
    assert events[-1]["display"] is None
    collection, _, kwargs = recorder.search_calls[0]
    assert collection == "collection:internal"
    assert kwargs["index"] == "index:internal"
    assert kwargs["rerank"] is corpora.rerank
    assert "rerank_floor" not in kwargs


def test_hit_view_shape():
    hit = Hit(
        text="本文",
        distance=0.1,
        occurrences=[
            {"source": "a.pdf", "location_type": "page", "location": 1},
            {"source": "b.pdf", "location_type": "page", "location": 2},
        ],
    )
    assert hit_view(hit) == {
        "caption": format_hit_caption(hit),
        "others": hit.all_citations()[1:],
        "text": "本文",
    }


def test_history_is_used_for_query_and_messages(no_conditions):
    history = [
        {"role": "user", "content": "前の質問"},
        {"role": "assistant", "content": "前の回答"},
    ]
    recorder = Recorder()
    _run(recorder, _request(history=history))
    assert recorder.search_calls[0][1] == contextual_query("首都は？", history)
    messages = recorder.chat_calls[0][1]
    assert messages[0]["role"] == "system"
    assert messages[1:-1] == history
    assert messages[-1]["role"] == "user"


def test_docs_corpus_translates_query_and_skips_condition_extraction(monkeypatch):
    def fail(*args):
        raise AssertionError("conditions.extract が呼ばれた")

    monkeypatch.setattr(answering.conditions, "extract", fail)
    recorder = Recorder(ask_reply='{"query": "capital city"}')
    corpora = FakeCorpora()
    events = _run(recorder, _request(corpus="docs"), corpora)
    assert events[-1]["type"] == "done"
    collection, query, kwargs = recorder.search_calls[0]
    assert collection == "collection:docs"
    assert query == "capital city"
    assert kwargs["rerank_floor"] == DOCS_RERANK_FLOOR
    assert kwargs["rerank"] is corpora.rerank
    assert corpora.schema_calls == []


def test_catalog_path_shows_beyond_only_on_screen(monkeypatch):
    conds = {"幅": {"op": "<=", "value": 500}}
    monkeypatch.setattr(
        answering.conditions,
        "extract",
        lambda q, s, ask: conditions.Extraction(conditions=conds),
    )
    monkeypatch.setattr(answering.conditions, "extract_ranking", lambda q, s, ask: None)
    monkeypatch.setattr(answering.catalog, "select", lambda c, cond: ["UD-100"])
    monkeypatch.setattr(answering.catalog, "relaxations", lambda c, cond: [])
    monkeypatch.setattr(
        answering.catalog, "exceeding", lambda c, cond, ranking, matched: ["UD-1400X"]
    )

    def format_table(cond, matched, relaxed, ranking=None, beyond=()):
        return " ".join(list(matched) + list(beyond))

    monkeypatch.setattr(answering.catalog, "format_table", format_table)
    recorder = Recorder()
    events = _run(recorder, _request(question="幅500mm以下の洗濯機は？"))
    evidence = next(e for e in events if e["type"] == "evidence")
    assert "UD-1400X" in evidence["table"]
    assert evidence["hits"] == []
    user_content = recorder.chat_calls[0][1][-1]["content"]
    assert "UD-100" in user_content
    assert "UD-1400X" not in user_content
    assert recorder.search_calls == []


def test_extraction_failure_adds_note_first(monkeypatch):
    monkeypatch.setattr(
        answering.conditions,
        "extract",
        lambda q, s, ask: conditions.Extraction(failed=True),
    )
    events = _run(Recorder(), _request())
    assert events[0] == {
        "type": "note",
        "text": "条件を解釈できませんでした。通常の検索で回答します。",
    }
    assert events[-1]["type"] == "done"


def test_embedding_error_yields_search_error_without_note(monkeypatch):
    monkeypatch.setattr(
        answering.conditions,
        "extract",
        lambda q, s, ask: conditions.Extraction(failed=True),
    )
    recorder = Recorder()

    def search(*args, **kwargs):
        raise EmbeddingError("接続できません")

    services = Services(
        stream_chat=recorder.stream_chat, ask_json=recorder.ask_json, search=search
    )
    events = list(answer_events(_request(), services, FakeCorpora(), TODAY))
    assert events == [
        {"type": "error", "message": "検索できませんでした: 接続できません"}
    ]
    assert recorder.chat_calls == []


def test_chat_error_yields_generation_error(no_conditions):
    def stream_chat(model, messages, temperature):
        raise ChatError("モデルがありません")
        yield  # pragma: no cover

    recorder = Recorder()
    services = Services(
        stream_chat=stream_chat, ask_json=recorder.ask_json, search=recorder.search
    )
    events = list(answer_events(_request(), services, FakeCorpora(), TODAY))
    assert events == [
        {"type": "error", "message": "回答を生成できませんでした: モデルがありません"}
    ]


def test_unexpected_error_mid_stream_ends_with_error_event(no_conditions):
    def stream_chat(model, messages, temperature):
        yield "途"
        raise RuntimeError("壊れた")

    recorder = Recorder()
    services = Services(
        stream_chat=stream_chat, ask_json=recorder.ask_json, search=recorder.search
    )
    events = list(answer_events(_request(), services, FakeCorpora(), TODAY))
    assert [e["type"] for e in events] == ["delta", "error"]
    assert events[-1]["message"] == "回答を生成できませんでした: 壊れた"


def test_display_mode_keeps_html_and_reports_display(no_conditions):
    recorder = Recorder(chunks=["| a<br>b |\n"])
    events = _run(recorder, _request(question="一覧をマークダウンで表示して"))
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "<br>" in text
    assert events[-1] == {"type": "done", "display": "markdown"}


def test_html_tags_are_stripped_without_display_mode(no_conditions):
    recorder = Recorder(chunks=["| a<br>b |\n"])
    events = _run(recorder, _request())
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "<br>" not in text


def test_answer_label_is_stripped(no_conditions):
    recorder = Recorder(chunks=["答え：東京"])
    events = _run(recorder, _request())
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "東京"


def test_system_prompt_uses_given_date():
    assert "2026年11月22日" in system_prompt(date(2026, 11, 22))


def test_messages_start_with_system_prompt_for_today(no_conditions):
    recorder = Recorder()
    _run(recorder, _request())
    assert recorder.chat_calls[0][1][0]["content"] == system_prompt(TODAY)


def test_docs_corpus_translates_query_with_previous_question(monkeypatch):
    # 追質問は単独では引けないため、翻訳に渡す前に直前の質問を継ぎ足す（contextual_query）。
    def fail(*args):
        raise AssertionError("conditions.extract が呼ばれた")

    monkeypatch.setattr(answering.conditions, "extract", fail)
    history = [
        {"role": "user", "content": "FastAPIの依存性注入とは"},
        {"role": "assistant", "content": "Depends を使います"},
    ]
    recorder = Recorder(ask_reply='{"query": "dependency injection example"}')
    events = _run(recorder, _request(corpus="docs", question="例は？", history=history))
    assert events[-1]["type"] == "done"
    assert len(recorder.ask_calls) == 1
    _, prompt = recorder.ask_calls[0]
    assert "FastAPIの依存性注入とは" in prompt
    assert "例は？" in prompt
    assert recorder.search_calls[0][1] == "dependency injection example"
