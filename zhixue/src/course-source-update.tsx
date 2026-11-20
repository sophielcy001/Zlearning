import { Link } from 'react-router-dom';
import type { RevisionSeed } from './course-revision';
import { BackedStatus, ErrorNote, Modal, useRead } from './shared';
import { citationUrl, type Citation, type SourceExcerpt } from './verification';

type Comparison = {
  previous: Citation;
  current: SourceExcerpt | null;
  status: 'changed' | 'retained' | 'moved' | 'unavailable' | 'no_text';
};
type SourceUpdate = {
  version: number | null;
  section: number | null;
  instruction: string;
  sections: { index: number; title: string; text: string; citations: Comparison[]; dependencyTitles: string[] }[];
};

const STATUS: Record<Comparison['status'], string> = {
  changed: '原定位的文字已有变化',
  retained: '仍能在原定位找到引文',
  moved: '引文出现在新的定位',
  unavailable: '原件已移除或暂不可读',
  no_text: '当前暂无可对照的定位文字',
};

export function CourseSourceUpdateDialog({ lesson, version, intact, close, onRevise }: {
  lesson: string; version: number; intact: boolean; close: () => void; onRevise: (seed: RevisionSeed) => void;
}) {
  const { value, error, refresh } = useRead<SourceUpdate>('/lessons/' + lesson + '/source-updates');
  const canRevise = intact && value?.version === version && value.sections.length > 0;
  function revise(source?: SourceExcerpt) {
    if (!value || !canRevise) return;
    onRevise({ section: value.section, instruction: value.instruction, sources: source ? [source] : [] });
  }

  return <Modal title="核对来源变更" onClose={close}>
    <div className="project-dialog-form source-update">
      {!value ? <BackedStatus error={error} retry={refresh} /> : <>
        <ErrorNote message={error} retry={refresh} />
        {value.version !== version ? <p className="notice">当前课程已换版。请重新打开课程查看。</p>
          : !value.sections.length ? <p className="notice" role="status">当前课程的引文依据无需更新。</p> : <>
            <p className="notice" role="status">第 {version} 版有 {value.sections.length} 个小节受引文依据变更影响。</p>
            {!intact && <p className="model-help">本机正文已变更或暂不可用。处理本机文件后可发起修订。</p>}
            <section className="source-update-advice">
              <h3>修订建议</h3>
              <p>{value.instruction}</p>
              <div className="action-row">
                {canRevise && <button type="button" className="button primary" onClick={() => revise()}>按建议修订</button>}
                <button type="button" className="text-action" onClick={refresh}>刷新对照</button>
              </div>
            </section>
            {value.sections.map(section => <section key={section.index} className="source-update-section">
              <h3>{section.title}</h3>
              <details className="section-verification"><summary>查看现有小节正文</summary>
                <p className="evidence-text">{section.text}</p>
              </details>
              {!!section.dependencyTitles.length && <p className="model-help">依赖的结论待核对 · {section.dependencyTitles.join('、')}</p>}
              {!!section.citations.length && <ul className="source-update-list">{section.citations.map(({ previous, current, status }, position) =>
                <li key={position}>
                  <p className="model-help">{STATUS[status]}</p>
                  <div className="source-update-comparison">
                    <section>
                      <h4>保存的原引文</h4>
                      <span>{previous.title} · {previous.label}</span>
                      <blockquote className="evidence-text">{previous.quote}</blockquote>
                    </section>
                    <section>
                      <h4>当前资料</h4>
                      {current ? <>
                        <Link className="text-action" to={citationUrl({ ...current, quote: current.excerpt })}>
                          {current.title} · {current.label}
                        </Link>
                        <blockquote className="evidence-text">{current.excerpt}</blockquote>
                        {canRevise && <button type="button" className="text-action" onClick={() => revise(current)}>
                          用此定位开始修订
                        </button>}
                      </> : <p>可在知识库检查文件位置，或为修订选用其他资料。</p>}
                    </section>
                  </div>
                </li>)}</ul>}
            </section>)}
          </>}
      </>}
    </div>
  </Modal>;
}
