"""vLLM の OpenAI 互換チャットAPI（/v1/chat/completions）を叩く。

openaiパッケージは使わず requests で直接叩く（依存を増やさないため）。

文脈の長さはリクエストでは変えられず、vLLM サーバー起動時の --max-model-len で
決まる。呼び出し側が文脈長を指定する口は持たない。

gpt-oss は内部の思考を本文とは別の欄（reasoning_content、版によっては reasoning）
で返す。ここでは本文（content）だけを取り出し、思考は呼び出し側へ渡さない。
思考が本文に混ざると、回答や JSON が壊れる。
"""
import json
import time
from collections.abc import Iterator

import requests

from ingest.vllm_config import AuthError, load_vllm_settings, new_session, raise_for_auth

_MAX_ATTEMPTS = 4  # embedder.pyと同じ: 初回 + 3回の再試行（1秒 → 2秒 → 4秒）
_TIMEOUT = 600


class ChatError(Exception):
    """チャット生成に失敗した。"""


def _auth_error_to_chat_error(error: AuthError) -> ChatError:
    # rag_web のハンドラは ChatError だけを見ている。AuthError を素通しすると
    # 認証の失敗だけ画面に出ず 500 になるため、同じ文言のまま包み直す。
    return ChatError(str(error))


def _empty_content_message(finish_reason) -> str:
    message = f"応答に本文がありません（finish_reason={finish_reason}"
    if finish_reason == "length":
        message += "。思考で出力の上限を使い切った可能性があります"
    return message + "）"


def _chat_completions_url() -> str:
    return f"{load_vllm_settings().chat_url}/v1/chat/completions"


def _complete(payload: dict, session) -> str:
    """非ストリームで1回生成して本文を返す。接続エラーと5xxは指数バックオフで再試行する。"""
    own_session = session is None
    session = session or new_session()
    try:
        url = _chat_completions_url()
        last_error = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = session.post(url, json=payload, timeout=_TIMEOUT)
                # 401/403 は何度送っても通らないので、再試行の対象にしない。
                raise_for_auth(response, url)
                response.raise_for_status()
                choice = response.json()["choices"][0]
                content = choice["message"].get("content")
                if content is None or not content.strip():
                    # 思考で出力の上限を使い切った、または途中で切れたときの空応答。
                    # temperature=0 では同じ結果を繰り返し、再試行は毎回フル生成になるので
                    # 再試行せず、原因が分かる文面で即座に止める。
                    raise ChatError(_empty_content_message(choice.get("finish_reason")))
                return content
            except AuthError as error:
                raise _auth_error_to_chat_error(error) from error
            except (requests.RequestException, KeyError, IndexError, ValueError) as error:
                last_error = error
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(2**attempt)
        base_url = load_vllm_settings().chat_url
        raise ChatError(
            f"{base_url} への生成リクエストが{_MAX_ATTEMPTS}回失敗しました: {last_error}"
        )
    finally:
        if own_session:
            session.close()


def ask_json(model: str, prompt: str, session=None) -> str:
    """JSONオブジェクト1個だけを返させる。条件抽出用。temperature=0固定。

    temperature=0固定の理由はingest/conditions.pyと同じ: 同じ質問で条件が
    揺れると再現性のない誤りになるため。
    """
    return _complete(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "stream": False,
        },
        session,
    )


def ask_text(model: str, prompt: str, session=None) -> str:
    """平文をそのまま返させる。文書生成用。

    ask_json と違って response_format を送らない。数千字の Markdown を JSON 文字列へ
    押し込ませると、改行・引用符・バックスラッシュのエスケープで壊れる。

    temperature も max_tokens も送らない。temperature は、ask_json が 0 を固定する
    のは条件抽出で答えが揺れると再現性のない誤りになるためで、文章生成にその要求は
    ない。max_tokens は、思考も出力の上限を食うため小さく決め打ちすると本文が途中で
    切れるので、サーバーの最大長に任せる。
    """
    return _complete(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        session,
    )


def stream_chat(
    model: str, messages: list[dict], temperature: float, session=None
) -> Iterator[str]:
    """回答をトークン単位で流す。

    ストリームはSSE（`data: {json}` の行、終端は `data: [DONE]`）。空行とコメント行
    （`:` 始まり。keep-alive に使われる）は読み飛ばす。本文は choices[0].delta.content
    だけを出し、role だけの差分・choices が空のチャンク・思考の差分は出さない。
    呼び出し側がジェネレータを最後まで消費し終えた時点（またはエラー）でsessionを閉じる。
    """
    own_session = session is None
    session = session or new_session()
    try:
        base_url = load_vllm_settings().chat_url
        url = f"{base_url}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        try:
            response = session.post(url, json=payload, stream=True, timeout=_TIMEOUT)
            raise_for_auth(response, url)
            response.raise_for_status()
        except AuthError as error:
            raise _auth_error_to_chat_error(error) from error
        except requests.RequestException as error:
            raise ChatError(f"{base_url} への生成リクエストに失敗しました: {error}") from error

        for raw_line in response.iter_lines():
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data:"):
                continue  # 空行、コメント、event: 行
            body = line[len("data:"):].strip()
            if body == "[DONE]":
                break
            try:
                data = json.loads(body)
            except ValueError as error:
                raise ChatError(f"生成結果を解釈できませんでした: {error}") from error
            if data.get("error"):
                error_body = data["error"]
                message = error_body.get("message") if isinstance(error_body, dict) else error_body
                raise ChatError(str(message or error_body))
            choices = data.get("choices") or []
            if not choices:
                continue
            content = choices[0].get("delta", {}).get("content")
            if content:
                yield content
    finally:
        if own_session:
            session.close()
