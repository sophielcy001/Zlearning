from sqlalchemy import select
from sqlalchemy.orm import aliased

from .db import ChatMessage, ChatReply
from .money_budget import MONEY_ERRORS


HISTORY_TURNS = 3
HISTORY_TEXT_LIMIT = 2000
REPLY_ERRORS = {
    **MONEY_ERRORS,
    "daily_budget": "已达到每日模型额度。额度恢复后可继续。",
    "configuration_changed": "主模型配置已变更，请重新确认发送范围。",
    "missing_key": "主模型缺少可用的密钥，请检查模型设置。",
    "credential_store": "无法读取本机模型密钥，请检查系统凭据后重试。",
    "unauthorized": "主模型拒绝了凭据，请检查模型设置。",
    "quota": "主模型服务报告余额或额度不足。",
    "rate_limited": "主模型服务请求过于频繁，请稍后重试。",
    "timeout": "等待主模型答复超时。",
    "network": "暂时无法连接主模型。",
    "service_unavailable": "主模型服务暂不可用。",
    "redirect": "主模型地址发生跳转，请核对连接地址。",
    "invalid_response": "主模型返回的内容无法使用。",
    "restored": "此问题从备份恢复，请重新确认后继续。",
    "dispatch_failed": "后台任务在请求发送前发生异常，请重试。",
    "uncertain": "上次发送结果未能确认。再次发送可能增加一次费用。",
}


def completed_history(db, conversation_id: str) -> list[dict]:
    answer = aliased(ChatMessage)
    rows = db.execute(select(ChatMessage.text, answer.text).select_from(ChatReply)
                      .join(ChatMessage, ChatMessage.id == ChatReply.id)
                      .join(answer, answer.id == ChatReply.reply_id)
                      .where(ChatMessage.conversation_id == conversation_id, ChatReply.state == "answered")
                      .order_by(ChatMessage.position.desc()).limit(HISTORY_TURNS)).all()
    return [{"role": role, "text": text[:HISTORY_TEXT_LIMIT]} for question, reply in reversed(rows)
            for role, text in (("user", question), ("assistant", reply))]


def model_reply_metadata(db, messages: list[str]) -> dict:
    rows = db.execute(select(ChatReply, ChatMessage.text).join(ChatMessage, ChatMessage.id == ChatReply.id)
                      .where(ChatReply.reply_id.in_(messages)))
    return {reply.reply_id: {"modelReply": {
        "questionId": reply.id, "questionText": question, "hasHistory": bool(reply.history), "state": reply.state,
        "recipient": reply.recipient, "errorCode": reply.error,
        "error": REPLY_ERRORS.get(reply.error, "暂时无法完成模型回复。") if reply.state == "blocked" else None,
    }} for reply, question in rows}
