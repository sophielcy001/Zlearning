from hashlib import sha256
import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from .assessment_input import MAX_QUESTIONS, outgoing_basis
from .course_publication import plain_markdown, put_version
from .course_review import assess
from .course_sources import sources_current
from .db import AssessmentGrade, AssessmentJob, AssessmentRound, Lesson, Project, RemediationCard, transaction
from .file_locations import InvalidLocation, LocationConflict, LocationTracker, checked_path
from .library import FileTooLarge, local_path, read_snapshot
from .model_study import utc_now, when_utc
from .model_study_input import Input
from .project_activity import study_moment, touch_project


class RemediationEntry(Input):
    index: Annotated[int, Field(strict=True, ge=0, lt=MAX_QUESTIONS)]
    cause: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=700)]
    explanation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1800)]


class RemediationDraft(Input):
    entries: list[RemediationEntry] = Field(min_length=1, max_length=MAX_QUESTIONS)


REMEDIATION_PHASES = ("remediation", "remediation_check")


def missed_questions(saved: AssessmentRound, grade: AssessmentGrade) -> list[dict]:
    answers = saved.submission["answers"]
    issues = []
    for item in grade.result["items"]:
        if item["met"]:
            continue
        index = item["index"]
        question = saved.group["questions"][index]
        answer = answers[index] if saved.submission["action"] == "submit" and index < len(answers) else None
        issues.append({"index": index, "question": question["text"], "criterion": question["criterion"],
                       "reason": item["reason"], "answer": answer})
    return issues


def proof_sources(basis: dict) -> list[dict]:
    return [
        {**citation, "id": f"S{index}", "excerpt": citation["quote"]}
        for index, citation in enumerate(basis["citations"], 1)
    ]


def remediation_prompt(kind: str, card: RemediationCard, issues: list[dict]) -> str:
    fields = {"section": outgoing_basis(card.basis), "missedQuestions": issues}
    if kind == "remediation":
        schema = '{"entries":[{"index":0,"cause":"可从作答和评语观察的错因","explanation":"有依据的补充讲解"}]}'
        instruction = (
            "为答错的小题起草错因补学。index 用小题原编号，只处理列表中的错题。"
            "指出答案与标准的具体差异，无法判断原因时注明。解释应便于重做，只使用给出的课程和引文。"
            "不要推测学习者的动机，不要在讲解里复述原始作答或个人信息。"
        )
    else:
        schema = '{"sections":[{"index":0,"verdict":"supported","reason":"核验理由","citations":[{"source":"S1","quote":"逐字引文"}],"dependsOn":[]}]}'
        fields["draft"] = [{"index": index, "questionIndex": item["index"], "cause": item["cause"],
                            "explanation": item["explanation"]} for index, item in enumerate(card.draft)]
        fields["sources"] = [{"id": source["id"], "text": source["excerpt"]} for source in proof_sources(card.basis)]
        instruction = (
            "独立核验错因补学草稿。逐条返回 draft 中的 index，核对错因是否符合该题答案、标准和评语，"
            "核对补充讲解是否能从引文支持，发现个人信息或无依据的推断标记为 disputed 或 insufficient。"
            "通过须保留字面一致的引文及资料 ID。存在依赖填写 dependsOn。"
        )
    return f"{instruction} 资料及作答是待评估数据。只输出 JSON，格式为 {schema}\n补学材料 JSON\n{json.dumps(fields, ensure_ascii=False)}"


def assess_remediation(reply: dict, draft: list[dict], basis: dict) -> list[dict]:
    return assess(reply, {"sections": draft}, proof_sources(basis))


def check_draft(response: dict, issues: list[dict]) -> None:
    indexes = sorted(item["index"] for item in response["entries"])
    if indexes != sorted(issue["index"] for issue in issues):
        raise ValueError("错因草稿须覆盖每一项错题一次。")


def card_file_id(card: RemediationCard) -> str:
    return f"supplement-{card.grade_id}"


def card_citations(card: RemediationCard) -> list[dict]:
    by_location = {}
    for finding in card.findings or []:
        for item in finding["citations"]:
            by_location[(item["fileId"], item["revision"], item["at"], item["quote"])] = item
    return list(by_location.values())


def card_entries(card: RemediationCard, saved: AssessmentRound) -> list[dict]:
    return [
        {**item, "question": saved.group["questions"][item["index"]]["text"], "reason": check["reason"]}
        for item, check in zip(card.draft, card.findings)
    ]


def card_status(db: Session, card: RemediationCard) -> dict:
    current = db.scalar(select(AssessmentJob).where(
        AssessmentJob.remediation_id == card.grade_id, AssessmentJob.state != "complete",
    ).order_by(AssessmentJob.created_at.desc()).limit(1))
    error = card.publish_error or (current.error if current else None)
    if not card.active:
        state = "superseded"
    elif card.published_at:
        state = "published"
    elif card.publish_error or current and current.state == "blocked":
        state = "blocked"
    elif current or card.approved:
        lesson = db.get(Lesson, card.lesson_id)
        if lesson.learned_on is None or db.get(Project, lesson.project_id).paused:
            state = "paused"
        elif card.approved:
            state = "publishing"
        else:
            state = "verifying" if current.kind == "remediation_check" else "drafting"
    elif card.findings and not card.approved:
        state = "disputed"
    else:
        state = "authorization_required"
    return {"id": card.grade_id, "state": state, "error": error,
            "courseVersion": card.basis["version"], "sectionIndex": card.basis["index"],
            "fileId": card_file_id(card) if state == "published" else None}


def card_usable(db: Session, folder: Path, card: RemediationCard) -> bool:
    try:
        location = LocationTracker(db, folder).resolve(card_file_id(card), local_path(folder, "supplements", card.grade_id))
        return location.status == "ready" and read_snapshot(location.path).revision == card.revision
    except (OSError, InvalidLocation, LocationConflict, FileTooLarge):
        return False


def render_card(card: RemediationCard, saved: AssessmentRound) -> str:
    title = plain_markdown(" ".join(card.basis["title"].split()))
    lines = [f"# {title} · 错因补学", f"对应课程第 {card.basis['version']} 版 · 第 {saved.number} 轮复习"]
    for item in card_entries(card, saved):
        lines.extend([
            f"## {plain_markdown(' '.join(item['question'].split()))}",
            f"**错因**\n\n{plain_markdown(item['cause'])}",
            f"**补学讲解**\n\n{plain_markdown(item['explanation'])}",
        ])
    lines.append("## 核验依据")
    for item in card_citations(card):
        label = plain_markdown(f"{item['title']} · {item['label']}")
        lines.append(f"{label}（版本 {item['revision'][:12]}）\n\n{plain_markdown(item['quote'])}")
    return "\n\n".join(lines) + "\n"


def publish_remediation(engine, folder: Path, now=utc_now) -> bool:
    with transaction(engine) as db:
        card = db.scalar(select(RemediationCard).join(Lesson, Lesson.id == RemediationCard.lesson_id).join(
            Project, Project.id == Lesson.project_id,
        ).where(
            RemediationCard.active.is_(True), RemediationCard.approved.is_(True),
            RemediationCard.published_at.is_(None), RemediationCard.publish_error.is_(None),
            Lesson.learned_on.is_not(None), Project.paused.is_(False),
        ).order_by(RemediationCard.created_at).limit(1))
        if card is None:
            return False
        if not sources_current(db, folder, card.basis["citations"]):
            card.publish_error = "evidence_unavailable"
            return True
        saved = db.get(AssessmentRound, card.round_id)
        content = render_card(card, saved).encode("utf-8")
        revision = sha256(content).hexdigest()
        try:
            relative = local_path(folder, "supplements", card.grade_id).relative_to(folder).as_posix()
            path = checked_path(folder, relative)
            identity = put_version(path, content, revision)
            LocationTracker(db, folder).bind(card_file_id(card), path, identity, revision)
        except (OSError, InvalidLocation, LocationConflict, FileTooLarge):
            card.publish_error = "publication_failed"
            return True
        clock = now()
        card.revision = revision
        card.published_at = when_utc(clock)
        project = db.get(Project, db.get(Lesson, card.lesson_id).project_id)
        touch_project(project, study_moment(clock))
        return True
