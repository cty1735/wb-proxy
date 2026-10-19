# 融合设计：把 dsh-our-free-model 的能力并入 wb-proxy

> 目标仓库：`Ebony-Vinyl/dsh-our-free-model`（Node/JS 的 DSH 插件）。
> 承接仓库：本项目（纯标准库 Python 独立网关）。
> 本文是**方案设计**，不含实现。分 P0–P3 四期，逐期可独立交付、可单独回滚。

---

## 0. 结论先行

- **能融合，但不能搬代码**：对方是宿主内插件（JS、零依赖、靠宿主能力），本项目是纯标准库 Python 服务器。唯一可直接复用的是**数据与规则**（提供商注册表、提示词文案、RTK 过滤规则），代码必须按 Python 风格重写。
- **最大障碍是范式冲突**：本项目请求路径全程假设"存在一个可用账号"（`POOL` 选号 → `derive_id` 指纹 → 额度护栏 → 按 uid 归因 → 剩余用量/计费），而对方最有价值的两个能力（**免密免费车道**、**自带 Key 聚合渠道**）**没有账号**。因此 P1 必须先补一层 `Channel`（渠道）抽象。
- **优先级**：P0 省 Token（低风险、立刻增益现有账号配额）→ P1 渠道抽象 + 聚合渠道（结构性升级）→ P2 免费车道 + 降级链（依赖 P1，含合规风险）→ P3 运维增强（升级自重启 / 公告 / 流式容错）。

---

## 1. 设计原则（贯穿全部工期）

| 原则 | 说明 |
|---|---|
| **渠道 ≠ 账号** | 无账号渠道绝不伪装成"假账号"塞进 `POOL`：会污染指纹、额度、计费、剩余用量四套逻辑。新增独立 `Channel` 抽象。 |
| **纯标准库** | 不引入 `requests` 等第三方依赖，保持"绿色包 + 零依赖"卖点。HTTP 一律 `urllib.request`。 |
| **新功能进新模块** | `wb_proxy.py` 已 ~1MB / 4.1 万行，新功能一律独立成 `wb_*.py`，只在 dispatch 处挂少量调用。 |
| **失败开放、默认关闭** | 新能力默认 off 或缺省即"行为逐字节不变"；中间件出错时放行原文。 |
| **数据可移植、代码要重写** | RTK 规则、提示词、registry 按数据搬运；实现重写并接入现有管线。 |

---

## 2. 现状锚点（本设计引用的现有实现）

| 位置 | 符号 | 作用 |
|---|---|---|
| `wb_proxy.py:13118` | `build_upstream_body(payload)` | WorkBuddy 请求体整理（角色归一、提示词模式、工具调用配对修复、thinking 注入） |
| `wb_proxy.py:13215` | `model_catalog_meta(model)` | 模型目录元数据（供 `/v1/models` 与能力宣告） |
| `wb_proxy.py:14145` | `open_upstream(...)` / `open_qoder_upstream(...)` / `open_trae_upstream(...)` | 三条上游的"选号 → 建请求 → 返回 SSE 字节源" |
| `wb_proxy.py:13814/13984` | `QoderUpstreamAdapter` / `TraeUpstreamAdapter` | 把上游私有流翻译成下游认的 `data: {...}` 序列（`read1()` 字节源） |
| `wb_proxy.py:17482` | `Handler._chat_provider(payload)` | 决定聊天走哪条上游（显式 `?provider=` 优先，否则按目录归属兜底） |
| `wb_proxy.py:20664/20949` | `_handle_messages(...)` / `_dispatch_chat_post(...)` | Anthropic Messages 与 OpenAI 形状的统一分发入口 |
| `wb_proxy.py:17585` | `_get_v1_models(query)` | `/v1/models` 路由 |
| `wb_settings.py` | `load/save/deep_merge/limits_*` | 全部运行期设置存 `accounts/settings.json` |
| `wb_accounts.py:226` | `PROVIDERS = ("workbuddy","qoder","trae")` | 现有账号型提供商枚举 |
| `wb_prompt.py` | `apply_prompt_mode(messages)` | 网关自有系统提示词模式（与"提示词瘦身"部分重叠） |
| `wb_catalog.py` / `wb_modelsdev.py` / `wb_probes.py` | 内置目录 / 上下文与输出上限 / 真实上限探测 | 模型清单与能力来源 |
| `dashboard.html` | `switchMainTab()` / `MAIN_TABS` / `.page-nav-item` | 单文件看板，按页签切页 |

---

## 3. P0 —— 省 Token 中间件（`wb_tokensaver`）

### 3.1 目标

在**请求发出前**压缩历史里的工具输出、可选地瘦身提示词，并在网关进程内直接应答 5 类"不需要上游"的请求。收益：现有账号的**每日 Token 限额**与**按模型每日 Token 限额**更耐用，且对**全部渠道**生效。

### 3.2 模块划分

```
wb_tokensaver.py        管线 + 统计 + 本地应答入口（对外唯一 API）
wb_tokensaver_rtk.py    工具输出过滤规则（git diff/status/log、grep、find、ls/tree、
                        构建与包管理器日志、编号文件转储、Cursor 搜索清单、重复日志行）
wb_tokensaver_prompts.py caveman / ponytail 两族提示词与档位文案（数据）
```

### 3.3 对外接口

```python
# wb_tokensaver.py
def compress_payload(payload, *, settings=None) -> dict:
    """就地/副本重写 payload['messages'] 里的 tool_result / role:"tool" 内容。
    返回同样含统计的 payload（统计挂在私有键 '_ts_stats'，出站前由调用方剔除）。
    幂等：对已压缩过的内容再调用结果不变。"""

def local_answer(payload) -> dict | None:
    """不需要上游就能答的 5 类请求。命中返回 OpenAI 形状响应体，否则 None。
    1) 额度探测  max_tokens==1 且内容含 'quota'      -> 'Quota check passed.'
    2) shell 前缀 <policy_spec> + 'Command:'          -> 命令前缀（检出注入则拒绝）
    3) 会话标题生成                                    -> 'Conversation'
    4) 建议模式                                        -> ''
    5) 文件路径提取                                    -> '<filepaths>…'
    """

def prompt_nudge(messages, family, level) -> list:  # 幂等、可撤销
    """按 family/level 注入或撤销提示词瘦身指令。"""
```

### 3.4 挂载点（最小侵入）

| 触点 | 位置 | 做什么 |
|---|---|---|
| 请求侧（全渠道） | `_dispatch_chat_post` 与 `_handle_messages` 开头，provider 判定后 | `payload = compress_payload(payload)`；三条上游（WorkBuddy/Qoder/Trae）与渠道因此**统一受益**，不必改 `build_upstream_body` 内部 |
| 本地应答 | 同上，压缩之后、选号/上游之前 | `ans = local_answer(payload)`；命中即直接按 `stream` 真假合成响应返回，**不占并发槽、不消耗额度** |
| 统计落账 | `record_usage(...)` | 追加字段 `rtk_in_bytes / rtk_out_bytes / rtk_blocks / ts_local` |

> 注意：不要在 `build_upstream_body()` 里做全渠道压缩——Qoder/Trae 走各自的 `*_chat_request` 构造，绕过它。

### 3.5 安全规则（照搬对方的硬约束）

1. **报错的工具结果永不触碰**（含 `is_error`、非零退出码、`Error/Exception/Traceback` 特征）。
2. 小于尺寸下限的块不碰。
3. **替换结果永不大于原文**；反之放弃该块。
4. 过滤器抛错 → 原样放行（fail-open）。
5. 整条管线**幂等**。

### 3.6 设置与面板

- 设置键（存 `accounts/settings.json`，全部缺省 = 现有行为）：
  `tokensaver_enabled`（默认建议 **on**，与对方一致，但读取侧按"缺省即关"更保守——见 §7 取舍）、
  `tokensaver_prompt_family`（`off`/`caveman`/`ponytail`）、`tokensaver_prompt_level`、
  `tokensaver_local`（5 个 bool）。
- 看板：新增「省 Token」页，展示"压缩前字节 → 压缩后字节、节省率、压缩块数、统计调用数"，数据取自 `record_usage` 同一批统计行。

---

## 4. P1 —— 渠道抽象 + 聚合渠道（`wb_channels`）

### 4.1 目标

把本项目从"3 家国内厂商专用网关"升级为**通用 OpenAI 兼容中枢**：写入第三方 Key（或本机免账号提供商）后，其模型以 `provider/model` 出现在 `/v1/models` 并可经本网关调用。

### 4.2 核心抽象

```python
# wb_channels.py
class Channel:
    id: str            # 'openrouter' / 'groq' / 'ollama' ...
    name: str
    category: str      # free | free-tier | apikey | oauth | adc | local
    wire: str          # 'chat' | 'responses' | 'messages'（线协议）
    base_url: str
    list_models: str | None      # 列表探测端点；None 表示不支持
    needs_key: bool              # local/free 车道为 False

class ChannelAdapter:            # 与 QoderUpstreamAdapter 同职责
    """把某渠道的响应翻译成下游管线认的 `data: {...}` SSE 字节源（read1()）。"""
```

### 4.3 数据：提供商注册表

- **数据文件**：`data/providers.json`（**纳入版本控制**；`catalog/` 是 gitignored 的运行期存档，不能放这里）。
- 字段：`id / name / category / auth / wire / base_url / list_models / key_env / models_override`。
- 来源：移植对方的 `src/providers/registry.json`（75 家，分类 free/apikey/oauth/local）；先落地主流 OpenAI 兼容的 10–15 家（OpenRouter、Groq、DeepSeek、Mistral、Together、NVIDIA NIM、SiliconFlow、Ollama…），其余按需补齐。
- 与 `/v1/models` 合并：`provider/model` 复合 id，去重后并入 `_get_v1_models`。

### 4.4 Key 存储与脱敏

- 键：`accounts/settings.json` → `providers: {id: {key, base_url, enabled, models}}`。
- **接口永不回显明文**：只返回 `hasKey` 与 `…abcd` 指纹；字段缺省=保留已存 Key，空串=清除。
- 落盘权限确认为当前用户可读（与既有 `settings.json` 一致），不写日志。

### 4.5 传输

- `urllib.request` 直连；`Timeout`、错误码语义与 `open_*` 对齐（401 刷新/换号、429 冷却、5xx 换渠道）。
- 线协议翻译：`chat` 直接透传；`responses` / `messages` 需要翻译器（概念上对应对方的 `stream.js`，用 Python 重写，产出统一 OpenAI chunk）。
- `oauth` / `adc` 认证的提供商：**照常列出但标注"不可驱动"**，不做交互式登录（与对方一致）。

### 4.6 分发与无账号路径

- `_chat_provider(payload)`：模型以已知渠道前缀开头 → 返回渠道 id。
- 无账号渠道**绕过**：`POOL` 选号、`derive_id` 指纹、额度护栏、剩余用量。用量行记为 `provider="channel"`、`account="channel:<id>"`，`/v1/models` 合并、看板按 provider 过滤照常工作。
- 面板：新增「聚合渠道」页（启用/停用、测试、发现模型、Base URL 覆盖、Key 指纹）。

### 4.7 与计价的衔接

- 渠道模型的"等价花费"优先取渠道自身价（若已知），否则沿用 `wb_pricing` 的 OpenRouter 匹配；未定价显示 `—`，不猜。

---

## 5. P2 —— 免费车道 + 降级链

### 5.1 免密免费车道（`category: free`）

- 在 P1 的 `Channel` 上扩展 `auth: none`，并支持 `session_mint` 钩子（部分上游要求每请求铸造会话 id / 特定 UA / 工具指纹——按上游说明实现）。
- 先只接**结构最简单**的一条（如 Kilo 公共池），验证通路后再评估其余。
- ⚠️ **合规与风控前置**：这是搭外部公共免费额度，需自行确认上游条款与封禁面；默认关闭，面板显著提示。

### 5.2 降级链 combo（`wb_combos.py`）

- 存储：`settings.json` → `combos: [{name, models:[...], strategy: fallback|round-robin, sticky: N}]`。
- 虚拟模型 `combo:<name>`：在 `_chat_provider` / 分发处解析为有序模型列表。
- 语义：**任何正文增量之前**失败的链接整段丢弃（思考增量不算正文）；一旦已交付正文，失败按正常方式结束该回合。
- 与现有"选号 + 冷却"协同：链内每个链接各自走其原有上游路径（含换号/冷却）。
- 容量取交集：链声明视觉仅当全部链接支持；上下文取最小值。

---

## 6. P3 —— 运维增强

| 项 | 做法 | 备注 |
|---|---|---|
| **应用内升级** | 扩展 `wb_updates.py`（现仅"发现"）：下载 release → SHA-256 校验 → 备份到 `accounts/backups/` → 原子替换 → 写重启标记 → 进程退出，由启动脚本/宿主重启 | **不做热重载**：Python 模块级全局状态太多，热重载不可靠；用"自重启"替代 |
| **公告中心** | 新增 `wb_feed.py`：远程 JSON feed 多源拉取、校验、缓存、到达检测；面板加一页（未读计数、紧急徽章、标记已读、轮询；系统通知用浏览器 `Notification` API） | 独立小功能，风险最低 |
| **流式响应体形状判定** | 在上游流读取处嗅探首段字节：`content-type` 声称 JSON 但实为 SSE 帧序列时，按 SSE 解析并把已嗅探字节重新注入 | 稳健性补丁，低风险 |
| **可用性按出口探测** | 复用 `wb_ipintel` / `wb_egress` / `wb_probes`，为 `/v1/models` 增加"实测可用 / 地区受限 / 不在清单"分组提示 | 可选 |

---

## 7. 存储 / 接口 / 兼容

### 7.1 `accounts/settings.json` 新增键（全部缺省 = 现有行为）

```jsonc
{
  "tokensaver_enabled": true,          // 见下方取舍
  "tokensaver_prompt_family": "off",   // off | caveman | ponytail
  "tokensaver_prompt_level": "off",
  "tokensaver_local": {"quota":true,"shell":true,"title":true,"suggest":true,"paths":true},
  "providers": { "<id>": {"key":"…","base_url":"","enabled":true,"models":[]} },
  "combos": [ {"name":"…","models":["…"],"strategy":"fallback","sticky":3} ],
  "feed_seen": ["<公告id>"]
}
```

> **默认值取舍**：省 Token 对"有配额"的本项目收益直接，但会改写发给模型的内容。建议 `tokensaver_enabled` 默认 **on**（与对方一致）但在面板给出开关与节省统计，让用户可关；若担心行为变化影响既有用户，可保守取 **off**，由用户在面板开启。**二选一，实施前定稿。**

### 7.2 新增 HTTP 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tokensaver` | 压缩统计（字节、节省率、块数） |
| POST | `/tokensaver/config` | 省 Token 开关/档位（面板会话） |
| GET | `/channels` | 聚合渠道列表（含 `hasKey` 与指纹，**不含明文**） |
| POST | `/channels/config` | 启用/停用、写 Key、覆盖 Base URL 与模型清单（面板会话） |
| POST | `/channels/discover` | 列表探测 + 拉取该渠道模型（面板会话） |
| GET | `/combos` · POST `/combos/config` | 降级链读写（面板会话） |
| GET | `/feed` · POST `/feed/seen` | 公告 feed 与已读（面板会话） |

### 7.3 兼容性

- 现有 `?provider=workbuddy|qoder|trae` 语义**逐字节不变**；渠道只在"模型带渠道前缀"或"显式渠道提示"时激活。
- `/v1/models` 合并渠道与链路行，但**不改变**原有 WorkBuddy/Qoder/Trae 行的字段。
- 新设置键缺失时全部走旧行为。

---

## 8. 测试计划（关键前置）

> ⚠️ 当前仓库**没有 `tests/` 目录**（README 声称 148 套件，`git ls-files` 未见）。融合属大改动，**建议 P1 之前先补最小冒烟测试**，否则回归无安全网。

| 层 | 用例 |
|---|---|
| 单元 | RTK 过滤：大 `git diff` 压缩率、报错结果不触碰、替换不长于原文、幂等 |
| 单元 | 本地应答：5 类命中/不命中边界；命令注入拒绝 |
| 单元 | 渠道 registry 解析、`provider/model` 复合 id、Key 脱敏 |
| 集成 | 三条原有上游在"省 Token 开/关"下响应逐字节可比（关 = 现状） |
| 集成 | 渠道模型的 chat / responses / messages 三线流式与非流式 |
| 集成 | combo fallback / round-robin；正文前失败整段丢弃 |
| 面板 | 新页签切换、`hidden_pages` 兼容、无 Key 时不泄露明文 |

---

## 9. 里程碑与工作量（粗估，人日）

| 期 | 内容 | 估时 | 依赖 | 交付判据 |
|---|---|---|---|---|
| P0 | `wb_tokensaver*` + 面板统计 + 测试 | 4–6 | 无 | 现有账号在开关下不发上游差异，开启后节省率可量化 |
| P1 | `Channel` 抽象 + registry + Key 存储 + 面板页 | 8–12 | P0（可选） | 至少 10 家 OpenAI 兼容渠道可列、可调、Key 不回显 |
| P2 | 免费车道 1 条 + `wb_combos` | 6–9 | P1 | 免密模型可调；链路降级对调用方不可见 |
| P3 | 升级自重启 / 公告页 / 流式容错 | 5–8 | 无 | 应用内升级成功且失败可回滚；公告到达 |

---

## 10. 风险与开放问题

1. **无测试网**（最高）：动手前补最小冒烟测试。
2. **合规/风控**：免费车道与"白嫖"渠道本质是搭外部免费额度；本项目已有指纹/护栏体系，叠加匿名大批量调用会扩大封禁面。**默认关闭**并显式提示。
3. **上游单点维护**：registry 与免费车道跟着上游变，移植后维护成本长期存在。
4. **热重载不可行**：Python 无可靠方案，一律用自重启替代。
5. **单文件膨胀**：务必新功能进新模块，禁止继续往 `wb_proxy.py` 堆。
6. **`tokensaver_enabled` 默认值**：on（对齐对方、收益直接）vs off（最保守）需定稿。

---

## 11. 明确不做

- 13 条"白嫖"账号渠道（CodeArts/LobsterAI/Loomy/Raccoon…）：每条都要独立逆向 + 长期维护，性价比远低于聚合渠道。
- EAC 协付渠道：强绑定对方桌面端生态 + GitHub 授权闸门。
- 整体 UI 换皮（11 页毛玻璃）：本项目看板更完整，得不偿失；仅按需借鉴具体控件。
- 独立 standalone 服务：本项目本身就是独立服务。
- 原生 Anthropic `/v1/messages` / count_tokens / 局域网共享：**已有**。

---

## 12. 实施记录（第 0 步 + P0 已完成）

> 本节记录实际落地的代码与对前面设计的三处偏差；**设计正文保持原样**，以本节为准。

### 12.1 第 0 步 · 地基（provider 维度）

- `wb_accounts.py` 新增常量 `CHANNEL_PROVIDER = "channel"` 与 `provider_or_none(value)`：
  已知厂商名或 `channel` 返回规范化值，**未知返回 `None`**——刻意区别于
  `normalize_provider()` 的「未知一律兜底成 workbuddy」。这是 P1 分辨「渠道 id vs
  厂商名」的地基，避免渠道用量被静默记到 WorkBuddy 账上。
- **现有 `normalize_provider` / `provider_filter` / `row_provider` 语义一字未改**，
  所以本步对现网行为零影响。

### 12.2 P0 · 省 Token（`wb_tokensaver*`）

新增三个模块（纯标准库、互不依赖 `wb_proxy`，可单测）：

| 文件 | 职责 |
|---|---|
| `wb_tokensaver_rtk.py` | 工具输出压缩过滤：git diff 裁上下文、折叠连续重复行、折叠构建/下载进度噪音、超长行截断、掐头去尾省略中段；`looks_like_error()` 命中 traceback/fatal/panic 直接放弃 |
| `wb_tokensaver_prompts.py` | 提示词瘦身数据（caveman 电报体 / ponytail YAGNI 各档）+ **幂等**注入 / 撤销（固定标记 `[wb-proxy:tokensaver]`） |
| `wb_tokensaver.py` | 三个入口 `compress_payload` / `apply_prompt` / `local_answer`，统计落盘与汇总，以及流式本地应答用的 `LocalUpstreamAdapter` |

**挂载点（2 处，覆盖三种协议 + 三个提供商）**

- `Handler._dispatch_chat_post()` 顶部：`/v1/chat/completions`、`/v1/completions`、`/v1/responses`，以及
  Qoder / Trae 两个分支（payload 在进入各自处理器前就被就地压缩）。
- `Handler._handle_messages()` 顶部：原生 Anthropic Messages。
- 压缩在**进入上游之前**发生，所以「没有可用账号」（503）这类失败同样留下压缩统计。

**对设计的三处偏差（已在代码里生效）**

1. **压缩点从「2 个 handler」精确到「chat_req 成型前」**：`chat` 走 `messages`、`anthropic`
   走 `content[].tool_result`、`responses` 走 `input[].function_call_output`，三种形状各
   自处理，而不是只看 `messages`。
2. **统计落 `usage/tokensaver.jsonl`，不进 `usage.jsonl`**：主用量文件被
   `_usage_snapshot` / `count_usage_rows` / `_perf_*` / `_remaining_*` 大量逐行扫描，加
   字段的连带面太大；新文件只写一行 JSON，`GET /tokensaver` 尾部读 4MB 汇总。
3. **本地应答默认全关**（设计里是「默认全开」）：判据是启发式规则，本网关服务
   OpenCode / Claude Code / Codex 等通用客户端，误答成本高于收益，改为面板逐项开关。

**默认值定稿**：`tokensaver_enabled` = **开**、`tokensaver_rtk` = **开**
（RTK 只缩不涨、失败开放，默认开才拿得到收益）；`prompt_family/level` = **off**、
`local.*` = **全关**。全部键缺失即此默认。

**本地应答的响应形状**：命中即进程内作答，**不写 `record_usage`**（避免像设计
初稿那样把 503 之外的本地行记进主用量、并被 `row_provider` 误归到 WorkBuddy）。
- Chat 非流式 → 直接给 `chat.completion`；流式 → `chat_text_chunk` + `chat_finish_chunk` + `[DONE]`。
- Anthropic 非流式 → `chat_to_messages(obj)`；流式 → `stream_messages_events(LocalUpstreamAdapter(...))`。
- 命令前缀抽取遇到 `; & | \` $ ( ) { } < >` 或换行即判定为注入，拒绝本地应答、放行到上游。

**接口与面板**

- `GET /tokensaver`（统计 + 当前设置）、`POST /tokensaver/config`（白名单键写入，需面板会话；
  非法族/档位在 `wb_settings` 侧归一化为 `off`）。
- `wb_settings.py` 新增 `tokensaver_*` 读 / 写访问器与 `tokensaver_config(dir)`（一次读盘）。
- `wb_proxy.py` 对配置做 2 秒 TTL 缓存，`POST /tokensaver/config` 写入后立即失效。
- `dashboard.html` 新增顶部页签「省 Token」与 `#pageTokensaver`（总开关与压缩 / 提示词瘦身 /
  本地应答 / 节省统计四区，侧栏由 `initPageNav` 自动生成）；`hidden_pages` 形状校验即可
  接受 `tokensaver`，前后端无需额外登记。

### 12.3 验证

- 纯模块冒烟：`wb_tokensaver*` 三个模块共 28 项断言（压缩率、报错不触碰、不长于原文、幂等、
  三协议形状、配置归一、本地应答边界、命令注入拒绝、统计汇总、假上游 SSE 形状）全过。
- 进程内端到端（不联网）：本地应答 200 且正文正确、无账号时 503 但已留下压缩统计、
  `GET /tokensaver` 汇总正确、关闭时不再本地应答、Anthropic 信封正确。
- 面板接线：`GET /` 含新区块、面板登录、`POST /tokensaver/config` 生效并落盘、非法档位归一、无 token 401。
- `dashboard.html` 全部内联脚本过 `node --check`。

### 12.4 仍未做（待在 P1 收口）

- `provider_or_none()` 尚无调用方：它服务于 P1 的渠道分发，现在只是地基。
- 提示词瘦身对 `/v1/responses` 不生效（该协议用 `instructions` 而非 `messages`，见 §4.6）。
- 本地应答不覆盖 Responses 协议（Codex 不发这类探测请求）。
- 渠道（`wb_channels*`）、combo、feed、升级自重启仍为设计态。
