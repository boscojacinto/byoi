const token = localStorage.getItem("byoiHostToken") || "byoi-host";
const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

async function refresh() {
  const [{ seats }, { items }] = await Promise.all([api("/api/seats"), api("/api/board")]);
  const box = document.getElementById("seats");
  box.innerHTML = seats
    .map((s) => {
      const occ = s.session;
      const status = occ ? "occupied" : "idle";
      return `<article class="seat ${occ ? "occupied" : ""}">
        <div>
          <strong>${s.name}</strong>
          <div class="pill">Wi-Fi · ${s.claude_label}</div>
          ${occ ? `<div>${occ.coder_name} · ${occ.status}</div>` : "<div>open</div>"}
        </div>
        <div>
          <span class="pill">${status}</span>
          ${occ ? `<button type="button" data-free="${s.id}">Free</button>` : ""}
        </div>
      </article>`;
    })
    .join("");
  box.querySelectorAll("[data-free]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/seats/${btn.getAttribute("data-free")}/free`, { method: "POST", headers });
        await refresh();
      } catch (err) {
        document.getElementById("checkinMsg").textContent = err.message;
      }
    });
  });
  const idle = seats.filter((s) => !s.session);
  const sel = document.getElementById("seatSel");
  sel.innerHTML = idle.map((s) => `<option value="${s.id}">${s.name}</option>`).join("");
  const msg = document.getElementById("checkinMsg");
  if (!idle.length) {
    msg.textContent = "All seats occupied — free one to check in.";
  } else if (msg.textContent.startsWith("All seats occupied")) {
    msg.textContent = "";
  }
  document.getElementById("board").innerHTML = items
    .map(
      (i) => `<article class="brief"><h3>${i.title}</h3><p>${i.brief}</p>
      <p class="pill">${i.wellness_minutes} min · break ${i.break_after}</p></article>`
    )
    .join("");
}

document.getElementById("checkin").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = document.getElementById("checkinMsg");
  msg.textContent = "printing slip…";
  try {
    const data = await api("/api/sessions/check-in", {
      method: "POST",
      headers,
      body: JSON.stringify({
        seat_id: document.getElementById("seatSel").value,
        coder_name: document.getElementById("coderName").value,
      }),
    });
    msg.textContent = `slip ${data.print.mode} · join ${data.join}`;
    const img = document.getElementById("slip");
    img.src = "/last-slip.png?t=" + Date.now();
    img.hidden = false;
    await refresh();
  } catch (err) {
    msg.textContent = err.message;
  }
});

document.getElementById("newBrief").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  await api("/api/board", {
    method: "POST",
    headers,
    body: JSON.stringify({
      title: fd.get("title"),
      brief: fd.get("brief"),
      wellness_minutes: Number(fd.get("wellness_minutes")),
      break_after: Number(fd.get("break_after")),
    }),
  });
  ev.target.reset();
  await refresh();
});

document.getElementById("freeAll").addEventListener("click", async () => {
  const msg = document.getElementById("checkinMsg");
  try {
    await api("/api/seats/free-all", { method: "POST", headers });
    msg.textContent = "floor cleared";
    await refresh();
  } catch (err) {
    msg.textContent = err.message;
  }
});

refresh().catch((err) => {
  document.getElementById("checkinMsg").textContent = err.message;
});
