# GB10 への配置手順（vLLM 版）

社内LANの GB10（DGX Spark、DGX OS 7 = Ubuntu 24.04、`aarch64`、統合メモリ 128GB）1台に、
RAG の画面（`rag_web/`）、社内資料の検索（MCP）、vLLM 3つを置く手順である。設計は
[vLLM バックエンド切り替えの設計書](superpowers/specs/2026-11-22-vllm-backend-design.md) にある。

> **実測はしていない。** GB10 の実機はまだ使えず、この手順と `deploy/gb10/` の設定は
> 一度も実機で動かしていない。確かめた範囲は次の2つだけである。
> - Colab（L4 24GB）で、gpt-oss-20b と bge-m3 を同じ指定の形で動かした（2026-11-23。
>   `colab/run_vllm_server.ipynb`）。
> - 単体テストと、開発機の画面を Colab の vLLM に向けた確認。
>
> GB10 でのメモリ配分・gpt-oss-120b の速度・Qwen3-VL-30B-A3B（FP8）の起動・Nginx の
> 動作・NGC のイメージ（`nvcr.io/nvidia/vllm:26.04-py3`）が GB10 で動くことは、いずれも
> 実測していない。各手順の「確かめる」を実機で通し、結果が違えば設定を直すこと。
> 通したら、この注記を外して日付と結果を残す。

## 構成

```text
クライアントPC（ブラウザー / VS Code + Claude Code）
        │ HTTPS 443（外へ開けるのはここだけ）
        ▼
GB10  Nginx（TLS・認証）                      deploy/gb10/nginx-rag.conf
 ├ /       Basic 認証 ─▶ rag_web            127.0.0.1:8502  （systemd: rag-web.service）
 ├ /llm/   vLLM の --api-key ─▶ 生成の vLLM  127.0.0.1:8001  gpt-oss-120b
 └ /mcp    Bearer トークン ─▶ rag_mcp_server 127.0.0.1:8080  （systemd: rag-mcp.service）
                                           │
 vLLM（Docker compose）                     │ rag_web / MCP から（127.0.0.1）
 ├ 生成    127.0.0.1:8001  gpt-oss-120b      deploy/gb10/compose.yaml
 ├ 埋め込み 127.0.0.1:8002  bge-m3
 └ 画像説明 127.0.0.1:8003  Qwen3-VL-30B-A3B-Instruct-FP8
SQLite: vector_store.sqlite3 / docs_store.sqlite3（/opt/rag 直下）
```

- GB10 に Ollama は置かない。
- vLLM と MCP は `127.0.0.1` にだけ bind する。LAN へ出るのは Nginx の 443 だけ。
- 質問・資料の本文は GB10 の外へ出ない。出るのは、リランカーのモデルの有無を確かめる
  huggingface.co への HEAD だけである（手順 4 で事前取得しておけば、オフラインでも動く）。

## 1. 前提を確かめる

```bash
cat /etc/os-release | head -3     # Ubuntu 24.04（DGX OS 7）
uname -m                          # aarch64
nvidia-smi                        # GB10 が見え、ドライバーが載っていること
docker --version && docker compose version
docker run --rm --gpus all nvcr.io/nvidia/vllm:26.04-py3 nvidia-smi   # コンテナから GPU が見えること
```

- `compose.yaml` のイメージは NVIDIA の NGC のコンテナ（`26.04-py3` は vLLM 0.19.0、
  CUDA 13.2.1 ベース）に固定してある。このタグを GB10 で動かしたことはない。NVIDIA の
  フォーラムに、このタグでドライバーの互換性の警告が出た報告がある
  （ドライバー 580 系）。起動して `compatibility mode is UNAVAILABLE` のような
  警告が出たら、
  [NVIDIA vLLM リリースノート](https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/index.html)
  で、そのタグに要るドライバーを確かめる。新しい版へ上げるときも同じ。
- Ubuntu 24.04 の標準の Python は 3.12 だが、このリポジトリは 3.13 を前提にしている。
  3.13 は `uv python install 3.13` などで入れる。

## 2. 配置と実行ユーザー

```bash
sudo useradd --system --home /opt/rag --shell /usr/sbin/nologin rag
sudo mkdir -p /opt/rag /etc/rag /etc/nginx/rag
sudo git clone <このリポジトリのURL> /opt/rag
sudo chown -R rag:rag /opt/rag
sudo -u rag python3.13 -m venv /opt/rag/rag_env
sudo -u rag /opt/rag/rag_env/bin/pip install -r /opt/rag/requirements.txt
```

- `requirements.txt` の各パッケージ（特に `onnxruntime`、`rapidocr`、`pymupdf`）が
  `aarch64` で入るかは確かめていない。入らないときは、`pip install` のエラーの
  パッケージ名で `aarch64` 用の wheel（`manylinux_2_28_aarch64` など）の有無を見る。
  wheel が無ければソースからのビルドになり、コンパイラーと開発用ヘッダーが要る。
- DB（`vector_store.sqlite3`、`docs_store.sqlite3`）、`source/`、`templates/` は
  `/opt/rag` 直下に置く。`rag` ユーザーだけが読み書きできる権限にする
  （`chmod 750 /opt/rag`）。`source/` は Git 管理外である。

## 3. 設定ファイルを置く（秘密はリポジトリの外）

`/etc/rag/vllm.env`（権限 600、所有者 root）。docker compose の `--env-file` に渡す。

```text
VLLM_API_KEY=<十分に長いランダム文字列>
HF_CACHE_DIR=/srv/models/huggingface
```

`/etc/rag/rag.env`（権限 600、所有者 rag）。`rag-web.service` と `rag-mcp.service` が読む。

```text
VLLM_CHAT_URL=http://127.0.0.1:8001
VLLM_EMBED_URL=http://127.0.0.1:8002
VLLM_VLM_URL=http://127.0.0.1:8003
VLLM_CHAT_MODEL=gpt-oss-120b
VLLM_EMBED_MODEL=bge-m3
VLLM_VLM_MODEL=qwen3-vl-30b-a3b
VLLM_API_KEY=<vllm.env と同じ値>

RAG_WEB_ADMIN_PASSWORD_HASH=<README の手順で作ったハッシュ>
RAG_WEB_HOST=127.0.0.1
RAG_WEB_PORT=8502
RAG_WEB_COOKIE_SECURE=true
RAG_WEB_FORWARDED_ALLOW_IPS=127.0.0.1
RAG_WEB_MAX_UPLOAD_MB=50
RAG_WEB_THREADPOOL_SIZE=40
RAG_WEB_MAX_COWORK_JOBS=2
```

- `VLLM_*_URL` は **`/v1` を付けない**（付けると起動時に止まる。プログラム側が `/v1/...` を足す）。
- `VLLM_*_MODEL` は `compose.yaml` の `--served-model-name` と一致させる。
- `RAG_WEB_FORWARDED_ALLOW_IPS` は Nginx が同じ GB10 にあるので `127.0.0.1`。
  `*` は起動時に拒否される。Nginx を Docker で動かす場合は、実際にプロキシが接続してくる
  IP（ブリッジの IP。例 `172.17.0.1`。`docker network inspect bridge` で確かめる）にする。
  そうしないと全利用者がログイン試行制限の同じ枠を共有する。
- 管理者パスワードは9文字以上。ハッシュの作り方は README「Web 画面」の手順に従う。
- `.env` を `/opt/rag` に置いた場合も `python-dotenv` が読むが、`EnvironmentFile`
  のほうが優先して使われる。値の出どころは `/etc/rag/rag.env` の1か所にすること。
- `RAG_WEB_MAX_UPLOAD_MB` を変えたら、Nginx の `client_max_body_size` も計算し直す（手順 7）。

## 4. モデルとリランカーを事前に取得する

`compose.yaml` は `HF_HUB_OFFLINE=1` で動くため、先にモデルを取得しておく。取得だけは
インターネットに出る（GB10 から Hugging Face へ。別のマシンで取得して
`HF_CACHE_DIR` へ複写してもよい）。

```bash
export HF_HOME=/srv/models/huggingface
pip install -U "huggingface_hub[cli]"
hf download openai/gpt-oss-120b                       # 約61GB（MXFP4）
hf download Qwen/Qwen3-VL-30B-A3B-Instruct-FP8        # 約31GB
hf download BAAI/bge-m3                               # 約2GB
```

コンテナ内のキャッシュは `/root/.cache/huggingface`（`compose.yaml` が
`HF_CACHE_DIR` をそこへ載せる）。`HF_HOME` 配下の `hub/` が、`HF_CACHE_DIR` の直下に
見える形にすること。

リランカー（`bge-reranker-v2-m3`、ONNX）は rag_web / MCP が初回の検索で確かめる。
オフラインで使うなら、先に一度、ネットワークのある状態で `search_documents` か
チャットの検索を実行して取得しておく（取得できないと、質問のたびに huggingface.co への
HEAD を繰り返し、失敗する）。

## 5. vLLM を起動する

```bash
cd /opt/rag/deploy/gb10
docker compose --env-file /etc/rag/vllm.env up -d
docker compose --env-file /etc/rag/vllm.env logs -f vllm-chat
```

起動は生成 → 画像説明 → 埋め込みの順（空きメモリの確認が重ならないように、
`depends_on` と healthcheck で並べてある）。初回は CUDA グラフの準備などで10分以上かかる。

### 確かめる

```bash
KEY=$(sudo grep ^VLLM_API_KEY= /etc/rag/vllm.env | cut -d= -f2-)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/v1/models                  # 401（キー無しは拒否）
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8001/v1/models | head -c 300     # gpt-oss-120b が載る
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8002/v1/models | head -c 300     # bge-m3
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8003/v1/models | head -c 300     # qwen3-vl-30b-a3b
```

- 1つ目が 200 なら、`VLLM_API_KEY` の環境変数が `--api-key` として効いていない。
  その場合は `compose.yaml` の各 `command` に `--api-key` を足す必要がある。
- gpt-oss は思考を本文と別の欄（`reasoning`、版により `reasoning_content`）で返す。
  アプリはその欄を読まない。非ストリームで1回叩き、`message.content` に回答が入ることを見る。
- 道具呼び出しは `/v1/messages` に `tools` を付けて叩き、`tool_use` が返ることを見る
  （手順は [claude-code-gb10.md](claude-code-gb10.md)）。

### メモリ配分の確かめ方（実測はしていない）

`compose.yaml` の割合は見込みで、決まった値ではない。128GB の統合メモリを OS・
アプリ・ページキャッシュとも取り合う。

| プロセス | 重みの目安 | `--gpu-memory-utilization`（仮） | `--max-model-len`（仮） |
|---|---|---|---|
| gpt-oss-120b（MXFP4） | 約 61GB | 0.55（約 70GB） | 65536 |
| Qwen3-VL-30B-A3B（FP8） | 約 31GB | 0.28（約 36GB） | 16384 |
| bge-m3 | 約 1GB | 0.04（約 5GB） | 8192 |
| 合計 | | 0.87 | |

確かめる手順。

1. 3つを起動し、`nvidia-smi` と `free -h` で、使用量と空き（OS 用に十数 GB 残るか）を見る。
2. 各 vLLM のログで KV キャッシュの大きさを見る。`ValueError: ... KV cache ... larger than
   the available` で落ちたら、その `--max-model-len` に要るメモリが足りない。割合を上げるか、
   最大長を下げる（Colab の L4 でも、gpt-oss-20b の 0.74 で 32768 に足りず起動に失敗した。
   0.80 に上げて通った。2026-11-23）。
3. 同時に質問する人数を増やしながら（rag_web で数人、Claude Code と同時に）速度を測る。
4. NVIDIA のリリースノートは、統合メモリでは既定の割合が大きすぎて OOM しうるとして、
   割合を下げる（例 0.7）ことを勧めている。ほかが起動できないなら、まず生成の割合を下げる。

足りないときの削り方は、①画像説明を常駐させず、取り込み時だけ起動する
（`docker compose stop vllm-vlm` で止め、取り込みの間だけ `start`）、②画像説明を
Qwen3-VL-8B に戻す、の順。①のとき、画像説明が止まっていても取り込みは動き、画像の説明が
付かない理由が出る。

## 6. rag_web と MCP を起動する

```bash
sudo cp /opt/rag/deploy/gb10/rag-web.service /opt/rag/deploy/gb10/rag-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rag-web rag-mcp
journalctl -u rag-web -f      # ログは journalctl で見る
```

- 起動しないとき、まず `journalctl -u rag-web` を見る。`VLLM_*_URL` が `/v1` で終わっている、
  古い `OLLAMA_*` が残っている、DB の埋め込みモデルの記録が設定と食い違っている、は
  どれも入口で止まり、原因が文面に出る。
- 資料の取り込み（`python -m scripts.ingest_source`）は `rag` ユーザーで、同じ
  `/etc/rag/rag.env` を読み込んで実行する。例:
  `sudo -u rag bash -c 'set -a; . /etc/rag/rag.env; set +a; cd /opt/rag && ./rag_env/bin/python -m scripts.ingest_source'`
- **GB10 は空の DB から始める。** 開発機の DB を持ち込むなら、先に埋め込みの一致を確かめる。
  下の「既存の DB を持ち込むとき」を読む。

### 既存の DB を持ち込むとき

Ollama 版で作った DB は、同じ bge-m3 でも vLLM の bge-m3 とベクトルがわずかにずれうる。
ずれても例外は出ず、検索の距離だけが静かに狂う。**本番の DB を切り替える前に**、
GB10 の埋め込みの vLLM に対して実行する。

```bash
cd /opt/rag
sudo -u rag bash -c 'set -a; . /etc/rag/rag.env; set +a; ./rag_env/bin/python -m scripts.check_embedding_compat --db vector_store.sqlite3 --limit 300'
sudo -u rag bash -c 'set -a; . /etc/rag/rag.env; set +a; ./rag_env/bin/python -m scripts.check_embedding_compat --db docs_store.sqlite3 --limit 300'
```

終了コード 0 が合格（最小のコサイン類似度が 0.99 以上）。開発機の Colab（L4）で測った結果
（2026-11-23）は、`vector_store` が n=300・最小 0.999774・平均 0.999971、`docs_store` が
n=300・最小 0.999820・平均 0.999976 で、どちらも合格だった。GB10 では測っていない。
終了コード 1 は不合格で、その場合は DB を取り込み直す。DB の `meta` 表には埋め込みモデルが記録され、
設定と食い違えば開いた時点で止まる（記録の無い既存の DB は bge-m3・1024次元とみなす）。

## 7. Nginx を置く

```bash
sudo apt install nginx apache2-utils
sudo htpasswd -c /etc/nginx/rag/htpasswd <利用者名>       # 利用者ごとに作る。リポジトリに入れない
sudo install -m 640 -o root -g www-data /dev/null /etc/nginx/rag/mcp-token.conf
```

`/etc/nginx/rag/mcp-token.conf`（1行。`<MCP のトークン>` は `openssl rand -hex 32` などで作る）:

```nginx
if ($http_authorization != "Bearer <MCP のトークン>") { return 401; }
```

`/etc/nginx/rag/tls.conf`（内部CAで発行した証明書）:

```nginx
ssl_certificate     /etc/nginx/rag/server.crt;
ssl_certificate_key /etc/nginx/rag/server.key;
```

```bash
sudo cp /opt/rag/deploy/gb10/nginx-rag.conf /etc/nginx/conf.d/rag.conf   # server_name を書き換える
sudo nginx -t && sudo systemctl reload nginx
```

`nginx-rag.conf` が押さえている点（段階③の申し送りの対処）。

- `client_max_body_size 70953643`: アップロードは base64 の JSON なので、本文は元の約1.34倍。
  式は `ceil(MB × 1,048,576 × 4 / 3) + 1,048,576` バイトで、`RAG_WEB_MAX_UPLOAD_MB=50`
  なら 70,953,643。rag_web は本文を全部メモリに読んでから 413 を返すため、Nginx で切る。
- `/` と `/llm/` は `proxy_buffering off`（チャットの NDJSON と SSE を溜め込まない）、
  `proxy_read_timeout 900s`（生成が長くても切れない）。
- `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`: ログイン試行制限を
  利用者ごとに数える。
- スレッドプール（`RAG_WEB_THREADPOOL_SIZE`、既定 40）と同時 Cowork 数
  （`RAG_WEB_MAX_COWORK_JOBS`、既定 2）は、想定の同時利用者数で確かめる。実測はしていない。
  生成中のチャットは枠を占めるので、数人が同時に長い質問を投げて、ほかの画面操作
  （状態・一覧）が待たされないかを見る。

### 確かめる

```bash
curl -sk -o /dev/null -w "%{http_code}\n" https://<GB10>/                         # 401（Basic 認証）
curl -sk -u <利用者名> -o /dev/null -w "%{http_code}\n" https://<GB10>/           # 200
curl -sk -o /dev/null -w "%{http_code}\n" https://<GB10>/mcp                      # 401（トークン無し）
curl -sk -H "Authorization: Bearer $KEY" https://<GB10>/llm/v1/models | head -c 200   # gpt-oss-120b
```

- 画面でチャットを送り、回答が1文字ずつ（まとめてではなく）流れることを見る
  （`proxy_buffering off` が効いている印）。
- MCP が 421 などで断られたら、`docs/mcp-server-network.md` の確認手順を見る
  （`/mcp` の `Host` ヘッダーの扱い）。実測はしていない。
- Claude Code からの使い方は [claude-code-gb10.md](claude-code-gb10.md)。

## 受け入れ確認の項目（実機で通すこと）

- [ ] 3つの vLLM が起動し、キー無しの要求が 401 になる
- [ ] `nvidia-smi` / `free` でメモリに余裕があり、3つ同居で OOM しない
- [ ] 画面（`/`）でチャットが両方のコーパスで流れて返る
- [ ] Cowork（文書生成）が通る。添付の取り込み（画像説明を含む）が通る
- [ ] `scripts/check_retrieval.py docs` と `internal` が通る（`internal` には、BM25 だけで
  引く回帰ケースが1件落ちる既知の問題がある。vLLM とは無関係で、この切り替えの前から
  ある。2026-11-23、Colab で確認）
- [ ] Nginx 越しでログイン試行制限が利用者ごとに数えられる
- [ ] 画像説明（Qwen3-VL-30B-A3B、FP8）の質。Colab では 8B の確認も済んでいない
  （実測はしていない。Colab での確認待ち）

## 関連

- [vLLM バックエンド切り替えの設計書](superpowers/specs/2026-11-22-vllm-backend-design.md)
- [Claude Code から GB10 を使う](claude-code-gb10.md)
- [社内資料の MCP サーバ](mcp-server.md) / [MCP を1台に集約する](mcp-server-network.md)
- `deploy/gb10/`: `compose.yaml`、`rag-web.service`、`rag-mcp.service`、`nginx-rag.conf`
