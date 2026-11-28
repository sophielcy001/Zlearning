import { FolderOpen, Network } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link, parsePath, useLocation, useSearchParams, type Location } from 'react-router-dom';

const FILE_CONTEXT = ['folder', 'kind', 'q', 'mode', 'scope', 'format', 'project', 'tag', 'shelf'] as const;
type FileTarget = Record<string, string | null>;

export function libraryFileParams(current: URLSearchParams, target: FileTarget = {}) {
  const next = new URLSearchParams();
  for (const key of FILE_CONTEXT) {
    const value = current.get(key);
    if (value) next.set(key, value);
  }
  for (const [key, value] of Object.entries(target)) {
    if (value === null) next.delete(key);
    else next.set(key, value);
  }
  return next;
}

export function libraryFileUrl(current: URLSearchParams, target?: FileTarget) {
  const query = libraryFileParams(current, target);
  return query.size ? '/library?' + query : '/library';
}

export function libraryLinkUrl(route: string, location: Pick<Location, 'pathname' | 'search'>) {
  const target = parsePath(route);
  if (location.pathname !== '/library' || target.pathname !== '/library') return route;
  return libraryFileUrl(new URLSearchParams(location.search), Object.fromEntries(new URLSearchParams(target.search)))
    + (target.hash ?? '');
}

export function LibraryContextLink({ to, children }: { to: string; children: ReactNode }) {
  const location = useLocation();
  return <Link to={libraryLinkUrl(to, location)}>{children}</Link>;
}

export function LibraryViewSwitch({ graph = false }: { graph?: boolean }) {
  const [params] = useSearchParams();
  return <nav aria-label="知识库视图" className="library-view-switch">
    <Link to={libraryFileUrl(params)} aria-current={!graph ? 'page' : undefined}><FolderOpen size={17} />文件</Link>
    <Link to={libraryFileUrl(params, { view: 'graph' })} aria-current={graph ? 'page' : undefined}><Network size={17} />图谱</Link>
  </nav>;
}
