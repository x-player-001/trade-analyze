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


def price_limit_pct(board: str, is_st: bool) -> float:
    """涨跌幅限制。

    ST: 5%
    创业板/科创板: 20%
    北交所: 30%
    主板: 10%
    """
    if is_st:
        return 5.0
    if board in ("gem", "star"):
        return 20.0
    if board == "bse":
        return 30.0
    return 10.0


def is_st_name(name: str) -> bool:
    """按名称判断 ST/退市风险。"""
    n = name.upper().replace(" ", "")
    return "ST" in n or "退" in n
