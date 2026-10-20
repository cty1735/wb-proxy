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
  querySelector() { return null; },
  querySelectorAll() { return []; },
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
  'openEggPoolPicker',
];
const setters = [
  'MODELS_DATA', 'EGG_POOLS', 'EGG_POOL_PRESETS', 'EGG_POOL_FORM',
  'EGG_POOL_CARD', 'EGG_POOL_FILTER', 'currentProvider', 'esc',
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

// 1. 池子筛选下拉：三个池子各一项，带模型数
api.buildEggPoolFilter();
const sel = document.getElementById('eggPoolFilter');
check('筛选框显示', sel.style.display === 'inline-block', sel.style.display);
check('筛选框有三个池子 + 全部',
  (sel.innerHTML.match(/<option/g) || []).length === 4, sel.innerHTML);
check('筛选框含池子名', sel.innerHTML.indexOf('NVIDIA NIM') >= 0, sel.innerHTML);
check('筛选框含模型数', sel.innerHTML.indexOf('（2）') >= 0, sel.innerHTML);

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

console.log('');
if (FAIL) { console.log('FAILED: ' + FAIL); process.exit(1); }
console.log('ALL PASS');
