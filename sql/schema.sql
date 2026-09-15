-- A股自动选股分析系统 MySQL 8.0 建表脚本
-- 字符集统一 utf8mb4。与 common/models.py 保持一致。
-- 用法: mysql -uroot -p trade_analyze < sql/schema.sql

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS trade_analyze
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE trade_analyze;

-- ============================ 基础数据 ============================
CREATE TABLE IF NOT EXISTS stock_basic (
  code            VARCHAR(10)  NOT NULL COMMENT '6位代码',
  name            VARCHAR(32)  NOT NULL COMMENT '股票名称',
  board           VARCHAR(8)   NOT NULL COMMENT '板块 main/gem/star/bse',
  industry        VARCHAR(64)           COMMENT '所属行业',
  list_date       DATE                  COMMENT '上市日期',
  price_limit_pct FLOAT        NOT NULL DEFAULT 10.0 COMMENT '涨跌幅限制(%)',
  is_st           TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '是否ST/退市风险',
  circ_mv         FLOAT                 COMMENT '流通市值(亿元)',
  is_active       TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '是否仍在交易',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (code),
  KEY idx_board (board),
  KEY idx_st (is_st)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票基础信息';

CREATE TABLE IF NOT EXISTS daily_quote (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  code        VARCHAR(10)  NOT NULL,
  trade_date  DATE         NOT NULL,
  open        DECIMAL(12,3) NOT NULL COMMENT '后复权开盘',
  high        DECIMAL(12,3) NOT NULL COMMENT '后复权最高',
  low         DECIMAL(12,3) NOT NULL COMMENT '后复权最低',
  close       DECIMAL(12,3) NOT NULL COMMENT '后复权收盘',
  raw_open    DECIMAL(12,3)          COMMENT '原始开盘',
  raw_high    DECIMAL(12,3)          COMMENT '原始最高',
  raw_low     DECIMAL(12,3)          COMMENT '原始最低',
  raw_close   DECIMAL(12,3)          COMMENT '原始收盘',
  volume      DECIMAL(20,2)          COMMENT '成交量(原始,2026-06-15前单位为股)',
  volume_std  DECIMAL(20,2)          COMMENT '成交量(手,已归一化;因子只读此列)',
  amount      DECIMAL(20,2)          COMMENT '成交额(元)',
  amplitude   FLOAT                  COMMENT '振幅(%)',
  pct_chg     FLOAT                  COMMENT '涨跌幅(%)',
  change_amt  DECIMAL(12,3)          COMMENT '涨跌额(原始)',
  turnover    FLOAT                  COMMENT '换手率(%)',
  PRIMARY KEY (id),
  UNIQUE KEY uq_daily_code_date (code, trade_date),
  KEY idx_daily_code (code),
  KEY idx_daily_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日线行情(后复权+原始OHLC)';

CREATE TABLE IF NOT EXISTS index_daily (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  index_code  VARCHAR(10)  NOT NULL,
  trade_date  DATE         NOT NULL,
  open        DECIMAL(14,3) NOT NULL,
  high        DECIMAL(14,3) NOT NULL,
  low         DECIMAL(14,3) NOT NULL,
  close       DECIMAL(14,3) NOT NULL,
  pct_chg     FLOAT,
  PRIMARY KEY (id),
  UNIQUE KEY uq_index_code_date (index_code, trade_date),
  KEY idx_index_code (index_code),
  KEY idx_index_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数日线';

CREATE TABLE IF NOT EXISTS market_status (
  trade_date   DATE        NOT NULL,
  sh_pct_chg   FLOAT                COMMENT '上证涨跌幅%',
  gem_pct_chg  FLOAT                COMMENT '创业板涨跌幅%',
  below_ma20   TINYINT(1)  NOT NULL DEFAULT 0 COMMENT '上证跌破20日线',
  is_open      TINYINT(1)  NOT NULL DEFAULT 1 COMMENT '是否允许出票',
  reason       VARCHAR(255)         COMMENT '关闭原因',
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日大盘开关';

-- ============================ 因子与选股 ============================
CREATE TABLE IF NOT EXISTS stock_factor (
  id                          BIGINT      NOT NULL AUTO_INCREMENT,
  code                        VARCHAR(10) NOT NULL,
  trade_date                  DATE        NOT NULL,
  passed_hard_filter          TINYINT(1)  NOT NULL DEFAULT 0,
  reject_reasons              VARCHAR(255),
  in_pullback_window          TINYINT(1)  NOT NULL DEFAULT 0,
  score_low_position          FLOAT       NOT NULL DEFAULT 0,
  score_shrink_consolidation  FLOAT       NOT NULL DEFAULT 0,
  score_probe_pullback        FLOAT       NOT NULL DEFAULT 0,
  score_small_yang            FLOAT       NOT NULL DEFAULT 0,
  score_confirm_prev_high     FLOAT       NOT NULL DEFAULT 0,
  score_pullback_ma5          FLOAT       NOT NULL DEFAULT 0,
  score_healthy_turnover      FLOAT       NOT NULL DEFAULT 0,
  score_strong_rally          FLOAT       NOT NULL DEFAULT 0,
  score_chip_concentration    FLOAT       NOT NULL DEFAULT 0,
  score_sector_strength       FLOAT       NOT NULL DEFAULT 0,
  total_score                 FLOAT       NOT NULL DEFAULT 0,
  param_version               VARCHAR(16) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_factor_code_date_ver (code, trade_date, param_version),
  KEY idx_factor_code (code),
  KEY idx_factor_date (trade_date),
  KEY idx_factor_passed (passed_hard_filter),
  KEY idx_factor_score (total_score)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日因子快照';

CREATE TABLE IF NOT EXISTS pick_snapshot (
  id                 BIGINT      NOT NULL AUTO_INCREMENT,
  trade_date         DATE        NOT NULL,
  code               VARCHAR(10) NOT NULL,
  name               VARCHAR(32) NOT NULL,
  board_group        VARCHAR(8)  NOT NULL DEFAULT 'main' COMMENT '板块分组 main/other',
  `rank`             INT         NOT NULL COMMENT '组内排名,1最高',
  total_score        FLOAT       NOT NULL,
  factor_scores_json TEXT        COMMENT '因子得分明细JSON',
  reasons            VARCHAR(512),
  decision_close     DECIMAL(12,3) NOT NULL COMMENT '后复权收盘',
  decision_raw_close DECIMAL(12,3)          COMMENT '原始收盘',
  limit_up           TINYINT(1)  NOT NULL DEFAULT 0,
  tradable           TINYINT(1)  NOT NULL DEFAULT 1,
  param_version      VARCHAR(16) NOT NULL,
  created_at         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_pick_date_code_ver (trade_date, code, param_version),
  KEY idx_pick_date (trade_date),
  KEY idx_pick_code (code),
  KEY idx_pick_group (board_group)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日选股快照(只写不改)';

CREATE TABLE IF NOT EXISTS pick_validation (
  id            BIGINT      NOT NULL AUTO_INCREMENT,
  snapshot_id   BIGINT      NOT NULL,
  trade_date    DATE        NOT NULL COMMENT '选股日',
  code          VARCHAR(10) NOT NULL,
  t1_high_ret   FLOAT,
  t2_high_ret   FLOAT,
  t3_high_ret   FLOAT,
  t1_close_ret  FLOAT,
  t2_close_ret  FLOAT,
  t3_close_ret  FLOAT,
  hit_7pct      TINYINT(1),
  max_drawdown  FLOAT,
  is_complete   TINYINT(1)  NOT NULL DEFAULT 0,
  created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_validation_snapshot (snapshot_id),
  KEY idx_val_date (trade_date),
  KEY idx_val_code (code),
  KEY idx_val_hit (hit_7pct),
  KEY idx_val_complete (is_complete)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='选股验证结果';

CREATE TABLE IF NOT EXISTS validation_report (
  id                        BIGINT      NOT NULL AUTO_INCREMENT,
  period_start              DATE        NOT NULL,
  period_end                DATE        NOT NULL,
  param_version             VARCHAR(16) NOT NULL,
  pick_count                INT         NOT NULL DEFAULT 0,
  tradable_count            INT         NOT NULL DEFAULT 0,
  hit_rate_7pct             FLOAT       COMMENT '3日命中7%+比例',
  avg_t3_high_ret           FLOAT       COMMENT '平均T3最高涨幅',
  avg_profit_loss_ratio     FLOAT       COMMENT '平均盈亏比',
  benchmark_market_ret      FLOAT       COMMENT '同期市场平均',
  benchmark_random_hit_rate FLOAT       COMMENT '随机组命中率',
  edge_over_random          FLOAT,
  detail_json               TEXT,
  created_at                DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at                DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_report_period (period_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='周度验证报告';

-- ============================ 参数与样本 ============================
CREATE TABLE IF NOT EXISTS param_config (
  version      VARCHAR(16) NOT NULL,
  description  VARCHAR(255),
  config_json  TEXT        NOT NULL,
  is_active    TINYINT(1)  NOT NULL DEFAULT 0,
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (version),
  KEY idx_param_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='因子参数版本';

CREATE TABLE IF NOT EXISTS benchmark_sample (
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  source_id    VARCHAR(64) NOT NULL COMMENT '截图目录id',
  post_date    DATE        COMMENT '发帖日期',
  code         VARCHAR(10) COMMENT '提取的股票代码',
  name         VARCHAR(32),
  buy_date     DATE        COMMENT '推断买入日',
  note         VARCHAR(255),
  system_score FLOAT       COMMENT '系统打分(回填)',
  system_rank  INT         COMMENT '系统排名(回填)',
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_benchmark_source (source_id),
  KEY idx_benchmark_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='实盘标注样本';

-- ============================ 监控池 ============================
CREATE TABLE IF NOT EXISTS watch_pool (
  id             BIGINT      NOT NULL AUTO_INCREMENT,
  code           VARCHAR(10) NOT NULL,
  name           VARCHAR(32) NOT NULL DEFAULT '',
  board_group    VARCHAR(8)  NOT NULL DEFAULT 'main',
  trigger_date   DATE        NOT NULL COMMENT '首板日',
  confirm_date   DATE        COMMENT '入池可见日(=首板日)',
  trigger_close  DECIMAL(12,3) COMMENT '首板日原始收盘',
  trigger_pct    FLOAT       COMMENT '首板日涨幅%',
  trigger_amount DECIMAL(20,2) COMMENT '首板日成交额',
  gain_from_low  FLOAT       NOT NULL COMMENT '距120日低点涨幅%',
  trigger_vol_ratio FLOAT    COMMENT '首板日放量倍数(vs前20日均额)',
  flat_days      INT         COMMENT '低位横盘天数(仅记录,IC≈0不计分)',
  entry_score    FLOAT       COMMENT '入池评分0~1(无未来函数)',
  entry_score_json TEXT      COMMENT '入池分项JSON',
  consec_boards  INT         COMMENT '首板起连板数(1=孤板)',
  entry_type     VARCHAR(12) COMMENT 'solo/consecutive',
  broke_open_date DATE       COMMENT '跌破首板开盘价日(标记非删除)',
  broke_open_days INT        COMMENT '距首板天数',
  live_score     FLOAT       COMMENT '跟踪评分0~1(含连板/跌破)',
  status         VARCHAR(12) NOT NULL DEFAULT 'watching' COMMENT 'watching/hit/expired',
  hit_date       DATE,
  hit_days       INT         COMMENT '距首板交易日数',
  expire_date    DATE        COMMENT '30交易日窗口末日',
  created_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_watch_code_trigger (code, trigger_date),
  KEY idx_watch_code (code),
  KEY idx_watch_trigger (trigger_date),
  KEY idx_watch_confirm (confirm_date),
  KEY idx_watch_status (status),
  KEY idx_watch_consec (consec_boards),
  KEY idx_watch_entry (entry_type),
  KEY idx_watch_entry_score (entry_score),
  KEY idx_watch_live_score (live_score)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='低位首板监控池(只写不改)';

CREATE TABLE IF NOT EXISTS watch_pool_daily (
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  pool_id      BIGINT      NOT NULL,
  code         VARCHAR(10) NOT NULL,
  trade_date   DATE        NOT NULL,
  days_since   INT         COMMENT '距首板第N个交易日',
  close        DECIMAL(12,3) COMMENT '原始收盘',
  pct_chg      FLOAT,
  ret_since    FLOAT       COMMENT '相对首板收盘%',
  amount_ratio FLOAT       COMMENT '成交额/首板日成交额',
  is_limit_up  TINYINT(1)  NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uq_wpd_pool_date (pool_id, trade_date),
  KEY idx_wpd_code (code),
  KEY idx_wpd_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='监控池每日量价跟踪';

-- ============================ 低位放量池（独立表：标签=收益率）============================
CREATE TABLE IF NOT EXISTS watch_lowvol (
  id             BIGINT      NOT NULL AUTO_INCREMENT,
  code           VARCHAR(10) NOT NULL,
  name           VARCHAR(32) NOT NULL DEFAULT '',
  board_group    VARCHAR(8)  NOT NULL DEFAULT 'main',
  trigger_date   DATE        NOT NULL COMMENT '放量日',
  trigger_close  DECIMAL(12,3) COMMENT '放量日原始收盘',
  trigger_pct    FLOAT       COMMENT '放量日涨跌幅%',
  trigger_amount DECIMAL(20,2) COMMENT '放量日成交额',
  gain_from_low  FLOAT       NOT NULL COMMENT '距120日低点涨幅%(实测单调)',
  vol_ratio      FLOAT       COMMENT '放量倍数(实测倒U型,2-3x最优)',
  limit_up       TINYINT(1)  NOT NULL DEFAULT 0 COMMENT '触发日涨停(难买入)',
  first_board    TINYINT(1)  NOT NULL DEFAULT 0 COMMENT '前60日无涨停',
  entry_score    FLOAT       COMMENT '入池评分0~1',
  entry_score_json TEXT      COMMENT '评分分项JSON',
  ret1           FLOAT       COMMENT 'T+1收益%',
  ret3           FLOAT       COMMENT 'T+3收益%',
  ret5           FLOAT       COMMENT 'T+5收益%',
  ret10          FLOAT       COMMENT 'T+10收益%',
  excess5        FLOAT       COMMENT 'T+5相对全市场超额%',
  max_ret10      FLOAT       COMMENT '10日内最高收益%',
  max_dd10       FLOAT       COMMENT '10日内最大回撤%',
  status         VARCHAR(12) NOT NULL DEFAULT 'watching' COMMENT 'watching/settled',
  settle_date    DATE        COMMENT 'T+10对应日期',
  created_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_lowvol_code_trigger (code, trigger_date),
  KEY idx_lowvol_code (code),
  KEY idx_lowvol_trigger (trigger_date),
  KEY idx_lowvol_status (status),
  KEY idx_lowvol_score (entry_score),
  KEY idx_lowvol_excess (excess5),
  KEY idx_lowvol_fb (first_board)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='低位放量池(标签=T+N收益率)';

CREATE TABLE IF NOT EXISTS watch_lowvol_daily (
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  pool_id      BIGINT      NOT NULL,
  code         VARCHAR(10) NOT NULL,
  trade_date   DATE        NOT NULL,
  days_since   INT         COMMENT '距触发日第N个交易日',
  close        DECIMAL(12,3) COMMENT '原始收盘',
  pct_chg      FLOAT,
  ret_since    FLOAT       COMMENT '相对触发日收盘%',
  amount_ratio FLOAT       COMMENT '额比vs触发日',
  PRIMARY KEY (id),
  UNIQUE KEY uq_lvd_pool_date (pool_id, trade_date),
  KEY idx_lvd_code (code),
  KEY idx_lvd_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='低位放量池每日跟踪';

-- ---------------------------------------------------------------------------
-- 突破回踩池：底部横盘 → 涨停启动 → 回调至MA10附近。触发日=回踩日，非涨停日。
-- 与 watch_pool / watch_lowvol 独立成表——三者标签与触发时机各不相同，
-- 共表会被迫共用结算逻辑（曾致 lowvol 命中率失真至 17.97%）。
-- 注意 COLLATE 必须显式写死与其它表一致，否则 JOIN 报 collation 混用错误。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watch_pullback (
  id                 BIGINT      NOT NULL AUTO_INCREMENT,
  code               VARCHAR(10) NOT NULL,
  name               VARCHAR(32) NOT NULL DEFAULT '',
  board_group        VARCHAR(8)  NOT NULL DEFAULT 'main',
  -- 阶段一：启动
  breakout_date      DATE        NOT NULL COMMENT '启动涨停日',
  breakout_close     DECIMAL(12,3) COMMENT '启动日原始收盘',
  breakout_open      DECIMAL(12,3) COMMENT '启动日原始开盘',
  breakout_pct       FLOAT       COMMENT '启动日涨幅%',
  breakout_amount    DECIMAL(20,2) COMMENT '启动日成交额',
  gain_from_low      FLOAT       COMMENT '距120日低点涨幅%',
  breakout_vol_ratio FLOAT       COMMENT '启动日放量倍数(vs前20日均额)',
  flat_days          INT         COMMENT '启动前横盘天数(仅记录,IC≈0不计分)',
  breakout_boards    INT         COMMENT '启动段内涨停板数(观测字段)',
  entry_kind         VARCHAR(12) NOT NULL DEFAULT 'streak' COMMENT 'limitup单根涨停/streak多根阳线',
  streak_days        INT         COMMENT '启动段阳线根数(不含中间十字星)',
  streak_gain        FLOAT       COMMENT '启动段累计涨幅%(段首开→段末收)',
  streak_end_date    DATE        COMMENT '启动段末日(回踩窗口起算点)',
  first_board        TINYINT(1)  COMMENT '启动段前60日无涨停(弱代理,默认不筛)',
  -- 启动前20日 pct_chg 标准差 = 「底部横盘」的直接度量，入池主筛选维度。
  -- 实测区分度是 first_board 的 3.6 倍：<1.5 T+10 +0.714% → >=4.0 -0.739%
  vol20              FLOAT       COMMENT '启动前20日涨跌幅标准差(越小越安静)',
  -- 阶段二：回踩(=入池日)
  -- 状态机下 armed/missed/failed/expired 从未回踩，这两列为 NULL
  pullback_date      DATE        COMMENT '回踩确认日=报警日(未触发则空)',
  pullback_close     DECIMAL(12,3) COMMENT '回踩日原始收盘(未触发则空)',
  drawdown           FLOAT       COMMENT '相对启动日收盘%(连板时可为正)',
  peak_close         DECIMAL(12,3) COMMENT '启动段最高收盘',
  drawdown_from_peak FLOAT       COMMENT '相对启动段最高收盘%(回调深度,入池判据)',
  dist_ma5           FLOAT       COMMENT '距MA5 %',
  dist_ma10          FLOAT       COMMENT '距MA10 %(触发判据±3%)',
  dist_ma20          FLOAT       COMMENT '距MA20 %',
  pullback_days      INT         COMMENT '启动→回踩交易日数',
  pullback_vol_ratio FLOAT       COMMENT '回踩日额比vs启动日',
  -- 跟踪与结算
  -- 状态机：armed=待回踩(未报警) / triggered=已报警 / missed=第二波已启动作废
  -- / failed=跌破段首开盘作废 / expired=未等到回踩 / hit,settled=触发后结算
  status             VARCHAR(12) NOT NULL DEFAULT 'armed' COMMENT '状态机,见 models.py',
  armed_date         DATE        COMMENT '登记待回踩日(段末次日)',
  peak_broken_date   DATE        COMMENT '收盘突破启动段峰值日(=第二波已启动)',
  hit_date           DATE,
  hit_days           INT         COMMENT '距回踩日交易日数',
  expire_date        DATE        COMMENT '10交易日窗口末日(未走满留NULL)',
  broke_date         DATE        COMMENT '跌破启动日开盘价日(标记非删除)',
  broke_days         INT         COMMENT '距回踩日天数',
  ret1               FLOAT       COMMENT '回踩后T+1收益%',
  ret3               FLOAT       COMMENT '回踩后T+3收益%',
  ret5               FLOAT       COMMENT '回踩后T+5收益%',
  ret10              FLOAT       COMMENT '回踩后T+10收益%',
  max_ret            FLOAT       COMMENT '窗口内最大收益%',
  created_at         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_wpb_code_breakout (code, breakout_date),
  KEY idx_wpb_code (code),
  KEY idx_wpb_pullback (pullback_date),
  KEY idx_wpb_breakout (breakout_date),
  KEY idx_wpb_status (status),
  KEY idx_wpb_kind (entry_kind),
  KEY idx_wpb_armed (armed_date),
  KEY idx_wpb_streak (streak_days),
  KEY idx_wpb_vol20 (vol20)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='突破回踩池(触发=回踩日)';

CREATE TABLE IF NOT EXISTS watch_pullback_daily (
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  pool_id      BIGINT      NOT NULL,
  code         VARCHAR(10) NOT NULL,
  trade_date   DATE        NOT NULL,
  days_since   INT         COMMENT '距回踩日第N个交易日',
  close        DECIMAL(12,3) COMMENT '原始收盘',
  pct_chg      FLOAT,
  ret_since    FLOAT       COMMENT '相对回踩日收盘%',
  amount_ratio FLOAT       COMMENT '额比vs回踩日',
  dist_ma10    FLOAT       COMMENT '距MA10 %',
  is_limit_up  TINYINT(1)  NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uq_wpbd_pool_date (pool_id, trade_date),
  KEY idx_wpbd_code (code),
  KEY idx_wpbd_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='突破回踩池每日跟踪';

-- ---------------------------------------------------------------------------
-- 收藏：本项目唯一一张【API 可写】的表。
-- 架构原则是「engine 写、api 只读」，但收藏是用户行为数据、不由跑批产生，
-- 故约定收窄为：业务数据只读、用户数据(仅本表)可写。
-- 按股票代码收藏、跨池共享；不区分用户(当前单人使用且 API 无认证)。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watch_favorite (
  id         BIGINT      NOT NULL AUTO_INCREMENT,
  code       VARCHAR(10) NOT NULL,
  name       VARCHAR(32) NOT NULL DEFAULT '' COMMENT '收藏时的名称快照',
  note       VARCHAR(255) COMMENT '备注:为什么关注它',
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_fav_code (code),
  KEY idx_fav_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人工收藏(唯一API可写表)';


-- ===========================================================================
-- 以下几张表此前只由 ORM(common/init_db.py) 建、未同步进本文件，
-- 导致「全新部署从 schema.sql 建库会缺表」。2026-09-15 从线上
-- mysqldump --no-data 导出补齐，与生产表结构逐字一致。
-- 【今后改表结构务必同步这里】否则线上与全新部署会继续分叉。
-- ===========================================================================

CREATE TABLE IF NOT EXISTS limitup_stock (

  `id` bigint NOT NULL AUTO_INCREMENT,
  `trade_date` date NOT NULL,
  `code` varchar(10) NOT NULL,
  `name` varchar(32) NOT NULL,
  `pct_chg` float DEFAULT NULL,
  `close` decimal(12,3) DEFAULT NULL,
  `amount` decimal(20,2) DEFAULT NULL COMMENT '成交额(元)',
  `circ_mv` decimal(20,2) DEFAULT NULL COMMENT '流通市值(元)',
  `turnover` float DEFAULT NULL COMMENT '换手率%',
  `seal_amount` decimal(20,2) DEFAULT NULL COMMENT '封板资金(元)',
  `first_seal_time` varchar(8) DEFAULT NULL COMMENT '首封时间',
  `last_seal_time` varchar(8) DEFAULT NULL COMMENT '最后封板',
  `open_times` int NOT NULL COMMENT '炸板次数',
  `boards` int NOT NULL COMMENT '连板数',
  `industry` varchar(32) DEFAULT NULL COMMENT '东财细分行业(比证监会分类细)',
  `limit_up_reason` varchar(255) DEFAULT NULL COMMENT '涨停原因(题材串)',
  `is_sealed_now` tinyint(1) DEFAULT NULL COMMENT '当前是否封板(盘中会变)',
  `snapshot_at` datetime DEFAULT NULL COMMENT '快照时刻',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_limitup_date_code` (`trade_date`,`code`),
  KEY `ix_limitup_stock_boards` (`boards`),
  KEY `ix_limitup_stock_trade_date` (`trade_date`),
  KEY `ix_limitup_stock_industry` (`industry`),
  KEY `ix_limitup_stock_code` (`code`)
) ENGINE=InnoDB AUTO_INCREMENT=53881 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS concept_daily (

  `id` bigint NOT NULL AUTO_INCREMENT,
  `trade_date` date NOT NULL,
  `thscode` varchar(16) NOT NULL,
  `name` varchar(48) NOT NULL,
  `last_price` float DEFAULT NULL COMMENT '板块指数点位',
  `pct_chg` float DEFAULT NULL COMMENT '涨跌幅%',
  `turnover` decimal(20,2) DEFAULT NULL COMMENT '成交额(元)',
  `volume` decimal(20,2) DEFAULT NULL,
  `zt_count` int DEFAULT NULL COMMENT '板块内涨停数',
  `turnover_share` float DEFAULT NULL COMMENT '成交额占比%',
  `rank_pct` int DEFAULT NULL COMMENT '当日涨幅排名',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_concept_date_code` (`trade_date`,`thscode`),
  KEY `ix_concept_daily_pct_chg` (`pct_chg`),
  KEY `ix_concept_daily_thscode` (`thscode`),
  KEY `ix_concept_daily_trade_date` (`trade_date`)
) ENGINE=InnoDB AUTO_INCREMENT=1171 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS theme_daily (

  `id` bigint NOT NULL AUTO_INCREMENT,
  `trade_date` date NOT NULL,
  `theme` varchar(48) NOT NULL,
  `zt_count` int NOT NULL COMMENT '挂此题材的涨停数',
  `max_boards` int NOT NULL COMMENT '该题材最高连板',
  `codes` text COMMENT '涨停个股代码,逗号分隔',
  `names` text COMMENT '涨停个股名称,逗号分隔',
  `consec_days` int NOT NULL COMMENT '连续上榜天数',
  `is_new` tinyint(1) NOT NULL COMMENT '近20日首次出现',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_theme_date` (`trade_date`,`theme`),
  KEY `ix_theme_daily_is_new` (`is_new`),
  KEY `ix_theme_daily_trade_date` (`trade_date`),
  KEY `ix_theme_daily_theme` (`theme`),
  KEY `ix_theme_daily_consec_days` (`consec_days`)
) ENGINE=InnoDB AUTO_INCREMENT=359 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS stock_concept (

  `id` bigint NOT NULL AUTO_INCREMENT,
  `code` varchar(10) NOT NULL,
  `thscode` varchar(16) NOT NULL COMMENT '概念板块代码',
  `concept_name` varchar(48) NOT NULL COMMENT '概念名称',
  `stock_name` varchar(32) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT (now()),
  `updated_at` datetime NOT NULL DEFAULT (now()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_sc_code_concept` (`code`,`thscode`),
  KEY `ix_stock_concept_concept_name` (`concept_name`),
  KEY `ix_stock_concept_code` (`code`),
  KEY `ix_stock_concept_thscode` (`thscode`)
) ENGINE=InnoDB AUTO_INCREMENT=71920 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS market_sentiment (

  `trade_date` date NOT NULL,
  `zt_count` int NOT NULL COMMENT '涨停家数',
  `zb_count` int NOT NULL COMMENT '炸板家数',
  `seal_rate` float DEFAULT NULL COMMENT '封板率%=涨停/(涨停+炸板)',
  `strong_count` int NOT NULL COMMENT '强势股家数',
  `first_board` int NOT NULL COMMENT '首板家数',
  `ge2` int NOT NULL COMMENT '2板以上家数',
  `ge3` int NOT NULL COMMENT '3板以上家数',
  `ge5` int NOT NULL COMMENT '5板以上家数',
  `height` int NOT NULL COMMENT '最高连板数',
  `tier_filled` int NOT NULL COMMENT '梯队完整度(2..height非空档位数)',
  `advance_rate` float DEFAULT NULL COMMENT '晋级率(昨连板池今日续板比例)',
  `prev_zt_avg_pct` float DEFAULT NULL COMMENT '昨日涨停股今日平均涨跌幅%',
  `prev_zt_win_rate` float DEFAULT NULL COMMENT '昨日涨停股今日上涨占比%',
  `ema_ge2` float DEFAULT NULL COMMENT 'ge2的EMA',
  `ema_height` float DEFAULT NULL COMMENT 'height的EMA',
  `ema_advance` float DEFAULT NULL COMMENT '晋级率的EMA',
  `phase_raw` varchar(8) DEFAULT NULL COMMENT '当日原始判定',
  `phase` varchar(8) DEFAULT NULL COMMENT '2日确认后的稳定阶段',
  `stance` varchar(16) DEFAULT NULL COMMENT '操作倾向',
  `created_at` datetime NOT NULL DEFAULT (now()),
  `updated_at` datetime NOT NULL DEFAULT (now()),
  `dt_count` int NOT NULL DEFAULT '0' COMMENT '跌停家数',
  `zt_dt_ratio` float DEFAULT NULL COMMENT '涨跌停比',
  PRIMARY KEY (`trade_date`),
  KEY `ix_market_sentiment_phase` (`phase`),
  KEY `ix_market_sentiment_advance_rate` (`advance_rate`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS adj_factor (

  `id` bigint NOT NULL AUTO_INCREMENT,
  `code` varchar(10) NOT NULL,
  `trade_date` date NOT NULL COMMENT '除权日(ex_date)',
  `dividend` float NOT NULL COMMENT '每股分红(元)',
  `bonus` float NOT NULL COMMENT '每股送转(股)',
  `allot_ratio` float NOT NULL COMMENT '配股比例',
  `allot_price` float NOT NULL COMMENT '配股价',
  `ratio` float NOT NULL COMMENT '单次除权比例',
  `factor` float NOT NULL COMMENT '后复权累乘因子',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_adj_code_date` (`code`,`trade_date`),
  KEY `ix_adj_factor_trade_date` (`trade_date`),
  KEY `ix_adj_factor_code` (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS watch_pullback_legacy (

  `id` bigint NOT NULL AUTO_INCREMENT,
  `code` varchar(10) NOT NULL,
  `name` varchar(32) NOT NULL DEFAULT '',
  `board_group` varchar(8) NOT NULL DEFAULT 'main',
  `breakout_date` date NOT NULL COMMENT '启动涨停日',
  `breakout_close` decimal(12,3) DEFAULT NULL COMMENT '启动日原始收盘',
  `breakout_open` decimal(12,3) DEFAULT NULL COMMENT '启动日原始开盘',
  `breakout_pct` float DEFAULT NULL COMMENT '启动日涨幅%',
  `breakout_amount` decimal(20,2) DEFAULT NULL COMMENT '启动日成交额',
  `gain_from_low` float DEFAULT NULL COMMENT '距120日低点涨幅%',
  `breakout_vol_ratio` float DEFAULT NULL COMMENT '启动日放量倍数(vs前20日均额)',
  `flat_days` int DEFAULT NULL COMMENT '启动前横盘天数(仅记录,IC≈0不计分)',
  `breakout_boards` int DEFAULT NULL COMMENT '启动段连板数(1=孤板)',
  `pullback_date` date DEFAULT NULL COMMENT '回踩确认日=报警日(未触发则空)',
  `pullback_close` decimal(12,3) DEFAULT NULL COMMENT '回踩日原始收盘(未触发则空)',
  `drawdown` float DEFAULT NULL COMMENT '相对启动日收盘%(连板时可为正)',
  `peak_close` decimal(12,3) DEFAULT NULL COMMENT '启动段最高收盘',
  `drawdown_from_peak` float DEFAULT NULL COMMENT '相对启动段最高收盘%(回调深度,入池判据)',
  `dist_ma5` float DEFAULT NULL COMMENT '距MA5 %',
  `dist_ma10` float DEFAULT NULL COMMENT '距MA10 %(触发判据±3%)',
  `dist_ma20` float DEFAULT NULL COMMENT '距MA20 %',
  `pullback_days` int DEFAULT NULL COMMENT '启动→回踩交易日数',
  `pullback_vol_ratio` float DEFAULT NULL COMMENT '回踩日额比vs启动日',
  `status` varchar(12) NOT NULL DEFAULT 'armed' COMMENT 'watching/hit/expired',
  `hit_date` date DEFAULT NULL,
  `hit_days` int DEFAULT NULL COMMENT '距回踩日交易日数',
  `expire_date` date DEFAULT NULL COMMENT '10交易日窗口末日(未走满留NULL)',
  `broke_date` date DEFAULT NULL COMMENT '跌破启动日开盘价日(标记非删除)',
  `broke_days` int DEFAULT NULL COMMENT '距回踩日天数',
  `ret1` float DEFAULT NULL COMMENT '回踩后T+1收益%',
  `ret3` float DEFAULT NULL COMMENT '回踩后T+3收益%',
  `ret5` float DEFAULT NULL COMMENT '回踩后T+5收益%',
  `ret10` float DEFAULT NULL COMMENT '回踩后T+10收益%',
  `max_ret` float DEFAULT NULL COMMENT '窗口内最大收益%',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `entry_kind` varchar(12) NOT NULL DEFAULT 'streak' COMMENT 'limitup单根涨停/streak多根阳线',
  `streak_days` int DEFAULT NULL COMMENT '启动段阳线根数(不含中间十字星)',
  `streak_gain` float DEFAULT NULL COMMENT '启动段累计涨幅%(段首开→段末收)',
  `streak_end_date` date DEFAULT NULL COMMENT '启动段末日(回踩窗口起算点)',
  `first_board` tinyint(1) DEFAULT NULL COMMENT '启动段前60日无涨停(观测字段)',
  `armed_date` date DEFAULT NULL COMMENT '登记待回踩日(段末次日)',
  `peak_broken_date` date DEFAULT NULL COMMENT '收盘突破启动段峰值日(=第二波已启动)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_wpb_code_breakout` (`code`,`breakout_date`),
  KEY `idx_wpb_code` (`code`),
  KEY `idx_wpb_pullback` (`pullback_date`),
  KEY `idx_wpb_breakout` (`breakout_date`),
  KEY `idx_wpb_status` (`status`),
  KEY `idx_wpb_kind` (`entry_kind`),
  KEY `idx_wpb_streak` (`streak_days`),
  KEY `idx_wpb_armed` (`armed_date`)
) ENGINE=InnoDB AUTO_INCREMENT=21549 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='突破回踩池(触发=回踩日)';
