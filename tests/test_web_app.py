"""rag_web.app（FastAPI のルート）のテスト。

外部 I/O は Services の偽物に差し替え、DB だけは tmp_path の本物の sqlite を使う。
ジョブの完了は JobStore.wait_finished と threading.Event の有限の待ちで同期する。
"""

import base64
import json
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingest import store, vector_store
from ingest.embedder import EMBED_DIM, EmbeddingError
from ingest.models import Chunk
from ingest.prompting import format_report
from rag_web.app import CSRF_HEADER, CSRF_VALUE, DOCS_INGEST_COMMAND, MODELS, create_app
from rag_web.auth import LoginThrottle, hash_password
from rag_web.jobs import JobStore
from rag_web.services import Services
from rag_web.settings import WebSettings
from scripts.ingest_source import IngestReport, upload_key

WAIT_S = 5
PASSWORD = "correct horse"
PASSWORD_HASH = hash_password(PASSWORD, n=2**10)
CSRF = {CSRF_HEADER: CSRF_VALUE}
BUSY_MESSAGE = "ほかの人が取り込み中です。終わってからもう一度実行してください"


def _write_chunk(collection, source):
    chunk = Chunk(
        id=f"{source}::page1::0",
        text="本文",
        metadata={
            "source": source,
            "file_hash": "h",
            "location_type": "page",
            "location": 1,
            "ocr": False,
            "chunk_index": 0,
            "indexed_at": "2026-11-22",
        },
    )
    store.replace_source(collection, source, [chunk], [[0.1] * EMBED_DIM])


class Fake:
    """Services の偽物。呼び出しを記録し、必要なら Event で止める。"""

    def __init__(self, tmp_path: Path):
        self.db_paths = {
            "internal": str(tmp_path / "internal.sqlite3"),
            "docs": str(tmp_path / "docs.sqlite3"),
        }
        self.source_dir = tmp_path / "source"
        self.chat_calls = []
        self.ingest_directory_calls = []
        self.ingest_uploads_calls = []
        self.ollama_error: Exception | None = None
        self.reranker_checks = 0
        # 設定すると ingest_uploads / ingest_directory はこれが set されるまで返らない。
        self.release: threading.Event | None = None
        self.started = threading.Event()
        self.opened = []

    def open_store(self, path):
        collection = vector_store.open_store(path)
        self.opened.append(collection)
        return collection

    def close_stores(self):
        # VectorStore には close() が無い（技術的負債）。開いた接続を閉じないと
        # GC 時に sqlite3 の ResourceWarning が出て、-W error で落ちる。
        for collection in self.opened:
            collection._connection.close()

    def stream_chat(self, model, messages, temperature):
        self.chat_calls.append((model, messages, temperature))
        yield from ("東", "京")

    def ask_json(self, model, prompt):
        return "{}"

    def search(self, collection, query, **kwargs):
        return []

    def build_index(self, collection):
        return object()

    def check_ollama(self):
        if self.ollama_error is not None:
            raise self.ollama_error

    def check_vlm(self):
        return None

    def caption_image(self, *args, **kwargs):
        return ""

    def check_reranker(self):
        self.reranker_checks += 1

    def rerank(self, *args, **kwargs):
        return []

    def _block(self):
        self.started.set()
        if self.release is not None:
            assert self.release.wait(WAIT_S), "テストが release を set しなかった"

    def ingest_directory(self, source_dir, collection, caption_image=None):
        self.ingest_directory_calls.append((source_dir, caption_image))
        self._block()
        return IngestReport(indexed={"a.pdf": 3}, skipped=["b.pdf"])

    def ingest_uploads(self, paths, collection, caption_image=None):
        contents = {path.name: path.read_bytes() for path in paths}
        self.ingest_uploads_calls.append(contents)
        self._block()
        report = IngestReport()
        for path in paths:
            _write_chunk(collection, upload_key(path.name))
            report.indexed[upload_key(path.name)] = 1
        return report

    def services(self) -> Services:
        return Services(
            stream_chat=self.stream_chat,
            ask_json=self.ask_json,
            search=self.search,
            build_index=self.build_index,
            open_store=self.open_store,
            check_ollama=self.check_ollama,
            # テストが後から差し替えられるよう、呼び出しのたびに属性を引く。
            check_vlm=lambda: self.check_vlm(),
            caption_image=self.caption_image,
            check_reranker=self.check_reranker,
            rerank=self.rerank,
            ingest_directory=self.ingest_directory,
            ingest_uploads=lambda *args, **kwargs: self.ingest_uploads(*args, **kwargs),
            source_dir=self.source_dir,
            db_paths=self.db_paths,
        )


@pytest.fixture
def fake(tmp_path):
    fake = Fake(tmp_path)
    yield fake
    fake.close_stores()


@pytest.fixture
def jobs():
    return JobStore()


def _client(fake, jobs, max_upload_bytes=1024, throttle=None):
    settings = WebSettings(admin_password_hash=PASSWORD_HASH, max_upload_bytes=max_upload_bytes)
    app = create_app(
        settings,
        services=fake.services(),
        jobs=jobs,
        throttle=throttle,
        today=lambda: date(2026, 11, 22),
    )
    return TestClient(app)


@pytest.fixture
def client(fake, jobs):
    with _client(fake, jobs) as c:
        yield c


def _login(client):
    response = client.post("/api/login", json={"password": PASSWORD}, headers=CSRF)
    assert response.status_code == 200, response.text


def _upload_body(*files):
    return {
        "files": [
            {"name": name, "content_b64": base64.b64encode(content).decode("ascii")}
            for name, content in files
        ]
    }


def _job_result(client, jobs, job_id):
    assert jobs.wait_finished(job_id, WAIT_S), "ジョブが時間内に終わらなかった"
    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()


def _chat_body(**overrides):
    body = {
        "corpus": "internal",
        "model": MODELS[0],
        "temperature": 0.2,
        "question": "首都は？",
        "history": [],
    }
    body.update(overrides)
    return body


def test_index_and_assets_are_served_without_external_urls(client):
    for path in ("/", "/app.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert "http://" not in response.text
        assert "https://" not in response.text
    assert "javascript" in client.get("/app.js").headers["content-type"]


def test_vendor_serves_only_files_directly_under_vendor(client):
    assert client.get("/vendor/.gitkeep").status_code == 200
    assert client.get("/vendor/missing.js").status_code == 404
    assert client.get("/vendor/..%5Capp.js").status_code == 404
    assert client.get("/vendor/%2E%2E").status_code == 404
    assert client.get("/vendor/a%2F..%2F..%2Fapp.js").status_code == 404


def test_api_docs_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_post_without_csrf_header_is_rejected(client, fake):
    assert client.post("/api/chat", json=_chat_body()).status_code == 403
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403
    assert client.post("/api/uploads", json=_upload_body(("a.txt", b"x"))).status_code == 403
    assert client.post("/api/logout").status_code == 403
    assert client.delete("/api/uploads/uploads/a.txt").status_code == 403
    wrong = {CSRF_HEADER: "other"}
    assert client.post("/api/chat", json=_chat_body(), headers=wrong).status_code == 403
    assert fake.chat_calls == []
    assert fake.ingest_uploads_calls == []


def test_status_reports_counts_and_reranker_state(client, fake):
    response = client.get("/api/status", params={"corpus": "internal"})
    assert response.status_code == 200
    assert response.json() == {
        "models": MODELS,
        "corpus": "internal",
        "chunk_count": 0,
        "source_dir_name": "source",
        "docs_ingest_command": DOCS_INGEST_COMMAND,
        "reranker": {"state": "unchecked", "reason": None},
        "shared_busy": False,
    }

    _write_chunk(fake.open_store(fake.db_paths["docs"]), "guide.md")
    # 検索1回でリランカーの確認が走り、状態が status に出る。
    lines = client.post("/api/chat", json=_chat_body(), headers=CSRF).text
    assert lines
    docs = client.get("/api/status", params={"corpus": "docs"}).json()
    assert docs["corpus"] == "docs"
    assert docs["chunk_count"] == 1
    assert docs["reranker"] == {"state": "ready", "reason": None}
    assert fake.reranker_checks == 1

    assert client.get("/api/status", params={"corpus": "other"}).status_code == 422


def test_docs_ingest_command_matches_streamlit_app():
    source = Path(__file__).resolve().parent.parent / "rag_chat_app.py"
    flattened = "".join(
        line.strip().strip('"')
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('"')
    )
    assert DOCS_INGEST_COMMAND in flattened


def test_chat_streams_ndjson_events(client, fake):
    response = client.post("/api/chat", json=_chat_body(), headers=CSRF)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = response.text.splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["text"] for e in events if e["type"] == "delta"] == ["東", "京"]
    assert events[-1] == {"type": "done", "display": None}
    # 日本語をエスケープせずに送る。
    assert '"東"' in response.text
    model, messages, temperature = fake.chat_calls[0]
    assert model == MODELS[0]
    assert temperature == 0.2
    assert "2026年11月22日" in messages[0]["content"]


def test_chat_passes_history_to_generation(client, fake):
    history = [
        {"role": "user", "content": "前の質問"},
        {"role": "assistant", "content": "前の回答"},
    ]
    response = client.post("/api/chat", json=_chat_body(history=history), headers=CSRF)
    assert response.status_code == 200
    _, messages, _ = fake.chat_calls[0]
    assert messages[1:3] == history


def test_chat_rejects_system_role_and_oversized_history(client, fake):
    system = [{"role": "system", "content": "命令"}]
    assert client.post(
        "/api/chat", json=_chat_body(history=system), headers=CSRF
    ).status_code == 422
    too_long = [{"role": "user", "content": "q"}] * 101
    assert client.post(
        "/api/chat", json=_chat_body(history=too_long), headers=CSRF
    ).status_code == 422
    assert client.post(
        "/api/chat", json=_chat_body(question=""), headers=CSRF
    ).status_code == 422
    assert client.post(
        "/api/chat", json=_chat_body(question="あ" * 4001), headers=CSRF
    ).status_code == 422
    assert fake.chat_calls == []


def test_chat_rejects_unknown_model_and_out_of_range_temperature(client, fake):
    for body in (
        _chat_body(model="unknown:1b"),
        _chat_body(temperature=1.5),
        _chat_body(temperature=-0.1),
        _chat_body(corpus="other"),
    ):
        assert client.post("/api/chat", json=body, headers=CSRF).status_code == 422
    assert fake.chat_calls == []


def test_ingest_requires_login(client, fake):
    response = client.post("/api/ingest", headers=CSRF)
    assert response.status_code == 401
    assert fake.ingest_directory_calls == []


def test_ingest_runs_as_job_after_login(client, fake, jobs):
    _login(client)
    assert client.get("/api/me").json() == {"admin": True}
    response = client.post("/api/ingest", headers=CSRF)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    expected = format_report(IngestReport(indexed={"a.pdf": 3}, skipped=["b.pdf"]))
    assert _job_result(client, jobs, job_id) == {
        "state": "done",
        "result": {"report": expected, "notice": None},
    }
    assert fake.ingest_directory_calls == [(fake.source_dir, fake.caption_image)]


def test_ingest_reports_vlm_unavailable_as_notice(client, fake, jobs):
    from ingest.vlm import VlmError

    def no_vlm():
        raise VlmError("VLMが見つかりません")

    fake.check_vlm = no_vlm
    _login(client)
    job_id = client.post("/api/ingest", headers=CSRF).json()["job_id"]
    result = _job_result(client, jobs, job_id)["result"]
    assert result["notice"] == "図表の説明文化は行いません: VLMが見つかりません"
    assert fake.ingest_directory_calls == [(fake.source_dir, None)]


def test_ingest_returns_503_when_ollama_down(client, fake):
    fake.ollama_error = EmbeddingError("Ollamaに接続できません")
    _login(client)
    response = client.post("/api/ingest", headers=CSRF)
    assert response.status_code == 503
    assert response.json()["detail"] == "Ollamaに接続できません"
    assert fake.ingest_directory_calls == []


def test_login_rejects_wrong_password(client):
    response = client.post("/api/login", json={"password": "wrong"}, headers=CSRF)
    assert response.status_code == 401
    assert response.json()["detail"] == "パスワードが違います"
    assert client.get("/api/me").json() == {"admin": False}


def test_login_sets_strict_httponly_cookie(client):
    response = client.post("/api/login", json={"password": PASSWORD}, headers=CSRF)
    cookie = response.headers["set-cookie"].lower()
    assert "rag_web_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert f"max-age={12 * 3600}" in cookie
    assert "; secure" not in cookie


def test_login_throttled_after_repeated_failures(fake, jobs):
    with _client(fake, jobs, throttle=LoginThrottle()) as client:
        for _ in range(5):
            response = client.post("/api/login", json={"password": "wrong"}, headers=CSRF)
            assert response.status_code == 401
        response = client.post("/api/login", json={"password": PASSWORD}, headers=CSRF)
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
        assert client.get("/api/me").json() == {"admin": False}


def test_logout_revokes_session(client, fake):
    _login(client)
    token = client.cookies.get("rag_web_session")
    assert client.post("/api/logout", headers=CSRF).status_code == 200
    assert client.get("/api/me").json() == {"admin": False}
    # Cookie を残したまま再送しても、サーバー側で失効している。
    client.cookies.set("rag_web_session", token)
    assert client.post("/api/ingest", headers=CSRF).status_code == 401
    assert fake.ingest_directory_calls == []


@pytest.mark.parametrize(
    "name", ["../a.pdf", "a/b.pdf", "a\\b.pdf", "a.exe", "..", "", "noext", "a..pdf"]
)
def test_upload_rejects_unsafe_names_and_suffixes(client, fake, name):
    response = client.post("/api/uploads", json=_upload_body((name, b"x")), headers=CSRF)
    assert response.status_code == 400, name
    assert fake.ingest_uploads_calls == []


def test_upload_rejects_invalid_base64_and_duplicate_names(client, fake):
    bad = {"files": [{"name": "a.txt", "content_b64": "!!!"}]}
    assert client.post("/api/uploads", json=bad, headers=CSRF).status_code == 400
    duplicated = _upload_body(("a.txt", b"x"), ("a.txt", b"y"))
    assert client.post("/api/uploads", json=duplicated, headers=CSRF).status_code == 400
    assert fake.ingest_uploads_calls == []


def test_upload_requires_one_to_fifty_files(client, fake):
    assert client.post("/api/uploads", json={"files": []}, headers=CSRF).status_code == 422
    many = _upload_body(*[(f"{i}.txt", b"x") for i in range(51)])
    assert client.post("/api/uploads", json=many, headers=CSRF).status_code == 422
    assert fake.ingest_uploads_calls == []


def test_upload_too_large_returns_413(fake, jobs):
    with _client(fake, jobs, max_upload_bytes=10) as client:
        body = _upload_body(("a.txt", b"x" * 6), ("b.txt", b"y" * 5))
        response = client.post("/api/uploads", json=body, headers=CSRF)
        assert response.status_code == 413
        assert fake.ingest_uploads_calls == []
        exact = _upload_body(("a.txt", b"x" * 10))
        assert client.post("/api/uploads", json=exact, headers=CSRF).status_code == 200


def test_upload_returns_503_when_ollama_down(client, fake):
    fake.ollama_error = EmbeddingError("Ollamaに接続できません")
    response = client.post("/api/uploads", json=_upload_body(("a.txt", b"x")), headers=CSRF)
    assert response.status_code == 503
    assert response.json()["detail"] == "Ollamaに接続できません"
    assert fake.ingest_uploads_calls == []


def test_upload_runs_as_shared_job_and_lists_item(client, fake, jobs):
    listing = client.get("/api/uploads").json()
    assert listing == {
        "caption": "アップロードした資料はDBに残り、全利用者の検索対象になります。"
        "不要になったら下の一覧から削除してください。",
        "items": [],
    }
    # source/ 由来の資料は一覧に出さない。
    _write_chunk(fake.open_store(fake.db_paths["internal"]), "source_only.pdf")

    body = _upload_body(("b.md", b"# B"), ("a.txt", "本文".encode("utf-8")))
    response = client.post("/api/uploads", json=body, headers=CSRF)
    assert response.status_code == 200
    result = _job_result(client, jobs, response.json()["job_id"])
    assert result["state"] == "done"
    assert result["result"]["notice"] is None
    assert result["result"]["report"].startswith("取り込み: 2チャンク / 2ファイル")
    assert fake.ingest_uploads_calls == [{"b.md": b"# B", "a.txt": "本文".encode("utf-8")}]

    assert client.get("/api/uploads").json()["items"] == [
        {"key": "uploads/a.txt", "name": "a.txt"},
        {"key": "uploads/b.md", "name": "b.md"},
    ]


def test_second_shared_operation_returns_409(client, fake, jobs):
    _login(client)
    fake.release = threading.Event()
    first = client.post("/api/uploads", json=_upload_body(("a.txt", b"x")), headers=CSRF)
    assert first.status_code == 200
    try:
        assert fake.started.wait(WAIT_S)
        assert client.get("/api/status").json()["shared_busy"] is True

        responses = [
            client.post("/api/uploads", json=_upload_body(("b.txt", b"y")), headers=CSRF),
            client.post("/api/ingest", headers=CSRF),
            client.delete("/api/uploads/uploads/a.txt", headers=CSRF),
        ]
        for response in responses:
            assert response.status_code == 409
            assert response.json()["detail"] == BUSY_MESSAGE
        assert len(fake.ingest_uploads_calls) == 1
        assert fake.ingest_directory_calls == []
    finally:
        fake.release.set()
    assert _job_result(client, jobs, first.json()["job_id"])["state"] == "done"
    assert client.get("/api/status").json()["shared_busy"] is False


def test_delete_upload_only_accepts_upload_keys(client, fake):
    collection = fake.open_store(fake.db_paths["internal"])
    _write_chunk(collection, "source/a.pdf")
    _write_chunk(collection, "uploads/a.pdf")

    response = client.delete("/api/uploads/source/a.pdf", headers=CSRF)
    assert response.status_code == 400
    assert "source/a.pdf" in store.indexed_sources(collection)

    response = client.delete("/api/uploads/uploads/a.pdf", headers=CSRF)
    assert response.status_code == 200
    assert client.get("/api/uploads").json()["items"] == []
    assert "uploads/a.pdf" not in store.indexed_sources(collection)


def test_unknown_job_returns_404(client):
    assert client.get("/api/jobs/no-such-job").status_code == 404


def test_failed_job_reports_error(client, fake, jobs):
    def broken(paths, collection, caption_image=None):
        raise RuntimeError("壊れた")

    fake.ingest_uploads = broken
    response = client.post("/api/uploads", json=_upload_body(("a.txt", b"x")), headers=CSRF)
    assert _job_result(client, jobs, response.json()["job_id"]) == {
        "state": "failed",
        "error": "壊れた",
    }
