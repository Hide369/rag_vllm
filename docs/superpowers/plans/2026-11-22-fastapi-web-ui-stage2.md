# FastAPI 画面 段階②（Cowork）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新しい画面（`rag_web/`）に Cowork（雛形あり・なしの文書生成、添付・貼り付け、ダウンロード、フォーマットファイルの登録・削除）を、今の Streamlit 版と同じ振る舞いで移す。

**Architecture:** 生成ロジックは `rag_web/cowork.py` に置き、画面に依存しない `generate_cowork(...) -> dict` として段階①の `answering.py` と同じ形で切り出す。API は Cowork の生成をジョブ（共有ロックなし）で走らせ、フォーマットファイルの登録・削除は共有操作のロック（`JobStore.shared_operation()`）の下で行う。画面は段階①の `app.js` にモード切り替えと Cowork パネルを足す。

**Tech Stack:** Python 3.13（`rag_env`）、FastAPI、pytest + TestClient、素の JavaScript（段階①の同梱ライブラリと CSP をそのまま使う）。

**Spec:** `docs/superpowers/specs/2026-11-22-fastapi-web-ui-design.md`（段階②、`POST /api/cowork`、`/api/templates`、機能一覧の ② の行）

## Global Constraints

- 仮想環境は `rag_env`。`rag_chat_app.py` と `tests/test_rag_chat_app*.py` には触れない（凍結）。`ingest/` `docgen/` `scripts/` は変更しない（呼ぶだけ）。
- 判定・分岐・文言は `rag_chat_app.py` の Cowork（`render_cowork_result` 〜 `_generate_freeform`、および 1015〜1060 行の分岐、`template_dialog`、Cowork の入力欄 896〜988 行）と同じにする。主な文言（そのまま使う）:
  - 「参照する資料も添付ファイルもありません。根拠が無いため生成しません。」
  - 「{雛形名} を開けませんでした: {error}」「{雛形名} に {{印}} がありません。埋める欄が無いため生成しません。」
  - 「技術ドキュメントが取り込まれていません。この検索は飛ばしました。」
  - 「{添付名} を開けませんでした: {error}」「{添付名} から本文を取り出せませんでした。」
  - 「埋まらなかった欄: {a、b}（フォーマットファイルの {{印}} が残ります）」「図にできなかった欄: …」（雛形あり）／「図にできなかった箇所: …」（雛形なし）
  - ファイル名: 雛形あり `{stem}_{YYYY-MM-DD}{suffix}`、雛形なし `文書_{YYYY-MM-DD}{suffix}`
  - 貼り付けの名前 `貼り付けたテキスト`、雛形なしの選択肢 `（フォーマットファイルなし）`
  - 画面の見出し・ラベル: `フォーマットファイル`、`フォーマットファイルを登録・削除`、`出力形式`、`社内資料を参照`、`技術ドキュメントを参照`、`添付（この回だけ使い、DBには入れません）`、`テキストを貼り付け（この回だけ使い、DBには入れません）`、`補足（N件）`、`ダウンロード`
  - ダイアログの注意書き「フォーマットファイルの空欄は {{会議名}} のように書いてください。登録したフォーマットファイルはこのマシンの templates/ に残ります。」
- 生成に使うモデルは `rag_web.app.CHAT_MODEL`（画面から選ばせない）。生成の LLM 呼び出しは `num_ctx=docgen.filling.GENERATION_NUM_CTX`。
- 添付・貼り付け・生成物はディスク・DB に残さない（添付は一時フォルダで解析して消す。生成物はジョブの結果としてメモリにだけ置く）。
- 添付の合計サイズは `settings.max_upload_bytes` まで、超えたら 413。添付の拡張子は `ingest.parsers.SUPPORTED_SUFFIXES`、雛形は `docgen.SUPPORTED_SUFFIXES`。ファイル名の検査（区切り文字・`..`・255文字・大文字小文字違いの重複）は段階①の `/api/uploads` と同じ関数を使う。
- GET 以外は `X-Requested-With: rag-web` 必須。雛形の登録・削除は共有操作（実行中なら 409「ほかの人が取り込み中です。終わってからもう一度実行してください」）。Cowork の生成は共有操作にしない（何も書き換えないため）。
- テストは `-W error` で警告ゼロ。警告フィルタで抑制しない。コミットはコンベンショナルコミット・英語。

## Review Focus

1. **生成中に別の人がその雛形を削除した** → 生成は「{雛形名} を開けませんでした: …」のエラー結果で終わり、ジョブが失敗のまま残ったりサーバーが落ちたりしない（Task 2 `test_cowork_template_removed_during_generation_reports_error`）。
2. **添付が壊れている・未対応形式・中身が空** → 壊れていれば「開けませんでした」で止まり、空なら補足に「本文を取り出せませんでした」を出して生成は続ける（Task 1 `test_broken_attachment_stops_with_error` / `test_empty_attachment_warns_and_continues`）。
3. **雛形名にパス区切りや `..`、未対応の拡張子** → 400 で拒否し、templates/ の外に書かない・消さない（Task 2 `test_template_register_rejects_unsafe_names` / `test_template_delete_rejects_unsafe_names`）。
4. **技術ドキュメントの DB が空のまま「技術ドキュメントを参照」** → 検索を飛ばして情報を出し、生成は続ける（Task 1 `test_empty_docs_corpus_is_skipped_with_info`）。
5. **生成ボタンの連打・生成中のモード切り替え** → 生成中は Cowork の入力とモード切り替えを無効にし、ジョブが done / failed / 通信断のどれで終わっても戻す（Task 3 の手動確認項目）。

---

### Task 1: Cowork の生成ロジック（`rag_web/cowork.py`）

**Files:**
- Modify: `rag_web/services.py`（`Services` に `ask_text`、`parse`、`templates_dir` を追加）
- Create: `rag_web/cowork.py`
- Test: `tests/test_web_cowork.py`

**Interfaces:**
- Consumes: `Services`、`CorpusCache`（段階①）、`answering.hit_view`
- Produces:
  ```python
  # services.py に追加
  ask_text: Callable[..., str] = chat.ask_text          # (model, prompt, num_ctx=...)
  parse: Callable[..., list] = ingest.parsers.parse     # (path, caption_image=...)
  templates_dir: Path | None = None                     # None なら docgen.templates の既定（templates/）
  # 既存の ask_json も (model, prompt, num_ctx=...) で呼ぶ（chat.ask_json は num_ctx を受け取る）

  # cowork.py
  NO_TEMPLATE_LABEL = "（フォーマットファイルなし）"
  PASTED_TEXT_NAME = "貼り付けたテキスト"
  @dataclass(frozen=True)
  class Attachment:
      name: str
      data: bytes
  @dataclass(frozen=True)
  class CoworkRequest:
      model: str
      question: str
      template: str | None          # templates_dir 直下のファイル名。None は雛形なし
      output_suffix: str            # 雛形なしのときだけ使う
      use_internal: bool
      use_docs: bool
      pasted: str
      attachments: list[Attachment]
  def output_suffixes() -> list[str]          # markdown_document.OUTPUT_SUFFIXES + source_code.OUTPUT_SUFFIXES（この順）
  def is_source_suffix(suffix: str) -> bool   # suffix.lower() in source_code.OUTPUT_SUFFIXES
  def template_path(services, name: str) -> Path   # docgen.templates の一覧から名前で引く。無ければ FileNotFoundError
  def generate_cowork(request: CoworkRequest, services: Services, corpora: CorpusCache, today: date) -> dict
      # {"errors": [str], "warnings": [str], "infos": [str],
      #  "download": {"file_name": str, "content_b64": str} | None,
      #  "sources": [{"kind": "社内資料"|"技術ドキュメント", "hits": [hit_view(...)]}]}
  ```
- 分岐は `rag_chat_app.py` 1015〜1060 行と同じ順:
  - 雛形なし: 根拠が無ければ（社内資料も技術ドキュメントも参照せず、添付が無く、貼り付けが空白だけ）エラーで止める → それ以外は `_generate_freeform` と同じ処理。
  - 雛形あり: `docgen.placeholders` が例外 → 「開けませんでした」。印が0個 → 「{{印}} がありません」。根拠が無い → 同上のエラー。それ以外は `_generate_document` と同じ処理。
  - 根拠集め（`_collect_evidence` と同じ）: 社内資料は質問そのままで `search(..., rerank=corpora.rerank_callable())`。技術ドキュメントは件数0なら info を出して飛ばし、そうでなければ `query_translation.topic_query(question, ask)` の英訳で `search(..., rerank_floor=DOCS_RERANK_FLOOR)`。`EmbeddingError` / `ChatError` はエラー結果。添付は一時フォルダに書いて `services.parse(path, caption_image=...)`（`caption_image_or_reason` の理由は warnings へ）。
  - 雛形なしで出力形式がソースコードなら `docgen.source_code.write_source` → `build`、そうでなければ `freeform.write_markdown` → `markdown_document.parse` + `References` → `build`。
  - `PromptTooLongError` / `ChatError` はエラー結果。想定外の例外はエラー結果「文書を生成できませんでした: {error}」にして `logger.exception` で残す（ジョブを failed にしない）。

- [ ] **Step 1: Write the failing tests**（偽の `Services`：`ask_json` / `ask_text` / `search` / `parse` / `check_vlm` を記録付きの偽物に。雛形は `tmp_path` の templates_dir に `.md` の雛形を置く（`docgen.md_template` が扱える形）。DB は段階①のテストと同じく `tmp_path` の本物の sqlite で、後始末で閉じる）

```python
def test_template_fills_and_offers_download(): ...                 # errors==[]、download.file_name=="議事録_2026-11-22.md"、base64 を戻すと値が入っている
def test_unfilled_marks_are_listed_as_warning(): ...                # warnings に「埋まらなかった欄: 」
def test_generation_uses_larger_context_size(): ...                 # ask_json / ask_text に num_ctx=GENERATION_NUM_CTX
def test_docs_not_searched_unless_requested(): ...                  # use_docs=False なら docs の search も topic_query も呼ばれない
def test_empty_docs_corpus_is_skipped_with_info(): ...              # infos == ["技術ドキュメントが取り込まれていません。この検索は飛ばしました。"]
def test_broken_template_reports_error(): ...                        # placeholders が例外 → errors[0] が「x.md を開けませんでした: 」で始まる
def test_template_without_marks_stops(): ...                         # 「に {{印}} がありません。埋める欄が無いため生成しません。」
def test_no_evidence_stops_without_calling_llm(): ...               # 雛形あり・なしの両方。ask_* は呼ばれない
def test_whitespace_only_paste_is_not_evidence(): ...
def test_pasted_text_alone_is_enough_and_named_in_references(): ... # 雛形なし .md の本文に「貼り付けたテキスト」
def test_broken_attachment_stops_with_error(): ...                  # parse が例外 → 「a.pdf を開けませんでした: 」
def test_empty_attachment_warns_and_continues(): ...                # parse が空 → warnings に「a.pdf から本文を取り出せませんでした。」、download あり
def test_attachments_are_captioned_through_vlm(): ...               # parse に caption_image=services.caption_image が渡る。check_vlm 失敗時は None と理由の warning
def test_freeform_markdown_output(): ...                            # 雛形なし .docx → file_name "文書_2026-11-22.docx"
def test_source_code_output_skips_markdown_path(): ...              # .go → write_source 経由、markdown_document.build は呼ばれない
def test_diagram_failures_are_reported(): ...                        # on_diagram_error が呼ばれたら warnings に「図にできなかった」
def test_search_error_becomes_result_error(): ...                   # search が EmbeddingError → errors に文言、download None
def test_unexpected_error_becomes_result_error_and_is_logged(caplog): ...
def test_sources_carry_hit_views(): ...                             # sources == [{"kind": "社内資料", "hits": [hit_view(hit)]}]
def test_output_suffixes_lists_documents_then_sources(): ...
```

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -W error -m pytest tests/test_web_cowork.py -v` → FAIL（モジュールが無い）
- [ ] **Step 3: Implement** `services.py` の追加と `rag_web/cowork.py`
- [ ] **Step 4: Run** → PASS。全体 `.\rag_env\Scripts\python.exe -m pytest -q` も失敗 0
- [ ] **Step 5: Commit** `feat: port Cowork document generation to rag_web`

### Task 2: Cowork とフォーマットファイルの API

**Files:**
- Modify: `rag_web/app.py`
- Test: `tests/test_web_app.py`（Cowork と雛形のテストを追加。肥大化するなら `tests/test_web_app_cowork.py` に分けてよい）

**Interfaces:**
- Consumes: Task 1 の `CoworkRequest` / `generate_cowork` / `output_suffixes` / `is_source_suffix` / `NO_TEMPLATE_LABEL`、段階①の `JobStore`・ファイル名検査・`CHAT_MODEL`
- Produces（Task 3 が使う）:
  - `GET /api/cowork/options` → `{"no_template_label": NO_TEMPLATE_LABEL, "output_suffixes": [...], "source_suffixes": [...], "attachment_suffixes": [...SUPPORTED_SUFFIXES 昇順], "template_suffixes": [...docgen.SUPPORTED_SUFFIXES 昇順], "templates": [{"name", "placeholders": [str] | None, "error": str | None}]}`
  - `POST /api/cowork` 本文 `{question (1〜100_000字), template: str|null, output_suffix: str, use_internal: bool, use_docs: bool, pasted: str (≤100_000字), attachments: [{name, content_b64}] (≤20件)}` → ジョブ（共有ロックなし）`{"job_id"}`。結果は Task 1 の dict。`output_suffix` が `output_suffixes()` に無ければ 422。`template` が一覧に無ければ 400「そのフォーマットファイルはありません」。添付の名前・拡張子・合計サイズは `/api/uploads` と同じ検査（413 を含む）。
  - `GET /api/templates` → `{"caption": <注意書き>, "items": [{"name", "placeholders", "error"}]}`（options の templates と同じ組み立てを共有する）
  - `POST /api/templates` 本文 `{files: [{name, content_b64}]}`（1〜20件、拡張子は docgen.SUPPORTED_SUFFIXES）→ `shared_operation()` の下で一時フォルダに書いて `docgen.templates.register(path, directory=services.templates_dir)`。`{"registered": n}`。
  - `DELETE /api/templates/{name}` → `shared_operation()` の下で `docgen.templates.remove(name, directory=...)`。`ValueError`（名前の検査）は 400、無ければ 404「そのフォーマットファイルはありません」。

- [ ] **Step 1: Write the failing tests**

```python
def test_cowork_options_lists_formats_and_templates(): ...
def test_cowork_runs_as_job_and_returns_download(): ...           # wait_finished → result.download.file_name
def test_cowork_requires_csrf_header(): ...
def test_cowork_rejects_unknown_output_suffix_and_template(): ...  # 422 / 400、生成は呼ばれない
def test_cowork_attachment_checks_match_uploads(): ...              # "../a.pdf"→400、未対応拡張子→400、合計超過→413
def test_cowork_is_not_blocked_by_shared_operation(): ...          # 取り込みジョブを止めた状態でも 200 で job_id
def test_cowork_template_removed_during_generation_reports_error(): ...  # 生成開始後に雛形を消す → result.errors[0] が「開けませんでした」
def test_template_register_and_list(): ...                           # 印の一覧が返る
def test_template_register_rejects_unsafe_names(): ...               # "../x.md"、"a/b.md"、"x.exe" → 400、templates_dir の外に何も無い
def test_template_delete_rejects_unsafe_names(): ...                 # 400、ファイルは残る
def test_template_delete_unknown_returns_404(): ...
def test_template_operations_return_409_while_shared_busy(): ...
def test_broken_template_is_listed_with_error(): ...                 # placeholders None、error に理由
```

- [ ] **Step 2: Run** `.\rag_env\Scripts\python.exe -W error -m pytest tests/test_web_app.py -v`（または分けたファイル）→ FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** → PASS、全体も失敗 0
- [ ] **Step 5: Commit** `feat: add Cowork and template API routes`

### Task 3: Cowork の画面

**Files:**
- Modify: `rag_web/static/index.html`、`rag_web/static/app.js`、`README.md`（新しい画面の節に「Cowork も使える」を1文）
- Test: `tests/test_web_app.py` に1件

**Interfaces:**
- Consumes: Task 2 の API。拡張子・出力形式・雛形なしのラベルはすべて `/api/cowork/options` から取る（app.js に写しを持たない）。

振る舞い（今の Streamlit と同じにするもの）:
- チャット入力欄の上に「チャット / Cowork」の切り替え。既定はチャット。Cowork の依頼はチャット履歴に積まない。
- Cowork パネル: フォーマットファイル（先頭が `（フォーマットファイルなし）`）、右に「フォーマットファイルを登録・削除」（雛形0件でも出す）。雛形なしのときだけ「出力形式」。
- 「社内資料を参照」（既定オン）「技術ドキュメントを参照」（既定オフ）。出力形式がソースコードの拡張子に**変わったとき**だけ技術ドキュメントを自動でオン、文書形式に戻ったらオフ（変わらない回は利用者の操作を上書きしない）。
- 添付（複数、accept は attachment_suffixes）と貼り付け欄。依頼文は既存の入力欄から送る。
- 送信 → `/api/cowork` → `/api/jobs/{id}` を2秒おきにポーリング（段階①の runJob を使う）。結果: errors は赤、warnings は折りたたみ「補足（N件）」の中に普通の文、infos は情報表示、download があれば「ダウンロード」ボタン（base64 → Blob → `a[download]`。CSP の範囲で動くこと）、sources は種類名の見出し＋段階①と同じ「参考にした情報（N件）」。
- 生成中は Cowork の入力・モード切り替え・送信を無効化し、done / failed / 通信断のどれでも戻す。
- 「フォーマットファイルを登録・削除」は `<dialog>`: 注意書き、複数ファイル選択（accept は template_suffixes）、登録、一覧（`名前 — 印 N個: a、b` か `名前 — 開けません: 理由`）と削除。409 / 400 / 404 は detail を表示。
- 出力された文言はすべて textContent で入れる（雛形名・印・理由も信頼しない入力）。

- [ ] **Step 1: Write the failing test**

```python
def test_index_has_cowork_controls():
    # index.html に mode 切り替え、cowork パネル、template ダイアログの要素 id がある
```

- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** `index.html` / `app.js` / README の1文
- [ ] **Step 4: Run** `.\rag_env\Scripts\python.exe -W error -m pytest tests/test_web_*.py -q` → PASS、全体も失敗 0、`node --check rag_web/static/app.js`
- [ ] **Step 5: ブラウザで確認**（段階①と同じく偽の Services で起動し Playwright で操作。結果を PR 本文に書く）:
  - 雛形あり（`.md` 雛形）で生成 → ダウンロードしたファイル名と中身
  - 雛形なしで `.docx` と `.go` を生成。`.go` を選ぶと「技術ドキュメントを参照」がオン、`.md` に戻すとオフ、`.go` のまま手でオフにしたら次の生成でもオフのまま
  - 根拠なし（両方オフ・添付なし・貼り付け空）で日本語のエラー
  - 貼り付けだけで生成できる
  - 補足が折りたたみの中に出る
  - 雛形の登録・削除、壊れた雛形が「開けません」で一覧に出て削除できる
  - 生成中にモード切り替えと送信が無効、終わったら戻る
  - 2タブ: 片方で取り込み中でももう片方の Cowork 生成は動く、雛形の登録は 409
  - 外部へのリクエストが0件（network requests）
- [ ] **Step 6: Commit** `feat: add Cowork panel and template dialog to the browser UI`
