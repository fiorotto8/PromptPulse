#!/usr/bin/env bash
# Install the monitor without adding packages or changing NVIDIA/Tailscale configuration.
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
BASE=/opt/syslume
UNIT=/etc/systemd/system/syslume.service
LEGACY_BASE=/opt/promptpulse
LEGACY_UNIT=/etc/systemd/system/promptpulse.service
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
[[ ! -f "$LEGACY_BASE/config.json" ]] || CONFIG_SOURCE="$LEGACY_BASE/config.json"
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
systemctl show-environment >/dev/null 2>&1 || { echo 'A running systemd system manager is required for this installer.' >&2; exit 1; }
if [[ -e "$UNIT" ]]; then
  grep -q '^# syslume managed unit' "$UNIT" || { echo "An unrelated $UNIT already exists. Refusing to overwrite it." >&2; exit 1; }
fi
if [[ -e "$LEGACY_UNIT" ]]; then
  grep -q '^# promptpulse managed unit' "$LEGACY_UNIT" || { echo "An unrelated $LEGACY_UNIT already exists. Refusing automatic migration." >&2; exit 1; }
fi
/usr/bin/python3 -B -m unittest discover -s tests -v
# Migrate an existing PromptPulse installation only when SysLume has no database yet.
MIGRATE_LEGACY=0
if [[ ! -e "$BASE/data/monitor.db" && -e "$LEGACY_BASE/data/monitor.db" ]]; then
  MIGRATE_LEGACY=1
  [[ ! -e "$LEGACY_UNIT" ]] || systemctl stop promptpulse.service
fi

# Copy only project files, never checkout metadata, caches, secrets or environments.
ASSETS=(monitor.py README.md CONTRIBUTING.md LICENSE SETUP_PROMPT.md
        config.example.json install.sh syslume.service
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
[[ -f "$BASE/config.json" ]] || install -m 0644 "$CONFIG_SOURCE" "$BASE/config.json"
install -d -m 0700 -o "$RUN_USER" -g "$RUN_GROUP" "$BASE/data"
if [[ "$MIGRATE_LEGACY" -eq 1 ]]; then
  for dbfile in monitor.db monitor.db-wal monitor.db-shm; do
    [[ ! -f "$LEGACY_BASE/data/$dbfile" ]] || install -m 0600 -o "$RUN_USER" -g "$RUN_GROUP" "$LEGACY_BASE/data/$dbfile" "$BASE/data/$dbfile"
  done
fi
chown -R root:root "$BASE/static" "$BASE/tests"
chown root:root "$BASE/monitor.py" "$BASE/config.example.json" "$BASE/syslume.service" "$BASE/README.md"
chmod 0755 "$BASE/monitor.py"
cd "$BASE"
runuser -u "$RUN_USER" -- /usr/bin/python3 -B - <<'PYTOOLS'
from pathlib import Path
import shutil
import threading
from monitor import config_load, NvidiaReader, TegraReader
c = config_load(Path('config.json'))
readers = [('nvidia-smi', NvidiaReader(c)), ('tegrastats', TegraReader(c, threading.Event()))]
found = [name for name, reader in readers if reader.path and shutil.which(reader.path)
         and (name != 'tegrastats' or c['gpu_index'] == 0)]
print('Optional GPU tools: ' + ', '.join(found) + '; verify actual readings in /api/current.'
      if found else 'No supported GPU tool found; continuing with host-only metrics.')
PYTOOLS
if [[ -e "$UNIT" ]]; then
  cp -a -- "$UNIT" "$UNIT.bak.$(date +%Y%m%d-%H%M%S)"
fi
sed -e "s/__USER__/$RUN_USER/g" -e "s/__GROUP__/$RUN_GROUP/g" "$BASE/syslume.service" > "$UNIT"
chmod 0644 "$UNIT"
systemctl daemon-reload
systemctl enable syslume.service
systemctl restart syslume.service
if [[ -e "$LEGACY_UNIT" ]]; then
  systemctl disable --now promptpulse.service >/dev/null 2>&1 || true
fi
printf '\nInstalled as %s.\n' "$RUN_USER"
if [[ "$MIGRATE_LEGACY" -eq 1 ]]; then
  echo 'Migrated the existing PromptPulse configuration/history to SysLume; the legacy installation was left in place but disabled.'
fi
PORT=$(/usr/bin/python3 -B -c 'from pathlib import Path; from monitor import config_load; print(config_load(Path("config.json"))["port"])')
if [[ "$INSTALL_BIND" == tailscale ]] && IP=$(tailscale ip -4 2>/dev/null | head -n1) && [[ -n "$IP" ]]; then
  printf 'Current: http://%s:%s/\nHistory: http://%s:%s/history\n' "$IP" "$PORT" "$IP" "$PORT"
elif [[ "$INSTALL_BIND" != tailscale ]]; then
  printf 'Current: http://%s:%s/\nHistory: http://%s:%s/history\n' "$INSTALL_BIND" "$PORT" "$INSTALL_BIND" "$PORT"
else
  echo 'The HTTP listener will wait for tailscale0. Sampling continues meanwhile.'
fi
printf '\nVerify: systemctl status syslume --no-pager\nLogs:   journalctl -u syslume -n 50 --no-pager\n'
