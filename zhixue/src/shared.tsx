import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, BookOpen, LoaderCircle, Upload, X } from 'lucide-react';
import ReactMarkdown, { type Options } from 'react-markdown';
import { Link } from 'react-router-dom';
import remarkFrontmatter from 'remark-frontmatter';
import { workspaceMarkdownRoute } from './markdown-links';
import { remarkWiki } from './wiki-markdown';
import { api, uploadDocument } from './api';

export function useRead<T>(path: string | null) {
  const [version, setVersion] = useState(0);
  const [resource, setResource] = useState<{ path: string; value?: T; error?: string }>();
  const refresh = useCallback(() => setVersion(v => v + 1), []);
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    api<T>(path, 'GET', undefined, controller.signal).then(value => {
      if (!controller.signal.aborted) setResource({ path, value });
    }).catch((error: Error) => {
      if (!controller.signal.aborted) setResource(previous => ({ path, value: previous?.path === path ? previous.value : undefined, error: error.message }));
    });
    return () => controller.abort();
  }, [path, version]);
  return { ...(resource?.path === path ? resource : undefined), refresh };
}

export function useAction() {
  const active = useRef(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  async function run<T>(work: () => Promise<T>): Promise<T | undefined> {
    if (active.current) return;
    active.current = true;
    setPending(true);
    setError('');
    try {
      return await work();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '操作暂时失败，请重试。');
    } finally {
      active.current = false;
      setPending(false);
    }
  }
  return { run, pending, error };
}

export function ErrorNote({ message, retry }: { message?: string; retry?: () => void }) {
  return message ? <div className="notice error" role="alert"><AlertCircle size={17} /><span>{message}</span>{retry && <button type="button" className="text-action" onClick={retry}>重试</button>}</div> : null;
}

export function Loading({ text = '正在读取本地数据' }: { text?: string }) {
  return <div className="notice loading" role="status"><LoaderCircle size={17} className="spinner" />{text}</div>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><BookOpen size={21} /><strong>{title}</strong>{children}</div>;
}

export function Modal({ title, onClose, children, busy = false }: {
  title: string; onClose: () => void; children: ReactNode; busy?: boolean;
}) {
  const heading = useId();
  const element = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = element.current;
    dialog?.showModal();
    return () => { dialog?.close(); };
  }, []);
  return <dialog ref={element} className="project-dialog panel" aria-labelledby={heading} onCancel={event => {
    event.preventDefault();
    if (!busy) onClose();
  }}>
    <div className="drawer-head"><h2 id={heading}>{title}</h2>
      <button type="button" className="icon-button" aria-label="关闭" disabled={busy} onClick={onClose}><X size={20} /></button>
    </div>
    {children}
  </dialog>;
}

export function Markdown({ children, components, remarkPlugins }: Pick<Options, 'components' | 'remarkPlugins'> & { children: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkFrontmatter, remarkWiki, ...(remarkPlugins ?? [])]} components={{
    ...components,
    a: ({ href, children: label }) => {
      const route = workspaceMarkdownRoute(href);
      if (route) return <Link to={route}>{label}</Link>;
      return href && /^https?:\/\//i.test(href)
        ? <a href={href} target="_blank" rel="noopener noreferrer">{label}</a> : <span>{label}</span>;
    },
    img: ({ alt }) => alt ? <span>{alt}</span> : null,
  }}>{children.replace(/^\uFEFF/, '')}</ReactMarkdown></div>;
}

export function FileUpload({ onImport, onPending, disabled = false, label = '导入资料' }: {
  onImport: (id: string, name: string) => void; onPending?: (pending: boolean) => void; disabled?: boolean; label?: string;
}) {
  const action = useAction();
  useEffect(() => { onPending?.(action.pending); }, [onPending, action.pending]);
  const [chosen, setChosen] = useState<File | null>(null);
  const [duplicate, setDuplicate] = useState<{ id: string; name: string } | null>(null);

  function upload(file: File, behavior: 'ask' | 'copy') {
    void action.run(async () => {
      const format = file.name.split('.').at(-1)?.toLowerCase();
      if (!format || !['txt', 'md', 'pdf', 'docx', 'png', 'jpg', 'jpeg', 'html'].includes(format)) {
        throw new Error('请选择 TXT、Markdown、PDF、DOCX、PNG、JPEG 或 HTML 文件。');
      }
      if (file.size === 0) throw new Error('请选择有内容的文件。');
      const text = format === 'txt' || format === 'md';
      if (file.size > (text ? 200_000 : 20 * 1024 * 1024)) {
        throw new Error(text ? '请选择不超过 200 KB 的文本。' : '请选择不超过 20 MiB 的文档。');
      }
      const result = await uploadDocument(file, behavior);
      if (result.status === 'duplicate') {
        setDuplicate({ id: result.id, name: result.name ?? file.name });
        return;
      }
      setDuplicate(null);
      setChosen(null);
      onImport(result.id, result.name ?? file.name.trim());
    });
  }

  return <div className="upload-control">
    <label className="button secondary file-button">
      <Upload size={17} />{action.pending ? '正在导入' : label}
      <input type="file" accept=".txt,.md,.pdf,.docx,.png,.jpg,.jpeg,.html" aria-label={label} disabled={disabled || action.pending} onChange={event => {
        const file = event.target.files?.[0];
        event.currentTarget.value = '';
        if (!file) return;
        setChosen(file);
        setDuplicate(null);
        upload(file, 'ask');
      }} />
    </label>
    {duplicate && chosen && <div className="notice upload-duplicate">
      <strong>相同内容已存在</strong><span>{duplicate.name}</span>
      <div className="action-row">
        <button type="button" className="button secondary" disabled={disabled || action.pending} onClick={() => {
          setDuplicate(null); setChosen(null); onImport(duplicate.id, duplicate.name);
        }}>复用已有文件</button>
        <button type="button" className="button soft" disabled={disabled || action.pending} onClick={() => upload(chosen, 'copy')}>保留新副本</button>
        <button type="button" className="text-action" disabled={action.pending} onClick={() => { setDuplicate(null); setChosen(null); }}>取消</button>
      </div>
    </div>}
    <ErrorNote message={action.error} retry={chosen && !disabled && !action.pending ? () => upload(chosen, duplicate ? 'copy' : 'ask') : undefined} />
  </div>;
}

export function BackedStatus({ error, retry }: { error?: string; retry: () => void }) {
  return error ? <ErrorNote message={error} retry={retry} /> : <Loading />;
}
