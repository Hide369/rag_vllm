# Windows の検索サーバ（Ollama 版）から RAG 用 GB10（vLLM 版）へ移行する

社内資料の検索（MCP）と RAG の画面を、Windows の検索サーバ（Ollama 版 `local-llm-rag`）から
RAG 専用の GB10（本リポジトリ、vLLM 版）へ移す手順である。DB は Windows で使っているものを
そのまま持ち込む。移行に合わせて、Claude Code のライブラリの調べものを context7 から
`search_tech_docs` へ切り替える。

対象のクライアントは VS Code の Claude Code だけである。

## GB10 は3台になる

| GB10 | 動かすもの | 使う人 | 本書で |
| --- | --- | --- | --- |
| GB10-A（既存） | gpt-oss-120b | Claude Code（LiteLLM 経由） | **触らない** |
| GB10-B（既存） | Qwen3-Coder-Next | Claude Code（LiteLLM 経由） | **触らない** |
| RAG 用 GB10（新設。本書では `<RAG用GB10>`） | vLLM 3つ（生成 gpt-oss-120b・埋め込み bge-m3・画像説明）、`rag-web`、`rag-mcp`、Nginx | RAG の画面、Claude Code の MCP | 立てて、Windows から移す |

**Claude Code の推論（モデル）は移さない。** 今までどおり LiteLLM から GB10-A / GB10-B を使う。
クライアントで変えるのは MCP の接続先と、それに関わる設定（`permissions`・`CLAUDE.md`・
context7）だけである。RAG 用 GB10 の gpt-oss-120b は、RAG の画面の回答生成と文書生成のためにある。

```text
クライアントPC（VS Code + Claude Code）
 ├ 推論 ─ http://<LiteLLM>:4000 ─▶ GB10-A gpt-oss-120b / GB10-B Qwen3-Coder-Next   （変えない）
 └ MCP  ─ https://<RAG用GB10>/mcp ─▶ Nginx ─▶ rag-mcp（search_documents / search_tech_docs）
                                               └ 埋め込み: 同じ GB10 の vLLM bge-m3
ブラウザー ─ https://<RAG用GB10>/ ─▶ Nginx ─▶ rag-web ─▶ 同じ GB10 の vLLM gpt-oss-120b
```

> **実測はしていない。** GB10 の実機はまだ使えず、この手順を通しで実行した記録は無い。
> 各段の「確かめる」を実機で通し、結果が違えば手順を直すこと。通したら、この注記を外して
> 日付と結果を残す。

## 移行前と移行後

| | 移行前（Windows） | 移行後（RAG 用 GB10） |
| --- | --- | --- |
| 置き場所 | `C:\App\local-llm-rag`（Ollama 版のリポジトリ） | `/opt/rag`（本リポジトリ） |
| MCP | NSSM の `local-docs-mcp`、`http://<検索サーバIP>:11434/mcp`、認証なし | systemd の `rag-mcp`、`https://<RAG用GB10>/mcp`、Bearer トークン |
| MCP のツール | `search_documents` | `search_documents` と `search_tech_docs` |
| RAG の画面 | NSSM の `local-llm-rag`（Streamlit） | systemd の `rag-web`（`https://<RAG用GB10>/`） |
| 埋め込み | Ollama（`127.0.0.1:11500`、bge-m3） | vLLM（`127.0.0.1:8002`、bge-m3） |
| RAG の回答生成 | Ollama 版の設定のまま | RAG 用 GB10 の vLLM（`127.0.0.1:8001`、gpt-oss-120b） |
| Claude Code の推論 | LiteLLM → GB10-A / GB10-B | **変えない** |
| ライブラリの調べもの | context7（社外のサービス） | `search_tech_docs`（GB10 の技術DB） |
| DB | `vector_store.sqlite3` / `docs_store.sqlite3` | 同じファイルを持ち込む |

移行前の構成の手順書は Ollama 版のリポジトリにある。
- [mcp-server-11434-claude-code.md](https://github.com/Hide369/local-llm-rag/blob/master/docs/mcp/mcp-server-11434-claude-code.md)（MCP の 11434 番とクライアント）
- [ollama-port-11500.md](https://github.com/Hide369/local-llm-rag/blob/master/docs/mcp/ollama-port-11500.md)（Ollama の 11500 番）

## 進め方：並行期間を置く

**Windows を動かしたまま GB10 を立て、クライアントを1台ずつ切り替える。** 全員が移って
1〜2週間問題が出なければ、Windows を止める。一斉に切り替えない理由は次のとおりである。

- **GB10 の構成は実機で一度も動かしていない**（[server-deployment.md](../server-deployment.md) の注記）。
  何かあったときに戻れる先を残しておく。
- **戻すのが安い。** クライアントの戻し方は MCP の登録1件と設定ファイルの復元だけで、
  数分で済む（段階6）。
- **モデルの振る舞いがクライアントごとに確かめられる。** ツール呼び出しには揺れがある。
  1台目で `claude-code-gb10.md` 手順5の表を通してから広げる。

並行期間の約束事は1つだけである。

**DB を写した時点から、Windows では取り込みをしない（凍結する）。** 両方で取り込むと、
どちらのDBが正しいか分からなくなる。並行期間に資料を足すなら GB10 でだけ取り込む。
その間、Windows に残ったクライアントは古い資料を引く。並行期間を短くするのはこのためである。

## 段階0：準備

### 0-1. 決めておくこと

| 項目 | 決めること |
| --- | --- |
| RAG 用 GB10 のホスト名と証明書 | クライアントが `https://<RAG用GB10>/` で届く名前。内部CAなら配布方法（[claude-code-gb10.md](../claude-code-gb10.md)「証明書が内部CAのとき」） |
| MCP のトークン | `openssl rand -hex 32` などで作る。利用者へ渡す方法も決める |
| `/llm/` を外へ開くか | Nginx の設定（`deploy/gb10/nginx-rag.conf`）は、RAG 用 GB10 の gpt-oss-120b を `/llm/` で外へ出す。3台構成では Claude Code はここを使わない。開けておく理由が無ければ、`location /llm/` の2つを外す（外さなくても `--api-key` が守る） |
| 並行期間の長さ | 目安は全員の切り替え完了から1〜2週間 |

### 0-2. Windows の今の状態を控える

検索サーバ機の PowerShell で実行する。戻すときと、後始末のときに使う。

```powershell
cd C:\App\local-llm-rag
git log --oneline -1                                    # Ollama 版のどの版か
nssm get local-docs-mcp AppParameters                   # --port 11434 であること
nssm get local-docs-mcp AppEnvironmentExtra
Get-NetFirewallRule -DisplayName "local-docs MCP (TCP 11434)" | Get-NetFirewallAddressFilter
Get-ChildItem vector_store.sqlite3, docs_store.sqlite3 | Select-Object Name, Length, LastWriteTime
Get-ChildItem source -Recurse -File | Measure-Object | Select-Object Count
```

## 段階1：RAG 用 GB10 を立てる（DB はまだ置かない）

[server-deployment.md](../server-deployment.md) の**手順1〜5**を行う（前提・配置と実行ユーザー・
設定ファイル・モデルとリランカーの事前取得・vLLM の起動）。

**手順6（`systemctl enable --now rag-web rag-mcp`）はまだ行わない。** DB を置く前に
`rag-mcp` が起きて誰かが検索すると、空のDBが作られて抱えられる（`server-deployment.md`
「技術ドキュメントDBを置く」）。DB を置いてから起動する（段階3）。

確かめる：`server-deployment.md` 手順5の「確かめる」が通る。とくに埋め込みの vLLM
（`127.0.0.1:8002`）が `bge-m3` を返すこと。段階2の互換性の確認がこれを使う。

## 段階2：Windows の DB と資料を RAG 用 GB10 へ写す

### 2-1. Windows 側を止めて写す

DB を開いているプロセスを止めてから写す。開いたままのファイルを写すと壊れうる。
止める時間は写す数分だけで、終わればすぐ戻す。

```powershell
# 管理者の PowerShell で
nssm stop local-docs-mcp
nssm stop local-llm-rag        # Streamlit の画面。登録していなければ不要
cd C:\App\local-llm-rag
Get-ChildItem *.sqlite3-journal   # 何も出ないこと（出たら書き込み途中。サービスの停止を確かめる）

# 写すものを1か所にまとめる
New-Item -ItemType Directory -Force C:\App\migration | Out-Null
Copy-Item vector_store.sqlite3, docs_store.sqlite3 C:\App\migration\
tar -czf C:\App\migration\source.tgz source
if (Test-Path templates) { tar -czf C:\App\migration\templates.tgz templates }
Get-FileHash C:\App\migration\* -Algorithm SHA256 | Format-Table Hash, Path

nssm start local-llm-rag
nssm start local-docs-mcp
```

**この時点から Windows では取り込みをしない**（並行期間の約束事）。

写すものと理由：

| もの | 理由 |
| --- | --- |
| `vector_store.sqlite3` | 社内資料のDB |
| `docs_store.sqlite3` | 技術ドキュメントのDB（`search_tech_docs` が引く） |
| `source/` | **必ず写す。** 取り込み（`ingest_source`、画面の「差分を取り込む」も同じ処理）は、`source/` に無い資料をDBから消す。`source/` が空のまま GB10 で一度でも取り込みを走らせると、持ち込んだ社内資料がDBから全部消える（画面からアップロードした資料は除く）。`source/` そのものが無ければ CLI は止まるが、空のフォルダがあると止まらない |
| `templates/` | 画面の文書生成で利用者が登録した雛形。無ければ写さなくてよい |

`docs_source/` は git で追跡済みなので写さない（GB10 の複製に入っている）。

### 2-2. RAG 用 GB10 へ運んで置く

Windows の PowerShell から、フォルダごと送る（Windows 10 以降は `scp` が入っている）。
`*` で個別に指定しないのは、Windows の `scp` がワイルドカードを展開しないことがあるため。

```powershell
scp -r C:\App\migration <RAG用GB10の管理ユーザー>@<RAG用GB10>:/tmp/rag-migration
```

GB10 側に `/tmp/rag-migration` が無ければ、その名前で作られる（既にあると、その下に
`migration/` ができるので、先に消しておく）。

GB10 で置く。

```bash
cd /tmp/rag-migration
sha256sum *                       # 2-1 の Get-FileHash と一致すること（大文字小文字は無視してよい）
sudo install -o rag -g rag -m 640 vector_store.sqlite3 docs_store.sqlite3 /opt/rag/
sudo -u rag tar -xzf source.tgz -C /opt/rag
[ -f templates.tgz ] && sudo -u rag tar -xzf templates.tgz -C /opt/rag
ls -l /opt/rag/*.sqlite3 && sudo -u rag find /opt/rag/source -type f | wc -l   # 0-2 の件数と一致すること
```

`tar` の展開で `source/` 以下のファイル名（日本語）が化けていないことも見る。化けていれば、
Windows の `tar` を使わず、`source` フォルダを zip にして GB10 の `unzip -O cp932` で展開する。

### 2-3. 埋め込みの一致を確かめる

Ollama 版で作ったベクトルは、同じ bge-m3 でも vLLM のものとわずかにずれうる。ずれても
例外は出ず、検索の距離だけが静かに狂う。[server-deployment.md](../server-deployment.md)
「既存の DB を持ち込むとき」の2行を実行する。

```bash
cd /opt/rag
sudo -u rag bash -c 'set -a; . /etc/rag/rag.env; set +a; ./rag_env/bin/python -m scripts.check_embedding_compat --db vector_store.sqlite3 --limit 300'
sudo -u rag bash -c 'set -a; . /etc/rag/rag.env; set +a; ./rag_env/bin/python -m scripts.check_embedding_compat --db docs_store.sqlite3 --limit 300'
```

| 終了コード | 意味 | 次にすること |
| --- | --- | --- |
| 0 | 合格（最小のコサイン類似度 0.99 以上） | 段階3へ |
| 1 | ずれが大きい | そのDBを GB10 で取り込み直す。社内資料は `scripts.ingest_source`、技術DBは `server-deployment.md`「技術ドキュメントDBを置く」の取り込みコマンド。`source/` が置けていることを先に確かめる |
| 2 | 測れなかった | 埋め込みの vLLM に届いているか（段階1の確かめ） |

参考：開発機の Colab（L4）では、両方のDBとも最小 0.9997 以上で合格した（2026-11-23）。
GB10 では測っていない。

## 段階3：RAG 用 GB10 を起動して受け入れ確認

[server-deployment.md](../server-deployment.md) の**手順6・7**を行う（`rag-web` と `rag-mcp` の起動、
Nginx と MCP のトークン）。そのあと同書の「受け入れ確認の項目」を通す。とくに次の3つ。

- 画面（`https://<RAG用GB10>/`）の社内資料のチャットで、Windows の画面と同じ質問に同じ出典が返る
- `sudo systemctl restart rag-mcp` の直後の最初の `search_tech_docs` が、`Reranker 未計測` ではなく
  `Reranker 0.xx` で返る（リランカーがオフラインで読めている）
- MCP のトークン無しの `https://<RAG用GB10>/mcp` が 401 になる

ここまでは利用者に影響しない。クライアントはまだ Windows を向いている。

## 段階4：クライアントを1台切り替える

まず管理者自身のPCなど1台で行い、段階5の確認を通してから他の人へ広げる。

### 4-1. 戻せるように控える

```powershell
$stamp = Get-Date -Format yyyyMMdd
Copy-Item $env:USERPROFILE\.claude\settings.json "$env:USERPROFILE\.claude\settings.json.$stamp"
Copy-Item $env:USERPROFILE\.claude\CLAUDE.md "$env:USERPROFILE\.claude\CLAUDE.md.$stamp"
claude mcp get local_docs        # 今の接続先（http://<検索サーバIP>:11434/mcp）を控える
```

VS Code のユーザー設定（`claudeCode.environmentVariables`）は変えない（推論は LiteLLM のまま）。

### 4-2. MCP の登録を付け替える

名前は `local_docs` のまま変えない。`permissions.allow` の名前がそれを前提にしている。

```powershell
claude mcp remove local_docs --scope user
claude mcp add --transport http --scope user local_docs https://<RAG用GB10>/mcp `
  --header "Authorization: Bearer <MCP のトークン>"
claude mcp list        # local_docs が Connected であること
```

トークンは `~/.claude.json` に残る。共有しないこと。

### 4-3. `settings.json` と `CLAUDE.md` を直す

11434 の手順書で設定済みのクライアントなら、変えるのは次の表だけである。

**[claude-code-gb10.md](../claude-code-gb10.md) 手順3の `settings.json` の例は丸写ししない。**
あの例は RAG 用 GB10 の `/llm/` を推論先にする1台構成のもので、`env` の `ANTHROPIC_*` を
写すと推論が LiteLLM（GB10-A / GB10-B）から外れ、プルダウン（`modelPicker`）の Qwen も
使えなくなる。使うのは同書の `permissions` の部分と、手順4（context7 と `CLAUDE.md`）だけである。

| 場所 | 変えること |
| --- | --- |
| `permissions.allow` | `mcp__local_docs__search_tech_docs` を足す（`search_documents` は既にある） |
| `permissions.deny` | `mcp__context7__*` を足す |
| `env.ENABLE_TOOL_SEARCH` | `"false"` のまま（11434 の手順書で入れてある） |
| context7 | `claude mcp remove context7 --scope user`。プラグインで入れていれば `/plugin` で無効にし、`/mcp` に出た名前で deny を足す（`claude-code-gb10.md` 手順4） |
| `CLAUDE.md` | 「外部ドキュメントの参照」節を、`claude-code-gb10.md` 手順4の `search_tech_docs` の文面に**置き換える**。社内資料の節はそのまま |
| `env.ANTHROPIC_*`・`modelPicker`・VS Code の `claudeCode.environmentVariables` | **変えない** |

VS Code を**完全に終了してから**起動し直す（Reload Window では古い設定が残ることがある）。

## 段階5：切り替えたクライアントで確かめる

[claude-code-gb10.md](../claude-code-gb10.md) の手順5の表（技術ドキュメントの検索）を通す。最低限、次の4つ。
プルダウンの **Qwen3-Coder-Next と gpt-oss-120b の両方で**行う。11434 の確認で通っているのは
Qwen だけで、gpt-oss-120b でツールが呼ばれるかはまだ確かめていない。

| # | 操作 | 正しい結果 |
| --- | --- | --- |
| 1 | `/mcp` | `local_docs` が Connected。ツールが2つ見える |
| 2 | ツール名を言わずに社内資料の質問（Windows で答えられていたもの） | `search_documents` が呼ばれ、Windows のときと同じ出典で答える |
| 3 | 「Streamlit でデータをキャッシュする方法は？」 | `search_tech_docs` が英語の検索語で呼ばれ、出典つきで答える |
| 4 | ツール名を言わずに「Streamlit でCSVを読んで表にするページを書いて」（3回） | 書く途中で自分から `search_tech_docs` を呼ぶ。`context7` は一度も呼ばれない |

通ったら、残りのクライアントも段階4・5を繰り返す。結果は `claude-code-gb10.md` に日付つきで
記録する（どのモデルで、何回試して、何回呼ばれたか）。

## 段階6：問題が出たら戻す

GB10 側で直せない問題が出たら、そのクライアントだけ Windows へ戻す。Windows は並行期間中
動き続けているので、サーバ側の作業は要らない。

```powershell
claude mcp remove local_docs --scope user
claude mcp add --transport http --scope user local_docs http://<検索サーバIP>:11434/mcp
$stamp = "<4-1 で付けた日付>"
Copy-Item "$env:USERPROFILE\.claude\settings.json.$stamp" $env:USERPROFILE\.claude\settings.json
Copy-Item "$env:USERPROFILE\.claude\CLAUDE.md.$stamp" $env:USERPROFILE\.claude\CLAUDE.md
```

context7 を外していれば、使い続ける場合は登録し直す（Ollama 版の
[context7-claude-code.md](https://github.com/Hide369/local-llm-rag/blob/master/docs/context7-claude-code.md)）。
VS Code を完全に再起動する。

戻ったクライアントは、DB を写した時点の資料を引く。並行期間中に GB10 で足した資料は見えない。

## 段階7：Windows を止める

全クライアントが GB10 に移り、1〜2週間問題が出なければ、Windows 側を止める。

```powershell
# 管理者の PowerShell で
nssm stop local-docs-mcp
nssm remove local-docs-mcp confirm
nssm stop local-llm-rag
nssm remove local-llm-rag confirm          # 登録していなければ不要
Remove-NetFirewallRule -DisplayName "local-docs MCP (TCP 11434)"
```

- **Ollama（11500 番）**：検索サーバ機で他に使っていなければ止める（トレイアプリを終了するか、
  サービスにしていれば `nssm stop ollama`）。ユーザー環境変数の `OLLAMA_HOST` も不要になる。
- **DB と `source/` は消さない。** `C:\App\migration\` の写しと元のファイルを、少なくとも
  次の1か月は残す。GB10 のDBが壊れたときの戻り先になる。
- 止めたあと、どのクライアントからも `Test-NetConnection <検索サーバIP> -Port 11434` が
  `False` になることを確かめる（11434 を向いたままのクライアントが残っていないか）。

## うまくいかないとき

| 症状 | 原因と対処 |
| --- | --- |
| `check_embedding_compat` が終了コード1 | ベクトルがずれている。そのDBを GB10 で取り込み直す（段階2-3の表）。`source/` を先に置くこと |
| GB10 の検索が「この DB は … で作られています」 | DB の埋め込みモデルの記録と `VLLM_EMBED_MODEL` が違う。Ollama 版のDBは記録が無く bge-m3 とみなされるので、`rag.env` の `VLLM_EMBED_MODEL=bge-m3` を確かめる |
| GB10 の検索が「DBが空です」 | DB を置く前に `rag-mcp` が起きて空のDBを抱えた。`sudo systemctl restart rag-mcp`。置き直すなら止めてから置く |
| 取り込みのあと社内資料が減った | `source/` が欠けたまま取り込んだ。2-1 の `source.tgz` を展開し直し、取り込み直す |
| 社内資料の出典の名前が化けている | `source/` の展開でファイル名が化けた（2-2 の最後）。化けた名前でDBに入っていれば、展開し直してから取り込み直す |
| クライアントから `local_docs` が Failed | URL 末尾の `/mcp`、トークン、証明書（[claude-code-gb10.md](../claude-code-gb10.md)「うまくいかないとき」） |
| ツールは2つ見えるのに一度も呼ばれない | `ENABLE_TOOL_SEARCH` が `false` か。VS Code を完全に再起動したか |

## 関連

- [GB10 への配置手順](../server-deployment.md)
- [VS Code の Claude Code を GB10 へ向ける](../claude-code-gb10.md)
- [技術ドキュメントDBの移植手順](../技術ドキュメントDBの移植手順.md)
- [技術ドキュメントDBを MCP から検索する設計書](../superpowers/specs/2026-11-24-tech-docs-mcp-tool-design.md)
