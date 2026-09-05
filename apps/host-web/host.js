// FastAPI's own validation errors put a list of {msg, loc} objects in
// `detail`, not a string — stringify that shape instead of letting it fall
// through to the default Object/Array toString ("[object Object]").
function errorDetail(detail, fallback) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((d) => (d && typeof d === "object" ? d.msg || JSON.stringify(d) : String(d))).join("; ");
  }
  return fallback;
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    // The desk is on the public internet; a session can lapse mid-shift.
    showSignIn();
    throw new Error(errorDetail(data.detail, "sign in to the desk"));
  }
  if (!res.ok) throw new Error(errorDetail(data.detail, res.statusText));
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
let githubApp = { configured: false, slug: null };
let livePick = "";
let lastPane = "floor";
let openSpecId = null;
let openGradingId = null;

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

const PROJECT_KIND_HINTS = {
  template: "Copies the chosen starter into a new folder on this server.",
  clone: "Clones a repo you already have on GitHub, by its URL.",
  github: "Creates a brand-new, empty repo on GitHub — needs gh auth login on this server.",
  local: "Uses a folder that already exists on this server's disk.",
};

// Each field is tagged data-kind="template clone …" for the modes it applies
// to; everything else stays hidden so a field that does nothing for the
// selected mode (e.g. "GitHub link" while creating a brand-new repo) is
// never visible to fill in.
function updateProjectKindFields() {
  const form = $("newProject");
  const kind = form.querySelector('input[name="kind"]:checked')?.value || "template";
  $("projectHint").textContent = PROJECT_KIND_HINTS[kind] || "";
  form.querySelectorAll("[data-kind]").forEach((el) => {
    el.hidden = !el.dataset.kind.split(" ").includes(kind);
  });
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

// A github.com remote, not any git host — mirrors projects.github_repo_slug.
function isGithubProject(project) {
  return /github\.com[:/]/.test((project && project.github) || "");
}

// Only offered right after a *new* project is created (see the newProject
// submit handler) — an existing project's GitHub App link, if any, was
// already made when that project was first created.
async function maybeOfferGithubAppLink(project) {
  if (!isGithubProject(project)) return;
  const msg = $("githubAppModalMsg");
  const action = $("githubAppModalAction");
  if (!githubApp.configured) {
    msg.textContent =
      "Set up the desk's GitHub App once so this project's issues sync into Solutions automatically.";
    action.textContent = "Set up GitHub App";
    action.onclick = () => {
      window.location.href = "/api/github/app/new";
    };
  } else {
    msg.textContent = `Install the GitHub App on ${project.name} so its issues sync automatically.`;
    action.textContent = "Link GitHub App to this repo";
    action.onclick = async () => {
      try {
        const res = await api(`/api/projects/${project.id}/github-app-install-url`);
        window.location.href = res.url;
      } catch (err) {
        msg.textContent = err.message;
      }
    };
  }
  openModal("githubAppModal");
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
  // GitHub sync happens here — on demand, every time the tab opens — and
  // nowhere else. It used to ride the 8s poll in refresh(), syncing every
  // GitHub-linked project on a timer regardless of which tab was open.
  if (name === "solutions") refreshSolutions().catch(() => {});
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
  $("qrGuest").textContent = "";
  $("qrSeat").textContent = "";
  $("qrProject").textContent = "";
  $("checkin").hidden = false;
  $("sitTitle").textContent = "Sit a guest";
  $("sitModal").querySelector(".modal-card").classList.remove("has-qr");
}

async function waitForSeat(started, msg, coderName, seatName, tries = 90) {
  msg.textContent = "Raising the seat…";
  for (let i = 0; i < tries; i += 1) {
    const state = await api(started.poll);
    if (state.state === "ready") {
      msg.textContent = "";
      showQrOnly(coderName, seatName);
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

function showQrOnly(coderName, seatName) {
  $("checkin").hidden = true;
  $("checkinMsg").textContent = "";
  $("sitTitle").textContent = "";
  $("qrGuest").textContent = coderName || "";
  $("qrSeat").textContent = seatName ? `Seat · ${seatName}` : "";
  $("qrProject").textContent = "Project · not picked yet";
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

// Collapsed to one line by default so a whole project's worth of briefs or
// results fits on screen; opening one closes any other open in the same
// list, so the open item gets the pane's full height instead of a cramped
// fixed-size box (see .qa-item[open] textarea in salon.css).
function wireAccordion(container, getOpenId, setOpenId) {
  container.querySelectorAll("details.qa-item").forEach((det) => {
    det.addEventListener("toggle", () => {
      const id = det.getAttribute("data-id");
      if (det.open) {
        container.querySelectorAll("details.qa-item[open]").forEach((other) => {
          if (other !== det) other.open = false;
        });
        setOpenId(id);
      } else if (getOpenId() === id) {
        setOpenId(null);
      }
    });
  });
}

function specPreview(spec) {
  const line = (spec || "")
    .split("\n")
    .map((l) => l.trim())
    .find(Boolean);
  return line || "No spec yet — tap to add plain-English facts.";
}

function occupantBadge(item, occ) {
  const seat = occ.find((s) => s.session.board_id === item.id);
  if (!seat) return "";
  return `<span class="badge is-running">${escapeHtml(seat.session.coder_name)} · ${escapeHtml(seat.name)}</span>`;
}

function specItemHTML(item, occ) {
  return `<details class="qa-item" data-id="${item.id}" ${item.id === openSpecId ? "open" : ""}>
    <summary>
      <strong>${escapeHtml(item.title)}</strong>
      ${occupantBadge(item, occ)}
      <p class="quiet qa-item-preview">${escapeHtml(specPreview(item.spec))}</p>
    </summary>
    <form class="qaSpecForm" data-brief="${item.id}">
      <textarea name="spec" placeholder="Plain-English facts, one per line (optional)">${escapeHtml(item.spec || "")}</textarea>
      <div class="row"><button type="submit">Save</button></div>
    </form>
  </details>`;
}

// Briefs belong to a project the same way Solutions does (boardGroupsHTML) —
// grouping them the same way here means "which project is this for" reads
// the same across both tabs.
function qaBriefGroupsHTML(items, occ) {
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
      return `<div class="q-group${group.busy ? " busy" : ""}">
        <div class="q-group-head">
          <strong>${escapeHtml(group.project.name)}</strong>
          <span class="quiet">${count} brief${count === 1 ? "" : "s"}${group.busy ? " · in progress" : ""}</span>
        </div>
        ${group.items.map((item) => specItemHTML(item, occ)).join("")}
      </div>`;
    })
    .join("");
  const soloBlock = solo.length
    ? `<div class="q-group">
        <div class="q-group-head"><strong>No project</strong><span class="quiet">${solo.length} brief${solo.length === 1 ? "" : "s"}</span></div>
        ${solo.map((item) => specItemHTML(item, occ)).join("")}
      </div>`
    : "";
  return projectBlocks + soloBlock;
}

async function refreshQABriefs() {
  const [{ items }, { seats }] = await Promise.all([api("/api/board"), api("/api/seats")]);
  const occ = seats.filter((s) => s.session);
  $("qaBriefs").innerHTML =
    qaBriefGroupsHTML(items, occ) || `<p class="quiet">No briefs yet — add one from Solutions.</p>`;
  wireAccordion($("qaBriefs"), () => openSpecId, (id) => (openSpecId = id));
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

function gradingHeading(status, report) {
  if (status === "running") return "Testing against the spec…";
  if (status === "passed") return `${report.passed ?? 0} passed`;
  return `${report.failed ?? 0} failed · ${report.passed ?? 0} passed`;
}

// Rebuilds a filter <select>'s options from live data while keeping whatever
// the operator already picked selected — same trick as fillProjectSelect —
// so re-polling every few seconds doesn't reset an in-progress filter.
function fillFilterSelect(sel, options, allLabel) {
  const current = sel.value;
  sel.innerHTML =
    `<option value="">${allLabel}</option>` +
    options
      .map((o) => `<option value="${escapeHtml(o.value)}" ${o.value === current ? "selected" : ""}>${escapeHtml(o.label)}</option>`)
      .join("");
  if (options.some((o) => o.value === current)) sel.value = current;
}

async function refreshQAGrading() {
  let sessions, items;
  try {
    [{ sessions }, { items }] = await Promise.all([api("/api/sessions/grading"), api("/api/board")]);
  } catch (err) {
    $("qaGrading").innerHTML = `<p class="quiet">${escapeHtml(err.message)}</p>`;
    return;
  }
  const projectByBoard = new Map(items.filter((i) => i.project).map((i) => [i.id, i.project]));

  const projectOptions = [...new Map(items.filter((i) => i.project).map((i) => [i.project.id, i.project.name])).entries()].map(
    ([value, label]) => ({ value, label })
  );
  fillFilterSelect($("qaFilterProject"), projectOptions, "All projects");
  const guestOptions = [...new Set(sessions.map((s) => s.coder_name).filter(Boolean))]
    .sort()
    .map((g) => ({ value: g, label: g }));
  fillFilterSelect($("qaFilterGuest"), guestOptions, "All guests");

  const filterProject = $("qaFilterProject").value;
  const filterGuest = $("qaFilterGuest").value;
  const filterStatus = $("qaFilterStatus").value;

  const filtered = sessions.filter((s) => {
    if (filterStatus && (s.test_status || "running") !== filterStatus) return false;
    if (filterGuest && s.coder_name !== filterGuest) return false;
    if (filterProject && (projectByBoard.get(s.board_id) || {}).id !== filterProject) return false;
    return true;
  });

  $("qaGrading").innerHTML =
    filtered
      .map((s) => {
        const report = s.test_report || {};
        const status = s.test_status || "running";
        const proj = projectByBoard.get(s.board_id);
        return `<details class="qa-item" data-id="${s.id}" ${s.id === openGradingId ? "open" : ""}>
          <summary>
            <strong>${escapeHtml(s.coder_name)} · ${escapeHtml(s.brief_title || "untitled")}</strong>
            ${testBadge(status)}
            <p class="quiet qa-item-preview">${escapeHtml(s.seat_name || "")}${proj ? ` · ${escapeHtml(proj.name)}` : ""} · ${escapeHtml(gradingHeading(status, report))}</p>
          </summary>
          ${report.summary ? `<p class="quiet">${escapeHtml(report.summary)}</p>` : ""}
          <ul class="cases">${caseRows(report.cases) || (status === "running" ? "" : "<li>Nothing to report.</li>")}</ul>
        </details>`;
      })
      .join("") ||
    `<p class="quiet">${
      sessions.length ? "No results match these filters." : "Nothing graded yet — results appear here once a guest taps “I’m done”."
    }</p>`;
  wireAccordion($("qaGrading"), () => openGradingId, (id) => (openGradingId = id));
}

function formatResetTime(epoch) {
  if (epoch == null) return "";
  const ms = Number(epoch) * 1000;
  if (!Number.isFinite(ms)) return "";
  const diff = ms - Date.now();
  if (diff <= 0) return "now";
  const mins = Math.round(diff / 60000);
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  if (hrs < 24) return `in ${hrs}h${rem ? ` ${rem}m` : ""}`;
  const d = new Date(ms);
  return `${d.toLocaleDateString(undefined, { weekday: "short" })} ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}

function formatHourLabel(hourKey) {
  const d = new Date(hourKey);
  if (Number.isNaN(d.getTime())) return hourKey;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" });
}

function quotaLines(quota) {
  if (!quota) return [];
  const lines = [];
  if (quota.five_hour != null) {
    const reset = formatResetTime(quota.five_hour_resets);
    lines.push(`5h ${Math.round(quota.five_hour)}%${reset ? ` · resets ${reset}` : ""}`);
  }
  if (quota.seven_day != null) {
    const reset = formatResetTime(quota.seven_day_resets);
    lines.push(`7d ${Math.round(quota.seven_day)}%${reset ? ` · resets ${reset}` : ""}`);
  }
  return lines;
}

// One hue, stepped light->dark (ordinal ramp, not categorical — validated with
// --ordinal against the panel surface #0c120e: monotone lightness, >=0.06
// adjacent gaps, single hue). Keeps every chart in the salon's own neon green
// rather than an unrelated rainbow; a legend + hover values carry identity
// where hue no longer can.
const CHART_PALETTE = ["#3dff8a", "#22c274", "#1a8f52", "#0f6339"];
const CHART_OTHER = "#3a4a3f";
let usageGroupBy = "seat";

function formatTokenShort(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
}

function formatCount(n) {
  return String(Math.round(n));
}

function localDateStr(d) {
  // The server buckets by local date (datetime.astimezone()); toISOString()
  // converts to UTC first, which rolls the date back a day anywhere east of
  // UTC (e.g. local midnight in IST is still "yesterday" in UTC) — build the
  // string from local getters instead so the two sides always agree.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function dateRange(days) {
  const out = [];
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  for (let i = days - 1; i >= 0; i--) {
    const dt = new Date(d);
    dt.setDate(dt.getDate() - i);
    out.push(localDateStr(dt));
  }
  return out;
}

function formatAxisDate(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function prepareBarLines(rawSeries, dates, valueKey) {
  // A 5th+ series folds into "Other" rather than stretching the ramp further.
  const ranked = rawSeries
    .map((s) => ({ ...s, total: s.points.reduce((a, p) => a + p[valueKey], 0) }))
    .sort((a, b) => b.total - a.total);
  const shown = ranked.slice(0, CHART_PALETTE.length);
  const rest = ranked.slice(CHART_PALETTE.length);

  const seriesForDates = (s) => {
    const byDate = Object.fromEntries(s.points.map((p) => [p.date, p[valueKey]]));
    return dates.map((d) => byDate[d] || 0);
  };
  const lines = shown.map((s, i) => ({ key: s.key, color: CHART_PALETTE[i], values: seriesForDates(s) }));
  if (rest.length) {
    const merged = dates.map((_, idx) => rest.reduce((sum, s) => sum + seriesForDates(s)[idx], 0));
    lines.push({ key: "Other", color: CHART_OTHER, values: merged });
  }
  return lines;
}

function renderBarChart(wrap, lines, dates, { formatValue = formatTokenShort, showLegend = true } = {}) {
  const W = 600,
    H = 190,
    padL = 40,
    padR = 10,
    padT = 10,
    padB = 20;
  const innerW = W - padL - padR,
    innerH = H - padT - padB;
  const totals = dates.map((_, i) => lines.reduce((sum, l) => sum + l.values[i], 0));
  const maxY = Math.max(1, ...totals);
  const slot = innerW / dates.length;
  const barW = Math.max(2, slot * 0.6);
  const xCenter = (i) => padL + slot * (i + 0.5);
  const yTop = (v) => padT + innerH - (v / maxY) * innerH;

  const gridY = [0, 0.5, 1]
    .map((t) => {
      const val = maxY * t;
      return `<line x1="${padL}" y1="${yTop(val)}" x2="${W - padR}" y2="${yTop(val)}" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>
      <text x="${padL - 6}" y="${yTop(val) + 3}" text-anchor="end" font-size="8" fill="#7d9788">${formatValue(val)}</text>`;
    })
    .join("");

  const xTicks = [...new Set([0, Math.floor((dates.length - 1) / 2), dates.length - 1])]
    .map(
      (i) =>
        `<text x="${xCenter(i)}" y="${H - 4}" text-anchor="middle" font-size="8" fill="#7d9788">${escapeHtml(formatAxisDate(dates[i]))}</text>`
    )
    .join("");

  // Stacked per date: each series a rounded segment, baseline-anchored, with a
  // 1px surface gap between adjacent segments (skip the gap on a lone series).
  const bars = dates
    .map((_, i) => {
      const bx = xCenter(i) - barW / 2;
      let cumulative = 0;
      return lines
        .map((l) => {
          const v = l.values[i];
          if (v <= 0) return "";
          const gap = lines.length > 1 ? 1 : 0;
          const segH = Math.max(0, (v / maxY) * innerH - gap);
          const segY = padT + innerH - cumulative - (v / maxY) * innerH + gap;
          cumulative += (v / maxY) * innerH;
          return `<rect x="${bx}" y="${segY}" width="${barW}" height="${segH}" rx="2" fill="${l.color}"/>`;
        })
        .join("");
    })
    .join("");

  const legend =
    showLegend && lines.length > 1
      ? `<div class="usage-legend">${lines.map((l) => `<span class="usage-legend-item"><i style="background:${l.color}"></i>${escapeHtml(l.key)}</span>`).join("")}</div>`
      : "";

  const tableRows = dates
    .map(
      (d, i) =>
        `<tr><td>${escapeHtml(formatAxisDate(d))}</td>${lines.map((l) => `<td>${l.values[i].toLocaleString()}</td>`).join("")}</tr>`
    )
    .join("");

  wrap.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" class="usage-svg" preserveAspectRatio="none">
      ${gridY}
      ${bars}
      ${xTicks}
      <rect class="usage-hover-rect" x="${padL}" y="${padT}" width="${innerW}" height="${innerH}" fill="transparent"/>
    </svg>
    ${legend}
    <div class="usage-tooltip" hidden></div>
    <details class="usage-table-toggle">
      <summary>Show as table</summary>
      <div class="usage-table-scroll">
        <table class="usage-table"><thead><tr><th>Date</th>${lines.map((l) => `<th>${escapeHtml(l.key)}</th>`).join("")}</tr></thead><tbody>${tableRows}</tbody></table>
      </div>
    </details>
  `;

  wireBarHover(wrap, dates, lines, { W, H, padL, padT, innerW, innerH, slot }, formatValue);
}

function wireBarHover(wrap, dates, lines, geo, formatValue) {
  const svg = wrap.querySelector("svg");
  const rect = wrap.querySelector(".usage-hover-rect");
  const tooltip = wrap.querySelector(".usage-tooltip");
  if (!svg || !rect || !tooltip) return;
  let marker = null;

  const move = (ev) => {
    const box = svg.getBoundingClientRect();
    const scaleX = geo.W / box.width;
    const px = (ev.clientX - box.left) * scaleX;
    const idx = Math.min(dates.length - 1, Math.max(0, Math.floor((px - geo.padL) / geo.slot)));
    if (!marker) {
      marker = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      marker.setAttribute("class", "usage-hover-band");
      marker.setAttribute("y", String(geo.padT));
      marker.setAttribute("height", String(geo.innerH));
      marker.setAttribute("width", String(geo.slot));
      svg.insertBefore(marker, svg.firstChild);
    }
    marker.setAttribute("x", String(geo.padL + idx * geo.slot));
    tooltip.hidden = false;
    const total = lines.reduce((sum, l) => sum + l.values[idx], 0);
    const rows =
      lines.length > 1
        ? lines
            .filter((l) => l.values[idx] > 0)
            .map((l) => `<span><i style="background:${l.color}"></i>${escapeHtml(l.key)} — ${formatValue(l.values[idx])}</span>`)
            .join("") +
          (lines.filter((l) => l.values[idx] > 0).length > 1
            ? `<span class="usage-tooltip-total">Total — ${formatValue(total)}</span>`
            : "")
        : `<span>${formatValue(total)}</span>`;
    tooltip.innerHTML = `<strong>${escapeHtml(formatAxisDate(dates[idx]))}</strong>${rows || `<span class="quiet">No data</span>`}`;
    const boxRect = wrap.getBoundingClientRect();
    const left = Math.min(boxRect.width - 170, Math.max(0, ev.clientX - boxRect.left + 10));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `4px`;
  };
  const leave = () => {
    tooltip.hidden = true;
    if (marker) marker.remove();
    marker = null;
  };
  rect.addEventListener("mousemove", move);
  rect.addEventListener("mouseleave", leave);
}

async function refreshUsageChart() {
  const wrap = $("usageChart");
  if (!wrap) return;
  try {
    const data = await api(`/api/usage/timeseries?group_by=${usageGroupBy}`);
    const days = data.days || 14;
    const dates = dateRange(days);
    const series = data.series || [];
    if (!series.length) {
      wrap.innerHTML = `<p class="quiet">No ${usageGroupBy === "guest" ? "guest" : "seat"} usage recorded in the last ${days} days.</p>`;
      return;
    }
    renderBarChart(wrap, prepareBarLines(series, dates, "total_tokens"), dates, { formatValue: formatTokenShort });
  } catch (err) {
    wrap.innerHTML = `<p class="quiet">${escapeHtml(err.message)}</p>`;
  }
}

async function refreshOccupancyChart() {
  const wrap = $("occupancyChart");
  if (!wrap) return;
  try {
    const data = await api("/api/floor/occupancy");
    const days = data.days || 14;
    const dates = dateRange(days);
    const points = data.points || [];
    if (!points.length) {
      wrap.innerHTML = `<p class="quiet">No visits recorded in the last ${days} days.</p>`;
      return;
    }
    const lines = prepareBarLines([{ key: "Visits", points }], dates, "visits");
    renderBarChart(wrap, lines, dates, { formatValue: formatCount, showLegend: false });
  } catch (err) {
    wrap.innerHTML = `<p class="quiet">${escapeHtml(err.message)}</p>`;
  }
}

async function openUsage(seatId, seatName, guestName) {
  $("usageTitle").textContent = `${guestName || "guest"} · ${seatName || "seat"}`;
  $("usageBody").innerHTML = `<p class="quiet">Loading…</p>`;
  openModal("usageModal");
  try {
    const data = await api(`/api/seats/${seatId}/usage`);
    renderUsage(data);
  } catch (err) {
    $("usageBody").innerHTML = `<p class="quiet">${escapeHtml(err.message)}</p>`;
  }
}

function usageRows(entries, dateKey, formatLabel) {
  return (
    (entries || [])
      .map(
        (e) =>
          `<tr><td>${escapeHtml(formatLabel(e[dateKey]))}</td><td>${e.messages}</td><td>${e.total_tokens.toLocaleString()}</td></tr>`
      )
      .join("") || `<tr><td colspan="3" class="quiet">No usage recorded yet.</td></tr>`
  );
}

function renderUsage(data) {
  const quota = data.quota || {};
  const stats = data.stats || { daily: [], hourly: [] };
  const guestStats = data.guest_stats;
  const quotaBadges = [];
  if (quota.five_hour != null) {
    const reset = formatResetTime(quota.five_hour_resets);
    quotaBadges.push(
      `<div class="usage-badge"><strong>${Math.round(quota.five_hour)}%</strong><span>5h session${reset ? ` · resets ${escapeHtml(reset)}` : ""}</span></div>`
    );
  }
  if (quota.seven_day != null) {
    const reset = formatResetTime(quota.seven_day_resets);
    quotaBadges.push(
      `<div class="usage-badge"><strong>${Math.round(quota.seven_day)}%</strong><span>7 day${reset ? ` · resets ${escapeHtml(reset)}` : ""}</span></div>`
    );
  }

  let guestSection = "";
  if (guestStats) {
    const t = guestStats.totals || { total_tokens: 0, messages: 0 };
    guestSection = `
      <h3>This visit</h3>
      <div class="usage-badges">
        <div class="usage-badge"><strong>${t.total_tokens.toLocaleString()}</strong><span>tokens</span></div>
        <div class="usage-badge"><strong>${t.messages.toLocaleString()}</strong><span>messages</span></div>
      </div>
      <table class="usage-table"><thead><tr><th>Hour</th><th>Msgs</th><th>Tokens</th></tr></thead><tbody>${usageRows(guestStats.hourly, "hour", formatHourLabel)}</tbody></table>`;
  }

  $("usageBody").innerHTML = `
    ${guestSection}
    <h3>Seat account${data.account ? ` — ${escapeHtml(data.account)}` : ""}</h3>
    <div class="usage-badges">${quotaBadges.join("") || `<p class="quiet">No rate-limit data yet.</p>`}</div>
    <table class="usage-table"><thead><tr><th>Date</th><th>Msgs</th><th>Tokens</th></tr></thead><tbody>${usageRows(stats.daily, "date", (d) => d)}</tbody></table>
  `;
}

// Solutions are individual tasks, but a project is the unit that deploys and
// the unit a guest gets locked into — so the desk lists tasks grouped under
// their project rather than flat, matching how a guest picks one.
function issueBadgeHTML(item) {
  if (!item.github_issue_number) return "";
  const href = item.github_issue_url ? ` href="${escapeHtml(item.github_issue_url)}" target="_blank" rel="noopener"` : "";
  const tag = item.github_issue_url ? "a" : "span";
  return ` <${tag} class="quiet"${href}>#${item.github_issue_number}</${tag}>`;
}

function boardTaskRowHTML(item, occ) {
  const taken = occ.some((s) => s.session.board_id === item.id);
  return `<article class="q-item nested">
    <span class="mark ${taken ? "live" : ""}">${taken ? "●" : "✓"}</span>
    <div>
      <strong>${taken ? "Live" : "Next"}: ${escapeHtml(item.title)}${issueBadgeHTML(item)}</strong>
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

// Solutions are the one thing on this desk backed by a live GitHub sync, so
// they get their own on-demand refresh (called from showPane) instead of
// riding the 8s poll below — see the comment in showPane.
async function refreshSolutions() {
  $("board").innerHTML = `<p class="quiet">Syncing GitHub issues…</p>`;
  try {
    const [{ items }, { seats }] = await Promise.all([api("/api/board"), api("/api/seats")]);
    const occ = seats.filter((s) => s.session);
    $("board").innerHTML = boardGroupsHTML(items, occ) || `<p class="quiet">No solutions yet.</p>`;
  } catch (err) {
    $("board").innerHTML = `<p class="quiet">${escapeHtml(err.message)}</p>`;
  }
}

async function refresh() {
  const [{ seats }, proj, tpl, health, accounts, live, ghApp] = await Promise.all([
    api("/api/seats"),
    api("/api/projects").catch(() => ({ projects: [] })),
    api("/api/templates").catch(() => ({ templates: [] })),
    api("/api/health").catch(() => ({ ok: false })),
    api("/api/claude-accounts").catch(() => ({ accounts: [] })),
    api("/api/live").catch(() => ({ sessions: [] })),
    api("/api/github/app").catch(() => ({ configured: false, slug: null })),
  ]);
  projects = proj.projects || [];
  githubApp = ghApp;
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
    <p class="quiet">${occ.length} sitting · ${open.length} open${claude.length ? ` · ${readyLogins} Claude login${readyLogins === 1 ? "" : "s"} ready` : ""}</p>`;

  $("seats").innerHTML = seats
    .map((s) => {
      const sess = s.session;
      const snap = liveBySeat[s.id] || {};
      const limited = (snap.accounts || claude).some((a) => a.in_use && a.limited);
      const account = snap.account || "";
      const lines = quotaLines(snap.quota);
      const handoff = sess && (snap.handoff || snap.handoff_text);
      const sub = sess ? `${escapeHtml(sess.coder_name)}${account ? ` · ${escapeHtml(account)}` : ""}` : "Ready";
      // The 5h/7d % never shows up for a real Claude Code account (statusLine
      // is never invoked under stream-json — see docs/salon.md), so the chip
      // must not depend on quota lines existing: the day/hour breakdown it
      // opens reads transcripts directly and works with no quota at all.
      const usageChip = sess
        ? `<button type="button" class="usage-chip" data-usage="${s.id}" data-seat-name="${escapeHtml(s.name)}" data-guest="${escapeHtml(sess.coder_name)}">${
            lines.length ? lines.map((l) => `<span>${escapeHtml(l)}</span>`).join("") : `<span>Usage</span>`
          }</button>`
        : "";
      return `<article class="seat ${sess ? "occupied" : ""} ${limited ? "limited" : ""}">
        <div><strong>${escapeHtml(s.name)}</strong><span class="quiet">${sub}</span>${usageChip}</div>
        <div class="seat-actions">
          ${handoff ? `<button type="button" class="text" data-handoff="${escapeHtml(sess.id)}">Handoff</button>` : ""}
          ${sess ? `<button type="button" data-free="${s.id}">End</button>` : ""}
        </div>
      </article>`;
    })
    .join("");
  document.querySelectorAll("[data-usage]").forEach((btn) => {
    btn.onclick = () =>
      openUsage(btn.getAttribute("data-usage"), btn.getAttribute("data-seat-name"), btn.getAttribute("data-guest"));
  });
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
  await refreshOccupancyChart().catch(() => {});
  await refreshUsageChart().catch(() => {});
}

document.querySelectorAll("[data-pane]").forEach((btn) => {
  btn.onclick = () => showPane(btn.getAttribute("data-pane"));
});
["qaFilterProject", "qaFilterGuest", "qaFilterStatus"].forEach((id) => {
  $(id).addEventListener("change", () => refreshQAGrading().catch(() => {}));
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

$("closeUsage").onclick = () => closeModal("usageModal");
$("usageModal").addEventListener("click", (ev) => {
  if (ev.target.id === "usageModal") closeModal("usageModal");
});

document.querySelectorAll("[data-usage-group]").forEach((btn) => {
  btn.onclick = () => {
    usageGroupBy = btn.getAttribute("data-usage-group");
    document.querySelectorAll("[data-usage-group]").forEach((b) => b.classList.toggle("is-on", b === btn));
    refreshUsageChart().catch(() => {});
  };
});

function resetAddDrawer() {
  document.querySelectorAll("#addModeToggle [data-mode]").forEach((b) => b.classList.toggle("is-on", b.dataset.mode === "existing"));
  $("existingProjectMode").hidden = false;
  $("newProjectMode").hidden = true;
  $("newProject").hidden = false; // the form itself, collapsed after a successful save
  $("briefSection").hidden = true;
  $("projectSel").value = "";
  $("briefProjectId").value = "";
  $("projectSyncMsg").textContent = "";
  $("suggestMsg").textContent = "";
  $("briefMsg").textContent = "";
  $("projectMsg").textContent = "";
  $("newProject").reset();
  updateProjectKindFields();
  $("newBrief").reset();
}

$("openAdd").onclick = () => {
  resetAddDrawer();
  openModal("addDrawer");
};
$("closeAdd").onclick = () => closeModal("addDrawer");
$("addDrawer").addEventListener("click", (ev) => {
  if (ev.target.id === "addDrawer") closeModal("addDrawer");
});

document.querySelectorAll("#addModeToggle [data-mode]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#addModeToggle [data-mode]").forEach((b) => b.classList.toggle("is-on", b === btn));
    const mode = btn.getAttribute("data-mode");
    $("existingProjectMode").hidden = mode !== "existing";
    $("newProjectMode").hidden = mode !== "new";
    $("newProject").hidden = false;
    $("projectMsg").textContent = "";
    // Switching modes means picking or creating a project again.
    $("briefSection").hidden = true;
    $("projectSel").value = "";
    $("briefProjectId").value = "";
  });
});

$("closeGithubAppModal").onclick = () => closeModal("githubAppModal");
$("githubAppModal").addEventListener("click", (ev) => {
  if (ev.target.id === "githubAppModal") closeModal("githubAppModal");
});

$("checkin").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = $("checkinMsg");
  msg.textContent = "Printing slip…";
  const coderName = $("coderName").value;
  const seatName = $("seatSel").selectedOptions[0] ? $("seatSel").selectedOptions[0].textContent : "";
  try {
    const started = await api("/api/sessions/check-in", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({
        seat_id: $("seatSel").value,
        coder_name: coderName,
      }),
    });
    if (started.state === "preparing") {
      // The seat is a container being raised right now. Showing the QR before
      // it answers would hand the guest a code for an address that 404s.
      await waitForSeat(started, msg, coderName, seatName);
    } else {
      showQrOnly(coderName, seatName);
    }
    await refresh();
  } catch (err) {
    msg.textContent = err.message;
  }
});

$("newBrief").querySelector('[name="title"]').addEventListener("blur", async (ev) => {
  const title = ev.target.value.trim();
  const projectId = $("briefProjectId").value;
  const briefField = $("newBrief").querySelector('[name="brief"]');
  const specField = $("newBrief").querySelector('[name="spec"]');
  const minutesField = $("newBrief").querySelector('[name="wellness_minutes"]');
  const breakField = $("newBrief").querySelector('[name="break_after"]');
  const msg = $("suggestMsg");
  if (!title || !projectId) return;
  // Never clobber something the host already started writing by hand.
  if (briefField.value.trim() || specField.value.trim()) return;
  msg.textContent = "Claude is drafting a brief from the repo…";
  try {
    const suggestion = await api("/api/board/suggest", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ title, project_id: projectId }),
    });
    briefField.value = suggestion.brief;
    specField.value = suggestion.spec;
    minutesField.value = suggestion.wellness_minutes;
    breakField.value = suggestion.break_after;
    msg.textContent = "Suggested by Claude — edit anything before adding.";
  } catch (err) {
    msg.textContent = `Could not draft a suggestion (${err.message}) — fill these in by hand.`;
  }
});

$("newBrief").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const msg = $("briefMsg");
  try {
    await api("/api/board", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({
        title: fd.get("title"),
        brief: fd.get("brief"),
        wellness_minutes: Number(fd.get("wellness_minutes")),
        break_after: Number(fd.get("break_after")),
        project_id: fd.get("project_id") || null,
        spec: fd.get("spec"),
      }),
    });
    closeModal("addDrawer");
    resetAddDrawer();
    await refreshSolutions();
  } catch (err) {
    msg.textContent = err.message;
  }
});

$("freeAll").addEventListener("click", async () => {
  try {
    await api("/api/seats/free-all", { method: "POST", headers: jsonHeaders });
    await refresh();
  } catch (err) {
    $("checkinMsg").textContent = err.message;
  }
});

// Picking an existing project syncs its GitHub issues right away — no
// separate "Fetch repo" / "Sync GitHub issues" buttons to click. A project
// that isn't GitHub-backed 400s here, which just means there's nothing to
// sync, not a real failure.
$("projectSel").addEventListener("change", async () => {
  const id = $("projectSel").value;
  const syncMsg = $("projectSyncMsg");
  if (!id) {
    $("briefSection").hidden = true;
    return;
  }
  $("briefProjectId").value = id;
  $("briefSection").hidden = false;
  syncMsg.textContent = "Syncing GitHub issues…";
  try {
    const res = await api(`/api/projects/${id}/sync-issues`, { method: "POST", headers: jsonHeaders });
    syncMsg.textContent = `Synced: ${res.added} added, ${res.updated} updated, ${res.removed} closed.`;
  } catch (err) {
    syncMsg.textContent = err.message.includes("not a GitHub repo") ? "" : err.message;
  }
});

document.querySelectorAll('#newProject input[name="kind"]').forEach((el) => {
  el.addEventListener("change", updateProjectKindFields);
});
updateProjectKindFields();

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
        description: fd.get("description") || "",
      }),
    });
    msg.textContent = `${created.name} ready.`;
    ev.target.hidden = true; // bifurcates project setup from the solution fields below
    projects.push(created);
    fillProjectSelect(created.id);
    $("briefProjectId").value = created.id;
    $("briefSection").hidden = false;
    await maybeOfferGithubAppLink(created);
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
