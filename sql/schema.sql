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
  open        FLOAT        NOT NULL,
  high        FLOAT        NOT NULL COMMENT '后复权最高',
  low         FLOAT        NOT NULL COMMENT '后复权最低',
  close       FLOAT        NOT NULL COMMENT '后复权收盘',
  raw_open    FLOAT                 COMMENT '原始开盘',
  raw_high    FLOAT                 COMMENT '原始最高',
  raw_low     FLOAT                 COMMENT '原始最低',
  raw_close   FLOAT                 COMMENT '原始收盘',
  volume      FLOAT                 COMMENT '成交量(手)',
  amount      FLOAT                 COMMENT '成交额(元)',
  amplitude   FLOAT                 COMMENT '振幅(%)',
  pct_chg     FLOAT                 COMMENT '涨跌幅(%)',
  change_amt  FLOAT                 COMMENT '涨跌额(原始)',
  turnover    FLOAT                 COMMENT '换手率(%)',
  PRIMARY KEY (id),
  UNIQUE KEY uq_daily_code_date (code, trade_date),
  KEY idx_daily_code (code),
  KEY idx_daily_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日线行情(后复权+原始OHLC)';

CREATE TABLE IF NOT EXISTS index_daily (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  index_code  VARCHAR(10)  NOT NULL,
  trade_date  DATE         NOT NULL,
  open        FLOAT        NOT NULL,
  high        FLOAT        NOT NULL,
  low         FLOAT        NOT NULL,
  close       FLOAT        NOT NULL,
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
  UNIQUE KEY uq_factor_code_date (code, trade_date),
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
  decision_close     FLOAT       NOT NULL COMMENT '后复权收盘',
  decision_raw_close FLOAT                COMMENT '原始收盘',
  limit_up           TINYINT(1)  NOT NULL DEFAULT 0,
  tradable           TINYINT(1)  NOT NULL DEFAULT 1,
  param_version      VARCHAR(16) NOT NULL,
  created_at         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_pick_date_code (trade_date, code),
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
