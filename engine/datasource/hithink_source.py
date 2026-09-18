"""同花顺金融数据 API（fuyao.aicubes.cn）数据源。

与其他数据源的定位差异：**这个源主要服务「盘中实时热点」**，不走每日批处理
落库，而是由 API 层按需调用 + 短 TTL 缓存直出（见 api/routers/hotspot.py）。

实测（2026-09-11，sgp 新加坡服务器）：
    概念板块列表  390个  2.1s   —— 一次请求全量，无批量上限
    板块行情快照  390个  2.0s   —— thscodes 可一次传 390 个
    涨停池        34条   1.3s   —— 含 limit_up_reason 题材串、seal_money 封单
    炸板池                      —— 含 open_times 炸板次数
    连板天梯      30天          —— 官方按档位组织，含 seal_nextday 晋级结果
    热榜/异动榜   30条          —— rank/heat/rank_change/rank_trend

限流（官方文档）：不限累计调用次数，但按实时负载动态限流；
HTTP 429 或 code=4001 表示触发。**文档明确要求"避免立即连续重试"**，
故本模块重试用指数退避（2s→4s→8s），不做紧凑重试。

已知不稳定：SSL 握手偶发超时（实测数次），重试即可恢复。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from common.config import settings
from common.logging_conf import get_logger

log = get_logger("datasource.hithink")

BASE = "https://fuyao.aicubes.cn/api"
TIMEOUT = 30
RETRIES = 3
BACKOFF = 2.0        # 指数退避基数；文档要求避免立即连续重试


class HithinkError(RuntimeError):
    pass


class HithinkSource:
    """同花顺 API 客户端。无状态，可安全共享。"""

    def __init__(self, api_key: str | None = None) -> None:
        self.key = api_key or getattr(settings, "ths_key", "") or ""
        if not self.key:
            log.warning("THS_KEY 未配置，同花顺接口不可用")

    # ---------------- 底层 ----------------
    def _get(self, path: str, **params: Any) -> Any:
        """GET 并返回 data.item（列表）或 data（其他结构）。"""
        if not self.key:
            raise HithinkError("THS_KEY 未配置")
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                req = urllib.request.Request(url, headers={"X-api-key": self.key})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    body = json.loads(r.read().decode("utf-8"))
                code = body.get("code")
                if code not in (0, None):
                    # 4001=限流，退避后重试；其他业务错误直接抛
                    if code == 4001 and attempt < RETRIES - 1:
                        time.sleep(BACKOFF * (2 ** attempt))
                        continue
                    raise HithinkError(f"code={code} {body.get('message')}")
                return body.get("data")
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < RETRIES - 1:
                    time.sleep(BACKOFF * (2 ** attempt))
                    last = e
                    continue
                raise
            except Exception as e:  # noqa: BLE001  SSL握手偶发超时等
                last = e
                if attempt < RETRIES - 1:
                    time.sleep(BACKOFF * (2 ** attempt))
                    continue
        raise HithinkError(f"{path} 重试{RETRIES}次仍失败: {last}")

    @staticmethod
    def _items(data: Any) -> list[dict]:
        if isinstance(data, dict):
            it = data.get("item")
            return it if isinstance(it, list) else []
        return data if isinstance(data, list) else []

    # ---------------- 板块 / 概念 ----------------
    def concept_list(self) -> list[dict]:
        """全部概念板块 [{thscode, name}]。实测 390 个。"""
        return self._items(self._get(
            "/a-share-index/catalog/ths-index-list", tag="cn_concept"))

    def industry_list(self) -> list[dict]:
        return self._items(self._get(
            "/a-share-index/catalog/ths-index-list", tag="industry"))

    def index_snapshot(self, thscodes: list[str]) -> list[dict]:
        """板块/指数行情快照。实测一次可传 390 个，无需分批。"""
        if not thscodes:
            return []
        return self._items(self._get(
            "/a-share-index/prices/snapshot", thscodes=",".join(thscodes)))

    def index_constituents(self, thscode: str) -> list[dict]:
        """板块成分股。"""
        return self._items(self._get(
            "/a-share-index/constituents/ths-stock-list", thscode=thscode))

    # ---------------- 涨停 / 情绪 ----------------
    def limit_up_pool(self, date: str | None = None) -> list[dict]:
        """涨停池。含 limit_up_reason(题材串)、seal_money(封单)、
        continue_day_cnt(连板数)、is_st/is_new。date 形如 2026-09-10。"""
        return self._items(self._get(
            "/a-share/special-data/limit-up-pool", date=date))

    def limit_break_pool(self, date: str | None = None) -> list[dict]:
        """炸板池。含 open_times(炸板次数)——这是日线算不出的字段。"""
        return self._items(self._get(
            "/a-share/special-data/limit-break-pool", date=date))

    def limit_down_pool(self, date: str | None = None) -> list[dict]:
        """跌停池。含 first_limit_time/last_limit_time/turnover_ratio_pct。

        情绪模块原本只有涨停/炸板，缺跌停这一半——涨跌停比是情绪强弱的
        经典指标，且「冰点」判定用跌停家数比用涨停家数贴地更直接。
        """
        return self._items(self._get(
            "/a-share/special-data/limit-down-pool", date=date))

    # ---------------- 个股实时行情 ----------------
    def stock_snapshot(self, thscodes: list[str]) -> list[dict]:
        """个股行情快照（**盘中实时**）。

        字段：last_price 最新价、open_price/high_price/low_price、
        prev_price 昨收、price_change_ratio_pct 涨跌幅%、
        turnover 成交额(元)、volume 成交量。

        **这是盘中唯一能拿到当日价格的路径**——tushare 的 `pro.daily`
        只有收盘后才有当日数据，盘中查返回 0 行。故盘中预警任务
        (watch_pullback_live) 必须走本接口，不能复用日线管线。

        注意 turnover 是成交额、volume 是成交量，与库内 amount/volume
        的命名相反，映射时别搞混。
        """
        if not thscodes:
            return []
        return self._items(self._get(
            "/a-share/prices/snapshot", thscodes=",".join(thscodes)))

    # ---------------- 集合竞价 ----------------
    def auction_snapshot(self, thscodes: list[str]) -> list[dict]:
        """集合竞价快照。**对短线打法价值最高的数据**。

        字段：auction_price/pct 竞价价与涨跌幅、auction_volume/amount 竞价量额、
        auction_unmatched 未匹配量（委托强度，负=卖压）、
        auction_yesterday_ratio_pct 竞价量占昨日成交比、auction_volume_ratio 量比。

        用途：本项目复刻的是「尾盘买入、次日卖出」，竞价数据覆盖决策链的
        另一端——昨日尾盘买的票今早竞价强不强，直接反映有无资金承接。
        """
        if not thscodes:
            return []
        return self._items(self._get(
            "/a-share/auction/snapshot", thscodes=",".join(thscodes)))

    def auction_benchmark(self) -> list[dict]:
        """短线风向标竞价基准。官方筛选过的标的，含 tags 题材标签。"""
        return self._items(self._get("/a-share/auction/short-term-benchmark"))

    def limit_up_ladder(self) -> list[dict]:
        """连板天梯：近30个交易日的梯队矩阵。
        每项 {date, boards:{two_board:[...], three_board:[...], ...}}，
        个股含 seal_nextday(次日是否封板)——官方晋级结果，比自算准。"""
        return self._items(self._get("/a-share/special-data/limit-up-ladder"))

    # ---------------- 热榜 ----------------
    def hot_stocks(self) -> list[dict]:
        """人气榜 [{thscode,name,rank,heat,rank_change,rank_trend}]。"""
        return self._items(self._get("/a-share/special-data/hot-stock-list"))

    def skyrocket(self) -> list[dict]:
        """飙升榜（异动）。"""
        return self._items(self._get("/a-share/special-data/skyrocket-list"))


def parse_reasons(reason: str | None) -> list[str]:
    """拆解涨停原因串为题材标签列表。

    实测格式是 `+` 连接的题材串：
        "覆铜板+业绩增长+扩产"
        "800G光引擎+CPO+AI算力+营收增长"
    聚合全市场涨停股的标签词频，即可得到当日主线——这是目前唯一能拿到的
    真正题材维度（东财/同花顺爬虫接口、tushare 免费档均取不到）。
    """
    if not reason:
        return []
    out: list[str] = []
    for part in str(reason).replace("＋", "+").split("+"):
        t = part.strip()
        if t:
            out.append(t)
    return out
