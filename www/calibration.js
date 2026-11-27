"use strict";
const $ = (s, r = document) => r.querySelector(s);
const fmtDb = (v) => (v == null || v <= -95 ? "−∞" : v.toFixed(1).replace("-", "−"));
let lan = false;

async function refresh() {
  try {
    const s = await (await fetch("/api/status", { cache: "no-store" })).json();
    lan = s.lan;
    $("#status").className = "status " + (s.online ? "online" : "offline");
    $("#status span").textContent = s.online ? "Live" : "No audio";
    $("#l-side").textContent = s.direction ? s.direction.toUpperCase() : "IDLE";
    $("#l-db").textContent = fmtDb(s.dbfs);
    $("#l-pk").textContent = fmtDb(s.peak_dbfs);
    $("#l-dev").textContent = s.deviation_khz == null ? "—" : s.deviation_khz.toFixed(2);
    $("#lan-note").textContent = lan ? "" : "Changes can only be made from the local network";
    document.querySelectorAll(".cal-card button").forEach((b) => (b.disabled = !lan));
    if (!refresh.loaded) { refresh.loaded = true; loadCal(); }
  } catch {
    $("#status").className = "status offline"; $("#status span").textContent = "Disconnected";
  }
  setTimeout(refresh, 250);
}

async function loadCal(cal) {
  if (!cal) {
    const r = await fetch("/api/status", { cache: "no-store" });
    const s = await r.json();
    cal = s.cal_values || null;
  }
  if (!cal) return;
  for (const d of ["rx", "tx"]) {
    const v = cal[d + "_fs_khz"];
    $(`.cal-card[data-dir=${d}] [data-fs]`).textContent = v == null ? "not set" : v.toFixed(2);
  }
}

async function post(dir, body, card) {
  const msg = $("[data-msg]", card);
  msg.className = "msg"; msg.textContent = "Working…";
  try {
    const r = await fetch("/api/calibrate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dir, ...body }) });
    const j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || r.status);
    msg.className = "msg ok"; msg.textContent = "Saved.";
    loadCal(j.cal);
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}

document.querySelectorAll(".cal-card").forEach((card) => {
  const dir = card.dataset.dir;
  $("[data-tone]", card).onsubmit = (e) => { e.preventDefault(); post(dir, { khz: +$("input", e.target).value }, card); };
  $("[data-fsform]", card).onsubmit = (e) => { e.preventDefault(); post(dir, { fs_khz: +$("input", e.target).value }, card); };
  $("[data-reset]", card).onclick = () => { if (confirm(`Clear the ${dir.toUpperCase()} calibration?`)) post(dir, { reset: true }, card); };
});
refresh();
