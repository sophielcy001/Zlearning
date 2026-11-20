import { FolderOpen, Network } from 'lucide-react';
import { Link } from 'react-router-dom';

export function LibraryViewSwitch({ graph = false }: { graph?: boolean }) {
  return <nav aria-label="知识库视图" className="library-view-switch">
    <Link to="/library" aria-current={!graph ? 'page' : undefined}><FolderOpen size={17} />文件</Link>
    <Link to="/library?view=graph" aria-current={graph ? 'page' : undefined}><Network size={17} />图谱</Link>
  </nav>;
}
