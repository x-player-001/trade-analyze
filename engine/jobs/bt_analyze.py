"""回测综合分析:① 30/60天命中率趋势 ② 模拟大盘择时 ③ 命中票因子画像。
只读已有快照(bt_v1/bt_v2)+ 大盘涨跌,不重算因子。"""
import json
import statistics
from collections import defaultdict

from sqlalchemy import select
from common.db import session_scope
from common.models import PickSnapshot, MarketStatus, IndexDaily
from engine.validation.validator import validate_snapshot

COST = 0.3
MARKET_DROP = -1.0   # 大盘开关阈值:上证跌幅 > 1% 当日不出票
FACTORS = [
    ("score_confirm_prev_high", "回踩确认前高(w2.5)"),
    ("score_probe_pullback", "试盘后回踩(w1.5)"),
    ("score_pullback_ma5", "回踩5日线(w1.5)"),
    ("score_chip_concentration", "筹码集中(w1.2)"),
    ("score_low_position", "低位启动(w1.0)"),
    ("score_shrink_consolidation", "缩量横盘(w1.0)"),
    ("score_small_yang", "连续小阳(w1.0)"),
    ("score_strong_rally", "拉升有力(w1.0)"),
    ("score_healthy_turnover", "换手健康(w0.8)"),
    ("score_sector_strength", "板块强弱(w0.8/1.5)"),
]


def avg(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def pct(a, b):
    return "%.0f%%" % (100 * a / b) if b else "-"


def load_sh_pct(s):
    """选股日 -> 上证涨跌%。优先 market_status,缺失回退 index_daily(上证000001)。"""
    m = {r[0]: float(r[1]) for r in s.execute(
        select(MarketStatus.trade_date, MarketStatus.sh_pct_chg)
        .where(MarketStatus.sh_pct_chg.isnot(None))).all()}
    # index_daily 兜底:上证综指 sh000001
    idx_rows = s.execute(
        select(IndexDaily.trade_date, IndexDaily.pct_chg)
        .where(IndexDaily.index_code == "sh000001")).all()
    for d, p in idx_rows:
        if d not in m and p is not None:
            m[d] = float(p)
    return m


def gather(s, ver):
    """返回 [(snap, validation, factor_dict)]，只含 tradable。"""
    snaps = s.scalars(select(PickSnapshot).where(PickSnapshot.param_version == ver)).all()
    out = []
    for snap in snaps:
        if not snap.tradable:
            continue
        v = validate_snapshot(s, snap, COST)
        fd = json.loads(snap.factor_scores_json) if snap.factor_scores_json else {}
        out.append((snap, v, fd))
    return out


def summarize(rows):
    comp = [v for _, v, _ in rows if v["is_complete"]]
    hits = [v for v in comp if v["hit_7pct"]]
    posn = [v for v in comp if (v["t3_close_ret"] or 0) > 0]
    return dict(
        n=len(rows), comp=len(comp),
        hit=pct(len(hits), len(comp)),
        t3c=avg([v["t3_close_ret"] for v in comp]),
        t3h=avg([v["t3_high_ret"] for v in comp]),
        pos=pct(len(posn), len(comp)),
        nhit=len(hits),
    )


def main():
    with session_scope() as s:
        sh = load_sh_pct(s)
        for ver, label in [("bt_v1", "v1 不看板块")]:
            rows = gather(s, ver)
            dates = sorted({snap.trade_date for snap, _, _ in rows})
            print("\n############### %s (%s) | 共 %d 天 %s~%s ###############"
                  % (ver, label, len(dates), dates[0], dates[-1]))

            # ---------- ① 30/60天命中率趋势 ----------
            print("--- ① 窗口对比(命中率=T+3内单日>=7%) ---")
            for w in (10, 20, len(dates)):
                cut = set(dates[-w:])
                sub = [r for r in rows if r[0].trade_date in cut]
                st = summarize(sub)
                print("  最近%2d天: 样本%4d 命中率%5s 正收益%5s T+3收盘%6s%% T+3最高%6s%%"
                      % (w, st["n"], st["hit"], st["pos"], st["t3c"], st["t3h"]))

            # ---------- ② 模拟大盘择时 ----------
            print("--- ② 模拟大盘择时(剔除上证跌幅>%.0f%%的选股日) ---" % MARKET_DROP)
            missing = [d for d in dates if d not in sh]
            kept = [r for r in rows if sh.get(r[0].trade_date, 0.0) >= MARKET_DROP]
            dropped_days = sorted({r[0].trade_date for r in rows
                                   if sh.get(r[0].trade_date, 0.0) < MARKET_DROP
                                   and r[0].trade_date in sh})
            st_all = summarize(rows)
            st_keep = summarize(kept)
            print("  择时前(全部): 样本%4d 命中率%5s 正收益%5s T+3收盘%6s%%"
                  % (st_all["n"], st_all["hit"], st_all["pos"], st_all["t3c"]))
            print("  择时后(开放日): 样本%4d 命中率%5s 正收益%5s T+3收盘%6s%%"
                  % (st_keep["n"], st_keep["hit"], st_keep["pos"], st_keep["t3c"]))
            print("  被剔除的下跌日(%d天): %s" % (len(dropped_days), [str(d) for d in dropped_days]))
            if missing:
                print("  注:缺大盘数据的%d天未参与择时过滤: %s" % (len(missing), [str(d) for d in missing[:10]]))

            # ---------- ③ 命中票因子画像 ----------
            print("--- ③ 命中 vs 未命中 因子分均值(完整T+3样本) ---")
            comp_rows = [(snap, v, fd) for snap, v, fd in rows if v["is_complete"]]
            hit_rows = [(snap, v, fd) for snap, v, fd in comp_rows if v["hit_7pct"]]
            miss_rows = [(snap, v, fd) for snap, v, fd in comp_rows if not v["hit_7pct"]]
            print("  命中 %d 只 / 未命中 %d 只" % (len(hit_rows), len(miss_rows)))
            print("  %-22s 命中均分  未命中均分  差值" % "因子")
            for key, name in FACTORS:
                h = avg([fd.get(key) for _, _, fd in hit_rows])
                m = avg([fd.get(key) for _, _, fd in miss_rows])
                diff = round((h or 0) - (m or 0), 2)
                flag = "  <== 命中票更高" if diff >= 0.05 else ("  (反向)" if diff <= -0.05 else "")
                print("  %-22s %7s   %8s   %+5.2f%s" % (name, h, m, diff, flag))
            # 命中票的 rank 分布
            hit_ranks = [snap.rank for snap, _, _ in hit_rows]
            if hit_ranks:
                buck = defaultdict(int)
                for r in hit_ranks:
                    buck["1-5" if r <= 5 else "6-10" if r <= 10 else "11-15" if r <= 15 else "16-20"] += 1
                print("  命中票rank分布:", dict(buck), " 平均rank=%.1f" % statistics.mean(hit_ranks))


if __name__ == "__main__":
    main()
