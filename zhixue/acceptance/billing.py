from csv import writer
from io import StringIO
from ipaddress import ip_address

from .live_config import SAMPLES
from .usage import ROLES


def connections(run):
    configured = next((record["output"] or {} for record in run["records"] if record["step"] == "real-model"), {})
    return {ROLES["main"]: configured.get("主模型") or {}, ROLES["review"]: configured.get("复核模型") or {}}


def is_loopback(host):
    name = host.lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ip_address(name)
        return (getattr(address, "ipv4_mapped", None) or address).is_loopback
    except ValueError:
        return False


def scope_for_hosts(hosts):
    if any(is_loopback(host) for host in hosts if isinstance(host, str)):
        return "含回环连接"
    if all(isinstance(host, str) and host for host in hosts):
        return "两条已记录地址均为非回环"
    return "连接范围未记录"


def connection_scope(run):
    recorded = run.get("connectionScope")
    if recorded:
        return recorded
    return scope_for_hosts([profile.get("主机") for profile in connections(run).values()])


def billing_usage(run):
    if run["mode"] != "real" or run["state"] == "running":
        return None
    for record in run["records"]:
        if record["step"] == "real-usage":
            output = record["output"]
            calls = output.get("调用记录")
            if isinstance(calls, list) and calls and all(isinstance(call, dict) for call in calls):
                return output
    return None


def safe_cell(value):
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n"))
                                   or value.lstrip("\ufeff \t\r\n\v\f").startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def billing_csv(run, usage):
    csv = StringIO(newline="")
    table = writer(csv)
    table.writerow((
        "运行 ID", "样本", "接入范围", "证据范围", "用量采集时间", "序号", "阶段", "模型分工",
        "服务商", "模型", "主机", "状态", "账本开始时间（UTC）", "结算记录时间（UTC）", "输入用量", "输出用量",
        "服务商回报币种", "服务商回报金额", "估算币种", "估算金额", "账单条目", "账单币种", "账单金额", "核对结论",
    ))
    profiles = connections(run)
    scope = connection_scope(run)
    sample = SAMPLES.get(run["sample"])
    for number, call in enumerate(usage["调用记录"], 1):
        profile = profiles.get(call.get("模型分工"), {})
        reported = call.get("服务商回报费用") or {}
        estimated = call.get("估算费用") or {}
        row = (
            run["id"], sample.title if sample else run["sample"], scope, usage.get("证据范围") or "验收运行记录",
            usage.get("采集时间"), number, call.get("阶段"), call.get("模型分工"),
            profile.get("服务商"), profile.get("模型"), profile.get("主机"), call.get("状态"),
            call.get("账本开始时间（UTC）"), call.get("结算记录时间（UTC）"), call.get("输入用量"), call.get("输出用量"),
            reported.get("currency"), reported.get("amount"), estimated.get("currency"), estimated.get("amount"),
            "", "", "", "",
        )
        table.writerow(safe_cell(cell) for cell in row)
    return csv.getvalue().encode("utf-8-sig")
