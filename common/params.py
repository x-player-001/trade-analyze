"""选股参数：硬过滤阈值 + 软评分权重。

默认参数 DEFAULT_PARAMS 直接来源于 独自前行/总结.txt 第86-109行的量化翻译。
所有阈值都"逻辑解释得通"（筹码逻辑支撑），回测调参时优先保留可解释参数。

参数以版本化方式存入 param_config 表；运行时优先读库中的 active 版本，
读不到则回退到此处 DEFAULT_PARAMS。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from common.config import settings
from common.models import ParamConfig

DEFAULT_PARAMS: dict = {
    # ---------------- 硬过滤阈值 ----------------
    "hard": {
        # 大盘开关
        # enabled=False 时：仍计算并记录大盘涨跌与跌破20日线（供回测对比），
        # 但 is_open 恒为 True，不因大盘下跌停止出票。随时改回 True 即可启用。
        "market_switch_enabled": False,
        "market_drop_pct": -1.0,          # 上证/创业板跌幅 > 1% 当日不出票
        # 破位：收盘 < 60日线 且 20日线斜率 < 0
        "ma_long_window": 60,
        "ma_slope_window": 20,
        # 放量：当日量 > 5日均量 * N
        "volume_spike_ratio": 2.0,
        "volume_ma_window": 5,
        # 死亡换手：近3日任一日换手 > N%
        "death_turnover_pct": 30.0,
        "death_turnover_lookback": 3,
        # 累计涨幅：近10日累计涨幅 > N% 排除
        "cum_return_window": 10,
        "cum_return_pct": 30.0,
        # 偏离均线：(收盘-5日线)/5日线 > N% 排除
        "deviation_ma_window": 5,
        "deviation_pct": 7.0,
        # 杂乱：近20日收益率标准差 > N 排除
        "chaos_std_window": 20,
        "chaos_std_pct": 6.0,
        # 流动性：日成交额 > N 元（500万资金可进出）
        "min_amount": 1.0e8,
        # 死水排除：近 activity_window 日最大换手率 < N% 排除(无活性的大盘蓝筹/银行)。
        # 标注校准:他买的票近期最大换手都≥2%(华大九天边界2.2%),银行股多<2%。
        "activity_window": 20,
        "min_activity_turnover": 2.0,
        # 流通市值区间（亿元）；0 表示不限。标注验证发现他买千亿大盘股
        # (京东方2037亿),原100亿上限误杀大盘股,故放开上限。
        "min_circ_mv": 0.0,
        "max_circ_mv": 0.0,
        # 上市天数下限（排除新股）
        "min_list_days": 120,
    },
    # ---------------- 当日状态过滤 ----------------
    # 回踩确认窗口：当日涨跌幅在该区间才进入评分
    "pullback_window": {"low": -1.0, "high": 1.0},
    # ---------------- 软评分权重 ----------------
    # 各因子权重，"回踩确认前高"权重最高（核心买点）。权重和不必为1，最终归一。
    "weights": {
        "low_position": 1.0,
        "shrink_consolidation": 1.0,
        "probe_pullback": 1.5,
        "small_yang": 1.0,
        "confirm_prev_high": 2.5,        # 核心买点，最高权重
        "pullback_ma5": 1.5,
        "healthy_turnover": 0.8,
        "strong_rally": 1.0,
        "chip_concentration": 1.2,
        "sector_strength": 0.8,
    },
    # ---------------- 软评分内部参数 ----------------
    "soft": {
        # 低位：当前价距120日低点涨幅 < N% 满分
        "low_position_window": 120,
        "low_position_max_gain_pct": 25.0,
        # 缩量横盘：近N日振幅 < M%，成交量 < 60日均量
        "consolidation_window": 15,
        "consolidation_amplitude_pct": 3.0,
        "consolidation_vol_ma": 60,
        # 试盘线：近10日内有放量上影/涨5%+，之后缩量回落
        "probe_window": 10,
        "probe_rise_pct": 5.0,
        # 连续小阳：近N日每日涨跌幅在 -1~+1%、阳线居多
        "small_yang_window": 5,
        "small_yang_pct": 1.0,
        # 回踩前高：当日低点接近上一平台高点/试盘高点 ±N%
        "confirm_tolerance_pct": 2.0,
        # 回踩5日线：当日最低触碰5日线后收回，容差 N%
        "ma5_tolerance_pct": 1.5,
        # 换手健康区间
        "healthy_turnover_low": 12.0,
        "healthy_turnover_high": 20.0,
        # 拉升有力：历史拉升段单日涨幅、回撤评估窗口
        "rally_window": 60,
        # 筹码集中度：换手率衰减半衰期（天），获利盘阈值
        "chip_halflife": 60,
        "chip_profit_threshold": 30.0,
    },
    # ---------------- 选股输出 ----------------
    "selection": {"top_n": 10},
    # ---------------- 验证 ----------------
    "validation": {
        "hit_threshold_pct": 7.0,        # 命中定义：3日内单日涨幅≥7%
        "cost_pct": 0.3,                  # 双边成本（佣金+印花税+滑点）
        "windows": [1, 2, 3],            # T+1/2/3
    },
}


def load_active_params(session: Session) -> dict:
    """读取当前生效参数：优先库中 active 版本，回退 DEFAULT_PARAMS。"""
    row = (
        session.query(ParamConfig)
        .filter(ParamConfig.version == settings.active_param_version)
        .one_or_none()
    )
    if row is None:
        return DEFAULT_PARAMS
    return json.loads(row.config_json)


def seed_default_params(session: Session) -> None:
    """将 DEFAULT_PARAMS 写入库作为 active 版本（幂等）。"""
    version = settings.active_param_version
    row = session.get(ParamConfig, version)
    payload = json.dumps(DEFAULT_PARAMS, ensure_ascii=False)
    if row is None:
        session.add(
            ParamConfig(
                version=version,
                description="默认参数 v1，来源于总结.txt 规则翻译",
                config_json=payload,
                is_active=True,
            )
        )
    else:
        row.config_json = payload
        row.is_active = True
