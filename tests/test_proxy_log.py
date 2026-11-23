"""Claude Code 用の診断プロキシ（scripts/proxy_log.py）の判定と中継。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from scripts import proxy_log

SEARCH = "mcp__local_docs__search_documents"


def _request(tools, **extra) -> bytes:
    payload = {
        "model": "gpt-oss-120b",
        "stream": True,
        "system": [{"type": "text", "text": "指示"}],
        "messages": [{"role": "user", "content": "懲戒解雇について"}],
        "tools": tools,
        **extra,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _tool(name, **extra):
    return {"name": name, "description": "d", "input_schema": {"type": "object"}, **extra}


def _sse(*events) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    ).encode("utf-8")


def _tool_use_stream(name):
    return _sse(
        {"type": "message_start", "message": {"id": "m", "content": []}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t", "name": name, "input": {}},
        },
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )


def _text_stream(text):
    return _sse(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    )


# --- 要求の判定 ---


def test_request_with_search_documents_is_marked():
    lines = proxy_log.summarize_request(_request([_tool("Read"), _tool("Bash"), _tool(SEARCH)]))
    text = "\n".join(lines)
    assert "tools配列: 3件" in text
    assert "組み込み: Read, Bash" in text
    assert f"- {SEARCH} ← これ" in text
    assert "★" not in text


def test_request_without_mcp_tool_is_flagged():
    text = "\n".join(proxy_log.summarize_request(_request([_tool("Read"), _tool("Bash")])))
    assert "★ search_documents が tools に無い" in text


def test_request_without_tools_is_treated_as_auxiliary_not_as_failure():
    # Claude Code の題名づけなどの補助の要求はツールを持たない。これを失敗と断じない。
    text = "\n".join(proxy_log.summarize_request(_request([])))
    assert "tools配列: 0件" in text
    assert "補助の要求" in text
    assert "★" not in text


def test_deferred_mcp_tool_is_reported():
    text = "\n".join(proxy_log.summarize_request(_request([_tool(SEARCH, defer_loading=True)])))
    assert "(defer_loading)" in text


def test_request_log_does_not_contain_the_conversation():
    text = "\n".join(proxy_log.summarize_request(_request([_tool(SEARCH)])))
    assert "懲戒解雇" not in text
    assert "messages:" in text and "文字" in text


def test_unparsable_request_is_reported_without_crashing():
    assert proxy_log.summarize_request(b"\xff\xfe") == ["本文を解釈できませんでした（2 バイト）"]
    assert proxy_log.summarize_request(b"[]") == ["本文がJSONのオブジェクトではありません（2 バイト）"]


# --- 応答の判定 ---


def test_streamed_tool_use_is_detected():
    text = "\n".join(proxy_log.summarize_response(200, _tool_use_stream(SEARCH)))
    assert "stop_reason='tool_use'" in text
    assert f"✅ モデルが呼んだツール: ['{SEARCH}']" in text


def test_non_streamed_tool_use_is_detected():
    body = json.dumps(
        {
            "type": "message",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t", "name": SEARCH, "input": {}}],
        }
    ).encode()
    assert any("✅" in line for line in proxy_log.summarize_response(200, body))


def test_plain_answer_is_reported_as_not_called():
    text = "\n".join(proxy_log.summarize_response(200, _text_stream("一般論として回答します")))
    assert "❌ モデルはツールを呼ばなかった" in text


def test_tool_name_written_as_text_points_to_the_parser():
    text = "\n".join(
        proxy_log.summarize_response(200, _text_stream(f'{SEARCH}({{"query": "x"}})'))
    )
    assert "tool_use になっていない" in text
    assert "--tool-call-parser" in text


def test_error_response_shows_type_and_truncated_message():
    body = json.dumps(
        {"type": "error", "error": {"type": "invalid_request_error", "message": "x" * 1000}}
    ).encode()
    lines = proxy_log.summarize_response(400, body)
    text = "\n".join(lines)
    assert "応答 HTTP 400" in text
    assert "invalid_request_error" in text
    assert "x" * 300 in text and "x" * 301 not in text
    assert "❌" not in text


# --- パス ---


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/messages", True),
        ("/v1/messages?beta=true", True),
        ("/v1/messages/count_tokens?beta=true", False),
        ("/v1/models", False),
    ],
)
def test_is_messages_path(path, expected):
    assert proxy_log.is_messages_path(path) is expected


def test_upstream_prefix_is_kept():
    assert (
        proxy_log.upstream_url("https://gb10.example/llm", "/v1/messages?beta=true")
        == "https://gb10.example/llm/v1/messages?beta=true"
    )


def test_upstream_ending_in_v1_is_rejected():
    with pytest.raises(SystemExit):
        proxy_log.main(["--upstream", "https://gb10.example/llm/v1"])


# --- 中継（実際のソケットで往復する） ---


@pytest.fixture
def fake_upstream():
    received = {}

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            received["path"] = self.path
            received["authorization"] = self.headers.get("Authorization")
            received["body"] = self.rfile.read(length)
            body = _tool_use_stream(SEARCH)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/llm", received
    server.shutdown()
    server.server_close()


def test_relay_forwards_to_prefixed_upstream_and_logs_the_tool_call(
    fake_upstream, tmp_path, monkeypatch
):
    upstream, received = fake_upstream
    log_path = tmp_path / "proxy_log.txt"
    monkeypatch.setattr(proxy_log, "LOG_PATH", str(log_path))

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy_log.Handler)
    proxy.upstream = upstream
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        body = _request([_tool("Read"), _tool(SEARCH)])
        response = requests.post(
            f"http://127.0.0.1:{proxy.server_address[1]}/v1/messages?beta=true",
            data=body,
            headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
            timeout=10,
        )
    finally:
        proxy.shutdown()
        proxy.server_close()

    assert response.status_code == 200
    assert response.content == _tool_use_stream(SEARCH)
    assert received["path"] == "/llm/v1/messages?beta=true"
    assert received["authorization"] == "Bearer secret-token"
    assert received["body"] == body

    log_text = log_path.read_text(encoding="utf-8")
    assert f"{SEARCH} ← これ" in log_text
    assert "✅ モデルが呼んだツール" in log_text
    assert "secret-token" not in log_text
    assert "懲戒解雇" not in log_text
