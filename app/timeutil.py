"""时间工具：API 层使用 ISO 8601，存储层日历时间统一使用 UTC epoch 毫秒（整数）。

注意：这里的 now_ms 是**墙钟**，只用于业务日历时间（分区 expires_at、
created/cleaned 等 UTC 字段）。清理租约与重试期限使用单调钟
（app/clock.py），与墙钟校时完全解耦。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .clock import wall_now_ms


def now_ms() -> int:
    return wall_now_ms()


def to_utc(dt: datetime) -> datetime:
    """归一化为带时区的 UTC 时间；naive 输入按 UTC 解释。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    return int(to_utc(dt).timestamp() * 1000)


def ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_to_ms(text: str) -> int:
    """解析 JSONL 中的时间戳（兼容 'Z' 后缀与显式时区偏移）。"""
    return dt_to_ms(datetime.fromisoformat(text))
