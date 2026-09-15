"""只读边界的机械校验。

account.py 的文档字符串写着"只实现 GET 查询，不实现任何下单/撤单/改杠杆接口"。
开源之后，别人凭什么信一句注释？把它变成测试，改坏了会有人知道。
"""
import ast
import pathlib
import re

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
ACCOUNT = APP / "account.py"

WRITE_METHODS = {"post", "put", "delete", "patch"}
WRITE_VERBS = {"POST", "PUT", "DELETE", "PATCH"}


def _tree(path):
    return ast.parse(path.read_text(), filename=str(path))


def test_account_module_makes_no_write_requests():
    """凭据在这个模块里用，所以这个模块必须只会读。"""
    offenders = []
    for node in ast.walk(_tree(ACCOUNT)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.lower() in WRITE_METHODS:
                offenders.append(f"第 {node.lineno} 行调用了 .{node.func.attr}()")
    assert not offenders, "account.py 出现了写请求：" + "；".join(offenders)


def test_account_module_names_no_write_verbs():
    """也不能绕开属性调用，用 request(method="POST") 这种写法。"""
    offenders = [
        f"第 {node.lineno} 行出现字面量 {node.value!r}"
        for node in ast.walk(_tree(ACCOUNT))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.upper() in WRITE_VERBS
    ]
    assert not offenders, "account.py 出现了写方法字面量：" + "；".join(offenders)


def test_no_order_endpoints_referenced():
    """币安的下单类端点路径不该在整个后端出现。"""
    forbidden = ["/fapi/v1/order", "/fapi/v1/batchOrders", "/fapi/v1/leverage",
                 "/fapi/v1/marginType", "/fapi/v1/positionSide", "/sapi/v1/capital/withdraw"]
    hits = []
    for path in APP.rglob("*.py"):
        text = path.read_text()
        hits += [f"{path.name} 含 {ep}" for ep in forbidden if ep in text]
    assert not hits, "出现了下单/改杠杆/提现端点：" + "；".join(hits)


def test_credentials_are_only_read_where_they_are_needed():
    """明文凭据的取用点越少越好，多一处就多一处泄露面。

    允许的只有：vault（加解密本身）、account（签名请求）。
    路由层、行情层都不该碰到明文。
    """
    allowed = {"vault.py", "account.py", "main.py"}   # main 只调 import_from_env
    # 用词边界匹配：db.py 里的 count_credentials() 是存密文的，不碰明文，
    # 子串匹配会把它误判成泄露
    markers = [r"\bvault\.get\(", r"(?<![\w.])credentials\(", r"\bBINANCE_API_SECRET\b"]
    leaks = []
    for path in APP.rglob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text()
        for marker in markers:
            if re.search(marker, text):
                leaks.append(f"{path.relative_to(APP)} 匹配 {marker}")
    assert not leaks, "明文凭据被引到了不该去的地方：" + "；".join(leaks)


@pytest.mark.parametrize("marker", ["api_secret", "api_key", "BINANCE_API_SECRET"])
def test_credentials_are_never_logged(marker):
    """凭据不进日志。日志会被转发、归档、贴进工单。"""
    bad = []
    for path in APP.rglob("*.py"):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if "log." in stripped and marker in stripped and "%s" not in stripped:
                bad.append(f"{path.name}:{i}")
    assert not bad, f"日志里可能带上了 {marker}：" + "；".join(bad)
