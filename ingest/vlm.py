"""vLLM の VLM(Vision Language Model) にPDF/PPTX埋め込み画像の説明文を作らせる。

OpenAI 互換のチャットAPI（/v1/chat/completions）へ、画像を data URL で渡す。

design: docs/superpowers/specs/2026-08-24-vlm-image-captioning-design.md
"""
import base64
import time

import requests

# 装飾画像の合図語は ingest/image_text.py の DECORATION が正本。ここで文字列を
# 書き写すと、片方だけ変えたときに判定が黙って外れる（image_text側はvlmを
# importしないので循環にはならない）。
from ingest.image_text import DECORATION
from ingest.vllm_config import (
    AuthError,
    list_model_ids,
    load_vllm_settings,
    new_session,
    raise_for_auth,
)

CAPTION_PROMPT = (
    "この画像に写っている図表・写真の内容を、日本語で2〜3文にまとめて説明してください。"
    f"ロゴやアイコンなど内容のない装飾画像であれば「{DECORATION}」とだけ答えてください。"
)

_MAX_ATTEMPTS = 4  # embedder.pyと同じ: 初回 + 3回の再試行（1秒 → 2秒 → 4秒）
_TIMEOUT = 600

_PNG_SIGNATURE = bytes([0x89]) + b"PNG"
_JPEG_SIGNATURE = bytes([0xFF, 0xD8, 0xFF])


class VlmError(Exception):
    """画像の説明取得に失敗した。"""


def _mime_type(image_bytes: bytes) -> str:
    # data URL には形式が要る。parsers が渡すのは主に PNG / JPEG なので、先頭の
    # バイトで見分け、判別できないものは PNG として送る。
    if image_bytes.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    if image_bytes.startswith(_PNG_SIGNATURE):
        return "image/png"
    return "image/png"


def _image_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{_mime_type(image_bytes)};base64,{encoded}"


def caption_image(image_bytes: bytes, session=None) -> str:
    """画像1枚を日本語の説明文にする。"""
    settings = load_vllm_settings()
    own_session = session is None
    session = session or new_session()
    try:
        url = f"{settings.vlm_url}/v1/chat/completions"
        payload = {
            "model": settings.vlm_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": CAPTION_PROMPT},
                        {"type": "image_url", "image_url": {"url": _image_url(image_bytes)}},
                    ],
                }
            ],
            "stream": False,
        }
        last_error = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = session.post(url, json=payload, timeout=_TIMEOUT)
                # 401/403 は何度送っても通らないので、再試行の対象にしない。
                raise_for_auth(response, url)
                response.raise_for_status()
                choice = response.json()["choices"][0]
                content = choice["message"].get("content")
                if content is None:
                    # 思考で出力の上限を使い切ったときなどの本文なし。再試行しても
                    # 同じ結果になりやすいので、原因が分かる文面で即座に止める。
                    raise VlmError(
                        f"応答に本文がありません（finish_reason={choice.get('finish_reason')}）"
                    )
                return content.strip()
            except AuthError as error:
                raise VlmError(str(error)) from error
            except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
                last_error = error
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(2**attempt)
        raise VlmError(
            f"{settings.vlm_url} への画像説明リクエストが{_MAX_ATTEMPTS}回失敗しました: {last_error}"
        )
    finally:
        if own_session:
            session.close()


def check_vlm(session=None) -> None:
    """取り込み開始前の疎通確認。モデル名の設定違いで460チャンクの処理が始まってから
    落ちるのを防ぐため、embedder.check_embedder() と同じ考え方で先に一度だけ確認する。
    """
    settings = load_vllm_settings()
    own_session = session is None
    session = session or new_session()
    try:
        try:
            served = list_model_ids(session, settings.vlm_url)
        except AuthError as error:
            raise VlmError(str(error)) from error
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            raise VlmError(
                f"vLLM（画像説明）に接続できません（{settings.vlm_url}）。"
                f"起動しているか確認してください: {error}"
            ) from error
        if settings.vlm_model not in served:
            raise VlmError(
                f"VLMモデル {settings.vlm_model} がサーバーにありません。"
                f"VLLM_VLM_MODEL を確かめてください（サーバーにあるモデル: {', '.join(served) or 'なし'}）。"
            )
    finally:
        if own_session:
            session.close()
