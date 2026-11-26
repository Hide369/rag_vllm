# search_tech_docs（技術ドキュメントDBを MCP から検索する）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GB10 の MCP サーバ（`scripts/rag_mcp_server.py`、`rag-mcp.service`、外からは Nginx の `/mcp`）に `search_tech_docs` を足し、Claude Code が context7 の代わりに GB10 の技術ドキュメントDBを引けるようにする。

**Architecture:** コーパスごとの違い（DB・リランカーの床・見出し・0件の文言・空DBの文言）を `_Corpus` という値1つにまとめ、検索の本体 `_search` は2つのツールで共通にする。ストアとBM25索引はコーパスごとの `_State` に持ち、リランカーの準備済みフラグはプロセスで1つにする。`search_tech_docs` は入口で日本語の検索語をはね、説明文は起動時に `docs_sources.toml` から組み立てる。

**Tech Stack:** Python 3.13（`rag_env`）、mcp 2.2.0（`MCPServer`）、pytest、Windows PowerShell（開発機）、GB10 は systemd + Nginx + vLLM（Docker compose）

**Spec:** [docs/superpowers/specs/2026-11-24-tech-docs-mcp-tool-design.md](../specs/2026-11-24-tech-docs-mcp-tool-design.md)

## Global Constraints

- **対象のクライアントは Claude Code だけ。** Codex 向けの構成・文書（`docs/mcp-server-network.md`・`docs/mcp-server.md`）と `AGENTS.md` は考慮せず、変更もしない（2026-11-26 利用者の指示）
- **配置先は GB10**（RAG も MCP も vLLM の上に組む。2026-11-26 利用者の指示）。手順書は `docs/server-deployment.md`・`docs/claude-code-gb10.md`・`deploy/gb10/` を更新する
- `search_documents` の入出力は1文字も変えない。既存テストの**テスト関数**は変更せずにすべて通す（autouse フィクスチャ `_fresh_state` に新しい状態のリセットを足すのは可。理由は Task 2）
- 社内資料の結果・文言に技術DB向けの語（`技術ドキュメント`・`context7`）を入れない。技術DBの結果・文言に社内資料向けの語（`社内固有`・`社内資料`）を入れない（ツールの**戻り値**の決まりである。`search_tech_docs` の説明文が「社内資料は `search_documents` を使う」と案内するのは構わない）
- DBのパスは `store.DB_PATH` / `store.DOCS_DB_PATH` だけを使う。引数や環境変数で受け取らない
- DBを開く関数は `store.open_store`（埋め込みモデルの一致まで確かめる版）を使う。`vector_store.open_store` を直接呼ばない
- 技術DBの検索は `rerank_floor=DOCS_RERANK_FLOOR`（`ingest/retrieval.py`、値 `1.0`）を渡す。社内資料側は床を効かせない
- 検索は両ツールとも同じ `_search_lock` で直列化する
- `version = "0.0.0"` のソースは説明文に版を出さない
- テストの実行は `.\rag_env\Scripts\python.exe -m pytest ...`。合格の報告は、出力が成功で失敗0件のときだけ
- ブランチ `feat/search-tech-docs` を **master** から切って作業する。コミットはコンベンショナルコミット形式・英語
- **並行している `feat/docs-corpus-admin-only` と同じ行に触る。** あちらは `rag_web/app.py` の `docs_ingest_command` の返し方を変え、こちらの Task 1 は同じファイルの `DOCS_INGEST_COMMAND` の定義を変える。先にマージされた側に合わせて、後の側を rebase する

## Review Focus

1. **全角の記号だけを含む英語の検索語**（`How do I cache data in Streamlit？`、全角スペース）は日本語とみなさず検索する。IME で `？` が混ざるのはよくあり、はねるとモデルが同じ語で呼び直し続ける → Task 3 にテスト
2. **日本語の検索語は、他の人の検索が走っていても待たずに返る。** はね返すだけの応答がロック待ちで数秒〜十数秒遅れる理由が無い → Task 3 にテスト
3. **`search_tech_docs` と `search_documents` を同時に呼んでも重ならない。** 別のロックにすると、共有するリランカーの遅延初期化が守られない → Task 2 にテスト
4. **技術DBが空のときの文言に `fetch_docs` を出さない。** オフラインの GB10 では実行できず、`docs_source/` は git で追跡済みなので不要でもある → Task 2 にテスト
5. **技術DBの埋め込みモデルが設定と食い違っていたら、理由を返してDBを抱えたままにしない。** Ollama 版から持ち込んだ `docs_store.sqlite3` が `VLLM_EMBED_MODEL` と合わないのは移行で起こりうる。開いたまま抱えると、DBを入れ直してもサービスを再起動するまで直らない → Task 2 にテスト

---

### Task 1: 技術DBの取り込みコマンドを `ingest/store.py` に一本化する

設計書6節。今は同じ文字列が `rag_web/app.py:66-70` と `scripts/check_retrieval.py:315-319` に二重にある。MCP サーバは `rag_web.app`（FastAPI の画面一式）を import すべきでない。3か所目を増やさず、`store` に移して全員がそこから使う。

**Files:**
- Modify: `ingest/store.py`（`DOCS_DB_PATH` の直後に定数2つ）
- Modify: `rag_web/app.py:65-70`（自前の定義を消し、`DOCS_INGEST_COMMAND = store.DOCS_INGEST_COMMAND`。`tests/test_web_app.py` が `rag_web.app.DOCS_INGEST_COMMAND` を import しているので名前は残す）
- Modify: `scripts/check_retrieval.py:315-319`（同上）
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `store.DOCS_INGEST_SOURCE_COMMAND: str` = `"python -m scripts.ingest_source --source-dir docs_source --db docs_store.sqlite3 --keep-code-blocks"`（`docs_store.sqlite3` の部分は `DOCS_DB_FILENAME` から組む）
  - `store.DOCS_INGEST_COMMAND: str` = `"python -m scripts.fetch_docs のあと " + DOCS_INGEST_SOURCE_COMMAND`（今の文字列と1文字も変えない）

- [ ] **Step 1: ブランチを切る**

Run: `git switch master; git pull; git switch -c feat/search-tech-docs`
（作業ツリーに `feat/docs-corpus-admin-only` の未追跡の設計書が残っていても、切り替えの妨げにはならない）

- [ ] **Step 2: 失敗するテストを書く**（`tests/test_store.py`）

```python
def test_the_docs_ingest_command_is_defined_once():
    from ingest import store
    from rag_web import app
    from scripts import check_retrieval

    assert store.DOCS_INGEST_COMMAND == (
        "python -m scripts.fetch_docs のあと "
        "python -m scripts.ingest_source --source-dir docs_source "
        "--db docs_store.sqlite3 --keep-code-blocks"
    )
    assert store.DOCS_INGEST_COMMAND.endswith(store.DOCS_INGEST_SOURCE_COMMAND)
    assert store.DOCS_DB_FILENAME in store.DOCS_INGEST_SOURCE_COMMAND
    assert "fetch_docs" not in store.DOCS_INGEST_SOURCE_COMMAND
    assert app.DOCS_INGEST_COMMAND is store.DOCS_INGEST_COMMAND
    assert check_retrieval.DOCS.ingest_command is store.DOCS_INGEST_COMMAND
```

- [ ] **Step 3: 失敗を確かめる**

Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_store.py::test_the_docs_ingest_command_is_defined_once -v`
Expected: FAIL（`AttributeError: ... has no attribute 'DOCS_INGEST_COMMAND'`）

- [ ] **Step 4: 定数を `store` に移し、2か所を差し替える**

`rag_web/app.py` と `check_retrieval.py` に残っていた説明のコメントは `store` 側へ移す。そのあと次を実行する。

Run: `git grep -n "python -m scripts.fetch_docs のあと"`
Expected: `ingest/store.py` の1行だけ

- [ ] **Step 5: テストを通す**

Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_store.py tests/test_check_retrieval.py tests/test_web_app.py -q`
Expected: 全件 PASS

- [ ] **Step 6: コミット**

```bash
git add ingest/store.py rag_web/app.py scripts/check_retrieval.py tests/test_store.py
git commit -m "refactor: define the docs ingest command once in ingest.store"
```

---

### Task 2: コーパスを値にまとめ、`search_tech_docs` を足す

**Files:**
- Modify: `scripts/rag_mcp_server.py`
- Test: `tests/test_rag_mcp_server.py`

**Interfaces:**
- Consumes: `store.DOCS_INGEST_SOURCE_COMMAND`（Task 1）、`retrieval.DOCS_RERANK_FLOOR`、`store.EmbeddingMismatchError`（既存。`_search` が既に `EmbeddingError` と並べて拾っている）
- Produces（Task 3 が使う。名前は変えないこと）:
  - `@dataclass(frozen=True) class _Corpus`：`label: str`、`rerank_floor: float | None`、`no_hits: str`、`caution: str`、`empty_store: str`
  - `_INTERNAL: _Corpus`（`label="社内資料"`、`rerank_floor=None`、`no_hits=_NO_HITS`、`caution=_CAUTION`、`empty_store=` 今の空DBの文言そのまま）
  - `_TECH_DOCS: _Corpus`（`label="技術ドキュメント"`、`rerank_floor=DOCS_RERANK_FLOOR`、新しい文言3つ）
  - `format_results(hits: list, corpus: _Corpus = _INTERNAL) -> str`。見出しは `f"{corpus.label}から {len(hits)} 件が見つかりました。"`
  - `_State`：`store`・`index`・`index_revision` の3つ。**`reranker_ready` は消す**
  - モジュール変数 `_state`（社内資料）、`_docs_state`（技術DB）、`_reranker_ready: bool = False`（`_ensure_reranker` が `global` で更新）
  - `_open() -> VectorStore`（変更なし）、`_open_docs() -> VectorStore`（`store.open_store(str(store.DOCS_DB_PATH))`）
  - `_get_store(state: _State, opener: Callable[[], object])`。`opener()` が `EmbeddingMismatchError` を投げたら `state.store` は `None` のまま（今の `_get_store` と同じ振る舞い）
  - `_search(corpus: _Corpus, state: _State, opener: Callable[[], object], query: str, n_results: int) -> str`：`_search_lock` を握った状態で呼ぶ。`search(..., rerank_floor=corpus.rerank_floor)` を渡す。拾う例外は今と同じ `(EmbeddingError, store.EmbeddingMismatchError)` だけ
  - `search_tech_docs(query: str, n_results: int = SEARCH_RESULT_COUNT) -> str`（`@mcp.tool()`。この Task では docstring を説明文とし、Task 3 で差し替える）
  - `search_documents` は `_search(_INTERNAL, _state, _open, query, n_results)` を呼ぶだけにする。`_state` と `_open` は**呼び出し時に**モジュールから引くこと（既存テストが `monkeypatch.setattr(rag_mcp_server, "_open", ...)` で差し替えるため、`_Corpus` に関数を持たせて import 時に束縛してはいけない）
  - `main()` の `reject_legacy_env()` / `load_vllm_settings()` の確認はそのまま残す

**技術DBの文言（設計書4.4）。** 言い回しは任せる。次の語を含めること（テストで固定する）。

| 定数 | 含める語 | 含めない語 |
| --- | --- | --- |
| `_DOCS_FALLBACK` | `context7`、`ウェブ検索`、`補おうとせず`、`一般的な知識`、`技術ドキュメントDBでは確認できなかった`、`バージョン`、`推測` | `社内固有`、`社内資料` |
| `_DOCS_NO_HITS` | `技術ドキュメントを検索しましたが`、`見つかりませんでした`、続けて `_DOCS_FALLBACK` | 同上 |
| `_DOCS_CAUTION` | `---`、`関連しない場合は根拠に使わず`、続けて `_DOCS_FALLBACK` | 同上 |
| `_DOCS_EMPTY_STORE` | `技術ドキュメントのDBが空です`、`store.DOCS_INGEST_SOURCE_COMMAND` | `fetch_docs` |

`_DOCS_FALLBACK` は社内資料の `_FALLBACK` と同じ型にする。一般知識で答えてよいことを先に言い、補う探索を名指しで止める（`_FALLBACK` のコメントにある 2026-09-09 のウェブ検索ループの理由がそのまま当てはまる）。

- [ ] **Step 1: autouse フィクスチャを広げる**

`_fresh_state` に2行足す。docstring に「リランカーの準備済みフラグはプロセスで1つなので `_State` の外にある。これもテストごとに戻す」と1文足す。

```python
    monkeypatch.setattr(rag_mcp_server, "_docs_state", rag_mcp_server._State())
    monkeypatch.setattr(rag_mcp_server, "_reranker_ready", False)
```

- [ ] **Step 2: 失敗するテストを書く**

共通の下ごしらえとして、テストファイルにヘルパを1つ足す。

```python
_ENGLISH = "How do I cache data in Streamlit?"

def _wire(monkeypatch, search_fn, internal=None, docs=None, rerank=None):
    monkeypatch.setattr(rag_mcp_server, "_open", lambda: internal or _FakeCollection())
    monkeypatch.setattr(rag_mcp_server, "_open_docs", lambda: docs or _FakeCollection())
    monkeypatch.setattr(rag_mcp_server, "build_index", lambda collection: "索引")
    monkeypatch.setattr(rag_mcp_server, "_ensure_reranker", lambda: rerank)
    monkeypatch.setattr(rag_mcp_server, "search", search_fn)
```

テストの名前と表明は次のとおり。

- `test_each_tool_opens_its_own_db`：`rag_mcp_server.store.open_store` を「渡されたパスを記録して `_FakeCollection()` を返す」ものに差し替える（`_open` / `_open_docs` は差し替えない。`build_index`・`_ensure_reranker`・`search` は差し替える）。`search_documents("質問")` → `search_tech_docs(_ENGLISH)` の順に呼ぶ。`opened == [str(store.DB_PATH), str(store.DOCS_DB_PATH)]`
- `test_only_the_tech_docs_tool_applies_the_rerank_floor`：`search` が `kwargs.get("rerank_floor")` を記録する。両ツールを呼んで `floors == [None, DOCS_RERANK_FLOOR]`
- `test_every_argument_is_wired_through_for_tech_docs`：既存の `test_every_argument_search_needs_is_wired_through` と同じ表明を `search_tech_docs(_ENGLISH, n_results=2)` で行う（query・index・rerank・n_results）
- `test_tech_docs_results_carry_no_internal_wording`：`search` → `[_hit()]`。`"技術ドキュメントから 1 件が見つかりました。" in text`、`"context7" in text`、`"社内固有" not in text`、`"社内資料" not in text`
- `test_internal_results_carry_no_tech_docs_wording`：`search` → `[_hit()]` と `[]` の2通りで `search_documents` を呼び、どちらも `"技術ドキュメント" not in text` かつ `"context7" not in text`
- `test_tech_docs_no_hits_forbids_context7_and_web_search`：`search` → `[]`。上の表の `_DOCS_NO_HITS` の「含める語」をすべて含み、`"社内固有" not in text`
- `test_an_empty_tech_docs_db_shows_only_the_ingest_source_command`：`docs` に `count()` が0の `_FakeCollection` の子クラスを渡す。`store.DOCS_INGEST_SOURCE_COMMAND in text`、`"fetch_docs" not in text`（Review Focus 4）
- `test_a_mismatched_docs_db_returns_the_reason`：既存の `test_a_mismatched_db_returns_the_reason_to_the_user` と同じ仕掛けで、`tmp_path` に `qwen3-embedding`・`2560` を記録したDBを作り、`monkeypatch.setattr(store, "DOCS_DB_PATH", path)`。`search_tech_docs(_ENGLISH)` の戻り値に `"qwen3-embedding"` と `"VLLM_EMBED_MODEL"`、`rag_mcp_server._docs_state.store is None`（Review Focus 5）
- `test_a_revision_change_in_one_corpus_does_not_rebuild_the_other`：`internal` と `docs` に別々の `_FakeCollection` を渡し、`build_index` を「渡された collection を記録する」ものに差し替える。両ツール → `internal.bump()` → 両ツールの順に呼ぶ。`built == [internal, docs, internal]`
- `test_the_reranker_is_checked_once_across_both_tools`：`_ensure_reranker` は差し替えず、`rag_mcp_server.reranker.check_reranker` を回数を数えるものにする。`search_tech_docs(_ENGLISH)` → `search_documents("質問")`。`calls == 1`
- `test_the_two_tools_never_search_at_the_same_time`：既存の `test_two_searches_never_run_at_the_same_time` と同じ仕掛けで、スレッド4本のうち2本を `search_documents("質問")`、2本を `search_tech_docs(_ENGLISH)` にする。`max(peak) == 1`（Review Focus 3）

- [ ] **Step 3: 失敗を確かめる**

Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_rag_mcp_server.py -q`
Expected: 全件が FAIL か ERROR。フィクスチャの `setattr` が、まだ無い `_docs_state` で落ちるため。それで正しい

- [ ] **Step 4: 実装する**

インタフェースの表のとおりに組み替える。`_ensure_reranker` は `_state.reranker_ready` ではなく `_reranker_ready` を見る。その docstring に「モデルは同じなので、コーパスが2つでも確かめるのは1回」と1文足す。`_State` の docstring の「Codex がセッションごとに起こす」は今の構成（GB10 の `rag-mcp.service` で常駐し、Claude Code から引く）に合わせて直す。他の docstring・コメントの Codex への言及は書き換えない（今回の変更の外）。

- [ ] **Step 5: テストを通す**

Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_rag_mcp_server.py -q`
Expected: 全件 PASS。既存のテスト関数は1つも書き換えていないこと。`git diff tests/test_rag_mcp_server.py` で、既存部分の変更がフィクスチャの2行と docstring だけであることを見る

- [ ] **Step 6: コミット**

```bash
git add scripts/rag_mcp_server.py tests/test_rag_mcp_server.py
git commit -m "feat: add search_tech_docs MCP tool over the docs store"
```

---

### Task 3: 日本語の検索語をはね、説明文を `docs_sources.toml` から組む

設計書4.3と4.5。

**Files:**
- Modify: `scripts/rag_mcp_server.py`
- Test: `tests/test_rag_mcp_server.py`

**Interfaces:**
- Consumes: `search_tech_docs`・`_search_lock`・`_wire`（Task 2）、`scripts.fetch_docs.load_sources() -> list`（各要素は `name: str` と `version: str` を持つ）
- Produces:
  - `_JAPANESE: re.Pattern`：ひらがな `぀-ゟ`、カタカナ `゠-ヿ`、CJK統合漢字拡張A `㐀-䶿`、CJK統合漢字 `一-鿿`、半角カタカナ `ｦ-ﾟ`。**`　-〿`（全角スペース・句読点）と `！-～`（全角英数・記号）は含めない**（Review Focus 1）
  - `_ASK_FOR_ENGLISH: str`：検索していないこと、技術ドキュメントは英語であること、英語の疑問文に書き直して `search_tech_docs` を呼び直すことを言う。例を1つ（`How do I cache data in Streamlit?`）載せる。`見つかりませんでした` と `0件` は**含めない**（0件の文言は「再検索しない」と言うため、混ぜると呼び直さなくなる）
  - `_tech_docs_description(sources: list) -> str`
  - `search_tech_docs` の入口：`_JAPANESE.search(query)` が当たれば、**`_search_lock` を取る前に** `_ASK_FOR_ENGLISH` を返す
  - 登録：`@mcp.tool(description=_tech_docs_description(load_sources()))`。`load_sources()` はモジュールの読み込み時に1回だけ呼ぶ。読めなければ例外のまま起動を止める（握りつぶして一覧の無い説明文で起動すると、モデルは収録範囲が分からないまま呼び続ける。`rag-mcp.service` は `Restart=on-failure` なので、`journalctl -u rag-mcp` に例外が残る）。`scripts.fetch_docs` は import 時に通信しない（`requests` を読むだけ）ので、MCP サーバから import してよい

**説明文の中身（設計書4.3〜4.5）。** 言い回しは任せる。次の要素を入れる。

- 何を検索するか：OSS ライブラリ・言語仕様の公式ドキュメント（英語）。社内資料は `search_documents` を使うこと
- 収録ライブラリの一覧：`name version` を `, ` 区切りで並べる。`version == "0.0.0"` なら名前だけ
- 検索語は**英語の疑問文**で書くこと。良い例 `How do I cache data in Streamlit?`、悪い例 `Streamlit キャッシュ` または `Write a caching function`（依頼文はリランカーの床で落ちる）
- 日本語を含む検索語は検索しないこと
- 一覧に無いライブラリでは呼ばなくてよいこと

- [ ] **Step 1: 失敗するテストを書く**

- `test_a_japanese_query_is_not_searched`：`@pytest.mark.parametrize` で `"きゃっしゅ"`、`"キャッシュ"`、`"設定"`、`"ｷｬｯｼｭ"`、`"How do I use 設定 in Streamlit?"`。`search` を呼ばれたら記録するものにし、`calls == []`、`"英語" in text`、`"search_tech_docs" in text`、`"見つかりませんでした" not in text`
- `test_full_width_punctuation_is_not_treated_as_japanese`：parametrize で `"How do I cache data in Streamlit？"`、`"How do I use pymupdf！"`、`"How　do I cache data in Streamlit?"`。`search` が1回呼ばれる（Review Focus 1）
- `test_a_japanese_query_does_not_wait_for_a_running_search`：テスト側で `rag_mcp_server._search_lock.acquire()` してから、別スレッドで `search_tech_docs("キャッシュ")` を走らせ、`thread.join(timeout=1)` のあと `not thread.is_alive()` と戻り値に `"英語"` を含むことを見る。`finally` で `release()`（Review Focus 2）
- `test_the_description_lists_each_library`：`types.SimpleNamespace(name="streamlit", version="1.61.1")` と `SimpleNamespace(name="ollama", version="0.0.0")` を渡す。`"streamlit 1.61.1" in d`、`"ollama" in d`、`"0.0.0" not in d`、`"How do I cache data in Streamlit?" in d`、`"search_documents" in d`
- `test_the_registered_tool_lists_the_configured_libraries`：`asyncio.run(rag_mcp_server.mcp.list_tools())` から名前 `search_tech_docs` のツールを取り、`load_sources()` の各 `name` がその `description` に含まれる
- `test_search_documents_keeps_its_description`：同じ一覧から名前 `search_documents` のツールを取り、`description` が `"社内資料を検索し、根拠となる原文を出典つきで返す。"` で始まり、`"技術ドキュメント"` を含まない

- [ ] **Step 2: 失敗を確かめる**

Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_rag_mcp_server.py -q`
Expected: 上の6種が FAIL。Task 2 までのテストは PASS

- [ ] **Step 3: 実装する**

インタフェースの表のとおり。

- [ ] **Step 4: テストを通す**

Run: `.\rag_env\Scripts\python.exe -m pytest tests/test_rag_mcp_server.py -q`
Expected: 全件 PASS

- [ ] **Step 5: サーバが起動し、2つのツールを公開することを開発機で見る**

PowerShell を2つ使う。1つ目で起動する（埋め込みの vLLM が無くても、起動と一覧までは通る）。

```powershell
.\rag_env\Scripts\python.exe -m scripts.rag_mcp_server --http --host 127.0.0.1 --port 8765
```

2つ目で握手してから一覧を取る。

```powershell
$h = @{ Accept = "application/json, text/event-stream" }
$init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
$r = Invoke-WebRequest -Uri http://127.0.0.1:8765/mcp -Method Post -Body $init -ContentType "application/json" -Headers $h
$h["Mcp-Session-Id"] = $r.Headers["Mcp-Session-Id"]
Invoke-WebRequest -Uri http://127.0.0.1:8765/mcp -Method Post -ContentType "application/json" -Headers $h -Body '{"jsonrpc":"2.0","method":"notifications/initialized"}' | Out-Null
(Invoke-WebRequest -Uri http://127.0.0.1:8765/mcp -Method Post -ContentType "application/json" -Headers $h -Body '{"jsonrpc":"2.0","id":2,"method":"tools/list"}').Content
```

Expected: 1つ目の応答の `serverInfo` に `local_docs`、最後の応答に `search_documents` と `search_tech_docs`、`search_tech_docs` の `description` に `streamlit 1.61.1`。確認後 Ctrl+C

- [ ] **Step 6: コミット**

```bash
git add scripts/rag_mcp_server.py tests/test_rag_mcp_server.py
git commit -m "feat: reject Japanese queries and list indexed libraries in search_tech_docs"
```

---

### Task 4: GB10 の配置手順と Claude Code の手順書を更新する

設計書4.6〜4.8。

**Files:**
- Modify: `docs/server-deployment.md`
- Modify: `deploy/gb10/rag-mcp.service`（先頭のコメントだけ）
- Modify: `docs/claude-code-gb10.md`
- Modify: `docs/処理箇所マップ.md`（8-1節）
- Modify: `.gitignore`

**決めたこと（設計書に無い、または設計書から変えた点）**

- **リランカーのキャッシュは `rag` のホーム（`/opt/rag/.cache/huggingface`）に置き、`HF_HOME` は設定しない。** 取得する人もサービスも同じ `rag` ユーザーなので、既定の置き場所で食い違わない。`/srv/models/huggingface`（vLLM 用）に寄せると、`ProtectSystem=strict` の下で読み取り専用になる。`hf_hub_download` がそこへロックファイルを書こうとするかを確かめる手間が増えるだけなので、寄せない
- **`/opt/rag` はリポジトリの複製そのものなので、`.cache/` を `.gitignore` に足す。** 足さないと、GB10 の `git status` にモデルのキャッシュが未追跡として出続ける
- **context7 の拒否は `mcp__context7` と書く**（設計書は書き方を決めていない）。サーバ名だけの形が、そのサーバの全ツールに効く書き方として Claude Code の権限の文書に載っている。書く前に、使っている版の文書で確かめること
- **`ENABLE_TOOL_SEARCH: "false"` を足し、`claude-code-gb10.md` の「MCP tool search について」を直す。** 今の文面は「Anthropic 以外を指しているときは既定で切る」と書く。しかし Ollama 版の手順書（Hide369/local-llm-rag の `docs/mcp/mcp-server-11434-claude-code.md`「ツール検索を切る理由」）は、v2.1.304 からゲートウェイ越しでも既定で有効になったと記録している。推論エンジンが `defer_loading` を黙って無視すると、ツールの定義がモデルに届かない。2026-11-25 に Qwen3-Coder-Next でツールが毎回呼ばれたのは、この値を `false` にした構成である。ツールが2つに増えるので、ここで揃える

- [ ] **Step 1: リランカーがオフラインでキャッシュから読めることを開発機で確かめる**

Run: `$env:HF_HUB_OFFLINE = "1"; .\rag_env\Scripts\python.exe -c "from ingest import reranker; reranker.check_reranker(); print('ok')"`
Expected: `ok`（開発機にはモデルがキャッシュ済み）。この形の確認を、GB10 向けに手順書へ書く

- [ ] **Step 2: `docs/server-deployment.md` を更新する**

- 構成図・本文の MCP の説明（「社内資料の検索（MCP）」）に、技術ドキュメントの検索も同じ MCP が受け持つことを足す
- 手順3の `rag.env` に `HF_HUB_OFFLINE=1` を1行足す。下の箇条に、Web 画面と MCP のリランカーがキャッシュだけを見るようになること、先に手順4で取得しておくことを書く
- 手順4の最後の段落（「リランカー（…）は rag_web / MCP が初回の検索で確かめる。…」）を、明示的な取得手順に差し替える

```bash
sudo -u rag bash -c 'set -a; . /etc/rag/rag.env; set +a; cd /opt/rag && HF_HUB_OFFLINE=0 ./rag_env/bin/python -c "from ingest import reranker; reranker.check_reranker(); print(\"ok\")"'
sudo -u rag bash -c 'set -a; . /etc/rag/rag.env; set +a; cd /opt/rag && ./rag_env/bin/python -c "from ingest import reranker; reranker.check_reranker(); print(\"ok\")"'
```

  1行目が外に出て取得し、2行目が `HF_HUB_OFFLINE=1` のままキャッシュから読めることを確かめる。どちらも `ok` になれば正しい。置き場所は `/opt/rag/.cache/huggingface` で、vLLM のモデル（`/srv/models/huggingface`）とは別である、と書く。上の「決めたこと」の理由を1〜2文で添える
- 手順6に「技術ドキュメントDBを置く」小節を足す。[技術ドキュメントDBの移植手順.md](../技術ドキュメントDBの移植手順.md) で `docs_store.sqlite3` を `/opt/rag` に置く。持ち込んだ場合は、既にある「既存の DB を持ち込むとき」の `check_embedding_compat --db docs_store.sqlite3` を通す。無いと `search_tech_docs` は「技術ドキュメントのDBが空です」を返す
- 「受け入れ確認の項目」に足す
  - `sudo systemctl restart rag-mcp` の直後の最初の `search_tech_docs` が、`HF_HUB_OFFLINE=1` のまま Reranker のスコアつきで返る
  - `search_tech_docs` と `search_documents` の両方が Claude Code から呼べる（`docs/claude-code-gb10.md` 手順5）

- [ ] **Step 3: `deploy/gb10/rag-mcp.service` と `.gitignore` を更新する**

- `rag-mcp.service` の2行目のコメント「社内資料の検索（MCP の search_documents）を…」を、`search_documents` と `search_tech_docs`（技術ドキュメント）の両方を公開する旨に直す。設定行は変えない（`HF_HUB_OFFLINE` は `EnvironmentFile` の `rag.env` から入る）
- `.gitignore` に `.cache/` を足す。理由のコメントを1行添える（GB10 では `/opt/rag` が `rag` のホームで、リランカーのキャッシュがここに入る）

- [ ] **Step 4: `docs/claude-code-gb10.md` を更新する**

- 冒頭の説明と構成図の MCP の行に、技術ドキュメントの検索（`search_tech_docs`）を足す
- 手順3の `settings.json` の例に次を足し、表に3行足す（意味：検索のたびの許可ダイアログを出さない／context7 を呼ばせない／ツールの定義を毎回モデルに渡す）

```json
  "permissions": {
    "allow": ["mcp__local_docs__search_documents", "mcp__local_docs__search_tech_docs"],
    "deny": ["mcp__context7"]
  },
```

  `env` には `"ENABLE_TOOL_SEARCH": "false"` を足す
- 手順4に足す
  - context7 を外す：`claude mcp remove context7 --scope user`（登録していなければ「見つからない」と出るだけでよい）
  - `%USERPROFILE%\.claude\CLAUDE.md` に貼る文面。社内資料の1行（`- 社内資料の検索には mcp__local_docs__search_documents ツールを使う。`）と、「外部ドキュメントの参照」節を次の内容で**置き換える**文面を載せる（設計書4.7）
    - OSS ライブラリ・フレームワークの API や設定は `mcp__local_docs__search_tech_docs` で調べる。収録ライブラリはツールの説明にある
    - 検索語は英語の疑問文で書く（例：`How do I cache data in Streamlit?`）
    - 収録ライブラリを使うコードを書くときも、引数・既定値・版による違いに自信が無ければ、書く前に調べる
    - 0件・無関係なら context7 やウェブ検索で補わない。一般的な知識で答えてよいが、技術ドキュメントDBでは確認できなかったと断り、版に依存する記述は推測と明示する
  - `search_tech_docs` はコーディングのたびに毎回呼ばれるものではない、と1文（設計書4.7）
- 手順5の確かめる項目に足す
  - 「Streamlit でデータをキャッシュする方法は？」と聞く → `search_tech_docs` が英語の検索語で呼ばれ、出典つきで答える
  - ツール名を言わずに「Streamlit でCSVを読んで表にするページを書いて」と頼む（3回）→ 書く途中で自分から `search_tech_docs` を呼ぶ
  - 収録外のライブラリ（例：Django）の使い方を聞く → 呼ばないか、0件で打ち切り、「技術ドキュメントDBでは確認できなかった」と断って答える
  - 上の3つの間、`context7` が一度も呼ばれない
- 「MCP tool search について」を、上の「決めたこと」のとおりに書き直す（`false` にする理由と、Ollama 版の手順書への参照）
- 「うまくいかないとき」の表に行を足す
  - `search_tech_docs` が「技術ドキュメントのDBが空です」→ `server-deployment.md` 手順6の技術DBの小節
  - `search_tech_docs` が「この DB は … で作られています」→ 技術DBの埋め込みモデルが `VLLM_EMBED_MODEL` と違う。`server-deployment.md`「既存の DB を持ち込むとき」
  - `search_tech_docs` の結果が話題とずれる・件数が多すぎる → リランカーが使えていない。`server-deployment.md` 手順4の2行目（オフラインの確認）が `ok` になるか
  - 「英語で書き直して」が返り続ける → モデルが日本語のまま呼び直している。`CLAUDE.md` に「英語の疑問文」の行があるか
  - ツール名を挙げても `search_tech_docs` が一度も呼ばれない → `ENABLE_TOOL_SEARCH` が `false` か。VS Code を完全に再起動したか

- [ ] **Step 5: `処理箇所マップ.md` を更新する**

8-1節：`rag_mcp_server.py` の各行の行番号を実装後の位置に合わせて直す。`_Corpus` / `_INTERNAL` / `_TECH_DOCS`・`_DOCS_*` の文言・`_JAPANESE` / `_ASK_FOR_ENGLISH`・`_tech_docs_description`・`_open_docs`・`search_tech_docs` の行を足す。「`search_documents()` — 唯一のツール」の「唯一の」を消す。末尾の注記（`search_documents` のシグネチャ）に `search_tech_docs` を並べる。`_State` の行から「リランカーの準備状況」を外し、`_reranker_ready` の行を足す

- [ ] **Step 6: 表記と全テストを確かめる**

Run: `git grep -n "唯一のツール" -- docs/処理箇所マップ.md`
Expected: 出力なし

Run: `git diff --stat master -- AGENTS.md docs/mcp-server-network.md docs/mcp-server.md`
Expected: 出力なし（Claude Code だけが対象。Codex 向けの文書と AGENTS.md に触れていない）

Run: `.\rag_env\Scripts\python.exe -m pytest -q`
Expected: 全件 PASS、失敗0件

- [ ] **Step 7: コミット**

```bash
git add docs/server-deployment.md deploy/gb10/rag-mcp.service docs/claude-code-gb10.md docs/処理箇所マップ.md .gitignore
git commit -m "docs: document search_tech_docs on GB10, offline reranker and context7 removal"
```
