"""python -m rag_web の入口。uvicorn は偽物に差し替え、実際には起動しない。"""

import pytest

import rag_web.__main__ as web_main
from rag_web.auth import hash_password


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    # 手元の .env が環境変数へ混ざらないようにする
    monkeypatch.setattr(web_main, "load_dotenv", lambda *a, **k: None)
    for name in ("RAG_WEB_ADMIN_PASSWORD_HASH", "RAG_WEB_HOST", "RAG_WEB_PORT",
                 "RAG_WEB_MAX_UPLOAD_MB", "RAG_WEB_SESSION_TTL_HOURS",
                 "RAG_WEB_COOKIE_SECURE", "RAG_WEB_FORWARDED_ALLOW_IPS",
                 "RAG_WEB_THREADPOOL_SIZE", "RAG_WEB_MAX_COWORK_JOBS"):
        monkeypatch.delenv(name, raising=False)


def test_main_reports_missing_hash_and_returns_2(capsys):
    assert web_main.main() == 2
    assert "RAG_WEB_ADMIN_PASSWORD_HASH" in capsys.readouterr().err


def test_main_reports_malformed_hash_and_returns_2(monkeypatch, capsys):
    monkeypatch.setenv("RAG_WEB_ADMIN_PASSWORD_HASH", "plain-text")
    assert web_main.main() == 2
    err = capsys.readouterr().err
    assert err.startswith("エラー: ")
    assert "RAG_WEB_ADMIN_PASSWORD_HASH" in err


def test_main_starts_uvicorn_with_settings(monkeypatch):
    monkeypatch.setenv("RAG_WEB_ADMIN_PASSWORD_HASH", hash_password("correct horse battery", n=2 ** 4))
    monkeypatch.setenv("RAG_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("RAG_WEB_PORT", "9000")
    calls = []
    monkeypatch.setattr(web_main.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))

    assert web_main.main() == 0

    assert len(calls) == 1
    app, kwargs = calls[0]
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9000
    assert app.title is not None


def _run_main(monkeypatch):
    monkeypatch.setenv("RAG_WEB_ADMIN_PASSWORD_HASH", hash_password("correct horse battery", n=2 ** 4))
    calls = []
    monkeypatch.setattr(web_main.uvicorn, "run", lambda app, **kw: calls.append(kw))
    assert web_main.main() == 0
    return calls[0]


def test_main_trusts_proxy_headers_from_localhost_by_default(monkeypatch):
    kwargs = _run_main(monkeypatch)
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "127.0.0.1"


def test_main_ignores_proxy_headers_when_allow_list_is_empty(monkeypatch):
    monkeypatch.setenv("RAG_WEB_FORWARDED_ALLOW_IPS", "")
    kwargs = _run_main(monkeypatch)
    assert kwargs["proxy_headers"] is False
    assert kwargs["forwarded_allow_ips"] is None


def test_main_refuses_to_start_with_a_legacy_ollama_setting(monkeypatch, capsys):
    monkeypatch.setenv("RAG_WEB_ADMIN_PASSWORD_HASH", hash_password("correct horse battery", n=2 ** 4))
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    calls = []
    monkeypatch.setattr(web_main.uvicorn, "run", lambda app, **kw: calls.append(kw))

    assert web_main.main() == 2

    err = capsys.readouterr().err
    assert err.startswith("エラー: ")
    assert "OLLAMA_HOST" in err
    assert "VLLM_" in err
    assert calls == []


def test_main_gives_the_chat_model_from_the_environment_to_the_app(monkeypatch):
    monkeypatch.setenv("RAG_WEB_ADMIN_PASSWORD_HASH", hash_password("correct horse battery", n=2 ** 4))
    monkeypatch.setenv("VLLM_CHAT_MODEL", "custom-chat")
    seen = {}
    monkeypatch.setattr(
        web_main.uvicorn, "run", lambda app, **kw: seen.update(app=app)
    )
    real_create_app = web_main.create_app

    def spy(settings, **kwargs):
        seen["chat_model"] = kwargs.get("chat_model")
        return real_create_app(settings, **kwargs)

    monkeypatch.setattr(web_main, "create_app", spy)

    assert web_main.main() == 0
    assert seen["chat_model"] == "custom-chat"
