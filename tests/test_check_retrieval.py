"""検索の実測スクリプトの異常系と、コーパスの取り違え防止。

open_store は存在しないパスに対して例外を出さず、空のDBを新規作成する。
ベクトルは復元できず全量再取り込みが要るため、移行直後にまだ取り込んでいない
状態でこのスクリプトを回すのは事故ではなく通常の順序であり、そこで書式化の
TypeError を出すと原因から遠い場所で落ちることになる。
"""
import sys

import pytest

from ingest import store
from ingest.embedder import EMBED_DIM
from ingest.store import open_store
from scripts import check_retrieval


def test_an_empty_store_stops_main_with_a_reason(monkeypatch, tmp_path, capsys):
    """件数0なら測るものが無い。何をすればよいかを告げて終了する。"""
    monkeypatch.setattr(check_retrieval.embedder, "check_embedder", lambda: None)
    monkeypatch.setattr(
        sys, "argv", ["check_retrieval", "--db", str(tmp_path / "empty.sqlite3")]
    )

    assert check_retrieval.main() == 1
    assert "scripts.ingest_source" in capsys.readouterr().out


def test_the_endpoints_and_the_default_translation_model_come_from_vllm_settings(
    monkeypatch, tmp_path, capsys
):
    """既定のモデル名が Ollama 流（gpt-oss:20b）のままだと、vLLM へそのまま送って
    404 になる。実行時と同じ VLLM_CHAT_MODEL を使い、宛先も必ず出す。"""
    monkeypatch.setenv("VLLM_CHAT_MODEL", "my-chat")
    monkeypatch.setenv("VLLM_EMBED_URL", "http://embed.example:9000")
    monkeypatch.setattr(check_retrieval.embedder, "check_embedder", lambda: None)
    monkeypatch.setattr(
        sys, "argv", ["check_retrieval", "--db", str(tmp_path / "empty.sqlite3")]
    )

    assert check_retrieval.main() == 1

    printed = capsys.readouterr().out
    assert "http://embed.example:9000" in printed
    assert "my-chat" in printed


def test_an_empty_docs_store_names_the_docs_commands(monkeypatch, tmp_path, capsys):
    """技術ドキュメント側は取り込み手順が違う。社内資料の手順を案内しても進めない。"""
    monkeypatch.setattr(check_retrieval.embedder, "check_embedder", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_retrieval", "--corpus", "docs", "--db", str(tmp_path / "empty.sqlite3")],
    )

    assert check_retrieval.main() == 1
    printed = capsys.readouterr().out
    assert "scripts.fetch_docs" in printed
    assert "--keep-code-blocks" in printed


def test_a_vector_search_that_returns_nothing_names_the_question(monkeypatch):
    """None を返すと呼び出し側の :.3f で TypeError になり、原因から3フレーム遠ざかる。"""
    monkeypatch.setattr(
        check_retrieval.embedder,
        "embed_query",
        lambda question, session=None: [1.0] + [0.0] * (EMBED_DIM - 1),
    )
    with pytest.raises(RuntimeError, match="運転音"):
        check_retrieval._vector_best(open_store(":memory:"), "運転音は", session=None)


def test_the_two_corpora_point_at_different_databases():
    """同じDBを指していたら、片方の実測がもう片方の実測を装ってしまう。

    open_store はパスを間違えても例外を出さず空のDBを作るため、綴りの取り違えは
    「総チャンク数 0」としてしか現れない。
    """
    assert check_retrieval.INTERNAL.db_path == store.DB_PATH
    assert check_retrieval.DOCS.db_path == store.DOCS_DB_PATH
    assert check_retrieval.INTERNAL.db_path != check_retrieval.DOCS.db_path


def test_the_docs_corpus_translates_the_query_and_the_internal_one_does_not():
    """アプリ（rag_web/answering.py）は技術ドキュメントの検索だけ英語へ訳してから search() を呼ぶ。

    実測でここを通さないと、実行時には存在しないクエリでしきい値を決めることに
    なる（社内資料は日本語のまま検索するので訳してはいけない）。
    """
    assert check_retrieval.DOCS.translate is True
    assert check_retrieval.INTERNAL.translate is False


def test_the_translator_passes_the_question_through_for_the_internal_corpus():
    def ask(prompt):
        raise AssertionError("社内資料で翻訳を呼んではいけない")

    query_of = check_retrieval.translator(check_retrieval.INTERNAL, ask)
    assert query_of("育児休業は誰が取得できますか") == "育児休業は誰が取得できますか"


def test_the_translator_asks_only_once_for_the_same_question():
    """同じ質問が分離の判定とリランカー比較の両方に出る。

    そのつど訳すと待ち時間が倍になるうえ、2回の翻訳が食い違えば同じ質問の
    測定値も食い違う。
    """
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return '{"query": "st.cache_data"}'

    query_of = check_retrieval.translator(check_retrieval.DOCS, ask)
    assert query_of("Streamlitのキャッシュ") == "st.cache_data"
    assert query_of("Streamlitのキャッシュ") == "st.cache_data"
    assert len(asked) == 1


def test_the_docs_questions_cover_every_configured_source():
    """1つのソースに偏った質問だけで測ると、そのソースの距離分布だけで
    しきい値が決まってしまう。docs_sources.toml に足したソースは、ここにも
    質問を足すこと。
    """
    import tomllib
    from pathlib import Path

    configured = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "docs_sources.toml").read_text(
            encoding="utf-8"
        )
    )
    # 設定の名前と質問文の言い回しは一致しない（langchain-text-splitters を
    # 「LangChain」と書くなど）ので、名前から引ける主要な語で照合する。
    keywords = {
        "streamlit": "Streamlit",
        "langchain-text-splitters": "LangChain",
        "ollama": "Ollama",
        "huggingface_hub": "huggingface_hub",
        "mcp": "MCP",
        "pymupdf": "PyMuPDF",
        "csharp": "C#",
        "go": "Go",
        # go と go-spec は同じ「Go」で照合できてしまう。仕様側は文法そのものを
        # 尋ねる語で照合する（リリースノートには select 文の構文は無い）。
        "go-spec": "select文",
        # csharp と csharp-spec も同じ「C#」で照合できてしまう。仕様側は規範的な
        # 規則の名前で照合する（MS Learn の解説に「確定代入」の節は無い）。
        "csharp-spec": "確定代入",
        # 「Python」では照合にならない。既存の「Ollamaの埋め込みAPIをPythonから
        # 呼ぶ方法」に含まれてしまい、python-spec の質問が1つも無くても通る。
        # この資料でしか答えられない規則の名前で照合する。
        "python-spec": "デスクリプタ",
        "typescript": "TypeScript",
        "mermaid": "Mermaid",
        "markdown": "Markdown",
    }
    names = {source["name"] for source in configured["source"]}
    assert names == set(keywords), "docs_sources.toml が変わったら質問も見直すこと"
    joined = "\n".join(check_retrieval.DOCS_RELEVANT)
    for name, keyword in keywords.items():
        assert keyword in joined, f"{name} に対応する関連質問が無い"


def test_the_snowflake_question_is_measured_as_out_of_domain():
    """2026-09-13の不具合の発端。技術ドキュメントに Snowflake の資料は1件も無い。

    ここから消すと、同じ質問がまた通るようになったことに気づけない。
    """
    assert any(
        "Snowflake" in question for question in check_retrieval.DOCS.out_of_domain
    )


def test_a_translation_that_fell_back_on_every_question_is_treated_as_broken():
    """translate_query は失敗しても例外を出さず原文を返す（回答を止めないため）。

    実測ではそれが害になる。2026-09-13、宛先の設定を取り違えていて25問すべてが
    日本語のまま測られ、英語コーパスのしきい値をその値で決めるところだった。
    """
    assert not check_retrieval.translation_worked(
        check_retrieval.DOCS, lambda question: question
    )


def test_one_translated_question_is_enough_to_call_it_working():
    """1問だけの失敗は、その行に原文が出るのでその場で読み取れる。全滅だけを異常とみなす。"""
    first = check_retrieval.DOCS.relevant[0]

    def query_of(question):
        return "translated" if question == first else question

    assert check_retrieval.translation_worked(check_retrieval.DOCS, query_of)


def test_the_internal_corpus_never_counts_as_a_broken_translation():
    """社内資料は日本語のまま検索する。原文と同じなのが正しい。"""
    assert check_retrieval.translation_worked(
        check_retrieval.INTERNAL, lambda question: question
    )


def test_main_stops_with_exit_code_2_on_a_legacy_ollama_setting(monkeypatch, tmp_path, capsys):
    """古い OLLAMA_* が残ったまま測ると、宛先の違う数字が黙って出てしまう。"""
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        sys, "argv", ["check_retrieval", "--db", str(tmp_path / "empty.sqlite3")]
    )

    assert check_retrieval.main() == 2

    err = capsys.readouterr().err
    assert "OLLAMA_HOST" in err
    assert "VLLM_" in err


def test_a_db_built_with_another_model_stops_main(monkeypatch, tmp_path, capsys):
    """別のモデルで測った距離は、しきい値の根拠にならない。"""
    from ingest import vector_store

    db = tmp_path / "s.sqlite3"
    seed = vector_store.open_store(str(db))
    seed.set_meta_if_absent("embedding_model", "qwen3-embedding")
    seed.set_meta_if_absent("embedding_dim", "2560")
    seed.close()
    monkeypatch.setattr(check_retrieval.embedder, "check_embedder", lambda: None)
    monkeypatch.setattr(sys, "argv", ["check_retrieval", "--db", str(db)])

    assert check_retrieval.main() == 2
    assert "qwen3-embedding" in capsys.readouterr().err


def test_the_regression_passes_on_any_slide_that_covers_fine_tuning():
    # 実測 2026-11-23 の上位3件。スライド11は5位だが、上位はどれもファインチューニングを
    # 本文で扱うスライドである。
    top = [
        ("セミナー資料/CNN画像認識セミナー.pptx スライド18（OCR）", 43.24),
        ("セミナー資料/CNN画像認識セミナー.pptx スライド18（OCR）", 42.77),
        ("セミナー資料/RAGセミナー.pptx スライド16（OCR）", 42.73),
    ]
    assert check_retrieval.regression_hit(top, check_retrieval.REGRESSION_EXPECTED)


def test_the_regression_fails_when_navigation_slides_fill_the_top():
    # このケースが捕まえたい失敗（2026-09-07 の記録）: 章扉・目次が上位を占める。
    top = [
        ("生成AI活用セミナー.pptx スライド2", 20.1),
        ("生成AI活用セミナー.pptx スライド3", 19.8),
        ("セミナー資料/RAGセミナー.pptx スライド2", 18.0),
    ]
    assert not check_retrieval.regression_hit(top, check_retrieval.REGRESSION_EXPECTED)
