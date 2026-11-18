# wb-proxy 常见问题

> 本文对应仓库 [cty1735/wb-proxy](https://github.com/cty1735/wb-proxy)。每条答案都可以独立引用；命令默认在项目根目录执行。

## 一、这是什么

### wb-proxy 是什么？

wb-proxy 是一个**本地自托管的 AI 网关**。它把 WorkBuddy（国际版 / 国内版）、Qoder、Trae 的多账号登录态，以及两路免账号的免费模型源，合并成一个 OpenAI 兼容接口，供 Claude Code、Codex CLI、OpenCode、DSH、Crush、ZCode、Hermes 等客户端使用。

对外提供三个协议入口：`POST /v1/chat/completions`（Chat Completions）、`POST /v1/responses`（Responses API），以及默认关闭的 `POST /v1/messages`（实验性 Anthropic Messages）。

### 它和 workbuddy2api-hub 是什么关系？

wb-proxy 以 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) 的 **v1.7.16** 为基座，完整保留其全部能力，并在其上增量开发。增量包括：Qoder / Trae / 鸡蛋三路通道、「路由」总网关（四网关合成一个模型池）、省 token、智能体一键配置、积分获取历史、剩余用量估算、更新通知中心等。逐条差异见 [COMPARISON.md](COMPARISON.md)。

### 它是破解工具吗？合不合法？

不是。它是一个**自托管协议网关**：只使用你自己拥有或已获授权的账号，在私有环境里把官方客户端已有的能力封装成标准 API。仓库不提供任何账号、额度或凭据，也不做商业转售。作者在 README 的「免责声明」里要求遵守各上游的服务条款。

## 二、上手

### 完全不登录账号，能用吗？

能。`?provider=egg`（「鸡蛋」）是**免账号、免密钥**的免费上游，聚合 OpenCode Zen 免费车道与 Kilo 免费池。克隆仓库、启动、直接请求即可：

```bash
git clone https://github.com/cty1735/wb-proxy.git && cd wb-proxy
python wb_proxy.py --port 8788
curl "http://127.0.0.1:8788/v1/chat/completions?provider=egg" \
  -H 'Content-Type: application/json' \
  -d '{"model":"kilo-auto/free","messages":[{"role":"user","content":"你好"}]}'
```

`GET /v1/models?provider=egg` 可以看到约 26 个免费模型（Kilo 免费池 18 + Zen 免费车道 8）。

注意：**Zen 免费车道按出口 IP 判定地区与滥用**，部分地区会整条返回 403 `not available in your country`，换出口（设置 → 账号代理槽 → 鸡蛋出口代理）即可；Kilo 免费池基本处处可用。

### 需要装什么依赖？

不需要。项目**只用 Python 标准库**，Python 3.9+ 即可运行，不用 `pip install`。Windows 绿色包自带精简 Python，双击 `start-wb-proxy.bat` 就能启动。

### 默认地址、端口、密码是什么？

- API Base URL：`http://127.0.0.1:8788/v1`
- Web 看板：`http://127.0.0.1:8788/`
- 面板访问密码：默认 `admin`（**首次登录后请立即修改**）
- API Key：本机单机模式可留空；开启局域网或配置过多 Key 后，用看板「设置」页生成的 Key

### 面板密码和 API Key 是一回事吗？

不是。面板密码只用来打开看板，以 PBKDF2-SHA256 摘要存在 `accounts/settings.json`（不存明文）；API Key 用于 `/v1` 接口鉴权，可以为不同客户端生成多把，并分别绑定出口与可用模型。`?pwd=` 参数只免手输登录面板，`?key=` 只给 `/v1` 用，两者不互相替代。

### 怎么部署到 NAS 或服务器？

三种方式，任选：

1. **一键脚本**：`curl -fsSL https://raw.githubusercontent.com/cty1735/wb-proxy/main/quick-deploy.sh | bash`（国内网络用 `gh-proxy.com` 前缀）；重复执行即平滑升级，数据不丢。
2. **单文件 Compose**：镜像 `ghcr.io/cty1735/wb-proxy:latest`，挂载 `./accounts` 与 `./usage` 两个目录。
3. **Watchtower**：`containrrr/watchtower --interval 86400 --cleanup wb-proxy` 每天自动拉新镜像。

镜像双架构（`linux/amd64` / `linux/arm64`），带 `HEALTHCHECK`（每 30 秒探 `/health`）。

### 升级会不会丢账号和用量？

不会。账号凭证、配置与 API Key 在 `accounts/`，用量流水在 `usage/`，两者都是宿主机挂载的目录，更新镜像或重建容器都不影响。升级前建议照常备份这两个目录。

## 三、账号与额度

### 一个 API Key 能同时用 WorkBuddy 和 Qoder 的额度吗？

能。不带 `?provider=` / `X-Provider` 请求 `/v1/models` 时，返回的是「路由」视图的合并清单，模型 id 形如 `模型名 · 供应商`（如 `DeepSeek-V4-Flash · Qoder-global`），后缀决定真实去向。一个客户端配一次 Base URL 和 Key 就能用全部已接入的网关。

### 四个网关之间怎么分流？

优先级固定：

1. 显式提示最高：`?provider=workbuddy|qoder|trae|egg` 或 `X-Provider` 头；
2. 没有提示时按模型归属兜底：WorkBuddy → Qoder → Trae → 鸡蛋，一个模型 id 只属于一家；
3. 出口（国际 / 国内）也是各家自己的，`?realm=` / `X-Realm` / Key 绑定的出口优先级最高。

### 账号怎么加？

看板 `http://127.0.0.1:8788/` 的账号区：

- **WorkBuddy**：点「+ 添加账号 (OAuth)」走浏览器授权；Windows 上也可以「扫描桌面客户端账号」直接从运行中的客户端导入（加密凭据就地解密）。
- **Qoder**：国际版可用浏览器设备码，国际 / 国内都可用 PAT（个人访问令牌）。
- **Trae**：按提供商的 OAuth 流程接入。
- **鸡蛋**：不需要账号。

### 支持哪些模型？

模型清单对齐官方桌面端：国际版 17 个、国内版 14 个（`deepseek-v4.1-flash`、`gpt-6-astra`、`glm-5.3`、`kimi-k3` 等），每项宣告上下文窗口、最大输出、视觉、工具调用与推理档位。清单与上游 `GET /v3/config` 保持同步，上游新上架模型无需发版即出现在 `/v1/models`。

目录里标 `x0.00` 不等于永久免费：活动到期或免费额度用尽后会按付费倍率标注，面板会显示「额度用尽 · 回落 x.xx」。

### 「限额护栏」是什么，怎么用？

设置 → 账号限额里有四条**账号级**护栏：保留积分、每日 Token 限额、每日积分限额、按模型每日 Token 限额。达到阈值时该账号暂停接单（或只停对应模型），请求自动切到其他账号，本地时间 0 点解封。默认全部关闭（填 0 即关闭），可以全局统一设，也可以国际版 / 国内版分别设。

### 剩余用量能估算吗？

能。上游按 24 小时窗口给「账号 × 模型」配额但不告知预算。看板「剩余用量估算」用**撞线账号当时的用量**反推预算，按区域聚合求平均；没有撞线记录的组合按下界（最近 24 小时用量）估算。接口 `GET /usage/remaining`，只读、不参与请求路径。

## 四、客户端与排错

### Claude Code 能直接接吗？

能，但**默认禁用**。网关对 Anthropic Messages 做协议转换，`cache_control` 会被忽略——已有上游 issue（#367）报告长会话缓存命中率仅约 11%，单次消耗可能超过 100 积分，所以默认返回 400。确认理解风险后，在网关侧设 `WB_ENABLE_ANTHROPIC_MESSAGES=1` 并重启，再配置：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_API_KEY="看板设置里生成的 Key"
```

Codex CLI 走 Responses API，OpenCode / DSH / Crush / ZCode / Hermes 走 OpenAI 兼容，均无此限制。

### 客户端在另一台电脑上，怎么配置？

看板的「一键配置」只改**网关所在机器**的配置，所以远程时用配置器 CLI：

```bash
curl -fsSL http://<网关地址>:8788/agents/cli -o wb_agents.py
python wb_agents.py apply --client codex --base-url http://<网关地址>:8788/v1 --key sk-xxxx
python wb_agents.py restore --client codex   # 随时字节级还原
```

支持 `claude-code` / `codex` / `opencode` / `dsh` / `crush` / `zcode` / `hermes`。

### 国内版账号全部失败，日志报 SSL / WinError 10054？

网关跑在海外出口后面时，到 `copilot.tencent.com` 的 TLS 握手会被复位。设 `WB_CN_CHAT_UPSTREAM=https://www.workbuddy.cn` 后重启即可（这是官方桌面端当前使用的 endpoint）。

### 请求返回 413 payload too large？

单请求体默认上限 16MB。需要更大就设 `WB_MAX_PAYLOAD_BYTES=33554432`（32MB），重启生效。

### 客户端在后台偷偷烧积分怎么办？

桌面客户端会把记忆生成等后台请求发到硬编码的付费模型上。设置 `WB_BACKGROUND_MODEL_NAMES`（要拦截的模型名，逗号分隔）与 `WB_BACKGROUND_MODEL_TARGET`（改写目标，默认 `deepseek-v4.1-flash`），网关会在选账号之前把 `model` 字段改写成免费目标。名单设为空即关闭。

### 报 CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain？

网关在 HTTPS 解密代理后面，或系统根证书没被 Python 读到。把签发该证书的根证书导出为 PEM，设 `WB_CA_BUNDLE=/path/to/ca.pem`（或 `SSL_CERT_FILE`）后重启。网关是追加信任，不会关闭证书校验。

### 「路由」页的 route/auto 是什么？

把模型写成 `route/auto`，网关按当前选定的策略在路由池里挑一个成员：`smart`（最快重置优先）、`order`（按池子顺序）、`rotate`（轮换）、`usage`（用得最少）、`pace`（重置前每小时剩余最多）。同一会话上一轮选中的成员仍可用时继续用它（`rotate` 除外），以保住提示词缓存。

### 怎么加自己的供应商？

鸡蛋视图 →「供应商池」→ 新增：填 Base URL、Key、协议（OpenAI 兼容或 Anthropic 兼容），拉取模型列表后勾选要暴露的模型。同一模型 id 同时存在于内置免费源与自建池时，**算自建池的**。配置存在 `accounts/egg-pools.json`，Key 明文保存，换机器整份拷走即可。

## 五、数据与安全

### 数据存在哪？

- `accounts/`：账号凭证、`settings.json`（API Key、限额、路由策略）、`egg-pools.json`（供应商池与平台账号）
- `usage/`：请求流水与用量统计
- `accounts/agent-backups/`：一键配置的客户端原件备份（每文件保留最新 10 份）

### 能不能暴露到公网？

默认监听 `127.0.0.1`。局域网共享用 `start-wb-proxy-lan.bat`（自动生成 Key）。**不建议直接暴露公网**：账号凭证在本机明文可读，面板也没有多用户隔离；若确实需要，请放在反向代理 + HTTPS + 访问控制之后。

### 日志与用量会外传吗？

不会。用量统计、账号状态、积分快照都只写本地文件。唯一的外部请求是更新通知正文（从 GitHub 的 `docs/notices.md` 拉取，可用 `WB_NOTICES_REMOTE=0` 关闭）与 OpenRouter 公开价格目录。

## 六、其他

### 提示词省 token 是怎么做的？

「设置 → 省 token」有两套提示词压缩（Caveman / Ponytail）、RTK 工具输出压缩，以及五类本地探针就地应答（不必发给上游）。统计写 `accounts/token-saver-stats.json`，单条请求可用请求头 `X-WB-Token-Saver: off` 绕过。

### 有测试吗？

有。`python tests/run_all.py` 跑全部套件（约 172 个：127 个 Python + 45 个 JS，JS 需要 PATH 上有 `node`）。CI 在 Ubuntu（Python 3.9 / 3.12）与 Windows（3.12）上跑同一条命令。

### 项目是 AI 写的吗？

是 100% Vibe Coding 协同产物：人类提出架构与业务意图，AI 完成逆向分析、链路调度、指纹脱敏与界面编写。代码全部开源（MIT），可自行审计。
