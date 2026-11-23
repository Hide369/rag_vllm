# Codex 開発ガイドライン

- `.agents/skills/using-superpowers/SKILL.md` が存在する場合は、応答やコマンドの実行前にその内容を確認し、記載されているスキル選択のワークフローに従ってください。
- `$skill-name` で明示的に指定されたスキルは必須です。実行前に `.agents/skills/<skill-name>/SKILL.md` を確認してください。
- 本リポジトリに記載されている Windows PowerShell コマンドおよび Python 仮想環境を使用してください。
- 現在のツールセットに `apply_patch` ツールが含まれていない場合は、利用できないツールを再試行するのではなく、PowerShell を使用して直接、小規模な編集を行ってください。
- テストの実行結果において、コマンドの出力が正常終了（成功）を示し、かつ失敗がゼロであることを確認した後にのみ、テストが合格したと報告してください。

## 社内資料の参照

- 社内資料・規程・議事録・会議録に関する質問は、推測せず `search_documents` で
  検索し、返された出典を明記して回答すること。
- 検索結果が質問と無関係な場合は、それを根拠に使わないこと。距離が閾値内である
  ことは関連性を保証しない。
- 検索が0件だった場合、および結果が無関係だった場合は、再検索やウェブ検索で
  補おうとしないこと。一般的な知識で答えられる質問なら、社内資料に基づかない
  一般的な回答である旨を断ったうえで通常どおり回答してよい。社内固有の事項
  （規程・議事録・社内の決定・組織固有の運用）だけは推測せず、社内資料からは
  確認できない旨を答えること。
- 資料に書かれていない主体（「弊社が」等）を補わないこと。

## 外部ドキュメントの参照

- OSS ライブラリ・フレームワークの API や設定は `context7` で調べること。
- 社内固有の情報を context7 のクエリ文に含めないこと。context7 は社外の
  サービスであり、**こちらから内容を送る唯一の経路**である。送った `query` と
  `libraryName` は先方に匿名で保存され、結果の並べ替えのため先方が使う外部 LLM
  にも渡る（手順と根拠は `docs/context7-claude-code.md`）。
  - `query` / `libraryName` に書いてよいのは、公開ライブラリ名とバージョン
    （例: `fastapi 0.115`）と、一般的な機能・概念の説明
    （例: `dependency injection with async database session`）だけである。
  - 書いてはいけないもの: 社内のシステム名・プロジェクト名・リポジトリ名・
    ホスト名・URL、社内コード由来のファイル名・関数名・クラス名・変数名、
    ソースコード片、エラーメッセージ中の社内パスや設定値、顧客名・個人情報、
    API キー・パスワード・トークン、業務内容や仕様が推測できる記述。
  - 送る前に、調べたい内容を「公開ライブラリの一般的な使い方」へ抽象化し、
    上の禁止事項が残っていないか確かめること。抽象化できない（社内固有の事情に
    依存する）質問には context7 を使わず、手元のコードと社内資料を参照すること。
  - 例: NG `<社内システム名> の retriever.py で ChromaDB の検索が遅い` →
    OK `chromadb query performance tuning`
- 外部へ出る通信は context7 だけではない。`search_documents` の初回呼び出しは、
  リランカーのモデルが手元にあるかを確かめるため huggingface.co へ HEAD を6回
  送る（実測 2026-09-08。`ingest/reranker.py` の `check_reranker` →
  `_model_paths`、詳細は `docs/mcp-server.md`）。送るのは要求するモデルファイルの
  パスだけで、検索語も社内資料の中身も含まない。それでも「社内資料の検索は
  完全にローカルで閉じている」とは言えないので、オフライン環境ではモデルを
  事前に取得しておくこと。
- 3本目は `scripts/fetch_docs.py` である。`docs_sources.toml` に書かれた宛先へ
  GET を出す。宛先は2種類ある。`kind = "llms"`（既定）は公式ドキュメントの
  `llms-full.txt` を1本取りに行く。`kind = "github"` は `api.github.com` の
  Trees API でリポジトリの木を1回引き、`raw.githubusercontent.com` から各ページの
  本文を取る。どちらも送るのはURLへの要求だけで、社内資料の内容も検索語も
  含まない。認証もトークンも使わない。3種類目の `kind = "local"` は宛先を
  持たない（`scripts/local_source.py`）。リポジトリ内のファイルを読んで
  `docs_source/` へ写すだけで、外部へは1バイトも出ない。
  設定に書かれたURL・リポジトリ・パスしか取りに行かず、ページ内のリンクは
  辿らない。唯一の例外が `resolve_code_refs = true` を書いたソースの `:::code`
  で、これは C# のドキュメントがコード例を外部ファイルに置いているために要る
  （`scripts/code_references.py`）。解決先は**同じリポジトリ・同じ ref の木に
  ある blob に限り**、木に無いパスは取りに行かない。外部のURLは辿らない。
  人が明示的に起動したときだけ走り、常駐プロセスもスケジューラも持たない。
  オフライン環境では実行できないが、取得済みの `docs_source/` があれば
  取り込みはローカルで完結する（`scripts/ingest_source.py` が触る外部の宛先は
  `VLLM_EMBED_URL` と `VLLM_VLM_URL` だけである）。
- 4本目は `VLLM_*` の3経路である。推論は役割ごとに別の vLLM（OpenAI 互換 API）が
  受け持ち、宛先も3つに分かれる。既定値（`ingest/vllm_config.py` の
  `127.0.0.1:8001` / `8002` / `8003`）のままなら、これらの経路はマシンの外へ出ない。
  本番の GB10 でも、vLLM は `127.0.0.1` にだけ公開する（`deploy/gb10/compose.yaml`）
  ので、アプリが GB10 の `127.0.0.1` を指していれば、外へは出ない。
  一方、開発機から Colab（README「ColabのL4でvLLMを試す」）や GB10 を指すと、
  `.env` の `VLLM_*_URL` が ngrok や LAN のURLになり、その経路に質問と資料の本文が
  そのつど乗る。経路ごとに乗るものは次のとおり。
  - `VLLM_CHAT_URL`（`/v1/chat/completions`）: 質問文・会話履歴・生成に渡す検索結果の
    チャンク本文。`ingest/chat.py` の `ask_json` / `ask_text` / `stream_chat` が叩く。
    `ingest/query_translation.py` の `translate_query()` も `ask_json` を呼ぶだけで、
    新しい経路を増やすものではない。
  - `VLLM_EMBED_URL`（`/v1/embeddings`）: 取り込み時のドキュメント自身の本文と、
    検索時の質問文。`ingest/embedder.py` が叩く。
  - `VLLM_VLM_URL`（`/v1/chat/completions`）: 取り込み時の画像（図表・スキャン画像）。
    `ingest/vlm.py` が叩く。
  - `VLLM_API_KEY` は上の3つに同じ `Authorization: Bearer` で送られる。
  雛形からの文書生成（`docgen/`）もこの経路を使う。新しい宛先は増えないが、
  画面で添付したファイルの本文が `ingest/chat.py` の `ask_json` に渡り、
  `VLLM_CHAT_URL` の経路へ乗る。添付はDBに入れない
  （`docs/superpowers/specs/2026-09-13-document-generation-design.md`）が、DBに
  残らないことと外へ出ないことは別である。オフラインの資料を扱うときは
  `VLLM_*_URL` がローカル（`127.0.0.1`）を指していることを確かめること。
  雛形なしの生成（`docgen/freeform.py` / `docgen/source_code.py`）も同じで、
  添付と貼り付けたテキストの本文が `ingest/chat.py` の `ask_text` に渡り、
  この経路へ乗る。
  FastAPI版の画面（`rag_web/`）も宛先を増やさない。ブラウザは CSP で同一オリジン
  にしか通信できず、サーバーが外へ出るのは `VLLM_*_URL` の3つと、チャットまたは
  Cowork（文書生成）で最初に検索するときのリランカーの確認（上の huggingface.co
  への HEAD）だけである。この確認は失敗すると次の検索でやり直すため、オフラインでは
  質問や生成のたびに繰り返す。
  オフラインで使うならリランカーのモデルを事前に取得しておくこと。
- 旧 `OLLAMA_HOST` / `OLLAMA_API_KEY` / `OLLAMA_VLM_MODEL` が環境に残っていると、
  入口が止まる（黙って既定の宛先へ繋がらないため）。`VLLM_*` へ移すこと。

