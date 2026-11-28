import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Bell, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, courseUrl, dateLabel } from './api';
import { mobileRequest } from './mobile-data';
import type { LessonReminder, Reminder, ReminderCheck, ReminderDecision, ReminderReceipt, ReminderSettings, ReminderTrigger } from './reminder-data';
import { BackedStatus, ErrorNote, useAction, useRead } from './shared';

const REMINDERS = '/reminders';
const CHANGED = 'zhixue-reminders-changed';
const WALL_DAY = new Intl.DateTimeFormat('en-CA', { dateStyle: 'short', timeZone: 'Asia/Shanghai' });
const TITLES = { daily: '每日学习提醒', budget: '模型额度恢复提醒', lesson: '固定课程提醒' };

export function refreshReminders() {
  window.dispatchEvent(new Event(CHANGED));
}

type PhoneNavigation = { reviews: () => void; lesson: (card: LessonReminder) => void };

function receipts(card: Reminder): ReminderReceipt[] {
  const primary = { id: card.id, version: card.version };
  return card.kind === 'daily' && card.disputeReceipt ? [primary, card.disputeReceipt] : [primary];
}

function outstanding(cards: Reminder[], decided: ReadonlyMap<string, number>): Reminder[] {
  const active = (receipt: ReminderReceipt) => (decided.get(receipt.id) ?? 0) < receipt.version;
  return cards.flatMap((card): Reminder[] => {
    if (card.kind !== 'daily' || !card.disputeReceipt) return active(card) ? [card] : [];
    const { disputeReceipt, ...daily } = card;
    if (active(card)) return [active(disputeReceipt) ? card : { ...daily, disputes: 0 }];
    return active(disputeReceipt) ? [{ ...daily, ...disputeReceipt, reviews: 0 }] : [];
  });
}

function ReminderFeed({ workspace, room, connected, peerChecks, navigation, notice }: {
  workspace?: string; room?: string; connected: boolean; peerChecks: boolean; navigation?: PhoneNavigation; notice?: string;
}) {
  const [cards, setCards] = useState<Reminder[]>([]);
  const current = useRef<Reminder[]>([]);
  const decided = useRef(new Map<string, number>());
  const request = useRef<(trigger: ReminderTrigger, force?: boolean) => void>(() => {});
  const connection = useRef<(ready: boolean) => void>(() => {});
  const action = useAction();
  const mobile = navigation !== undefined;
  const available = connected && (!mobile || workspace !== undefined);

  useEffect(() => {
    let disposed = false;
    let ready = false;
    let checking = false;
    let queued: ReminderTrigger | undefined;
    let deadline: number | undefined;
    let today: string | null = null;
    let lastCheck = 0;
    let previousWallDay = '';
    let pendingForeground = '';
    let delivery: string | undefined;
    const scope = mobile ? workspace ?? 'phone' : 'desktop';
    // Only the pairing that made a check may recover its missing response.
    const key = 'zhixue-reminder-delivery/' + scope + (mobile && room ? '/' + room : '');
    const foreground = () => document.visibilityState === 'visible' && (mobile || document.hasFocus());

    current.current = [];
    decided.current.clear();
    setCards([]);
    function clearDeadline() {
      if (deadline !== undefined) window.clearTimeout(deadline);
      deadline = undefined;
    }
    function arm(task: () => void, ms: number) {
      deadline = window.setTimeout(task, ms);
    }
    async function check(trigger: ReminderTrigger, force = false) {
      if (disposed || !foreground()) return;
      const realtime = Date.now();
      const wallDay = WALL_DAY.format(realtime);
      if (!ready) {
        if (trigger === 'foreground') pendingForeground = wallDay;
        return;
      }
      if (trigger === 'scheduled' && !today) return;
      if (trigger === 'foreground' && !force && wallDay === previousWallDay && realtime - lastCheck < 5000) return;
      if (checking) {
        if (queued !== 'foreground') queued = trigger;
        return;
      }
      checking = true;
      clearDeadline();
      if (trigger === 'foreground') {
        lastCheck = realtime;
        previousWallDay = wallDay;
        pendingForeground = wallDay;
      }
      if (!delivery) {
        try { delivery = sessionStorage.getItem(key) ?? undefined; } catch { /* A private window can deny storage. */ }
        delivery ??= crypto.randomUUID();
        try { sessionStorage.setItem(key, delivery); } catch { /* The current page can still retry. */ }
      }
      try {
        const shown = current.current.flatMap(receipts);
        const input = { trigger, day: today, shown, delivery, notice };
        const response: ReminderCheck = workspace
          ? await mobileRequest({ op: 'reminder-check', workspace, ...input }, room)
          : await api(REMINDERS + '/check', 'POST', input);
        if (disposed || !ready || !foreground()) return;
        // A stale scheduled response cannot authorize the new day's notifications.
        const eligibleDay = trigger === 'foreground' || response.day === input.day;
        today = eligibleDay ? response.day : null;
        if (trigger === 'foreground' && pendingForeground === wallDay) pendingForeground = '';
        const fresh = eligibleDay ? outstanding(response.reminders, decided.current) : [];
        current.current = fresh;
        setCards(fresh);
        try { if (sessionStorage.getItem(key) === delivery) sessionStorage.removeItem(key); } catch { /* Optional reload recovery. */ }
        delivery = undefined;
        if (today && response.nextCheckSeconds !== null) {
          arm(() => { void check('scheduled'); }, response.nextCheckSeconds * 1000 + 100);
        }
      } catch {
        if (!disposed && ready && foreground()) {
          arm(() => {
            if (trigger === 'scheduled' || WALL_DAY.format(Date.now()) === wallDay) void check(trigger, true);
          }, 2800);
        }
      } finally {
        checking = false;
        if (queued) {
          const next = queued;
          queued = undefined;
          void check(next, true);
        }
      }
    }

    const onFocus = () => { void check('foreground', true); };
    const onVisibility = () => { if (foreground()) onFocus(); else clearDeadline(); };
    const onInput = (event: Event) => { if (event.isTrusted) void check('foreground'); };
    request.current = (trigger, force) => { void check(trigger, force); };
    connection.current = value => {
      if (ready === value) return;
      ready = value;
      if (!ready) {
        clearDeadline();
        setCards([]);
      } else if (pendingForeground === WALL_DAY.format(Date.now())) {
        pendingForeground = '';
        onFocus();
      } else if (today) {
        void check('scheduled', true);
      }
    };
    const initial = window.setTimeout(onFocus, 0);
    window.addEventListener('focus', onFocus);
    window.addEventListener('blur', clearDeadline);
    window.addEventListener('pointerdown', onInput, { passive: true });
    window.addEventListener('keydown', onInput);
    window.addEventListener('wheel', onInput, { passive: true });
    window.addEventListener(CHANGED, onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      disposed = true;
      window.clearTimeout(initial);
      clearDeadline();
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('blur', clearDeadline);
      window.removeEventListener('pointerdown', onInput);
      window.removeEventListener('keydown', onInput);
      window.removeEventListener('wheel', onInput);
      window.removeEventListener(CHANGED, onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [mobile, workspace, room, notice]);

  useEffect(() => { connection.current(available); }, [available, workspace, room, notice]);
  useEffect(() => {
    if (!peerChecks || !available) return;
    const poll = window.setInterval(() => request.current('scheduled'), 12_000);
    return () => window.clearInterval(poll);
  }, [peerChecks, available]);

  async function decide(card: Reminder, value: ReminderDecision) {
    const data = {
      version: card.version, action: value, disputeReceipt: card.kind === 'daily' ? card.disputeReceipt : undefined,
    };
    const saved = await action.run(() => workspace
      ? mobileRequest({ op: 'reminder-action', workspace, id: card.id, ...data }, room)
      : api(REMINDERS + '/' + card.id + '/action', 'POST', data));
    if (saved) {
      for (const receipt of receipts(card)) decided.current.set(receipt.id, receipt.version);
      const remaining = outstanding(current.current, decided.current);
      current.current = remaining;
      setCards(remaining);
    }
    request.current('scheduled', true);
  }

  return available && cards.length ? <aside className={mobile ? 'phone-reminders' : 'app-reminders'} aria-label="学习提醒">
    {cards.map(card => <section key={card.id + '/' + card.version} className={mobile ? 'study-card phone-reminder' : 'panel app-reminder'}
      aria-label={card.kind === 'lesson' ? TITLES.lesson + ' ' + card.title : TITLES[card.kind]}>
      <Bell size={19} aria-hidden="true" />
      <div className="app-reminder-content">
        <div role="status">
          <strong>{TITLES[card.kind]}</strong>
          {card.kind === 'daily' ? <p>{[card.reviews ? card.reviews + ' 项复习到期' : '', card.disputes
            ? card.disputes + ' 项争议待处理' + (mobile ? '，请在电脑查看' : '') : ''].filter(Boolean).join(' · ')}</p>
            : card.kind === 'budget' ? <p>{card.count} 项模型任务可继续</p>
            : <p>{card.title} · {card.projectTopic} · {dateLabel(card.slotDay)} {card.startsAt}</p>}
        </div>
        <div className="app-reminder-actions">
          {card.kind === 'daily' ? <>
            {!!card.reviews && (navigation
              ? <button type="button" className="text-action" onClick={navigation.reviews}>查看到期复习</button>
              : <Link className="text-action" to="/reviews">查看到期复习</Link>)}
            {!!card.disputes && !mobile && <Link className="text-action" to="/disputes">查看待处理争议</Link>}
          </> : card.kind === 'budget' ? <Link className="text-action" to="/settings/budget">查看可继续任务</Link>
            : navigation ? <button type="button" className="text-action" onClick={() => navigation.lesson(card)}>打开预约课程</button>
              : <Link className="text-action" to={courseUrl(card.lessonId)}>打开预约课程</Link>}
          <button className="text-action" type="button" disabled={action.pending} onClick={() => void decide(card, 'in_30_minutes')}>30 分钟后</button>
          <button className="text-action" type="button" disabled={action.pending} onClick={() => void decide(card, 'tomorrow')}>明天</button>
        </div>
      </div>
      <button className="icon-button" type="button" aria-label="关闭提醒" disabled={action.pending}
        onClick={() => void decide(card, 'dismiss')}><X size={17} /></button>
    </section>)}
    <ErrorNote message={action.error} />
  </aside> : null;
}

export function LocalReminders({ paired }: { paired: boolean }) {
  return <ReminderFeed connected peerChecks={paired} />;
}

export function PhoneReminders({ workspace, room, connected, navigation, notice }: {
  workspace?: string; room?: string; connected: boolean; navigation: PhoneNavigation; notice?: string;
}) {
  return <ReminderFeed key={room} workspace={workspace} room={room} connected={connected}
    peerChecks navigation={navigation} notice={notice} />;
}

type SettingsDraft = { enabled: boolean; scheduled: boolean; start: string; end: string };
type SettingsView = { value?: ReminderSettings; error?: string; refresh: () => void };

function SettingsForm({ read, saveSettings, mobile }: {
  read: SettingsView; saveSettings: (value: ReminderSettings) => Promise<ReminderSettings>; mobile?: boolean;
}) {
  const [draft, setDraft] = useState<SettingsDraft>();
  const [saved, setSaved] = useState(false);
  const action = useAction();
  const className = mobile ? 'study-card reminder-settings' : 'panel settings reminder-settings';
  const title = <h2><Bell size={20} />学习提醒</h2>;
  if (!read.value) return <section className={className}>{title}<BackedStatus error={read.error} retry={read.refresh} /></section>;
  const values = draft ?? {
    enabled: read.value.enabled, scheduled: read.value.quiet !== null,
    start: read.value.quiet?.start ?? '22:00', end: read.value.quiet?.end ?? '08:00',
  };

  function edit(update: Partial<SettingsDraft>) {
    setDraft({ ...values, ...update });
    setSaved(false);
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    const result = await action.run(() => saveSettings({
      enabled: values.enabled, quiet: values.scheduled ? { start: values.start, end: values.end } : null,
    }));
    if (result) {
      setSaved(true);
      read.refresh();
      refreshReminders();
    }
  }
  return <section className={className}>
    {title}
    <form onSubmit={save}>
      <fieldset disabled={action.pending}>
        <label className="reminder-switch"><input type="checkbox" checked={values.enabled} onChange={event => edit({ enabled: event.target.checked })} />
          启用学习提醒
        </label>
        <label className="reminder-switch"><input type="checkbox" checked={values.scheduled} onChange={event => edit({ scheduled: event.target.checked })} />
          使用免打扰时段
        </label>
        {values.scheduled && <div className="reminder-quiet">
          <label>开始时间<input type="time" required value={values.start} onChange={event => edit({ start: event.target.value })} /></label>
          <label>结束时间<input type="time" required value={values.end} onChange={event => edit({ end: event.target.value })} /></label>
          <span>北京时间</span>
        </div>}
      </fieldset>
      <ErrorNote message={action.error || read.error} retry={read.error ? read.refresh : undefined} />
      {saved && <p className="notice success" role="status">提醒设置已保存。</p>}
      <button className={mobile ? 'action secondary' : 'button secondary'} disabled={action.pending}>
        {action.pending ? '正在保存' : '保存提醒设置'}
      </button>
    </form>
  </section>;
}

export function ReminderPreferences() {
  const read = useRead<ReminderSettings>(REMINDERS + '/settings');
  return <SettingsForm read={read} saveSettings={value => api(REMINDERS + '/settings', 'PUT', value)} />;
}

function PhoneSettings({ workspace, room }: { workspace: string; room?: string }) {
  const [value, setValue] = useState<ReminderSettings>();
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true;
    void mobileRequest<ReminderSettings>({ op: 'reminder-settings', workspace }, room)
      .then(settings => { if (live) { setValue(settings); setError(''); } })
      .catch(error => { if (live) setError(error instanceof Error ? error.message : '暂时无法读取提醒设置。'); });
    return () => { live = false; };
  }, [workspace, room, revision]);
  return <SettingsForm mobile read={{ value, error, refresh: () => setRevision(count => count + 1) }}
    saveSettings={settings => mobileRequest({ op: 'reminder-save', workspace, ...settings }, room)} />;
}

export function PhoneReminderPreferences({ workspace, room }: { workspace: string; room?: string }) {
  const [expanded, setExpanded] = useState(false);
  return <details className="study-card phone-reminder-settings" onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary>提醒设置</summary>
    {expanded && <PhoneSettings key={room ?? workspace} workspace={workspace} room={room} />}
  </details>;
}
