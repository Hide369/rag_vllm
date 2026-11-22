"""Web 画面（FastAPI）。rag_chat_app.py（Streamlit）のチャットとサイドバーの置き換え。

- 閲覧・チャット・アップロード・アップロード済み資料の削除は誰でもできる（ログイン不要）
- 社内資料の差分取り込み（POST /api/ingest）だけは管理者ログインが要る
- GET 以外は X-Requested-With ヘッダーを必須にする（CSRF 対策。ログインが無くても、
  別サイトのページから取り込みや削除を実行させられるのを防ぐ）
- 取り込み・アップロード・削除は共有ロックで全利用者を通して1つずつ。取れなければ 409

Batch_Exec の godoc_agent/web/app.py の書き方に倣う。
"""

import base64
import binascii
import json
import math
import tempfile
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ingest import store
from ingest.embedder import EmbeddingError
from ingest.parsers import SUPPORTED_SUFFIXES
from ingest.prompting import format_report
from scripts.ingest_source import UPLOAD_PREFIX

from .answering import ChatRequest, answer_events
from .auth import LoginThrottle, SessionStore, validate_password_hash, verify_password
from .corpora import CorpusCache
from .jobs import JobStore, SharedOperationBusy
from .services import Services, caption_image_or_reason
from .settings import WebSettings

COOKIE_NAME = "rag_web_session"
# 状態を変える操作に必須のヘッダー。別サイトのフォームからは付けられないため、CSRF 対策になる
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "rag-web"
STATIC_DIR = Path(__file__).resolve().parent / "static"
VENDOR_DIR = STATIC_DIR / "vendor"

# 段階③で Streamlit 版（rag_chat_app.py）を消すまでの一時的な重複。値は同じに保つ
# （tests/test_web_app.py が一致を確かめる）。
MODELS = ["gpt-oss:20b"]
DOCS_INGEST_COMMAND = (
    "python -m scripts.fetch_docs のあと "
    "python -m scripts.ingest_source --source-dir docs_source "
    "--db docs_store.sqlite3 --keep-code-blocks"
)

UPLOAD_CAPTION = (
    "アップロードした資料はDBに残り、全利用者の検索対象になります。"
    "不要になったら下の一覧から削除してください。"
)
SHARED_BUSY_MESSAGE = "ほかの人が取り込み中です。終わってからもう一度実行してください"

# 取り込み・アップロードの対象は社内資料だけ。技術ドキュメントは CLI で取り込む。
INTERNAL = "internal"

Corpus = Literal["internal", "docs"]


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class Message(BaseModel):
    # system は受け取らない。システムプロンプトはサーバーが付ける。
    role: Literal["user", "assistant"]
    content: str = Field(max_length=100_000)


class ChatBody(BaseModel):
    corpus: Corpus
    model: str
    temperature: float = Field(ge=0, le=1)
    question: str = Field(min_length=1, max_length=4000)
    history: list[Message] = Field(max_length=100)

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value not in MODELS:
            raise ValueError(f"使えないモデルです: {value}")
        return value


class UploadFile(BaseModel):
    name: str
    content_b64: str


class UploadBody(BaseModel):
    files: list[UploadFile] = Field(min_length=1, max_length=50)


def _check_upload_name(name: str) -> None:
    """一時フォルダの外へ書かせない。ファイル名1つ分だけを受け付ける。

    Path(name).name との比較だけでは、実行環境の OS が区切りとみなさない文字
    （Linux での \\）を通してしまうため、区切り文字も明示的に拒む。
    """
    if (
        not name
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or ".." in name
    ):
        raise HTTPException(400, f"ファイル名にパスは含められません: {name}")
    if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(400, f"取り込めない種類のファイルです: {name}")


def _decode_uploads(files: list[UploadFile], max_bytes: int) -> list[tuple[str, bytes]]:
    seen: set[str] = set()
    decoded: list[tuple[str, bytes]] = []
    total = 0
    for file in files:
        _check_upload_name(file.name)
        # 同じ名前は一時フォルダで上書きされ、黙って1つが消える。
        if file.name in seen:
            raise HTTPException(400, f"同じ名前のファイルが複数あります: {file.name}")
        seen.add(file.name)
        try:
            content = base64.b64decode(file.content_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(400, f"ファイルの内容を読めません: {file.name}") from e
        total += len(content)
        if total > max_bytes:
            raise HTTPException(
                413,
                f"アップロードは合計 {max_bytes // (1024 * 1024)}MB までです",
            )
        decoded.append((file.name, content))
    return decoded


def create_app(
    settings: WebSettings,
    services: Services | None = None,
    jobs: JobStore | None = None,
    throttle: LoginThrottle | None = None,
    today: Callable[[], date] = date.today,
) -> FastAPI:
    validate_password_hash(settings.admin_password_hash)
    services = services or Services()
    jobs = jobs or JobStore()
    throttle = throttle or LoginThrottle()
    sessions = SessionStore(settings.session_ttl_s)
    corpora = CorpusCache(services)

    app = FastAPI(title="社内資料RAG", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(SharedOperationBusy)
    def shared_busy(_request: Request, _error: SharedOperationBusy) -> JSONResponse:
        return JSONResponse({"detail": SHARED_BUSY_MESSAGE}, status_code=409)

    def is_admin(request: Request) -> bool:
        return sessions.is_valid(request.cookies.get(COOKIE_NAME))

    def require_csrf_header(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(403, "不正なリクエストです")

    def require_admin(request: Request) -> None:
        if request.method not in ("GET", "HEAD"):
            require_csrf_header(request)
        if not is_admin(request):
            raise HTTPException(401, "管理者としてログインしてください")

    def require_ollama() -> None:
        # 取り込みを始めてから落ちるのを防ぐ。CLI・Streamlit 版と同じ確認。
        try:
            services.check_ollama()
        except EmbeddingError as e:
            raise HTTPException(503, str(e)) from e

    # --- 画面 ---------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/app.js", include_in_schema=False)
    def script() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")

    @app.get("/vendor/{name}", include_in_schema=False)
    def vendor(name: str) -> FileResponse:
        # vendor/ 直下のファイルだけ。区切りや .. を含む名前で外へ出させない。
        if not name or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(404, "見つかりません")
        path = VENDOR_DIR / name
        if path.name != name or not path.is_file():
            raise HTTPException(404, "見つかりません")
        return FileResponse(path)

    # --- ログイン -----------------------------------------------------------------------

    @app.get("/api/me")
    def me(request: Request) -> dict:
        return {"admin": is_admin(request)}

    @app.post("/api/login", dependencies=[Depends(require_csrf_header)])
    def login(body: LoginBody, request: Request, response: Response) -> dict:
        client = request.client.host if request.client else "unknown"
        if (wait := throttle.retry_after(client)) > 0:
            raise HTTPException(
                429,
                f"ログインの失敗が続いたため、{int(wait // 60) + 1} 分ほど待ってから再試行してください",
                headers={"Retry-After": str(math.ceil(wait))},
            )
        if not verify_password(body.password, settings.admin_password_hash):
            throttle.record_failure(client)
            raise HTTPException(401, "パスワードが違います")
        throttle.record_success(client)
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        response.set_cookie(
            COOKIE_NAME, sessions.create(), max_age=settings.session_ttl_s,
            httponly=True, secure=settings.cookie_secure, samesite="strict", path="/",
        )
        return {"admin": True}

    @app.post("/api/logout", dependencies=[Depends(require_csrf_header)])
    def logout(request: Request, response: Response) -> dict:
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"admin": False}

    # --- 状態・チャット -----------------------------------------------------------------

    @app.get("/api/status")
    def status(corpus: Corpus = INTERNAL) -> dict:
        return {
            "models": MODELS,
            "corpus": corpus,
            "chunk_count": corpora.collection(corpus).count(),
            "source_dir_name": services.source_dir.name,
            "docs_ingest_command": DOCS_INGEST_COMMAND,
            "reranker": corpora.reranker_status(),
            "shared_busy": jobs.shared_busy(),
        }

    @app.post("/api/chat", dependencies=[Depends(require_csrf_header)])
    def chat(body: ChatBody) -> StreamingResponse:
        request = ChatRequest(
            corpus=body.corpus,
            model=body.model,
            temperature=body.temperature,
            question=body.question,
            history=[message.model_dump() for message in body.history],
        )

        def lines() -> Iterator[str]:
            for event in answer_events(request, services, corpora, today()):
                yield json.dumps(event, ensure_ascii=False) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    # --- 取り込み -----------------------------------------------------------------------

    @app.post("/api/ingest", dependencies=[Depends(require_admin)])
    def ingest() -> dict:
        require_ollama()
        caption_image, reason = caption_image_or_reason(services)
        collection = corpora.collection(INTERNAL)

        def work() -> dict:
            report = services.ingest_directory(
                services.source_dir, collection, caption_image=caption_image
            )
            return {"report": format_report(report), "notice": reason}

        return {"job_id": jobs.start(work, shared=True)}

    # --- アップロード -------------------------------------------------------------------

    @app.get("/api/uploads")
    def list_uploads() -> dict:
        # source/ 由来の資料は出さない。原本がサーバー側にあり、画面から消しても
        # 次の差分取り込みで戻ってくるため、消せるように見せるのは嘘になる。
        keys = sorted(
            source
            for source in store.indexed_sources(corpora.collection(INTERNAL))
            if source.startswith(UPLOAD_PREFIX)
        )
        return {
            "caption": UPLOAD_CAPTION,
            "items": [{"key": key, "name": key[len(UPLOAD_PREFIX):]} for key in keys],
        }

    @app.post("/api/uploads", dependencies=[Depends(require_csrf_header)])
    def upload(body: UploadBody) -> dict:
        files = _decode_uploads(body.files, settings.max_upload_bytes)
        require_ollama()
        caption_image, reason = caption_image_or_reason(services)
        collection = corpora.collection(INTERNAL)

        def work() -> dict:
            # 一時ディレクトリは取り込みが終われば消える。原本を残さないのは
            # 設計上の選択であり、DBのチャンクだけが残る。
            with tempfile.TemporaryDirectory() as workspace:
                paths = []
                for name, content in files:
                    path = Path(workspace) / name
                    path.write_bytes(content)
                    paths.append(path)
                report = services.ingest_uploads(
                    paths, collection, caption_image=caption_image
                )
            return {"report": format_report(report), "notice": reason}

        return {"job_id": jobs.start(work, shared=True)}

    @app.delete("/api/uploads/{key:path}", dependencies=[Depends(require_csrf_header)])
    def delete_upload(key: str) -> dict:
        if not key.startswith(UPLOAD_PREFIX):
            raise HTTPException(400, "アップロードした資料だけを削除できます")
        collection = corpora.collection(INTERNAL)
        with jobs.shared_operation():
            # 空のチャンク列を渡すとその資料はDBから消える（ingest/store.py）。
            store.replace_source(collection, key, [], [])
        return {"deleted": key}

    # --- ジョブ -------------------------------------------------------------------------

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict:
        result = jobs.get(job_id)
        if result is None:
            raise HTTPException(404, "ジョブが見つかりません（結果を取得済みか、期限切れです）")
        return result

    return app
