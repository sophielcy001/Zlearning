from uuid import uuid4

from test_chat_model import confirmed, draft, worker
from test_lesson_assistant import PASS_REPLY, ask_one, open_thread, permit_assistant
from test_model_assessments import COMPOSED, GRADE, assess_one, permit, review, source_lesson
from test_model_study import ok, session


STYLE = "/api/teaching-style"


def choose_style(http, style):
    assert ok(http.put(STYLE, json={"style": style})) == {"style": style}


def test_teaching_style_persists_in_its_workspace_and_rejects_other_values(tmp_path):
    first = tmp_path / "first"
    with session(first) as http:
        assert ok(http.get(STYLE)) == {"style": "direct"}
        assert http.put(STYLE, json={"style": "different-rubric"}).status_code == 422
        choose_style(http, "guided")
    with session(first) as http:
        assert ok(http.get(STYLE)) == {"style": "guided"}
    with session(tmp_path / "other") as http:
        assert ok(http.get(STYLE)) == {"style": "direct"}


def test_chat_and_course_capture_same_style_without_altering_assessment_scope(tmp_path):
    general_calls, course_prompts, assessment_prompts = [], [], []
    with session(tmp_path) as http:
        lesson, _ = source_lesson(http, tmp_path, adopt=True)
        course_thread = open_thread(http, lesson)
        permit_assistant(http, lesson)
        general = f"/api/chat-model/{uuid4()}"
        question = draft("如何从试印发现字行有问题？")
        direct, stale = confirmed(http, general, question)
        assert direct["style"] == "direct"

        choose_style(http, "guided")
        assert http.post(general + "/send", json=stale).status_code == 409
        guided, authored = confirmed(http, general, question)
        assert guided["style"] == "guided"
        saved = ok(http.post(general + "/send", json=authored))
        course = f"/api/conversations/{course_thread}/questions"
        ok(http.post(course, json=draft("可以先提示我该检查哪里吗？")))

        choose_style(http, "direct")
        assert worker(tmp_path, general_calls)
        assert ask_one(tmp_path, PASS_REPLY, course_prompts)
        delivery = ok(http.get(f"/api/conversations/{course_thread}"))["messages"][-1]["answer"]
        assert delivery["support"] == "grounded" and delivery["citations"]
        assert ok(http.post(general + "/send", json=authored))["id"] == saved["id"]

        ok(http.post(course, json=draft("试印前如何固定活字？")))
        assert ask_one(tmp_path, PASS_REPLY, course_prompts)
        assert "教学风格：启发式引导" in general_calls[0][1] and "教学风格：启发式引导" in course_prompts[0]
        assert "教学风格：直接讲解" in course_prompts[1]

        choose_style(http, "guided")
        permit(http, lesson)
        assert assess_one(tmp_path, COMPOSED, assessment_prompts)
        assert "教学风格" not in assessment_prompts[0][1]
        item = review(http, lesson)
        ok(http.post("/api/demo/next-day"))
        group = ok(http.get(f"/api/lessons/{lesson}/assessments/{item['key']}"))["group"]
        target = f"/api/assessments/{group['id']}"
        ok(http.post(target + "/submit", json={"action": "submit", "answers": ["固定在版框内。", "纸张尺寸。"]}))
        assert assess_one(tmp_path, GRADE, assessment_prompts)
        assert "教学风格" not in assessment_prompts[-1][1]
        assert ok(http.get(target))["result"]["outcome"] == "passed"
        assert review(http, lesson)["stage"] == 1
