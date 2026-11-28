from hashlib import sha256

import pytest

from test_full_restore import app_client, request_restore, wait_restore
from test_backups import accepted, inspect_backup, make_backup
from test_model_study import ok, session


SKILLS = "/api/skills"
SOURCE = (
    "\ufeff---\r\nname: study-checklist\r\ndescription: >-\r\n"
    "  整理试印前检查事项。\r\nlicense: MIT\r\n"
    "compatibility: 只需文字资料。\r\nmetadata:\r\n  version: '1.0'\r\n"
    "allowed-tools: Read\r\n---\r\n# 分步检查\r\n"
    "记录版框、墨色与字行。<em>显示原文</em>\r\n"
).encode("utf-8")


def upload(http, route, source=SOURCE, checksum=None):
    headers = {"Content-Type": "text/markdown"}
    if checksum is not None:
        headers["X-Skill-Checksum"] = checksum
    return http.post(SKILLS + route, content=source, headers=headers)


def install(http, source=SOURCE):
    preview = ok(upload(http, "/preview", source))
    return ok(upload(http, "", source, preview["checksum"]))


def test_preview_readback_restart_and_confirmed_duplicate(tmp_path):
    with session(tmp_path) as http:
        preview = ok(upload(http, "/preview"))
        assert preview == {
            "name": "study-checklist", "description": "整理试印前检查事项。", "license": "MIT",
            "compatibility": "只需文字资料。", "version": "1.0", "declaredTools": "Read",
            "source": SOURCE.decode("utf-8"), "checksum": sha256(SOURCE).hexdigest(),
        }
        assert ok(http.get(SKILLS)) == {"items": []}
        assert upload(http, "").status_code == 409
        assert upload(http, "", SOURCE + b"\n", preview["checksum"]).status_code == 409
        first = ok(upload(http, "", checksum=preview["checksum"]))
        assert first["source"] == preview["source"]
        assert first["status"] == "installed"
        assert ok(upload(http, "", checksum=preview["checksum"])) == first
        changed = SOURCE.replace(b"'1.0'", b"'2.0'")
        other = ok(upload(http, "/preview", changed))
        assert upload(http, "", changed, other["checksum"]).status_code == 409

    with session(tmp_path) as http:
        [summary] = ok(http.get(SKILLS))["items"]
        assert summary["checksum"] == first["checksum"] and "source" not in summary
        assert ok(http.get(SKILLS + "/study-checklist")) == first
        assert http.get(SKILLS + "/not-installed").status_code == 404

    with session(tmp_path / "another-workspace") as http:
        assert ok(http.get(SKILLS)) == {"items": []}


@pytest.mark.parametrize(("source", "status"), [
    (b"--\nmissing frontmatter", 422),
    (b"---\nname: invalid--name\ndescription: example\n---\nBody", 422),
    (b"---\nname: duplicate\nname: extra\ndescription: example\n---\nBody", 422),
    (b"---\nname: example\ndescription: !custom example\n---\nBody", 422),
    (b"---\nname: example\ndescription: &example note\nlicense: *example\n---\nBody", 422),
    (b"---\nname: example\ndescription: " + b"[" * 30 + b"x" + b"]" * 30 + b"\n---\nBody", 422),
    (b"---\nname: example\ndescription: example\n---\n", 422),
    (b"\xff", 422),
    (b"x" * (256 * 1024 + 1), 413),
])
def test_invalid_documents_are_reported_without_storage(tmp_path, source, status):
    with session(tmp_path) as http:
        response = upload(http, "/preview", source)
        assert response.status_code == status
        assert isinstance(response.json()["detail"], str)
        assert ok(http.get(SKILLS)) == {"items": []}


def test_full_backup_restores_skill_text_and_index(tmp_path):
    with app_client(tmp_path) as http:
        first = install(http)
        backup = make_backup(http)
        install(http, SOURCE.replace(b"study-checklist", b"another-checklist"))
        preview = accepted(inspect_backup(http, backup))
        assert preview["counts"]["backup"]["installed_skills"] == 1
        request_restore(http, backup, preview)
        assert wait_restore(http)["state"] == "complete"
        assert ok(http.get(SKILLS + "/study-checklist")) == first
        assert http.get(SKILLS + "/another-checklist").status_code == 404

DETAIL = SKILLS + "/study-checklist"
NEW = (SOURCE.replace(b"'1.0'", b"'2.0'").replace(b"allowed-tools: Read", b"allowed-tools: Read Search")
       .replace("只需文字资料。".encode(), "需检索资料。".encode()))
ALTERNATE = SOURCE.replace(b"'1.0'", b"'3.0'").replace("记录版框".encode(), "先检查字号".encode())


def confirm_update(http, source, preview):
    return http.post(SKILLS, content=source, headers={
        "Content-Type": "text/markdown", "X-Skill-Checksum": preview["checksum"],
        "X-Skill-Update-Confirmation": preview["update"]["confirmation"],
    })


def version(http, checksum):
    return ok(http.get(f"{DETAIL}/versions/{checksum}"))


def test_confirmed_updates_keep_immutable_history_and_reject_stale_decisions(tmp_path):
    with session(tmp_path) as http:
        first = install(http)
        proposed = ok(upload(http, "/preview", NEW))
        alternate = ok(upload(http, "/preview", ALTERNATE))
        assert proposed["update"]["current"]["checksum"] == first["checksum"]
        assert proposed["update"]["changes"][:2] == [
            {"field": "declaredTools", "before": "Read", "after": "Read Search"},
            {"field": "compatibility", "before": "只需文字资料。", "after": "需检索资料。"},
        ]
        assert ok(http.get(DETAIL)) == first
        assert upload(http, "", NEW, proposed["checksum"]).status_code == 409
        assert confirm_update(http, ALTERNATE, {**proposed, "checksum": alternate["checksum"]}).status_code == 409
        second = ok(confirm_update(http, NEW, proposed))
        assert second["version"] == "2.0"
        assert confirm_update(http, ALTERNATE, alternate).status_code == 409
        assert version(http, first["checksum"])["document"]["source"] == first["source"]
        assert version(http, second["checksum"])["document"]["source"] == second["source"]
        old = version(http, first["checksum"])
        assert old["comparison"]["changes"][0]["before"] == "Read Search"
        assert old["comparison"]["changes"][0]["after"] == "Read"
        chosen = ok(http.post(DETAIL + "/rollback", json={
            "checksum": first["checksum"], "confirmation": old["comparison"]["confirmation"],
        }))
        assert chosen == first
        assert confirm_update(http, ALTERNATE, alternate).status_code == 409

    with session(tmp_path) as http:
        listed = ok(http.get(DETAIL + "/versions"))
        assert listed["currentChecksum"] == first["checksum"]
        assert [(item["position"], item["checksum"]) for item in listed["items"]] == [
            (2, second["checksum"]), (1, first["checksum"]),
        ]
        again = ok(upload(http, "/preview", NEW))
        assert ok(confirm_update(http, NEW, again)) == second
        assert len(ok(http.get(DETAIL + "/versions"))["items"]) == 2


def test_full_backup_restores_skill_history_and_current_selection(tmp_path):
    with app_client(tmp_path) as http:
        first = install(http)
        second = ok(confirm_update(http, NEW, ok(upload(http, "/preview", NEW))))
        backup = make_backup(http)
        old = version(http, first["checksum"])
        ok(http.post(DETAIL + "/rollback", json={
            "checksum": first["checksum"], "confirmation": old["comparison"]["confirmation"],
        }))
        plan = accepted(inspect_backup(http, backup))
        assert plan["counts"]["backup"]["skill_versions"] == 2
        request_restore(http, backup, plan)
        assert wait_restore(http)["state"] == "complete"
        assert ok(http.get(DETAIL)) == second
        assert http.post(DETAIL + "/rollback", json={
            "checksum": first["checksum"], "confirmation": old["comparison"]["confirmation"],
        }).status_code == 409
        assert version(http, first["checksum"])["document"]["source"] == first["source"]


def test_0053_documents_migrate_into_history_as_version_one(tmp_path):
    from alembic import command
    from alembic.config import Config

    from backend.db import ROOT, open_db

    with session(tmp_path) as http:
        first = install(http)
    engine = open_db(tmp_path)
    try:
        config = Config()
        config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0053_installed_skills")
    finally:
        engine.dispose()
    with session(tmp_path) as http:
        assert ok(http.get(DETAIL)) == first
        items = ok(http.get(DETAIL + "/versions"))["items"]
        assert len(items) == 1 and items[0]["position"] == 1
        result = version(http, first["checksum"])
        assert result["document"]["source"] == SOURCE.decode("utf-8") and result["comparison"] is None

