import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { createRoot } from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import { ArrowLeft, BookOpen, Cloud, Download, ListChecks, RefreshCw, Trash2 } from 'lucide-react';
import { ApiError, hasDesktopSession } from './api';
import { advancePair, RelayUnavailable, takeInvitation } from './mobile-pairing';
import { DesktopPairingPanel, PhonePairingPanel } from './mobile-pairing-panel';
import { OfflineLecture } from './lecture-markdown';
import { mobileRequest, studyKey, type Answer, type Catalog, type LessonChoice, type LessonSnapshot,
  type Paper, type ReviewChange, type ReviewChoice, type ReviewHistory } from './mobile-data';
import { downloaded, downloadVersion, hasLocalWork, mergeReviewAnswers, NoteEditConflict, PairingChanged, phonePair, removeDownload,
  ReviewDraftConflict, saveAnswers, saveLesson, saveNote, savePaper, watchPhonePair,
  type LessonRecord, type MobilePair, type OfflineRecord, type ReviewRecord } from './mobile-store';
import { needsSync, synchronize } from './mobile-sync';
import { clearPhoneNotice, phoneNoticeFromUrl, phoneNoticeMatchesPair, PhonePushSettings, readPhoneNotice, rememberPhoneNotice,
  synchronizePushRemoval } from './mobile-push';
import { LocalTimerBanner, StartLocalTimer } from './mobile-timer-controls';
import { saveTimerAction, synchronizeTimers, timerCount, timerLedger, type TimerTarget } from './mobile-timer';
import { ErrorNote, useAction } from './shared';
import { PhoneReminderPreferences, PhoneReminders } from './reminders';
import type { LessonReminder } from './reminder-data';
import './mobile.css';

const desktop = hasDesktopSession();
const invitation = takeInvitation();
const initialNotice = desktop ? undefined : phoneNoticeFromUrl();

type Tab = 'lessons' | 'reviews' | 'sync';
type BeforeLeave = RefObject<(() => Promise<void | false>) | undefined>;

function useUnsavedUnloadGuard(guard: boolean) {
  useEffect(() => {
    if (!guard) return;
    function keep(event: BeforeUnloadEvent) { event.preventDefault(); }
    window.addEventListener('beforeunload', keep);
    return () => window.removeEventListener('beforeunload', keep);
  }, [guard]);
}

function inheritedLineEndings(base: string, draft: string) {
  const lf = base.replace(/\r\n/g, '\n');
  return lf !== base && !/(?:^|[^\r])\n/.test(base) ? draft.replace(/\r?\n/g, '\r\n') : draft;
}

function message(error: unknown) {
  if (error instanceof DOMException && error.name === 'QuotaExceededError') return '本机可用空间不足。请移除已用完的下载内容。';
  return error instanceof Error ? error.message : String(error);
}

function ReviewState({ entry }: { entry: ReviewRecord }) {
  if (!entry.submission) return <small>{entry.draft.some(value => value !== null) ? '本机草稿' : '可离线作答'}</small>;
  const saved = entry.reply?.state;
  const label = saved === 'settled' ? '已评价' : saved === 'grading' ? '等待电脑评价'
    : saved === 'held' ? '待处理' : '作答已保存到本机';
  return <small role="status">{label}</small>;
}

function Reading({ entry, changed, onBack, beforeLeave, timerControl, noteTimer }: {
  entry: LessonRecord; changed: (record: OfflineRecord) => void; onBack: () => void; beforeLeave: BeforeLeave;
  timerControl: ReactNode; noteTimer: ReactNode;
}) {
  const note = entry.note;
  const content = note?.content;
  const latest = useRef({ draft: content ?? '', base: content ?? '' });
  const [{ draft, base }, setText] = useState(latest.current);
  const writeInProgress = useRef<Promise<void>>(undefined);
  const [writing, setWriting] = useState(false);
  const [comparison, setComparison] = useState<string>();
  const save = useAction();
  const clean = draft === base;
  const saveStatus = writing ? '正在保存到本机' : !clean ? '笔记有未保存的修改'
    : note?.editedAt && !note.conflict ? '笔记已保存到本机' : '';
  const updateText = useCallback((next: { draft: string; base: string }) => {
    latest.current = next;
    setText(next);
  }, []);
  useEffect(() => {
    const current = latest.current;
    if (comparison === undefined && !writing && current.draft === current.base && content !== undefined && content !== current.base) {
      updateText({ draft: content, base: content });
    }
  }, [content, clean, comparison, writing, updateText]);
  useUnsavedUnloadGuard(!clean || writing || comparison !== undefined);

  const persist = useCallback(async (reviewed?: string) => {
    if (writeInProgress.current) return writeInProgress.current;
    const { draft: submitted, base } = latest.current;
    if (reviewed === undefined && submitted === base) return;
    const expected = reviewed ?? base;
    const text = inheritedLineEndings(expected, submitted);
    setWriting(true);
    const write = saveNote(entry.key, expected, text).then(record => {
      const current = latest.current.draft;
      updateText({ draft: current === submitted ? text : current, base: text });
      setComparison(undefined);
      if (record) changed(record);
    }).catch(error => {
      if (error instanceof NoteEditConflict) setComparison(error.current);
      throw error;
    }).finally(() => {
      writeInProgress.current = undefined;
      setWriting(false);
    });
    writeInProgress.current = write;
    return write;
  }, [changed, entry.key, updateText]);
  const saveBeforeLeaving = useCallback(async (): Promise<void | false> => {
    if (comparison !== undefined && !writeInProgress.current) return false;
    try {
      while (writeInProgress.current || latest.current.draft !== latest.current.base) await persist();
    } catch (error) {
      if (error instanceof NoteEditConflict) return false;
      throw error;
    }
  }, [comparison, persist]);
  useEffect(() => {
    beforeLeave.current = saveBeforeLeaving;
    return () => { beforeLeave.current = undefined; };
  }, [beforeLeave, saveBeforeLeaving]);
  return <section className="study-detail">
    <button type="button" className="link-button" onClick={onBack}>
      <ArrowLeft size={18} />返回课程
    </button>
    <h2>{entry.snapshot.title}</h2>
    <p className="subtle">{entry.snapshot.projectTopic}</p>
    {timerControl}
    <article className="reader"><OfflineLecture body={entry.snapshot.body} /></article>
    {note && <section className="study-card note-box" aria-label="笔记">
      <h3>{note.title}</h3>
      {comparison !== undefined && <>
        <label htmlFor="phone-note-comparison">本机已保存的版本</label>
        <textarea id="phone-note-comparison" value={comparison} readOnly rows={5} />
        <p className="subtle">请核对两份内容，在「本节笔记」中编辑合并结果。</p>
      </>}
      <label htmlFor="phone-note">本节笔记</label>
      <textarea id="phone-note" value={draft} onChange={event => updateText({ ...latest.current, draft: event.target.value })} maxLength={50000} rows={7} />
      <div className="actions">
        {noteTimer}
        <button type="button" className="action" disabled={writing || (clean && comparison === undefined)}
          onClick={() => { void save.run(() => persist(comparison)); }}>
          {comparison === undefined ? '保存到本机' : '保存合并结果'}
        </button>
        <span role="status">{saveStatus}</span>
        {note.conflict && <span role="status">与电脑笔记冲突 · 待处理</span>}
      </div>
      <ErrorNote message={save.error} />
    </section>}
  </section>;
}

function Questionnaire({ entry, changed, onBack, beforeLeave, timerControl }: {
  entry: ReviewRecord; changed: (record: OfflineRecord) => void; onBack: () => void; beforeLeave: BeforeLeave;
  timerControl: ReactNode;
}) {
  type Editor = { draft: ReviewRecord['draft']; base: ReviewRecord['draft']; comparison?: ReviewDraftConflict };
  const latest = useRef<Editor>({ draft: entry.draft, base: entry.draft });
  const [{ draft: answers, base, comparison }, setEditor] = useState(latest.current);
  const updateEditor = useCallback((state: Editor) => {
    latest.current = state;
    setEditor(state);
  }, []);
  const submittedHere = useRef(false);
  const writeInProgress = useRef<{ action?: ReviewChange['action']; promise: Promise<void> }>(undefined);
  const [writing, setWriting] = useState<'draft' | ReviewChange['action']>();
  const [confirmSubmission, setConfirmSubmission] = useState(false);
  const save = useAction();
  const paper = entry.snapshot;
  const submitted = !!entry.submission;
  useUnsavedUnloadGuard(writing !== undefined || (!submitted && (answers !== base || comparison !== undefined)));
  const persist = useCallback(async (action?: ReviewChange['action'], resolveComparison = false) => {
    if (writeInProgress.current) return writeInProgress.current.promise;
    if (submitted || submittedHere.current) return;
    const { draft, base, comparison } = latest.current;
    if (comparison && !resolveComparison) return;
    if (!action && !comparison && draft === base) return;
    const expected = comparison
      ? base.map((value, index) => comparison.conflicts.includes(index) ? comparison.saved[index] : value)
      : base;

    setConfirmSubmission(false);
    setWriting(action ?? 'draft');
    const promise = saveAnswers(entry.key, expected, draft, action, comparison?.conflicts).then(record => {
      if (record?.kind !== 'review') return;
      const saved = record.draft;
      if (record.submission) {
        submittedHere.current = true;
        updateEditor({ draft: saved, base: saved });
      } else {
        const pending = mergeReviewAnswers(draft, latest.current.draft, saved);
        const unchanged = pending.answers.every((value, index) => value === saved[index]);
        updateEditor({ draft: unchanged ? saved : pending.answers, base: saved,
          comparison: pending.conflicts.length ? new ReviewDraftConflict(saved, pending.conflicts) : undefined });
        setConfirmSubmission(action !== undefined);
      }
      changed(record);
    }).catch(error => {
      if (!(error instanceof ReviewDraftConflict)) throw error;
      const current = latest.current;
      const conflicts = [...new Set([...(comparison?.conflicts ?? []), ...error.conflicts])];
      updateEditor({ ...current, comparison: new ReviewDraftConflict(error.saved, conflicts) });
    }).finally(() => {
      writeInProgress.current = undefined;
      setWriting(undefined);
    });
    writeInProgress.current = { action, promise };
    return promise;
  }, [changed, entry.key, submitted, updateEditor]);
  const saveBeforeLeaving = useCallback(async (): Promise<void | false> => {
    while (writeInProgress.current || (!submitted && !submittedHere.current)) {
      if (writeInProgress.current) await writeInProgress.current.promise;
      else if (latest.current.comparison) return false;
      else if (latest.current.draft === latest.current.base) return;
      else await persist();
    }
  }, [persist, submitted]);
  useEffect(() => {
    beforeLeave.current = saveBeforeLeaving;
    return () => { beforeLeave.current = undefined; };
  }, [beforeLeave, saveBeforeLeaving]);
  function update(index: number, value: Answer | null) {
    if (submitted || submittedHere.current || writeInProgress.current?.action) return;
    const current = latest.current;
    updateEditor({ ...current, draft: current.draft.map((item, position) => position === index ? value : item) });
  }
  return <section className="study-detail">
    <button type="button" className="link-button" onClick={onBack}><ArrowLeft size={18} />返回复习</button>
    <h2>{paper.title}</h2>
    <p className="subtle">{paper.projectTopic} · 第 {paper.round} 轮</p>
    {timerControl}
    {paper.questions.map((question, index) => {
      const stored = comparison?.saved[index];
      return <fieldset key={index} className="study-card question" disabled={submitted || writing === 'submit' || writing === 'unknown'}>
        <legend>{question.text}</legend>
        {question.options ? question.options.map((option, position) => <label className="option" key={position}>
          <input type="radio" name={'question-' + index} checked={answers[index] === position} onChange={() => update(index, position)} />
          <span>{option}</span>
        </label>) : <textarea aria-label={'第 ' + (index + 1) + ' 题答案'} rows={4}
          maxLength={3000} value={typeof answers[index] === 'string' ? answers[index] : ''} onChange={event => update(index, event.target.value)} />}
        {comparison?.conflicts.includes(index) && <div className="review-comparison" role="group" aria-label={'第 ' + (index + 1) + ' 题答案核对'}>
          <p className="subtle">本机已保存的答案</p>
          <p>{typeof stored === 'number' ? question.options?.[stored] : stored || '未作答'}</p>
          <button className="action secondary" type="button" disabled={writing !== undefined}
            onClick={() => update(index, comparison.saved[index])}>采用本机第 {index + 1} 题答案</button>
        </div>}
      </fieldset>;
    })}
    {entry.submission ? <div className="study-card">
      <ReviewState entry={entry} />
      {entry.reply?.reason && <p>{entry.reply.reason}</p>}
      {entry.reply?.result && <>
        <p>{entry.reply.result.explanation}</p>
        {entry.reply.result.dueOn && <p>下次复习 {entry.reply.result.dueOn}</p>}
      </>}
    </div>
      : comparison ? <div className="actions">
        <p role="status">{comparison.message}</p>
        <button className="action" type="button" disabled={writing !== undefined}
          onClick={() => { void save.run(() => persist(undefined, true)); }}>保存核对结果</button>
      </div>
      : <div className="actions">
        <button className="action" type="button" disabled={writing !== undefined} onClick={() => { void save.run(() => persist('submit')); }}>提交作答</button>
        <button className="action secondary" type="button" disabled={writing !== undefined} onClick={() => { void save.run(() => persist()); }}>保存草稿</button>
        <button className="action secondary" type="button" disabled={writing !== undefined} onClick={() => { void save.run(() => persist('unknown')); }}>我不会</button>
      </div>}
    {confirmSubmission && !submitted && <p role="status">已合并另一页保存的答案，请核对后再提交。</p>}
    <ErrorNote message={save.error} />
  </section>;
}

function MobileStudy() {
  const [receivedNotice, setNotice] = useState(initialNotice);
  const [validatedNotice, setValidatedNotice] = useState<typeof initialNotice>();
  const notice = validatedNotice === receivedNotice ? validatedNotice : undefined;
  const [noticeError, setNoticeError] = useState<{ delivery: string; message: string; retry: boolean }>();
  const [tab, setTab] = useState<Tab>(invitation ? 'sync' : 'lessons');
  const [phone, setPhone] = useState<MobilePair>();
  const [pairPrompt, setPairPrompt] = useState(invitation);
  const [active, setActive] = useState<string>();
  const [items, setItems] = useState<OfflineRecord[]>([]);
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [catalog, setCatalog] = useState<Catalog>();
  const [reminderWorkspace, setReminderWorkspace] = useState<string>();
  const [history, setHistory] = useState<ReviewHistory[]>([]);
  const [online, setOnline] = useState(navigator.onLine);
  const [connecting, setConnecting] = useState(false);
  const [storageError, setStorageError] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [timers, setTimers] = useState<Awaited<ReturnType<typeof timerLedger>>>();
  const [pendingTimerReview, setPendingTimerReview] = useState(0);
  const [timerError, setTimerError] = useState('');
  const [pushSyncVersion, setPushSyncVersion] = useState(0);
  const timerAction = useAction();
  const update = useAction();
  const live = useRef(false);
  const busy = useRef(false);
  const pairingRevision = useRef(0);
  const beforeLeave = useRef<BeforeLeave['current']>(undefined);
  const handledNotice = useRef<string>(undefined);
  const noticeFailure = noticeError?.delivery === notice?.delivery ? noticeError : undefined;
  const mayHaveChanges = items.some(needsSync);
  const pendingTimers = timerCount(timers);
  const shown = items.find(item => item.key === active);
  const pending = items.filter(item => item.kind === 'lesson' ? item.note?.editedAt && !item.note.conflict : item.submission && !item.reply);
  const held = items.filter(item => item.kind === 'lesson' ? item.note?.conflict : item.reply?.state === 'held');
  const assessments = items.filter(item => item.kind === 'review' && item.reply?.state === 'grading');

  const refresh = useCallback(async () => {
    const [local, timing] = await Promise.all([downloaded(), timerLedger()]);
    if (live.current) { setItems(local); setTimers(timing); }
  }, []);

  const connect = useCallback(async (manual = false) => {
    if (busy.current || !live.current) return;
    if (!navigator.onLine) {
      setOnline(false); setCatalog(undefined); setConnectionError('');
      return;
    }
    const revision = pairingRevision.current;
    const isCurrent = () => live.current && pairingRevision.current === revision;
    busy.current = true;
    if (manual) setConnecting(true);
    try {
      let room: string | undefined;
      if (!desktop) {
        const pair = await phonePair();
        if (!isCurrent()) return;
        setPhone(pair);
        if (!pair) {
          setCatalog(undefined);
          setReminderWorkspace(undefined);
          return;
        }
        if (pair.invitation) {
          const approved = await advancePair(pair);
          if (!isCurrent()) return;
          if (!approved) {
            setConnectionError('');
            return;
          }
          const saved = await phonePair();
          if (!isCurrent() || saved?.room !== pair.room || saved.invitation) return;
          setPhone(saved);
        }
        room = pair.room;
      }
      const next = await mobileRequest<Catalog>({ op: 'catalog' }, room);
      if (!isCurrent()) return;
      setCatalog(next);
      setReminderWorkspace(next.workspace);
      setConnectionError('');
      if (room) void synchronizePushRemoval(room, next.workspace).then(status => {
        if (status && isCurrent()) setPushSyncVersion(value => value + 1);
      }).catch(() => { /* The saved removal will retry on the next connection. */ });
      try {
        const pending = await synchronizeTimers(next.workspace, room);
        if (isCurrent()) { setPendingTimerReview(pending); setTimerError(''); }
      } catch (error) {
        if (error instanceof PairingChanged || error instanceof RelayUnavailable) throw error;
        if (isCurrent()) setTimerError(error instanceof ApiError && [404, 405, 422].includes(error.status)
          ? '电脑端需要更新后才能同步计时。' : message(error));
      }
      if (!isCurrent()) return;
      await synchronize(next.workspace, room);
      await refresh();
      if (desktop) {
        const saved = await mobileRequest<{ reviews: ReviewHistory[] }>({ op: 'history' });
        if (isCurrent()) setHistory(saved.reviews);
      }
    } catch (error) {
      if (!isCurrent() || error instanceof PairingChanged) return;
      if (error instanceof RelayUnavailable || error instanceof ApiError && [401, 410, 503].includes(error.status)) {
        setCatalog(undefined);
      }
      setConnectionError(error instanceof ApiError && error.status === 401
        ? '请从电脑的智学会话打开手机离线页。' : message(error));
      await refresh().catch(failure => { if (isCurrent()) setStorageError(message(failure)); });
    } finally {
      busy.current = false;
      if (live.current) setConnecting(false);
    }
  }, [refresh]);

  const resetPairConnection = useCallback(() => {
    pairingRevision.current++;
    setCatalog(undefined);
    setReminderWorkspace(undefined);
    setConnectionError('');
    setPendingTimerReview(0);
    setTimerError('');
    const pendingNotice = phoneNoticeFromUrl();
    if (pendingNotice) clearPhoneNotice(pendingNotice.delivery);
    setNotice(undefined);
  }, []);

  useEffect(() => {
    live.current = true;
    let alive = true;
    const loadPhone = (reconnect: boolean) => {
      const revision = pairingRevision.current;
      const isCurrent = () => alive && pairingRevision.current === revision;
      void phonePair().then(pair => {
        if (!isCurrent()) return;
        setPhone(pair);
        if (reconnect && pair) void connect();
      }).catch(error => { if (isCurrent()) setStorageError(message(error)); });
    };
    const stopWatchingPair = desktop ? undefined : watchPhonePair(() => {
      resetPairConnection();
      setPhone(undefined);
      loadPhone(true);
    });
    if (!desktop) loadPhone(false);
    void refresh().catch(error => { if (alive) setStorageError(message(error)); }).finally(() => { if (alive) setLoading(false); });
    if (!('serviceWorker' in navigator)) {
      setStorageError('当前地址无法准备离线页面。请用受支持的浏览器和安全地址打开。');
    } else {
      void navigator.serviceWorker.register('/mobile/sw.js', { scope: '/mobile/' })
        .then(() => navigator.serviceWorker.ready).then(() => { if (alive) setReady(true); })
        .catch(error => { if (alive) setStorageError(message(error)); });
    }
    const connection = () => { setOnline(navigator.onLine); void connect(); };
    connection();
    window.addEventListener('online', connection);
    window.addEventListener('offline', connection);
    window.addEventListener('focus', connection);
    return () => {
      alive = false; live.current = false;
      stopWatchingPair?.();
      window.removeEventListener('online', connection);
      window.removeEventListener('offline', connection);
      window.removeEventListener('focus', connection);
    };
  }, [refresh, connect, resetPairConnection]);

  useEffect(() => {
    if (desktop) return;
    const fromRoute = () => {
      const nextInvitation = takeInvitation();
      if (nextInvitation !== undefined) {
        setPairPrompt(nextInvitation);
        switchTab('sync');
      }
      const push = phoneNoticeFromUrl();
      if (push && handledNotice.current !== push.delivery) {
        setNotice(current => current?.delivery === push.delivery ? current : push);
      }
    };
    const receive = (event: MessageEvent) => {
      if (event.data?.type !== 'zhixue-notice') return;
      const push = readPhoneNotice(event.data.notice);
      if (push && handledNotice.current !== push.delivery) {
        rememberPhoneNotice(push);
        setNotice(push);
      }
    };
    window.addEventListener('hashchange', fromRoute);
    fromRoute();
    if ('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message', receive);
    return () => {
      window.removeEventListener('hashchange', fromRoute);
      if ('serviceWorker' in navigator) navigator.serviceWorker.removeEventListener('message', receive);
    };
  }, []);

  useEffect(() => {
    if (!online || !(mayHaveChanges || phone || timers?.active || pendingTimers)) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void connect();
    }, phone?.invitation || timers?.active || pendingTimers ? 2500 : 10000);
    return () => window.clearInterval(timer);
  }, [online, mayHaveChanges, phone?.room, phone?.invitation?.room, timers?.active?.id, pendingTimers, connect]);

  function changeTimer(intent: Parameters<typeof saveTimerAction>[0]) {
    void timerAction.run(async () => {
      const saved = await saveTimerAction(intent, navigator.onLine ? catalog?.workspace : undefined);
      if (live.current) setTimers(saved);
      if (catalog && navigator.onLine) void connect();
    });
  }

  function timerButton(target: TimerTarget) {
    return <StartLocalTimer target={target} timer={timers?.active} disabled={timerAction.pending}
      start={target => changeTimer({ action: 'start', target })} />;
  }

  function changePair(pair?: MobilePair) {
    resetPairConnection();
    setPhone(pair);
    if (pair) {
      setPairPrompt(undefined);
      void connect();
    }
  }

  const chosen = useCallback((record: OfflineRecord) => {
    setItems(current => [...current.filter(item => item.key !== record.key), record]);
  }, []);

  function loadChoice(lesson: LessonChoice | ReviewChoice) {
    const workspace = catalog?.workspace;
    const room = phone?.room;
    if (!workspace || !ready || !desktop && (!room || phone?.invitation)) return;
    void navigator.storage?.persist?.().catch(() => false);
    void update.run(async () => {
      const key = studyKey('lessonId' in lesson ? 'review' : 'lesson', workspace, lesson.id);
      const version = await downloadVersion(key);
      if ('lessonId' in lesson) {
        const data = await mobileRequest<Paper>({ op: 'paper', lesson: lesson.lessonId, key: lesson.key }, room);
        if (data.workspace !== workspace || data.id !== lesson.id) throw new Error('题组已变化，请刷新电脑的下载列表。');
        await savePaper(data, room, version);
      } else {
        const data = await mobileRequest<LessonSnapshot>({ op: 'course', lesson: lesson.id }, room);
        if (data.workspace !== workspace || data.id !== lesson.id) throw new Error('课程已变化，请刷新电脑的下载列表。');
        await saveLesson(data, room, version);
      }
      await refresh();
    });
  }

  function remove(key: string) {
    void update.run(async () => { await removeDownload(key); await refresh(); });
  }

  function switchTab(next: Tab) {
    void update.run(async () => {
      if (await beforeLeave.current?.() === false) return;
      setTab(next);
      setActive(undefined);
    });
  }

  function returnToStudyList() {
    void update.run(async () => {
      if (await beforeLeave.current?.() === false) return;
      setActive(undefined);
    });
  }

  async function showAppointment(lessonId: string, workspace: string) {
    const key = studyKey('lesson', workspace, lessonId);
    const revision = pairingRevision.current;
    if (!items.some(item => item.key === key)) {
      const room = phone?.room;
      if (!desktop && (!room || phone?.invitation)) throw new PairingChanged();
      const version = await downloadVersion(key);
      const lesson = await mobileRequest<LessonSnapshot>({ op: 'course', lesson: lessonId }, room);
      if (lesson.workspace !== workspace) throw new Error('提醒对应的工作区已更换。');
      if (lesson.id !== lessonId) throw new Error('没有收到预约课程，请重试。');
      const saved = await saveLesson(lesson, room, version);
      await refresh();
      if (!saved) throw new Error('本机下载已移除，请重新打开课程。');
    }
    if (await beforeLeave.current?.() === false) return false;
    if (pairingRevision.current !== revision) throw new PairingChanged();
    setTab('lessons');
    setActive(key);
    window.scrollTo(0, 0);
  }

  function openAppointment(card: LessonReminder) {
    const workspace = catalog?.workspace;
    if (workspace) void update.run(() => showAppointment(card.lessonId, workspace));
  }

  useEffect(() => {
    if (!receivedNotice) return;
    let active = true;
    const revision = pairingRevision.current;
    const settle = (matches: boolean) => {
      if (!active || pairingRevision.current !== revision) return;
      if (matches) setValidatedNotice(receivedNotice);
      else {
        clearPhoneNotice(receivedNotice.delivery);
        setNotice(current => current === receivedNotice ? undefined : current);
      }
    };
    void phoneNoticeMatchesPair(receivedNotice).then(settle, () => settle(false));
    return () => { active = false; };
  }, [receivedNotice]);

  useEffect(() => {
    if (!notice || handledNotice.current === notice.delivery || loading || update.pending) return;
    const { delivery, workspace, lessonId } = notice;
    const connected = catalog?.workspace;
    if (connected && connected !== workspace) {
      handledNotice.current = delivery;
      clearPhoneNotice(delivery);
      setNoticeError({ delivery, message: '提醒对应的工作区已更换。', retry: false });
      return;
    }
    if (noticeFailure) return;
    if (!connected && lessonId && !items.some(item => item.key === studyKey('lesson', workspace, lessonId))) return;
    const revision = pairingRevision.current;
    void update.run(async () => {
      try {
        if (!await phoneNoticeMatchesPair(notice) || pairingRevision.current !== revision) {
          clearPhoneNotice(delivery);
          setNotice(current => current === notice ? undefined : current);
          return;
        }
        if (lessonId) {
          if (await showAppointment(lessonId, workspace) === false) throw new Error('请先核对并保存当前草稿。');
        } else {
          if (await beforeLeave.current?.() === false) throw new Error('请先核对并保存当前草稿。');
          if (pairingRevision.current !== revision) return;
          setTab('reviews');
          setActive(undefined);
        }
      } catch (error) {
        if (pairingRevision.current !== revision || error instanceof PairingChanged) return;
        setNoticeError({ delivery, message: message(error), retry: true });
        return;
      }
      handledNotice.current = delivery;
      clearPhoneNotice(delivery);
    });
  }, [notice, noticeFailure, catalog, items, loading, update.pending]);

  const here = tab === 'lessons' ? 'lesson' : 'review';
  const offlineItems = items.filter(item => item.kind === here);
  const choices = tab === 'lessons' ? catalog?.lessons : catalog?.reviews;
  const available = choices?.filter(choice => catalog && !items.some(item => item.key === studyKey(here, catalog.workspace, choice.id)));
  return <div className="mobile-shell">
    <header className="mobile-head">
      <img src="/zhixue.png" alt="" width="42" height="42" />
      <h1>智学离线</h1>
      <span className={'connection' + (online && catalog ? ' connected' : '')}>{online ? (catalog ? '电脑已连接' : '等待连接') : '离线'}</span>
    </header>
    <nav className="mobile-tabs" aria-label="手机学习">
      <button type="button" aria-pressed={tab === 'lessons'} onClick={() => switchTab('lessons')}><BookOpen size={19} />课程</button>
      <button type="button" aria-pressed={tab === 'reviews'} onClick={() => switchTab('reviews')}><ListChecks size={19} />复习</button>
      <button type="button" aria-pressed={tab === 'sync'} onClick={() => switchTab('sync')}><Cloud size={19} />同步</button>
    </nav>
    <main aria-busy={loading}>
      <PhoneReminders workspace={reminderWorkspace} room={phone?.room} connected={online && catalog !== undefined}
        notice={notice?.workspace === reminderWorkspace ? notice?.delivery : undefined}
        navigation={{ reviews: () => switchTab('reviews'), lesson: openAppointment }} />
      <ErrorNote message={storageError || update.error || timerAction.error} />
      <ErrorNote message={noticeFailure?.message} retry={noticeFailure?.retry ? () => {
        setNoticeError(undefined);
        if (!catalog) void connect();
      } : undefined} />
      {notice?.lessonId && !catalog && !loading && !noticeFailure && handledNotice.current !== notice.delivery &&
        <p role="status">连接电脑后可打开预约课程。</p>}
      {timers?.active && <LocalTimerBanner timer={timers.active} disabled={timerAction.pending}
        change={(action, id) => changeTimer({ action, id })} />}
      {shown?.kind === 'lesson' ? <Reading key={shown.key} entry={shown} changed={chosen} beforeLeave={beforeLeave} onBack={returnToStudyList}
        timerControl={timerButton({ workspace: shown.snapshot.workspace, lessonId: shown.snapshot.id, title: shown.snapshot.title, category: 'learning' })}
        noteTimer={timerButton({ workspace: shown.snapshot.workspace, lessonId: shown.snapshot.id, title: shown.snapshot.title, category: 'notes' })} />
        : shown?.kind === 'review' ? <Questionnaire key={shown.key} entry={shown} changed={chosen} beforeLeave={beforeLeave} onBack={returnToStudyList}
          timerControl={timerButton({ workspace: shown.snapshot.workspace, lessonId: shown.snapshot.lessonId, title: shown.snapshot.title, reviewKey: shown.snapshot.key, category: 'review' })} />
        : tab === 'sync' ? <section>
          <h2>同步到电脑</h2>
          <div className="study-card">
            <div className="actions"><strong>{pending.length} 项待同步</strong>
              <button className="action" type="button" disabled={!online || connecting} onClick={() => { void connect(true); }}>
                <RefreshCw size={17} />{connecting ? '正在同步' : '同步到电脑'}
              </button>
            </div>
            {!!pendingTimers && <p>{pendingTimers} 次计时操作待同步。</p>}
            {!!pendingTimerReview && <p>{pendingTimerReview} 个时段待核对。请打开电脑的管理中心。</p>}
            {timerError && <ErrorNote message={timerError} />}
            {!!assessments.length && <p>{assessments.length} 份作答等待电脑评价。</p>}
            {!!held.length && <p>{held.length} 项本机记录待处理。</p>}
            {catalog && (items.some(entry => entry.snapshot.workspace !== catalog.workspace && needsSync(entry)) ||
              timers?.events.some(entry => entry.workspace !== catalog.workspace)) &&
              <p>这台设备还保存了其他工作区的待同步记录。</p>}
            <ErrorNote message={connectionError} />
          </div>
          {online && catalog && <PhoneReminderPreferences workspace={catalog.workspace} room={phone?.room} />}
          {!desktop && phone && !phone.invitation &&
            <PhonePushSettings key={phone.room} room={phone.room} workspace={online ? catalog?.workspace : undefined}
              syncVersion={pushSyncVersion} />}
          {desktop ? <DesktopPairingPanel /> : <PhonePairingPanel invite={pairPrompt} pair={phone}
            changed={changePair} online={online} />}
          {!!held.length && <section>
            <h3>本机待处理</h3>
            {held.map(item => <article className="study-card" key={item.key}>
              <strong>{item.snapshot.title}</strong>
              {item.kind === 'lesson' && item.note?.conflict
                ? <p>{desktop && catalog ? <a href={'/#/library?file=note-' + item.note.id}>在电脑核对笔记冲突</a> : '待在电脑上核对笔记冲突。'}</p>
                : item.kind === 'review' ? <p>{item.reply?.reason}</p> : null}
            </article>)}
          </section>}
          {!!history.length && <section>
            <h3>电脑作答回执</h3>
            {history.map(row => <details className="study-card" key={row.id}>
              <summary>第 {row.round} 轮 · {row.state === 'held' ? '待处理' : row.state === 'grading' ? '等待评价' : '已评价'}</summary>
              {row.reason && <p>{row.reason}</p>}
              <p>电脑接收于 {new Date(row.receivedAt.endsWith('Z') ? row.receivedAt : row.receivedAt + 'Z').toLocaleString()}</p>
              {!!row.answers.length && <ol>{row.answers.map((answer, index) => <li key={index}>{typeof answer === 'number' ? '选择第 ' + (answer + 1) + ' 项' : answer}</li>)}</ol>}
            </details>)}
          </section>}
          <p className="subtle">可从浏览器菜单将智学添加到主屏幕。待同步记录保存在当前浏览器。</p>
          {desktop && online && <a href="/">打开电脑工作区</a>}
        </section> : <section>
          <h2>{tab === 'lessons' ? '已下载课程' : '已下载题组'}</h2>
          {offlineItems.length ? <ul className="study-list">{offlineItems.map(item => <li className="study-card study-list-row" key={item.key}>
            <button type="button" className="card-button" aria-label={'打开' + (item.kind === 'lesson' ? '课程 ' : '题组 ') + item.snapshot.title}
              onClick={() => { setActive(item.key); window.scrollTo(0, 0); }}>
              <strong>{item.snapshot.title}</strong><span>{item.snapshot.projectTopic}</span>
              {item.kind === 'review' ? <ReviewState entry={item} /> : item.note?.editedAt ? <small>本机笔记待同步</small> : null}
            </button>
            <button type="button" className="remove" title="移除本机下载" aria-label={'移除下载 ' + item.snapshot.title}
              disabled={hasLocalWork(item) || update.pending} onClick={() => remove(item.key)}><Trash2 size={17} /></button>
          </li>)}</ul> : <p className="study-card subtle">{loading ? '正在读取本机内容…' : '此设备还没有下载内容。'}</p>}
          {catalog && online && <>
            <h2>{tab === 'lessons' ? '从电脑下载课程' : '从电脑下载题组'}</h2>
            {!ready && <p role="status" className="subtle">正在准备离线页面…</p>}
            {available?.length ? <ul className="study-list">{available.map(choice => <li className="study-card study-list-row" key={choice.id}>
              <div><strong>{choice.title}</strong><p className="subtle">{choice.projectTopic}</p></div>
              <button type="button" className="download action secondary"
                aria-label={'下载' + (tab === 'lessons' ? '课程 ' : '题组 ') + choice.title}
                disabled={!ready || update.pending} onClick={() => loadChoice(choice)}><Download size={16} />下载</button>
            </li>)}</ul> : <p className="study-card subtle">{tab === 'lessons' ? '没有其他已准备课程。' : '没有其他已准备的到期题组。'}</p>}
          </>}
        </section>}
    </main>
  </div>;
}

createRoot(document.getElementById('root')!).render(<HashRouter><MobileStudy /></HashRouter>);
