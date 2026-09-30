"use strict";
(() => {
  const {byId, has, number, text} = M;

  const definitions = [
    {title: "Utilization", unit: "%", max: 100, wide: true, keys: [["cpu_percent", "CPU"], ["gpu_percent", "GPU"], ["gpu_memory_percent", "GPU memory controller"]]},
    {title: "Host memory", unit: "GiB", factor: 1073741824, keys: [["ram_used_bytes", "RAM used"], ["ram_available_bytes", "RAM available"], ["swap_used_bytes", "Swap used"]]},
    {title: "GPU VRAM", unit: "GiB", factor: 1073741824, keys: [["gpu_vram_used_bytes", "VRAM used"], ["gpu_vram_total_bytes", "VRAM total"]]},
    {title: "Temperatures", unit: "°C", keys: [["cpu_temp_c", "CPU"], ["gpu_temp_c", "GPU"]]},
    {title: "GPU power", unit: "W", keys: [["gpu_power_w", "Draw"], ["gpu_power_limit_w", "Limit"]]},
    {title: "Clocks", unit: "MHz", keys: [["cpu_freq_mhz", "CPU mean"], ["gpu_freq_mhz", "GPU graphics"], ["gpu_memory_freq_mhz", "GPU memory"]]},
    {title: "Storage I/O", unit: "MiB/s", factor: 1048576, keys: [["disk_read_bps", "Read"], ["disk_write_bps", "Write"]]},
    {title: "Network", unit: "MiB/s", factor: 1048576, keys: [["net_rx_bps", "Receive"], ["net_tx_bps", "Transmit"]]},
    {title: "Tailscale traffic", unit: "MiB/s", factor: 1048576, keys: [["tailscale_rx_bps", "Receive"], ["tailscale_tx_bps", "Transmit"]]},
    {title: "Swap I/O", unit: "MiB/s", factor: 1048576, keys: [["swap_in_bps", "Swap in"], ["swap_out_bps", "Swap out"]]},
    {title: "Storage capacity", unit: "GiB", factor: 1073741824, keys: [["disk_used_bytes", "Used"]]},
    {title: "Load average", unit: "runnable / waiting", keys: [["load_1", "1 minute"], ["load_5", "5 minutes"], ["load_15", "15 minutes"]]},
  ];
  let from = Date.now() / 1000 - 3600, to = Date.now() / 1000;
  let base = {duration: 3600}, retention = 30 * 86400, sampleInterval = 5;
  let controller = null, debounce = null, lastHistoryRead = 0;
  const live = byId("live");
  const localInput = ts => { const d = new Date(ts * 1000); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19); };
  function niceMax(value) {
    if (!has(value) || value <= 0) return 1;
    const exponent = 10 ** Math.floor(Math.log10(value)), fraction = value / exponent;
    return (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10) * exponent;
  }
  function axisNumber(v) { return v >= 1000 ? number(v, 0) : v >= 10 ? number(v, 0) : v >= 1 ? number(v, 1) : number(v, 2); }

  class Chart {
    constructor(definition) {
      this.def = definition; this.factor = definition.factor || 1;
      this.hidden = new Set(); this.data = null; this.hover = null; this.drag = null;
      this.panel = document.createElement("section"); this.panel.className = "chart-panel" + (definition.wide ? " wide" : "");
      const heading = document.createElement("div"); heading.className = "panel-head";
      const title = document.createElement("h2"); title.textContent = definition.title;
      const unit = document.createElement("span"); unit.className = "hint"; unit.textContent = definition.unit;
      heading.append(title, unit);
      const legend = document.createElement("div"); legend.className = "legend";
      definition.keys.forEach(([key, label], index) => {
        const button = document.createElement("button"); button.setAttribute("aria-pressed", "true");
        const swatch = document.createElement("span"); swatch.style.background = `var(--chart-${index + 1})`; button.append(swatch, document.createTextNode(label));
        button.onclick = () => { this.hidden.has(key) ? this.hidden.delete(key) : this.hidden.add(key); button.classList.toggle("disabled", this.hidden.has(key)); button.setAttribute("aria-pressed", String(!this.hidden.has(key))); this.draw(); };
        legend.append(button);
      });
      this.wrap = document.createElement("div"); this.wrap.className = "canvas-wrap";
      this.canvas = document.createElement("canvas"); this.canvas.setAttribute("role", "img");
      this.canvas.setAttribute("aria-label", `${definition.title} time plot. Use the time-range controls to zoom and move through history.`);
      this.tooltip = document.createElement("div"); this.tooltip.className = "tooltip"; this.tooltip.hidden = true;
      this.wrap.append(this.canvas, this.tooltip);
      this.foot = document.createElement("div"); this.foot.className = "chart-footer";
      this.panel.append(heading, legend, this.wrap, this.foot); byId("charts").append(this.panel);
      this.ctx = this.canvas.getContext("2d");
      this.resize = new ResizeObserver(() => this.draw()); this.resize.observe(this.wrap);
      this.canvas.addEventListener("pointerdown", event => this.down(event));
      this.canvas.addEventListener("pointermove", event => this.move(event));
      this.canvas.addEventListener("pointerup", event => this.up(event));
      this.canvas.addEventListener("pointercancel", () => {this.drag = null; this.draw();});
      this.canvas.addEventListener("pointerleave", () => { if (!this.drag) {this.hover = null; this.tooltip.hidden = true; this.draw();} });
      this.canvas.addEventListener("dblclick", () => reset());
      this.canvas.addEventListener("wheel", event => {
        if (!event.ctrlKey) return;
        event.preventDefault();
        const x = this.position(event).x, fraction = Math.max(0, Math.min(1, (x - 51) / (this.width - 66)));
        zoom(event.deltaY > 0 ? 1.25 : 0.8, fraction);
      }, {passive: false});
    }
    position(event) { const rect = this.canvas.getBoundingClientRect(); return {x: event.clientX - rect.left, y: event.clientY - rect.top}; }
    down(event) {
      if (event.button !== 0) return;
      const point = this.position(event);
      if (point.x < 51 || point.x > this.width - 15 || point.y < 12 || point.y > this.height - 32) return;
      this.drag = {start: point.x, end: point.x}; this.canvas.setPointerCapture(event.pointerId);
    }
    move(event) {
      const point = this.position(event);
      if (this.drag) { this.drag.end = Math.max(51, Math.min(this.width - 15, point.x)); this.tooltip.hidden = true; }
      else this.hover = point;
      this.draw();
    }
    up(event) {
      if (!this.drag) return;
      const {start, end} = this.drag; this.drag = null;
      if (this.canvas.hasPointerCapture(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
      if (Math.abs(end - start) >= 8) {
        const span = to - from, width = this.width - 66;
        changeRange(from + (Math.min(start, end) - 51) / width * span, from + (Math.max(start, end) - 51) / width * span);
      } else this.draw();
    }
    setData(data) {
      this.data = data; this.hover = null; this.tooltip.hidden = true;
      this.foot.textContent = data.sample_count ? (data.bucket_seconds > sampleInterval ? "Mean lines · shaded min–max ranges" : "Recorded samples") : "No stored measurements in this range";
      this.draw();
    }
    draw() {
      const style = getComputedStyle(document.documentElement);
      const color = name => style.getPropertyValue(name).trim();
      const palette = [1, 2, 3, 4].map(n => color(`--chart-${n}`));
      const width = this.width = Math.max(180, this.wrap.clientWidth), height = this.height = this.wrap.clientHeight;
      const ratio = window.devicePixelRatio || 1;
      if (this.canvas.width !== Math.round(width * ratio) || this.canvas.height !== Math.round(height * ratio)) {
        this.canvas.width = Math.round(width * ratio); this.canvas.height = Math.round(height * ratio);
      }
      const ctx = this.ctx; ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height); ctx.fillStyle = color("--paper"); ctx.fillRect(0, 0, width, height);
      const left = 51, right = width - 15, top = 12, bottom = height - 32, span = to - from;
      const x = t => left + (t - from) / span * (right - left);
      let maximum = 0, readings = false;
      if (this.data) this.def.keys.forEach(([key]) => {
        if (this.hidden.has(key)) return;
        const series = this.data.series[key];
        if (series) for (const value of series.max) if (has(value)) {maximum = Math.max(maximum, value / this.factor); readings = true;}
      });
      const upper = this.def.max || niceMax(maximum * 1.07);
      const y = value => bottom - value / upper * (bottom - top);
      ctx.font = "10px system-ui, sans-serif"; ctx.fillStyle = color("--muted"); ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const value = upper * i / 4, yy = y(value);
        ctx.strokeStyle = color("--line"); ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(right, yy); ctx.stroke();
        ctx.textAlign = "right"; ctx.fillText(axisNumber(value), left - 9, yy + 3);
      }
      const ticks = width < 430 ? 3 : width < 750 ? 4 : 7;
      for (let i = 0; i < ticks; i++) {
        const time = from + span * i / (ticks - 1), xx = x(time), d = new Date(time * 1000);
        const label = span >= 172800 ? d.toLocaleDateString(undefined, {month: "short", day: "numeric"}) : d.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit", ...(span < 600 ? {second: "2-digit"} : {})});
        ctx.textAlign = i === 0 ? "left" : i === ticks - 1 ? "right" : "center";
        ctx.fillText(label, xx, bottom + 20);
      }
      if (!this.data || !this.data.timestamps.length || !readings) {
        ctx.fillStyle = color("--muted"); ctx.textAlign = "center"; ctx.font = "12px system-ui, sans-serif";
        ctx.fillText(!this.data ? "Loading measurements…" : !this.data.timestamps.length ? "No samples in this time range" : this.hidden.size === this.def.keys.length ? "All series hidden" : "No sensor readings in this time range", (left + right) / 2, (top + bottom) / 2);
        this.tooltip.hidden = true; return;
      }
      const times = this.data.timestamps;
      ctx.save(); ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
      this.def.keys.forEach(([key], index) => {
        if (this.hidden.has(key)) return;
        const series = this.data.series[key]; if (!series) return;
        const halfBucket = this.data.bucket_seconds / span * (right - left) / 2;
        ctx.fillStyle = palette[index]; ctx.globalAlpha = 0.16;
        for (let i = 0; i < times.length; i++) {
          if (!has(series.min[i]) || !has(series.max[i]) || series.min[i] === series.max[i]) continue;
          const yy = y(series.max[i] / this.factor), low = y(series.min[i] / this.factor);
          ctx.fillRect(x(times[i]) - halfBucket, yy, Math.max(1, halfBucket * 2), Math.max(1, low - yy));
        }
        ctx.globalAlpha = 1; ctx.strokeStyle = palette[index]; ctx.lineWidth = 1.65;
        ctx.beginPath(); let active = false;
        for (let i = 0; i < times.length; i++) {
          const value = series.mean[i];
          if (!has(value)) {active = false; continue;}
          const xx = x(times[i]), yy = y(value / this.factor);
          active ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy); active = true;
        }
        ctx.stroke();
        // Visible isolated samples, without drawing a line across an outage.
        ctx.fillStyle = palette[index];
        for (let i = 0; i < times.length; i++) if (has(series.mean[i]) &&
          (i === 0 || !has(series.mean[i - 1])) && (i === times.length - 1 || !has(series.mean[i + 1]))) {
          ctx.beginPath(); ctx.arc(x(times[i]), y(series.mean[i] / this.factor), 2.5, 0, Math.PI * 2); ctx.fill();
        }
      });
      if (this.drag) {
        ctx.fillStyle = color("--selection"); ctx.fillRect(Math.min(this.drag.start, this.drag.end), top, Math.abs(this.drag.end - this.drag.start), bottom - top);
        ctx.strokeStyle = color("--accent"); ctx.lineWidth = 1; ctx.strokeRect(Math.min(this.drag.start, this.drag.end), top, Math.abs(this.drag.end - this.drag.start), bottom - top);
      }
      if (this.hover && !this.drag && this.hover.x >= left && this.hover.x <= right && this.hover.y >= top && this.hover.y <= bottom) {
        const target = from + (this.hover.x - left) / (right - left) * span;
        let low = 0, high = times.length - 1;
        while (low < high) { const middle = Math.floor((low + high) / 2); if (times[middle] < target) low = middle + 1; else high = middle; }
        let idx = low;
        if (idx > 0 && Math.abs(times[idx - 1] - target) < Math.abs(times[idx] - target)) idx--;
        if (Math.abs(times[idx] - target) <= this.data.bucket_seconds * 1.5 && this.data.sample_counts[idx]) {
          const xx = x(times[idx]); ctx.strokeStyle = color("--muted"); ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(xx, top); ctx.lineTo(xx, bottom); ctx.stroke(); ctx.setLineDash([]);
          const lines = [M.date(times[idx])];
          this.def.keys.forEach(([key, label]) => {
            if (this.hidden.has(key)) return;
            const series = this.data.series[key], mean = series.mean[idx];
            if (!has(mean)) {lines.push(`${label}: —`); return;}
            let line = `${label}: ${number(mean / this.factor, 2)} ${this.def.unit}`;
            if (this.data.sample_counts[idx] > 1 && series.min[idx] !== series.max[idx]) line += `\n  min ${number(series.min[idx] / this.factor, 2)} · max ${number(series.max[idx] / this.factor, 2)}`;
            lines.push(line);
          });
          if (this.data.sample_counts[idx] > 1) lines.push(`${this.data.sample_counts[idx]} samples in this bucket`);
          this.tooltip.textContent = lines.join("\n"); this.tooltip.hidden = false;
          const tooltipWidth = this.tooltip.offsetWidth;
          this.tooltip.style.left = `${Math.max(0, Math.min(width - tooltipWidth, this.hover.x + 13))}px`;
          this.tooltip.style.top = `${Math.max(0, Math.min(height - this.tooltip.offsetHeight, 18))}px`;
        } else this.tooltip.hidden = true;
      } else this.tooltip.hidden = true;
      ctx.restore();
    }
  }
  const charts = definitions.map(def => new Chart(def));
  window.addEventListener("themechange", () => charts.forEach(chart => chart.draw()));
  function displayRange() {
    byId("from").value = localInput(from); byId("to").value = localInput(to);
    text("range-label", `${M.date(from)} → ${M.date(to)}`);
    charts.forEach(chart => chart.draw());
  }
  async function load() {
    if (controller) controller.abort(); controller = new AbortController();
    const myController = controller;
    text("resolution", "Loading…");
    try {
      const data = await M.get(`/api/history?start=${from}&end=${to}&points=1000`, myController.signal);
      if (myController !== controller) return;
      lastHistoryRead = Date.now(); charts.forEach(chart => chart.setData(data));
      const grouped = data.sample_counts.some(n => n > 1);
      text("resolution", data.sample_count ? `${data.sample_count.toLocaleString()} samples · ${grouped ? `${number(data.bucket_seconds, 0)}s buckets` : "recorded resolution"}` : "No stored samples in this range");
      text("history-summary", `${retention / 86400}-day retention · ${sampleInterval}s sampling · browser local time`);
      M.notice("");
    } catch (error) {
      if (error.name === "AbortError") return;
      text("resolution", "History unavailable"); M.notice(`Cannot load this range: ${error.message}`);
    }
  }
  function queueLoad() { clearTimeout(debounce); debounce = setTimeout(load, 180); }
  function changeRange(start, end, pause = true) {
    const minSpan = Math.max(20, sampleInterval * 2), now = Date.now() / 1000;
    let span = Math.min(retention, Math.max(minSpan, end - start));
    if (end > now) {end = now; start = end - span;} else if (end - start !== span) start = (start + end - span) / 2;
    from = Math.max(0, start); to = from + span;
    if (pause) live.checked = false;
    displayRange(); queueLoad();
  }
  function zoom(factor, fraction = 0.5) {
    const span = to - from, newSpan = Math.min(retention, Math.max(20, sampleInterval * 2, span * factor));
    const anchor = from + span * fraction;
    changeRange(anchor - newSpan * fraction, anchor + newSpan * (1 - fraction));
  }
  function reset() {
    if (base.duration) {live.checked = true; const now = Date.now() / 1000; changeRange(now - base.duration, now, false);}
    else {live.checked = false; changeRange(base.from, base.to, false);}
  }
  byId("preset").addEventListener("change", event => {
    if (event.target.value === "custom") {live.checked = false; return;}
    base = {duration: Math.min(retention, Number(event.target.value))}; reset();
  });
  ["from", "to"].forEach(id => byId(id).addEventListener("change", () => {byId("preset").value = "custom"; live.checked = false;}));
  byId("apply").onclick = () => {
    const start = new Date(byId("from").value).getTime() / 1000, end = new Date(byId("to").value).getTime() / 1000;
    if (!has(start) || !has(end) || end <= start) {M.notice("Choose an end time after the start time."); return;}
    if (end - start > retention) {M.notice(`Choose a range no longer than ${retention / 86400} days.`); return;}
    base = {from: start, to: end}; byId("preset").value = "custom"; changeRange(start, end);
  };
  byId("pan-left").onclick = () => {const half = (to - from) / 2; changeRange(from - half, to - half);};
  byId("pan-right").onclick = () => {const half = (to - from) / 2; changeRange(from + half, to + half);};
  byId("zoom-in").onclick = () => zoom(0.5); byId("zoom-out").onclick = () => zoom(2); byId("reset").onclick = reset;
  live.onchange = () => {if (live.checked) {const span = to - from, now = Date.now() / 1000; changeRange(now - span, now, false);}};
  async function metadata() {
    try {
      const data = await M.get("/api/current"); M.receive(data); retention = data.meta.retention_days * 86400; sampleInterval = data.meta.interval_seconds;
      for (const option of byId("preset").options) if (option.value !== "custom") option.disabled = Number(option.value) > retention;
    } catch (error) {M.offline(); M.notice(`Cannot reach the monitor: ${error.message}`);}
  }
  async function init() {await metadata(); displayRange(); await load();}
  setInterval(async () => {
    if (document.hidden) return;
    await metadata();
    const refreshSeconds = Math.max(15, Math.min(300, (to - from) / 1000));
    if (live.checked && Date.now() - lastHistoryRead >= refreshSeconds * 1000) {const span = to - from; to = Date.now() / 1000; from = to - span; displayRange(); await load();}
  }, 15000);
  init();
})();
