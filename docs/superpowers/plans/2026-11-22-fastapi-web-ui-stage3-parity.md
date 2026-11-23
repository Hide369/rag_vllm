# FastAPI 画面 段階③ 旧テストとの突き合わせ表

段階③ Task 2 で Streamlit 版（`rag_chat_app.py`）と旧テスト2本
（`tests/test_rag_chat_app.py` 49件、`tests/test_rag_chat_app_cowork.py` 30件）を消す前に、
旧テスト1件ごとに、同じ振る舞いが新しいテスト（`tests/test_web_*.py`）で押さえられているかを確かめた記録である。
設計書の要求「③で消すのは、同じ振る舞いが新しいテストで押さえられていることを確かめてからにする」に応える。

## 区分

- `covered`: 新しいテストが同じ振る舞いを確かめている。
- `added`: 穴だったので、この Task で新しいテストを足した。
- `ui-only`: 画面の部品の配置・開閉・描き分けだけを確かめている。新しい画面では `<dialog>` と JS（`rag_web/static/`）が担い、サーバーのテストでは扱えない。文言は `app.js` / `index.html` に旧画面のまま移してあることを確かめた。
- `obsolete`: Streamlit の仕組み（再実行・session_state・ウィジェットの無効化）にしか無い問題を確かめている。理由を書く。

## 件数

| 区分 | チャット（49件） | Cowork（30件） | 合計（79件） |
|---|---|---|---|
| covered | 24 | 20 | 44 |
| added | 9 | 1 | 10 |
| ui-only | 8 | 8 | 16 |
| obsolete | 8 | 1 | 9 |

`added` は10行だが、1本のテストが2行（技術ドキュメントのプロンプトと翻訳クエリ）を押さえるため、足したテストは9本である。末尾の「足したテスト」にまとめる。

略記: 新テストの `answering` は `tests/test_web_answering.py`、`app` は `tests/test_web_app.py`、
`cowork` は `tests/test_web_cowork.py`、`corpora` は `tests/test_web_corpora.py`。

## チャット（`tests/test_rag_chat_app.py`）

| 旧テスト | 確かめている振る舞い | 区分 | 新しいテスト / 理由 |
|---|---|---|---|
| test_search_failure_is_reported_instead_of_crashing | 埋め込みAPIが落ちても、トレースバックではなく短いメッセージを出す | covered | answering::test_embedding_error_yields_search_error_without_note |
| test_search_failure_does_not_also_claim_it_will_search | 条件抽出と検索が同じ原因で落ちたとき「通常の検索で回答します」を出さない | covered | answering::test_embedding_error_yields_search_error_without_note（note が出ず error だけになる） |
| test_a_follow_up_question_is_searched_with_the_previous_one | 追質問の検索クエリに直前の質問を継ぎ足す | covered | answering::test_history_is_used_for_query_and_messages |
| test_the_model_is_picked_from_the_pulled_models | pull 済みモデルのプルダウンから選ぶ | obsolete | PR #5 で利用者の依頼によりモデル選択を廃止し、モデルは `rag_web/app.py` の `CHAT_MODEL` に固定した。画面にプルダウンが無いことは app::test_index_has_no_model_picker |
| test_the_picked_model_is_the_one_that_generates | 選んだモデルが生成に渡る | obsolete | 同上。固定モデルで答え、クライアントが送ったモデル名は無視されることを app::test_chat_ignores_client_supplied_model が確かめる |
| test_products_that_fail_the_condition_are_shown_but_not_sent_to_the_model | 条件を満たさない機種は画面にだけ出し、モデルへ渡さない | covered | answering::test_catalog_path_shows_beyond_only_on_screen |
| test_no_quality_switches_are_left_to_the_reader | VLM・Reranker を利用者に切らせず常に効かせる | covered | answering::test_internal_search_streams_deltas_then_evidence_then_done（rerank を常に渡す）、app::test_ingest_runs_as_job_after_login（caption_image を常に渡す）。要求本体 `ChatBody` に切り替え用の項目は無い（app::test_chat_rejects_out_of_range_temperature_and_unknown_corpus が受け付ける項目の外を 422 にする） |
| test_ingesting_passes_the_captioner_without_being_asked | VLM が使えれば caption_image を取り込みへ渡す | covered | app::test_ingest_runs_as_job_after_login |
| test_ingesting_without_a_vlm_still_ingests_and_says_why | VLM が無くても取り込み、理由を伝える | covered | app::test_ingest_reports_vlm_unavailable_as_notice、corpora::test_caption_image_or_reason_reports_vlm_error |
| test_the_ingest_result_survives_the_rerun | 取り込みの要約が st.rerun() で消えない | obsolete | Streamlit の再実行で描画が捨てられる問題。新しい構成では結果をジョブとして保持し（app::test_ingest_runs_as_job_after_login、jobs::test_job_runs_and_result_is_returned_once）、JS が描くので再実行が無い |
| test_answer_is_shown_without_the_repeated_label | 「答え：」のラベルを落とし、中身は残す | covered | answering::test_answer_label_is_stripped |
| test_input_is_re_enabled_and_history_has_exactly_one_exchange_after_answering | 生成後に入力欄が戻り、履歴が二重に積まれない | obsolete | 入力欄の無効化と履歴の積み方は session_state の仕組みの話。サーバーは要求ごとに履歴を受け取るだけで履歴を持たず、積まれるのは JS の `state.history` 側。生成が1要求1回であることは app::test_chat_streams_ndjson_events（chat_calls が1回） |
| test_a_reranker_model_failure_says_what_happens_instead | リランカー取得に失敗したとき、並べ替えない旨を理由つきで伝える | covered | corpora::test_reranker_failure_retried_next_time（reason が「Rerankerで並べ替えません: …」）、app::test_status_reports_counts_and_reranker_state（status に載せる）。表示は app.js の `reranker-msg` |
| test_the_reranker_is_always_wired_into_search | search に常に rerank を渡す | covered | answering::test_internal_search_streams_deltas_then_evidence_then_done、answering::test_docs_corpus_translates_query_and_skips_condition_extraction |
| test_a_missing_reranker_model_does_not_stop_the_search | リランカーが無くても並べ替えずに検索する | added | answering::test_search_still_runs_without_reranking_when_the_reranker_is_unavailable（チャット経路。Cowork 経路は cowork::test_reranker_is_checked_once_per_generation_for_both_corpora が既に確かめていた） |
| test_the_caches_are_keyed_on_the_revision_not_the_chunk_count | BM25索引・属性一覧の鍵は revision であり、チャンク数ではない | covered | corpora::test_index_rebuilt_when_revision_changes、corpora::test_schema_follows_revision_and_is_not_shared_between_corpora |
| test_get_index_and_get_schema_are_keyed_on_the_corpus_not_revision_alone | revision が同じでも、コーパスが違えば結果を共有しない | covered | corpora::test_index_not_shared_between_corpora_with_equal_revision、corpora::test_schema_follows_revision_and_is_not_shared_between_corpora |
| test_the_upload_dialog_writes_the_file_and_ingests_it | アップロードの中身を一時ファイルに書いて取り込みへ渡す | covered | app::test_upload_runs_as_shared_job_and_lists_item（fake が一時ファイルの中身を読み、渡った内容と突き合わせる） |
| test_the_upload_dialog_does_not_ingest_when_nothing_is_selected | ファイル未選択では取り込みを呼ばない | covered | app::test_upload_requires_one_to_fifty_files（0件は 422 で取り込みを呼ばない）。「先にファイルを選んでください。」は app.js が旧文言のまま出す |
| test_the_upload_dialog_lists_only_the_uploaded_documents | source/ 由来の資料は一覧に出さない | covered | app::test_upload_runs_as_shared_job_and_lists_item（source_only.pdf が出ない） |
| test_the_upload_dialog_deletes_an_uploaded_document | 一覧の削除で DB から消える | covered | app::test_delete_upload_only_accepts_upload_keys |
| test_the_upload_dialog_reports_an_offline_ollama_without_ingesting | Ollama が落ちていれば取り込みを始めず短く伝える | covered | app::test_upload_returns_503_when_ollama_down、app::test_ingest_returns_503_when_ollama_down |
| test_an_uploaded_document_really_reaches_the_store | 解析・チャンク・格納まで本物を通し、DB に入って一覧に出る | added | app::test_uploaded_document_really_reaches_the_store（`ingest_uploads` を本物にし、埋め込みだけ固定ベクトル） |
| test_the_close_button_comes_before_the_uploaded_list | 閉じるボタンが一覧より前にある | ui-only | ダイアログ内の部品の並び順。新しい画面は `<dialog>`（index.html）で、閉じるボタンは一覧の外にある |
| test_the_upload_dialog_stays_closed_after_dismissing_it | 「×」で閉じたあと勝手に開き直さない | ui-only | session_state のフラグで開閉していた Streamlit 固有の問題。`<dialog>` は `close()` で閉じ、再実行が無いので開き直さない |
| test_the_uploaded_list_is_inside_a_fixed_height_container | 一覧を固定高さの入れ物に入れる | ui-only | 画面の見た目（CSS）だけの話 |
| test_no_container_is_drawn_when_nothing_was_uploaded | 0件のとき空の箱を描かない | ui-only | 画面の描き分けだけの話。0件のとき一覧が空で返ることは app::test_upload_runs_as_shared_job_and_lists_item |
| test_a_markdown_request_shows_the_notation_instead_of_rendering_it | 「マークダウンで表示して」は描画せず記法のまま出す | covered | answering::test_display_mode_keeps_html_and_reports_display（サーバーが `display` を返す）。コードブロックで出すのは app.js の `renderAnswer` |
| test_a_mermaid_request_shows_both_the_notation_and_the_diagram | マーメイドの依頼で記法と図の両方を出す | added | answering::test_mermaid_request_reports_display_and_keeps_html（サーバー側が `display: "mermaid"` を返す）。図の描画は app.js の `renderAnswer` |
| test_the_diagram_is_drawn_from_the_fence_only | 図はフェンスの中だけから描く | ui-only | 描画は app.js の `mermaidDefinitions`（`answer_text.mermaid_definitions` と同じ規則）が担う。JS の単体テストの仕組みは無く、サーバーのテストでは扱えない |
| test_a_bare_definition_is_still_drawn | フェンスの無い素の定義でも図を描く | ui-only | 同上（`mermaidDefinitions`） |
| test_a_markdown_request_draws_no_diagram | 図を出すのはマーメイドのときだけ | covered | answering::test_display_mode_keeps_html_and_reports_display（`display` が markdown）。JS は `display === "mermaid"` のときだけ図を描く |
| test_an_ordinary_question_still_renders_markdown | 通常の質問は従来どおり Markdown として描く | covered | answering::test_html_tags_are_stripped_without_display_mode、app::test_chat_streams_ndjson_events（`display` が None） |
| test_the_notation_survives_a_rerun | 再描画しても記法のまま残る | obsolete | 履歴の再描画で見え方が戻る問題は Streamlit の再実行にしか無い。新しい画面は描いた DOM をそのまま保つ |
| test_the_display_mode_does_not_carry_over_to_the_next_question | 表示指定を次の質問へ持ち越さない | added | answering::test_display_mode_is_decided_per_question |
| test_a_search_failure_is_not_shown_as_notation | 検索失敗の文を記法扱いにしない | added | answering::test_display_mode_is_not_set_when_the_search_fails（error だけを返し display が付かない） |
| test_the_corpus_switch_offers_both_choices | コーパスの切り替えに2つの選択肢がある | ui-only | ラジオの選択肢の配置。サーバー側の許容値は app::test_chat_rejects_out_of_range_temperature_and_unknown_corpus |
| test_choosing_the_documentation_corpus_searches_the_other_database | 技術ドキュメントを選ぶと別の DB を引く | covered | answering::test_docs_corpus_translates_query_and_skips_condition_extraction（`collection:docs`）、corpora::test_collection_opened_once_per_corpus |
| test_the_documentation_corpus_uses_the_documentation_prompt | 技術ドキュメントでは専用のプロンプトを使い、社内向けの歯止めを混ぜない | added | answering::test_docs_corpus_uses_docs_prompt_and_asks_with_the_original_question |
| test_the_documentation_corpus_searches_with_the_translated_query | 検索は英訳クエリ、生成は原文の質問 | added | 同上（検索は翻訳後、生成は原文）。検索側は answering::test_docs_corpus_translates_query_and_skips_condition_extraction も確かめる |
| test_the_internal_corpus_never_calls_translation | 社内資料では翻訳の LLM 呼び出しをしない | added | answering::test_internal_corpus_uses_internal_prompt_and_never_translates |
| test_the_internal_corpus_is_unchanged_by_the_switch | 社内資料の振る舞いは切り替えの導入前と同じ | covered | answering::test_internal_search_streams_deltas_then_evidence_then_done、answering::test_internal_corpus_uses_internal_prompt_and_never_translates（社内向けプロンプトで答える） |
| test_switching_to_the_empty_documentation_corpus_warns_with_the_cli_commands | 技術ドキュメントが0件なら CLI の2コマンドを警告で示す | added | app::test_status_of_an_empty_docs_corpus_carries_the_ingest_command（0件と2コマンドを status が返す）。警告文は app.js の `docs-empty`（旧文言のまま） |
| test_the_nonempty_documentation_corpus_does_not_warn | 中身があれば警告を出さず、従来のキャプションだけ出す | covered | app::test_status_reports_counts_and_reranker_state（docs の `chunk_count` が1）。出し分けは app.js |
| test_switching_corpus_clears_history_and_does_not_poison_the_next_query | 切り替えたら履歴を捨て、前コーパスの質問を次の検索に混ぜない | obsolete | 混入の原因は Streamlit が履歴を session_state に持ち回ることにある。新しい API は要求ごとにコーパスと履歴を送り、サーバーは送られた履歴だけを使う（answering::test_docs_corpus_translates_query_with_previous_question が翻訳へ渡るのは受け取った履歴だけであることを示す）。履歴を捨てるのは app.js の切り替え処理 |
| test_switching_corpus_with_no_prior_history_does_not_show_the_reset_notice | 履歴が無ければリセット通知を出さない | ui-only | 通知の出し分けは app.js（`state.history.length` で分岐）の話 |
| test_the_corpus_radio_is_disabled_while_generating | 生成中はコーパス切り替えを無効にする | obsolete | 生成中に切り替えると Streamlit が実行を打ち切り、状態が残る問題への手当て。新しい構成は切り替えても進行中の要求に影響しない（要求がコーパスと履歴を自分で持つ）。app.js も生成中は切り替えを受け付けない（`state.generating` で戻る） |
| test_switching_corpus_mid_generation_does_not_reprocess_the_stale_question | 生成中の切り替えで古い質問を新コーパスへ再検索しない | obsolete | 再検索の原因は `pending_question` が実行をまたいで残る仕組み。新しい構成では要求ごとにコーパスと質問が閉じており、処理し直す入口が無い（answering の各テストが要求1件で完結する） |
| test_only_the_documentation_corpus_applies_the_rerank_floor | rerank_floor は技術ドキュメントだけに効かせる | covered | answering::test_internal_search_streams_deltas_then_evidence_then_done（floor を渡さない）、answering::test_docs_corpus_translates_query_and_skips_condition_extraction（`DOCS_RERANK_FLOOR` を渡す） |

## Cowork（`tests/test_rag_chat_app_cowork.py`）

| 旧テスト | 確かめている振る舞い | 区分 | 新しいテスト / 理由 |
|---|---|---|---|
| test_the_mode_toggle_offers_both_modes | チャットと Cowork の切り替えがある | ui-only | 切り替えの配置。要素の存在は app::test_index_has_cowork_controls（`mode-chat` / `mode-cowork`） |
| test_cowork_without_any_template_does_not_tell_the_user_to_register_one | 雛形0件でも「登録されていません」の警告で行き止まりにしない | ui-only | 画面の出し分け。0件でも雛形なしの選択肢が API から返ることは app::test_cowork_options_lists_formats_and_templates |
| test_cowork_offers_the_register_button_before_any_template_exists | 雛形0件でも登録ボタンがある | ui-only | ボタンの配置。`open-templates` と登録ダイアログの存在は app::test_index_has_cowork_controls |
| test_cowork_without_a_template_does_not_silently_answer_as_chat | 雛形なしの Cowork がチャット回答に落ちず、結果（ダウンロード）を残す | covered | cowork::test_freeform_markdown_output、app::test_cowork_runs_as_job_and_returns_download（Cowork は `/api/cowork` で、チャットの `/api/chat` と経路が別） |
| test_cowork_shows_the_error_when_generation_fails | PromptTooLongError 等の失敗を画面に伝える | covered | cowork::test_prompt_too_long_becomes_result_error、cowork::test_chat_error_during_generation_becomes_result_error |
| test_cowork_generates_a_file_and_offers_it_for_download | 生成してダウンロードを出す | covered | cowork::test_template_fills_and_offers_download、app::test_cowork_runs_as_job_and_returns_download |
| test_cowork_lists_the_marks_it_could_not_fill | 埋まらなかった欄を伝える | covered | cowork::test_unfilled_marks_are_listed_as_warning |
| test_cowork_uses_a_larger_context_size | 文書生成は文脈長を広げて呼ぶ | covered | cowork::test_generation_uses_larger_context_size |
| test_cowork_does_not_search_the_documentation_unless_it_is_asked | 頼まれない限り技術ドキュメントを引かない | covered | cowork::test_docs_not_searched_unless_requested |
| test_cowork_tells_the_user_when_a_diagram_could_not_be_drawn | 図にできなかったことを伝える | covered | cowork::test_diagram_failures_are_reported |
| test_cowork_reports_a_broken_template_instead_of_crashing | 壊れた雛形で落ちず、エラーとして伝える | covered | cowork::test_broken_template_reports_error（「開けませんでした」）。generating を戻す部分は session_state の仕組みで、ジョブは例外でも結果を返す（jobs::test_failed_job_reports_error） |
| test_template_dialog_shows_a_broken_template_but_still_offers_delete | 壊れた雛形を一覧に出し、削除もできる | covered | app::test_broken_template_is_listed_with_error（一覧に error つきで出て、削除も通る） |
| test_template_dialog_stays_closed_after_dismissing_it | 「×」で閉じたあと開き直さない | ui-only | 上のアップロードダイアログと同じ。`<dialog>` に再実行が無い |
| test_cowork_does_not_leave_the_request_in_the_chat_history | Cowork の依頼文をチャット履歴に残さない | obsolete | 履歴を session_state で共有していたために起きた汚染。`/api/cowork` は履歴を受け取らず返さず、チャットの履歴は JS の `state.history` にしか積まれない |
| test_cowork_attachments_are_captioned_through_the_vlm | 添付の解析に caption_image を渡す | covered | cowork::test_attachments_are_captioned_through_vlm |
| test_cowork_offers_generating_without_a_template | 雛形なしの選択肢がある | covered | app::test_cowork_options_lists_formats_and_templates（`no_template_label`）、app::test_index_has_cowork_controls |
| test_cowork_offers_source_code_as_an_output_format | 出力形式にソースコードがある | covered | cowork::test_output_suffixes_lists_documents_then_sources、app::test_cowork_options_lists_formats_and_templates（`source_suffixes` が空でない） |
| test_cowork_generates_go_source_without_the_markdown_path | .go は Markdown の経路に通さない | covered | cowork::test_source_code_output_skips_markdown_path |
| test_cowork_without_a_template_generates_markdown | 雛形なしで .md を組み立てる | covered | cowork::test_freeform_markdown_output |
| test_cowork_without_a_template_stops_when_there_is_no_evidence | 根拠が無ければ生成しない | covered | cowork::test_no_evidence_stops_without_calling_llm |
| test_cowork_warns_when_an_attachment_yields_no_text | 本文が取れなかった添付を警告する | covered | cowork::test_empty_attachment_warns_and_continues |
| test_the_supplementary_messages_are_not_yellow_boxes | 補足を黄色い警告枠にせず畳んだ場所へ出す | ui-only | 補足は `warnings` / `infos` の配列として返り（cowork::test_unfilled_marks_are_listed_as_warning）、畳んで描くのは app.js の描画 |
| test_choosing_a_source_format_turns_on_the_technical_documents | ソースコード形式を選ぶと技術ドキュメントを既定でオンにする | ui-only | チェックボックスの連動は app.js（`source_suffixes` に含まれるかで切り替え）。判定の元になる `source_suffixes` は app::test_cowork_options_lists_formats_and_templates |
| test_switching_back_to_a_document_format_turns_it_off_again | 文書形式へ戻すとオフに戻す | ui-only | 同上 |
| test_the_user_can_still_turn_it_off_for_a_source_format | 既定をオンにしても利用者がオフにできる | ui-only | 同上（連動は選択時にだけ値を設定し、利用者の操作を奪わない） |
| test_an_empty_docs_search_adds_no_note | 技術ドキュメントの検索が0件でも補足に何も出さない | added | cowork::test_empty_docs_search_adds_no_note |
| test_pasted_text_alone_is_enough_to_run | 貼り付けだけで生成でき、本文がプロンプトに載る | covered | cowork::test_pasted_text_alone_is_enough_and_named_in_references |
| test_the_pasted_text_is_named_in_the_reference_list | 貼り付けを「参照したファイル」に名前つきで残す | covered | 同上 |
| test_a_paste_of_only_whitespace_is_not_evidence | 空白だけの貼り付けを根拠と数えない | covered | cowork::test_whitespace_only_paste_is_not_evidence |
| test_the_screen_calls_it_a_format_file | 画面では「雛形」ではなく「フォーマットファイル」と呼ぶ | covered | app::test_index_has_cowork_controls（ラベル）。index.html に「雛形」の語は無い |

## 足したテスト

すべて今回の Task で足した。旧画面の振る舞いに合わせ、日本語の docstring を付けた。`rag_web/` は変更していない（足したテストはすべて、最初から通った）。

1. answering::test_search_still_runs_without_reranking_when_the_reranker_is_unavailable
2. answering::test_mermaid_request_reports_display_and_keeps_html
3. answering::test_display_mode_is_decided_per_question
4. answering::test_display_mode_is_not_set_when_the_search_fails
5. answering::test_internal_corpus_uses_internal_prompt_and_never_translates
6. answering::test_docs_corpus_uses_docs_prompt_and_asks_with_the_original_question
7. app::test_uploaded_document_really_reaches_the_store
8. app::test_status_of_an_empty_docs_corpus_carries_the_ingest_command
9. cowork::test_empty_docs_search_adds_no_note

## Task 2 への申し送り

- app::test_docs_ingest_command_matches_streamlit_app は `rag_chat_app.py` を読んで `DOCS_INGEST_COMMAND` が一致することを確かめている。旧アプリを消すとこのテストは読む相手を失うため、Task 2 で消すか、`DOCS_INGEST_COMMAND` が `scripts/` の実在するモジュール名を指すことを確かめる形へ書き換えること。
- `tests/test_rag_chat_app_cowork.py` は `tests.test_rag_chat_app` から `dismiss_dialog` を import している。2本は一緒に消す。他のテストが旧2本を import していないことを、削除前に grep で確かめること。
