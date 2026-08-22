const $ = (id) => document.getElementById(id);

async function load() {
  const res = await fetch("/local/status");
  const data = await res.json();
  const lan = data.lan || location.hostname;
  $("name").textContent = data.name || "Seat";
  $("status").textContent = data.admitted
    ? data.coder_name
      ? `${data.coder_name} is sitting`
      : "Someone is sitting"
    : "Ready for a guest";
  $("guest").href = "/guest/";
  $("desk").href = `http://${lan === location.hostname ? "127.0.0.1" : lan}:8080/`;
}

load().catch((err) => {
  $("status").textContent = "Can't reach this seat yet.";
});
