# ColabのL4でvLLMを立てるノートブック 設計

## 目的

RAGをOllamaからvLLMへ移すための第一歩として、Colab L4（24GB, Ada SM 8.9）上の
vLLMで `openai/gpt-oss-20b` を起動し、手元から OpenAI 互換API
（`/v1/chat/completions`）を叩けるようにする。動作と速度を確かめることが目的で、
RAG本体（`ingest/` など）のコードはこの段階では変えない。

Ollama版は `Hide369/local-llm-rag` にそのまま残し、vLLM版は履歴ごと複製した
このリポジトリ（`Hide369/rag_vllm`）で進める。Ollama版の修正は
`upstream-ollama` リモートから取り込める。

## ゴール / 非ゴール

- ゴール: `colab/run_vllm_server.ipynb` を上から実行すると、ngrok経由のURLと
  APIキーが表示され、手元から `X-API-Key` 付きで `/v1/models` と
  `/v1/chat/completions`（非ストリーム・ストリーム）が通る。
- ゴール: ノートブック内で応答時間（初トークンまで・全体・トークン/秒）を計れる。
- 非ゴール: bge-m3（埋め込み）と qwen3-vl（VLM）のvLLM化。RAG側の切り替え。
- アンチゴール: 認証なしでvLLMを外部公開すること。

## 検討した方式

1. **既存の認証プロキシ + ngrok を流用し、転送先だけvLLMにする（採用）**。
   ローカル側の `.env` の `OLLAMA_HOST` / `OLLAMA_API_KEY`（`X-API-Key`）の
   扱いをそのまま使え、既存ノートブックとテストの型にも乗れる。
2. vLLMの `--api-key` で認証し、ngrokでvLLMを直接公開する。部品は減るが、
   認証ヘッダーが `Authorization: Bearer` になりRAG側の `new_session()` と
   食い違う。後でRAGを切り替えるときに両方の流儀が混ざるため不採用。
3. Ollamaノートブックに vLLM 切り替えを足す。Ollama版を手付かずで残す方針に
   反するため不採用。

## 設計

- 新規 `colab/run_vllm_server.ipynb`。セル構成:
  1. L4 GPU の確認（`nvidia-smi`。L4以外なら止める）
  2. vLLM を専用の仮想環境へ導入（`uv venv` + `uv pip install vllm --torch-backend=auto`）。
     Colab同梱の torch との衝突を避けるため。
  3. `vllm serve openai/gpt-oss-20b --port 8001 --max-model-len 65536
     --gpu-memory-utilization 0.90` をバックグラウンド起動し、`/v1/models` が
     応答するまで待つ。ログは `vllm.log` に書き、失敗時は末尾を表示する。
     8001 なのはプロキシが 8000 を使うため。
  4. APIキー発行（既存と同じ。環境変数で渡し、ファイルへ埋め込まない）
  5. 認証プロキシの書き出し。既存ノートブックのコードを写し、転送先を
     `VLLM_UPSTREAM`（既定 `http://127.0.0.1:8001`）にする。
  6. プロキシ起動（再実行時に前回プロセスを止める挙動も既存どおり）
  7. ngrok 公開
  8. 動作・速度確認（Colab内から `/v1/chat/completions` を非ストリームと
     ストリームで叩き、`reasoning` と `content` の有無、時間を表示）
  9. 手元のPowerShellから叩く例（Markdown）
- テスト `tests/test_colab_vllm_notebook.py`: 既存
  `tests/test_colab_agent_notebook.py` と同じくセルを取り出して偽物の
  subprocess/requests/httpx で実行し、起動引数・ポート・認証拒否・転送を検証する。

## ビルダーに任せる点（筆者の判断）

- プロキシコードは既存ノートブックからの写しになる。Colabからリポジトリを
  読まない構成のため。重複はノートブック内に注記する（技術的負債）。
- `--max-model-len` は既存Ollamaノートブックの 65536 に合わせる。VRAM不足時は
  32768 へ下げる旨をセルに書く。
