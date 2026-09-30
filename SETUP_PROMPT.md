# Set up PromptPulse on this machine

Use this repository as the working application. Inspect this Linux host, configure
the monitor for it, and install and verify the service. Hardware telemetry is optional.
Prefer configuration; add a small collector when an available tool needs support.
Keep the work and your responses concise.
The installer copies the application to `/opt/promptpulse` and creates
`promptpulse.service`. The checkout folder may have any name.

## Inspect once

Read `config.example.json`, `install.sh`, and `promptpulse.service`. Read the README
for usage and only the relevant readers in `monitor.py` when adapting telemetry.
Do not scan unrelated directories, dump hardware inventories, create
planning/report files, or install extra tooling to perform routine checks.

In one batch, check Python/SQLite versions, systemd, the normal service account,
Tailscale's connection and IPv4, available private LAN addresses, `/proc` and `/sys`,
the filesystem to monitor, physical network interfaces, and existing telemetry
sources appropriate to the detected hardware: for example `nvidia-smi`, Jetson's
`tegrastats`, vendor GPU utilities, and readable hwmon/devfreq sensors. Use bounded,
read-only probes as the normal service account; finding a binary is not proof that
it supplies usable readings. Missing tools, GPU access, or sensors must not block
host monitoring. Do not install drivers or telemetry packages automatically.
Inspect any existing `/opt/promptpulse/config.json` and service before changing them.

## Adapt available telemetry

- Configure an existing reader first. PromptPulse prefers `nvidia-smi` and falls
  back to `tegrastats` for GPU index `0`; both executable paths default to `auto`.
- If a useful installed source is unsupported, implement the smallest reader and
  parser in `monitor.py`, following the existing `sample()` values/status contract.
  Wire it into collection and the generic `details.gpu_telemetry` status for GPU
  sources. Update installer discovery, documented configuration, and focused tests
  as needed; do not build a plugin framework or add dependencies.
- Verify field meanings and units from actual output and the tool's documentation.
  Populate existing metrics only when equivalent. Keep other readings in details;
  shared RAM is not VRAM, system EMC is not GPU memory utilization, and combined
  CPU/GPU or whole-device power is not GPU-only power. Missing, invalid, or stale
  readings remain null. Discover device paths instead of hardcoding bus addresses.
- Bound subprocess waits and output, isolate tool failures from host sampling, and
  terminate/reap only children this application owns. Test representative output,
  missing tools/permissions, stale data, and cleanup. Never use global stop commands.

## Configure locally

- On a first install, copy `config.example.json` to ignored `config.json` and change
  only settings this host needs. On upgrades, the installed configuration takes
  precedence: preserve it and any existing measurements.
- Keep `bind: tailscale` when Tailscale is connected. If it is unavailable, ask
  whether to connect Tailscale or use a specific private LAN address found on this
  host. Do not guess the user's intended network or install Tailscale automatically.
- LAN mode must bind one assigned RFC1918 IPv4, such as `192.168.1.50`. Never use
  wildcard, public, or hostname binds, change firewall/ACL rules, or expose the
  dashboard publicly. Loopback is only for development/tests, never installation.
- Keep the port at `8765` unless occupied. Use an absolute `storage_path` (default
  `/`), automatic interface selection unless it is unsuitable, and automatic tool
  discovery with GPU index `0` unless the host requires another available GPU.
- Preserve `data/monitor.db`, 5-second sampling, 30-day retention, API routes, schema,
  units, and the standard-library-only design. Unavailable values stay null/`—`.
- Do not embed this host's paths, addresses, or device names in tracked defaults.

## Verify and install

1. Run `python3 -B -m unittest discover -s tests -v` and `bash -n install.sh`.
2. State the chosen network mode, service account, and any unavailable telemetry
   briefly. Run `sudo bash install.sh` using that normal account. If privileges are
   unavailable, give the exact command for the user; never ask for their password.
3. Check `systemctl status promptpulse --no-pager` and recent service logs. Fetch
   `/api/current` using the exact private IP and port; verify a fresh host sample
   and readings from the selected telemetry source after its first interval.
   Check `/` and `/history`; use a browser if already available. For Tailscale, the
   viewing device must also have access to this Tailnet.
4. If a host-specific defect remains, inspect only its controlling code path, make
   the smallest compatible fix, and run focused tests plus the existing suite.
   Do not redesign the application or add dependencies/extra project structure.
5. Finish with the dashboard URL, changed settings/collectors, selected telemetry
   source, checks passed, and any unavailable readings, inconsistencies, or genuine
   blocker. Keep local configuration, logs, databases, and reports out of Git.

Do not seed synthetic data, replace an existing database, or start a second writer
against the installed database. Do not commit or publish machine-specific setup.
