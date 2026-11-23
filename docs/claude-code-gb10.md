# VS Code の Claude Code を GB10 の gpt-oss-120b と社内資料へ向ける

社内のクライアントPC（Windows）の VS Code で Claude Code を使い、推論は社内LANの GB10 の
vLLM に置いた `gpt-oss-120b` に行わせる。社内資料の検索（MCP の `search_documents`）も
同じ GB10 から HTTP で引く。クラウドのモデルは使わない。

GB10 側の配置（vLLM・Nginx・MCP）は [server-deployment.md](server-deployment.md) にある。
この文書は、その配置が済んだあとのクライアントの設定を扱う。

> **実測はしていない。** GB10 はまだ使えず、この手順を通しで実行した記録は無い。Colab の
> vLLM（gpt-oss-20b）で、`--enable-auto-tool-choice --tool-call-parser openai` の指定で
> 起動することまでは確かめた（2026-11-23）が、Claude Code から繋いではいない。各段階に
> 「何が出れば成功か」を書いたので、確かめながら進めること。

## 知っておくこと

1. **Anthropic のサポート対象外の構成である。** Claude Code を Anthropic 以外のモデルへ向ける
   使い方は公式に想定されておらず、動かなくなっても窓口は無い。Claude Code の更新で壊れる
   可能性がある。
2. **gpt-oss-120b は Claude Code の道具呼び出しで失敗した報告がある。** Claude Code は
   Claude 前提で作られており、DGX Spark 上でエージェント用途を比べた外部のベンチマークでは、
   gpt-oss-120b が道具呼び出しで不正な JSON を出して失敗したという報告がある。ファイルを読まずに
   一般論で答える、道具の引数が壊れる、といった形で現れる。設定では直らない。
3. **RAG と 120b を共有する。** 画面（rag_web）の回答生成も同じ vLLM の `gpt-oss-120b` を使う。
   混んでいると互いに遅くなる。Claude Code は1回の要求が大きい（指示文だけで数万トークン）。

## 構成

```text
Windows（VS Code + Claude Code 拡張）
 ├ モデル ─ HTTPS 443 /llm/ ─▶ Nginx ─▶ 生成の vLLM（127.0.0.1:8001）  /v1/messages
 └ MCP    ─ HTTPS 443 /mcp  ─▶ Nginx ─▶ rag_mcp_server（127.0.0.1:8080）
```

vLLM は Anthropic Messages API（`/v1/messages`）を自分で話すので、変換プロキシ（LiteLLM など）は
挟まない。認証は、モデルが vLLM の `--api-key`（`Authorization: Bearer`）、MCP が Nginx の
トークン確認である。Claude Code は Basic 認証のヘッダーを送れないため、`/llm/` には
Basic 認証を重ねない。

## 1. GB10 側で確かめる

Claude Code を入れる前に、サーバー側だけで確かめる。ここが通らなければ、クライアントを
いくら設定しても動かない。

```bash
KEY=<vLLM の --api-key と同じ値>
curl -sk https://<GB10>/llm/v1/messages \
  -H "Authorization: Bearer $KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"gpt-oss-120b","max_tokens":256,"messages":[{"role":"user","content":"12*17"}]}'
```

Anthropic 形式の応答（`"content":[{"type":"text"...` の形）が返ること。思考は別のブロックで返りうる。
`max_tokens` が小さいと思考だけで使い切って本文が空になるので、`256` で空なら増やす。

道具呼び出しも試す。Claude Code の動作はここに懸かっている。

```bash
curl -sk https://<GB10>/llm/v1/messages \
  -H "Authorization: Bearer $KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"gpt-oss-120b","max_tokens":1024,"tools":[{"name":"read_file","description":"Read a file from the workspace.","input_schema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}],"messages":[{"role":"user","content":"README.md の中身を見てほしい"}]}'
```

`"type":"tool_use"` を含むブロックが返れば道具が使える。返らない、あるいは `input` が壊れた
JSON なら、そこがこの構成の限界である（上の「知っておくこと」の2）。

## 2. クライアント: Claude Code を入れる

この経路に Python は要らない。要るのは Node.js / npm と VS Code である。

```powershell
npm install -g @anthropic-ai/claude-code
claude --version
```

VS Code の拡張機能の Claude Code も入れる。**この時点ではまだ Anthropic にログインしない。**
次の設定を入れてから起動する。

## 3. クライアント: 接続先を設定する

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

| キー | なぜ要るか |
|---|---|
| `ANTHROPIC_BASE_URL` | 末尾に `/v1` を付けない。Claude Code が `/v1/messages` を足す。`/llm` が Nginx の経路 |
| `ANTHROPIC_AUTH_TOKEN` | `Authorization: Bearer <値>` で送られる。vLLM の `--api-key`（`/etc/rag/vllm.env` の `VLLM_API_KEY`）と一致させる |
| `ANTHROPIC_MODEL` | 既定のモデル。`--served-model-name` と一致させる |
| `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` | `/model` の枠ごとの行き先。`haiku` は裏の処理（要約など）に使われる。潰さないと存在しないモデル名が飛ぶ |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` / `DISABLE_TELEMETRY` | 本筋以外の外部通信を止める。社内で閉じる構成では付ける |

- **`ANTHROPIC_API_KEY` は設定しない。** `ANTHROPIC_AUTH_TOKEN` より優先され、どちらの認証が
  効いているか分からなくなる。
- モデル名に `claude-opus` などの別名は付けない（`--served-model-name` は `gpt-oss-120b` の1つ）。
  上の `ANTHROPIC_DEFAULT_*_MODEL` で枠に割り当てれば、別名なしでドロップダウンに出る。
- JSON は厳密である。`//` のコメントも末尾のカンマも構文エラーになり、起動時に
  Settings Error が出る。
- プロジェクトごとに分けるなら `.claude/settings.local.json` に書く。**トークンを書くので、
  共有される `.claude/settings.json` には書かないこと。**
- 設定したら VS Code を完全に再起動する（起動済みのウィンドウには反映されない）。

### 証明書が内部CAのとき

Nginx の証明書を社内の認証局（内部CA）で発行している場合、Claude Code は Node.js で動くため、
Windows の証明書ストアではなく、CA の証明書ファイルを環境変数で渡す必要がある。

```json
"NODE_EXTRA_CA_CERTS": "C:\\certs\\internal-ca.pem"
```

これも `settings.json` の `env` に足してよい。証明書の検証エラー
（`unable to verify the first certificate`、`UNABLE_TO_GET_ISSUER_CERT_LOCALLY` など）が出るなら
これが無い。`NODE_TLS_REJECT_UNAUTHORIZED=0` で検証を切ることはしない。

## 4. クライアント: 社内資料の MCP を登録する

```powershell
claude mcp add --transport http --scope user local_docs https://<GB10のホスト名>/mcp `
  --header "Authorization: Bearer <MCP のトークン>"
claude mcp list        # local_docs が Connected であること
```

`<MCP のトークン>` は Nginx の `/etc/nginx/rag/mcp-token.conf` に書いた値で、vLLM のキーとは
別のものである。末尾の `/mcp` を省かないこと（省くと 404）。**この登録のコマンドや
`~/.claude.json` にトークンが残る**ので、共有しないこと。

`search_documents` を呼ばせる指示は、`%USERPROFILE%\.claude\CLAUDE.md` に書く（接続先だけでは
どの質問で検索するかは決まらない）。書き方は [claude-code-vscode.md](claude-code-vscode.md)
と [mcp-server-network.md](mcp-server-network.md) にある。接続できているのに
`search_documents` が呼ばれないときは [mcp-tool-not-called.md](mcp-tool-not-called.md)。

## 5. 動かして確かめる

VS Code で Claude Code を開き、次を聞く。

```text
このリポジトリの README.md を読んで、何をするプロジェクトか説明して
```

- **応答が返ること** — 返らなければ1節の curl に戻る。差は認証かモデル名である。
- **ファイルを実際に読みに行くこと** — 読まずに一般論を答えるなら、道具呼び出しが効いていない。
- **社内資料の質問で `search_documents` が呼ばれ、出典つきで返ること。**

### 確認項目（未確認のもの）

- [ ] **VS Code 拡張が `settings.json` の `env` を、CLI と同じように読むか。** 設計時点で
      未確認。読まない場合は、Windows のユーザー環境変数に同じ値を置いて VS Code を完全に
      再起動する（[claude-code-vscode.md](claude-code-vscode.md) に拡張まわりの注意がある）。
- [ ] 道具呼び出しが、ファイル読み・編集・コマンド実行で通る
- [ ] 長い文脈（数万トークン）で、`--max-model-len 65536` の範囲で動く
- [ ] RAG の画面を同時に使っても、実用的な速度で返る

## MCP tool search について

Claude Code は `ANTHROPIC_BASE_URL` が Anthropic 以外を指しているとき、MCP の tool search
（道具が多いときに検索で絞る仕組み）を既定で切る。`local_docs` のような普通の MCP は
そのまま使える。ただし道具の定義が増えるほど、道具呼び出しの成否はモデルの出来に懸かる。

## うまくいかないとき

| 症状 | 原因 | 対処 |
|---|---|---|
| 401 / 403 | `ANTHROPIC_AUTH_TOKEN` と `--api-key` の不一致 | 見比べる。`Bearer ` は自動で付くので値に含めない |
| 404 `/v1/v1/messages` | `ANTHROPIC_BASE_URL` に `/v1` を付けた | `https://<GB10>/llm` までにする |
| モデルが見つからない | `ANTHROPIC_MODEL` と `--served-model-name` の不一致 | `gpt-oss-120b` に合わせる |
| 証明書のエラー | 内部CAをクライアントが知らない | `NODE_EXTRA_CA_CERTS` を設定する |
| 普段は動くが、たまに落ちる | `haiku` の行き先が未設定 | `ANTHROPIC_DEFAULT_HAIKU_MODEL` を設定する |
| ファイルを読まずに答える | 道具呼び出しが効いていない | 1節の2つ目の curl で切り分ける。設定では直らない |
| 応答が途中で切れる／空 | 思考で出力の上限を使い切った | 出力の上限や `--max-model-len` を見直す |
| 設定が効かない | VS Code を再起動していない、JSON の構文エラー、拡張が env を読まない | 完全に再起動。Settings Error を見る。確認項目の1つ目 |
| Anthropic へのログインを求められる | `settings.json` の場所か綴りが違う | `%USERPROFILE%\.claude\settings.json` であること |

## 旧手順（LiteLLM + Ollama）

以前は、Ollama の `gpt-oss:120b` の手前に LiteLLM を置いて Anthropic 形式を変換していた。
GB10 は vLLM に替わり、vLLM が `/v1/messages` を直接話すため、この手順は使わない。
当時の手順は Git の履歴（このファイルの2026-11-23より前の版）にある。Ollama へ直接繋ぐ
手順（Qwen3-Coder-Next）は [qwen/claude-code-gb10-qwen3-coder-next.md](qwen/claude-code-gb10-qwen3-coder-next.md)
にあるが、これも旧手順で、GB10 に Ollama を置かない現在の構成では使えない。

## 関連

- [GB10 への配置手順](server-deployment.md)
- [vLLM バックエンド切り替えの設計書](superpowers/specs/2026-11-22-vllm-backend-design.md)
- [Claude Code 環境変数リファレンス](https://code.claude.com/docs/en/env-vars)
