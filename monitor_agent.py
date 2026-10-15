#!/usr/bin/env python3
"""Linux 本機監控代理人；僅使用 Python 標準函式庫。"""

import argparse
import configparser
import datetime
import errno
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass


SCHEMA_VERSION = 1
AGENT_VERSION = "1.1.0"
RESOURCE_KEYS = ("cpu_pct", "mem_pct", "zombie_count", "load1", "disk_pct")
SCRIPT = str(Path(__file__).resolve())
FAILURES = {
    errno.ETIMEDOUT: "tcp_connection_timeout",
    errno.ECONNREFUSED: "tcp_connection_refused",
    errno.ENETUNREACH: "network_unreachable",
    errno.EHOSTUNREACH: "network_unreachable",
    errno.ENETDOWN: "network_unreachable",
    errno.ECONNRESET: "tcp_reset",
    errno.ECONNABORTED: "tcp_reset",
}


@dataclass(frozen=True)
class Endpoint:
    target: str
    host: str
    port: int
    fqdn: str = ""

    @property
    def destination(self):
        return "[{}]:{}".format(self.host, self.port) if ":" in self.host else "{}:{}".format(self.host, self.port)


@dataclass(frozen=True)
class Settings:
    interval: float
    startup_jitter: float
    dns_timeout: float
    tcp_timeout: float
    log_path: str
    hostname: str
    disk_path: str
    thresholds: dict
    endpoints: tuple
    heartbeat_every_cycles: int = 10


def number(section, key, default, low, high):
    value = float(section.get(key, default))
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("invalid numeric setting: " + key)
    return value


def load_settings(path):
    parser = configparser.ConfigParser(interpolation=None)
    with open(path, encoding="utf-8") as handle:
        parser.read_file(handle)
    allowed = {
        "agent": {"interval_seconds", "startup_jitter_seconds", "log_path", "hostname", "disk_path", "heartbeat_every_cycles"},
        "thresholds": {"cpu_pct", "mem_pct", "zombie_count", "load1", "disk_pct"},
        "network": {"dns_timeout_seconds", "tcp_timeout_seconds"},
        "internal": {"dest", "fqdn", "port"},
        "external": {"dest", "fqdn", "port"},
    }
    if parser.defaults() or set(parser.sections()) != set(allowed):
        raise ValueError("invalid configuration sections")
    for name, keys in allowed.items():
        if set(parser[name]) - keys:
            raise ValueError("unknown configuration option")
    agent, net = parser["agent"], parser["network"]
    heartbeat_every_cycles = agent.getint("heartbeat_every_cycles", 10)
    if not 0 <= heartbeat_every_cycles <= 1000000:
        raise ValueError("invalid heartbeat_every_cycles")
    log_path = agent.get("log_path", "/var/log/monitor-agent/agent.jsonl").strip()
    disk_path = agent.get("disk_path", "/").strip()
    if not os.path.isabs(log_path) or not os.path.isabs(disk_path):
        raise ValueError("paths must be absolute")
    host = agent.get("hostname", "").strip() or socket.gethostname()
    if len(host) > 253 or any(ord(char) < 32 for char in host):
        raise ValueError("invalid hostname")
    endpoints = []
    for target in ("internal", "external"):
        section = parser[target]
        port = section.getint("port", 443)
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        dest, fqdn = section.get("dest", "").strip(), section.get("fqdn", "").strip()
        if not dest and not fqdn:
            raise ValueError("target requires dest or fqdn")
        if dest:
            ipaddress.ip_address(dest)
            endpoints.append(Endpoint(target, dest, port))
        if fqdn:
            ascii_host = fqdn.encode("idna").decode("ascii").rstrip(".")
            labels = ascii_host.split(".")
            if len(ascii_host) > 253 or any(
                not label or len(label) > 63 or not label[0].isalnum() or not label[-1].isalnum()
                or any(not (char.isalnum() or char == "-") for char in label)
                for label in labels
            ):
                raise ValueError("invalid fqdn")
            endpoints.append(Endpoint(target, ascii_host, port, ascii_host))
    thresholds = {
        key: number(parser["thresholds"], key, default, 0, upper)
        for key, default, upper in (
            ("cpu_pct", 90, 100), ("mem_pct", 90, 100), ("zombie_count", 1, 1000000),
            ("load1", 8, 1000000), ("disk_pct", 90, 100),
        )
    }
    return Settings(
        number(agent, "interval_seconds", 30, 1, 86400),
        number(agent, "startup_jitter_seconds", 5, 0, 3600),
        number(net, "dns_timeout_seconds", 2, 0.05, 5),
        number(net, "tcp_timeout_seconds", 3, 0.05, 5),
        log_path, host, disk_path, thresholds, tuple(endpoints), heartbeat_every_cycles,
    )


def failure_type(exc):
    if isinstance(exc, socket.gaierror):
        return "dns_resolution_error"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "tcp_connection_timeout"
    return FAILURES.get(getattr(exc, "errno", None), "internal_error")


def child_request(mode, request, timeout):
    # libc 的 DNS 查詢不能靠 socket timeout 或 ThreadPoolExecutor 可靠中止。
    completed = subprocess.run(
        [sys.executable, SCRIPT, "--worker", mode],
        input=json.dumps(request), capture_output=True, text=True, timeout=timeout,
        check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        raise RuntimeError("worker failed")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise ValueError("invalid worker response")
    return result


def resolve_worker(request):
    try:
        records = socket.getaddrinfo(request["host"], request["port"], 0, socket.SOCK_STREAM)
        addresses, seen = [], set()
        for family, socktype, protocol, _, address in records:
            key = (family, socktype, protocol, address)
            if family not in (socket.AF_INET, socket.AF_INET6) or key in seen:
                continue
            seen.add(key)
            addresses.append([family, socktype, protocol, address])
            if len(addresses) >= 16:
                break
        if not addresses:
            return {"failure_type": "dns_resolution_error", "addresses": []}
        return {"failure_type": "ok", "addresses": addresses}
    except Exception as exc:
        return {"failure_type": failure_type(exc), "addresses": []}


def resolve(endpoint, timeout):
    if not endpoint.fqdn:
        address = ipaddress.ip_address(endpoint.host)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        sockaddr = (endpoint.host, endpoint.port, 0, 0) if address.version == 6 else (endpoint.host, endpoint.port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, sockaddr)], "ok", "literal_ip"
    try:
        data = child_request("resolve", {"host": endpoint.host, "port": endpoint.port}, timeout)
        kind = data["failure_type"]
        if kind not in {"ok", "dns_resolution_error", "internal_error"}:
            raise ValueError("invalid resolver classification")
        return data["addresses"], kind, "dns_resolved" if kind == "ok" else "dns_failed"
    except subprocess.TimeoutExpired:
        return [], "dns_resolution_error", "dns_timeout"
    except Exception:
        return [], "internal_error", "resolver_worker_failed"


def tcp_connect(addresses, timeout):
    deadline = time.monotonic() + timeout
    last_failure, attempts = "internal_error", []
    for family, socktype, protocol, address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "tcp_connection_timeout", "tcp_budget_exhausted;attempts=" + ",".join(attempts)
        try:
            with socket.socket(family, socktype, protocol) as connection:
                connection.settimeout(remaining)
                connection.connect(tuple(address))
            return "ok", "tcp_connected"
        except Exception as exc:
            last_failure = failure_type(exc)
            attempts.append(last_failure)
    return last_failure, "tcp_failed;attempts=" + ",".join(attempts)


def base_event(hostname, event):
    return {"v": SCHEMA_VERSION,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
            "host": hostname, "event": event}


@dataclass
class Heartbeat:
    every_cycles: int
    started_at: float
    completed_cycles: int = 0

    def after_cycle(self, writer, hostname, cycle_ms, logged):
        self.completed_cycles += 1
        if not self.every_cycles or self.completed_cycles % self.every_cycles or not logged:
            return True
        event = base_event(hostname, "heartbeat")
        event.update(pid=os.getpid(), uptime_s=round(max(0.0, time.monotonic() - self.started_at), 3),
                     version=AGENT_VERSION, cycle_ms=round(max(0.0, cycle_ms), 3), level="ok")
        return writer.emit(event)


def probe(endpoint, settings):
    started = time.monotonic()
    try:
        addresses, kind, detail = resolve(endpoint, settings.dns_timeout)
        if kind == "ok":
            kind, detail = tcp_connect(addresses, settings.tcp_timeout)
    except Exception:
        kind, detail = "internal_error", "probe_failed"
    event = base_event(settings.hostname, "network")
    event.update(target=endpoint.target, dest=endpoint.destination, fqdn=endpoint.fqdn,
                 result="ok" if kind == "ok" else "fail", failure_type=kind,
                 latency_ms=round((time.monotonic() - started) * 1000, 3),
                 detail=detail[:512], level="ok" if kind == "ok" else "error")
    return event


def cpu_counts(proc_root):
    with open(proc_root / "stat", encoding="ascii") as handle:
        fields = handle.readline().split()
    if fields[0] != "cpu" or len(fields) < 5:
        raise ValueError("invalid cpu counters")
    counters = [int(value) for value in fields[1:9]]
    if any(value < 0 for value in counters):
        raise ValueError("negative cpu counter")
    return counters


def cpu_percentage(current, previous):
    if previous is None or len(current) != len(previous):
        return None
    deltas = [now - old for now, old in zip(current, previous)]
    if any(delta < 0 for delta in deltas) or sum(deltas) <= 0:
        return None
    idle = deltas[3] + (deltas[4] if len(deltas) > 4 else 0)
    return round(100 * (1 - idle / sum(deltas)), 2)


def memory_percentage(proc_root):
    values = {}
    with open(proc_root / "meminfo", encoding="ascii") as handle:
        for line in handle:
            key, raw = line.split(":", 1)
            values[key] = int(raw.split()[0])
    total = values["MemTotal"]
    if total <= 0:
        raise ValueError("invalid memory total")
    available = values.get("MemAvailable")
    if available is None:
        available = sum(values.get(key, 0) for key in ("MemFree", "Buffers", "Cached", "SReclaimable")) - values.get("Shmem", 0)
    return round(100 * (1 - min(total, max(0, available)) / total), 2)


def zombie_count(proc_root):
    deadline, count = time.monotonic() + 0.2, 0
    with os.scandir(proc_root) as entries:
        for entry in entries:
            if time.monotonic() > deadline:
                return None
            if not entry.name.isdigit():
                continue
            try:
                with open(Path(entry.path) / "stat", encoding="utf-8", errors="replace") as handle:
                    row = handle.read(8192)
                closing = row.rfind(")")
                if closing < 0:
                    raise ValueError("invalid process stat")
                if row[closing + 1:].split()[0] == "Z":
                    count += 1
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (PermissionError, OSError, ValueError, IndexError):
                # 無權讀取不是零隻 zombie，避免沙箱或 hidepid 造成假陰性。
                return None
    return count


def resource_worker(request):
    root = Path(request["proc_root"])
    result = {key: None for key in RESOURCE_KEYS}
    counts = None
    try:
        counts = cpu_counts(root)
        result["cpu_pct"] = cpu_percentage(counts, request.get("previous_cpu"))
    except Exception:
        pass
    try:
        result["mem_pct"] = memory_percentage(root)
    except Exception:
        pass
    try:
        result["zombie_count"] = zombie_count(root)
    except Exception:
        pass
    try:
        with open(root / "loadavg", encoding="ascii") as handle:
            result["load1"] = float(handle.read(100).split()[0])
    except Exception:
        pass
    try:
        info = os.statvfs(request["disk_path"])
        used = info.f_blocks - info.f_bfree
        capacity = used + info.f_bavail
        if capacity > 0:
            result["disk_pct"] = round(100 * used / capacity, 2)
    except Exception:
        pass
    for key, value in result.items():
        if value is not None and (not math.isfinite(value) or value < 0):
            result[key] = None
    return {"metrics": result, "cpu_counts": counts}


def sample_resources(settings, proc_root, previous_cpu):
    try:
        data = child_request("resource", {"proc_root": str(proc_root), "disk_path": settings.disk_path,
                                          "previous_cpu": previous_cpu}, 1.0)
        metrics, counts = data["metrics"], data["cpu_counts"]
        if set(metrics) != set(RESOURCE_KEYS):
            raise ValueError("invalid resource result")
    except Exception:
        metrics, counts = {key: None for key in RESOURCE_KEYS}, None
    level = "ok"
    if metrics["mem_pct"] is None or counts is None:
        level = "error"
    elif any(metrics[key] is None for key in RESOURCE_KEYS):
        level = "warn"
    if level != "error" and any(value is not None and value >= settings.thresholds[key] for key, value in metrics.items()):
        level = "warn"
    event = base_event(settings.hostname, "resource")
    event.update(metrics)
    event["level"] = level
    return event, counts


class JsonlWriter:
    def __init__(self, path):
        self.path = path

    def emit(self, event):
        try:
            payload = (json.dumps(event, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(self.path, flags, 0o640)
            with os.fdopen(descriptor, "ab", buffering=0) as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("log must be a regular file")
                if handle.write(payload) != len(payload):
                    raise OSError("partial log write")
            return True
        except Exception as exc:
            print("monitor-agent: log_write_failed type=" + type(exc).__name__, file=sys.stderr, flush=True)
            return False


def run_cycle(settings, writer, proc_root, previous_cpu, stop):
    event, previous_cpu = sample_resources(settings, proc_root, previous_cpu)
    success = writer.emit(event)
    for endpoint in settings.endpoints:
        if stop.is_set():
            break
        success = writer.emit(probe(endpoint, settings)) and success
    return previous_cpu, success


def main(argv=None):
    parser = argparse.ArgumentParser(description="Linux 本機資源與 TCP 監控")
    parser.add_argument("--config", default="/etc/monitor-agent/monitor-agent.conf")
    parser.add_argument("--once", action="store_true", help="執行一輪；CPU 初次取樣為 null")
    parser.add_argument("--check-config", action="store_true", help="僅驗證設定，不探測網路或寫入日誌")
    parser.add_argument("--proc-root", default="/proc", help="測試用 procfs 路徑；正式環境維持 /proc")
    parser.add_argument("--worker", choices=("resource", "resolve"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        try:
            request = json.load(sys.stdin)
            result = resource_worker(request) if args.worker == "resource" else resolve_worker(request)
            print(json.dumps(result, allow_nan=False))
            return 0
        except Exception:
            return 1
    try:
        settings = load_settings(args.config)
    except Exception as exc:
        print("monitor-agent: configuration_invalid type=" + type(exc).__name__, file=sys.stderr)
        return 2
    if args.check_config:
        print("configuration: ok")
        return 0
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    writer, previous_cpu = JsonlWriter(settings.log_path), None
    heartbeat = Heartbeat(settings.heartbeat_every_cycles, time.monotonic())
    if not args.once and stop.wait(random.uniform(0, settings.startup_jitter)):
        return 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            previous_cpu, logged = run_cycle(settings, writer, args.proc_root, previous_cpu, stop)
            if not stop.is_set():
                # 心跳只描述剛完成的一輪，不以健康心跳掩蓋該輪日誌寫入失敗。
                cycle_ms = (time.monotonic() - started) * 1000
                logged = heartbeat.after_cycle(writer, settings.hostname, cycle_ms, logged) and logged
        except Exception as exc:
            print("monitor-agent: cycle_failed type=" + type(exc).__name__, file=sys.stderr, flush=True)
            logged = False
        if args.once:
            return 0 if logged else 1
        stop.wait(max(0.1, settings.interval - (time.monotonic() - started)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
