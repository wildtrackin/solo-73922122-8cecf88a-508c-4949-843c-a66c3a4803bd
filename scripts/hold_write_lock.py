#!/usr/bin/env python3
"""演示辅助：在容器内持有 SQLite 写锁 HOLD_SECONDS 秒后释放。

用于“锁等待”演示：另一个进程的清理请求会在 busy_timeout 上排队等待，
拿到写锁后才在锁内建立租约并继续，而不是报错。

用法（在 api-a 容器内）：
    python /app/scripts/hold_write_lock.py /data/datalake.db 8
"""
from __future__ import annotations

import sqlite3
import sys
import time


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/data/datalake.db"
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    print(f"[lock-holder] BEGIN IMMEDIATE on {db_path}, holding {hold}s ...", flush=True)
    conn.execute("BEGIN IMMEDIATE")
    print("[lock-holder] write lock acquired; other writers now block on busy_timeout", flush=True)
    time.sleep(hold)
    conn.execute("ROLLBACK")
    conn.close()
    print("[lock-holder] lock released", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
