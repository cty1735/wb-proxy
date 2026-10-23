// 用最小 DOM 桩跑一遍「更新通知」：未读计数、已读历史、逐条标已读/未读、
// 一键已读、正文 markdown 与语言选择。不靠浏览器。
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
  'renderNotices', 'noticeUnread', 'noticeBodyHtml', 'noticePick',
  'setNoticeRead', 'markAllNoticesRead', 'loadNotices',
  'openNoticeMenu', 'closeNoticeMenu',
];
const EXPORTS =
  '(function(){' + main + '\n;return {' + want.map(n => n + ': ' + n).join(',') +
  ', __set: function(o){' +
  '  NOTICE_ITEMS = o.items || [];' +
  '  NOTICE_READ = new Set(o.read || o.dismissed || []);' +
  '  NOTICE_SOURCE = o.source || "";' +
  '  NOTICE_SIG = "";' +
  '  if(o.postJSON) postJSON = o.postJSON;' +
  '  if(o.getJSON) getJSON = o.getJSON;' +
  '  window.WB_I18N = {current: function(){ return o.lang || "zh"; },' +
  '    translateHant: function(s){ return "HANT:" + s; }};' +
  '}, __state: function(){ return {items: NOTICE_ITEMS,' +
  '  read: Array.from(NOTICE_READ)}; }' +
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

api.__set({items: sample(), read: ['b'], source: 'docs/notices.md'});
api.renderNotices();

const badge = els.noticeBadge;
const list = els.noticeList;
check('未读数只算未读的', badge.textContent === '1', badge.textContent);
check('徽标打开', badge.classList.contains('on'));
check('计数显示 1 条未读', els.noticeCount.textContent.indexOf('1') >= 0
      && els.noticeCount.textContent.indexOf('未读') >= 0);
check('已读历史仍然渲染', list.innerHTML.indexOf('修好了') >= 0);
check('未读和已读都渲染', list.innerHTML.indexOf('通知中心') >= 0);
check('已读项有 read 标记', list.innerHTML.indexOf('notice-item read') >= 0);
check('已读项显示已读标签', list.innerHTML.indexOf('>已读<') >= 0);
check('未读项按钮是标为已读', list.innerHTML.indexOf('>标为已读<') >= 0);
check('已读项按钮是标为未读', list.innerHTML.indexOf('>标为未读<') >= 0);
check('标题做了 HTML 转义', list.innerHTML.indexOf('&lt;上线&gt;') >= 0);
check('正文支持行内 code', list.innerHTML.indexOf('<code>code</code>') >= 0);
check('正文支持粗体', list.innerHTML.indexOf('<b>粗体</b>') >= 0);
check('列表渲染成 ul/li', list.innerHTML.indexOf('<ul><li>一</li><li>二</li></ul>') >= 0);
check('每条的按钮带 id', list.innerHTML.indexOf('data-notice-toggle="a"') >= 0);
check('底部显示来源', els.noticeSrc.textContent === 'docs/notices.md');
check('一键已读按钮可见', els.noticeReadAll.style.display === '');

// 英文：优先 title_en；没有就保留原文。
api.__set({items: sample(), read: [], source: 'x', lang: 'en'});
check('英文取 title_en', api.noticePick(sample()[0], 'title') === 'Notices <live>');
check('英文缺字段回退原文', api.noticePick(sample()[1], 'title') === '修好了');
api.__set({items: sample(), read: [], source: 'x', lang: 'zh-Hant'});
check('正體中文缺字段走转换', api.noticePick(sample()[1], 'title') === 'HANT:修好了');

// 逐条已读：请求 /notices/read {id, read:true}，已读后历史不消失。
let calls = [];
api.__set({
  items: sample(), read: ['b'], source: 'x',
  postJSON: function(url, body) {
    calls.push({url: url, body: body});
    return Promise.resolve({read: ['a', 'b']});
  },
});
Promise.resolve(api.setNoticeRead('a', true)).then(function() {
  check('标为已读发的是 /notices/read',
        calls[0] && calls[0].url === '/notices/read', calls[0]);
  check('标为已读带 id 和 read:true',
        calls[0] && calls[0].body && calls[0].body.id === 'a'
        && calls[0].body.read === true);
  check('标为已读后历史不消失',
        els.noticeList.innerHTML.indexOf('通知中心') >= 0
        && els.noticeList.innerHTML.indexOf('修好了') >= 0);
  check('标为已读后徽标关闭', !els.noticeBadge.classList.contains('on'));
  check('标为已读后一键按钮隐藏', els.noticeReadAll.style.display === 'none');

  // 再标回未读：历史仍然保留，徽标回来。
  calls = [];
  api.__set({
    items: sample(), read: ['a', 'b'], source: 'x',
    postJSON: function(url, body) {
      calls.push({url: url, body: body});
      return Promise.resolve({read: ['b']});
    },
  });
  return api.setNoticeRead('a', false);
}).then(function() {
  check('标为未读发 read:false',
        calls[0] && calls[0].body && calls[0].body.read === false);
  check('标为未读后徽标回来', els.noticeBadge.textContent === '1'
        && els.noticeBadge.classList.contains('on'));
  check('标为未读后历史还在', els.noticeList.innerHTML.indexOf('修好了') >= 0);

  // 一键已读：全部标为已读，但列表仍保留全部历史。
  calls = [];
  api.__set({
    items: sample(), read: ['b'], source: 'x',
    postJSON: function(url, body) {
      calls.push({url: url, body: body});
      return Promise.resolve({read: ['a', 'b']});
    },
  });
  return api.markAllNoticesRead();
}).then(function() {
  check('一键已读发 all:true 和 read:true',
        calls[0] && calls[0].url === '/notices/read'
        && calls[0].body && calls[0].body.all === true
        && calls[0].body.read === true);
  check('一键已读后徽标关闭', !els.noticeBadge.classList.contains('on'));
  check('一键已读后历史保留',
        els.noticeList.innerHTML.indexOf('通知中心') >= 0
        && els.noticeList.innerHTML.indexOf('修好了') >= 0);
  check('一键已读后按钮隐藏', els.noticeReadAll.style.display === 'none');

  // 空通知：空态文案，不显示一键已读。
  api.__set({items: [], read: [], source: 'x'});
  api.renderNotices();
  check('没有通知时列表空态', els.noticeList.innerHTML.indexOf('暂无更新通知') >= 0);
  check('没有通知时隐藏一键已读', els.noticeReadAll.style.display === 'none');

  // 远程正文：点开铃铛会带 refresh=1 强制回源；来源显示为 GitHub。
  let loadCalls = [];
  api.__set({
    items: [], read: [], source: 'GitHub · test',
    getJSON: function(url) {
      loadCalls.push(url);
      return Promise.resolve({notices: [], read: [], source: 'GitHub · test'});
    },
  });
  return api.loadNotices(false, true).then(function() {
    check('打开铃铛强制回源', loadCalls[0] === '/notices?refresh=1', loadCalls[0]);
    check('来源显示 GitHub', els.noticeSrc.textContent === 'GitHub · test');

    console.log(FAIL ? ('FAILED: ' + FAIL) : 'all checks passed');
    process.exit(FAIL ? 1 : 0);
  });
});
