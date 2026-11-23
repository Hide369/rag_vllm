# 社内資料 RAG MCP サーバの設定

`scripts/rag_mcp_server.py` は、取り込み済みの社内資料（`vector_store.sqlite3`）を
Codex から検索できるようにする stdio MCP サーバである。ツールは1本だけで、
検索結果は回答文ではなく根拠となる原文を出典つきで返す。

```
search_documents(query: str, n_results: int = 4) -> str
```

- `query`: 検索したい内容。自然文でよい。
- `n_results`: 返すチャンク数。既定は `SEARCH_RESULT_COUNT`（4）。

設計の経緯・却下案・エラーハンドリングの全体像は
[2026-09-05-rag-mcp-server-design.md](superpowers/specs/2026-09-05-rag-mcp-server-design.md)
にある。本ドキュメントは導入先で手を動かすための手順に絞る。

**本書は1台のPCで完結する構成である。** Codex がサーバを子プロセスとして
起動する（stdio）。サーバ機を1台立てて複数のPCから引く構成は
[mcp-server-network.md](mcp-server-network.md) にある。`config.toml` の
書き方が異なり、両方を書くと衝突する。

## 前提

- 埋め込みの vLLM（`bge-m3`）が起動していること（下記）
- Python 依存関係が導入済みであること（`pip install -r requirements.txt`。`mcp` を含む）
- 資料が `source/` に配置され、`python -m scripts.ingest_source` で取り込み済みであること

DB のパスはサーバが `store.DB_PATH`（`ingest/store.py:20`。リポジトリルート直下の
`vector_store.sqlite3`）から自動的に決める。引数にも環境変数にも取らないため、
利用者が DB の場所を設定する項目はない。

### 埋め込みの vLLM（`bge-m3`）は必須

質問文のベクトル化に使う埋め込みモデルは `bge-m3` である（`ingest/embedder.py` の
`EMBED_DIM` が1024次元を前提にし、モデル名は `VLLM_EMBED_MODEL`）。これが動いていないと
検索が一切成立しない。宛先は `VLLM_EMBED_URL`（既定 `http://127.0.0.1:8002`）で、
認証が要るなら `VLLM_API_KEY`。起動は [server-deployment.md](server-deployment.md)
（開発機なら Colab の `chat+embed` の組。README「ColabのL4でvLLMを試す」）。

**このサーバが vLLM に求めるのは埋め込み（`bge-m3`）だけである。** 回答文を作らないため
生成モデル（`gpt-oss` 等）は一切呼ばない。回答を書くのは Claude Code や Codex 側である。

取り込み時と検索時で埋め込みモデルが食い違うと、例外は出ないまま検索結果だけが
無意味になる。DB には埋め込みモデルが記録され、設定と食い違う DB は開いた時点で
止まる（このサーバも止まる）。取り込み側（`scripts/ingest_source.py`）も同じ `bge-m3` を使う。

[mcp-server-network.md](mcp-server-network.md) の同名の節にある。

### エラー時の応答

埋め込みの vLLM が起動していない、あるいは `VLLM_EMBED_MODEL` のモデルが載っていない場合は、`embedder` が
持つ案内文言（「vLLM（埋め込み）に接続できません」「埋め込みモデル … がサーバーにありません」等）がそのまま返る。
想定内のエラー（vLLM 疎通・埋め込みの失敗）は検索結果と同じ形式のテキストとして
届く。リランカーの取得に失敗した場合は、エラーにせず RRF 順のまま検索を続ける。

それ以外の想定外の例外はサーバプロセスを落とさず、MCP の `is_error=True` な結果に
なる。ただしそこに載るのは `Error executing tool search_documents` という定型文
だけで、元の例外の文面は届かない（mcp 2.2.0 の
`mcp/server/mcpserver/exceptions.py:66-67`）。原因はサーバの標準エラー出力に
トレースバックとして出るので、そちらを見ること。

## 導入先での作業手順（まとめ）

1. `pip install -r requirements.txt`（`mcp` を含む）
2. 埋め込みの vLLM（`bge-m3`）を起動する（生成モデルは不要）
3. 資料を `source/` に配置し、`python -m scripts.ingest_source` で取り込む
4. `~/.codex/config.toml` に上記3サーバを登録する
5. `~/.codex/AGENTS.md` に `AGENTS.md` の「社内資料の参照」「外部ドキュメントの参照」を貼る
6. Codex を起動し直し、動作確認の手順で出典が返ることを見る

リランカー（`bge-reranker-v2-m3` ONNX）のモデルは初回実行時に HuggingFace Hub
から自動取得される。導入先はインターネットに到達できる前提のため、事前の
持ち込みは不要である。
