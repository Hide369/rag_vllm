# RAG の推論を Ollama から vLLM へ切り替える 設計

## 目的

本番の設置先は社内LANの GB10（DGX Spark、DGX OS 7 = Ubuntu 24.04、`aarch64`、
統合メモリ 128GB）1台に決まった。そこで RAG の推論（回答生成・埋め込み・画像説明）を
すべて vLLM（OpenAI 互換 API）で行う。あわせて生成モデルを `gpt-oss-120b` に上げ、
埋め込みと画像説明のモデルも GB10 の余力に見合うものへ上げる。

もう1つの用途として、社内のクライアントPCで使う **Claude Code（VS Code 拡張）** が、
GB10 の `gpt-oss-120b` と社内資料の検索（MCP の `search_documents`）を使えるようにする。
いずれも社内で閉じ、クラウドのモデルは使わない（利用者の選択 2026-11-22）。

今は `ingest/chat.py`・`ingest/embedder.py`・`ingest/vlm.py` が Ollama のネイティブ API
（`/api/chat`・`/api/embed`・`/api/tags`）を直接叩いている。これを OpenAI 互換 API に
書き換え、変数名・環境変数・文言の「OLLAMA」を「VLLM」に改める（以前の合意 (2)）。

## ゴール / 非ゴール / アンチゴール

- ゴール: GB10 上の vLLM 3プロセス（生成・埋め込み・画像説明）だけで、RAG の全機能
  （チャット・Cowork・取り込み・アップロード・MCP 検索）が動く。GB10 に Ollama を置かない。
- ゴール: 埋め込みモデルを**自分の資料で測ってから**決める（下の「埋め込みモデルの選定」）。
- ゴール: クライアントの Claude Code が、ドロップダウンの枠から GB10 の `gpt-oss-120b` を
  選べ、社内資料の MCP を HTTP で引ける。
- ゴール: モデル名・接続先は環境変数で変えられる（開発機と GB10 でモデルが違うため）。
- 非ゴール: クラウドのモデル（Anthropic など）への接続、Claude と GB10 のモデルを1つの
  ドロップダウンで混ぜること（利用者の選択 (a)）。
- 非ゴール: Codex 向けの部品（`coding_agent/`、`scripts/coding_agent.py`、
  `scripts/measure_autocomplete.py`、`scripts/proxy_log.py`、`docs/gb10-coding-agent.md`）の
  書き換え。Codex はもう使っていない（利用者 2026-11-22）。撤去するかは別に決める。
- 非ゴール: リランカーの変更。`bge-reranker-v2-m3`（ONNX、アプリ内）のまま。
- アンチゴール: 質問・資料の本文・検索結果が社外へ出ること。外へ出るのは今と同じく
  リランカーの確認（huggingface.co への HEAD）だけにとどめる。
- アンチゴール: 埋め込みモデルの違う DB を黙って混ぜて使うこと（距離が意味を失う）。

## 検討した方式

推論サーバーの置き方で3つを比べ、**A を採用した**（利用者の選択）。

- **A. 3つとも vLLM（採用）**: 生成・埋め込み・画像説明を別々の vLLM プロセスで動かす。
  vLLM は1プロセス1モデルのため。Ollama を GB10 から無くせる。代わりにメモリ配分の
  調整と、3プロセスの起動・監視が要る。
- **B. 生成だけ vLLM、埋め込みと画像説明は Ollama**: 変更が小さく既存 DB も使えるが、
  GB10 に Ollama が残る。
- **C. 埋め込みをアプリ内（ONNX）で動かす**: サーバーが1つ減るが、取り込みが CPU を食い、
  依存が増える。

Claude Code からの接続で2つを比べ、**直結を採用した**。

- **vLLM へ直結（採用）**: vLLM は Anthropic Messages API（`/v1/messages`）を自分で話す
  （vLLM PR #22627、2025-10 マージ）。変換プロキシが要らない。
- LiteLLM などのゲートウェイを挟む: Claude と混ぜるときに要るが、(a) では不要。

モデル名に `claude-opus` などの別名を付ける案（`--served-model-name` の複数指定）は
採らない（設計者の判断）。Claude Code はモデル名から Claude 向けの機能を有効にする
可能性があり、vLLM が受け付けない指定を送られうる。クライアント側の
`ANTHROPIC_DEFAULT_*_MODEL` で枠に割り当てれば、別名なしでドロップダウンに出る。

## 全体構成（GB10 1台）

```text
クライアントPC
 ├ ブラウザー ──────── HTTPS 443 /         ─┐
 └ VS Code + Claude Code                    │
     ├ モデル ──────── HTTPS 443 /llm/     ─┤   外に開けるのは 443 だけ
     └ MCP ─────────── HTTPS 443 /mcp      ─┤
                                            ▼
GB10  Nginx（TLS・認証）
 ├ rag_web            127.0.0.1:8502  （画面。段階③の設計どおり）
 ├ rag_mcp_server     127.0.0.1:8080  （社内資料の検索。HTTP）
 ├ vLLM 生成          127.0.0.1:8001  gpt-oss-120b（/v1/chat/completions と /v1/messages）
 ├ vLLM 埋め込み      127.0.0.1:8002  （選定結果。本命 Qwen3-Embedding-4B）
 ├ vLLM 画像説明      127.0.0.1:8003  Qwen3-VL-30B-A3B-Instruct（FP8）
 └ SQLite             vector_store.sqlite3 / docs_store.sqlite3
```

- ポート番号は例で、環境変数で変える。vLLM と MCP は 127.0.0.1 にだけ bind する。
- vLLM は Docker で動かす（利用者の想定）。GB10（Blackwell、`aarch64`）で動く版の
  イメージ（NVIDIA が配る vLLM コンテナなど）を**版を固定して**使う。どの版かは実装時に
  公式の情報で確かめる（この設計では決めない）。
- Claude Code のモデル経路（`/llm/`）は、Nginx が `/v1/messages` を生成の vLLM へ
  転送する。認証は vLLM の `--api-key`（`Authorization: Bearer`）で行い、Claude Code の
  `ANTHROPIC_AUTH_TOKEN` と一致させる。Nginx 側でも Basic 認証などを重ねるかは実装時に
  決めてよい（Claude Code は Basic 認証のヘッダーを送れないため、重ねるならトークン方式）。
- MCP（`/mcp`）は今の `scripts/rag_mcp_server.py` を HTTP で常駐させ、Nginx の後ろに置く。
  今は認証が無い（`docs/mcp-server-network.md`）。Nginx でトークン（`Authorization`
  ヘッダー）を確かめ、Claude Code の `claude mcp add --transport http ... --header` で渡す。

## メモリ配分（見込み。実測で決める）

| プロセス | 重みの目安 | 備考 |
|---|---|---|
| gpt-oss-120b（MXFP4） | 約 61GB | 残りは KV キャッシュ。同時利用と Claude Code の長い文脈に効く |
| Qwen3-VL-30B-A3B（FP8） | 約 31GB | 使うのは取り込み・アップロード・Cowork の添付のときだけ |
| Qwen3-Embedding-4B | 約 8GB | 8B を選べば約 16GB |

- 各 vLLM の `--gpu-memory-utilization` と `--max-model-len` を決めて起動する。
  統合メモリのため OS・アプリ・ページキャッシュとも取り合う。値は GB10 で3つを起動し、
  `nvidia-smi` / `free` と、同時に質問したときの速度で確かめて決める手順にする。
- Claude Code は1回の要求が大きい（指示文だけで数万トークン）。生成の `--max-model-len` は
  Claude Code が動く長さ（目安 64K 以上）にし、その分の KV キャッシュを先に確保する。
- 足りなければ、①埋め込みを 4B に抑える、②画像説明を常駐させず取り込み時だけ起動する、
  ③画像説明を Qwen3-VL-8B に戻す、の順で削る。

## アプリの変更

### 接続先と環境変数

`OLLAMA_HOST` / `OLLAMA_API_KEY` / `OLLAMA_VLM_MODEL` をやめ、役割ごとに分ける。

| 変数 | 既定値 | 意味 |
|---|---|---|
| `VLLM_CHAT_URL` | `http://127.0.0.1:8001` | 生成の vLLM（`/v1` の手前まで） |
| `VLLM_EMBED_URL` | `http://127.0.0.1:8002` | 埋め込みの vLLM |
| `VLLM_VLM_URL` | `http://127.0.0.1:8003` | 画像説明の vLLM |
| `VLLM_CHAT_MODEL` | `gpt-oss-120b` | `--served-model-name` と一致させる |
| `VLLM_EMBED_MODEL` | 選定結果 | 同上 |
| `VLLM_VLM_MODEL` | `qwen3-vl-30b-a3b` | 同上 |
| `VLLM_API_KEY` | （空） | 設定されていれば `Authorization: Bearer` で送る |

- 開発機から Colab の vLLM（ngrok + `X-API-Key` の認証プロキシ）へ繋ぐ今の使い方は、
  認証ヘッダーの形が違う。どちらに揃えるかは「利用者に確認すること」を参照。
- 古い `OLLAMA_*` が `.env` に残っていたら、黙って無視せず起動時に「VLLM_* へ移行して
  ください」と止める（設計者の判断。設定したつもりの値が効かない事故を防ぐ）。
- `rag_web/app.py` の `CHAT_MODEL` 定数は `VLLM_CHAT_MODEL` から読む。
- 読み込み時点で環境変数を読む今の作り（`ingest/embedder.py` がインポート時に読む）は
  変えてよい。`.env` の読み込み順の注意（`rag_web/__main__.py`）が要らなくなるなら消す。

### 生成（`ingest/chat.py`）

- `/v1/chat/completions` を使う。ストリームは SSE（`data: ...` 行、`[DONE]` で終わり）。
- `num_ctx` は要求ごとに指定できない（サーバーの `--max-model-len` で決まる）。
  `num_ctx` 引数と `NUM_CTX` / `GENERATION_NUM_CTX` は、出力の上限（`max_tokens`）など
  vLLM にある指定へ置き換えるか、削る。どちらにするかは実装者に任せるが、Cowork の
  長い出力が途中で切れないこと（今 `GENERATION_NUM_CTX` で広げている理由）を保つ。
- `ask_json` の `format: "json"` は `response_format: {"type": "json_object"}`
  （vLLM の構造化出力）に置き換える。
- gpt-oss の思考（reasoning）は本文に混ぜない。vLLM が分けて返す思考の欄は読まない
  （今の「思考だけで文脈を食い潰す」対策の意図を保つ。`ingest/chat.py` 冒頭の経緯は
  vLLM の事情に合わせて書き直す）。
- `keep_alive` など Ollama 固有の指定は消す。
- 再試行（初回 + 3回、指数バックオフ）と、利用者に見せるエラー文の形は保つ。文中の
  「Ollama」は「vLLM」にする。

### 埋め込み（`ingest/embedder.py`）

- `/v1/embeddings` を使う。バッチの大きさは計測して決め直す（今の 8 は Ollama・16GB 機の値）。
- **質問と資料で入力を分ける。** Qwen3-Embedding は質問の先頭に指示文
  （`Instruct: ...\nQuery: `）を付け、資料には付けない方式である。`embed_query` と
  `embed_texts` はすでに分かれているので、指示文はモデルごとの設定として
  `embed_query` 側で付ける。bge-m3 には付けない。
- 次元は `VLLM_EMBED_DIM`（既定は選定結果）で持ち、返ってきた次元が違えば今と同じく
  DB に書く前に止める。Qwen3-Embedding は次元を縮められる（MRL）が、縮めるかどうかも
  選定の計測で決める。
- **DB に埋め込みモデルを記録する。** `meta` 表に `embed_model` と `embed_dim` を書き、
  開くときに設定と食い違えば「この DB は <モデル> で作られています。取り込み直して
  ください」で止める。記録の無い既存 DB は bge-m3（1024次元）製とみなす。
- 接続確認は `/api/tags` から `/v1/models` に変え、設定したモデル名が載っているかまで
  確かめる（今の「モデルを取り違えると次元が変わる」対策を一段前に出す）。

### 画像説明（`ingest/vlm.py`）

- `/v1/chat/completions` に、画像を `image_url`（`data:image/...;base64,...`）として渡す。
- 指示文（`CAPTION_PROMPT`）と「装飾画像」の扱いは変えない。

### 検索の調整値

`ingest/retrieval.py` などの距離のしきい値・候補数、`ingest/synonyms.py` の補い方は
bge-m3 で実測して決めたものである。埋め込みを替えたら `scripts/check_retrieval.py` で
測り直し、値と根拠のコメントを新しい実測で置き換える。リランカーの足切り
（技術ドキュメント側）はリランカーが変わらないので原則そのままだが、候補の顔ぶれが
変わるため同じ計測で確かめる。

## 埋め込みモデルの選定

1. 候補: bge-m3（基準）、Qwen3-Embedding-4B（本命）、Qwen3-Embedding-8B。日本語専用の
   モデル（Ruri v3 など）は、vLLM で動くことを確かめられた場合だけ加える。
2. 各候補を vLLM で立て、同じ資料を別々の DB へ取り込み、`scripts/check_retrieval.py` の
   質問（社内資料・技術ドキュメントの両方）で上位の当たり方を比べる。
3. 当たり方・取り込みの速さ・メモリで決め、結果（数値と日付）を設計書と README に残す。
4. 計測の場所は GB10（3プロセスの配分の確かめも兼ねる）。GB10 が使えるまでは Colab の L4
   で 4B まで測ってよい。

## Claude Code から使う（クライアント側）

`%USERPROFILE%\.claude\settings.json` の `env` に置く（値は例）。

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://<GB10のホスト名>/llm",
    "ANTHROPIC_AUTH_TOKEN": "<vLLM の --api-key と同じ値>",
    "ANTHROPIC_MODEL": "gpt-oss-120b",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "gpt-oss-120b",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-oss-120b",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-oss-120b",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1"
  }
}
```

- `ANTHROPIC_API_KEY` は設定しない（`ANTHROPIC_AUTH_TOKEN` より優先されるため）。
- MCP: `claude mcp add --transport http --scope user local_docs https://<GB10>/mcp
  --header "Authorization: Bearer <MCP のトークン>"`。
- 知っておくこと（手順書の冒頭に書く）: Anthropic のサポート対象外の構成である。
  gpt-oss-120b は Claude Code の道具呼び出しで失敗した報告がある。RAG の利用者と同じ
  120b を共有するため、混んでいると互いに遅くなる。
- VS Code 拡張が上の環境変数を CLI と同じく読むかは未確認。手順書の確認項目にする。
- 既存の `docs/claude-code-gb10.md`（LiteLLM + Ollama の旧手順）と `docs/qwen/` は、
  この構成の手順に置き換えるか「旧手順」と明記する。

## 文書

- `AGENTS.md`「外部ドキュメントの参照」の外部経路の説明: `OLLAMA_HOST` の段落を
  `VLLM_*` の3経路に書き換える。GB10 の 127.0.0.1 なら外へ出ないこと、開発機から
  Colab や GB10 を指すと質問・資料の本文がその経路に乗ることを、今と同じ粒度で書く。
- GB10 への配置手順（段階③の Task 4 の Step 1 から移す）: Docker での vLLM 3つの起動
  （固定した版・`--served-model-name`・`--api-key`・メモリの割合・最大長）、systemd、
  Nginx（`/`・`/llm/`・`/mcp`）、モデルの事前取得、メモリ配分の確かめ方。
- README・`.env.example`・`docs/依存関係一覧.md`・`docs/処理箇所マップ.md`・
  `docs/mcp-server.md`・`docs/mcp-server-network.md` の Ollama の記述。
- 実測していないもの（GB10 の実機）は「実測はしていない」と明記する。

## 段階③との関係

段階③（Streamlit の撤去）の Task 1〜3 は Ollama に依らないので先に進める。段階③の
Task 4 のうち GB10 の配置手順書は、この設計の実装で vLLM 前提で書く（Ollama 前提で
書いてすぐ書き直す無駄を避ける）。README など Streamlit の記述の書き換えは段階③で行う。

## テスト

- 今の `tests/test_chat.py`・`test_embedder.py`・`test_vlm.py` などは Ollama の応答の形を
  偽物で返している。OpenAI 互換の形（SSE のストリーム、`/v1/embeddings` の `data[].embedding`、
  `/v1/models`）に書き換える。
- 新しく押さえる振る舞い: 古い `OLLAMA_*` が残っていたら止まる、DB の埋め込みモデルの
  食い違いで止まる、思考の欄が本文に混ざらない、質問だけに指示文が付く、接続確認で
  モデル名の不在を見つける。
- 実機の vLLM を叩くテストは `integration` 印を付け、通常の実行から外す（今の方針）。

## 実装者に任せること

- `ingest/` 内の HTTP クライアントの共通化の仕方（3つの接続先で同じ再試行・認証を使う）。
- `num_ctx` の置き換え方（上の制約を守る範囲で）。
- Docker の起動を `docker run` で書くか `docker compose` で書くか。
- Nginx の設定の細部、MCP のトークンの保管場所。

## 設計レビュー後に決めたこと（設計者の判断）

- **gpt-oss の起動の指定**: vLLM の gpt-oss 向けの公式の手順（推論の分離、道具呼び出しの
  解析、`--enable-auto-tool-choice`）に従う。指定名は固定した版の公式文書で確かめる。
  Claude Code の道具呼び出しは、これが無いと動かない。
- **JSON の出力**: `response_format: json_object` を本命にし、実装の最初に Colab の vLLM
  （gpt-oss-20b）で条件抽出と翻訳が通るかを確かめる。通らなければ、今と同じ「JSON を
  解釈できなければ再試行」の仕組みに頼る（構造化出力の指定を外しても壊れない作りにする）。
- **出力の上限**: 思考の分も出力の上限（`max_tokens`）を食う。Cowork は今
  `GENERATION_NUM_CTX` で広げている分を、`max_tokens` を指定しない（サーバーの最大長に
  任せる）か十分大きくして保つ。どちらにしたかと理由をコードに書く。
- **既存の DB**: GB10 は空の DB から始める。開発機の DB（bge-m3 製）は記録が無いので
  bge-m3・1024次元とみなし、埋め込みを替えたら取り込み直す。食い違いは**開いた時点**で
  止める（読むだけの MCP サーバーも止める。質問の埋め込みが別のモデルになり、検索が
  黙って壊れるため）。記録は最初の書き込みで `meta` に入れる。
- **画像説明が止まっているとき**: 今の振る舞い（画像説明なしで取り込み、理由を出す。
  `tests/test_rag_chat_app.py` の `test_ingesting_without_a_vlm_still_ingests_and_says_why`）を
  保つ。常駐を既定とし、取り込み時だけ起動する運用はメモリが足りないときの手段として
  手順書に書くだけにする。
- **Nginx と証明書**: GB10 に新しく入れる。証明書は既存の手順（内部CA）を流用する。
  Claude Code は Node.js で動くため、内部CAのときはクライアントに
  `NODE_EXTRA_CA_CERTS` が要る。手順書に書く。画面（`/`）は Basic 認証、`/llm/` は vLLM の
  `--api-key`、`/mcp` は Nginx でトークンを確かめる。`/llm/` と `/` はバッファリングを切り、
  読み取りのタイムアウトを延ばす。
- **MCP の待ち受け**: `scripts/rag_mcp_server.py --http` の streamable-http を使う
  （すでにある）。Claude Code の `--transport http` はこれに繋がる。
- **成果物の形**: Docker（compose）・systemd・Nginx の設定は、リポジトリの `deploy/gb10/` に
  ファイルとして置き、手順書から参照する。実機では試せないので、手順書に「実測はしていない」
  と書く。イメージの版は公式の公開情報で調べて固定してよい。
- **計測前の既定値**: バッチの大きさなどは今の値を仮に使い、計測の手順を残す。

## 開発機での開発と確認（利用者の選択 2026-11-22）

RAG 用の GB10 は**まだ使えない**。開発機（Windows、16GB）では次のように進める。

- 単体テストは今までどおり偽物のサーバーで行う（実機は要らない）。
- 実機での確認は Colab の vLLM で行う。生成は `gpt-oss-20b`（今の
  `colab/run_vllm_server.ipynb`）。埋め込み・画像説明は、同じノートブックで起動する
  モデルを選べるようにして、1つずつ立てて確かめる（L4 の 24GB に3つは載らない）。
  埋め込みの選定（上の「埋め込みモデルの選定」）も GB10 が使えるまでは Colab で 4B まで測る。
- ノートブックの認証プロキシは `X-API-Key` をやめ、`Authorization: Bearer` を確かめる形に
  変える（アプリの `VLLM_API_KEY` と同じ形に揃える）。ノートブックを編集する前に、
  利用者に VS Code でそのタブを閉じてもらう。
- Colab は1つのURLで1つのモデルしか出せないため、確認する役割の `VLLM_*_URL` だけを
  ngrok のURLへ向ける。開発機に Ollama は残さない。
- MCP（Claude Code から社内資料を引く経路）の HTTP・Nginx・トークンの設定は、GB10 が
  使えるまで実機で確かめなくてよい（利用者 2026-11-22）。コードの変更（埋め込みの切り替えに
  伴うもの）とテストまでを行い、接続の確認は GB10 の受け入れ確認に回す。
- GB10 が使えるようになったら、開発機から LAN 越しに GB10 の vLLM を使う。配置手順と
  `deploy/gb10/` の設定は、その時点で実機で確かめて「実測はしていない」の注記を外す。
