import configparser
import contextlib
import datetime
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor_agent", ROOT / "monitor_agent.py")
agent = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent
spec.loader.exec_module(agent)
RESOURCE_FIELDS = {"v", "ts", "host", "event", "cpu_pct", "mem_pct", "zombie_count", "load1", "disk_pct", "level"}
NETWORK_FIELDS = {"v", "ts", "host", "event", "target", "dest", "fqdn", "result", "failure_type", "latency_ms", "detail", "level"}
HEARTBEAT_FIELDS = {"v", "ts", "host", "event", "pid", "uptime_s", "version", "cycle_ms", "level"}


def create_proc(root):
    root.mkdir()
    (root / "stat").write_text("cpu 100 0 50 800 50 0 0 0 20 5\n", encoding="ascii")
    (root / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n", encoding="ascii")
    (root / "loadavg").write_text("0.50 0.40 0.30 1/100 500\n", encoding="ascii")
    for pid, name, state in ((101, "sleep", "S"), (202, "name ) with ( brackets", "Z")):
        directory = root / str(pid)
        directory.mkdir()
        (directory / "stat").write_text("{} ({}) {} 1 2 3\n".format(pid, name, state), encoding="ascii")


def create_config(path, log_path, port):
    parser = configparser.ConfigParser()
    parser.read(ROOT / "config" / "monitor-agent.conf")
    parser["agent"]["log_path"] = str(log_path)
    parser["agent"]["disk_path"] = str(path.parent)
    parser["agent"]["hostname"] = "test-host"
    parser["agent"]["startup_jitter_seconds"] = "0"
    parser["agent"]["interval_seconds"] = "1"
    for target in ("internal", "external"):
        parser[target]["dest"] = "127.0.0.1"
        parser[target]["fqdn"] = "localhost"
        parser[target]["port"] = str(port)
    with open(path, "w", encoding="ascii") as handle:
        parser.write(handle)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="monitor-agent-test-")
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        create_proc(self.proc)
        self.config = self.root / "test.conf"
        self.log = self.root / "agent.jsonl"
        create_config(self.config, self.log, 443)
        self.settings = agent.load_settings(self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def test_heartbeat_setting_defaults_and_validation(self):
        self.assertEqual(self.settings.heartbeat_every_cycles, 10)
        original = self.config.read_text()
        self.config.write_text(original.replace("heartbeat_every_cycles = 10\n", ""))
        self.assertEqual(agent.load_settings(self.config).heartbeat_every_cycles, 10)
        for value in ("0", "1", "7"):
            self.config.write_text(original.replace("heartbeat_every_cycles = 10", "heartbeat_every_cycles = " + value))
            self.assertEqual(agent.load_settings(self.config).heartbeat_every_cycles, int(value))
        for value in ("-1", "1.5", "nan", "true", "1000001"):
            with self.subTest(value=value):
                self.config.write_text(original.replace("heartbeat_every_cycles = 10", "heartbeat_every_cycles = " + value))
                with self.assertRaises(ValueError):
                    agent.load_settings(self.config)

    def test_heartbeat_period_schema_and_monotonic_duration(self):
        heartbeat = agent.Heartbeat(3, 100.0)
        writer = mock.Mock(emit=mock.Mock(return_value=True))
        with mock.patch.object(agent.time, "monotonic", return_value=107.25):
            for cycle in range(1, 8):
                self.assertTrue(heartbeat.after_cycle(writer, "test-host", cycle * 12.5, True))
                self.assertEqual(writer.emit.call_count, cycle // 3)
        events = [call.args[0] for call in writer.emit.call_args_list]
        self.assertEqual([event["cycle_ms"] for event in events], [37.5, 75.0])
        for event in events:
            self.assertEqual(set(event), HEARTBEAT_FIELDS)
            self.assertIs(type(event["v"]), int)
            self.assertEqual(event["v"], 1)
            self.assertEqual(event["pid"], os.getpid())
            self.assertEqual(event["uptime_s"], 7.25)
            self.assertEqual(event["version"], agent.AGENT_VERSION)
            self.assertEqual(event["level"], "ok")
            self.assertIsNotNone(datetime.datetime.fromisoformat(event["ts"]).tzinfo)

    def test_heartbeat_disabled_emits_nothing(self):
        heartbeat = agent.Heartbeat(0, 0)
        writer = mock.Mock()
        for _ in range(25):
            self.assertTrue(heartbeat.after_cycle(writer, "test-host", 10, True))
        writer.emit.assert_not_called()

    def test_heartbeat_failed_cycle_suppression_and_recovery(self):
        heartbeat = agent.Heartbeat(2, time.monotonic())
        writer = mock.Mock(emit=mock.Mock(return_value=True))
        for logged in (True, False, True):
            self.assertTrue(heartbeat.after_cycle(writer, "test-host", 10, logged))
        writer.emit.assert_not_called()
        self.assertTrue(heartbeat.after_cycle(writer, "test-host", 20, True))
        writer.emit.assert_called_once()
        writer.emit.return_value = False
        self.assertTrue(heartbeat.after_cycle(writer, "test-host", 30, True))
        self.assertFalse(heartbeat.after_cycle(writer, "test-host", 40, True))

    def test_heartbeat_uses_main_writer_and_survives_rotation(self):
        writer = agent.JsonlWriter(str(self.log))
        heartbeat = agent.Heartbeat(1, time.monotonic())
        self.assertTrue(heartbeat.after_cycle(writer, "test-host", 1, True))
        old_log = self.root / "agent.jsonl.1"
        self.log.rename(old_log)
        self.assertTrue(heartbeat.after_cycle(writer, "test-host", 2, True))
        self.assertEqual(json.loads(old_log.read_text())["cycle_ms"], 1)
        self.assertEqual(json.loads(self.log.read_text())["cycle_ms"], 2)
        with mock.patch.object(agent.os, "open", side_effect=OSError(errno.ENOSPC, "secret")), contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            self.assertFalse(heartbeat.after_cycle(writer, "test-host", 3, True))
        self.assertIn("log_write_failed", diagnostic.getvalue())
        self.assertNotIn("secret", diagnostic.getvalue())

    def test_resource_level_priority_and_configurable_thresholds(self):
        metrics = {"cpu_pct": 10.0, "mem_pct": 20.0, "zombie_count": 0, "load1": 0.5, "disk_pct": 30.0}
        counts = [1, 0, 1, 10, 0, 0, 0, 0]
        cases = [(metrics, counts, "ok"), ({**metrics, "cpu_pct": None}, counts, "warn"),
                 ({**metrics, "zombie_count": None}, counts, "warn"),
                 ({**metrics, "disk_pct": None}, counts, "warn"),
                 ({**metrics, "load1": None}, counts, "warn"),
                 ({**metrics, "mem_pct": None, "cpu_pct": 99}, counts, "error"),
                 (metrics, None, "error")]
        for values, cpu, expected in cases:
            with self.subTest(expected=expected, values=values), \
                    mock.patch.object(agent, "child_request", return_value={"metrics": values, "cpu_counts": cpu}):
                event, _ = agent.sample_resources(self.settings, self.proc, None)
                self.assertEqual(event["level"], expected)
        settings = replace(self.settings, thresholds={**self.settings.thresholds, "cpu_pct": 10})
        with mock.patch.object(agent, "child_request", return_value={"metrics": metrics, "cpu_counts": counts}):
            event, _ = agent.sample_resources(settings, self.proc, None)
            self.assertEqual(event["level"], "warn")

    def test_version_added_without_changing_existing_values(self):
        event, _ = agent.sample_resources(self.settings, self.proc, None)
        self.assertEqual(set(event) - {"v"}, RESOURCE_FIELDS - {"v"})
        self.assertNotIn("failure_type", event)
        self.assertIsNone(event["cpu_pct"])
        self.assertIs(type(event["zombie_count"]), int)
        for kind in ("resource", "network", "heartbeat"):
            self.assertIs(type(agent.base_event("test-host", kind)["v"]), int)
        with mock.patch.object(agent, "resolve", return_value=([], "dns_resolution_error", "dns_failed")):
            event = agent.probe(agent.Endpoint("external", "localhost", 443, "localhost"), self.settings)
        self.assertIsInstance(event["dest"], str)
        self.assertIsInstance(event["fqdn"], str)
        self.assertIsInstance(event["detail"], str)
        self.assertIsInstance(event["latency_ms"], (int, float))
        self.assertEqual((event["result"], event["failure_type"], event["level"]), ("fail", "dns_resolution_error", "error"))

    def test_main_heartbeat_failure_affects_once_exit_status(self):
        settings = replace(self.settings, heartbeat_every_cycles=1)
        for cycle_logged, heartbeat_logged, expected in ((True, False, 1), (False, True, 1), (True, True, 0)):
            with self.subTest(cycle_logged=cycle_logged, heartbeat_logged=heartbeat_logged), \
                    mock.patch.object(agent, "load_settings", return_value=settings), \
                    mock.patch.object(agent, "run_cycle", return_value=(None, cycle_logged)), \
                    mock.patch.object(agent, "JsonlWriter") as writer, \
                    mock.patch.object(agent.signal, "signal"):
                writer.return_value.emit.return_value = heartbeat_logged
                self.assertEqual(agent.main(["--once"]), expected)
                self.assertEqual(writer.return_value.emit.call_count, int(cycle_logged))

    def test_main_heartbeat_tracks_completed_cycle_not_sleep(self):
        settings = replace(self.settings, heartbeat_every_cycles=1)
        with mock.patch.object(agent, "load_settings", return_value=settings), \
                mock.patch.object(agent, "run_cycle", return_value=(None, True)), \
                mock.patch.object(agent, "JsonlWriter") as writer, \
                mock.patch.object(agent.signal, "signal"), \
                mock.patch.object(agent.time, "monotonic", side_effect=[10, 11, 11.25, 11.5]):
            writer.return_value.emit.return_value = True
            self.assertEqual(agent.main(["--once"]), 0)
        event = writer.return_value.emit.call_args.args[0]
        self.assertEqual(event["cycle_ms"], 250)
        self.assertEqual(event["uptime_s"], 1.5)

    def test_main_stopping_cycle_does_not_emit_heartbeat(self):
        stop = threading.Event()
        settings = replace(self.settings, heartbeat_every_cycles=1)

        def interrupt(*_args):
            stop.set()
            return None, True

        with mock.patch.object(agent, "load_settings", return_value=settings), \
                mock.patch.object(agent, "run_cycle", side_effect=interrupt), \
                mock.patch.object(agent, "JsonlWriter") as writer, \
                mock.patch.object(agent.signal, "signal"), \
                mock.patch.object(agent.threading, "Event", return_value=stop):
            self.assertEqual(agent.main(["--once"]), 0)
        writer.return_value.emit.assert_not_called()

    def test_once_cli_with_heartbeat_evidence(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            create_config(self.config, self.log, listener.getsockname()[1])
            self.config.write_text(self.config.read_text().replace("heartbeat_every_cycles = 10", "heartbeat_every_cycles = 1"))
            child = subprocess.Popen([sys.executable, str(ROOT / "monitor_agent.py"), "--config", str(self.config),
                                      "--proc-root", str(self.proc), "--once"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                _, errors = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, errors)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        events = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(events), 6)
        self.assertEqual([event["event"] for event in events], ["resource"] + ["network"] * 4 + ["heartbeat"])
        heartbeat = events[-1]
        self.assertEqual(set(heartbeat), HEARTBEAT_FIELDS)
        self.assertEqual(heartbeat["pid"], child.pid)
        self.assertGreater(heartbeat["cycle_ms"], 0)
        self.assertGreaterEqual(heartbeat["uptime_s"] * 1000 + 1, heartbeat["cycle_ms"])
        for event in events:
            self.assertIs(type(event["v"]), int)
            self.assertEqual(event["v"], 1)
        evidence_dir = os.environ.get("MONITOR_AGENT_TEST_EVIDENCE")
        if evidence_dir:
            evidence = Path(evidence_dir)
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "once-heartbeat.jsonl").write_text(self.log.read_text())

    def test_cpu_delta_ignores_guest_double_count(self):
        self.assertEqual(agent.cpu_counts(self.proc), [100, 0, 50, 800, 50, 0, 0, 0])
        self.assertEqual(agent.cpu_percentage([120, 0, 60, 850, 70, 0, 0, 0], [100, 0, 50, 800, 50, 0, 0, 0]), 30.0)
        self.assertIsNone(agent.cpu_percentage([1, 2, 3, 4], None))
        self.assertIsNone(agent.cpu_percentage([1, 2, 3, 4], [1, 2, 3, 4]))
        self.assertIsNone(agent.cpu_percentage([1, 2, 3, 4], [5, 2, 3, 4]))

    def test_memory_available_and_fallback(self):
        self.assertEqual(agent.memory_percentage(self.proc), 60.0)
        (self.proc / "meminfo").write_text("MemTotal: 1000 kB\nMemFree: 100 kB\nCached: 200 kB\nBuffers: 50 kB\nSReclaimable: 40 kB\nShmem: 10 kB\n")
        self.assertEqual(agent.memory_percentage(self.proc), 62.0)

    def test_zombie_name_with_brackets(self):
        self.assertEqual(agent.zombie_count(self.proc), 1)

    def test_zombie_permissions_report_unknown(self):
        with mock.patch("builtins.open", side_effect=PermissionError()):
            self.assertIsNone(agent.zombie_count(self.proc))

    def test_resource_schema_and_threshold(self):
        event, counts = agent.sample_resources(self.settings, self.proc, [90, 0, 40, 780, 40, 0, 0, 0])
        self.assertEqual(set(event), RESOURCE_FIELDS)
        self.assertEqual(event["mem_pct"], 60.0)
        self.assertEqual(event["zombie_count"], 1)
        self.assertEqual(event["level"], "warn")
        self.assertEqual(counts, [100, 0, 50, 800, 50, 0, 0, 0])
        self.assertIsNotNone(datetime.datetime.fromisoformat(event["ts"]).tzinfo)

    def test_resource_worker_timeout_is_unknown_not_zero(self):
        with mock.patch.object(agent, "child_request", side_effect=subprocess.TimeoutExpired("worker", 1)):
            event, counts = agent.sample_resources(self.settings, self.proc, None)
        self.assertEqual(event["level"], "error")
        self.assertTrue(all(event[key] is None for key in agent.RESOURCE_KEYS))
        self.assertIsNone(counts)

    def test_missing_proc_does_not_crash(self):
        event, _ = agent.sample_resources(self.settings, self.root / "missing", None)
        self.assertEqual(event["level"], "error")
        self.assertIsNone(event["cpu_pct"])
        self.assertIsNone(event["mem_pct"])

    def test_errno_classification(self):
        cases = [(socket.gaierror(socket.EAI_NONAME, "sensitive detail"), "dns_resolution_error"),
                 (socket.timeout("sensitive detail"), "tcp_connection_timeout"),
                 (OSError(errno.ETIMEDOUT, "x"), "tcp_connection_timeout"),
                 (OSError(errno.ECONNREFUSED, "x"), "tcp_connection_refused"),
                 (OSError(errno.ENETUNREACH, "x"), "network_unreachable"),
                 (OSError(errno.EHOSTUNREACH, "x"), "network_unreachable"),
                 (OSError(errno.ECONNRESET, "x"), "tcp_reset"),
                 (RuntimeError("sensitive detail"), "internal_error")]
        for exception, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(agent.failure_type(exception), expected)

    def test_dns_failure_and_dns_timeout(self):
        endpoint = agent.Endpoint("internal", "test.invalid", 443, "test.invalid")
        with mock.patch.object(agent, "child_request", return_value={"failure_type": "dns_resolution_error", "addresses": []}):
            event = agent.probe(endpoint, self.settings)
            self.assertEqual(event["failure_type"], "dns_resolution_error")
            self.assertEqual(event["detail"], "dns_failed")
        with mock.patch.object(agent, "child_request", side_effect=subprocess.TimeoutExpired("dns", 0.1)):
            event = agent.probe(endpoint, self.settings)
            self.assertEqual(event["failure_type"], "dns_resolution_error")
            self.assertEqual(event["detail"], "dns_timeout")
        with mock.patch.object(agent.socket, "getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, "x")):
            self.assertEqual(agent.resolve_worker({"host": "test.invalid", "port": 443})["failure_type"], "dns_resolution_error")

    def test_tcp_failures_network_schema_and_no_exception_details(self):
        for exception, kind in ((TimeoutError("secret"), "tcp_connection_timeout"),
                                (ConnectionRefusedError(errno.ECONNREFUSED, "secret"), "tcp_connection_refused"),
                                (OSError(errno.ENETUNREACH, "secret"), "network_unreachable"),
                                (ConnectionResetError(errno.ECONNRESET, "secret"), "tcp_reset"),
                                (RuntimeError("secret"), "internal_error")):
            with self.subTest(kind=kind), mock.patch.object(agent.socket, "socket") as factory:
                factory.return_value.__enter__.return_value.connect.side_effect = exception
                event = agent.probe(agent.Endpoint("external", "127.0.0.1", 443), self.settings)
                self.assertEqual(set(event), NETWORK_FIELDS)
                self.assertEqual(event["failure_type"], kind)
                self.assertEqual(event["result"], "fail")
                self.assertEqual(event["level"], "error")
                self.assertNotIn("secret", json.dumps(event))

    def test_ipv6_numeric_address_does_not_resolve(self):
        with mock.patch.object(agent, "child_request", side_effect=AssertionError("DNS called")):
            records, kind, _ = agent.resolve(agent.Endpoint("internal", "::1", 443), 1)
        self.assertEqual(kind, "ok")
        self.assertEqual(records[0][3], ("::1", 443, 0, 0))

    def test_multi_address_budget_not_per_address_timeout(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 0, ("127.0.0.1", 443))] * 2
        with mock.patch.object(agent.time, "monotonic", side_effect=[0, 0.1, 0.3]), mock.patch.object(agent.socket, "socket") as factory:
            connection = factory.return_value.__enter__.return_value
            connection.connect.side_effect = OSError(errno.ECONNREFUSED, "x")
            kind, _ = agent.tcp_connect(records, 1)
        self.assertEqual(kind, "tcp_connection_refused")
        self.assertEqual(connection.settimeout.call_args_list, [mock.call(0.9), mock.call(0.7)])

    def test_actual_child_timeout_kills_and_reaps(self):
        sleeper = self.root / "sleep_worker.py"
        pid_file = self.root / "pid"
        sleeper.write_text("import os, time\nfrom pathlib import Path\nPath({!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n".format(str(pid_file)))
        started = time.monotonic()
        with mock.patch.object(agent, "SCRIPT", str(sleeper)):
            with self.assertRaises(subprocess.TimeoutExpired):
                agent.child_request("resolve", {}, 0.3)
        self.assertLess(time.monotonic() - started, 2)
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_configuration_rejects_unsafe_or_invalid_values(self):
        original = self.config.read_text()
        for old, new in (("interval_seconds = 1", "interval_seconds = nan"),
                         ("interval_seconds = 1", "interval_seconds = 0"),
                         ("port = 443", "port = 70000"),
                         ("dest = 127.0.0.1", "dest = https://token@host"),
                         ("fqdn = localhost", "fqdn = user:secret@example.com"),
                         ("startup_jitter_seconds = 0", "unknown = 0")):
            with self.subTest(new=new):
                self.config.write_text(original.replace(old, new))
                with self.assertRaises(ValueError):
                    agent.load_settings(self.config)

    def test_jsonl_append_rotation_and_symlink(self):
        writer = agent.JsonlWriter(str(self.log))
        self.assertTrue(writer.emit({"event": "first"}))
        old_log = self.root / "agent.jsonl.1"
        self.log.rename(old_log)
        self.assertTrue(writer.emit({"event": "second"}))
        self.assertEqual(json.loads(self.log.read_text()), {"event": "second"})
        self.assertEqual(json.loads(old_log.read_text()), {"event": "first"})
        link = self.root / "link.jsonl"
        link.symlink_to(self.log)
        with contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            self.assertFalse(agent.JsonlWriter(str(link)).emit({"event": "unsafe"}))
        self.assertIn("log_write_failed", diagnostic.getvalue())

    def test_full_disk_write_is_reported_without_crash(self):
        with mock.patch.object(agent.os, "open", side_effect=OSError(errno.ENOSPC, "secret")), contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            self.assertFalse(agent.JsonlWriter(str(self.log)).emit({"event": "test"}))
        self.assertIn("log_write_failed", diagnostic.getvalue())
        self.assertNotIn("secret", diagnostic.getvalue())

    def test_real_loopback_success_and_refused(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            port = listener.getsockname()[1]
            success = agent.probe(agent.Endpoint("internal", "127.0.0.1", port), self.settings)
            self.assertEqual(success["failure_type"], "ok")
            accepted, _ = listener.accept()
            accepted.close()
        with socket.socket() as not_listening:
            not_listening.bind(("127.0.0.1", 0))
            closed_port = not_listening.getsockname()[1]
        refused = agent.probe(agent.Endpoint("external", "127.0.0.1", closed_port), self.settings)
        self.assertEqual(refused["failure_type"], "tcp_connection_refused")

    def test_once_cli_real_process_and_log_contract(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            create_config(self.config, self.log, listener.getsockname()[1])
            completed = subprocess.run([sys.executable, str(ROOT / "monitor_agent.py"), "--config", str(self.config),
                                        "--proc-root", str(self.proc), "--once"], capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        events = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(events), 5)
        self.assertEqual(set(events[0]), RESOURCE_FIELDS)
        self.assertIsNone(events[0]["cpu_pct"])
        for event in events[1:]:
            self.assertEqual(set(event), NETWORK_FIELDS)
            self.assertEqual(event["result"], "ok")
        self.assertEqual({event["target"] for event in events[1:]}, {"internal", "external"})
        evidence_dir = os.environ.get("MONITOR_AGENT_TEST_EVIDENCE")
        if evidence_dir:
            evidence = Path(evidence_dir)
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "once-loopback.jsonl").write_text(self.log.read_text())

    def test_shutdown_interrupts_interval_wait(self):
        settings = replace(self.settings, interval=60, startup_jitter=0)
        stop = threading.Event()
        with mock.patch.object(agent, "sample_resources", return_value=({}, None)), mock.patch.object(agent, "probe") as network:
            stop.set()
            _, result = agent.run_cycle(settings, mock.Mock(emit=mock.Mock(return_value=True)), self.proc, None, stop)
        self.assertTrue(result)
        network.assert_not_called()

    def test_sigterm_stops_real_daemon(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(32)
            create_config(self.config, self.log, listener.getsockname()[1])
            child = subprocess.Popen([sys.executable, str(ROOT / "monitor_agent.py"), "--config", str(self.config), "--proc-root", str(self.proc)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not self.log.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(self.log.exists())
                child.send_signal(signal.SIGTERM)
                _, errors = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, errors)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()


if __name__ == "__main__":
    unittest.main()
