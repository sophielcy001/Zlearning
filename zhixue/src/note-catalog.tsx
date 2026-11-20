import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';

export type NoteSummary = { id: string; title: string };
type NoteLesson = { id: string; title: string; projectId: string; projectTopic: string };
type NoteMetadata = NoteSummary & { metadataRevision: number; lessons: NoteLesson[] };

export function NewNoteDialog({ lessonId, onDone, onClose }: {
  lessonId?: string; onDone: (note: NoteSummary) => void; onClose: () => void;
}) {
  const [id] = useState(() => crypto.randomUUID());
  const [title, setTitle] = useState('');
  const action = useAction();

  function create(event: FormEvent) {
    event.preventDefault();
    void action.run(async () => {
      const note = await api<NoteMetadata>('/notes', 'POST', { id, title: title.trim(), lessonIds: lessonId ? [lessonId] : [] });
      onDone(note);
    });
  }

  return <Modal title="新建笔记" busy={action.pending} onClose={onClose}>
    <form className="project-dialog-form" onSubmit={create}>
      <label className="project-field">笔记标题
        <input autoFocus required maxLength={120} disabled={action.pending} value={title}
          onChange={event => setTitle(event.target.value)} />
      </label>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button className="button secondary" type="button" disabled={action.pending} onClick={onClose}>取消</button>
        <button className="button primary" type="submit" disabled={!title.trim() || action.pending}>创建笔记</button>
      </div>
    </form>
  </Modal>;
}

export function NoteDetailsDialog({ noteId, onDone, onClose }: {
  noteId: string; onDone: (note: NoteSummary) => void; onClose: () => void;
}) {
  const endpoint = '/notes/' + noteId + '/details';
  const read = useRead<NoteMetadata>(endpoint);
  const options = useRead<NoteLesson[]>('/notes/lesson-options');
  const action = useAction();

  function save(title: string, lessonIds: string[]) {
    if (!read.value) return;
    const metadataRevision = read.value.metadataRevision;
    void action.run(async () => {
      const note = await api<NoteMetadata>(endpoint, 'PUT', { title, lessonIds, metadataRevision });
      onDone(note);
    });
  }

  return <Modal title="笔记标题与课程关联" busy={action.pending} onClose={onClose}>
    {read.value ? <NoteDetailsForm key={read.value.metadataRevision} note={read.value} choices={options.value}
      optionsError={options.error} retryOptions={options.refresh} save={save} cancel={onClose}
      error={action.error} busy={action.pending} refresh={read.refresh} />
      : <BackedStatus error={read.error} retry={read.refresh} />}
  </Modal>;
}

function NoteDetailsForm({ note, choices, optionsError, save, cancel, error, busy, refresh, retryOptions }: {
  note: NoteMetadata; choices?: NoteLesson[]; optionsError?: string; error: string; busy: boolean;
  save: (title: string, ids: string[]) => void; cancel: () => void; refresh: () => void; retryOptions: () => void;
}) {
  const [title, setTitle] = useState(note.title);
  const [picked, setPicked] = useState(() => new Set(note.lessons.map(lesson => lesson.id)));
  const [query, setQuery] = useState('');
  const search = query.trim().toLocaleLowerCase();
  const visible = choices?.filter(lesson => (lesson.title + ' ' + lesson.projectTopic).toLocaleLowerCase().includes(search));
  const hasChanged = note.title !== title.trim() || picked.size !== note.lessons.length ||
    note.lessons.some(lesson => !picked.has(lesson.id));

  function toggle(id: string) {
    setPicked(current => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  return <form className="project-dialog-form" onSubmit={event => { event.preventDefault(); save(title.trim(), [...picked]); }}>
    <label className="project-field">笔记标题
      <input autoFocus required maxLength={120} disabled={busy} value={title} onChange={event => setTitle(event.target.value)} />
    </label>
    <fieldset className="note-course-choices" disabled={busy}>
      <legend>关联知识点</legend>
      <input type="search" className="note-course-search" aria-label="搜索知识点或项目" placeholder="搜索知识点或项目"
        value={query} onChange={event => setQuery(event.target.value)} />
      {visible ? <div className="note-course-list">
        {visible.map(lesson => <label key={lesson.id}>
          <input type="checkbox" checked={picked.has(lesson.id)} onChange={() => toggle(lesson.id)} />
          <span>{lesson.title}<small>{lesson.projectTopic}</small></span>
        </label>)}
        {!visible.length && <p>{search ? '没有匹配的知识点。' : '知识库暂无可关联的知识点。'}</p>}
      </div> : <BackedStatus error={optionsError} retry={retryOptions} />}
      <p className="note-course-count">已选择 {picked.size} 个知识点</p>
    </fieldset>
    {!!note.lessons.length && <details className="note-current-courses">
      <summary>打开已关联的课程</summary>
      <ul>{note.lessons.map(lesson => <li key={lesson.id}>
        <Link className="text-action" to={'/lessons/' + lesson.id}>{lesson.title} · {lesson.projectTopic}</Link>
      </li>)}</ul>
    </details>}
    <ErrorNote message={error} retry={refresh} />
    <div className="project-dialog-actions">
      <button className="button secondary" type="button" disabled={busy} onClick={cancel}>取消</button>
      <button className="button primary" type="submit" disabled={!hasChanged || !title.trim() || busy}>保存设置</button>
    </div>
  </form>;
}
