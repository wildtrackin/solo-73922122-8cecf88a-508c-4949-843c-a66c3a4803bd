#!/usr/bin/env python3
"""端到端测试：真实启动两个 API 进程，验证全部需求语义。

运行：python3 tests/e2e_test.py
注意：本环境无法向进程发信号，服务器经 tests/server_runner.py 启动，
到 SERVER_LIFETIME 后 os._exit 自行退出（等价崩溃）；每个场景使用独立端口与数据目录。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PY = str(ROOT / ".venv" / "bin" / "python")
BASE_PORT = 9500
RUN_STAMP = f"{os.getpid()}"
FAILURES: list[str] = []
PASSES: list[str] = []

# 测试进程与服务器进程在同一宿主上，单调钟与 boot_id 同源
from app import clock  # noqa: E402


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASSES if cond else FAILURES).append(f"{name} {detail}")
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


def http(method: str, port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Server:
    _next_port = BASE_PORT

    def __init__(self, data_dir: Path, name: str, lifetime: int = 45, **env):
        Server._next_port += 1
        self.port = Server._next_port
        self.data_dir = data_dir
        self.name = name
        self.lifetime = lifetime
        self.extra_env = env
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Server":
        env = dict(os.environ)
        env.update({
            "DATA_DIR": str(self.data_dir),
            "PROCESS_NAME": self.name,
            "CLEANUP_INTERVAL_SECONDS": "2",
            "DELETE_LEASE_SECONDS": "4",
            "DELETE_RETRY_SECONDS": "3",
            # 校时演示偏移经共享文件注入，两个进程读取同一份
            "CLOCK_OFFSET_FILE": str(self.data_dir / "clock-offset"),
            "SERVER_LIFETIME": str(self.lifetime),
            "PORT": str(self.port),
        })
        env.update({k: str(v) for k, v in self.extra_env.items()})
        log = open(self.data_dir / f"server-{self.name}-{self.port}.log", "ab")
        self.proc = subprocess.Popen(
            [PY, "tests/server_runner.py"],
            cwd=ROOT, env=env, stdout=log, stderr=log,
        )
        deadline = time.time() + 40
        last_err: Exception | None = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"server {self.name} exited early rc={self.proc.returncode}; see {log.name}"
                )
            try:
                code, _ = http("GET", self.port, "/health")
                if code == 200:
                    return self
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_err = exc
            time.sleep(0.3)
        raise RuntimeError(
            f"server {self.name} did not become healthy (last_err={last_err!r}); see {log.name}"
        )

    def wait_dead(self, timeout_s: float | None = None) -> None:
        deadline = time.time() + (timeout_s or self.lifetime + 15)
        while time.time() < deadline:
            try:
                http("GET", self.port, "/health")
                time.sleep(0.4)
            except (urllib.error.URLError, ConnectionError, OSError):
                return
        raise RuntimeError(f"server {self.name} still alive after lifetime")


def fresh_dir(name: str) -> Path:
    d = Path("/tmp") / f"{name}-{RUN_STAMP}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def partition_status(port: int, pid: str) -> dict:
    _, parts = http("GET", port, "/partitions")
    return next(p for p in parts if p["partition_id"] == pid)


def db_set_deleting(data_dir: Path, pid: str, lease_delay_s: float, delete_file: bool) -> None:
    """模拟崩溃的删除者（旧版墙钟记录）：行卡在 deleting，单调列为 NULL。

    迁移后的认领逻辑把“单调期限缺失”视为已到期，验证已有 deleting 记录可恢复。
    可选地让文件已删（崩溃于两个不同阶段）。
    """
    conn = sqlite3.connect(str(data_dir / "datalake.db"), isolation_level=None)
    lease = int((time.time() + lease_delay_s) * 1000)
    conn.execute(
        "UPDATE partitions SET status='deleting', delete_owner='ghost:1', "
        "delete_lease_ms=?, delete_attempts=delete_attempts+1 WHERE partition_id=?",
        (lease, pid),
    )
    conn.close()
    if delete_file:
        (data_dir / "partitions" / f"{pid}.jsonl").unlink()


def db_set_deleting_mono(data_dir: Path, pid: str, lease_delay_s: float) -> None:
    """模拟崩溃的删除者（新版单调钟记录）：租约按实际经过时长 lease_delay_s 后到期。"""
    conn = sqlite3.connect(str(data_dir / "datalake.db"), isolation_level=None)
    conn.execute(
        "UPDATE partitions SET status='deleting', delete_owner='ghost:1', "
        "delete_lease_mono_ns=?, deadline_boot_id=?, delete_lease_ms=NULL, "
        "delete_attempts=delete_attempts+1 WHERE partition_id=?",
        (clock.mono_seconds_after(lease_delay_s), clock.boot_id(), pid),
    )
    conn.close()


def set_clock_offset(data_dir: Path, seconds: float) -> None:
    """写入墙钟偏移（秒）：模拟一次校时；单调钟不受影响。无需修改宿主时间。"""
    (data_dir / "clock-offset").write_text(f"{seconds}\n", encoding="ascii")


def wait_for_status(port: int, pid: str, wanted: str, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    st: dict = {}
    while time.time() < deadline:
        st = partition_status(port, pid)
        if st["status"] == wanted:
            return st
        time.sleep(0.3)
    return st


# ---------------------------------------------------------------- 场景 1：重启幂等
def scenario_restart_idempotent() -> None:
    print("== S1 重启幂等：已清理分区不得重新加入 ==")
    d = fresh_dir("lk-s1")
    s1 = Server(d, "s1a", lifetime=18, BACKGROUND_CLEANUP_ENABLED="false").start()
    code, res = http("POST", s1.port, "/cleanup/run")
    ok("S1 首轮清理回收 5 个到期分区", code == 200 and len(res["outcomes"]) == 5, str(res.get("message")))
    s1.wait_dead()

    s2 = Server(d, "s1b", lifetime=25, BACKGROUND_CLEANUP_ENABLED="false").start()
    _, parts = http("GET", s2.port, "/partitions")
    by_id = {p["partition_id"]: p for p in parts}
    ok("S1 重启后仍是 8 个分区", len(parts) == 8)
    ok("S1 已清理分区保持 cleaned 且文件未复活",
       all(by_id[f"P{i:04d}"]["status"] == "cleaned" and not by_id[f"P{i:04d}"]["file_exists"]
           for i in (1, 2, 4, 6, 8)))
    ok("S1 未到期分区保持 active 且文件在盘",
       all(by_id[f"P{i:04d}"]["status"] == "active" and by_id[f"P{i:04d}"]["file_exists"]
           for i in (3, 5, 7)))
    files = sorted(p.name for p in (d / "partitions").glob("*.jsonl"))
    ok("S1 磁盘上只剩 3 个未到期分区文件", files == ["P0003.jsonl", "P0005.jsonl", "P0007.jsonl"], str(files))


# ------------------------------------------------- 场景 2：中断后重启继续未完成删除
def scenario_crash_resume() -> None:
    print("== S2 中断的删除在重启后继续 ==")
    d = fresh_dir("lk-s2")
    s1 = Server(d, "s2a", lifetime=14, BACKGROUND_CLEANUP_ENABLED="false").start()
    # 崩溃窗口 A：已认领、租约将到期，文件还没删
    db_set_deleting(d, "P0001", lease_delay_s=2, delete_file=False)
    # 崩溃窗口 B：文件已删、状态还没落库
    db_set_deleting(d, "P0002", lease_delay_s=2, delete_file=True)
    s1.wait_dead()

    s2 = Server(d, "s2b", lifetime=30, BACKGROUND_CLEANUP_ENABLED="true").start()
    deadline = time.time() + 25
    final = {}
    while time.time() < deadline:
        p1 = partition_status(s2.port, "P0001")
        p2 = partition_status(s2.port, "P0002")
        final = {"P0001": p1["status"], "P0002": p2["status"]}
        if p1["status"] == "cleaned" and p2["status"] == "cleaned":
            break
        time.sleep(1)
    ok("S2 崩溃遗留的 deleting 分区最终被回收", final.get("P0001") == "cleaned" and final.get("P0002") == "cleaned", str(final))
    ok("S2 文件与索引一致",
       not (d / "partitions" / "P0001.jsonl").exists() and not (d / "partitions" / "P0002.jsonl").exists())


# ------------------------------------------------- 场景 3：删除失败不得报已回收 + 重试
def scenario_delete_failure() -> None:
    print("== S3 删除失败不报已回收，恢复后重试成功 ==")
    d = fresh_dir("lk-s3")
    s1 = Server(d, "s3a", lifetime=22, BACKGROUND_CLEANUP_ENABLED="true",
                DELETE_FAIL_PARTITIONS="P0004").start()
    deadline = time.time() + 18
    st = {}
    while time.time() < deadline:
        st = partition_status(s1.port, "P0004")
        if st["status"] == "delete_failed":
            break
        time.sleep(1)
    ok("S3 注入失败的分区进入 delete_failed", st.get("status") == "delete_failed", st.get("status", ""))
    ok("S3 失败分区绝未标记 cleaned", st.get("status") != "cleaned")
    ok("S3 失败原因被记录", bool(st.get("last_error")), str(st.get("last_error")))
    ok("S3 失败分区文件仍在盘上（未误删）", st.get("file_exists") is True)
    _, status = http("GET", s1.port, "/cleanup/status")
    ok("S3 状态接口如实报告失败", any(f["partition_id"] == "P0004" for f in status["failures"]))
    ok("S3 其余到期分区正常回收",
       all(partition_status(s1.port, f"P{i:04d}")["status"] == "cleaned" for i in (1, 2, 6, 8)))
    s1.wait_dead()

    s2 = Server(d, "s3b", lifetime=25, BACKGROUND_CLEANUP_ENABLED="true").start()
    deadline = time.time() + 15
    st = {}
    while time.time() < deadline:
        st = partition_status(s2.port, "P0004")
        if st["status"] == "cleaned":
            break
        time.sleep(1)
    ok("S3 去掉故障注入后重试成功回收", st.get("status") == "cleaned", st.get("status", ""))


# ------------------------------------------------- 场景 4：保全 vs 删除资格 确定先后
def scenario_race() -> None:
    print("== S4 两个进程间 保全/清理 并发：恰有一个赢家 ==")
    results = []
    for round_no in range(1, 7):
        d = fresh_dir(f"lk-s4-{round_no}")
        a = Server(d, f"s4a{round_no}", lifetime=20, BACKGROUND_CLEANUP_ENABLED="false").start()
        b = Server(d, f"s4b{round_no}", lifetime=20, BACKGROUND_CLEANUP_ENABLED="false").start()
        pres: dict = {}

        def do_pres():
            pres["r"] = http("POST", a.port, "/preservations", {
                "vehicle_id": "T101", "start": "2026-10-01T00:10:00Z",
                "end": "2026-10-01T00:50:00Z", "investigator": "racer", "reason": "race"})

        def do_clean():
            pres["c"] = http("POST", b.port, "/cleanup/run")

        t1, t2 = threading.Thread(target=do_pres), threading.Thread(target=do_clean)
        t1.start(); t2.start(); t1.join(); t2.join()
        pcode = pres["r"][0]
        p1 = partition_status(a.port, "P0001")
        if pcode == 201 and p1["status"] == "active":
            verdict = "preservation won"
            good = True
        elif pcode == 409 and p1["status"] in ("deleting", "cleaned"):
            verdict = f"cleanup won ({p1['status']})"
            good = True
        else:
            verdict = f"VIOLATION code={pcode} status={p1['status']}"
            good = False
        results.append(good)
        ok(f"S4 round{round_no}", good, verdict)
    ok("S4 全部轮次满足不变量", all(results))


# ------------------------------------------------- 场景 5：reconcile 孤儿文件
def scenario_reconcile() -> None:
    print("== S5 启动 reconcile 清理孤儿文件 ==")
    d = fresh_dir("lk-s5")
    s1 = Server(d, "s5a", lifetime=16, BACKGROUND_CLEANUP_ENABLED="false").start()
    http("POST", s1.port, "/cleanup/run")  # 回收全部到期分区
    s1.wait_dead()
    # 制造孤儿：一个无索引文件 + 一个已清理分区的残留文件
    (d / "partitions" / "P9999.jsonl").write_text("{}\n")
    shutil.copyfile(ROOT / "seed" / "partitions" / "P0001.jsonl", d / "partitions" / "P0001.jsonl")
    s2 = Server(d, "s5b", lifetime=20, BACKGROUND_CLEANUP_ENABLED="false").start()
    files = sorted(p.name for p in (d / "partitions").glob("*.jsonl"))
    ok("S5 孤儿文件被清除", files == ["P0003.jsonl", "P0005.jsonl", "P0007.jsonl"], str(files))
    ok("S5 已清理分区未被复活", partition_status(s2.port, "P0001")["status"] == "cleaned")


# ------------------------------------------------- 场景 6：保全语义细节
def scenario_preservation_semantics() -> None:
    print("== S6 保全语义：闭区间、原子拒绝、独立生效、幂等解除 ==")
    d = fresh_dir("lk-s6")
    a = Server(d, "s6a", lifetime=40, BACKGROUND_CLEANUP_ENABLED="false").start()
    b = Server(d, f"s6b", lifetime=40, BACKGROUND_CLEANUP_ENABLED="false").start()

    # 闭区间边界：P0001=[00:00,01:00], P0002=[01:00,02:00]；查询终点恰为 01:00 -> 两个都重叠
    code, res = http("POST", a.port, "/preservations", {
        "vehicle_id": "T101", "start": "2026-10-01T00:30:00Z", "end": "2026-10-01T01:00:00Z",
        "investigator": "i1", "reason": "boundary"})
    ok("S6 闭区间边界重叠两个分区", code == 201 and res["preservation"]["partitions"] == ["P0001", "P0002"],
       str(res.get("preservation", {}).get("partitions")))
    prs1 = res["preservation"]["preservation_id"]

    # 部分不可保全 -> 整次拒绝（P0001 已被保全不影响；先制造 deleting 状态再请求）
    db_set_deleting(d, "P0008", lease_delay_s=60, delete_file=False)
    code, res = http("POST", b.port, "/preservations", {
        "vehicle_id": "T101", "start": "2026-10-01T00:05:00Z", "end": "2026-10-01T03:30:00Z",
        "investigator": "i2", "reason": "should-fail"})
    ok("S6 任一分区已开始删除则整次 409", code == 409)
    _, pres_list = http("GET", a.port, "/preservations?vehicle_id=T101&status=active")
    ok("S6 被拒请求未产生任何保全记录", all(p["investigator"] != "i2" for p in pres_list))
    # 回滚 P0008 状态，避免干扰后续步骤
    conn = sqlite3.connect(str(d / "datalake.db"), isolation_level=None)
    conn.execute("UPDATE partitions SET status='active', delete_owner=NULL, delete_lease_ms=NULL WHERE partition_id='P0008'")
    conn.close()

    # 重叠保全分别生效：第二份保全也覆盖 P0001
    code, res = http("POST", b.port, "/preservations", {
        "vehicle_id": "T101", "start": "2026-10-01T00:45:00Z", "end": "2026-10-01T00:55:00Z",
        "investigator": "i3", "reason": "overlap"})
    prs2 = res["preservation"]["preservation_id"]
    ok("S6 第二份重叠保全建立", code == 201 and res["preservation"]["partitions"] == ["P0001"])

    http("POST", a.port, f"/preservations/{prs1}/release")
    code, res = http("POST", b.port, "/cleanup/run")
    cleaned = {o["partition_id"] for o in res["outcomes"] if o["result"] == "cleaned"}
    ok("S6 解除一份后 P0001 仍受第二份保护", "P0001" not in cleaned and partition_status(a.port, "P0001")["status"] == "active")
    ok("S6 P0002 已无保全被回收", "P0002" in cleaned)

    # 幂等解除
    code1, _ = http("POST", a.port, f"/preservations/{prs2}/release")
    code2, res2 = http("POST", b.port, f"/preservations/{prs2}/release")
    ok("S6 重复解除幂等", code1 == 200 and code2 == 200 and res2["status"] == "released")
    code, _ = http("POST", a.port, "/preservations/PRS-notexist/release")
    ok("S6 解除不存在保全返回 404", code == 404)

    code, res = http("POST", a.port, "/cleanup/run")
    ok("S6 全部解除后 P0001 被回收", partition_status(a.port, "P0001")["status"] == "cleaned")


# ------------------------------------------------- 场景 7：清理期间其他分区可操作
def scenario_operable_during_cleanup() -> None:
    print("== S7 清理期间其他分区仍可查询/保全 ==")
    d = fresh_dir("lk-s7")
    # 注入对 P0004 的删除失败，让清理“卡住重试”，期间操作其他分区
    a = Server(d, "s7a", lifetime=30, BACKGROUND_CLEANUP_ENABLED="true",
               DELETE_FAIL_PARTITIONS="P0004").start()
    b = Server(d, "s7b", lifetime=30, BACKGROUND_CLEANUP_ENABLED="false").start()
    time.sleep(3)  # 让清理循环跑起来
    code, res = http("GET", b.port, "/samples?vehicle_id=T101&start=2026-10-01T02:00:00Z&end=2026-10-01T02:01:00Z")
    ok("S7 清理进行中波形查询正常", code == 200 and res["matched"] > 0, f"matched={res.get('matched')}")
    code, res = http("POST", b.port, "/preservations", {
        "vehicle_id": "T102", "start": "2026-10-02T01:10:00Z", "end": "2026-10-02T01:20:00Z",
        "investigator": "i", "reason": "during-cleanup"})
    ok("S7 清理进行中可对未到期分区建立保全", code == 201 and res["preservation"]["partitions"] == ["P0005"])
    _, st = http("GET", b.port, "/cleanup/status")
    ok("S7 状态接口在清理期间可用", "counts" in st)


# --------------------------------- S8：墙钟校时不改变清理期限（单调钟判定）
def scenario_clock_jump_immunity() -> None:
    print("== S8 墙钟前后跳：租约不被提前接管、重试不被推迟/提前 ==")

    # --- S8a 租约：向前校时 1 小时，未满 30s（测试中为 4s）的租约不得被接管 ---
    d = fresh_dir("lk-s8a")
    a = Server(d, "s8a", lifetime=40, BACKGROUND_CLEANUP_ENABLED="false",
               SLOW_DELETE_PARTITIONS="P0001", SLOW_DELETE_SECONDS="3").start()
    b = Server(d, "s8b", lifetime=40, BACKGROUND_CLEANUP_ENABLED="false").start()
    t0 = time.time()
    # a 在后台线程认领 P0001 并卡在慢删除里（事务已提交、锁已释放、租约生效中）
    threading.Thread(
        target=lambda: http("POST", a.port, "/cleanup/run"), daemon=True
    ).start()
    st = wait_for_status(b.port, "P0001", "deleting", 4)
    ok("S8a 慢删除期间分区处于 deleting", st.get("status") == "deleting", st.get("status", ""))
    # 向前校时 +3600s（仅墙钟，单调钟不动）
    set_clock_offset(d, 3600)
    time.sleep(0.5)
    _, h = http("GET", b.port, "/health")
    ok("S8a 墙钟确实向前跳 1 小时", abs(h["wall_offset_seconds"] - 3600) < 0.01, str(h.get("wall_offset_seconds")))
    code, res = http("POST", b.port, "/cleanup/run")
    p1 = partition_status(b.port, "P0001")
    ok("S8a 前跳后租约未到期：另一进程不得接管 P0001",
       code == 200 and p1["status"] == "deleting" and p1["delete_owner"].startswith("s8a"),
       f"{p1['status']} owner={p1['delete_owner']} outcomes={[o['partition_id'] for o in res['outcomes']]}")
    _, cst = http("GET", b.port, "/cleanup/status")
    inflight = next(x for x in cst["in_flight"] if x["partition_id"] == "P0001")
    ok("S8a 租约剩余时长按单调钟继续走（仍 >0）", inflight["lease_remaining_seconds"] > 0,
       str(inflight["lease_remaining_seconds"]))
    # 慢删除结束后由持有进程自己收敛为 cleaned（接管窗口从未因校时而提前打开）
    final = wait_for_status(a.port, "P0001", "cleaned", 8)
    ok("S8a 慢删除结束后持有进程自行完成回收（未发生提前接管）",
       final.get("status") == "cleaned", f"{final.get('status')} elapsed={time.time() - t0:.1f}s")

    # --- S8b 重试：向后校时 1 小时，已到重试时间的失败分区不得继续等待 ---
    d = fresh_dir("lk-s8b")
    a = Server(d, "s8b-a", lifetime=40, BACKGROUND_CLEANUP_ENABLED="true",
               DELETE_FAIL_PARTITIONS="P0004").start()
    st = wait_for_status(a.port, "P0004", "delete_failed", 12)
    ok("S8b 失败分区进入 delete_failed", st.get("status") == "delete_failed")
    # 向后校时 -3600s
    set_clock_offset(d, -3600)
    time.sleep(0.5)
    _, cst = http("GET", a.port, "/cleanup/status")
    fail = next(x for x in cst["failures"] if x["partition_id"] == "P0004")
    ok("S8b 后跳瞬间重试倒计时仍按实际时长继续", fail["retry_remaining_seconds"] is not None
       and fail["retry_remaining_seconds"] >= 0, str(fail["retry_remaining_seconds"]))
    # 等单调重试间隔（3s）过去；墙钟倒退不应让它继续等待
    time.sleep(4)
    _, cst = http("GET", a.port, "/cleanup/status")
    fail = next((x for x in cst["failures"] if x["partition_id"] == "P0004"), None)
    st = partition_status(a.port, "P0004")
    ok("S8b 实际经过重试间隔后仍处 delete_failed（注入未解除，反复重试不谎报成功）",
       st["status"] == "delete_failed" and fail is not None)
    # 重试确实在按单调钟发生：attempts 应 >1
    ok("S8b 墙钟倒退期间重试按单调钟持续发生", st["delete_attempts"] >= 2,
       f"attempts={st['delete_attempts']}")
    a.wait_dead()


# ----------------------------- S9：容器单独/全部重启不重计期限
def scenario_restart_keeps_deadlines() -> None:
    print("== S9 同宿主启动周期内重启：单调期限连续，不重计 ==")

    # --- S9a 执行进程退出后，租约到期仍可接管（重启前后单调读数同源）---
    d = fresh_dir("lk-s9a")
    s1 = Server(d, "s9a", lifetime=12, BACKGROUND_CLEANUP_ENABLED="false").start()
    db_set_deleting_mono(d, "P0001", lease_delay_s=5)
    s1.wait_dead()
    s2 = Server(d, "s9b", lifetime=24, BACKGROUND_CLEANUP_ENABLED="true").start()
    _, meta = http("GET", s2.port, "/meta/info")
    ok("S9a 重启后 boot_id 不变（同一宿主启动周期）",
       meta["clock"]["boot_id"] == clock.boot_id())
    t0 = time.time()
    st = wait_for_status(s2.port, "P0001", "cleaned", 10)
    elapsed = time.time() - t0
    ok("S9a 重启后租约按原单调时刻到期并被接管", st.get("status") == "cleaned",
       f"{st.get('status')} wait={elapsed:.1f}s")
    ok("S9a 接管发生在原租约到期附近而非重新计满 4s", elapsed <= 7, f"{elapsed:.1f}s")

    # --- S9b delete_failed 行的重试期限跨重启不重计 ---
    d = fresh_dir("lk-s9b")
    s1 = Server(d, "s9c", lifetime=16, BACKGROUND_CLEANUP_ENABLED="true",
                DELETE_FAIL_PARTITIONS="P0004").start()
    st = wait_for_status(s1.port, "P0004", "delete_failed", 12)
    attempts_before = st["delete_attempts"]
    ok("S9b 重启前已进入 delete_failed", st.get("status") == "delete_failed")
    s1.wait_dead()
    # 重启前先把重试间隔几乎等完
    time.sleep(2.5)
    s2 = Server(d, "s9d", lifetime=22, BACKGROUND_CLEANUP_ENABLED="true",
                DELETE_FAIL_PARTITIONS="P0004").start()
    time.sleep(3)  # 若重计 3s，这段时间内不会有新尝试；若连续则已再次重试
    st = partition_status(s2.port, "P0004")
    ok("S9b 重启不重计重试期限：后台循环很快再次重试",
       st["status"] == "delete_failed" and st["delete_attempts"] > attempts_before,
       f"before={attempts_before} after={st['delete_attempts']}")


# ----------------------------- S10：写锁等待（busy_timeout 串行，不报错）
def scenario_lock_wait() -> None:
    print("== S10 SQLite 写锁被占时清理事务等待而非失败 ==")
    d = fresh_dir("lk-s10")
    s = Server(d, "s10", lifetime=30, BACKGROUND_CLEANUP_ENABLED="false").start()
    holder = sqlite3.connect(str(d / "datalake.db"), timeout=30,
                             isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # 占住写锁

    def release_lock():
        time.sleep(3)  # 让清理请求先在 busy_timeout 上等待
        holder.execute("ROLLBACK")
        holder.close()

    threading.Thread(target=release_lock, daemon=True).start()
    try:
        t0 = time.time()
        code, res = http("POST", s.port, "/cleanup/run")
        waited = time.time() - t0
        ok("S10 锁等待期间请求不报错，拿到锁后正常完成",
           code == 200 and len(res.get("outcomes", [])) == 5,
           f"code={code} waited={waited:.1f}s outcomes={res.get('outcomes')}")
        ok("S10 确实经历了锁等待（>=2s）", waited >= 2.0, f"{waited:.1f}s")
    finally:
        try:
            holder.execute("ROLLBACK")
            holder.close()
        except sqlite3.Error:
            pass
    # 锁释放后再次清理应立即返回（没有新分区）
    code, res = http("POST", s.port, "/cleanup/run")
    ok("S10 锁释放后清理即时返回", code == 200 and res["outcomes"] == [])


# ----------------------------- S11：旧版墙钟记录迁移后可恢复
def scenario_legacy_rows_recover() -> None:
    print("== S11 仅有墙钟期限的 deleting/delete_failed 记录迁移后可恢复 ==")
    d = fresh_dir("lk-s11")
    s1 = Server(d, "s11a", lifetime=14, BACKGROUND_CLEANUP_ENABLED="false").start()
    # 旧版 deleting：墙钟租约写在“很久以后”，但单调列为 NULL
    db_set_deleting(d, "P0001", lease_delay_s=86400, delete_file=False)
    # 旧版 delete_failed：墙钟重试时刻写在“很久以后”，单调列为 NULL
    conn = sqlite3.connect(str(d / "datalake.db"), isolation_level=None)
    conn.execute(
        "UPDATE partitions SET status='delete_failed', last_error='legacy wall error', "
        "next_retry_ms=?, delete_attempts=1 WHERE partition_id='P0002'",
        (int((time.time() + 86400) * 1000),),
    )
    conn.close()
    s1.wait_dead()

    s2 = Server(d, "s11b", lifetime=24, BACKGROUND_CLEANUP_ENABLED="true").start()
    st1 = wait_for_status(s2.port, "P0001", "cleaned", 12)
    st2 = wait_for_status(s2.port, "P0002", "cleaned", 12)
    ok("S11 旧版 deleting（墙钟租约未到期）迁移后立即被接管回收",
       st1.get("status") == "cleaned", st1.get("status", ""))
    ok("S11 旧版 delete_failed（墙钟重试未到）迁移后立即重试并回收",
       st2.get("status") == "cleaned", st2.get("status", ""))
    # 迁移后的新认领写入单调期限列与 boot 标识
    conn = sqlite3.connect(str(d / "datalake.db"), isolation_level=None)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(partitions)")}
    conn.close()
    ok("S11 旧库已在线迁移出单调期限列",
       {"delete_lease_mono_ns", "next_retry_mono_ns", "deadline_boot_id"} <= cols,
       str(sorted(cols)))


def main() -> int:
    scenarios = [
        scenario_restart_idempotent,
        scenario_crash_resume,
        scenario_delete_failure,
        scenario_race,
        scenario_reconcile,
        scenario_preservation_semantics,
        scenario_operable_during_cleanup,
        scenario_clock_jump_immunity,
        scenario_restart_keeps_deadlines,
        scenario_lock_wait,
        scenario_legacy_rows_recover,
    ]
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    for sc in scenarios:
        name = sc.__name__
        if only and not any(o in name for o in only):
            continue
        try:
            sc()
        except Exception as exc:  # noqa: BLE001
            ok(f"{name} 异常终止", False, repr(exc))
    print("\n========== 汇总 ==========")
    print(f"PASS: {len(PASSES)}  FAIL: {len(FAILURES)}")
    for f in FAILURES:
        print("FAIL:", f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
