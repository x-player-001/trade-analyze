# A股自动选股分析系统 (trade-analyze)

量化复刻"独自前行"超短线筹码博弈选股逻辑的候选池生成与验证闭环系统。

## 定位

**候选池生成器 + 验证闭环**，不做自动交易。每日从全市场筛选 Top10 候选股入库，
前端通过 API 查询；每日自动用 T+1/2/3 实际走势验证历史选股并迭代阈值。

选股逻辑分析详见 [独自前行/总结.txt](独自前行/总结.txt)。

## 架构

```
┌──────────────────┐         ┌──────────────┐         ┌──────────────┐
│  跑批服务 engine    │  写入    │  MySQL 8.0    │  只读    │  API服务 api   │
│  数据采集/因子/选股   │ ──────→ │  共享数据库     │ ←────── │  FastAPI      │
│  /验证  (cron调度)  │         │              │         │  给已有前端     │
└──────────────────┘         └──────────────┘         └──────────────┘
         └──────────── 共享 common 包(模型层/配置/DB连接) ────────────┘
```

- **common**: SQLAlchemy 模型、配置、数据库连接，被两个服务共享
- **engine**: 数据采集、因子计算、硬过滤、评分选股、验证。cron 调度，只写库
- **api**: FastAPI，只读库，给已有前端提供 REST 查询接口

## 目录结构

```
trade-analyze/
├── common/              # 共享包
│   ├── config.py        # pydantic-settings 配置
│   ├── db.py            # SQLAlchemy 引擎/会话
│   ├── models.py        # ORM 模型
│   └── logging_conf.py  # 日志配置
├── engine/              # 跑批服务
│   ├── datasource/      # akshare 数据采集
│   ├── factors/         # 因子计算（硬过滤+软评分）
│   ├── selection/       # 选股引擎
│   ├── validation/      # 验证闭环
│   └── jobs/            # 可执行任务入口（被 cron 调用）
├── api/                 # API 服务
│   ├── main.py          # FastAPI 应用
│   ├── routers/         # 路由
│   └── schemas/         # Pydantic 响应模型
├── sql/                 # MySQL 建表脚本
├── scripts/             # 运维脚本
├── tests/               # 测试
├── docker-compose.yml   # MySQL + engine + api
├── pyproject.toml       # 依赖
└── .env.example         # 配置模板
```

## 快速开始

```bash
# 1. 复制配置
cp .env.example .env   # 编辑数据库连接等

# 2. 起 MySQL（docker）
docker compose up -d mysql

# 3. 安装依赖
pip install -e .

# 4. 建表
mysql -h127.0.0.1 -uroot -p trade_analyze < sql/schema.sql
# 或: python -m common.init_db

# 5. 拉取数据（首次全量）
python -m engine.jobs.fetch_basic        # 股票基础信息
python -m engine.jobs.fetch_daily --full # 全市场历史日线

# 6. 跑选股
python -m engine.jobs.run_selection      # 当日选股

# 7. 启动 API
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

## 每日定时任务（cron）

```cron
# 盘后 15:30 数据更新 + 选股
30 15 * * 1-5 cd /app && python -m engine.jobs.daily_pipeline >> /var/log/ta_daily.log 2>&1
# 盘后 16:00 验证回填（对 T-3 选股做 T+3 验证）
0 16 * * 1-5 cd /app && python -m engine.jobs.run_validation >> /var/log/ta_validation.log 2>&1
# 每周一 17:00 周度验证报告
0 17 * * 1 cd /app && python -m engine.jobs.weekly_report >> /var/log/ta_weekly.log 2>&1
```
