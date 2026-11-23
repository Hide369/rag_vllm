# RAG の推論を Ollama から vLLM へ切り替える Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RAG の生成・埋め込み・画像説明を、Ollama のネイティブ API から vLLM の OpenAI 互換 API へ切り替える。`OLLAMA_*` を `VLLM_*` に改める。GB10（Ubuntu）への配置手順と Claude Code の接続手順を書く。

**Architecture:**
- 接続先とモデル名は、新しいモジュール `ingest/vllm_config.py` に集める。環境変数は、呼ばれた時点で読む。
- `ingest/chat.py`・`ingest/embedder.py`・`ingest/vlm.py` は、これを使って `/v1/...` を叩く。再試行とエラー文の形は今のまま保つ。
- 埋め込みは bge-m3 のままとする。DB に埋め込みモデルを記録し、食い違ったら止める。
- 実機での確認は、Colab のノートブックで vLLM を立てて行う。GB10 向けの設定は、ファイルとして `deploy/gb10/` に置く。

**Tech Stack:**
- Python 3.13（`rag_env`）、`requests`（新しい依存は足さない）、pytest
- vLLM（OpenAI 互換 API、Anthropic Messages API）、Docker Compose、systemd、Nginx

**Spec:** `docs/superpowers/specs/2026-11-22-vllm-backend-design.md`

## Global Constraints

- **環境変数**（既定値は設計書の表のとおり）:

  | 変数 | 既定値 |
  |---|---|
  | `VLLM_CHAT_URL` | `http://127.0.0.1:8001` |
  | `VLLM_EMBED_URL` | `http://127.0.0.1:8002` |
  | `VLLM_VLM_URL` | `http://127.0.0.1:8003` |
  | `VLLM_CHAT_MODEL` | `gpt-oss-120b` |
  | `VLLM_EMBED_MODEL` | `bge-m3` |
  | `VLLM_VLM_MODEL` | `qwen3-vl-30b-a3b` |
  | `VLLM_API_KEY` | 空 |

  - `VLLM_API_KEY` を設定すると、`Authorization: Bearer <値>` を送る。
- 埋め込みは **bge-m3・1024 次元のまま**とする。距離のしきい値や候補数などの検索の調整値は変えない。
- `OLLAMA_HOST` / `OLLAMA_API_KEY` / `OLLAMA_VLM_MODEL` に空でない値が入っていたら、RAG の入口で起動を止める。止めるときは「VLLM_* へ移行してください」という日本語の `ValueError` を出す。入口は次の4つ。
  - `python -m rag_web`
  - `scripts.ingest_source`
  - `scripts.rag_mcp_server`
  - `scripts.check_retrieval`
- 利用者に見せる文言は日本語にする。エラー文の形（「<URL> への生成リクエストが4回失敗しました: …」など）は変えず、「Ollama」を「vLLM」に置き換える。
- 新しい依存パッケージは足さない（`openai` パッケージも入れない）。
- 外へ出る宛先は増やさない。
- 秘密情報（API キー、Nginx のトークン、htpasswd）はリポジトリに入れない。
- テストは `.\rag_env\Scripts\python.exe -m pytest -q` で失敗 0 にする。`tests/test_web_*.py` は `-W error` を付けても失敗 0 にする。実機の vLLM を叩くテストには `@pytest.mark.integration` を付ける。
- Codex 向けの部品（`coding_agent/`、`scripts/coding_agent.py`、`scripts/measure_autocomplete.py`、`scripts/proxy_log.py`、`docs/gb10-coding-agent.md`）と、そのテストは触らない。
- コミットは英語のコンベンショナルコミットにする。コメントは日本語で「なぜ」を書く。
- ノートブック（`colab/*.ipynb`）を編集する前に、利用者が VS Code でそのタブを閉じたことを確かめる。編集は JSON の文字列だけを書き換える。編集後は `git show HEAD:<path>` で中身を確かめる。
- バックスラッシュを含む文字列（Windows のパスなど）は、シェルの echo や、raw でない Python 文字列を通して書かない（段階③で `\r` が改行に化けた事故がある）。

## Review Focus

1. **ストリームの読み方**: 次のどの場合も、本文だけが順に出ること。思考の文字は本文に混ざらないこと（Task 2 のテスト）。
   - `choices` が空のチャンク
   - `role` だけを持つ差分
   - 思考の差分（`reasoning_content` / `reasoning`）
   - `data: [DONE]`
   - 途中で `{"error": ...}` が来たら、`ChatError` にする。
2. **URL の書き方の揺れ**: `VLLM_*_URL` の末尾の `/` は取る。`/v1` で終わる URL は、黙って `/v1/v1/...` を叩くことになるので、変数名入りの `ValueError` で止める（Task 1 のテスト）。
3. **認証の失敗**: 401 と 403 は再試行しない。「VLLM_API_KEY を確かめてください」と分かる文で止める（Task 1 のヘルパーと Task 2・3 のテスト）。
4. **埋め込みの順序**: `/v1/embeddings` の `data[]` は `index` で並べ直してから使う。逆順で返ってきても、対応がずれないこと（Task 3 のテスト）。
5. **DB の埋め込みモデル**: 次のことを確かめる（Task 4 のテスト）。
   - 記録の無い DB は `bge-m3`・1024 として扱う。
   - `bge-m3` と `BAAI/bge-m3` は同じモデルとして扱う。
   - 読むだけで開く場合（MCP サーバー、画面の検索）も、食い違っていれば止まる。
   - 記録は最初の書き込みで残り、その後は上書きされない。

---

### Task 1: 接続先の設定モジュールと、古い設定の拒否

**Files:**
- Create: `ingest/vllm_config.py`
- Test: `tests/test_vllm_config.py`
- Modify: `rag_web/app.py`（`CHAT_MODEL` 定数を、`load_vllm_settings().chat_model` から読むように変える）、`rag_web/__main__.py`、`scripts/ingest_source.py`、`scripts/rag_mcp_server.py`、`scripts/check_retrieval.py`（入口で `reject_legacy_env()` を呼ぶ）
- Modify: `tests/test_web_app.py` など、`CHAT_MODEL` を参照しているテスト

**Interfaces:**
- Produces（`ingest/vllm_config.py`）:
  - `@dataclass(frozen=True) class VllmSettings`: フィールドは `chat_url: str`、`embed_url: str`、`vlm_url: str`、`chat_model: str`、`embed_model: str`、`vlm_model: str`、`api_key: str`
  - `load_vllm_settings(environ: Mapping[str, str] = os.environ) -> VllmSettings`: 呼ぶたびに読む。URL は末尾の `/` を取る。`/v1` で終わる URL は `ValueError` にする。
  - `reject_legacy_env(environ: Mapping[str, str] = os.environ) -> None`: `OLLAMA_HOST` / `OLLAMA_API_KEY` / `OLLAMA_VLM_MODEL` のどれかに空でない値があれば `ValueError` にする。文言は「.env の OLLAMA_* は使われなくなりました。VLLM_* へ移行してください（.env.example 参照）: <変数名>」。
  - `new_session(settings: VllmSettings | None = None) -> requests.Session`: `api_key` があれば `Authorization: Bearer` を付ける。
  - `class AuthError(Exception)` と `raise_for_auth(response, url: str) -> None`: 401/403 なら `AuthError("<url> が認証を拒否しました（HTTP <code>）。VLLM_API_KEY を確かめてください")` を投げる。それ以外のときは何もしない。
- 既存の `ingest.embedder.new_session` / `OLLAMA_HOST` / `DEFAULT_OLLAMA_HOST` は Task 3 で消す。このタスクでは残してよい。

- [ ] **Step 1: テストを書く**（`tests/test_vllm_config.py`）
  - 既定値が Global Constraints の表どおりであること。
  - 環境変数で各値を上書きできること。
  - `"http://h:8001/"` は `"http://h:8001"` になること。`"http://h:8001/v1"` と `"http://h:8001/v1/"` は、変数名入りの `ValueError` になること。
  - `reject_legacy_env`:
    - `{"OLLAMA_HOST": "http://x"}` は `ValueError` になり、文言に `OLLAMA_HOST` と `VLLM_` が入ること。
    - `{"OLLAMA_HOST": ""}` と `{}` は通ること。
  - `new_session`:
    - キーがあれば `session.headers["Authorization"] == "Bearer k"` であること。
    - キーが無ければ `Authorization` を付けないこと。
  - `raise_for_auth`: 401 と 403 は `AuthError`（文言に `VLLM_API_KEY` が入る）、200 と 500 は何もしないこと。

- [ ] **Step 2: 落ちることを確かめる**
  Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_vllm_config.py -q` → Expected: FAIL（モジュールが無い）

- [ ] **Step 3: 実装する**
  - 入口の4か所では、`load_dotenv()` のあとに `reject_legacy_env()` と `load_vllm_settings()` を呼ぶ。`ValueError` は、各入口の既存のやり方で日本語にして終了コード 2 で止める。`rag_web/__main__.py` は `エラー: …` を表示して 2 で止める。
  - `rag_web/app.py` は、`create_app` の中で `load_vllm_settings().chat_model` を使う。テストから差し替えられるように、`create_app` の引数（例: `chat_model: str | None = None`）で受けてよい。
  - 画面の状態表示や `/api/status` にモデル名を出している箇所があれば、その値を使う。

- [ ] **Step 4: 入口のテストを足す**
  - `tests/test_web_main.py`: `OLLAMA_HOST` が残っていれば `main()` が 2 を返し、エラー文が出ること。
  - `scripts.ingest_source` / `scripts.rag_mcp_server` / `scripts.check_retrieval` の各テストファイル: 同じく `OLLAMA_HOST` があれば止まること。1つのファイルに1本ずつ足す。

- [ ] **Step 5: 全体を流す**
  Run: `.\rag_env\Scripts\python.exe -m pytest -q`、および `-W error` を付けた `tests/test_web_*.py` → Expected: 失敗 0

- [ ] **Step 6: Commit** `feat: add vLLM connection settings and reject legacy OLLAMA_* variables`

---

### Task 2: 生成（`ingest/chat.py`）を OpenAI 互換 API にする

**Files:**
- Modify: `ingest/chat.py`、`docgen/filling.py`（`GENERATION_NUM_CTX` を消し、理由を書いたコメントにする）、`rag_web/services.py`、`rag_web/cowork.py`
- Test: `tests/test_chat.py`、`tests/test_web_cowork.py`（`test_generation_uses_larger_context_size` を書き直す）、ほかに `num_ctx` を参照するテスト

**Interfaces:**
- Consumes: Task 1 の `load_vllm_settings`、`new_session`、`raise_for_auth`、`AuthError`
- Produces:
  - `ask_json(model: str, prompt: str, session=None) -> str`
  - `ask_text(model: str, prompt: str, session=None) -> str`
  - `stream_chat(model: str, messages: list[dict], temperature: float, session=None) -> Iterator[str]`
  - `ChatError` は今のまま残す。
  - `num_ctx` 引数、`NUM_CTX`、`GENERATION_NUM_CTX` は消す。

- [ ] **Step 1: テストを書き直す**（`tests/test_chat.py`。偽物は `/v1/chat/completions` の形で返す）
  - `ask_json`:
    - `POST {chat_url}/v1/chat/completions` に、`model`、`messages`、`temperature: 0`、`response_format: {"type": "json_object"}`、`stream: false` を送ること。
    - `choices[0].message.content` を返すこと。
  - `ask_text`: `response_format` も `max_tokens` も送らないこと。`content` を返すこと。
  - 思考の分離: 非ストリームの応答に `reasoning_content` があっても、返り値は `content` だけであること。
  - `stream_chat`: SSE の各行（`data: {...}`）から `choices[0].delta.content` だけを順に出すこと。Review Focus 1 の5つの場合を、それぞれテストにする。
  - 再試行:
    - 接続エラーと 5xx は、計4回試してから `ChatError` になること。文言は「<chat_url> への生成リクエストが4回失敗しました」で始まること。
    - 401 は1回で `AuthError` になること（`ChatError` の子クラスにするか、`ChatError` に包む。どちらにしたかをテストで固定する）。
  - Ollama 固有の指定（`keep_alive`、`options`、`format`）を送らないこと。

- [ ] **Step 2: 落ちることを確かめる** Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_chat.py -q` → Expected: FAIL

- [ ] **Step 3: 実装する**
  - 冒頭の docstring にある Ollama の経緯（`num_ctx` が `extra_body` で効かなかった話）は、vLLM の事情に書き直す。
    - 文脈の長さはサーバーの `--max-model-len` で決まる。
    - 思考は別の欄で返るので、本文には混ぜない。
  - Cowork の長い出力について。
    - `max_tokens` を指定せず、サーバーの最大長に任せる。
    - 理由（思考も出力の上限を食うため、小さく決め打ちすると途中で切れる）を、`docgen/filling.py` の旧 `GENERATION_NUM_CTX` の位置にコメントで書く。
  - `rag_web/services.py` と `rag_web/cowork.py` の `num_ctx=` 渡しを消す。
  - `test_generation_uses_larger_context_size` は、`test_generation_does_not_cap_output_tokens` に書き直す。内容は、Cowork の生成が `ask_text(model, prompt)` を出力の上限なしで呼ぶことを確かめるもの。

- [ ] **Step 4: テストを流す** Run: 全体と `-W error` の `tests/test_web_*.py` → Expected: 失敗 0

- [ ] **Step 5: Commit** `feat: generate answers through the vLLM chat completions API`

---

### Task 3: 埋め込みと画像説明を OpenAI 互換 API にする

**Files:**
- Modify: `ingest/embedder.py`、`ingest/vlm.py`、`ingest/reranker.py`（`check_ollama` に触れた docstring だけ直す）
- Modify: `rag_web/services.py`、`rag_web/app.py`（`require_ollama` を `require_embedder` にする）、`rag_web/answering.py`・`rag_web/cowork.py`（コメントの「Ollama」）、`scripts/ingest_source.py`、`scripts/check_retrieval.py`、`scripts/rag_mcp_server.py`（`embedder.new_session` と `check_ollama` の呼び出し）
- Test: `tests/test_embedder.py`、`tests/test_vlm.py`、`tests/test_web_app.py`、`tests/test_ingest_source.py`、`tests/test_check_retrieval.py`、`tests/test_rag_mcp_server.py`（該当する箇所だけ）

**Interfaces:**
- Consumes: Task 1
- Produces:
  - `embedder.embed_texts(texts, session=None) -> list[list[float]]` と `embedder.embed_query(text, session=None) -> list[float]`（シグネチャは今と同じ）
  - `embedder.EMBED_DIM = 1024`
  - `embedder.check_embedder(session=None) -> None`（旧 `check_ollama`）
  - `vlm.caption_image(image_bytes, session=None) -> str` と `vlm.check_vlm(session=None) -> None`
  - `embedder.OLLAMA_HOST` / `DEFAULT_OLLAMA_HOST` / `new_session` は消す。呼び出し側は `ingest.vllm_config.new_session` を使う。
  - `vlm.VLM_MODEL` 定数は消し、`load_vllm_settings().vlm_model` を使う。

- [ ] **Step 1: テストを書き直す**
  - 埋め込みの要求:
    - `POST {embed_url}/v1/embeddings` に `{"model": embed_model, "input": [...]}` を送ること。
    - `data[].embedding` を `index` の順に並べて返すこと。逆順で返ってきても対応がずれないこと（Review Focus 4）。
  - 次元が 1024 でないときは、DB に書く前に `EmbeddingError` になること（今と同じ）。
  - 401 は再試行せずに止まること。
  - `check_embedder`:
    - `GET {embed_url}/v1/models` の `data[].id` に `embed_model` があれば通ること。
    - 無ければ、`EmbeddingError` になり、文言に `VLLM_EMBED_MODEL` とサーバーにあるモデル名が入ること。
    - 接続できなければ、「vLLM（埋め込み）に接続できません（<url>）」になること。
  - `caption_image`:
    - `POST {vlm_url}/v1/chat/completions` に、`content: [{"type":"text","text":CAPTION_PROMPT},{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]` を送ること。
    - 画像の形式は、先頭のバイトから PNG / JPEG を見分けて MIME を付けること。それ以外は `image/png` にする。
    - `content` を strip して返すこと。
  - `check_vlm`: `/v1/models` で、上と同じ形の確認をすること。
  - `rag_web` で、埋め込みのサーバーが落ちているときの API の挙動が、今の文言の形のまま `vLLM` になっていること（既存のテストの期待値を直す）。

- [ ] **Step 2: 落ちることを確かめる** Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_embedder.py tests/test_vlm.py -q` → Expected: FAIL

- [ ] **Step 3: 実装し、呼び出し側を直す**
  - モジュールの docstring にある Ollama の実測（localhost と IPv6、`keep_alive`）は、vLLM に関係するものだけ残す。
    - 「localhost を使わず 127.0.0.1 を使う」理由は、vLLM でも同じなので残す。
    - `EMBED_BATCH_SIZE` は仮に今の 8 のままにし、「GB10 で計測して決め直す」とコメントする（設計書「計測前の既定値」）。

- [ ] **Step 4: 残りを確かめる**
  Run: `git grep -n -E "OLLAMA_|check_ollama|/api/(chat|embed|tags)|keep_alive" -- ingest docgen rag_web scripts/ingest_source.py scripts/rag_mcp_server.py scripts/check_retrieval.py tests ':!tests/test_agent_*' ':!tests/test_coding_agent_cli.py' ':!tests/test_colab_*'`
  Expected: 0 件（Task 1 の `reject_legacy_env` とそのテストは除く）

- [ ] **Step 5: テストを流す** → 失敗 0

- [ ] **Step 6: Commit** `feat: embed and caption through the vLLM OpenAI-compatible API`

---

### Task 4: DB に埋め込みモデルを記録し、既存ベクトルとの一致を測るスクリプトを作る

**Files:**
- Modify: `ingest/vector_store.py`（`meta` の読み書きを足す）、`ingest/store.py`（確認の関数と、最初の書き込みでの記録）
- Modify: `rag_web/corpora.py`（`CorpusCache.collection` で開いたときに確かめる）、`scripts/ingest_source.py`、`scripts/rag_mcp_server.py`、`scripts/check_retrieval.py`
- Create: `scripts/check_embedding_compat.py`
- Test: `tests/test_vector_store.py`、`tests/test_store.py`、`tests/test_web_corpora.py`、`tests/test_rag_mcp_server.py`、`tests/test_check_embedding_compat.py`

**Interfaces:**
- Produces:
  - `VectorStore.get_meta(key: str) -> str | None` と `VectorStore.set_meta_if_absent(key: str, value: str) -> None`。`meta` 表の `value` 列は INTEGER 型だが、SQLite は文字列もそのまま入れられる。ただし、読み出したときに文字列に戻ることをテストで固定する。
  - `VectorStore.sample_embeddings(limit: int) -> list[tuple[str, np.ndarray]]`: チャンク本文と、保存済みのベクトル（正規化済み）の組を返す。
  - `ingest.store.EmbeddingMismatchError(Exception)`
  - `ingest.store.ensure_embedding_model(collection, model: str, dim: int) -> None`:
    - 記録が無ければ `bge-m3`/`1024` とみなす。
    - 比べる前に、モデル名を正規化する（`BAAI/bge-m3` → `bge-m3`。大文字小文字は区別しない）。
    - 食い違えば `EmbeddingMismatchError("この DB は <記録> で作られています。設定は <model> です。…")` を投げる。
  - `ingest.store.record_embedding_model(collection, model: str, dim: int) -> None`: 記録が無いときだけ書く。`replace_source` の中で、最初の書き込みの直前に呼ぶ。

- [ ] **Step 1: テストを書く**（Review Focus 5 のすべての場合を、それぞれ1本にする）
  - 記録の無い DB は、設定が `bge-m3`/1024 なら通り、`qwen3-embedding`/2560 なら止まること。
  - `BAAI/bge-m3` の記録に対して、設定が `bge-m3` なら通ること。
  - 書き込みで記録が残り、2回目の書き込みで上書きされないこと。
  - `CorpusCache.collection` と MCP サーバーの検索で、食い違った DB が止まること。止まったときに利用者へ出る文言を確かめる。
- [ ] **Step 2: `scripts/check_embedding_compat.py` のテストを書く**（偽の埋め込みを使う）
  - `--db <path> --limit N`（既定 300）で、保存済みのベクトルと、新しく埋め込んだベクトルのコサイン類似度を測ること。
  - 件数・最小値・平均を表示すること。
  - 最小値が `--threshold`（既定 0.99）未満なら、終了コード 1 になること。
  - 空の DB なら、その旨を表示して終了コード 2 になること。
- [ ] **Step 3: 落ちることを確かめる → 実装する → 全体を流す**（失敗 0）
- [ ] **Step 4: Commit**
  - `feat: record the embedding model in the vector store and refuse mismatches`
  - `feat: add a script that compares stored vectors with vLLM embeddings`

---

### Task 5: Colab のノートブックを Bearer 認証と複数モデルに対応させる

始める前に、利用者が VS Code で `colab/run_vllm_server.ipynb` のタブを閉じたことを確かめる。

**Files:**
- Modify: `colab/run_vllm_server.ipynb`
- Test: `tests/test_colab_vllm_notebook.py`

**Interfaces:**
- Produces（ノートブックの振る舞い）:
  - 起動する組を選ぶ変数を1つ設ける。
    - `"chat+embed"`（既定）: gpt-oss-20b と bge-m3
    - `"vlm"`: Qwen3-VL-8B
  - 選んだ組の vLLM を、別々のポートで起動する。GPU メモリの割合は、組ごとに決めた値を使う。
  - 認証プロキシは1つにまとめ、パスの先頭（`/chat`、`/embed`、`/vlm`）で振り分ける。こうすれば、ngrok の URL も1つで済む。
    - 例: `VLLM_CHAT_URL=https://xxx.ngrok-free.app/chat`、`VLLM_EMBED_URL=https://xxx.ngrok-free.app/embed`
  - プロキシは `Authorization: Bearer <キー>` を確かめる（`X-API-Key` はやめる）。
  - 速度の確認セルと、手元から試す PowerShell の例も、Bearer 認証とこの URL の形に合わせる。
  - 開発機の `.env` に書く `VLLM_*` の例を、最後のセルに出す。値は表示するだけで、ファイルには書かない（既存の方針）。

- [ ] **Step 1: テストを直す・足す**
  - プロキシ:
    - 正しい Bearer があれば `/chat/v1/...` を chat の vLLM へ、`/embed/v1/...` を埋め込みの vLLM へ転送すること。
    - `X-API-Key` だけ、またはキー違いなら、401 を返して転送しないこと。
    - 知らない先頭のパスは 404 にすること。
  - 起動: 組ごとに、期待するモデル名・ポート・`--served-model-name` で起動すること。gpt-oss の起動の指定は、固定した版の公式文書で確かめた値にする（設計書「gpt-oss の起動の指定」）。
  - 既存のテスト（保存済みの実行結果やメタデータが無いこと、ngrok のシークレット、起動失敗時のログ表示）が通ること。
- [ ] **Step 2: 落ちることを確かめる → JSON の文字列だけを書き換えて実装する → `git show HEAD:colab/run_vllm_server.ipynb` が正しい JSON であることを確かめる**
- [ ] **Step 3: Commit** `feat: serve chat and embeddings behind one Bearer proxy in the Colab notebook`

---

### Task 6: Colab での実機確認（コントローラーと利用者で行う）

このタスクはサブエージェントに渡さない。コントローラーが利用者に Colab での実行を頼み、結果を受けて判断する。

- [ ] **Step 1:** 利用者が `"chat+embed"` の組でノートブックを実行し、開発機の `.env` に `VLLM_*` を設定する。
- [ ] **Step 2: 既存ベクトルとの一致を測る**
  Run: `.\rag_env\Scripts\python.exe -m scripts.check_embedding_compat --db vector_store.sqlite3` と `--db docs_store.sqlite3`
  Expected: 最小値 0.99 以上で、終了コード 0。下回ったら、ここで止めて利用者に報告する。そのときは、設計書「将来の埋め込みモデルの変更」の再計算で揃える案を出す。
- [ ] **Step 3: JSON の出力と翻訳を確かめる**
  Run: `.\rag_env\Scripts\python.exe -m scripts.check_retrieval`（技術ドキュメントの翻訳と、社内資料の条件抽出が両方走る設定で実行する）
  Expected:
  - 翻訳と条件抽出が JSON として解釈できること。
  - 当たり方が、README と設計書に残っている今の記録と大きく変わらないこと。
- [ ] **Step 4: 画面での確認**: `.\run_web.ps1` で、チャット（社内資料・技術ドキュメント）と Cowork が動くこと。回答が逐次表示されること。
- [ ] **Step 5:** 利用者が `"vlm"` の組で、画像を含む資料をアップロードし、画像の説明が付くことを確かめる。
- [ ] **Step 6:** 結果（件数、最小値、平均、日付、気づいたこと）を進捗の台帳に記録する。Task 7 の文書には、これを書く。

---

### Task 7: GB10 への配置設定と文書

**Files:**
- Create:
  - `deploy/gb10/compose.yaml`（vLLM 3つ。イメージの版は固定し、`--served-model-name`・`--api-key`（ファイルか環境変数から渡し、値は書かない）・`--gpu-memory-utilization`・`--max-model-len`・gpt-oss の推論と道具呼び出しの指定を書く。ポートは `127.0.0.1` にだけ公開する）
  - `deploy/gb10/rag-web.service`、`deploy/gb10/rag-mcp.service`（systemd）
  - `deploy/gb10/nginx-rag.conf`（`/`・`/llm/`・`/mcp` の3経路）
- Modify（全面的に書き直す）: `docs/server-deployment.md`（GB10 への配置手順）、`docs/claude-code-gb10.md`（vLLM への直結と、GB10 上の MCP。旧手順は「旧手順」と明記した節に縮めるか、消す）
- Modify:
  - `AGENTS.md`（外部へ出る経路の段落を、`OLLAMA_HOST` から `VLLM_*` の3経路にする）
  - `README.md`
  - `.env.example`
  - `docs/依存関係一覧.md`、`docs/処理箇所マップ.md`
  - `docs/mcp-server.md`、`docs/mcp-server-network.md`
  - `docs/qwen/` の Ollama 前提の文書: 冒頭に「旧手順」の注記を付けるだけにする。
  - `docs/superpowers/specs/2026-11-22-vllm-backend-design.md`（Task 6 の実測結果）

**Interfaces:**
- Consumes: Task 1 の環境変数名と既定値、Task 6 の実測結果。段階③の設計書「段階③への申し送り」で「vLLM 切り替えの配置手順（GB10）で対処」とした項目（`client_max_body_size`、`proxy_buffering off`、`proxy_read_timeout`）。

- [ ] **Step 1: 設定ファイルを書く**
  - vLLM のイメージの版と gpt-oss の起動の指定は、公式の公開情報で確かめる（調べるときは、社内の情報を含まない一般的な語だけを使う。AGENTS.md の規則）。
  - Nginx の設定について。
    - `/mcp` のトークンと `/` の htpasswd は、リポジトリの外のファイルを `include` する形にする。
    - `client_max_body_size` は、段階③の設計書の式の値にする（既定の 50MB なら 70,953,643 バイト）。
    - `/` と `/llm/` は `proxy_buffering off` にし、読み取りのタイムアウトを延ばす。
    - `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` を設定する。
    - Nginx を Docker で動かす場合は `RAG_WEB_FORWARDED_ALLOW_IPS` をブリッジの IP にすること（設計書）を、コメントで書く。
- [ ] **Step 2: `docs/server-deployment.md` を GB10 向けに書く**
  - 段階③の計画（`docs/superpowers/plans/2026-11-22-fastapi-web-ui-stage3.md`）の Task 4 Step 1 に挙げた項目を押さえる。Ollama は vLLM に置き換える。
  - メモリ配分の確かめ方（設計書「メモリ配分」）を書く。
  - 実測していない部分には「実測はしていない」と書く。
- [ ] **Step 3: `docs/claude-code-gb10.md` を書き直す**
  - 設計書「Claude Code から使う」の `settings.json` の例を載せる。
  - MCP の登録（`--header`）を書く。
  - 内部CAを使う場合の `NODE_EXTRA_CA_CERTS` を書く。
  - 冒頭に「知っておくこと」（サポート対象外であること、道具呼び出しの失敗報告があること、RAG と 120b を共有すること）を置く。
  - 確認項目には「VS Code 拡張が環境変数を読むか」を入れる。
- [ ] **Step 4: AGENTS.md・README・その他の文書を直す**
  - AGENTS.md の外部経路の説明は、今と同じ粒度で `VLLM_*` の3経路に書き換える。
    - GB10 の `127.0.0.1` を指していれば、外へは出ない。
    - 開発機から Colab や GB10 を指すと、質問と資料の本文がその経路に乗る。
  - 残りを確かめる。
    Run: `git grep -n -i -E "OLLAMA_HOST|OLLAMA_API_KEY|OLLAMA_VLM_MODEL|ollama pull|/api/(chat|embed|tags)" -- . ':!docs_source' ':!docs/superpowers' ':!coding_agent' ':!scripts/coding_agent.py' ':!scripts/measure_autocomplete.py' ':!scripts/proxy_log.py' ':!docs/gb10-coding-agent.md' ':!docs/qwen' ':!colab/run_ollama_server.ipynb' ':!tests/test_agent_*' ':!tests/test_coding_agent_cli.py' ':!tests/test_colab_agent_notebook.py' ':!sqlite-vector-store'`
    Expected: 残るのは次の2つだけ。
    - `reject_legacy_env` とそのテスト
    - 日付つきの実測の記録（Ollama で測った過去の事実）
    残ったものは、報告に列挙する。
- [ ] **Step 5: テストを流す**（失敗 0）
- [ ] **Step 6: Commit**
  - `feat: add GB10 deployment files for vLLM, rag_web and MCP`
  - `docs: rewrite the GB10 deployment and Claude Code guides for vLLM`
  - `docs: describe the vLLM endpoints in AGENTS.md, README and references`
