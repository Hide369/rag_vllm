# FastAPI 画面 段階③（Streamlit の撤去と配置手順の書き換え）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Streamlit 版（`rag_chat_app.py`）とその依存を取り除き、本番配置の手順を FastAPI 版（`rag_web/`）向けに書き換える。あわせて設計書「段階③への申し送り」のうち、コードで対処するものを片づける。

**Architecture:** 新しい機能は足さない。①まず旧テストの振る舞いが新テストで押さえられているかを突き合わせ、欠けを埋める。②次に Streamlit を消し、コード中の参照を新しい置き場所へ向け直す。③IIS 配下での運用に要る設定（プロキシの信頼範囲、スレッドプール、Cowork の同時数）を足す。④最後に文書を書き換える。

**Tech Stack:** Python 3.13（`rag_env`）、FastAPI 0.162.2、uvicorn 0.54.0、anyio 4.14.2、pytest。IIS + ARR（Windows）、Nginx（Linux 版の手順書）。

**Spec:** `docs/superpowers/specs/2026-11-22-fastapi-web-ui-design.md`（特に「段階と、各段階の完了条件」の3と「段階③への申し送り」）

## Global Constraints

- `ingest/` と `docgen/` の振る舞いは変えない。例外は `VectorStore.close()` の追加（Task 3）とコメントの書き換え（Task 2）だけ。
- 利用者に見せる文言は日本語。既存のエラー文の文面は変えない。
- 環境変数で受ける設定は `rag_web/settings.py` の `load_web_settings` で読む。不正な値は変数名入りの `ValueError` で起動時に止める（既存の方針）。
- 新しい依存パッケージは足さない。`requirements.txt` から `streamlit` を消す。
- テストは `.\rag_env\Scripts\python.exe -m pytest -q` で失敗 0。`tests/test_web_*.py` は `-W error` でも失敗 0。
- コミットは英語のコンベンショナルコミット。
- `docs_source/`、`docs_sources.toml`、技術ドキュメントのコーパスとしての「streamlit.md」に触れた実測の記述（`ingest/retrieval.py`・`ingest/chunker.py`・`ingest/parsers/*`・`scripts/check_retrieval.py`・`tests/test_query_translation.py` などの Streamlit の質問例）は**消さない**。これらはアプリではなく、取り込んだ文書の名前である。
- `sqlite-vector-store/` は別の小プロジェクトなので触らない。
- ノートブック（`colab/*.ipynb`）を編集する前に、利用者が VS Code でそのノートブックを閉じていることを確かめる（開いたままだと上書きされた事故がある）。編集後はコミット済みの内容を `git show HEAD:<path>` で確かめる。

## Review Focus

1. **ARR 越しのログイン試行制限**: ARR は `X-Forwarded-For` に「IP:ポート」を入れることがある。ポートごとに別人として数えると、制限をすり抜けられる。同じ IP でポートだけ違う失敗は、同じ接続元として数えること（Task 3 のテスト）。
2. **信頼しない接続元が送った `X-Forwarded-For`**: `RAG_WEB_FORWARDED_ALLOW_IPS` に無い接続元がこのヘッダーを付けても、接続元は書き換わらないこと（Task 3 のテスト）。
3. **Cowork の枠が失敗で漏れない**: 生成が例外で終わっても、同時実行の枠は返ること。枠が埋まっている間は 503 を返し、空けば次の生成を受け付けること（Task 3 のテスト）。
4. **Streamlit の残骸**: `streamlit` を import するコードが1つも残らないこと。`requirements.txt` から消しても、Streamlit が間接的に入れていたパッケージ（例: `pandas`、`pyarrow`、`requests`）をコードが直接 import していれば、明示的な依存として残すこと（Task 2 の確認手順）。
5. **旧テストだけが押さえていた振る舞い**: 旧テストを消す前に、新しいテストで同じ振る舞いが押さえられていること（Task 1 の突き合わせ表）。

---

### Task 1: 旧テストとの突き合わせ（振る舞いの穴を埋める）

設計書の要求「③で消すのは、同じ振る舞いが新しいテストで押さえられていることを確かめてからにする」に応える。

**Files:**
- Create: `docs/superpowers/plans/2026-11-22-fastapi-web-ui-stage3-parity.md`
- Modify (必要なときだけ): `tests/test_web_app.py`、`tests/test_web_answering.py`、`tests/test_web_cowork.py`、`tests/test_web_corpora.py`、`rag_web/*.py`
- Read: `tests/test_rag_chat_app.py`（49件）、`tests/test_rag_chat_app_cowork.py`（30件）

**Interfaces:**
- Consumes: 段階①②の `rag_web` 全体（変えない）
- Produces: 突き合わせ表。Task 2 はこの表を見てから旧テストを消す。

- [ ] **Step 1: 突き合わせ表を作る**

旧テストの1件ごとに、表に1行を書く。列は「旧テスト名 / 確かめている振る舞い（1行） / 区分 / 新しいテスト（`ファイル::関数名`）または理由」とする。区分は次の4つのどれかにする。

- `covered`: 新しいテストが同じ振る舞いを確かめている。
- `added`: Step 2 で足した。
- `ui-only`: 画面の部品の配置や開閉だけを確かめている（例: 閉じるボタンの位置、固定高さの入れ物、ダイアログを閉じたあと開かないこと）。新しい画面では `<dialog>` と JS が担うので、サーバーのテストでは扱えない。理由を1行で書く。
- `obsolete`: Streamlit の仕組みにしか無い問題を確かめている（例: 再実行で結果が消えないこと、生成中のラジオの無効化）。新しい構成で同じ問題が起きない理由を1行で書く。

`obsolete` と `ui-only` にしたものでも、その裏にサーバー側の振る舞いがあるならそちらを押さえる。例: 生成中にコーパスを切り替えたとき古い質問を処理し直さないこと。これは、要求ごとにコーパスと履歴を送る API なら起きない。表にはその根拠を書く。

- [ ] **Step 2: 欠けている振る舞いのテストを足す**

区分が決まらず「サーバー側の振る舞いなのに新しいテストが無い」ものは、該当する `tests/test_web_*.py` にテストを足す。テストは、旧テストと同じ観点で、日本語の docstring を付ける。テストが落ちたら、旧画面の振る舞いに合わせて `rag_web` を直す。文面は旧画面のままにする。

- [ ] **Step 3: テストを流す**

Run: `.\rag_env\Scripts\python.exe -W error -m pytest (Get-ChildItem tests -Filter "test_web_*.py").FullName -q`
Expected: 失敗 0、警告 0

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-11-22-fastapi-web-ui-stage3-parity.md tests/ rag_web/
git commit -m "test: cover the remaining Streamlit behaviors in the web API tests"
```

---

### Task 2: Streamlit 版を消し、参照を向け直す

**Files:**
- Delete: `rag_chat_app.py`、`tests/test_rag_chat_app.py`、`tests/test_rag_chat_app_cowork.py`、`run_streamlit.ps1`
- Modify: `requirements.txt`（`streamlit==1.61.1` を消す）
- Modify: `rag_web/app.py:65-71`（「段階③で消すまでの一時的な重複」のコメントを消す。`DOCS_INGEST_COMMAND` が唯一の置き場所になる）
- Modify: `tests/test_web_app.py`（`test_docs_ingest_command_matches_streamlit_app` を消す。ほかに `rag_chat_app.py` を読むテストがあれば同様に扱う）
- Modify（コメント・docstring だけ）: `rag_web/*.py`、`rag_web/static/app.js`、`rag_web/static/index.html`、`ingest/prompting.py`、`ingest/query_translation.py`、`ingest/store.py`、`ingest/vector_store.py`、`ingest/display_mode.py`、`ingest/answer_text.py`、`ingest/parsers/drawio_parser.py`、`ingest/retrieval.py:192`、`scripts/check_retrieval.py`、`scripts/rag_mcp_server.py`、`scripts/ingest_source.py`、`tests/test_check_retrieval.py`、`tests/test_prompting.py`、`tests/test_display_mode.py`、`tests/test_vector_store.py`、`tests/test_migrate_store.py`、`tests/test_parser_drawio.py`
- Modify: `colab/run_ollama_server.ipynb`（`streamlit run rag_chat_app.py` を `run_web.ps1` / `python -m rag_web` に置き換える）、`colab/run_vllm_server.ipynb`（「`rag_chat_app.py` は…」を「RAGアプリ（`rag_web` と `ingest/`）は…」にする）
- Modify: `CLAUDE.md`（「仮想環境（`myvenv313`）」→「仮想環境（`rag_env`）」）。`AGENTS.md` に venv の名前があれば同様に直す。

**Interfaces:**
- Consumes: Task 1 の突き合わせ表。区分が4つのどれにも決まっていない行が残っていたら消さずに止める。
- Produces: Streamlit の無いツリー

- [ ] **Step 1: 突き合わせ表に未決の行が無いことを確かめる**

- [ ] **Step 2: ファイルを消し、`requirements.txt` から `streamlit` を消す**

- [ ] **Step 3: Streamlit が間接的に入れていたパッケージを、コードが直接使っていないか確かめる**

Run: `.\rag_env\Scripts\python.exe -m pip show streamlit` の `Requires:` に並ぶパッケージを、`git grep -n -E "^\s*(import|from) (<名前>)"` で1つずつ探す（`ingest/ docgen/ rag_web/ scripts/ tests/` が対象）。
Expected: 該当なし。該当があれば、そのパッケージを今入っている版で `requirements.txt` に足し、`docs/依存関係一覧.md` にも書く（Task 4 で整える）。

- [ ] **Step 4: コメントの参照を向け直す**

規則は次の3つ。
- 「`rag_chat_app.py` の X と同じ」と書いている箇所は、新しい置き場所（`rag_web/answering.py` の `answer_events`、`rag_web/static/app.js` の `renderHits` など）を指すように書き換える。新しい置き場所が無いものは文を消す。
- Streamlit の仕組み（`@st.cache_resource`、再実行、サイドバー）を理由にしている箇所は、今の理由に書き換える。例: `ingest/vector_store.py:127` の `check_same_thread=False` の理由は、「FastAPI のスレッドプールの複数のスレッドから1つの接続を使うため」になる。理由が無くなったものは消す。
- 移行中であることを書いた箇所（`rag_web/app.py:1` の「置き換え」、`app.js:3` など）は、今の役割の説明にする。

- [ ] **Step 5: 残りを確かめる**

Run: `git grep -n -i -E "rag_chat_app|run_streamlit|import streamlit|st\.session_state|cache_resource" -- . ':!docs' ':!docs_source' ':!sqlite-vector-store'`
Expected: 0件（`docs/` は Task 4 で扱う）

Run: `git grep -n -i "streamlit" -- ingest scripts rag_web tests colab`
Expected: 残るのは Global Constraints にある、コーパスとしての streamlit.md と Streamlit の質問例だけ。

- [ ] **Step 6: テストを流す**

Run: `.\rag_env\Scripts\python.exe -m pytest -q`
Expected: 失敗 0

- [ ] **Step 7: Commit**（コードとノートブックを別コミットにする）

```bash
git commit -m "refactor: remove the Streamlit app and point comments at the web UI"
git commit -m "docs: point the Colab notebooks at the web UI"
```

---

### Task 3: IIS 配下で動かすための設定（申し送りのうちコードで直すもの）

**Files:**
- Modify: `rag_web/settings.py`、`rag_web/__main__.py`、`rag_web/app.py`、`rag_web/auth.py`、`ingest/vector_store.py`
- Modify: `.env.example`
- Test: `tests/test_web_settings.py`、`tests/test_web_main.py`、`tests/test_web_app.py`、`tests/test_web_auth.py`、`tests/test_vector_store.py`、`tests/test_web_corpora.py`

**Interfaces:**
- Produces（`WebSettings` に足すフィールド。Task 4 の文書はこの名前と既定値を書く）:
  - `forwarded_allow_ips: str = "127.0.0.1"`（`RAG_WEB_FORWARDED_ALLOW_IPS`）。空文字のときは、プロキシのヘッダーを信頼しない。
  - `threadpool_size: int = 40`（`RAG_WEB_THREADPOOL_SIZE`、1以上）
  - `max_cowork_jobs: int = 2`（`RAG_WEB_MAX_COWORK_JOBS`、1以上）
- Produces: `VectorStore.close() -> None`

- [ ] **Step 1: 設定のテストを書く**（`tests/test_web_settings.py`）

- 既定値が上の3つになること。
- `RAG_WEB_FORWARDED_ALLOW_IPS=""` のとき、値が `""` になること（既定値に倒さない）。値の前後の空白は取る。
- `RAG_WEB_THREADPOOL_SIZE=0` と `RAG_WEB_MAX_COWORK_JOBS=abc` は、変数名入りの `ValueError` になること。

- [ ] **Step 2: 起動のテストを書く**（`tests/test_web_main.py`、`uvicorn.run` を monkeypatch）

- 既定では `uvicorn.run(..., proxy_headers=True, forwarded_allow_ips="127.0.0.1")` で呼ばれること。
- `RAG_WEB_FORWARDED_ALLOW_IPS=""` のときは `proxy_headers=False` になること。

- [ ] **Step 3: ログイン試行制限の接続元のテストを書く**（`tests/test_web_auth.py` / `tests/test_web_app.py`）

- 新しい関数 `client_key(host: str) -> str`（`rag_web/auth.py`）について:
  - `"10.0.0.5:51234"` → `"10.0.0.5"`
  - `"10.0.0.5"` → そのまま
  - `"[::1]:443"` → `"::1"`
  - `"::1"` → そのまま
- `/api/login` で、`request.client.host` が同じ IP でポートだけ違う失敗が5回続いたら、6回目は 429 になること。TestClient で再現しにくい場合は、`client_key` を通していることを `LoginThrottle` を差し替えて確かめる。

- [ ] **Step 4: Cowork の同時実行数のテストを書く**（`tests/test_web_app.py`）

- `max_cowork_jobs=1` で、終わらない偽の生成を1本走らせる。2本目の `POST /api/cowork` は 503 になり、`detail` が「ほかの人の生成が混み合っています。少し待ってからもう一度実行してください」になること。
- 1本目が終わったあとは、2本目を受け付けること。
- 生成が例外で終わっても枠が返り、次を受け付けること。

- [ ] **Step 5: スレッドプールのテストを書く**（`tests/test_web_app.py`）

- `threadpool_size=7` で作ったアプリを `with TestClient(app):` で起動している間は、`anyio.to_thread.current_default_thread_limiter().total_tokens == 7` であること。

- [ ] **Step 6: `VectorStore.close()` のテストを書く**（`tests/test_vector_store.py`）

- `close()` のあとに `count()` を呼ぶと `sqlite3.ProgrammingError` になること。
- `close()` を2回呼んでも例外にならないこと。

- [ ] **Step 7: テストが落ちることを確かめる**

Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_web_settings.py tests/test_web_main.py tests/test_web_auth.py tests/test_web_app.py tests/test_vector_store.py -q`
Expected: 新しいテストだけが FAIL

- [ ] **Step 8: 実装する**

- `settings.py`: 3つのフィールドを足す。整数は既存の `_positive_int` で読む。
- `__main__.py`: `uvicorn.run(app, host=..., port=..., proxy_headers=bool(settings.forwarded_allow_ips), forwarded_allow_ips=settings.forwarded_allow_ips or None)`。
- `auth.py`: `client_key` を追加する。`app.py` のログインで `request.client.host` をこれに通す。
- `app.py`:
  - `create_app` の lifespan で、起動時に `anyio.to_thread.current_default_thread_limiter().total_tokens = settings.threadpool_size` を設定する。
  - Cowork は `threading.BoundedSemaphore(settings.max_cowork_jobs)` を `acquire(blocking=False)` で取る。取れなければ 503 を返す。ジョブの関数の `finally` で返す。`jobs.start` 自体が失敗したときも返す。`app.py` の「上限なし」の技術的負債コメントは消す。
- `ingest/vector_store.py`: `close()` を `self._lock` の下で接続を閉じるものにする。
- テストの `store._connection.close()`（`tests/test_web_app.py`、`tests/test_web_corpora.py`）は `close()` に置き換える。
- `.env.example`: 3つの変数を、説明と既定値つきで足す。

- [ ] **Step 9: テストを流す**

Run: `.\rag_env\Scripts\python.exe -m pytest -q`
Expected: 失敗 0

Run: `.\rag_env\Scripts\python.exe -W error -m pytest (Get-ChildItem tests -Filter "test_web_*.py").FullName -q`
Expected: 失敗 0

Run（記録用。失敗してもこのタスクは止めない）: `.\rag_env\Scripts\python.exe -W error -m pytest -q`
結果の件数と、残る `ResourceWarning` を出しているテストファイルを報告に書く。

- [ ] **Step 10: Commit**

```bash
git commit -m "feat: trust forwarded client IPs only from the configured proxy"
git commit -m "feat: bound concurrent Cowork jobs and size the thread pool from settings"
git commit -m "feat: add VectorStore.close and use it in web tests"
```

---

### Task 4: 文書を新しい画面に合わせて書き換える

**Files:**
- Modify: `docs/server-deployment-windows.md`（本番の手順。いちばん重要）
- Modify: `docs/server-deployment.md`（Linux 版）
- Modify: `docs/server-set.md`（旧草案。冒頭の注記を残したまま、Streamlit の起動・設定を `rag_web` に置き換える。量の多い書き直しはしない）
- Modify: `README.md`
- Modify: `docs/依存関係一覧.md`、`docs/処理箇所マップ.md`、`docs/技術ドキュメントDBの移植手順.md`、`docs/コーディング対応ライブラリ.md`（アプリとしての Streamlit への言及だけ。コーパスとしての streamlit は残す）、`docs/mcp-server-network.md`、`docs/gb10-japanese-input.md`、`docs/gb10-coding-agent.md`（同上）
- Modify: `docs/superpowers/specs/2026-11-22-fastapi-web-ui-design.md`（「段階③への申し送り」の各項目に、対応の結果を1行ずつ書き足す）

**Interfaces:**
- Consumes: Task 3 の設定名と既定値。`run_web.ps1`。`python -m rag_web`（既定 `127.0.0.1:8502`）。

- [ ] **Step 1: `docs/server-deployment-windows.md` を書き換える**

対象を `rag_web` にする。各節で押さえることは次のとおり。

- 起動: ランチャーと起動時タスクで `python -m rag_web` を動かす。Streamlit の非秘密設定（3.2）の節は、`RAG_WEB_*` の設定の節に置き換える。
- 秘密情報: `RAG_WEB_ADMIN_PASSWORD_HASH` の作り方を書く。README にある手順を参照してよい。
- ポート: 8501 を 8502 に置き換える（ファイアウォールの節も）。
- IIS の転送ルール:
  - WebSocket の設定を外す。
  - ARR の応答バッファリングを切る（`responseBufferLimit="0"`）。
  - チャットの NDJSON が溜め込まれないよう、`/api/chat` の動的圧縮を外す。
  - ARR のタイムアウトを延ばす。
  - `X-Forwarded-For` を付け、ポートを含めない（`includePortInXForwardedFor` を false）。アプリ側もポートを取り除くが、手順でも揃える。
  - `RAG_WEB_FORWARDED_ALLOW_IPS` は ARR の接続元（同じサーバーなら `127.0.0.1`）にする。
- 要求サイズ: `requestFiltering` の `maxAllowedContentLength` を、`RAG_WEB_MAX_UPLOAD_MB` の base64 後の大きさに JSON の枠を足した値にする。式は `ceil(MB × 1,048,576 × 4 / 3) + 1,048,576`。既定 50MB なら 70,953,643 バイト。式と既定値での値の両方を書く。
- Cookie: HTTPS の配下なので `RAG_WEB_COOKIE_SECURE=true`。
- スレッドプールと Cowork の同時数: 既定値と、想定の同時利用者数で計測してから変える手順を短く書く。計測の方法は、ブラウザーのタブを複数開いて同時に質問し、状態の表示が待たされないかを見る、で足りる。
- 受け入れ確認（8章）と公開前チェックリストを、上の項目に合わせて直す。
  - 確かめる項目: 回答が逐次表示されること。管理者ログインで差分取り込みができること。上限近くのファイルをアップロードできること。

IIS / ARR の設定名は context7 または公式文書で確かめる。context7 に送るのは「IIS ARR response buffering」のような一般的な語だけにする（AGENTS.md の規則）。

- [ ] **Step 2: `docs/server-deployment.md`（Linux）を書き換える**

- systemd の `ExecStart` を `python -m rag_web` にする。
- Nginx の設定:
  - `proxy_buffering off;`
  - `proxy_read_timeout` を延ばす。
  - `client_max_body_size` を上の式の値にする。
  - WebSocket の `Upgrade` 設定を外す。
  - `X-Forwarded-For` を付ける。

- [ ] **Step 3: README を書き換える**

- セットアップの `myvenv313` を `rag_env` に書き換える（設計書「開発環境」）。
- 起動手順を `.\run_web.ps1` にする。「新しい画面（移行中・開発機で試用）」の節は、移行中の記述を消して、通常の使い方の節にまとめる。
- ファイル一覧の `rag_chat_app.py` の行を `rag_web/` の行に置き換える。
- 「`streamlit.exe` のようなランチャー」の注意は、`python.exe -m` の形の一般的な注意として残す。
- Streamlit プロセスへの言及（同時利用、取り込み中に開いたまま、など）は、Web 画面に置き換える。

- [ ] **Step 4: 残りの文書を直し、確かめる**

Run: `git grep -n -i -E "rag_chat_app|run_streamlit|streamlit run|8501|myvenv313" -- docs README.md CLAUDE.md AGENTS.md ':!docs/superpowers'`
Expected: 0件。ただし、README の「`myvenv313` は過去の名前」という1行と、実測の記録で日付つきの過去の事実として書かれたものは残してよい。残したものは報告に列挙する。

- [ ] **Step 5: 設計書の申し送りに結果を書く**

各項目の末尾に「→ 段階③: …」を1行で書く。

- 文書で対処したもの: どの文書のどの節か。
- コードで対処したもの: 設定名。
- 対処しないもの（エラー文の絶対パス）: 理由。

- [ ] **Step 6: テストを流す**（README のコマンドを確かめるテストがあれば、それも含まれる）

Run: `.\rag_env\Scripts\python.exe -m pytest -q`
Expected: 失敗 0

- [ ] **Step 7: Commit**

```bash
git commit -m "docs: rewrite the deployment guides for the web UI"
git commit -m "docs: update README and references for the web UI"
```
