from hashlib import sha256
from urllib.parse import urlencode

import pymupdf

from .simulation import OCR, OCR_DRAFT
from .steps import check


SCAN = "Selected Scan Page"
CORRECTED = "人工确认的试印顺序"


def scanned_pdf():
    with pymupdf.open() as typeset, pymupdf.open() as scanned:
        for title in ("Different Cover Page", SCAN):
            page = typeset.new_page(width=540, height=180)
            page.insert_text((35, 95), title, fontsize=34)
            image = page.get_pixmap(dpi=180)
            scanned.new_page(width=540, height=180).insert_image(page.rect, pixmap=image)
        return scanned.tobytes(garbage=4, deflate=True)


def run_ocr_scenario(run, api, models, address):
    source = scanned_pdf()
    name = "独立验收双页扫描.pdf"
    with run.step("ocr", {"来源": name, "页码": 2}) as output:
        uploaded = api.post("/api/files/upload?" + urlencode({"name": name}), source, expected=201)
        file = f"/api/files/{uploaded['id']}"
        original = api.get(file)
        check(original["pageCount"] == 2 and original["unreadablePages"] == [1, 2], "样本应为两页未索引的扫描图。")
        page = f"{file}/pages/2"
        revision = original["revision"]
        ocr = f"{page}/ocr"
        local = api.post(ocr, {"revision": revision})
        check(SCAN in " ".join(local["text"].split()) and not models.calls("ocr"), "本机 OCR 未识别所选页或产生了外部调用。")

        connection = api.post("/api/models/connections", {
            "name": "隔离单页识别", "provider": "custom", "protocol": "chat-completions", "model": OCR, "endpoint": address,
        })
        service = {"connectionId": connection["id"]}
        api.authorize("/api/recognition/voice", service)
        voice = api.get("/api/recognition/voice")["authorization"]
        api.post(f"{ocr}/external", {"revision": revision, "authorizationId": voice["id"]}, expected=409)
        consent = api.authorize("/api/recognition/ocr", service)
        approved = api.get("/api/recognition/ocr")["authorization"]
        check(consent["endpoint"] == f"{address}/chat/completions" and "单页 PNG" in consent["scope"]
              and approved["id"] != voice["id"] and not models.calls("ocr"), "用途授权或预览的发送范围不正确。")
        request = {"revision": revision, "authorizationId": approved["id"]}
        image = api.get(f"{page}/image", raw=True)
        cover = api.get(f"{file}/pages/1/image", raw=True)
        digest = sha256(image).hexdigest()
        draft = api.post(f"{ocr}/external", request)
        check(image != cover and models.calls("ocr") == [{"phase": "ocr", "sha256": digest}],
              "模拟服务收到的页图与第 2 页渲染内容不同。")
        check(draft == {"text": OCR_DRAFT, "truncated": False}
              and not api.get("/api/files/search?" + urlencode({"q": OCR_DRAFT}))["hits"],
              "外部识别未生成草稿，或草稿未经校对已进入检索。")
        output.update({"本机识别文字": SCAN, "语音授权不能用于本页": "HTTP 409",
                       "本次请求内容": "只有 PDF 第 2 页 PNG、识字指令与模型字段", "页图 SHA-256": digest,
                       "外部请求数": 1, "草稿进入索引": False})

    with run.step("ocr-conflict", {"操作": "两个草稿基于尚无校对的第 2 页"}) as output:
        saved = api.put(ocr, {"revision": revision, "textRevision": None, "text": CORRECTED})
        conflict = api.put(ocr, {"revision": revision, "textRevision": None, "text": draft["text"]}, expected=409)
        confirmed = api.get(f"{file}?at=2")["anchor"]
        hits = api.get("/api/files/search?" + urlencode({"q": CORRECTED}))["hits"]
        check("已在其他窗口保存" in conflict.get("detail", "") and confirmed["text"] == CORRECTED
              and confirmed["textRevision"] == saved["textRevision"] and len(hits) == 1
              and hits[0]["id"] == uploaded["id"] and hits[0]["location"]["number"] == 2
              and not api.get("/api/files/search?" + urlencode({"q": OCR_DRAFT}))["hits"],
              "旧校对覆盖了已确认文字，或索引未按新校对刷新。")
        check(api.get(f"{file}/original", raw=True) == source, "校对后 PDF 原件的字节已变化。")
        api.request("DELETE", "/api/recognition/ocr")
        api.post(f"{ocr}/external", request, expected=409)
        check(api.get("/api/recognition/ocr")["authorization"] is None
              and api.get("/api/recognition/voice")["authorization"] == voice and len(models.calls("ocr")) == 1,
              "单页识别撤销后仍发送请求，或改变了语音授权。")
        output.update({"旧版本保存": "HTTP 409", "索引文本": CORRECTED, "索引定位": "第 2 页",
                       "原件逐字一致": True, "撤销后请求": "HTTP 409", "语音授权仍有效": True})
