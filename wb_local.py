#!/usr/bin/env python3
"""Local session usage: aggregate the request volume of local agent clients.

The dashboard's "local" option covers every agent session running on this machine
(OpenCode / Claude Code / Codex, ...), and tells apart the requests that went
through this project's gateway from the ones that never touched it.

Why this module exists: a request that bypasses the gateway never lands in this
project's usage log (usage/usage.jsonl), so no provider filter (WorkBuddy /
Qoder / Trae / Egg) can ever cover it. The clients keep their own session
history, so the numbers are read from there instead - each client writes down
its own token accounting, and the only thing this module adds is the gateway
classification (did this request go through us?) and the folding into the same
payload shape the dashboard already knows how to draw.

Everything here is read-only, and every step degrades to "no data" rather than
to an error: a missing session store, a locked database, a format change - none
of them may take the data board down with them.
"""
import datetime
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse

try:
    import wb_pricing
except Exception:  # pragma: no cover - pricing is optional
    wb_pricing = None

# The provider ids wb_agents.py writes into a client's config: a request that
# carries one of them went through this project's gateway, whatever the base
# url says (the url may point at the LAN address of this very machine).
GATEWAY_PROVIDER_IDS = ("wb-proxy",)

CLIENT_LABELS = {
    "opencode": "OpenCode",
    "claude": "Claude Code",
    "codex": "Codex",
}

# Scope: which requests the "local" view counts.
SCOPE_ALL = "all"              # every local session request
SCOPE_NON_GATEWAY = "nongw"    # only the ones that bypassed this gateway

# The gateway's own "all" range looks back this far; the local view uses the
# same horizon so its "total" means the same thing as the gateway's.
HORIZON_DAYS = 365

# How long a scan is reused before it is redone. The dashboard polls every five
# seconds; the refresh only reads what is new (see _collect), so 30s is both
# cheap and fresh enough.
SCAN_TTL = 30.0

_store_lock = threading.Lock()
_store = {"rows": [], "horizon": None, "cursors": {}, "at": 0.0, "scan_ms": 0,
          "sources": []}

_gateway_port = None


# ---------------------------------------------------------------------------
# 本项目网关的判定
# ---------------------------------------------------------------------------
def set_gateway_port(port):
    """Tell the module which port this process listens on.

    Called once at startup: a client whose base url points at that port on the
    loopback interface is talking to us, and its requests are already counted
    in the gateway's own view.
    """
    global _gateway_port
    try:
        _gateway_port = int(port) if port else None
    except (TypeError, ValueError):
        _gateway_port = None


def gateway_port():
    return _gateway_port


def _home():
    return os.path.expanduser("~")


def _is_gateway_url(url):
    """True when `url` points at this project's own gateway."""
    if not url:
        return False
    try:
        parts = urllib.parse.urlsplit(str(url).strip())
    except Exception:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if not _gateway_port or port != _gateway_port:
        return False
    # 回环地址（含 IPv4 映射的 127.x.x.x）才算是「本机的本网关」。
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    return host.startswith("127.")


def _is_gateway_provider(provider_id, base_url):
    """True when a request made through this provider went through us."""
    pid = str(provider_id or "").strip().lower()
    if pid in GATEWAY_PROVIDER_IDS:
        return True
    return _is_gateway_url(base_url)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _read_json(path):
    """Read a JSON file; JSONC (comments) is tolerated. None on any problem."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    stripped = re.sub(r"(^|\s)//[^\n]*", r"\1", stripped)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    try:
        return json.loads(stripped)
    except Exception:
        return None


def _as_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(text):
    """ISO-8601 timestamp -> epoch seconds (None when unparseable)."""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except Exception:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except Exception:
        return None


def _iso_stamp(at):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------
def _opencode_dir():
    env = os.environ.get("OPENCODE_DATA_DIR") or ""
    if env:
        return env
    xdg = os.environ.get("XDG_DATA_HOME") or ""
    base = xdg if xdg else os.path.join(_home(), ".local", "share")
    return os.path.join(base, "opencode")


def _opencode_db_path():
    return os.path.join(_opencode_dir(), "opencode.db")


def _opencode_config_paths():
    home = _home()
    paths = []
    env = os.environ.get("OPENCODE_CONFIG") or ""
    if env:
        paths.append(env)
    paths.append(os.path.join(home, ".config", "opencode", "opencode.json"))
    paths.append(os.path.join(home, ".config", "opencode", "opencode.jsonc"))
    return paths


def _opencode_provider_urls():
    """{provider id: base url} from OpenCode's own config."""
    out = {}
    for path in _opencode_config_paths():
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        providers = data.get("provider")
        if not isinstance(providers, dict):
            continue
        for pid, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            options = entry.get("options")
            if not isinstance(options, dict):
                continue
            base = options.get("baseURL") or options.get("baseUrl") or ""
            if base and str(pid) not in out:
                out[str(pid)] = str(base)
    return out


def _opencode_session_dirs(con):
    """{session id: working directory} - for the row tooltip."""
    out = {}
    for table in ("session_v2", "session"):
        try:
            cur = con.execute("select id, directory from %s" % table)
        except Exception:
            continue
        try:
            for sid, directory in cur:
                if sid and directory and sid not in out:
                    out[sid] = str(directory)
        except Exception:
            continue
    return out


# 两张表结构不同：新版把消息放在 session_message（模型在 $.model 下），
# 旧版放在 message（模型在 $.modelID）。两张都读，历史才不会被截断。
#
# 只取五段 JSON 而不是十个字段：json_extract 每调一次就把整份 data 重新解析
# 一遍，而 data 里塞的是消息正文（工具输出往往几十 KB），十次解析就是十倍代价。
# 取成两个小对象（tokens / model）后在 Python 里拆，扫描快了六倍。
_OC_SELECT = (
    "id, time_created, session_id, json_extract(data,'$.tokens'), "
    "json_extract(data,'$.model'), json_extract(data,'$.modelID'), "
    "json_extract(data,'$.providerID'), json_extract(data,'$.cost'), "
    "json_extract(data,'$.time.completed'), json_extract(data,'$.error.name')"
)


def _scan_opencode(since_ms):
    """Rows from OpenCode's own session database.

    Returns (rows, cursor): the cursor is the newest message timestamp seen,
    so the next refresh only has to read what came after it.
    """
    path = _opencode_db_path()
    if not os.path.isfile(path):
        return [], since_ms
    providers = _opencode_provider_urls()
    uri = "file:%s?mode=ro" % path.replace("\\", "/")
    try:
        con = sqlite3.connect(uri, uri=True, timeout=3.0)
    except Exception:
        return [], since_ms
    rows = []
    cursor = since_ms
    try:
        try:
            con.execute("pragma busy_timeout=3000")
        except Exception:
            pass
        dirs = _opencode_session_dirs(con)
        for table in ("session_message", "message"):
            sql = "select %s from %s where time_created > ?" % (_OC_SELECT, table)
            try:
                cur = con.execute(sql, (int(since_ms),))
            except Exception:
                continue
            for rec in cur:
                if rec[1] and rec[1] > cursor:
                    cursor = rec[1]
                row = _opencode_row(rec, providers, dirs)
                if row is not None:
                    rows.append(row)
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass
    return rows, cursor


def _opencode_row(rec, providers, dirs):
    (msg_id, created, session_id, tokens_json, model_json, model_v1, provider_v1,
     cost, completed, error) = rec
    at = _as_float(created, 0.0) / 1000.0
    if not at:
        return None
    tokens = _json_obj(tokens_json)
    model_info = _json_obj(model_json)
    model = model_info.get("id") or model_v1 or "(unknown)"
    provider = model_info.get("providerID") or provider_v1 or ""
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    prompt = (_as_int(tokens.get("input")) + _as_int(cache.get("read"))
              + _as_int(cache.get("write")))
    output = _as_int(tokens.get("output"))
    reasoning = _as_int(tokens.get("reasoning"))
    cached = _as_int(cache.get("read"))
    total = prompt + output + reasoning
    if not total and not error:
        # 没有 token 也没有报错：这不是一次请求（用户消息、被中断的空回合）。
        return None
    finished = _as_float(completed, 0.0) / 1000.0
    elapsed = None
    if finished and finished > at:
        elapsed = int((finished - at) * 1000)
    base = providers.get(str(provider or ""))
    return {
        "at": at,
        "rid": "opencode:%s" % (msg_id or session_id or created),
        "client": "opencode",
        "model": str(model),
        "gateway": _is_gateway_provider(provider, base),
        "ok": not error,
        "prompt_tokens": prompt,
        "completion_tokens": output + reasoning,
        "reasoning_tokens": reasoning,
        "cached_tokens": cached,
        "total_tokens": total,
        "cost_usd": _as_float(cost, 0.0),
        "elapsed_ms": elapsed,
        "session": str(session_id or ""),
        "project": dirs.get(str(session_id or ""), ""),
    }


def _json_obj(text):
    """A JSON object that came out of json_extract (or None)."""
    if not text:
        return {}
    if isinstance(text, dict):
        return text
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}



# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------
def _claude_settings_url():
    """The base url Claude Code is pointed at (its own settings file)."""
    for path in (os.path.join(_home(), ".claude", "settings.json"),
                 os.path.join(_home(), ".claude", "settings.local.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        env = data.get("env")
        if isinstance(env, dict):
            base = env.get("ANTHROPIC_BASE_URL") or env.get("ANTHROPIC_API_URL")
            if base:
                return str(base)
    return ""


def _scan_claude(since_ts):
    """Rows from Claude Code's per-session JSONL files."""
    base = os.path.join(_home(), ".claude", "projects")
    if not os.path.isdir(base):
        return [], 0
    gateway = _is_gateway_url(_claude_settings_url())
    rows = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            rows.extend(_scan_claude_file(os.path.join(root, name), since_ts, gateway))
    return rows, 0


def _scan_claude_file(path, since_ts, gateway):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                at = _parse_iso(rec.get("timestamp"))
                if at is None or at < since_ts:
                    continue
                prompt = (_as_int(usage.get("input_tokens"))
                          + _as_int(usage.get("cache_read_input_tokens"))
                          + _claude_cache_write(usage))
                output = _as_int(usage.get("output_tokens"))
                cached = _as_int(usage.get("cache_read_input_tokens"))
                if not (prompt or output):
                    continue
                out.append({
                    "at": at,
                    "rid": "claude:%s:%d" % (os.path.basename(path), index),
                    "client": "claude",
                    "model": str(msg.get("model") or "(unknown)"),
                    "gateway": gateway,
                    "ok": True,
                    "prompt_tokens": prompt,
                    "completion_tokens": output,
                    "reasoning_tokens": 0,
                    "cached_tokens": cached,
                    "total_tokens": prompt + output,
                    "cost_usd": 0.0,
                    "elapsed_ms": None,
                    "session": str(rec.get("sessionId") or ""),
                    "project": str(rec.get("cwd") or ""),
                })
    except Exception:
        return out
    return out



def _claude_cache_write(usage):
    """Cache-creation tokens (the top level field, or the sum of the parts)."""
    top = _as_int(usage.get("cache_creation_input_tokens"))
    if top:
        return top
    detail = usage.get("cache_creation")
    if isinstance(detail, dict):
        return sum(_as_int(v) for v in detail.values() if isinstance(v, (int, float)))
    return 0


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------
def _codex_base_url():
    path = os.path.join(_home(), ".codex", "config.toml")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return ""
    match = re.search(r'base_url\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""


def _scan_codex(since_ts):
    """Rows from Codex's rollout files."""
    base = os.path.join(_home(), ".codex", "sessions")
    if not os.path.isdir(base):
        return [], 0
    gateway = _is_gateway_url(_codex_base_url())
    rows = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            rows.extend(_scan_codex_file(os.path.join(root, name), since_ts, gateway))
    return rows, 0


def _scan_codex_file(path, since_ts, gateway):
    out = []
    model = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "turn_context" or rec.get("type") == "turn_context":
                    model = str(payload.get("model") or model or "")
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue
                at = _parse_iso(rec.get("timestamp"))
                if at is None or at < since_ts:
                    continue
                prompt = _as_int(usage.get("input_tokens")) + _as_int(usage.get("cached_input_tokens"))
                output = _as_int(usage.get("output_tokens"))
                reasoning = _as_int(usage.get("reasoning_output_tokens"))
                cached = _as_int(usage.get("cached_input_tokens"))
                if not (prompt or output):
                    continue
                out.append({
                    "at": at,
                    "rid": "codex:%s:%d" % (os.path.basename(path), index),
                    "client": "codex",
                    "model": str(model or "(unknown)"),
                    "gateway": gateway,
                    "ok": True,
                    "prompt_tokens": prompt,
                    "completion_tokens": output + reasoning,
                    "reasoning_tokens": reasoning,
                    "cached_tokens": cached,
                    "total_tokens": prompt + output + reasoning,
                    "cost_usd": 0.0,
                    "elapsed_ms": None,
                    "session": os.path.splitext(os.path.basename(path))[0],
                    "project": "",
                })
    except Exception:
        return out
    return out



# ---------------------------------------------------------------------------
# 收集与缓存
# ---------------------------------------------------------------------------
def _client_present(name):
    """True when this client keeps a session store on this machine."""
    if name == "opencode":
        return os.path.isfile(_opencode_db_path())
    if name == "claude":
        return os.path.isdir(os.path.join(_home(), ".claude", "projects"))
    if name == "codex":
        return os.path.isdir(os.path.join(_home(), ".codex", "sessions"))
    return False


def _scan_all(horizon_ts, cursors):
    """Every local row since `horizon_ts`, from every client that has one.

    `cursors` is the per-client "read up to here" mark: a client that can be
    read incrementally (OpenCode's database) only returns what is new, the
    others are re-read from the start - their files are small.
    """
    rows = []
    sources = []
    new_cursors = {}
    for name, fn in (("opencode", _scan_opencode),
                     ("claude", _scan_claude),
                     ("codex", _scan_codex)):
        cursor = cursors.get(name)
        if cursor is None:
            # 首次（或整表重来）：从起点读。
            part, new_cursor = fn(horizon_ts * 1000 if name == "opencode" else horizon_ts)
        else:
            part, new_cursor = fn(cursor)
        if _client_present(name):
            sources.append(name)
        rows.extend(part)
        new_cursors[name] = new_cursor
    return rows, sources, new_cursors


def _collect(since_ts):
    """Rows since `since_ts`, reusing the last scan for a short while.

    The scan horizon is the earlier of the requested start and the gateway's own
    "all" range, so the total shown next to a window always means the same
    thing as the gateway's. A refresh only reads what is new (the per-client
    cursor), so the steady-state cost is a few milliseconds instead of a full
    pass over the client's session database.
    """
    now = time.time()
    horizon = min(_as_float(since_ts, now), now - HORIZON_DAYS * 86400)
    with _store_lock:
        stale = (now - _store["at"]) > SCAN_TTL
        covered = _store["horizon"] is not None and _store["horizon"] <= horizon + 1
        if not stale and covered:
            return list(_store["rows"].values()), _store["sources"], _store["scan_ms"]
        keep = dict(_store["rows"]) if covered else {}
        cursors = dict(_store["cursors"]) if covered else {}
    started = time.time()
    try:
        fresh, sources, new_cursors = _scan_all(horizon, cursors)
    except Exception:
        fresh, sources, new_cursors = [], [], {}
    for row in fresh:
        rid = row.get("rid")
        if not rid:
            continue
        keep[rid] = row
    rows = sorted(keep.values(), key=lambda r: r.get("at") or 0.0)
    scan_ms = int((time.time() - started) * 1000)
    with _store_lock:
        _store.update({"rows": keep, "horizon": horizon, "cursors": new_cursors,
                       "at": time.time(), "scan_ms": scan_ms, "sources": sources})
    return rows, sources, scan_ms



def _scope_rows(rows, scope, since=None, until=None):
    """Apply the window and the scope filter."""
    out = []
    for row in rows:
        at = row.get("at") or 0.0
        if since is not None and at < since:
            continue
        if until is not None and at > until:
            continue
        if scope != SCOPE_ALL and row.get("gateway"):
            continue
        out.append(row)
    return out


def normalize_scope(value):
    """The scope parameter: only the non-gateway requests, or everything."""
    text = str(value or "").strip().lower()
    if text in ("all", "include_gateway", "gateway"):
        return SCOPE_ALL
    return SCOPE_NON_GATEWAY


#: 客户端筛选的别名：看板的网关口径用 claude-code / codex / opencode，本地会话
#: 记录里写的是 claude / codex / opencode，两边都要能筛同一个客户端。
CLIENT_ALIASES = {
    "claude-code": "claude",
    "claude_code": "claude",
    "claudecode": "claude",
}


def normalize_client(value):
    """The client filter: None means every local client."""
    text = str(value or "").strip().lower()
    if not text or text == "all":
        return None
    return CLIENT_ALIASES.get(text, text)


def _client_rows(rows, client):
    """Rows produced by one client (no filter = every client)."""
    if not client:
        return rows
    return [row for row in rows if (row.get("client") or "local") == client]


def fingerprint(since=None, until=None, scope=SCOPE_NON_GATEWAY, client=None):
    """A short token describing the current local data.

    The panel polls every five seconds, so the payload needs a validator that
    only changes when the data does: how many requests, how many tokens, and
    when the last one happened. The client filter joins the token: two clients
    must never share one validator.
    """
    rows, _sources, _scan_ms = _collect(since if since is not None else 0)
    window = _client_rows(_scope_rows(rows, scope, since, until), client)
    count = len(window)
    tokens = 0
    last = 0.0
    for row in window:
        tokens += row.get("total_tokens") or 0
        at = row.get("at") or 0.0
        if at > last:
            last = at
    return "%d:%d:%d" % (count, tokens, int(last))


# ---------------------------------------------------------------------------
# 折叠（与网关的统计口径一致）
# ---------------------------------------------------------------------------
def _new_stat():
    return {
        "requests": 0, "errors": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
        "cached_tokens": 0, "total_tokens": 0,
        "credit": 0.0, "cost_cny": 0.0,
        "ttft_sum": 0.0, "ttft_n": 0,
        "speed_sum": 0.0, "speed_n": 0,
        "elapsed_sum": 0.0, "elapsed_n": 0,
    }


def _finalize(stat):
    prompt = stat["prompt_tokens"]
    cached = stat["cached_tokens"]
    out = stat["completion_tokens"]
    reasoning = stat["reasoning_tokens"]
    stat["cache_hit_pct"] = round((cached / prompt * 100), 1) if prompt > 0 else 0.0
    stat["reasoning_ratio"] = round((reasoning / out * 100), 1) if out > 0 else 0.0
    stat["ttft_ms_avg"] = round(stat["ttft_sum"] / stat["ttft_n"]) if stat["ttft_n"] > 0 else 0
    stat["speed_avg"] = round(stat["speed_sum"] / stat["speed_n"], 1) if stat["speed_n"] > 0 else 0.0
    stat["elapsed_ms_avg"] = round(stat["elapsed_sum"] / stat["elapsed_n"]) if stat["elapsed_n"] > 0 else 0
    return stat


def _price_model(model):
    """The model id the price table knows, or None.

    Local clients spell model ids the way their own provider does
    ("minimax/minimax-m3.1-flash:free"), while the price table is keyed by the
    bare model id. Try the id as it is, then without the provider prefix, then
    without the ":free" suffix - the first one the table knows wins.
    """
    mid = str(model or "").strip()
    if not mid:
        return None
    candidates = [mid]
    if "/" in mid:
        candidates.append(mid.rsplit("/", 1)[-1])
    base = candidates[-1]
    if ":" in base:
        candidates.append(base.split(":", 1)[0])
    try:
        models = (wb_pricing.load_pricing().get("models") or {}) if wb_pricing else {}
    except Exception:
        return None
    for cand in candidates:
        if cand and cand in models:
            return cand
    return None


def _row_cost(row):
    """Equivalent cost in CNY, priced like the gateway does (OpenRouter list)."""
    if wb_pricing is None:
        return 0.0
    mid = _price_model(row.get("model"))
    if not mid:
        return 0.0
    try:
        cost = wb_pricing.compute_row({
            "model": mid,
            "at": row.get("at"),
            "prompt_tokens": row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
            "cached_tokens": row.get("cached_tokens"),
        })
    except Exception:
        return 0.0
    if not cost.get("known"):
        return 0.0
    return _as_float(cost.get("cny"), 0.0)


def _fold(stat, row, cost_cny):
    """One row into one stat object (the gateway's own accounting rules)."""
    if row.get("ok"):
        stat["requests"] += 1
    else:
        stat["errors"] += 1
    stat["prompt_tokens"] += row.get("prompt_tokens") or 0
    stat["completion_tokens"] += row.get("completion_tokens") or 0
    stat["reasoning_tokens"] += row.get("reasoning_tokens") or 0
    stat["cached_tokens"] += row.get("cached_tokens") or 0
    stat["total_tokens"] += row.get("total_tokens") or 0
    stat["cost_cny"] += cost_cny
    elapsed = row.get("elapsed_ms")
    if elapsed:
        stat["elapsed_sum"] += elapsed
        stat["elapsed_n"] += 1


def _bump_models(target, model, row, cost_cny):
    """One row into one {model: {...}} bucket (successful requests only)."""
    if not row.get("ok"):
        return
    entry = target.get(model)
    if entry is None:
        entry = target[model] = {"requests": 0, "tokens": 0, "reasoning": 0, "cost_cny": 0.0}
    entry["requests"] += 1
    entry["tokens"] += row.get("total_tokens") or 0
    entry["reasoning"] += row.get("reasoning_tokens") or 0
    entry["cost_cny"] += cost_cny


# ---------------------------------------------------------------------------
# 载荷
# ---------------------------------------------------------------------------
def _usd_cny():
    if wb_pricing is None:
        return 7.1
    try:
        return _as_float(wb_pricing.usd_cny(), 7.1)
    except Exception:
        return 7.1


def analytics(since=None, until=None, scope=SCOPE_NON_GATEWAY, client=None):
    """The /usage/analytics payload for the local view."""
    rows, sources, scan_ms = _collect(since if since is not None else 0)
    # 客户端下拉的选项来自「全部客户端」那一份：筛掉一个客户端之后，仍然要能
    # 切回别的客户端，所以选项按未过滤的行统计；KPI / 曲线 / 明细用过滤后的行。
    clients_window = _scope_rows(rows, scope, since, until)
    clients_total = _scope_rows(rows, scope, None, None)
    window = _client_rows(clients_window, client)
    total = _client_rows(clients_total, client)
    # 窗口内「经本网关 / 没走网关」的拆分：口径与当前所选区间一致，面板拿它
    # 说明「本地」这一格到底装了什么（这两个数与 scope 无关，切口径时不变）。
    window_all = _client_rows(_scope_rows(rows, SCOPE_ALL, since, until), client)

    win_stat = _new_stat()
    all_stat = _new_stat()
    win_accts, all_accts = {}, {}
    win_models, all_models = {}, {}
    cost_usd = 0.0
    gateway_requests = 0
    non_gateway_requests = 0
    for row in window_all:
        if row.get("gateway"):
            gateway_requests += 1
        else:
            non_gateway_requests += 1
    for row in window:
        cost = _row_cost(row)
        _fold(win_stat, row, cost)
        client = row.get("client") or "local"
        entry = win_accts.get(client)
        if entry is None:
            entry = win_accts[client] = {
                "uid": client,
                "nickname": CLIENT_LABELS.get(client, client),
                "realm": "",
                "domain": "",
                "window": _new_stat(),
                "all_time": _new_stat(),
                "window_models": {},
                "all_models": {},
            }
        _fold(entry["window"], row, cost)
        _bump_models(entry["window_models"], row.get("model") or "(unknown)", row, cost)
        model = win_models.get(row.get("model") or "(unknown)")
        if model is None:
            model = win_models[row.get("model") or "(unknown)"] = {
                "model": row.get("model") or "(unknown)",
                "window": _new_stat(),
                "all_time": _new_stat(),
            }
        _fold(model["window"], row, cost)
        cost_usd += _as_float(row.get("cost_usd"), 0.0)
    for row in total:
        cost = _row_cost(row)
        _fold(all_stat, row, cost)
        client = row.get("client") or "local"
        entry = all_accts.get(client)
        if entry is None:
            entry = all_accts[client] = {
                "uid": client,
                "nickname": CLIENT_LABELS.get(client, client),
                "realm": "",
                "domain": "",
                "window": _new_stat(),
                "all_time": _new_stat(),
                "window_models": {},
                "all_models": {},
            }
        _fold(entry["all_time"], row, cost)
        _bump_models(entry["all_models"], row.get("model") or "(unknown)", row, cost)
        model = all_models.get(row.get("model") or "(unknown)")
        if model is None:
            model = all_models[row.get("model") or "(unknown)"] = {
                "model": row.get("model") or "(unknown)",
                "window": _new_stat(),
                "all_time": _new_stat(),
            }
        _fold(model["all_time"], row, cost)
    # 账号行（本地客户端）的 window_models 要跟 window 桶对得上。
    for client, entry in win_accts.items():
        other = all_accts.get(client)
        if other is not None:
            entry["all_models"] = other["all_models"]
            # 账号行是从窗口那份里拿的，全历史那一半只在 all_accts 里折过；
            # 不接过来，「全部时间」就永远是 0（明细弹窗会当成没有历史）。
            entry["all_time"] = other["all_time"]
    for model_id, entry in win_models.items():
        other = all_models.get(model_id)
        if other is not None:
            entry["all_time"] = other["all_time"]

    _finalize(win_stat)
    _finalize(all_stat)
    for entry in win_accts.values():
        _finalize(entry["window"])
        _finalize(entry["all_time"])
    for entry in win_models.values():
        _finalize(entry["window"])
        _finalize(entry["all_time"])
    # 本地客户端不记「每秒多少 token」，也不记首字延迟：没有样本就写 None，
    # 让面板显示「—」而不是一个看着像 0.0 的实测值。
    for stat in [win_stat, all_stat]:
        if not stat.get("speed_n"):
            stat["speed_avg"] = None
        if not stat.get("ttft_n"):
            stat["ttft_ms_avg"] = None
    for entry in list(win_accts.values()) + list(all_accts.values()):
        for half in ("window", "all_time"):
            stat = entry.get(half) or {}
            if not stat.get("speed_n"):
                stat["speed_avg"] = None
            if not stat.get("ttft_n"):
                stat["ttft_ms_avg"] = None
    for entry in list(win_models.values()) + list(all_models.values()):
        for half in ("window", "all_time"):
            stat = entry.get(half) or {}
            if not stat.get("speed_n"):
                stat["speed_avg"] = None
            if not stat.get("ttft_n"):
                stat["ttft_ms_avg"] = None

    # 每个客户端的请求数（窗口内 / 全部）。这两个数按未过滤的行统计：下拉要
    # 一直列出所有本机客户端，筛掉一个也还能切回去；数字本身给用户做对照。
    clients = {}
    for source, window_key, total_key in (
            (clients_window, "requests", "tokens"),
            (clients_total, "total_requests", "total_tokens")):
        for row in source:
            cid = row.get("client") or "local"
            info = clients.get(cid)
            if info is None:
                info = clients[cid] = {
                    "id": cid,
                    "label": CLIENT_LABELS.get(cid, cid),
                    "requests": 0, "tokens": 0,
                    "total_requests": 0, "total_tokens": 0,
                }
            info[window_key] += 1
            info[total_key] += row.get("total_tokens") or 0

    accts_list = sorted(win_accts.values(),
                        key=lambda a: (-a["window"]["total_tokens"], -a["all_time"]["total_tokens"]))
    models_list = sorted(win_models.values(),
                         key=lambda m: (-m["window"]["total_tokens"], -m["all_time"]["total_tokens"]))
    return {
        "window": {"since": since, "until": until},
        "realm": "local",
        "usd_cny": _usd_cny(),
        "summary": {"window": win_stat, "all_time": all_stat},
        "accounts": accts_list,
        "models": models_list,
        "keys": [],
        "model_rates": {},
        "local": {
            "scope": scope,
            "agent": client,
            "sources": sources,
            "scan_ms": scan_ms,
            "clients": sorted(clients.values(), key=lambda c: -(c.get("tokens") or 0)),
            "gateway_requests": gateway_requests,
            "non_gateway_requests": non_gateway_requests,
            "cost_usd": round(cost_usd, 6),
            "gateway_port": _gateway_port,
        },
    }


def timeseries(since=None, until=None, bucket_seconds=None, scope=SCOPE_NON_GATEWAY,
               client=None):
    """The /usage/timeseries payload for the local view."""
    rows, _sources, _scan_ms = _collect(since if since is not None else 0)
    window = _client_rows(_scope_rows(rows, scope, since, until), client)
    lo = _as_float(since, 0.0) or (min([r["at"] for r in window]) if window else time.time())
    hi = _as_float(until, 0.0) or (max([r["at"] for r in window]) if window else lo)
    span = max(1.0, hi - lo)
    if not bucket_seconds:
        if span <= 6 * 3600:
            step = 60
        elif span <= 14 * 86400:
            step = 3600
        else:
            step = 86400
    else:
        step = int(bucket_seconds)
    origin = _bucket_origin(lo, step)
    buckets = {}
    for row in window:
        at = row.get("at") or 0.0
        key = int((at - origin) // step)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = {
                "at": origin + key * step, "requests": 0, "errors": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "reasoning_tokens": 0, "cached_tokens": 0,
                "total_tokens": 0, "credit": 0.0,
            }
        if row.get("ok"):
            bucket["requests"] += 1
            bucket["prompt_tokens"] += row.get("prompt_tokens") or 0
            bucket["completion_tokens"] += row.get("completion_tokens") or 0
            bucket["reasoning_tokens"] += row.get("reasoning_tokens") or 0
            bucket["cached_tokens"] += row.get("cached_tokens") or 0
            bucket["total_tokens"] += row.get("total_tokens") or 0
        else:
            bucket["errors"] += 1
    return {
        "ok": True,
        "realm": "local",
        "bucket_seconds": step,
        "since": since,
        "until": until,
        "series": [buckets[key] for key in sorted(buckets)],
        "credits": [],
    }


def _bucket_origin(lo, step):
    """The grid the buckets are aligned to (local midnight / hour / minute)."""
    if step == 86400:
        lt = time.localtime(lo)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    if step == 3600:
        lt = time.localtime(lo)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1))
    if step == 60:
        return int(lo // 60) * 60
    return lo


def recent(limit=100, page=1, scope=SCOPE_NON_GATEWAY, client=None):
    """The /usage/recent payload for the local view."""
    rows, _sources, _scan_ms = _collect(0)
    window = _client_rows(_scope_rows(rows, scope, None, None), client)
    window.sort(key=lambda r: r.get("at") or 0.0, reverse=True)
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 100
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    total = len(window)
    total_pages = max(1, (total + limit - 1) // limit) if total else 1
    page = min(page, total_pages)
    start = (page - 1) * limit
    out = []
    for row in window[start:start + limit]:
        at = row.get("at") or 0.0
        out.append({
            "at": at,
            "iso": _iso_stamp(at),
            "model": row.get("model") or "(unknown)",
            "outcome": "completed" if row.get("ok") else "failed",
            "elapsed_ms": row.get("elapsed_ms") or 0,
            "prompt_tokens": row.get("prompt_tokens") or 0,
            "completion_tokens": row.get("completion_tokens") or 0,
            "reasoning_tokens": row.get("reasoning_tokens") or 0,
            "cached_tokens": row.get("cached_tokens") or 0,
            "total_tokens": row.get("total_tokens") or 0,
            "credit": 0.0,
            "cost_cny": round(_row_cost(row), 6),
            "account": row.get("client") or "local",
            "client": row.get("client") or "local",
            "client_label": CLIENT_LABELS.get(row.get("client") or "", row.get("client") or ""),
            "gateway": bool(row.get("gateway")),
            "project": row.get("project") or "",
            "session": row.get("session") or "",
        })
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "usd_cny": _usd_cny(),
        "rows": out,
    }


def _dist(values):
    """A percentile block in the gateway's shape (avg/p50/p90/p99/max/samples)."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    count = len(values)

    def pick(p):
        idx = min(count - 1, max(0, int(round((p / 100.0) * (count - 1)))))
        return values[idx]

    return {
        "avg": round(sum(values) / float(count), 1),
        "p50": pick(50),
        "p90": pick(90),
        "p99": pick(99),
        "max": values[-1],
        "samples": count,
    }


def perf(scope=SCOPE_NON_GATEWAY, client=None):
    """The /usage/perf payload for the local view, in the gateway's shape.

    Local clients record how long a turn took, not when its first token arrived,
    so ttft_ms (and the generation time split that depends on it) stay null:
    an empty number beats a wrong one. Generation speed is left null for the
    same reason - dividing tokens by the whole turn (queueing and tool waits
    included) would print a number that looks precise and is not. The rest maps
    one-to-one onto the gateway payload, so the panel's percentile fold needs
    no special case.
    """
    rows, _sources, _scan_ms = _collect(0)
    window = _client_rows(_scope_rows(rows, scope, None, None), client)
    elapsed = []
    cache = []
    ok = 0
    for row in window:
        if row.get("ok"):
            ok += 1
        seconds = (row.get("elapsed_ms") or 0) / 1000.0
        if seconds > 0:
            elapsed.append(row.get("elapsed_ms"))
        prompt = row.get("prompt_tokens") or 0
        if prompt > 0:
            cache.append((row.get("cached_tokens") or 0) / float(prompt) * 100.0)
    total = len(window)
    return {
        "ok": True,
        "realm": "local",
        "sampled": total,
        "success": ok,
        "errors": total - ok,
        "client_aborted": 0,
        "success_rate_pct": round(ok * 100.0 / total, 1) if total else None,
        "ttft_ms": None,
        "generation_ms": None,
        "wall_ms": _dist(elapsed),
        "tokens_per_sec": None,
        "cache_hit_pct": _dist(cache),
    }


def perf_rows(since=None, until=None, scope=SCOPE_NON_GATEWAY, client=None,
              limit=None):
    """Local requests as gateway-shaped rows for a merged perf sample.

    「全部提供商」要把网关的样本和本机非网关的样本放进同一次分位计算，
    不能拿两份已经算好的分位去加权（那就不再是原始样本的分位了）。这里把
    本地行摊成与 usage.jsonl 同名的字段，交给网关那条折叠逻辑一起算。

    `limit` 只留最近 N 条：网关的 perf 样本本来就是「日志末尾 N 行」，
    本地这一半也守同一个口径，否则一整年的本机会话会把样本带偏。
    """
    rows, _sources, _scan_ms = _collect(0)
    window = _client_rows(_scope_rows(rows, scope, since, until), client)
    window.sort(key=lambda r: r.get("at") or 0.0)
    if limit and len(window) > limit:
        window = window[-int(limit):]
    out = []
    for row in window:
        prompt = row.get("prompt_tokens") or 0
        hit = None
        if row.get("ok") and prompt > 0:
            hit = (row.get("cached_tokens") or 0) / float(prompt) * 100.0
        out.append({
            "at": row.get("at") or 0.0,
            "model": row.get("model") or "unknown",
            # 本地请求没有出口之分：给它一个自己的 realm，性能表里能看出
            # 这一行不是从网关走的。
            "realm": "local",
            "account": row.get("client") or "local",
            "outcome": "completed" if row.get("ok") else "failed",
            "elapsed_ms": row.get("elapsed_ms"),
            "cache_hit_pct": hit,
        })
    return out
