const $ = (sel, root = document) => root.querySelector(sel);
const params = new URLSearchParams(location.search);
const STORE = "byoi.guest";

const SLASH = [
  { cmd: "/help", hint: "Commands that work on the phone", local: true },
  { cmd: "/status", hint: "Model, cwd, session, spend", local: true },
  { cmd: "/cost", hint: "Token spend this session", local: true },
  { cmd: "/permissions", hint: "Current permission mode", local: true },
  { cmd: "/export", hint: "Download this transcript", local: true },
  { cmd: "/handoff", hint: "Download compact summary for account switch", local: true },
  { cmd: "/clear", hint: "New conversation", local: true },
  { cmd: "/compact", hint: "Compress the conversation" },
  { cmd: "/model sonnet", hint: "Switch to Sonnet" },
  { cmd: "/model opus", hint: "Switch to Opus" },
  { cmd: "/model haiku", hint: "Switch to Haiku" },
  { cmd: "/plan", hint: "Plan first, then implement" },
  { cmd: "/fast", hint: "Lower-latency replies" },
  { cmd: "/effort high", hint: "Think harder" },
  { cmd: "/init", hint: "Write CLAUDE.md for this repo" },
  { cmd: "/review", hint: "Review the working tree" },
  { cmd: "/diff", hint: "Uncommitted changes" },
  { cmd: "/commit", hint: "Commit with a good message" },
  { cmd: "/pr", hint: "Open a pull request" },
];

const MODES = [
  { id: "plan", label: "Plan", hint: "Design before touching files" },
  { id: "acceptEdits", label: "Code", hint: "Auto-accept file edits" },
  { id: "auto", label: "Auto", hint: "Classifier reviews risky tools" },
  { id: "manual", label: "Ask", hint: "Prompt you for every tool" },
];

const state = {
  view: "join",
  otp: params.get("otp") || "",
  ticket: params.get("ticket") || "",
  join: null,
  status: "Open the slip QR, or paste the join URL.",
  // The seat tells us at boot whether it is a PC in this room, in which
  // case the guest does have to be on its Wi-Fi, or a cloud container,
  // where saying so would send them hunting for a password they do not need.
  onWifi: false,
  busy: false,
  messages: [],
  chatBusy: false,
  chatLabel: "Ready",
  ws: null,
  model: "",
  mode: "acceptEdits",
  cwd: "",
  usage: null,
  account: "",
  quota: null,
  handoffAvailable: false,
  todos: [],
  suggestions: [],
  // Explicit open/closed state for activity groups, tool bodies and thinking,
  // keyed "g:"/"t:"/"k:" + id. Absent means "use the default for that row".
  expanded: {},
  sheet: null,
  files: null,
  filePath: "",
  images: [],
  timerId: null,
  testStatus: null,
  testReport: null,
  deployment: null,
  deploying: false,
  byo: false,
  byoStage: "idle",
  byoUrl: "",
  byoPowers: [],
  byoEmail: "",
  byoBusy: false,
  byoError: "",
};

function loadStore() {
  try {
    return JSON.parse(sessionStorage.getItem(STORE) || "{}");
  } catch {
    return {};
  }
}

function saveStore(patch) {
  sessionStorage.setItem(STORE, JSON.stringify({ ...loadStore(), ...patch }));
}

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// highlight.js is precached with the rest of the app, but a phone that opened
// the PWA mid-deploy could be running an older cache — fall back to plain text
// rather than losing the message.
function hl(code, lang) {
  return window.HL ? window.HL.highlight(code, lang) : escapeHtml(code);
}

function hlLang(path) {
  return window.HL ? window.HL.langFromPath(path) : "";
}

function inlineMarkdown(raw) {
  let html = escapeHtml(raw);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  html = html.replace(
    /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>'
  );
  html = html.replace(
    /(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    '$1<a href="$2" target="_blank" rel="noreferrer noopener">$2</a>'
  );
  return html;
}

function codeBlockHTML(chunk) {
  const nl = chunk.indexOf("\n");
  const lang = (nl === -1 ? "" : chunk.slice(0, nl)).trim().split(/\s+/)[0];
  const body = (nl === -1 ? chunk : chunk.slice(nl + 1)).replace(/\n+$/, "");
  return `<div class="code">
    <div class="code-bar"><span class="lang">${escapeHtml(lang || "code")}</span><button type="button" class="copy">Copy</button></div>
    <pre class="term"><code>${hl(body, lang)}</code></pre>
  </div>`;
}

function renderProse(raw) {
  const out = [];
  let list = null;
  let para = [];
  const flushPara = () => {
    if (para.length) out.push(`<p>${para.join("<br>")}</p>`);
    para = [];
  };
  const flushList = () => {
    if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join("")}</${list.tag}>`);
    list = null;
  };
  for (const line of String(raw || "").split("\n")) {
    const text = line.trim();
    if (!text) {
      flushPara();
      flushList();
      continue;
    }
    const heading = text.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushPara();
      flushList();
      out.push(`<div class="md-h md-h${heading[1].length}">${inlineMarkdown(heading[2])}</div>`);
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(text)) {
      flushPara();
      flushList();
      out.push("<hr>");
      continue;
    }
    const bullet = text.match(/^[-*+]\s+(.*)$/);
    if (bullet) {
      flushPara();
      if (!list || list.tag !== "ul") {
        flushList();
        list = { tag: "ul", items: [] };
      }
      list.items.push(inlineMarkdown(bullet[1]));
      continue;
    }
    const numbered = text.match(/^\d+[.)]\s+(.*)$/);
    if (numbered) {
      flushPara();
      if (!list || list.tag !== "ol") {
        flushList();
        list = { tag: "ol", items: [] };
      }
      list.items.push(inlineMarkdown(numbered[1]));
      continue;
    }
    const quote = text.match(/^>\s?(.*)$/);
    if (quote) {
      flushPara();
      flushList();
      out.push(`<blockquote>${inlineMarkdown(quote[1])}</blockquote>`);
      continue;
    }
    flushList();
    para.push(inlineMarkdown(text));
  }
  flushPara();
  flushList();
  return out.join("");
}

function renderMarkdown(raw) {
  return String(raw || "")
    .split("```")
    .map((chunk, i) => (i % 2 === 1 ? codeBlockHTML(chunk) : renderProse(chunk)))
    .join("");
}

async function readJson(res, fallback) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : fallback || `HTTP ${res.status}`);
  }
  return data;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  return readJson(res, "request failed");
}

async function sit(otp) {
  const code = (otp || state.otp || "").trim();
  if (!code) {
    state.status = "Scan the slip QR, or type the OTP from the slip.";
    render();
    return;
  }
  state.busy = true;
  state.status = "reaching the seat…";
  render();
  try {
    const data = await api(`/api/join?otp=${encodeURIComponent(code)}`);
    state.otp = code;
    state.join = data;
    state.status = `${data.seat?.name || "Seat"} · hello ${data.session?.coder_name || ""}`;
    saveStore({ otp: code, sessionId: data.session?.id });
    if (state.view !== "chat") {
      state.view = params.get("view") === "chat" && state.ticket ? "chat" : "floor";
    }
  } catch (err) {
    state.status = err.message || "Cannot reach this seat.";
    state.view = "join";
  } finally {
    state.busy = false;
    render();
  }
}

async function claim(boardId) {
  if (!state.join?.session) return;
  state.busy = true;
  render();
  try {
    const data = await api(`/api/sessions/${state.join.session.id}/claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ board_id: boardId }),
    });
    state.join.session = data.session || state.join.session;
    state.join.item = data.item || state.join.item;
    state.status = "brief claimed · open chat";
  } catch (err) {
    state.status = err.message;
  } finally {
    state.busy = false;
    render();
  }
}

async function openChat() {
  state.busy = true;
  render();
  try {
    const data = await api("/local/unlock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ otp: state.otp, session_id: state.join?.session?.id }),
    });
    state.ticket = data.ticket || "";
    saveStore({ ticket: state.ticket });
    state.view = "chat";
    state.status = "";
  } catch (err) {
    state.status = err.message;
  } finally {
    state.busy = false;
    render();
    if (state.view === "chat") connectChat();
  }
}

async function shipped() {
  if (!state.join?.session) return;
  try {
    const data = await api(`/api/sessions/${state.join.session.id}/complete`, { method: "POST" });
    if (state.join.session) state.join.session.status = "done";
    if (data.testing) {
      state.view = "results";
      state.testStatus = "running";
      state.testReport = null;
      state.status = "shipped · building the spec test suite";
      pollTests();
    } else {
      state.status = "shipped. leave the seat.";
    }
  } catch (err) {
    state.status = err.message;
  }
  render();
}

async function pollTests() {
  const sid = state.join?.session?.id;
  if (!sid) return;
  try {
    const data = await api(`/api/sessions/${sid}/tests`);
    state.testStatus = data.test_status;
    state.testReport = data.test_report;
    render();
    if (data.test_status === "running" || !data.test_status) {
      setTimeout(pollTests, 2000);
    }
  } catch (err) {
    state.status = err.message;
    render();
    setTimeout(pollTests, 3000);
  }
}

function chatUrl(ticket) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/chat?ticket=${encodeURIComponent(ticket)}`;
}

function connectChat() {
  if (!state.ticket) return;
  state.leaveChat = false;
  if (state.ws && (state.ws.readyState === 0 || state.ws.readyState === 1)) return;
  const ws = new WebSocket(chatUrl(state.ticket));
  state.ws = ws;
  ws.onopen = () => {
    state.reconnectDelay = 800;
  };
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    applyChatEvent(msg);
  };
  ws.onclose = () => {
    if (state.ws === ws) state.ws = null;
    if (state.view !== "chat" || state.leaveChat) return;
    state.chatLabel = "Reconnecting…";
    state.chatBusy = false;
    renderChatChrome();
    const delay = Math.min(state.reconnectDelay || 800, 8000);
    state.reconnectDelay = delay * 1.6;
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(connectChat, delay);
  };
}

function applyChatEvent(msg) {
  const kind = msg.type;
  if (kind === "ready") {
    if (Array.isArray(msg.history) && state.messages.length === 0) {
      state.messages = msg.history.map(fromHistory);
      const lastTodos = [...msg.history].reverse().find((h) => h.todos);
      if (lastTodos) state.todos = lastTodos.todos;
    }
    state.chatBusy = !!msg.busy;
    state.model = msg.model || state.model;
    state.mode = msg.mode || state.mode;
    state.cwd = msg.cwd || state.cwd;
    state.usage = msg.usage || state.usage;
    state.suggestions = msg.suggestions || state.suggestions;
    state.account = msg.account || state.account;
    // The translator's own `ready` (on session init) carries no account fields —
    // clobbering here would flip the phone back to "not signed in" mid-session.
    if (msg.byo !== undefined) state.byo = !!msg.byo;
    state.quota = msg.quota || state.quota;
    state.handoffAvailable = !!msg.handoff || state.handoffAvailable;
    state.chatLabel = msg.busy ? "Working…" : msg.error || labelReady();
    if (msg.error) upsert({ id: "err", kind: "system", text: msg.error });
    render();
    return;
  }
  if (kind === "quota") {
    state.quota = msg;
    renderChatChrome();
    return;
  }
  if (kind === "account") {
    const prev = msg.previous ? ` (${msg.previous})` : "";
    upsert({
      id: uid(),
      kind: "system",
      text: `Claude account${prev} hit a usage limit — continuing on ${msg.label || "a spare login"}, same files.`,
    });
    state.account = msg.label || state.account;
    state.handoffAvailable = !!msg.handoff || state.handoffAvailable;
    state.chatBusy = false;
    state.chatLabel = labelReady();
    render();
    return;
  }
  if (kind === "cleared") {
    state.messages = [];
    state.todos = [];
    state.suggestions = [];
    state.usage = null;
    render();
    return;
  }
  if (kind === "mode") {
    state.mode = msg.mode || state.mode;
    renderChatChrome();
    return;
  }
  if (kind === "status") {
    state.chatBusy = !!msg.busy;
    state.chatLabel = msg.label || (msg.busy ? "Working…" : labelReady());
    renderChatChrome();
    return;
  }
  if (kind === "usage") {
    state.usage = msg;
    renderChatChrome();
    return;
  }
  if (kind === "suggestion") {
    if (msg.text && !state.suggestions.includes(msg.text)) state.suggestions.push(msg.text);
    renderChatChrome();
    return;
  }
  if (kind === "user") {
    const last = [...state.messages].reverse().find((m) => m.kind === "user");
    if (last && last.local && last.text === (msg.text || "")) {
      last.id = msg.id || last.id;
      last.local = false;
      return;
    }
    upsert({ id: msg.id || uid(), kind: "user", text: msg.text || "" });
    render();
    return;
  }
  if (kind === "assistant" && isNoise(msg.text)) return;
  if (kind === "system" && isNoise(msg.message || msg.text)) return;
  if (kind === "assistant" || kind === "thinking") {
    const id = msg.id || kind;
    const existing = state.messages.find((m) => m.id === id && m.kind === kind);
    const clock =
      kind === "thinking"
        ? { t0: existing?.t0 || Date.now(), t1: msg.done ? Date.now() : existing?.t1 }
        : {};
    if (msg.delta) {
      upsert({ id, kind, text: (existing?.text || "") + (msg.text || ""), done: !!msg.done, ...clock });
    } else {
      upsert({ id, kind, text: msg.text || existing?.text || "", done: !!msg.done, ...clock });
    }
    render();
    return;
  }
  if (kind === "tool" || kind === "ask") {
    if (kind === "tool" && msg.status === "running") {
      const info = toolInfo(msg);
      state.chatLabel = [info.verbing, info.subject].filter(Boolean).join(" ").slice(0, 60);
    }
    upsert({
      id: msg.id || uid(),
      kind,
      name: msg.name,
      detail: msg.detail || "",
      status: msg.status || "running",
      output: msg.output || "",
      diff: msg.diff,
      todos: msg.todos,
      questions: msg.questions,
      input: msg.input,
    });
    if (msg.todos) state.todos = msg.todos;
    render();
    return;
  }
  if (kind === "permission") {
    upsert({
      id: msg.request_id || uid(),
      kind: "permission",
      requestId: msg.request_id,
      name: msg.name,
      detail: msg.detail || "",
      diff: msg.diff,
    });
    render();
    return;
  }
  if (kind === "error") {
    upsert({ id: uid(), kind: "system", text: msg.message || "error" });
    state.chatBusy = false;
    render();
    return;
  }
  if (kind === "turn") {
    state.chatBusy = false;
    state.chatLabel = labelReady();
    renderChatChrome();
  }
}

function quotaBits() {
  const q = state.quota;
  if (!q) return [];
  const bits = [];
  if (q.five_hour != null) bits.push(`5h ${Math.round(Number(q.five_hour))}%`);
  if (q.seven_day != null) bits.push(`7d ${Math.round(Number(q.seven_day))}%`);
  return bits;
}

function labelReady() {
  const bits = [state.mode === "plan" ? "plan" : "code"];
  if (state.model) bits.push(String(state.model).split("-").slice(-2).join(" "));
  if (state.account) bits.push(state.account);
  bits.push(...quotaBits());
  if (state.usage?.cost != null) bits.push(`$${Number(state.usage.cost).toFixed(3)}`);
  return bits.join(" · ");
}

function toMs(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n > 1e12 ? n : n * 1000;
}

function formatClock(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${Number(m)}:${ss}`;
}

function sessionTiming() {
  const sess = state.join?.session;
  if (!sess) return null;
  const start = toMs(sess.started_at);
  if (!start) return null;
  const item = state.join?.item;
  const now = Date.now();
  const elapsed = Math.max(0, now - start);
  const end = toMs(sess.ends_at) || (item?.wellness_minutes ? start + item.wellness_minutes * 60 * 1000 : null);
  const breakAt = item?.break_after ? start + item.break_after * 60 * 1000 : null;
  if (sess.status === "done") return { kind: "over", label: "ended" };
  if (end && now >= end) return { kind: "over", label: `+${formatClock(now - end)}` };
  if (breakAt && now >= breakAt && end && now < end) {
    return { kind: "warn", label: `break ${formatClock(end - now)}` };
  }
  if (end) return { kind: "ok", label: formatClock(end - now) };
  return { kind: "ok", label: formatClock(elapsed) };
}

function tickTimer() {
  const nodes = document.querySelectorAll("#session-timer");
  if (!nodes.length) return;
  const t = sessionTiming();
  nodes.forEach((el) => {
    if (!t) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.textContent = t.label;
    el.classList.remove("ok", "warn", "over");
    el.classList.add(t.kind);
  });
}

function ensureTimer() {
  if (state.timerId) return;
  tickTimer();
  state.timerId = setInterval(tickTimer, 1000);
}

function fromHistory(item) {
  return { ...item, kind: item.kind || item.type, done: true };
}

function upsert(item) {
  const idx = state.messages.findIndex((m) => m.id === item.id && m.kind === item.kind);
  if (idx >= 0) state.messages[idx] = { ...state.messages[idx], ...item };
  else state.messages.push(item);
}

function uid() {
  return Math.random().toString(36).slice(2, 10);
}

function wsSend(payload) {
  if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(payload));
}

function isNoise(text) {
  const t = String(text || "");
  return /ede_diagnostic|stop_reason=tool_use/.test(t) && t.length < 500;
}

function localReply(userText, reply) {
  state.messages.push({ id: uid(), kind: "user", text: userText, local: true });
  state.messages.push({ id: uid(), kind: "assistant", text: reply, done: true, local: true });
  render();
}

function handleLocalSlash(trimmed) {
  const [cmd] = trimmed.split(/\s+/);
  if (cmd === "/clear") {
    wsSend({ type: "clear" });
    state.images = [];
    return true;
  }
  if (cmd === "/help") {
    localReply(
      trimmed,
      SLASH.map((s) => `${s.cmd} — ${s.hint}`).join("\n")
    );
    return true;
  }
  if (cmd === "/status") {
    const cost = state.usage?.cost != null ? `$${Number(state.usage.cost).toFixed(4)}` : "—";
    localReply(
      trimmed,
      [
        `seat: ${state.join?.seat?.name || "this PC"}`,
        `account: ${state.account || "—"}`,
        `model: ${state.model || "default"}`,
        `mode: ${state.mode}`,
        `cwd: ${state.cwd || "—"}`,
        `busy: ${state.chatBusy ? "yes" : "no"}`,
        `cost: ${cost}`,
        `quota: ${quotaBits().join(" · ") || "—"}`,
        `timer: ${sessionTiming()?.label || "—"}`,
        `session: ${state.join?.session?.id || "—"}`,
      ].join("\n")
    );
    return true;
  }
  if (cmd === "/cost") {
    const u = state.usage || {};
    localReply(
      trimmed,
      u.cost == null
        ? "No turn has finished yet, so there is no cost snapshot."
        : `cost: $${Number(u.cost).toFixed(4)}\nduration: ${u.duration_ms || "—"} ms\nturns: ${u.turns || "—"}`
    );
    return true;
  }
  if (cmd === "/permissions") {
    const hint = MODES.find((m) => m.id === state.mode);
    localReply(trimmed, `mode: ${state.mode}${hint ? `\n${hint.hint}` : ""}\nSwitch from ☰ or /plan.`);
    return true;
  }
  if (cmd === "/export") {
    const body = state.messages
      .map((m) => `# ${m.kind}\n${m.text || m.detail || m.output || ""}\n`)
      .join("\n");
    const blob = new Blob([body], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "byoi-session.txt";
    a.click();
    localReply(trimmed, "Transcript downloaded as byoi-session.txt.");
    return true;
  }
  if (cmd === "/handoff") {
    downloadHandoff(trimmed);
    return true;
  }
  return false;
}

async function downloadHandoff(trimmed) {
  if (!state.ticket) {
    localReply(trimmed || "/handoff", "Unlock chat first.");
    return;
  }
  try {
    const res = await fetch(`/local/handoff?ticket=${encodeURIComponent(state.ticket)}`);
    if (!res.ok) {
      localReply(trimmed || "/handoff", "No compact handoff yet. It appears after a usage-limit account switch.");
      return;
    }
    const body = await res.text();
    const blob = new Blob([body], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "byoi-handoff.md";
    a.click();
    state.handoffAvailable = true;
    localReply(trimmed || "/handoff", "Compact summary downloaded as byoi-handoff.md.");
  } catch (err) {
    localReply(trimmed || "/handoff", err.message || "Could not download handoff.");
  }
}

function sendUser(text, images) {
  const trimmed = (text || "").trim();
  const pics = images || state.images;
  if (!trimmed && !pics.length) return;
  if (trimmed.startsWith("/") && handleLocalSlash(trimmed)) {
    state.sheet = null;
    state.images = [];
    return;
  }
  if (!state.ws || state.ws.readyState !== 1) return;
  state.messages.push({ id: uid(), kind: "user", text: trimmed, local: true, hasImage: !!pics.length });
  state.chatBusy = true;
  state.chatLabel = "Working…";
  state.suggestions = [];
  wsSend({ type: "user", text: trimmed, images: pics });
  state.images = [];
  render();
  scrollLog(true);
}

function runSlash(entry) {
  state.sheet = null;
  if (entry.cmd === "/plan") {
    wsSend({ type: "mode", mode: "plan" });
    render();
    return;
  }
  sendUser(entry.cmd);
}

function setMode(mode) {
  state.sheet = null;
  wsSend({ type: "mode", mode });
  render();
}

function answerPermission(requestId, allow) {
  wsSend({ type: "permission", request_id: requestId, allow });
  const item = state.messages.find((m) => m.kind === "permission" && m.requestId === requestId);
  if (item) item.resolved = allow ? "allowed" : "denied";
  render();
}

function baseName(path) {
  const clean = String(path || "").replace(/\/+$/, "");
  const cut = clean.lastIndexOf("/");
  return cut === -1 ? clean : clean.slice(cut + 1);
}

function dirName(path) {
  const clean = String(path || "").replace(/\/+$/, "");
  const cut = clean.lastIndexOf("/");
  return cut === -1 ? "" : clean.slice(0, cut);
}

function hostOf(url) {
  const match = String(url || "").match(/^https?:\/\/([^/]+)/i);
  return match ? match[1] : String(url || "");
}

const KIND_ICON = {
  read: "▤",
  edit: "✎",
  create: "＋",
  run: "❯",
  search: "⌕",
  web: "◎",
  plan: "☰",
  agent: "✦",
  tool: "•",
};

// One tool call, said the way the mobile app says it: a past-tense verb for
// what happened and a present participle for what is happening right now.
function toolInfo(msg) {
  const name = String(msg.name || "tool");
  const input = msg.input && typeof msg.input === "object" ? msg.input : {};
  const detail = msg.detail || "";
  const failed = msg.status === "error";
  const path = input.file_path || input.path || detail;
  const make = (kind, verb, verbing, subject, sub) => ({
    kind,
    verb,
    verbing,
    subject: subject || "",
    sub: sub || "",
    name,
  });
  if (name === "Read") return make("read", "Read", "Reading", baseName(path), dirName(path));
  if (name === "NotebookRead") return make("read", "Read", "Reading", baseName(path), dirName(path));
  if (name === "Edit" || name === "MultiEdit" || name === "NotebookEdit") {
    return make("edit", "Edited", "Editing", baseName(path), dirName(path));
  }
  if (name === "Write") return make("create", "Wrote", "Writing", baseName(path), dirName(path));
  if (name === "Bash") {
    const command = String(input.command || detail || "");
    if (input.run_in_background) {
      return make(
        "run",
        failed ? "Background shell failed" : "Started a background shell",
        "Starting a background shell",
        command
      );
    }
    return make("run", failed ? "Command failed" : "Ran", "Running", command);
  }
  if (name === "BashOutput") {
    return make("run", failed ? "Background shell failed" : "Checked a background shell", "Checking a background shell", detail);
  }
  if (name === "KillShell" || name === "KillBash") {
    return make("run", "Stopped a background shell", "Stopping a background shell", detail);
  }
  if (name === "Grep") return make("search", "Searched for", "Searching for", input.pattern || detail);
  if (name === "Glob") return make("search", "Found files matching", "Looking for files matching", input.pattern || detail);
  if (name === "WebFetch") return make("web", "Fetched", "Fetching", hostOf(input.url || detail));
  if (name === "WebSearch") return make("web", "Searched the web for", "Searching the web for", input.query || detail);
  if (name === "TodoWrite") {
    const count = Array.isArray(input.todos) ? input.todos.length : 0;
    return make("plan", "Updated the task list", "Updating the task list", count ? `${count} tasks` : "");
  }
  if (name === "ExitPlanMode") return make("plan", "Finished the plan", "Finishing the plan", "");
  if (name === "Task" || name === "Agent") {
    return make("agent", "Ran an agent", "Running an agent", input.description || detail);
  }
  if (name === "Skill") return make("agent", "Ran the skill", "Running the skill", input.skill || detail);
  return make("tool", name, name, detail);
}

// What a permission prompt is asking for, as something Claude is about to do
// rather than something it did.
const ASK_VERB = {
  Read: "read",
  NotebookRead: "read",
  Edit: "edit",
  MultiEdit: "edit",
  NotebookEdit: "edit",
  Write: "write",
  Bash: "run this command",
  BashOutput: "check a background shell",
  KillShell: "stop a background shell",
  KillBash: "stop a background shell",
  Grep: "search for",
  Glob: "look for files matching",
  WebFetch: "fetch",
  WebSearch: "search the web for",
  TodoWrite: "update the task list",
  ExitPlanMode: "finish the plan",
  Task: "run an agent",
  Agent: "run an agent",
  Skill: "run a skill",
};

const diffCache = new Map();

function diffLines(oldText, newText) {
  const a = String(oldText || "").split("\n");
  const b = String(newText || "").split("\n");
  if (a.length && a[a.length - 1] === "") a.pop();
  if (b.length && b[b.length - 1] === "") b.pop();
  const n = a.length;
  const m = b.length;
  // An LCS table over very large edits costs more than the diff is worth on a
  // phone; past this size show the blocks whole rather than line-matched.
  if (n * m > 250000) {
    return {
      rows: [...a.map((s) => ({ t: "-", s })), ...b.map((s) => ({ t: "+", s }))],
      added: m,
      removed: n,
      coarse: true,
    };
  }
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  let added = 0;
  let removed = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ t: " ", s: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ t: "-", s: a[i] });
      removed++;
      i++;
    } else {
      rows.push({ t: "+", s: b[j] });
      added++;
      j++;
    }
  }
  while (i < n) {
    rows.push({ t: "-", s: a[i++] });
    removed++;
  }
  while (j < m) {
    rows.push({ t: "+", s: b[j++] });
    added++;
  }
  return { rows, added, removed };
}

function diffOf(msg) {
  if (!msg.diff) return null;
  const oldText = msg.diff.old || "";
  const newText = msg.diff.new || "";
  const key = `${msg.id}:${oldText.length}:${newText.length}`;
  let cached = diffCache.get(key);
  if (!cached) {
    cached = diffLines(oldText, newText);
    if (diffCache.size > 200) diffCache.clear();
    diffCache.set(key, cached);
  }
  return cached;
}

function trimContext(rows, context = 3) {
  const keep = new Array(rows.length).fill(false);
  rows.forEach((row, idx) => {
    if (row.t === " ") return;
    for (let k = idx - context; k <= idx + context; k++) {
      if (k >= 0 && k < rows.length) keep[k] = true;
    }
  });
  const out = [];
  let skipped = false;
  rows.forEach((row, idx) => {
    if (keep[idx]) {
      if (skipped) out.push({ t: "…", s: "" });
      skipped = false;
      out.push(row);
    } else {
      skipped = true;
    }
  });
  return out;
}

function statBadge(added, removed) {
  if (!added && !removed) return "";
  const plus = added ? `<span class="add">+${added}</span>` : "";
  const minus = removed ? `<span class="del">−${removed}</span>` : "";
  return `<span class="stat">${plus}${minus}</span>`;
}

const diffHtmlCache = new Map();

function diffHTML(msg) {
  const diff = diffOf(msg);
  if (!diff) return "";
  const path = msg.diff.path || "";
  // The whole log re-renders on every streamed token, so colouring a hunk once
  // and keeping the string matters more here than anywhere else.
  const key = `${msg.id}:${(msg.diff.old || "").length}:${(msg.diff.new || "").length}:${path}`;
  const cached = diffHtmlCache.get(key);
  if (cached) return cached;
  const lang = hlLang(path);
  const rows = (diff.coarse ? diff.rows : trimContext(diff.rows)).slice(0, 500);
  const body = rows
    .map((row) => {
      if (row.t === "…") return `<div class="dl skip">⋯</div>`;
      const cls = row.t === "+" ? "add" : row.t === "-" ? "del" : "same";
      const sign = row.t === " " ? " " : row.t;
      // A hunk is not a whole file, so each line is coloured on its own: a
      // string or comment left open by the cut cannot bleed into the rest.
      return `<div class="dl ${cls}"><span class="sign">${sign}</span>${hl(row.s, lang) || "&nbsp;"}</div>`;
    })
    .join("");
  const head = path ? `<div class="diff-path">${escapeHtml(path)}</div>` : "";
  const html = `<div class="diff">${head}<div class="diff-body">${body}</div></div>`;
  if (diffHtmlCache.size > 120) diffHtmlCache.clear();
  diffHtmlCache.set(key, html);
  return html;
}

// A shipped pull request or commit is the outcome of a visit, not a line of
// terminal output — pull it out of the Bash result and give it its own card.
function outcomeHTML(msg) {
  if (msg.name !== "Bash" || msg.status === "running") return "";
  const command = String(msg.input?.command || msg.detail || "");
  const output = String(msg.output || "");
  const cards = [];
  const pr = output.match(/https:\/\/github\.com\/[^\s/]+\/[^\s/]+\/pull\/(\d+)/);
  if (pr && /\bpr\s+create\b/.test(command)) {
    cards.push(`<a class="outcome pr" href="${escapeHtml(pr[0])}" target="_blank" rel="noreferrer noopener">
      <span class="outcome-verb">Created PR #${escapeHtml(pr[1])}</span>
      <span class="outcome-sub">${escapeHtml(pr[0])}</span>
    </a>`);
  }
  const commit = output.match(/^\[(\S+)\s+([0-9a-f]{7,40})\]\s+(.*)$/m);
  if (commit) {
    cards.push(`<div class="outcome commit">
      <span class="outcome-verb">Committed ${escapeHtml(commit[2].slice(0, 7))} on ${escapeHtml(commit[1])}</span>
      <span class="outcome-sub">${escapeHtml(commit[3])}</span>
    </div>`);
  }
  return cards.join("");
}

const GUTTER = /^(\s*\d+(?:→|\t|\|))(.*)$/;

// A Read is a file, so colour it like one. Claude Code prefixes each line with
// its number ("  12→code"); that gutter stays plain so it reads as a margin
// rather than as part of the code.
function outputHTML(msg) {
  const raw = isNoise(msg.output) ? "" : msg.output || "";
  if (!raw) return "";
  const body = raw.slice(0, 32000);
  const isFile = msg.name === "Read" || msg.name === "NotebookRead";
  const lang = isFile ? hlLang(msg.input?.file_path || msg.input?.path || msg.detail) : "";
  if (!lang) return `<pre class="term">${escapeHtml(body)}</pre>`;
  const lines = body.split("\n");
  if (!lines.some((line) => GUTTER.test(line))) {
    return `<pre class="term">${hl(body, lang)}</pre>`;
  }
  const marked = lines
    .map((line) => {
      const parts = line.match(GUTTER);
      if (!parts) return hl(line, lang);
      return `<span class="ln">${escapeHtml(parts[1])}</span>${hl(parts[2], lang)}`;
    })
    .join("\n");
  return `<pre class="term">${marked}</pre>`;
}

function toolBodyHTML(msg) {
  const diff = diffHTML(msg);
  const out = outputHTML(msg);
  if (!diff && !out) {
    return msg.status === "running" ? "" : `<div class="tool-empty">No output.</div>`;
  }
  return `${diff}${out}`;
}

function toolRowHTML(msg) {
  const info = toolInfo(msg);
  const running = msg.status === "running";
  const failed = msg.status === "error";
  const open = state.expanded[`t:${msg.id}`] ?? failed;
  const diff = diffOf(msg);
  const mark = running
    ? `<span class="spin"></span>`
    : `<span class="glyph ${failed ? "bad" : ""}">${failed ? "!" : KIND_ICON[info.kind] || "•"}</span>`;
  const verb = running ? info.verbing : info.verb;
  const subject = info.subject
    ? `<span class="subject ${info.kind === "run" ? "mono" : ""}">${escapeHtml(info.subject)}</span>`
    : "";
  const sub = info.sub && !open ? "" : info.sub ? `<div class="tool-sub">${escapeHtml(info.sub)}</div>` : "";
  const stat = diff ? statBadge(diff.added, diff.removed) : "";
  return `<div class="tool-row ${failed ? "bad" : ""} ${running ? "live" : ""}">
    <button type="button" class="tool-head" data-toggle="t:${escapeHtml(msg.id)}" data-open="${open ? "1" : "0"}">
      ${mark}<span class="verb">${escapeHtml(verb)}</span>${subject}${stat}
      <span class="chev">${open ? "▾" : "›"}</span>
    </button>
    ${sub}
    ${open ? `<div class="tool-body">${toolBodyHTML(msg)}</div>` : ""}
    ${outcomeHTML(msg)}
  </div>`;
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

// "Edited 2 files, ran 6 commands  +19 −6" — the rolled-up line the mobile app
// shows once a burst of tool calls is over.
function groupSummary(items) {
  const edited = new Set();
  const created = new Set();
  const read = new Set();
  const counts = { run: 0, search: 0, web: 0, agent: 0, plan: 0, tool: 0 };
  let added = 0;
  let removed = 0;
  let failed = 0;
  for (const msg of items) {
    const info = toolInfo(msg);
    if (msg.status === "error") failed++;
    const diff = diffOf(msg);
    if (diff) {
      added += diff.added;
      removed += diff.removed;
    }
    const label = info.subject || info.name;
    if (info.kind === "edit") edited.add(label);
    else if (info.kind === "create") created.add(label);
    else if (info.kind === "read") read.add(label);
    else counts[info.kind] = (counts[info.kind] || 0) + 1;
  }
  const clauses = [];
  const named = (set, verb) => {
    if (!set.size) return;
    clauses.push(set.size === 1 ? `${verb} ${[...set][0]}` : `${verb} ${plural(set.size, "file")}`);
  };
  named(edited, "edited");
  named(created, "wrote");
  named(read, "read");
  if (counts.run) clauses.push(`ran ${plural(counts.run, "command")}`);
  if (counts.search) clauses.push(`ran ${plural(counts.search, "search")}`.replace("searchs", "searches"));
  if (counts.web) clauses.push(`fetched ${plural(counts.web, "page")}`);
  if (counts.agent) clauses.push(`ran ${plural(counts.agent, "agent")}`);
  if (counts.plan) clauses.push("updated the task list");
  if (counts.tool) clauses.push(`used ${plural(counts.tool, "tool")}`);
  const text = clauses.join(", ") || `${plural(items.length, "step")}`;
  return {
    text: text.charAt(0).toUpperCase() + text.slice(1),
    added,
    removed,
    failed,
  };
}

function toolGroupHTML(group) {
  const items = group.items;
  const running = items.some((m) => m.status === "running");
  const choice = state.expanded[`g:${group.id}`];
  const open = choice ?? (running || items.length === 1);
  if (open) {
    const rows = items.map(toolRowHTML).join("");
    const collapse =
      items.length > 1
        ? `<button type="button" class="group-collapse" data-toggle="g:${escapeHtml(group.id)}" data-open="1">Hide steps</button>`
        : "";
    return `<div class="msg tools"><div class="group open">${rows}${collapse}</div></div>`;
  }
  const summary = groupSummary(items);
  const bad = summary.failed ? `<span class="stat"><span class="del">${summary.failed} failed</span></span>` : "";
  return `<div class="msg tools"><div class="group">
    <button type="button" class="group-head" data-toggle="g:${escapeHtml(group.id)}" data-open="0">
      <span class="glyph">✓</span><span class="verb">${escapeHtml(summary.text)}</span>
      ${statBadge(summary.added, summary.removed)}${bad}<span class="chev">›</span>
    </button>
  </div></div>`;
}

// Consecutive tool calls become one activity group; everything else stays a
// message of its own, in order.
function chatBlocks(messages) {
  const blocks = [];
  for (const msg of messages) {
    if (msg.kind === "tool") {
      const last = blocks[blocks.length - 1];
      if (last && last.type === "tools") {
        last.items.push(msg);
        continue;
      }
      blocks.push({ type: "tools", id: msg.id, items: [msg] });
      continue;
    }
    blocks.push({ type: "msg", msg });
  }
  return blocks;
}

function logHTML() {
  const claimed = state.join?.item;
  if (!state.messages.length) {
    return `<div class="empty">
        <h2>What do you want to ship?</h2>
        <p>${claimed ? escapeHtml(claimed.title) : "Tell Claude what you want to ship."}</p>
        ${claimed ? `<button class="btn small" id="use-brief">Start from this brief</button>` : ""}
      </div>`;
  }
  return chatBlocks(state.messages)
    .map((block) => (block.type === "tools" ? toolGroupHTML(block) : messageHTML(block.msg)))
    .join("");
}

function todosHTML() {
  if (!state.todos.length) return "";
  const items = state.todos
    .map((t) => {
      const cls = t.status === "completed" ? "todo-done" : t.status === "in_progress" ? "todo-now" : "";
      const mark = t.status === "completed" ? "✓" : t.status === "in_progress" ? "→" : "○";
      return `<li class="${cls}">${mark} ${escapeHtml(t.content || t.activeForm || "")}</li>`;
    })
    .join("");
  return `<div class="todos"><strong>Todos</strong><ul>${items}</ul></div>`;
}

function suggestionsHTML() {
  if (!state.suggestions.length || state.chatBusy) return "";
  return `<div class="chips">${state.suggestions
    .slice(0, 4)
    .map((s) => `<button type="button" class="chip" data-suggest="${escapeHtml(s)}">${escapeHtml(s)}</button>`)
    .join("")}</div>`;
}

function closeSheet() {
  state.sheet = null;
  render();
}

function sheetWrap(title, inner) {
  return `<div class="sheet-backdrop" id="sheet-backdrop">
    <div class="sheet" id="sheet">
      <div class="sheet-head">
        <h3>${escapeHtml(title)}</h3>
        <button type="button" class="icon-btn" id="sheet-close" aria-label="Close">✕</button>
      </div>
      ${inner}
    </div>
  </div>`;
}

function sheetHTML() {
  if (state.sheet === "slash") {
    return sheetWrap(
      "Slash commands",
      SLASH.map(
        (s) => `<button type="button" class="item" data-slash="${escapeHtml(s.cmd)}">${escapeHtml(s.cmd)}<span>${escapeHtml(s.hint)}</span></button>`
      ).join("")
    );
  }
  if (state.sheet === "menu") {
    const modes = MODES.map(
      (m) =>
        `<button type="button" class="item" data-mode="${m.id}">${escapeHtml(m.label)}${state.mode === m.id ? " · on" : ""}<span>${escapeHtml(m.hint)}</span></button>`
    ).join("");
    return sheetWrap(
      "Session",
      `${modes}
      <button type="button" class="item" data-slash="/compact">Compact<span>Free context</span></button>
      <button type="button" class="item" data-slash="/handoff">Handoff<span>Download compact summary</span></button>
      <button type="button" class="item" data-slash="/cost">Cost<span>Tokens this session</span></button>
      <button type="button" class="item" data-slash="/status">Status</button>
      <button type="button" class="item" data-slash="/clear">New conversation</button>`
    );
  }
  if (state.sheet === "files" && state.files) {
    const up = state.files.parent != null
      ? `<button type="button" class="item" data-dir="${escapeHtml(state.files.parent)}">‹ ..</button>`
      : "";
    const rows = state.files.entries
      .map((e) =>
        e.dir
          ? `<button type="button" class="item" data-dir="${escapeHtml(e.path)}">${escapeHtml(e.name)}/<span>folder</span></button>`
          : `<button type="button" class="item" data-file="${escapeHtml(e.path)}">${escapeHtml(e.name)}<span>mention in chat</span></button>`
      )
      .join("");
    return sheetWrap(
      `Files ${state.files.cwd || "/"}`,
      `<div class="row"><button type="button" class="btn small" id="take-photo">Camera</button>
      <button type="button" class="btn small" id="pick-photo">Photo</button></div>
      ${up}${rows || "<p class='lede'>empty</p>"}`
    );
  }
  return "";
}

function render() {
  const app = $("#app");
  if (state.view === "chat" && $("#app .screen.chat")) {
    state.stick = nearBottom();
    const heading = $(".topbar h1");
    if (heading) {
      const seat = state.join?.seat?.name || "Claude";
      heading.innerHTML = `<span class="dot ${state.chatBusy ? "busy" : ""}"></span>${escapeHtml(seat)}`;
    }
    $("#log").innerHTML = logHTML();
    const todoWrap = $("#todo-wrap");
    if (todoWrap) todoWrap.innerHTML = todosHTML();
    const sug = $("#suggest-wrap");
    if (sug) sug.innerHTML = suggestionsHTML();
    const thumbs = $("#thumbs");
    if (thumbs) thumbs.innerHTML = state.images.map((img) => `<img alt="" src="data:${img.media_type};base64,${img.data}">`).join("");
    const sheetHost = $("#sheet-host");
    if (sheetHost) sheetHost.innerHTML = sheetHTML();
    bindLog();
    bindSheet();
    renderChatChrome();
    tickTimer();
    scrollLog();
    return;
  }
  if (state.view === "chat") app.innerHTML = chatHTML();
  else if (state.view === "floor") app.innerHTML = floorHTML();
  else if (state.view === "results") app.innerHTML = resultsHTML();
  else app.innerHTML = joinHTML();
  bind();
  tickTimer();
  if (state.view === "chat") scrollLog(true);
}

function joinHTML() {
  return `<div class="screen">
    <p class="eyebrow">BYOI</p>
    <h1>Have a seat</h1>
    <p class="lede">${state.onWifi ? "Same Wi-Fi as this table. " : ""}Scan the slip, or type the code.</p>
    <div class="card">
      <label for="otp">Code from the slip</label>
      <input id="otp" type="text" inputmode="text" autocomplete="one-time-code" value="${escapeHtml(state.otp)}" placeholder="on the printed slip" />
      ${state.status ? `<p class="status">${escapeHtml(state.status)}</p>` : ""}
      <button class="btn" id="sit" ${state.busy ? "disabled" : ""}>${state.busy ? "Sitting…" : "Sit"}</button>
    </div>
  </div>`;
}

function floorHTML() {
  const session = state.join?.session;
  const seat = state.join?.seat;
  const claimed = state.join?.item;
  const board = state.join?.board || [];
  const hello = session
    ? `${seat?.name || "Seat"} · hello ${escapeHtml(session.coder_name || "")}`
    : "Checked in at the desk? Scan the slip QR.";
  const briefs = board
    .map(
      (item) => `<article class="brief">
        <h3>${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.brief)}</p>
        <p class="pill">${item.wellness_minutes} min · break at ${item.break_after}${item.project ? ` · ${escapeHtml(item.project.name)}` : ""}</p>
        <button class="btn ghost small" data-claim="${escapeHtml(item.id)}" ${!session || state.busy ? "disabled" : ""}>Choose</button>
      </article>`
    )
    .join("");
  return `<div class="screen floor">
    <header>
      <p class="eyebrow">BYOI</p>
      <h1>${escapeHtml(seat?.name || "Seat")}</h1>
      <p class="lede">${hello}</p>
      <p class="pill timer" id="session-timer" hidden></p>
    </header>
    <section class="fold open" id="sessionFold">
      <button type="button" class="fold-head" data-fold="sessionFold"><span>This session</span><span class="chevron">▾</span></button>
      <div class="fold-body">
        ${
          claimed
            ? `<div class="card"><h2>${escapeHtml(claimed.title)}</h2><p class="lede">${escapeHtml(claimed.brief)}</p>${
                claimed.project ? `<p class="pill">${escapeHtml(claimed.project.name)}</p>` : ""
              }</div>`
            : `<p class="lede">Pick a solution below.</p>`
        }
        ${state.status ? `<p class="status">${escapeHtml(state.status)}</p>` : ""}
        <button class="btn" id="open-chat" ${state.busy || !session ? "disabled" : ""}>${state.busy ? "Opening…" : "Chat"}</button>
        ${session && claimed && claimed.project ? `<button class="btn ghost" id="deploy" ${state.deploying ? "disabled" : ""}>${state.deploying ? "Deploying…" : "Deploy preview"}</button>` : ""}
        ${deployHTML()}
        ${session ? `<button class="btn ghost" id="shipped">I'm done</button>` : ""}
        <button class="btn ghost" id="leave">Leave</button>
      </div>
    </section>
    <section class="fold" id="byoFold">
      <button type="button" class="fold-head" data-fold="byoFold"><span>Your Claude account</span><span class="chevron">▾</span></button>
      <div class="fold-body">
        ${byoHTML()}
      </div>
    </section>
    <section class="fold open" id="boardFold">
      <button type="button" class="fold-head" data-fold="boardFold"><span>Solutions</span><span class="chevron">▾</span></button>
      <div class="fold-body">
        ${briefs || "<p class='lede'>Nothing on the board yet.</p>"}
      </div>
    </section>
  </div>`;
}

function byoHTML() {
  if (state.byo) {
    return `<div class="card">
      <h2>Running on your account</h2>
      <p class="lede">${state.byoEmail ? `Signed in as ${escapeHtml(state.byoEmail)}.` : "Signed in."} This session uses your Claude usage, not the salon's.</p>
      <p class="fine">Access is revoked and deleted when your session ends.</p>
      ${(state.byoPowers || []).length ? `<p class="fine">Until then this seat can ${(state.byoPowers || []).map((p) => escapeHtml(p)).join(", ")} with your account.</p>` : ""}
      <button class="btn ghost" id="byo-cancel" ${state.byoBusy ? "disabled" : ""}>${state.byoBusy ? "Signing out…" : "Use the salon's account instead"}</button>
    </div>`;
  }
  if (state.byoStage === "url") {
    const powers = (state.byoPowers || []).map((p) => escapeHtml(p)).join(", ");
    return `<div class="card">
      <h2>Sign in on this phone</h2>
      <p class="lede">Open the link, approve it in your own Claude account, then paste the code it gives you.</p>
      ${powers ? `<p class="fine warn">Claude's sign-in asks for full access — this seat will be able to ${powers} until your session ends. It is revoked at checkout.</p>` : ""}
      <p><a class="byo-link" href="${escapeHtml(state.byoUrl)}" target="_blank" rel="noreferrer noopener">Open Claude sign-in ↗</a></p>
      <form id="byo-form" class="byo-form">
        <input id="byo-code" name="code" type="text" inputmode="text" autocomplete="one-time-code"
               placeholder="Paste the code" spellcheck="false" ${state.byoBusy ? "disabled" : ""} />
        <button class="btn" type="submit" ${state.byoBusy ? "disabled" : ""}>${state.byoBusy ? "Checking…" : "Done"}</button>
      </form>
      ${state.byoError ? `<p class="status">${escapeHtml(state.byoError)}</p>` : ""}
      <button class="btn ghost" id="byo-cancel">Cancel</button>
    </div>`;
  }
  return `<div class="card">
    <h2>Use your own Claude account</h2>
    <p class="lede">Already pay for Claude? Run this session on your account instead of the salon's.</p>
    <p class="fine">Your password never reaches this computer — you sign in on your own phone.
    The access it grants is deleted and revoked when your session ends.</p>
    <p class="fine">While your session is running, the salon's computer is running Claude with your
    account. Only sit down at a machine you trust.</p>
    ${state.byoError ? `<p class="status">${escapeHtml(state.byoError)}</p>` : ""}
    <button class="btn ghost" id="byo-start" ${state.byoBusy ? "disabled" : ""}>${state.byoBusy ? "Starting…" : "Use my own Claude account"}</button>
  </div>`;
}

async function byoStart() {
  if (!state.ticket) {
    state.byoError = "Open the chat once first, so this seat knows it is you.";
    render();
    return;
  }
  state.byoBusy = true;
  state.byoError = "";
  render();
  try {
    const data = await api("/local/byo/start", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ticket: state.ticket }),
    });
    if (data.done) {
      state.byo = true;
      state.byoStage = "idle";
    } else {
      state.byoUrl = data.auth_url || "";
      state.byoPowers = data.powers || [];
      state.byoStage = "url";
    }
  } catch (err) {
    state.byoError = err.message;
  }
  state.byoBusy = false;
  render();
}

async function byoCode(code) {
  const value = (code || "").trim();
  if (!value) return;
  state.byoBusy = true;
  state.byoError = "";
  render();
  try {
    const data = await api("/local/byo/code", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ticket: state.ticket, code: value }),
    });
    state.byo = true;
    state.byoEmail = data.email || "";
    state.byoPowers = data.powers || state.byoPowers;
    state.byoStage = "idle";
    state.byoUrl = "";
    state.messages = [];
  } catch (err) {
    state.byoError = err.message;
  }
  state.byoBusy = false;
  render();
}

async function byoCancel() {
  state.byoBusy = true;
  state.byoError = "";
  render();
  try {
    await api("/local/byo/cancel", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ticket: state.ticket }),
    });
  } catch (err) {
    state.byoError = err.message;
  }
  state.byo = false;
  state.byoStage = "idle";
  state.byoUrl = "";
  state.byoEmail = "";
  state.byoBusy = false;
  render();
}

function deployHTML() {
  const d = state.deployment;
  if (!d) return "";
  if (d.state === "running") return `<p class="status">deploying · building your preview</p>`;
  if (d.state === "failed") return `<p class="status">deploy failed · ${escapeHtml(d.detail || "unknown error")}</p>`;
  if (d.state === "torn_down") return `<p class="status">preview was taken down at checkout</p>`;
  if (d.url) {
    return `<p class="status">preview live · <a href="${escapeHtml(d.url)}" target="_blank" rel="noreferrer">${escapeHtml(d.url)}</a></p>`;
  }
  return "";
}

async function deploy() {
  if (!state.join?.session || state.deploying) return;
  state.deploying = true;
  state.status = "deploying…";
  render();
  try {
    const data = await api(`/api/sessions/${state.join.session.id}/deploy`, {
      method: "POST",
      body: JSON.stringify({ production: false }),
    });
    state.deployment = data.deployment;
    pollDeployment();
  } catch (err) {
    state.deploying = false;
    state.status = err.message;
  }
  render();
}

async function pollDeployment() {
  if (!state.join?.session) return;
  try {
    const data = await api(`/api/sessions/${state.join.session.id}/deployment`);
    state.deployment = data.deployment;
    const live = data.deployment && data.deployment.state === "running";
    state.deploying = Boolean(live);
    if (!live) state.status = "";
    render();
    if (live) setTimeout(pollDeployment, 3000);
  } catch {
    state.deploying = false;
  }
}

function resultsHTML() {
  const report = state.testReport;
  const status = state.testStatus || "running";
  const cases = (report && report.cases) || [];
  // What went wrong is what you act on, so it reads first. `reason` is written
  // by the desk from the spec; the suite itself never reaches this screen.
  const ordered = [...cases].sort((a, b) => Number(!!a.pass) - Number(!!b.pass));
  const rows = ordered
    .map(
      (c) => `<li class="${c.pass ? "pass" : "fail"}">${c.pass ? "✓" : "✕"} <strong>${escapeHtml(c.name)}</strong>
      ${!c.pass && c.reason ? `<span>${escapeHtml(c.reason)}</span>` : ""}</li>`
    )
    .join("");
  const heading =
    status === "running"
      ? "Testing against the spec…"
      : report?.blocked
        ? "Couldn't finish grading"
        : status === "passed"
          ? `${report?.passed ?? 0} passed`
          : `${report?.failed ?? 0} failed · ${report?.passed ?? 0} passed`;
  return `<div class="screen">
    <p class="eyebrow">Review</p>
    <h1>${escapeHtml(heading)}</h1>
    <p class="lede">${escapeHtml(report?.summary || state.status || "")}</p>
    ${status === "running" ? `<p class="status">Checking your work. Hang tight.</p>` : ""}
    ${report?.note ? `<p class="status">${escapeHtml(report.note)}</p>` : ""}
    <ul class="cases">${rows || (status === "running" ? "" : "<li>Nothing to report.</li>")}</ul>
    <button class="btn ghost" id="leave">Leave</button>
  </div>`;
}

function chatHTML() {
  const seat = state.join?.seat?.name || "Claude";
  const embedded = params.get("embedded") === "1";
  return `<div class="screen chat">
    <header class="topbar">
      ${embedded ? "" : `<button class="back" id="back" type="button">‹</button>`}
      <div class="who">
        <h1><span class="dot ${state.chatBusy ? "busy" : ""}"></span>${escapeHtml(seat)}</h1>
        <p class="pill" id="chat-label">${escapeHtml(state.chatLabel)}</p>
      </div>
      <span id="session-timer" class="pill timer" hidden></span>
      <button class="icon-btn" id="menu" type="button" aria-label="Session">☰</button>
    </header>
    <div id="todo-wrap">${todosHTML()}</div>
    <div class="log" id="log">${logHTML()}</div>
    <div id="suggest-wrap">${suggestionsHTML()}</div>
    <div id="thumbs" class="thumbs"></div>
    <form class="composer" id="composer">
      <div class="tools">
        <button class="icon-btn" id="plus" type="button" aria-label="Attach">＋</button>
        <button class="icon-btn" id="slash" type="button" aria-label="Commands">/</button>
      </div>
      <div class="composer-col">
        <textarea id="draft" rows="1" placeholder="Message or /command" enterkeyhint="send"></textarea>
      </div>
      <button class="send stop" id="stop" type="button" aria-label="Stop" hidden>■</button>
      <button class="send" id="send" type="submit" aria-label="Send">↑</button>
    </form>
    <input id="photo" type="file" accept="image/*" capture="environment" hidden />
    <input id="library" type="file" accept="image/*" hidden />
    <div id="sheet-host">${sheetHTML()}</div>
    <div id="term-full" class="term-full" hidden>
      <header><span>Console · double-tap a block to open</span><button type="button" class="icon-btn" id="term-full-close" aria-label="Close">✕</button></header>
      <pre id="term-full-body"></pre>
    </div>
  </div>`;
}

function messageHTML(msg) {
  if (msg.kind === "user") {
    return `<div class="msg user"><div class="bubble">${escapeHtml(msg.text)}${msg.hasImage ? "<div class='meta'>attachment</div>" : ""}</div></div>`;
  }
  if (msg.kind === "thinking") {
    const body = (msg.text || "").trim();
    if (!body) return "";
    const open = state.expanded[`k:${msg.id}`] ?? false;
    const seconds = msg.t0 && msg.t1 ? Math.max(1, Math.round((msg.t1 - msg.t0) / 1000)) : 0;
    const label = msg.done ? (seconds ? `Thought for ${seconds}s` : "Thought about it") : "Thinking…";
    return `<div class="msg thinking"><div class="think">
      <button type="button" class="think-head" data-toggle="k:${escapeHtml(msg.id)}" data-open="${open ? "1" : "0"}">
        <span class="glyph">✳</span><span class="verb">${escapeHtml(label)}</span><span class="chev">${open ? "▾" : "›"}</span>
      </button>
      ${open ? `<div class="think-body">${escapeHtml(body.slice(0, 8000))}</div>` : ""}
    </div></div>`;
  }
  if (msg.kind === "assistant") {
    return `<div class="msg assistant"><div class="bubble">${renderMarkdown(msg.text)}</div></div>`;
  }
  if (msg.kind === "system") {
    return `<div class="msg system"><div class="bubble">${escapeHtml(msg.text)}</div></div>`;
  }
  if (msg.kind === "tool") {
    return `<div class="msg tools"><div class="group open">${toolRowHTML(msg)}</div></div>`;
  }
  if (msg.kind === "ask") {
    const qs = (msg.questions || [])
      .map((q, i) => {
        const opts = (q.options || [])
          .map((o) => `<button type="button" class="btn small" data-answer="${escapeHtml(o.label || o)}">${escapeHtml(o.label || o)}</button>`)
          .join("");
        return `<p>${escapeHtml(q.question || q.header || `Question ${i + 1}`)}</p><div class="row">${opts}</div>`;
      })
      .join("");
    const title = !msg.name || msg.name === "AskUserQuestion" ? "Claude has a question" : msg.name;
    return `<div class="msg ask"><div class="permission"><strong>${escapeHtml(title)}</strong>${qs}</div></div>`;
  }
  if (msg.kind === "permission") {
    if (msg.resolved) {
      return `<div class="msg permission"><div class="permission"><strong>${escapeHtml(msg.name)}</strong><p>${escapeHtml(msg.resolved)}</p></div></div>`;
    }
    const info = toolInfo({ ...msg, status: "running" });
    const diff = msg.diff ? diffHTML(msg) : "";
    const stat = msg.diff ? (() => { const d = diffOf(msg); return statBadge(d.added, d.removed); })() : "";
    const asking = ASK_VERB[msg.name] || `use ${msg.name}`;
    return `<div class="msg permission"><div class="permission">
      <strong>Allow Claude to ${escapeHtml(asking)}?</strong>
      <p class="ask-line"><span class="glyph">${KIND_ICON[info.kind] || "•"}</span>
      <span class="subject ${info.kind === "run" ? "mono" : ""}">${escapeHtml(info.subject || msg.detail || "")}</span>${stat}</p>${diff}
      <div class="row">
        <button class="btn ghost small" data-deny="${escapeHtml(msg.requestId)}">Deny</button>
        <button class="btn small" data-allow="${escapeHtml(msg.requestId)}">Allow</button>
      </div>
    </div></div>`;
  }
  return "";
}

function renderChatChrome() {
  const label = $("#chat-label");
  const dot = $(".dot");
  const stop = $("#stop");
  if (label) {
    label.textContent = state.chatLabel;
    // The idle pill is a set of uppercase tags; a live label is a sentence
    // with a shell command in it, and shouting it is unreadable.
    label.classList.toggle("live", state.chatBusy);
  }
  if (dot) dot.classList.toggle("busy", state.chatBusy);
  if (stop) stop.hidden = !state.chatBusy;
}

function bind() {
  const sitBtn = $("#sit");
  if (sitBtn) {
    sitBtn.onclick = () => {
      state.otp = $("#otp").value;
      sit(state.otp);
    };
    $("#otp").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") sitBtn.click();
    });
  }
  const open = $("#open-chat");
  if (open) open.onclick = openChat;
  const leave = $("#leave");
  if (leave) {
    leave.onclick = () => {
      state.view = "join";
      state.status = "Left the seat. Scan again to sit.";
      render();
    };
  }
  const ship = $("#shipped");
  if (ship) ship.onclick = shipped;
  const byoBtn = $("#byo-start");
  if (byoBtn) byoBtn.onclick = byoStart;
  const byoOff = $("#byo-cancel");
  if (byoOff) byoOff.onclick = byoCancel;
  const byoForm = $("#byo-form");
  if (byoForm) {
    byoForm.onsubmit = (event) => {
      event.preventDefault();
      byoCode($("#byo-code")?.value || "");
    };
  }
  const dep = $("#deploy");
  if (dep) dep.onclick = deploy;
  document.querySelectorAll("[data-fold]").forEach((btn) => {
    btn.onclick = () => {
      const fold = document.getElementById(btn.getAttribute("data-fold"));
      if (!fold) return;
      fold.classList.toggle("open");
      if (!$(".fold.open")) fold.classList.add("open");
    };
  });
  document.querySelectorAll("[data-claim]").forEach((btn) => {
    btn.onclick = () => claim(btn.getAttribute("data-claim"));
  });
  const back = $("#back");
  if (back) {
    back.onclick = () => {
      const full = $("#term-full");
      if (full && !full.hidden) {
        full.hidden = true;
        return;
      }
      if (state.sheet) {
        closeSheet();
        return;
      }
      state.leaveChat = true;
      clearTimeout(state.reconnectTimer);
      if (state.ws) state.ws.close();
      state.view = "floor";
      render();
    };
  }
  bindComposer();
  bindLog();
  bindSheet();
}

function bindComposer() {
  const form = $("#composer");
  const draft = $("#draft");
  if (!form || !draft) return;
  draft.addEventListener("input", () => {
    draft.style.height = "auto";
    draft.style.height = Math.min(draft.scrollHeight, 128) + "px";
    if (draft.value === "/") {
      state.sheet = "slash";
      render();
      $("#draft")?.focus();
    }
  });
  form.onsubmit = (ev) => {
    ev.preventDefault();
    const text = draft.value;
    draft.value = "";
    draft.style.height = "auto";
    sendUser(text);
  };
  $("#stop")?.addEventListener("click", () => wsSend({ type: "interrupt" }));
  $("#slash")?.addEventListener("click", () => {
    state.sheet = state.sheet === "slash" ? null : "slash";
    render();
  });
  $("#menu")?.addEventListener("click", () => {
    state.sheet = state.sheet === "menu" ? null : "menu";
    render();
  });
  $("#plus")?.addEventListener("click", () => {
    if (state.sheet === "files") {
      closeSheet();
      return;
    }
    loadFiles(state.filePath);
  });
  $("#photo")?.addEventListener("change", (ev) => ingestFiles(ev.target.files));
  $("#library")?.addEventListener("change", (ev) => ingestFiles(ev.target.files));
}

function openTerm(text) {
  const full = $("#term-full");
  const body = $("#term-full-body");
  if (!full || !body) return;
  body.textContent = text || "";
  full.hidden = false;
  body.scrollTop = 0;
  body.scrollLeft = 0;
}

function bindTermExpand(el) {
  if (el.dataset.expandBound) return;
  el.dataset.expandBound = "1";
  el.addEventListener("dblclick", () => openTerm(el.textContent));
  let last = 0;
  el.addEventListener("touchend", (ev) => {
    const now = Date.now();
    if (now - last < 350) {
      ev.preventDefault();
      openTerm(el.textContent);
    }
    last = now;
  });
}

function bindLog() {
  const useBrief = $("#use-brief");
  if (useBrief) {
    useBrief.onclick = () => {
      const item = state.join?.item;
      if (!item) return;
      sendUser(`I claimed this brief: ${item.title}\n\n${item.brief}\n\nHelp me ship it on this seat.`);
    };
  }
  document.querySelectorAll("[data-allow]").forEach((btn) => {
    btn.onclick = () => answerPermission(btn.getAttribute("data-allow"), true);
  });
  document.querySelectorAll("[data-deny]").forEach((btn) => {
    btn.onclick = () => answerPermission(btn.getAttribute("data-deny"), false);
  });
  document.querySelectorAll("[data-answer]").forEach((btn) => {
    btn.onclick = () => sendUser(btn.getAttribute("data-answer"));
  });
  document.querySelectorAll(".copy").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const wrap = btn.closest(".code") || btn.parentElement;
      const code = wrap?.querySelector("code, pre");
      navigator.clipboard?.writeText(code?.textContent || "");
      btn.textContent = "Copied";
      setTimeout(() => {
        btn.textContent = "Copy";
      }, 1200);
    };
  });
  document.querySelectorAll("pre.term").forEach((el) => bindTermExpand(el));
  const closeFull = $("#term-full-close");
  if (closeFull && !closeFull.dataset.bound) {
    closeFull.dataset.bound = "1";
    closeFull.addEventListener("click", () => {
      const full = $("#term-full");
      if (full) full.hidden = true;
    });
  }
  document.querySelectorAll("[data-suggest]").forEach((btn) => {
    btn.onclick = () => sendUser(btn.getAttribute("data-suggest"));
  });
  document.querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      state.expanded[btn.getAttribute("data-toggle")] = btn.getAttribute("data-open") !== "1";
      render();
    };
  });
}

function bindSheet() {
  $("#sheet-close")?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    closeSheet();
  });
  $("#sheet-backdrop")?.addEventListener("click", (ev) => {
    if (ev.target.id === "sheet-backdrop") closeSheet();
  });
  document.querySelectorAll("[data-slash]").forEach((btn) => {
    btn.onclick = () => {
      const cmd = btn.getAttribute("data-slash");
      const entry = SLASH.find((s) => s.cmd === cmd) || { cmd };
      runSlash(entry);
    };
  });
  document.querySelectorAll("[data-mode]").forEach((btn) => {
    btn.onclick = () => setMode(btn.getAttribute("data-mode"));
  });
  document.querySelectorAll("[data-dir]").forEach((btn) => {
    btn.onclick = () => loadFiles(btn.getAttribute("data-dir") || "");
  });
  document.querySelectorAll("[data-file]").forEach((btn) => {
    btn.onclick = () => {
      const path = btn.getAttribute("data-file");
      const draft = $("#draft");
      if (draft) {
        draft.value = `${draft.value}${draft.value && !draft.value.endsWith(" ") ? " " : ""}@${path} `;
        draft.focus();
      }
      state.sheet = null;
      render();
    };
  });
  $("#take-photo")?.addEventListener("click", () => $("#photo")?.click());
  $("#pick-photo")?.addEventListener("click", () => $("#library")?.click());
}

async function loadFiles(rel) {
  try {
    const data = await api(`/local/workspace?ticket=${encodeURIComponent(state.ticket)}&path=${encodeURIComponent(rel || "")}`);
    state.filePath = data.cwd || "";
    state.files = data;
    state.sheet = "files";
  } catch (err) {
    state.status = err.message;
    state.sheet = null;
  }
  render();
}

async function ingestFiles(fileList) {
  const files = [...(fileList || [])].slice(0, 4);
  for (const file of files) {
    const packed = await compressImage(file);
    if (packed) state.images.push(packed);
  }
  render();
}

function compressImage(file) {
  return new Promise((resolve) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const scale = Math.min(1, 1280 / Math.max(img.width, img.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      const dataUrl = canvas.toDataURL("image/jpeg", 0.82);
      URL.revokeObjectURL(url);
      const data = dataUrl.split(",")[1] || "";
      resolve(data ? { media_type: "image/jpeg", data } : null);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve(null);
    };
    img.src = url;
  });
}

// Only chase the bottom when the guest is already there, so opening a step
// they scrolled back to does not throw them forward again.
function nearBottom() {
  const log = $("#log");
  if (!log) return true;
  return log.scrollHeight - log.scrollTop - log.clientHeight < 96;
}

function scrollLog(force) {
  const log = $("#log");
  if (log && (force || state.stick !== false)) log.scrollTop = log.scrollHeight;
}

function fitViewport() {
  const vv = window.visualViewport;
  const height = vv ? vv.height : window.innerHeight;
  document.documentElement.style.setProperty("--vvh", `${height}px`);
  if (state.view === "chat") scrollLog();
}

async function seatNetwork() {
  try {
    const res = await fetch("/local/status");
    if (!res.ok) return;
    const status = await res.json();
    state.onWifi = status.guest_net !== "public";
    if (state.onWifi && state.status.startsWith("Open the slip")) {
      state.status = "Same Wi-Fi as the seat PC. " + state.status;
    }
  } catch (err) {
    // Not knowing is fine: the neutral copy is true either way.
  }
}

async function boot() {
  await seatNetwork();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/guest/sw.js", { scope: "/guest/" }).catch(() => {});
  }
  fitViewport();
  ensureTimer();
  window.visualViewport?.addEventListener("resize", fitViewport);
  window.addEventListener("resize", fitViewport);
  const saved = loadStore();
  if (!state.otp && saved.otp) state.otp = saved.otp;
  if (!state.ticket && saved.ticket) state.ticket = saved.ticket;
  if (params.get("view") === "chat" && state.ticket) {
    state.view = "chat";
    render();
    connectChat();
    if (state.otp) sit(state.otp);
    return;
  }
  if (state.otp) {
    await sit(state.otp);
    return;
  }
  render();
}

boot();
