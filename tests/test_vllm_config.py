"""vLLM 接続設定。実環境の環境変数には依存せず、environ を明示して渡す。"""
import pytest

from ingest.vllm_config import (
    AuthError,
    VllmSettings,
    load_vllm_settings,
    new_session,
    raise_for_auth,
    reject_legacy_env,
)


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def test_defaults_match_the_documented_table():
    settings = load_vllm_settings({})
    assert settings == VllmSettings(
        chat_url="http://127.0.0.1:8001",
        embed_url="http://127.0.0.1:8002",
        vlm_url="http://127.0.0.1:8003",
        chat_model="gpt-oss-120b",
        embed_model="bge-m3",
        vlm_model="qwen3-vl-30b-a3b",
        api_key="",
    )


def test_every_value_can_be_overridden():
    settings = load_vllm_settings(
        {
            "VLLM_CHAT_URL": "http://c:1",
            "VLLM_EMBED_URL": "http://e:2",
            "VLLM_VLM_URL": "http://v:3",
            "VLLM_CHAT_MODEL": "chat-m",
            "VLLM_EMBED_MODEL": "embed-m",
            "VLLM_VLM_MODEL": "vlm-m",
            "VLLM_API_KEY": "secret",
        }
    )
    assert settings == VllmSettings(
        "http://c:1", "http://e:2", "http://v:3", "chat-m", "embed-m", "vlm-m", "secret"
    )


def test_trailing_slash_is_removed():
    assert load_vllm_settings({"VLLM_CHAT_URL": "http://h:8001/"}).chat_url == "http://h:8001"


@pytest.mark.parametrize("url", ["http://h:8001/v1", "http://h:8001/v1/"])
def test_v1_suffix_is_rejected_with_the_variable_name(url):
    with pytest.raises(ValueError, match="VLLM_EMBED_URL"):
        load_vllm_settings({"VLLM_EMBED_URL": url})


def test_reject_legacy_env_names_the_variable_and_the_replacement():
    with pytest.raises(ValueError) as caught:
        reject_legacy_env({"OLLAMA_HOST": "http://x"})
    assert "OLLAMA_HOST" in str(caught.value)
    assert "VLLM_" in str(caught.value)


@pytest.mark.parametrize("name", ["OLLAMA_API_KEY", "OLLAMA_VLM_MODEL"])
def test_reject_legacy_env_covers_the_other_variables(name):
    with pytest.raises(ValueError, match=name):
        reject_legacy_env({name: "x"})


@pytest.mark.parametrize("environ", [{}, {"OLLAMA_HOST": ""}])
def test_reject_legacy_env_allows_unset_and_empty(environ):
    reject_legacy_env(environ)


def test_session_sends_bearer_token_when_key_is_set():
    settings = load_vllm_settings({"VLLM_API_KEY": "k"})
    assert new_session(settings).headers["Authorization"] == "Bearer k"


def test_session_has_no_authorization_without_a_key():
    assert "Authorization" not in new_session(load_vllm_settings({})).headers


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_raises_with_a_hint(status):
    with pytest.raises(AuthError) as caught:
        raise_for_auth(_Response(status), "http://h:8001/v1/chat/completions")
    message = str(caught.value)
    assert "VLLM_API_KEY" in message
    assert str(status) in message
    assert "http://h:8001/v1/chat/completions" in message


@pytest.mark.parametrize("status", [200, 500])
def test_other_statuses_are_left_to_the_caller(status):
    raise_for_auth(_Response(status), "http://h:8001")
