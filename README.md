# 轨道交通振动数据湖：到期清理 + 故障调查保全

FastAPI + SQLite（WAL）+ 本地 JSONL 分区的振动数据湖 API。支持：

- **首次载入**随附清单（车号 / 采样时段 / 到期时间）与 JSONL 波形数据；重启**不会**重新加入已清理分区；
- 调查人员按**车号 + 时间闭区间**保全所有重叠分区；任一分区已开始删除或已清理则**整次拒绝**；解除前不得删除；重叠保全**分别生效**；
- **后台清理**只处理已到期且无生效保全的分区，**逐分区认领删除资格**；清理期间其他分区照常可操作；
- **清理期限按单调钟实际经过时长判定**：删除租约与失败重试不受墙钟校时（前后跳）影响；同一宿主启动周期内容器单独/全部重启不重计期限，宿主重启后旧期限立即可接管；
- **两个 API 进程**共享同一 SQLite 与分区目录，保全与删除资格有**确定先后**；删除失败**绝不报已回收**；中断后重启**继续未完成删除**；索引与文件**最终一致**；
- 按车号、时段查询**尚存**的采样时间与值；另有保全与清理状态接口。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- api-a: http://localhost:8001/docs
- api-b: http://localhost:8002/docs

两个容器各运行一个 API 进程，挂载同一数据卷 `lake-data`（SQLite 库 + JSONL 分区文件）。
在任一进程的 `/docs` 里即可完成保全、清理、波形查询演示；也可以直接跑：

```bash
./scripts/demo.sh                 # 完整演示：保全 -> 清理 -> 查询 -> 解除 -> 回收
```

### 演示流程（/docs 内手动操作）

1. `GET /partitions` —— 8 个种子分区，P0001/P0002/P0004/P0006/P0008 已到期；
2. `GET /samples?vehicle_id=T203&start=...00:30:50Z&end=...00:31:10Z` —— 看故障冲击段波形；
3. `POST /preservations` 车号 `T203`、区间 `[2026-10-03T00:30:00Z, 2026-10-03T00:35:00Z]` —— 覆盖 P0006；
4. `POST /cleanup/run` —— 回收其余到期分区，P0006 因保全跳过；
5. 对 T101 `[00:10, 00:20]` 再发 `POST /preservations` —— 返回 **409**（P0001 已清理，整次拒绝）；
6. `POST /preservations/{id}/release` 解除后再次 `POST /cleanup/run` —— P0006 被回收；
7. `GET /cleanup/status` —— 状态统计、在途删除、失败记录。

### 演示删除失败语义（可选）

```bash
DELETE_FAIL_PARTITIONS=P0004 docker compose up --build
# P0004 进入 delete_failed（带错误信息），绝不会被报为已回收；
# 去掉该环境变量重启后，后台循环自动重试并成功回收。
```

### 验证重启幂等

```bash
docker compose restart api-a
# 已清理分区保持 cleaned，文件不会被种子数据重新拷回。
```

## 校时 / 锁等待 / 重启演示（无需修改宿主时间）

清理租约 `DELETE_LEASE_SECONDS` 与失败重试 `DELETE_RETRY_SECONDS` 一律按内核
**单调钟 `CLOCK_MONOTONIC_RAW` 的实际经过时长**判定（实现见 `app/clock.py`）：
向共享卷写偏移文件只跳墙钟（UTC 展示字段），单调钟不变，因此校时不改变任何期限。

```bash
# 演示覆盖配置：期限调短（租约 10s / 重试 6s），打开慢删除注入与偏移文件
docker compose -f docker-compose.yml -f docker-compose.demo.yml down -v
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build
# 另开终端：
./scripts/demo_clock.sh
```

脚本覆盖三个场景，全部可在 `/docs`（http://localhost:8001/docs、
http://localhost:8002/docs）手工复现：

1. **校时**：写 `/data/clock-offset` 模拟墙钟前跳 1 小时——慢删除中的 P0001
   租约未满，另一进程不得接管；再后跳 1 小时——`delete_failed` 分区的重试倒计时
   仍按实际时长递减，已到重试时间不会继续等待（`GET /cleanup/status` 的
   `lease_remaining_seconds` / `retry_remaining_seconds` 可直接观察）；
2. **锁等待**：`scripts/hold_write_lock.py` 在容器内占住 SQLite 写锁，
   `POST /cleanup/run` 在 `busy_timeout` 上排队等待、取锁后成功（不报错）；
   租约时刻在**取得写锁之后**才建立，锁等待耗时不计入租约；
3. **重启**：`restart api-a` 与 `restart api-a api-b` 后，两个进程
   `GET /health` 的 `boot_id` 一致（同一宿主启动周期），单调读数同源，
   已有的 `deleting` / `delete_failed` 记录按原期限继续被接管，不重计。

故障注入进程（删除 P0004 必失败）通过 profile 启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml --profile faildemo run --rm api-fail
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/partitions` | 分区清单（状态、到期、生效保全数、文件是否在盘） |
| GET | `/samples` | 按车号 + 时间闭区间查询尚存样本（仅 active 分区） |
| POST | `/preservations` | 保全所有重叠分区（原子；409/404 语义见下） |
| GET | `/preservations` / `/{id}` | 保全列表 / 详情 |
| POST | `/preservations/{id}/release` | 解除保全（幂等） |
| POST | `/cleanup/run` | 立即执行一轮清理 |
| GET | `/cleanup/status` | 清理状态总览 |
| GET | `/health`, `/meta/info` | 健康与进程信息 |

**保全拒绝语义**：无重叠分区 → `404`；任一重叠分区为 `deleting` / `delete_failed` / `cleaned` → `409` 整次拒绝（不产生任何保全记录）。

## 设计要点

### 分区与索引

- 每个分区 = 一个 JSONL 文件（每行一个样本：`ts, vehicle_id, ax, ay, az`）+ 一行 `partitions` 索引；
- 分区状态机：`active → deleting → cleaned`，失败分支 `deleting → delete_failed →（重试）deleting …`；
- `cleaned` 行作为**墓碑**永久保留：重启时种子载入以 `partition_id` 主键 `INSERT OR IGNORE`，已清理分区既不重新插行、也不回拷文件。

### 两个进程间“保全 vs 删除资格”的确定先后

所有“检查 + 变更”的关键段（保全创建、删除资格认领、种子载入、reconcile）都包在
`BEGIN IMMEDIATE` 事务里。SQLite 写锁把这些关键段**全序化**：

- 保全事务先提交 → 清理认领时的 `NOT EXISTS(生效保全)` 看到它，跳过该分区；
- 清理认领先提交（`active→deleting`）→ 保全事务看到非 `active` 状态，整次 409；
- 认领用条件 `UPDATE` + 行数校验，两个清理进程不会同时拿到同一分区。

清理认领后**先释放数据库锁再删文件**：清理某分区期间，其他分区的查询、保全不受影响。

### 崩溃恢复与最终一致

- 认领带**租约**（`DELETE_LEASE_SECONDS`，默认 30s）：持有进程崩溃后，租约到期才允许其他进程接管，避免双删；
- 租约与失败重试（`DELETE_RETRY_SECONDS`）的期限存的是**单调钟纳秒时刻 + 宿主 boot_id**
  （`delete_lease_mono_ns` / `next_retry_mono_ns` / `deadline_boot_id` 三列）：
  - 期限按 `CLOCK_MONOTONIC_RAW` 的实际经过时长比较，墙钟 `settimeofday`/NTP 校时前后跳都不改变期限；
  - 单调钟是内核级时钟，同宿主所有容器/进程读数同源：两个 API 进程共用判定，容器单独或全部重启不重计；
  - 单调钟在宿主重启后归零，因此同时记录 `/proc/sys/kernel/random/boot_id`：boot_id 变化（宿主重启、卷迁移到别的宿主）后旧期限立即视为到期，防止归零读数把分区永久锁死；
  - 旧版仅含墙钟期限列的数据库在启动时**在线迁移**（`ALTER TABLE ... ADD COLUMN`，幂等）；
    已有的 `deleting` / `delete_failed` 行新列为 NULL，认领条件将其视为已到期，下一轮即可恢复；
- 删文件是幂等的：文件已不存在视为删除成功（覆盖“文件已删、状态未落库”的崩溃窗口），随后置 `cleaned`；
- 删除失败置 `delete_failed` 并记录错误，后续 pass 按单调钟重试间隔自动重试，**绝不误报已回收**；
- 认领事务在 `BEGIN IMMEDIATE` 取得写锁**之后**才读取单调时刻并写入完整租约，锁等待（`busy_timeout` 30s）耗时不计入租约；
- 文件删除在所有数据库事务之外执行（**删除期间不持写锁**）；每次启动执行 `reconcile`：删除“无索引行或行已 cleaned”的孤儿文件，索引与磁盘最终一致。

### 时间字段约定

- 分区 `expires_at`、`created_at`、`cleaned_at`、`started_at` 等业务时间继续是 **UTC 墙钟 ISO 8601**（存储为 epoch 毫秒），分区到期仍按 UTC 日历时间判断；
- `delete_lease_expires_at` 等租约展示字段由单调时刻折算为预估墙钟，仅用于展示；真正的期限判定只认真单调列与 boot_id；
- `GET /cleanup/status` 额外给出 `lease_elapsed_seconds` / `lease_remaining_seconds` /
  `retry_remaining_seconds`（实际经过/剩余秒数）与 `clock`（boot_id、monotonic_ns、墙钟偏移）。

### 种子载入

`seed/manifest.json` + `seed/partitions/*.jsonl` 随镜像分发（`scripts/generate_seed.py` 可重新生成）。
每次启动幂等执行：新分区插行 + 同事务拷文件；已存在（含已清理）分区跳过。

## 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=./var .venv/bin/uvicorn app.main:app --port 8000   # 单进程
# 两个进程（模拟 compose 拓扑，共享 ./var）：
DATA_DIR=./var PROCESS_NAME=a .venv/bin/uvicorn app.main:app --port 8001 &
DATA_DIR=./var PROCESS_NAME=b .venv/bin/uvicorn app.main:app --port 8002 &
```

## 测试

```bash
python3 tests/core_clock_test.py   # 仅标准库：单调钟/租约/重试/锁等待/迁移 19 项核心判定
python3 tests/e2e_test.py          # 真实拉起双进程，11 个场景的端到端语义（含校时/重启/锁等待）
```

场景：重启幂等 / 崩溃续删 / 删除失败不误报+重试 / 双进程保全-清理竞态（6 轮不变量）/
reconcile 孤儿文件 / 保全闭区间与原子拒绝与独立生效 / 清理期间可操作性 /
**墙钟前后跳不改变租约与重试期限** / **重启不重计期限（含执行进程退出后接管）** /
**写锁等待不报错** / **旧版墙钟记录迁移后可恢复**。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `/data`（容器） | SQLite 与分区文件目录 |
| `SEED_DIR` | `/app/seed`（容器） | 种子清单与数据 |
| `CLEANUP_INTERVAL_SECONDS` | `15`（compose 中为 `120`） | 后台清理周期；首轮在启动一个周期后执行，给演示留出保全时间窗 |
| `DELETE_LEASE_SECONDS` | `30` | 删除资格租约（秒，按单调钟实际经过时长判定） |
| `DELETE_RETRY_SECONDS` | `30` | 删除失败后的重试间隔（秒，单调钟；防止持续失败分区饿死其他分区） |
| `CLEANUP_MAX_PER_PASS` | `100` | 单轮清理分区上限 |
| `BACKGROUND_CLEANUP_ENABLED` | `true` | 后台循环开关（手动 `/cleanup/run` 不受影响） |
| `DELETE_FAIL_PARTITIONS` | 空 | 故障注入：指定分区删除必失败（演示/测试用） |
| `SLOW_DELETE_PARTITIONS` / `SLOW_DELETE_SECONDS` | 空 / `8` | 慢删除注入：指定分区删文件前睡眠若干秒（事务外，不持写锁），制造租约观察窗口 |
| `CLOCK_OFFSET_FILE` | `$DATA_DIR/clock-offset` | 墙钟偏移注入文件（秒）：只改墙钟不动单调钟，用于无侵入校时演示 |
| `CLOCK_OFFSET_SECONDS` | 空 | 直接以环境变量注入墙钟偏移（优先级低于文件） |
| `BOOT_ID_PATH` | `/proc/sys/kernel/random/boot_id` | 宿主启动周期标识来源 |
