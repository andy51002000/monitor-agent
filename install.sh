#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != Linux || "$EUID" -ne 0 ]]; then
  printf '%s\n' 'Run as root on a Linux host with systemd.' >&2
  exit 1
fi
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for executable in python3 systemctl getent useradd install; do
  if ! command -v "$executable" >/dev/null 2>&1; then
    printf 'Missing prerequisite: %s\n' "$executable" >&2
    exit 1
  fi
done
if ! command -v logrotate >/dev/null 2>&1; then
  printf '%s\n' 'WARNING: logrotate not found; agent.jsonl will grow without rotation.' >&2
fi
if [[ ! -x /usr/bin/python3 ]]; then
  printf '%s\n' 'The service requires /usr/bin/python3.' >&2
  exit 1
fi
/usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'
/usr/bin/python3 "$SOURCE_DIR/monitor_agent.py" --config "$SOURCE_DIR/config/monitor-agent.conf" --check-config

for path in /opt/monitor-agent /etc/monitor-agent /var/log/monitor-agent \
  /opt/monitor-agent/monitor_agent.py /etc/monitor-agent/monitor-agent.conf \
  /var/log/monitor-agent/agent.jsonl /etc/systemd/system/monitor-agent.service \
  /etc/logrotate.d/monitor-agent; do
  if [[ -L "$path" ]]; then
    printf 'Refusing symlink: %s\n' "$path" >&2
    exit 1
  fi
done
if getent passwd monitor-agent >/dev/null; then
  account="$(getent passwd monitor-agent)"
  IFS=: read -r _ _ uid _ _ home shell <<< "$account"
  if [[ "$uid" == 0 || "$home" != /nonexistent || "$shell" != */nologin ]]; then
    printf '%s\n' 'Existing monitor-agent account does not match the dedicated service account.' >&2
    exit 1
  fi
else
  useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin monitor-agent
fi
install -d -o root -g root -m 0755 /opt/monitor-agent
install -d -o root -g monitor-agent -m 0750 /etc/monitor-agent
install -d -o monitor-agent -g monitor-agent -m 0750 /var/log/monitor-agent
if [[ ! -e /etc/monitor-agent/monitor-agent.conf ]]; then
  install -o root -g monitor-agent -m 0640 "$SOURCE_DIR/config/monitor-agent.conf" /etc/monitor-agent/monitor-agent.conf
fi
/usr/bin/python3 "$SOURCE_DIR/monitor_agent.py" --config /etc/monitor-agent/monitor-agent.conf --check-config
install -o root -g root -m 0644 "$SOURCE_DIR/monitor_agent.py" /opt/monitor-agent/monitor_agent.py
install -o root -g root -m 0644 "$SOURCE_DIR/systemd/monitor-agent.service" /etc/systemd/system/monitor-agent.service
install -o root -g root -m 0644 "$SOURCE_DIR/logrotate/monitor-agent" /etc/logrotate.d/monitor-agent
systemctl daemon-reload
systemctl enable monitor-agent.service
systemctl restart monitor-agent.service
systemctl is-active --quiet monitor-agent.service
# is-active 只證明行程存在；再驗證日誌真的有新寫入（涵蓋 startup jitter 與第一輪）。
log_file=/var/log/monitor-agent/agent.jsonl
before_size=0
if [[ -f "$log_file" ]]; then
  before_size="$(stat -c %s "$log_file")"
fi
verified=false
for _ in $(seq 1 30); do
  sleep 1
  if [[ -f "$log_file" && "$(stat -c %s "$log_file")" -gt "$before_size" ]]; then
    verified=true
    break
  fi
done
if "$verified"; then
  printf '%s\n' 'Verified: agent wrote at least one new log event.'
else
  printf '%s\n' 'WARNING: no new log event observed within 30s.' >&2
  printf '%s\n' 'Inspect: journalctl -u monitor-agent.service -n 30' >&2
fi
printf '%s\n' 'Installed and started monitor-agent; existing configuration was preserved.'
printf '%s\n' 'Verify metrics and logrotate scheduling before rolling out to other hosts.'
