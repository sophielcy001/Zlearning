import { useEffect, useId, useState } from 'react';
import { ChevronLeft, ChevronRight, ReceiptText } from 'lucide-react';
import type { Money } from './money-limit';
import { BackedStatus, Empty, ErrorNote, useRead } from './shared';

export type Pricing = { currency: string; inputPerMillion: string; outputPerMillion: string };
type ReportedUsage = {
  inputTokens: number | null; outputTokens: number | null; totalTokens: number | null;
  thinkingTokens: number | null; audioSeconds: number | null;
};
export type CostSummary = ReportedUsage & {
  calls: number;
  inFlight: number;
  unknownCalls: number;
  reportedCalls: number;
  currencies: { currency: string; actual: string | null; estimated: string | null }[];
};
type CallRecord = ReportedUsage & {
  id: string;
  kind: string;
  phase: string | null;
  status: string;
  startedAt: string;
  finishedAt: string | null;
  recipient: { name: string; model: string } | null;
  pricing: Pricing | null;
  actualCost: Money | null;
  estimatedCost: Money | null;
  reservedCost: Money | null;
};
type UsagePage = { billingDay: string; page: number; lastPage: number; total: number; summary: CostSummary; items: CallRecord[] };

const CALL_KINDS: Record<string, string> = {
  generation: '课程生成', assessment: '复习与错因补学', answer: '课内答疑', chat: '自由聊天',
  consolidation: '学完后的答疑整理', prerequisite: '前置知识讲解', stage_review: '阶段成果评审',
  note_ai: '笔记助手', note_check: '笔记核对', graph: '图谱推荐', classification: '知识库整理',
  goal_authoring: '学习目标整理', probe: '连接测试',
  voice_transcription: '外部语音转写', page_ocr: '外部单页识别',
};
const PHASES: Record<string, string> = { route: '路线', draft: '草稿', revision_draft: '课程修订起草', review: '独立核验' };
const time = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});

function UsageCounts({ usage }: { usage: ReportedUsage }) {
  return <div className="usage-token-counts">
    {usage.inputTokens !== null && <span>输入 {usage.inputTokens} Token</span>}
    {usage.outputTokens !== null && <span>输出 {usage.outputTokens} Token</span>}
    {usage.thinkingTokens !== null && <span>思考 {usage.thinkingTokens} Token</span>}
    {usage.totalTokens !== null && <span>服务商报告总计 {usage.totalTokens} Token</span>}
    {usage.audioSeconds != null && <span>服务商报告录音 {usage.audioSeconds.toLocaleString('zh-CN', { maximumFractionDigits: 3 })} 秒</span>}
  </div>;
}

export function CostTotals({ costs }: { costs: CostSummary }) {
  return <div className="usage-costs">
    {costs.currencies.map(({ currency, actual, estimated }) => <div key={currency}>
      {actual !== null && <span>实计 {actual} {currency}</span>}
      {estimated !== null && <span>估算 {estimated} {currency}</span>}
    </div>)}
    {costs.unknownCalls > 0 && <span>{costs.unknownCalls} 次费用未知</span>}
    {costs.inFlight > 0 && <span>{costs.inFlight} 次等待回报</span>}
    {costs.calls === 0 && <span>暂无费用记录</span>}
  </div>;
}

function UsageRecord({ item }: { item: CallRecord }) {
  const scope = CALL_KINDS[item.kind] ?? '模型调用';
  const phase = item.phase ? PHASES[item.phase] : undefined;
  const money = item.actualCost ?? item.estimatedCost;
  const title = item.recipient?.name ?? '连接未知';
  return <li className="usage-record">
    <div className="usage-record-head">
      <div className="usage-record-title">
        <strong>{title}</strong>
        {item.recipient?.model && <span>{item.recipient.model}</span>}
      </div>
      <div className="usage-record-cost" data-cost={item.actualCost ? 'actual' : item.estimatedCost ? 'estimated' : 'unknown'}>
        {money ? `${item.actualCost ? '实计' : '估算'} ${money.amount} ${money.currency}`
          : item.finishedAt ? '费用未知' : '等待回报'}
      </div>
    </div>
    <div className="usage-context">
      <span>{scope}{phase && ` · ${phase}`}</span>
      <time dateTime={item.startedAt}>{time.format(new Date(item.startedAt))}</time>
      {item.status === 'uncertain' && <span>结果待核对</span>}
    </div>
    <UsageCounts usage={item} />
    {item.reservedCost && <p className="model-help">
      额度预留 {item.reservedCost.amount} {item.reservedCost.currency}
    </p>}
    {item.estimatedCost && item.pricing && <p className="model-help">
      每百万 Token 单价为输入 {item.pricing.inputPerMillion}、输出 {item.pricing.outputPerMillion} {item.pricing.currency}
    </p>}
  </li>;
}

export function ModelUsageLedger() {
  const heading = useId();
  const [day, setDay] = useState<string>();
  const [page, setPage] = useState(1);
  const query = new URLSearchParams({ page: String(page), ...(day ? { billingDay: day } : {}) });
  const { value, error, refresh } = useRead<UsagePage>(`/generation/usage?${query}`);
  const waiting = Boolean(value?.summary.inFlight);
  useEffect(() => {
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [refresh]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh();
    }, waiting ? 2000 : 5000);
    return () => window.clearInterval(timer);
  }, [waiting, refresh]);

  return <section className="panel settings model-ledger" aria-labelledby={heading}>
    <div className="usage-heading">
      <h2 id={heading}><ReceiptText size={20} aria-hidden="true" />模型用量账本</h2>
      <label className="project-field">计费日期
        <input type="date" value={day ?? value?.billingDay ?? ''} onChange={event => {
          setDay(event.target.value || undefined);
          setPage(1);
        }} />
      </label>
    </div>
    <p className="model-help">按北京时间记录。估算采用发送时保存的固定单价，同一币种的实计与估算金额分别汇总。</p>
    {!value ? <BackedStatus error={error} retry={refresh} /> : <>
      <ErrorNote message={error} retry={refresh} />
      {value.total ? <>
        <div className="usage-overview">
          <strong>共 {value.total} 次调用</strong>
          <CostTotals costs={value.summary} />
          {value.summary.reportedCalls > 0 && <span className="model-help">{value.summary.reportedCalls} 次收到用量回报</span>}
          <UsageCounts usage={value.summary} />
        </div>
        <ul className="usage-records" aria-label="模型调用记录">
          {value.items.map(item => <UsageRecord key={`${item.kind}/${item.id}`} item={item} />)}
        </ul>
        {value.lastPage > 1 && <div className="pager">
          <span>第 {value.page} / {value.lastPage} 页</span>
          <button type="button" className="icon-button" aria-label="账本上一页" disabled={value.page === 1}
            onClick={() => setPage(value.page - 1)}><ChevronLeft size={17} /></button>
          <button type="button" className="icon-button" aria-label="账本下一页" disabled={value.page === value.lastPage}
            onClick={() => setPage(value.page + 1)}><ChevronRight size={17} /></button>
        </div>}
      </> : <Empty title="该日期没有模型调用记录" />}
    </>}
  </section>;
}
