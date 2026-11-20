from base64 import b64decode
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Lock, Thread

from backend.recognition import OCR_PROMPT


SOURCE = "拣字后用版框固定活字，试印前需要检查字行是否平整。"
PRIVATE_LINE = "这一行模拟未授权的笔记内容。"
BODY = "将排好的活字固定在版框中。"
QUESTION = "试印前为什么要检查字行？"
ANSWER = "固定好活字后，先检查字行是否平整，再试印。"
ADDITION = "答疑整理 · 字行检查"
MAIN = "acceptance-main"
REVIEWER = "acceptance-review"
OCR = "acceptance-ocr"
OCR_DRAFT = "合成图像识别草稿"
REVISED_SOURCE = "固定版框后先检查墨色是否均匀，再检查字行是否平整。"
REVISED_BODY = "固定版框后，依次检查墨色和字行，再开始试印。"
REVISION_NOTE_PRIVATE = "本机摘录私有尾行 0051"
REVISION_DRAFT = {"sections": [
    {"title": "试印前的两项检查", "text": REVISED_BODY, "checks": ["检查墨色和字行"]},
    {"title": "核对检查顺序", "text": "先确认墨色均匀，再检查字行是否平整。", "checks": ["解释先后顺序"]},
]}
REVISION_FINDINGS = {"sections": [
    {"index": index, "verdict": "supported", "reason": "新版合成资料支持该项检查。",
     "citations": [{"source": "S1", "quote": REVISED_SOURCE}], "dependsOn": []}
    for index in range(len(REVISION_DRAFT["sections"]))
]}
PLAN = {"stages": [{"name": "印刷基础", "lessons": [
    {"key": "type", "title": "活字排版", "outcome": "说明试印前的检查内容"},
]}]}
DRAFT = {"sections": [
    {"title": "固定活字", "text": BODY, "checks": ["固定方式"]},
    {"title": "温度", "text": "试印前必须把纸加热。", "checks": ["温度依据"]},
    {"title": "恒温步骤", "text": "按照前文温度设定恒温。", "checks": ["适用条件"]},
    {"title": "版面规格", "text": "每行都必须排三十字。", "checks": ["行数依据"]},
]}
CITATION = {"source": "S1", "quote": SOURCE}
FINDINGS = {"sections": [
    {"index": 0, "verdict": "supported", "reason": "资料描述了版框。", "citations": [CITATION], "dependsOn": []},
    {"index": 1, "verdict": "disputed", "reason": "所选原文没有温度依据。", "citations": [], "dependsOn": []},
    {"index": 2, "verdict": "supported", "reason": "本段依赖前段的温度说法。", "citations": [CITATION], "dependsOn": [1]},
    {"index": 3, "verdict": "supported", "reason": "模拟一段无法定位的引文。",
     "citations": [{"source": "S1", "quote": "每行限定三十字。"}], "dependsOn": []},
]}
ASSESSMENT = {"objectives": [{
    "title": "说明固定活字的方法", "ability": "apply", "criterion": "说明使用版框固定活字",
    "group": {"hint": "回想选定的资料原句。", "questions": [
        {"text": "如何固定排好的活字？", "criterion": "说明使用版框", "required": True, "quote": SOURCE},
        {"text": "试印前可以先检查哪项？", "criterion": "检查字行是否平整", "required": False, "quote": SOURCE},
    ]},
}]}
GRADE = {
    "items": [
        {"index": 0, "met": True, "reason": "作答说明了使用版框固定活字。"},
        {"index": 1, "met": False, "reason": "作答缺少字行检查。"},
    ],
    "explanation": "核心作答满足标准，仍可补练试印前的检查。",
}

# Each message or error is routed by its role and the phase in the production prompt.
RESPONSES = (
    ("route", MAIN, "制定学习路线", PLAN),
    ("draft", MAIN, "起草一个知识点", DRAFT),
    ("revision-author", MAIN, "修订材料 JSON", REVISION_DRAFT),
    ("verify", REVIEWER, "复核材料 JSON", FINDINGS),
    ("answer", MAIN, "答疑上下文 JSON", {"answer": ANSWER, "support": "grounded", "citations": [CITATION]}),
    ("consolidate", MAIN, "整理同一节课的答疑", {
        "section": {"title": ADDITION, "text": ANSWER, "checks": ["试印前的检查"]},
    }),
    ("consolidation-review", REVIEWER, "独立核验学完后的答疑补充", {
        "verdict": "supported", "reason": "试印检查可定位到合成原句。", "citations": [CITATION],
    }),
    ("questions", MAIN, "拟定可独立检验", ASSESSMENT),
    ("grade", MAIN, "评价整组答案", GRADE),
)


@dataclass
class Gate:
    arrived: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)


class SimulatedModels:
    def __init__(self, output_tokens=None):
        self.lock = Lock()
        self.observations = []
        self.gates = {}
        self.next_status = {}
        self.output_tokens = output_tokens or {}

    def calls(self, phase):
        with self.lock:
            return [call for call in self.observations if call["phase"] == phase]

    def phases(self):
        with self.lock:
            return [call["phase"] for call in self.observations]

    def fail_next(self, phase, status):
        with self.lock:
            self.next_status[phase] = status

    @contextmanager
    def hold(self, phase):
        gate = Gate()
        with self.lock:
            self.gates[phase] = gate
        try:
            yield gate
        finally:
            gate.release.set()
            with self.lock:
                del self.gates[phase]

    def reply_ocr(self, payload):
        message, = payload["messages"]
        instruction, picture = message["content"]
        header, _, encoded = picture["image_url"]["url"].partition(",")
        if (set(payload) != {"model", "max_tokens", "messages"} or set(message) != {"role", "content"}
                or message["role"] != "user" or set(instruction) != {"type", "text"}
                or set(picture) != {"type", "image_url"} or set(picture["image_url"]) != {"url"}
                or instruction["type"] != "text" or instruction["text"] != OCR_PROMPT
                or picture["type"] != "image_url" or header != "data:image/png;base64"):
            raise ValueError("Expected only a page image and its instructions")
        image = b64decode(encoded, validate=True)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Expected a PNG")
        with self.lock:
            self.observations.append({"phase": "ocr", "sha256": sha256(image).hexdigest()})
        return 200, {"choices": [{"message": {"content": OCR_DRAFT}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 60, "completion_tokens": 12}}

    def reply(self, model, prompt):
        response = next((item for item in RESPONSES if item[1] == model and item[2] in prompt), None)
        phase = response[0] if response else "unexpected"
        # The revision review uses the same production prompt as the initial independent review.
        if phase == "verify" and REVISED_BODY in prompt:
            phase = "revision-review"
        with self.lock:
            status = self.next_status.pop(phase, 200) if response else 400
            gate = self.gates.get(phase)
            self.observations.append({
                "phase": phase, "model": model, "status": status,
                "includesSource": SOURCE in prompt, "includesUnauthorizedLine": PRIVATE_LINE in prompt,
                "includesUpdatedSource": REVISED_SOURCE in prompt, "includesRevisionNote": REVISION_NOTE_PRIVATE in prompt,
            })
        if response is None:
            return status, {"error": "Unexpected synthetic request"}
        if gate:
            gate.arrived.set()
            if not gate.release.wait(14):
                return 504, {"error": "Synthetic barrier timed out"}
        if status != 200:
            return status, {"error": "Synthetic temporary response"}
        result = REVISION_FINDINGS if phase == "revision-review" else response[3]
        return 200, {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
                     "usage": {"prompt_tokens": 40, "completion_tokens": self.output_tokens.get(phase, 20)}}


@contextmanager
def local_models(*, output_tokens=None):
    models = SimulatedModels(output_tokens)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            try:
                size = int(self.headers["Content-Length"])
                if not 0 < size <= 2 * 1024 * 1024:
                    raise ValueError("Invalid synthetic request size")
                payload = json.loads(self.rfile.read(size))
                if payload["model"] == OCR:
                    status, body = models.reply_ocr(payload)
                else:
                    status, body = models.reply(payload["model"], payload["messages"][0]["content"])
            except (KeyError, IndexError, TypeError, ValueError):
                status, body = 400, {"error": "Invalid synthetic request"}
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except OSError:
                pass

        def log_message(self, _format, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            yield models, f"http://127.0.0.1:{server.server_port}/v1"
        finally:
            server.shutdown()
            thread.join()
