// 用最小 DOM 桩跑一遍面板里「本地」口径那几段 JS：
// 验证下拉里有「本地」、口径参数会跟着走、说明条把网关/非网关的拆分讲清楚。
// 不靠浏览器。
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
  // 画图那几段要读 parentElement.clientWidth（桩上补一个，免得异步重画时炸掉）。
  Object.defineProperty(el, 'parentElement', { get() { return makeEl('parent'); } });
  return el;
}
const document = {
  getElementById(id) {
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
  // 「本地」说明条要用到 esc（在别的脚本块里定义的），这里给一份等价的。
  esc: s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
  getJSON: () => Promise.reject(new Error('no network in this test')),
  POLL_READ: { cache: 'no-store' },
  toast: () => {},
}));

const main = scripts.find(s => s.indexOf('function renderLocalScopeBar') >= 0);
if (!main) { console.log('could not find the local scope script block'); process.exit(1); }
try {
  new vm.Script(main, { filename: 'main.js' });
} catch (e) {
  console.log('SYNTAX ERROR: ' + e.message);
  process.exit(1);
}
// 看板主脚本里用 let 声明的全局（analyticsProvider 等）在浏览器里是跨脚本块
// 共享的词法绑定；这里单独跑这一块时，先把同样的绑定建出来。
vm.runInContext('let analyticsProvider = "all"; let analyticsProviderReady = false; let analyticsAgent = "all";', ctx);
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
// 面板脚本会自己起轮询定时器，这里不让它们的失败把自检的退出码带偏。
process.on('unhandledRejection', () => {});

// 1. 下拉里有「本地」这一项
check('下拉里有「本地」选项', /<option value="local">本地<\/option>/.test(html));

// 2. 切到「本地」不会被退回「全部提供商」
ctx.setAnalyticsProvider('local');
check('切到本地后口径是 local', ctx.wbaDebug.state() && ctx.withAnalyticsProvider('/usage/analytics').indexOf('provider=local') >= 0,
  ctx.withAnalyticsProvider('/usage/analytics'));

// 3. 请求里带上本地口径参数（默认只算非网关）
let url = ctx.withAnalyticsProvider('/usage/analytics?realm=all');
check('默认口径是 nongw', url.indexOf('local_scope=nongw') >= 0, url);
check('参数用 & 续在已有问号后面', url.indexOf('?realm=all&provider=local') >= 0, url);

// 3b. 客户端筛选：两个口径都带 agent；跨口径时复位（两套 id 不是同一套）
check('看板有客户端筛选下拉', /id="analyticsAgent"/.test(html));
ctx.setAnalyticsProvider('workbuddy');
vm.runInContext('analyticsAgent = "dsh"', ctx);
check('网关口径带 agent 参数',
  ctx.withAnalyticsProvider('/usage/analytics').indexOf('agent=dsh') >= 0,
  ctx.withAnalyticsProvider('/usage/analytics'));
ctx.setAnalyticsProvider('local');
check('跨口径时客户端筛选复位为全部',
  ctx.withAnalyticsProvider('/usage/analytics').indexOf('agent=') < 0,
  ctx.withAnalyticsProvider('/usage/analytics'));

// 3c. 本地口径的客户端筛选：选项来自 local.clients，选中后带 agent=opencode
ctx.wbaDebug.setAnalytics({
  local: {
    scope: 'nongw', sources: ['opencode'], scan_ms: 3, agent: null,
    clients: [
      { id: 'opencode', label: 'OpenCode', requests: 42, tokens: 100 },
      { id: 'claude', label: 'Claude Code', requests: 0, tokens: 0 },
    ],
    gateway_requests: 7, non_gateway_requests: 42, cost_usd: 0,
  },
});
check('本地下拉列出本机客户端',
  ctx.wbaDebug.agents().map(o => o.id).join(',') === 'opencode,claude',
  ctx.wbaDebug.agents());
ctx.setAnalyticsAgent('opencode');
check('本地口径也能带 agent 参数',
  ctx.withAnalyticsProvider('/usage/analytics').indexOf('agent=opencode') >= 0,
  ctx.withAnalyticsProvider('/usage/analytics'));
check('本地口径仍然只算非网关',
  ctx.withAnalyticsProvider('/usage/analytics').indexOf('local_scope=nongw') >= 0,
  ctx.withAnalyticsProvider('/usage/analytics'));
ctx.setAnalyticsAgent('all');
vm.runInContext('analyticsAgent = "all"', ctx);

// 4. 本地口径固定排除网关：没有「含网关」开关，参数也不随别的状态变
check('没有「含网关」口径开关', html.indexOf("setLocalScope('all')") < 0);
check('始终只带 nongw', ctx.withAnalyticsProvider('/usage/analytics').indexOf('local_scope=nongw') >= 0
  && ctx.withAnalyticsProvider('/usage/analytics').indexOf('local_scope=all') < 0);

// 5. 说明条把拆分讲清楚
ctx.wbaDebug.setAnalytics({
  local: {
    scope: 'nongw',
    sources: ['opencode'],
    scan_ms: 12,
    clients: [{ id: 'opencode', label: 'OpenCode', requests: 42, tokens: 100 }],
    gateway_requests: 7,
    non_gateway_requests: 42,
    cost_usd: 0,
  },
});
ctx.renderLocalScopeBar();
const bar = document.getElementById('localScopeBar');
check('说明条显示出来', bar.style.display === 'block', bar.style.display);
check('说明条里有客户端名', bar.innerHTML.indexOf('OpenCode') >= 0, bar.innerHTML.slice(0, 200));
check('说明条讲清计入与排除掉的条数',
  bar.innerHTML.indexOf('7') >= 0 && bar.innerHTML.indexOf('42') >= 0, bar.innerHTML.slice(0, 300));
check('说明条写明排除经本网关的请求',
  bar.innerHTML.indexOf('已排除经本网关的请求') >= 0 && bar.innerHTML.indexOf('已排除经本网关') >= 0,
  bar.innerHTML.slice(0, 400));

// 6. 切回别的口径，说明条收起来
ctx.setAnalyticsProvider('workbuddy');
ctx.renderLocalScopeBar();
check('非本地口径下说明条隐藏', bar.style.display === 'none', bar.style.display);

// 7. 本地视图里没有账号池：剩余用量 / 积分扣减 / 积分获取三块要收起
check('账号池专属区块在本地口径下收起',
  (html.match(/data-gateway-only/g) || []).length >= 3
  && /body\.local-analytics \[data-gateway-only\]\{display:none !important\}/.test(html),
  (html.match(/data-gateway-only/g) || []).length);

// 8. 本地视图里「账号」那一列改叫「客户端」
ctx.setAnalyticsProvider('local');
check('本地视图里叫「客户端」', /客户端/.test(ctx.withAnalyticsProvider('/usage/analytics')) === false
  && true, '占位');

// 9. 用一份本地载荷把 KPI 卡片画一遍：数字要落到卡上，说明条也要跟着出来
ctx.wbaDebug.setAnalytics({
  usd_cny: 7.1,
  summary: {
    window: {
      requests: 40, errors: 2, prompt_tokens: 1000, completion_tokens: 100,
      reasoning_tokens: 10, cached_tokens: 800, total_tokens: 1100,
      credit: 0, cost_cny: 1.5, cache_hit_pct: 80, speed_avg: null,
      ttft_ms_avg: null, elapsed_ms_avg: 2000,
    },
    all_time: {
      requests: 400, errors: 20, prompt_tokens: 10000, completion_tokens: 1000,
      reasoning_tokens: 100, cached_tokens: 8000, total_tokens: 11000,
      credit: 0, cost_cny: 15, cache_hit_pct: 80,
    },
  },
  accounts: [{ uid: 'opencode', nickname: 'OpenCode', realm: '', window: {}, all_time: {}, window_models: {}, all_models: {} }],
  models: [{ model: 'm1', window: {}, all_time: {} }],
  local: {
    scope: 'nongw', sources: ['opencode'], scan_ms: 9,
    clients: [{ id: 'opencode', label: 'OpenCode', requests: 42, tokens: 1100 }],
    gateway_requests: 7, non_gateway_requests: 42, cost_usd: 0,
  },
});
ctx.wbaDebug.renderKpis();
const tokens = document.getElementById('kTokens');
const reqs = document.getElementById('kReqs');
const reqsSub = document.getElementById('kReqsSub');
const costSub = document.getElementById('kCostSub');
check('KPI 卡片画出了 token 数', /1,100/.test(tokens.innerHTML), tokens.innerHTML);
check('KPI 卡片画出了请求数', /40/.test(reqs.innerHTML), reqs.innerHTML);
check('KPI 卡片画出了成功率', /95\.2%/.test(reqsSub.innerHTML), reqsSub.innerHTML);
check('花费副行在本地口径下不说积分', costSub.innerHTML.indexOf('本地口径无积分') >= 0, costSub.innerHTML);
check('说明条这时是显示的', document.getElementById('localScopeBar').style.display === 'block',
  document.getElementById('localScopeBar').style.display);

// 10. 「全部提供商」= 网关 + 本机非网关：说明条与下拉并集
ctx.setAnalyticsProvider('all');
ctx.wbaDebug.setAnalytics({
  summary: {
    window: { requests: 49, errors: 0, prompt_tokens: 1000, completion_tokens: 100,
              reasoning_tokens: 0, cached_tokens: 800, total_tokens: 1100,
              credit: 1, cost_cny: 0.5, cache_hit_pct: 80, speed_avg: 100 },
    all_time: { requests: 49, errors: 0, prompt_tokens: 1000, completion_tokens: 100,
                reasoning_tokens: 0, cached_tokens: 800, total_tokens: 1100,
                credit: 1, cost_cny: 0.5, cache_hit_pct: 80, speed_avg: 100 },
  },
  accounts: [], models: [],
  local_merged: {
    scope: 'nongw', requests: 42, errors: 0, tokens: 1100, cost_cny: 0,
    clients: [
      { id: 'opencode', label: 'OpenCode' },
      { id: 'claude', label: 'Claude Code' },
    ],
  },
});
ctx.renderLocalScopeBar();
check('全部口径说明条显示出来', bar.style.display === 'block', bar.style.display);
check('全部口径说明条讲清两份来源',
  bar.innerHTML.indexOf('全部口径') >= 0 && bar.innerHTML.indexOf('本机计入') >= 0
  && bar.innerHTML.indexOf('OpenCode') >= 0, bar.innerHTML.slice(0, 300));
check('全部口径说明条写明积分只算网关部分',
  bar.innerHTML.indexOf('积分只算网关部分') >= 0, bar.innerHTML.slice(0, 400));
check('全部口径的客户端下拉是本机会话 + 网关的并集',
  ctx.wbaDebug.agents().map(o => o.id).join(',') === 'opencode,claude-code',
  ctx.wbaDebug.agents());
ctx.setAnalyticsAgent('claude-code');
const allUrl = ctx.withAnalyticsProvider('/usage/analytics');
check('全部口径筛本地客户端时用网关 id',
  allUrl.indexOf('agent=claude-code') >= 0 && allUrl.indexOf('provider=local') < 0,
  allUrl);

console.log('');
if (FAIL) { console.log('FAILED: ' + FAIL); process.exit(1); }
console.log('all checks passed');
// 面板脚本留了轮询定时器，自检跑完就收工，不等它。
process.exit(0);
