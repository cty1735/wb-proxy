"""wb_quota_scan.py —— 模型额度定时主动查询 (Quota Scan)

上游每条配额线（账号 × 模型）只有在被真实请求撞到 429 时才会「有记录」：
在那之前没人知道它离限额还有多远，等到真撞上，客户端已经吃了一次失败。
窗口探针（wb_window_probe）解决的是后半段——已经有 429 记录之后，把重置
时刻用掉；前半段没人管。

本模块补的就是前半段：操作者在面板勾选要留意的模型，网关按固定间隔
（默认 6 小时）为每个「账号 × 模型」发一个最小请求，主动看这条配额线是否
已经被打满：

  * 拿到 429 + 重置时刻  -> 这条线已被登记，交给窗口探针到点抢占新窗口，
    并且立即记模型冷却，让真实请求不再撞同一个坑；之后本模块不再查它；
  * 请求通了            -> 这条线还活着，继续按 6 小时查；
  * 网络 / 解析失败     -> 不算查过，15 分钟后重试（连续失败到上限才推迟
    到下一个周期）。

因此判定条件只有一句：**本地已经有登记的 429 窗口记录的配额线不再主动查询**，
其余按间隔轮询；窗口记录消失（探针把它抢回来了）之后自动恢复到轮询队列。

状态落在 `accounts/quota_scan.json`，重启后每个组合的「上次查询时刻」仍然有
效，重启不会让所有模型重新查一遍。

只依赖标准库。
"""

import json
import os
import threading
import time

import wb_accounts
import wb_settings
import wb_window_probe

STATE_NAME = "quota_scan.json"
STATE_VERSION = 1

# 轮询间隔：一条配额线两次主动查询之间至少隔这么久。
SCAN_INTERVAL = 6 * 3600
# 失败的查询不算查过，隔这么久重试。
RETRY_DELAY = 15 * 60.0
# 连续失败到这个次数才放弃这一轮（按正常间隔等下一次）。
MAX_ATTEMPTS = 6
# 首次登记的配额线要排开，别在勾选的一瞬间把请求全打上去。
FIRST_DELAY = 5.0
FIRST_STAGGER = 15.0
FIRST_DELAY_MAX = 3600.0
# 每轮最多处理几个组合，避免一个卡死的上游把调度线程占满。
BATCH_LIMIT = 6
# 相邻两次查询之间的间隔（测试里会把它置 0）。
SCAN_GAP = 0.4
# 这么久没动过的记录直接丢弃：账号大概已经被删了。
MAX_AGE = 30 * 86400

_lock = threading.RLock()
_run_lock = threading.Lock()
_state = {"dir": None, "scans": {}}


# --------------------------------------------------------------- 状态存取

def _state_path(accounts_dir):
    return os.path.join(accounts_dir, STATE_NAME)


def _key(uid, model):
    return "%s\x00%s" % (uid, model)


def _new_row(uid, model, now):
    return {
        "uid": uid,
        "model": model,
        # last_scan=0 表示「还没正经查过」，只有成功的查询才会推进它。
        "last_scan": 0.0,
        "next_at": 0.0,
        "seen_at": now,
        "attempts": 0,
        "last_try": 0.0,
        "last_result": "",
        "last_detail": "",
    }


def _prune(scans, now):
    """丢掉字段残缺、以及太久没动静的记录。"""
    out = {}
    for key, row in (scans or {}).items():
        if not isinstance(row, dict):
            continue
        uid = str(row.get("uid") or "")
        model = str(row.get("model") or "")
        if not uid or not model:
            continue
        stamp = float(row.get("last_try") or row.get("seen_at") or 0)
        if stamp and stamp < now - MAX_AGE:
            continue
        out[str(key)] = {
            "uid": uid,
            "model": model,
            "last_scan": float(row.get("last_scan") or 0),
            "next_at": float(row.get("next_at") or 0),
            "seen_at": float(row.get("seen_at") or 0),
            "attempts": int(row.get("attempts") or 0),
            "last_try": float(row.get("last_try") or 0),
            "last_result": str(row.get("last_result") or "")[:40],
            "last_detail": str(row.get("last_detail") or "")[:200],
        }
    return out


def _load(accounts_dir, now=None):
    """按目录懒加载。换目录（或首次调用）时从磁盘重读。调用方需持有 _lock。"""
    now = time.time() if now is None else now
    if _state["dir"] == accounts_dir:
        return _state["scans"]
    scans = {}
    try:
        # utf-8-sig：手改过的文件可能带 BOM，而 json.load 遇到 BOM 直接抛异常。
        with open(_state_path(accounts_dir), encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        stored = raw.get("scans") if isinstance(raw, dict) else raw
        if isinstance(stored, list):
            for row in stored:
                if isinstance(row, dict) and row.get("uid") and row.get("model"):
                    scans[_key(str(row["uid"]), str(row["model"]))] = row
        elif isinstance(stored, dict):
            scans = {str(k): v for k, v in stored.items() if isinstance(v, dict)}
    except FileNotFoundError:
        pass
    except Exception:
        # 状态文件坏掉不该拖垮网关：当作没有记录，从头观察。
        scans = {}
    _state["dir"] = accounts_dir
    _state["scans"] = _prune(scans, now)
    return _state["scans"]


def _save(accounts_dir):
    """原子落盘。调用方需持有 _lock。写失败只丢持久化，不影响本轮查询。"""
    payload = {
        "version": STATE_VERSION,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "interval_hours": SCAN_INTERVAL / 3600.0,
        "scans": list(_state["scans"].values()),
    }
    path = _state_path(accounts_dir)
    tmp = path + ".tmp"
    try:
        os.makedirs(accounts_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def reset_state():
    """丢掉模块内缓存，模拟「换一个进程再看这份状态文件」（测试用）。"""
    with _lock:
        _state["dir"] = None
        _state["scans"] = {}


# --------------------------------------------------------------- 目标选取

def _due_at(row):
    """这条配额线下一次该查的时刻。

    last_scan=0 表示从没查过，此时不能用它推导——0 + 间隔是个 1970 年的
    时间戳，混进 max() 里会把真正的 next_at 挤掉。
    """
    last_scan = float(row.get("last_scan") or 0)
    base = last_scan + SCAN_INTERVAL if last_scan else 0.0
    return max(base, float(row.get("next_at") or 0))


def _seed(scans, wanted, now):
    """给从未登记过的组合写一条占位，并把首次查询排开。

    「刚勾上一堆模型」时每个组合都没记录，若不排开，网关会在一瞬间把所有
    请求打上去。这里按登记顺序给每个新组合一个递增的起点：第一个几分钟内
    就查，后面的顺着铺开（上限 1 小时）。
    """
    delay = FIRST_DELAY
    for uid, model in wanted:
        key = _key(uid, model)
        if key in scans:
            continue
        row = _new_row(uid, model, now)
        row["next_at"] = now + delay
        scans[key] = row
        delay = min(delay + FIRST_STAGGER, FIRST_DELAY_MAX)


def due_targets(pool, accounts_dir=None, now=None, limit=BATCH_LIMIT,
                force=False):
    """本轮该查的 (账号, 模型) 列表，按应查时刻升序。

   三条过滤按顺序：**退出观察列表 / 已经计入登记的 429 记录 / 还没到下一次
   查询时刻**。最后一条是默认轮询的正主：本地有窗口记录的组合不会出现在这
   里。`force=True` 只跳过最后一条（面板「立即查询」按钮），前两条不动——
   已经登记过 429 窗口的配额线永远不该被额外撞一次。
    """
    if pool is None:
        return []
    accounts_dir = accounts_dir or (getattr(pool, "dir", "") or "")
    if not accounts_dir:
        return []
    now = time.time() if now is None else now
    models = wb_settings.quota_scan_models(accounts_dir)
    if not models:
        return []
    watched = set(models)

    pool_accounts = [a for a in (getattr(pool, "accounts", []) or [])
                     if a.enabled and a.access_token]
    if not pool_accounts:
        return []

    wanted = [(str(a.uid), m) for m in models for a in pool_accounts]
    with _lock:
        scans = _load(accounts_dir, now)
        _seed(scans, wanted, now)

    by_uid = {}
    for account in pool_accounts:
        by_uid.setdefault(str(account.uid), account)

    out = []
    with _lock:
        for key, row in scans.items():
            model = str(row.get("model") or "")
            if model not in watched:
                continue
            account = by_uid.get(str(row.get("uid") or ""))
            if account is None:
                continue  # 账号已不在池中：留着记录，等它回来
            if wb_window_probe.has_window(accounts_dir, account.uid, model):
                continue  # 已有 429 记录 -> 交给窗口探针处理
            if account.throttle_wait(model) > 0:
                continue  # 正在冷却（含未给出重置时刻的 429）
            if not force and now < _due_at(row):
                continue
            out.append((_due_at(row), account, model))

    out.sort(key=lambda item: (item[0], str(item[2])))
    return [(account, model) for _, account, model in out][:max(1, int(limit or BATCH_LIMIT))]


# --------------------------------------------------------------- 执行

def run_due(pool, log=None, limit=BATCH_LIMIT, now=None, force=False):
    """查询所有到点的组合，返回实际发出的请求数。

    阻塞式实现，由调用方决定放在哪个线程里跑（wb_scheduler 用独立线程）。
    同一时刻只允许一轮，重入直接返回 0。`force=True` 忽略查询间隔，供面板
    「立即查询」使用。
    """
    log = log or (lambda msg: None)
    accounts_dir = getattr(pool, "dir", "") or ""
    if pool is None or not accounts_dir:
        return 0
    if not _run_lock.acquire(blocking=False):
        return 0
    try:
        stamp = time.time() if now is None else now
        targets = due_targets(pool, accounts_dir, now=stamp, limit=limit,
                              force=force)
        sent = 0
        for account, model in targets:
            sent += 1
            try:
                _scan(accounts_dir, account, model, log, now=stamp)
            except Exception as exc:
                log("额度查询：账号 [%s] 的 %s 执行异常: %s"
                    % (str(account.uid)[:8], model, exc))
            time.sleep(SCAN_GAP)
        return sent
    finally:
        _run_lock.release()


def _scan(accounts_dir, account, model, log, now=None):
    """对一个组合发一次查询，并按结果更新记录。返回 result 字符串。"""
    uid = str(account.uid)
    uid8 = uid[:8]
    now = time.time() if now is None else now
    # Token 快过期就先刷新，否则查询只会拿回 401，白跑一趟。
    exp = account.expires_at or wb_accounts.jwt_exp(account.access_token)
    if exp and (exp - now) < 120:
        try:
            account.refresh()
        except Exception:
            pass

    try:
        ok, reset_at, detail = wb_window_probe.probe(account, model)
    except Exception as exc:
        ok, reset_at, detail = False, None, str(exc)[:200]

    if ok:
        # 请求通了：这条配额线还没被打满。顺便解除可能残留的模型冷却。
        account.clear_model_cooldown(model)
        _record(accounts_dir, uid, model, now, last_scan=now, result="ok",
                detail="")
        log("✓ 额度查询：账号 [%s] 的 %s 正常（未达限额）" % (uid8, model))
        return "ok"

    row = _row(accounts_dir, uid, model) or {}
    attempts = int(row.get("attempts") or 0)

    if reset_at and reset_at > now + 5:
        # 这条线已经被打满了。除了交给窗口探针抢占新窗口，还要立刻记
        # 冷却，否则真实请求会继续撞进同一条已满的配额线。
        wait = max(1.0, reset_at - now)
        account.note_error("HTTP 429 (model throttled)", model=model,
                           until=reset_at, cooldown=wait)
        scheduled = False
        try:
            if wb_window_probe.schedule(accounts_dir, uid, model, reset_at,
                                        now=now):
                scheduled = True
        except Exception:
            scheduled = False
        _record(accounts_dir, uid, model, now, last_scan=now, attempts=0,
                result="throttled", detail=detail)
        if scheduled:
            log("额度查询：账号 [%s] 的 %s 已达限额，已登记窗口（%s 到点）"
                % (uid8, model,
                   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset_at))))
        else:
            log("额度查询：账号 [%s] 的 %s 已达限额，冷却至 %s"
                % (uid8, model,
                   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(reset_at))))
        return "throttled"

    attempts += 1
    result = "throttled" if "429" in str(detail or "") else "error"
    if attempts >= MAX_ATTEMPTS:
        # 连续失败说明不是一次抖动：推迟到下一个周期，别一直重试。
        _record(accounts_dir, uid, model, now, last_scan=now, attempts=0,
                result=result, detail=detail)
        log("额度查询：账号 [%s] 的 %s 连续 %d 次失败，推迟到下一个周期（%s）"
            % (uid8, model, attempts, detail or "无详情"))
        return result
    _record(accounts_dir, uid, model, now, attempts=attempts,
            next_at=now + RETRY_DELAY, result=result, detail=detail)
    log("额度查询：账号 [%s] 的 %s 未完成检查（%s），%d 秒后重试 (%d/%d)"
        % (uid8, model, detail or "无详情", int(RETRY_DELAY), attempts,
           MAX_ATTEMPTS))
    return result


def _row(accounts_dir, uid, model):
    with _lock:
        row = _load(accounts_dir).get(_key(uid, model))
        return dict(row) if row else None


def _record(accounts_dir, uid, model, now, last_scan=None, next_at=0.0,
            attempts=None, result="", detail=""):
    """写回一条记录。`last_scan` 为空表示这次不算「查过」。"""
    with _lock:
        scans = _load(accounts_dir, now)
        key = _key(uid, model)
        row = scans.get(key) or _new_row(uid, model, now)
        if last_scan:
            row["last_scan"] = max(float(row.get("last_scan") or 0),
                                   float(last_scan))
        row["next_at"] = float(next_at or 0)
        row["attempts"] = int(row.get("attempts") or 0) if attempts is None \
            else int(attempts)
        row["last_try"] = now
        row["last_result"] = str(result or "")[:40]
        row["last_detail"] = str(detail or "")[:200]
        scans[key] = row
        _save(accounts_dir)
        return row


# --------------------------------------------------------------- 展示

def snapshot(accounts_dir=None, now=None, limit=20):
    """只读快照，按下一次应查时刻升序，给看板与调度器状态用。"""
    if not accounts_dir:
        return []
    now = time.time() if now is None else now
    with _lock:
        rows = [dict(r) for r in _load(accounts_dir, now).values()]
    watched = set(wb_settings.quota_scan_models(accounts_dir))
    out = []
    for row in rows:
        model = str(row.get("model") or "")
        due = _due_at(row)
        uid = str(row.get("uid") or "")
        out.append({
            "uid": uid,
            "uid8": uid[:8],
            "model": model,
            "watched": model in watched,
            "pending": wb_window_probe.has_window(accounts_dir, uid, model),
            "last_scan": float(row.get("last_scan") or 0),
            "last_result": str(row.get("last_result") or ""),
            "last_detail": str(row.get("last_detail") or ""),
            "due_in": max(0, int(due - now)),
        })
    out.sort(key=lambda r: (r["due_in"], r["model"], r["uid"]))
    return out[:max(1, int(limit or 20))]


def drop(accounts_dir, uid, model):
    """撤销一条观察记录（账号已不在池中时使用）。"""
    with _lock:
        scans = _load(accounts_dir)
        if scans.pop(_key(str(uid or ""), str(model or "")), None) is not None:
            _save(accounts_dir)
            return True
    return False
