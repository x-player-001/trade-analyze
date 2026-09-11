"""同步个股 ↔ 同花顺概念映射：遍历 390 个概念板块取成分股，反建映射表。

**为什么用反向路径**：同花顺的「个股反查所属指数」接口尚未上线
（docs 标 "敬请期待"，实测三个候选路径全部 404）。但板块成分股接口可用
（`/a-share-index/constituents/ths-stock-list`，实测新能源汽车 1064 只 / 1.5s），
遍历 390 个板块即可反建完整的双向映射。

**成本**：390 次请求 × 约1.5s ≈ 10 分钟。板块成分变动很慢，一周跑一次足够，
不必进每日管线。

**比证监会分类强在哪**：
    stock_basic.industry  一只票只有一个证监会大类（C39 含 658 只票，
                          把消费电子/PCB/半导体/光模块全混在一起）
    stock_concept         一只票可同时属于多个概念（人形机器人+减速器+
                          工业母机），概念才是 A 股主线的真实载体
这直接让 v2 版本的 use_industry_strength 因子有了升级空间——
从"证监会行业跑赢大盘"升级为"所属最强概念的强度"。

用法：
    python -m engine.jobs.sync_concepts             # 全量同步
    python -m engine.jobs.sync_concepts --limit 20  # 只同步前20个板块(试跑)
"""
from __future__ import annotations

import argparse
import time

from sqlalchemy import func, select, text

from common.db import session_scope
from common.logging_conf import setup_logging
from common.models import StockConcept
from common.upsert import bulk_upsert
from engine.datasource.hithink_source import HithinkError, HithinkSource

log = setup_logging("sync_concepts")

SLEEP = 0.4        # 板块间隔，避免触发动态限流（文档要求合理控制频率）
CHUNK = 2000


def main() -> None:
    ap = argparse.ArgumentParser(description="同步个股↔概念映射")
    ap.add_argument("--limit", type=int, default=0, help="只同步前N个板块(试跑)")
    args = ap.parse_args()

    src = HithinkSource()
    concepts = src.concept_list()
    if args.limit:
        concepts = concepts[:args.limit]
    log.info("待同步概念板块 %d 个（预计 %.0f 分钟）", len(concepts),
             len(concepts) * (SLEEP + 1.5) / 60)

    rows: list[dict] = []
    ok = fail = 0
    for i, c in enumerate(concepts, 1):
        ths, cname = c.get("thscode", ""), c.get("name", "")
        if not ths:
            continue
        try:
            cons = src.index_constituents(ths)
            for m in cons:
                code = str(m.get("ticker") or "").zfill(6)
                if not code or code == "000000":
                    continue
                rows.append(dict(
                    code=code, thscode=ths, concept_name=cname[:48],
                    stock_name=str(m.get("name", ""))[:32],
                ))
            ok += 1
        except HithinkError as e:
            fail += 1
            log.warning("%s(%s) 取成分失败: %s", cname, ths, str(e)[:60])
        if i % 50 == 0:
            log.info("进度 %d/%d，累计映射 %d 条", i, len(concepts), len(rows))
        time.sleep(SLEEP)

    if not rows:
        log.warning("无映射数据，未写库")
        return

    # 全量替换：概念成分会增删，只 upsert 会留下已剔除的旧成分
    with session_scope() as s:
        if not args.limit:          # 试跑不清表，避免误删全量数据
            s.execute(text("DELETE FROM stock_concept"))
        for i in range(0, len(rows), CHUNK):
            bulk_upsert(s, StockConcept, rows[i:i + CHUNK])

    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(StockConcept)) or 0
        n_stock = s.scalar(select(func.count(func.distinct(StockConcept.code)))) or 0
        n_conc = s.scalar(select(func.count(func.distinct(StockConcept.thscode)))) or 0
        top = s.execute(text(
            "SELECT code, stock_name, COUNT(*) c FROM stock_concept "
            "GROUP BY code, stock_name ORDER BY c DESC LIMIT 5")).all()
    log.info("完成：板块 %d 成功 / %d 失败；映射 %d 条，覆盖 %d 只票 / %d 个概念",
             ok, fail, n, n_stock, n_conc)
    log.info("概念最多的票: %s", ", ".join(f"{nm}({c}个)" for _, nm, c in top))


if __name__ == "__main__":
    main()
