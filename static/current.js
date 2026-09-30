"use strict";
(() => {
  const {byId, has, number, unit, gib, rate, text} = M;
  function card(id, value, suffix, digits = 1) {
    const el = byId(id); el.replaceChildren(document.createTextNode(number(value, digits)));
    if (has(value)) { const small = document.createElement("small"); small.textContent = suffix; el.append(small); }
  }
  function meter(id, value) { const el = byId(id); if (el) el.style.width = `${has(value) ? Math.max(0, Math.min(100, value)) : 0}%`; }
  function duration(s) {
    if (!has(s)) return "—";
    const days = Math.floor(s / 86400), hours = Math.floor(s % 86400 / 3600), mins = Math.floor(s % 3600 / 60);
    return `${days ? days + "d " : ""}${hours}h ${mins}m`;
  }
  function render(data) {
    M.receive(data);
    text("interval", `${data.meta.interval_seconds} s`); text("retention", `${data.meta.retention_days} days`);
    text("db-size", data.database_bytes >= 1073741824 ? gib(data.database_bytes) : unit(data.database_bytes / 1048576, "MiB", 2));
    if (!data.sample) { M.notice("The collector is starting. The first readings will appear automatically."); return; }
    const m = data.sample.metrics, d = data.sample.details;
    card("cpu-value", m.cpu_percent, "%"); card("gpu-value", m.gpu_percent, "%");
    card("ram-value", has(m.ram_used_bytes) ? m.ram_used_bytes / 1073741824 : null, "GiB");
    card("power-value", m.gpu_power_w, "W");
    card("cpu-temp-value", m.cpu_temp_c, "°C"); card("gpu-temp-value", m.gpu_temp_c, "°C");
    card("load-value", m.load_1, "", 2);
    text("load-note", `5 min ${number(m.load_5, 2)} · 15 min ${number(m.load_15, 2)}`);
    meter("cpu-meter", m.cpu_percent); meter("gpu-meter", m.gpu_percent);
    const ramPercent = has(m.ram_used_bytes) && m.ram_total_bytes > 0 ? 100 * m.ram_used_bytes / m.ram_total_bytes : null;
    meter("ram-meter", ramPercent);
    text("ram-note", `${gib(m.ram_total_bytes)} total · ${number(ramPercent, 0)}% in use`);
    text("gpu-note", d.gpu_name || "NVIDIA GPU");
    text("power-note", has(m.gpu_power_limit_w) ? `${unit(m.gpu_power_w, "W")} / ${unit(m.gpu_power_limit_w, "W")} limit` : "Reported GPU power");
    const cores = d.cpu_cores || [];
    text("cpu-note", `${cores.filter(c => c.online).length} online cores · ${unit(m.cpu_freq_mhz, "MHz", 0)} mean`);
    const fragment = document.createDocumentFragment();
    cores.forEach(c => {
      const el = document.createElement("div"); el.className = `core${c.online ? "" : " offline"}`;
      const top = document.createElement("div"); top.className = "core-top";
      const label = document.createElement("span"); label.textContent = c.name;
      const value = document.createElement("strong"); value.textContent = c.online ? unit(c.percent, "%", 0) : "offline";
      top.append(label, value);
      const track = document.createElement("div"); track.className = "meter";
      const fill = document.createElement("span"); fill.style.width = `${has(c.percent) ? c.percent : 0}%`; track.append(fill);
      const clock = document.createElement("div"); clock.className = "core-clock"; clock.textContent = unit(c.mhz, "MHz", 0);
      el.append(top, track, clock); fragment.append(el);
    });
    if (cores.length) byId("cores").replaceChildren(fragment);
    const vramPct = has(m.gpu_vram_used_bytes) && m.gpu_vram_total_bytes > 0 ? 100 * m.gpu_vram_used_bytes / m.gpu_vram_total_bytes : null;
    card("vram-value", vramPct, "%", 0); text("vram-card-note", `${gib(m.gpu_vram_used_bytes)} / ${gib(m.gpu_vram_total_bytes)}`); meter("vram-card-meter", vramPct);
    text("gpu-name", d.gpu_name || "NVIDIA GPU"); text("vram-detail", `${gib(m.gpu_vram_used_bytes)} / ${gib(m.gpu_vram_total_bytes)}`);
    text("vram-percent", unit(vramPct, "%")); meter("vram-meter", vramPct);
    text("gpu-memory-util", unit(m.gpu_memory_percent, "%")); text("gpu-temp", unit(m.gpu_temp_c, "°C"));
    text("gpu-power-detail", `${unit(m.gpu_power_w, "W")} / ${unit(m.gpu_power_limit_w, "W")}`);
    text("gpu-fan", unit(m.gpu_fan_percent, "%")); text("gpu-clock", unit(m.gpu_freq_mhz, "MHz", 0));
    text("gpu-memory-clock", unit(m.gpu_memory_freq_mhz, "MHz", 0)); text("gpu-pstate", d.gpu_pstate || "—");
    text("gpu-driver", d.driver_version || "—"); text("gpu-bus", d.gpu_bus_id || "—");
    text("ram-detail", `${gib(m.ram_used_bytes)} / ${gib(m.ram_total_bytes)}`); text("ram-available", gib(m.ram_available_bytes));
    text("swap-detail", `${gib(m.swap_used_bytes)} / ${gib(m.swap_total_bytes)}`); text("swap-io", `${rate(m.swap_in_bps)} / ${rate(m.swap_out_bps)}`);
    text("cpu-temp", unit(m.cpu_temp_c, "°C")); text("fan", unit(m.fan_rpm, "rpm", 0)); text("cpu-clock", unit(m.cpu_freq_mhz, "MHz", 0));
    text("uptime", duration(m.uptime_seconds)); text("load", [m.load_1, m.load_5, m.load_15].map(v => number(v, 2)).join(" / ")); text("iowait", unit(m.cpu_iowait_percent, "%"));
    text("disk-path", d.storage_path); text("disk-device", d.disk_io_device || "Block I/O unavailable"); text("disk-usage", `${gib(m.disk_used_bytes)} / ${gib(m.disk_total_bytes)}`);
    text("disk-read", rate(m.disk_read_bps)); text("disk-write", rate(m.disk_write_bps)); meter("disk-meter", has(m.disk_used_bytes) && m.disk_total_bytes > 0 ? 100 * m.disk_used_bytes / m.disk_total_bytes : null);
    text("net-interfaces", (d.network_interfaces || []).join(" + ") || "No selected interface"); text("net-rx", rate(m.net_rx_bps)); text("net-tx", rate(m.net_tx_bps)); text("tail-rx", rate(m.tailscale_rx_bps)); text("tail-tx", rate(m.tailscale_tx_bps));
    const warnings = [];
    if (data.collector_error) warnings.push(`Collector error: ${data.collector_error}. Showing the last stored reading.`);
    const telemetry = d.gpu_telemetry || d.nvidia_smi;
    if (!telemetry?.available) warnings.push(telemetry?.message || "GPU telemetry is unavailable.");
    else if (!has(m.gpu_percent)) warnings.push("GPU utilization was not reported by the available telemetry source.");
    M.notice(warnings.join(" "));
    text("footer-sample", `Saved ${M.date(data.sample.timestamp)} · unavailable readings are —`);
  }
  let interval = 5000;
  async function poll() {
    try {
      if (!document.hidden) { const data = await M.get("/api/current"); render(data); interval = data.meta.interval_seconds * 1000; }
    } catch (error) { M.offline(); M.notice(`Cannot read the monitor: ${error.message}. Existing values are the last successful reading.`); }
    setTimeout(poll, interval);
  }
  poll();
})();
