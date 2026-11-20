import { useRef, useState } from 'react';
import type { AssessmentNoteExcerpt } from './assessment-types';
import { sourceOffset } from './note-content';
import { BackedStatus, useRead } from './shared';

type NoteOption = { id: string; title: string; linked: boolean };
type NoteFile = { revision: string | null; content: string; missing: boolean };

export function AssessmentNotePicker({ path, value, disabled, onChange }: {
  path: string; value: AssessmentNoteExcerpt[]; disabled: boolean; onChange: (value: AssessmentNoteExcerpt[]) => void;
}) {
  const [opened, setOpened] = useState(false);
  const [id, setId] = useState('');
  const [message, setMessage] = useState('');
  const cursor = useRef<HTMLTextAreaElement>(null);
  const options = useRead<NoteOption[]>(opened ? path + '/note-options' : null);
  const file = useRead<NoteFile>(id && opened ? '/notes/' + id : null);
  const current = options.value?.find(note => note.id === id);

  function add() {
    const field = cursor.current;
    const body = file.value;
    if (!field || !body?.revision || !current) return;
    const from = sourceOffset(body.content, field.selectionStart);
    const to = sourceOffset(body.content, field.selectionEnd);
    const text = body.content.slice(from, to);
    const start = [...body.content.slice(0, from)].length;
    const end = start + [...text].length;
    if (end - start > 1200 || [...text.trim()].length < 2) {
      setMessage('请选择 2 至 1,200 个字符。');
    } else if (value.some(item => item.noteId === id && item.revision === body.revision && item.start === start && item.end === end)) {
      setMessage('此片段已加入。');
    } else {
      onChange([...value, { noteId: id, revision: body.revision, title: current.title, text, start, end }]);
      setMessage('');
    }
  }

  return <details className="assessment-note-picker" open={opened}
    onToggle={event => setOpened(event.currentTarget.open)}>
    <summary>选用个人笔记片段{value.length ? ' · ' + value.length + ' 段' : ''}</summary>
    {opened && <div className="assessment-note-controls">
      {options.value ? options.value.length ? <label className="project-field">选择笔记
        <select value={id} disabled={disabled} onChange={event => { setId(event.target.value); setMessage(''); }}>
          <option value="">请选择</option>
          {options.value.map(note => <option key={note.id} value={note.id}>
            {note.title}{note.linked ? ' · 本节' : ''}
          </option>)}
        </select>
      </label> : <p className="model-help">知识库还没有可选笔记。</p>
        : <BackedStatus error={options.error} retry={options.refresh} />}
      {current && (file.value ? file.value.revision && file.value.content.trim() ? <>
        <label className="project-field">选中要核验的原文
          <textarea ref={cursor} aria-label="笔记选段原文" readOnly rows={6} value={file.value.content} />
        </label>
        <button type="button" className="button secondary" disabled={disabled || value.length >= 4}
          onMouseDown={event => event.preventDefault()} onClick={add}>加入所选片段</button>
        <p className="model-help" role="status">{message || '最多选四段，每段 1,200 个字符。'}</p>
      </> : <p className="model-help">{file.value.missing ? '原件缺失，请在知识库处理文件位置。' : '保存笔记正文后可选段。'}</p>
        : <BackedStatus error={file.error} retry={file.refresh} />)}
      {!!value.length && <ul className="assessment-note-list" aria-label="已选择的笔记片段">
        {value.map((item, index) => <li key={[item.noteId, item.revision, item.start, item.end].join('/')}>
          <strong>{item.title}</strong>
          <blockquote>{item.text}</blockquote>
          <button type="button" className="text-action" aria-label={'移除第 ' + (index + 1) + ' 个笔记片段'}
            disabled={disabled} onClick={() => onChange(value.filter((_, position) => index !== position))}>移除</button>
        </li>)}
      </ul>}
    </div>}
  </details>;
}
