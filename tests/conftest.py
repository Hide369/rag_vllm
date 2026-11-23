"""全テスト共通の前提。"""
import pytest

_CONNECTION_VARIABLES = (
    "OLLAMA_HOST", "OLLAMA_API_KEY", "OLLAMA_VLM_MODEL",
    "VLLM_CHAT_URL", "VLLM_EMBED_URL", "VLLM_VLM_URL",
    "VLLM_CHAT_MODEL", "VLLM_EMBED_MODEL", "VLLM_VLM_MODEL", "VLLM_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_connection_env(monkeypatch):
    # 開発機の .env や環境変数に接続先の設定が残っていても、入口が古い設定を
    # 拒否してテストが落ちたり、宛先が混ざったりしないようにする。
    for name in _CONNECTION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
