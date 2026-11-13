#!/usr/bin/env python3
"""核心期限判定测试（仅标准库，无需 FastAPI/uvicorn）。

直接在进程内驱动 app.clock / app.db / app.cleanup，验证：
1. 墙钟前后跳不改变租约/重试期限（单调钟实际经过时长判定）；
2. 认领在写锁内建立完整租约，锁等待不计入租约；
3. 同宿主重启不重计期限，宿主重启（boot_id 变化）后旧期限立即可接管；
4. 旧版仅有墙钟期限的 deleting/delete_failed 行迁移后可恢复；
5. 文件删除期间不持 SQLite 写锁。

运行：python3 tests/core_clock_test.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="core-clock-"))
    os.environ["DATA_DIR"] = str(tmp)
    os.environ["CLOCK_OFFSET_FILE"] = str(tmp / "clock-offset")
    os.environ["DELETE_LEASE_SECONDS"] = "2"
    os.environ["DELETE_RETRY_SECONDS"] = "2"
    os.environ["CLEANUP_INTERVAL_SECONDS"] = "60"
    os.environ["BACKGROUND_CLEANUP_ENABLED"] = "false"

    from app import clock, config, db
    from app import cleanup as cu
    from app.seed import seed_from_manifest

    db.init_db()
    seed_from_manifest()
    boot = clock.boot_id()
    print("== 1. 单调钟不受墙钟偏移影响 ==")
    m0, w0 = clock.mono_ns(), clock.wall_now_ms()
    (tmp / "clock-offset").write_text("3600\n")
    m1, w1 = clock.mono_ns(), clock.wall_now_ms()
    check("墙钟 +3600s 生效", abs((w1 - w0) - 3_600_000) < 100, f"{w1 - w0}ms")
    check("单调钟读数不随墙钟跳变", abs((m1 - m0) / 1e9) < 0.5, f"{(m1 - m0)/1e9:.3f}s")
    (tmp / "clock-offset").write_text("-7200\n")
    check("墙钟 -7200s 同样不影响单调钟",
          abs(clock.mono_ns() - m0) < 5e8 and clock.wall_now_ms() < w0)
    (tmp / "clock-offset").write_text("0\n")

    print("== 2. 租约按实际经过时长到期，墙钟跳变不改变期限 ==")
    claim = cu.claim_one()
    check("认领到一个到期分区", claim is not None)
    pid = claim["partition_id"]
    with db.read_conn() as conn:
        row = conn.execute(
            "SELECT delete_lease_mono_ns, deadline_boot_id, delete_lease_ms "
            "FROM partitions WHERE partition_id=?", (pid,)).fetchone()
    check("租约写入单调列与 boot_id",
          row["delete_lease_mono_ns"] is not None and row["deadline_boot_id"] == boot)
    check("旧墙钟展示列已停用（NULL）", row["delete_lease_ms"] is None)
    # 前跳墙钟，租约不得提前到期
    (tmp / "clock-offset").write_text("86400\n")
    again = cu.claim_one(exclude=set())
    check("墙钟前跳 1 天后租约仍有效（同一分区不会被再次认领）",
          again is None or again["partition_id"] != pid,
          str(again and again["partition_id"]))
    (tmp / "clock-offset").write_text("0\n")
    # 等单调租约到期
    time.sleep(2.1)
    again = cu.claim_one()
    check("实际经过租约时长后可重新认领", again is not None and again["partition_id"] == pid)
    # 收敛掉测试分区
    cu.execute_delete(pid, claim["file_path"])

    print("== 3. 重试期限按实际经过时长判定 ==")
    fail_pid = "P0002"
    os.environ["DELETE_FAIL_PARTITIONS"] = fail_pid
    config.DELETE_FAIL_PARTITIONS = {fail_pid}
    claimed = cu.claim_one()
    check("认领失败注入分区", claimed is not None and claimed["partition_id"] == fail_pid)
    res = cu.execute_delete(fail_pid, claimed["file_path"])
    check("删除失败绝不报 cleaned", res["result"] == "failed")
    with db.read_conn() as conn:
        row = conn.execute(
            "SELECT next_retry_mono_ns, deadline_boot_id FROM partitions WHERE partition_id=?",
            (fail_pid,)).fetchone()
    check("重试期限写入单调列", row["next_retry_mono_ns"] is not None and row["deadline_boot_id"] == boot)
    (tmp / "clock-offset").write_text("-86400\n")
    skipped = cu.claim_one()
    check("墙钟倒退 1 天不会让重试提前（分区不可被认领）",
          skipped is None or skipped["partition_id"] != fail_pid)
    (tmp / "clock-offset").write_text("0\n")
    time.sleep(2.1)
    retry = cu.claim_one()
    check("单调重试间隔过后可再次认领", retry is not None and retry["partition_id"] == fail_pid)
    config.DELETE_FAIL_PARTITIONS = set()
    os.environ.pop("DELETE_FAIL_PARTITIONS", None)
    cu.execute_delete(fail_pid, claimed["file_path"])

    print("== 4. 写锁竞争：等待后在锁内建立租约 ==")
    holder = sqlite3.connect(str(config.DB_PATH), timeout=30,
                             isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")

    def release():
        time.sleep(2.0)
        holder.execute("ROLLBACK")
        holder.close()

    threading.Thread(target=release, daemon=True).start()
    t0 = time.time()
    got = cu.claim_one()
    waited = time.time() - t0
    check("写锁被占时认领等待而非报错", got is not None, f"waited={waited:.2f}s")
    check("确实等待了锁（>=1.5s），租约时刻取在拿锁之后", waited >= 1.5, f"{waited:.2f}s")
    cu.execute_delete(got["partition_id"], got["file_path"])

    print("== 5. boot_id 变化（宿主重启/卷迁移）后旧期限立即视为到期 ==")
    claimed = cu.claim_one()
    with db.immediate_txn() as conn:
        conn.execute(
            "UPDATE partitions SET deadline_boot_id='stale-boot-id' WHERE partition_id=?",
            (claimed["partition_id"],))
    takeover = cu.claim_one()
    check("boot_id 失配后租约立即到期、可被接管",
          takeover is not None and takeover["partition_id"] == claimed["partition_id"])
    cu.execute_delete(claimed["partition_id"], claimed["file_path"])

    print("== 6. 旧版墙钟记录迁移后可恢复 ==")
    with db.immediate_txn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO partitions "
            "(partition_id, vehicle_id, start_ms, end_ms, expires_ms, file_path, "
            " sample_count, status, created_ms, delete_lease_ms, next_retry_ms, "
            " delete_lease_mono_ns, next_retry_mono_ns, deadline_boot_id) "
            "VALUES ('P9001','TX',0,1,0,'partitions/P9001.jsonl',0,'deleting',0,?,?,NULL,NULL,NULL)",
            (int(time.time() * 1000) + 86_400_000, None))
    legacy = cu.claim_one()
    check("旧版 deleting（墙钟租约在 1 天后，单调列为 NULL）立即被接管",
          legacy is not None and legacy["partition_id"] == "P9001")

    print("== 7. 文件删除期间不持写锁（删除慢时其他事务可写） ==")
    os.environ["SLOW_DELETE_PARTITIONS"] = "P9002"
    os.environ["SLOW_DELETE_SECONDS"] = "2"
    config.SLOW_DELETE_PARTITIONS = {"P9002"}
    config.SLOW_DELETE_SECONDS = 2.0
    with db.immediate_txn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO partitions "
            "(partition_id, vehicle_id, start_ms, end_ms, expires_ms, file_path, "
            " sample_count, status, created_ms) "
            "VALUES ('P9002','TX',0,1,0,'partitions/P9002.jsonl',0,'active',0)")
    claimed = cu.claim_one()
    check("认领 P9002 成功", claimed is not None and claimed["partition_id"] == "P9002")

    def slow_delete():
        cu.execute_delete("P9002", claimed["file_path"])

    t = threading.Thread(target=slow_delete, daemon=True)
    t.start()
    time.sleep(0.5)
    t0 = time.time()
    with db.immediate_txn() as conn:  # 删除进行中应能立刻拿到写锁
        conn.execute("INSERT INTO meta(key,value) VALUES('write-during-delete','ok') "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    write_wait = time.time() - t0
    check("慢删除期间其他写事务不被阻塞（删除不持写锁）", write_wait < 0.5, f"{write_wait:.2f}s")
    t.join()

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n========== 汇总 ==========\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for f in FAIL:
        print("FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
