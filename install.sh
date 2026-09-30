#!/usr/bin/env bash
# Install the monitor without adding packages or changing NVIDIA/Tailscale configuration.
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
BASE=/opt/rtx-monitor
UNIT=/etc/systemd/system/rtx-monitor.service
[[ "$EUID" -eq 0 ]] || { echo 'Run: sudo bash install.sh' >&2; exit 1; }
RUN_USER=${MONITOR_USER:-${SUDO_USER:-}}
[[ -n "$RUN_USER" && "$RUN_USER" != root ]] || {
  echo 'Select the normal login account, for example: sudo MONITOR_USER=myuser bash install.sh' >&2
  exit 1
}
id "$RUN_USER" >/dev/null
RUN_GROUP=$(id -gn "$RUN_USER")
[[ "$RUN_USER" =~ ^[a-zA-Z0-9_.-]+$ && "$RUN_GROUP" =~ ^[a-zA-Z0-9_.-]+$ ]] || { echo 'Unsupported account name' >&2; exit 1; }
[[ -x /usr/bin/python3 ]] || { echo '/usr/bin/python3 is required.' >&2; exit 1; }
/usr/bin/python3 - <<'PY'
import sys, sqlite3
assert sys.version_info >= (3, 8), 'Python 3.8 or newer is required'
assert sqlite3.sqlite_version_info >= (3, 24), 'SQLite 3.24 or newer is required'
PY
# Resolve and validate the effective config before changing installed files.
cd "$SOURCE"
CONFIG_SOURCE="$SOURCE/config.example.json"
[[ ! -f "$SOURCE/config.json" ]] || CONFIG_SOURCE="$SOURCE/config.json"
[[ ! -f "$BASE/config.json" ]] || CONFIG_SOURCE="$BASE/config.json"
INSTALL_BIND=$(/usr/bin/python3 -B - "$CONFIG_SOURCE" "$BASE" <<'PYCONFIG'
import json
import sys
from pathlib import Path
import monitor
# Resolve paths exactly as the installed service will, without opening its database.
monitor.APP_DIR = Path(sys.argv[2])
c = monitor.config_load(Path(sys.argv[1]))
if c['bind'] == '127.0.0.1':
    raise ValueError('Loopback is for development only; choose tailscale or an explicit private IPv4')
if c['database'] != str(monitor.APP_DIR / 'data/monitor.db'):
    raise ValueError('Keep the default database at data/monitor.db for the supplied service')
if not Path(json.loads(Path(sys.argv[1]).read_text()).get('storage_path', '/')).is_absolute():
    raise ValueError('Use an absolute storage_path, such as /')
print(c['bind'])
PYCONFIG
)
if [[ "$INSTALL_BIND" == tailscale ]] && ! command -v tailscale >/dev/null; then
  echo 'The tailscale bind requires Tailscale. Connect it or set an explicit private IPv4 in config.json.' >&2
  exit 1
fi
command -v systemctl >/dev/null || { echo 'systemd is required for this installer.' >&2; exit 1; }
if [[ -e "$UNIT" ]]; then
  grep -q '^# rtx-monitor managed unit' "$UNIT" || { echo "An unrelated $UNIT already exists. Refusing to overwrite it." >&2; exit 1; }
fi
/usr/bin/python3 -B -m unittest discover -s tests -v
# Copy only project files, never checkout metadata, caches, secrets or environments.
ASSETS=(monitor.py README.md CONTRIBUTING.md LICENSE SETUP_PROMPT.md
        config.example.json install.sh rtx-monitor.service
        static/*.html static/*.css static/*.js tests/test*.py docs/*.png)
for item in "${ASSETS[@]}"; do
  [[ -f "$SOURCE/$item" ]] || { echo "Missing project asset: $item" >&2; exit 1; }
done
install -d -m 0755 "$BASE"
if [[ "$SOURCE" != "$BASE" ]]; then
  for item in "${ASSETS[@]}"; do
    install -d -m 0755 "$BASE/$(dirname "$item")"
    install -m 0644 "$SOURCE/$item" "$BASE/$item"
  done
fi
# Remove the superseded managed prompt after installing its replacement.
rm -f -- "$BASE/DEVICE_AGNOSTIC_IMPLEMENTATION_PROMPT.md"
[[ -f "$BASE/config.json" ]] || install -m 0644 "$CONFIG_SOURCE" "$BASE/config.json"
install -d -m 0700 -o "$RUN_USER" -g "$RUN_GROUP" "$BASE/data"
chown -R root:root "$BASE/static" "$BASE/tests"
chown root:root "$BASE/monitor.py" "$BASE/config.example.json" "$BASE/rtx-monitor.service" "$BASE/README.md"
chmod 0755 "$BASE/monitor.py"
cd "$BASE"
if command -v nvidia-smi >/dev/null; then
  runuser -u "$RUN_USER" -- nvidia-smi -L >/dev/null || { echo 'The service user cannot query nvidia-smi; host metrics will still work, but fix GPU access if GPU metrics are expected.' >&2; }
else
  echo 'nvidia-smi not found; continuing with host-only metrics.'
fi
if [[ -e "$UNIT" ]]; then
  cp -a -- "$UNIT" "$UNIT.bak.$(date +%Y%m%d-%H%M%S)"
fi
sed -e "s/__USER__/$RUN_USER/g" -e "s/__GROUP__/$RUN_GROUP/g" "$BASE/rtx-monitor.service" > "$UNIT"
chmod 0644 "$UNIT"
systemctl daemon-reload
systemctl enable rtx-monitor.service
systemctl restart rtx-monitor.service
printf '\nInstalled as %s.\n' "$RUN_USER"
PORT=$(/usr/bin/python3 -B -c 'from pathlib import Path; from monitor import config_load; print(config_load(Path("config.json"))["port"])')
if [[ "$INSTALL_BIND" == tailscale ]] && IP=$(tailscale ip -4 2>/dev/null | head -n1) && [[ -n "$IP" ]]; then
  printf 'Current: http://%s:%s/\nHistory: http://%s:%s/history\n' "$IP" "$PORT" "$IP" "$PORT"
elif [[ "$INSTALL_BIND" != tailscale ]]; then
  printf 'Current: http://%s:%s/\nHistory: http://%s:%s/history\n' "$INSTALL_BIND" "$PORT" "$INSTALL_BIND" "$PORT"
else
  echo 'The HTTP listener will wait for tailscale0. Sampling continues meanwhile.'
fi
printf '\nVerify: systemctl status rtx-monitor --no-pager\nLogs:   journalctl -u rtx-monitor -n 50 --no-pager\n'
