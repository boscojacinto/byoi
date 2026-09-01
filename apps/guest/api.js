// The one call the app makes on its own. Joining, the board, claiming a brief,
// unlocking chat and shipping are all the seat's guest UI talking to its own
// seat — see apps/guest-web/guest.js. Duplicating them here is what let the
// app drift behind the page it wraps.
export async function seatStatus(base) {
  const res = await fetch(`${base}/local/status`);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : `seat not reachable (HTTP ${res.status})`);
  }
  return data;
}
