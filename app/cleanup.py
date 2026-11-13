"""后台清理：仅处理“已到期且无生效保全”的分区，逐分区认领删除资格。

关键语义：
- 期限判定使用内核单调钟（CLOCK_MONOTONIC_RAW，见 app/clock.py）的实际经过
  时长：DELETE_LEASE_SECONDS / DELETE_RETRY_SECONDS 不受墙钟前后跳（校时）影响。
  两个 API 进程共用同一单调钟与 boot_id，判定完全一致；容器单独或全部重启
  不重计期限；宿主重启（boot_id 变化）后旧期限立即视为到期、可被接管。
- 认领（claim）在 BEGIN IMMEDIATE 事务内完成：进入事务即取得 SQLite 写锁，
  **租约时刻在取得写锁之后才读取**，与保全创建互斥串行，谁先提交谁生效；
  条件 UPDATE + rowcount 校验保证同一分区只有一个赢家。
- 拿到资格后提交事务、释放数据库锁再删文件：清理某分区期间，其他分区照常
  可操作（文件删除期间不持写锁）。
- 文件删除失败 -> 状态置 delete_failed 并记录错误，绝不标记为已回收；
  后续 pass 按单调钟重试间隔重试。
- 进程中断 -> deleting 行的租约按实际经过时长到期后，任一存活进程可重新认领
  并继续删除；文件已删但状态未更新的情况通过“文件不存在即视为删除成功”
  收敛到 cleaned。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import clock, config
from .db import immediate_txn
from .timeutil import ms_to_iso, now_ms

log = logging.getLogger("datalake.cleanup")

# 可认领条件：活跃 / 删除失败且已到重试时刻 / 删除中但租约已过期（持有进程已崩溃）
# 期限一律按单调钟实际经过时长判定；boot_id 变化或旧版空期限列视为已到期。
CLAIM_SQL = """
SELECT p.partition_id, p.file_path
FROM partitions p
WHERE p.expires_ms <= :wall_now
  AND (
      p.status = 'active'
      OR (
          p.status = 'delete_failed'
          AND (
              p.next_retry_mono_ns IS NULL
              OR p.deadline_boot_id IS NULL
              OR p.deadline_boot_id != :boot
              OR p.next_retry_mono_ns <= :mono_now
          )
      )
      OR (
          p.status = 'deleting'
          AND (
              p.delete_lease_mono_ns IS NULL
              OR p.deadline_boot_id IS NULL
              OR p.deadline_boot_id != :boot
              OR p.delete_lease_mono_ns <= :mono_now
          )
      )
  )
  AND NOT EXISTS (
      SELECT 1
      FROM preservation_partitions pp
      JOIN preservations pr ON pr.preservation_id = pp.preservation_id
      WHERE pp.partition_id = p.partition_id
        AND pr.status = 'active'
  )
ORDER BY p.expires_ms ASC, p.partition_id ASC
LIMIT :lim
"""

MARK_CLAIMED_SQL = """
UPDATE partitions
SET status = 'deleting',
    delete_owner = :owner,
    delete_lease_mono_ns = :lease_mono,
    deadline_boot_id = :boot,
    -- 墙钟展示列不再作为判定依据，清空以免产生歧义
    delete_lease_ms = NULL,
    next_retry_ms = NULL,
    delete_attempts = delete_attempts + 1
WHERE partition_id = :pid
  AND (
      status = 'active'
      OR (
          status = 'delete_failed'
          AND (
              next_retry_mono_ns IS NULL OR deadline_boot_id IS NULL
              OR deadline_boot_id != :boot OR next_retry_mono_ns <= :mono_now
          )
      )
      OR (
          status = 'deleting'
          AND (
              delete_lease_mono_ns IS NULL OR deadline_boot_id IS NULL
              OR deadline_boot_id != :boot OR delete_lease_mono_ns <= :mono_now
          )
      )
  )
"""


def claim_one(exclude: set[str] | None = None) -> dict[str, Any] | None:
    """在一个 IMMEDIATE 事务内挑选并认领一个可删除分区。

    时刻在 BEGIN IMMEDIATE 取得写锁之后才读取：租约完整建立于锁内，
    锁等待耗时不会被计入租约，且与保全事务严格串行。

    exclude：本轮 pass 已尝试过的分区，避免持续失败的分区在同一轮里被
    反复认领而饿死其他分区（与 next_retry 退避共同保证公平）。
    """
    exclude = exclude or set()
    with immediate_txn() as conn:
        # 取得写锁之后再建立“现在”基准与完整租约
        mono_now = clock.mono_ns()
        boot = clock.boot_id()
        rows = conn.execute(
            CLAIM_SQL,
            {"wall_now": now_ms(), "mono_now": mono_now, "boot": boot,
             "lim": len(exclude) + 1},
        ).fetchall()
        row = next((r for r in rows if r["partition_id"] not in exclude), None)
        if row is None:
            return None
        cur = conn.execute(
            MARK_CLAIMED_SQL,
            {
                "owner": config.OWNER,
                "lease_mono": clock.mono_seconds_after(config.DELETE_LEASE_SECONDS),
                "boot": boot,
                "pid": row["partition_id"],
                "mono_now": mono_now,
            },
        )
        if cur.rowcount != 1:
            # 理论上被 IMMEDIATE 串行化后不会发生；兜底视为认领失败
            return None
        return {"partition_id": row["partition_id"], "file_path": row["file_path"]}


def _mark_delete_failed(partition_id: str, err: str) -> None:
    """失败落库：重试期限同样按单调钟实际经过时长计算（锁内取时刻）。"""
    with immediate_txn() as conn:
        mono_now = clock.mono_ns()
        boot = clock.boot_id()
        conn.execute(
            "UPDATE partitions SET status = 'delete_failed', last_error = ?, "
            "delete_lease_mono_ns = NULL, next_retry_mono_ns = ?, "
            "deadline_boot_id = ?, delete_lease_ms = NULL, next_retry_ms = NULL "
            "WHERE partition_id = ?",
            (err, mono_now + int(config.DELETE_RETRY_SECONDS * 1_000_000_000),
             boot, partition_id),
        )


def execute_delete(partition_id: str, file_path: str) -> dict[str, str]:
    """删文件并按结果更新索引；失败绝不报已回收。

    文件删除在任何数据库事务之外执行——不持 SQLite 写锁，删除期间
    其他分区的查询与保全不受影响。
    """
    path = config.DATA_DIR / file_path
    # 慢删除注入：制造“删除进行中”的观察窗口（仍在事务/写锁之外）
    if partition_id in config.SLOW_DELETE_PARTITIONS:
        log.info("分区 %s 命中慢删除注入，睡眠 %ss（不持数据库锁）",
                 partition_id, config.SLOW_DELETE_SECONDS)
        time.sleep(config.SLOW_DELETE_SECONDS)
    try:
        if partition_id in config.DELETE_FAIL_PARTITIONS:
            raise OSError("injected delete failure (DELETE_FAIL_PARTITIONS)")
        os.remove(path)
        detail = "file deleted"
    except FileNotFoundError:
        detail = "file already absent (crash-resume converge)"
    except OSError as exc:
        err = f"{type(exc).__name__}: {exc}"
        log.warning("分区 %s 删除失败：%s", partition_id, err)
        _mark_delete_failed(partition_id, err)
        return {"partition_id": partition_id, "result": "failed", "detail": err}

    with immediate_txn() as conn:
        conn.execute(
            "UPDATE partitions SET status = 'cleaned', cleaned_ms = ?, "
            "delete_owner = NULL, delete_lease_ms = NULL, next_retry_ms = NULL, "
            "delete_lease_mono_ns = NULL, next_retry_mono_ns = NULL, "
            "deadline_boot_id = NULL, last_error = NULL WHERE partition_id = ?",
            (now_ms(), partition_id),
        )
    log.info("分区 %s 已回收（%s）", partition_id, detail)
    return {"partition_id": partition_id, "result": "cleaned", "detail": detail}


def cleanup_pass(reason: str) -> dict[str, Any]:
    """同步执行一轮清理：逐个认领并删除，直到没有合格分区或达到上限。"""
    started = now_ms()
    outcomes: list[dict[str, str]] = []
    attempted: set[str] = set()
    for _ in range(config.CLEANUP_MAX_PER_PASS):
        claimed = claim_one(exclude=attempted)
        if claimed is None:
            break
        attempted.add(claimed["partition_id"])
        outcomes.append(execute_delete(claimed["partition_id"], claimed["file_path"]))
    finished = now_ms()
    result = {
        "process": config.OWNER,
        "reason": reason,
        "started_at": ms_to_iso(started),
        "finished_at": ms_to_iso(finished),
        "outcomes": outcomes,
        "message": (
            f"claimed {len(outcomes)} partition(s): "
            f"{sum(1 for o in outcomes if o['result'] == 'cleaned')} cleaned, "
            f"{sum(1 for o in outcomes if o['result'] == 'failed')} failed"
        ),
    }
    log.info("清理 pass（%s）完成：%s", reason, result["message"])
    return result


async def run_cleanup_once(app: Any, reason: str) -> dict[str, Any]:
    """异步包装：同进程内串行（锁），跨进程由 SQLite 事务串行。"""
    async with app.state.cleanup_lock:
        result = await asyncio.to_thread(cleanup_pass, reason)
        app.state.last_cleanup_run = result
    return result


async def cleanup_loop(app: Any) -> None:
    """后台循环：到期的删除资格认领（含崩溃遗留的租约过期分区）在这里被持续拾起。

    先等待一个周期间隔再执行首轮，给演示/保全操作留出时间窗；
    手动 POST /cleanup/run 不受此延迟影响。
    """
    log.info(
        "后台清理循环已启动：interval=%ss lease=%ss retry=%ss owner=%s boot=%s",
        config.CLEANUP_INTERVAL_SECONDS, config.DELETE_LEASE_SECONDS,
        config.DELETE_RETRY_SECONDS, config.OWNER, clock.boot_id(),
    )
    while True:
        await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)
        try:
            await run_cleanup_once(app, "timer")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 循环不允许因单次失败退出
            log.exception("清理 pass 异常，下个周期重试")
