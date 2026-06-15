"""按选股排名分档,看高分票 vs 低分票的收益差异(评分区分度检验)。"""
import statistics
from collections import defaultdict
from sqlalchemy import select
from common.db import session_scope
from common.models import PickSnapshot
from engine.validation.validator import validate_snapshot

COST = 0.3


def avg(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def pct(a, b):
    return "%.0f%%" % (100 * a / b) if b else "-"


def rank_bucket(r):
    # 每个 board_group 内 rank 从1开始,分四档
    if r <= 5:
        return "Top1-5 "
    if r <= 10:
        return "6-10   "
    if r <= 15:
        return "11-15  "
    return "16-20  "


def main():
    with session_scope() as s:
        for ver, label in [("bt_v1", "v1 不看板块"), ("bt_v2", "v2 结合板块")]:
            snaps = s.scalars(
                select(PickSnapshot).where(PickSnapshot.param_version == ver)
            ).all()
            buckets = defaultdict(list)   # bucket -> list of (validation, score)
            scored = []                   # (score, t3_close_ret, hit) 用于相关性
            for snap in snaps:
                if not snap.tradable:
                    continue
                v = validate_snapshot(s, snap, COST)
                buckets[rank_bucket(snap.rank)].append(v)
                if v["t3_close_ret"] is not None:
                    scored.append((float(snap.total_score), v["t3_close_ret"], bool(v["hit_7pct"])))
            print("================ %s  (%s) ================" % (ver, label))
            print("  档位      样本  命中率   T+3收盘均收益  T+3最高均收益  正收益占比")
            for b in ["Top1-5 ", "6-10   ", "11-15  ", "16-20  "]:
                vs = buckets[b]
                comp = [v for v in vs if v["is_complete"]]
                hits = [v for v in comp if v["hit_7pct"]]
                posn = [v for v in comp if (v["t3_close_ret"] or 0) > 0]
                print("  %s  %4d  %5s   %9s%%   %9s%%   %6s"
                      % (b, len(vs), pct(len(hits), len(comp)),
                         avg([v["t3_close_ret"] for v in comp]),
                         avg([v["t3_high_ret"] for v in comp]),
                         pct(len(posn), len(comp))))
            # 分数与收益的相关性
            if len(scored) > 2:
                xs = [x[0] for x in scored]
                ys = [x[1] for x in scored]
                try:
                    r = statistics.correlation(xs, ys)
                    print("  评分 vs T+3收盘收益 相关系数: %.3f" % r)
                except Exception as e:
                    print("  相关系数计算失败:", e)
            # 分数区间分布
            top = sorted(scored, key=lambda x: -x[0])
            print("  最高分票区间 total_score: %.3f ~ %.3f"
                  % (top[0][0], top[-1][0]))


if __name__ == "__main__":
    main()
