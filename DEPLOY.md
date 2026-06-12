# Linux 服务器部署指南

从零开始在 Linux 服务器（Ubuntu 22.04+ / CentOS 8+ 均可）把系统跑起来的完整步骤。
两条路径任选：**Docker 部署（推荐）** 或 **裸机部署**。

---

## 路径 A：Docker 部署（推荐）

### A1. 准备

```bash
# 安装 docker 与 compose 插件（已装跳过）
curl -fsSL https://get.docker.com | sh

# 拉取项目代码到服务器
git clone <你的仓库地址> /opt/trade-analyze   # 或 scp 上传
cd /opt/trade-analyze
```

### A2. 配置

```bash
cp .env.example .env
vi .env
# 必改项:
#   DB_PASSWORD=<改成强密码>
#   API_CORS_ORIGINS=<你前端的域名,如 https://your-frontend.com>
# 可选项:
#   HISTORY_START_DATE=2023-01-01   # 历史数据起点,3年足够回测
```

### A3. 启动 MySQL 与 API

```bash
docker compose up -d mysql        # 首次启动会自动执行 sql/schema.sql 建表
docker compose up -d api          # API 服务 :8000

# 验证
curl http://localhost:8000/health          # {"status":"ok","db":true}
curl http://localhost:8000/docs            # Swagger 文档(浏览器打开)
```

### A4. 首次数据初始化（全量历史，跑一次）

```bash
# 拉股票基础信息(几分钟)
docker compose run --rm engine python -m engine.jobs.fetch_basic
# 拉3年历史日线(全市场约5000只,免费源限速,预计 2-6 小时,放后台跑)
nohup docker compose run --rm engine python -m engine.jobs.fetch_daily --full \
  > /var/log/ta_init.log 2>&1 &
tail -f /var/log/ta_init.log
```

### A5. 跑一次选股验证全链路

```bash
docker compose run --rm engine python -m engine.jobs.run_selection
docker compose run --rm engine python -m engine.jobs.run_validation
curl "http://localhost:8000/api/picks/daily"   # 应返回选股结果
```

### A6. 配置每日定时（宿主机 crontab）

```bash
crontab -e
```

```cron
# 工作日 15:30 盘后:增量数据+选股+验证回填
30 15 * * 1-5 cd /opt/trade-analyze && docker compose run --rm engine python -m engine.jobs.daily_pipeline >> /var/log/ta_daily.log 2>&1
# 每周一 17:00 周度验证报告
0 17 * * 1 cd /opt/trade-analyze && docker compose run --rm engine python -m engine.jobs.weekly_report >> /var/log/ta_weekly.log 2>&1
```

---

## 路径 B：裸机部署

### B1. 环境

```bash
# Python 3.11
sudo apt install -y python3.11 python3.11-venv   # Ubuntu
# MySQL 8.0(已有现成实例可跳过,建库即可)
sudo apt install -y mysql-server-8.0

# 建库与账号
sudo mysql -e "
CREATE DATABASE trade_analyze DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'ta'@'localhost' IDENTIFIED BY '<强密码>';
GRANT ALL ON trade_analyze.* TO 'ta'@'localhost';
"
mysql -uta -p trade_analyze < sql/schema.sql
```

### B2. 安装项目

```bash
cd /opt/trade-analyze
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env && vi .env    # DB_USER=ta, DB_PASSWORD=..., 等
python -m common.init_db           # 二次确认建表(幂等)
```

### B3. API 用 systemd 常驻

`/etc/systemd/system/ta-api.service`:

```ini
[Unit]
Description=Trade Analyze API
After=network.target mysql.service

[Service]
WorkingDirectory=/opt/trade-analyze
ExecStart=/opt/trade-analyze/.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
EnvironmentFile=-/opt/trade-analyze/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now ta-api
curl http://localhost:8000/health
```

### B4. 数据初始化 + cron

同路径 A 的 A4/A6，把命令换成：

```cron
30 15 * * 1-5 cd /opt/trade-analyze && .venv/bin/python -m engine.jobs.daily_pipeline >> /var/log/ta_daily.log 2>&1
0 17 * * 1 cd /opt/trade-analyze && .venv/bin/python -m engine.jobs.weekly_report >> /var/log/ta_weekly.log 2>&1
```

---

## 前端对接

API 文档（自动生成）：`http://<服务器>:8000/docs`

| 接口 | 说明 |
|------|------|
| `GET /api/picks/daily?date=2026-06-12` | 某日 Top10 选股+因子得分+理由（date 省略=最新）|
| `GET /api/picks/dates` | 有选股记录的日期列表 |
| `GET /api/picks/{code}/detail` | 个股因子明细+历史选中记录 |
| `GET /api/validation/daily?date=` | 某选股日的 T+1/2/3 验证回填 |
| `GET /api/validation/summary` | 周度验证报告（命中率/对照组/edge）|
| `GET /api/market/status?date=` | 大盘开关状态 |
| `GET /api/params/versions` | 参数版本列表 |
| `GET /health` | 健康检查 |

**重要**：`/api/picks/daily` 返回的 `actionable=false` 表示当日大盘开关关闭
（「大盘下跌时所有系统失效」），前端应显著标注"今日空仓不操作"。
`tradable=false` 的个股表示决策日已涨停、尾盘买不进，仅供观察。

> **大盘开关当前默认停用**（`market_switch_enabled=False`）：大盘暴跌日 `actionable`
> 仍为 true，不会停止出票；但 `below_ma20`、指数涨跌幅照常计算并入库，
> `market_status.reason` 会标注"开关已停用(原本触发:…)"。要启用，把
> [common/params.py](common/params.py) 的 `market_switch_enabled` 改为 `True`
> （或在 param_config 新增版本），下次跑批生效。停用期间数据仍完整记录，
> 便于日后回测对比"有开关 vs 无开关"的效果。

## 运维要点

- **快照不可改**：`pick_snapshot` 只写不改，是验证可信的根基。任何人不要手工 UPDATE。
- **参数调优**：改阈值时在 `param_config` 表插入新版本（如 v2），改 `.env` 的
  `ACTIVE_PARAM_VERSION=v2` 重跑。历史快照保留旧版本号，报告按版本对比。
- **akshare 接口变动**：免费源字段偶尔变化，采集失败看 `logs/` 日志，
  事件类接口失败不阻断主流程（日线是核心）。
- **数据质量**：每天 `daily_pipeline` 日志里关注"硬过滤通过 N"数量，
  正常应在 50-200 区间（总结.txt 第149行）；异常偏离说明数据或参数有问题。
- **备份**：`mysqldump trade_analyze pick_snapshot pick_validation validation_report`
  这三张表是核心资产（验证凭证），建议每日备份。

## 验证闭环的使用方法（上线后第一个月）

1. 每天盘后看 `/api/picks/daily`，对照他的截图风格人工检查选出来的票像不像（图灵测试）
2. 每周一看 `/api/validation/summary`：关注 `edge_over_random` 是否 > 0
   （评分排序是否优于随机），而不是绝对命中率
3. 一个月后若 edge 稳定为正 → 开始用 71 条实盘样本做监督校准（P7）；
   若 edge≈0 → 优先调整核心买点因子（confirm_prev_high）的容差参数
