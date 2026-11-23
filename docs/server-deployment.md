# サーバー配置手順（準備中）

配置手順は、推論を Ollama から vLLM へ切り替えるときに、GB10（DGX OS 7 / Ubuntu 24.04）向けに
書き直す。それまでこの画面（`rag_web/`）は本番には配置しない。開発機では
`.\run_web.ps1` で動かす（README を参照）。

構成の方針と切り替えの計画は
[vLLM バックエンド切り替えの設計書](superpowers/specs/2026-11-22-vllm-backend-design.md) にある。

以前あった Windows サーバー向けの手順書（IIS + NSSM）と旧草案は、Streamlit 版を前提にしていたため削除した。
