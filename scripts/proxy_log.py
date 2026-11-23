"""Claude Code と vLLM の間に挟んで、送信内容を記録する診断用プロキシ。

目的は1つだけである。**Claude Code が MCP ツール（search_documents）をモデルへ
渡しているか、モデルがそれを呼んだか**を見る。

Claude Code は Anthropic Messages API（POST /v1/messages）で話す。ツールは要求の
`tools` 配列に入り、モデルが呼べば応答に `tool_use` のブロックが現れる。この2点を
記録する。

手順は docs/mcp-tool-not-called.md にある。

このファイルは単体で動く。プロジェクトの他のモジュールを import しないので、
Claude Code が動いている機械へこの1枚だけコピーすればよい（必要なのは Python と
requests だけである）。クライアント機にリポジトリを置く必要はない。

使い方（Claude Code が動いている機械で）:

    python proxy_log.py --upstream https://<GB10>/llm

上流は Claude Code の設定にある ANTHROPIC_BASE_URL の値をそのまま渡す。
そのあと ANTHROPIC_BASE_URL を http://127.0.0.1:11500 に向け、Claude Code を
完全に再起動して1回質問する。proxy_log.txt に結果が出る。

終わったら ANTHROPIC_BASE_URL を元に戻すこと。
"""
import argparse
import datetime
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

LOG_PATH = "proxy_log.txt"
DEFAULT_PORT = 11500
TARGET_TOOL = "search_documents"
_log_lock = threading.Lock()

# 中継してはいけないヘッダ。長さと転送方式は自分で決め直す。
# accept-encoding を落とすのは、上流に圧縮させず、応答を読めるようにするため。
_SKIP_REQUEST = {"host", "content-length", "accept-encoding", "connection"}
_SKIP_RESPONSE = {"content-length", "transfer-encoding", "connection", "content-encoding"}

# エラー応答の本文を記録するときの上限。原因（文脈長の超過など）を読むには足り、
# 本文が長く漏れることはない長さにする。
_ERROR_MESSAGE_LIMIT = 300


def log(message: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    with _log_lock:
        print(line, flush=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def upstream_url(upstream: str, path: str) -> str:
    """上流のURLに要求のパスを足す。

    urljoin は使わない。上流が https://<GB10>/llm のように前置きを持つとき、
    "/v1/messages" との urljoin は /llm を捨てて Nginx の 404 になる。
    """
    return upstream.rstrip("/") + path


def is_messages_path(path: str) -> bool:
    """Claude Code の本題の要求か。

    Claude Code は /v1/messages?beta=true のように問い合わせ文字列を付ける。
    /v1/messages/count_tokens はトークン数の見積もりで、モデルは応答しない。
    """
    return path.split("?", 1)[0].rstrip("/").endswith("/v1/messages")


def _tool_name(tool) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or "")
    return ""


def summarize_request(body: bytes) -> list[str]:
    """要求の中身のうち、知りたいことだけを書き出す。

    本文の全文は出さない。社内資料の断片やソースコードが混ざりうるためで、
    ツール名と件数、入力の長さだけあれば今回の判定には足りる。
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [f"本文を解釈できませんでした（{len(body)} バイト）"]
    if not isinstance(payload, dict):
        return [f"本文がJSONのオブジェクトではありません（{len(body)} バイト）"]

    tools = payload.get("tools") or []
    lines = [
        f"model={payload.get('model')!r}  stream={payload.get('stream')}  本文={len(body)}バイト",
        f"tools配列: {len(tools)}件",
    ]
    if not tools:
        # Claude Code は本題のほかに、題名づけや要約の補助の要求も送る。これらは
        # ツールを持たないので、ツール無しの要求が1つあるだけでは何も言えない。
        lines.append(
            "  ツール無し。補助の要求（題名づけ・要約など）ならこれで正常。"
            "Read や Bash が並ぶ本題の要求を見ること"
        )
        return lines

    builtin = [_tool_name(tool) for tool in tools if not _tool_name(tool).startswith("mcp__")]
    mcp = [tool for tool in tools if _tool_name(tool).startswith("mcp__")]
    if builtin:
        lines.append(f"  組み込み: {', '.join(builtin)}")
    for tool in mcp:
        name = _tool_name(tool)
        notes = []
        if tool.get("defer_loading"):
            # tool search が有効だと、定義が後回しにされ、モデルは検索してからでないと呼べない。
            notes.append("defer_loading")
        mark = " ← これ" if TARGET_TOOL in name else ""
        suffix = f"  ({', '.join(notes)})" if notes else ""
        lines.append(f"  - {name}{suffix}{mark}")
    if not any(TARGET_TOOL in _tool_name(tool) for tool in tools):
        lines.append(f"  ★ {TARGET_TOOL} が tools に無い。MCPツールが渡されていない")

    if "tool_choice" in payload:
        lines.append(f"  tool_choice: {json.dumps(payload['tool_choice'], ensure_ascii=False)}")
    for key in ("system", "messages"):
        if key in payload:
            text = json.dumps(payload[key], ensure_ascii=False)
            lines.append(f"  {key}: {len(text)}文字")
    return lines


def _error_lines(error) -> list[str]:
    if not isinstance(error, dict):
        return []
    message = str(error.get("message") or "")[:_ERROR_MESSAGE_LIMIT]
    return [f"  エラー: type={error.get('type')!r}  message={message!r}"]


def summarize_response(status: int, body: bytes) -> list[str]:
    """応答に tool_use のブロックが現れたかどうかを見る。

    ストリーミング（SSE）でも、そうでない JSON でも読む。
    """
    text = body.decode("utf-8", "replace")
    called: list[str] = []
    answer_text: list[str] = []
    stop_reason = None
    error_lines: list[str] = []

    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                continue
    if not events:
        try:
            events = [json.loads(text)]
        except json.JSONDecodeError:
            events = []

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "error":
            error_lines.extend(_error_lines(event.get("error")))
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                called.append(block.get("name"))
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                answer_text.append(str(delta.get("text") or ""))
        elif kind == "message_delta":
            stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
        elif kind == "message":
            # ストリーミングでない応答は、メッセージ全体が1つの JSON で返る。
            stop_reason = event.get("stop_reason")
            for block in event.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    called.append(block.get("name"))
                elif block.get("type") == "text":
                    answer_text.append(str(block.get("text") or ""))

    lines = [f"応答 HTTP {status}  stop_reason={stop_reason!r}"]
    lines.extend(error_lines)
    if status >= 400 and not error_lines:
        lines.append(f"  エラー応答（{len(body)} バイト、形式を解釈できませんでした）")
    if status >= 400:
        return lines

    if called:
        lines.append(f"  ✅ モデルが呼んだツール: {called}")
    elif TARGET_TOOL in "".join(answer_text):
        # モデルは呼ぶつもりで名前を書いたが、vLLM がツール呼び出しとして
        # 解釈できず、ただの文章として返った形である。
        lines.append(
            f"  ★ 本文に {TARGET_TOOL} の名前が出ているが tool_use になっていない。"
            "ツール呼び出しの解釈（vLLM の --tool-call-parser）を疑う"
        )
    else:
        lines.append("  ❌ モデルはツールを呼ばなかった")
    return lines


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # 既定のアクセスログは出さない。自前のログだけにする

    def _relay(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        # 認証ヘッダ（Authorization / x-api-key）はそのまま上流へ渡す。記録はしない。
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _SKIP_REQUEST
        }

        log(f"--- {method} {self.path} ---")
        interesting = method == "POST" and is_messages_path(self.path)
        if interesting and body:
            for line in summarize_request(body):
                log(f"  {line}")

        try:
            upstream = requests.request(
                method,
                upstream_url(self.server.upstream, self.path),
                data=body or None,
                headers=headers,
                stream=True,
                timeout=(10, 900),
            )
        except requests.RequestException as error:
            log(f"  上流への中継に失敗: {error}")
            self.send_error(502, "upstream failed")
            return

        self.send_response(upstream.status_code)
        for key, value in upstream.headers.items():
            if key.lower() not in _SKIP_RESPONSE:
                self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        chunks = []
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if not chunk:
                    continue
                if interesting:
                    chunks.append(chunk)
                self.wfile.write(f"{len(chunk):X}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Claude Code は中断（Esc）で途中で切る。記録だけ残して続ける。
            log("  クライアントが接続を切りました")
        except requests.RequestException as error:
            log(f"  上流からの受信が途中で切れました: {error}")
        finally:
            upstream.close()

        if interesting:
            for line in summarize_response(upstream.status_code, b"".join(chunks)):
                log(f"  {line}")

    def do_POST(self):
        self._relay("POST")

    def do_GET(self):
        self._relay("GET")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Claude Code の送信内容を記録する診断用プロキシ")
    parser.add_argument(
        "--upstream",
        required=True,
        help="本物の接続先。Claude Code の ANTHROPIC_BASE_URL の値（例: https://<GB10>/llm）",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    upstream = args.upstream.rstrip("/")
    if upstream.endswith("/v1"):
        # Claude Code が /v1/messages を足すので、/v1 まで含めると /v1/v1/messages になる。
        parser.error("--upstream の末尾に /v1 を付けないこと（ANTHROPIC_BASE_URL と同じ値を渡す）")

    log(f"プロキシ開始: http://127.0.0.1:{args.port} → {upstream}")
    log(f"ANTHROPIC_BASE_URL を http://127.0.0.1:{args.port} に向け、Claude Code を完全に再起動してください")
    log("終わったら ANTHROPIC_BASE_URL を元に戻すこと")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.upstream = upstream
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("停止しました")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
