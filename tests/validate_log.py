#!/usr/bin/env python3
"""驗證 monitor-agent 日誌是否符合契約。用法: python3 validate_log.py <logfile>"""
import json, sys, re

RESOURCE  = {"v","ts","host","event","cpu_pct","mem_pct","zombie_count","load1","disk_pct","level"}
NETWORK   = {"v","ts","host","event","target","dest","fqdn","result","failure_type","latency_ms","detail","level"}
HEARTBEAT = {"v","ts","host","event","pid","uptime_s","version","cycle_ms","level"}
SCHEMA_V  = 1
FAILURES  = {"ok","dns_resolution_error","tcp_connection_timeout","tcp_connection_refused",
             "network_unreachable","tcp_reset","internal_error"}
LEVELS    = {"ok","warn","error"}
TS_RE    = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def check(o, n):
    errs = []
    ev = o.get("event")
    need = {"resource": RESOURCE, "network": NETWORK, "heartbeat": HEARTBEAT}.get(ev)
    if need is None:
        return [f"line {n}: event 值非法或缺少 event: {ev!r}"]
    if set(o) != need:
        miss, extra = need - set(o), set(o) - need
        if miss:  errs.append(f"line {n}: 缺欄位 {sorted(miss)}")
        if extra: errs.append(f"line {n}: 多餘欄位 {sorted(extra)}")
    if not TS_RE.match(str(o.get("ts"))): errs.append(f"line {n}: ts 格式或時區不符 ISO-8601")
    if o.get("level") not in LEVELS:      errs.append(f"line {n}: level 非法 {o.get('level')!r}")
    v = o.get("v")
    if not isinstance(v, int) or isinstance(v, bool) or v != SCHEMA_V:
        errs.append(f"line {n}: v 需為整數 {SCHEMA_V}，實得 {v!r}")
    if ev == "resource":
        for k in ("cpu_pct","mem_pct","disk_pct"):
            val = o.get(k)
            if val is not None and (not num(val) or not 0 <= val <= 100):
                errs.append(f"line {n}: {k} 需為 0-100 number 或 null")
        z = o.get("zombie_count")
        if z is not None and (not isinstance(z, int) or isinstance(z, bool) or z < 0):
            errs.append(f"line {n}: zombie_count 需為 >=0 integer 或 null")
        l1 = o.get("load1")
        if l1 is not None and (not num(l1) or l1 < 0):
            errs.append(f"line {n}: load1 需為 >=0 number 或 null")
        if o.get("level") == "error" and all(o.get(k) is not None for k in
                ("mem_pct","zombie_count","load1","disk_pct")):
            errs.append(f"line {n}: level=error 但關鍵指標皆有值（判定不一致）")
        if "failure_type" in o:
            errs.append(f"line {n}: 資源事件不得含 failure_type")
    elif ev == "network":
        if o.get("target") not in ("internal","external"): errs.append(f"line {n}: target 非法 {o.get('target')!r}")
        if o.get("result") not in ("ok","fail"):           errs.append(f"line {n}: result 非法 {o.get('result')!r}")
        if not isinstance(o.get("fqdn"), str):
            errs.append(f'line {n}: fqdn 需為字串（IP 直連為 ""）')
        if not isinstance(o.get("detail"), str):
            errs.append(f"line {n}: detail 需為字串")
        if o.get("failure_type") not in FAILURES:          errs.append(f"line {n}: failure_type 非法 {o.get('failure_type')!r}")
        elif o.get("result") == "ok" and (o["failure_type"] != "ok" or o.get("level") != "ok"):
            errs.append(f"line {n}: result=ok 時 failure_type 與 level 必須皆為 ok")
        lat = o.get("latency_ms")
        if not num(lat) or lat < 0:
            errs.append(f"line {n}: latency_ms 需為 >=0 number（不得為 null）")
    else:
        if not num(o.get("uptime_s")) or o["uptime_s"] < 0: errs.append(f"line {n}: uptime_s 需為 >=0 number")
        if not num(o.get("cycle_ms")) or o["cycle_ms"] < 0: errs.append(f"line {n}: cycle_ms 需為 >=0 number")
        pid = o.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            errs.append(f"line {n}: pid 需為正整數")
        if not isinstance(o.get("version"), str) or not o["version"]:
            errs.append(f"line {n}: version 需為非空字串")
        if o.get("level") != "ok":                          errs.append(f"line {n}: heartbeat level 必須為 ok")
    return errs

def main(path):
    bad, rows = [], []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip():
                bad.append(f"line {n}: 空行"); continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError as e:
                bad.append(f"line {n}: 非合法 JSON ({e})"); continue
            rows.append((n, o)); bad += check(o, n)
    # ts 單調性（AC-22）：格式化良好的 ts 之字典序必須等於時間序
    raw = [o["ts"] for _, o in rows if isinstance(o.get("ts"), str) and TS_RE.match(o["ts"])]
    if raw != sorted(raw): bad.append("ts 未遞增（字典序 ≠ 時間序）")
    print(f"行數={len(rows)} 錯誤={len(bad)}")
    for b in bad[:50]: print(" ", b)
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/var/log/monitor-agent/agent.jsonl"))