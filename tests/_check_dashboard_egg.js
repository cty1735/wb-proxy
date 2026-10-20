// 用最小 DOM 桩跑一遍面板里「供应商池 + 池子筛选」那几段 JS：
// 验证渲染出来的 HTML 与筛选逻辑都对，不靠浏览器。
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
    dataset: {},
  };
}
const document = {
  getElementById(id) {
    if (!els[id]) els[id] = makeEl(id);
    return els[id];
  },
  querySelector(sel) {
    // 池子选择器的筛选要真的操作 DOM：把下面那几个桩接上。
    if (sel && sel.indexOf('.egg-pool-pick-list') >= 0) return PICK_LIST;
    if (sel && sel.indexOf('.egg-pool-pick-shown') >= 0) return PICK_HINT;
    if (sel && sel.indexOf('.egg-pool-pick-count') >= 0) return PICK_COUNT;
    return null;
  },
  querySelectorAll(sel) {
    if (sel && sel.indexOf('input[data-scope=') >= 0 && PICK_LIST) {
      return PICK_LIST.querySelectorAll('input[type=checkbox]');
    }
    return [];
  },
  createElement() {
    const el = makeEl('');
    el.appendChild = function() {};
    el.removeChild = function() {};
    return el;
  },
  body: makeEl('body'),
  head: (() => { const h = makeEl('head'); h.appendChild = function() {}; return h; })(),
  documentElement: makeEl('html'),
  addEventListener() {},
};
// 选择器筛选用的 DOM 桩（下面第 10 组用例会填内容）。
let PICK_LIST = null;
let PICK_HINT = makeEl('eggPoolPickHint');
let PICK_COUNT = makeEl('eggPoolPickCount');
// 造一份「模型条目列表」：每条有 data-mid / data-free 与一个复选框。
function makePickList(items) {
  const rows = items.map(it => {
    const input = {
      checked: !!it.checked,
      getAttribute(n) { return n === 'data-scope' ? 'card' : null; },
    };
    const label = {
      style: {},
      getAttribute(n) {
        if (n === 'data-mid') return it.mid;
        if (n === 'data-free') return it.free ? '1' : null;
        return null;
      },
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      querySelectorAll(sel) { return sel === 'input[type=checkbox]' ? [input] : []; },
    };
    input.closest = sel => (sel === '.egg-pool-pick-item' ? label : null);
    return { label, input };
  });
  return {
    querySelectorAll(sel) {
      if (sel === '.egg-pool-pick-item') return rows.map(r => r.label);
      if (sel === 'input[type=checkbox]') return rows.map(r => r.input);
      return [];
    },
  };
}
const window = {
  document,
  localStorage: { getItem() { return null; }, setItem() {} },
  location: { search: '' },
  addEventListener() {},
  matchMedia() { return { matches: false, addEventListener() {} }; },
  setTimeout, clearTimeout, setInterval, clearInterval,
};
window.window = window;
window.globalThis = window;

// 桩掉几个会立刻跑起来的全局初始化
const ctx = vm.createContext(Object.assign(window, {
  document, console,
  fetch: () => Promise.reject(new Error('no network in this test')),
  navigator: { userAgent: 'node' },
  URLSearchParams,
}));

// 只跑含「供应商池」那几段函数的那个脚本块（前面的 head 脚本依赖太多东西）。
const main = scripts.find(s => s.indexOf('function buildEggPoolFilter') >= 0);
if (!main) { console.log('could not find the egg pool script block'); process.exit(1); }
try {
  new vm.Script(main, {filename: 'main.js'});
} catch (e) {
  console.log('SYNTAX ERROR: ' + e.message);
  process.exit(1);
}

// 把主脚本里的函数挑出来执行：脚本里的状态是用 let 声明的，被 IIFE 作用域
// 关着，所以在同一个作用域里挂一个 __set 出来，从里面改这些状态。
const want = [
  'buildEggPoolFilter', 'eggFilteredModels', 'renderEggPoolList',
  'renderEggPresets', 'applyEggPreset', 'resetEggPoolForm',
  'renderEggPoolFormPicker', 'eggPoolPickerHtml', 'toggleEggPoolModel',
  'eggPoolSelectAll', 'onEggPoolFilterChange', 'editEggPool',
  'openEggPoolPicker', 'eggPoolPickerFilter', 'eggModelIsFree',
];
const setters = [
  'MODELS_DATA', 'EGG_POOLS', 'EGG_POOL_PRESETS', 'EGG_POOL_FORM',
  'EGG_POOL_CARD', 'EGG_POOL_FILTER', 'EGG_POOL_PICK_FILTER', 'currentProvider', 'esc',
  'fmtCatalogStamp', 'toast', 'getJSON', 'postJSON', 'loadModels',
].map(n => "  try{ if(o." + n + " !== undefined) " + n + " = o." + n + "; }catch(e){}");
const exportSrc = want.map(n => n + ': ' + n).join(',');
const ctxSrc = vm.runInContext(
  '(function(){' + main + '\n;return {' + exportSrc + ', __set: function(o){' +
  setters.join('\n') +
  '}, __state: function(){ return {EGG_POOL_FORM: EGG_POOL_FORM,'
  + ' EGG_POOL_CARD: EGG_POOL_CARD, EGG_POOL_FILTER: EGG_POOL_FILTER}; }'
  + '}})()', ctx);
const api = ctxSrc;
if (!api) { console.log('could not extract functions'); process.exit(1); }

let FAIL = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok   ' + name);
  else { console.log('  FAIL ' + name + ' ' + (detail === undefined ? '' : JSON.stringify(detail))); FAIL++; }
}

// --- 准备状态 ---------------------------------------------------------
api.__set({
  currentProvider: function () { return 'egg'; },
  MODELS_DATA: [
    { id: 'm1', egg_source: 'zen', egg_source_label: 'Zen 免费车道' },
    { id: 'm2', egg_source: 'kilo', egg_source_label: 'Kilo 免费池' },
    { id: 'm3', egg_source: 'pool-abc', egg_source_label: 'NVIDIA NIM' },
    { id: 'm4', egg_source: 'pool-abc', egg_source_label: 'NVIDIA NIM' },
  ],
  EGG_POOLS: [
    {
      id: 'pool-abc', name: 'NVIDIA NIM', protocol: 'openai',
      protocol_label: 'OpenAI 兼容',
      base_url: 'https://integrate.api.nvidia.com/v1',
      api_key_masked: 'nvapi****lQ63', enabled: true,
      models: ['m3', 'm4'],
      catalog: [{ id: 'm3' }, { id: 'm4' }, { id: 'm9' }],
      model_count: 2, catalog_count: 3, catalog_at: 1750000000,
    },
  ],
  EGG_POOL_PRESETS: [
    {
      name: 'NVIDIA NIM', base_url: 'https://integrate.api.nvidia.com/v1',
      protocol: 'openai', host: 'integrate.api.nvidia.com',
    },
  ],
  EGG_POOL_FORM: { editingId: '', picked: [], fetched: [] },
  EGG_POOL_CARD: { id: '', picked: [] },
  EGG_POOL_FILTER: '',
  esc: function (s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  },
  fmtCatalogStamp: function (sec) { return sec ? '2026-01-01 00:00:00' : ''; },
  toast: function () {},
  getJSON: function () { return Promise.resolve({}); },
  postJSON: function () { return Promise.resolve({ ok: true }); },
  loadModels: function () { return Promise.resolve(); },
});

// 1. 池子筛选下拉：三个池子各一项，带模型数与模型名
api.buildEggPoolFilter();
const sel = document.getElementById('eggPoolFilter');
check('筛选框显示', sel.style.display === 'inline-block', sel.style.display);
check('筛选框有三个池子 + 全部',
  (sel.innerHTML.match(/<option/g) || []).length === 4, sel.innerHTML);
check('筛选框含池子名', sel.innerHTML.indexOf('NVIDIA NIM') >= 0, sel.innerHTML);
check('筛选框含模型数', sel.innerHTML.indexOf('（2 个模型') >= 0, sel.innerHTML);
// 下拉里要能看出「是哪些模型」，不只是个数
check('筛选框含模型名', sel.innerHTML.indexOf('m3、m4') >= 0, sel.innerHTML);
check('筛选框含全部池子', sel.innerHTML.indexOf('全部池子（4 个模型）') >= 0, sel.innerHTML);

// 1b. 说明行：选中池子后把该池的模型摊开
const hint = document.getElementById('eggPoolFilterHint');
check('说明行默认显示全部摘要',
  hint.textContent.indexOf('全部池子（4 个模型）') >= 0, hint.textContent);
api.__set({ EGG_POOL_FILTER: 'pool-abc' });
api.buildEggPoolFilter();
check('说明行摊开选中池子的模型',
  hint.textContent.indexOf('NVIDIA NIM（2 个模型）：m3、m4') >= 0, hint.textContent);
check('说明行带完整模型清单', hint.title.indexOf('m3') >= 0 && hint.title.indexOf('m4') >= 0,
  hint.title);
api.__set({ EGG_POOL_FILTER: '' });
api.buildEggPoolFilter();

// 2. 过滤生效
api.__set({ EGG_POOL_FILTER: 'pool-abc' });
check('按池子过滤', api.eggFilteredModels().length === 2, api.eggFilteredModels().length);
api.__set({ EGG_POOL_FILTER: 'zen' });
check('按 Zen 过滤', api.eggFilteredModels().length === 1);
api.__set({ EGG_POOL_FILTER: '' });
check('不过滤时是全部', api.eggFilteredModels().length === 4);

// 3. 池子卡片
api.renderEggPoolList();
const list = document.getElementById('eggPoolList');
check('卡片里有池子名', list.innerHTML.indexOf('NVIDIA NIM') >= 0, list.innerHTML.slice(0, 200));
check('卡片里有协议标签', list.innerHTML.indexOf('OpenAI 兼容') >= 0);
check('卡片里有 Key 掩码', list.innerHTML.indexOf('nvapi****lQ63') >= 0);
check('卡片里有模型数', list.innerHTML.indexOf('2 模型') >= 0, list.innerHTML.slice(0, 400));
check('卡片里有模型 chip', list.innerHTML.indexOf('egg-pool-model') >= 0);
check('卡片里有 Base URL', list.innerHTML.indexOf('integrate.api.nvidia.com') >= 0);
['挑选模型', '编辑', '刷新', '删除'].forEach(label => {
  check('卡片有「' + label + '」', list.innerHTML.indexOf('>' + label + '<') >= 0);
});

// 4. 预设
api.renderEggPresets();
const presets = document.getElementById('eggPresetList');
check('预设渲染', presets.innerHTML.indexOf('NVIDIA NIM') >= 0, presets.innerHTML);
check('预设带主机名', presets.innerHTML.indexOf('integrate.api.nvidia.com') >= 0);

// 5. 预设点一下：填表单、清掉编辑态
api.applyEggPreset(0);
check('预设填了名称', document.getElementById('eggPoolName').value === 'NVIDIA NIM',
  document.getElementById('eggPoolName').value);
check('预设填了 Base URL',
  document.getElementById('eggPoolBaseUrl').value === 'https://integrate.api.nvidia.com/v1');
check('预设填了协议', document.getElementById('eggPoolProtocol').value === 'openai');
check('预设清掉编辑态', api.__state().EGG_POOL_FORM.editingId === '');

// 6. 编辑：把池子填进表单
api.editEggPool('pool-abc');
check('编辑填了名称', document.getElementById('eggPoolName').value === 'NVIDIA NIM');
check('编辑填了 Base URL',
  document.getElementById('eggPoolBaseUrl').value === 'https://integrate.api.nvidia.com/v1');
check('编辑态记住 id', api.__state().EGG_POOL_FORM.editingId === 'pool-abc');
check('编辑不回显 Key', document.getElementById('eggPoolKey').value === '');
check('编辑标题带名字',
  document.getElementById('eggPoolFormTitle').textContent.indexOf('NVIDIA NIM') >= 0,
  document.getElementById('eggPoolFormTitle').textContent);

// 7. 清空
api.resetEggPoolForm();
check('清空后名称空', document.getElementById('eggPoolName').value === '');
check('清空后编辑态空', api.__state().EGG_POOL_FORM.editingId === '');

// 8. 挑选模型：卡片里摊开选择器
api.openEggPoolPicker('pool-abc');
check('卡片选择器打开', api.__state().EGG_POOL_CARD.id === 'pool-abc');
const list2 = document.getElementById('eggPoolList');
check('选择器摊在卡片里', list2.innerHTML.indexOf('egg-pool-pick-item') >= 0);
check('选择器有上游的 3 个模型',
  (list2.innerHTML.match(/egg-pool-pick-item/g) || []).length === 3,
  (list2.innerHTML.match(/egg-pool-pick-item/g) || []).length);
check('选择器默认勾中已挑的',
  (list2.innerHTML.match(/type="checkbox" checked/g) || []).length === 3,
  (list2.innerHTML.match(/type="checkbox" checked/g) || []).length);

// 9. 非鸡蛋视图：筛选框藏起来
api.__set({ currentProvider: function () { return 'workbuddy'; } });
api.buildEggPoolFilter();
check('非鸡蛋视图藏起筛选框', sel.style.display === 'none', sel.style.display);
check('非鸡蛋视图不过滤', api.eggFilteredModels().length === 4);

// 10. 挑选模型：按 id 搜 + 只看免费
api.__set({ currentProvider: function () { return 'egg'; } });
check('免费判定：:free 后缀',
  api.eggModelIsFree('minimax/minimax-m3.1-flash:free') === true);
check('免费判定：/free 后缀', api.eggModelIsFree('kilo-auto/free') === true);
check('免费判定：普通付费模型',
  api.eggModelIsFree('anthropic/claude-sonnet-4-5') === false);
check('免费判定：上游标 free 的',
  api.eggModelIsFree({ id: 'x/y', free: true }) === true);

const pickItems = [
  { mid: 'minimax/minimax-m3.1-flash:free', free: true, checked: true },
  { mid: 'anthropic/claude-sonnet-4-5', free: false, checked: false },
  { mid: 'nvidia/nemotron-3.5-super-vl:free', free: true, checked: false },
  { mid: 'kilo-auto/free', free: true, checked: false },
];
PICK_LIST = makePickList(pickItems);
PICK_HINT = makeEl('eggPoolPickHint');
PICK_COUNT = makeEl('eggPoolPickCount');
api.__set({
  EGG_POOL_PICK_FILTER: { form: { q: '', free: false }, card: { q: '', free: false } },
  EGG_POOL_CARD: { id: 'pool-abc', picked: ['minimax/minimax-m3.1-flash:free'] },
});
api.eggPoolPickerFilter('card', '', true);
check('只看免费：显示数对',
  PICK_HINT.textContent.indexOf('显示 3 / 4') >= 0, PICK_HINT.textContent);
check('只看免费：免费计数对',
  PICK_HINT.textContent.indexOf('免费 3') >= 0, PICK_HINT.textContent);
const hidden = PICK_LIST.querySelectorAll('.egg-pool-pick-item')
  .filter(i => i.style.display === 'none');
check('只看免费：付费的藏起来',
  hidden.length === 1
  && hidden[0].getAttribute('data-mid') === 'anthropic/claude-sonnet-4-5', hidden.length);
check('藏起来的勾选没被动',
  PICK_LIST.querySelectorAll('input[type=checkbox]')[0].checked === true);

// 全选：只作用于当前看得见的（筛到只看免费再全选 = 只勾免费）
api.eggPoolSelectAll(true, 'card');
const picked = api.__state().EGG_POOL_CARD.picked;
check('全选只选看得见的', picked.length === 3, picked);
check('全选含原来勾着的免费模型',
  picked.indexOf('minimax/minimax-m3.1-flash:free') >= 0, picked);
check('全选不含藏起来的付费模型',
  picked.indexOf('anthropic/claude-sonnet-4-5') < 0, picked);

// 按 id 搜索
api.eggPoolPickerFilter('card', 'nemotron', false);
check('按 id 搜索',
  PICK_HINT.textContent.indexOf('显示 1 / 4') >= 0, PICK_HINT.textContent);

console.log('');
if (FAIL) { console.log('FAILED: ' + FAIL); process.exit(1); }
console.log('ALL PASS');
