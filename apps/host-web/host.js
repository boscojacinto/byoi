async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

const jsonHeaders = { "Content-Type": "application/json" };
let projects = [];
let livePick = "";
let lastPane = "floor";

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fillProjectSelect(selected) {
  const sel = $("projectSel");
  if (!sel) return;
  const current = selected || sel.value;
  sel.innerHTML =
    `<option value="">Project</option>` +
    projects.map((p) => `<option value="${p.id}" ${p.id === current ? "selected" : ""}>${escapeHtml(p.name)}</option>`).join("");
}

function spark(seats) {
  const n = seats.length || 1;
  const occ = seats.filter((s) => s.session).length;
  const pts = seats.map((_, i) => {
    const x = 8 + (i / Math.max(n - 1, 1)) * 200;
    const y = 52 - (occ / n) * 36 - Math.min(i, occ) * 4;
    return `${x},${Math.max(10, y)}`;
  });
  const fill = `8,60 ${pts.join(" ")} 208,60`;
  return `<svg class="spark" viewBox="0 0 216 64" preserveAspectRatio="none">
    <polygon fill="rgba(61,255,138,0.16)" points="${fill}"/>
    <polyline fill="none" stroke="#3dff8a" stroke-width="2.5" points="${pts.join(" ")}"/>
  </svg>`;
}

function showPane(name) {
  if (!["floor", "solutions", "live"].includes(name)) name = "floor";
  lastPane = name;
  document.querySelectorAll(".pane").forEach((el) => el.classList.toggle("is-on", el.id === `pane-${name}`));
  document.querySelectorAll(".tab").forEach((btn) => btn.classList.toggle("is-on", btn.getAttribute("data-pane") === name));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "live") refreshLive().catch(() => {});
}

function openModal(id) {
  $(id).hidden = false;
}

function closeModal(id) {
  $(id).hidden = true;
}

function resetSitModal() {
  $("qrStage").hidden = true;
  $("qr").removeAttribute("src");
  $("checkin").hidden = false;
  $("sitTitle").textContent = "Sit a guest";
  $("sitModal").querySelector(".modal-card").classList.remove("has-qr");
}

function showQrOnly() {
  $("checkin").hidden = true;
  $("checkinMsg").textContent = "";
  $("sitTitle").textContent = "";
  $("qrStage").hidden = false;
  $("qr").src = "/last-qr.png?t=" + Date.now();
  $("sitModal").querySelector(".modal-card").classList.add("has-qr");
}

function termLines(session) {
  const live = (session && session.live) || session || {};
  const seat = session && session.seat;
  const hist = live.history || [];
  const lines = [];
  if (live.error) {
    lines.push({ cls: "ask", text: live.error });
    return lines;
  }
  const who = (seat && seat.session && seat.session.coder_name) || (live.gate && live.gate.coder_name);
  if (who || (seat && seat.name)) {
    lines.push({ cls: "user", text: `${who || "guest"} · ${seat && seat.name ? seat.name : "seat"}` });
  }
  if (live.cwd) lines.push({ cls: "", text: `cwd ${live.cwd}` });
  if (live.account) lines.push({ cls: "", text: `account ${live.account}` });
  if (live.quota && (live.quota.five_hour != null || live.quota.seven_day != null)) {
    const five = live.quota.five_hour != null ? `5h ${Math.round(live.quota.five_hour)}%` : "";
    const week = live.quota.seven_day != null ? `7d ${Math.round(live.quota.seven_day)}%` : "";
    lines.push({ cls: "", text: [five, week].filter(Boolean).join(" · ") });
  }
  if (live.handoff_text) lines.push({ cls: "tool", text: "handoff ready · download from the floor" });
  if (live.model) lines.push({ cls: "", text: `${live.model}${live.busy ? " · thinking" : ""}` });
  for (const ev of hist) {
    if (ev.type === "user") lines.push({ cls: "user", text: `$ ${ev.text || ""}` });
    else if (ev.type === "assistant") lines.push({ cls: "", text: ev.text || "" });
    else if (ev.type === "tool") lines.push({ cls: "tool", text: `▸ ${ev.name || "tool"} ${ev.detail || ev.status || ""}` });
    else if (ev.type === "thinking") lines.push({ cls: "tool", text: `# ${ev.text || "…"}` });
    else if (ev.type === "ask") lines.push({ cls: "ask", text: `? ${ev.name || "ask"} ${ev.detail || ""}` });
  }
  if (!hist.length && !live.error) lines.push({ cls: "", text: "waiting for the guest…" });
  lines.push({ cls: "cursor", text: "█" });
  return lines;
}

function renderTerm(session) {
  const lines = termLines(session);
  return `<ol>${lines
    .map((line) => `<li class="${line.cls || ""}">${escapeHtml(line.text)}</li>`)
    .join("")}</ol>`;
}

async function refreshLive() {
  const root = $("live");
  const chips = $("liveSeats");
  let data;
  try {
    data = await api("/api/live");
  } catch (err) {
    root.className = "term empty";
    root.innerHTML = renderTerm({ live: { error: err.message, history: [] } });
    chips.innerHTML = "";
    return;
  }
  const sessions = data.sessions || [];
  if (!sessions.length) {
    livePick = "";
    chips.innerHTML = "";
    root.className = "term empty";
    root.innerHTML = `<ol><li>no guest is sitting</li><li class="cursor">█</li></ol>`;
    return;
  }
  if (!sessions.some((s) => s.seat.id === livePick)) livePick = sessions[0].seat.id;
  chips.innerHTML = sessions
    .map((s) => {
      const name = s.seat.session ? s.seat.session.coder_name : s.seat.name;
      return `<button type="button" class="chip ${s.seat.id === livePick ? "is-on" : ""}" data-live="${s.seat.id}">${escapeHtml(name)}</button>`;
    })
    .join("");
  chips.querySelectorAll("[data-live]").forEach((btn) => {
    btn.onclick = () => {
      livePick = btn.getAttribute("data-live");
      refreshLive().catch(() => {});
    };
  });
  const chosen = sessions.find((s) => s.seat.id === livePick) || sessions[0];
  root.className = "term";
  root.innerHTML = renderTerm(chosen);
  root.scrollTop = root.scrollHeight;
}

function quotaLabel(quota) {
  if (!quota) return "";
  const bits = [];
  if (quota.five_hour != null) bits.push(`5h ${Math.round(quota.five_hour)}%`);
  if (quota.seven_day != null) bits.push(`7d ${Math.round(quota.seven_day)}%`);
  return bits.join(" · ");
}

async function refresh() {
  const [{ seats }, { items }, proj, health, accounts, live] = await Promise.all([
    api("/api/seats"),
    api("/api/board"),
    api("/api/projects").catch(() => ({ projects: [] })),
    api("/api/health").catch(() => ({ ok: false })),
    api("/api/claude-accounts").catch(() => ({ accounts: [] })),
    api("/api/live").catch(() => ({ sessions: [] })),
  ]);
  projects = proj.projects || [];
  fillProjectSelect();

  const occ = seats.filter((s) => s.session);
  const open = seats.filter((s) => !s.session);
  const claude = accounts.accounts || [];
  const readyLogins = claude.filter((a) => a.credentialed && !a.limited).length;
  const liveBySeat = {};
  (live.sessions || []).forEach((row) => {
    if (row.seat && row.seat.id) liveBySeat[row.seat.id] = row.live || {};
  });
  $("today").innerHTML = `
    <strong>${occ.length ? "Busy" : "Calm"}</strong>
    <p class="quiet">${occ.length} sitting · ${open.length} open${claude.length ? ` · ${readyLogins} Claude login${readyLogins === 1 ? "" : "s"} ready` : ""}</p>
    ${spark(seats)}`;

  $("seats").innerHTML = seats
    .map((s) => {
      const sess = s.session;
      const snap = liveBySeat[s.id] || {};
      const limited = (snap.accounts || claude).some((a) => a.in_use && a.limited);
      const account = snap.account || "";
      const quota = quotaLabel(snap.quota);
      const handoff = sess && (snap.handoff || snap.handoff_text);
      const sub = sess
        ? `${escapeHtml(sess.coder_name)}${account ? ` · ${escapeHtml(account)}` : ""}${quota ? ` · ${escapeHtml(quota)}` : ""}`
        : "Ready";
      return `<article class="seat ${sess ? "occupied" : ""} ${limited ? "limited" : ""}">
        <div><strong>${escapeHtml(s.name)}</strong><span class="quiet">${sub}</span></div>
        <div class="seat-actions">
          ${handoff ? `<button type="button" class="text" data-handoff="${escapeHtml(sess.id)}">Handoff</button>` : ""}
          ${sess ? `<button type="button" data-free="${s.id}">End</button>` : ""}
        </div>
      </article>`;
    })
    .join("");
  document.querySelectorAll("[data-handoff]").forEach((btn) => {
    btn.onclick = async () => {
      const sid = btn.getAttribute("data-handoff");
      try {
        const res = await fetch(`/api/sessions/${sid}/handoff`);
        if (!res.ok) throw new Error("no handoff yet");
        const body = await res.text();
        const blob = new Blob([body], { type: "text/markdown" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `byoi-handoff-${sid}.md`;
        a.click();
      } catch (err) {
        $("checkinMsg").textContent = err.message;
      }
    };
  });
  document.querySelectorAll("[data-free]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/seats/${btn.getAttribute("data-free")}/free`, { method: "POST", headers: jsonHeaders });
        await refresh();
      } catch (err) {
        $("checkinMsg").textContent = err.message;
      }
    };
  });

  $("board").innerHTML =
    items
      .map((i) => {
        const taken = occ.some((s) => s.session.board_id === i.id);
        return `<article class="q-item">
        <span class="mark ${taken ? "live" : ""}">${taken ? "●" : "✓"}</span>
        <div>
          <strong>${taken ? "Live" : "Next"}: ${escapeHtml(i.title)}</strong>
          <p class="quiet">${escapeHtml(i.project ? i.project.name : "No project yet")} · ${i.wellness_minutes} min</p>
        </div>
      </article>`;
      })
      .join("") || `<p class="quiet">No solutions yet.</p>`;

  const sel = $("seatSel");
  sel.innerHTML = open.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("");
  const msg = $("checkinMsg");
  if ($("qrStage").hidden) {
    if (!open.length) msg.textContent = "All seats are taken.";
    else if (msg.textContent === "All seats are taken.") msg.textContent = "";
  }

  const pct = seats.length ? Math.round((open.length / seats.length) * 100) : 0;
  $("healthBar").style.width = `${Math.max(12, pct)}%`;
  $("healthDesk").textContent = health.ok ? "Desk ok" : "Desk?";
  $("healthSeats").textContent = `${open.length}/${seats.length} open`;

  if (lastPane === "live") await refreshLive().catch(() => {});
}

document.querySelectorAll("[data-pane]").forEach((btn) => {
  btn.onclick = () => showPane(btn.getAttribute("data-pane"));
});
window.addEventListener("hashchange", () => showPane(location.hash.slice(1)));
showPane(location.hash.slice(1) || "floor");

$("openSit").onclick = () => {
  resetSitModal();
  openModal("sitModal");
};
$("closeSit").onclick = () => closeModal("sitModal");
$("sitModal").addEventListener("click", (ev) => {
  if (ev.target.id === "sitModal") closeModal("sitModal");
});

$("openAdd").onclick = () => openModal("addDrawer");
$("closeAdd").onclick = () => closeModal("addDrawer");
$("addDrawer").addEventListener("click", (ev) => {
  if (ev.target.id === "addDrawer") closeModal("addDrawer");
});

$("checkin").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = $("checkinMsg");
  msg.textContent = "Printing slip…";
  try {
    await api("/api/sessions/check-in", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({
        seat_id: $("seatSel").value,
        coder_name: $("coderName").value,
      }),
    });
    showQrOnly();
    await refresh();
  } catch (err) {
    msg.textContent = err.message;
  }
});

$("newBrief").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  await api("/api/board", {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({
      title: fd.get("title"),
      brief: fd.get("brief"),
      wellness_minutes: Number(fd.get("wellness_minutes")),
      break_after: Number(fd.get("break_after")),
      project_id: fd.get("project_id") || null,
      spec: fd.get("spec") || "",
    }),
  });
  ev.target.reset();
  closeModal("addDrawer");
  await refresh();
});

$("freeAll").addEventListener("click", async () => {
  try {
    await api("/api/seats/free-all", { method: "POST", headers: jsonHeaders });
    await refresh();
  } catch (err) {
    $("checkinMsg").textContent = err.message;
  }
});

$("newProject").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const msg = $("projectMsg");
  msg.textContent = "Saving…";
  try {
    const created = await api("/api/projects", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({
        kind: fd.get("kind"),
        name: fd.get("name"),
        url: fd.get("url"),
        path: fd.get("path"),
        description: fd.get("description"),
        private: fd.get("private") === "on",
      }),
    });
    msg.textContent = `${created.name} ready.`;
    ev.target.reset();
    ev.target.querySelector('[name="kind"][value="github"]').checked = true;
    await refresh();
    fillProjectSelect(created.id);
  } catch (err) {
    msg.textContent = err.message;
  }
});

refresh().catch((err) => {
  $("checkinMsg").textContent = err.message;
});
setInterval(() => {
  refresh().catch(() => {});
}, 8000);
setInterval(() => {
  if (lastPane === "live") refreshLive().catch(() => {});
}, 2000);
