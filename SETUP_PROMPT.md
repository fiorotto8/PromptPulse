# Set up PromptPulse on this machine

Use this repository as the working application. Inspect this Linux host, configure
the monitor for it, and install and verify the service. NVIDIA hardware is optional.
Prefer configuration over code changes. Keep the work and your responses concise.
The service and installation path remain `rtx-monitor` and `/opt/rtx-monitor` for
upgrade compatibility; do not rename or migrate them during setup.

## Inspect once

Read `config.example.json`, `install.sh`, and `rtx-monitor.service`. Read the README
for usage and only the relevant functions in `monitor.py` if a compatibility issue
appears. Do not scan unrelated directories, dump hardware inventories, create
planning/report files, or install extra tooling to perform routine checks.

In one batch, check Python/SQLite versions, systemd, the normal service account,
Tailscale's connection and IPv4, available private LAN addresses, `/proc` and `/sys`,
the filesystem to monitor, physical network interfaces, and optional `nvidia-smi`.
Missing NVIDIA tools, GPU access, or sensors must not block host monitoring.
Inspect any existing `/opt/rtx-monitor/config.json` and service before changing them.

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
  `/`), automatic interface selection unless it is unsuitable, and automatic NVIDIA
  discovery with GPU index `0` unless the host requires another available GPU.
- Preserve `data/monitor.db`, 5-second sampling, 30-day retention, API routes, schema,
  units, and the standard-library-only design. Unavailable values stay null/`—`.
- Do not embed this host's paths, addresses, or device names in tracked defaults.

## Verify and install

1. Run `python3 -B -m unittest discover -s tests -v` and `bash -n install.sh`.
2. State the chosen network mode, service account, and any unavailable telemetry
   briefly. Run `sudo bash install.sh` using that normal account. If privileges are
   unavailable, give the exact command for the user; never ask for their password.
3. Check `systemctl status rtx-monitor --no-pager` and recent service logs. Fetch
   `/api/current` using the exact private IP and port; verify a fresh host sample.
   Check `/` and `/history`; use a browser if already available. For Tailscale, the
   viewing device must also have access to this Tailnet.
4. If a host-specific defect remains, inspect only its controlling code path, make
   the smallest compatible fix, and run focused tests plus the existing suite.
   Do not redesign the application or add dependencies/extra project structure.
5. Finish with the dashboard URL, changed settings, checks passed, and any genuine
   blocker. Keep local configuration, logs, databases, and reports out of Git.

Do not seed synthetic data, replace an existing database, or start a second writer
against the installed database. Do not commit or publish machine-specific setup.
