from uuid import uuid4

from backend.library import local_path
from backend.worker import prepare_one
from test_course_verification import authorize, model_traffic, prepare, publish
from test_model_study import ok, run, session


def local_project(http, topic):
    return ok(http.post('/api/projects', json={
        'id': str(uuid4()), 'topic': topic, 'outcome': '能检查一份练习结果',
    }))


def save_linked_note(http, lesson_id):
    note_id = str(uuid4())
    note = '/api/notes/' + note_id
    details = ok(http.post('/api/notes', json={'id': note_id, 'title': '保留的笔记', 'lessonIds': [lesson_id]}))
    body = '核对第一节课的练习记录'
    base_revision = ok(http.get(note))['revision']
    ok(http.put(note, json={'baseRevision': base_revision, 'content': body}))
    return note, details, body


def test_local_task_can_stop_resume_and_delete_without_removing_saved_work(tmp_path):
    with session(tmp_path) as http:
        project = local_project(http, 'SQLite 准备控制')
        project_url = '/api/projects/' + project['id']
        tasks = project_url + '/generation'
        assert prepare_one(tmp_path)
        lesson_id = project['lessons'][0]['id']
        lesson_file = local_path(tmp_path, 'courses', lesson_id)
        first_course = lesson_file.read_bytes()
        note, details, note_body = save_linked_note(http, lesson_id)
        ok(http.post(tasks + '/control', json={'action': 'stop'}))
        stopped = ok(http.get(project_url))
        assert stopped['status'] == 'active' and stopped['task']['status'] == 'stopped'
        item, = ok(http.get('/api/tasks'))['items']
        assert (item['status'], item['generationMode'], item['completed'], item['total']) == ('stopped', 'local', 1, 3)
        assert not prepare_one(tmp_path)

        old_scope = ok(http.post(tasks + '/delete-preview'))
        assert (old_scope['drafts'], old_scope['pending'], old_scope['running'], old_scope['routeNodes']) == (0, 2, 0, 3)
        ok(http.post(tasks + '/control', json={'action': 'resume'}))
        assert prepare_one(tmp_path)
        assert http.post(tasks + '/delete', json={'confirmation': old_scope['confirmation']}).status_code == 409
        preview = ok(http.post(tasks + '/delete-preview'))
        assert preview['pending'] == 1
        confirmation = {'confirmation': preview['confirmation']}
        deleted = ok(http.post(tasks + '/delete', json=confirmation))
        assert ok(http.post(tasks + '/delete', json=confirmation)) == deleted
        assert ok(http.get('/api/tasks'))['items'] == [] and not prepare_one(tmp_path)
        assert http.post(tasks + '/control', json={'action': 'resume'}).status_code == 409
        saved = ok(http.get(project_url))
        assert saved['task']['status'] == 'deleted' and len(saved['lessons']) == 3
        assert sum(item['ready'] for item in saved['lessons']) == 2
        assert ok(http.get(note + '/details')) == details
        assert ok(http.get(note))['content'] == note_body
        assert lesson_file.read_bytes() == first_course


def test_task_list_shows_actual_local_preparation_error_and_can_retry(tmp_path):
    with session(tmp_path) as http:
        project = local_project(http, 'SQLite 文件冲突')
        identity = project['id']
        path = local_path(tmp_path, 'courses', project['lessons'][0]['id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('已有外部内容', encoding='utf-8')
        assert prepare_one(tmp_path)
        saved = ok(http.get(f'/api/projects/{identity}'))
        item, = ok(http.get('/api/tasks'))['items']
        assert item['status'] == 'failed' and item['error'] == saved['task']['error']
        assert '写入失败' in item['error'] and path.read_text(encoding='utf-8') == '已有外部内容'
        path.unlink()
        ok(http.post(f'/api/projects/{identity}/generation/control', json={'action': 'resume'}))
        assert prepare_one(tmp_path)
        refreshed, = ok(http.get('/api/tasks'))['items']
        assert refreshed['error'] is None and refreshed['completed'] == 1


def test_model_delete_requires_current_review_plan_and_preserves_published_course(tmp_path):
    calls = []
    with session(tmp_path) as http:
        project, lesson, _, scope = prepare(http, tmp_path, calls)
        generation = f'/api/projects/{project}/generation'
        assert authorize(http, project, scope)['authorized']
        assert run(tmp_path, model_traffic(calls)) and publish(tmp_path)
        course_url = f'/api/lessons/{lesson}'
        original = ok(http.get(course_url))
        publication = ok(http.get(course_url + '/verification'))['published']
        assert original['courseVersion'] == 1 and original['body'] and publication
        note, details, note_body = save_linked_note(http, lesson)

        stale = ok(http.post(generation + '/delete-preview'))
        assert authorize(http, project, scope)['authorized']
        assert http.post(generation + '/delete', json={'confirmation': stale['confirmation']}).status_code == 409
        current = ok(http.post(generation + '/delete-preview'))
        deleted = ok(http.post(generation + '/delete', json={'confirmation': current['confirmation']}))
        assert deleted['state'] == 'deleted' and deleted['usage']['calls'] == len(calls) == 3
        retained = ok(http.get(course_url))
        assert retained['body'] == original['body'] and retained['courseVersion'] == 1
        assert ok(http.get(course_url + '/verification'))['published'] == publication
        assert ok(http.get(note + '/details')) == details
        assert ok(http.get(note))['content'] == note_body
