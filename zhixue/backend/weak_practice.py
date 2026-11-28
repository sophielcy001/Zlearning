from hmac import compare_digest, digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field

from .content import Question
from .db import Attempt, LocalPracticeAttempt, transaction
from .local_reviews import find_review
from .mobile_api import fingerprint
from .model_study_input import Input


WEAKNESS = {"failed", "hinted"}
PRACTICE = {
    "rows-columns": (
        Question("签到表已有「姓名、日期」两列。要记下小周今天签到，应怎样做？",
                 ("给小周创建一列", "在原有两列下增加一条记录", "互换两个列名"), 1),
        Question("要比较不同签到记录的日期，应该查看哪里？",
                 ("标题和表名", "任意一行的所有值", "各行的日期列"), 2),
    ),
    "unique-key": (
        Question("两条试听记录都写着「周三」，用哪个值能单独定位每一条？",
                 ("每条记录的唯一编号", "相同的试听日期", "表的列数"), 0),
        Question("给下一条试听记录选主键时，最需要检查什么？",
                 ("主题是否出现过", "编号是否与任何已有记录重复", "日期是否与上一条相同"), 1),
    ),
    "select-where": (
        Question("只想看时长少于 10 分钟的记录，应选择哪个筛选条件？",
                 ("minutes <= 10", "minutes = 10", "minutes < 10"), 2),
        Question("SELECT topic FROM learning_log WHERE minutes = 10 会返回什么？",
                 ("满足条件的记录的主题", "每条记录的主题和时长", "所有记录的主题"), 0),
    ),
    "order-limit": (
        Question("希望先看耗时最短的记录，并且最多显示三行，应该怎样写？",
                 ("ORDER BY minutes ASC LIMIT 3", "ORDER BY minutes DESC LIMIT 3", "WHERE minutes = 3"), 0),
        Question("时长并列时，想优先看编号较小的记录，应该追加哪个排序条件？",
                 ("再指定 LIMIT 1", "在时长排序后加 id ASC", "把 ORDER BY 换成 WHERE id = 1"), 1),
    ),
}
Round = Annotated[int, Field(ge=1, le=2_147_483_647)]


class PracticeInput(Input):
    basis: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    answers: list[Annotated[int, Field(strict=True, ge=0, le=2)]] = Field(min_length=2, max_length=2)


def practice_context(db, identity, key, source_round, secret):
    project, lesson, review, objective = find_review(db, identity, key)
    source = db.get(Attempt, (lesson.id, key, source_round))
    if source is None or source.result["outcome"] not in WEAKNESS:
        raise HTTPException(409, "请选择报告中有正式薄弱评价的知识点。")
    details = {
        "lessonId": lesson.id, "key": key, "title": objective.title, "projectTopic": project.topic,
        "sourceRound": source_round, "sourceOn": source.submitted_on.isoformat(),
        "sourceOutcome": source.result["outcome"], "explanation": source.result["explanation"],
    }
    reason = None
    if project.paused or not lesson.ready or lesson.learned_on is None:
        reason = "课程当前无法继续专项练习，仍可查看原讲解。"
    elif source.course_version != lesson.version:
        reason = "这次评价使用较早的课程版本，可回看原讲解。"
    question_bank = PRACTICE.get(key)
    if not question_bank:
        reason = "此知识点已有讲解，可从课程复习原内容。"
    public = [{"text": q.text, "options": list(q.options)} for q in question_bank or ()]
    identity_digest = fingerprint({
        "kind": "local-weak-practice", "lesson": lesson.id, "version": lesson.version, "key": review.key,
        "source": source_round, "questions": public, "correct": [q.correct for q in question_bank or ()],
    })
    basis = digest(secret, identity_digest.encode("ascii"), "sha256").hex()
    return {**details, "state": "ready" if not reason else "read_only", "reason": reason,
            "questions": public if not reason else [], "result": None, "basis": basis if not reason else None}, question_bank


def local_practice_router(engine, current_day, session_token):
    secret = session_token.encode('utf-8')
    router = APIRouter(prefix="/api/lessons/{identity}/reviews/{key}/practice")

    @router.get("/{source_round}")
    def view(identity: UUID, key: str, source_round: Round):
        with transaction(engine, write=False) as db:
            saved = db.get(LocalPracticeAttempt, (str(identity), key, source_round))
            return saved.view if saved else practice_context(db, identity, key, source_round, secret)[0]

    @router.post("/{source_round}")
    def submit(identity: UUID, key: str, source_round: Round, data: PracticeInput):
        answers = data.model_dump()
        with transaction(engine) as db:
            saved = db.get(LocalPracticeAttempt, (str(identity), key, source_round))
            if saved:
                if saved.input != answers:
                    raise HTTPException(409, "这次专项练习已交卷，可回看已保存的答案。")
                return saved.view
            context, bank = practice_context(db, identity, key, source_round, secret)
            if context["state"] != "ready" or not compare_digest(context["basis"], data.basis):
                raise HTTPException(409, "专项练习的课程或题组已变化，请刷新后查看。")
            correct = [question.correct for question in bank]
            result = {
                "correctCount": sum(selected == expected for selected, expected in zip(data.answers, correct, strict=True)),
                "answers": data.answers, "correct": correct, "submittedOn": current_day(db).isoformat(),
            }
            completed = {**context, "state": "completed", "result": result}
            db.add(LocalPracticeAttempt(lesson_id=str(identity), key=key, source_round=source_round,
                                        input=answers, view=completed))
            return completed

    return router
