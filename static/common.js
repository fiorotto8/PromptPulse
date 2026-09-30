"use strict";
window.M = (() => {
  const byId = id => document.getElementById(id);
  const has = v => typeof v === "number" && Number.isFinite(v);
  const number = (v, digits = 1) => has(v) ? v.toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits}) : "—";
  const unit = (v, suffix, digits = 1) => has(v) ? `${number(v, digits)} ${suffix}` : "—";
  const gib = v => unit(has(v) ? v / 1073741824 : null, "GiB");
  const rate = v => !has(v) ? "—" : v >= 1048576 ? unit(v / 1048576, "MiB/s") : unit(v / 1024, "KiB/s");
  const text = (id, value) => { const el = byId(id); if (el) el.textContent = value; };
  const date = ts => new Date(ts * 1000).toLocaleString();
  let last = null, received = 0, online = false;
  async function get(path, signal) {
    const response = await fetch(path, {cache: "no-store", signal});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }
  function status() {
    const el = byId("connection-status");
    if (!el) return;
    if (!last) { el.textContent = online ? "Waiting for data" : "Connecting"; return; }
    const age = last.age_seconds === null ? null : last.age_seconds + (Date.now() - received) / 1000;
    const stale = age === null || age > Math.max(15, 3 * last.meta.interval_seconds);
    el.className = `status ${!online ? "bad" : stale || last.collector_error ? "warn" : "good"}`;
    el.textContent = !online ? "Disconnected" : stale ? "Last reading · stale" : last.collector_error ? "Read error" : "Reading normally";
    text("last-read", last.sample ? `${date(last.sample.timestamp)} · ${Math.floor(age)}s ago` : "No stored reading yet");
  }
  function receive(data) {
    last = data; received = Date.now(); online = true;
    text("host-name", data.meta.hostname);
    text("machine-model", data.meta.model);
    status();
  }
  function offline() { online = false; status(); const el = byId("connection-status"); if (!last && el) {el.textContent = "Disconnected"; el.className = "status bad";} }
  function notice(message) { const el = byId("notice"); el.textContent = message || ""; el.hidden = !message; }
  setInterval(status, 1000);
  return {byId, has, number, unit, gib, rate, text, date, get, receive, offline, notice};
})();
