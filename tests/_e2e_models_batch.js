// 端到端：把真正的 dashboard.html 跑起来，配一个假网关，验证「一键测试模型」
// 真的能跑完、真的并发、真的能叫停。
//   · 服务的是仓库里那份 dashboard.html（一个字没改），只在 </body> 前插一段
//     驱动脚本（点击按钮、采样、回报）。
//   · /v1/chat/completions 按模型给不同的延迟 / 失败，用来逼出并发的效果。
//   · 一个 <img> 把 load 事件拖住，headless Chrome 才会等到测完再截图。
//
// 跑法：node tests/_e2e_models_batch.js [full|stop]
//   full（默认）= 跑完一整轮；stop = 跑到一半点「停止测试」。
// 需要本机装了 Chrome（路径写在下面 CHROME 常量里），全程约 30 秒。
const fs = require('fs');
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 8792;
const DASH = fs.readFileSync('dashboard.html', 'utf8');

// --- 假网关 ------------------------------------------------------------
const MODELS = [
  {id: 'm-fast-01', name: 'Fast 01'}, {id: 'm-fast-02', name: 'Fast 02'},
  {id: 'm-fast-03', name: 'Fast 03'}, {id: 'm-fast-04', name: 'Fast 04'},
  {id: 'm-fast-05', name: 'Fast 05'}, {id: 'm-fast-06', name: 'Fast 06'},
  {id: 'm-fast-07', name: 'Fast 07'}, {id: 'm-fast-08', name: 'Fast 08'},
  {id: 'm-dead', name: 'Dead (500)'},
  {id: 'm-slow', name: 'Slow (4s)'},
  {id: 'm-off', name: 'Disabled', disabled: true},
].map((m, i) => Object.assign({
  context_window: 131072, max_output_tokens: 32768, available: true,
  modalities: ['text'], supports_tools: true, reasoning_efforts: [],
  egg_source: 'zen', egg_source_label: 'Zen 免费车道',
}, m));

function modelPayload() {
  return {
    data: MODELS,
    catalog: {source: 'remote', fetched_at: Date.now() / 1000},
    egg: {count: MODELS.length, zen: 8, kilo: 3},
  };
}

// 每个模型的「上游表现」：延迟多久回、还是直接报错。
function upstreamFor(id) {
  if (id === 'm-dead') return {kind: 'error'};
  if (id === 'm-slow') return {kind: 'ok', delay: 4000};
  return {kind: 'ok', delay: 600};
}

let inFlight = 0, maxInFlight = 0, chatCalls = 0;

// --- 驱动脚本（插进页面里） -------------------------------------------
const DRIVER = `
<img src="/__hold?ms=16000" style="display:none">
<script>
(function(){
  var log = [], t0 = Date.now();
  function badges(){
    var t = document.querySelectorAll('#modelsTable .ava');
    var c = {ok:0, bad:0, run:0, idle:0};
    for (var i = 0; i < t.length; i++){
      var c2 = t[i].className;
      if (c2.indexOf('ava-ok') >= 0) c.ok++;
      else if (c2.indexOf('ava-bad') >= 0) c.bad++;
      else if (c2.indexOf('ava-run') >= 0) c.run++;
      else c.idle++;
    }
    return c;
  }
  function snap(){
    var b = document.getElementById('btnTestAllModels');
    var s = document.getElementById('btnStopModelTests');
    log.push({ms: Date.now() - t0, btn: b ? b.textContent : '',
              stop: s ? s.style.display : '', badges: badges()});
  }
  function report(){
    fetch('/__report', {method:'POST', body: JSON.stringify(log)});
  }
  function isolate(){
    // 只留下「网关模型清单」那一段，截图才看得到结果（这页上半截是鸡蛋概览、
    // 平台账号池、供应商池，页面很长，滚过去容易滚到空白处）。
    var host = document.getElementById('pageGateway');
    if (!host) return;
    var secs = host.querySelectorAll('section');
    for (var i = 0; i < secs.length; i++){
      if (!secs[i].querySelector('#modelsTable')) secs[i].style.display = 'none';
    }
    var hdr = host.querySelectorAll(':scope > *');
    for (var j = 0; j < hdr.length; j++){
      if (hdr[j].tagName !== 'SECTION' && !hdr[j].querySelector('#modelsTable')) hdr[j].style.display = 'none';
    }
    window.scrollTo(0, 0);
  }
  function run(stopAfterMs){
    var b = document.getElementById('btnTestAllModels');
    if (!b) { report(); return; }
    b.click();
    var iv = setInterval(snap, 250);
    if (stopAfterMs > 0) setTimeout(function(){
      var s = document.getElementById('btnStopModelTests');
      if (s) s.click();
      snap();
    }, stopAfterMs);
    setTimeout(function(){ clearInterval(iv); snap(); report(); }, 13000);
  }
  // 等模型清单到位再点
  var tries = 0;
  var iv2 = setInterval(function(){
    var rows = document.querySelectorAll('#modelsTable tbody tr').length;
    if (rows > 3) { clearInterval(iv2); setTimeout(function(){
      isolate();
      run(${process.argv[2] === 'stop' ? 500 : 0});
    }, 200); }
    else if (++tries > 80) { clearInterval(iv2); report(); }
  }, 150);
})();
</script>
`;

let report = null;

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = u.pathname;
  const json = (obj, code) => {
    res.writeHead(code || 200, {'Content-Type': 'application/json; charset=utf-8'});
    res.end(JSON.stringify(obj));
  };

  if (p === '/' || p === '/dashboard') {
    const html = DASH.replace('</body>', DRIVER + '</body>');
    res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
    res.end(html);
    return;
  }
  if (p === '/__hold') {
    const ms = Number(u.searchParams.get('ms') || 1000);
    setTimeout(() => {
      res.writeHead(200, {'Content-Type': 'image/gif'});
      res.end(Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64'));
    }, ms);
    return;
  }
  if (p === '/__report' && req.method === 'POST') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => { try { report = JSON.parse(body); } catch (e) {} res.writeHead(200); res.end('ok'); });
    return;
  }
  if (p === '/panel/status') return json({authenticated: true, panel_password_is_default: false});
  if (p === '/settings') return json({provider: 'egg', realm: 'intl', api_keys: [], ui_language: 'zh'});
  if (p === '/v1/models') return json(modelPayload());
  if (p === '/v1/chat/completions') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      let id = '';
      try { id = (JSON.parse(body || '{}').model) || ''; } catch (e) {}
      chatCalls++;
      inFlight++;
      if (inFlight > maxInFlight) maxInFlight = inFlight;
      const up = upstreamFor(id);
      const done = () => { inFlight--; };
      if (up.kind === 'error') {
        setTimeout(() => {
          done();
          res.writeHead(500, {'Content-Type': 'application/json'});
          res.end(JSON.stringify({error: {message: '上游挂了'}}));
        }, 50);
        return;
      }
      setTimeout(() => {
        res.writeHead(200, {'Content-Type': 'text/event-stream'});
        res.write('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"read_file","arguments":"{}"}}]}}]}\n\n');
        res.write('data: [DONE]\n\n');
        res.end();
        done();
      }, up.delay);
    });
    return;
  }
  // 其余接口一律给空对象：页面上别的区块报错没关系，模型清单能出来就行。
  json({});
});

function runChrome(args) {
  return new Promise(resolve => {
    const cp = spawn(CHROME, args, {stdio: 'ignore'});
    const kill = setTimeout(() => { try { cp.kill(); } catch (e) {} }, 60000);
    cp.on('exit', () => { clearTimeout(kill); resolve(); });
  });
}

function analyze(log, label) {
  console.log('\n===== ' + label + ' =====');
  if (!log || !log.length) { console.log('  没拿到观测数据'); return false; }
  const first = log.find(s => s.btn.indexOf('测试中') >= 0);
  const last = log[log.length - 1];
  const runCount = log.filter(s => s.badge && s.badges.run > 0).length;
  const maxRun = Math.max.apply(null, log.map(s => s.badges.run));
  console.log('  观测点 ' + log.length + ' 个，跨 ' + (last.ms - log[0].ms) + 'ms');
  console.log('  按钮最后长这样：' + last.btn);
  console.log('  同时在飞的模型最多 ' + maxRun + ' 条（并发上限 4）');
  console.log('  徽标末态：可用 ' + last.badges.ok + ' · 不可用 ' + last.badges.bad
              + ' · 测试中 ' + last.badges.run + ' · 未测试 ' + last.badges.idle);
  // 进度有没有往前走
  const busy = log.filter(s => s.btn.indexOf('测试中') >= 0);
  const progresses = busy.map(s => (s.btn.match(/测试中 (\d+)\/(\d+)/) || [])[1])
                        .filter(x => x !== undefined);
  const uniq = progresses.filter((x, i) => i === 0 || x !== progresses[i - 1]);
  console.log('  进度跳变：' + uniq.join(' → '));
  if (busy.length) {
    const t0 = busy[0].ms, t1 = busy[busy.length - 1].ms;
    console.log('  整轮跑了约 ' + (t1 - t0) + 'ms（串行的话至少要 ' + (chatCalls * 600) + 'ms）');
  }
  const elapsed = busy.map(s => (s.btn.match(/· (\d+:\d+)/) || [])[1]).filter(Boolean);
  if (elapsed.length) {
    console.log('  按钮上的计时：' + elapsed[0] + ' → ' + elapsed[elapsed.length - 1]);
  }
  // 叫停那一轮：应该只测了开头那几条，后面的没再发出去。
  if (process.argv[2] === 'stop') {
    const ok = chatCalls < 10 && last.badges.ok + last.badges.bad < 10;
    console.log('  叫停是否生效：' + (ok ? '是（只测了 ' + (last.badges.ok + last.badges.bad) + ' 条，其余没发出去）' : '否'));
  }
  return true;
}

server.listen(PORT, '127.0.0.1', async () => {
  const mode = process.argv[2] === 'stop' ? 'stop' : 'full';
  const png = path.resolve('tests/_e2e_models_' + mode + '.png');
  await runChrome([
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-sandbox',
    '--window-size=1500,760', '--screenshot=' + png,
    'http://127.0.0.1:' + PORT + '/?provider=egg',
  ]);
  // 等页面把报告发回来
  for (let i = 0; i < 60 && !report; i++) await new Promise(r => setTimeout(r, 250));
  console.log('\n后端收到的 /v1/chat/completions 请求数：' + chatCalls
              + '，后端看到的最大并发：' + maxInFlight);
  analyze(report, mode === 'stop' ? '叫停那一轮' : '跑完一整轮');
  console.log('截图：' + png);
  server.close();
  process.exit(0);
});
