"use strict";

// 社内文書RAG の画面。rag_chat_app.py（Streamlit）のチャットとサイドバーを置き換える。
// 回答と資料の本文はどちらも信頼しない入力として扱う。HTML として入れるのは
// Markdown を描画して DOMPurify を通したものだけで、ほかは textContent で入れる。

// 状態を変える操作には、サーバー側の CSRF 対策と同じヘッダーを付ける
const CSRF = { "X-Requested-With": "rag-web" };
const POLL_MS = 2000;
// ジョブの結果を取りに行く通信が続けて失敗したら諦める回数（一時的な断は待つ）
const POLL_MAX_FAILURES = 5;
// サーバーが受け付ける履歴の件数の上限（rag_web/app.py の ChatBody.history）
const HISTORY_LIMIT = 100;
// サーバーが受け付ける質問の文字数の上限（rag_web/app.py の ChatBody.question）
const QUESTION_MAX_CHARS = 100000;
const SPINNER_MESSAGE = "取り込み中…（図表があると時間がかかります）";
const CONNECTION_LOST = "回答を生成できませんでした: 通信が切れました";
const MERMAID_SRC = "/vendor/mermaid-12.3.0.min.js";
// 取り込める拡張子。ingest/parsers の SUPPORTED_SUFFIXES の写しで、API では返していない。
// 食い違いは tests/test_web_app.py が検出する。
const UPLOAD_SUFFIXES = [
  ".bat", ".cs", ".docx", ".drawio", ".go", ".htm", ".html", ".jpeg", ".jpg", ".js",
  ".json", ".md", ".mod", ".pdf", ".png", ".pptx", ".ps1", ".py", ".sh", ".sum",
  ".ts", ".tsx", ".txt", ".vue", ".xlsm", ".xlsx", ".yaml", ".yml",
];
// 回答の Markdown に許さない要素と属性。画像・動画・背景は外部 URL を読みに行き（回答に
// 仕込まれた URL へ質問や資料の中身を送る経路になる）、style は画面全体の見た目を書き換えられる。
// 最後の砦は index.html の CSP で、ここは描画の段階で落としておく1枚目である。
const FORBIDDEN_TAGS = ["style", "img", "picture", "source", "video", "audio", "track", "form", "input"];
const FORBIDDEN_ATTRS = ["style", "background", "srcset", "poster"];

const $ = (id) => document.getElementById(id);

const state = {
  admin: false,
  corpus: "internal",
  // 会話履歴。サーバーへ送るのは {role, content} だけ（表示用の情報は DOM が持つ）
  history: [],
  generating: false,
  uploading: false,
};

// --- 共通 ---------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function errorMessage(res) {
  let message = `${res.status} ${res.statusText}`;
  try {
    const detail = (await res.json()).detail;
    // 入力の形式エラー（422）は配列で返るので、読める形にする
    message = Array.isArray(detail)
      ? detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join(" / ")
      : detail || message;
  } catch (_) { /* 本文が JSON でない（プロキシのエラーページなど）。状態コードで伝える */ }
  return message;
}

// チャットの入力がサーバーの検証（422）で弾かれたときの文言。pydantic の英語の
// メッセージは利用者に見せず、どの項目が原因かだけを日本語で伝える。
async function chatRejectedMessage(res) {
  let fields = [];
  try {
    const detail = (await res.json()).detail;
    if (Array.isArray(detail)) fields = detail.map((d) => (d.loc || [])[1]);
  } catch (_) { /* 本文が JSON でない。下の汎用の文言で伝える */ }
  if (fields.includes("question")) {
    return `質問が長すぎます（${QUESTION_MAX_CHARS.toLocaleString("ja-JP")}文字まで）。短くしてから送ってください。`;
  }
  if (fields.includes("history")) {
    return "会話履歴が長すぎます。「会話履歴をリセット」を押してから質問してください。";
  }
  return "入力の形式が正しくないため、質問を送れませんでした。";
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (!res.ok) throw new ApiError(await errorMessage(res), res.status);
  return res.json();
}

function send(method, path, body) {
  return api(path, {
    method,
    headers: { "Content-Type": "application/json", ...CSRF },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const child of children) if (child != null) node.append(child);
  return node;
}

function show(id, text) {
  $(id).textContent = text || "";
}

function setHidden(id, hidden) {
  $(id).classList.toggle("hidden", hidden);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --- 描画（Markdown・サニタイズ・コードブロック・Mermaid） ------------------------------

const markdown = new marked.Marked({ gfm: true });

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  // 回答中のリンクは別タブで開き、開いた先からこの画面を操作させない
  if (node.tagName === "A" && node.hasAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

function sanitizeHtml(html) {
  // html プロファイルだけを許す。既定のままだと SVG の <image href> や feImage が通る。
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: FORBIDDEN_TAGS,
    FORBID_ATTR: FORBIDDEN_ATTRS,
  });
}

function renderMarkdown(text) {
  // 回答はモデルの出力で、資料の本文をそのまま写すこともある。描画結果は必ずサニタイズする。
  const fragment = document.createElement("div");
  fragment.innerHTML = sanitizeHtml(markdown.parse(text, { async: false }));
  return fragment;
}

// ingest/answer_text.py の _MERMAID_FENCE と同じ形。開きと同じ本数のバッククォートで閉じる。
const MERMAID_FENCE = /^(`{3,})[ \t]*mermaid[ \t]*\n([\s\S]*?)^\1[ \t]*$/gim;

// answer_text.mermaid_definitions と同じ規則で、図として描く定義を取り出す。
// フェンスが無ければ回答全体を1つの定義とみなし、中身の空なフェンスしか無ければ描かない。
function mermaidDefinitions(text) {
  if (!text || !text.trim()) return [];
  const blocks = [...text.matchAll(MERMAID_FENCE)].map((m) => m[2].trim());
  const found = blocks.filter(Boolean);
  if (found.length) return found;
  return blocks.length ? [] : [text.trim()];
}

let mermaidLoading = null;

// mermaid は約5.5MB あるため、図を描くときに初めて読み込む
function loadMermaid() {
  if (!mermaidLoading) {
    mermaidLoading = new Promise((resolve, reject) => {
      const script = el("script", { src: MERMAID_SRC });
      script.onload = () => {
        const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        window.mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: dark ? "dark" : "default",
          // ラベルを foreignObject の HTML ではなく SVG の text で描かせる。下で SVG を
          // DOMPurify に通すとき、foreignObject の中の HTML が落ちてラベルが消えるのを避ける。
          htmlLabels: false,
          flowchart: { htmlLabels: false },
        });
        resolve(window.mermaid);
      };
      script.onerror = () => {
        mermaidLoading = null;
        reject(new Error("図の描画ライブラリを読み込めませんでした"));
      };
      document.head.append(script);
    });
  }
  return mermaidLoading;
}

let diagramSeq = 0;

function renderDiagram(definition) {
  const box = el("div", { className: "diagram" }, el("span", { className: "muted small", textContent: "図を描画中…" }));
  (async () => {
    const id = `mermaid-diagram-${++diagramSeq}`;
    try {
      const mermaid = await loadMermaid();
      const { svg } = await mermaid.render(id, definition);
      box.innerHTML = DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } });
    } catch (error) {
      box.replaceChildren(el("div", { className: "msg error", textContent: `図を描けませんでした: ${error.message || error}` }));
    } finally {
      // 構文エラーのとき mermaid が描画用の一時要素を body に残すことがある
      for (const leftover of [document.getElementById(id), document.getElementById(`d${id}`)]) {
        if (leftover && !box.contains(leftover)) leftover.remove();
      }
    }
  })();
  return box;
}

async function copyText(text) {
  // navigator.clipboard は https か localhost でしか使えない。社内サーバーは http のことが多い
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = el("textarea", { value: text });
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  const ok = document.execCommand("copy");
  area.remove();
  if (!ok) throw new Error("コピーできませんでした");
}

function codeBlock(text, language) {
  const code = el("code", { textContent: text });
  if (language) code.className = `language-${language}`;
  const button = el("button", { type: "button", className: "copy", textContent: "コピー" });
  button.addEventListener("click", async () => {
    try {
      await copyText(text);
      button.textContent = "コピーしました";
    } catch (_) {
      button.textContent = "コピーできませんでした";
    }
    setTimeout(() => { button.textContent = "コピー"; }, 1500);
  });
  return el("div", { className: "codebox" }, button, el("pre", {}, code));
}

// 回答の本文を描く。rag_chat_app.py の render_answer と同じ振る舞い:
// 記法で頼まれたとき（display あり）はコピーボタン付きのコードブロックで出し、mermaid なら
// その下に図も描く。頼まれていなければ Markdown として描き、```mermaid フェンスは図にする。
function renderAnswer(container, text, display) {
  container.replaceChildren();
  if (display) {
    container.append(codeBlock(text, display));
    if (display === "mermaid") {
      for (const definition of mermaidDefinitions(text)) container.append(renderDiagram(definition));
    }
    return;
  }
  const rendered = renderMarkdown(text);
  for (const code of rendered.querySelectorAll("pre > code")) {
    // ```Mermaid も図にする（answer_text の _MERMAID_FENCE は大文字小文字を区別しない）
    if (![...code.classList].some((name) => name.toLowerCase() === "language-mermaid")) continue;
    const definition = code.textContent.trim();
    if (definition) code.parentElement.replaceWith(renderDiagram(definition));
  }
  container.append(...rendered.childNodes);
}

// 生成の途中は Markdown だけを描く。図は生成が終わってから描く（途中の定義は必ず構文エラーになる）。
function renderPartial(container, text) {
  container.replaceChildren(...renderMarkdown(text).childNodes);
}

// 根拠の表示。rag_chat_app.py の render_evidence / render_hits と同じ構成にする。
function renderEvidence(hits, table) {
  const nodes = [];
  if (table) {
    nodes.push(el("details", { className: "evidence" },
      el("summary", { textContent: "絞り込んだ一覧" }),
      el("pre", { textContent: table })));
  }
  if (hits && hits.length) {
    const details = el("details", { className: "evidence" },
      el("summary", { textContent: `参考にした情報（${hits.length}件）` }));
    for (const hit of hits) {
      const item = el("div", { className: "hit" }, el("div", { className: "caption", textContent: hit.caption }));
      if (hit.others && hit.others.length) {
        // 見出しは代表しか名乗らない。同じ記述がどこにあるかを資料を開かずに追えるようにする
        item.append(el("div", { className: "caption", textContent: `同じ記述: ${hit.others.join(" ／ ")}` }));
      }
      item.append(el("div", { className: "text", textContent: hit.text }));
      details.append(item);
    }
    nodes.push(details);
  }
  return nodes;
}

// --- 状態（/api/status） -------------------------------------------------------------------

async function loadStatus() {
  const corpus = state.corpus;
  let status;
  try {
    status = await api(`/api/status?corpus=${encodeURIComponent(corpus)}`);
  } catch (error) {
    show("status-error", `状態を取得できませんでした: ${error.message}`);
    return;
  }
  // 取りに行っている間に検索対象が切り替わったら、古い結果で上書きしない
  if (corpus !== state.corpus) return;
  show("status-error", "");

  show("chunk-count", String(status.chunk_count));
  const docs = corpus === "docs";
  setHidden("docs-panel", !docs);
  setHidden("internal-panel", docs);
  if (docs) {
    // 空の DB は検索が黙って全部空になる。理由が分かるよう取り込みのコマンドを示す
    show("docs-empty", status.chunk_count === 0
      ? `技術ドキュメントのDBが空です。次の2つのコマンドで取り込んでください: ${status.docs_ingest_command}`
      : "");
    show("docs-command", status.chunk_count === 0 ? "" : `更新は CLI で行います: ${status.docs_ingest_command}`);
  } else {
    show("source-dir", `取り込み元: ${status.source_dir_name}/`);
  }
  const reranker = status.reranker || {};
  // reason は「Rerankerで並べ替えません: …」まで組み立て済みで届く（rag_web/corpora.py）
  show("reranker-msg", reranker.state === "failed" ? reranker.reason : "");
}

// --- 管理者ログイン ---------------------------------------------------------------------------

function applyAdmin() {
  setHidden("login-form", state.admin);
  setHidden("logged-in", !state.admin);
  setHidden("ingest", !state.admin);
}

async function loadMe() {
  try {
    state.admin = (await api("/api/me")).admin;
  } catch (error) {
    show("auth-error", `ログイン状態を取得できませんでした: ${error.message}`);
  }
  applyAdmin();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  show("auth-error", "");
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    state.admin = (await send("POST", "/api/login", { password: $("password").value })).admin;
    $("password").value = "";
  } catch (error) {
    show("auth-error", error.message);
  } finally {
    if (button) button.disabled = false;
  }
  applyAdmin();
});

$("logout").addEventListener("click", async () => {
  show("auth-error", "");
  try {
    state.admin = (await send("POST", "/api/logout")).admin;
  } catch (error) {
    show("auth-error", error.message);
  }
  applyAdmin();
});

// --- ジョブ（取り込み・アップロード） -------------------------------------------------------

// 結果が出るまで2秒おきに取りに行く。done / failed は最初の取得で消えるため、ここだけが読む。
async function waitJob(jobId) {
  let failures = 0;
  for (;;) {
    await sleep(POLL_MS);
    let job;
    try {
      job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    } catch (error) {
      // 404 は結果が消えたことを意味し、待っても戻らない。それ以外の断は少し待つ
      if (error instanceof ApiError && error.status === 404) throw error;
      if (++failures >= POLL_MAX_FAILURES) throw error;
      continue;
    }
    failures = 0;
    if (job.state === "done") return job.result;
    if (job.state === "failed") throw new Error(job.error);
  }
}

// 取り込みの結果を4つの枠（進行中・注意・報告・エラー）へ出す
async function runJob(prefix, start) {
  for (const kind of ["notice", "report", "error"]) show(`${prefix}-${kind}`, "");
  show(`${prefix}-progress`, SPINNER_MESSAGE);
  try {
    const { job_id: jobId } = await start();
    const result = await waitJob(jobId);
    show(`${prefix}-notice`, result.notice);
    show(`${prefix}-report`, result.report);
    return true;
  } catch (error) {
    show(`${prefix}-error`, error.message);
    // 管理者の操作が 401 なら、セッションが切れている（期限切れ・サーバーの再起動）。
    // ログイン済みの見た目のままにせず、ログイン欄へ戻す
    if (error instanceof ApiError && error.status === 401) await loadMe();
    return false;
  } finally {
    show(`${prefix}-progress`, "");
  }
}

$("ingest").addEventListener("click", async () => {
  $("ingest").disabled = true;
  try {
    await runJob("ingest", () => send("POST", "/api/ingest"));
    await loadStatus();
  } finally {
    $("ingest").disabled = false;
  }
});

// --- アップロード ----------------------------------------------------------------------------

$("upload-files").accept = UPLOAD_SUFFIXES.join(",");

async function toBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function setUploadBusy(busy) {
  state.uploading = busy;
  $("run-upload").disabled = busy;
  for (const button of $("upload-list").querySelectorAll("button")) button.disabled = busy;
}

async function loadUploads() {
  let data;
  try {
    data = await api("/api/uploads");
  } catch (error) {
    show("upload-error", `アップロード済みの資料を取得できませんでした: ${error.message}`);
    return;
  }
  show("upload-caption", data.caption);
  setHidden("upload-list-wrap", data.items.length === 0);
  $("upload-list").replaceChildren(...data.items.map((item) => {
    const remove = el("button", { type: "button", className: "danger", textContent: "削除", disabled: state.uploading });
    remove.addEventListener("click", () => deleteUpload(item, remove));
    return el("div", { className: "upload-item" }, el("span", { textContent: item.name }), remove);
  }));
}

async function deleteUpload(item, button) {
  for (const kind of ["notice", "report", "error"]) show(`upload-${kind}`, "");
  button.disabled = true;
  try {
    const key = item.key.split("/").map(encodeURIComponent).join("/");
    await send("DELETE", `/api/uploads/${key}`);
  } catch (error) {
    show("upload-error", error.message);
    button.disabled = false;
    return;
  }
  await Promise.all([loadUploads(), loadStatus()]);
}

$("open-upload").addEventListener("click", () => {
  $("upload-dialog").showModal();
  loadUploads();
});

$("close-upload").addEventListener("click", () => $("upload-dialog").close());

$("run-upload").addEventListener("click", async () => {
  const files = [...$("upload-files").files];
  for (const kind of ["notice", "report", "error"]) show(`upload-${kind}`, "");
  if (!files.length) {
    show("upload-notice", "先にファイルを選んでください。");
    return;
  }
  setUploadBusy(true);
  try {
    const ok = await runJob("upload", async () => {
      const body = { files: await Promise.all(files.map(async (file) => ({ name: file.name, content_b64: await toBase64(file) }))) };
      return send("POST", "/api/uploads", body);
    });
    if (ok) $("upload-files").value = "";
  } finally {
    setUploadBusy(false);
  }
  await Promise.all([loadUploads(), loadStatus()]);
});

// --- チャット --------------------------------------------------------------------------------

function setGenerating(generating) {
  state.generating = generating;
  // 1つのタブで並行して質問させない。検索対象も生成中は変えさせない
  $("question").disabled = generating;
  $("send").disabled = generating;
  $("reset-history").disabled = generating;
  for (const radio of document.querySelectorAll("input[name=corpus]")) radio.disabled = generating;
}

function clearChat() {
  state.history = [];
  $("messages").replaceChildren(el("p", { id: "empty-chat", className: "muted", textContent: "メッセージを入力して質問してください。" }));
}

function addBubble(role) {
  $("empty-chat")?.remove();
  const bubble = el("div", { className: `bubble ${role}` },
    el("div", { className: "role", textContent: role === "user" ? "あなた" : "アシスタント" }));
  $("messages").append(bubble);
  return bubble;
}

function scrollToBottom() {
  // 入力欄は sticky で常に見えているため、scrollIntoView では動かない。ページの末尾へ送る
  window.scrollTo(0, document.documentElement.scrollHeight);
}

// 利用者が上へ戻って読んでいるときは、逐次表示で引き戻さない
function nearBottom() {
  const root = document.documentElement;
  return window.innerHeight + window.scrollY >= root.scrollHeight - 120;
}

// NDJSON を1行ずつ読む。途中で切れたら（done も error も来ないまま終われば）例外にする
async function* readEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) yield JSON.parse(line);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) yield JSON.parse(buffer);
  } finally {
    // done / error で読むのをやめたときも、例外のときも、接続を手放す
    reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

async function ask(question) {
  show("corpus-msg", "");
  const userBubble = addBubble("user");
  userBubble.append(el("div", { className: "user-text", textContent: question }));
  // 今回の質問は history に含めない（サーバーが質問として別に受け取る）
  const history = state.history.slice(-HISTORY_LIMIT);
  state.history.push({ role: "user", content: question });

  const bubble = addBubble("assistant");
  const noteBox = el("div", { className: "msg warning" });
  const answerBox = el("div", { className: "answer" }, el("span", { className: "muted", textContent: "回答を生成中…" }));
  bubble.append(noteBox, answerBox);
  scrollToBottom();

  let answer = "";
  let finished = null; // { content, display } または { error, rejected? }
  let evidence = null;
  let pending = false;
  const redraw = () => {
    // 1トークンごとに描き直すと長い回答で重くなる。描画の機会ごとにまとめる
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (finished) return;
      const follow = nearBottom();
      renderPartial(answerBox, answer);
      if (follow) scrollToBottom();
    });
  };

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...CSRF },
      body: JSON.stringify({
        corpus: state.corpus,
        temperature: Number($("temperature").value),
        question,
        history,
      }),
    });
    if (response.status === 422) {
      // 質問そのものが受け付けられなかった。履歴に残すと次の質問も同じ理由で弾かれ続ける
      finished = { error: await chatRejectedMessage(response), rejected: true };
    } else if (!response.ok) {
      finished = { error: `回答を生成できませんでした: ${await errorMessage(response)}` };
    } else {
      for await (const event of readEvents(response)) {
        if (event.type === "note") noteBox.textContent = event.text;
        else if (event.type === "delta") { answer += event.text; redraw(); }
        else if (event.type === "evidence") evidence = event;
        else if (event.type === "error") { finished = { error: event.message }; break; }
        else if (event.type === "done") { finished = { content: answer, display: event.display }; break; }
      }
    }
  } catch (_) {
    // fetch の失敗・ストリームの途中切れ・壊れた行。どれも利用者には通信断として伝える
  }
  if (!finished) finished = { error: CONNECTION_LOST };

  const follow = nearBottom();
  if (!finished.error) {
    try {
      renderAnswer(answerBox, finished.content, finished.display);
      if (evidence) bubble.append(...renderEvidence(evidence.hits, evidence.table));
    } catch (error) {
      // 描画に失敗しても、履歴の user / assistant の組は崩さない
      console.error("回答の描画に失敗しました", error);
      finished = { error: `回答を生成できませんでした: ${error.message || error}` };
    }
  }
  if (finished.error) {
    // 途中まで届いた回答は消さずに残し、その下にエラーを出す。履歴にはエラー文を残す
    // （Streamlit 版と同じく assistant の発言として）
    try {
      if (answer) renderPartial(answerBox, answer);
      else answerBox.replaceChildren();
    } catch (_) {
      answerBox.replaceChildren();
    }
    answerBox.append(el("div", { className: "msg error", textContent: finished.error }));
    if (finished.rejected) {
      // 送れなかった質問は会話に含めない。上で積んだ user の発言を取り除き、組を残さない
      state.history.pop();
    } else {
      state.history.push({ role: "assistant", content: finished.error });
    }
  } else {
    state.history.push({ role: "assistant", content: finished.content });
  }
  if (follow) scrollToBottom();
}

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question || state.generating) return;
  $("question").value = "";
  setGenerating(true);
  try {
    await ask(question);
  } finally {
    // done / error / 通信断のどれでも必ず入力欄を戻す
    setGenerating(false);
    $("question").focus();
  }
  // リランカーの確認結果とチャンク数は検索のあとで変わりうる
  loadStatus();
});

$("question").addEventListener("keydown", (event) => {
  // Enter で送信、Shift+Enter で改行。日本語の変換確定の Enter では送らない
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});

// --- 設定欄 ----------------------------------------------------------------------------------

$("temperature").addEventListener("input", () => show("temperature-value", Number($("temperature").value).toFixed(1)));

for (const radio of document.querySelectorAll("input[name=corpus]")) {
  radio.addEventListener("change", () => {
    if (!radio.checked || state.generating) return;
    state.corpus = radio.value;
    // 履歴を残すと、前の検索対象向けの質問が検索語に継ぎ足されて検索ごと壊れる
    // （rag_chat_app.py のコーパス切り替えのコメント参照）。消したことは画面に出す。
    if (state.history.length) {
      clearChat();
      show("corpus-msg", "検索対象を切り替えたため、会話履歴をリセットしました。");
    } else {
      show("corpus-msg", "");
    }
    show("chunk-count", "-");
    loadStatus();
  });
}

$("reset-history").addEventListener("click", () => {
  if (state.generating) return;
  clearChat();
  show("corpus-msg", "");
});

// --- 起動 ------------------------------------------------------------------------------------

// ブラウザが戻る・再読み込みでラジオの選択を復元することがある。画面の表示を正とする
state.corpus = document.querySelector("input[name=corpus]:checked")?.value || "internal";
show("temperature-value", Number($("temperature").value).toFixed(1));
loadMe();
loadStatus();
