const params = new URLSearchParams(location.search);
const otp = params.get("otp");
const house = "";
const $ = (id) => document.getElementById(id);
let sessionId = null;
let termSocket = null;

async function load() {
  if (otp) {
    const res = await fetch(`${house}/api/join?otp=${encodeURIComponent(otp)}`);
    if (!res.ok) {
      $("status").textContent = "slip expired or unknown";
      return;
    }
    const data = await res.json();
    sessionId = data.session.id;
    $("ssid").textContent = data.wifi_ssid || data.seat.pan_ssid || "salon Wi-Fi";
    $("status").textContent = `${data.seat.name} · hello ${data.session.coder_name}`;
    $("session").hidden = false;
    $("session").innerHTML = `<p>Seat TTY: tmux <code>claude-guest</code> · ${data.seat.claude_label}</p>`;
    renderBoard(data.board);
    $("unlock").disabled = false;
    return;
  }
  const res = await fetch(`${house}/api/board`);
  const data = await res.json();
  $("status").textContent = "join the same Wi-Fi as the seat PC, or open this page with ?otp= from the slip";
  renderBoard(data.items);
}

function renderBoard(items) {
  $("board").innerHTML = items
    .map(
      (i) => `<article class="brief" data-id="${i.id}">
        <h3>${i.title}</h3>
        <p>${i.brief}</p>
        <p>${i.wellness_minutes} min · break at ${i.break_after}</p>
        <button type="button" data-claim="${i.id}">Claim this brief</button>
      </article>`
    )
    .join("");
}

$("board").addEventListener("click", async (ev) => {
  const id = ev.target.getAttribute("data-claim");
  if (!id || !sessionId) return;
  const res = await fetch(`${house}/api/sessions/${sessionId}/claim`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ board_id: id }),
  });
  if (!res.ok) {
    $("status").textContent = "claim failed — check in at the desk first";
    return;
  }
  $("status").textContent = "brief claimed · attach the TTY in this browser";
  $("unlock").disabled = false;
});

$("unlock").addEventListener("click", async () => {
  const res = await fetch("/local/unlock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ otp, session_id: sessionId }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("rc").textContent = data.detail || "denied — stay on the same Wi-Fi as the seat PC";
    return;
  }
  $("rc").innerHTML = `Attached via <strong>${data.via}</strong>. Same TTY as <code>${data.ssh}</code> then <code>${data.tmux}</code>.`;
  $("done").hidden = false;
  openTerm();
});

function openTerm() {
  const wrap = $("termwrap");
  wrap.hidden = false;
  const term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    theme: { background: "#1b1612", foreground: "#f4efe4" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("term"));
  fit.fit();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const host = location.port === "8080" ? `${location.hostname}:8787` : location.host;
  const ws = new WebSocket(`${proto}://${host}/term`);
  termSocket = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    ws.send(JSON.stringify({ cols: term.cols, rows: term.rows }));
    term.focus();
  };
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) term.write(new Uint8Array(ev.data));
    else term.write(ev.data);
  };
  ws.onclose = () => term.writeln("\r\n[disconnected]");
  term.onData((data) => {
    if (ws.readyState === 1) ws.send(data);
  });
  window.addEventListener("resize", () => {
    fit.fit();
    if (ws.readyState === 1) ws.send(JSON.stringify({ cols: term.cols, rows: term.rows }));
  });
}

$("done").addEventListener("click", async () => {
  if (!sessionId) return;
  if (termSocket) termSocket.close();
  await fetch(`${house}/api/sessions/${sessionId}/complete`, { method: "POST" });
  $("status").textContent = "shipped. detach and leave the seat.";
});

load().catch((err) => {
  $("status").textContent = err.message;
});
