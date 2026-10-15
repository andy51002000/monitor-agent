#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != Linux || "$EUID" -ne 0 ]]; then
  printf '%s\n' 'Run as root on Linux.' >&2
  exit 1
fi
purge=false
if [[ "${1:-}" == --purge-data && "$#" -eq 1 ]]; then
  purge=true
elif [[ "$#" -ne 0 ]]; then
  printf '%s\n' 'Usage: bash uninstall.sh [--purge-data]' >&2
  exit 2
fi
if command -v trash-put >/dev/null 2>&1; then
  trash=(trash-put)
elif command -v gio >/dev/null 2>&1; then
  trash=(gio trash)
else
  printf '%s\n' 'Install trash-cli or gio first; files are never permanently deleted.' >&2
  exit 1
fi

if [[ -e /etc/systemd/system/monitor-agent.service || -L /etc/systemd/system/monitor-agent.service ]]; then
  systemctl disable --now monitor-agent.service
fi
for path in /etc/systemd/system/monitor-agent.service /etc/logrotate.d/monitor-agent /opt/monitor-agent; do
  if [[ -e "$path" || -L "$path" ]]; then
    "${trash[@]}" "$path"
  fi
done
if "$purge"; then
  for path in /etc/monitor-agent /var/log/monitor-agent; do
    if [[ -e "$path" || -L "$path" ]]; then
      "${trash[@]}" "$path"
    fi
  done
fi
systemctl daemon-reload
systemctl reset-failed monitor-agent.service 2>/dev/null || true
printf '%s\n' 'Service removed. Files were moved to the root user trash.'
printf '%s\n' 'The non-login service account is retained to preserve ownership and avoid UID reuse.'
if ! "$purge"; then
  printf '%s\n' 'Configuration and logs were retained. Use --purge-data to move them to trash.'
fi
