from fastapi import HTTPException
from sqlalchemy import text

from .db import LibraryLocation, ProfessionalAgent
from .file_locations import InvalidLocation, LocationConflict, checked_path
from .library import file_signature
from .library_folders import directory_path, entry_stat
from .library_index import keyword_search, search_scope


MAX_EXCERPTS = 4


def check_folders(workspace, paths):
    workspace = workspace.resolve()
    try:
        for relative in paths:
            path = directory_path(workspace, relative)
            while path != workspace:
                entry_stat(path)
                path = path.parent
    except (InvalidLocation, LocationConflict, OSError):
        raise HTTPException(409, "授权的知识库文件夹不可用，请重新选择。") from None


def granted_folders(db, workspace, agent_id):
    agent = db.get(ProfessionalAgent, agent_id) if agent_id else None
    folders = agent.definition.get("knowledgeFolders", []) if agent else []
    if not folders:
        raise HTTPException(409, "所选专业 Agent 没有知识库检索授权。")
    check_folders(workspace, folders)
    return folders


def indexed_passage(db, identity, number):
    return db.execute(text("""
        SELECT d.path, d.revision, json_extract(d.fingerprint, '$[1]') AS signature, p.text_revision
        FROM library_index_documents d JOIN library_passages p ON p.file_id=d.id
        WHERE d.id=:id AND p.position=:position
    """), {"id": identity, "position": number}).mappings().first()


def excerpt_current(db, workspace, folders, item):
    path = item["path"]
    if not any(path.startswith(folder + "/") for folder in folders):
        return False
    location = db.get(LibraryLocation, item["id"])
    if location and (location.path != path or location.missing or location.trash_id):
        return False
    version = indexed_passage(db, item["id"], item["number"])
    if not version or any(version[key] != item[field] for key, field in (
        ("path", "path"), ("revision", "revision"), ("signature", "signature"), ("text_revision", "textRevision"),
    )):
        return False
    try:
        current = checked_path(workspace, path)
        entry_stat(current)
        return file_signature(current) == item["signature"]
    except (InvalidLocation, OSError):
        return False


def knowledge_scope(db, workspace, agent_id, query):
    folders = granted_folders(db, workspace, agent_id)
    excerpts = []
    seen = set()
    for folder in folders:
        result = keyword_search(query, search_scope(None, None, None, folder, None, "all"),
                                lambda sql, params: db.execute(text(sql), params).mappings())
        for hit in result["hits"]:
            location = hit["location"]
            if not location or not hit["revision"]:
                continue
            key = (hit["id"], location["number"])
            if key in seen:
                continue
            seen.add(key)
            version = indexed_passage(db, *key)
            if not version:
                continue
            item = {
                "id": hit["id"], "name": hit["name"], "path": hit["path"], "revision": hit["revision"],
                "number": location["number"], "label": location["label"], "textRevision": location.get("textRevision"),
                "excerpt": hit["snippet"], "signature": version["signature"],
            }
            if excerpt_current(db, workspace, folders, item):
                excerpts.append(item)
            if len(excerpts) == MAX_EXCERPTS:
                return {"folders": folders, "excerpts": excerpts}
    return {"folders": folders, "excerpts": excerpts}


def knowledge_current(db, workspace, scope):
    excerpts = scope.get("knowledge", {}).get("excerpts", []) if scope else []
    if not excerpts:
        return True
    if workspace is None:
        return False
    try:
        folders = granted_folders(db, workspace, scope["id"])
    except HTTPException:
        return False
    return all(excerpt_current(db, workspace, folders, item) for item in excerpts)