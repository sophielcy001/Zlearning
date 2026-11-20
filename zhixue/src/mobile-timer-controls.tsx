import { useEffect, useState } from 'react';
import { Timer as TimerIcon } from 'lucide-react';
import { timeLabel } from './api';
import { timerMilliseconds, type LocalTimer, type TimerTarget } from './mobile-timer';

const LABELS = { learning: '学习', notes: '笔记', review: '复习' };

export function LocalTimerBanner({ timer, disabled, change }: {
  timer: LocalTimer; disabled: boolean; change: (action: 'pause' | 'resume' | 'finish', id: string) => void;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    setNow(Date.now());
    if (!timer.running) return;
    const clock = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(clock);
  }, [timer]);
  return <section className="study-card local-timer" aria-label="本机计时">
    <p><TimerIcon size={17} aria-hidden="true" />{timer.title} · {LABELS[timer.category]}</p>
    <div className="actions">
      <strong role="status">{timer.running ? '计时中' : '计时已暂停'}</strong>
      <time>{timeLabel(Math.floor(timerMilliseconds(timer, now) / 1000))}</time>
      <button className="action secondary" type="button" disabled={disabled}
        onClick={() => change(timer.running ? 'pause' : 'resume', timer.id)}>
        {timer.running ? '暂停计时' : '继续计时'}
      </button>
      <button className="action secondary" type="button" disabled={disabled}
        onClick={() => change('finish', timer.id)}>结束计时</button>
    </div>
  </section>;
}

export function StartLocalTimer({ target, timer, disabled, start }: {
  target: TimerTarget; timer?: LocalTimer; disabled: boolean; start: (target: TimerTarget) => void;
}) {
  if (timer && timer.workspace === target.workspace && timer.lessonId === target.lessonId
    && timer.category === target.category && timer.reviewKey === target.reviewKey) return null;
  return <button className="action secondary" type="button" disabled={disabled} onClick={() => start(target)}>
    <TimerIcon size={16} aria-hidden="true" />{timer ? '结束原计时并' : ''}开始{LABELS[target.category]}计时
  </button>;
}
