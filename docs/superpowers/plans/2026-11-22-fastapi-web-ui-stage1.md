# FastAPI 画面 段階①（チャットとサイドバーの操作）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python -m rag_web` で起動する FastAPI + HTML/JS の画面で、今の Streamlit 版のチャットとサイドバーの操作（検索対象・モデル・Temperature・差分取り込み・資料のアップロードと削除・履歴リセット）を使えるようにし、差分取り込みだけ管理者ログインを必須にする。

**Architecture:** `rag_web/` パッケージに、ルーティングだけを持つ `app.py`（`create_app()`）と、画面に依存しない処理（`answering.py` / `corpora.py` / `jobs.py` / `auth.py`）を分ける。外部 I/O（Ollama・検索・取り込み・リランカー・VLM）は `services.py` の `Services` にまとめ、テストでは偽物を渡す。画面は `static/index.html` + `static/app.js`（ビルドなし）で、Markdown・サニタイズ・Mermaid の JS は `static/vendor/` に同梱する。

**Tech Stack:** Python 3.13（`rag_env`）、FastAPI、uvicorn、pytest + `fastapi.testclient`、素の JavaScript。

**Spec:** `docs/superpowers/specs/2026-11-22-fastapi-web-ui-design.md`

## Global Constraints

- 仮想環境は `rag_env`。コマンドは `.\rag_env\Scripts\python.exe -m ...`（PowerShell）。
- `rag_chat_app.py` と `tests/test_rag_chat_app*.py` には触れない（移行中は凍結）。
- `ingest/` `docgen/` `scripts/` は変更しない（呼ぶだけ）。
- 既定の待ち受けは `127.0.0.1:8502`。環境変数は `RAG_WEB_HOST` / `RAG_WEB_PORT` / `RAG_WEB_MAX_UPLOAD_MB`（既定50）/ `RAG_WEB_ADMIN_PASSWORD_HASH`（必須）/ `RAG_WEB_SESSION_TTL_HOURS`（既定12）/ `RAG_WEB_COOKIE_SECURE`（既定 false）。
- GET 以外は `X-Requested-With: rag-web` ヘッダー必須。無ければ 403「不正なリクエストです」。
- 画面の表示で外部（CDN 等）へ通信しない。`index.html` / `app.js` に `http://` `https://` で始まる読み込み先を書かない。
- 質問・回答・添付をディスク・DB に残さない。会話履歴はブラウザの JS のメモリだけ。
- 文言は今の画面と同じにする: 「検索できませんでした: {error}」「回答を生成できませんでした: {error}」「条件を解釈できませんでした。通常の検索で回答します。」「検索対象を切り替えたため、会話履歴をリセットしました。」、アップロードの注意書き「アップロードした資料はDBに残り、全利用者の検索対象になります。不要になったら下の一覧から削除してください。」、取り込み中表示「取り込み中…（図表があると時間がかかります）」。
- 共有操作が実行中のときの 409 は「ほかの人が取り込み中です。終わってからもう一度実行してください」。未ログインの取り込みの 401 は「管理者としてログインしてください」。
- `SYSTEM_PROMPT` は今と同じ文面で、日付はリクエストごとに作る: `f"あなたは有能なアシスタントです。今日の日付は{today:%Y年%m月%d日}です。\n日本語で回答して下さい。"`
- `MODELS = ["gpt-oss:20b"]`。Temperature は 0.0〜1.0（既定 0.3、0.1 刻み）。
- コミットはコンベンショナルコミット形式・英語。

## Review Focus

1. **履歴に `system` ロールや巨大な履歴を混ぜて送られる** → 422 で拒否し、生成を呼ばない（Task 6 のテスト `test_chat_rejects_system_role_and_oversized_history`）。
2. **アップロードのファイル名にパス区切り・`..`・未対応の拡張子** → 400 で拒否し、何も書かず取り込みも呼ばない（Task 6 `test_upload_rejects_unsafe_names_and_suffixes`）。
3. **取り込みが例外で落ちる** → ジョブは `failed` になり、共有操作のロックが解放され、次の取り込みが 409 にならない（Task 5 `test_shared_lock_released_after_failed_job`）。
4. **アップロードで DB の revision が進んだ直後の検索** → 組み直した BM25 索引で検索する（古い索引を使わない）（Task 3 `test_index_rebuilt_when_revision_changes`）。
5. **生成の途中で想定外の例外** → ストリームは `error` イベントで必ず終わり、途中まで届いた本文は捨てない（Task 4 `test_unexpected_error_mid_stream_ends_with_error_event`）。画面は `error` / `done` / 通信断のどれでも入力欄を再び有効にする（Task 7 の手動確認項目）。

---

### Task 1: パッケージの土台と設定

**Files:**
- Create: `rag_web/__init__.py`（空）、`rag_web/settings.py`
- Modify: `requirements.txt`（`fastapi==0.165.0`、`uvicorn==0.54.0`、`httpx==0.28.1` を追加。`pip index versions` で公開から2週間以上経っていない版なら、Batch_Exec と同じ `fastapi==0.162.2` にする）、`.env.example`（`RAG_WEB_*` の6項目をコメント付きで追記）
- Test: `tests/test_web_settings.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class WebSettings: admin_password_hash: str; host: str = "127.0.0.1"; port: int = 8502; max_upload_bytes: int = 50 * 1024 * 1024; session_ttl_s: int = 12 * 3600; cookie_secure: bool = False`
  - `def load_web_settings(environ: Mapping[str, str] = os.environ) -> WebSettings` — `ValueError`（日本語、変数名入り）を送出

- [ ] **Step 1: 依存を入れる** — `requirements.txt` を更新し `.\rag_env\Scripts\python.exe -m pip install -r requirements.txt`。Expected: エラーなく終わる。
- [ ] **Step 2: Write the failing tests**

```python
def test_defaults_when_only_hash_is_set():
    s = load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "scrypt:..."})
    assert (s.host, s.port, s.max_upload_bytes, s.session_ttl_s, s.cookie_secure) == (
        "127.0.0.1", 8502, 50 * 1024 * 1024, 12 * 3600, False)

def test_missing_hash_is_rejected():
    with pytest.raises(ValueError, match="RAG_WEB_ADMIN_PASSWORD_HASH"):
        load_web_settings({})

@pytest.mark.parametrize("name,value", [("RAG_WEB_PORT", "abc"), ("RAG_WEB_PORT", "0"),
    ("RAG_WEB_MAX_UPLOAD_MB", "-1"), ("RAG_WEB_SESSION_TTL_HOURS", "x"), ("RAG_WEB_COOKIE_SECURE", "maybe")])
def test_invalid_values_name_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "h", name: value})
```

- [ ] **Step 3: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_settings.py -v` → FAIL（モジュールが無い）
- [ ] **Step 4: Implement** `rag_web/settings.py`。真偽値は `true/false/1/0/yes/no`（大文字小文字無視）。ハッシュ形式の検証は Task 2 の `validate_password_hash` が受け持つので、ここは空かどうかだけ見る。
- [ ] **Step 5: Run** 同上 → PASS
- [ ] **Step 6: Commit** `feat: add rag_web package settings and web dependencies`

### Task 2: 管理者ログイン（Batch_Exec から移植）

**Files:**
- Create: `rag_web/auth.py`、`rag_web/password.py`
- Test: `tests/test_web_auth.py`

**Interfaces:**
- Produces（Batch_Exec `godoc_agent/web/auth.py` と同じ名前・振る舞い）:
  - `hash_password(password: str, *, n: int = 2**15) -> str`、`validate_password_hash(stored: str) -> None`、`verify_password(password: str, stored: str) -> bool`
  - `class SessionStore(ttl_s: int, clock=time.monotonic)`: `create() -> str`、`is_valid(token: str | None) -> bool`、`revoke(token: str | None) -> None`
  - `@dataclass class LoginThrottle(max_failures=5, window_s=900, lockout_s=900, clock=time.monotonic)`: `retry_after(client) -> float`、`record_failure(client)`、`record_success(client)`
  - `rag_web/password.py` の `main() -> int`（`python -m rag_web.password`。12文字未満・2回の入力不一致は 2 を返す。成功時に `RAG_WEB_ADMIN_PASSWORD_HASH=<hash>` を表示）
- エラー文中の変数名とコマンド名は `RAG_WEB_ADMIN_PASSWORD_HASH` / `rag_web.password` に置き換える。

- [ ] **Step 1: Write the failing tests** — Batch_Exec の `tests/test_web.py` のうち auth に関するテストを移植し、最低限次を含める（ハッシュは `n=2**10`）:

```python
def test_hash_roundtrip_and_wrong_password(): ...            # verify True / False
def test_validate_rejects_malformed_hash():                  # "plain", "scrypt:1:2" → ValueError（"RAG_WEB_ADMIN_PASSWORD_HASH" を含む）
def test_session_expires_and_revokes():                      # 偽の clock で ttl 経過後 False、revoke 後 False
def test_throttle_locks_after_five_failures_and_resets_on_success():
def test_password_main_rejects_short_and_mismatch(monkeypatch, capsys):  # getpass を差し替え、戻り値 2
def test_password_main_prints_env_line(monkeypatch, capsys):  # 出力が "RAG_WEB_ADMIN_PASSWORD_HASH=scrypt:" で始まる
```

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_auth.py -v` → FAIL
- [ ] **Step 3: Implement** — Batch_Exec の `auth.py` / `password.py` を写し、名前を置き換える。モジュール docstring に「Batch_Exec godoc_agent/web/auth.py から移植」と出典を書く。
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat: add admin login primitives ported from Batch_Exec`

### Task 3: 外部 I/O の束と、コーパスのキャッシュ

**Files:**
- Create: `rag_web/services.py`、`rag_web/corpora.py`
- Test: `tests/test_web_corpora.py`

**Interfaces:**
- Produces:
  - `services.py`:
    ```python
    @dataclass(frozen=True)
    class Services:
        stream_chat: Callable[[str, list[dict], float], Iterator[str]] = chat.stream_chat
        ask_json: Callable[[str, str], str] = chat.ask_json
        search: Callable[..., list] = retrieval.search          # (collection, query, index=, rerank=, rerank_floor=)
        build_index: Callable = retrieval.build_index
        open_store: Callable[[str], object] = store_module.open_store   # ingest.vector_store.open_store
        check_ollama: Callable[[], None] = embedder.check_ollama
        check_vlm: Callable[[], None] = vlm.check_vlm
        caption_image: Callable = vlm.caption_image
        check_reranker: Callable[[], None] = reranker.check_reranker
        rerank: Callable = reranker.rerank
        ingest_directory: Callable = ingest_source.ingest_directory
        ingest_uploads: Callable = ingest_source.ingest_uploads
        source_dir: Path = ingest_source.DEFAULT_SOURCE_DIR
        db_paths: Mapping[str, str] = {"internal": str(store.DB_PATH), "docs": str(store.DOCS_DB_PATH)}
    ```
    （`db_paths` は `field(default_factory=...)`）。`def caption_image_or_reason(services) -> tuple[Callable | None, str | None]` — `rag_chat_app.caption_image_or_reason` と同じ判定・文面（「図表の説明文化は行いません: {error}」）。
  - `corpora.py`:
    ```python
    CORPORA = ("internal", "docs")
    class CorpusCache:
        def __init__(self, services: Services): ...
        def collection(self, corpus: str): ...            # DB はパスごとに1回だけ開く
        def index(self, corpus: str): ...                 # (db_path, revision) を鍵に build_index
        def schema(self, corpus: str) -> dict[str, str]:  # (db_path, revision) を鍵に conditions.available_keys
        def rerank_callable(self) -> Callable | None: ... # 下の状態遷移
        def reranker_status(self) -> dict:                # {"state": "unchecked"|"checking"|"ready"|"failed", "reason": str|None}
    ```
    未知の corpus は `KeyError`。
- リランカー: 成功したら覚えて以後確認しない。失敗は覚えず `None` を返し（その回は並べ替えなし）、次の呼び出しで再確認する。確認中に来た呼び出しはロックで待つ（確認を重ねて走らせない）。失敗理由は `reranker_status()` に「Rerankerで並べ替えません: {error}」として残す。

- [ ] **Step 1: Write the failing tests**（`Services` に偽物を入れる。`open_store` は `ingest.vector_store.open_store(tmp_path / "x.sqlite3")` の本物でよい。`revision()` を進めるには `store.replace_source` で1件書く）

```python
def test_collection_opened_once_per_corpus(): ...                    # open_store の呼び出し回数 == 2（internal と docs を各2回取っても）
def test_index_rebuilt_when_revision_changes(): ...                  # build_index 呼び出し: 同じ revision で2回 → 1回、replace_source 後 → 2回
def test_index_not_shared_between_corpora_with_equal_revision(): ... # 両 DB の revision が同じでも別の索引を返す
def test_unknown_corpus_raises_key_error(): ...
def test_reranker_success_is_remembered(): ...                       # check_reranker 1回だけ、rerank_callable() is services.rerank
def test_reranker_failure_retried_next_time(): ...                   # 1回目 RerankError → None・state "failed"・reason に文面、2回目に成功 → callable
def test_reranker_check_not_run_concurrently(): ...                  # 2スレッドから同時に呼んでも check_reranker は1回（Event で1本目を止めて検証）
def test_caption_image_or_reason_reports_vlm_error(): ...            # check_vlm が VlmError("x") → (None, "図表の説明文化は行いません: x")
```

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_corpora.py -v` → FAIL
- [ ] **Step 3: Implement** — キャッシュは `threading.Lock` の下で辞書に持つ。鍵の理由（revision は DB ごとの独立カウンタ）は `rag_chat_app.get_index` の docstring から要点を写してコメントに残す。
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat: add corpus cache and external service bundle for rag_web`

### Task 4: チャット1回分の処理（イベント列）

**Files:**
- Create: `rag_web/answering.py`
- Test: `tests/test_web_answering.py`

**Interfaces:**
- Consumes: `Services`、`CorpusCache`（Task 3）
- Produces:
  ```python
  @dataclass(frozen=True)
  class ChatRequest:
      corpus: str            # "internal" | "docs"
      model: str
      temperature: float
      question: str
      history: list[dict]    # [{"role": "user"|"assistant", "content": str}]、今回の質問は含まない
  def system_prompt(today: date) -> str
  def hit_view(hit) -> dict  # {"caption": format_hit_caption(hit), "others": hit.all_citations()[1:], "text": hit.text}
  def answer_events(request: ChatRequest, services: Services, corpora: CorpusCache, today: date) -> Iterator[dict]
  ```
  イベントは `{"type": "note", "text"}` / `{"type": "delta", "text"}` / `{"type": "evidence", "hits": [hit_view...], "table": str|None}` / `{"type": "error", "message"}` / `{"type": "done", "display": str|None}`。**最後は必ず `done` か `error` のどちらか1つ。**
- 処理は `rag_chat_app.py` の 1061〜1218 行と同じ順序・同じ分岐にする:
  - `display = display_mode.detect(question)`
  - docs: 条件抽出なし。`contextual_query(question, history)` → `query_translation.translate_query(query, ask)` → `search(..., rerank=..., rerank_floor=DOCS_RERANK_FLOOR)` → `build_docs_prompt`
  - internal で `conditions.extract(...)` に条件あり: `extract_ranking` → `catalog.select` / `relaxations` / `exceeding` → プロンプトには beyond なしの表、`evidence.table` には beyond 入りの表 → `build_catalog_prompt`
  - internal で条件なし: `contextual_query` → `search(..., rerank=...)` → `build_prompt`
  - `ask` は `lambda prompt: services.ask_json(request.model, prompt)`
  - `EmbeddingError` → `error`「検索できませんでした: {e}」（note は出さない）
  - `extraction.failed` かつ検索成功 → 最初に `note`
  - 生成: messages = `[system] + history + [{"role":"user","content": user_content}]`、`answer_text.without_label(services.stream_chat(model, messages, temperature))`。`display` が無ければさらに `strip_html_tags_stream` を通す。断片ごとに `delta`。
  - 生成後に `evidence`、最後に `done`。`ChatError` → `error`「回答を生成できませんでした: {e}」。
  - それ以外の例外 → `error`「回答を生成できませんでした: {e}」を出して終わる（ログには `logger.exception` で残す）。

- [ ] **Step 1: Write the failing tests**（`search` / `stream_chat` / `ask_json` は偽物。`Hit` は `ingest.retrieval.Hit(text=..., distance=0.1, occurrences=[{"source": "a.pdf", ...}])` で作る。条件抽出の経路は `monkeypatch` で `rag_web.answering.conditions.extract` 等を差し替えてよい）

```python
def test_internal_search_streams_deltas_then_evidence_then_done(): ...
    # types == ["delta", "delta", "evidence", "done"]、evidence.hits[0] == hit_view(hit)
def test_history_is_used_for_query_and_messages(): ...
    # search に渡った query == contextual_query(question, history)、stream_chat の messages[0]["role"] == "system"、messages[1:-1] == history
def test_docs_corpus_translates_query_and_skips_condition_extraction(): ...
    # conditions.extract が呼ばれない、search に rerank_floor=DOCS_RERANK_FLOOR
def test_catalog_path_shows_beyond_only_on_screen(): ...
    # evidence.table に beyond の型番が含まれ、stream_chat に渡した最後の user content には含まれない
def test_extraction_failure_adds_note_first(): ...           # 先頭が {"type":"note","text":"条件を解釈できませんでした。通常の検索で回答します。"}
def test_embedding_error_yields_search_error_without_note(): ...
def test_chat_error_yields_generation_error(): ...
def test_unexpected_error_mid_stream_ends_with_error_event(): ...  # delta 1つの後 RuntimeError → [..., delta, error]、done は出ない
def test_display_mode_keeps_html_and_reports_display(): ...  # "マークダウンで表示して" → <br> が残り done.display == "markdown"
def test_answer_label_is_stripped(): ...                     # "答え：東京" → delta の連結 == "東京"
def test_system_prompt_uses_given_date(): ...                # system_prompt(date(2026, 11, 22)) に "2026年11月22日"
```

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_answering.py -v` → FAIL
- [ ] **Step 3: Implement** `rag_web/answering.py`
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat: port chat answering pipeline to an event generator`

### Task 5: ジョブと共有操作のロック

**Files:**
- Create: `rag_web/jobs.py`
- Test: `tests/test_web_jobs.py`

**Interfaces:**
- Produces:
  ```python
  class SharedOperationBusy(Exception): ...
  class JobStore:
      def __init__(self, clock=time.monotonic, ttl_s: float = 600): ...
      def start(self, work: Callable[[], dict], *, shared: bool) -> str:
          # shared=True なら共有ロックを取れなければ SharedOperationBusy（待たない）。
          # 別スレッドで work() を実行。id は secrets.token_urlsafe(24)
      def get(self, job_id: str) -> dict | None:
          # {"state": "running"} / {"state": "done", "result": dict} / {"state": "failed", "error": str}
          # done / failed を一度返したらそのジョブは消す。終了から ttl_s を過ぎたものも消す（get のたびに掃除）
      def shared_busy(self) -> bool: ...
  ```
  `work()` が例外を出したら `failed`、`error` は `str(e)`（ログには `logger.exception`）。共有ロックは成功・失敗どちらでも解放する。

- [ ] **Step 1: Write the failing tests**（スレッドの完了待ちは `threading.Event` で同期し、`sleep` に頼らない）

```python
def test_job_runs_and_result_is_returned_once(): ...           # running → done(result) → 2回目の get は None
def test_failed_job_reports_error(): ...
def test_second_shared_job_is_rejected_while_first_runs(): ... # SharedOperationBusy、shared_busy() True
def test_non_shared_jobs_run_concurrently(): ...
def test_shared_lock_released_after_failed_job(): ...          # 失敗後に shared ジョブを start できる
def test_finished_job_expires_after_ttl(): ...                 # 偽 clock で 601 秒進める → None
def test_job_ids_are_unguessable(): ...                        # 2件の id が異なり長さ >= 32
```

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_jobs.py -v` → FAIL
- [ ] **Step 3: Implement** `rag_web/jobs.py`（`threading.Thread(daemon=True)`）
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat: add in-memory job store with a shared-operation lock`

### Task 6: API（`create_app`）

**Files:**
- Create: `rag_web/app.py`
- Test: `tests/test_web_app.py`

**Interfaces:**
- Consumes: Task 1〜5 のすべて
- Produces: `def create_app(settings: WebSettings, services: Services | None = None, jobs: JobStore | None = None, throttle: LoginThrottle | None = None, today: Callable[[], date] = date.today) -> FastAPI`、定数 `CSRF_HEADER = "X-Requested-With"`、`CSRF_VALUE = "rag-web"`、`COOKIE_NAME = "rag_web_session"`、`STATIC_DIR`
- ルート（`docs_url` / `redoc_url` / `openapi_url` は None。エンドポイントは同期 `def`）:
  - `GET /` → `static/index.html`、`GET /app.js`、`GET /vendor/{name}`（`STATIC_DIR/"vendor"` 直下のファイルだけ。パス区切りを含む名前は 404）
  - `GET /api/status?corpus=internal|docs` → `{"models": MODELS, "corpus", "chunk_count", "source_dir_name", "docs_ingest_command": DOCS_INGEST_COMMAND, "reranker": corpora.reranker_status(), "shared_busy": jobs.shared_busy()}`。`DOCS_INGEST_COMMAND` は `rag_chat_app.py` と同じ文字列を `rag_web/app.py` に定数で持つ（③で Streamlit 側が消えるまでの一時的な重複）。
  - `GET /api/me` → `{"admin": bool}`。`POST /api/login`（`{password}`、1〜1024文字）→ 成功で Cookie（`httponly=True`, `samesite="strict"`, `secure=settings.cookie_secure`, `max_age=settings.session_ttl_s`）、失敗 401「パスワードが違います」、ロック中 429（`Retry-After`）。接続元は `request.client.host`。`POST /api/logout`。
  - `POST /api/chat` → 本文 `ChatBody(corpus: Literal["internal","docs"], model: str, temperature: float = Field(ge=0, le=1), question: str = Field(min_length=1, max_length=4000), history: list[Message] = Field(max_length=100))`、`Message(role: Literal["user","assistant"], content: str = Field(max_length=100_000))`。`model not in MODELS` → 422。応答は `StreamingResponse(media_type="application/x-ndjson")`、1行に `json.dumps(event, ensure_ascii=False)`。
  - `POST /api/ingest`（管理者のみ）→ `services.check_ollama()` が `EmbeddingError` なら 503（本文 detail は理由）。`caption_image_or_reason` → `jobs.start(..., shared=True)` → `{"job_id"}`。結果は `{"report": format_report(report), "notice": reason}`。
  - `GET /api/uploads` → `{"caption": <注意書き>, "items": [{"key", "name"}]}`（`UPLOAD_PREFIX` で始まるものだけ、昇順）。
  - `POST /api/uploads` → 本文 `{files: [{name, content_b64}]}`（1〜50件）。名前は `Path(name).name == name` かつ `..` を含まず、拡張子が `SUPPORTED_SUFFIXES` に入ること。base64 デコード後の合計が `settings.max_upload_bytes` 超なら 413。`check_ollama` 失敗は 503。ジョブ（shared）の中で一時フォルダへ書いて `ingest_uploads(paths, collection, caption_image=...)`、結果 `{"report", "notice"}`。
  - `DELETE /api/uploads/{key:path}` → `UPLOAD_PREFIX` で始まらなければ 400。共有ロックを取って `store.replace_source(collection, key, [], [])`（取れなければ 409）。
  - `GET /api/jobs/{job_id}` → `jobs.get`、無ければ 404。
  - `SharedOperationBusy` → 409「ほかの人が取り込み中です。終わってからもう一度実行してください」。

- [ ] **Step 1: Write the failing tests**（`TestClient(create_app(settings, services=fake_services, ...))`。ハッシュは `n=2**10`。DB は `tmp_path` の本物の sqlite）

```python
def test_index_and_assets_are_served_without_external_urls(): ...
    # GET / と /app.js が 200、本文に "http://" "https://" を含まない（vendor の中身は対象外）
def test_post_without_csrf_header_is_rejected(): ...             # /api/chat, /api/login, /api/uploads → 403
def test_status_reports_counts_and_reranker_state(): ...
def test_chat_streams_ndjson_events(): ...                       # 行ごとに json.loads、最後が done
def test_chat_rejects_system_role_and_oversized_history(): ...   # role "system" → 422、history 101件 → 422、stream_chat は呼ばれない
def test_chat_rejects_unknown_model_and_out_of_range_temperature(): ...
def test_ingest_requires_login(): ...                            # 未ログイン 401、ingest_directory は呼ばれない
def test_ingest_runs_as_job_after_login(): ...                   # login → POST /api/ingest → job_id → GET /api/jobs/{id} が done、report が format_report の文面
def test_ingest_returns_503_when_ollama_down(): ...
def test_login_throttled_after_repeated_failures(): ...          # 5回失敗 → 429
def test_logout_revokes_session(): ...
def test_upload_rejects_unsafe_names_and_suffixes(): ...         # "../a.pdf", "a/b.pdf", "a.exe" → 400、ingest_uploads 呼ばれない
def test_upload_too_large_returns_413(): ...
def test_upload_runs_as_shared_job_and_lists_item(): ...         # 完了後 GET /api/uploads に name が出る
def test_second_shared_operation_returns_409(): ...              # 1本目を Event で止めたまま 2本目の upload / ingest / delete が 409
def test_delete_upload_only_accepts_upload_keys(): ...           # "source/a.pdf" → 400、"uploads/a.pdf" → 200 で一覧から消える
def test_unknown_job_returns_404(): ...
```

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_app.py -v` → FAIL
- [ ] **Step 3: Implement** `rag_web/app.py`（Batch_Exec `app.py` の `require_csrf_header` / `require_admin` / login・logout の書き方に倣う。Task 7 の前なので、`static/index.html` と `static/app.js` は1行の仮ファイルを置き、Task 7 で置き換える）
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat: add FastAPI routes for chat, status, login, ingest and uploads`

### Task 7: 画面（HTML/JS）と同梱ライブラリ

**Files:**
- Create/Replace: `rag_web/static/index.html`、`rag_web/static/app.js`、`rag_web/static/vendor/`（Markdown 描画・HTML サニタイズ・Mermaid の各1ファイル、minified、版固定）
- Modify: `docs/依存関係一覧.md`（3ライブラリの名前・版・ライセンス・取得元を追記）
- Test: `tests/test_web_app.py` に1件追加

**Interfaces:**
- Consumes: Task 6 の API。状態変更はすべて `X-Requested-With: rag-web` を付ける。
- 画面の構成は設計書「画面」の図のとおり。色の変数・ダークモード・`header` / `section` の見た目は Batch_Exec `godoc_agent/web/static/index.html` の `:root` と基本スタイルを写す。

振る舞い（今の Streamlit と同じにするもの）:
- 設定欄: モデル（`/api/status` の models）、Temperature（range 0〜1、step 0.1、既定 0.3）、検索対象（ラジオ）、チャンク数、技術ドキュメントが空なら警告＋CLI コマンド／空でなければ「更新は CLI で行います: …」、社内資料なら「取り込み元: {source_dir_name}/」、リランカーが failed なら警告、会話履歴をリセット。
- 管理者: 未ログインなら「管理者ログイン」（パスワード欄）。ログイン中のみ「差分を取り込む」「ログアウト」を出す。取り込み中は「取り込み中…（図表があると時間がかかります）」を出し、`/api/jobs/{id}` を2秒おきに取りに行く。結果（report）は成功表示、notice は警告表示。
- 資料をアップロード: `<dialog>`。注意書き、複数ファイル選択（accept は対応拡張子）、取り込む、アップロード済み一覧と削除。409 / 413 / 503 は本文の detail をそのまま表示。
- 検索対象を切り替えたら、履歴があれば消して「検索対象を切り替えたため、会話履歴をリセットしました。」を出す。
- チャット: 送信で user 発言を表示 → `fetch` で `/api/chat` → `ReadableStream` を行単位で読み、`delta` を連結して表示。`note` は警告、`error` は赤で表示し本文として履歴に残す、`evidence` は折りたたみ「参考にした情報（N件）」（caption、「同じ記述: a ／ b」、本文）と「絞り込んだ一覧」（table を等幅で）。
- 回答の描画: `display` が null なら Markdown → サニタイズ → DOM。`display` があればコードブロック（コピーボタン付き）、`mermaid` ならその下に図（`answer_text.mermaid_definitions` と同じく ```mermaid フェンスの中身だけを描く）。null のときも ```mermaid フェンスは図にする（今の `st.write` と同じ）。
- 生成中は入力欄・送信・検索対象のラジオを無効化。`done` / `error` / 通信断（`fetch` の例外・ストリームの途中切れ）のどれでも必ず戻す。通信断のときは「回答を生成できませんでした: 通信が切れました」を履歴に残す。
- 履歴（`{role, content}` だけ）は JS の配列に持ち、送信時に今回の質問を除いて `history` に入れる。

- [ ] **Step 1: Write the failing test**

```python
def test_vendor_assets_are_served_and_referenced_locally():
    # index.html が参照する /vendor/*.js がすべて 200 で返る
```

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_app.py -v` → 新しいテストが FAIL
- [ ] **Step 3: ライブラリを取得して `vendor/` に置く**（npm レジストリの tarball から取り出す。版は公開から2週間以上経ったものに固定）。`docs/依存関係一覧.md` に追記。
- [ ] **Step 4: Implement** `index.html` / `app.js`
- [ ] **Step 5: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_app.py -v` → PASS
- [ ] **Step 6: ブラウザで確認**（Task 8 の起動手順で立て、Playwright MCP で操作）。次をすべて確認し、結果を PR 本文に書く:
  - 社内資料で質問 → 逐次表示・参考にした情報が出る
  - 検索対象の切り替えで履歴が消え、メッセージが出る
  - 「マークダウンで表示して」でコードブロック、Mermaid で図
  - 回答に `<img src=x onerror=alert(1)>` を含ませても実行されない（偽の stream_chat を使う起動か、サニタイズ関数を直接 evaluate して確かめる）
  - 2つのタブで同時に質問できる。片方でアップロード中にもう片方のアップロードが 409 の文面になる
  - 未ログインで「差分を取り込む」が見えない／ログイン後に見えて実行できる
  - Ollama を止めた状態で「検索できませんでした: …」が出て、入力欄が戻る
  - 幅 400px で縦に積まれ、横スクロールが出ない
- [ ] **Step 7: Commit** `feat: add browser UI for chat and sidebar operations`

### Task 8: 起動（`python -m rag_web`）と手順

**Files:**
- Create: `rag_web/__main__.py`、`run_web.ps1`
- Modify: `README.md`（「新しい画面（移行中・開発機で試用）」の節を追加: ハッシュの作り方、`.env` の項目、起動コマンド、URL `http://127.0.0.1:8502`。Streamlit の手順はそのまま残す）
- Test: `tests/test_web_main.py`

**Interfaces:**
- Produces: `def main() -> int` — `load_dotenv()` を `ingest` の import より先に行う（`rag_chat_app.py` 冒頭と同じ理由をコメントに書く）。`load_web_settings()` と `validate_password_hash()` の `ValueError` は「エラー: …」を stderr に出して 2 を返す。成功時は `uvicorn.run(app, host, port)`。
- `run_web.ps1`: `run_streamlit.ps1` と同じ構造（8502 の LISTEN プロセスを止める → `rag_env` → `myvenv313` の順で python を探す → `-m rag_web`）。

- [ ] **Step 1: Write the failing tests**

```python
def test_main_reports_missing_hash_and_returns_2(monkeypatch, capsys): ...   # 環境変数なし → 2、stderr に "RAG_WEB_ADMIN_PASSWORD_HASH"
def test_main_starts_uvicorn_with_settings(monkeypatch): ...                 # uvicorn.run を偽物にし、host/port が設定どおり
```
`load_dotenv` は `monkeypatch` で無効化し、手元の `.env` に左右されないようにする。

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -m pytest tests/test_web_main.py -v` → FAIL
- [ ] **Step 3: Implement** `__main__.py`、`run_web.ps1`、README の節
- [ ] **Step 4: Run** 全体 `.\rag_env\Scripts\python.exe -m pytest -q` → 失敗 0
- [ ] **Step 5: 起動確認** `.\run_web.ps1` → `http://127.0.0.1:8502` が開く
- [ ] **Step 6: Commit** `feat: add rag_web entry point and startup script`
