from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from backend.model_costs import MoneyLimit, Pricing
from backend.model_protocols import DEFAULT_PROVIDER, PROVIDERS, test_address
from backend.model_settings_api import ConnectionInput


SAMPLE_LIMITS = {"course": 3, "answer": 1, "consolidation": 2, "assessment": 6}
MAX_CALLS = sum(SAMPLE_LIMITS.values())
CHECK_LABELS = {"facts": "事实与例题", "evidence": "引文与原始来源", "boundary": "适用条件与争议片段"}
VERDICTS = {"confirmed": "符合", "disputed": "有偏差", "insufficient": "证据不足"}


@dataclass(frozen=True)
class Sample:
    id: str
    title: str
    topic: str
    outcome: str
    source_name: str
    source_url: str
    source: str
    question: str
    answer: str
    criteria: dict[str, str]


SAMPLES = {sample.id: sample for sample in (
    Sample(
        "sqlite", "SQLite 筛选与排序", "SQLite SELECT 的筛选、排序和限制条数",
        "用一节初学课讲清如何从学习记录表取出分钟数至少为 30 的前两行，按分钟数降序、同分按名称升序。",
        "SQLite SELECT 官方文档摘要.txt", "https://www.sqlite.org/lang_select.html",
        "SQLite 的 WHERE 只保留条件为真的行。ORDER BY minutes DESC, name ASC 先按 minutes 降序，同值再按 name 升序。"
        "LIMIT 2 最多返回排序结果中的两行。示例表的行是 A 45、B 45、C 30、D 20，先筛出 A、B、C，最后返回 A、B。",
        "如果 A 和 B 的学习分钟数相同，为什么 A 先返回？",
        "WHERE minutes >= 30 保留 A、B、C。ORDER BY minutes DESC, name ASC 用名称升序打破同分，LIMIT 2 返回 A、B。",
        {
            "facts": "按 30 分钟筛出 A、B、C，最终按规则返回 A、B。",
            "evidence": "关键结论能在 SQLite 官方文档的 WHERE、ORDER BY、LIMIT 小节及验收资料摘要中定位。",
            "boundary": "同分由第二排序键决定。正文只发布有依据的内容，争议片段与课文分开。",
        },
    ),
    Sample(
        "si", "米与厘米的换算", "国际单位制中米与厘米的长度换算",
        "用一节初学课说明前缀 c、米和厘米的关系，并演示把 250 cm 换成 m 和把 1.2 m 换成 cm。",
        "国际计量局 SI 前缀表摘要.txt", "https://www.bipm.org/en/measurement-units/si-prefixes",
        "SI 前缀 centi 的符号是 c，代表 10 的负二次方。1 cm 等于 0.01 m，1 m 等于 100 cm。"
        "长度由厘米换成米时除以 100，从米换成厘米时乘以 100。250 cm 等于 2.5 m，1.2 m 等于 120 cm。",
        "把 250 cm 换成 m 时，为什么要除以 100？",
        "1 m 等于 100 cm，所以 250 cm 等于 2.5 m。反向要乘以 100，所以 1.2 m 等于 120 cm。",
        {
            "facts": "前缀 c 表示 10 的负二次方，两个换算结果分别为 2.5 m 和 120 cm。",
            "evidence": "结论与国际计量局前缀表及验收资料摘要一致，复核引文能定位。",
            "boundary": "乘除 100 的规则用于长度；正文只发布有依据的内容，争议片段与课文分开。",
        },
    ),
)}


class LiveBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: MoneyLimit
    mainPricing: Pricing
    reviewPricing: Pricing

    @model_validator(mode="after")
    def same_currency(self):
        if self.limit.currency != self.mainPricing.currency or self.limit.currency != self.reviewPricing.currency:
            raise ValueError("联调金额上限与两条连接的单价须使用同一币种。")
        return self


class LiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample: Literal["sqlite", "si"]
    confirmed: StrictBool
    main: ConnectionInput
    review: ConnectionInput
    budget: LiveBudget | None = None

    @model_validator(mode="after")
    def validate_live(self):
        from backend.course_review import same_model

        if not self.confirmed:
            raise ValueError("请确认本次外发范围及可能的模型费用。")
        recipients = []
        for role, profile in (("main", self.main), ("review", self.review)):
            if profile.provider not in PROVIDERS or profile.protocol not in PROVIDERS[profile.provider][1]:
                raise ValueError("请选择服务商支持的协议。")
            if profile.provider != "custom" and profile.apiKey is None:
                raise ValueError("请填写该服务商的 API 密钥。")
            recipients.append({
                "id": role, "provider": profile.provider, "model": profile.model,
                "address": test_address(profile.endpoint, profile.protocol, profile.model),
            })
        if same_model(*recipients):
            raise ValueError("请选择独立于主模型的复核连接。")
        return self

    def secrets(self) -> tuple[str, ...]:
        return tuple(item.apiKey.get_secret_value() for item in (self.main, self.review) if item.apiKey)

    def connections(self):
        return (("main", self.main), ("review", self.review))


def describe_connection(profile: ConnectionInput) -> dict:
    destination = urlsplit(profile.endpoint)
    return {
        "服务商": PROVIDERS[profile.provider][0],
        "接口协议": profile.protocol,
        "模型": profile.model,
        "主机": destination.hostname,
        "接入": "本机 HTTP" if destination.scheme == "http" else "HTTPS",
    }


def live_definition() -> dict:
    return {
        "maxCalls": MAX_CALLS, "limits": SAMPLE_LIMITS, "defaultProvider": DEFAULT_PROVIDER,
        "providers": [{"id": identity, "label": name, "protocols": protocols}
                      for identity, (name, protocols) in PROVIDERS.items()],
        "samples": [asdict(item) for item in SAMPLES.values()],
        "checkLabels": CHECK_LABELS, "verdicts": VERDICTS,
    }


class QualityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: dict[Literal["facts", "evidence", "boundary"], Literal["confirmed", "disputed", "insufficient"]] = Field(
        min_length=3, max_length=3,
    )
