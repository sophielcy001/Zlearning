from typing import Literal

from fastapi import APIRouter

from .db import TeachingPreference, transaction
from .model_study_input import Input


TeachingStyle = Literal["direct", "guided"]
DEFAULT_STYLE = "direct"
INSTRUCTIONS = {
    "direct": "教学风格：直接讲解。先给出清楚的回答，再说明原因或步骤，并在有帮助时给一个简短示例。",
    "guided": "教学风格：启发式引导。先承接用户的思路，给出一条提示或提出一个能推进理解的问题。"
              "如果用户明确要求完整解释，就给出完整答案。",
}


class StyleUpdate(Input):
    style: TeachingStyle


def teaching_style(db) -> str:
    saved = db.get(TeachingPreference, 1)
    return saved.style if saved else DEFAULT_STYLE


def style_instruction(style: str | None) -> str:
    return INSTRUCTIONS.get(style, INSTRUCTIONS[DEFAULT_STYLE])


def teaching_style_router(engine) -> APIRouter:
    router = APIRouter(prefix="/api/teaching-style")

    @router.get("")
    def read():
        with transaction(engine, write=False) as db:
            return {"style": teaching_style(db)}

    @router.put("")
    def save(update: StyleUpdate):
        with transaction(engine) as db:
            saved = db.get(TeachingPreference, 1)
            if saved:
                saved.style = update.style
            else:
                db.add(TeachingPreference(id=1, style=update.style))
            return {"style": update.style}

    return router
