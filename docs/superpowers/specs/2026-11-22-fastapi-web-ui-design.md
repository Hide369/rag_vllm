# チャット画面を Streamlit から FastAPI + HTML/JS へ移す 設計

## 目的

`rag_chat_app.py`（Streamlit、1223行）を、社内の複数人が同時に使うサーバーとして
運用できる形へ移す。構成は Batch_Exec リポジトリの Web 画面
（`godoc_agent/web/`：FastAPI の `create_app()` + `static/index.html` + 素の
`static/app.js`、ビルドなし）を手本にする。

Streamlit は操作のたびにスクリプト全体を再実行するため、`generating` /
`pending_question` の後始末、`st.rerun()` で描画が消える問題、ダイアログの開閉
フラグなど、画面の都合の対処がコードの大半を占めている。サーバー（API）と
画面（JS）を分ければ、この種の対処は要らなくなり、同時利用時の振る舞い
（共有資源の排他）もサーバー側で明示的に書ける。

検索・生成・文書作成の本体はすでに `ingest/` と `docgen/` にあり、今回は変えない。
移すのは画面と、それらをつなぐ部分である。

## ゴール / 非ゴール / アンチゴール

- ゴール: 今の画面の機能を、足しも削りもせずに新しい画面へ移す（下の「機能一覧」）。
- ゴール: 複数人が同時に使っても、共有資源（DB・雛形フォルダ）が壊れない。
- ゴール: 移し終えたら Streamlit 版とその依存を取り除く。
- ゴール: 差分取り込み（サーバー上の `source/` をまとめて取り込む操作）だけは、管理者として
  ログインした人しか実行できない（下の「管理者ログイン」）。
- 非ゴール: 利用者ごとの権限・アカウント。差分取り込み以外（質問、資料のアップロードと
  削除、雛形の登録・削除）は今の Streamlit と同じく誰でもできる（利用者の選択）。
  画面に入れる人の制限は今の配置手順どおり手前の IIS（Windows 認証 + 利用グループ）で行う。
- 非ゴール: 会話履歴の保存。履歴はブラウザのタブの中だけにあり、再読み込みで消える
  （今の Streamlit と同じ）。
- 非ゴール: 新機能、他アプリ向けの公開 API（API は画面のためのもので、互換性は約束しない）。
- アンチゴール: 画面の表示のために外部（CDN など）へ通信すること。社内資料を扱う画面が、
  開くたびに社外へリクエストを出す状態にはしない。
- アンチゴール: 質問・回答・添付をサーバー側に残すこと。

## 検討した方式

移行の進め方で2つを比べ、**A を採用した**（利用者の選択）。

- **A. 段階的に移す（採用）**: ①チャットとサイドバーの操作 → ②Cowork → ③Streamlit 版の
  削除と手順書の書き換え、の3段階。段階ごとに PR を1本出す。途中は新旧の画面が
  別ポートで並び、本番は Streamlit のまま。①②の試用は**開発機だけ**で行い（利用者の選択）、
  本番サーバーの DB・`templates/` は共有しない。本番は③で一度に切り替える。そのため
  プロセスをまたぐ排他も、①②の段階での IIS の入口づくりも要らない。PR が小さくレビューしやすく、チャットを先に
  試せる。代わりに移行中は画面の修正が二重になる（→ 移行中は Streamlit 版を凍結する）。
- **B. 一度に置き換える**: 新旧が並ぶ期間はないが、差分が大きく、全部揃うまで試せない。

外した方式:
- 回答の逐次表示を EventSource（SSE）で送る: GET しか使えず、会話履歴を URL に詰めることに
  なり長さの上限に当たる。代わりに POST の応答を `fetch` + `ReadableStream` で読む。
- React / htmx: 手本から外れ、ビルド工程か依存が増える。
- Streamlit を残して API だけ足す: 複数人の運用という目的に応えない。

## 全体構成

```text
ブラウザ（index.html + app.js）
   │ 会話履歴はJSのメモリだけ。質問のたびに履歴ごと送る
   ▼
rag_web（FastAPI, 1プロセス, 既定 127.0.0.1:8502）
   ├─ /api/chat ……… NDJSON で逐次返す
   ├─ /api/cowork …… 生成した文書を JSON（base64）で返す。サーバーには残さない
   ├─ 共有資源を変える操作（取り込み・アップロード・削除・雛形登録）は1本ずつ
   └─ ingest/ ・ docgen/ ・ scripts.ingest_source を呼ぶ（変更しない）
        └─ OLLAMA_HOST（既存の経路のみ）
```

### パッケージ構成

Batch_Exec の `godoc_agent/web/` に倣い、リポジトリ直下に `rag_web/` を作る。

- `rag_web/__main__.py` — `python -m rag_web` で起動。`.env` を読み（`load_dotenv()`、
  `ingest` を import する前。理由は今の `rag_chat_app.py` 冒頭と同じ）、uvicorn で起動する。
- `rag_web/settings.py` — 環境変数 `RAG_WEB_HOST`（既定 `127.0.0.1`）、`RAG_WEB_PORT`
  （既定 `8502`。移行中に Streamlit の 8501 と並べるため）、`RAG_WEB_MAX_UPLOAD_MB`
  （1リクエストで受け取る上限、既定 50）、管理者ログイン用の `RAG_WEB_ADMIN_PASSWORD_HASH`
  （必須）・`RAG_WEB_SESSION_TTL_HOURS`・`RAG_WEB_COOKIE_SECURE`。`.env.example` に追記する。
- `rag_web/auth.py` / `rag_web/password.py` — 管理者ログイン（Batch_Exec から移植）。
- `rag_web/app.py` — `create_app(...)`。ルーティングと入力の検証だけを持つ。依存
  （チャット関数・埋め込み・ストアの開き方・雛形フォルダ）は引数で差し替えられるように
  して、テストで偽物を渡す（Batch_Exec の `create_app(settings, jobs=..., ...)` と同じ形）。
- `rag_web/corpora.py` — DB を開いて保持し、`revision()` が変わったら BM25 索引と属性一覧を
  組み直すキャッシュ。今の `get_collection` / `get_index` / `get_schema` を置き換える。
  鍵は (DB のパス, revision)。理由は今の docstring にあるとおり（revision はDBごとの独立した
  カウンタ）。組み直しはロックの下で1回だけ行う。
- `rag_web/answering.py` — チャット1回分の処理（条件抽出・検索・プロンプト組み立て・
  生成）をイベントの列として返すジェネレータ。今の `rag_chat_app.py` 1006〜1218行の
  ロジックを、画面の呼び出しを取り除いて移す。
- `rag_web/cowork.py` — `_collect_evidence` / `_generate_document` / `_generate_freeform` /
  `_has_no_evidence` を移し、結果を dict（errors / warnings / infos / download / sources）で返す。
  `st.session_state` に積む代わりに戻り値で返す。
- `rag_web/shared_ops.py` — 共有資源を変える操作の排他（下の「同時利用」）。
- `rag_web/static/index.html` / `app.js` — 画面。スタイルは index.html に内包し、色の変数と
  ダークモードは Batch_Exec の `:root` 定義をそのまま使う。
- `rag_web/static/vendor/` — Markdown 描画・サニタイズ・Mermaid 用の JS をリポジトリに
  同梱する（アンチゴールのとおり CDN は使わない）。ライブラリの選定とバージョン固定は
  実装者に任せるが、ライセンスと出典を `docs/依存関係一覧.md` に追記すること。

移行中、Streamlit 版（`rag_chat_app.py`）には手を入れない（凍結）。ロジックは
`rag_web/` へ写すので、移行期間だけ同じロジックが2箇所にある。③で Streamlit 版ごと
消えて解消する。Streamlit 版を共通モジュールに乗せ換える案は、消すコードへの改修で
あり、AppTest のテストが `rag_chat_app` のモジュール変数を差し替えている箇所を
壊すため取らない。

## 機能一覧（今の画面との対応）

| 今の画面 | 新しい画面 | 段階 |
|---|---|---|
| サイドバー: モデル名（`MODELS`）、Temperature（0.0〜1.0, 0.1刻み, 既定0.3） | 左の設定欄。値はタブ内の JS が持ち、毎回送る | ① |
| 検索対象ラジオ（社内資料 / 技術ドキュメント）。切り替えで履歴を消し、その旨を表示 | 同じ。履歴の消去と表示は JS 側 | ① |
| インデックス済みチャンク数、技術ドキュメントが空のときの警告と CLI コマンド | `GET /api/status` | ① |
| 差分を取り込む（社内資料のみ）、結果の報告と VLM が使えない旨 | `POST /api/ingest`（管理者ログインが必要。新規） | ① |
| 資料をアップロード（ダイアログ）、アップロード済み一覧と削除 | `GET/POST /api/uploads`、`DELETE /api/uploads/{key}` | ① |
| リランカーの確認と、使えないときの警告 | 初回の検索で確認し、結果を `/api/status` で見せる（下の注） | ① |
| 会話履歴をリセット | JS 側だけ | ① |
| チャット: 逐次表示、条件を解釈できなかった旨の注記、検索・生成の失敗表示 | `POST /api/chat` | ① |
| 参考にした情報（件数、出典、同じ記述、本文）／絞り込んだ一覧（表） | `evidence` イベント | ① |
| 「マークダウンで表示して」等の記法表示、Mermaid の図 | `done` イベントの `display` を見て JS が描く | ① |
| 表のセル内の HTML タグの除去（`strip_html_tags_stream`） | サーバー側で今と同じく通す | ① |
| モード切り替え（チャット / Cowork） | 同じ | ② |
| フォーマットファイル選択、出力形式（雛形なしのとき）、参照コーパスのチェック（ソースコード出力のとき技術ドキュメントを既定でオン） | 同じ | ② |
| 添付・貼り付け（DB に入れない） | `POST /api/cowork` の本文に含める | ② |
| 結果（エラー・補足・情報・ダウンロード・参照した検索結果） | `POST /api/cowork` の応答 | ② |
| フォーマットファイルの登録・削除（ダイアログ、印の一覧） | `GET/POST /api/templates`、`DELETE /api/templates/{name}` | ② |

リランカーの確認は今の `@st.cache_resource` と同じ振る舞いにする（設計者の判断）。
成功したらプロセス内で覚えて二度と確認しない。失敗は覚えず、その回は並べ替えなしで
検索し、次の検索でもう一度確認する（一時的な通信断のあとにずっと並べ替えなしになる
ことを避ける）。確認中（初回は570MB・約1分）に届いた別の検索は、ロックで待たせて
同じ確認を重ねて走らせない。状態は「未確認 / 確認中 / 使える / 直前の確認で失敗（理由）」。

生成中はチャット入力欄・検索対象・モード・Cowork の入力を無効にする（今と同じ）。
今の Streamlit と違い、無効にする理由は「生成を打ち切らないため」ではなく、
「1つのタブで並行して質問させない」ためだけになる。

## API

状態を変えるリクエスト（GET 以外）は、`X-Requested-With: rag-web` ヘッダーを必須にする
（Batch_Exec と同じ CSRF 対策。ログインが無くても、別サイトのページから取り込みや削除を
実行させられるのを防ぐ）。無ければ 403。

ファイルの受け渡しは Batch_Exec と同じく JSON + base64 にする（`python-multipart` を
足さない）。1リクエストの上限は `RAG_WEB_MAX_UPLOAD_MB`、超えたら 413。

### `GET /api/status?corpus=internal|docs`
モデルの一覧、選択中コーパスのチャンク数、取り込み元フォルダ名、技術ドキュメントの
CLI コマンド（`DOCS_INGEST_COMMAND`）、リランカーの状態（未確認 / 使える / 使えない＋理由）、
共有操作が実行中か。

### `POST /api/chat`
本文: `{corpus, model, temperature, question, history: [{role, content}]}`。
`model` は `MODELS` に含まれるもの、`temperature` は 0〜1、`history` の各要素の `role` は
user / assistant のみ（system は受け取らない。サーバーが付ける）。

応答: `application/x-ndjson`。1行1イベント。
- `{"type":"note","text":...}` — 条件を解釈できなかった旨（今の `note`）
- `{"type":"delta","text":...}` — 回答の断片
- `{"type":"evidence","hits":[...],"table":...}` — 根拠。hit は
  `{caption, others:[...], text}`（`format_hit_caption` と `all_citations()[1:]` をサーバーで整形）
- `{"type":"error","message":...}` — 検索・生成の失敗（今の「検索できませんでした: …」
  「回答を生成できませんでした: …」と同じ文面）
- `{"type":"done","display":null|"markdown"|"mermaid"|...}`

ストリームは必ず `done` か `error` で終わる。JS は受け取った回答を履歴に積み、次の
質問で `history` として送る。エラー文も今と同じく履歴に残す（assistant の発言として）。

`contextual_query(question, history)` の `history` には今と同じく直前までの発言を渡す。
今の実装は `messages` に `hits` / `table` / `note` を持たせているが、`contextual_query` と
生成が使うのは `role` と `content` だけなので、送るのはその2つだけにする。

### 時間のかかる処理はジョブにする（設計者の判断）

取り込み（図表ごとに VLM を呼ぶため数分かかる）と Cowork の生成（30〜60秒、添付に図表が
あればさらに延びる）は、1回の同期 POST では返さない。素の HTTP では IIS の ARR の
プロキシのタイムアウト（既定120秒）に当たって 502 になるためで、今の Streamlit は
WebSocket なのでこの問題が見えていなかった。Batch_Exec の `jobs.py` と同じく、
「POST でジョブを起動して `job_id` を返す → `GET /api/jobs/{job_id}` を JS が数秒おきに
取りに行く」形にする。

- ジョブの状態は `running` / `done` / `failed`。`done` なら結果、`failed` ならエラー文を持つ。
- ジョブはプロセスのメモリにだけ持ち、結果を取りに来たあと、または終了から10分で捨てる。
  Cowork の生成物（ダウンロードするファイル）もこの間だけメモリにあり、ディスクには
  書かない（アンチゴール「サーバーに残さない」は、ディスク・DB に残さないという意味で守る）。
- `job_id` は推測できない乱数にする（ログインが無いので、ID を知っている人だけが結果を取れる）。
- チャットはジョブにしない。最初の断片が届けばストリームが流れ続けるため。③の配置手順では
  それでも ARR のタイムアウトを延ばしておく（最初の断片までの待ちに備える）。

### 管理者ログイン

Batch_Exec の `godoc_agent/web/auth.py` と `password.py` を移植する。

- 管理者は1種類だけ（パスワードを知っている人）。`.env` の `RAG_WEB_ADMIN_PASSWORD_HASH` に
  scrypt のハッシュ（`scrypt:N:r:p:salt:hash` 形式）だけを書く。平文のパスワードはどこにも
  置かない。ハッシュは `python -m rag_web.password` で作る。
- 起動時にハッシュの形式を確かめ、未設定・壊れていれば起動しない（誰もログインできない
  状態で動き続けるのを防ぐ。Batch_Exec の `validate_password_hash` と同じ）。
- セッションはサーバーのメモリに持ち、Cookie（`HttpOnly`、`SameSite=Strict`、HTTPS 配下では
  `Secure`。`RAG_WEB_COOKIE_SECURE`）で判別する。有効期限は `RAG_WEB_SESSION_TTL_HOURS`
  （既定12）。再起動するとログアウトされる。
- 失敗が続いたら一定時間ログインを受け付けない（Batch_Exec の `LoginThrottle`）。
- API: `POST /api/login`（`{password}`）、`POST /api/logout`、`GET /api/me`（`{admin: bool}`）。
- 画面: 設定欄に「管理者ログイン」を置く。ログインしていないときは「差分を取り込む」
  ボタンを出さない。ログイン中は「ログアウト」を出す。
- ログインが要るのは `POST /api/ingest` だけ。未ログインなら 401。

### `POST /api/ingest`（管理者のみ）
社内資料の差分取り込みのジョブを起動する。結果は `{report, notice}`（`format_report` の
文面と、VLM が使えない理由）。Ollama に繋がらなければ、ジョブを起動せず 503 と理由を返す。

### `GET /api/uploads` / `POST /api/uploads` / `DELETE /api/uploads/{key}`
今の `upload_dialog` と同じ。POST はジョブを起動する（上記）。本文は `{files:[{name, content_b64}]}`。拡張子は
`SUPPORTED_SUFFIXES` に限る。ファイル名はパス区切りを含まないことを確かめてから一時
フォルダへ書く。削除は `UPLOAD_PREFIX` で始まるキーだけを受け付ける。

### `POST /api/cowork`（段階②）
本文: `{model, question, template|null, output_suffix, use_internal, use_docs, pasted,
attachments:[{name, content_b64}]}`。ジョブを起動し、結果は `{errors, warnings, infos, download:
{file_name, content_b64}|null, sources:[{kind, hits:[...]}]}`。判定と文面は今の
`rag_chat_app.py` の Cowork 分岐と同じ。

### `GET /api/templates` / `POST /api/templates` / `DELETE /api/templates/{name}`（段階②）
一覧は `{name, placeholders:[...]|null, error}`。登録・削除は `docgen.templates` を呼ぶ
（ファイル名の検査は `templates._checked` に任せる）。

## 同時利用

- 生成・検索は並行に受け付ける。Ollama 側が順に処理する（今の Streamlit と同じ）。
  アプリ側で同時数の上限は設けない。
- 共有資源を変える操作（差分取り込み、アップロード、アップロード済み資料の削除、
  雛形の登録・削除）はプロセス内のロック1つで直列にし、取れなければ待たずに 409
  「ほかの人が取り込み中です」を返す。今は2人が同時に押すと同じ DB に並行して書ける。
- 書き込みと検索が重なったときの整合は `VectorStore` の `RLock`（`ingest/vector_store.py`）に
  任せる。BM25 索引は `revision()` が変わった次の検索で組み直す。
- エンドポイントは同期関数（`def`）で書き、FastAPI のスレッドプールで動かす
  （`ingest/` は同期 I/O のため）。チャットのストリームは同期ジェネレータの
  `StreamingResponse` にする。
- 1プロセスで動かす（SQLite とメモリ内キャッシュのため。今の配置手順と同じ）。

## 画面

Batch_Exec の見た目（ヘッダー、`section` のカード、色の変数、ダークモード）に揃える。
左に設定欄（今のサイドバー）、右にチャット（または Cowork）。幅が狭いときは縦に積む。

```text
┌ 社内文書RAG ───────────────────────────── [チャット|Cowork] ┐
├─────────────┬───────────────────────────────────────────────┤
│ 設定         │ (user) 洗濯機の最大容量は？                     │
│ モデル [▼]   │ (assistant) …逐次表示…                          │
│ Temperature  │   ▸ 参考にした情報（5件）                        │
│ 検索対象 ◉社内│                                               │
│  ○技術Doc    │                                               │
│ チャンク 1234│                                               │
│ [差分を取込] │                                               │
│ [資料をUP]   │ ┌───────────────────────────────┐ [送信]        │
│ [履歴リセット]│ │メッセージを入力                │               │
└─────────────┴─┴───────────────────────────────┴───────────────┘
```

ダイアログは `<dialog>` 要素を使う（開閉フラグの問題は起きない）。回答は Markdown として
描画し、サニタイズしてから DOM に入れる（回答・資料の本文はどちらも信頼しない入力）。
`display` が付いたときはコードブロック（コピーボタン付き）で出し、`mermaid` なら図も描く。

## 段階と、各段階の完了条件

1. **チャットとサイドバーの操作**（`rag_web/` 新設、`/api/status` `/api/chat` `/api/ingest`
   `/api/uploads`、画面）。`requirements.txt` に `fastapi` と `uvicorn` を足す（Batch_Exec と
   同じ版から始め、最新の安定版を確認して固定する）。起動用に `run_web.ps1` を足す
   （`run_streamlit.ps1` と同じく `rag_env` → `myvenv313` の順で探す）。完了条件: API の
   テストが通り、ブラウザで2つのタブから同時に質問・取り込みをして期待どおり振る舞う。
2. **Cowork**（`/api/cowork` `/api/templates`、画面）。完了条件: 雛形あり・なしの各出力形式で
   生成・ダウンロードでき、API のテストが通る。
3. **Streamlit の撤去**: `rag_chat_app.py`、`tests/test_rag_chat_app.py`、
   `tests/test_rag_chat_app_cowork.py`、`run_streamlit.ps1`、`requirements.txt` の
   `streamlit` を削除。`rag_chat_app` / Streamlit に触れている文書とコメント
   （README、`docs/server-deployment-windows.md`、`docs/server-deployment.md`、`docs/server-set.md`、
   `docs/依存関係一覧.md`、`docs/処理箇所マップ.md`、`docs/技術ドキュメントDBの移植手順.md`、
   `ingest/prompting.py`、`ingest/query_translation.py`、`ingest/store.py`、`scripts/check_retrieval.py`、
   `scripts/rag_mcp_server.py` と関連テスト）を新しい画面に合わせて直す。配置手順は IIS の
   転送先を新しいポートへ変え、WebSocket の設定を外し、ストリーミングが溜め込まれないよう
   ARR の応答バッファリングを切る手順に変える。

今の AppTest のテストが確かめている振る舞い（コーパス切り替えでの履歴消去、条件抽出の
失敗時の注記、Ollama 停止時のエラー文、空 DB の警告、雛形が壊れているとき・印が無い
とき・根拠が無いときに生成しないこと など）は、①②で API のテストとして書き直す。
③で消すのは、同じ振る舞いが新しいテストで押さえられていることを確かめてからにする。

## 開発環境

仮想環境は `rag_env`（Python 3.13）を作り、①の最初の手順で `requirements.txt` を入れる。
README のセットアップ手順の `myvenv313` は③で `rag_env` に書き換える（`.gitignore` には
両方ある）。

## 実装者に任せること

- Markdown 描画・サニタイズ・Mermaid の JS ライブラリの選定（同梱・版固定・ライセンス記載が条件）。
- NDJSON の読み取りや画面部品の細部、文言の細かな調整（エラー文は今と同じ文面を保つ）。
- クライアントが途中で切断したときに生成を打ち切るかどうか（打ち切れない場合は最後まで
  生成して捨ててよい）。
- `rag_web/` 内のモジュールの分け方の細部（上の分け方は目安）。
- テストでの偽物の作り方（Batch_Exec の `tests/fake_*.py` が参考になる）。

## 段階③への申し送り

段階①の最終レビュー（2026-11-22）で見つかり、IIS 配下での本番運用に関わるため③で
対応・確認する事項。

- **IIS の要求サイズ上限**: アップロードは JSON に base64 で載せるため、本文は元のファイルの
  約1.34倍になる。IIS の `requestFiltering` の `maxAllowedContentLength` は
  `RAG_WEB_MAX_UPLOAD_MB` の base64 後の大きさ（＋JSON の枠）を通す値にすること。
  また、サイズの確認（413）は本文をすべて読んで base64 を解いたあとに行うため、上限を
  大きく超える要求もいったんメモリに載る。前段の IIS で切るのが本当の上限になる。
- **ログイン試行の制限の単位**: `LoginThrottle` は `request.client.host` ごとに数える。ARR の
  リバースプロキシ越しではこれがプロキシの IP になり、1人の失敗の連続で全員がロックアウト
  される。`X-Forwarded-For` を信頼できる前段からのときだけ読む（uvicorn の
  `--proxy-headers` / `--forwarded-allow-ips`）などの対処を決める。
- **スレッドプールの共有**: ストリーミング中のチャットと、ほかの同期エンドポイント（状態・
  一覧・ジョブの取得など）は anyio の同じスレッドプール（既定 40）を使う。生成中のチャットが
  枠を占めると、ほかの要求が待たされる。想定の同時利用者数で計測してから配置すること。
- **VectorStore に close() が無い**: `ingest/vector_store.py` の接続は閉じる手段が無く、テストは
  `_connection` を直接閉じて凌いでいる（`tests/test_web_app.py` / `tests/test_web_corpora.py`）。
  スイート全体を `-W error` で流すと、ほかのテストが開いた接続の `ResourceWarning` で落ちる。
- **ARR の応答バッファリングとタイムアウト**: チャットは NDJSON を逐次返す。ARR の応答
  バッファリングを切らないと溜め込まれて最後にまとめて届く。生成が長いと ARR の既定の
  タイムアウトで切れるため、プロキシのタイムアウトも合わせて延ばす。
- **Cowork ジョブの同時実行数に上限が無い**（段階②の最終レビュー）: 共有ロックを取らない
  ため、同時に走る生成の数を制限していない。1本ごとに添付（最大 `RAG_WEB_MAX_UPLOAD_MB`）と
  生成物がメモリに載る。案: `BoundedSemaphore` で上限を決め、取れなければ 503 を返す。
- **エラー文にサーバーの絶対パスが入りうる**（段階②の最終レビュー）: OSError の詳細や
  「開けませんでした: [Errno 2] …」のような文面は、サーバー上の絶対パスを含みうる。社内
  利用なので当面は許容するが、公開範囲を広げるなら応答の文面から落とし、詳細はログにだけ残す。
