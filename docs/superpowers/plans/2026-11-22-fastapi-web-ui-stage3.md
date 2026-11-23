# FastAPI 画面 段階③（Streamlit の撤去と GB10 への配置手順）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Streamlit 版（`rag_chat_app.py`）とその依存を取り除く。本番の配置手順を、GB10（Ubuntu）上の FastAPI 版（`rag_web/`）向けに書き直す。あわせて設計書「段階③への申し送り」のうち、コードで対処するものを片づける。

**Architecture:** 新しい機能は足さない。作業は次の順に進める。
- ①旧テストの振る舞いが新しいテストで押さえられているかを突き合わせ、欠けを埋める。
- ②Streamlit を消し、コード中の参照を新しい置き場所へ向け直す。
- ③リバースプロキシ（Nginx）の配下での運用に要る設定を足す。対象は、プロキシの信頼範囲、スレッドプールの大きさ、Cowork の同時実行数。
- ④文書を書き換える。

**Tech Stack:** Python 3.13（`rag_env`）、FastAPI 0.162.2、uvicorn 0.54.0、anyio 4.14.2、pytest。配置先は GB10（DGX OS 7 = Ubuntu 24.04、`aarch64`）で、systemd と Nginx を使う。

**Spec:** `docs/superpowers/specs/2026-11-22-fastapi-web-ui-design.md`。特に「段階と、各段階の完了条件」の3と「段階③への申し送り」を読む。設計書の IIS の記述は、配置先が GB10 に変わったため Task 4 で書き換える（利用者の指示 2026-11-22）。

## Global Constraints

- **配置先は GB10 の Ubuntu だけ。** Windows サーバー・IIS・ARR 向けの手順は作らない。その向けの既存文書（`docs/server-deployment-windows.md`、`docs/server-set.md`）は消す。
- 開発機は今までどおり Windows のままとする。`run_web.ps1` は開発機の起動用に残す。
- `ingest/` と `docgen/` の振る舞いは変えない。例外は次の2つだけ。
  - `VectorStore.close()` の追加（Task 3）
  - コメントの書き換え（Task 2）
- 利用者に見せる文言は日本語にする。既存のエラー文の文面は変えない。
- 環境変数で受ける設定は `rag_web/settings.py` の `load_web_settings` で読む。不正な値は、変数名入りの `ValueError` で起動時に止める（既存の方針）。
- 新しい依存パッケージは足さない。`requirements.txt` から `streamlit` を消す。
- テストは `.\rag_env\Scripts\python.exe -m pytest -q` で失敗 0 にする。`tests/test_web_*.py` は `-W error` を付けても失敗 0 にする。
- コミットは英語のコンベンショナルコミットにする。
- 次のものは**消さない**。いずれもアプリではなく、取り込んだ文書（技術ドキュメントのコーパスにある streamlit.md）の名前である。
  - `docs_source/` と `docs_sources.toml`
  - streamlit.md に触れた実測の記述（`ingest/retrieval.py`、`ingest/chunker.py`、`ingest/parsers/*` など）
  - Streamlit を題材にした質問例（`scripts/check_retrieval.py`、`tests/test_query_translation.py` など）
- `sqlite-vector-store/` は別の小プロジェクトなので触らない。
- ノートブック（`colab/*.ipynb`）を編集する前に、利用者が VS Code でそのノートブックを閉じていることを確かめる。開いたままだと上書きされた事故がある。編集後は、コミット済みの内容を `git show HEAD:<path>` で確かめる。
- GB10 の実機はこの作業の中では使えない。手順書のうち実機で確かめていない部分は、`docs/gb10-japanese-input.md` と同じように「実測はしていない」と明記する。

## Review Focus

1. **信頼しない接続元が送った `X-Forwarded-For`**: `RAG_WEB_FORWARDED_ALLOW_IPS` に無い接続元がこのヘッダーを付けても、ログイン試行制限の接続元が書き換わらないこと（Task 3 のテスト）。
2. **Nginx 越しのログイン試行制限**: Nginx の配下では、試行の数え方の単位がプロキシの IP ではなく、利用者の IP になること（Task 3 のテスト）。
3. **Cowork の枠が失敗で漏れない**: 生成が例外で終わっても、同時実行の枠は返ること。枠が埋まっている間は 503 を返し、枠が空けば次の生成を受け付けること（Task 3 のテスト）。
4. **Streamlit の残骸**: `streamlit` を import するコードが1つも残らないこと。また、Streamlit が間接的に入れていたパッケージ（例: `pandas`、`pyarrow`、`requests`）をコードが直接 import しているなら、`requirements.txt` から streamlit を消したあとも、明示的な依存として残すこと（Task 2 の確認手順）。
5. **旧テストだけが押さえていた振る舞い**: 旧テストを消す前に、新しいテストで同じ振る舞いが押さえられていること（Task 1 の突き合わせ表）。

---

### Task 1: 旧テストとの突き合わせ（振る舞いの穴を埋める）

設計書の要求「③で消すのは、同じ振る舞いが新しいテストで押さえられていることを確かめてからにする」に応える。

**Files:**
- Create: `docs/superpowers/plans/2026-11-22-fastapi-web-ui-stage3-parity.md`
- Modify（必要なときだけ）: `tests/test_web_app.py`、`tests/test_web_answering.py`、`tests/test_web_cowork.py`、`tests/test_web_corpora.py`、`rag_web/*.py`
- Read: `tests/test_rag_chat_app.py`（49件）、`tests/test_rag_chat_app_cowork.py`（30件）

**Interfaces:**
- Consumes: 段階①②の `rag_web` 全体（変えない）
- Produces: 突き合わせ表。Task 2 は、この表を確かめてから旧テストを消す。

- [ ] **Step 1: 突き合わせ表を作る**

旧テスト1件ごとに1行を書く。列は次の4つ。
- 旧テスト名
- 確かめている振る舞い（1行）
- 区分
- 新しいテスト（`ファイル::関数名`）、または理由

区分は次の4つのどれかにする。
- `covered`: 新しいテストが同じ振る舞いを確かめている。
- `added`: Step 2 で足した。
- `ui-only`: 画面の部品の配置や開閉だけを確かめている（例: 閉じるボタンの位置、固定高さの入れ物、閉じたダイアログが勝手に開かないこと）。新しい画面では `<dialog>` と JS が担っており、サーバーのテストでは扱えない。理由を1行で書く。
- `obsolete`: Streamlit の仕組みにしか無い問題を確かめている（例: 再実行で結果が消えないこと、生成中にラジオボタンを無効にすること）。新しい構成では同じ問題が起きない理由を1行で書く。

`obsolete` や `ui-only` にしたテストでも、その裏にサーバー側の振る舞いがあるなら、そちらを押さえる。たとえば「生成中にコーパスを切り替えても、古い質問を処理し直さない」というテストは、要求ごとにコーパスと履歴を送る API では起きない問題になる。表にはその根拠を書く。

- [ ] **Step 2: 欠けている振る舞いのテストを足す**

サーバー側の振る舞いなのに新しいテストが無く、区分が決まらないものは、該当する `tests/test_web_*.py` にテストを足す。
- 旧テストと同じ観点で書き、日本語の docstring を付ける。
- テストが落ちたら、旧画面の振る舞いに合わせて `rag_web` を直す。文面は旧画面のままにする。

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
- Modify: `rag_web/app.py:65-71`（「段階③で消すまでの一時的な重複」のコメントを消す。以後は `DOCS_INGEST_COMMAND` が唯一の置き場所になる）
- Modify: `tests/test_web_app.py`（`test_docs_ingest_command_matches_streamlit_app` を消す。ほかに `rag_chat_app.py` を読むテストがあれば、同じように扱う）
- Modify（コメントと docstring だけ）:
  - `rag_web/*.py`、`rag_web/static/app.js`、`rag_web/static/index.html`
  - `ingest/prompting.py`、`ingest/query_translation.py`、`ingest/store.py`、`ingest/vector_store.py`、`ingest/display_mode.py`、`ingest/answer_text.py`、`ingest/parsers/drawio_parser.py`、`ingest/retrieval.py:192`
  - `scripts/check_retrieval.py`、`scripts/rag_mcp_server.py`、`scripts/ingest_source.py`
  - `tests/test_check_retrieval.py`、`tests/test_prompting.py`、`tests/test_display_mode.py`、`tests/test_vector_store.py`、`tests/test_migrate_store.py`、`tests/test_parser_drawio.py`
- Modify: Colab のノートブック2本
  - `colab/run_ollama_server.ipynb`: `streamlit run rag_chat_app.py` を `run_web.ps1`（または `python -m rag_web`）に置き換える。
  - `colab/run_vllm_server.ipynb`: 「`rag_chat_app.py` は…」を「RAGアプリ（`rag_web` と `ingest/`）は…」にする。
- Modify: `CLAUDE.md`（「仮想環境（`myvenv313`）」を「仮想環境（`rag_env`）」にする）。`AGENTS.md` に venv の名前があれば、同じように直す。

**Interfaces:**
- Consumes: Task 1 の突き合わせ表。4つの区分のどれにも決まっていない行が残っていたら、旧テストを消さずに止める。
- Produces: Streamlit の無いツリー

- [ ] **Step 1: 突き合わせ表に、区分の決まっていない行が無いことを確かめる**

- [ ] **Step 2: ファイルを消し、`requirements.txt` から `streamlit` を消す**

- [ ] **Step 3: Streamlit が間接的に入れていたパッケージを、コードが直接使っていないか確かめる**

Run: `.\rag_env\Scripts\python.exe -m pip show streamlit` の `Requires:` に並ぶパッケージを、1つずつ `git grep -n -E "^\s*(import|from) (<名前>)"` で探す。探す範囲は `ingest/`、`docgen/`、`rag_web/`、`scripts/`、`tests/`。
Expected: 該当なし。該当があれば、そのパッケージを今入っている版で `requirements.txt` に足し、`docs/依存関係一覧.md` にも書く（Task 4 で整える）。

- [ ] **Step 4: コメントの参照を向け直す**

規則は次の3つ。
- 「`rag_chat_app.py` の X と同じ」と書いている箇所は、新しい置き場所を指すように書き換える。例: `rag_web/answering.py` の `answer_events`、`rag_web/static/app.js` の描画関数。新しい置き場所が無いものは、文ごと消す。
- Streamlit の仕組み（`@st.cache_resource`、再実行、サイドバー）を理由にしている箇所は、今の理由に書き換える。たとえば `ingest/vector_store.py:127` の `check_same_thread=False` の理由は、「FastAPI のスレッドプールの複数のスレッドから1つの接続を使うため」になる。理由が無くなったものは消す。
- 移行中であることを書いた箇所（`rag_web/app.py:1` の「置き換え」、`app.js:3` など）は、今の役割の説明にする。

- [ ] **Step 5: 残りを確かめる**

Run: `git grep -n -i -E "rag_chat_app|run_streamlit|import streamlit|st\.session_state|cache_resource" -- . ':!docs' ':!docs_source' ':!sqlite-vector-store'`
Expected: 0件（`docs/` は Task 4 で扱う）

Run: `git grep -n -i "streamlit" -- ingest scripts rag_web tests colab`
Expected: 残るのは、Global Constraints に挙げたコーパスとしての streamlit.md と、Streamlit を題材にした質問例だけ。

- [ ] **Step 6: テストを流す**

Run: `.\rag_env\Scripts\python.exe -m pytest -q`
Expected: 失敗 0

- [ ] **Step 7: Commit**（コードとノートブックは別のコミットにする）

```bash
git commit -m "refactor: remove the Streamlit app and point comments at the web UI"
git commit -m "docs: point the Colab notebooks at the web UI"
```

---

### Task 3: リバースプロキシの配下で動かすための設定（申し送りのうちコードで直すもの）

**Files:**
- Modify: `rag_web/settings.py`、`rag_web/__main__.py`、`rag_web/app.py`、`ingest/vector_store.py`
- Modify: `.env.example`
- Test: `tests/test_web_settings.py`、`tests/test_web_main.py`、`tests/test_web_app.py`、`tests/test_vector_store.py`、`tests/test_web_corpora.py`

**Interfaces:**
- Produces: `WebSettings` に足すフィールド。Task 4 の文書は、この名前と既定値で書く。
  - `forwarded_allow_ips: str = "127.0.0.1"`（`RAG_WEB_FORWARDED_ALLOW_IPS`）。空文字のときは、プロキシのヘッダーを信頼しない。
  - `threadpool_size: int = 40`（`RAG_WEB_THREADPOOL_SIZE`、1以上）
  - `max_cowork_jobs: int = 2`（`RAG_WEB_MAX_COWORK_JOBS`、1以上）
- Produces: `VectorStore.close() -> None`

- [ ] **Step 1: 設定のテストを書く**（`tests/test_web_settings.py`）

- 既定値が上の3つになること。
- `RAG_WEB_FORWARDED_ALLOW_IPS=""` のとき、値が `""` になること（既定値に戻さない）。値の前後の空白は取り除く。
- `RAG_WEB_THREADPOOL_SIZE=0` や `RAG_WEB_MAX_COWORK_JOBS=abc` は、変数名入りの `ValueError` になること。

- [ ] **Step 2: 起動のテストを書く**（`tests/test_web_main.py`、`uvicorn.run` を monkeypatch する）

- 既定では、`uvicorn.run(..., proxy_headers=True, forwarded_allow_ips="127.0.0.1")` で呼ばれること。
- `RAG_WEB_FORWARDED_ALLOW_IPS=""` のときは、`proxy_headers=False` になること。

- [ ] **Step 3: プロキシ越しの接続元のテストを書く**（`tests/test_web_app.py`）

uvicorn の `ProxyHeadersMiddleware` で包んだアプリを TestClient で叩く。包み方は `__main__` と同じ設定値から作る。テストのため、それを作る小さな関数を `rag_web/__main__.py` か `rag_web/app.py` に切り出してよい。
- 信頼する接続元からの `X-Forwarded-For: 10.0.0.5` で、ログインに5回失敗したとき:
  - 6回目は 429 になること。
  - 別の `X-Forwarded-For: 10.0.0.6` からの試行は受け付けること。
- 信頼しない接続元が `X-Forwarded-For` を付けても、接続元は書き換わらないこと。

uvicorn の版によってミドルウェアの引数名が違う場合は、インストールされている版のソースで確かめる。

- [ ] **Step 4: Cowork の同時実行数のテストを書く**（`tests/test_web_app.py`）

- `max_cowork_jobs=1` で、終わらない偽の生成を1本走らせる。2本目の `POST /api/cowork` は 503 になり、`detail` が「ほかの人の生成が混み合っています。少し待ってからもう一度実行してください」であること。
- 1本目が終わったあとは、2本目を受け付けること。
- 生成が例外で終わっても枠は返り、次の生成を受け付けること。

- [ ] **Step 5: スレッドプールのテストを書く**（`tests/test_web_app.py`）

- `threadpool_size=7` で作ったアプリを `with TestClient(app):` で起動している間は、`anyio.to_thread.current_default_thread_limiter().total_tokens == 7` であること。

- [ ] **Step 6: `VectorStore.close()` のテストを書く**（`tests/test_vector_store.py`）

- `close()` のあとに `count()` を呼ぶと、`sqlite3.ProgrammingError` になること。
- `close()` を2回呼んでも例外にならないこと。

- [ ] **Step 7: テストが落ちることを確かめる**

Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_web_settings.py tests/test_web_main.py tests/test_web_app.py tests/test_vector_store.py -q`
Expected: 新しいテストだけが FAIL

- [ ] **Step 8: 実装する**

- `settings.py`: 3つのフィールドを足す。整数は、既存の `_positive_int` で読む。
- `__main__.py`: `uvicorn.run(app, host=..., port=..., proxy_headers=bool(settings.forwarded_allow_ips), forwarded_allow_ips=settings.forwarded_allow_ips or None)` で起動する。
- `app.py`:
  - `create_app` の lifespan で、起動時に `anyio.to_thread.current_default_thread_limiter().total_tokens = settings.threadpool_size` を設定する。
  - Cowork の生成は、`threading.BoundedSemaphore(settings.max_cowork_jobs)` を `acquire(blocking=False)` で取ってから始める。取れなければ 503 を返す。
  - 取った枠は、ジョブの関数の `finally` で返す。`jobs.start` 自体が失敗したときも返す。
  - `app.py` にある「上限なし」の技術的負債のコメントは消す。
- `ingest/vector_store.py`: `close()` は、`self._lock` の下で接続を閉じるものにする。
- テストの `store._connection.close()`（`tests/test_web_app.py`、`tests/test_web_corpora.py`）は、`close()` に置き換える。
- `.env.example`: 3つの変数を、説明と既定値つきで足す。

- [ ] **Step 9: テストを流す**

Run: `.\rag_env\Scripts\python.exe -m pytest -q`
Expected: 失敗 0

Run: `.\rag_env\Scripts\python.exe -W error -m pytest (Get-ChildItem tests -Filter "test_web_*.py").FullName -q`
Expected: 失敗 0

Run（記録用。失敗しても、このタスクは止めない）: `.\rag_env\Scripts\python.exe -W error -m pytest -q`
結果の件数と、`ResourceWarning` が残るテストファイルを報告に書く。

- [ ] **Step 10: Commit**

```bash
git commit -m "feat: take client IPs from forwarded headers only from the configured proxy"
git commit -m "feat: bound concurrent Cowork jobs and size the thread pool from settings"
git commit -m "feat: add VectorStore.close and use it in web tests"
```

---

### Task 4: 文書を新しい画面に合わせて書き換える

> **2026-11-22 変更:** 本番の推論を Ollama から vLLM へ切り替えることになった
> （`docs/superpowers/specs/2026-11-22-vllm-backend-design.md`）。GB10 の配置手順は
> vLLM 前提で書くため、**Step 1 はこの段階では行わない**。代わりに `docs/server-deployment.md` を
> 短い案内に置き換える。案内には次の3点を書く。
> - 配置手順は、vLLM への切り替えのときに GB10 向けに書き直す。
> - それまで本番には配置しない。
> - 設計書へのリンク。
>
> Step 2 以降のうち、GB10 の手順に依るもの（README のリンク先など）は、この案内を指す。
> 設計書の申し送り（Step 3）は、次のように分けて書く。
> - Nginx の設定で対処する項目: 「vLLM 切り替えの配置手順で対処」とする。
> - コードで対処した項目: 結果を書く。

**Files:**
- Modify: `docs/server-deployment.md` を、上の短い案内に置き換える。下の Step 1 は、vLLM 切り替えの計画へ移す。
- Delete: `docs/server-deployment-windows.md`、`docs/server-set.md`
- Modify: `README.md`
- Modify（アプリとしての Streamlit や Windows サーバーへの言及とリンクだけを直す。コーパスとしての streamlit は残す）:
  - `docs/依存関係一覧.md`、`docs/処理箇所マップ.md`、`docs/技術ドキュメントDBの移植手順.md`、`docs/コーディング対応ライブラリ.md`
  - `docs/mcp-server-network.md`（`server-set.md` へのリンクと NSSM の記述）
  - `docs/gb10-japanese-input.md`、`docs/gb10-coding-agent.md`
- Modify: `docs/superpowers/specs/2026-11-22-fastapi-web-ui-design.md`。配置先を GB10 に書き換える（入口の認証は IIS の Windows 認証から Nginx へ）。「段階③への申し送り」の各項目には、対応の結果を書き足す。

**Interfaces:**
- Consumes:
  - Task 3 の設定名と既定値
  - 起動コマンド `python -m rag_web`（既定 `127.0.0.1:8502`）
  - 既存の `docs/gb10-coding-agent.md`。GB10 の Ollama、`systemctl edit ollama`、LAN への公開の注意が書いてある。

- [ ] **Step 1: `docs/server-deployment.md` を GB10 向けに書き直す**

構成図: ブラウザー →（HTTPS :443、認証あり）Nginx → `127.0.0.1:8502` の `rag_web`（systemd、1プロセス）→ SQLite と、同じ GB10 上の Ollama（`127.0.0.1:11434`）。

押さえること:
- **前提**: DGX OS 7（Ubuntu 24.04、`aarch64`）であることを確かめるコマンドを書く。Ubuntu 24.04 の標準の Python は 3.12 で、このリポジトリは 3.13 を前提にしている。そのため Python 3.13 の入れ方（例: `uv python install 3.13` などの一般的な方法）を書き、`rag_env` を `python3.13 -m venv rag_env` で作る。`requirements.txt` の各パッケージ（特に `torch`、リランカー、OCR まわり）が `aarch64` で入るかは確かめておらず、入らないときに見る箇所を書く。
- **Ollama**: 同じ GB10 の Ollama を使い、`OLLAMA_HOST=http://127.0.0.1:11434` にする。`docs/gb10-coding-agent.md` で Ollama を `0.0.0.0` に公開している場合は、RAG の質問や資料の本文もその Ollama を通ることと、LAN から誰でもそのモデルを使えることを注意として書く。必要なモデル（埋め込み `bge-m3`、生成は `rag_web/app.py` の `CHAT_MODEL`、画像の説明に使う VLM）を `ollama pull` する手順も書く。
- **リランカーのモデル**: オフラインで使う場合は事前に取得する（AGENTS.md の記述に合わせる）。
- **実行ユーザーと配置**: 専用ユーザーで動かし、`/opt/...` に置く。DB と `source/`、`templates/` の置き場所と権限を書く。
- **`.env`**: 権限は 600 にする。次の値を書く。
  - `RAG_WEB_ADMIN_PASSWORD_HASH`（作り方は README の手順を参照）
  - `RAG_WEB_HOST=127.0.0.1`、`RAG_WEB_PORT=8502`
  - `RAG_WEB_COOKIE_SECURE=true`（HTTPS の配下のため）
  - `RAG_WEB_FORWARDED_ALLOW_IPS=127.0.0.1`（Nginx が同じ GB10 にあるため）
  - `RAG_WEB_MAX_UPLOAD_MB`、`RAG_WEB_THREADPOOL_SIZE`、`RAG_WEB_MAX_COWORK_JOBS`
- **systemd**: `ExecStart=/opt/.../rag_env/bin/python -m rag_web` で動かす。`WorkingDirectory` を設定し、`.env` は `python-dotenv` が作業ディレクトリから読む。`Restart=on-failure` にし、ログは `journalctl -u ...` で見る。
- **Nginx**:
  - `/api/chat` の逐次表示が溜め込まれないよう、`proxy_buffering off;` にする（`gzip off;` も検討する）。
  - 生成が長くても切れないよう、`proxy_read_timeout` を延ばす。
  - 要求サイズの上限 `client_max_body_size` は、`RAG_WEB_MAX_UPLOAD_MB` を base64 にしたあとの大きさに、JSON の枠を足した値にする。式は `ceil(MB × 1,048,576 × 4 / 3) + 1,048,576` バイトで、既定の 50MB なら 70,953,643 バイト。式と既定値での値の両方を書く。
  - `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` と `X-Forwarded-Proto` を設定する。
  - WebSocket の `Upgrade` 設定は要らない。
  - HTTPS は、既存の文書にある内部 CA の手順を流用する。
- **入口の認証**: 既存の文書と同じ、Nginx の Basic 認証にする。画面に入れる人をここで絞る。差分取り込みには、これとは別に、アプリの管理者ログインが要る。
- **ファイアウォール**: `ufw` で 443 だけを開ける。8502 は `127.0.0.1` にしか bind しないことも書く。
- **スレッドプールと Cowork の同時数**: 既定値を書く。想定の同時利用者数で確かめてから変える。確かめ方は、ブラウザーのタブを複数開いて同時に質問し、状態の表示が待たされないかを見る、で足りる。
- **受け入れ確認と公開前チェックリスト**: 次を確かめる。
  - 回答が逐次表示されること
  - 管理者ログインで差分取り込みができること
  - 上限に近い大きさのファイルをアップロードできること
  - 再起動後に自動で起動すること
- **停止・更新**: `git pull`、`pip install -r requirements.txt`、`systemctl restart` の順で更新する。

Nginx と systemd の設定名は、context7 または公式文書で確かめる。context7 に送るのは「nginx proxy_buffering streaming response」のような一般的な語だけにする（AGENTS.md の規則）。

- [ ] **Step 2: README を書き換える**

- セットアップの `myvenv313` を `rag_env` に書き換える（設計書「開発環境」）。
- 開発機での起動手順は `.\run_web.ps1` にする。
- 「新しい画面（移行中・開発機で試用）」の節は、移行中である旨の記述を消し、通常の使い方の節にまとめる。
- 冒頭の Windows サーバー配置手順へのリンクは、GB10 の配置手順（`docs/server-deployment.md`）へのリンクにする。
- ファイル一覧の `rag_chat_app.py` の行は、`rag_web/` の行に置き換える。
- 「`streamlit.exe` のようなランチャー」の注意は、`python.exe -m` の形で起動する一般的な注意として残す。
- Streamlit のプロセスへの言及（同時利用、取り込み中に開いたままでよい、など）は、Web 画面への言及に置き換える。

- [ ] **Step 3: 設計書を直す**

- 「非ゴール」「検討した方式」「段階③」にある IIS の記述は、GB10 の Nginx（Basic 認証）に置き換える。配置先を変えたのは利用者の指示であることと、その日付を書く。
- 「段階③への申し送り」の各項目の末尾に、「→ 段階③: …」の形で結果を1行ずつ書く。
  - IIS の要求サイズ → Nginx の `client_max_body_size`
  - ARR → Nginx の `proxy_buffering` と `proxy_read_timeout`
  - ログイン試行制限 → `RAG_WEB_FORWARDED_ALLOW_IPS`
  - スレッドプール → `RAG_WEB_THREADPOOL_SIZE`
  - `close()` → 追加した
  - Cowork の同時実行数 → `RAG_WEB_MAX_COWORK_JOBS`
  - エラー文の絶対パス → 対処しない（理由も書く）

- [ ] **Step 4: 残りの文書を直し、確かめる**

Run: `git grep -n -i -E "rag_chat_app|run_streamlit|streamlit run|8501|myvenv313|server-deployment-windows|server-set\.md|IIS|NSSM" -- docs README.md CLAUDE.md AGENTS.md ':!docs/superpowers/plans'`
Expected: 0件。ただし、次のものは残してよい。残したものは報告に列挙する。
- README の「`myvenv313` は過去の名前」という1行
- 実測の記録で、日付つきの過去の事実として書かれたもの
- 設計書の中で、配置先を変更したいきさつとして IIS に触れた箇所

- [ ] **Step 5: テストを流す**（README のコマンドを確かめるテストがあれば、それも含まれる）

Run: `.\rag_env\Scripts\python.exe -m pytest -q`
Expected: 失敗 0

- [ ] **Step 6: Commit**

```bash
git commit -m "docs: replace the Windows deployment guides with a GB10 Ubuntu guide"
git commit -m "docs: update README, references and the design for the web UI on GB10"
```
