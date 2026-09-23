"""股票代码 → 板块 / 涨跌幅制度 分类。纯函数，无外部依赖，便于单测。"""
from __future__ import annotations


def classify_board(code: str) -> str:
    """按代码前缀判板块。

    主板: 沪 60xxxx / 深 000xxx,001xxx,002xxx,003xxx
    创业板: 300xxx,301xxx
    科创板: 688xxx,689xxx
    北交所: 8xxxxx,4xxxxx,920xxx
    """
    c = code.zfill(6)
    if c.startswith(("60", "000", "001", "002", "003")):
        return "main"
    if c.startswith(("300", "301")):
        return "gem"
    if c.startswith(("688", "689")):
        return "star"
    if c.startswith(("8", "4", "920")):
        return "bse"
    return "main"


def board_group(board: str) -> str:
    """板块 → 选股分组。main=主板；其余(创业板/科创板/北交所)归为 other。"""
    return "main" if board == "main" else "other"


def price_limit_pct(board: str, is_st: bool) -> float:  # noqa: ARG001
    """涨跌幅限制（现行规则）。

    创业板/科创板: 20%（ST 同样 20%，从来不是 5%）
    北交所: 30%
    主板: 10%（**ST 也是 10%**）

    【原「ST 一律 5%」已过时】沪深交易所 2025-07 起主板风险警示股
    涨跌幅由 5% 调为 10%。线上实证（2026-09-23 竞价）：*ST天箭 002977
    竞价 −6.95%、*ST亚士 603378 −5.84%——竞价价不可能越过涨跌停价，
    若仍是 5% 这两个价格根本不存在；原逻辑却把它们都判成了跌停。
    `is_st` 参数保留以兼容调用方，已不影响结果。
    **回测 2025-07 之前的主板 ST 时注意**：当时确为 5%。
    """
    if board in ("gem", "star"):
        return 20.0
    if board == "bse":
        return 30.0
    return 10.0


def is_st_name(name: str) -> bool:
    """按名称判断 ST/退市风险。"""
    n = name.upper().replace(" ", "")
    return "ST" in n or "退" in n
