// 用最小 DOM 桩跑一遍「新功能通知」：未读计数、隐藏项、正文 markdown、
// 「不再显示」的请求体与语言选择。不靠浏览器。
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('dashboard.html', 'utf8');
const scripts = [];
const re = /<script\b[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = re.exec(html)) !== null) scripts.push(m[1]);

// 先确认每个 script 块都能编译，别让一处笔误拖到运行期才暴露。
scripts.forEach((src, i) => {
  try { new vm.Script(src, {filename: 'script' + i + '.js'}); }
  catch (e) { console.log('SYNTAX ERROR in script ' + i + ': ' + e.message); process.exit(1); }
});

const els = {};
function makeEl(id) {
  const el = {
    id: id || '',
    value: '',
    textContent: '',
    innerHTML: '',
    style: {},
    checked: false,
    disabled: false,
    attrs: {},
    classes: new Set(),
    dataset: {},
    classList: {
      add(c) { el.classes.add(c); },
      remove(c) { el.classes.delete(c); },
      toggle(c, on) {
        if (on === undefined) on = !el.classes.has(c);
        if (on) el.classes.add(c); else el.classes.delete(c);
        return on;
      },
      contains(c) { return el.classes.has(c); },
    },
    addEventListener() {},
    appendChild() {},
    removeChild() {},
    scrollIntoView() {},
    focus() {},
    blur() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    getAttribute(k) { return el.attrs[k] === undefined ? null : el.attrs[k]; },
    setAttribute(k, v) { el.attrs[k] = String(v); },
    removeAttribute(k) { delete el.attrs[k]; },
  };
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
const localStorage = {
  getItem() { return null; },
  setItem() {},
  removeItem() {},
};

const main = scripts.find(s => s.indexOf('function renderNotices') >= 0);
if (!main) { console.log('could not find the notices script block'); process.exit(1); }

const want = [
  'renderNotices', 'noticeVisible', 'noticeBodyHtml', 'noticePick',
  'dismissNotice', 'dismissAllNotices', 'loadNotices',
  'openNoticeMenu', 'closeNoticeMenu',
];
const EXPORTS =
  '(function(){' + main + '\n;return {' + want.map(n => n + ': ' + n).join(',') +
  ', __set: function(o){' +
  '  NOTICE_ITEMS = o.items || [];' +
  '  NOTICE_DISMISSED = new Set(o.dismissed || []);' +
  '  NOTICE_SOURCE = o.source || "";' +
  '  NOTICE_SIG = "";' +
  '  if(o.postJSON) postJSON = o.postJSON;' +
  '  if(o.getJSON) getJSON = o.getJSON;' +
  '  window.WB_I18N = {current: function(){ return o.lang || "zh"; },' +
  '    translateHant: function(s){ return "HANT:" + s; }};' +
  '}, __state: function(){ return {items: NOTICE_ITEMS,' +
  '  dismissed: Array.from(NOTICE_DISMISSED)}; }' +
  '}})()';

const win = {
  document, localStorage, console,
  location: { search: '' },
  addEventListener() {},
  matchMedia() { return { matches: false, addEventListener() {} }; },
  setTimeout, clearTimeout, setInterval, clearInterval,
  navigator: { userAgent: 'node' },
  URLSearchParams, TextDecoder, TextEncoder, AbortController,
};
win.window = win;
win.globalThis = win;
const api = vm.runInContext(EXPORTS, vm.createContext(win));
if (!api) { console.log('could not extract functions'); process.exit(1); }

let FAIL = 0;
function check(name, cond, detail) {
  if (cond) console.log('  ok   ' + name);
  else { console.log('  FAIL ' + name + ' ' + (detail === undefined ? '' : JSON.stringify(detail))); FAIL++; }
}

function sample() {
  return [
    {
      id: 'a', date: '2026-10-23', level: 'feature',
      title: '通知中心 <上线>', body: '第一段 `code` 与 **粗体**。\n\n- 一\n- 二',
      title_en: 'Notices <live>', body_en: '', title_hant: '', body_hant: '',
    },
    {
      id: 'b', date: '2026-10-22', level: 'fix',
      title: '修好了', body: '正文', title_en: '', body_en: '', title_hant: '', body_hant: '',
    },
  ];
}

api.__set({items: sample(), dismissed: ['b'], source: 'docs/notices.md'});
api.renderNotices();

const badge = els.noticeBadge;
const list = els.noticeList;
check('未读数只算没隐藏的', badge.textContent === '1', badge.textContent);
check('徽标打开', badge.classList.contains('on'));
check('计数显示 1 条', els.noticeCount.textContent.indexOf('1') >= 0);
check('隐藏的通知不渲染', list.innerHTML.indexOf('修好了') < 0);
check('标题做了 HTML 转义', list.innerHTML.indexOf('&lt;上线&gt;') >= 0);
check('正文支持行内 code', list.innerHTML.indexOf('<code>code</code>') >= 0);
check('正文支持粗体', list.innerHTML.indexOf('<b>粗体</b>') >= 0);
check('列表渲染成 ul/li', list.innerHTML.indexOf('<ul><li>一</li><li>二</li></ul>') >= 0);
check('每条的按钮带 id', list.innerHTML.indexOf('data-notice-dismiss="a"') >= 0);
check('底部显示来源', els.noticeSrc.textContent === 'docs/notices.md');
check('全部隐藏按钮可见', els.noticeDismissAll.style.display === '');

// 英文：优先 title_en；没有就保留原文。
api.__set({items: sample(), dismissed: [], source: 'x', lang: 'en'});
check('英文取 title_en', api.noticePick(sample()[0], 'title') === 'Notices <live>');
check('英文缺字段回退原文', api.noticePick(sample()[1], 'title') === '修好了');
api.__set({items: sample(), dismissed: [], source: 'x', lang: 'zh-Hant'});
check('正體中文缺字段走转换', api.noticePick(sample()[1], 'title') === 'HANT:修好了');

// 「不再显示」：请求体是 {id}，回来按服务端的 dismissed 重画。
let calls = [];
api.__set({
  items: sample(), dismissed: [], source: 'x',
  postJSON: function(url, body) {
    calls.push({url: url, body: body});
    return Promise.resolve({dismissed: ['a']});
  },
});
Promise.resolve(api.dismissNotice('a')).then(function() {
  check('dismiss 发的是 /notices/dismiss',
        calls[0] && calls[0].url === '/notices/dismiss', calls[0]);
  check('dismiss 带 id', calls[0] && calls[0].body && calls[0].body.id === 'a');
  check('dismiss 后按服务端状态隐藏', els.noticeBadge.textContent === '1'
        && api.__state().dismissed.indexOf('a') >= 0);

  // 全部隐藏
  calls = [];
  api.__set({
    items: sample(), dismissed: [], source: 'x',
    postJSON: function(url, body) {
      calls.push({url: url, body: body});
      return Promise.resolve({dismissed: ['a', 'b']});
    },
  });
  return Promise.resolve(api.dismissAllNotices());
}).then(function() {
  check('全部隐藏发 all:true', calls[0] && calls[0].body && calls[0].body.all === true);
  check('全部隐藏后徽标关闭', !els.noticeBadge.classList.contains('on'));
  check('全部隐藏后列表空态', els.noticeList.innerHTML.indexOf('暂无新功能通知') >= 0);

  // 空通知不该打开面板
  api.__set({items: [], dismissed: [], source: 'x'});
  api.renderNotices();
  check('没有通知时列表空态', els.noticeList.innerHTML.indexOf('暂无新功能通知') >= 0);
  check('没有通知时隐藏全部按钮', els.noticeDismissAll.style.display === 'none');

  // 远程正文：点开铃铛会带 refresh=1 强制回源；来源显示为 GitHub。
  let loadCalls = [];
  api.__set({
    items: [], dismissed: [], source: 'GitHub · test',
    getJSON: function(url) {
      loadCalls.push(url);
      return Promise.resolve({notices: [], dismissed: [], source: 'GitHub · test'});
    },
  });
  return api.loadNotices(false, true).then(function() {
    check('打开铃铛强制回源', loadCalls[0] === '/notices?refresh=1', loadCalls[0]);
    check('来源显示 GitHub', els.noticeSrc.textContent === 'GitHub · test');

    console.log(FAIL ? ('FAILED: ' + FAIL) : 'all checks passed');
    process.exit(FAIL ? 1 : 0);
  });
});
