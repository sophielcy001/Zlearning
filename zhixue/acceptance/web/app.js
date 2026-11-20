const byId = (id) => document.getElementById(id);
const timeFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' });
const formatTime = (iso) => iso ? timeFormat.format(new Date(iso)) : '未记录';
const modeLabel = (mode) => mode === 'real' ? '模型接口联调' : '本机模拟接口';
const protocolLabels = {
  'chat-completions': 'Chat Completions',
  'anthropic-messages': 'Anthropic Messages',
  'gemini-generate-content': 'Gemini Generate Content',
};
const labels = { running: '执行中', passed: '通过', failed: '失败', interrupted: '已中断', unverified: '未验证' };

const incoming = new URLSearchParams(location.hash.slice(1)).get('session');
if (incoming) {
  sessionStorage.setItem('zhixue-acceptance-session', incoming);
  history.replaceState(null, '', location.pathname + location.search);
}
const token = sessionStorage.getItem('zhixue-acceptance-session');

let definition = [];
let liveConfig = null;
const profileFields = {};
let runs = [];
let selectedId = null;
let currentRun = null;
let selectionRevision = 0;
let loadingSelection = false;
let stepId = null;
let follow = false;
let starting = false;
let pollTimer = null;
const nodes = new Map();

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    headers: { Authorization: 'Bearer ' + (token ?? ''), 'Content-Type': 'application/json' },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 401) throw new Error('本机会话已失效。请从验收启动入口重新打开。');
  if (response.status === 422 && options.explainValidation) {
    const detail = (await response.json()).detail;
    throw new Error(typeof detail === 'string' ? detail : '请检查连接配置和费用确认。');
  }
  if (response.status === 409) throw new Error('请求暂时无法完成。请查看运行历史或稍后重试。');
  if (!response.ok) throw new Error('无法读取验收服务的结果。');
  return options.download ? response.blob() : response.json();
}

function showError(error) {
  byId('message').textContent = error.message || '无法连接验收服务。';
  byId('message').hidden = false;
}

function child(tag, text = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  return element;
}

function showBadge(element, state, caption = labels[state]) {
  element.dataset.state = state;
  if (element.textContent !== caption) element.textContent = caption;
  element.hidden = false;
}

function makeNode(step) {
  const item = child('li');
  const button = child('button');
  button.type = 'button';
  button.className = 'node';
  button.setAttribute('aria-pressed', 'false');
  const badge = child('span');
  badge.className = 'badge';
  button.append(child('span', step.title), badge);
  button.addEventListener('click', () => selectStep(step.id, true));
  item.append(button);
  nodes.set(step.id, { button, badge, branch: null });
  return item;
}

function renderDefinition() {
  for (const step of definition.filter((entry) => !entry.parent)) {
    const item = makeNode(step);
    const children = definition.filter((entry) => entry.parent === step.id);
    if (children.length) {
      const branch = child('details');
      branch.className = 'branch';
      const list = child('ul');
      list.className = 'branch-list';
      for (const entry of children) {
        list.append(makeNode(entry));
        nodes.get(entry.id).branch = branch;
      }
      branch.append(child('summary', '展开分支（' + children.length + '）'), list);
      item.append(branch);
    }
    byId('flow-list').append(item);
  }
}

function renderHistory() {
  const list = byId('history-list');
  const rebuild = list.dataset.version !== JSON.stringify(runs);
  if (rebuild) {
    const focused = document.activeElement?.dataset.identity;
    list.dataset.version = JSON.stringify(runs);
    list.replaceChildren();
    for (const run of runs) {
      const line = child('li');
      const button = child('button');
      const when = child('time', formatTime(run.startedAt));
      when.dateTime = run.startedAt;
      const badge = child('span');
      badge.className = 'badge';
      showBadge(badge, run.state);
      button.type = 'button';
      button.className = 'history-item';
      button.dataset.identity = run.id;
      const mode = child('span', [modeLabel(run.mode), sampleTitle(run.sample)].filter(Boolean).join(' · '));
      mode.className = 'history-mode';
      button.append(when, mode, badge);
      button.addEventListener('click', () => openRun(run.id).catch(showError));
      line.append(button);
      list.append(line);
      if (focused === run.id) button.focus();
    }
  }
  for (const button of list.querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.identity === selectedId));
  }
  byId('history-empty').hidden = runs.length > 0;
  const unavailable = !token || starting || runs.some((run) => run.state === 'running');
  byId('start-run').disabled = unavailable;
  byId('start-live').disabled = unavailable || !liveConfig;
}

function renderSummary() {
  const run = currentRun;
  byId('run-title').textContent = run ? '本次验收' : '验收流程';
  byId('run-description').textContent = run
    ? [formatTime(run.startedAt), modeLabel(run.mode), sampleTitle(run.sample), run.connectionScope].filter(Boolean).join(' · ')
    : loadingSelection ? '正在读取这次运行的记录。' : '选择流程节点查看要求和当次记录。';
  byId('run-reason').hidden = !run?.reason;
  byId('run-reason').textContent = run?.reason ?? '';
  byId('download').disabled = !run || run.state === 'running';
  byId('download-billing').hidden = !(run?.mode === 'real' && run.state !== 'running' && run.records.some(
    (record) => record.step === 'real-usage' && record.output['调用记录']?.length
  ));
  byId('open-quality').hidden = !qualityRecord(run);
  const status = byId('run-status');
  status.hidden = !run;
  if (run) {
    const passed = run.records.filter((record) => record.status === 'passed').length;
    showBadge(status, run.state, labels[run.state] + ' · ' + passed + ' 节点');
  }
}

function fields(id, values) {
  const region = byId(id);
  region.replaceChildren();
  for (const [key, value] of Object.entries(values ?? {})) {
    const line = child('div');
    const data = child('dd');
    if (Array.isArray(value) && value.every((item) => typeof item !== 'object')) data.textContent = value.join('、');
    else if (value && typeof value === 'object') data.append(child('pre', JSON.stringify(value, null, 2)));
    else data.textContent = value === null ? '无' : typeof value === 'boolean' ? (value ? '是' : '否') : String(value);
    line.append(child('dt', key), data);
    region.append(line);
  }
  if (!region.children.length) {
    const line = child('div');
    line.append(child('dt', '结果'), child('dd', '暂无记录'));
    region.append(line);
  }
}

function renderPlayback(records, index) {
  byId('playback').hidden = !records.length;
  const selector = byId('timepoint');
  selector.replaceChildren(child('option', '选择已记录的节点'));
  selector.firstElementChild.value = '';
  records.forEach((record, position) => {
    const title = definition.find((step) => step.id === record.step)?.title;
    const option = child('option', (position + 1) + '  ' + title + ' · ' + labels[record.status]);
    option.value = record.step;
    selector.append(option);
  });
  selector.value = index < 0 ? '' : records[index].step;
  byId('previous').disabled = index <= 0;
  byId('next').disabled = index >= records.length - 1;
}

function renderDetails() {
  const records = currentRun?.records ?? [];
  const index = records.findIndex((record) => record.step === stepId);
  const record = records[index];
  const step = definition.find((entry) => entry.id === stepId);
  for (const [id, { button, badge }] of nodes) {
    button.setAttribute('aria-pressed', String(id === stepId));
    showBadge(badge, records.find((entry) => entry.step === id)?.status ?? 'unverified');
  }
  byId('step-title').textContent = step?.title ?? '节点详情';
  byId('step-design').textContent = step?.design ?? '';
  if (step) showBadge(byId('step-status'), record?.status ?? 'unverified');
  else byId('step-status').hidden = true;
  byId('evidence').hidden = !record;
  byId('unverified').hidden = Boolean(record);
  byId('unverified').textContent = currentRun ? '此次运行没有执行此节点。' : '运行后可以查看实际证据。';
  if (record) {
    byId('step-start').textContent = formatTime(record.startedAt);
    byId('step-elapsed').textContent = record.elapsedMs === null ? '未记录' : record.elapsedMs + ' ms';
    byId('step-reason').hidden = !record.reason;
    byId('step-reason').textContent = record.reason ?? '';
    fields('step-input', record.input);
    fields('step-output', record.output);
  }
  renderQuality(record);
  renderPlayback(records, index);
}

function selectStep(id, move = false) {
  if (!nodes.has(id)) return;
  follow = false;
  stepId = id;
  const branch = nodes.get(id).branch;
  if (branch) branch.open = true;
  renderDetails();
  if (move && matchMedia('(max-width: 565px)').matches) {
    byId('inspect').focus({ preventScroll: true });
    byId('inspect').scrollIntoView({ block: 'start' });
  }
}

function applyRun(run) {
  currentRun = run;
  if (follow || !stepId) {
    stepId = (follow ? run.records.at(-1) : run.records[0])?.step ?? definition[0]?.id;
    const branch = nodes.get(stepId)?.branch;
    if (branch) branch.open = true;
  }
  renderSummary();
  renderDetails();
}

async function openRun(id, live = false) {
  const revision = ++selectionRevision;
  selectedId = id;
  currentRun = null;
  loadingSelection = true;
  follow = live;
  stepId = null;
  byId('message').hidden = true;
  renderHistory();
  renderSummary();
  renderDetails();
  try {
    const loaded = await api('/api/runs/' + id);
    if (selectionRevision !== revision) return;
    loadingSelection = false;
    applyRun(loaded);
  } catch (error) {
    if (selectionRevision !== revision) return;
    loadingSelection = false;
    renderSummary();
    throw error;
  }
}

function poll() {
  if (pollTimer) clearTimeout(pollTimer);
  if (!runs.some((run) => run.state === 'running') && currentRun?.state !== 'running') return;
  pollTimer = setTimeout(async () => {
    try {
      await loadHistory();
      if (selectedId && !loadingSelection && (currentRun?.state === 'running' || follow)) {
        const revision = selectionRevision;
        try {
          const updated = await api('/api/runs/' + selectedId);
          if (selectionRevision === revision && JSON.stringify(updated) !== JSON.stringify(currentRun)) applyRun(updated);
        } catch (error) {
          if (selectionRevision === revision) throw error;
        }
      }
    } catch (error) {
      showError(error);
    } finally {
      poll();
    }
  }, 550);
}

async function loadHistory() {
  runs = await api('/api/runs');
  renderHistory();
}

async function requestRun(path, body, accepted) {
  byId('message').hidden = true;
  starting = true;
  renderHistory();
  try {
    const started = await api(path, { method: 'POST', body, explainValidation: true });
    accepted?.();
    await loadHistory();
    await openRun(started.id, true);
  } finally {
    starting = false;
    renderHistory();
    poll();
  }
}

byId('start-run').addEventListener('click', () => {
  requestRun('/api/runs', { mode: 'simulated' }).catch((error) => {
    showError(error);
    loadHistory().then(poll).catch(showError);
  });
});

byId('timepoint').addEventListener('change', (event) => selectStep(event.target.value));
for (const [id, direction] of [['previous', -1], ['next', 1]]) {
  byId(id).addEventListener('click', () => {
    const records = currentRun?.records ?? [];
    const index = records.findIndex((record) => record.step === stepId);
    const record = records[index + direction];
    if (record) selectStep(record.step);
  });
}
for (const [button, route, extension] of [['download', 'report', '.md'], ['download-billing', 'billing', '-billing.csv']]) {
  byId(button).addEventListener('click', async () => {
    const id = currentRun?.id;
    if (!id) return;
    try {
      const blob = await api('/api/runs/' + id + '/' + route, { download: true });
      const link = child('a');
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = 'zhixue-acceptance-' + id + extension;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      showError(error);
    }
  });
}

function sampleById(id) {
  return liveConfig?.samples.find((sample) => sample.id === id);
}

function sampleTitle(id) {
  return sampleById(id)?.title;
}

function option(value, label) {
  const row = child('option', label);
  row.value = value;
  return row;
}

function createProfile(role, title, initialProvider) {
  const fieldset = child('fieldset');
  fieldset.className = 'live-profile';
  fieldset.append(child('legend', title));
  const fields = {};
  for (const [name, label, type] of [
    ['provider', '服务商', 'select'],
    ['protocol', '接口协议', 'select'],
    ['endpoint', 'API 地址', 'url'],
    ['model', '模型名称', 'text'],
    ['apiKey', 'API 密钥', 'password'],
  ]) {
    const wrapper = child('label', label);
    wrapper.className = 'live-field';
    const control = child(type === 'select' ? 'select' : 'input');
    control.id = 'live-' + role + '-' + name;
    wrapper.htmlFor = control.id;
    if (type !== 'select') {
      control.type = type;
      control.maxLength = name === 'apiKey' ? 4096 : (name === 'endpoint' ? 2048 : 180);
    }
    control.required = name !== 'apiKey';
    if (name === 'apiKey') control.autocomplete = 'new-password';
    wrapper.append(control);
    fieldset.append(wrapper);
    fields[name] = control;
  }
  fields.provider.append(...liveConfig.providers.map((item) => option(item.id, item.label)));
  fields.provider.value = initialProvider;
  const setProtocols = () => {
    const selected = liveConfig.providers.find((provider) => provider.id === fields.provider.value);
    fields.protocol.replaceChildren(...selected.protocols.map((id) => option(id, protocolLabels[id] ?? id)));
    fields.apiKey.required = selected.id !== 'custom';
  };
  fields.provider.addEventListener('input', setProtocols);
  setProtocols();
  profileFields[role] = { title, fields, setProtocols };
  return fieldset;
}

function updateScope() {
  const sample = sampleById(byId('live-sample').value);
  if (!sample) return;
  fields('live-scope-fields', {
    '目标与成果': sample.topic + '。' + sample.outcome,
    '首课核验资料': sample.source,
    '课内问题': sample.question,
    '复习作答材料': sample.answer,
    '主模型接收': '目标字段、已核验正文、样本问题、答疑整理材料与复习答案。',
    '复核模型接收': '首课草稿、答疑补充草稿及所选公开资料摘要。',
  });
}

function updateConfirmation() {
  const rates = byId('live-budget-rates');
  rates.disabled = !byId('live-limit-amount').value.trim();
  const ratesReady = [...rates.querySelectorAll('input')].every((input) => input.validity.valid);
  byId('confirm-live').disabled = starting || !byId('live-consent').checked || !ratesReady;
  byId('cancel-live').disabled = starting;
}

function setupLive() {
  if (!liveConfig) return;
  byId('live-sample').append(...liveConfig.samples.map((sample) => option(sample.id, sample.title)));
  byId('live-profiles').append(
    createProfile('main', '主模型', liveConfig.defaultProvider),
    createProfile('review', '独立复核模型', 'custom'),
  );
  const limits = liveConfig.limits;
  byId('live-cost').textContent = '每次样本最多 ' + liveConfig.maxCalls + ' 次模型请求。'
    + '课程与复核 ' + limits.course + ' 次、答疑 ' + limits.answer
    + ' 次、答疑整理 ' + limits.consolidation + ' 次、复习 ' + limits.assessment
    + ' 次。服务商可能计费。';
  updateScope();
}

function profileData(role) {
  const { title, fields } = profileFields[role];
  const publicFields = Object.fromEntries(Object.entries(fields)
    .filter(([name]) => name !== 'apiKey').map(([name, control]) => [name, control.value.trim()]));
  return { name: title, ...publicFields, ...(fields.apiKey.value ? { apiKey: fields.apiKey.value } : {}) };
}

function liveBudget() {
  const amount = byId('live-limit-amount').value.trim();
  if (!amount) return undefined;
  const currency = byId('live-limit-currency').value.trim().toUpperCase();
  const pricing = (role) => ({
    currency,
    inputPerMillion: byId('live-' + role + '-input-price').value.trim(),
    outputPerMillion: byId('live-' + role + '-output-price').value.trim(),
  });
  return { limit: { amount, currency }, mainPricing: pricing('main'), reviewPricing: pricing('review') };
}

byId('start-live').addEventListener('click', () => {
  byId('live-error').hidden = true;
  byId('live-consent').checked = false;
  updateConfirmation();
  byId('live-dialog').showModal();
});
byId('cancel-live').addEventListener('click', () => byId('live-dialog').close());
byId('live-dialog').addEventListener('cancel', (event) => {
  if (starting) event.preventDefault();
});
byId('live-dialog').addEventListener('close', () => {
  for (const { fields } of Object.values(profileFields)) fields.apiKey.value = '';
  byId('live-consent').checked = false;
  updateConfirmation();
});
byId('live-form').addEventListener('input', (event) => {
  if (event.target.id === 'live-sample') updateScope();
  if (event.target.id !== 'live-consent') byId('live-consent').checked = false;
  updateConfirmation();
});
byId('live-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (starting || !byId('live-consent').checked) return;
  byId('live-error').hidden = true;
  const pending = requestRun('/api/real-runs', {
    sample: byId('live-sample').value,
    confirmed: true,
    main: profileData('main'),
    review: profileData('review'),
    budget: liveBudget(),
  }, () => byId('live-dialog').close());
  updateConfirmation();
  try {
    await pending;
  } catch (error) {
    if (!byId('live-dialog').open) showError(error);
    else {
      byId('live-error').textContent = error.message;
      byId('live-error').hidden = false;
    }
  } finally {
    updateConfirmation();
  }
});

function qualityRecord(run) {
  if (run?.mode !== 'real' || run.state === 'running') return undefined;
  return run.records.findLast((record) => ['real-publish', 'real-save'].includes(record.step)
    && record.status === 'passed' && record.output['正文']);
}

byId('open-quality').addEventListener('click', () => {
  const record = qualityRecord(currentRun);
  if (record) selectStep(record.step, true);
});

function renderQuality(record) {
  const region = byId('quality');
  const sample = sampleById(currentRun?.sample);
  const publication = qualityRecord(currentRun);
  const show = sample && publication && publication.step === record?.step;
  region.hidden = !show;
  if (!show) return;
  const saved = currentRun.quality;
  const version = publication.output['课程版本'];
  byId('quality-status').textContent = saved
    ? ['已记录', version ? '课程第 ' + version + ' 版' : null, formatTime(saved.checkedAt)].filter(Boolean).join(' · ')
    : '人工抽查未完成';
  byId('quality-source').href = sample.source_url;
  byId('quality-checks').replaceChildren(...Object.entries(liveConfig.checkLabels).map(([key, title]) => {
    const item = child('div');
    item.className = 'quality-item';
    const control = child('select');
    control.id = 'check-' + key;
    control.dataset.check = key;
    control.required = true;
    control.append(option('', '选择结论'), ...Object.entries(liveConfig.verdicts).map(([id, label]) => option(id, label)));
    control.value = saved?.checks[key] ?? '';
    const label = child('label', title);
    label.htmlFor = control.id;
    const criteria = child('p', sample.criteria[key]);
    criteria.className = 'quiet';
    item.append(label, criteria, control);
    return item;
  }));
}

byId('quality-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const id = currentRun?.id;
  if (!id) return;
  const button = byId('save-quality');
  button.disabled = true;
  try {
    const checks = Object.fromEntries([...byId('quality-checks').querySelectorAll('select')]
      .map((control) => [control.dataset.check, control.value]));
    const result = await api('/api/runs/' + id + '/quality', { method: 'PUT', body: { checks } });
    if (currentRun?.id === id) {
      currentRun.quality = result;
      renderDetails();
    }
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
  }
});

async function initialize() {
  if (!token) throw new Error('请从验收启动入口打开本机网页。');
  const workflow = await api('/api/definition');
  definition = workflow.steps;
  liveConfig = workflow.live;
  setupLive();
  stepId = definition[0]?.id;
  renderDefinition();
  renderDetails();
  await loadHistory();
  if (runs.length) await openRun(runs[0].id, runs[0].state === 'running');
  poll();
}

initialize().catch(showError);
