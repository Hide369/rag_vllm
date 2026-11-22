# ColabのL4でvLLMを立てるノートブック 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Colab L4 上で vLLM（`openai/gpt-oss-20b`）を起動し、認証プロキシ + ngrok 経由で手元から OpenAI 互換APIを叩けるノートブックを作る。

**Architecture:** 新規ノートブック `colab/run_vllm_server.ipynb`。vLLM は専用 venv に入れて 8001 番で起動し、既存ノートブックと同じ形の FastAPI 認証プロキシ（8000 番）が `X-API-Key` を検証して転送、ngrok が 8000 番を公開する。Python のコードセルはテストから `exec` して検証する（既存 `tests/test_colab_agent_notebook.py` と同じ方式）。

**Tech Stack:** vLLM（uv で導入）, FastAPI / uvicorn / httpx, pyngrok, requests, pytest

**Spec:** `docs/superpowers/specs/2026-11-22-vllm-colab-notebook-design.md`

## Global Constraints

- モデル: `openai/gpt-oss-20b`。起動: `--port 8001 --max-model-len 65536 --gpu-memory-utilization 0.90`
- プロキシは 8000 番、転送先は環境変数 `VLLM_UPSTREAM`（既定 `http://127.0.0.1:8001`）
- APIキーは環境変数 `PROXY_API_KEY` で渡し、生成ファイルへ埋め込まない。ローカル側は既存どおり `.env` の `OLLAMA_HOST` / `OLLAMA_API_KEY`
- ノートブックは出力・実行回数・利用者メタデータを保存しない
- コード中のコメント・メッセージは日本語（既存ノートブックに合わせる）
- テストは `..\local_llm\myvenv313\Scripts\python.exe -m pytest` を rag_vllm 直下で実行（rag_vllm 専用 venv はまだ無い）

## Review Focus

- vLLM が起動途中で落ちた（OOM・未対応GPU等）→ 待ち時間を使い切らず、すぐに `vllm.log` の末尾を見せて止まること（Task 2 のテストで固定）
- セルの再実行 → 前回の vLLM / プロキシのプロセスを止めてから起動し、ポート競合で古いプロセスに当たり続けないこと（Task 2・Task 3）
- L4 以外の GPU（T4 等）を割り当てられた → 24GB 前提の設定で謎の失敗をする前に、GPU 名を示して止まること（Task 2）
- 誤った／無い APIキー → 401、転送しない、キーを応答に含めない（Task 3）
- ストリームに `reasoning` だけが来て `content` が空のまま終わる（文脈長の使い切り）→ 速度確認セルがそれを明示すること（Task 4）

---

### Task 1: プロキシ用の偽モジュールを共有ヘルパーへ切り出す

既存テストの `_install_proxy_fakes` を新しいテストでも使うため、DRY のために移す。振る舞いは変えない。

**Files:**
- Create: `tests/notebook_proxy_fakes.py`
- Modify: `tests/test_colab_agent_notebook.py`（`_install_proxy_fakes` を削除し import に置換）

**Interfaces:**
- Produces: `install_proxy_fakes(monkeypatch, upstreams: list) -> None`（中身は現行 `_install_proxy_fakes` と同一）

- [ ] **Step 1:** 関数を `tests/notebook_proxy_fakes.py` へ移し、既存テストは `from tests.notebook_proxy_fakes import install_proxy_fakes` を使う
- [ ] **Step 2:** `python -m pytest tests/test_colab_agent_notebook.py -q` → 全件 PASS（件数は移動前と同じ5件）
- [ ] **Step 3:** Commit `test: share notebook proxy fakes between notebook tests`

### Task 2: ノートブック骨格 + GPU確認・vLLM導入・vLLM起動セル

**Files:**
- Create: `colab/run_vllm_server.ipynb`（nbformat 4.5、metadata は既存ノートブックと同じ `accelerator: GPU` / `colab.gpuType: L4`）
- Create: `tests/test_colab_vllm_notebook.py`

**Interfaces:**
- Produces: セルは先頭コメント `# 1.` 〜 `# 8.` で識別（テストの `_code_cell(prefix)` が使う）。セル3はグローバル `vllm_proc`（Popen）, `VLLM_COMMAND: list[str]`, `VLLM_LOG_PATH = Path("vllm.log")` を定義する
- Produces: 定数 `VLLM_VENV = "/content/vllm-env"`, `VLLM_MODEL = "openai/gpt-oss-20b"`, `VLLM_PORT = 8001`

セル構成（Markdown 冒頭の手順説明は既存ノートブックの形に合わせ、vLLM 用に書き換える）:
1. `# 1.` GPU確認: `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader`。失敗/空なら RuntimeError、名前に `L4` が無ければ GPU 名を含む RuntimeError
2. `# 2.` 導入: `pip install -q uv` → `uv venv {VLLM_VENV} --python 3.12` → `uv pip install --python {VLLM_VENV}/bin/python vllm --torch-backend=auto` → `{VLLM_VENV}/bin/vllm download-kernels`。`subprocess.run(..., check=True)`
3. `# 3.` 起動: 前回の `vllm_proc` が生きていれば terminate/wait。`VLLM_COMMAND = [f"{VLLM_VENV}/bin/vllm", "serve", VLLM_MODEL, "--port", "8001", "--max-model-len", "65536", "--gpu-memory-utilization", "0.90"]` を stdout/stderr→`vllm.log` で Popen。最大1800秒、5秒ごとに `http://127.0.0.1:8001/v1/models` を確認。`vllm_proc.poll()` が非 None になったら即座にログ末尾（3000字）を表示して RuntimeError。タイムアウトでも同様。VRAM 不足時は 65536→32768 に下げる旨をコメントに書く

- [ ] **Step 1: 失敗するテストを書く**
  - `test_gpu_check_rejects_non_l4_gpu`: 偽 `subprocess.run` が `"Tesla T4, 15360 MiB\n"` を返す → `RuntimeError` で、メッセージに `"Tesla T4"` を含む
  - `test_vllm_starts_on_8001_with_spec_flags`: 偽 Popen（poll→None）と偽 requests.get（200）で `# 3.` を exec → Popen の第1引数が `VLLM_COMMAND` で、`"serve"`, `"openai/gpt-oss-20b"`, `"--port"`, `"8001"`, `"--max-model-len"`, `"65536"`, `"--gpu-memory-utilization"`, `"0.90"` を含む
  - `test_vllm_crash_fails_fast_with_log_tail`: poll→1、`vllm.log` に `"CUDA out of memory"` → RuntimeError、`time.sleep` 呼び出しが1800秒分に達していない（偽 sleep の合計 < 60）、標準出力に `"CUDA out of memory"`
  - `test_vllm_restart_terminates_previous_process`: namespace に生きている偽 `vllm_proc` を入れて exec → その `terminate` が呼ばれる
  - `test_notebook_has_no_saved_execution_data_or_user_metadata`: 既存テストと同じ検査を新ノートブックに対して行う
- [ ] **Step 2:** `python -m pytest tests/test_colab_vllm_notebook.py -q` → ノートブック未作成で FAIL
- [ ] **Step 3:** ノートブックを作成（セル `# 1.`〜`# 3.` と冒頭 Markdown）。ipynb は Python スクリプトで JSON を書き出して作り、出力は空のまま
- [ ] **Step 4:** 同コマンドで PASS
- [ ] **Step 5:** Commit `feat: add vLLM Colab notebook with GPU check and server startup`

### Task 3: APIキー・認証プロキシ・プロキシ起動・ngrok セル

**Files:**
- Modify: `colab/run_vllm_server.ipynb`
- Modify: `tests/test_colab_vllm_notebook.py`

**Interfaces:**
- Consumes: `install_proxy_fakes`（Task 1）
- Produces: `# 4.` が `os.environ["PROXY_API_KEY"]` とグローバル `PROXY_API_KEY` を設定。`# 5.` が `vllm_proxy.py` を書き出す（モジュール内に `_client`, `async def proxy(path, request)`）。`# 6.` がグローバル `proxy_proc` を起動、`# 7.` が `public_url` を表示

プロキシは `colab/run_ollama_server.ipynb` のセル6の写し。違いは次だけ: キーは `PROXY_API_KEY`、転送先は `os.environ.get("VLLM_UPSTREAM", "http://127.0.0.1:8001")`、エラー文言は `"vLLM upstream timed out"`（504）/ `"Unable to reach vLLM upstream"`（502）。写しであること（Colab からリポジトリを読まないため）をセル冒頭のコメントに書く。`# 6.` の起動確認は `/v1/models` に対して行い、再実行時の前回プロセス停止は既存セル7と同じ。

- [ ] **Step 1: 失敗するテストを書く**（既存の3つのプロキシテストと同じ形）
  - `test_proxy_reads_key_at_runtime_and_streams_v1_routes`: キーが生成ファイルに含まれない。`v1/chat/completions` の SSE が素通しされ、upstream が close される。`_client.kwargs["base_url"] == "http://127.0.0.1:8001"`
  - `test_proxy_upstream_overridable_by_env`: `VLLM_UPSTREAM=http://127.0.0.1:9999` → `base_url` がその値
  - `test_proxy_rejects_missing_and_invalid_keys_without_forwarding`: 401、upstreams 未消費、キーが応答に無い
  - `test_proxy_maps_upstream_connection_and_timeout_errors`: 504 `{"error": "vLLM upstream timed out"}` / 502 `{"error": "Unable to reach vLLM upstream"}`
- [ ] **Step 2:** 実行して FAIL を確認
- [ ] **Step 3:** セル `# 4.`〜`# 7.` を追加
- [ ] **Step 4:** `python -m pytest tests/test_colab_vllm_notebook.py tests/test_colab_agent_notebook.py -q` → PASS
- [ ] **Step 5:** Commit `feat: add authenticated proxy and ngrok cells to vLLM notebook`

### Task 4: 動作・速度確認セルと手元からの呼び出し例

**Files:**
- Modify: `colab/run_vllm_server.ipynb`
- Modify: `tests/test_colab_vllm_notebook.py`

**Interfaces:**
- Consumes: `PROXY_API_KEY`（Task 3）
- Produces: `# 8.` で定義する `measure_chat(base_url: str, api_key: str, prompt: str, max_tokens: int = 1024) -> dict` を実行。戻り値キー: `first_token_seconds: float | None`, `total_seconds: float`, `completion_tokens: int | None`, `tokens_per_second: float | None`, `reasoning_chars: int`, `content: str`

`measure_chat` は `requests.post(f"{base_url}/v1/chat/completions", headers={"X-API-Key": api_key}, json={"model": "openai/gpt-oss-20b", "messages": [...], "stream": True, "stream_options": {"include_usage": True}, "max_tokens": max_tokens}, stream=True, timeout=600)` を送り、`raise_for_status()` の後に SSE の `data:` 行を `[DONE]` まで読む。`delta.reasoning` と `delta.content` の最初の到着で初トークン時刻を取り、`usage.completion_tokens` からトークン/秒を出す。セル本体は `measure_chat("http://127.0.0.1:8000", PROXY_API_KEY, "日本の首都はどこですか？一文で答えてください。")` を呼んで結果を表示し、`content` が空なら「思考だけで max_tokens を使い切った可能性」を表示する。

最後の Markdown セル「手元から試す」: PowerShell の `Invoke-RestMethod` で `$env:OLLAMA_HOST/v1/models` と `/v1/chat/completions` を `X-API-Key` 付きで叩く例。RAGアプリはまだ Ollama API を使うため、このURLを `.env` に入れても RAG は動かない旨を明記する。

- [ ] **Step 1: 失敗するテストを書く**（偽 `requests.post` が SSE 行を返す）
  - `test_measure_chat_reports_reasoning_content_and_speed`: reasoning 2 チャンク・content 2 チャンク・usage `completion_tokens: 40`・`[DONE]` → `content == "東京です。"`、`reasoning_chars > 0`、`completion_tokens == 40`、`tokens_per_second > 0`、送信ヘッダーに `X-API-Key`、URL が `/v1/chat/completions`
  - `test_measure_chat_flags_reasoning_only_answer`: reasoning のみで `[DONE]` → `content == ""`、セル実行時の出力に「max_tokens」を含む
  - `test_measure_chat_raises_on_http_error`: `raise_for_status` が例外 → 握りつぶさず伝播
- [ ] **Step 2:** 実行して FAIL を確認
- [ ] **Step 3:** セル `# 8.` と Markdown を追加
- [ ] **Step 4:** `python -m pytest -q`（全体）→ 失敗 0
- [ ] **Step 5:** Commit `feat: add speed check cell and local usage guide to vLLM notebook`

### Task 5: README への導線と PR

**Files:**
- Modify: `README.md`（「ColabのL4 GPU」節の近くに vLLM ノートブックと、RAG 本体はまだ Ollama API 前提である旨を数行）

- [ ] **Step 1:** 追記し、Commit `docs: point README to the vLLM Colab notebook`
- [ ] **Step 2:** `git push -u origin feat/vllm-colab-notebook` し、`Hide369/rag_vllm` へ PR を作成
