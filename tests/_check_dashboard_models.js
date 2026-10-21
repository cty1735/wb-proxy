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
const window = {
  document,
  localStorage: { getItem() { return null; }, setItem() {} },
  location: { search: '' },
  addEventListener() {},
  matchMedia() { return { matches: false, addEventListener() {} }; },
  setTimeout, clearTimeout, setInterval, clearInterval,
  VIEW_REALM: 'intl',
};
window.window = window;
window.globalThis = window;

const ctx = vm.createContext(Object.assign(window, {
  document, console,
  fetch: () => Promise.reject(new Error('no network in this test')),
  navigator: { userAgent: 'node' },
  URLSearchParams, TextDecoder, TextEncoder,
}));

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
];
const setters = [
  'MODELS_DATA', 'EGG_POOL_FILTER', 'currentProvider', 'esc', 'toast',
  'postJSON', 'getJSON', 'MODEL_TEST_MODE',
].map(n => "  try{ if(o." + n + " !== undefined) " + n + " = o." + n + "; }catch(e){}");
const exportSrc = want.map(n => n + ': ' + n).join(',');
const ctxSrc = vm.runInContext(
  '(function(){' + main + '\n;return {' + exportSrc + ', __set: function(o){' +
  setters.join('\n') +
  '}, __testState: function(o){ Object.keys(MODEL_TEST_STATE).forEach(function(k){ delete MODEL_TEST_STATE[k]; });'
  + ' Object.keys(o || {}).forEach(function(k){ MODEL_TEST_STATE[k] = o[k]; }); }'
  + '}})()', ctx);
const api = ctxSrc;
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
check('每行 11 列', (htmlOut.match(/<td\b/g) || []).length === 22,
      (htmlOut.match(/<td\b/g) || []).length);
check('有启用列', htmlOut.indexOf('data-label="启用"') >= 0);
check('有测试列', htmlOut.indexOf('data-label="测试"') >= 0);
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
check('计数提到已停用', els.availModelCount.textContent.indexOf('1 个已停用') >= 0,
      els.availModelCount.textContent);
check('测试按钮接上函数',
      htmlOut.indexOf('onclick="testGatewayModel(&quot;hy4-preview&quot;, this)"') >= 0);
check('启停开关接上函数',
      htmlOut.indexOf('onchange="toggleModelEnabled(&quot;deepseek-v4.1-flash&quot;, this.checked, this)"') >= 0);
check('停用行的测试按钮禁用',
      /data-label="测试"><button class="sec mini" disabled/.test(htmlOut));

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

// --- 空态 -------------------------------------------------------------
api.__set({MODELS_DATA: []});
api.renderAvailableModels();
check('空态跨满 11 列', MODELS_TBODY.innerHTML.indexOf('colspan="11"') >= 0,
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

(async function () {
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

  console.log('');
  if (FAIL) { console.log('FAILED: ' + FAIL); process.exit(1); }
  console.log('all checks passed');
})();
