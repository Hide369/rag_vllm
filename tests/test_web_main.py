"""python -m rag_web の入口。uvicorn は偽物に差し替え、実際には起動しない。"""
import subprocess
import sys
from pathlib import Path

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


def test_importing_main_does_not_import_ingest():
    # ingest.embedder は OLLAMA_HOST をインポート時に読む。.env を読む前に取り込まれると
    # .env の OLLAMA_HOST が効かないため、入口のインポートだけでは ingest を読まない。
    # 同じプロセスでは他のテストが ingest を読み込み済みなので、別プロセスで確かめる。
    code = (
        "import sys, rag_web.__main__; "
        "loaded = sorted(m for m in sys.modules if m == 'ingest' or m.startswith('ingest.')); "
        "assert not loaded, loaded"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
