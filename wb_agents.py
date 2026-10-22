"""One-click agent integration for local AI clients.

Detects installed AI clients (Claude Code, Codex, OpenCode, DSH, Crush, ZCode,
Hermes), rewrites their configuration to point at this gateway, keeps a byte-exact
backup of every file it touches, and can restore the original state with one
call. Inspired by EasyCLIProxyAPI's agents mechanism.

The same module is runnable as a standalone configurator, for clients on a
machine that does not run the gateway (the panel's one-click setup only works
from the gateway host itself - a remote browser cannot write another
machine's config files). Run it where the clients are and point it at the
gateway over the network:

    python wb_agents.py list                    # what is installed here
    python wb_agents.py apply --client codex \
        --base-url http://gateway:8788/v1 --key sk-...
    python wb_agents.py restore --client codex

Only the Python standard library is required. The YAML/TOML editing is a
small, deliberately conservative text-level upserter: it never reformats
lines it does not own, so hand-written comments and ordering survive.
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

PROVIDER_ID = "wb-proxy"
PROVIDER_NAME = "WB Proxy"
CREDENTIAL_REF = "WB_PROXY_API_KEY"
#: 写进客户端配置的归属头。网关按它把用量记到具体客户端；老配置没有这个
#: 头时网关退回 User-Agent 识别（见 wb_proxy.detect_agent）。
AGENT_HEADER = "X-WB-Agent"

#: 模型名后面挂的短标签（写进 OpenCode 的模型表，客户端选择器里显示成
#: 「模型名 标签」）。标签按**当前提供商**给：WorkBuddy 挂 wb、Qoder 挂
#: qoder、Trae 挂 trae；选中鸡蛋时再按免费源细分——Kilo 免费池挂 kilo、
#: OpenCode Zen 免费车道挂 opencode-zen，操作者自建的供应商池挂池子名。
#: 以前一律挂 wb，Qoder / Trae / 鸡蛋的模型在选择器里也顶着 wb 的名字，
#: 看着像 WorkBuddy 的模型。
PROVIDER_TAGS = {
    "workbuddy": "wb",
    "qoder": "qoder",
    "trae": "trae",
    "egg": "egg",
}

#: 鸡蛋（免账号的免费网关）里各条免费源的短标签。
EGG_SOURCE_TAGS = {
    "zen": "opencode-zen",
    "kilo": "kilo",
}

# A config file bigger than this is almost certainly not the hand-edited
# settings file we expect, so refuse rather than risk corrupting user data.
MAX_CONFIG_BYTES = 8 * 1024 * 1024
# How many backups per file are kept; older ones are pruned after each apply.
BACKUP_KEEP = 10

STATE_FILE = "integration-state.json"
BACKUP_DIRNAME = "agent-backups"


class AgentConfigError(Exception):
    """Human-readable failure while probing, applying or restoring a client."""


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------

def _resolve_home(home=None):
    """Return the home directory base; injectable so tests stay hermetic."""
    if home:
        return os.path.abspath(home)
    return os.path.expanduser("~")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) > MAX_CONFIG_BYTES:
        raise AgentConfigError(
            "refusing to edit %s: file is %d bytes (limit %d)"
            % (path, len(data), MAX_CONFIG_BYTES))
    return data


def _read_text(path):
    """UTF-8 text of an existing file, or "" when the file does not exist.

    Decoded with utf-8-sig: a config file another tool wrote with a BOM would
    otherwise fail to parse (json.loads chokes on the leading U+FEFF) and the
    panel would report a broken client instead of just reading it.
    """
    try:
        data = _read_bytes(path)
    except FileNotFoundError:
        return ""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AgentConfigError("%s is not valid UTF-8: %s" % (path, exc))


# Every file this module writes carries the gateway API key (inline, or via the
# env reference that the sibling credentials file resolves), so a fresh one is
# created owner-only rather than with open()'s 0666 & ~umask - 0644 under the
# usual umask 022. DSH refuses to start when its credentials file is readable
# beyond its owner, and the same 0644 would expose every other client's key.
WRITE_MODE = 0o600


def _target_mode(path):
    """The mode to give `path` on the next write.

    `WRITE_MODE` for a new file, or for one whose current mode is wider than
    owner-only (or does not even let its owner read it back); otherwise the
    file's own mode, so a re-apply preserves a stricter permission the user set
    by hand instead of widening it back - 0600 -> 0600 and 0400 -> 0400, but
    0644 -> 0600.
    """
    try:
        current = os.stat(path).st_mode & 0o777
    except OSError:
        return WRITE_MODE
    if current & 0o077 or not current & 0o400:
        return WRITE_MODE
    return current


def _atomic_write(path, data):
    """tmp + os.replace so a crash cannot leave a half-written config.

    The temp file is created owner-only (see `_target_mode`) rather than with
    the umask's default, and settled on the resolved mode after writing. Any
    failure on the way - including the chmod or the replace - removes the temp
    file, so a retry never trips over a leftover it cannot write to.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    mode = _target_mode(path)
    try:
        os.unlink(tmp)  # a stale temp from a killed run may not be writable
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, WRITE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path, text):
    _atomic_write(path, text.encode("utf-8"))


def _atomic_write_bytes(path, data):
    _atomic_write(path, data)


def _prune_empty_parents(path, home=None):
    """Remove now-empty directories left behind by our own config writes.

    Only directories that are empty are removed, and the walk stops at the
    home directory, so a user's own (even empty) tree above the home is never
    touched and nothing that still holds files is ever deleted.
    """
    home = os.path.abspath(home or _resolve_home())
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        if not directory or os.path.abspath(directory) == home:
            return
        if not os.path.abspath(directory).startswith(home + os.sep):
            return
        try:
            os.rmdir(directory)  # fails unless empty
        except OSError:
            return
        directory = os.path.dirname(directory)


def _load_json(path, what):
    """Parse a JSON config file; missing file means an empty document."""
    text = _read_text(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AgentConfigError("%s is not valid JSON (%s): %s" % (what, path, exc))
    if not isinstance(data, dict):
        raise AgentConfigError("%s must be a JSON object: %s" % (what, path))
    return data


def _dump_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def deep_merge(base, patch):
    """Recursively merge patch into base; sibling keys outside patch survive.

    Returns a new dict; the inputs are not mutated.
    """
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Text-level YAML editor (subset; standard library only)
# ---------------------------------------------------------------------------

def _yaml_indent(line):
    return len(line) - len(line.lstrip(" "))


def _yaml_key(line):
    """The mapping key of a block-style `key: ...` line, or None.

    Inline values (anything after the colon) and comments are ignored. Lines
    that are not plain `word: value` mappings (list items, quoted keys) are
    outside the supported subset.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s|$)", line.lstrip(" "))
    if not m:
        return None
    return m.group(1)


def _yaml_block_end(lines, start, indent):
    """Index one past the block opened by the line at `start`.

    The block covers every following non-blank, non-comment line indented
    deeper than `indent`.
    """
    idx = start + 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and _yaml_indent(line) <= indent:
            break
        idx += 1
    return idx


def _yaml_insert_point(lines, start, end, indent):
    """Last content line inside [start, end), so blank/comment trailers that
    belong to the *next* section stay attached to it."""
    idx = end
    while idx > start and not lines[idx - 1].strip():
        idx -= 1
    return idx if idx > start else end


def _yaml_inline_to_block(lines, idx, key, inline_value):
    """Expand `key: <inline>` into `key:` plus one nested block line.

    Only a plain single-line flow mapping `{k: v, ...}` can be nested under
    the key; anything else is outside the supported subset.
    """
    indent = _yaml_indent(lines[idx])
    value = inline_value.strip()
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        out = [" " * indent + key + ":"]
        if inner:
            for part in inner.split(","):
                part = part.strip()
                if part:
                    out.append(" " * (indent + 2) + part)
        lines[idx:idx + 1] = out
        return
    if value == "":
        return
    raise AgentConfigError(
        "cannot nest keys under `%s: %s` - only an inline {..} mapping "
        "can be expanded" % (key, inline_value))


def yaml_upsert_inline(text, key_path, value):
    """Upsert a nested mapping key with an inline-flow (JSON-compatible) value.

    `key_path` is a dotted path (e.g. "llm-pi-ai.providers.wb-proxy"); the
    leaf is written as `wb-proxy: <json value>` at the right indentation.
    Lines outside the touched block are preserved byte for byte. When a key
    of the path is missing it is created; when a parent key currently holds
    an inline `{..}` mapping the mapping is expanded into a block first.
    Structures outside the supported subset raise AgentConfigError.
    """
    keys = [part.strip() for part in str(key_path).split(".") if part.strip()]
    if not keys:
        raise AgentConfigError("yaml_upsert_inline: empty key path")
    lines = text.split("\n")
    lines = _yaml_upsert_at(lines, 0, len(lines), 0, keys,
                            json.dumps(value, ensure_ascii=False))
    return "\n".join(lines)


def _yaml_upsert_at(lines, start, end, indent, keys, value_json):
    """Recursive block scanner for yaml_upsert_inline; returns new lines."""
    key = keys[0]
    idx = start
    while idx < end:
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == indent and _yaml_key(line) == key:
            after = line.split(":", 1)[1].strip()
            child_end = _yaml_block_end(lines, idx, indent)
            if len(keys) == 1:
                # Replace this leaf line, keeping its indentation.
                lines[idx] = " " * indent + key + ": " + value_json
                return lines
            if after:
                # Parent currently holds an inline value: expand `{..}` into
                # a block so the child has somewhere to live.
                _yaml_inline_to_block(lines, idx, key, after)
                child_end = _yaml_block_end(lines, idx, indent)
                lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                        indent + 2, keys[1:], value_json)
                return lines
            lines = _yaml_upsert_at(lines, idx + 1, child_end,
                                    indent + 2, keys[1:], value_json)
            return lines
        idx += 1
    # Key not found in this block: append it at the block's end.
    insert = _yaml_insert_point(lines, start, end, indent)
    if len(keys) == 1:
        new_lines = [" " * indent + key + ": " + value_json]
    else:
        new_lines = [" " * indent + keys[0] + ":"]
        new_lines.extend(
            _yaml_upsert_at([], 0, 0, indent + 2, keys[1:], value_json))
    lines[insert:insert] = new_lines
    return lines


def yaml_refs_upsert(text, key, value):
    """Upsert `key: value` inside the top-level `refs:` block.

    When the document has no `refs:` block yet, one is appended at the end.
    A `refs:` line that already holds an inline value (e.g. `refs: {}`) is
    outside the supported subset and raises AgentConfigError instead of
    being silently mangled.
    """
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == 0 and _yaml_key(line) == "refs":
            after = line.split(":", 1)[1].strip()
            if after:
                raise AgentConfigError(
                    "cannot upsert into `refs: %s` - only a block-style "
                    "refs: mapping is supported" % after)
            end = _yaml_block_end(lines, idx, 0)
            j = idx + 1
            while j < end:
                if lines[j].strip() and _yaml_indent(lines[j]) == 2 \
                        and _yaml_key(lines[j]) == key:
                    lines[j] = "  %s: %s" % (key, value)
                    return "\n".join(lines)
                j += 1
            insert = _yaml_insert_point(lines, idx + 1, end, 2)
            lines[insert:insert] = ["  %s: %s" % (key, value)]
            return "\n".join(lines)
        idx += 1
    # No refs block yet.
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("refs:")
    lines.append("  %s: %s" % (key, value))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Text-level TOML editor (subset; standard library only)
# ---------------------------------------------------------------------------

_TOML_TABLE_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")
_TOML_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        raise AgentConfigError("TOML has no null value")
    return json.dumps(value, ensure_ascii=False)


def toml_upsert_top_key(text, key, value):
    """Upsert a top-level TOML key (it must live before the first [table]).

    An existing assignment is replaced in place; a missing one is inserted
    after the last existing top-level assignment, so table bodies are never
    disturbed.
    """
    rendered = "%s = %s" % (key, _toml_value(value))
    lines = text.split("\n")
    idx = 0
    last_top = -1
    while idx < len(lines):
        line = lines[idx]
        if _TOML_TABLE_RE.match(line):
            break
        m = _TOML_KEY_RE.match(line)
        if m:
            last_top = idx
            if m.group(1) == key:
                lines[idx] = rendered
                return "\n".join(lines)
        idx += 1
    lines.insert(last_top + 1, rendered)
    return "\n".join(lines)


def toml_upsert_table(text, table, mapping):
    """Upsert several keys inside [table]; append the table when missing."""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        m = _TOML_TABLE_RE.match(lines[idx])
        if m and m.group(1) == table:
            end = idx + 1
            while end < len(lines) and not _TOML_TABLE_RE.match(lines[end]):
                end += 1
            return "\n".join(
                _toml_upsert_in_range(lines, idx + 1, end, mapping))
        idx += 1
    # Table not present: append it whole at the end of the file.
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.append("[%s]" % table)
    for key in mapping:
        lines.append("%s = %s" % (key, _toml_value(mapping[key])))
    return "\n".join(lines) + "\n"


def _toml_upsert_in_range(lines, start, end, mapping):
    remaining = dict(mapping)
    last_content = start
    idx = start
    while idx < end:
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith("#"):
            last_content = idx + 1
            m = _TOML_KEY_RE.match(lines[idx])
            if m and m.group(1) in remaining:
                key = m.group(1)
                lines[idx] = "%s = %s" % (key, _toml_value(remaining.pop(key)))
        idx += 1
    insert = last_content
    for key, value in remaining.items():
        lines.insert(insert, "%s = %s" % (key, _toml_value(value)))
        insert += 1
    return lines


# ---------------------------------------------------------------------------
# Text-level dotenv editor
# ---------------------------------------------------------------------------

def env_upsert(text, mapping):
    """Upsert KEY=VALUE lines; comments and unrelated lines are preserved."""
    lines = text.split("\n")
    remaining = dict(mapping)
    for idx, line in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            lines[idx] = "%s=%s" % (key, remaining.pop(key))
    if remaining:
        while lines and not lines[-1].strip():
            lines.pop()
        for key, value in remaining.items():
            lines.append("%s=%s" % (key, value))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Readers for the "managed" slice of a client config
#
# The integration record used to compare the *whole* config file with the
# bytes we last wrote. Any unrelated edit (another provider's model list, a
# plugin entry, the client rewriting its own file) then looked like a hand
# edit of *our* part, and the panel skipped that client when the gateway
# switched provider - so the model list baked into the client config stayed on
# the previous provider's catalogue ("switch the gateway, the models do not
# change").
#
# Every client now declares the slice of its files the gateway owns (the
# "managed" projection below). Only a change inside that slice counts as an
# external change; the rest of the file may drift freely.
# ---------------------------------------------------------------------------

def _json_from_text(text):
    """Parse a JSON document from text; {} when it is missing or broken."""
    if not text or not str(text).strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _yaml_leaf(text, key_path):
    """Read one dotted key path out of a block-style YAML document.

    Only the subset this module writes is supported: block mappings whose
    leaves are inline values. Returns the raw inline text ("" when the leaf
    is there but empty) or None when the path is absent.
    """
    keys = [part.strip() for part in str(key_path or "").split(".") if part.strip()]
    if not keys:
        return None
    lines = str(text or "").split("\n")
    return _yaml_find_leaf(lines, 0, len(lines), 0, keys)


def _yaml_value(value):
    """Normalise a YAML leaf for comparison: quotes and spacing are spelling.

    客户端会自己重排配置（Hermes 把 `default: "x"` 写成 `default: x`），
    值没变、字面变了。拿原文去比指纹就会读成「用户手改过我们那段」，
    面板重刷模型清单时就会跳过这个客户端 —— 它的模型表就一直停在上一家
    提供商的目录上（切网关后客户端模型不更新的第二个原因）。
    """
    text = str("" if value is None else value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def _yaml_json_value(value):
    """Same as :func:`_yaml_value`, for a leaf that holds an inline JSON object.

    重新序列化（键排序）后再比：键序与空格差异不算「被改过」。
    """
    text = _yaml_value(value)
    if not text:
        return ""
    try:
        return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return text


def _yaml_find_leaf(lines, start, end, indent, keys):
    """Block scanner for _yaml_leaf; mirrors _yaml_upsert_at's structure."""
    key = keys[0]
    idx = start
    while idx < end:
        line = lines[idx]
        if line.strip() and not line.strip().startswith("#") \
                and _yaml_indent(line) == indent and _yaml_key(line) == key:
            after = line.split(":", 1)[1].strip() if ":" in line else ""
            child_end = _yaml_block_end(lines, idx, indent)
            if len(keys) == 1:
                return after
            if after:
                # The parent holds an inline mapping: this module never writes
                # that shape, so the path cannot be read back.
                return None
            return _yaml_find_leaf(lines, idx + 1, child_end, indent + 2, keys[1:])
        idx += 1
    return None


def _env_value(text, key):
    """The value of a KEY=VALUE line in a dotenv document (None when absent)."""
    for line in str(text or "").split("\n"):
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m and m.group(1) == key:
            return m.group(2).strip()
    return None


def _toml_section(text, table):
    """The raw lines of a TOML table section (header + body), "" when absent."""
    header = "[%s]" % table
    lines = str(text or "").split("\n")
    out = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if inside:
                break
            inside = (stripped == header)
            if inside:
                out.append(line)
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def _toml_top_value(text, key):
    """The value of a top-level TOML key (None when absent)."""
    for line in str(text or "").split("\n"):
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("#"):
            continue
        name = stripped.split("=", 1)[0].strip()
        if name == key:
            return stripped.split("=", 1)[1].strip()
    return None


def _managed_projection(client, paths, contents):
    """The client's managed slice as *values* (not spelling).

    ``contents`` maps a file path to its current text. Returns None when the
    client declares no managed slice (or it cannot be read), which the caller
    treats as "unknown".
    """
    fn = client.get("managed")
    if not fn:
        return None
    try:
        projection = fn(paths, contents)
    except Exception:
        return None
    if projection is None:
        return None
    return str(projection)


def _managed_fingerprint(client, paths, contents):
    """Hash of the slice of the client's files the gateway owns.

    ``contents`` maps a file path to its current text. Returns None when the
    client declares no managed slice (or it cannot be read), which the caller
    treats as "unknown".
    """
    projection = _managed_projection(client, paths, contents)
    if projection is None:
        return None
    return _sha256_bytes(projection.encode("utf-8"))


# ---------------------------------------------------------------------------
# Backup + state bookkeeping
# ---------------------------------------------------------------------------

def _state_path(accounts_dir):
    return os.path.join(accounts_dir, STATE_FILE)


def _load_state(accounts_dir):
    try:
        with open(_state_path(accounts_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def _save_state(accounts_dir, state):
    os.makedirs(accounts_dir, exist_ok=True)
    _atomic_write_text(_state_path(accounts_dir), _dump_json(state))


def _backup_dir(accounts_dir, client_id):
    return os.path.join(accounts_dir, BACKUP_DIRNAME, client_id)


def _backup_file(accounts_dir, client_id, path, original):
    """Store the original bytes of `path`; returns the backup file path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "%s-%s" % (stamp, os.path.basename(path) or "config")
    directory = _backup_dir(accounts_dir, client_id)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, name)
    seq = 1
    while os.path.exists(target):
        target = os.path.join(directory, "%s-%d" % (name, seq))
        seq += 1
    _atomic_write_bytes(target, original)
    _prune_backups(directory)
    return target


def _prune_backups(directory, keep=BACKUP_KEEP):
    """Keep only the newest `keep` backups of each basename."""
    by_name = {}
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return
    for name in entries:
        base = re.sub(r"^\d{8}-\d{6}-", "", name)
        by_name.setdefault(base, []).append(name)
    for base, names in by_name.items():
        for stale in names[:-keep] if len(names) > keep else []:
            try:
                os.remove(os.path.join(directory, stale))
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Client registry
# ---------------------------------------------------------------------------

def _claude_settings_path(home):
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return os.path.join(override, "settings.json")
    return os.path.join(home, ".claude", "settings.json")


def _apply_claude_code(paths, ctx):
    path = paths["settings"]
    data = _load_json(path, "claude settings")
    env = dict(data.get("env") or {})
    # Claude Code appends /v1 itself when it needs it; keep the bare root.
    env["ANTHROPIC_BASE_URL"] = re.sub(r"/v1/?$", "", ctx["base_url"])
    env["ANTHROPIC_AUTH_TOKEN"] = ctx["api_key"]
    # 模型 id 跨提供商重名时，网关只认显式提示。Claude Code 支持自定义头，
    # 把提供商钉进去；切回 WorkBuddy 时只摘掉我们自己写的那一条。
    wanted = str(ctx.get("provider") or "").strip()
    custom = str(env.get("ANTHROPIC_CUSTOM_HEADERS") or "").strip()
    if wanted and wanted != "workbuddy":
        env["ANTHROPIC_CUSTOM_HEADERS"] = "X-Provider: %s" % wanted
    elif custom.lower() in ("x-provider: qoder", "x-provider: trae"):
        env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    if ctx.get("model"):
        for name in ("ANTHROPIC_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[name] = ctx["model"]
    data["env"] = env
    return {path: _dump_json(data)}


def _managed_claude_code(paths, contents):
    """The env entries Claude Code points at the gateway with."""
    data = _json_from_text(contents.get(paths["settings"]))
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL")
    return json.dumps({k: env.get(k) for k in keys},
                      sort_keys=True, ensure_ascii=False)


def _codex_home(home):
    return os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")


def _apply_codex(paths, ctx):
    config_path = paths["config"]
    text = _read_text(config_path)
    text = toml_upsert_top_key(text, "model_provider", PROVIDER_ID)
    if ctx.get("model"):
        text = toml_upsert_top_key(text, "model", ctx["model"])
    text = toml_upsert_table(text, "model_providers." + PROVIDER_ID, {
        "name": PROVIDER_NAME,
        "base_url": ctx["base_url_v1"],
        "wire_api": "responses",
        "env_key": "OPENAI_API_KEY",
    })
    auth_path = paths["auth"]
    auth = _load_json(auth_path, "codex auth")
    auth = deep_merge(auth, {"OPENAI_API_KEY": ctx["api_key"]})
    return {config_path: text, auth_path: _dump_json(auth)}


def _managed_codex(paths, contents):
    """The provider table and the key Codex uses to reach the gateway."""
    text = contents.get(paths["config"]) or ""
    auth = _json_from_text(contents.get(paths["auth"]))
    return json.dumps({
        "provider": _toml_section(text, "model_providers." + PROVIDER_ID),
        "model_provider": _toml_top_value(text, "model_provider"),
        "key": auth.get("OPENAI_API_KEY"),
    }, sort_keys=True, ensure_ascii=False)


def _tag_slug(text):
    """把池子名之类的标签压成一个能搜的小写词（NVIDIA NIM → nvidia-nim）。"""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower())
    return slug.strip("-")


def model_source_tag(entry, provider=None):
    """这个模型在客户端选择器里该挂的短标签。

    ``entry`` 是模型表里的一条（可以带 ``source`` / ``source_label``：
    鸡蛋的免费源分别是 ``kilo`` / ``zen``，自建池是 ``pool-xxxx``）。
    ``provider`` 是这次配置的当前提供商 —— 标签以它为准：鸡蛋按免费源细分，
    其余按提供商名（workbuddy → wb、qoder → qoder、trae → trae）。
    """
    entry = entry if isinstance(entry, dict) else {}
    provider = str(provider or "").strip().lower()
    source = str(entry.get("source") or "").strip().lower()
    label = str(entry.get("source_label") or "").strip()
    if provider == "egg":
        tag = EGG_SOURCE_TAGS.get(source)
        if tag:
            return tag
        if source.startswith("pool-"):
            # 操作者自建的供应商池：挂池子的名字，一眼知道这条模型走哪一家。
            return _tag_slug(label) or "pool"
        return PROVIDER_TAGS["egg"]
    if provider:
        return PROVIDER_TAGS.get(provider, provider)
    # 没有提供商信息时退回到模型自己的来源，再退回到 WorkBuddy 的 wb。
    if source in EGG_SOURCE_TAGS:
        return EGG_SOURCE_TAGS[source]
    if source:
        return _tag_slug(label or source) or source
    return PROVIDER_TAGS["workbuddy"]


def _apply_opencode(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "opencode config")
    if "$schema" not in data:
        data["$schema"] = "https://opencode.ai/config.json"
    models = {}
    provider_id = str(ctx.get("provider") or "").strip().lower()
    for entry in ctx.get("models") or []:
        if isinstance(entry, dict):
            mid = entry.get("id")
            name = entry.get("name")
        else:
            mid = entry
            name = None
        mid = str(mid or "").strip()
        if not mid:
            continue
        # OpenCode v2's model picker searches model names only - the provider
        # name is not a search key unless an integration is connected - so tag
        # every model with the short name of where it actually comes from:
        # "hy3 wb" for WorkBuddy, "hy3 qoder" / "hy3 trae" for those
        # providers, and "hy3 kilo" / "hy3 opencode-zen" for the egg's free
        # lanes. Typing the tag then lists exactly that provider's models.
        label = str(name or mid).strip() or mid
        tag = model_source_tag(entry if isinstance(entry, dict) else {},
                               provider_id)
        # 每条模型都挂上来源标签（名字里已经以它结尾的就不重复挂），这样
        # 选择器里每一行都能看出这条模型走的是哪一家，搜索也能按来源过滤。
        words = label.lower().split()
        if tag and (not words or words[-1] != tag.lower()):
            label = "%s %s" % (label, tag)
        models[mid] = {"name": label}
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": PROVIDER_NAME,
        "options": {
            "baseURL": ctx["base_url_v1"],
            "apiKey": ctx["api_key"],
        },
    }
    # 模型表是「当前目录」的快照，必须整表替换。deep_merge 会递归合并两个
    # dict，从 WorkBuddy 切到 Qoder 再应用时，上一家的模型会一条条留在表里
    # （两边的 id 基本不重叠），选择器里就变成两家混排——这正是「切到 Qoder
    # 还看得到 WorkBuddy 模型」的来源。除 models 外的字段仍走合并。
    rest = {k: v for k, v in provider.items() if k != "models"}
    data = deep_merge(data, {"provider": {PROVIDER_ID: rest}})
    node = data.setdefault("provider", {}).setdefault(PROVIDER_ID, {})
    # 模型 id 可能跨提供商重名（Trae 国内的 glm-5.2 与 WorkBuddy 目录同名）。
    # 网关只能靠显式提示分账，所以把提供商钉进请求头：切回 WorkBuddy 时再把
    # 这一项摘掉，other headers 原样保留。
    options = node.setdefault("options", {})
    headers = options.get("headers")
    if isinstance(headers, dict):
        headers.pop("X-Provider", None)
        if not headers:
            options.pop("headers", None)
    wanted = str(ctx.get("provider") or "").strip()
    if wanted and wanted != "workbuddy":
        options.setdefault("headers", {})["X-Provider"] = wanted
    # OpenCode 把 options.headers 原样交给 AI SDK；写入归属头后，看板即使
    # 面对没有特征 User-Agent 的版本也能把用量算到 OpenCode 头上。
    options.setdefault("headers", {})[AGENT_HEADER] = "opencode"
    if models:
        node["models"] = models
    else:
        node.pop("models", None)
    if ctx.get("model"):
        data["model"] = "%s/%s" % (PROVIDER_ID, ctx["model"])
    elif models and isinstance(data.get("model"), str) \
            and data["model"].startswith(PROVIDER_ID + "/"):
        # 旧默认模型已经不在新目录里（例如从 WorkBuddy 切到 Qoder），留着会
        # 让客户端一开就请求一个不存在的模型。
        if data["model"].split("/", 1)[1] not in models:
            data.pop("model", None)
    return {path: _dump_json(data)}


def _managed_opencode(paths, contents):
    """The wb-proxy provider entry (model table included) and the default model."""
    data = _json_from_text(contents.get(paths["config"]))
    provider = data.get("provider") if isinstance(data.get("provider"), dict) else {}
    model = data.get("model")
    if not (isinstance(model, str) and model.startswith(PROVIDER_ID + "/")):
        model = None
    return json.dumps({"provider": provider.get(PROVIDER_ID), "model": model},
                      sort_keys=True, ensure_ascii=False)


def dsh_reasoning_levels(raw):
    """{档位: 线上拼写} for DSH, from the gateway's `reasoning_efforts` list.

    DSH 的模型条目用 `reasoningEfforts` 声明可选思考档位（键=选择器里的档位，
    值=请求里实际发的拼写）。本路由不在它的内置目录里，所以不声明就没有选择
    器——`/v1/models` 里的 `reasoning_efforts` 它不读，它只读 id / name /
    contextWindow / maxTokens。网关宣告的档位名就是上游认的拼写，一一照抄即可；
    固定档位的模型（网关只给 reasoning_fixed_effort）没有列表，就不声明：给了
    选择器也不会改变上游实际跑的档位。
    """
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        pairs = [(lvl, lvl) for lvl in raw]
    else:
        return None
    levels = {}
    for level, wire in pairs:
        name = str(level or "").strip()
        if not name:
            continue
        levels[name] = str(wire or "").strip() or name
    return levels or None


def dsh_default_model(text, provider, model):
    """The `agent-default-model` entry to write, keeping keys we do not own.

    yaml_upsert_inline() replaces that leaf wholesale, so writing a bare
    {provider, model} silently drops whatever the user set beside it - a
    reasoningEffort, for instance, which is how a default thinking level gets
    lost on a re-apply. Read the current inline mapping and merge the route
    over it; a block-style or unparsable value falls back to the bare pair
    rather than guessing.
    """
    out = {"provider": provider, "model": model}
    for line in str(text or "").split("\n"):
        stripped = line.strip()
        if not stripped.startswith("agent-default-model:"):
            continue
        raw = stripped.split(":", 1)[1].strip()
        if raw.startswith("{"):
            try:
                current = json.loads(raw)
            except ValueError:
                current = None
            if isinstance(current, dict):
                out = dict(current)
        break
    out["provider"] = provider
    out["model"] = model
    return out


def _apply_dsh(paths, ctx):
    settings_path = paths["settings"]
    text = _read_text(settings_path)
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid}
        if entry.get("context_window"):
            item["contextWindow"] = entry["context_window"]
        if entry.get("max_output"):
            try:
                item["maxTokens"] = int(entry["max_output"])
            except (TypeError, ValueError):
                pass
        levels = dsh_reasoning_levels(entry.get("reasoning_efforts"))
        if levels:
            item["reasoningEfforts"] = levels
        if entry.get("supports_vision") or entry.get("supportsVision"):
            item["supportsVision"] = True
        models.append(item)
    provider = {
        "displayName": PROVIDER_NAME,
        "apiKeyEnv": CREDENTIAL_REF,
        "api": "openai-completions",
        "baseURL": ctx["base_url_v1"],
        # 看板按客户端筛选用量：DSH 的 provider 支持静态 headers，直接把归属
        # 头写进去，比从 User-Agent 猜更准。
        "headers": {AGENT_HEADER: "dsh"},
        # wb-proxy 不在 DSH 的内置目录里，推理字段的线上形状必须自己说明：
        # 走标准 reasoning_effort，不发 thinking 块——DeepSeek 系需要的那份
        # thinking / reasoning_content 由网关补（wb_proxy.build_upstream_body）。
        "compat": {"thinkingFormat": "openai", "supportsReasoningEffort": True},
    }
    if models:
        provider["models"] = models
    text = yaml_upsert_inline(
        text, "llm-pi-ai.providers.%s" % PROVIDER_ID, provider)
    if ctx.get("model"):
        text = yaml_upsert_inline(
            text, "agent-default-model",
            dsh_default_model(text, PROVIDER_ID, ctx["model"]))
    credentials_path = paths["credentials"]
    cred_text = _read_text(credentials_path)
    cred_text = yaml_refs_upsert(cred_text, CREDENTIAL_REF, ctx["api_key"])
    return {settings_path: text, credentials_path: cred_text}


def _managed_dsh(paths, contents):
    """The provider entry, the default model and the credential reference."""
    text = contents.get(paths["settings"]) or ""
    cred = contents.get(paths["credentials"]) or ""
    return json.dumps({
        "provider": _yaml_json_value(
            _yaml_leaf(text, "llm-pi-ai.providers." + PROVIDER_ID)),
        "default_model": _yaml_value(
            _yaml_leaf(text, "agent-default-model")),
        "credential": _env_value(cred, CREDENTIAL_REF),
    }, sort_keys=True, ensure_ascii=False)


def _apply_crush(paths, ctx):
    path = paths["config"]
    data = _load_json(path, "crush config")
    models = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id")
        if not mid:
            continue
        item = {"id": mid, "name": mid}
        if entry.get("context_window"):
            item["context_window"] = entry["context_window"]
        if entry.get("max_output"):
            item["default_max_tokens"] = entry["max_output"]
        models.append(item)
    provider = {
        "name": PROVIDER_NAME,
        "type": "openai",
        "base_url": ctx["base_url_v1"],
        "api_key": ctx["api_key"],
        "models": models,
    }
    data = deep_merge(data, {"providers": {PROVIDER_ID: provider}})
    return {path: _dump_json(data)}


def _managed_crush(paths, contents):
    """The wb-proxy provider entry (model table included)."""
    data = _json_from_text(contents.get(paths["config"]))
    providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    return json.dumps({"provider": providers.get(PROVIDER_ID)},
                      sort_keys=True, ensure_ascii=False)


def _apply_zcode(paths, ctx):
    """Add or update a wb-proxy provider rule in ZCode's provider config.

    The rule is addressed by a stable providerId, so re-applying updates that
    one entry instead of stacking new ones, and everything else in the
    document - other providers, their order, the model rules - is merged
    around untouched. Note the file belongs to the ZCode app and is rewritten
    by it while running; configure with ZCode closed (the panel card and the
    CLI both say so) or the app's next save can drop the new entry.
    """
    path = paths["config"]
    data = _load_json(path, "zcode provider config")
    if not data:
        # No ZCode profile on this machine yet: start from the wrapper shape a
        # live 3.x profile writes (schemaVersion + config + the two rule
        # holders), so a fresh install can be configured without running the
        # app first.
        data = {"schemaVersion": 1,
                "config": {"providerOrder": [],
                           "providerConfigRules": {"providerRules": []}}}
    config = data.get("config")
    if not isinstance(config, dict):
        raise AgentConfigError(
            "zcode provider config has no config object: %s" % path)
    holder = config.get("providerConfigRules")
    if not isinstance(holder, dict):
        raise AgentConfigError(
            "zcode provider config has no providerConfigRules: %s" % path)
    rules = holder.get("providerRules")
    if not isinstance(rules, list):
        raise AgentConfigError(
            "zcode providerConfigRules has no providerRules list: %s" % path)

    model_ids = []
    for entry in ctx.get("models") or []:
        mid = entry.get("id") if isinstance(entry, dict) else entry
        mid = str(mid or "").strip()
        if mid and mid not in model_ids:
            model_ids.append(mid)

    rule = {
        "providerId": PROVIDER_ID,
        "providerName": PROVIDER_NAME,
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": ctx["api_key"]},
            "api": {"type": "openai-chat-completions",
                    "baseUrl": ctx["base_url_v1"]},
        },
    }
    if model_ids:
        rule["config"]["personalModelIds"] = model_ids
        rule["config"]["modelOrder"] = list(model_ids)

    replaced = False
    for index, existing in enumerate(rules):
        if isinstance(existing, dict) and existing.get("providerId") == PROVIDER_ID:
            rules[index] = deep_merge(existing, rule)
            replaced = True
            break
    if not replaced:
        rules.append(rule)

    order = config.get("providerOrder")
    if not isinstance(order, list):
        order = []
        config["providerOrder"] = order
    if PROVIDER_ID not in order:
        order.append(PROVIDER_ID)
    return {path: _dump_json(data)}


def _managed_zcode(paths, contents):
    """The wb-proxy provider rule (its model list included)."""
    data = _json_from_text(contents.get(paths["config"]))
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    holder = config.get("providerConfigRules") \
        if isinstance(config.get("providerConfigRules"), dict) else {}
    rules = holder.get("providerRules") if isinstance(holder.get("providerRules"), list) else []
    rule = None
    for item in rules:
        if isinstance(item, dict) and item.get("providerId") == PROVIDER_ID:
            rule = item
            break
    return json.dumps({"rule": rule}, sort_keys=True, ensure_ascii=False)


def _hermes_home(home):
    """Hermes home dir: ``HERMES_HOME``, else Hermes' own platform default.

    Hermes resolves an unset ``HERMES_HOME`` to ``~/.hermes`` on POSIX but to
    ``%LOCALAPPDATA%\\hermes`` on Windows (see ``get_hermes_home`` in
    ``hermes_constants.py``), where the Windows installer puts ``config.yaml``,
    ``.env`` and ``bin/hermes.exe``. Probing only ``~/.hermes`` therefore missed
    every native Windows install. ``HERMES_DATA_DIR_SUFFIX`` renames the default
    data dir, so it is mirrored here as well.
    """
    override = os.environ.get("HERMES_HOME")
    if override:
        return os.path.expanduser(os.path.expandvars(override))
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA") or "").strip() or os.path.join(
            home, "AppData", "Local")
        return os.path.join(base, "hermes" + suffix)
    return os.path.join(home, ".hermes" + suffix)


def _apply_hermes(paths, ctx):
    """Register the gateway as a named custom provider for Hermes Agent.

    Hermes keeps non-secret settings in ``config.yaml`` and secrets in
    ``.env``, so the key is written to ``.env`` and referenced from the
    provider entry via ``key_env``. The provider is added under
    ``providers:`` and selected with ``model.provider: custom:wb-proxy``,
    mirroring what ``hermes model`` -> Custom Endpoint writes. The optional
    per-model ``models`` map carries the context window so Hermes compresses
    at the right point instead of guessing.
    """
    config_path = paths["config"]
    text = _read_text(config_path)
    provider = {
        "api": ctx["base_url_v1"],
        "key_env": CREDENTIAL_REF,
        "transport": "chat_completions",
        # Hermes 的 providers.<id> 支持 extra_headers（会并进 OpenAI 客户端的
        # default_headers），看板的客户端归属靠它，不依赖 User-Agent。
        "extra_headers": {AGENT_HEADER: "hermes"},
    }
    models = {}
    for entry in ctx.get("models") or []:
        mid = entry.get("id") if isinstance(entry, dict) else entry
        mid = str(mid or "").strip()
        if not mid or mid in models:
            continue
        item = {}
        if isinstance(entry, dict) and entry.get("context_window"):
            item["context_length"] = entry["context_window"]
        models[mid] = item
    if models:
        provider["models"] = models
    # Hermes routes every turn through the selected provider, so a model the
    # gateway does not serve fails on the first message. Prefer the panel's
    # pick; when none was chosen, fall back to the first served model instead
    # of leaving a stale default (e.g. "anthropic/claude-opus-4.6") behind.
    model = ctx.get("model")
    if not model and models:
        model = next(iter(models))
    if model:
        provider["default_model"] = model
    text = yaml_upsert_inline(text, "providers.%s" % PROVIDER_ID, provider)
    text = yaml_upsert_inline(text, "model.provider", "custom:" + PROVIDER_ID)
    if model:
        text = yaml_upsert_inline(text, "model.default", model)
    env_path = paths["env"]
    env_text = _read_text(env_path)
    env_text = env_upsert(env_text, {CREDENTIAL_REF: ctx["api_key"]})
    return {config_path: text, env_path: env_text}


def _managed_hermes(paths, contents):
    """The provider entry, the selected provider/model and the key reference."""
    text = contents.get(paths["config"]) or ""
    env = contents.get(paths["env"]) or ""
    return json.dumps({
        "provider": _yaml_json_value(
            _yaml_leaf(text, "providers." + PROVIDER_ID)),
        "model_provider": _yaml_value(_yaml_leaf(text, "model.provider")),
        "model_default": _yaml_value(_yaml_leaf(text, "model.default")),
        "key": _env_value(env, CREDENTIAL_REF),
    }, sort_keys=True, ensure_ascii=False)


CLIENTS = {
    "claude-code": {
        "id": "claude-code",
        "label": "Claude Code",
        "desc": "Anthropic CLI; points ANTHROPIC_BASE_URL at this gateway.",
        "protocol": "anthropic",
        "files": lambda home: {
            "settings": _claude_settings_path(home),
        },
        "probe": lambda home: (
            os.path.isdir(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"))
            or os.path.isfile(_claude_settings_path(home))
            or bool(shutil.which("claude"))
        ),
        "apply": _apply_claude_code,
        "managed": _managed_claude_code,
    },
    "codex": {
        "id": "codex",
        "label": "Codex CLI",
        "desc": "OpenAI Codex CLI; adds a wb-proxy model provider.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(_codex_home(home), "config.toml"),
            "auth": os.path.join(_codex_home(home), "auth.json"),
        },
        "probe": lambda home: (
            os.path.isdir(_codex_home(home))
            or os.path.isfile(os.path.join(_codex_home(home), "config.toml"))
            or bool(shutil.which("codex"))
        ),
        "apply": _apply_codex,
        "managed": _managed_codex,
    },
    "opencode": {
        "id": "opencode",
        "label": "OpenCode",
        "desc": "OpenCode; registers an openai-compatible provider.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "opencode", "opencode.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "opencode"))
            or os.path.isfile(os.path.join(home, ".config", "opencode", "opencode.json"))
            or bool(shutil.which("opencode"))
        ),
        "apply": _apply_opencode,
        "managed": _managed_opencode,
    },
    "dsh": {
        "id": "dsh",
        "label": "DeepSeek Harness",
        "desc": "DSH; adds a wb-proxy provider to settings.yaml.",
        "protocol": "openai",
        "files": lambda home: {
            "settings": os.path.join(home, ".dsh", "settings.yaml"),
            "credentials": os.path.join(home, ".dsh", ".credentials.yaml"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".dsh"))
            or os.path.isfile(os.path.join(home, ".dsh", "settings.yaml"))
            or bool(shutil.which("dsh"))
        ),
        "apply": _apply_dsh,
        "managed": _managed_dsh,
    },
    "crush": {
        "id": "crush",
        "label": "Crush",
        "desc": "Charm Crush; adds a wb-proxy provider entry.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".config", "crush", "crush.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".config", "crush"))
            or os.path.isfile(os.path.join(home, ".config", "crush", "crush.json"))
            or bool(shutil.which("crush"))
        ),
        "apply": _apply_crush,
        "managed": _managed_crush,
    },
    "zcode": {
        "id": "zcode",
        "label": "ZCode",
        "desc": "ZCode; adds a wb-proxy provider to its provider config. "
                "Quit ZCode first - it rewrites that file while running.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(home, ".zcode", "v2", "provider_config.json"),
        },
        "probe": lambda home: (
            os.path.isdir(os.path.join(home, ".zcode"))
            or os.path.isfile(os.path.join(home, ".zcode", "v2",
                                           "provider_config.json"))
        ),
        "apply": _apply_zcode,
        "managed": _managed_zcode,
    },
    "hermes": {
        "id": "hermes",
        "label": "Hermes CLI",
        "desc": "Hermes Agent; registers a wb-proxy provider in config.yaml, "
                "selects it, and picks a served model.",
        "protocol": "openai",
        "files": lambda home: {
            "config": os.path.join(_hermes_home(home), "config.yaml"),
            "env": os.path.join(_hermes_home(home), ".env"),
        },
        "probe": lambda home: (
            os.path.isdir(_hermes_home(home))
            or os.path.isfile(os.path.join(_hermes_home(home), "config.yaml"))
            or bool(shutil.which("hermes"))
        ),
        "apply": _apply_hermes,
        "managed": _managed_hermes,
    },
}


def _client(client_id):
    client = CLIENTS.get(client_id)
    if client is None:
        raise AgentConfigError(
            "unknown client %r (known: %s)"
            % (client_id, ", ".join(sorted(CLIENTS))))
    return client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _client_mentions_gateway(client, home):
    """True when an existing config file of this client already points at us.

    Configs written outside the panel/CLI have no integration-state record, so
    syncAppliedAgents() used to skip them forever and their baked-in model
    list stayed on the old provider after a switch. Every integration we write
    stamps PROVIDER_ID into the file, so a substring check is enough to adopt
    them for the next apply (which then records proper state/backups).
    """
    try:
        paths = client["files"](home)
    except Exception:
        return False
    for path in paths.values():
        try:
            if not os.path.exists(path):
                continue
            text = _read_bytes(path).decode("utf-8", "replace")
        except Exception:
            continue
        if PROVIDER_ID in text:
            return True
    return False


def overview(accounts_dir, home=None):
    """Per-client install/apply summary for the panel."""
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    out = {}
    for client_id in sorted(CLIENTS):
        client = CLIENTS[client_id]
        paths = client["files"](home)
        existing = [p for p in paths.values() if os.path.exists(p)]
        applied = None
        record = state.get(client_id)
        if isinstance(record, dict):
            # external_change = 「我们自己写的那一段」被手改过；
            # file_changed = 整个文件跟我们上次写的字节不一样（只是提示）。
            # 以前这里比的是整份文件：别的 provider 加个模型、客户端自己重排
            # 一下配置，都会被当成「用户改了我们那段」，于是切网关时这个客户端
            # 被跳过，配置里写死的模型表就一直停在上一个提供商的目录。
            external = False
            file_changed = False
            for entry in record.get("files") or []:
                path = entry.get("path")
                if not path or not os.path.exists(path):
                    file_changed = True
                    continue
                # Compare against the bytes we last wrote, not the backup: a
                # hand edit after apply must read as an external change.
                try:
                    if _sha256_bytes(_read_bytes(path)) != entry.get("sha256"):
                        file_changed = True
                except AgentConfigError:
                    file_changed = True
            managed_values = record.get("managed_values")
            if managed_values:
                contents = {}
                for entry in record.get("files") or []:
                    path = entry.get("path")
                    if not path:
                        continue
                    try:
                        contents[path] = _read_text(path)
                    except AgentConfigError:
                        contents[path] = ""
                # 比的是**取值**而不是原文：客户端会自己重排配置（Hermes 把
                # `default: "x"` 写成 `default: x`），字面变了值没变。按原文
                # 比就会读成「用户改了我们那段」，面板重刷模型清单时跳过这个
                # 客户端 —— 它的模型表就一直停在上一家提供商的目录上。
                current = _managed_projection(client, paths, contents)
                external = (current is None or current != managed_values)
            else:
                # 旧记录只存了指纹、没有可比对的取值：无从判断我们那段有没有
                # 被改过。先当作没被改——切网关时我们要做的正是「把我们那段
                # 重写一遍」，不能因为文件里别的段落变了就把这个客户端跳过。
                # 下一次 apply 会记上取值，之后的判断就准了。
                external = False
            applied = {
                "at": record.get("applied_at"),
                "base_url": record.get("base_url"),
                "model": record.get("model"),
                "key_id": record.get("key_id") or "",
                "files": len(record.get("files") or []),
                "external_change": external,
                "file_changed": file_changed,
            }
        out[client_id] = {
            "id": client_id,
            "label": client["label"],
            "desc": client["desc"],
            "protocol": client["protocol"],
            "installed": bool(client["probe"](home)) or bool(existing),
            # "configured" is about this gateway's integration record, not about
            # the client merely having a config file on disk - otherwise every
            # pre-existing config would advertise a restore that cannot run.
            "configured": isinstance(record, dict),
            # 已有配置文件里提到本网关（但没被面板/CLI 记录过）：
            # syncAppliedAgents 用它来把这些「孤儿配置」的模型表也一起刷新。
            "linked": _client_mentions_gateway(client, home),
            "config_paths": list(paths.values()),
            "applied": applied,
        }
    return out


def agent_inventory(accounts_dir):
    """Every known client with its label and whether it has been applied.

    The analytics page needs this to build its agent filter: only applied
    clients become options, but labels for rows tagged with a client that was
    later restored still come from the same list. Reads the integration state
    only - unlike overview() it never probes the home directory.
    """
    state = _load_state(accounts_dir)
    out = []
    for client_id in sorted(CLIENTS):
        client = CLIENTS[client_id]
        record = state.get(client_id)
        applied = isinstance(record, dict)
        out.append({
            "id": client_id,
            "label": client["label"],
            "applied": applied,
            "applied_at": record.get("applied_at") if applied else None,
        })
    return out


def plan(client_id, base_url, api_key, model=None, models=None, home=None,
         provider=None):
    """The exact file contents integrate() would write; touches nothing.

    Each entry in ``files`` carries the target ``path``, the new ``content``
    and whether the file already ``existed``, so a caller can preview an
    apply - the CLI's --dry-run - before anything is backed up or written.
    integrate() and the preview share this one builder, so what a dry run
    prints is what a real run writes.
    """
    client = _client(client_id)
    home = _resolve_home(home)
    base_url = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise AgentConfigError("base_url must start with http:// or https://")
    base_url_v1 = base_url if base_url.endswith("/v1") else base_url + "/v1"
    ctx = {
        "base_url": base_url,
        "base_url_v1": base_url_v1,
        "provider": str(provider or "").strip() or None,
        "api_key": str(api_key or ""),
        "model": str(model or "").strip() or None,
        "models": list(models or []),
    }
    paths = client["files"](home)
    try:
        new_contents = client["apply"](paths, ctx)
    except AgentConfigError:
        raise
    except Exception as exc:
        raise AgentConfigError("failed to build %s config: %s"
                               % (client_id, exc))
    files = []
    for path in paths.values():
        content = new_contents.get(path)
        if content is None:
            continue
        files.append({
            "path": path,
            "content": content,
            "existed": os.path.exists(path),
        })
    return {
        "client": client_id,
        "label": client["label"],
        "base_url": base_url,
        "model": ctx["model"],
        "files": files,
    }


def integrate(accounts_dir, client_id, base_url, api_key, model=None,
              models=None, home=None, key_id=None, provider=None):
    """Point `client_id` at this gateway; returns what was written.

    The first apply backs up the original bytes of every touched file; later
    applies keep the earliest backup so restore always returns to the state
    before the very first integration.
    """
    built = plan(client_id, base_url, api_key, model=model, models=models,
                 home=home, provider=provider)
    client = _client(client_id)
    paths = client["files"](_resolve_home(home))
    state = _load_state(accounts_dir)
    record = state.get(client_id) or {}
    previous_files = {entry.get("path"): entry
                      for entry in record.get("files") or []
                      if isinstance(entry, dict)}
    # Phase 1: stage backups for all target files before making any modifications.
    staged = []
    for entry in built["files"]:
        path, content, existed = entry["path"], entry["content"], entry["existed"]
        prior = previous_files.get(path)
        if prior is not None and not prior.get("existed") and existed:
            # We created this file in an earlier apply. While it still holds
            # exactly what we wrote it stays "ours", so restore deletes it
            # instead of resurrecting our own bytes (that is the documented
            # promise, and the bytes may hold the API key); a hand edit since
            # then makes it the user's file again, and it gets backed up.
            try:
                if _sha256_bytes(_read_bytes(path)) == prior.get("sha256"):
                    existed = False
            except AgentConfigError:
                pass
        backup = None
        if existed and prior and prior.get("backup"):
            # Keep the very first backup: it holds the pre-integration bytes.
            backup = prior.get("backup")
        elif existed:
            backup = _backup_file(accounts_dir, client_id, path,
                                  _read_bytes(path))
        staged.append((path, content, existed, backup))

    # Phase 2: apply writes transactionally. If any write fails, roll back
    # all already-written files so a multi-file integration (e.g. DSH) never
    # leaves a half-configured state with missing state records.
    written = []
    entries = []
    try:
        for path, content, existed, backup in staged:
            _atomic_write_text(path, content)
            written.append((path, existed, backup))
            entries.append({
                "path": path,
                "existed": existed,
                "backup": backup,
                "sha256": _sha256_bytes(content.encode("utf-8")),
                "bytes": len(content.encode("utf-8")),
            })
    except Exception as exc:
        for w_path, w_existed, w_backup in written:
            try:
                if w_existed and w_backup:
                    # w_backup 是 _backup_file() 记下的**完整备份路径**（state
                    # 与 restore() 都按整条路径用它），直接读回即可。这里曾经
                    # 先拼了一次 backup_dir(...)，而那个名字全模块都不存在：
                    # NameError 被下面的 except 吞掉，于是多文件客户端写一半
                    # 失败时既不回滚也不写 state——面板显示「未配置」，而
                    # restore() 又因为查不到记录而拒绝执行。
                    _atomic_write_bytes(w_path, _read_bytes(w_backup))
                elif not w_existed and os.path.exists(w_path):
                    os.unlink(w_path)
            except Exception:
                pass
        raise AgentConfigError("failed to write %s files: %s" % (client_id, exc))

    state[client_id] = {
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": built["base_url"],
        "model": built["model"],
        # 记下用的是哪把面板 Key：切换提供商后自动重应用时沿用同一把，
        # 不会把用户特意选的 Key 换成全局 Key。
        "key_id": str(key_id or ""),
        "files": entries,
        # 我们自己写的那一段（provider 条目 / 模型表 / 默认模型）：
        # 存取值（可比对）与指纹（留个记录）。之后判断「文件被外部改过」只看
        # 这一段，文件里别的部分怎么变都不算。
        "managed_values": _managed_projection(
            client, paths,
            {entry["path"]: entry["content"] for entry in built["files"]}),
        "managed_sha256": _managed_fingerprint(
            client, paths,
            {entry["path"]: entry["content"] for entry in built["files"]}),
    }
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": built["label"],
        "applied_at": state[client_id]["applied_at"],
        "base_url": built["base_url"],
        "model": built["model"],
        "files": entries,
    }


def restore(accounts_dir, client_id, home=None):
    """Undo integrate(): put the backed-up originals back byte for byte.

    Files that did not exist before the first apply are deleted; the state
    record for the client is cleared.
    """
    client = _client(client_id)
    home = _resolve_home(home)
    state = _load_state(accounts_dir)
    record = state.get(client_id)
    if not isinstance(record, dict) or not record.get("files"):
        raise AgentConfigError(
            "%s has no recorded integration to restore" % client["label"])
    restored = []
    for entry in record.get("files") or []:
        path = entry.get("path")
        if not path:
            continue
        if entry.get("existed") and entry.get("backup"):
            backup = entry["backup"]
            if not os.path.exists(backup):
                raise AgentConfigError(
                    "backup missing for %s: %s" % (path, backup))
            _atomic_write_bytes(path, _read_bytes(backup))
            restored.append({"path": path, "action": "restored"})
        else:
            # We created this file; removing it returns to the original state.
            try:
                os.remove(path)
                restored.append({"path": path, "action": "deleted"})
            except FileNotFoundError:
                restored.append({"path": path, "action": "already-absent"})
            # Applying had to create the parent directories too. Leaving an
            # empty one behind makes the probe report the client as installed
            # when the only config it ever had was ours, so prune the chain we
            # emptied - never touching a directory that still holds anything.
            _prune_empty_parents(path, home)
    state.pop(client_id, None)
    _save_state(accounts_dir, state)
    return {
        "client": client_id,
        "label": client["label"],
        "restored": restored,
    }


# ---------------------------------------------------------------------------
# Standalone configurator
# ---------------------------------------------------------------------------
#
# The panel's one-click setup runs inside the gateway process, so it can only
# ever edit the machine the gateway runs on - which is also why /agents/apply
# refuses non-loopback callers and container deployments. The CLI inverts the
# direction: it runs where the clients are and reaches the gateway only over
# HTTP (the model list), so a gateway on a server, NAS or router can still
# serve clients on every other machine.


def fetch_models_from_gateway(base_url, api_key, timeout=10):
    """The gateway's model list, for clients that embed model definitions.

    Reads the OpenAI-compatible ``GET /v1/models``, which is reachable with
    the API key alone (it is not a panel route). Doubles as key validation:
    a 401 here means the key is wrong, anything else means the gateway or its
    upstream is unreachable.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise AgentConfigError("base_url must start with http:// or https://")
    if base.endswith("/v1"):
        base = base[:-3]
    request = urllib.request.Request(
        base + "/v1/models",
        headers={"Authorization": "Bearer " + str(api_key or "")})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AgentConfigError(
                "the gateway rejected the API key (HTTP 401)")
        raise AgentConfigError("gateway /v1/models failed: HTTP %s" % exc.code)
    except ValueError as exc:
        raise AgentConfigError(
            "gateway /v1/models returned invalid JSON: %s" % exc)
    except Exception as exc:
        raise AgentConfigError(
            "cannot reach the gateway /v1/models: %s" % exc)
    models = []
    seen = set()
    for item in (payload.get("data") or []):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        entry = {"id": mid}
        context = item.get("context_length") or item.get("max_input_tokens")
        if context:
            try:
                entry["context_window"] = int(context)
            except (TypeError, ValueError):
                pass
        out_tokens = item.get("max_output_tokens")
        if out_tokens:
            try:
                entry["max_output"] = int(out_tokens)
            except (TypeError, ValueError):
                pass
        efforts = item.get("reasoning_efforts")
        if isinstance(efforts, (list, tuple)):
            levels = [str(e).strip() for e in efforts if str(e or "").strip()]
            if levels:
                entry["reasoning_efforts"] = levels
        name = item.get("name")
        if name:
            entry["name"] = str(name)
        # 免费源归属（鸡蛋的 kilo / zen 免费车道、自建池）：客户端据此给模型
        # 挂上来源标签（见 model_source_tag）。
        source = item.get("egg_source")
        if source:
            entry["source"] = str(source)
            source_label = item.get("egg_source_label")
            if source_label:
                entry["source_label"] = str(source_label)
        models.append(entry)
    # Same cap as the panel's apply: a runaway catalog must not turn into a
    # megabyte config file.
    return models[:80]


def _default_state_dir():
    """Where the standalone CLI keeps its state and backups.

    Deliberately NOT the gateway's accounts/ directory: this runs on a
    different machine, and every machine keeps its own record and backups.
    """
    return os.path.join(os.path.expanduser("~"), ".wb-agent-config")


def _mask_key(key):
    key = str(key or "")
    if not key:
        return "(none)"
    if len(key) <= 4:
        return "***"
    return "***" + key[-4:]


def _cli_error(message):
    sys.stderr.write("error: %s\n" % message)
    return 1


def main(argv=None):
    # --home/--state-dir/--json are accepted before AND after the subcommand:
    # the shared parent carries them into every subparser with SUPPRESS
    # defaults, so a subparser never clobbers a value given at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS,
                        help="home directory to configure "
                             "(default: the current user's home)")
    common.add_argument("--state-dir", default=argparse.SUPPRESS,
                        dest="state_dir",
                        help="where the integration state and backups live "
                             "(default: ~/.wb-agent-config)")
    common.add_argument("--json", action="store_true", dest="as_json",
                        default=argparse.SUPPRESS,
                        help="print machine-readable JSON")

    parser = argparse.ArgumentParser(
        prog="wb_agents.py", parents=[common],
        description="One-click agent integration for local AI clients. Runs "
                    "where the clients are, so the gateway may live on "
                    "another machine.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", parents=[common],
                   help="supported clients and what is installed on this machine")
    sub.add_parser("status", parents=[common],
                   help="this machine's integration records")

    apply_p = sub.add_parser("apply", parents=[common],
                             help="point a client at the gateway")
    apply_p.add_argument("--client", required=True,
                         help="one of: " + ", ".join(sorted(CLIENTS)))
    apply_p.add_argument("--base-url", required=True, dest="base_url",
                         help="gateway URL, e.g. http://gateway:8788/v1")
    apply_p.add_argument("--key", default=None,
                         help="gateway API key "
                              "(default: $WB_PROXY_API_KEY, then a prompt)")
    apply_p.add_argument("--model", default=None,
                         help="default model id to select in the client")
    apply_p.add_argument("--provider", default=None,
                         help="current provider of the gateway "
                              "(workbuddy | qoder | trae | egg); decides the "
                              "tag the models carry in the client's picker. "
                              "Guessed from the model list when omitted.")
    apply_p.add_argument("--models-from-gateway", action="store_true",
                         dest="models_from_gateway",
                         help="fetch the model list from /v1/models "
                              "(this is the default)")
    apply_p.add_argument("--no-models", action="store_true", dest="no_models",
                         help="skip the model list; also skips key validation")
    apply_p.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="print what would be written; touch nothing")

    restore_p = sub.add_parser("restore", parents=[common],
                               help="undo an apply, byte for byte")
    restore_p.add_argument("--client", required=True,
                           help="one of: " + ", ".join(sorted(CLIENTS)))

    args = parser.parse_args(argv)

    # A Chinese path or model name in the output must not abort the run on a
    # legacy Windows console - the same guard tests/run_all.py applies to
    # itself.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    as_json = getattr(args, "as_json", False)
    state_dir = getattr(args, "state_dir", None) or _default_state_dir()
    home = _resolve_home(getattr(args, "home", None))

    if args.command in ("list", "status"):
        data = overview(state_dir, home)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        for client_id in sorted(data):
            entry = data[client_id]
            print("%-12s %-18s %s" % (
                client_id, entry["label"],
                "installed" if entry["installed"] else "not detected"))
            for path in entry["config_paths"]:
                print("    config:  %s" % path)
            if args.command == "status":
                applied = entry.get("applied")
                if applied:
                    print("    applied: %s  base_url=%s  model=%s%s" % (
                        applied["at"], applied["base_url"],
                        applied["model"] or "-",
                        "  (external change detected)"
                        if applied["external_change"] else ""))
                else:
                    print("    applied: never")
        return 0

    if args.command == "apply":
        if args.no_models and args.models_from_gateway:
            return _cli_error(
                "--no-models and --models-from-gateway are mutually exclusive")
        client_id = str(args.client).strip()
        try:
            _client(client_id)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        base_url = str(args.base_url).strip()
        if not re.match(r"^https?://", base_url):
            return _cli_error("--base-url must start with http:// or https://")
        key = str(args.key or os.environ.get("WB_PROXY_API_KEY") or "")
        # Prompt only when there is a real console. On Windows isatty() is
        # also true for the NUL device (a common stdin for spawned or
        # service-run processes), and getpass there reads CONIN$ directly -
        # it would block forever in a process that has no console. stdout
        # being a terminal is the signal that one exists.
        if not key and sys.stdin.isatty() and sys.stdout.isatty():
            try:
                key = getpass.getpass("Gateway API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                key = ""
        if not key:
            return _cli_error(
                "no API key: pass --key, set WB_PROXY_API_KEY, or run "
                "interactively")

        models = []
        if not args.no_models:
            try:
                models = fetch_models_from_gateway(base_url, key)
            except AgentConfigError as exc:
                return _cli_error(
                    "%s - retry, or pass --no-models to configure without "
                    "the model list and key validation" % exc)

        # 当前提供商：命令行给了就用它；没给就从模型清单猜——清单里带免费源
        # 标记（egg_source）的就是鸡蛋的免费车道 / Kilo 免费池。
        provider = str(args.provider or "").strip().lower()
        if not provider:
            provider = "egg" if any(m.get("source") for m in models) \
                else "workbuddy"
        if provider not in PROVIDER_TAGS:
            return _cli_error(
                "unknown provider %r (known: %s)"
                % (provider, ", ".join(sorted(PROVIDER_TAGS))))

        if args.dry_run:
            try:
                built = plan(client_id, base_url, key, model=args.model,
                             models=models, home=home, provider=provider)
            except AgentConfigError as exc:
                return _cli_error(str(exc))
            if as_json:
                print(json.dumps({
                    "client": built["client"],
                    "label": built["label"],
                    "base_url": built["base_url"],
                    "model": built["model"],
                    "files": [{"path": item["path"],
                               "existed": item["existed"],
                               "content": item["content"]}
                              for item in built["files"]],
                }, ensure_ascii=False, indent=2))
                return 0
            print("dry run: nothing will be written")
            print("client:  %s (%s)" % (built["label"], built["client"]))
            print("gateway: %s" % built["base_url"])
            print("key:     %s" % _mask_key(key))
            for item in built["files"]:
                print()
                print("--- %s %s" % (
                    "modify" if item["existed"] else "create", item["path"]))
                print(item["content"], end="")
                if not item["content"].endswith("\n"):
                    print()
            return 0

        try:
            result = integrate(state_dir, client_id, base_url, key,
                               model=args.model, models=models, home=home,
                               provider=provider)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("configured %s (%s) -> %s" % (
            result["label"], result["client"], result["base_url"]))
        for entry in result["files"]:
            print("  %-8s %s" % (
                "updated" if entry["existed"] else "created", entry["path"]))
        if models:
            print("models:  %d fetched from the gateway (key accepted)"
                  % len(models))
        print("key:     %s" % _mask_key(key))
        print("state:   %s" % state_dir)
        print("restore: python %s restore --client %s"
              % (os.path.basename(__file__), client_id))
        return 0

    if args.command == "restore":
        client_id = str(args.client).strip()
        try:
            result = restore(state_dir, client_id, home=home)
        except AgentConfigError as exc:
            return _cli_error(str(exc))
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print("restored %s (%s)" % (result["label"], result["client"]))
        for entry in result["restored"]:
            print("  %-9s %s" % (entry["action"], entry["path"]))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
