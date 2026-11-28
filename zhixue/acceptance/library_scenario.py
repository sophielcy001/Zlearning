from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from shutil import copyfile
import sqlite3
from urllib.parse import urlencode

from .product import setup_workspace, start_product, until
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

def run_semantic_first_scenario(run, runtime, folder):
    query = "缓存提交验收"
    content = f"本机合成资料\n第一个版本用于{query}。\n"
    revision = sha256(content.encode("utf-8")).hexdigest()
    status_path = "/api/files/semantic-index"
    hybrid_path = "/api/files/search?" + urlencode({"q": query, "format": "txt", "mode": "hybrid"})
    cache = folder / ".semantic" / "e5-small-v1.sqlite3"
    checkpoint = runtime / ".semantic-first-before-commit"
    release = runtime / ".semantic-first-release"
    with run.step("library-semantic-first", {"操作": "并行读取首次提交前后的状态和检索结果",
                                             "工作区": "独立的新建临时工作区", "模型下载": "隔离验收中停用"}) as output:
        copyfile(Path(__file__).with_name("semantic_first_runtime.py"), runtime / "acceptance_semantic_first_runtime.py")
        with start_product(runtime, folder, entry=("acceptance_semantic_first_runtime",)) as api:
            setup_workspace(api)
            identity = api.post("/api/files", {"name": "语义首启验收.txt", "content": content}, expected=201)["id"]
            file_path = f"/api/files/{identity}"
            keyword = one_hit(api, identity, query)
            check(keyword["revision"] == revision and keyword["location"]["number"] == 2,
                  "首次启用前的关键词命中位置或版本有误。")
            baseline = api.get(hybrid_path)
            check(len(baseline["hits"]) == 1 and baseline["hits"][0]["id"] == identity
                  and baseline["hits"][0]["revision"] == revision and baseline["hits"][0]["location"] == keyword["location"]
                  and baseline["semantic"]["state"] == "disabled" and not cache.exists(),
                  "空白工作区的混合检索或索引初始状态异常。")

            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(api.post, status_path, expected=202)
                try:
                    until(checkpoint.is_file, "首次启用未到达 SQLite 提交前的检查点。", timeout=6)
                    visible = cache.exists()
                    state = api.get(status_path)["state"]
                    output.update({"发布前缓存可见": visible, "发布前索引状态": state})
                    check(not visible and state == "disabled", "首次缓存在 SQLite 提交前已对读者可见。")
                    check(api.get(hybrid_path) == baseline and one_hit(api, identity, query) == keyword,
                          "首次提交前的关键词位置或混合检索结果发生变化。")
                finally:
                    release.touch()
                published = pending.result()

            state = api.get(status_path)
            output["发布后索引状态"] = state["state"]
            check(published == state and state["state"] == "preparing" and cache.is_file(),
                  "首次启用后没有读到完整的已提交缓存。")
            with closing(sqlite3.connect(cache.as_uri() + "?mode=ro", uri=True)) as db:
                generation = db.execute("SELECT generation FROM settings WHERE id=1").fetchone()
                check(generation is not None, "首次启用缺少缓存代次。")
                repeated = api.post(status_path, expected=202)
                check(repeated == state and db.execute("SELECT generation FROM settings WHERE id=1").fetchone() == generation
                      and db.execute("PRAGMA quick_check").fetchone() == ("ok",), "重复启用改变了准备中的缓存或缓存快检失败。")
            after = api.get(hybrid_path)
            check(one_hit(api, identity, query) == keyword and after["hits"] == baseline["hits"] and after["semantic"] == state,
                  "首次提交后关键词位置或混合检索回退发生变化。")
            anchor = api.get(file_path + "?" + urlencode({"at": 2, "revision": revision}))["anchor"]
            original = api.get(file_path + "/original", raw=True)
            check(anchor == {**keyword["location"], "text": content.splitlines()[1]} and original == content.encode("utf-8"),
                  "重新打开存档时原文位置或原件字节有误。")
            output.update({"提交前后关键词原文行一致": True, "混合检索保留关键词结果": True,
                           "重复启用沿用缓存代次": True, "SQLite 缓存快检": "ok",
                           "原文位置": keyword["location"]["label"], "原件 SHA-256": sha256(original).hexdigest()})


WIKI_TARGET = "双链验收资料.md"
WIKI_RENAMED = "library/双链改名资料.md"
WIKI_BODY = "# 同名验收资料\n\n保留的正文。"


def resolve_wiki(api, target):
    return api.get("/api/files/wiki/resolve?" + urlencode({"target": target}))


def wiki_readback(api, target, source):
    resolved = resolve_wiki(api, target)
    check(resolved["state"] == "resolved" and len(resolved["choices"]) == 1,
          "稳定双链编号没有解析到唯一文件。")
    choice = resolved["choices"][0]
    check(choice["id"] == target and choice["path"] == WIKI_RENAMED, "改名后双链没有指向原文件。")
    check(api.get(f"/api/files/{target}/original", raw=True) == WIKI_BODY.encode("utf-8"),
          "双链目标改名后原件内容变化。")
    links = api.get(f"/api/files/{target}/backlinks")
    check(len(links) == 1 and links[0]["id"] == source and links[0]["at"] == 3,
          "反向链接没有给出唯一的原文第 3 行。")
    link = links[0]
    anchor = api.get(f"/api/files/{source}?" + urlencode({"at": link["at"], "revision": link["revision"]}))["anchor"]
    check(anchor == {"number": 3, "label": "第 3 行", "text": wiki_line(target)},
          "反向链接无法用记录的版本打开原文。")
    return {"目标路径": choice["path"], "反向链接位置": anchor["label"], "原文版本": link["revision"]}


def wiki_line(target):
    return f"查看 [[{target}|指定资料]] 再继续。"


def run_wiki_scenario(run, api, folder):
    with run.step("library-wiki", {"目标": WIKI_TARGET, "来源": "双链验收笔记.md"}) as output:
        first = api.post("/api/files", {"name": WIKI_TARGET, "content": WIKI_BODY}, expected=201)
        second = api.post("/api/files", {"name": WIKI_TARGET, "content": "# 第二份同名资料"}, expected=201)
        target = first["id"]
        matched = resolve_wiki(api, "双链验收资料")
        check(matched["state"] == "ambiguous" and {item["id"] for item in matched["choices"]} == {target, second["id"]},
              "同名双链没有返回两份可区分的资料。")
        source = api.post("/api/files", {
            "name": "双链验收笔记.md", "content": "# 双链验收笔记\n\n" + wiki_line(target),
        }, expected=201)["id"]
        current = api.get(f"/api/files/{target}")
        original = (folder / current["path"]).resolve()
        renamed = folder / WIKI_RENAMED
        check(original.is_relative_to(renamed.parent.resolve()) and not renamed.exists(),
              "双链改名用的隔离原件位置不符。")
        original.rename(renamed)
        evidence = wiki_readback(api, target, source)
        remaining = resolve_wiki(api, "双链验收资料")
        check(remaining["state"] == "resolved" and [item["id"] for item in remaining["choices"]] == [second["id"]],
              "改名后旧名称的双链没有解析到剩余的同名资料。")
        output.update({"同名候选数量": len(matched["choices"]), **evidence})
    return target, source, evidence


RETURNED_NAME = "归位验收资料.md"
RETURNED_LINE = "原件归位后再次保存的正文。"
RETURNED_BODY = "# 原件归位验收\n" + RETURNED_LINE


def original_return_readback(api, folder, identity, path, source):
    endpoint = f"/api/files/{identity}"
    body = RETURNED_BODY.encode("utf-8")
    revision = sha256(body).hexdigest()
    detail = api.get(endpoint + "?" + urlencode({"at": 2, "revision": revision}))
    anchor = {"number": 2, "label": "第 2 行", "text": RETURNED_LINE}
    check(detail["id"] == identity and detail["status"] == "ready" and detail["path"] == path
          and detail["revision"] == revision and detail["anchor"] == anchor,
          "再次保存时丢失原文件编号、版本或第 2 行定位。")
    check(api.get(endpoint + "/original", raw=True) == body and (folder / path).read_bytes() == body,
          "再次保存的原件字节与当前版本不一致。")
    for target in (path, path.removeprefix("library/")):
        answer = resolve_wiki(api, target)
        check(answer["state"] == "resolved" and [(x["id"], x["path"]) for x in answer["choices"]] == [(identity, path)],
              "原有双链未定位到再次保存的文件。")
    backlinks = api.get(endpoint + "/backlinks")
    check([(x["id"], x["at"]) for x in backlinks] == [(source, 1), (source, 3)],
          "原件归位后的两处反向链接丢失。")
    return {"原文件编号": identity, "原路径": path, "文件 SHA-256": revision, "原文位置": anchor["label"],
            "反向链接位置": [x["at"] for x in backlinks]}


def run_original_return_scenario(run, api, folder):
    with run.step("library-original-return", {"资料": RETURNED_NAME, "操作": "移出、归位和再次原子替换保存"}) as output:
        initial = "# 原件归位验收\n归位前的首版。"
        returned = "# 原件归位验收\n移出期间保存的正文。"
        identity = api.post("/api/files", {"name": RETURNED_NAME, "content": initial}, expected=201)["id"]
        endpoint = f"/api/files/{identity}"
        file = api.get(endpoint)
        path = file["path"]
        original = (folder / path).resolve()
        parked = folder / "验收期间移出资料.md"
        check(original.is_relative_to((folder / "library").resolve()) and not parked.exists(),
              "原件归位样本不在隔离工作区。")
        check(original.read_bytes() == initial.encode("utf-8"), "初始合成原件字节不一致。")
        source = api.post("/api/files", {"name": "归位验收旧路径双链.md",
                                        "content": f"[[{path}]]\n\n[[{path.removeprefix('library/')}]]"}, expected=201)["id"]
        before = original.stat()
        original.rename(parked)
        away = api.get(endpoint)
        check(away["id"] == identity and away["status"] == "missing", "移出后没有记录原件缺失状态。")
        parked.write_bytes(returned.encode("utf-8"))
        parked.rename(original)
        after = original.stat()
        check(before.st_ino and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
              "归位后的隔离文件身份发生变化。")
        restored = api.get(endpoint)
        check(restored["id"] == identity and restored["status"] == "ready" and restored["content"] == returned,
              "归位后的原件无法按原编号打开。")
        replacement = original.with_name(".acceptance-save")
        with replacement.open("xb") as stream:
            stream.write(RETURNED_BODY.encode("utf-8"))
        replacement.replace(original)
        evidence = original_return_readback(api, folder, identity, path, source)
        output.update({"原件离开时的状态": away["status"], "归位后文件可读": True, **evidence})
    return identity, path, source, evidence
