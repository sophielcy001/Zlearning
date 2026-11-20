import { useEffect, useState } from 'react';
import { Timer as TimerIcon } from 'lucide-react';
import { api, timeLabel, type TimeCategory, type Timer } from './api';
import { ErrorNote, useAction } from './shared';

export const TIME_CATEGORIES: Record<TimeCategory, string> = {
  learning: '学习新知识', review: '复习', assistant: '答疑', notes: '笔记', unclassified: '未分类',
};

export function TimerValue({ timer }: { timer: Timer }) {
  const [seconds, setSeconds] = useState(timer.elapsed);
  useEffect(() => {
    const base = Date.parse(timer.measuredAt);
    const update = () => setSeconds(timer.elapsed + (timer.status === 'running' ? Math.max(0, Math.floor((Date.now() - base) / 1000)) : 0));
    update();
    if (timer.status !== 'running') return;
    const tick = window.setInterval(update, 250);
    return () => window.clearInterval(tick);
  }, [timer]);
  return <span className="timer-value">{timeLabel(seconds)}</span>;
}

export function StudyTimer({ active, lessonId, category = 'learning', reviewKey, onChange }: {
  active: Timer | null; lessonId: string; category?: Exclude<TimeCategory, 'unclassified'>; reviewKey?: string; onChange: () => void;
}) {
  const action = useAction();
  const sameLesson = active?.device !== 'phone' && active?.lessonId === lessonId;
  const matches = active && sameLesson && active.reviewKey === (reviewKey ?? null)
    && (active.category === category || (category === 'learning' && active.category === 'unclassified'));
  function change(kind: 'start' | 'pause' | 'resume' | 'finish') {
    void action.run(async () => {
      await api('/timer', 'POST', { action: kind, lessonId, category, reviewKey, timerId: active?.id });
      onChange();
    });
  }
  return <div className="lesson-timer">
    <div className="action-row">
      {sameLesson && active ? <>
        <span className="timer-tag"><TimerIcon size={17} />{active.status === 'running' ? '计时中' : '计时已暂停'}
          <TimerValue timer={active} /><span>· {TIME_CATEGORIES[active.category]}</span></span>
        <button type="button" className="button secondary" disabled={action.pending} onClick={() => change(active.status === 'running' ? 'pause' : 'resume')}>
          {active.status === 'running' ? '暂停计时' : '继续计时'}
        </button>
        <button type="button" className="button secondary" disabled={action.pending} onClick={() => change('finish')}>结束计时</button>
        {!matches && <button type="button" className="button secondary" disabled={action.pending} onClick={() => change('start')}>
          结束当前计时并开始{TIME_CATEGORIES[category]}
        </button>}
      </> : <>
        <button type="button" className="button secondary" disabled={action.pending} onClick={() => change('start')}>
          <TimerIcon size={16} />{active ? '结束上一计时并开始本节' : '开始计时'}
        </button>
        {active && <span className="timer-context">{active.device === 'phone' ? '手机 · ' : ''}{active.title} · <TimerValue timer={active} /></span>}
      </>}
    </div>
    <ErrorNote message={action.error} />
  </div>;
}
