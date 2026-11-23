import json as json_module

import pytest
import requests

import ingest.chat as chat
from ingest.chat import ChatError, ask_json, ask_text, stream_chat
from ingest.vllm_config import AuthError


class _FakeResponse:
    def __init__(self, payload=None, lines=None, status=200):
        self._payload = payload
        self._lines = lines or []
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload

    def iter_lines(self):
        return iter(self._lines)


class _FakeSession:
    """POSTされたURLとペイロードを記録し、あらかじめ決めた応答を返す。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []
        self.payloads = []

    def post(self, url, json, timeout=None, stream=None):
        self.urls.append(url)
        self.payloads.append(json)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _reply(text, **message_extra):
    message = {"role": "assistant", "content": text, **message_extra}
    return _FakeResponse({"choices": [{"message": message}]})


def _sse(*objs):
    """vLLMのストリームはSSE。各チャンクは `data: {json}` の1行。"""
    return [b"data: " + json_module.dumps(obj).encode("utf-8") for obj in objs]


def _delta(**delta):
    return {"choices": [{"index": 0, "delta": delta}]}


def _stream(session, temperature=0.3):
    return list(stream_chat("m", [{"role": "user", "content": "hi"}], temperature, session=session))


def test_ask_json_posts_openai_chat_completions_with_json_mode():
    """条件抽出は再現性が要るのでtemperature=0固定。JSON以外を返されると困るのでJSONモードも固定。"""
    session = _FakeSession([_reply('{"a": 1}')])
    result = ask_json("gpt-oss-120b", "prompt", session=session)
    assert result == '{"a": 1}'
    assert session.urls == ["http://127.0.0.1:8001/v1/chat/completions"]
    assert session.payloads[0] == {
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": "prompt"}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "stream": False,
    }


def test_chat_url_is_read_when_called(monkeypatch):
    """import時に固定すると、.env を読む前に取り込まれたとき設定が効かない。"""
    monkeypatch.setenv("VLLM_CHAT_URL", "http://gb10:9000/")
    session = _FakeSession([_reply("x")])
    ask_text("m", "p", session=session)
    assert session.urls == ["http://gb10:9000/v1/chat/completions"]


def test_ask_json_ignores_reasoning_in_non_stream_reply():
    session = _FakeSession([_reply("答え", reasoning_content="考え中", reasoning="考え中")])
    assert ask_json("m", "p", session=session) == "答え"


def test_ask_text_sends_neither_response_format_nor_max_tokens():
    """JSONモードを送ると長いMarkdownがエスケープで壊れる。max_tokensを決め打ちすると
    思考も上限を食うため本文が途中で切れる。"""
    session = _FakeSession([_reply("# 設計書\n\n本文")])
    result = ask_text("m", "設計書を書いて", session=session)
    assert result == "# 設計書\n\n本文"
    assert session.payloads[0] == {
        "model": "m",
        "messages": [{"role": "user", "content": "設計書を書いて"}],
        "stream": False,
    }


def test_requests_carry_no_ollama_specific_fields():
    session = _FakeSession([_reply("a"), _reply("b"), _FakeResponse(lines=_sse(_delta(content="c")))])
    ask_json("m", "p", session=session)
    ask_text("m", "p", session=session)
    _stream(session)
    for payload in session.payloads:
        assert not {"keep_alive", "options", "format"} & payload.keys()


def test_ask_json_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(chat.time, "sleep", lambda _s: None)
    session = _FakeSession([requests.ConnectionError("boom"), _reply('{"ok": true}')])
    assert ask_json("m", "p", session=session) == '{"ok": true}'


@pytest.mark.parametrize("failure", [requests.ConnectionError("x"), _FakeResponse(status=503)])
def test_ask_gives_up_after_four_attempts(monkeypatch, failure):
    monkeypatch.setattr(chat.time, "sleep", lambda _s: None)
    session = _FakeSession([failure] * 4)
    with pytest.raises(ChatError) as info:
        ask_json("m", "p", session=session)
    assert str(info.value).startswith("http://127.0.0.1:8001 への生成リクエストが4回失敗しました")
    assert len(session.payloads) == 4


def test_auth_failure_is_not_retried_and_becomes_chat_error(monkeypatch):
    """ChatError に包む（AuthError の子ではない）。rag_web の既存ハンドラが
    ChatError だけ見ていれば認証の失敗も拾えるようにするため。"""
    monkeypatch.setattr(chat.time, "sleep", lambda _s: pytest.fail("認証失敗は再試行しない"))
    session = _FakeSession([_FakeResponse(status=401)])
    with pytest.raises(ChatError, match="VLLM_API_KEY") as info:
        ask_json("m", "p", session=session)
    assert not isinstance(info.value, AuthError)
    assert isinstance(info.value.__cause__, AuthError)
    assert len(session.payloads) == 1


def test_stream_auth_failure_becomes_chat_error():
    session = _FakeSession([_FakeResponse(status=403)])
    with pytest.raises(ChatError, match="VLLM_API_KEY"):
        _stream(session)


def test_stream_chat_yields_content_pieces_in_order():
    session = _FakeSession(
        [
            _FakeResponse(
                lines=_sse(_delta(content="こん"), _delta(content="にちは"))
                + [b"data: [DONE]"]
            )
        ]
    )
    assert "".join(_stream(session)) == "こんにちは"
    assert session.urls == ["http://127.0.0.1:8001/v1/chat/completions"]


def test_stream_chat_sends_temperature_and_stream_flag():
    session = _FakeSession([_FakeResponse(lines=[b"data: [DONE]"])])
    _stream(session, temperature=0.7)
    assert session.payloads[0] == {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
        "stream": True,
    }


def test_stream_skips_chunks_with_empty_choices():
    session = _FakeSession(
        [_FakeResponse(lines=_sse({"choices": []}, _delta(content="a")))]
    )
    assert _stream(session) == ["a"]


def test_stream_skips_role_only_delta():
    session = _FakeSession(
        [_FakeResponse(lines=_sse(_delta(role="assistant"), _delta(content="a")))]
    )
    assert _stream(session) == ["a"]


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_stream_never_emits_reasoning(field):
    session = _FakeSession(
        [_FakeResponse(lines=_sse(_delta(**{field: "考え中"}), _delta(content="答え")))]
    )
    assert _stream(session) == ["答え"]


def test_stream_stops_at_done_and_ignores_blank_and_comment_lines():
    session = _FakeSession(
        [
            _FakeResponse(
                lines=[b"", b": keep-alive"]
                + _sse(_delta(content="a"))
                + [b"", b"data: [DONE]"]
                + _sse(_delta(content="after"))
            )
        ]
    )
    assert _stream(session) == ["a"]


def test_stream_raises_chat_error_on_mid_stream_error():
    session = _FakeSession(
        [
            _FakeResponse(
                lines=_sse(_delta(content="途中まで"), {"error": {"message": "model crashed"}})
            )
        ]
    )
    gen = stream_chat("m", [], 0.3, session=session)
    assert next(gen) == "途中まで"
    with pytest.raises(ChatError, match="model crashed"):
        next(gen)


def test_stream_raises_chat_error_on_unparsable_chunk():
    session = _FakeSession([_FakeResponse(lines=[b"data: {not json"])])
    with pytest.raises(ChatError, match="解釈できませんでした"):
        _stream(session)


def test_stream_raises_when_the_connection_fails():
    session = _FakeSession([requests.ConnectionError("refused")])
    with pytest.raises(ChatError, match="への生成リクエストに失敗しました"):
        _stream(session)
