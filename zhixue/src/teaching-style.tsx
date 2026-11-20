import { useEffect } from 'react';
import { api } from './api';
import { ErrorNote, useAction, useRead } from './shared';

export type TeachingStyle = 'direct' | 'guided';

export const TEACHING_STYLE_NAMES: Record<TeachingStyle, string> = {
  direct: '直接讲解',
  guided: '启发式引导',
};

type Preference = { style: TeachingStyle };
const PATH = '/teaching-style';
const CHANGED = 'zhixue-teaching-style-changed';

export function TeachingStyleSelect() {
  const preference = useRead<Preference>(PATH);
  const action = useAction();
  const refresh = preference.refresh;
  useEffect(() => {
    window.addEventListener(CHANGED, refresh);
    window.addEventListener('focus', refresh);
    return () => {
      window.removeEventListener(CHANGED, refresh);
      window.removeEventListener('focus', refresh);
    };
  }, [refresh]);

  async function choose(style: TeachingStyle) {
    const saved = await action.run(() => api<Preference>(PATH, 'PUT', { style }));
    if (saved) window.dispatchEvent(new Event(CHANGED));
  }

  return <div className="teaching-style">
    <label className="teaching-style-picker">
      <span>教学风格</span>
      <select value={preference.value?.style ?? ''} disabled={!preference.value || action.pending}
        onChange={event => void choose(event.target.value as TeachingStyle)}>
        {!preference.value && <option value="">—</option>}
        {Object.entries(TEACHING_STYLE_NAMES).map(([style, label]) => <option key={style} value={style}>{label}</option>)}
      </select>
    </label>
    <ErrorNote message={action.error || preference.error} retry={preference.error ? refresh : undefined} />
  </div>;
}
