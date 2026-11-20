from urllib.parse import urlencode

from .steps import check


FILE = "验收用检索资料.txt"
ORIGINAL_LINE = "Straße " * 90 + "现在记录中文复习计划和重点。" + " 后续正文" * 48
ORIGINAL = "本机合成资料说明\n\n" + ORIGINAL_LINE
CURRENT_LINE = "索引更新后核对阅读计划。"
CURRENT = "本机合成资料说明\n" + CURRENT_LINE


def matches(api, identity, query):
    params = urlencode({"q": query, "format": "txt", "mode": "keyword"})
    return [hit for hit in api.get(f"/api/files/search?{params}")["hits"] if hit["id"] == identity]


def one_hit(api, identity, query):
    hits = matches(api, identity, query)
    check(len(hits) == 1, f"检索「{query}」没有返回唯一的验收原文行。")
    return hits[0]


def run_library_scenario(run, api, folder):
    queries = ("复", "复习", "复习计划")
    with run.step("library-search", {"文件": FILE, "查询词": list(queries)}) as output:
        imported = api.post("/api/files", {"name": FILE, "content": ORIGINAL}, expected=201)
        identity = imported["id"]
        endpoint = f"/api/files/{identity}"
        current = api.get(endpoint)
        location = {"number": 3, "label": "第 3 行"}
        for query in queries:
            hit = one_hit(api, identity, query)
            check(hit["location"] == location and hit["revision"] == current["revision"]
                  and "中文复习计划" in hit["snippet"], "检索摘要、位置或版本与合成原文不一致。")
        original_revision = current["revision"]
        preview = api.get(f"{endpoint}?at=3&revision={original_revision}")
        check(preview["anchor"] == {**location, "text": ORIGINAL_LINE}, "检索结果无法定位到原文第 3 行。")
        output.update({"命中位置": location["label"], "片段包含": "中文复习计划", "对应原文": True})

    with run.step("library-reindex", {"操作": "外部编辑临时原件后重建索引"}) as output:
        source = (folder / current["path"]).resolve()
        check(source.is_relative_to((folder / "library").resolve()), "验收文件不在隔离知识库内。")
        check(source.read_bytes() == ORIGINAL.encode("utf-8"), "导入后的原件字节与合成样本不一致。")
        replacement = CURRENT.encode("utf-8")
        source.write_bytes(replacement)
        check(not matches(api, identity, "复习计划"), "外部修改后仍返回旧的索引命中。")
        updated = one_hit(api, identity, "核对阅读计划")
        check(updated["revision"] != original_revision, "外部编辑后归档版本没有更新。")
        api.post("/api/files/reindex")
        rebuilt = one_hit(api, identity, "核对阅读计划")
        revision = rebuilt["revision"]
        anchor = api.get(f"{endpoint}?at=2&revision={revision}")["anchor"]
        check(anchor == {"number": 2, "label": "第 2 行", "text": CURRENT_LINE}
              and rebuilt["location"] == {"number": 2, "label": "第 2 行"} and revision == updated["revision"]
              and not matches(api, identity, "复习计划") and source.read_bytes() == replacement,
              "索引重建后新定位或原件内容发生变化。")
        output.update({"命中位置": anchor["label"], "旧命中消失": True, "版本已更新": True,
                       "索引重建后原件逐字一致": True})

    with run.step("library-anchor", {"操作": "打开旧版本及缺失行，再打开当前文件"}) as output:
        api.get(f"{endpoint}?at=3&revision={original_revision}", expected=409)
        unavailable = api.get(f"{endpoint}?at=3&revision={revision}", expected=404)
        api.get(f"{endpoint}?at=3", expected=404)
        check("找不到文件中的这一行或段落" in unavailable.get("detail", ""), "失效位置没有返回可理解的说明。")
        latest = api.get(endpoint)
        check(latest["revision"] == revision and latest["content"] == CURRENT,
              "切换到当前文件后无法读取有效原文。")
        output.update({"过期版本": "HTTP 409", "缺失位置": "HTTP 404", "当前文件仍可打开": True})
