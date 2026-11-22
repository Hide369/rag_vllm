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
                 "RAG_WEB_COOKIE_SECURE"):
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
