#!/usr/bin/env bash
# 校时 / 锁等待 / 重启 三场景演示（全程不修改宿主时间）。
#
# 前置（全新环境效果最干净）：
#   docker compose -f docker-compose.yml -f docker-compose.demo.yml down -v
#   docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build
#
# 原理：删除租约（DELETE_LEASE_SECONDS）与失败重试（DELETE_RETRY_SECONDS）
# 按内核单调钟 CLOCK_MONOTONIC_RAW 的实际经过时长判定。向共享卷写
# clock-offset 只跳墙钟（UTC 展示字段），单调钟不变，所以校时不改变任何期限。
set -euo pipefail

A="${A:-http://localhost:8001}"
B="${B:-http://localhost:8002}"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
DC="$DC -f docker-compose.yml -f docker-compose.demo.yml"
PP='python3 -m json.tool'

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
# 写墙钟偏移（秒）到共享卷：两个 API 进程下一次读时间即生效，无需重启
offset() { $DC exec -T api-a sh -c "echo '$1' > /data/clock-offset"; }
# 在 api-a 容器后台占住 SQLite 写锁若干秒
hold_lock() { $DC exec -d api-a python /app/scripts/hold_write_lock.py /data/datalake.db "$1"; }
wait_healthy() {
  for u in "$A" "$B"; do
    for _ in $(seq 1 30); do curl -sf "$u/health" >/dev/null && break; sleep 1; done
  done
}

wait_healthy
step "0. 两进程健康检查：boot_id 相同（同一宿主启动周期），单调读数同源"
curl -s "$A/health" | $PP
curl -s "$B/health" | $PP

# ====================================== 预备：制造一条 delete_failed 记录
step "0b. 启动故障注入进程（api-fail），让 P0004 删除必失败 -> delete_failed"
$DC run --rm -d --name demo-fail api-fail
sleep 9
docker logs demo-fail 2>&1 | tail -3 || true
docker rm -f demo-fail >/dev/null 2>&1 || true
curl -s "$B/cleanup/status" | $PP   # failures 中应出现 P0004，带 retry_remaining_seconds

# ================================================== 场景一：校时不改变期限
step "1a. 向前校时 +3600s：未满租约时长的删除不得被另一进程接管"
offset 3600
curl -s "$B/health" | $PP   # now（墙钟 UTC）向前跳 1 小时；monotonic 基座不变
# P0001 命中慢删除注入：api-a 认领后停在 deleting，租约（10s）生效中
curl -s -X POST "$A/cleanup/run" >/dev/null &
sleep 2
echo "-- api-a 视角的在途删除：lease_remaining_seconds 仍 > 0（按实际经过时长）"
curl -s "$A/cleanup/status" | $PP
echo "-- api-b 同时尝试清理：P0001 不在 outcomes 中（租约未到期，拒绝接管）"
curl -s -X POST "$B/cleanup/run" | $PP
echo "-- P0001 仍是 deleting 且 owner 为 api-a"
curl -s "$A/partitions?status=deleting" | $PP
wait
echo ">> 结论：墙钟前跳 1 小时没有让 10s 租约提前到期"

step "1b. 向后校时 -3600s：已到重试时间的失败分区不得继续等待"
offset -3600   # 墙钟向后跳 1 小时
sleep 1
curl -s "$B/health" | $PP
echo "-- 墙钟倒退后，retry_remaining_seconds 仍按实际经过时长递减（不为大数、不重计）"
curl -s "$B/cleanup/status" | $PP
echo ">> 结论：重试期限由单调钟决定，墙钟倒退不会让已到重试时间的分区继续等待"
offset 0

# ================================================== 场景二：锁等待
step "2. 占住 SQLite 写锁 8s：清理请求在 busy_timeout 上等待，取锁后成功"
hold_lock 8
sleep 1
echo "-- 下面的请求应阻塞约 7s 后返回 200（而非 500/数据库锁定错误）"
time curl -s -X POST "$B/cleanup/run" | $PP
echo ">> 结论：认领事务排队等待写锁，并在取得写锁后的时刻才建立完整租约"

# ================================================== 场景三：重启不重计
step "3a. 单独重启 api-a：期限不重计，已清理分区不复活"
$DC restart api-a
wait_healthy
curl -s "$A/health" | $PP
curl -s "$A/cleanup/status" | $PP

step "3b. 两个容器全部重启：单调钟与 boot_id 同源，租约/重试期限连续"
$DC restart api-a api-b
wait_healthy
echo "-- 两个 health 的 boot_id 一致（同一宿主启动周期），单调读数同源"
curl -s "$A/health" | $PP
curl -s "$B/health" | $PP
echo "-- deleting/delete_failed 旧记录仍可被任一进程按原期限接管"
curl -s "$A/cleanup/status" | $PP
echo ">> 结论：同一宿主启动周期内，单独或全部重启容器都不会重计期限"

echo
echo "演示完成。重置环境：$DC down -v"
