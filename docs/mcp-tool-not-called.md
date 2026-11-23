# MCPのツールが呼ばれないときの切り分け

Claude Code から社内資料の MCP サーバへ接続できているのに、`search_documents` が一度も
呼ばれず DB を検索できない、という症状の調べ方である。

クライアントの設定は [claude-code-gb10.md](claude-code-gb10.md)、サーバの構築手順は
[mcp-server-network.md](mcp-server-network.md)（複数PC・HTTP）と
[mcp-server.md](mcp-server.md)（1台・stdio）にある。本書は、そこまで済んだうえで
ツールが呼ばれない場合を扱う。

## 症状

- `claude mcp list` で `local_docs` が Connected になっている
- モデルに「使えるツールを列挙して」と頼むと `mcp__local_docs__search_documents` が挙がる
- しかし実際の質問では一度も呼ばれず、モデルが自分の知識で答える
- ツール名を明示して「これを使え」と頼んでも呼ばれない

## 先に読むこと

**「ツールが列挙できる」ことは「呼べる」ことの証明にならない。** ツールの一覧が
指示文の文章として入っていれば、モデルは名前を復唱できる。しかし要求の `tools` 配列に
入っていなければ呼びようがない。この2つは別の事実である。

**サーバのログでは判別できない。** アクセスログはどの JSON-RPC メソッドが来たかを
区別しない。接続の握手（`initialize` / `tools/list`）も実際の検索（`tools/call`）も
同じ1行に見える。

```text
INFO:  127.0.0.1:61507 - "POST /mcp HTTP/1.1" 200 OK   ← initialize
INFO:  127.0.0.1:61507 - "POST /mcp HTTP/1.1" 202 Accepted
INFO:  127.0.0.1:61507 - "POST /mcp HTTP/1.1" 200 OK   ← tools/list
INFO:  127.0.0.1:61507 - "POST /mcp HTTP/1.1" 200 OK   ← tools/call
```

握手は接続した時点で必ず起きるので、ツールが1度も呼ばれなくても到達記録は残る。

**呼ばれない原因は3か所のどれかにある。** 切り分けはこの順に進める。

1. サーバが正しく広告していない（手順1）
2. モデル（vLLM）がツールを呼べない、または呼んでも vLLM が解釈できない（手順2）
3. Claude Code が要求の `tools` にツールを入れていない（手順3）

## 過去の測定（2026-09-11、Codex + Ollama + gpt-oss:20b）

Codex と Ollama で使っていた頃に、次を調べた。**クライアントもモデルの実行系も
替わったので、サーバ側の結果だけがそのまま使える。** モデル側は手順2で測り直すこと。

| 容疑者 | 当時の判定 | 今の構成（Claude Code + vLLM）で |
| --- | --- | --- |
| MCPサーバの広告と検索 | 無実 | そのまま使える（サーバは同じ） |
| `mcp__` 接頭辞つきの長い名前 | 無実 | 名前は Claude Code でも同じ `mcp__local_docs__search_documents` |
| 日本語の説明文・実物のスキーマ | 無実（14/15回呼んだ） | モデルが120bに替わったので測り直す |
| モデルの function call 能力 | 無実 | 実行系が vLLM に替わったので手順2で測り直す |

当時の測定では、同じ要求でも呼ぶのは14/15回（約93%）で、毎回ではなかった。
**ただし93%と「一度も呼ばれない」は違う症状である。** 揺れを理由にしないこと。

## 手順1: サーバ側を確かめる

クライアントを介さず、HTTP で直接叩く。ここが通ればサーバは無実である。

```powershell
# initialize（セッションIDを取る）
$body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
$r = Invoke-WebRequest -Uri https://<GB10>/mcp -Method Post -Body $body `
  -ContentType "application/json" `
  -Headers @{ Accept = "application/json, text/event-stream"; Authorization = "Bearer <MCP のトークン>" }
$r.Headers["mcp-session-id"]
```

`serverInfo` に `local_docs` が含まれれば正しい。パスは `/mcp` である。`/` に投げると
404 になる。`<MCP のトークン>` は Nginx の値で、vLLM のキーとは別である。

**応答は SSE（`text/event-stream`）で返る。** 1つのイベントが複数の `data:` 行に
分かれることがあるので、先頭行だけを読むと JSON が途中で切れる。行を連結してから
解釈すること。文字コードは UTF-8 である。ライブラリの推測に任せると日本語が化ける。

## 手順2: モデル側を確かめる

Claude Code を介さず、vLLM の `/v1/messages` に `tools` を付けて直接送る。手順は
[claude-code-gb10.md](claude-code-gb10.md) の「1. GB10 側で確かめる」の2つ目の curl
である。`search_documents` で試すなら、`tools` を次に差し替える。

```json
[{"name":"mcp__local_docs__search_documents","description":"社内資料を検索し、根拠となる原文を出典つきで返す","input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}]
```

質問は「社内資料で懲戒解雇について調べて」とする。

| 結果 | 意味 |
| --- | --- |
| `"type":"tool_use"` のブロックが返る | モデルも vLLM も無実。手順3へ |
| 本文の文章にツール名や JSON が書かれている | モデルは呼ぶつもりだが、vLLM がツール呼び出しとして解釈できていない。`--enable-auto-tool-choice` と `--tool-call-parser openai` を確かめる（[deploy/gb10/compose.yaml](../deploy/gb10/compose.yaml) の生成の vLLM） |
| ツールに触れずに答える | モデルが呼ばない。何回か試し、毎回なら手順3の結果と合わせて判断する |

## 手順3: Claude Code が何を送っているかを見る

ここまでで原因が出なければ、送信内容を直接観測する。推測を重ねないこと。

[scripts/proxy_log.py](../scripts/proxy_log.py) を Claude Code と vLLM の間に挟む。
このファイルは単体で動き、プロジェクトの他のモジュールを import しない。
**Claude Code が動いている機械へこの1枚だけコピーすればよい**（Python と requests だけ要る）。

### 手順

1. プロキシを起動する（別ウィンドウ）。`--upstream` には、いまの `ANTHROPIC_BASE_URL` の
   値をそのまま渡す。末尾に `/v1` は付けない。

   ```powershell
   python proxy_log.py --upstream https://<GB10>/llm
   ```

   Nginx の証明書が内部CAのときは、Python に CA を教える（`NODE_EXTRA_CA_CERTS` は
   Node.js 用で、Python には効かない）。

   ```powershell
   $env:REQUESTS_CA_BUNDLE = "C:\certs\internal-ca.pem"
   python proxy_log.py --upstream https://<GB10>/llm
   ```

2. `%USERPROFILE%\.claude\settings.json` の `ANTHROPIC_BASE_URL` をプロキシへ向ける。
   **元の値を控えておく。** `ANTHROPIC_AUTH_TOKEN` はそのままでよい（プロキシが上流へ
   そのまま渡す）。

   ```json
   "ANTHROPIC_BASE_URL": "http://127.0.0.1:11500"
   ```

3. VS Code を完全に再起動し、Claude Code から1回質問する

   ```text
   社内資料で懲戒解雇について調べて
   ```

4. `proxy_log.txt`（プロキシを起動したフォルダにできる）を読む

5. **`ANTHROPIC_BASE_URL` を元に戻し、VS Code を再起動する**

### ログの読み方

Claude Code は1回の質問で複数の要求を送る。本題の要求は、`Read` や `Bash` などの
組み込みのツールが並ぶものである。ツールを持たない要求は、題名づけや要約などの
補助の要求で、ツールが無くて正常である。

ツールが渡されており、モデルが呼んだ場合:

```text
[23:59:10] --- POST /v1/messages?beta=true ---
[23:59:10]   model='gpt-oss-120b'  stream=True  本文=48211バイト
[23:59:10]   tools配列: 18件
[23:59:10]     組み込み: Task, Bash, Glob, Grep, Read, Edit, Write, ...
[23:59:10]     - mcp__local_docs__search_documents ← これ
[23:59:10]     system: 21034文字
[23:59:10]     messages: 1520文字
[23:59:14]   応答 HTTP 200  stop_reason='tool_use'
[23:59:14]     ✅ モデルが呼んだツール: ['mcp__local_docs__search_documents']
```

判定は次のとおりである。

| ログ（本題の要求で） | 意味 | 次にすること |
| --- | --- | --- |
| `★ search_documents が tools に無い` | 組み込みのツールは渡しているが、MCP ツールだけ落ちている | `claude mcp list` で Connected か、登録の `--scope`、ツールの許可設定を見る |
| `(defer_loading)` が付いている | tool search が有効で、ツールの定義が後回しにされている。モデルは先に検索してからでないと呼べない | `ENABLE_TOOL_SEARCH` を設定していないか見る（下記） |
| `★ 本文に search_documents の名前が出ているが tool_use になっていない` | モデルは呼ぶつもりだが、vLLM が解釈できていない | 手順2の表の2行目と同じ |
| ツールは載っているが `❌ モデルはツールを呼ばなかった` | 渡してはいる | 指示（`CLAUDE.md`）が読まれているか、ツール数・`system` の長さを疑う |
| `応答 HTTP 4xx` とエラー | 要求が受け付けられていない | エラーの `type` と `message` を見る。文脈長の超過なら `--max-model-len` |

**本文の全文は記録しない。** 社内資料の断片やソースコード、API キーが混ざりうるためで、
ツール名と件数、入力の長さだけを出す。認証ヘッダも記録しない。エラー応答の
`message` だけは、原因を読むために先頭300文字まで出す。

## MCP tool search について

Claude Code は `ANTHROPIC_BASE_URL` が Anthropic 以外を指しているとき、MCP の tool search を
既定で切る。このとき MCP ツールは定義ごと `tools` に入る。`ENABLE_TOOL_SEARCH` などで
有効にしていると、定義が後回し（`defer_loading`）になり、モデルは先に検索の道具を
使わないと MCP ツールを呼べない。Claude 以外のモデルには難しい手順なので、この構成では
有効にしない。

## ツールを呼ぶ判断はどこにあるか

接続の設定（`claude mcp add`）が教えるのは接続先だけである。**どの質問で検索するか**は
エージェントの判断であり、ツールの説明文（「社内資料を検索し、根拠となる原文を
出典つきで返す」）は呼ぶべき条件を含まない。

その判断を与えるのが `%USERPROFILE%\.claude\CLAUDE.md` の「社内資料の参照」節である
（書き方は [claude-code-vscode.md](claude-code-vscode.md)）。読まれているかは
Claude Code の `/context` で確かめられる。

ただし **ツール名を明示して頼んでも呼ばれないなら、この節の問題ではない。**
その場合は手順3へ進むこと。

## まだ潰せていないもの

2026-11-23 時点で、次は未確認である。GB10 が使えるようになったら確かめる。

- gpt-oss-120b（vLLM）が、Claude Code の多数のツールと長い指示文の中から
  `search_documents` を選ぶか（手順3で確定する）
- vLLM の `--tool-call-parser openai` が、Claude Code の要求で tool_use を正しく返すか

ツール選択は規模と訓練に強く依存する能力であり、設定だけでは詰め切れない可能性は残る
（[claude-code-gb10.md](claude-code-gb10.md) の「知っておくこと」の2）。
