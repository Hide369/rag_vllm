"""vLLM（OpenAI 互換 API）への接続設定。

生成・埋め込み・画像説明の3つは別々の vLLM プロセスが受け持つため、URL も
モデル名も別々に持つ。設定は呼ぶたびに環境変数から読む。import 時に固定すると、
.env を読む前に取り込まれたときに設定が効かなくなる（Ollama 版で踏んだ問題）。
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass

import requests

_URL_DEFAULTS = {
    "VLLM_CHAT_URL": "http://127.0.0.1:8001",
    "VLLM_EMBED_URL": "http://127.0.0.1:8002",
    "VLLM_VLM_URL": "http://127.0.0.1:8003",
}

# Ollama 版の設定。黙って無視すると、利用者は変更が効いていると思ったまま既定の
# 宛先へ繋がってしまうため、入口で止める。
_LEGACY_VARIABLES = ("OLLAMA_HOST", "OLLAMA_API_KEY", "OLLAMA_VLM_MODEL")


@dataclass(frozen=True)
class VllmSettings:
    chat_url: str
    embed_url: str
    vlm_url: str
    chat_model: str
    embed_model: str
    vlm_model: str
    api_key: str


class AuthError(Exception):
    """認証の拒否（401/403）。各クライアントが自分のエラー型へ包み直す。"""


def _read_url(environ: Mapping[str, str], name: str) -> str:
    url = environ.get(name) or _URL_DEFAULTS[name]
    url = url.rstrip("/")
    # クライアントが /v1/... を自分で付ける。/v1 付きで受けると /v1/v1/... を
    # 黙って叩いて 404 になり、原因が URL の書き方だと気づきにくい。
    if url.endswith("/v1"):
        raise ValueError(
            f"{name} は /v1 を付けずに書いてください（/v1 はプログラム側で付けます）: {url}"
        )
    return url


def load_vllm_settings(environ: Mapping[str, str] = os.environ) -> VllmSettings:
    return VllmSettings(
        chat_url=_read_url(environ, "VLLM_CHAT_URL"),
        embed_url=_read_url(environ, "VLLM_EMBED_URL"),
        vlm_url=_read_url(environ, "VLLM_VLM_URL"),
        chat_model=environ.get("VLLM_CHAT_MODEL") or "gpt-oss-120b",
        embed_model=environ.get("VLLM_EMBED_MODEL") or "bge-m3",
        vlm_model=environ.get("VLLM_VLM_MODEL") or "qwen3-vl-30b-a3b",
        api_key=environ.get("VLLM_API_KEY") or "",
    )


def reject_legacy_env(environ: Mapping[str, str] = os.environ) -> None:
    for name in _LEGACY_VARIABLES:
        if environ.get(name):
            raise ValueError(
                ".env の OLLAMA_* は使われなくなりました。"
                f"VLLM_* へ移行してください（.env.example 参照）: {name}"
            )


def new_session(settings: VllmSettings | None = None) -> requests.Session:
    settings = settings or load_vllm_settings()
    session = requests.Session()
    if settings.api_key:
        session.headers["Authorization"] = f"Bearer {settings.api_key}"
    return session


def raise_for_auth(response: requests.Response, url: str) -> None:
    # 401/403 は何度送っても通らないので、呼び出し側が再試行へ回す前にここで止める。
    if response.status_code in (401, 403):
        raise AuthError(
            f"{url} が認証を拒否しました（HTTP {response.status_code}）。"
            "VLLM_API_KEY を確かめてください"
        )
