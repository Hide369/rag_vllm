"""rag_web.app（FastAPI のルート）のテスト。

外部 I/O は Services の偽物に差し替え、DB だけは tmp_path の本物の sqlite を使う。
ジョブの完了は JobStore.wait_finished と threading.Event の有限の待ちで同期する。
"""

import base64
import json
import re
import threading
from datetime import date
from pathlib import Path

import pytest
import anyio.to_thread
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import docgen
from ingest import store, vector_store
from ingest.embedder import EMBED_DIM, EmbeddingError
from ingest.models import Chunk
from ingest.parsers import SUPPORTED_SUFFIXES
from ingest.prompting import format_report
from rag_web.app import CSRF_HEADER, CSRF_VALUE, DOCS_INGEST_COMMAND, STATIC_DIR, create_app
from rag_web.auth import LoginThrottle, hash_password
from rag_web.cowork import NO_TEMPLATE_LABEL, is_source_suffix, output_suffixes
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
        self.embedder_error: Exception | None = None
        self.reranker_checks = 0
        # 設定すると ingest_uploads / ingest_directory はこれが set されるまで返らない。
        self.release: threading.Event | None = None
        self.started = threading.Event()
        self.opened = []
        self.templates_dir = tmp_path / "templates"
        self.ask_json_calls = []
        self.ask_json_reply = "{}"
        self.ask_text_calls = []
        self.parse_calls = []
        # 設定すると ask_json はこれが set されるまで返らない（生成の途中で止める）。
        self.ask_release: threading.Event | None = None
        self.ask_started = threading.Event()
        # 雛形なしの生成（ask_text）を止める。Cowork の同時実行数のテスト用。
        self.text_release: threading.Event | None = None
        self.text_started = threading.Event()

    def open_store(self, path):
        collection = vector_store.open_store(path)
        self.opened.append(collection)
        return collection

    def close_stores(self):
        # 開いた接続を閉じないと
        # GC 時に sqlite3 の ResourceWarning が出て、-W error で落ちる。
        for collection in self.opened:
            collection.close()

    def stream_chat(self, model, messages, temperature):
        self.chat_calls.append((model, messages, temperature))
        yield from ("東", "京")

    def ask_json(self, model, prompt):
        self.ask_json_calls.append((model, prompt))
        self.ask_started.set()
        if self.ask_release is not None:
            assert self.ask_release.wait(WAIT_S), "テストが ask_release を set しなかった"
        return self.ask_json_reply

    def ask_text(self, model, prompt):
        self.ask_text_calls.append((model, prompt))
        self.text_started.set()
        if self.text_release is not None:
            assert self.text_release.wait(WAIT_S), "テストが text_release を set しなかった"
        return "# 文書\n\n本文\n"

    def parse(self, path, caption_image=None):
        self.parse_calls.append(path.name)
        return []

    def search(self, collection, query, **kwargs):
        return []

    def build_index(self, collection):
        return object()

    def check_embedder(self):
        if self.embedder_error is not None:
            raise self.embedder_error

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
            ask_text=self.ask_text,
            parse=self.parse,
            search=self.search,
            build_index=self.build_index,
            open_store=self.open_store,
            check_embedder=self.check_embedder,
            # テストが後から差し替えられるよう、呼び出しのたびに属性を引く。
            check_vlm=lambda: self.check_vlm(),
            caption_image=self.caption_image,
            check_reranker=self.check_reranker,
            rerank=self.rerank,
            ingest_directory=self.ingest_directory,
            ingest_uploads=lambda *args, **kwargs: self.ingest_uploads(*args, **kwargs),
            source_dir=self.source_dir,
            db_paths=self.db_paths,
            templates_dir=self.templates_dir,
        )


@pytest.fixture
def jobs():
    return JobStore()


@pytest.fixture
def fake(tmp_path, jobs):
    fake = Fake(tmp_path)
    yield fake
    # ジョブのスレッドが sqlite に書いている途中で接続を閉じると落ちるため、
    # 全ジョブの終了を待ってから閉じる。終わらなければ黙って閉じずに失敗させる。
    idle = jobs.wait_idle(WAIT_S)
    assert idle, "ジョブが終わらないまま teardown に来た（接続を閉じられない）"
    fake.close_stores()


# create_app へ明示的に渡す生成モデル名。環境変数 VLLM_CHAT_MODEL に依存させない。
CHAT_MODEL = "test-chat-model"


def _client(fake, jobs, max_upload_bytes=1024, throttle=None, **settings_overrides):
    settings = WebSettings(
        admin_password_hash=PASSWORD_HASH, max_upload_bytes=max_upload_bytes,
        **settings_overrides,
    )
    app = create_app(
        settings,
        services=fake.services(),
        jobs=jobs,
        throttle=throttle,
        today=lambda: date(2026, 11, 22),
        chat_model=CHAT_MODEL,
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


def test_vendor_assets_are_served_and_referenced_locally(client):
    # 画面は外部の CDN を使わない。Markdown 描画・サニタイズ・Mermaid の3つを
    # 同梱し、index.html と app.js が参照する /vendor/*.js がすべて返ることを確かめる
    # （mermaid は大きいので app.js が必要になったときに読み込む）。
    referenced = set()
    for path in ("/", "/app.js"):
        referenced |= set(re.findall(r"/vendor/([\w.-]+\.js)", client.get(path).text))
    for library in ("marked", "purify", "mermaid"):
        assert any(name.startswith(f"{library}-") for name in referenced), library
    for name in referenced:
        response = client.get(f"/vendor/{name}")
        assert response.status_code == 200, name
        assert response.content, name


def test_index_declares_content_security_policy(client):
    # 回答に仕込まれた外部 URL（SVG の image、背景属性など）をサニタイズが取りこぼしても、
    # ブラウザが外へ取りに行かないよう CSP で同一オリジンに閉じる。
    match = re.search(
        r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">',
        client.get("/").text,
    )
    assert match, "index.html に CSP の meta が無い"
    directives = {
        part.split()[0]: part.split()[1:]
        for part in match.group(1).split(";")
        if part.strip()
    }
    assert directives["default-src"] == ["'self'"]
    assert directives["connect-src"] == ["'self'"]
    assert directives["script-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]


@pytest.mark.parametrize("path", ["/", "/api/me", "/api/status", "/api/jobs/missing"])
def test_security_headers_on_every_response(client, path):
    headers = client.get(path).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"] == "frame-ancestors 'none'"


def test_security_headers_on_streamed_chat(client):
    response = client.post("/api/chat", json=_chat_body(), headers=CSRF)
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


def test_index_disables_dns_prefetch(client):
    assert '<meta http-equiv="x-dns-prefetch-control" content="off">' in client.get("/").text


def test_upload_accept_list_matches_supported_suffixes(client):
    # 対応拡張子を API で返していないため、画面側に写しを持つ。食い違うと、選べない
    # 資料が出るか、選べても 400 で弾かれる。
    script = client.get("/app.js").text
    match = re.search(r"const UPLOAD_SUFFIXES = \[(.*?)\];", script, re.DOTALL)
    assert match, "app.js に UPLOAD_SUFFIXES が無い"
    assert set(re.findall(r'"(\.[\w]+)"', match.group(1))) == SUPPORTED_SUFFIXES


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
    # ログイン済みでもヘッダーが無ければ取り込みは走らない。
    _login(client)
    assert client.post("/api/ingest").status_code == 403
    assert fake.chat_calls == []
    assert fake.ingest_uploads_calls == []
    assert fake.ingest_directory_calls == []


def test_status_reports_counts_and_reranker_state(client, fake):
    response = client.get("/api/status", params={"corpus": "internal"})
    assert response.status_code == 200
    assert response.json() == {
        "corpus": "internal",
        "chunk_count": 0,
        # 取り込み元のフォルダ名はサーバーの内部の事情なので、管理者にしか返さない
        "source_dir_name": None,
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


def test_status_reports_source_dir_name_only_to_admin(client, fake):
    assert client.get("/api/status").json()["source_dir_name"] is None

    _login(client)
    assert client.get("/api/status").json()["source_dir_name"] == "source"

    client.post("/api/logout", headers=CSRF)
    assert client.get("/api/status").json()["source_dir_name"] is None


def _record_other_embedding_model(path):
    seed = vector_store.open_store(path)
    seed.set_meta_if_absent("embedding_model", "qwen3-embedding")
    seed.set_meta_if_absent("embedding_dim", "2560")
    seed.close()


def test_a_mismatched_db_shows_a_readable_message_not_a_500(client, fake):
    """設定側の食い違いは利用者が直せる。内部エラーの定型文にしない。"""
    _record_other_embedding_model(fake.db_paths["internal"])

    response = client.get("/api/status", params={"corpus": "internal"})

    assert response.status_code == 503
    assert "qwen3-embedding" in response.json()["detail"]
    assert "VLLM_EMBED_MODEL" in response.json()["detail"]


def test_a_mismatched_db_reaches_the_chat_screen_as_an_error_event(client, fake):
    _record_other_embedding_model(fake.db_paths["internal"])

    text = client.post("/api/chat", json=_chat_body(), headers=CSRF).text

    assert "qwen3-embedding" in text
    assert '"type": "error"' in text


def test_status_of_an_empty_docs_corpus_carries_the_ingest_command(client):
    """技術ドキュメントが0件のとき、画面が警告に出す取り込みコマンドを status が返す。

    open_store はパスを間違えても例外を出さず空の DB を新規作成する。件数0のまま
    黙って検索させず、取り込みに使う2つのコマンドを画面が示せるようにする。
    """
    docs = client.get("/api/status", params={"corpus": "docs"}).json()
    assert docs["chunk_count"] == 0
    assert "scripts.fetch_docs" in docs["docs_ingest_command"]
    assert "scripts.ingest_source" in docs["docs_ingest_command"]


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
    assert model == CHAT_MODEL
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
        "/api/chat", json=_chat_body(question="あ" * 100_001), headers=CSRF
    ).status_code == 422
    assert fake.chat_calls == []


def test_chat_accepts_long_question(client, fake):
    # 資料を貼って尋ねる長さは通す。
    response = client.post("/api/chat", json=_chat_body(question="あ" * 4001), headers=CSRF)
    assert response.status_code == 200
    assert json.loads(response.text.splitlines()[-1])["type"] == "done"
    assert len(fake.chat_calls) == 1


def test_chat_rejects_out_of_range_temperature_and_unknown_corpus(client, fake):
    for body in (
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


def test_ingest_returns_503_when_embedder_down(client, fake):
    fake.embedder_error = EmbeddingError("vLLM（埋め込み）に接続できません")
    _login(client)
    response = client.post("/api/ingest", headers=CSRF)
    assert response.status_code == 503
    assert response.json()["detail"] == "vLLM（埋め込み）に接続できません"
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


def test_logout_clears_cookie_with_same_attributes(client):
    _login(client)
    response = client.post("/api/logout", headers=CSRF)
    cookie = response.headers["set-cookie"].lower()
    assert "rag_web_session=" in cookie
    assert "max-age=0" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert "; secure" not in cookie


def test_logout_cookie_is_secure_when_configured(fake, jobs):
    settings = WebSettings(admin_password_hash=PASSWORD_HASH, cookie_secure=True)
    app = create_app(settings, services=fake.services(), jobs=jobs)
    # Secure の Cookie は http の TestClient には送り返されないため、ログインを経ずに確かめる。
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/api/logout", headers=CSRF)
    cookie = response.headers["set-cookie"].lower()
    assert "; secure" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_login_throttled_after_repeated_failures(fake, jobs):
    with _client(fake, jobs, throttle=LoginThrottle()) as client:
        for _ in range(5):
            response = client.post("/api/login", json={"password": "wrong"}, headers=CSRF)
            assert response.status_code == 401
        response = client.post("/api/login", json={"password": PASSWORD}, headers=CSRF)
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
        assert client.get("/api/me").json() == {"admin": False}


def _behind_proxy(fake, jobs, trusted_hosts):
    """__main__ が uvicorn に任せる設定と同じ trusted_hosts で、アプリを包む。"""
    settings = WebSettings(admin_password_hash=PASSWORD_HASH)
    app = create_app(
        settings, services=fake.services(), jobs=jobs, throttle=LoginThrottle(),
        today=lambda: date(2026, 11, 22),
    )
    return TestClient(ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts))


def _login_as(client, forwarded_for, password):
    return client.post(
        "/api/login", json={"password": password},
        headers={**CSRF, "X-Forwarded-For": forwarded_for},
    )


def test_login_throttle_uses_forwarded_client_from_trusted_proxy(fake, jobs):
    # TestClient の接続元は "testclient"。これを信頼するプロキシとして扱う。
    with _behind_proxy(fake, jobs, "testclient") as client:
        for _ in range(5):
            assert _login_as(client, "10.0.0.5", "wrong").status_code == 401
        assert _login_as(client, "10.0.0.5", PASSWORD).status_code == 429
        # 別の利用者は、同じプロキシ経由でも巻き添えにならない。
        assert _login_as(client, "10.0.0.6", PASSWORD).status_code == 200


def test_forwarded_header_from_untrusted_peer_does_not_change_client(fake, jobs):
    # 信頼しない接続元が X-Forwarded-For を偽っても、接続元は書き換わらない。
    # 書き換わるなら、送り先を変えるだけでログインの試行回数制限を逃れられる。
    with _behind_proxy(fake, jobs, "127.0.0.1") as client:
        for index in range(5):
            assert _login_as(client, f"10.0.1.{index}", "wrong").status_code == 401
        assert _login_as(client, "10.0.1.99", PASSWORD).status_code == 429


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
    "name", ["../a.pdf", "a/b.pdf", "a\\b.pdf", "a.exe", "..", ".", "", "noext"]
)
def test_upload_rejects_unsafe_names_and_suffixes(client, fake, name):
    response = client.post("/api/uploads", json=_upload_body((name, b"x")), headers=CSRF)
    assert response.status_code == 400, name
    assert fake.ingest_uploads_calls == []


def test_upload_accepts_consecutive_dots_inside_a_name(client, fake, jobs):
    # ".." を含むだけでは拒まない。区切りが無ければ外へは出られない。
    response = client.post("/api/uploads", json=_upload_body(("v1..2.md", b"# v")), headers=CSRF)
    assert response.status_code == 200, response.text
    assert _job_result(client, jobs, response.json()["job_id"])["state"] == "done"
    assert fake.ingest_uploads_calls == [{"v1..2.md": b"# v"}]


def test_upload_rejects_invalid_base64_and_duplicate_names(client, fake):
    bad = {"files": [{"name": "a.txt", "content_b64": "!!!"}]}
    assert client.post("/api/uploads", json=bad, headers=CSRF).status_code == 400
    duplicated = _upload_body(("a.txt", b"x"), ("a.txt", b"y"))
    assert client.post("/api/uploads", json=duplicated, headers=CSRF).status_code == 400
    # Windows の一時フォルダでは大文字小文字違いも同じファイルになる。
    case_only = _upload_body(("A.txt", b"x"), ("a.txt", b"y"))
    assert client.post("/api/uploads", json=case_only, headers=CSRF).status_code == 400
    assert fake.ingest_uploads_calls == []


def test_upload_rejects_overlong_name(client, fake):
    long_name = "a" * 252 + ".txt"  # 256文字
    response = client.post("/api/uploads", json=_upload_body((long_name, b"x")), headers=CSRF)
    assert response.status_code == 422
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
        response = client.post("/api/uploads", json=exact, headers=CSRF)
        assert response.status_code == 200
        assert _job_result(client, jobs, response.json()["job_id"])["state"] == "done"


def test_upload_returns_503_when_ollama_down(client, fake):
    fake.embedder_error = EmbeddingError("vLLM（埋め込み）に接続できません")
    response = client.post("/api/uploads", json=_upload_body(("a.txt", b"x")), headers=CSRF)
    assert response.status_code == 503
    assert response.json()["detail"] == "vLLM（埋め込み）に接続できません"
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


def test_uploaded_document_really_reaches_the_store(client, fake, jobs, monkeypatch):
    """実際の取り込み処理（解析・チャンク・格納）を通し、DB に入って一覧に出るまでを見る。

    他のアップロードのテストは ingest_uploads を偽物に差し替えており、配線しか見ていない。
    解析から格納まで本物を通すのはここだけである。埋め込みの HTTP 呼び出しだけは固定の
    ベクトルに差し替える（Ollama に依存させない）。
    """
    from ingest import embedder
    from scripts import ingest_source

    monkeypatch.setattr(
        embedder, "embed_texts", lambda texts, session=None: [[0.1] * EMBED_DIM for _ in texts]
    )
    fake.ingest_uploads = ingest_source.ingest_uploads
    markdown = "# 議題\n\n取り込みUIの設計を決めた。\n"
    body = _upload_body(("持ち込み.md", markdown.encode("utf-8")))
    response = client.post("/api/uploads", json=body, headers=CSRF)
    assert response.status_code == 200, response.text
    result = _job_result(client, jobs, response.json()["job_id"])
    assert result["state"] == "done", result
    collection = fake.open_store(fake.db_paths["internal"])
    assert store.indexed_sources(collection) == {"uploads/持ち込み.md"}
    assert client.get("/api/uploads").json()["items"] == [
        {"key": "uploads/持ち込み.md", "name": "持ち込み.md"}
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


def test_delete_unknown_upload_returns_404(client, jobs):
    response = client.delete("/api/uploads/uploads/missing.pdf", headers=CSRF)
    assert response.status_code == 404
    assert response.json()["detail"] == "その資料は見つかりません"
    # 404 で抜けても共有ロックは解放されている。
    assert jobs.shared_busy() is False


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


def test_chat_ignores_client_supplied_model(client, fake):
    # モデルは画面から選ばせない。古い画面が送ってきても、サーバーの CHAT_MODEL で答える。
    response = client.post("/api/chat", json=_chat_body(model="unknown:1b"), headers=CSRF)
    assert response.status_code == 200
    model, _messages, _temperature = fake.chat_calls[0]
    assert model == CHAT_MODEL


def test_index_has_no_model_picker():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="model"' not in html
    assert "モデル名" not in html


def test_index_has_cowork_controls():
    # チャット / Cowork の切り替え、Cowork の入力欄、フォーマットファイルのダイアログ。
    # 見出し・ラベルの文言を固定する。
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for element_id in (
        "mode-chat", "mode-cowork", "cowork-panel", "cowork-template", "open-templates",
        "cowork-output-wrap", "cowork-output-suffix", "cowork-internal", "cowork-docs",
        "cowork-files", "cowork-pasted", "cowork-result",
        "template-dialog", "template-caption", "template-files", "run-template-register",
        "template-error", "template-list", "close-templates",
    ):
        assert f'id="{element_id}"' in html, element_id
    for label in (
        "フォーマットファイル", "フォーマットファイルを登録・削除", "出力形式",
        "社内資料を参照", "技術ドキュメントを参照",
        "添付（この回だけ使い、DBには入れません）",
        "テキストを貼り付け（この回だけ使い、DBには入れません）",
    ):
        assert label in html, label
    # 雛形なしのラベル・拡張子は /api/cowork/options から取り、画面側に写しを持たない
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert NO_TEMPLATE_LABEL not in script
    assert "/api/cowork/options" in script


# --- Cowork とフォーマットファイル --------------------------------------------------------

TEMPLATE_CAPTION = (
    "フォーマットファイルの空欄は {{会議名}} のように書いてください。"
    "登録したフォーマットファイルはこのマシンの templates/ に残ります。"
)
NO_SUCH_TEMPLATE = "そのフォーマットファイルはありません"


def _template_file(fake, name="議事録.md", content="# {{会議名}}\n\n{{決定事項}}\n"):
    fake.templates_dir.mkdir(parents=True, exist_ok=True)
    path = fake.templates_dir / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return name


def _cowork_body(**overrides):
    body = {
        "question": "第5回会議の議事録を作って",
        "template": None,
        "output_suffix": ".md",
        "use_internal": False,
        "use_docs": False,
        "pasted": "会議の記録",
        "attachments": [],
    }
    body.update(overrides)
    return body


def _cowork_result(client, jobs, body):
    response = client.post("/api/cowork", json=body, headers=CSRF)
    assert response.status_code == 200, response.text
    outcome = _job_result(client, jobs, response.json()["job_id"])
    assert outcome["state"] == "done", outcome
    return outcome["result"]


def test_cowork_options_lists_formats_and_templates(client, fake):
    _template_file(fake)
    options = client.get("/api/cowork/options").json()
    assert options == {
        "no_template_label": NO_TEMPLATE_LABEL,
        "output_suffixes": output_suffixes(),
        "source_suffixes": [s for s in output_suffixes() if is_source_suffix(s)],
        "attachment_suffixes": sorted(SUPPORTED_SUFFIXES),
        "template_suffixes": sorted(docgen.SUPPORTED_SUFFIXES),
        "templates": [
            {"name": "議事録.md", "placeholders": ["会議名", "決定事項"], "error": None}
        ],
    }
    assert options["source_suffixes"], "ソースコードの出力形式が1つも無い"


def test_cowork_runs_as_job_and_returns_download(client, fake, jobs):
    result = _cowork_result(client, jobs, _cowork_body())
    assert result["errors"] == []
    assert result["download"]["file_name"] == "文書_2026-11-22.md"
    text = base64.b64decode(result["download"]["content_b64"]).decode("utf-8")
    assert text.startswith("# 文書")
    assert [call[0] for call in fake.ask_text_calls] == [CHAT_MODEL]

    name = _template_file(fake)
    fake.ask_json_reply = json.dumps({"会議名": "第5回"}, ensure_ascii=False)
    result = _cowork_result(client, jobs, _cowork_body(template=name))
    assert result["download"]["file_name"] == "議事録_2026-11-22.md"
    text = base64.b64decode(result["download"]["content_b64"]).decode("utf-8")
    assert "# 第5回" in text
    assert fake.ask_json_calls[-1][0] == CHAT_MODEL


def test_cowork_requires_csrf_header(client, fake):
    assert client.post("/api/cowork", json=_cowork_body()).status_code == 403
    wrong = {CSRF_HEADER: "other"}
    assert client.post("/api/cowork", json=_cowork_body(), headers=wrong).status_code == 403
    assert fake.ask_text_calls == []


def test_cowork_rejects_unknown_output_suffix_and_template(client, fake):
    response = client.post(
        "/api/cowork", json=_cowork_body(output_suffix=".exe"), headers=CSRF
    )
    assert response.status_code == 422
    response = client.post(
        "/api/cowork", json=_cowork_body(template="無い.md"), headers=CSRF
    )
    assert response.status_code == 400
    assert response.json()["detail"] == NO_SUCH_TEMPLATE
    # 一覧から引くので、templates/ の外を指す名前も「無い」になる。
    _template_file(fake)
    response = client.post(
        "/api/cowork", json=_cowork_body(template="../templates/議事録.md"), headers=CSRF
    )
    assert response.status_code == 400
    assert response.json()["detail"] == NO_SUCH_TEMPLATE
    for body in (_cowork_body(question=""), _cowork_body(pasted="あ" * 100_001)):
        assert client.post("/api/cowork", json=body, headers=CSRF).status_code == 422
    too_many = _cowork_body(
        attachments=_upload_body(*[(f"{i}.txt", b"x") for i in range(21)])["files"]
    )
    assert client.post("/api/cowork", json=too_many, headers=CSRF).status_code == 422
    assert fake.ask_json_calls == []
    assert fake.ask_text_calls == []


def test_cowork_attachment_accepts_consecutive_dots_inside_a_name(client, fake, jobs):
    body = _cowork_body(attachments=_upload_body(("v1..2.md", b"# v"))["files"])
    result = _cowork_result(client, jobs, body)
    assert result["errors"] == []
    assert fake.parse_calls == ["v1..2.md"]


@pytest.mark.parametrize("name", ["../a.pdf", "a/b.pdf", "a\\b.pdf", "a.exe", "..", "."])
def test_cowork_attachment_checks_match_uploads(client, fake, name):
    body = _cowork_body(attachments=_upload_body((name, b"x"))["files"])
    assert client.post("/api/cowork", json=body, headers=CSRF).status_code == 400, name
    assert fake.parse_calls == []
    assert fake.ask_text_calls == []


def test_cowork_attachment_duplicates_and_total_size(fake, jobs):
    with _client(fake, jobs, max_upload_bytes=10) as client:
        case_only = _cowork_body(
            attachments=_upload_body(("A.txt", b"x"), ("a.txt", b"y"))["files"]
        )
        assert client.post("/api/cowork", json=case_only, headers=CSRF).status_code == 400
        too_large = _cowork_body(
            attachments=_upload_body(("a.txt", b"x" * 6), ("b.txt", b"y" * 5))["files"]
        )
        response = client.post("/api/cowork", json=too_large, headers=CSRF)
        assert response.status_code == 413
        assert fake.parse_calls == []
        exact = _cowork_body(attachments=_upload_body(("a.txt", b"x" * 10))["files"])
        result = _cowork_result(client, jobs, exact)
        assert result["errors"] == []
        assert fake.parse_calls == ["a.txt"]


def test_cowork_is_not_blocked_by_shared_operation(client, fake, jobs):
    fake.release = threading.Event()
    upload = client.post("/api/uploads", json=_upload_body(("a.txt", b"x")), headers=CSRF)
    assert upload.status_code == 200
    try:
        assert fake.started.wait(WAIT_S)
        assert jobs.shared_busy() is True
        response = client.post("/api/cowork", json=_cowork_body(), headers=CSRF)
        assert response.status_code == 200
        assert _job_result(client, jobs, response.json()["job_id"])["state"] == "done"
    finally:
        fake.release.set()
    assert _job_result(client, jobs, upload.json()["job_id"])["state"] == "done"


def test_cowork_template_removed_during_generation_reports_error(client, fake, jobs):
    name = _template_file(fake)
    fake.ask_json_reply = json.dumps({"会議名": "第5回"}, ensure_ascii=False)
    fake.ask_release = threading.Event()
    response = client.post("/api/cowork", json=_cowork_body(template=name), headers=CSRF)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    try:
        # 印を読み終え、LLM の応答を待っている間に別の人が消す。
        assert fake.ask_started.wait(WAIT_S)
        deleted = client.delete(f"/api/templates/{name}", headers=CSRF)
        assert deleted.status_code == 200
        assert not (fake.templates_dir / name).exists()
    finally:
        fake.ask_release.set()
    outcome = _job_result(client, jobs, job_id)
    assert outcome["state"] == "done"
    assert outcome["result"]["errors"][0].startswith(f"{name} を開けませんでした: ")
    assert outcome["result"]["download"] is None


def test_template_register_and_list(client, fake):
    assert client.get("/api/templates").json() == {"caption": TEMPLATE_CAPTION, "items": []}
    body = _upload_body(
        ("議事録.md", "# {{会議名}}\n\n{{決定事項}}\n".encode("utf-8")),
        ("空.md", "印なし\n".encode("utf-8")),
    )
    response = client.post("/api/templates", json=body, headers=CSRF)
    assert response.status_code == 200
    assert response.json() == {"registered": 2}
    assert client.get("/api/templates").json() == {
        "caption": TEMPLATE_CAPTION,
        "items": [
            {"name": "空.md", "placeholders": [], "error": None},
            {"name": "議事録.md", "placeholders": ["会議名", "決定事項"], "error": None},
        ],
    }
    # 同名は上書き（差し替え）。
    replaced = _upload_body(("議事録.md", "{{日付}}\n".encode("utf-8")))
    response = client.post("/api/templates", json=replaced, headers=CSRF)
    assert response.json() == {"registered": 1}
    items = client.get("/api/templates").json()["items"]
    assert items[1] == {"name": "議事録.md", "placeholders": ["日付"], "error": None}


def test_template_register_and_delete_accept_consecutive_dots_inside_a_name(client, fake):
    response = client.post(
        "/api/templates", json=_upload_body(("v1..2.md", b"# {{x}}")), headers=CSRF
    )
    assert response.status_code == 200, response.text
    assert [p.name for p in fake.templates_dir.iterdir()] == ["v1..2.md"]
    assert client.delete("/api/templates/v1..2.md", headers=CSRF).status_code == 200
    assert list(fake.templates_dir.iterdir()) == []


@pytest.mark.parametrize("name", ["../x.md", "a/b.md", "a\\b.md", "x.exe", "..", ".", ""])
def test_template_register_rejects_unsafe_names(client, fake, tmp_path, name):
    response = client.post(
        "/api/templates", json=_upload_body((name, b"# {{x}}")), headers=CSRF
    )
    assert response.status_code == 400, name
    assert not (tmp_path / "x.md").exists()
    assert not (tmp_path / "b.md").exists()
    assert not fake.templates_dir.exists() or not any(fake.templates_dir.iterdir())


def test_template_register_requires_one_to_twenty_files_and_csrf(client, fake):
    assert client.post("/api/templates", json={"files": []}, headers=CSRF).status_code == 422
    many = _upload_body(*[(f"{i}.md", b"x") for i in range(21)])
    assert client.post("/api/templates", json=many, headers=CSRF).status_code == 422
    one = _upload_body(("a.md", b"x"))
    assert client.post("/api/templates", json=one).status_code == 403
    name = _template_file(fake)
    assert client.delete(f"/api/templates/{name}").status_code == 403
    assert sorted(p.name for p in fake.templates_dir.iterdir()) == [name]


def test_template_register_too_large_returns_413(fake, jobs):
    with _client(fake, jobs, max_upload_bytes=10) as client:
        body = _upload_body(("a.md", b"x" * 6), ("b.md", b"y" * 5))
        assert client.post("/api/templates", json=body, headers=CSRF).status_code == 413
        assert not fake.templates_dir.exists()


@pytest.mark.parametrize("encoded", ["..%2Fx.md", "a%2F..%2F..%2Fx.md", "a%5Cx.md", "%2E%2E"])
def test_template_delete_rejects_unsafe_names(client, fake, tmp_path, encoded):
    _template_file(fake, name="x.md")
    outside = tmp_path / "x.md"
    outside.write_text("外", encoding="utf-8")
    response = client.delete(f"/api/templates/{encoded}", headers=CSRF)
    assert response.status_code == 400, encoded
    assert outside.read_text(encoding="utf-8") == "外"
    assert (fake.templates_dir / "x.md").exists()


def test_template_delete_in_use_returns_409_and_releases_lock(client, fake, jobs, monkeypatch):
    # Windows では別の要求が開いているファイルを消すと PermissionError になる。
    name = _template_file(fake)

    def locked(*args, **kwargs):
        raise PermissionError(13, "別のプロセスが使用中です")

    monkeypatch.setattr("docgen.templates.remove", locked)
    response = client.delete(f"/api/templates/{name}", headers=CSRF)
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "フォーマットファイルが使用中のため削除できませんでした。"
        "少し待ってからもう一度実行してください"
    )
    assert jobs.shared_busy() is False
    assert (fake.templates_dir / name).exists()


def test_template_delete_unknown_returns_404(client, fake, jobs):
    _template_file(fake, name="x.md")
    response = client.delete("/api/templates/missing.md", headers=CSRF)
    assert response.status_code == 404
    assert response.json()["detail"] == NO_SUCH_TEMPLATE
    # 404 で抜けても共有ロックは解放されている。
    assert jobs.shared_busy() is False
    response = client.delete("/api/templates/x.md", headers=CSRF)
    assert response.status_code == 200
    assert client.get("/api/templates").json()["items"] == []


def test_template_operations_return_409_while_shared_busy(client, fake, jobs):
    name = _template_file(fake)
    fake.release = threading.Event()
    upload = client.post("/api/uploads", json=_upload_body(("a.txt", b"x")), headers=CSRF)
    assert upload.status_code == 200
    try:
        assert fake.started.wait(WAIT_S)
        responses = [
            client.post(
                "/api/templates", json=_upload_body(("b.md", b"{{x}}")), headers=CSRF
            ),
            client.delete(f"/api/templates/{name}", headers=CSRF),
        ]
        for response in responses:
            assert response.status_code == 409
            assert response.json()["detail"] == BUSY_MESSAGE
        # 一覧はロックを取らないので見られる。
        assert client.get("/api/templates").status_code == 200
    finally:
        fake.release.set()
    assert _job_result(client, jobs, upload.json()["job_id"])["state"] == "done"
    assert sorted(p.name for p in fake.templates_dir.iterdir()) == [name]


def test_broken_template_is_listed_with_error(client, fake):
    _template_file(fake, name="壊れた.docx", content=b"not a zip")
    for items in (
        client.get("/api/templates").json()["items"],
        client.get("/api/cowork/options").json()["templates"],
    ):
        assert len(items) == 1
        assert items[0]["name"] == "壊れた.docx"
        assert items[0]["placeholders"] is None
        assert items[0]["error"]
    # 壊れていても一覧から消せる（消せる場所がここしか無い）。
    assert client.delete("/api/templates/壊れた.docx", headers=CSRF).status_code == 200


# Windows で別物になる名前（代替データストリーム・予約デバイス名・末尾の . と空白・制御文字）。
WINDOWS_TRICK_NAMES = ["ab:y.md", "x.md:s.md", "NUL.md", "con.txt", "a.md.", "a.md ", "a\x01.md"]


def _files_under(directory):
    return sorted(p.name for p in directory.rglob("*")) if directory.exists() else []


@pytest.mark.parametrize("name", WINDOWS_TRICK_NAMES)
def test_template_register_rejects_windows_trick_names(client, fake, tmp_path, name):
    existing = _template_file(fake, name="x.md")
    before = _files_under(tmp_path)
    response = client.post(
        "/api/templates", json=_upload_body((name, b"# {{x}}")), headers=CSRF
    )
    assert response.status_code == 400, name
    assert _files_under(tmp_path) == before
    assert (fake.templates_dir / existing).read_text(encoding="utf-8").startswith("# {{会議名}}")


@pytest.mark.parametrize("name", WINDOWS_TRICK_NAMES)
def test_cowork_attachment_rejects_windows_trick_names(client, fake, name):
    body = _cowork_body(attachments=_upload_body((name, b"x"))["files"])
    assert client.post("/api/cowork", json=body, headers=CSRF).status_code == 400, name
    assert fake.parse_calls == []
    assert fake.ask_text_calls == []


@pytest.mark.parametrize("name", WINDOWS_TRICK_NAMES)
def test_upload_rejects_windows_trick_names(client, fake, name):
    response = client.post("/api/uploads", json=_upload_body((name, b"x")), headers=CSRF)
    assert response.status_code == 400, name
    assert fake.ingest_uploads_calls == []


def test_template_register_reports_partial_failure(client, fake):
    # 2件目の登録先がディレクトリで、コピーが OSError になる状況を作る。
    (fake.templates_dir / "b.md").mkdir(parents=True)
    body = _upload_body(("a.md", b"{{x}}"), ("b.md", b"{{y}}"))
    response = client.post("/api/templates", json=body, headers=CSRF)
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("b.md を登録できませんでした: ")
    assert detail.endswith("（登録済み: a.md）")
    assert [item["name"] for item in client.get("/api/templates").json()["items"]] == ["a.md"]


COWORK_BUSY_MESSAGE = "ほかの人の生成が混み合っています。少し待ってからもう一度実行してください"


def _start_blocked_cowork(client, fake):
    """終わらない生成を1本走らせ、生成の中に入るまで待つ。"""
    fake.text_release = threading.Event()
    fake.text_started.clear()
    response = client.post("/api/cowork", json=_cowork_body(), headers=CSRF)
    assert response.status_code == 200, response.text
    assert fake.text_started.wait(WAIT_S)
    return response.json()["job_id"]


def test_cowork_over_the_limit_is_rejected_with_503(fake, jobs):
    with _client(fake, jobs, max_cowork_jobs=1) as client:
        first = _start_blocked_cowork(client, fake)
        try:
            response = client.post("/api/cowork", json=_cowork_body(), headers=CSRF)
            assert response.status_code == 503
            assert response.json() == {"detail": COWORK_BUSY_MESSAGE}
        finally:
            fake.text_release.set()
        assert _job_result(client, jobs, first)["state"] == "done"


def test_cowork_slot_is_returned_when_the_job_finishes(fake, jobs):
    with _client(fake, jobs, max_cowork_jobs=1) as client:
        first = _start_blocked_cowork(client, fake)
        fake.text_release.set()
        assert _job_result(client, jobs, first)["state"] == "done"
        assert _cowork_result(client, jobs, _cowork_body())["errors"] == []


def test_cowork_slot_is_returned_when_the_job_raises(fake, jobs, monkeypatch):
    # generate_cowork は例外を捕まえてエラー結果として返すため、偽の ask_text で
    # 失敗させても work の外へは出ない。work 自体から例外を出して finally を通す。
    def explode(*args, **kwargs):
        raise RuntimeError("生成に失敗")

    with _client(fake, jobs, max_cowork_jobs=1) as client:
        monkeypatch.setattr("rag_web.app.generate_cowork", explode)
        response = client.post("/api/cowork", json=_cowork_body(), headers=CSRF)
        assert response.status_code == 200
        outcome = _job_result(client, jobs, response.json()["job_id"])
        assert outcome == {"state": "failed", "error": "生成に失敗"}
        monkeypatch.undo()
        assert _cowork_result(client, jobs, _cowork_body())["errors"] == []


def test_cowork_slot_is_returned_when_the_job_cannot_start(fake, jobs, monkeypatch):
    def cannot_start(*args, **kwargs):
        raise RuntimeError("スレッドを起動できない")

    monkeypatch.setattr(jobs, "start", cannot_start)
    with _client(fake, jobs, max_cowork_jobs=1) as client:
        with pytest.raises(RuntimeError, match="スレッドを起動できない"):
            client.post("/api/cowork", json=_cowork_body(), headers=CSRF)
        monkeypatch.undo()
        assert _cowork_result(client, jobs, _cowork_body())["errors"] == []


def test_threadpool_size_is_applied_on_startup(fake, jobs):
    with _client(fake, jobs, threadpool_size=7) as client:
        # リミッターはイベントループごとにあるため、アプリが動くループの中で読む。
        limiter = client.portal.call(anyio.to_thread.current_default_thread_limiter)
        assert limiter.total_tokens == 7
