#!/bin/bash
cd /root/trade-analyze
echo "[$(date)] === 开始拉取基础信息 ==="
.venv/bin/python -m engine.jobs.fetch_basic
echo "[$(date)] === 基础信息完成,开始拉取全量日线 ==="
.venv/bin/python -m engine.jobs.fetch_daily --full
echo "[$(date)] === 全部完成 ==="
