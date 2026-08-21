async function readJson(res, fallback) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    const msg = typeof detail === "string" ? detail : fallback;
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return data;
}

export async function joinSlip(base, otp) {
  const res = await fetch(`${base}/api/join?otp=${encodeURIComponent(otp)}`);
  if (res.status === 404) throw new Error("unknown slip");
  if (res.status === 410) throw new Error("session finished");
  return readJson(res, "could not join this seat");
}

export async function fetchBoard(base) {
  const res = await fetch(`${base}/api/board`);
  const data = await readJson(res, "board unavailable");
  return data.items || [];
}

export async function seatStatus(base) {
  const res = await fetch(`${base}/local/status`);
  return readJson(res, "seat not reachable");
}

export async function claimBrief(base, sessionId, boardId) {
  const res = await fetch(`${base}/api/sessions/${sessionId}/claim`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ board_id: boardId }),
  });
  return readJson(res, "claim failed — check in at the desk first");
}

export async function unlockSeat(base, otp, sessionId) {
  const res = await fetch(`${base}/local/unlock`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ otp, session_id: sessionId }),
  });
  return readJson(res, "denied — OTP does not match this seat");
}

export async function completeSession(base, sessionId) {
  const res = await fetch(`${base}/api/sessions/${sessionId}/complete`, {
    method: "POST",
  });
  return readJson(res, "could not mark shipped");
}
