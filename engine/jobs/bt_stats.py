"""只读快照 + 跑验证统计(不重算因子)。配合 backtest 已写入的 bt_v1/bt_v2 快照。"""
import statistics
from sqlalchemy import select
from common.db import session_scope
from common.models import PickSnapshot
from engine.validation.validator import validate_snapshot

COST = 0.3
HIT = 7.0


def avg(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def pct(a, b):
    return "%.1f%%" % (100 * a / b) if b else "-"


def main():
    with session_scope() as s:
        for ver, label in [("bt_v1", "v1 不看板块"), ("bt_v2", "v2 结合板块")]:
            snaps = s.scalars(
                select(PickSnapshot).where(PickSnapshot.param_version == ver)
            ).all()
            res = [validate_snapshot(s, snap, COST) for snap in snaps if snap.tradable]
            n = len(res)
            comp = [v for v in res if v["is_complete"]]
            hits = [v for v in comp if v["hit_7pct"]]
            posn = [v for v in comp if (v["t3_close_ret"] or 0) > 0]
            t1 = avg([v["t1_close_ret"] for v in res])
            t3c = avg([v["t3_close_ret"] for v in comp])
            t3h = avg([v["t3_high_ret"] for v in comp])
            mdd = avg([v["max_drawdown"] for v in res])
            print("================ %s  (%s) ================" % (ver, label))
            print("  可买入样本: %d   完整T+3: %d" % (n, len(comp)))
            print("  命中率(T+3内单日>=%.0f%%): %s  (%d/%d)"
                  % (HIT, pct(len(hits), len(comp)), len(hits), len(comp)))
            print("  T+3收盘正收益占比: %s  (%d/%d)"
                  % (pct(len(posn), len(comp)), len(posn), len(comp)))
            print("  平均收益  T+1收盘:%s%%  T+3收盘:%s%%  T+3最高:%s%%" % (t1, t3c, t3h))
            print("  平均最大回撤: %s%%" % mdd)


if __name__ == "__main__":
    main()
