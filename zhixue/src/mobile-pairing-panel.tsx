import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from './api';
import { preparePair } from './mobile-pairing';
import { browserPushSubscription, dismissPhoneReminders } from './mobile-push';
import { PairingChanged, phonePairVersion, savePair, type MobilePair } from './mobile-store';
import { ErrorNote, useAction } from './shared';

type DesktopStatus = {
  revision: string;
  paired: { name: string } | null;
  invitation: { url: string; expiresAt: number } | null;
  candidate: { name: string; code: string } | null;
  error: string | null;
};

const endpoint = '/device-sync';

function desktopRequest(method = 'GET', path = '', data?: object, revision?: string) {
  const query = revision ? '?' + new URLSearchParams({ revision }) : '';
  return api<DesktopStatus>(endpoint + path + query, method, data, AbortSignal.timeout(5000));
}

export function DesktopPairingPanel() {
  const [status, setStatus] = useState<DesktopStatus>();
  const [relay, setRelay] = useState('');
  const [mobile, setMobile] = useState('');
  const [copied, setCopied] = useState(false);
  const action = useAction();
  const requests = useRef({ mutation: 0, changing: false });
  const { invitation, candidate, paired } = status ?? {};

  useEffect(() => {
    let live = true;
    let polling = false;
    const refresh = () => {
      if (polling || requests.current.changing) return;
      polling = true;
      const mutation = requests.current.mutation;
      void desktopRequest().then(next => {
        if (live && mutation === requests.current.mutation && !requests.current.changing) setStatus(next);
      }).catch(() => {}).finally(() => { polling = false; });
    };
    refresh();
    const timer = window.setInterval(refresh, 1200);
    return () => { live = false; window.clearInterval(timer); };
  }, []);

  function change(method: string, path = '', data?: object) {
    if (!status) return;
    const revision = status.revision;
    void action.run(async () => {
      requests.current.mutation++;
      requests.current.changing = true;
      try {
        setStatus(await desktopRequest(method, path, data, revision));
        setCopied(false);
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          await desktopRequest().then(setStatus, () => {});
        }
        throw error;
      } finally {
        requests.current.changing = false;
      }
    });
  }

  return <section className="study-card pair-panel">
    <h3>配对手机</h3>
    {paired ? <>
      <p role="status">已配对 {paired.name}</p>
      <button className="action secondary" type="button" disabled={action.pending}
        onClick={() => change('DELETE')}>撤销手机配对</button>
    </> : invitation ? <>
      <label htmlFor="pair-link">手机配对链接</label>
      <input id="pair-link" readOnly value={invitation.url} onFocus={event => event.currentTarget.select()} />
      <p className="subtle">在手机打开链接。邀请有效至 {new Date(invitation.expiresAt * 1000).toLocaleTimeString()}。</p>
      <div className="actions">
        <button className="action secondary" type="button" disabled={action.pending} onClick={() => {
          void action.run(async () => { await navigator.clipboard.writeText(invitation.url); setCopied(true); });
        }}>{copied ? '已复制' : '复制配对链接'}</button>
        <button className="action secondary" type="button" disabled={action.pending}
          onClick={() => change('DELETE', '/invite')}>取消邀请</button>
      </div>
      {candidate ? <>
        <p>来自 {candidate.name} 的请求。核对手机显示的配对码。</p>
        <p className="pair-code"><output aria-label="电脑配对码">{candidate.code}</output></p>
        <button className="action" type="button" disabled={action.pending}
          onClick={() => change('POST', '/approve', { code: candidate.code })}>确认配对码并批准</button>
      </> : <p role="status">等待手机申请配对。</p>}
    </> : <form onSubmit={event => {
      event.preventDefault();
      change('POST', '/invite', { relay, mobile });
    }}>
      <label htmlFor="trusted-mobile">手机网页地址</label>
      <input id="trusted-mobile" type="url" value={mobile} onChange={event => setMobile(event.target.value)}
        placeholder="https://study.example.com" required />
      <label htmlFor="sync-relay">中继地址</label>
      <input id="sync-relay" type="url" value={relay} onChange={event => setRelay(event.target.value)}
        placeholder="https://relay.example.com" required />
      <button className="action" type="submit" disabled={action.pending || !status}>生成配对链接</button>
    </form>}
    <ErrorNote message={action.error || status?.error || undefined} />
  </section>;
}

export function PhonePairingPanel({ invite, pair, changed, online }: {
  invite?: string; pair?: MobilePair; changed: (pair?: MobilePair) => void; online: boolean;
}) {
  const [name, setName] = useState('我的手机');
  const action = useAction();

  return <section className="study-card pair-panel">
    <h3>电脑配对</h3>
    {invite ? <form onSubmit={event => {
      event.preventDefault();
      void action.run(async () => {
        const version = await phonePairVersion();
        const next = await preparePair(invite, name);
        const subscription = await browserPushSubscription().catch(() => undefined);
        if (!await savePair(next, version)) throw new PairingChanged();
        changed(next);
        if (version.room && version.room !== next.room) await dismissPhoneReminders(version.room);
        await subscription?.unsubscribe().catch(() => {});
      });
    }}>
      <label htmlFor="device-name">此手机的名称</label>
      <input id="device-name" value={name} onChange={event => setName(event.target.value)} maxLength={50} required />
      <button className="action" type="submit" disabled={action.pending || !online}>申请配对</button>
    </form> : pair ? <>
      {pair.invitation ? <>
        <p>在电脑核对同一个配对码，再批准这台手机。</p>
        <p className="pair-code"><output aria-label="手机配对码">{pair.invitation.code}</output></p>
      </> : <p>此浏览器已保存配对。</p>}
      <button className="action secondary" type="button" disabled={action.pending} onClick={() => {
        void action.run(async () => {
          const subscription = await browserPushSubscription().catch(() => undefined);
          if (await savePair(undefined, pair.room)) {
            changed();
            await dismissPhoneReminders(pair.room);
            await subscription?.unsubscribe().catch(() => {});
          }
        });
      }}>移除本机配对</button>
    </> : <p>请在手机上打开电脑生成的配对链接。</p>}
    <ErrorNote message={action.error} />
  </section>;
}
