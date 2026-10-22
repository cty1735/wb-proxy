// 用最小 DOM 桩跑一遍面板「省 token」那几段 JS：
// 档位高亮、开关重放、统计格式化。不靠浏览器。
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('dashboard.html', 'utf8');
const scripts = [];
const re = /<script\b[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = re.exec(html)) !== null) scripts.push(m[1]);

const els = {};
const overrides = {};
function makeEl(id) {
  const el = {
    id: id || '',
    value: '',
    textContent: '',
    innerHTML: '',
    style: { setProperty() {}, removeProperty() {} },
    checked: false,
    disabled: false,
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
    getContext() { return { setTransform() {}, clearRect() {}, scale() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {}, fillRect() {}, fillText() {}, createLinearGradient() { return { addColorStop() {} }; }, measureText() { return { width: 10 }; } }; },
    clientWidth: 800,
    clientHeight: 200,
    width: 800,
    height: 200,
    dataset: {},
  };
  Object.defineProperty(el, 'parentElement', { get() { return makeEl('parent'); } });
  return el;
}
const document = {
  getElementById(id) {
    if (overrides[id]) return overrides[id];
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  },
  querySelector() { return null; },
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
  devicePixelRatio: 1,
  innerWidth: 1400,
  innerHeight: 900,
};
window.window = window;
window.globalThis = window;

const ctx = vm.createContext(Object.assign(window, {
  document, console,
  fetch: () => Promise.reject(new Error('no network in this test')),
  navigator: { userAgent: 'node' },
  URLSearchParams,
  MutationObserver: undefined,
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  esc: s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
  getJSON: () => Promise.reject(new Error('no network in this test')),
  postJSON: () => Promise.reject(new Error('no network in this test')),
  POLL_READ: { cache: 'no-store' },
  toast: () => {},
}));

const main = scripts.find(s => s.indexOf('function tsApply') >= 0);
if (!main) { console.log('could not find the dashboard main script block'); process.exit(1); }
try {
  new vm.Script(main, { filename: 'main.js' });
} catch (e) {
  console.log('SYNTAX ERROR: ' + e.message);
  process.exit(1);
}
try {
  vm.runInContext(main, ctx, { filename: 'main.js' });
} catch (e) {
  console.log('RUNTIME ERROR: ' + (e && e.message));
  process.exit(1);
}

let FAIL = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok   ' + name);
  else { console.log('  FAIL ' + name + ' ' + (detail === undefined ? '' : JSON.stringify(detail))); FAIL++; }
}
process.on('unhandledRejection', () => {});

function pill(level) {
  return {
    level,
    on: false,
    getAttribute(name) { return name === 'data-level' ? this.level : null; },
    classList: {
      toggle(cls, val) { if (cls === 'on') this.owner.on = !!val; },
      owner: null,
    },
  };
}
const cavemanPills = ['off', 'lite', 'full', 'ultra', 'wenyan-lite', 'wenyan', 'wenyan-ultra']
  .map(pill);
const ponytailPills = ['off', 'lite', 'full', 'ultra'].map(pill);
cavemanPills.concat(ponytailPills).forEach(p => { p.classList.owner = p; });
overrides.tsCaveman = { querySelectorAll: () => cavemanPills };
overrides.tsPonytail = { querySelectorAll: () => ponytailPills };

// 1. 静态标记：统计卡、两套档位、六类开关都在
check('面板有省 token 区块', /id="secTokenSaver"/.test(html));
check('统计卡 6 张',
  ['tsBefore', 'tsAfter', 'tsSaved', 'tsBlocks', 'tsCalls', 'tsInjections']
    .every(id => html.indexOf('id="' + id + '"') >= 0));
const cavemanBlock = html.slice(html.indexOf('id="tsCaveman"'), html.indexOf('id="tsPonytail"'));
check('Caveman 七档（关闭/轻/全/极/文言轻/文言/文言极）',
  (cavemanBlock.match(/data-level=/g) || []).length === 7,
  (cavemanBlock.match(/data-level="[^"]*"/g) || []));
const ponytailBlock = html.slice(html.indexOf('id="tsPonytail"'), html.indexOf('id="TS_END_MARK"') >= 0
  ? html.indexOf('id="TS_END_MARK"') : html.indexOf('id="setTokenSaverRtk"'));
check('Ponytail 四档', (ponytailBlock.match(/data-level=/g) || []).length === 4,
  (ponytailBlock.match(/data-level="[^"]*"/g) || []));
check('本地应答五个细项开关都在',
  ['quota', 'command_prefix', 'title', 'suggestion', 'file_path']
    .every(k => html.indexOf('id="setTokenSaverLocal' +
      k.replace(/(^|_)(\w)/g, (a, b, c) => c.toUpperCase())) >= 0) ||
  ['Quota', 'CommandPrefix', 'Title', 'Suggestion', 'FilePath']
    .every(s => html.indexOf('id="setTokenSaverLocal' + s + '"') >= 0));

// 2. tsBytes
// vm 里顶层 function 声明落在 contextified global 上，不在我们的 window 桩上，
// 所以用 runInContext 调，别直接读 ctx.tsBytes。
function call(expr){ return vm.runInContext(expr, ctx); }
check('字节格式化 B', call('tsBytes(0)') === '0 B', call('tsBytes(0)'));
check('字节格式化 KB', call('tsBytes(6144)') === '6.0 KB', call('tsBytes(6144)'));
check('字节格式化 10KB 以上取整', call('tsBytes(10240)') === '10 KB', call('tsBytes(10240)'));
check('字节格式化 MB', call('tsBytes(5*1024*1024)') === '5.00 MB', call('tsBytes(5*1024*1024)'));

// 3. tsApply 重放服务端的值
call('tsApply(' + JSON.stringify({
  enabled: true, rtk: false,
  prompt: { caveman: 'wenyan', ponytail: 'ultra' },
  local: { enabled: true, quota: false, command_prefix: true, title: true, suggestion: true, file_path: true },
}) + ')');
check('总开关跟着存值', document.getElementById('setTokenSaverEnabled').checked === true);
check('RTK 开关跟着存值', document.getElementById('setTokenSaverRtk').checked === false);
check('Caveman 只高亮文言',
  cavemanPills.filter(p => p.on).map(p => p.level).join(',') === 'wenyan',
  cavemanPills.filter(p => p.on).map(p => p.level));
check('Ponytail 只高亮极',
  ponytailPills.filter(p => p.on).map(p => p.level).join(',') === 'ultra',
  ponytailPills.filter(p => p.on).map(p => p.level));
check('本地应答细项：额度探测关闭',
  document.getElementById('setTokenSaverLocalQuota').checked === false);
check('本地应答细项：会话标题开启',
  document.getElementById('setTokenSaverLocalTitle').checked === true);
check('总开关打开时细项不打灰',
  document.getElementById('tsLocalRows').style.opacity === '1',
  document.getElementById('tsLocalRows').style.opacity);

call('tsApply(' + JSON.stringify({ enabled: false, local: { enabled: false }, prompt: {} }) + ')');
check('关掉总开关显示状态', document.getElementById('setTokenSaverState').textContent === '(已关闭)',
  document.getElementById('setTokenSaverState').textContent);
check('本地应答关闭时细项打灰',
  document.getElementById('tsLocalRows').style.opacity === '.6',
  document.getElementById('tsLocalRows').style.opacity);

// 4. 统计渲染
call('tsRenderStats(' + JSON.stringify({
  before: 10240, after: 4096, saved: 6144, blocks: 3, calls: 12,
  injections: 1, percent: 60, local: { quota: 2, title: 1 },
}) + ')');
check('压缩前', document.getElementById('tsBefore').textContent === '10 KB',
  document.getElementById('tsBefore').textContent);
check('压缩后', document.getElementById('tsAfter').textContent === '4.0 KB',
  document.getElementById('tsAfter').textContent);
check('节省字节', document.getElementById('tsSaved').textContent === '6.0 KB',
  document.getElementById('tsSaved').textContent);
check('压缩块数', document.getElementById('tsBlocks').textContent === '3');
check('统计调用', document.getElementById('tsCalls').textContent === '12');
check('提示词注入', document.getElementById('tsInjections').textContent === '1');
const summary = document.getElementById('tsSummary').textContent;
check('口径行写明节省/请求数/百分比/块数',
  summary.indexOf('6.0 KB saved over 12 request(s) (60%), 3 block(s) compacted') >= 0,
  summary);
const note = document.getElementById('tsNote').textContent;
check('说明行把三件事都数出来',
  note.indexOf('12 次请求') >= 0 && note.indexOf('3 块') >= 0 && note.indexOf('3 次探测') >= 0,
  note);

call('tsRenderStats(' + JSON.stringify({ before: 0, after: 0, saved: 0, blocks: 0, calls: 0, injections: 0, local: {} }) + ')');
check('没有数据时回到「还没有可统计的请求」',
  document.getElementById('tsNote').textContent.indexOf('还没有可统计的请求') >= 0,
  document.getElementById('tsNote').textContent);

if (FAIL) { console.log('\n' + FAIL + ' FAILED'); process.exit(1); }
console.log('\nall checks passed');
