from secrets import token_urlsafe
from urllib.parse import urlencode

from .library_scenario import resolve_wiki
from .product import restore_backup, setup_workspace, start_product
from .steps import check


ORIGINAL = "# 验收用回收原件\n\n合成资料的原文定位。"
ORDINARY_PATH = "library/imports/整库找回资料.md"
REVIEWED_PATH = "library/imports/核对后找回资料.md"


def archived_path(folder, entry):
    archive = (folder / entry["archivePath"]).resolve()
    check(archive.is_relative_to((folder / "library" / ".trash").resolve()), "归档原件超出隔离回收站。")
    check(archive.read_bytes() == ORIGINAL.encode(), "回收站原件与已知版本不一致。")
    return archive


def restored_evidence(api, folder, item, source, paths, target):
    endpoint = f"/api/files/{item['id']}"
    data = api.get(endpoint + "?" + urlencode({"at": 3, "revision": item["revision"]}))
    check(data["id"] == item["id"] and data["revision"] == item["revision"] and data["path"] == target
          and data["anchor"]["text"] == ORIGINAL.splitlines()[2], "找回后的原文件编号、版本或原文位置有误。")
    check(api.get(endpoint + "/original", raw=True) == ORIGINAL.encode()
          and (folder / target).read_bytes() == ORIGINAL.encode(), "找回后原件字节发生变化。")
    for path in paths:
        result = resolve_wiki(api, path)
        check(result["state"] == "resolved" and [(x["id"], x["path"]) for x in result["choices"]] == [(item["id"], target)],
              "旧路径双链没有指向恢复后的原件。")
    backlinks = api.get(endpoint + "/backlinks")
    check([(x["id"], x["at"]) for x in backlinks] == [(source, 1), (source, 3)], "找回后反向链接的两处位置有误。")
    return {"原文件编号": data["id"], "原文版本": data["revision"], "恢复位置": target,
            "原文位置": data["anchor"]["label"], "反向链接位置": [x["at"] for x in backlinks]}


def run_trash_scenario(run, runtime, scratch):
    producer = scratch / "trash-producer"
    receiver = scratch / "trash-receiver"
    media = scratch / "trash-transfer"
    password = token_urlsafe(24)

    with start_product(runtime, producer) as api:
        with run.step("library-trash", {"资料": "合成资料", "链接": ["完整旧路径", "相对旧路径"]}) as output:
            setup_workspace(api)
            created = api.post("/api/files", {"name": "验收用回收原件.md", "content": ORIGINAL}, expected=201)
            endpoint = f"/api/files/{created['id']}"
            item = api.get(endpoint)
            paths = [item["path"], item["path"].removeprefix("library/")]
            source = api.post("/api/files", {"name": "验收用双链来源.md",
                                             "content": f"[[{paths[0]}]]\n\n[[{paths[1]}]]"}, expected=201)["id"]
            offer = api.get(endpoint + "/trash")
            impact = offer["wikiImpact"]
            check(impact["sourceCount"] == 1 and [(x["id"], x["lines"]) for x in impact["sources"]] == [(source, 2)],
                  "移入前预览缺少合成双链的引用位置。")
            entry = api.post(endpoint + "/trash", {"path": offer["path"], "revision": offer["revision"]})
            archived_path(producer, entry)
            api.post("/api/files/reindex")
            states = [resolve_wiki(api, path) for path in paths]
            for resolution in states:
                check(resolution["state"] == "resolved"
                      and [(x["id"], x["status"]) for x in resolution["choices"]] == [(item["id"], "trashed")],
                      "重建索引后旧路径未解析到回收原件。")
            check(item["id"] not in {x["id"] for x in api.get("/api/files")}, "回收原件进入了普通文件列表。")
            output.update({"引用文件数": impact["sourceCount"], "引用原文位置数": impact["sources"][0]["lines"],
                           "完整与相对旧路径状态": [x["choices"][0]["status"] for x in states]})

        with run.step("backup-trash", {"操作": "在另一份临时工作区整体恢复加密归档"}) as output:
            api.put("/api/backups/destination", {"directory": str(media)})
            snapshot = api.post("/api/backups", {"password": password}, expected=201)
            backup = f"/api/backups/{snapshot['id']}"
            encrypted = (media / snapshot["name"]).read_bytes()
            check(ORIGINAL.encode() not in encrypted and password.encode() not in encrypted, "加密归档出现明文。")

            with start_product(runtime, receiver) as target:
                setup_workspace(target)
                prior = target.post("/api/files", {"name": "接收库已有资料.md", "content": "# 接收库原件"}, expected=201)
                prior_path = target.get(f"/api/files/{prior['id']}")["path"]
                target.put("/api/backups/destination", {"directory": str(media)})
                plan = target.post(f"{backup}/preview", {"password": password})
                check(any(x["path"] == entry["archivePath"] and x["revision"] == item["revision"] for x in plan["files"])
                      and any(x["path"] == prior_path for x in plan["currentOnly"]), "换机预览未报告回收原件或接收库差异。")
                result = restore_backup(target, backup, password)
                rollback = target.post(f"/api/backups/{result['rollbackBackupId']}/preview", {"password": password})
                check(any(x["path"] == prior_path for x in rollback["files"]), "加密回退快照缺少接收库原件。")
                archived_path(receiver, entry)
                route = f"/api/files/trash/{entry['id']}/restore"
                target.post(route, {"path": ORDINARY_PATH, "revision": item["revision"]})
                evidence = restored_evidence(target, receiver, item, source, paths, ORDINARY_PATH)
                output.update({"整库恢复": result["state"], "整库恢复后按普通确认找回": True,
                               "加密回退快照可预览": True, **evidence})

    with run.step("backup-trash-review", {"操作": "复制隔离回收原件以模拟换机后的文件身份变化"}) as output:
        with start_product(runtime, receiver) as api:
            result = restore_backup(api, backup, password)
            archive = archived_path(receiver, entry)
            copy = archive.with_name(".acceptance-copy")
            with copy.open("xb") as handle:
                handle.write(archive.read_bytes())
            copy.replace(archive)
            first = {"path": ORDINARY_PATH, "revision": item["revision"]}
            second = {"path": REVIEWED_PATH, "revision": item["revision"]}
            response = api.post(route, first, expected=409)
            check(response.get("code") == "archive_identity" and response.get("confirmation"),
                  "内容一致的回收副本未进入核对确认。")
            rejected = api.post(route, {**second, "confirmation": response["confirmation"]}, expected=409)
            check("confirmation" not in rejected and archive.read_bytes() == ORIGINAL.encode(),
                  "修改路径后旧确认仍可用或归档发生变化。")
            confirmed = api.post(route, second, expected=409)
            check(confirmed.get("code") == "archive_identity" and confirmed.get("confirmation"),
                  "新位置没有获得独立的核对确认。")
            api.post(route, {**second, "confirmation": confirmed["confirmation"]})
            check(not archive.exists(), "已找回的原件仍留在回收站。")
            evidence = restored_evidence(api, receiver, item, source, paths, REVIEWED_PATH)
        with start_product(runtime, receiver) as reopened:
            check(reopened.get("/api/backups/restore-status")["id"] == result["id"]
                  and restored_evidence(reopened, receiver, item, source, paths, REVIEWED_PATH) == evidence,
                  "重启后找回原件的证据或整库恢复记录丢失。")
        output.update({"改路径的旧确认": "HTTP 409", "重启后原件与双链读回一致": True, **evidence})
