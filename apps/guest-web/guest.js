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
  status: "Same Wi-Fi as the seat PC. Open the slip QR, or paste the join URL.",
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

function renderMarkdown(raw) {
  const escaped = escapeHtml(raw);
  const parts = escaped.split(/```([\s\S]*?)```/);
  return parts
    .map((chunk, i) => {
      if (i % 2 === 1) {
        const nl = chunk.indexOf("\n");
        const body = (nl === -1 ? chunk : chunk.slice(nl + 1)).replace(/\n$/, "");
        return `<div class="term-wrap"><button type="button" class="copy">copy</button><pre class="term"><code>${body}</code></pre></div>`;
      }
      return chunk
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|\n)- (.*)/g, "$1• $2")
        .replace(/\n\n/g, "</p><p>")
        .replace(/\n/g, "<br>");
    })
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
    if (msg.delta) {
      upsert({ id, kind, text: (existing?.text || "") + (msg.text || ""), done: !!msg.done });
    } else {
      upsert({ id, kind, text: msg.text || existing?.text || "", done: !!msg.done });
    }
    render();
    return;
  }
  if (kind === "tool" || kind === "ask") {
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

function logHTML() {
  const claimed = state.join?.item;
  if (!state.messages.length) {
    return `<div class="empty">
        <h2>What do you want to ship?</h2>
        <p>${claimed ? escapeHtml(claimed.title) : "Tell Claude what you want to ship."}</p>
        ${claimed ? `<button class="btn small" id="use-brief">Start from this brief</button>` : ""}
      </div>`;
  }
  return state.messages.map(messageHTML).join("");
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
  if (state.view === "chat") scrollLog();
}

function joinHTML() {
  return `<div class="screen">
    <p class="eyebrow">BYOI</p>
    <h1>Have a seat</h1>
    <p class="lede">Same Wi-Fi as this table. Scan the slip, or type the code.</p>
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
  const rows = cases
    .map(
      (c) => `<li class="${c.pass ? "pass" : "fail"}">${c.pass ? "✓" : "✕"} <strong>${escapeHtml(c.name)}</strong>
      ${c.detail ? `<span>${escapeHtml(c.detail)}</span>` : ""}</li>`
    )
    .join("");
  const heading =
    status === "running"
      ? "Testing against the spec…"
      : status === "passed"
        ? `${report?.passed ?? 0} passed`
        : `${report?.failed ?? 0} failed · ${report?.passed ?? 0} passed`;
  return `<div class="screen">
    <p class="eyebrow">Review</p>
    <h1>${escapeHtml(heading)}</h1>
    <p class="lede">${escapeHtml(report?.summary || state.status || "")}</p>
    ${status === "running" ? `<p class="status">Checking your work. Hang tight.</p>` : ""}
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
    const body = escapeHtml((msg.text || "").slice(0, 2000));
    if (!body) return "";
    return `<div class="msg thinking"><div class="thinking">${body}</div></div>`;
  }
  if (msg.kind === "assistant") {
    return `<div class="msg assistant"><div class="bubble"><p>${renderMarkdown(msg.text)}</p></div></div>`;
  }
  if (msg.kind === "system") {
    return `<div class="msg system"><div class="bubble">${escapeHtml(msg.text)}</div></div>`;
  }
  if (msg.kind === "tool") {
    const mark =
      msg.status === "running"
        ? `<span class="spin"></span>`
        : `<span class="mark">${msg.status === "error" ? "!" : "✓"}</span>`;
    const diff = msg.diff
      ? `<div class="diff">${msg.diff.old ? `<div class="old">- ${escapeHtml(msg.diff.old.slice(0, 2500))}</div>` : ""}${
          msg.diff.new ? `<div class="new">+ ${escapeHtml(msg.diff.new.slice(0, 2500))}</div>` : ""
        }</div>`
      : "";
    const rawOut = isNoise(msg.output) ? "" : msg.output || "";
    const out = rawOut ? `<pre class="term">${escapeHtml(rawOut.slice(0, 32000))}</pre>` : "";
    return `<div class="msg tool"><div class="console ${msg.status === "error" ? "error" : ""}">
      <div class="console-bar">${mark}<span class="name">${escapeHtml(msg.name || "tool")}</span>
      <span class="detail">${escapeHtml(msg.detail || "")}</span></div>${diff}${out}
    </div></div>`;
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
    return `<div class="msg ask"><div class="permission"><strong>${escapeHtml(msg.name || "Claude has a question")}</strong>${qs}</div></div>`;
  }
  if (msg.kind === "permission") {
    if (msg.resolved) {
      return `<div class="msg permission"><div class="permission"><strong>${escapeHtml(msg.name)}</strong><p>${escapeHtml(msg.resolved)}</p></div></div>`;
    }
    const diff = msg.diff
      ? `<div class="diff">${msg.diff.old ? `<div class="old">${escapeHtml(msg.diff.old.slice(0, 1200))}</div>` : ""}<div class="new">${escapeHtml((msg.diff.new || "").slice(0, 1200))}</div></div>`
      : "";
    return `<div class="msg permission"><div class="permission">
      <strong>Allow ${escapeHtml(msg.name)}?</strong>
      <p>${escapeHtml(msg.detail || "")}</p>${diff}
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
  if (label) label.textContent = state.chatLabel;
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
      const code = btn.parentElement?.querySelector("code, pre");
      navigator.clipboard?.writeText(code?.textContent || "");
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

function scrollLog() {
  const log = $("#log");
  if (log) log.scrollTop = log.scrollHeight;
}

function fitViewport() {
  const vv = window.visualViewport;
  const height = vv ? vv.height : window.innerHeight;
  document.documentElement.style.setProperty("--vvh", `${height}px`);
  if (state.view === "chat") scrollLog();
}

async function boot() {
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
