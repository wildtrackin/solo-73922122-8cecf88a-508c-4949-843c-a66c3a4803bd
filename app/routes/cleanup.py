"""清理接口：手动触发一轮清理、查看清理状态。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import clock, config
from ..cleanup import run_cleanup_once
from ..db import read_conn
from ..models import (
    CleanupRunResult,
    CleanupStatus,
    ClockInfo,
    DeleteFailure,
    InFlightDelete,
)
from ..timeutil import ms_to_iso, now_ms

router = APIRouter(prefix="/cleanup", tags=["清理"])


@router.post(
    "/run",
    response_model=CleanupRunResult,
    summary="立即执行一轮清理（仅处理已到期且无生效保全的分区）",
)
async def run_cleanup(request: Request) -> CleanupRunResult:
    """逐分区认领删除资格并删除文件；与后台循环共用同一把进程内锁。

    删除失败的分区在返回中标记为 failed（索引中为 delete_failed），
    不会被报告为已回收。租约/重试期限按单调钟实际经过时长判定。
    """
    result = await run_cleanup_once(request.app, "manual")
    return CleanupRunResult(**result)


@router.get("/status", response_model=CleanupStatus, summary="清理状态总览")
def cleanup_status(request: Request) -> CleanupStatus:
    mono_now = clock.mono_ns()
    boot = clock.boot_id()
    with read_conn() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM partitions GROUP BY status"
        )}
        for key in ("active", "deleting", "delete_failed", "cleaned"):
            counts.setdefault(key, 0)

        wall_now = now_ms()
        pending = conn.execute(
            """
            SELECT COUNT(*) AS n FROM partitions p
            WHERE p.status = 'active' AND p.expires_ms <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM preservation_partitions pp
                  JOIN preservations pr ON pr.preservation_id = pp.preservation_id
                  WHERE pp.partition_id = p.partition_id AND pr.status = 'active'
              )
            """,
            (wall_now,),
        ).fetchone()["n"]

        in_flight: list[InFlightDelete] = []
        for r in conn.execute(
            "SELECT partition_id, delete_owner, delete_lease_ms, delete_lease_mono_ns, "
            "deadline_boot_id, delete_attempts "
            "FROM partitions WHERE status = 'deleting' ORDER BY partition_id"
        ):
            lease_mono = r["delete_lease_mono_ns"]
            if lease_mono is not None and r["deadline_boot_id"] == boot:
                remaining_ns = lease_mono - mono_now
                remaining_s = max(remaining_ns / 1_000_000_000, 0.0)
                elapsed_s = max(config.DELETE_LEASE_SECONDS - remaining_s, 0.0)
                lease_iso = ms_to_iso(clock.mono_to_wall_ms(lease_mono))
            else:
                # 旧版墙钟行或宿主已重启：租约即刻到期
                remaining_s, elapsed_s = 0.0, config.DELETE_LEASE_SECONDS
                lease_iso = ms_to_iso(r["delete_lease_ms"])
            in_flight.append(InFlightDelete(
                partition_id=r["partition_id"],
                owner=r["delete_owner"],
                lease_expires_at=lease_iso,
                lease_total_seconds=config.DELETE_LEASE_SECONDS,
                lease_elapsed_seconds=round(elapsed_s, 3),
                lease_remaining_seconds=round(remaining_s, 3),
                attempts=r["delete_attempts"],
            ))

        failures: list[DeleteFailure] = []
        for r in conn.execute(
            "SELECT partition_id, delete_attempts, last_error, next_retry_mono_ns, "
            "deadline_boot_id FROM partitions "
            "WHERE last_error IS NOT NULL AND status != 'cleaned' "
            "ORDER BY partition_id"
        ):
            retry_mono = r["next_retry_mono_ns"]
            if retry_mono is not None and r["deadline_boot_id"] == boot:
                retry_remaining = max((retry_mono - mono_now) / 1_000_000_000, 0.0)
            else:
                retry_remaining = None  # 旧记录/宿主重启：立即可重试
            failures.append(DeleteFailure(
                partition_id=r["partition_id"],
                attempts=r["delete_attempts"],
                last_error=r["last_error"],
                retry_remaining_seconds=(
                    round(retry_remaining, 3) if retry_remaining is not None else None
                ),
            ))

        active_pres = conn.execute(
            "SELECT COUNT(*) AS n FROM preservations WHERE status = 'active'"
        ).fetchone()["n"]

    last_run = getattr(request.app.state, "last_cleanup_run", None)
    return CleanupStatus(
        process=config.OWNER,
        now=ms_to_iso(wall_now),
        clock=ClockInfo(
            boot_id=boot,
            monotonic_ns=mono_now,
            wall_offset_seconds=clock.wall_offset_seconds(),
            delete_lease_seconds=config.DELETE_LEASE_SECONDS,
            delete_retry_seconds=config.DELETE_RETRY_SECONDS,
        ),
        counts=counts,
        pending_expired_unpreserved=pending,
        in_flight=in_flight,
        failures=failures,
        active_preservations=active_pres,
        background_cleanup_enabled=config.BACKGROUND_CLEANUP_ENABLED,
        cleanup_interval_seconds=config.CLEANUP_INTERVAL_SECONDS,
        last_run=CleanupRunResult(**last_run) if last_run else None,
    )
