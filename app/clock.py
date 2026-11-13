"""时钟基座：墙钟（日历时间）与单调钟（实际经过时长）分离。

清理期限（删除租约 DELETE_LEASE_SECONDS、失败重试 DELETE_RETRY_SECONDS）
一律按 **CLOCK_MONOTONIC_RAW 单调钟的实际经过时长** 判定：

- 数据湖维护人员校准服务器时间（墙钟向前/向后跳）不改变任何清理期限；
- 单调钟是内核级时钟，同一宿主上的所有容器/进程读数同源，
  因此两个 API 进程共用同一判定，容器单独或全部重启都不会重计期限；
- 单调钟在宿主重启后归零。期限行同时记录取得该单调读数时的宿主 boot_id，
  boot_id 变化（宿主重启/卷迁移到另一宿主）后旧期限立即视为到期、可被接管，
  避免“归零后的单调读数”被误判为远未到期而永久卡住。

分区 expires_at 是业务日历时间，继续走 UTC 墙钟（见 timeutil.now_ms）。

墙钟偏移注入（CLOCK_OFFSET_FILE / CLOCK_OFFSET_SECONDS）仅用于演示与测试：
它只影响墙钟读数，不影响单调钟——用于“不修改宿主时间”的校时演示。
"""
from __future__ import annotations

import os
import time

from . import config


def mono_ns() -> int:
    """返回单调钟读数（纳秒）。优先 CLOCK_MONOTONIC_RAW：完全不受 NTP/校时影响。

    RAW 不可用时退回 time.monotonic_ns()（基于 CLOCK_MONOTONIC，仅受
    adjtime 类小幅频率修正影响，墙钟跳变（settimeofday）同样不会改变它）。
    """
    raw = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    if raw is not None:
        try:
            return time.clock_gettime(raw) * 1_000_000_000
        except OSError:
            pass
    return time.monotonic_ns()


def mono_seconds_after(seconds: float) -> int:
    """单调钟上“现在起经过 seconds 秒后”的纳秒时刻。"""
    return mono_ns() + int(seconds * 1_000_000_000)


def boot_id() -> str:
    """返回当前宿主启动周期标识（Linux /proc boot_id）。

    同一宿主上的所有容器挂载的是同一个 procfs，取值一致；宿主重启后改变。
    读取失败（非 Linux）返回固定哨兵：此时不再具备跨宿主周期判别能力，
    但同机内的单调判定仍然正确。
    """
    try:
        with open(config.BOOT_ID_PATH, "r", encoding="ascii") as fh:
            return fh.read().strip() or "unknown-boot"
    except OSError:
        return "unknown-boot"


def _injected_wall_offset_seconds() -> float:
    """读取墙钟偏移注入（秒），仅用于演示/测试，默认 0 即真实墙钟。"""
    raw = os.environ.get("CLOCK_OFFSET_SECONDS")
    if raw is not None and raw.strip():
        try:
            return float(raw)
        except ValueError:
            pass
    path = os.environ.get("CLOCK_OFFSET_FILE")
    if path:
        try:
            with open(path, "r", encoding="ascii") as fh:
                text = fh.read().strip()
            if text:
                return float(text)
        except (OSError, ValueError):
            pass
    return 0.0


def wall_now_ms() -> int:
    """墙钟（UTC epoch 毫秒）。偏移注入只改墙钟，绝不触碰单调钟。"""
    return int((time.time() + _injected_wall_offset_seconds()) * 1000)


def wall_offset_seconds() -> float:
    return _injected_wall_offset_seconds()


def mono_to_wall_ms(mono_ns_value: int | None) -> int | None:
    """把单调钟纳秒时刻折算成预估墙钟毫秒，仅供展示（如 delete_lease_expires_at）。"""
    if mono_ns_value is None:
        return None
    return wall_now_ms() + int((mono_ns_value - mono_ns()) / 1_000_000)
