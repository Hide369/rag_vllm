# 技術ドキュメントを Web 画面の管理者専用にする 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Web 画面で、技術ドキュメント（`docs` コーパス）の検索・参照・取り込み案内を管理者だけに見せ、API でも管理者以外を断る。

**Architecture:** `rag_web/app.py` に「技術ドキュメントを使う要求なら管理者か確かめる」判定を1つ足し、`/api/status`・`/api/chat`・`/api/cowork` の入口で呼ぶ。画面は技術ドキュメントの要素に `admin-only` クラスを付けて最初から隠し、`applyAdmin()` がログイン状態に合わせて出し入れし、管理者でなくなったら検索対象を社内資料へ戻す。

**Tech Stack:** FastAPI / pydantic、素の JavaScript（ビルドなし）、pytest、Windows PowerShell と仮想環境 `rag_env`

**Spec:** `docs/superpowers/specs/2026-11-26-docs-corpus-admin-only-design.md`

## Global Constraints

- 断るときは 401、文言は `管理者としてログインしてください`（既存の `require_admin` と同じ。定数 `ADMIN_REQUIRED_MESSAGE` にまとめて両方で使う）。
- 判定は既存の `is_admin(request)`（セッション Cookie）を使う。新しい認証の仕組みを作らない。
- `docs_ingest_command` は `corpus=docs` の `/api/status` 応答にだけ入れる。
- 管理者として通る場合の振る舞いは今と変えない。
- テストの実行: `.\rag_env\Scripts\python.exe -m pytest <対象> -q`。合格は終了コード 0・失敗 0 の確認後にだけ報告する。
- 画面の文言（そのまま使う）:
  - 管理者でなくなって検索対象を戻したとき（履歴なし）: `管理者としてログインしていないため、検索対象を社内資料に戻しました。`
  - 同（履歴あり）: `管理者としてログインしていないため、検索対象を社内資料に戻し、会話履歴をリセットしました。`
- コミットはコンベンショナルコミット形式・英語。

## Review Focus

- 期限切れのセッションで技術ドキュメントを選んだまま質問した → 401 のエラーが回答欄に出て、生成が終わったあと検索対象が社内資料へ戻る（Task 2 の手動確認 4）。
- 利用者がブラウザの再読み込みで「技術ドキュメント」が選ばれた状態に戻った → `/api/status` の 401 を見せずに社内資料で表示される（起動時は `loadMe` を待ってから `loadStatus`。Task 2 の手動確認 3）。
- 利用者がソースコード形式を選んだ → 隠れた「技術ドキュメントを参照」が自動で入って `use_docs: true` が送られ、401 になる、ということが起きない（Task 2 の静的テストと手動確認 5）。
- 技術ドキュメントの DB の埋め込みモデルが設定と食い違っている → 利用者には 503 の詳細ではなく 401 が返る（判定は DB を開く前。Task 1 のテスト）。
- Cowork を生成中にセッションが切れた → 生成中に検索対象や会話を書き換えない（`applyAdmin` は生成中は戻す処理をしない。Task 2 の静的テスト）。

---

### Task 1: サーバーで技術ドキュメントの利用を管理者に限る

**Files:**
- Modify: `rag_web/app.py`（定数群 55-110 行付近、`require_admin` 343 行付近、`status` 410 行付近、`chat` 423 行付近、`cowork` 522 行付近、モジュール docstring 1-10 行）
- Test: `tests/test_web_app.py`

**Interfaces:**
- Produces: `rag_web.app.ADMIN_REQUIRED_MESSAGE: str`。`create_app` の中のクロージャ `require_docs_access(request: Request, uses_docs: bool) -> None`（`uses_docs` が真で `is_admin(request)` が偽なら `HTTPException(401, ADMIN_REQUIRED_MESSAGE)`）。
- API の振る舞い（Task 2 が前提にする）: 利用者の `GET /api/status?corpus=docs`・`POST /api/chat {"corpus":"docs"}`・`POST /api/cowork {"use_docs":true}` は 401 `{"detail": "管理者としてログインしてください"}`。社内資料の `/api/status` 応答には `docs_ingest_command` キーが無い。

- [ ] **Step 1: 失敗するテストを書く**

  既存テストの直し:
  - `test_status_reports_counts_and_reranker_state`: 期待する辞書から `"docs_ingest_command"` を消す。`corpus=docs` を取る前に `_login(client)` を入れる。
  - `test_status_of_an_empty_docs_corpus_carries_the_ingest_command`: 先頭で `_login(client)`。

  新しいテスト（`test_status_reports_source_dir_name_only_to_admin` の近くに置く）:

  ```python
  ADMIN_REQUIRED = "管理者としてログインしてください"

  def test_docs_status_is_for_admin_only(client, fake):
      response = client.get("/api/status", params={"corpus": "docs"})
      assert response.status_code == 401
      assert response.json()["detail"] == ADMIN_REQUIRED
      _login(client)
      assert client.get("/api/status", params={"corpus": "docs"}).status_code == 200
      client.post("/api/logout", headers=CSRF)
      assert client.get("/api/status", params={"corpus": "docs"}).status_code == 401

  def test_internal_status_does_not_carry_the_docs_ingest_command(client):
      _login(client)
      assert "docs_ingest_command" not in client.get("/api/status").json()

  def test_docs_access_is_checked_before_opening_the_db(client, fake):
      # 食い違った DB でも、利用者には 503 の詳細（モデル名）ではなく 401 を返す
      _record_other_embedding_model(fake.db_paths["docs"])
      response = client.get("/api/status", params={"corpus": "docs"})
      assert response.status_code == 401
      assert "qwen3-embedding" not in response.text

  def test_docs_chat_is_for_admin_only(client, fake):
      response = client.post("/api/chat", json=_chat_body(corpus="docs"), headers=CSRF)
      assert response.status_code == 401
      assert response.json()["detail"] == ADMIN_REQUIRED
      assert fake.chat_calls == []
      _login(client)
      assert client.post("/api/chat", json=_chat_body(corpus="docs"), headers=CSRF).status_code == 200

  def test_cowork_with_docs_is_for_admin_only(client, fake, jobs):
      response = client.post("/api/cowork", json=_cowork_body(use_docs=True), headers=CSRF)
      assert response.status_code == 401
      assert response.json()["detail"] == ADMIN_REQUIRED
      assert fake.ask_text_calls == [] and fake.ask_json_calls == []
      # 技術ドキュメントを使わない依頼は、利用者も今までどおり通る
      _cowork_result(client, jobs, _cowork_body(use_docs=False))
      _login(client)
      _cowork_result(client, jobs, _cowork_body(use_docs=True))
  ```

  `test_cowork_with_docs_is_for_admin_only` は `_cowork_body` の定義より後ろ（Cowork の節）に置く。

- [ ] **Step 2: 失敗を確かめる**

  Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_web_app.py -q -k "docs or status"`
  Expected: 新しいテストが 401 を期待して FAIL（現状は 200）、`test_status_reports_counts_and_reranker_state` が `docs_ingest_command` の有無で FAIL。

- [ ] **Step 3: 実装する（`rag_web/app.py`）**

  - モジュール定数 `ADMIN_REQUIRED_MESSAGE = "管理者としてログインしてください"` を足し、`require_admin` の文言をこれに置き換える。
  - `create_app` 内に `require_docs_access(request, uses_docs)` を足す（Interfaces のとおり）。
  - `status(request, corpus)`: 先頭で `require_docs_access(request, corpus == "docs")`（DB を開く `corpora.collection` より前）。`docs_ingest_command` は `corpus == "docs"` のときだけ辞書に入れる。
  - `chat(body, request)`: `request: Request` を受け取り、`ChatRequest` を作る前に `require_docs_access(request, body.corpus == "docs")`。中の変数名 `request`（`ChatRequest`）は衝突するので `chat_request` などに改める。
  - `cowork(body, request)`: 同じく `request: Request` を受け、`_decode_uploads` より前に `require_docs_access(request, body.use_docs)`。`CoworkRequest` を入れる変数は `cowork_request` などに改める。
  - docstring（4 行目付近）に「技術ドキュメントの検索・参照（チャット・Cowork・状態）も管理者だけ」を1行足し、110 行付近のコメント「技術ドキュメントは CLI で取り込む」はそのまま。

- [ ] **Step 4: 通ることを確かめる**

  Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_web_app.py -q`
  Expected: 全件 PASS、終了コード 0。

- [ ] **Step 5: コミット**

  ```powershell
  git add rag_web/app.py tests/test_web_app.py
  git commit -m "feat: restrict tech-doc search and status to admins in the web API"
  ```

---

### Task 2: 画面で技術ドキュメントを管理者にだけ見せる

**Files:**
- Modify: `rag_web/static/index.html`（169 行のラジオ、231・240 行の案内文、269 行のチェック）
- Modify: `rag_web/static/app.js`（`setGenerating` 547 行付近、`applyAdmin` 364 行付近、ログイン/ログアウト 381-408 行、`ask` 610 行付近、送信ハンドラ 704 行付近、`syncOutputSuffix` 749 行付近、`runCowork` の `use_docs` 867 行付近、検索対象のラジオ 978 行付近、起動処理 1004 行付近）
- Modify: `README.md`（「技術ドキュメントの取り込み」317 行付近、Cowork の節 529・546 行付近）、その他 `grep -rn "技術ドキュメント" docs/*.md` で画面の操作を説明している箇所
- Test: `tests/test_web_app.py`（静的ファイルの文字列テスト）

**Interfaces:**
- Consumes: Task 1 の API の振る舞い（利用者の技術ドキュメント利用は 401、`docs_ingest_command` は docs の状態にだけ入る）。
- Produces（app.js 内）:
  - `applyAdmin(): void` — `.admin-only` を出し入れし、管理者でなく生成中でもなければ `switchCorpus` で社内資料へ戻す。
  - `switchCorpus(corpus: string, resetMessage: string, plainMessage: string): void` — ラジオの選択と `state.corpus` を合わせ、履歴があれば `clearChat()` して `resetMessage`、無ければ `plainMessage` を `corpus-msg` に出し、`chunk-count` を `-` にして `loadStatus()`。
    - 利用者が切り替えたとき: `("…", "検索対象を切り替えたため、会話履歴をリセットしました。", "")`（今の文言のまま）
    - 管理者でなくなったとき: Global Constraints の2文（履歴あり / なし）
  - `ask(question): Promise<boolean>` — サーバーが 401 を返したら true。

- [ ] **Step 1: 失敗するテストを書く**

  ```python
  def _html():
      return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

  def _script():
      return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

  def test_docs_controls_start_hidden_and_are_marked_admin_only():
      html = _html()
      # 技術ドキュメントのラジオと Cowork のチェックは、管理者と分かるまで出さない
      for marker in ('value="docs"', 'id="cowork-docs"'):
          start = html.rindex("<label", 0, html.index(marker))
          tag = html[start:html.index(">", start)]
          assert "admin-only" in tag and "hidden" in tag, marker
      # Cowork の案内文の「技術ドキュメント」も admin-only の中にだけ書く
      start = html.index('id="cowork-help"')
      guide = html[start:html.index("</details>", start)]
      for span in re.findall(r'<span class="admin-only hidden">[^<]*</span>', guide):
          guide = guide.replace(span, "")
      assert "技術ドキュメント" not in guide

  def test_script_never_sends_docs_requests_for_guests():
      script = _script()
      assert "use_docs: state.admin && $(\"cowork-docs\").checked" in script
      assert "state.admin && state.coworkOptions.source_suffixes.includes" in script
      assert '$("logout").disabled = generating' in script
      assert "管理者としてログインしていないため、検索対象を社内資料に戻し" in script
      assert 'querySelectorAll(".admin-only")' in script
  ```

  `test_index_has_cowork_controls` の「技術ドキュメントを参照」の確認はそのまま残る（文言は HTML にある）。

- [ ] **Step 2: 失敗を確かめる**

  Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_web_app.py -q -k "admin_only or guests"`
  Expected: 2件 FAIL（`admin-only` が無い）。

- [ ] **Step 3: `index.html` を直す**

  - 169 行と 269 行の `<label>` に `class="admin-only hidden"`。
  - 231 行: `社内資料<span class="admin-only hidden">や技術ドキュメント</span>を検索し`。
  - 240 行: `参照する資料（社内資料<span class="admin-only hidden">・技術ドキュメント</span>）にチェックを入れます。`
  - 案内文は1行に書く（既存コメントのとおり、文の途中で改行しない）。

- [ ] **Step 4: `app.js` を直す**

  - `setGenerating`: `$("logout").disabled = generating;` を足す（理由のコメント: 生成中に検索対象や会話を書き換えると表示が壊れる）。
  - `switchCorpus` を作り、検索対象ラジオの `change` ハンドラ（978 行付近）はこれを呼ぶ形にする。
  - `applyAdmin`: `.admin-only` の各要素に `classList.toggle("hidden", !state.admin)`。管理者でなければ `$("cowork-docs").checked = false`。さらに `!state.admin && !state.generating && state.corpus === "docs"` なら `switchCorpus("internal", …)`。
  - ログアウトのハンドラ: `applyAdmin()` のあとは追加の処理不要（戻すときは `switchCorpus` が `loadStatus` する）。
  - `syncOutputSuffix`: 自動チェックを `state.admin && state.coworkOptions.source_suffixes.includes(...)` にする。
  - `runCowork`: `use_docs: state.admin && $("cowork-docs").checked`。
  - `ask`: `response.status === 401` のときも既存の `!response.ok` の扱い（エラー表示）にし、関数の戻り値で true を返す。それ以外は false。
  - 送信ハンドラ: `ask` の戻り値を受け、`setGenerating(false)` のあとで、true なら `await loadMe()`、そうでなければ `applyAdmin()`（Cowork の 401 では `runJob` が生成中に `loadMe` を呼ぶが、そのとき戻す処理は飛ばしているので、ここで改めて行う）。そのあと既存の `loadStatus()`。
  - 起動処理: `loadMe(); loadStatus();` を `loadMe().then(loadStatus);` にする（再読み込みで「技術ドキュメント」が復元されても、利用者なら先に社内資料へ戻ってから状態を取る）。

- [ ] **Step 5: テストを通す**

  Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_web_app.py -q`
  Expected: 全件 PASS、終了コード 0。

- [ ] **Step 6: README と docs を直す**

  - README「技術ドキュメントの取り込み」: Web 画面では技術ドキュメントは管理者にだけ表示され、利用者は社内資料だけを検索する旨を1〜2文。
  - README の Cowork の節: 「技術ドキュメント」のチェックとソースコード形式での自動チェックは、管理者としてログインしているときだけである旨。
  - `grep -rn "技術ドキュメント" docs/*.md README.md` で、画面の操作を説明している他の箇所があれば同じ趣旨に直す（設計・計画の過去の文書 `docs/superpowers/` は直さない）。

- [ ] **Step 7: 実際の画面で確かめる**

  `.env` に `RAG_WEB_ADMIN_PASSWORD_HASH` があることを確かめ、`run_web.ps1` で起動し、Playwright でブラウザから確かめる（vLLM が無くても 1〜3・5 は確認できる。4 は vLLM が無ければ 401 の表示と戻りだけ確認する）。
  1. 利用者: 検索対象に「技術ドキュメント」が無い。Cowork に「技術ドキュメントを参照」が無く、案内文に「技術ドキュメント」の文字が無い。
  2. ログイン: 上の3つが出る。「技術ドキュメント」を選ぶと取り込みコマンドの案内が出る。ログアウトすると消え、検索対象が社内資料に戻り、`管理者としてログインしていないため、検索対象を社内資料に戻しました。` が出る。
  3. 管理者で「技術ドキュメント」を選んだまま、別のタブでログアウトしてから元のタブを再読み込み: 状態取得のエラーが出ず、社内資料で表示される。
  4. 管理者で「技術ドキュメント」を選び、別のタブでログアウトしてから元のタブで質問: 回答欄に `管理者としてログインしてください` が出て、そのあと検索対象が社内資料へ戻る。
  5. 利用者で Cowork の出力形式に `.py` を選ぶ: 送信した要求の `use_docs` が false（`browser_network_request` で本文を見る）。

- [ ] **Step 8: コミット**

  ```powershell
  git add rag_web/static/index.html rag_web/static/app.js tests/test_web_app.py README.md docs
  git commit -m "feat: show tech-doc controls in the web UI to admins only"
  ```
