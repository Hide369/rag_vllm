"""Web 画面（FastAPI）。チャット・Cowork・サイドバー（資料の取り込みと管理）の API と静的ファイルの配信。

- 閲覧・チャット・アップロード・アップロード済み資料の削除は誰でもできる（ログイン不要）
- 社内資料の差分取り込み（POST /api/ingest）だけは管理者ログインが要る
- GET 以外は X-Requested-With ヘッダーを必須にする（CSRF 対策。ログインが無くても、
  別サイトのページから取り込みや削除を実行させられるのを防ぐ）
- 取り込み・アップロード・削除・フォーマットファイルの登録削除は共有ロックで全利用者を通して
  1つずつ。取れなければ 409。Cowork（文書生成）は共有リソースを変えないのでロックを取らない

Batch_Exec の godoc_agent/web/app.py の書き方に倣う。
"""

import base64
import binascii
import json
import logging
import math
import tempfile
import threading
from contextlib import asynccontextmanager
from collections.abc import Callable, Collection, Iterator
from datetime import date
from pathlib import Path
from typing import Literal

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

import docgen
from docgen import templates as docgen_templates
from ingest import store
from ingest.embedder import EmbeddingError
from ingest.parsers import SUPPORTED_SUFFIXES
from ingest.prompting import format_report
from ingest.vllm_config import load_vllm_settings
from scripts.ingest_source import UPLOAD_PREFIX

from .answering import ChatRequest, answer_events
from .auth import LoginThrottle, SessionStore, validate_password_hash, verify_password
from .corpora import CorpusCache
from .cowork import (
    NO_TEMPLATE_LABEL,
    Attachment,
    CoworkRequest,
    generate_cowork,
    is_source_suffix,
    output_suffixes,
    template_path,
)
from .jobs import JobStore, SharedOperationBusy
from .services import Services, caption_image_or_reason
from .settings import WebSettings

logger = logging.getLogger(__name__)

COOKIE_NAME = "rag_web_session"
# 状態を変える操作に必須のヘッダー。別サイトのフォームからは付けられないため、CSRF 対策になる
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "rag-web"
STATIC_DIR = Path(__file__).resolve().parent / "static"
VENDOR_DIR = STATIC_DIR / "vendor"

# 社内資料以外（技術ドキュメント）のコーパスが空のとき、画面に示す取り込みコマンド。
DOCS_INGEST_COMMAND = (
    "python -m scripts.fetch_docs のあと "
    "python -m scripts.ingest_source --source-dir docs_source "
    "--db docs_store.sqlite3 --keep-code-blocks"
)

UPLOAD_CAPTION = (
    "アップロードした資料はDBに残り、全利用者の検索対象になります。"
    "不要になったら下の一覧から削除してください。"
)
COWORK_BUSY_MESSAGE = (
    "ほかの人の生成が混み合っています。少し待ってからもう一度実行してください"
)
SHARED_BUSY_MESSAGE = "ほかの人が取り込み中です。終わってからもう一度実行してください"
TEMPLATE_CAPTION = (
    "フォーマットファイルの空欄は {{会議名}} のように書いてください。"
    "登録したフォーマットファイルはこのマシンの templates/ に残ります。"
)
NO_SUCH_TEMPLATE_MESSAGE = "そのフォーマットファイルはありません"
TEMPLATE_IN_USE_MESSAGE = (
    "フォーマットファイルが使用中のため削除できませんでした。"
    "少し待ってからもう一度実行してください"
)
UNSUPPORTED_UPLOAD = "取り込めない種類のファイルです"
UNSUPPORTED_ATTACHMENT = "添付できない種類のファイルです"
UNSUPPORTED_TEMPLATE = "フォーマットファイルとして使えない形式です"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# すべての応答に付けるヘッダー。index.html の meta の CSP は frame-ancestors を
# 扱えない（meta では無視される）ため、埋め込み禁止はヘッダーで送る。
SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
)

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
    temperature: float = Field(ge=0, le=1)
    # 上限は履歴1件と同じ。長い資料を貼って尋ねる使い方を狭めないため、
    # 異常に大きい入力だけを弾く値にしている。
    question: str = Field(min_length=1, max_length=100_000)
    history: list[Message] = Field(max_length=100)


class UploadItem(BaseModel):
    name: str = Field(max_length=255)
    content_b64: str


class UploadBody(BaseModel):
    files: list[UploadItem] = Field(min_length=1, max_length=50)


class CoworkBody(BaseModel):
    question: str = Field(min_length=1, max_length=100_000)
    # 登録済みのフォーマットファイルの名前。None は雛形なし。
    template: str | None = Field(default=None, max_length=255)
    output_suffix: str
    use_internal: bool
    use_docs: bool
    pasted: str = Field(default="", max_length=100_000)
    attachments: list[UploadItem] = Field(default_factory=list, max_length=20)

    @field_validator("output_suffix")
    @classmethod
    def _known_output_suffix(cls, value: str) -> str:
        if value not in output_suffixes():
            raise ValueError(f"選べない出力形式です: {value}")
        return value


class TemplateUploadBody(BaseModel):
    files: list[UploadItem] = Field(min_length=1, max_length=20)


def _check_file_name(name: str) -> None:
    """一時フォルダや templates/ の外を指させない。ファイル名1つ分だけを受け付ける。

    ".." は「v1..2.md」のように名前の一部なら外へ出られないので通す。拒むのは
    「.」「..」という名前そのものだけである（区切りは下で別に拒む）。

    Path(name).name との比較だけでは、実行環境の OS が区切りとみなさない文字
    （Linux での \\）を通してしまうため、区切り文字も明示的に拒む。
    """
    if (
        not name
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or name in (".", "..")
    ):
        raise HTTPException(400, f"ファイル名にパスは含められません: {name}")
    # Windows では次の名前が別物になる。":" は代替データストリーム（"ab:y.md" は
    # 拡張子の無い "ab" を作り、"議事録.md:x.md" は既存の雛形にストリームを足す）、
    # 末尾の "." と空白は黙って落とされ、予約デバイス名は実ファイルにならない。
    # どれも一覧に出ず消せないファイルや、意図しない書き込み先を生むため拒む。
    if (
        ":" in name
        or any(ord(char) < 32 for char in name)
        or name.endswith((".", " "))
        or name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise HTTPException(400, f"ファイル名に使えない文字か名前です: {name}")


def _check_upload_name(
    name: str,
    suffixes: Collection[str] = SUPPORTED_SUFFIXES,
    unsupported: str = UNSUPPORTED_UPLOAD,
) -> None:
    _check_file_name(name)
    if Path(name).suffix.lower() not in suffixes:
        raise HTTPException(400, f"{unsupported}: {name}")


def _decode_uploads(
    files: list[UploadItem],
    max_bytes: int,
    suffixes: Collection[str] = SUPPORTED_SUFFIXES,
    unsupported: str = UNSUPPORTED_UPLOAD,
) -> list[tuple[str, bytes]]:
    seen: set[str] = set()
    decoded: list[tuple[str, bytes]] = []
    total = 0
    for file in files:
        _check_upload_name(file.name, suffixes, unsupported)
        # 同じ名前は一時フォルダで上書きされ、黙って1つが消える。Windows は
        # 大文字小文字を区別しないため、A.txt と a.txt も同じ名前として扱う。
        folded = file.name.casefold()
        if folded in seen:
            raise HTTPException(400, f"同じ名前のファイルが複数あります: {file.name}")
        seen.add(folded)
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


def _partial_registration_message(
    failed: str, error: Exception, registered: list[str]
) -> str:
    done = "、".join(registered) if registered else "なし"
    return f"{failed} を登録できませんでした: {error}（登録済み: {done}）"


def _template_items(services: Services) -> list[dict]:
    """登録済みのフォーマットファイルと、その印（開けなければ理由）。

    Cowork の選択肢と登録・削除の一覧の両方がこれを使う。印を添えるのは、
    印が1つも無い雛形をこの場で気づけるようにするため。
    """
    items = []
    for path in docgen_templates.templates(services.templates_dir):
        try:
            names = docgen.placeholders(path)
        except Exception as error:
            # templates.register は拡張子しか見ないため、中身が壊れた雛形も登録
            # できてしまう。ここで落とすと、その雛形を消せる唯一の画面ごと出せなく
            # なるため、理由を添えて一覧に残す（画面の雛形一覧もこの行を出す）。
            items.append({"name": path.name, "placeholders": None, "error": str(error)})
        else:
            items.append({"name": path.name, "placeholders": names, "error": None})
    return items


class SecurityHeadersMiddleware:
    """全応答に SECURITY_HEADERS を足す ASGI ミドルウェア。

    BaseHTTPMiddleware（@app.middleware）ではなく素の ASGI で書くのは、チャットの
    NDJSON ストリーミングを途中で溜め込まずにそのまま流すため。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in SECURITY_HEADERS if n not in names]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _session_cookie_attributes(settings: WebSettings) -> dict:
    """ログインとログアウトで同じ属性を使う。属性が違うと消せないブラウザがある。"""
    return {"httponly": True, "secure": settings.cookie_secure, "samesite": "strict", "path": "/"}


def create_app(
    settings: WebSettings,
    services: Services | None = None,
    jobs: JobStore | None = None,
    throttle: LoginThrottle | None = None,
    today: Callable[[], date] = date.today,
    chat_model: str | None = None,
) -> FastAPI:
    validate_password_hash(settings.admin_password_hash)
    # 回答生成に使うモデル。画面からは選ばせない（接続先の vLLM が載せている生成モデルが
    # これ1つだけで、選択肢を出しても利用者に選ぶ意味がないため）。変えるときは .env の
    # VLLM_CHAT_MODEL を直す。引数はテストから差し替えるためのもので、環境変数に依存させない。
    chat_model = chat_model or load_vllm_settings().chat_model
    services = services or Services()
    jobs = jobs or JobStore()
    throttle = throttle or LoginThrottle()
    sessions = SessionStore(settings.session_ttl_s)
    corpora = CorpusCache(services)

    # 1本ごとに添付と生成物がメモリに載るため、同時に走る Cowork の数を絞る。
    cowork_slots = threading.BoundedSemaphore(settings.max_cowork_jobs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 同期ルートは anyio の既定スレッドプール（既定40）で走る。サイズはここで決める。
        anyio.to_thread.current_default_thread_limiter().total_tokens = settings.threadpool_size
        yield

    app = FastAPI(
        title="社内資料RAG", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.add_middleware(SecurityHeadersMiddleware)
    cookie_attributes = _session_cookie_attributes(settings)

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

    def require_embedder() -> None:
        # 取り込みを始めてから落ちるのを防ぐ。CLI と同じ確認。
        try:
            services.check_embedder()
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
            **cookie_attributes,
        )
        return {"admin": True}

    @app.post("/api/logout", dependencies=[Depends(require_csrf_header)])
    def logout(request: Request, response: Response) -> dict:
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        response.delete_cookie(COOKIE_NAME, **cookie_attributes)
        return {"admin": False}

    # --- 状態・チャット -----------------------------------------------------------------

    @app.get("/api/status")
    def status(corpus: Corpus = INTERNAL) -> dict:
        return {
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
            model=chat_model,
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
        require_embedder()
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
        require_embedder()
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
            # 存在の確認もロックの中で行う。外で確かめると、確認と削除の間に
            # 別の操作が割り込める。
            if key not in store.indexed_sources(collection):
                raise HTTPException(404, "その資料は見つかりません")
            # 空のチャンク列を渡すとその資料はDBから消える（ingest/store.py）。
            store.replace_source(collection, key, [], [])
        return {"deleted": key}

    # --- Cowork（文書生成） -------------------------------------------------------------

    @app.get("/api/cowork/options")
    def cowork_options() -> dict:
        suffixes = output_suffixes()
        return {
            "no_template_label": NO_TEMPLATE_LABEL,
            "output_suffixes": suffixes,
            "source_suffixes": [suffix for suffix in suffixes if is_source_suffix(suffix)],
            "attachment_suffixes": sorted(SUPPORTED_SUFFIXES),
            "template_suffixes": sorted(docgen.SUPPORTED_SUFFIXES),
            "templates": _template_items(services),
        }

    @app.post("/api/cowork", dependencies=[Depends(require_csrf_header)])
    def cowork(body: CoworkBody) -> dict:
        files = _decode_uploads(
            body.attachments, settings.max_upload_bytes,
            SUPPORTED_SUFFIXES, UNSUPPORTED_ATTACHMENT,
        )
        if body.template is not None:
            try:
                template_path(services, body.template)
            except FileNotFoundError as e:
                raise HTTPException(400, NO_SUCH_TEMPLATE_MESSAGE) from e
        request = CoworkRequest(
            model=chat_model,
            question=body.question,
            template=body.template,
            output_suffix=body.output_suffix,
            use_internal=body.use_internal,
            use_docs=body.use_docs,
            pasted=body.pasted,
            attachments=[Attachment(name, content) for name, content in files],
        )
        generated_on = today()

        # 同時に走る数は RAG_WEB_MAX_COWORK_JOBS まで。待たせずに 503 で断る。
        # 共有ロックは取らない。生成は共有リソースを変えず（添付はDBに入れない）、
        # 取り込み中でも使えるようにする。生成中に雛形が消されたときは、
        # generate_cowork がエラー結果として返す。
        if not cowork_slots.acquire(blocking=False):
            raise HTTPException(503, COWORK_BUSY_MESSAGE)

        def work() -> dict:
            try:
                return generate_cowork(request, services, corpora, generated_on)
            finally:
                cowork_slots.release()

        try:
            job_id = jobs.start(work, shared=False)
        except BaseException:
            # ジョブが始まらなければ work の finally は走らない。枠を返さないと減り続ける。
            cowork_slots.release()
            raise
        return {"job_id": job_id}

    # --- フォーマットファイル -----------------------------------------------------------

    @app.get("/api/templates")
    def list_templates() -> dict:
        return {"caption": TEMPLATE_CAPTION, "items": _template_items(services)}

    @app.post("/api/templates", dependencies=[Depends(require_csrf_header)])
    def register_templates(body: TemplateUploadBody) -> dict:
        files = _decode_uploads(
            body.files, settings.max_upload_bytes,
            docgen.SUPPORTED_SUFFIXES, UNSUPPORTED_TEMPLATE,
        )
        # 順序は「全件の検査（_decode_uploads）→ 全件を一時フォルダへ書く → 全件を
        # 登録」。名前・拡張子・サイズ・base64 の誤りは templates/ に触れる前に断る。
        # それでも登録の途中で失敗したら（ディスクの異常など）、そこまでに登録した
        # ものは残るため、黙らずに応答で伝える。
        with jobs.shared_operation():
            with tempfile.TemporaryDirectory() as workspace:
                paths = []
                for name, content in files:
                    path = Path(workspace) / name
                    path.write_bytes(content)
                    paths.append(path)
                registered: list[str] = []
                for path in paths:
                    try:
                        docgen_templates.register(path, directory=services.templates_dir)
                    except (ValueError, docgen.UnsupportedTemplateError, OSError) as e:
                        # ValueError 等は上の検査で通常は来ない。docgen 側の検査も 400 にする。
                        status = 500 if isinstance(e, OSError) else 400
                        raise HTTPException(
                            status, _partial_registration_message(path.name, e, registered)
                        ) from e
                    registered.append(path.name)
        return {"registered": len(registered)}

    @app.delete("/api/templates/{name:path}", dependencies=[Depends(require_csrf_header)])
    def delete_template(name: str) -> dict:
        # :path で受けるのは、区切りを含む名前もここへ届けて 400 で断るため
        # （{name} だとルートに合わず 404 になり、拒んだ理由が伝わらない）。
        _check_file_name(name)
        with jobs.shared_operation():
            # 存在の確認もロックの中で行う（delete_upload と同じ理由）。
            try:
                template_path(services, name)
            except FileNotFoundError as e:
                raise HTTPException(404, NO_SUCH_TEMPLATE_MESSAGE) from e
            try:
                docgen_templates.remove(name, directory=services.templates_dir)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            except OSError as e:
                # Windows では、別の要求（Cowork の生成など）が開いているファイルを
                # 消そうとすると PermissionError になる。素の 500 にせず、待てば
                # 通ることを伝える。
                logger.exception("フォーマットファイルを削除できませんでした: %s", name)
                raise HTTPException(409, TEMPLATE_IN_USE_MESSAGE) from e
        return {"deleted": name}

    # --- ジョブ -------------------------------------------------------------------------

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict:
        result = jobs.get(job_id)
        if result is None:
            raise HTTPException(404, "ジョブが見つかりません（結果を取得済みか、期限切れです）")
        return result

    return app
