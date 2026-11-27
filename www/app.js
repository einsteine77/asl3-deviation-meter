"use strict";
// ASL3 Deviation Meter 2.0 - live client.
let TITLE = "ASL3 Deviation Meter", PUBLIC_URL = "";
// Data arrives by Server-Sent Events (~25/s); everything on screen is animated
// at display rate with peak-meter ballistics (instant attack, timed release).

const $ = (s) => document.querySelector(s);
const MIN_DB = -60, DEV_MAX = 6, WARN = 4.5, LIMIT = 5.0;
const REL_DB_S = 26;          // level release, dB per second
const REL_KHZ_S = 9;          // deviation bar release, kHz per second
const STRIP_S = 60;

const st = {
  snap: null, lastMsg: 0, side: null,
  lvl: { rx: { tgt: -96, disp: -96, txt: -96 }, tx: { tgt: -96, disp: -96, txt: -96 } },
  dev: { tgt: 0, disp: 0, num: null, numT: 0, buf: [] },
  strip: [],                   // {t, db, side}
  curStart: null, hist: [], histSeen: 0,
};

const pos = (db) => Math.max(0, Math.min(100, ((db - MIN_DB) / -MIN_DB) * 100));
const kpos = (k) => Math.max(0, Math.min(100, (k / DEV_MAX) * 100));
const fmtDb = (v) => (v == null || v <= -95 ? "−∞" : v.toFixed(1).replace("-", "−"));
const fmtK = (v) => (v == null ? "—" : v.toFixed(2));
const zone = (v) => (v == null ? "" : v >= LIMIT ? "hi" : v >= WARN ? "warn" : "");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sideOf = (d) => (d === "rx" ? "rx" : d ? "tx" : null);   // "audio" (no AMI) shows on the TX bar

function whoText(s) {
  if (s.direction === "rx") return "Local RF · COS";
  if (s.direction === "tx") return s.talkers && s.talkers.length ? "Network · " + s.talkers.join(", ") : "Telemetry / ID";
  if (s.direction === "audio") return "Audio present (no AMI keying)";
  return s.online ? "Waiting for activity" : "No audio from the node";
}

// ---------------------------------------------------------------------------
// incoming data
// ---------------------------------------------------------------------------
function onSnap(s) {
  const now = performance.now();
  st.snap = s; st.lastMsg = now;
  const side = sideOf(s.direction);
  const burst = Math.max(...(s.lv && s.lv.length ? s.lv : [s.dbfs]));
  for (const k of ["rx", "tx"]) st.lvl[k].tgt = k === side ? burst : -96;
  if (!side && s.online) st.lvl.tx.tgt = -96;

  const d = s.deviation_khz;
  st.dev.tgt = d == null ? 0 : d;
  if (d != null) st.dev.buf.push([now, d]);

  st.strip.push({ t: Date.now(), db: side ? burst : Math.min(burst, -70), side: side || "idle" });
  const cut = Date.now() - STRIP_S * 1000 - 1000;
  while (st.strip.length && st.strip[0].t < cut) st.strip.shift();

  st.curStart = s.current ? s.current.start * 1000 : null;
  if (s.tx_history) renderHistory(s.tx_history);
  if (s.waveform) st.wave = s.waveform;
  renderSlow(s);
}

function renderSlow(s) {
  $("#node").textContent = s.node;
  if (s.title && TITLE !== s.title) { TITLE = s.title; $("#title").textContent = s.title; }
  PUBLIC_URL = s.public_url || "";
  const stEl = $("#status");
  stEl.className = "status " + (s.online ? "online" : "offline");
  $("#status span").textContent = s.online ? "Live" : "No audio";
  const side = sideOf(s.direction);
  const sideEl = $("#side");
  sideEl.className = "side " + (s.direction === "audio" ? "audio tx" : side || "idle");
  $("#side-label").textContent = s.direction === "rx" ? "RX" : s.direction === "tx" ? "TX" : s.direction === "audio" ? "AUDIO" : "IDLE";
  $("#side-who").textContent = whoText(s);
  $(".side-card").classList.toggle("keyed", !!side);
  document.querySelectorAll(".lvl").forEach((el) => {
    const on = el.dataset.side === side;
    el.classList.toggle("active", on);
    el.classList.toggle("dim", !on);
  });
  $("#peakdb").textContent = fmtDb(s.peak_dbfs);
  $("#max-dev").textContent = fmtK(s.max_deviation_khz);
  const cm = s.current ? s.current.max_deviation_khz : null;
  const txm = $("#tx-max");
  txm.textContent = fmtK(cm ?? (s.current ? null : st.lastTxMax ?? null));
  txm.className = zone(cm);
  if (s.current) st.lastTxMax = cm;
  const calNeeded = side === "rx" ? !s.cal.rx : side === "tx" ? !s.cal.tx : !(s.cal.rx || s.cal.tx);
  $("#cal-tag").hidden = !calNeeded;
  $("#cal-tag").textContent = side ? `${side.toUpperCase()} uncalibrated` : "uncalibrated";
  $(".dev-card").classList.toggle("side-tx", side === "tx");
  $("#channel").textContent = s.channel;
  $("#ami-state").textContent = s.ami ? `Keying: Asterisk AMI · ${s.links} link${s.links === 1 ? "" : "s"}` : "Keying: audio level (AMI not connected)";
  $("#listen-box").hidden = !s.listen;
  document.title = side ? `● ${side.toUpperCase()} · ${TITLE}` : TITLE;
}

function renderHistory(rows) {
  const body = $("#tx-history");
  if (!rows.length) { body.innerHTML = '<tr><td colspan="7" class="empty">No transmissions recorded yet</td></tr>'; return; }
  const newest = rows[0].ended;
  const fresh = st.histSeen && newest > st.histSeen;
  st.histSeen = newest;
  body.innerHTML = rows.slice(0, 8).map((x, i) => {
    const side = x.direction === "rx" ? "rx" : "tx";
    const src = x.direction === "rx" ? "Local RF" : x.direction === "audio" ? "—" : x.talkers && x.talkers.length ? x.talkers.join(", ") : "Telemetry / ID";
    const z = zone(x.max_deviation_khz);
    return `<tr class="${fresh && i === 0 ? "fresh" : ""}">
      <td>${new Date(x.ended * 1000).toLocaleTimeString()}</td>
      <td><span class="tag ${side}">${side.toUpperCase()}</span></td>
      <td>${esc(src)}</td>
      <td class="num">${x.duration_s.toFixed(1)} s</td>
      <td class="num">${x.average_deviation_khz == null ? "—" : x.average_deviation_khz.toFixed(2) + " kHz"}</td>
      <td class="num"><span class="${z}">${x.max_deviation_khz == null ? "—" : x.max_deviation_khz.toFixed(2) + " kHz"}</span></td>
      <td class="num">${fmtDb(x.peak_dbfs)}</td></tr>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// transport: SSE, falling back to polling
// ---------------------------------------------------------------------------
let es = null, sseFails = 0, pollTimer = null;
function connect() {
  if (!window.EventSource || sseFails > 4) return poll();
  es = new EventSource("/api/stream");
  es.onmessage = (e) => { sseFails = 0; try { onSnap(JSON.parse(e.data)); } catch {} };
  es.onerror = () => {
    sseFails++;
    $("#status").className = "status offline"; $("#status span").textContent = "Reconnecting";
    if (sseFails > 4) { es.close(); poll(); }
  };
}
async function poll() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    onSnap(await r.json());
  } catch {
    $("#status").className = "status offline"; $("#status span").textContent = "Disconnected";
  }
  pollTimer = setTimeout(poll, 150);
}

// ---------------------------------------------------------------------------
// animation
// ---------------------------------------------------------------------------
const lvlEls = {};
document.querySelectorAll(".lvl").forEach((el) => {
  lvlEls[el.dataset.side] = { fill: el.querySelector("[data-fill]"), pk: el.querySelector("[data-pk]"), v: el.querySelector("[data-v]"), txtT: 0 };
});
const devFill = $("#devfill"), devHold = $("#devhold"), devNum = $("#dev"), devCard = $(".dev-card");

function fitCanvas(c) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = Math.round(c.clientWidth * dpr), h = Math.round(c.clientHeight * dpr);
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  return dpr;
}

let lastT = performance.now();
function frame(t) {
  const dt = Math.min(0.1, (t - lastT) / 1000); lastT = t;
  const s = st.snap;
  const stale = !s || t - st.lastMsg > 1500;

  for (const k of ["rx", "tx"]) {
    const L = st.lvl[k], E = lvlEls[k];
    const tgt = stale ? -96 : L.tgt;
    L.disp = tgt >= L.disp ? tgt : Math.max(tgt, L.disp - REL_DB_S * dt);
    E.fill.style.width = pos(L.disp) + "%";
    if (s && sideOf(s.direction) === k) E.pk.style.left = `calc(${pos(s.peak_dbfs)}% - 2px)`;
    if (t - E.txtT > 100) { E.v.textContent = fmtDb(L.disp > -90 ? L.disp : null); E.txtT = t; }
  }

  // deviation: bar animates every frame; the number shows the 150 ms max, 8x/s
  const D = st.dev;
  const dtgt = stale ? 0 : D.tgt;
  D.disp = dtgt >= D.disp ? dtgt : Math.max(dtgt, D.disp - REL_KHZ_S * dt);
  devFill.style.width = kpos(D.disp) + "%";
  if (s && s.max_deviation_khz != null) { devHold.style.opacity = 0.9; devHold.style.left = `calc(${kpos(s.max_deviation_khz)}% - 2px)`; }
  else devHold.style.opacity = 0;
  if (t - D.numT > 125) {
    D.buf = D.buf.filter(([bt]) => t - bt < 150);
    const v = !stale && s && s.deviation_khz != null ? Math.max(s.deviation_khz, ...D.buf.map((b) => b[1])) : null;
    devNum.textContent = fmtK(v);
    devCard.classList.remove("warn", "hi");
    const z = zone(v); if (z) devCard.classList.add(z);
    D.numT = t;
  }

  // keyed timer
  if (t - (st.timerT || 0) > 250) {
    st.timerT = t;
    if (st.curStart) {
      const sec = Math.max(0, (Date.now() - st.curStart) / 1000);
      $("#timer").textContent = `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
    }
  }

  drawStrip();
  drawWave();
  requestAnimationFrame(frame);
}

const css = getComputedStyle(document.documentElement);
const C = { rx: css.getPropertyValue("--rx").trim(), tx: css.getPropertyValue("--tx").trim(), line: "#1b3038", faint: "#2b4650" };

function drawStrip() {
  const c = $("#strip"), dpr = fitCanvas(c), g = c.getContext("2d");
  const w = c.width, h = c.height, now = Date.now();
  g.clearRect(0, 0, w, h);
  g.fillStyle = C.line;
  for (let i = 1; i < 6; i++) g.fillRect(Math.round((i / 6) * w), 0, 1, h);
  const bw = Math.max(1, (w / (STRIP_S * 25)) + 0.6);
  for (const p of st.strip) {
    const x = w - ((now - p.t) / (STRIP_S * 1000)) * w;
    if (x < -2) continue;
    const v = Math.max(0, (p.db - MIN_DB) / -MIN_DB);
    const bh = Math.max(p.side === "idle" ? 1 * dpr : 2 * dpr, v * (h - 4 * dpr));
    g.fillStyle = p.side === "rx" ? C.rx : p.side === "tx" ? C.tx : C.faint;
    g.fillRect(x, h - bh, bw, bh);
  }
  g.fillStyle = "#56717a"; g.font = `${10 * dpr}px ui-monospace, monospace`;
  g.fillText("−60 s", 4 * dpr, 12 * dpr);
  g.fillText("now", w - 24 * dpr, 12 * dpr);
}

function drawWave() {
  const c = $("#wave");
  const on = $("#wave-toggle").checked;
  c.style.display = on ? "" : "none";
  if (!on) return;
  const dpr = fitCanvas(c), g = c.getContext("2d"), w = c.width, h = c.height;
  g.clearRect(0, 0, w, h);
  g.strokeStyle = C.line; g.lineWidth = 1;
  g.beginPath(); g.moveTo(0, h / 2); g.lineTo(w, h / 2); g.stroke();
  const a = st.wave || [];
  if (a.length < 2 || !st.snap || performance.now() - st.lastMsg > 1500) return;
  const side = sideOf(st.snap.direction);
  g.strokeStyle = side === "tx" ? C.tx : side === "rx" ? C.rx : "#3d5e66";
  g.lineWidth = 2 * dpr; g.lineJoin = "round";
  g.beginPath();
  a.forEach((v, i) => { const x = (i / (a.length - 1)) * w, y = h / 2 - v * h * 0.45; i ? g.lineTo(x, y) : g.moveTo(x, y); });
  g.stroke();
}

// ---------------------------------------------------------------------------
// Listen: raw 8 kHz PCM over fetch streaming into Web Audio, ~0.25 s latency
// ---------------------------------------------------------------------------
const Player = {
  ctx: null, gain: null, abort: null, next: 0, on: false,
  TARGET: 0.25, MAXLAG: 0.9,
  async start() {
    this.ctx = this.ctx || new (window.AudioContext || window.webkitAudioContext)();
    await this.ctx.resume();
    if (!this.gain) { this.gain = this.ctx.createGain(); this.gain.connect(this.ctx.destination); }
    this.gain.gain.value = +$("#vol").value;
    this.on = true; this.next = 0;
    this.ui();
    while (this.on) {
      try { await this.run(); } catch (e) { if (!this.on) break; this.note("Reconnecting…"); }
      if (this.on) await new Promise((r) => setTimeout(r, 1500));
    }
  },
  async run() {
    this.abort = new AbortController();
    const r = await fetch("/api/audio", { cache: "no-store", signal: this.abort.signal });
    if (!r.ok || !r.body) throw new Error("audio " + r.status);
    const rd = r.body.getReader();
    let carry = null;
    this.note("Buffering…");
    for (;;) {
      const { value, done } = await rd.read();
      if (done) throw new Error("ended");
      let bytes = value;
      if (carry) { const m = new Uint8Array(carry.length + bytes.length); m.set(carry); m.set(bytes, carry.length); bytes = m; carry = null; }
      if (bytes.length & 1) { carry = bytes.slice(-1); bytes = bytes.subarray(0, bytes.length - 1); }
      if (!bytes.length) continue;
      this.play(bytes);
    }
  },
  play(bytes) {
    const ctx = this.ctx, n = bytes.length >> 1;
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    const buf = ctx.createBuffer(1, n, 8000), ch = buf.getChannelData(0);
    for (let i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
    const now = ctx.currentTime;
    if (this.next < now + 0.02) this.next = now + this.TARGET;          // underrun: rebuffer
    else if (this.next - now > this.MAXLAG) return;                       // too far behind: drop to catch up
    const src = ctx.createBufferSource();
    src.buffer = buf; src.connect(this.gain); src.start(this.next);
    this.next += buf.duration;
    if (performance.now() - (this.noteT || 0) > 1000) { this.note(`${Math.round((this.next - now) * 1000)} ms delay`); this.noteT = performance.now(); }
  },
  stop() {
    this.on = false;
    if (this.abort) this.abort.abort();
    if (this.ctx) this.ctx.suspend();
    this.note(""); this.ui();
  },
  note(t) { $("#listen-note").textContent = t; },
  ui() {
    const b = $("#listen");
    b.setAttribute("aria-pressed", String(this.on));
    b.querySelector("span").textContent = this.on ? "Listening" : "Listen live";
  },
};
$("#listen").onclick = () => (Player.on ? Player.stop() : Player.start());
$("#vol").oninput = (e) => { if (Player.gain) Player.gain.gain.value = +e.target.value; try { localStorage.m444vol = e.target.value; } catch {} };
try { if (localStorage.m444vol) $("#vol").value = localStorage.m444vol; } catch {}
try { const w = localStorage.m444wave; if (w) $("#wave-toggle").checked = w === "1"; } catch {}
$("#wave-toggle").onchange = (e) => { try { localStorage.m444wave = e.target.checked ? "1" : "0"; } catch {} };

$("#bookmark").onclick = () => {
  const target = PUBLIC_URL || location.origin + "/";
  alert(/Android|iPhone|iPad/i.test(navigator.userAgent)
    ? `Open your browser menu and choose "Add Bookmark" or "Add to Home Screen" for ${target}`
    : `Press Ctrl+D (Windows/Linux) or ⌘+D (Mac) to bookmark ${target}`);
};

connect();
requestAnimationFrame(frame);
