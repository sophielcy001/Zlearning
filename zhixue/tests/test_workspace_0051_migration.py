from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config

from backend.db import ROOT, open_db
from backend.storage_files import sqlite_file_uri
from test_first_use import accepted, import_reference, launch, reached, until

OLD = '0051_course_anchor_links'
NEW = '0066_trash_reference_status'
NOTE = '迁移前写入的笔记。'
SOURCE = '迁移前导入的文本。'


def inspect_database(path):
    with closing(sqlite3.connect(sqlite_file_uri(path, mode="ro"), uri=True)) as db:
        return (db.execute('SELECT version_num FROM alembic_version').fetchone()[0],
                db.execute('PRAGMA quick_check').fetchone()[0])


def test_0051_keeps_course_note_source_and_reviews_when_launch_upgrades_to_current_schema(tmp_path):
    control = tmp_path / '应用入口'
    workspace = tmp_path / '实际学习工作区'
    project = '/api/projects/' + str(uuid4())
    with launch(control) as (http, _):
        assert http.put('/api/setup/storage', json={'directory': str(workspace)}).status_code == 202
        until(lambda: reached(http, 'models'))
        accepted(http.post('/api/setup/complete'))
        course = accepted(http.post('/api/projects', json={
            'id': project.rsplit('/', 1)[1], 'topic': 'SQLite', 'outcome': '核对学习状态迁移',
        }))
        until(lambda: accepted(http.get(project))['task']['status'] == 'ready')
        lesson_id = course['lessons'][0]['id']
        lesson = '/api/lessons/' + lesson_id
        revision = accepted(http.get(lesson + '/note'))['revision']
        note = accepted(http.put(lesson + '/note', json={'content': NOTE, 'baseRevision': revision}))
        accepted(http.put(lesson + '/learned', json={'learned': True}))
        courses = accepted(http.get(project))['lessons']
        source = import_reference(http, body=SOURCE)['id']
        reviews = accepted(http.get('/api/reviews'))['reviews']
        assert len(reviews) == 2 and courses[0]['learnedOn']
    original = workspace / 'library' / 'courses' / (lesson_id + '.md')
    original_digest = sha256(original.read_bytes()).hexdigest()
    database = workspace / 'zhixue.sqlite3'
    config = Config()
    config.set_main_option('script_location', str((ROOT / 'migrations').resolve()).replace('%', '%%'))
    engine = open_db(workspace)
    try:
        with engine.begin() as connection:
            config.attributes['connection'] = connection
            command.downgrade(config, OLD)
    finally:
        engine.dispose()
    assert inspect_database(database) == (OLD, 'ok')
    for _ in range(2):
        with launch(control) as (http, _):
            assert reached(http, 'complete')
            assert Path(accepted(http.get('/api/setup'))['directory']) == workspace
            assert accepted(http.get(project))['lessons'] == courses
            restored_note = accepted(http.get(lesson + '/note'))
            assert restored_note['content'] == NOTE and restored_note['revision'] == note['revision']
            assert accepted(http.get('/api/files/' + source))['content'] == SOURCE
            assert accepted(http.get('/api/reviews'))['reviews'] == reviews
            assert sha256(original.read_bytes()).hexdigest() == original_digest
            assert inspect_database(database) == (NEW, 'ok')
            [snapshot] = (workspace / 'backups' / 'database-upgrades').glob('*.sqlite3')
            assert inspect_database(snapshot) == (OLD, 'ok')
    assert not (control / 'zhixue.sqlite3').exists()
