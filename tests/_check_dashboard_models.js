// 用最小 DOM 桩跑一遍「网关模型清单」的渲染与测试请求体：
// 验证新增的「启用」与「测试」两列、停用行的灰显与徽章、测试结果的就地渲染，
// 以及两种测试口径发出去的请求形状。不靠浏览器。
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('dashboard.html', 'utf8');
const scripts = [];
const re = /<script\b[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = re.exec(html)) !== null) scripts.push(m[1]);

// --- 最小 DOM 桩 -------------------------------------------------------
const els = {};
function makeEl(id) {
  return {
    id: id || '',
    value: '',
    textContent: '',
    innerHTML: '',
    style: {},
    checked: false,
    disabled: false,
    children: [],
    tagName: 'DIV',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {},
    appendChild() {},
    removeChild() {},
    scrollIntoView() {},
    focus() {},
    blur() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    getAttribute() { return null; },
    setAttribute() {},
    removeAttribute() {},
    dataset: {},
  };
}
const MODELS_TBODY = makeEl('modelsTbody');
const document = {
  getElementById(id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  },
  querySelector(sel) {
    if (sel === '#modelsTable tbody') return MODELS_TBODY;
    return null;
  },
  querySelectorAll() { return []; },
  createElement() { return makeEl(''); },
  body: makeEl('body'),
  head: makeEl('head'),
  documentElement: makeEl('html'),
  addEventListener() {},
};
// --- localStorage 桩：真存东西，「刷新页面」前后共用同一份 ------------
const STORE = new Map();
const localStorage = {
  getItem(k) { return STORE.has(k) ? STORE.get(k) : null; },
  setItem(k, v) { STORE.set(k, String(v)); },
  removeItem(k) { STORE.delete(k); },
};

const window = {
  document,
  localStorage,
  location: { search: '' },
  addEventListener() {},
  matchMedia() { return { matches: false, addEventListener() {} }; },
  setTimeout, clearTimeout, setInterval, clearInterval,
  VIEW_REALM: 'intl',
};
window.window = window;
window.globalThis = window;

const main = scripts.find(s => s.indexOf('function renderAvailableModels') >= 0);
if (!main) { console.log('could not find the models script block'); process.exit(1); }
try {
  new vm.Script(main, {filename: 'main.js'});
} catch (e) {
  console.log('SYNTAX ERROR: ' + e.message);
  process.exit(1);
}

const want = [
  'renderAvailableModels', 'modelTestKey', 'modelTestPayload',
  'readModelTestStream', 'testGatewayModel', 'toggleModelEnabled',
  'setModelTestMode', 'buildEggPoolFilter', 'eggFilteredModels',
  'modelTestDetail', 'modelAvailabilityBadge', 'runModelTest',
  'testAllGatewayModels', 'modelTestStamp', 'stopModelBatchTest',
  'fmtElapsed', 'modelTestStateOf',
  'setModelTestState', 'clearModelTestState', 'saveModelTestState',
  'loadModelTestState',
  'renderModelTestDetail', 'showModelTestDetail', 'closeModelTestDetail',
  'copyModelTestError', 'retestFromModelDetail',
];
const setters = [
  'MODELS_DATA', 'EGG_POOL_FILTER', 'currentProvider', 'esc', 'toast',
  'postJSON', 'getJSON', 'MODEL_TEST_MODE',
].map(n => "  try{ if(o." + n + " !== undefined) " + n + " = o." + n + "; }catch(e){}");
const exportSrc = want.map(n => n + ': ' + n).join(',');
const EXPORTS =
  '(function(){' + main + '\n;return {' + exportSrc + ', __set: function(o){' +
  setters.join('\n') +
  '}, __testState: function(o){ Object.keys(MODEL_TEST_STATE).forEach(function(k){ delete MODEL_TEST_STATE[k]; });'
  + ' Object.keys(o || {}).forEach(function(k){ MODEL_TEST_STATE[k] = o[k]; }); }'
  + ', __state: function(){ var o = {}; Object.keys(MODEL_TEST_STATE).forEach(function(k){ o[k] = MODEL_TEST_STATE[k]; }); return o; }'
  + '}})()';

// 开一个「页面」。再开一个就是刷新：脚本从头跑一遍，localStorage 还在。
function bootPage(fetchImpl) {
  const win = {
    document, console, localStorage,
    location: { search: '' },
    addEventListener() {},
    matchMedia() { return { matches: false, addEventListener() {} }; },
    setTimeout, clearTimeout, setInterval, clearInterval,
    VIEW_REALM: 'intl',
    fetch: fetchImpl || (() => Promise.reject(new Error('no network in this test'))),
    navigator: { userAgent: 'node' },
    URLSearchParams, TextDecoder, TextEncoder,
    // vm 的沙箱里没有 AbortController：runModelTest 少了它就不会超时中断，
    // 那个用例会永远挂着。
    AbortController,
  };
  win.window = win;
  win.globalThis = win;
  return vm.runInContext(EXPORTS, vm.createContext(win));
}
const api = bootPage();
if (!api) { console.log('could not extract functions'); process.exit(1); }

let FAIL = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok   ' + name);
  else { console.log('  FAIL ' + name + ' ' + (detail === undefined ? '' : JSON.stringify(detail))); FAIL++; }
}

// --- 渲染：一条正常 + 一条停用 ---------------------------------------
function sampleModels() {
  return [
    {
      id: 'hy4-preview', name: 'HY4 Preview', context_window: 128000,
      max_output_tokens: 8192, reasoning_efforts: ['low'], available: true,
    },
    {
      id: 'deepseek-v4.1-flash', name: 'DeepSeek V4.1 Flash', context_window: 200000,
      max_output_tokens: 64000, available: true, disabled: true,
    },
  ];
}

api.__set({
  currentProvider: function () { return 'workbuddy'; },
  esc: function (s) { return String(s == null ? '' : s); },
  toast: function () {},
  postJSON: function () { return Promise.resolve({ok: true}); },
  getJSON: function () { return Promise.resolve({}); },
  MODELS_DATA: sampleModels(),
  EGG_POOL_FILTER: '',
});
api.__testState({});
api.renderAvailableModels();

const htmlOut = MODELS_TBODY.innerHTML;
check('每行 12 列', (htmlOut.match(/<td\b/g) || []).length === 24,
      (htmlOut.match(/<td\b/g) || []).length);
check('有启用列', htmlOut.indexOf('data-label="启用"') >= 0);
check('有测试列', htmlOut.indexOf('data-label="测试"') >= 0);
check('有可用性列', htmlOut.indexOf('data-label="可用性"') >= 0);
check('启用列是胶囊开关',
      /data-label="启用"><label class="switch"[^>]*><input type="checkbox"/.test(htmlOut));
check('正常行开关打开',
      /data-label="启用"><label class="switch"[^>]*><input type="checkbox" checked/.test(htmlOut));
check('停用行开关关闭',
      /data-label="启用"><label class="switch"[^>]*><input type="checkbox"(?! checked)/.test(htmlOut));
check('开关带胶囊轨道',
      (htmlOut.match(/<span class="switch-track"><\/span>/g) || []).length === 2,
      (htmlOut.match(/switch-track/g) || []).length);
check('停用行有徽章', htmlOut.indexOf('已停用</span>') >= 0);
check('停用行灰显', htmlOut.indexOf('style="opacity:.55"') >= 0);
check('计数提到已停用', (els.availModelCount.innerHTML || els.availModelCount.textContent || '')
      .indexOf('1 个已停用') >= 0, els.availModelCount.innerHTML);
check('测试按钮接上函数',
      htmlOut.indexOf('onclick="testGatewayModel(&quot;hy4-preview&quot;, this)"') >= 0);
check('启停开关接上函数',
      htmlOut.indexOf('onchange="toggleModelEnabled(&quot;deepseek-v4.1-flash&quot;, this.checked, this)"') >= 0);
check('停用行的测试按钮禁用',
      /data-label="测试"[^>]*><button class="sec mini" disabled/.test(htmlOut)
      || /class="test-cell"[^>]*data-label="测试"><button class="sec mini" disabled/.test(htmlOut));

// --- 测试结果就地渲染 -------------------------------------------------
api.__testState({
  'workbuddy|intl|hy4-preview': {status: 'ok', ms: 1234, ttft: 400, tool: true, text: '正常'},
  'workbuddy|intl|deepseek-v4.1-flash': {status: 'bad', ms: 800, error: 'HTTP 400：模型不存在'},
});
api.renderAvailableModels();
const html2 = MODELS_TBODY.innerHTML;
check('可用结果显示在行内', html2.indexOf('✓ 可用 1.2s') >= 0);
check('不可用结果显示在行内', html2.indexOf('✗ 不可用') >= 0);
check('不可用原因进 title', html2.indexOf('HTTP 400：模型不存在') >= 0);

// --- 可用性徽标：绿=可用 / 红=不可用 / 琥珀=测试中 / 灰=未测试 -------
const badge = api.modelAvailabilityBadge;
check('未测过是灰色徽标', badge(null, false).indexOf('ava-idle') >= 0
      && badge(null, false).indexOf('未测试') >= 0);
check('可用是绿色徽标', badge({status: 'ok', ms: 1200}, false).indexOf('ava-ok') >= 0
      && badge({status: 'ok', ms: 1200}, false).indexOf('可用') >= 0);
check('不可用是红色徽标', badge({status: 'bad', error: 'boom'}, false).indexOf('ava-bad') >= 0
      && badge({status: 'bad', error: 'boom'}, false).indexOf('不可用') >= 0);
check('测试中是琥珀色徽标', badge({status: 'running'}, false).indexOf('ava-run') >= 0
      && badge({status: 'running'}, false).indexOf('测试中') >= 0);
check('徽标带状态圆点', badge({status: 'ok', ms: 1}, false).indexOf('ava-dot') >= 0);
check('停用的模型给的是未测试而不是不可用',
      badge(null, true).indexOf('ava-idle') >= 0 && badge(null, true).indexOf('ava-bad') < 0);

// --- 徽标可点：点开是这条结论的详情弹框 ------------------------------
const clickableBadge = badge({status: 'bad', error: 'boom'}, false, 'hy4-preview');
check('表格里的徽标带模型 id（可点开详情）',
      clickableBadge.indexOf('data-model-detail="hy4-preview"') >= 0, clickableBadge);
check('表格里的徽标是可点样式', clickableBadge.indexOf('ava-btn') >= 0);
check('表格里的徽标提示可以点', clickableBadge.indexOf('点击查看详情') >= 0);
check('徽标仍然保留红色状态',
      clickableBadge.indexOf('ava ava-bad') >= 0 && clickableBadge.indexOf('不可用') >= 0);
check('弹框里的状态徽标不可点（不传 id）',
      badge({status: 'bad', error: 'boom'}, false).indexOf('data-model-detail') < 0);

api.__testState({
  'workbuddy|intl|hy4-preview': {status: 'ok', ms: 1234, ttft: 400, tool: true, text: '正常'},
  'workbuddy|intl|deepseek-v4.1-flash': {status: 'bad', ms: 800, error: 'HTTP 400：模型不存在'},
});
api.renderAvailableModels();
const html3 = MODELS_TBODY.innerHTML;
check('可用性列渲染出绿徽标', html3.indexOf('ava ava-ok') >= 0);
check('可用性列渲染出红徽标', html3.indexOf('ava ava-bad') >= 0);
check('可用性列在启用列之后、测试列之前',
      html3.indexOf('data-label="启用"') < html3.indexOf('data-label="可用性"')
      && html3.indexOf('data-label="可用性"') < html3.indexOf('data-label="测试"'));

// 表头的计数里带可用 / 不可用各几条，一眼看出这个视图整体能不能用。
const countHtml = els.availModelCount.innerHTML || '';
check('表头计数带可用 / 不可用条数',
      countHtml.indexOf('可用 1') >= 0 && countHtml.indexOf('不可用 1') >= 0, countHtml);
check('表头计数里可用是绿的、不可用是红的',
      countHtml.indexOf('color:var(--accent2)') >= 0
      && countHtml.indexOf('color:var(--bad)') >= 0, countHtml);

// --- 可用性详情弹框：点徽标看「为什么不可用」 -------------------------
api.__set({MODELS_DATA: sampleModels()});
api.__testState({
  'workbuddy|intl|hy4-preview': {
    status: 'bad', at: Date.now(), ms: 800, http: 400,
    error: 'HTTP 400：模型不存在', mode: 'agent', provider: 'workbuddy', realm: 'intl',
  },
});
api.renderModelTestDetail('hy4-preview');
const detailHtml = els.modelTestDetailBody.innerHTML || '';
check('详情弹框把不可用原因写全', detailHtml.indexOf('HTTP 400：模型不存在') >= 0, detailHtml);
check('详情弹框把原因放在「为什么不可用」里', detailHtml.indexOf('为什么不可用') >= 0);
check('详情弹框给复制原因的入口', detailHtml.indexOf('copyModelTestError') >= 0);
check('详情弹框写明测试口径', detailHtml.indexOf('Agent（带工具）') >= 0);
check('详情弹框写明测的是哪家 / 哪个出口',
      detailHtml.indexOf('WorkBuddy') >= 0 && detailHtml.indexOf('国际版') >= 0);
check('详情弹框带上测试时间', detailHtml.indexOf('测试时间') >= 0);
check('详情弹框标题带模型 id',
      String(els.modelTestDetailTitle.textContent || '').indexOf('hy4-preview') >= 0,
      els.modelTestDetailTitle.textContent);
check('详情弹框的「重新测试」可用', els.modelTestDetailRetest.disabled !== true);

// 停用的模型不能重测（网关会直接拒绝），弹框里也要说清为什么没有结论。
api.renderModelTestDetail('deepseek-v4.1-flash');
check('停用模型的详情里重测按钮禁用', els.modelTestDetailRetest.disabled === true);

// 没测过：告诉用户怎么测，而不是空白一片。
api.__testState({});
api.renderModelTestDetail('hy4-preview');
check('没测过的详情说明怎么发起测试',
      (els.modelTestDetailBody.innerHTML || '').indexOf('还没测过') >= 0,
      els.modelTestDetailBody.innerHTML);

// 成功结论：把模型原话摘出来，并写清工具 / 推理情况。
api.__testState({
  'workbuddy|intl|hy4-preview': {
    status: 'ok', at: Date.now(), ms: 1200, ttft: 300, tool: true, reasoning: false,
    text: '正常', mode: 'agent', provider: 'workbuddy', realm: 'intl',
  },
});
api.renderModelTestDetail('hy4-preview');
const okDetail = els.modelTestDetailBody.innerHTML || '';
check('可用的详情摘出模型回复', okDetail.indexOf('正常') >= 0 && okDetail.indexOf('回复摘录') >= 0);
check('可用的详情写明工具调用情况', okDetail.indexOf('已调用工具') >= 0);

// --- 空态 -------------------------------------------------------------
api.__set({MODELS_DATA: []});
api.renderAvailableModels();
check('空态跨满 12 列', MODELS_TBODY.innerHTML.indexOf('colspan="12"') >= 0,
      MODELS_TBODY.innerHTML);

// --- 两种测试口径的请求体 ---------------------------------------------
api.__set({MODEL_TEST_MODE: 'agent'});
const agentBody = api.modelTestPayload('hy4-preview');
check('Agent 口径带工具', Array.isArray(agentBody.tools) && agentBody.tools.length === 1);
check('Agent 口径工具名是 read_file', agentBody.tools[0].function.name === 'read_file');
check('Agent 口径 tool_choice=auto', agentBody.tool_choice === 'auto');
check('Agent 口径是流式', agentBody.stream === true);
check('Agent 口径模型名正确', agentBody.model === 'hy4-preview');

api.__set({MODEL_TEST_MODE: 'chat'});
const chatBody = api.modelTestPayload('hy4-preview');
check('对话口径不带工具', !chatBody.tools);
check('对话口径只有一个 user 消息', chatBody.messages.length === 1
      && chatBody.messages[0].role === 'user');
check('切换口径写回状态', (api.setModelTestMode('agent'), api.modelTestKey('x') === 'workbuddy|intl|x'));

// --- SSE 解析 ----------------------------------------------------------
function sseResponse(chunks) {
  const encoder = new TextEncoder();
  const queue = chunks.map(c => encoder.encode(c));
  return {
    body: {
      getReader() {
        return {
          read() {
            if (!queue.length) return Promise.resolve({done: true});
            return Promise.resolve({done: false, value: queue.shift()});
          },
        };
      },
    },
  };
}

// 一个「答得好好的」回包：并发 / 超时 / 叫停那几个用例复用。
function okResponse() {
  const encoder = new TextEncoder();
  const chunks = [encoder.encode('data: {"choices":[{"delta":{"content":"正常"}}]}\n\n')];
  return {
    ok: true, status: 200,
    body: {
      getReader() {
        return {
          read() {
            return chunks.length
              ? Promise.resolve({done: false, value: chunks.shift()})
              : Promise.resolve({done: true});
          },
        };
      },
    },
  };
}

(async function () {
  // 看门狗：有个 await 挂住时，node 会因为事件循环空了直接退出（还不算失败，
  // 看着像跑完了）。这里给它一个上限，超时就算失败。
  const watchdog = setTimeout(() => {
    console.log('  FAIL 用例卡住了（30 秒没跑完）');
    console.log('FAILED: timeout');
    process.exit(1);
  }, 30000);
  const finish = () => {
    clearTimeout(watchdog);
    console.log('');
    if (FAIL) { console.log('FAILED: ' + FAIL); process.exit(1); }
    console.log('all checks passed');
    // 页面里有个每分钟的定时器（结论过期看门狗），在 vm 里它照样活着，
    // 不主动退出的话 node 会一直等着事件循环空下来。
    process.exit(0);
  };
  const okStream = await api.readModelTestStream(sseResponse([
    'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
    'data: {"choices":[{"delta":{"reasoning_content":"想一想"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"正常"}}]}\n\n',
    'data: [DONE]\n\n',
  ]));
  check('SSE：读到正文', okStream.text === '正常', okStream.text);
  check('SSE：读到推理', okStream.reasoning === true);
  check('SSE：没有报错', okStream.error === '');

  const toolStream = await api.readModelTestStream(sseResponse([
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1"}]}}]}\n\n',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]}}]}\n\n',
  ]));
  check('SSE：识别工具调用', toolStream.tool === true);

  const errStream = await api.readModelTestStream(sseResponse([
    'data: {"error":{"message":"上游 403"}}\n\n',
  ]));
  check('SSE：带内错误被抓到', errStream.error === '上游 403', errStream.error);

  const jsonStream = await api.readModelTestStream(sseResponse([
    '{"choices":[{"message":{"role":"assistant","content":"正常"}}]}',
  ]));
  check('忽略 stream 的 JSON 回包也能读', jsonStream.text === '正常', jsonStream.text);

  // --- 一键测试模型：挨个测、跳过停用的、逐条更新状态 ----------------
  const seen = [];
  const apiBatch = bootPage(function (url, opts) {
    seen.push(JSON.parse(opts.body).model);
    if (seen.length === 2) {
      return Promise.resolve({ok: false, status: 500, text: () => Promise.resolve('boom')});
    }
    return Promise.resolve({
      ok: true, status: 200,
      body: {getReader() {
        const enc = new TextEncoder();
        const chunks = [enc.encode('data: {"choices":[{"delta":{"content":"正常"}}]}\n\n')];
        return {read() {
          return chunks.length
            ? Promise.resolve({done: false, value: chunks.shift()})
            : Promise.resolve({done: true});
        }};
      }},
    });
  });
  apiBatch.__set({
    currentProvider: function () { return 'workbuddy'; },
    esc: function (s) { return String(s == null ? '' : s); },
    toast: function () {},
    postJSON: function () { return Promise.resolve({ok: true}); },
    getJSON: function () { return Promise.resolve({}); },
    EGG_POOL_FILTER: '',
  });
  apiBatch.__set({MODELS_DATA: [
    {id: 'm-ok', name: 'OK', available: true},
    {id: 'm-bad', name: 'Bad', available: true},
    {id: 'm-off', name: 'Off', available: true, disabled: true},
  ]});
  apiBatch.__testState({});
  const btn = {textContent: '一键测试模型', disabled: false};
  await apiBatch.testAllGatewayModels(btn);
  check('一键测试跑完了所有启用的模型',
        seen.length === 2 && seen.indexOf('m-ok') >= 0 && seen.indexOf('m-bad') >= 0, seen);
  check('一键测试跳过了停用的模型', seen.indexOf('m-off') < 0, seen);
  const after = MODELS_TBODY.innerHTML;
  check('测完把可用性刷成绿', after.indexOf('ava ava-ok') >= 0, after);
  check('测完把可用性刷成红', after.indexOf('ava ava-bad') >= 0, after);
  check('一键测试结束后按钮复原',
        btn.textContent === '一键测试模型' && btn.disabled === false, btn.textContent);

  // --- 批量测试要够快：并发跑、超时短、能叫停 -------------------------
  // 串行跑三十多条要好几分钟，用户看着就是「一直不动」：得并发。
  const events = [];
  const btnConc = {textContent: '一键测试模型', disabled: false};
  let midText = '';
  const apiConc = bootPage(function (url, opts) {
    const id = JSON.parse(opts.body).model;
    events.push('start:' + id);
    midText = btnConc.textContent;
    return new Promise(resolve => setTimeout(() => {
      events.push('end:' + id);
      resolve(okResponse());
    }, 30));
  });
  apiConc.__set({
    currentProvider: function () { return 'workbuddy'; },
    esc: function (s) { return String(s == null ? '' : s); },
    toast: function () {},
    EGG_POOL_FILTER: '',
    MODELS_DATA: [1, 2, 3, 4, 5].map(n => ({id: 'c' + n, name: 'C' + n, available: true})),
  });
  apiConc.__testState({});
  await apiConc.testAllGatewayModels(btnConc);
  const firstEnd = events.findIndex(e => e.indexOf('end:') === 0);
  check('批量测试是并发跑的（不止一条同时在飞）', firstEnd >= 2, events);
  check('并发不超过 4 条', firstEnd <= 4, events);
  check('按钮上带进度和已用时间',
        midText.indexOf('测试中') >= 0 && midText.indexOf('/5') >= 0, midText);
  check('elapsed 走 mm:ss', apiConc.fmtElapsed(0) === '0:00'
        && apiConc.fmtElapsed(65000) === '1:05', apiConc.fmtElapsed(65000));

  // 批量里的单条超时更短（45s），单条测试仍然给 120s。
  const apiTimeout = bootPage(function (url, opts) {
    return new Promise((resolve, reject) => {
      const sig = opts.signal;
      if (sig) sig.addEventListener('abort', () => {
        const e = new Error('aborted'); e.name = 'AbortError'; reject(e);
      });
    });
  });
  apiTimeout.__set({
    currentProvider: function () { return 'workbuddy'; },
    esc: function (s) { return String(s == null ? '' : s); },
    toast: function () {},
  });
  const timedOut = await apiTimeout.runModelTest('slow-model', 1200);
  check('超时的模型判为不可用', timedOut.status === 'bad', timedOut);
  check('超时的原因写明等了多久',
        String(timedOut.error || '').indexOf('1 秒内没有完成回答') >= 0, timedOut.error);
  check('失败结论记下测试口径和提供商',
        timedOut.mode === 'agent' && timedOut.provider === 'workbuddy', timedOut);

  // HTTP 失败：错误正文要带状态码和上游原话，详情弹框还要单独拿到状态码。
  const apiHttp = bootPage(function () {
    return Promise.resolve({
      ok: false, status: 400,
      text: () => Promise.resolve('{"error":{"message":"模型不存在"}}'),
    });
  });
  apiHttp.__set({
    currentProvider: function () { return 'workbuddy'; },
    esc: function (s) { return String(s == null ? '' : s); },
    toast: function () {},
  });
  const httpBad = await apiHttp.runModelTest('bad-model', 5000);
  check('失败结论记下 HTTP 状态码', httpBad.http === 400, httpBad);
  check('失败原因带状态码和上游原文',
        String(httpBad.error || '').indexOf('HTTP 400') >= 0
        && String(httpBad.error || '').indexOf('模型不存在') >= 0, httpBad.error);

  // 叫停：已经发出去的那几条跑完就收手，没开始的就不再测。
  let started = 0;
  const apiStop = bootPage(function (url, opts) {
    started++;
    return new Promise(resolve => setTimeout(() => resolve(okResponse()), 60));
  });
  apiStop.__set({
    currentProvider: function () { return 'workbuddy'; },
    esc: function (s) { return String(s == null ? '' : s); },
    toast: function () {},
    EGG_POOL_FILTER: '',
    MODELS_DATA: [1, 2, 3, 4, 5, 6].map(n => ({id: 's' + n, name: 'S' + n, available: true})),
  });
  apiStop.__testState({});
  const btnStop = {textContent: '一键测试模型', disabled: false};
  const running = apiStop.testAllGatewayModels(btnStop);
  await new Promise(r => setTimeout(r, 10));
  apiStop.stopModelBatchTest();
  await running;
  check('叫停后不再测剩下的模型', started < 6, started);
  check('叫停后按钮复原',
        btnStop.textContent === '一键测试模型' && btnStop.disabled === false, btnStop.textContent);

  // --- 结论落盘：刷新页面后还在 ---------------------------------------
  STORE.clear();
  api.__testState({});
  api.setModelTestState('workbuddy|intl|hy4-preview', {status: 'ok', at: Date.now(), ms: 1234});
  const raw = localStorage.getItem('wb-model-test-state');
  check('测试结论写进了 localStorage',
        !!raw && raw.indexOf('workbuddy|intl|hy4-preview') >= 0, raw);

  // 「测试中」是进行中的临时状态，刷新回来它早就不成立了，不该落盘。
  api.setModelTestState('workbuddy|intl|half-done', {status: 'running', at: Date.now()});
  check('进行中的状态不落盘',
        localStorage.getItem('wb-model-test-state').indexOf('half-done') < 0);

  // 可用性会变：超过有效期（24h）的旧结论不再当「当前状态」。
  api.setModelTestState('workbuddy|intl|stale-one',
                        {status: 'ok', at: Date.now() - 48 * 3600 * 1000, ms: 10});
  check('过期的结论被丢掉',
        localStorage.getItem('wb-model-test-state').indexOf('stale-one') < 0);

  // 模拟刷新：再开一个页面（脚本从头跑一遍，localStorage 还在）。
  const api2 = bootPage();
  const restored = api2.__state();
  check('刷新后读回了上次的结论',
        !!(restored['workbuddy|intl|hy4-preview']
          && restored['workbuddy|intl|hy4-preview'].status === 'ok'),
        Object.keys(restored));
  check('刷新后过期的结论没有回来', !restored['workbuddy|intl|stale-one']);
  check('刷新后进行中的状态没有回来', !restored['workbuddy|intl|half-done']);

  // 停用模型时清掉结论，localStorage 里也要跟着没。
  api.clearModelTestState('workbuddy|intl|hy4-preview');
  check('清掉的结论也从 localStorage 消失',
        (localStorage.getItem('wb-model-test-state') || '').indexOf('hy4-preview') < 0);

  // 结论带时间戳：面板刷新后读回来的还是上次那次的，悬停要看得出它有多旧。
  check('可用结论的 tooltip 带上测试时间',
        api.modelTestDetail({status: 'ok', ms: 1200, at: Date.now()}).indexOf('测试于') >= 0);
  check('不可用结论的 tooltip 带上测试时间',
        api.modelAvailabilityBadge({status: 'bad', error: 'boom', at: Date.now()}, false)
          .indexOf('测试于') >= 0);

  // 过期（24h）的结论画表时也要当作没有 —— 页面开着不动也可能跨过有效期，
  // 不能只在读盘那一刻查一次。
  api.__set({MODELS_DATA: sampleModels()});
  api.__testState({
    'workbuddy|intl|hy4-preview': {status: 'ok', at: Date.now() - 25 * 3600 * 1000, ms: 10},
    'workbuddy|intl|deepseek-v4.1-flash': {status: 'ok', at: Date.now() - 3600 * 1000, ms: 10},
  });
  api.renderAvailableModels();
  const htmlStale = MODELS_TBODY.innerHTML;
  // 两条里：25 小时前那条过期了 → 未测试；1 小时前那条还在 → 可用。
  check('过期的结论画出来是未测试（不是绿）',
        (htmlStale.match(/ava ava-idle/g) || []).length === 1
        && (htmlStale.match(/ava ava-ok/g) || []).length === 1,
        {idle: (htmlStale.match(/ava ava-idle/g) || []).length,
         ok: (htmlStale.match(/ava ava-ok/g) || []).length});
  check('没过期的结论照旧显示',
        api.modelTestStateOf('deepseek-v4.1-flash')
        && api.modelTestStateOf('deepseek-v4.1-flash').status === 'ok');
  check('过期的结论取不到',
        !api.modelTestStateOf('hy4-preview'));

  // --- 列头固定：清单自己纵向滚动，表头吸在容器上沿 ---------------------
  const htmlRaw = fs.readFileSync('dashboard.html', 'utf8');
  check('有可用性详情弹框', /id="modelTestDetailModal"/.test(htmlRaw));
  check('详情弹框带就地重测按钮', /id="modelTestDetailRetest"/.test(htmlRaw));
  check('可用性徽标有可点样式', /\.ava-btn\{cursor:pointer/.test(htmlRaw));
  check('清单外面套了带 id 的滚动容器',
        /id="modelsTableScroll"/.test(htmlRaw));
  check('滚动容器自己纵向滚动',
        /#modelsTableScroll\{overflow:auto/.test(htmlRaw));
  check('滚动容器有高度上限（不然表头钉不住）',
        /#modelsTableScroll\{[^}]*max-height:/.test(htmlRaw));
  check('表头吸在容器上沿',
        /#modelsTable thead th\{position:sticky;top:0/.test(htmlRaw));
  check('表头有 z-index（盖住滚上来的行）',
        /#modelsTable thead th\{position:sticky;top:0;z-index:/.test(htmlRaw));
  check('表头底色不透明（--panel2）',
        /th\{color:var\(--dim\);[^}]*background:var\(--panel2\)\}/.test(htmlRaw));
  check('窄屏卡片视图下撤掉滚动区',
        /#modelsTableScroll\{overflow:visible;max-height:none\}/.test(htmlRaw));

  finish();
})();
