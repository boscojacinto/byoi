async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    // The desk is on the public internet; a session can lapse mid-shift.
    showSignIn();
    throw new Error(data.detail || "sign in to the desk");
  }
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function showSignIn(hint) {
  const gate = document.getElementById("signin");
  if (!gate) return;
  if (hint) document.getElementById("signinHint").textContent = hint;
  gate.hidden = false;
  document.getElementById("signinPw").focus();
}

function hideSignIn() {
  const gate = document.getElementById("signin");
  if (gate) gate.hidden = true;
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

function fillTemplateSelect(templates) {
  const sel = document.querySelector("#templateSelect");
  if (!sel) return;
  const chosen = sel.value;
  sel.innerHTML = templates
    .map((t) => `<option value="${t.name}">${t.name} — ${(t.needs || []).join(", ") || "no infra"}</option>`)
    .join("");
  if (chosen) sel.value = chosen;
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
  if (!["floor", "solutions", "qa", "live"].includes(name)) name = "floor";
  lastPane = name;
  document.querySelectorAll(".pane").forEach((el) => el.classList.toggle("is-on", el.id === `pane-${name}`));
  document.querySelectorAll(".tab").forEach((btn) => btn.classList.toggle("is-on", btn.getAttribute("data-pane") === name));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "live") refreshLive().catch(() => {});
  if (name === "qa") {
    refreshQABriefs().catch(() => {});
    refreshQAGrading().catch(() => {});
  }
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

async function waitForSeat(started, msg, tries = 90) {
  msg.textContent = "Raising the seat…";
  for (let i = 0; i < tries; i += 1) {
    const state = await api(started.poll);
    if (state.state === "ready") {
      msg.textContent = "";
      showQrOnly();
      return state;
    }
    if (state.state === "failed") {
      throw new Error(state.error || "the seat did not come up");
    }
    await new Promise((done) => setTimeout(done, 1000));
  }
  throw new Error("the seat is taking longer than expected — check the desk log");
}

async function refreshPrinter() {
  const pill = $("healthPrinter");
  if (!pill) return;
  try {
    const p = await api("/api/print/status");
    if (p.mode !== "relay") {
      // The printer is on this machine; there is no relay to be offline.
      pill.textContent = "Printer local";
      pill.className = "pill";
      return;
    }
    const waiting = (p.queued || 0) + (p.claimed || 0);
    pill.textContent = p.online
      ? waiting
        ? `Printer · ${waiting} waiting`
        : "Printer ok"
      : "Printer offline";
    pill.className = p.online ? "pill is-ok" : "pill is-bad";
    pill.title = p.online
      ? "the counter's relay is polling"
      : "no relay at the counter — slips are queued, the QR is still on screen";
  } catch (err) {
    pill.textContent = "Printer?";
    pill.className = "pill";
  }
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

function caseRows(cases) {
  return (cases || [])
    .map(
      (c) => `<li class="${c.pass ? "pass" : "fail"}">${c.pass ? "✓" : "✕"} <strong>${escapeHtml(c.name)}</strong>
      ${c.detail ? `<span>${escapeHtml(c.detail)}</span>` : ""}</li>`
    )
    .join("");
}

function testBadge(status) {
  const cls = { running: "is-running", passed: "is-passed", failed: "is-failed" }[status] || "";
  const label = { running: "Running", passed: "Passed", failed: "Failed" }[status] || "—";
  return `<span class="badge ${cls}">${label}</span>`;
}

async function refreshQABriefs() {
  const { items } = await api("/api/board");
  $("qaBriefs").innerHTML =
    items
      .map(
        (i) => `<article class="qa-item">
        <div class="card-head"><strong>${escapeHtml(i.title)}</strong></div>
        <form class="qaSpecForm" data-brief="${i.id}">
          <textarea name="spec" placeholder="Plain-English facts, one per line (optional)">${escapeHtml(i.spec || "")}</textarea>
          <div class="row"><button type="submit">Save</button></div>
        </form>
      </article>`
      )
      .join("") || `<p class="quiet">No briefs yet — add one from Solutions.</p>`;
  $("qaBriefs")
    .querySelectorAll(".qaSpecForm")
    .forEach((form) => {
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const id = form.getAttribute("data-brief");
        const fd = new FormData(form);
        const btn = form.querySelector("button");
        const was = btn.textContent;
        btn.textContent = "Saving…";
        try {
          await api(`/api/board/${id}/spec`, {
            method: "POST",
            headers: jsonHeaders,
            body: JSON.stringify({ spec: fd.get("spec") || "" }),
          });
          btn.textContent = "Saved";
        } catch (err) {
          btn.textContent = was;
          alert(err.message);
          return;
        }
        setTimeout(() => (btn.textContent = was), 1200);
      });
    });
}

async function refreshQAGrading() {
  let sessions;
  try {
    ({ sessions } = await api("/api/sessions/grading"));
  } catch (err) {
    $("qaGrading").innerHTML = `<p class="quiet">${escapeHtml(err.message)}</p>`;
    return;
  }
  $("qaGrading").innerHTML =
    sessions
      .map((s) => {
        const report = s.test_report || {};
        const status = s.test_status || "running";
        const heading =
          status === "running"
            ? "Testing against the spec…"
            : status === "passed"
              ? `${report.passed ?? 0} passed`
              : `${report.failed ?? 0} failed · ${report.passed ?? 0} passed`;
        return `<article class="qa-item">
          <div class="card-head">
            <strong>${escapeHtml(s.coder_name)} · ${escapeHtml(s.brief_title || "untitled")}</strong>
            ${testBadge(status)}
          </div>
          <p class="quiet">${escapeHtml(s.seat_name || "")} · ${escapeHtml(heading)}</p>
          ${report.summary ? `<p class="quiet">${escapeHtml(report.summary)}</p>` : ""}
          <ul class="cases">${caseRows(report.cases) || (status === "running" ? "" : "<li>Nothing to report.</li>")}</ul>
        </article>`;
      })
      .join("") || `<p class="quiet">Nothing graded yet — results appear here once a guest taps “I’m done”.</p>`;
}

function quotaLabel(quota) {
  if (!quota) return "";
  const bits = [];
  if (quota.five_hour != null) bits.push(`5h ${Math.round(quota.five_hour)}%`);
  if (quota.seven_day != null) bits.push(`7d ${Math.round(quota.seven_day)}%`);
  return bits.join(" · ");
}

// Solutions are individual tasks, but a project is the unit that deploys and
// the unit a guest gets locked into — so the desk lists tasks grouped under
// their project rather than flat, matching how a guest picks one.
function boardTaskRowHTML(item, occ) {
  const taken = occ.some((s) => s.session.board_id === item.id);
  return `<article class="q-item nested">
    <span class="mark ${taken ? "live" : ""}">${taken ? "●" : "✓"}</span>
    <div>
      <strong>${taken ? "Live" : "Next"}: ${escapeHtml(item.title)}</strong>
      <p class="quiet">${item.wellness_minutes} min</p>
    </div>
  </article>`;
}

function boardGroupsHTML(items, occ) {
  const groups = new Map();
  const solo = [];
  for (const item of items) {
    if (!item.project) {
      solo.push(item);
      continue;
    }
    const key = item.project.id;
    if (!groups.has(key)) groups.set(key, { project: item.project, busy: !!item.project_busy, items: [] });
    groups.get(key).items.push(item);
  }
  const projectBlocks = [...groups.values()]
    .map((group) => {
      const count = group.items.length;
      return `<article class="q-group${group.busy ? " busy" : ""}">
        <div class="q-group-head">
          <strong>${escapeHtml(group.project.name)}</strong>
          <span class="quiet">${count} task${count === 1 ? "" : "s"}${group.busy ? " · in progress" : ""}</span>
        </div>
        ${group.items.map((item) => boardTaskRowHTML(item, occ)).join("")}
      </article>`;
    })
    .join("");
  const soloRows = solo
    .map((item) => {
      const taken = occ.some((s) => s.session.board_id === item.id);
      return `<article class="q-item">
        <span class="mark ${taken ? "live" : ""}">${taken ? "●" : "✓"}</span>
        <div>
          <strong>${taken ? "Live" : "Next"}: ${escapeHtml(item.title)}</strong>
          <p class="quiet">No project yet · ${item.wellness_minutes} min</p>
        </div>
      </article>`;
    })
    .join("");
  return projectBlocks + soloRows;
}

async function refresh() {
  const [{ seats }, { items }, proj, tpl, health, accounts, live] = await Promise.all([
    api("/api/seats"),
    api("/api/board"),
    api("/api/projects").catch(() => ({ projects: [] })),
    api("/api/templates").catch(() => ({ templates: [] })),
    api("/api/health").catch(() => ({ ok: false })),
    api("/api/claude-accounts").catch(() => ({ accounts: [] })),
    api("/api/live").catch(() => ({ sessions: [] })),
  ]);
  projects = proj.projects || [];
  fillProjectSelect();
  fillTemplateSelect(tpl.templates || []);

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

  $("board").innerHTML = boardGroupsHTML(items, occ) || `<p class="quiet">No solutions yet.</p>`;

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
  await refreshPrinter();

  if (lastPane === "live") await refreshLive().catch(() => {});
  if (lastPane === "qa") await refreshQAGrading().catch(() => {});
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
    const started = await api("/api/sessions/check-in", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({
        seat_id: $("seatSel").value,
        coder_name: $("coderName").value,
      }),
    });
    if (started.state === "preparing") {
      // The seat is a container being raised right now. Showing the QR before
      // it answers would hand the guest a code for an address that 404s.
      await waitForSeat(started, msg);
    } else {
      showQrOnly();
    }
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

$("fetchProject").addEventListener("click", async () => {
  const id = $("projectSel").value;
  const msg = $("fetchMsg");
  if (!id) {
    msg.textContent = "Pick a project first.";
    return;
  }
  msg.textContent = "Cloning…";
  try {
    const res = await api(`/api/projects/${id}/fetch`, { method: "POST", headers: jsonHeaders });
    msg.textContent = `Ready at ${res.local_path}`;
  } catch (err) {
    msg.textContent = err.message;
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
        template: fd.get("template"),
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

$("signinForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = $("signinMsg");
  msg.textContent = "";
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ password: $("signinPw").value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    $("signinPw").value = "";
    hideSignIn();
    await boot();
  } catch (err) {
    msg.textContent = err.message;
  }
});

$("signOut").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  showSignIn("Signed out.");
});

async function boot() {
  const session = await api("/api/session");
  if (!session.signed_in) {
    showSignIn(
      session.password_set
        ? "Sign in to open the floor."
        : "No operator password yet — run scripts/salon-secrets.sh operator on the desk."
    );
    return;
  }
  hideSignIn();
  await refresh();
}

boot().catch((err) => {
  $("checkinMsg").textContent = err.message;
});
setInterval(() => {
  if (!$("signin").hidden) return;
  refresh().catch(() => {});
}, 8000);
setInterval(() => {
  if (lastPane === "live" && $("signin").hidden) refreshLive().catch(() => {});
}, 2000);
setInterval(() => {
  if (lastPane === "qa" && $("signin").hidden) refreshQAGrading().catch(() => {});
}, 3000);
