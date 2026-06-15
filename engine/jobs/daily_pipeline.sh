#!/usr/bin/env bash
# 每日管线(分片版):并行分片拉日线 → 行业 → v1/v2选股 → 验证回填。
# 用法: bash engine/jobs/daily_pipeline.sh [分片数,默认4]
# cron: 30 18 * * 1-5 cd /root/trade-analyze && bash engine/jobs/daily_pipeline.sh >> logs/cron_daily.log 2>&1
set -u
cd "$(dirname "$0")/../.." || exit 1
PY=.venv/bin/python
SHARDS="${1:-4}"
TS() { date '+%Y-%m-%d %H:%M:%S'; }

echo "[$(TS)] ===== 每日管线(分片=$SHARDS)启动 ====="

# 1. 并行分片拉日线增量(baostock,每进程独立会话)。只 shard 0 顺带拉指数。
echo "[$(TS)] 1) 分片拉日线..."
pids=()
for i in $(seq 0 $((SHARDS-1))); do
    $PY -m engine.jobs.fetch_daily --shard "$i/$SHARDS" --source baostock \
        > "logs/daily_shard_${i}.log" 2>&1 &
    pids+=($!)
done
fail=0
for pid in "${pids[@]}"; do
    wait "$pid" || { echo "[$(TS)] 分片 pid=$pid 退出码非0"; fail=$((fail+1)); }
done
echo "[$(TS)] 分片完成(失败 $fail 个)。日线行数变化见 logs/daily_shard_*.log"

# 2. 行业分类(板块因子用,~30s)
echo "[$(TS)] 2) 同步行业分类..."
$PY -m engine.jobs.fetch_industry 2>&1 | tail -2

# 3. v1/v2 选股(对库内最新交易日)
echo "[$(TS)] 3) v1/v2 选股..."
$PY -m engine.jobs.run_selection --versions v1,v2 2>&1 | tail -8

# 4. 验证回填(对所有历史快照补 T+1/2/3)
echo "[$(TS)] 4) 验证回填..."
$PY -m engine.jobs.run_validation 2>&1 | tail -3

echo "[$(TS)] ===== 每日管线结束 ====="
