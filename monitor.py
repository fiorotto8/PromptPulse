#!/usr/bin/env python3
"""Read-only Linux/NVIDIA RTX monitor: Python standard library, SQLite and local web UI."""
from __future__ import annotations

import argparse
import csv
import copy
from contextlib import closing
import fcntl
import glob
import gzip
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

APP_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("rtx-monitor")
TAILNET = ipaddress.ip_network("100.64.0.0/10")
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
))
DEFAULTS = {
    "bind": "tailscale", "port": 8765, "interval_seconds": 5,
    "retention_days": 30, "storage_path": "/",
    "database": "data/monitor.db", "network_interfaces": [],
    "nvidia_smi_path": "auto", "gpu_index": 0,
}
# Values are SI base units, except percentages, MHz and temperatures in Celsius.
METRICS = {
    "cpu_percent": "%", "cpu_iowait_percent": "%",
    "gpu_percent": "%", "gpu_memory_percent": "%",
    "ram_used_bytes": "bytes", "ram_available_bytes": "bytes", "ram_total_bytes": "bytes",
    "swap_used_bytes": "bytes", "swap_total_bytes": "bytes",
    "swap_in_bps": "bytes/s", "swap_out_bps": "bytes/s",
    "cpu_freq_mhz": "MHz", "gpu_freq_mhz": "MHz", "gpu_memory_freq_mhz": "MHz",
    "cpu_temp_c": "C", "gpu_temp_c": "C",
    "gpu_power_w": "W", "gpu_power_limit_w": "W",
    "gpu_vram_used_bytes": "bytes", "gpu_vram_total_bytes": "bytes",
    "gpu_fan_percent": "%",
    "fan_rpm": "rpm", "disk_used_bytes": "bytes", "disk_total_bytes": "bytes",
    "disk_read_bps": "bytes/s", "disk_write_bps": "bytes/s",
    "net_rx_bps": "bytes/s", "net_tx_bps": "bytes/s",
    "tailscale_rx_bps": "bytes/s", "tailscale_tx_bps": "bytes/s",
    "load_1": "load", "load_5": "load", "load_15": "load", "uptime_seconds": "s",
}


def read_text(path):
    try:
        return Path(path).read_text().strip().replace("\x00", "")
    except (OSError, UnicodeError):
        return ""


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def validate_bind(bind):
    """Check address syntax/ranges without requiring a running network interface."""
    if bind in ("tailscale", "127.0.0.1"):
        return
    try:
        address = ipaddress.ip_address(bind)
        if address.version == 4 and any(address in network for network in PRIVATE_NETWORKS):
            return
    except ValueError:
        pass
    raise ValueError("Unsafe bind address; use tailscale, an explicit private IPv4, or 127.0.0.1 for development")


def config_load(path):
    cfg = dict(DEFAULTS)
    if path.exists():
        supplied = json.loads(path.read_text())
        if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
            raise ValueError("Configuration must be an object with documented keys only")
        cfg.update(supplied)
    for name, low, high in [("port", 1024, 65535), ("interval_seconds", 2, 300), ("retention_days", 1, 365)]:
        value = cfg[name]
        if isinstance(value, bool) or not finite(value) or not low <= value <= high:
            raise ValueError("Invalid configuration value: " + name)
    if int(cfg["port"]) != cfg["port"]:
        raise ValueError("port must be an integer")
    cfg["port"] = int(cfg["port"])
    if not isinstance(cfg["network_interfaces"], list) or any(
        not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", x)
        for x in cfg["network_interfaces"]
    ):
        raise ValueError("network_interfaces must contain interface names")
    for key in ("database", "storage_path", "nvidia_smi_path", "bind"):
        if not isinstance(cfg[key], str) or not cfg[key]:
            raise ValueError("Invalid configuration value: " + key)
    validate_bind(cfg["bind"])
    if isinstance(cfg["gpu_index"], bool) or not isinstance(cfg["gpu_index"], int) or cfg["gpu_index"] < 0:
        raise ValueError("gpu_index must be a non-negative integer")
    cfg["database"] = str((APP_DIR / cfg["database"]).resolve())
    cfg["storage_path"] = str(Path(cfg["storage_path"]).resolve())
    if not Path(cfg["storage_path"]).is_dir():
        raise ValueError("Monitored storage path does not exist: " + cfg["storage_path"])
    return cfg


def _number(text):
    text = (text or "").strip()
    if not text or text.upper() in {"N/A", "NA", "[N/A]", "NOT SUPPORTED"}:
        return None
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def parse_nvidia_csv(line):
    """Parse one nvidia-smi CSV row. Missing/unsupported fields remain absent."""
    fields = next(csv.reader([line], skipinitialspace=True))
    if len(fields) != 15:
        raise ValueError("Unexpected nvidia-smi field count: %d" % len(fields))
    (name, uuid, driver, bus_id, gpu_util, mem_util, mem_used, mem_total,
     temp, power, power_limit, graphics_clock, memory_clock, fan, pstate) = [x.strip() for x in fields]
    result = {
        "gpu_name": name, "gpu_uuid": uuid, "driver_version": driver,
        "gpu_bus_id": bus_id, "gpu_pstate": pstate,
    }
    mapping = {
        "gpu_percent": _number(gpu_util),
        "gpu_memory_percent": _number(mem_util),
        "gpu_temp_c": _number(temp),
        "gpu_power_w": _number(power),
        "gpu_power_limit_w": _number(power_limit),
        "gpu_freq_mhz": _number(graphics_clock),
        "gpu_memory_freq_mhz": _number(memory_clock),
        "gpu_fan_percent": _number(fan),
    }
    for key, value in mapping.items():
        if value is not None:
            result[key] = value
    used, total = _number(mem_used), _number(mem_total)
    if used is not None:
        result["gpu_vram_used_bytes"] = used * 1024 * 1024
    if total is not None:
        result["gpu_vram_total_bytes"] = total * 1024 * 1024
    return result


class NvidiaReader:
    QUERY = (
        "name,uuid,driver_version,pci.bus_id,utilization.gpu,utilization.memory,"
        "memory.used,memory.total,temperature.gpu,power.draw,power.limit,"
        "clocks.gr,clocks.mem,fan.speed,pstate"
    )

    def __init__(self, cfg):
        self.cfg = cfg
        path = cfg["nvidia_smi_path"]
        self.path = shutil.which("nvidia-smi") if path == "auto" else path
        if not self.path and path == "auto":
            for candidate in ("/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi"):
                if Path(candidate).is_file():
                    self.path = candidate
                    break

    def sample(self):
        if not self.path:
            return {}, {"available": False, "message": "nvidia-smi not found; host metrics remain available"}
        cmd = [self.path, "--query-gpu=" + self.QUERY, "--format=csv,noheader,nounits", "-i", str(self.cfg["gpu_index"])]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  timeout=max(2, min(5, self.cfg["interval_seconds"])), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {}, {"available": False, "message": "nvidia-smi failed: %s" % exc}
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "nvidia-smi returned an error").strip().replace("\n", " ")[:240]
            return {}, {"available": False, "message": message}
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            return {}, {"available": False, "message": "nvidia-smi returned no GPU row"}
        try:
            parsed = parse_nvidia_csv(lines[0])
        except Exception as exc:
            return {}, {"available": False, "message": "Cannot parse nvidia-smi output: %s" % exc}
        return parsed, {"available": True, "message": "Reading nvidia-smi"}


def cpu_ticks():
    values = {}
    for line in read_text("/proc/stat").splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"cpu\d*", parts[0]):
            # Guest counters are already included in user/nice; do not double count them.
            ticks = [int(v) for v in parts[1:9]]
            values[parts[0]] = (sum(ticks), ticks[3] + ticks[4], ticks[4])
    return values


def cpu_delta(previous, current):
    if previous is None:
        return None, None
    total = current[0] - previous[0]
    if total <= 0:
        return None, None
    busy = 100 * (1 - (current[1] - previous[1]) / total)
    iowait = 100 * (current[2] - previous[2]) / total
    return max(0, min(100, busy)), max(0, min(100, iowait))


class LinuxReader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.previous_cpu = {}
        self.previous_counters = {}
        self.previous_time = None
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        dev = os.stat(cfg["storage_path"]).st_dev
        self.disk_id = (os.major(dev), os.minor(dev))

    def sample(self):
        now = time.monotonic()
        elapsed = now - self.previous_time if self.previous_time else None
        self.previous_time = now
        metrics = {key: None for key in METRICS}
        details = {"cpu_cores": [], "storage_path": self.cfg["storage_path"], "network_interfaces": []}
        counters = {}

        def rate(key, value):
            counters[key] = value
            old = self.previous_counters.get(key)
            return max(0, (value - old) / elapsed) if old is not None and elapsed else None

        cpus = cpu_ticks()
        for name, values in cpus.items():
            busy, iowait = cpu_delta(self.previous_cpu.get(name), values)
            if name == "cpu":
                metrics["cpu_percent"], metrics["cpu_iowait_percent"] = busy, iowait
        for path in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*"), key=lambda p: int(re.search(r"cpu(\d+)$", p)[1])):
            core = Path(path).name
            online = read_text(Path(path) / "online") != "0"
            raw_freq = read_text(Path(path) / "cpufreq/scaling_cur_freq")
            freq = float(raw_freq) / 1000 if raw_freq.isdigit() and online else None
            busy = cpu_delta(self.previous_cpu.get(core), cpus[core])[0] if core in cpus and online else None
            details["cpu_cores"].append({"name": core, "online": online, "percent": busy, "mhz": freq})
        self.previous_cpu = cpus
        freqs = [c["mhz"] for c in details["cpu_cores"] if c["mhz"] is not None]
        metrics["cpu_freq_mhz"] = sum(freqs) / len(freqs) if freqs else None

        mem = {}
        for line in read_text("/proc/meminfo").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                mem[parts[0].rstrip(":")] = int(parts[1]) * 1024
        metrics["ram_total_bytes"] = mem.get("MemTotal")
        metrics["ram_available_bytes"] = mem.get("MemAvailable")
        if "MemTotal" in mem and "MemAvailable" in mem:
            metrics["ram_used_bytes"] = max(0, mem["MemTotal"] - mem["MemAvailable"])
        metrics["swap_total_bytes"] = mem.get("SwapTotal")
        if "SwapTotal" in mem and "SwapFree" in mem:
            metrics["swap_used_bytes"] = max(0, mem["SwapTotal"] - mem["SwapFree"])
        vm = dict(line.split()[:2] for line in read_text("/proc/vmstat").splitlines() if len(line.split()) >= 2)
        for key, target in (("pswpin", "swap_in_bps"), ("pswpout", "swap_out_bps")):
            if key in vm:
                metrics[target] = rate(key, int(vm[key]) * self.page_size)
        try:
            total, used, _ = shutil.disk_usage(self.cfg["storage_path"])
            metrics["disk_total_bytes"], metrics["disk_used_bytes"] = total, used
        except OSError:
            pass
        # Count the monitored filesystem's block device once, not whole disk + partitions.
        for line in read_text("/proc/diskstats").splitlines():
            p = line.split()
            if len(p) >= 14 and (int(p[0]), int(p[1])) == self.disk_id:
                details["disk_io_device"] = p[2]
                metrics["disk_read_bps"] = rate("disk_read", int(p[5]) * 512)
                metrics["disk_write_bps"] = rate("disk_write", int(p[9]) * 512)
                break
        interfaces = {}
        for line in read_text("/proc/net/dev").splitlines()[2:]:
            name, _, raw = line.partition(":")
            p = raw.split()
            if len(p) >= 16:
                interfaces[name.strip()] = (int(p[0]), int(p[8]))
        selected = self.cfg["network_interfaces"] or [n for n in interfaces if Path("/sys/class/net", n, "device").exists()]
        if not selected:
            selected = [n for n in interfaces if not n.startswith(("lo", "tailscale", "docker", "veth", "virbr", "br-", "tun", "tap", "wg"))]
        selected = sorted(n for n in set(selected) if n in interfaces and n != "tailscale0")
        details["network_interfaces"] = selected
        rates = {}
        for name, (rx, tx) in interfaces.items():
            if name in selected or name == "tailscale0":
                rates[name] = (rate(name + ":rx", rx), rate(name + ":tx", tx))
        if selected:
            for index, key in ((0, "net_rx_bps"), (1, "net_tx_bps")):
                values = [rates[n][index] for n in selected]
                metrics[key] = sum(values) if all(v is not None for v in values) else None
        if "tailscale0" in rates:
            metrics["tailscale_rx_bps"], metrics["tailscale_tx_bps"] = rates["tailscale0"]
        self.previous_counters = counters
        try:
            metrics["load_1"], metrics["load_5"], metrics["load_15"] = os.getloadavg()
            metrics["uptime_seconds"] = float(read_text("/proc/uptime").split()[0])
        except (OSError, ValueError, IndexError):
            pass
        temps = {}
        for path in glob.glob("/sys/class/thermal/thermal_zone*"):
            name, raw = read_text(Path(path) / "type"), read_text(Path(path) / "temp")
            try:
                value = float(raw) / 1000
                if name and -40 <= value <= 150:
                    temps[name] = value
            except ValueError:
                pass
        details["thermal_zones"] = temps
        cpu_candidates = [v for name, v in temps.items() if any(token in name.lower() for token in ("cpu", "x86_pkg", "package"))]
        if cpu_candidates:
            metrics["cpu_temp_c"] = max(cpu_candidates)
        hwtemps = {}
        for path in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
            raw = read_text(path)
            try:
                value = float(raw) / 1000
            except ValueError:
                continue
            if not -40 <= value <= 150:
                continue
            label = read_text(Path(path).with_name(Path(path).name.replace("_input", "_label")))
            hwname = read_text(Path(path).parent / "name")
            name = ":".join(x for x in (hwname, label) if x) or path
            hwtemps[name] = value
            lower = name.lower()
            if any(token in lower for token in ("package", "tctl", "tdie", "cpu")):
                cpu_candidates.append(value)
        details["hwmon_temperatures"] = hwtemps
        if cpu_candidates:
            metrics["cpu_temp_c"] = max(cpu_candidates)
        fans = {}
        for path in glob.glob("/sys/class/hwmon/hwmon*/fan*_input"):
            raw = read_text(path)
            if raw.isdigit():
                fans[path] = int(raw)
        details["fans_rpm"] = fans
        if fans:
            metrics["fan_rpm"] = next(iter(fans.values()))
        return metrics, details


class Store:
    def __init__(self, cfg):
        self.path = Path(cfg["database"])
        self.interval = float(cfg["interval_seconds"])
        self.retention = float(cfg["retention_days"]) * 86400
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported database version; do not overwrite existing data")
            columns = ", ".join('"%s" REAL' % k for k in METRICS)
            conn.execute("CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, %s)" % columns)
            conn.execute("CREATE TABLE IF NOT EXISTS latest (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            conn.execute("PRAGMA user_version=1")

    def connect(self, readonly=False):
        if readonly:
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            conn = sqlite3.connect(str(self.path), timeout=10)
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-4096")
        conn.row_factory = sqlite3.Row
        return conn

    def write(self, conn, sample):
        keys = list(METRICS)
        conn.execute("INSERT OR REPLACE INTO samples (ts,%s) VALUES (%s)" %
                     (",".join(keys), ",".join("?" for _ in range(len(keys) + 1))),
                     [sample["timestamp"]] + [sample["metrics"].get(k) for k in keys])
        conn.execute("INSERT OR REPLACE INTO latest (id,payload) VALUES (1,?)", (json.dumps(sample, allow_nan=False),))
        conn.commit()

    def last(self):
        with closing(self.connect(True)) as conn:
            row = conn.execute("SELECT payload FROM latest WHERE id=1").fetchone()
            return json.loads(row[0]) if row else None

    def prune(self, conn, now):
        conn.execute("DELETE FROM samples WHERE ts < ?", (now - self.retention,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def history(self, start, end, points):
        if not all(finite(x) for x in (start, end)) or end <= start:
            raise ValueError("Use a valid start/end time range")
        if start < 0 or end - start > self.retention + 86400:
            raise ValueError("Requested range exceeds configured retention")
        points = max(100, min(2000, int(points)))
        width = max(self.interval, math.ceil((end - start) / points / self.interval) * self.interval)
        anchor = math.floor(start / width) * width
        stats = ["AVG(%s) AS %s_avg, MIN(%s) AS %s_min, MAX(%s) AS %s_max" % (k, k, k, k, k, k) for k in METRICS]
        sql = ("SELECT CAST((ts-?)/? AS INTEGER) AS bucket, AVG(ts) AS t, COUNT(*) AS n, " +
               ",".join(stats) + " FROM samples WHERE ts>=? AND ts<=? GROUP BY bucket ORDER BY bucket")
        with closing(self.connect(True)) as conn:
            # Bound work even for accidentally large/slow queries.
            deadline = time.monotonic() + 12
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            rows = conn.execute(sql, (anchor, width, start, end)).fetchall()
        times, counts = [], []
        series = {k: {"mean": [], "min": [], "max": []} for k in METRICS}
        previous_bucket = None
        for row in rows:
            if previous_bucket is not None and row["bucket"] > previous_bucket + 1:
                times.append(anchor + (previous_bucket + 1.5) * width)
                counts.append(0)
                for values in series.values():
                    for array in values.values():
                        array.append(None)
            times.append(row["t"])
            counts.append(row["n"])
            for key in METRICS:
                for label, suffix in (("mean", "avg"), ("min", "min"), ("max", "max")):
                    series[key][label].append(row[key + "_" + suffix])
            previous_bucket = row["bucket"]
        return {"start": start, "end": end, "bucket_seconds": width, "timestamps": times,
                "sample_counts": counts, "sample_count": sum(counts), "series": series, "units": METRICS}


class Application:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.store = Store(cfg)
        self.latest = self.store.last()
        self.error = None
        self.nvidia = NvidiaReader(cfg)
        self.linux = LinuxReader(cfg)
        self.meta = {
            "hostname": socket.gethostname(),
            "model": (" ".join(x for x in (read_text("/sys/class/dmi/id/sys_vendor"), read_text("/sys/class/dmi/id/product_name")) if x).strip() or "Linux PC"),
            "interval_seconds": cfg["interval_seconds"], "retention_days": cfg["retention_days"],
            "storage_path": cfg["storage_path"], "port": cfg["port"],
        }

    def collect(self):
        metrics, details = self.linux.sample()
        gpu, status = self.nvidia.sample()
        for key in METRICS:
            if key in gpu:
                metrics[key] = gpu[key]
        details.update({k: v for k, v in gpu.items() if k not in METRICS})
        details["nvidia_smi"] = status
        metrics = {k: round(v, 4) if finite(v) else None for k, v in metrics.items()}
        return {"timestamp": time.time(), "metrics": metrics, "details": details}

    def sample_loop(self):
        conn = self.store.connect()
        last_prune = 0
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    sample = self.collect()
                    self.store.write(conn, sample)
                    with self.lock:
                        self.latest, self.error = sample, None
                    if time.time() - last_prune >= 3600:
                        self.store.prune(conn, time.time())
                        last_prune = time.time()
                except Exception as exc:
                    LOG.exception("Sampling failed")
                    with self.lock:
                        self.error = str(exc)
                self.stop.wait(max(0.1, self.cfg["interval_seconds"] - (time.monotonic() - started)))
        finally:
            conn.close()

    def current(self):
        with self.lock:
            latest, error = copy.deepcopy(self.latest), self.error
        now = time.time()
        age = max(0, now - latest["timestamp"]) if latest else None
        disk_bytes = 0
        for path in (self.store.path, Path(str(self.store.path) + "-wal")):
            try:
                disk_bytes += path.stat().st_size
            except OSError:
                pass
        return {"meta": self.meta, "sample": latest, "server_time": now,
                "age_seconds": age, "stale": age is None or age > max(15, 3 * self.cfg["interval_seconds"]),
                "collector_error": error, "database_bytes": disk_bytes}


def tailnet_address(bind):
    validate_bind(bind)
    if bind == "127.0.0.1":
        return bind
    if bind == "tailscale":
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                raw = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", b"tailscale0"))
            actual = socket.inet_ntoa(raw[20:24])
            if ipaddress.ip_address(actual) not in TAILNET:
                raise RuntimeError("Configured address is not the local tailscale0 IPv4")
            return actual
        except OSError as exc:
            raise RuntimeError("Waiting for tailscale0 to have an IPv4 address") from exc
    if ipaddress.ip_address(bind) in TAILNET:
        actual = tailnet_address("tailscale")
        if bind != actual:
            raise RuntimeError("Configured address is not the local tailscale0 IPv4")
    return bind


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, address, app):
        self.app = app
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)
        self.timeout = 1

    def verify_request(self, request, client_address):
        peer = ipaddress.ip_address(client_address[0])
        address = ipaddress.ip_address(self.server_address[0])
        if address.is_loopback:
            return peer.is_loopback
        if address in TAILNET:
            return peer in TAILNET
        return any(peer in network for network in PRIVATE_NETWORKS if network != TAILNET)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "RTXMonitor"
    sys_version = ""
    STATIC = {"/": ("current.html", "text/html"), "/history": ("history.html", "text/html"),
              "/static/style.css": ("style.css", "text/css"),
              "/static/common.js": ("common.js", "application/javascript"),
              "/static/current.js": ("current.js", "application/javascript"),
              "/static/history.js": ("history.js", "application/javascript")}

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, fmt, *args):
        # No per-refresh journal noise. Errors are logged below.
        pass

    def reply(self, status, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        compressed = "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 2048
        if compressed:
            body = gzip.compress(body, compresslevel=1)
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        host = "%s:%s" % self.server.server_address
        if self.headers.get("Host") != host or self.headers.get("Origin", "http://" + host) != "http://" + host:
            self.reply(403, {"error": "Use this machine's exact IP:port"})
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.path in self.STATIC:
                path, mime = self.STATIC[parsed.path]
                self.reply(200, (APP_DIR / "static" / path).read_bytes(), mime)
            elif parsed.path == "/api/current":
                self.reply(200, self.server.app.current())
            elif parsed.path == "/api/history":
                args = parse_qs(parsed.query, max_num_fields=8)
                now = time.time()
                start = float(args.get("start", [now - 3600])[0])
                end = float(args.get("end", [now])[0])
                points = int(args.get("points", [900])[0])
                self.reply(200, self.server.app.store.history(start, end, points))
            else:
                self.reply(404, {"error": "Not found"})
        except (ValueError, OverflowError) as exc:
            self.reply(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except sqlite3.OperationalError:
            LOG.exception("Database read failed")
            self.reply(503, {"error": "Database temporarily unavailable; narrow the range or retry"})
        except Exception:
            LOG.exception("Request failed")
            self.reply(500, {"error": "Internal read error; check the service log"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=APP_DIR / "config.json")
    parser.add_argument("--bind", help="Override bind: tailscale or 127.0.0.1 (local tests only)")
    parser.add_argument("--once", action="store_true", help="Print one measured sample, then exit; no HTTP listener")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config_load(args.config)
    if args.bind:
        cfg["bind"] = args.bind
    if cfg["bind"] not in ("tailscale", "127.0.0.1"):
        # Fail unsafe configuration before starting collectors.
        tailnet_address(cfg["bind"])
    app = Application(cfg)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.stop.set())
    if args.once:
        try:
            app.collect()  # Prime interval-based CPU and I/O counters.
            app.stop.wait(cfg["interval_seconds"] + 1)
            print(json.dumps({"meta": app.meta, "sample": app.collect()}, indent=2, allow_nan=False))
        finally:
            app.stop.set()
        return
    worker = threading.Thread(target=app.sample_loop, name="sampler", daemon=True)
    worker.start()
    server = None
    last_warning = 0
    try:
        while not app.stop.is_set() and server is None:
            try:
                ip = tailnet_address(cfg["bind"])
                server = MonitorServer((ip, cfg["port"]), app)
            except (RuntimeError, OSError) as exc:
                if time.monotonic() - last_warning > 60:
                    LOG.warning("HTTP listener: %s; measurements continue", exc)
                    last_warning = time.monotonic()
                app.stop.wait(5)
        if server:
            LOG.info("Dashboard: http://%s:%s", *server.server_address)
            while not app.stop.is_set():
                if not worker.is_alive():
                    raise RuntimeError("Sampler thread stopped unexpectedly")
                server.handle_request()
    finally:
        app.stop.set()
        if server:
            server.server_close()
        worker.join(timeout=10)


if __name__ == "__main__":
    main()
