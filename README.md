# wb-proxy — 基于 workbuddy2api-hub 的增量增强版

> **本项目仓库**：**[cty1735/wb-proxy](https://github.com/cty1735/wb-proxy)**

> 本项目以 **[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)** 的 **v1.7.16** 为基座：**完整保留其全部原始能力与实现细节**（见下方「原始项目介绍」与后续技术章节），并在此基础上持续叠加改进与修复。
>
> 相对上游基座多做了什么，见下方「增量功能」。

## 增量功能

相对上游基座（v1.7.16）新增的功能（日期为落地当天）：

- **2026-10-23｜新功能通知中心**：右上角新增铃铛图标与未读徽标，登录后自动展开未读通知；点「不再显示」按通知 id 记在网关本机（`settings.json` 的 `notices_dismissed`），换浏览器也记得。正文默认从 GitHub 的 **[docs/notices.md](docs/notices.md)** 实时读取：服务端缓存 20 秒，打开铃铛会强制回源，直连失败时依次走 gh-proxy、Gitee、本地副本；`WB_NOTICES_URL` 可换地址，`WB_NOTICES_REMOTE=0` 强制只读本地。支持 `feature` / `improve` / `fix` / `notice` 四种标记与中英 / 正體中文文案。接口：`GET /notices`（`?refresh=1` 跳过缓存）、`POST /notices/dismiss`（均只认面板会话）。
- **2026-10-23｜「路由」总网关**：顶部新增「路由」页签（默认视图），把 WorkBuddy / Qoder / Trae（各含国际、国内）+ 鸡蛋的全部模型合成一个池子；`/v1/models` 不带 `?provider=` 时返回合并清单，id 为 `模型名 · 供应商`，后缀优先决定去向。策略 `smart` / `order` / `rotate` / `usage` / `pace`（参考 magpie），客户端用 `route/auto` 或 `route/<策略>`；四个网关标签只切视图，不再改网关上游。
- **2026-10-22｜省 token（提示词压缩 / RTK 工具输出压缩 / 本地应答）**：设置页新增「省 token」一块。**Caveman** 七档（关闭 / 轻 / 全 / 极 / 文言轻 / 文言 / 文言极）与 **Ponytail** 四档（YAGNI）往 system 提示词尾部追加写作指令 —— 这是唯一能省**输出** Token 的一块，默认关闭。**RTK** 按内容识别 git diff / status / log、grep、find、ls、tree、构建日志、带行号的文件转储与重复日志行，只压缩 `role=tool` 的结果（默认开；小于 500 字节、报错类结果、压完不比原文小的一律原样）。**本地应答**把额度探测 / 命令前缀 / 会话标题 / 建议模式 / 文件路径提取五类探针就地回掉，不占上游额度（默认关闭）。省下的字节按本机真实统计显示（压缩前 / 后 / 节省 / 块数 / 调用数 / 提示词注入）。单条请求带 `X-WB-Token-Saver: off` 可整体绕过；开关与档位存在 `accounts/settings.json`，统计存在 `accounts/token-saver-stats.json`。
- **2026-10-22｜可用性详情弹框**：点「可用性」徽标看这条结论的详情（失败原因、耗时、首字），可复制、可就地重测。
- **2026-10-21｜平台账号池**：鸡蛋视图新增「平台账号」区 —— 华为云 CodeArts（浏览器授权）、Cline（设备码）登录即用，凭据自动续期。
- **2026-10-21｜模型清单逐条测试 / 启停**：每行可真发一次请求测可用性，也可停用某个模型（停用后客户端选择器里不再出现）。
- **2026-10-21｜模型清单「可用性」列 + 一键测试**：新增实测状态列（绿/红），表头可一键批量测试；结论记在本机，24 小时内有效。
- **2026-10-21｜模型清单列头固定**：表头吸在滚动区上沿，滚到下面仍对得上号。
- **2026-10-21｜四个网关彻底隔离**：WorkBuddy / Qoder / Trae / 鸡蛋之间模型、请求、出口互不影响；显式 `?provider=` 优先。
- **2026-10-21｜「全部提供商」含本机非网关会话**：看板这一格 = 经网关的请求 + 本机各客户端自己的会话，不重复计算。
- **2026-10-21｜看板的「本地」口径**：单看本机 OpenCode / Claude Code / Codex 的会话用量，排除经本网关的请求。
- **2026-10-20｜鸡蛋免费网关**：免账号、免密钥的免费上游，`?provider=egg` 直接用。
- **2026-10-20｜自建供应商池**：可挂多个 OpenAI / Anthropic 兼容上游，拉目录、挑模型、测 Key。
- **2026-10-20｜代理槽**：鸡蛋的所有出口也能走指定代理槽，代理不可用自动回退直连。
- **2026-10-17 ~ 10-20｜模型清单增强**：按池子 / 提供商筛选、只看免费、按 id 搜索；客户端模型名后带来源标签。
- **2026-10-17 ~ 10-18｜更多提供商**：新增 Qoder、Trae（国际 / 国内）通道与 OAuth 接入，账号、模型清单、用量按提供商维度打通。

> 完整更新历史见 **[docs/CHANGELOG.md](docs/CHANGELOG.md)**。

---

## 原始项目介绍（上游基座）

### WorkBuddy2API-Hub — 国际版、国内版多账号网关中枢

<p align="center">
  <a href="https://github.com/cty1735/wb-proxy/releases"><img src="https://img.shields.io/badge/Release-v1.7.16-2496ED?style=flat-square" alt="Version 1.7.16"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
</p>

把腾讯 **[www.workbuddy.ai](https://www.workbuddy.ai)**（国际版）与 **[codebuddy.cn](https://www.codebuddy.cn)**（国内版）的原生服务封装成标准 OpenAI 兼容接口（Chat Completions / Responses），并提供默认关闭的实验性 Anthropic Messages 适配；同时补齐多账号调度与运维能力：

- **开箱即用**：绿色包自带精简 Python（Windows），双击脚本即启；
- **双区域独立路由**：国际版 / 国内版各自配置与调度，看板一键切换，状态落盘；
- **协议与风险**：默认支持 Chat Completions、Responses API（Codex）；Anthropic Messages（Claude Code / Anthropic SDK）经协议转换且缓存兼容性尚未验证，默认禁用，仅可显式选择启用；
- **模型目录对齐官方桌面端**：剔除补全通道与专线变体，能力与规格按桌面端宣告，上游新模型无需发版即进 `/v1/models`；
- **设备指纹隔离 (`derive_id`)**：以账号 UID 稳定派生机器码与会话标识，同一账号不漂移、不同账号互不关联；
- **账号接入**：看板点链接走浏览器 OAuth；Windows 上还能直接从运行中的桌面客户端导入（加密 token 就地解密），macOS / Linux 可手动粘贴解码密钥后导入；
- **每日自动化**：国内版签到 / 成长与积分任务 / 猫猫旅行；国际版每日活跃打卡；后台调度器按 09:00 & 21:00、22:00、01:00 排程；
- **限额护栏**：保留积分 · 每日 Token · 每日积分 · 按模型 Token 四条账号级护栏，单账号独立计数，本地 0 点解封（默认全部关闭）；
- **OpenRouter 价估算**：把 token 折算成等价花费（人民币 / 美元可切）；
- **多 Key 与出口绑定**：每个 Key 可固定走国际版或国内版、可限定模型，用量按 Key 归属；
- **Web 看板**：指标卡片、模型性能与用量大表、请求流水、账号与任务管理一屏可查。

> ⚡ **Vibe Coding 产物**：本项目为 100% Vibe Coding 协同产物——人类开发者提出架构与业务意图，AI 助手端到端完成逆向分析、链路调度、WAF 指纹脱敏与界面编写。

---

## 一、快速启动

### 0. 只想用免费模型？不用登录任何账号

**鸡蛋网关（`provider=egg`）不认账号、不认 Key**：Zen 免费车道与 Kilo 免费池都是公开的免费上游，克隆下来启动就能调 —— 不需要 OAuth、不需要任何凭据、不需要先登录面板。

```bash
# 1. 拉下来（Python 3.9+，只用标准库，不用 pip install）
git clone https://github.com/cty1735/wb-proxy.git && cd wb-proxy

# 2. 启动（Windows 双击 start-wb-proxy.bat；macOS / Linux 用系统 Python）
python wb_proxy.py --port 8788

# 3. 直接调（第一次不用等目录拉完，兜底清单已经在手上）
curl "http://127.0.0.1:8788/v1/chat/completions?provider=egg" \
  -H 'Content-Type: application/json' \
  -d '{"model":"kilo-auto/free","messages":[{"role":"user","content":"你好"}]}'
```

看有哪些模型：`curl "http://127.0.0.1:8788/v1/models?provider=egg"`（26 个左右：Kilo 免费池 18 + Zen 免费车道 8）。

**上手要点**

- **第一次不卡**：冷启动时 `/v1/models` 立刻返回内置的兜底清单（毫秒级），真实目录在后台慢慢拉，几秒后自动换成上游的实时名单；
- **哪一路能用取决于出口 IP**：**Kilo 免费池**不需要任何凭据，基本处处可用；**Zen 免费车道**按出口 IP 判地区与滥用，部分地区会被整条拒掉（403 `not available in your country`）—— 换个出口就能用：在「设置 → 账号代理槽」里加一个槽位（可点「自动发现 mihomo 端口」），再把「**鸡蛋出口代理**」选成它，Zen 免费车道、自建供应商池的请求都会从那个出口出去；代理连不上会自动回退直连，不会整个哑掉；
- **面板也能用**：`http://127.0.0.1:8788/`（默认密码 `admin`）→「鸡蛋」页签看模型清单、挑池子；「设置」页管 Key、代理槽与限额；
- **要接自己的上游**：「鸡蛋 → 供应商池」里加一个池子（Base URL + Key + 挑模型），它的模型会直接出现在清单里；一个模型 id 只能有一个去处，池子勾中的 id 会顶掉内置免费源同名的那条（卡片上会写明顶掉了几个）。

### 1. 本机运行

**Windows** 双击 **`start-wb-proxy.bat`**，保持窗口运行；**macOS** 双击 **`start-wb-proxy.command`**（首次被 Gatekeeper 拦截时右键 →「打开」确认一次），或在终端执行 `./start-wb-proxy.sh [端口]`（默认 8788）。

启动后：**API 地址** `http://127.0.0.1:8788/v1`，**Web 看板** `http://127.0.0.1:8788/`。首次启动还没有账号，打开看板点「+ 添加账号 (OAuth)」完成授权即自动入库。

> 绿色包自带的精简 Python 运行时只覆盖 Windows；macOS / Linux 直接使用系统 Python，需要 **3.9+**（启动脚本找不到内置运行时会明确打印所用的解释器与版本）。Docker 部署自带基础镜像，不受影响。

> zip 解压后若提示权限不足，先执行一次：`chmod +x start-wb-proxy.sh start-wb-proxy.command start-wb-proxy-lan.sh start-wb-proxy-lan.command allow-firewall.command`

### 2. 面板访问密码

看板需要**面板访问密码**（默认 `admin`），它与 API Key 相互独立：密码只用于打开看板，可在「设置」页修改（或启动时 `--panel-password` 指定），以 PBKDF2-SHA256 摘要存于 `accounts/settings.json`（不存明文），登录态只留在浏览器会话里。

本机 / 受信局域网自用可带 `?pwd=面板密码` 免手输直接进面板（如 `http://127.0.0.1:8788/?pwd=admin`）；公网暴露时不要用——密码会留在浏览器历史与反向代理日志里。注意它和局域网共享的 `?key=` 不同：`?key=` 只把 API Key 存给 `/v1` 用，不会自动登录面板。**首次登录后请立即改掉默认密码。**

### 3. 局域网共享

- **Windows** 双击 `start-wb-proxy-lan.bat`；**macOS** 双击 `start-wb-proxy-lan.command`，或 `./start-wb-proxy-lan.sh [端口] [Key]`；
- **Base URL** `http://<本机局域网IP>:8788/v1`；带密钥直达面板 `http://<IP>:8788/?key=生成的Key`；
- **API Key**：首次启动生成高强度随机 Key，写入 `accounts/settings.json` 并打印在终端，重启复用；也可用第二个参数指定自己的 Key；
- **macOS 防火墙**：首次监听端口时选「允许」；macOS 15+ 还要在「系统设置 → 隐私与安全性 → 本地网络」允许终端，`./allow-firewall.command` 可查看状态并加白。

### 4. 多 API Key 与出口绑定

「设置」页可管理多个 Key，每个 Key 独立出口——不同客户端各用各的 Key，国内外流量互不干扰：

- **生成 / 绑定**：填名称点「生成随机 Key」；可固定走 🌐 国际版或 🇨🇳 国内版，不绑定则跟随看板顶部的全局出口开关；
- **模型限制**：可填允许的模型（如 `deepseek*`、`gpt-6-astra`，支持 `*`，逗号分隔），留空不限；不在列表里的请求本机直接返回 400，不送达上游、不消耗额度；
- **区域自检**：Key 绑定的出口与模型不匹配时（如国际版 Key 调国内独占的 `deepseek-v4-pro`）直接返回可读 400，而不是上游晦涩的 WAF 报错；
- **启停 / 删除**：可单独启停；删除后密钥立即失效，条目以只读形式留在「设置」页的「已删除」折叠区，历史用量仍显示它的名字；
- **用量归因**：「数据看板」页按 Key 列出请求数、Token、缓存命中、积分与模型分布，口径与账号表一致；
- **防冲突**：面板保存过 Key 后，启动参数里的 `--api-key` 自动失效。

### 5. Docker 部署

预编译双架构镜像（`linux/amd64`、`linux/arm64`）发布在 GHCR 与 Docker Hub，**无需克隆代码、无需本地编译**：

**方式一：一键脚本（推荐）**

```bash
# GitHub 源（可直连 GitHub 环境）：
curl -fsSL https://raw.githubusercontent.com/cty1735/wb-proxy/main/quick-deploy.sh | bash

# 国内网络 / NAS 加速（Connection reset 时用）：
curl -fsSL https://gh-proxy.com/https://raw.githubusercontent.com/cty1735/wb-proxy/main/quick-deploy.sh | sudo bash
```

NAS（如飞牛 fnOS）普通用户没有 Docker 权限时，把结尾的 `bash` 换成 `sudo bash`。再次运行同一条命令即平滑升级，账号与用量数据不受影响。

**方式二：NAS / 面板单文件 Compose**

在 NAS 或面板的 Compose 界面新建项目并粘贴以下内容保存启动，无需拉取源码：

```yaml
services:
  wb-proxy:
    image: ghcr.io/cty1735/wb-proxy:latest   # 或 cty1735/wb-proxy:latest
    container_name: wb-proxy
    restart: unless-stopped
    ports:
      - "8788:8788"          # 左侧宿主端口可自选；右侧必须与下面的 PORT 一致
    environment:
      - HOST=0.0.0.0
      - PORT=8788
      # - API_KEY=your_secret_key   # 留空则自动生成并打印在启动日志
      - TZ=Asia/Shanghai
    volumes:
      - ./accounts:/app/accounts    # 账号凭证与配置（更新/重建容器不丢）
      - ./usage:/app/usage          # 用量流水日志（更新/重建容器不丢）
```

更新：面板里点「拉取最新镜像并重启」，或 `docker compose pull && docker compose up -d`。

**方式三：Watchtower 自动静默更新**

```bash
docker run -d --name wb-proxy-watchtower --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower:latest --interval 86400 --cleanup wb-proxy
```

**补充与排错**

- **持久化**：`./accounts` 与 `./usage` 由宿主机挂载，更新或重建容器都不丢数据；
- **docker run 快捷启动**：
  ```bash
  docker run -d --name wb-proxy --restart unless-stopped -p 8788:8788 \
    -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
    ghcr.io/cty1735/wb-proxy:latest
  ```
- **鉴权**：容器以 `--lan` 启动，无显式 `API_KEY` 时自动生成 Key 写入 `./accounts/settings.json` 并打印在日志里：`docker compose logs wb-proxy | grep -i "api key"`；
- **镜像名要带 registry**：写成 `cty1735/wb-proxy` 会去 Docker Hub 找并报 `pull access denied`，正确写法是 `ghcr.io/cty1735/wb-proxy:latest`（GHCR 公开包，拉取无需登录）；
- **目录权限**：默认以 root（`0:0`）运行；想以宿主用户跑就设 `PUID` / `PGID`（或 `--user $(id -u):$(id -g)`），并确保两个挂载目录对该 uid 可写；
- **健康检查**：镜像自带 `HEALTHCHECK`（每 30s 探一次 `/health`），`docker ps` 的 STATUS 列会显示 healthy / unhealthy；
- **源码构建**：`docker compose -f docker-compose.build.yml up -d --build`。

### 6. 测试

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只跑名字里含 realm 的
```

- 172 个套件：127 个 Python + 45 个 JS；JS 需要 PATH 上有 `node`，缺失时会跳过并提示。
- `tests/_mobile_check.py` 是独立的 Playwright 手机/桌面布局检查器（需自行安装 Playwright），按需手动运行，不在上面的套件集里。
- CI（`.github/workflows/tests.yml`）跑同一条命令：Ubuntu 上 python 3.9 与 3.12（3.9 是本项目声称的最低版本），Windows 上 python 3.12；推送 `v*` tag 时额外断言 **tag == 源码版本**（`-ci` 演练 tag 豁免）。

---

## 二、核心特性

### 1. 模型列表对齐官方桌面端

对官方本地配置清单（50+ 底层模型）做清洗：剔除行内补全专用模型（`codewise-*`、`completion-gf`、`hunyuan-3b/7b` 等）与多云专线变体（`*-volc`、`*-lkeap` 等），严格对齐官方 Windows 桌面端，并宣告每个模型的上下文窗口、单次最大输出、视觉支持、工具调用与推理档位。

- **🌐 国际版（17 个）**：`hy4-preview-f`、`hy3`、`deepseek-v4.1-flash`、`gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.5`、`gpt-5.4`、`grok-4.7`、`gemini-3.5-flash`、`glm-5.3-flash`、`glm-5.3`、`glm-5.2`、`kimi-k3`、`kimi-k2.6`、`kimi-k2.8-preview`
- **🇨🇳 国内版（14 个）**：`hy4-preview-f`、`hy3`、`deepseek-v4.1-flash`、`deepseek-v4-pro`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`minimax-m3`、`kimi-k3-1`、`kimi-k2.8-preview`、`kimi-k2.7`、`kimi-k2.6`

清单与上游 `GET /v3/config` 的 `agents[cli].models` 保持同步（接口不可用时依次回落到桌面端缓存文件、内置快照）；过滤规则：去掉 5 个档位别名与 `auto`、去掉 `-sg` / `-x` 变体、同名的只留 0.00 倍率那一档。上游新上架的模型无需发版即出现在 `/v1/models`。

> 同名模型（如 `deepseek-v4.1-flash`）在**单网关视图**里仍按「当前所选出口」走，不做跨国内 / 国际账号的自动混跑。要让两个区域（以及四个网关）之间按策略挑一个，用「路由」视图的池子模型或 `route/auto`：每条候选自带 (网关, 出口)，选路不会改变单网关的默认出口，也不影响出站指纹对齐。

### 2. 设备指纹隔离（`derive_id`）

国际版与国内版共用同一套算法内核：以账号 UID 结合固定业务盐值单向哈希派生机器码与会话标识——同一账号每次出站都来自同一台虚拟设备（不随机漂移），不同账号之间彼此独立（阻断跨账号关联风控）。

### 3. 国内版自动化

- **每日签到**：一键完成国内版打卡领积分；
- **自动连续打卡与连登管家**：每日定时为国内版账号上报轻量会话事件（复刻客户端 `chat_request_send`），点亮官方成长中心连登天数与热力墙；自动闭环检查昨日漏签（有补签卡则自动补签保连登）、领取新手礼包/补偿、兑换 7d/14d/28d 连登档位奖励，并自动抽完抽奖次数；
- **成长任务与积分任务**：自动批量接取未接任务，构造规范行为事件上报点亮（画布创建、灵感案例、模板使用、模型体验、多轮对话等 14 项），并自动领奖入账；
- **猫猫日常**：自动检查旅行状态，在家自动派出、归来自动领奖；
- **今日新增积分（任务与福利页）**：与「已获取福利总积分」同源同判据——只框定今天，副标给出近 7 天与昨日；整卡点开「每日福利积分明细」，按发放日期分天列出每天领了多少（当天再按「账号 + 来源」合并成行，带时间窗与占比条），顶部汇总今日 / 昨日 / 近 7 天 / 快照可见合计，底部注明快照时刻与口径；快照超过 30 分钟未更新时进入本页会静默拉一次上游积分（10 分钟节流，失败保持旧快照）。

### 4. 后台常驻调度器

常驻后台，按固定整点执行自动化运维排程：

- **每日 09:00 & 21:00**：国内版账号自动签到、对话活跃上报（点亮连登）与猫猫旅行闭环；国际版账号执行每日活跃打卡（领官方每日 30/50 积分福利）；
- **每日 22:00**：集中扫描全库账号，Token 剩余寿命不足 2 小时自动调用 Refresh Token 保活；
- **每日 01:00**：深夜时段执行夜猫子任务；
- 看板顶部另有「立即巡检保活」与「每日活跃打卡 (国际版)」可随时手动触发。

### 5. 限额护栏（设置 → 账号限额）

四条账号级护栏放在**同一张表**里，每行一条、每列一个作用域：

| 护栏 | 作用 |
| --- | --- |
| 保留积分 | 账号余额低于该值时不再接单，避免余额被用尽后触发上游的提醒短信 |
| 每日 Token 限额 | 账号当日消耗的 token 达到该值时暂停接单，请求自动切到其他账号 |
| 每日积分限额 | 账号当日消费的积分达到该值后只服务免费模型，需要花积分的模型自动切号 |
| 按模型每日 Token 限额 | 账号在单个模型上当日消耗的 token 达到该值时只禁该模型，同账号其他模型照常 |

- **全局默认 + 可选分版本**：每条护栏的「全局默认」同时作用于国际版与国内版；勾上「分别设置国际版 / 国内版」后可单独设值，某版本**留空即继承全局**（占位符会写出继承到的数字），取消勾选再保存等于清回继承。
- 每条填 `0` 表示关闭（默认值）；四条都按**本地时间 0 点**解封，只在请求路径生效（定时任务不受影响），且都是**单账号独立计数**。
- 免费 / 付费以**各出口自己的模型目录**为准，未知模型按付费处理；账号行会显示对应徽章与 `模型 · N tok 达限` 标记。
- 某个出口的全部账号都达额时，该出口的请求返回 `429`（文案说明本地 0 点恢复，`Retry-After` 指向 0 点）。
- 存储：`accounts/settings.json` 里的一组 `limits` 映射（`{"global": …, "intl": …, "cn": …}`，`null` 表示继承全局）；旧版写在外层的四个扁平键会在第一次读取时自动折入。

### 6. OpenRouter 价估算（等价 token 花费）

把每条请求的 token 消耗按 **OpenRouter 公布的模型价**折算成等价金额，回答「这些 token 放在 OpenRouter 上值多少钱」——与账号实际扣除的积分是两个口径，看板里并列显示：

- **总开关**（「设置 → 模型价格估算」，默认开启）：关掉后价格列、「API 等价花费」卡片与未定价清单一起隐藏，后端也不再取价与折算；重新打开会立刻抓一次价并**补算关闭期间的请求**。
- **计价口径**：输入按缓存命中 / 未命中两档单价拆分，输出单独单价；输出 token 取上游 `completion_tokens`（已含推理 token）。按条件定价的模型（按输入长度或 UTC 时段）取每条请求最紧的那一档。
- **限时免费模型不折算**：目录里倍率 0 的模型（国际版 `deepseek-v4.1-flash`、国内版 `step-5-preview` 等）上游不扣积分，折算侧也不算钱（否则「等价花费」会把免费额度显示成支出）。这类请求照常计入 Token 与请求数。判定与倍率徽章用的是同一份本地目录。
- **定价来源**：OpenRouter 模型目录的**模型级公布价**。每份价格存成一条「定价策略」，每条请求只记引用了哪条策略，所以之后调价不会改写历史数字。
- **刷新**：「设置 → 定价刷新」默认每 5 分钟一次（填 0 关闭）；只有价格真的变了才写策略。面板可看当前策略、上次 / 下次取价时间与失败原因，也能点「立即取价」。
- **新增模型自动取价**：候选清单 = 内置目录 ∪ 网关实时目录，上游新上架的名字 5 分钟内进入取价。匹配顺序：面板手填覆盖 → 人工覆盖表 → 同名唯一命中 → 变体后缀继承。
- **未定价可见**：`/pricing` 列出未定价模型及分类与相似候选（仅建议，不自动采用），可在设置里手填 OpenRouter id 并登记（写 `pricing-overrides.json`，不改源码）。
- **展示位置**：数据看板「API 等价花费」卡片、各账号用量透视与模型性能表的最后一列、网关页「估算价格」卡片、最近请求最后一列（悬停可看完整定价链）。人民币 / 美元一键切换（记在浏览器本地）；快照覆盖不到的模型显示 `—`，不猜数字。

### 7. 本地网络工具（可选，默认关闭）

Codex App 这类客户端会在 Responses 请求里声明 `web_search` / `web_fetch`，而上游没有对应的执行器。打开「设置 → 本地网络工具」后，网关把那份声明换成自己的同名 function，拦下模型的调用并在本地执行（搜索在 DuckDuckGo / Bing / Wikipedia / Stack Overflow / Google News 之间自动轮换），再把结果喂回模型，最多代跑 3 轮（`WB_MAX_WEB_ROUNDS` 可调，上限 8）。

- 关闭时（默认）声明原样透传，客户端自己声明的搜索工具照常拿到调用；
- 打开后网关会主动出网抓取模型给出的 URL（字面私网地址与解析进内网的域名都会被挡；`WB_WEB_TOOLS_PROXY` 可给这组工具单独指定出口代理），每轮代跑都会多跑一次上游、多消耗该账号额度；
- 某个引擎被限流／拦截时自动轮换到下一家，全部不可用才把明确错误交给模型；Wikipedia / Stack Overflow / Google News 这类非常规来源会在结果里标明来源；
- 只影响声明了这两个工具的客户端，普通 `/v1/chat/completions` 客户端不经过这条路径。

### 8. 智能体一键配置

看板「智能体配置」页可检测本机已装的 AI 客户端，一键把它们的配置指向本网关，改前自动备份、随时字节级还原：

| 客户端 | 写入位置 | 协议 |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | 原生 Anthropic Messages（自动剥 `/v1` 后缀） |
| Codex CLI | `~/.codex/config.toml`、`~/.codex/auth.json` | Responses API |
| OpenCode | `~/.config/opencode/opencode.json` | OpenAI 兼容（`@ai-sdk/openai-compatible`） |
| DSH (DeepSeek Harness) | `~/.dsh/settings.yaml`、`~/.dsh/.credentials.yaml` | OpenAI 兼容 |
| Crush (Charm Crush) | `~/.config/crush/crush.json` | OpenAI 兼容 |
| ZCode | `~/.zcode/v2/provider_config.json` | OpenAI 兼容（`openai-chat-completions`；**配置前先退出 ZCode**，运行中它会重写该文件） |
| Hermes CLI (Hermes Agent) | `~/.hermes/config.yaml`、`~/.hermes/.env`（Windows 为 `%LOCALAPPDATA%\hermes\`；设置了 `HERMES_HOME` 则用它） | OpenAI 兼容（`providers.wb-proxy` + `model.provider: custom:wb-proxy`） |

用法：打开看板 →「智能体配置」→ 选 API Key（支持全局默认或绑定特定出口的多 Key）、默认模型与网关地址 → 在检测到的客户端卡片上点「一键配置」；随时可点「一键还原」。

- **非侵入**：纯标准库实现的文本级 YAML / TOML / JSON / .env 编辑器，只增量插入或更新 `wb-proxy` 那段，绝不重排或抹掉用户的注释、缩进与其他 provider 配置；
- **可回滚**：写入前自动备份到 `accounts/agent-backups/<客户端ID>/`（每文件保留最新 10 份），首次接入的原件永久保留；多文件客户端（DSH、Codex）两阶段写入，任一文件失败立即回退；
- **还原**：byte-exact 回到首次接入前，网关新建的文件会被清理；若之后手动改过配置，看板会提示「检测到外部修改」，还原仍会覆盖回原件；
- **模型表跟着网关走**：OpenCode / Crush / ZCode / DSH / Hermes 把模型写死在各自配置文件里，所以看板切提供商（WorkBuddy / Qoder / Trae / 鸡蛋）或切出口版本时，会把当前目录的模型表重写进这些客户端（并带上 `X-Provider` 提示），否则选择器里会一直停在上一个提供商的模型；「智能体配置」页的「同步模型清单」按钮可以随时手动推一次。客户端多数只在启动时读一次配置，改完要重启客户端；
- **外部修改只看网关自己那段**：判断「配置被手改过」时只比对网关写进去的那一段（provider 条目 / 模型表 / 默认模型 / Key），文件里别的部分（别的 provider、插件、客户端自己重排的格式）怎么变都不算，也就不会误伤上面的模型同步；
- **密钥安全**：API Key 只写进客户端自己的本地配置目录（权限仅限当前系统用户），单文件超过 8MB 拒绝编辑。

**DSH：思考档位与上下文窗口写在哪**（`~/.dsh/settings.yaml`）

一键配置给 DSH 写的每个模型条目都带上网关宣告的规格，DSH 会据此生成它自己的选择器：

```yaml
    wb-proxy:
      api: openai-completions
      baseURL: http://127.0.0.1:8788/v1
      compat: {thinkingFormat: openai, supportsReasoningEffort: true}
      models:
        - id: deepseek-v4.1-flash
          contextWindow: 1000000     # 客户端认为的上下文容量（压缩 / 截断按它算）
          maxTokens: 128000          # 单次输出上限
          reasoningEfforts:          # 会话里可选的思考档位：键=档位，值=发给网关的拼写
            low: low
            high: high
            max: max
```

- **思考程度在 DSH 里怎么选**：不在「设置 → 模型」页，而在**输入框旁的模型菜单**（点「推理等级」选档位；`/model` 弹窗只列模型）。选项完全来自上面的 `reasoningEfforts` —— 不声明就没有这一行。
- **上下文长度怎么改**：`contextWindow` 就是 DSH 用的容量。改客户端侧就编辑这一行，改网关侧就改 `/v1/models` 宣告的 `context_length`；两条链路共用 `wb_modelsdev.lookup` 的同一套取值。
- **上游钉死档位的模型**（目录里只有 `reasoning.effort`）不写 `reasoningEfforts`。
- **注意 profile 覆盖层**：若 `$DSH_HOME/profiles/<profile>/cordis.patch.yml` 里有 `- id: llm-pi-ai` 条目，它会**整体替换**该插件的配置，模型清单与档位以 patch 那份为准。

**在另一台机器上接入（远程 / 非本机）**：看板的一键配置运行在网关进程里，改的是**网关所在机器**的配置目录——所以它只在网关本机、且用 `127.0.0.1` 打开看板时可用（远程访问、容器 / OpenWrt 部署下按设计隐藏入口）。客户端装在别的电脑（网关跑在服务器 / NAS / 路由器上）时，改用自带的配置器 CLI：它在**客户端所在的机器**上运行，写本机配置、只通过 HTTP 从网关取模型表：

```bash
# 1. 取配置器（网关直接提供该文件；也可从仓库 / 绿色包复制 wb_agents.py）
curl -fsSL http://<网关地址>:8788/agents/cli -o wb_agents.py

# 2. 一条命令接入（需要 Python 3.9+，Key 在「设置」页生成）
python wb_agents.py list                                   # 本机装了哪些客户端
python wb_agents.py apply --client codex --base-url http://<网关地址>:8788/v1 --key sk-xxxx
python wb_agents.py restore --client codex                 # 随时字节级还原
```

- 支持 `claude-code` / `codex` / `opencode` / `dsh` / `crush` / `zcode` / `hermes` 七个客户端，与看板共用同一套写入、备份与还原逻辑（`plan()` 同时驱动 `--dry-run` 与真实写入）；
- 默认带 Key 请求一次 `GET /v1/models`：既为 OpenCode / DSH / Crush / ZCode / Hermes 填入模型表，也顺带验证 Key（401 = Key 被拒）；网关不可达或想完全离线时加 `--no-models`；
- 状态与备份存在该机器自己的 `~/.wb-agent-config/`（可用 `--state-dir` 覆盖），每台机器一份，`restore` 只对执行过 `apply` 的机器有效；
- `--dry-run` 只打印将要写入的内容、不落盘；Key 也可用环境变量 `WB_PROXY_API_KEY` 传入。

模块内部设计与新增客户端的方法见 `wb_agents.py` 的模块说明。

### 9. 剩余用量估算（数据看板 → 剩余用量估算）

上游按 **24 小时窗口**给「账号 × 模型」配额（用满时 429 / code 6004 会带上重置墙钟），但从不告诉你这个窗口的预算是多少。看板新增的「剩余用量估算」区块把它反推出来：

- **预算 = 撞线账号窗口用量的平均**：账号撞线（收到带重置时刻的 429）那一刻，它在该模型上「自窗口起点以来的成功用量」就是一个样本；按区域聚合，先按账号平均再跨账号平均。样本在 429 处理的那一刻就记进 `usage/limit-events.jsonl`。
- **已用从每个账号自己的重置时刻起算**：窗口起点取该账号该模型最近一次重置时刻；正在冷却的组合剩余记 0 并显示恢复时间。
- **没有撞线记录的组合标「按 24h 估算」**：窗口起点未知时按最近 24 小时用量统计，这是剩余量的**下界**（偏保守）。
- 接口 `GET /usage/remaining`（需面板会话）返回预算均值 / 区间 / 样本数与每个「账号 × 模型」的已用、预算、剩余、窗口起点、是否冷却、是否估算；区块只读不写，不参与请求路径。
- **查询侧不做重复计算**：用量缓冲是**时间戳 + 累计和**两个列表（窗口求和 = 两次二分 + 一次相减），载荷挂短 TTL（`WB_REMAINING_TTL`，默认 15 秒）并支持 `ETag` / `If-None-Match`。

### 10. 积分获取历史（数据看板 → 积分获取历史）

「积分扣减历史」看的是花掉的部分，这一块补上进账的部分：上游对每个账号返回一份**积分包清单**（每日活跃奖励的 Bonus Pack、免费套餐、活动包…），每个包带面额、发放时间与到期时间。区块把全部账号的包摊平成一张表、新的在前：

- 列：时间（发放）/ 账号 / 区域 / 名称 / **来源** / 积分 / 剩余 / 到期 / 状态；
- 状态按「过期 > 用完 > 在扣减 > 可用」归类：生效中 / 可用 / 已用完 / 已过期；`no_expiry` 的包显示「不过期」；
- **来源**：上游自带的发放原因原样显示（如「Buddy 加油站签到」「成长计划奖励」）；国际版的 Bonus Pack 30/50 没有原因字段，按官方规则推断为「每日活跃奖励」（悬停里注明这是推断）。签到 / 每日活跃记录也会按时间关联上去（发放前 2 小时内最近的一次尝试）。
- 摘要行给出笔数、合计面额、合计剩余与**快照时刻**。接口 `GET /accounts/credits/grants` 只读内存快照、**不发上游请求**，数字的新鲜度取决于最近一次「一键刷新积分」。

### 11. 单请求体体积限制（`WB_MAX_PAYLOAD_BYTES`）

针对携带超长历史、大内联附件或高分辨率图片的请求，网关默认对单次请求体做上限保护（默认 16MB = `16777216` 字节），以防止异常巨大的请求挤占内存。若请求体超过上限，网关在解析前即直接返回 HTTP 413（`payload too large` 并附带 `gateway_hint` 说明）。

如需调整上限（例如提升至 32MB）：
- 设置环境变量 `WB_MAX_PAYLOAD_BYTES=33554432`（单位为字节）；
- 此环境变量仅在进程启动时读取一次，修改后重启 `wb-proxy` 即可生效。

---

### 12. 后台模型重定向（`WB_BACKGROUND_MODEL_NAMES` / `WB_BACKGROUND_MODEL_TARGET`）

桌面客户端（Codex 等）的模型选择器**只管主对话线程**。客户端内置的「模型路由配置」会把后台工作——记忆生成、环境建议、临时内容生成、自动化任务——送到自己硬编码的模型上，UI 里选什么免费模型都改不掉。这些请求带着同一把 Key 打到网关，于是在共享 Key 的部署里悄悄烧付费积分。

网关在唯一的 POST 分发点（选账号之前）把这类模型名改写成免费目标：

- `WB_BACKGROUND_MODEL_NAMES`：要拦截的模型名清单（逗号分隔，大小写不敏感），默认 `gpt-5.6-terra,gpt-5.6-luna,gpt-5.3-codex`；
- `WB_BACKGROUND_MODEL_TARGET`：改写目标，默认 `deepseek-v4.1-flash`。

改写只动 `model` 字段，请求体其余部分原样透传；命中与改写都会记一条 `alias: background model X -> Y` 日志，方便核对。OpenAI 形状的两个路由（`/v1/chat/completions`、`/v1/responses`）与 Anthropic 形状的 `/v1/messages` 都覆盖。不需要时把名单设为空即可关闭。

---

### 13. 自签证书 / TLS 解密代理（WB_CA_BUNDLE）

如果网关跑在公司代理、Clash/Surge 等 HTTPS 解密后面，或 macOS 上 Python 没有读到系统钥匙串里的根证书，上游请求会报 CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain，面板上的 OAuth 登录和账号测试会显示 502。把签发该证书的根证书导出为 PEM，然后设置 WB_CA_BUNDLE=/path/to/ca.pem（优先）或 SSL_CERT_FILE=/path/to/ca.pem，重启 wb-proxy。网关会在系统根证书之外追加这份 PEM，不关闭证书校验；证书错误也不再重试，日志和面板会直接给出这段提示。

---

### 14. 国内版上游地址（`WB_CN_CHAT_UPSTREAM`）

国内版对话侧默认发往 `https://copilot.tencent.com`。网关跑在海外出口后面（代理软件的全局模式、海外服务器）时，到它的 TLS 握手会被复位，日志里是 `SSL: UNEXPECTED_EOF_WHILE_READING` 或 `WinError 10054`，国内版账号的对话、刷新 token、模型目录、签到与成长任务全部失败。`www.workbuddy.cn` 提供同一套接口且海外可达（官方桌面端当前即以它为 endpoint，VSCode 身分本来也走它）。

- 设置 `WB_CN_CHAT_UPSTREAM=https://www.workbuddy.cn`，重启 `wb-proxy`；启动日志的 `realm` 行会显示新的出口，并多一行 `cn upstream: ... (WB_CN_CHAT_UPSTREAM)`；
- 只改「往哪发」：`X-Domain` 仍用账号登录时记下的 domain，计费侧（`www.codebuddy.cn`）不变；
- 不是 `http(s)://` 开头的值会被忽略并回落默认地址，启动日志给出提示；不设则行为与此前完全一致。

---

### 15. 路由总网关（四个网关合成一个池子）

顶部提供商标签条最前面是 **「路由」**，也是默认视图。它把 WorkBuddy / Qoder / Trae（各含国际、国内）与鸡蛋的全部模型合成一个池子：

- 不带 `?provider=` / `X-Provider` 请求 `GET /v1/models`，返回的就是这份合并清单；
- 模型 id 是 **`模型名 · 供应商`**，例如 `MiniMax-M3.1-Flash · OpenRouter`。网关名后缀带出口：`WorkBuddy-global` / `WorkBuddy-cn`、`Qoder-global` / `Qoder-cn`、`Trae-global` / `Trae-cn`；鸡蛋用供应商或池子名；
- 后缀决定真实去向，**优先于** `?provider=` / `X-Provider`：客户端不需要再按网关拆配置；
- 顶部 WorkBuddy / Qoder / Trae / 鸡蛋 四个标签现在只是**看板视图**（本地记住选择），不再切换网关上游、不改默认出口。要改真实上游仍在对应视图的出口控件里操作。

路由策略参考 [magpie](https://github.com/yetone/magpie) 的路由组，五种：

| 策略 | 选谁 |
| --- | --- |
| `smart` | 额度最快重置的优先（重置时浪费最少） |
| `order` | 按池子顺序，先用第一个 |
| `rotate` | 每轮换一个成员 |
| `usage` | 优先用得最少的 |
| `pace` | 优先「距重置前每小时剩余额度」最多的 |

客户端把模型写成 `route/auto` 就按「路由」页当前选定的策略选路；也可以钉死 `route/smart`、`route/order`、`route/rotate`、`route/usage`、`route/pace`。策略存在 `settings.json` 的 `routing.strategy`，`GET /routing` 可看候选排名与最近 200 条决策。同一会话上一轮选中的成员仍可用时继续用它（`rotate` 除外），以保住提示词缓存；候选只含有可用账号的网关，鸡蛋始终可用，面板停用的模型不参与路由。

「路由」页还有两个按钮：**刷新全部模型清单**（后台重拉七份目录）与 **同步到客户端**（把统一清单写进已接入的客户端配置；不写 `X-Provider`，靠 id 后缀路由）。

---

## 三、账号添加与管理

打开看板 `http://127.0.0.1:8788/`，在「账号」区域操作。若上游对某账号的单个模型返回 429，账号行会显示受限模型与预计恢复时间（浏览器本地时间），该账号仍可用于其他模型；模型冷却只在当前进程内保留，重启清空。

窗口探针管的是「已经撞到 429 之后」；想提前知道某条配额线有没有被打满，可在「设置 → 模型额度定时主动查询」勾选模型：网关每 6 小时为每个「账号 + 模型」发一个最小请求做体检，查到 429 就立刻记该模型的冷却并把重置时刻交给窗口探针，请求通了就说明还没到限额。一旦某个「账号 + 模型」已经有了登记的 429 记录，就停止定时查询（改由窗口探针负责），记录消失后自动恢复轮询；每次查询都会消耗该账号极少量额度，默认不勾选任何模型。

### 方式一：浏览器 OAuth 授权（推荐，免客户端）

1. 点「+ 添加账号 (OAuth)」；
2. 选择要登录的区域（国际版 / 国内版），点弹出的官方授权链接并在浏览器完成登录；
3. 程序自动检测回调，账号自动加入账号池，无需手动复制凭证。

### 方式二：从本机桌面客户端导入

1. 让 **WorkBuddy 桌面客户端保持运行并已登录**（Windows 上网关要从它的进程内存里取解码密钥，这一步不能省）；
2. 看板点「扫描桌面客户端账号」，弹窗列出本机已登录的国际版 / 国内版账号；
3. 若提示凭据已加密，先把解码密钥交给网关，再点账号行的「导入」：
   - **Windows**：点弹窗里的「回收密钥」（只读，实测 1 秒内完成）；
   - **macOS / Linux**：进程内存回收只在 Windows 上实现，弹窗里会直接给出输入框，粘贴密钥即可（见下）。

关于加密凭据：桌面客户端从 2026-09-24 起把 `accessToken` / `refreshToken` 存成 `$wbEncrypted` 信封，网关按客户端 `packages/at-rest-crypto` 的同一套方案（AES-256-GCM + `WB-AAD` 帧头）就地解密，导入的仍是可直接使用的 token；解码密钥编译在客户端原生模块里、磁盘上没有明文。Windows 上可以从**正在运行的**桌面端进程内存里找回（只读 `OpenProcess(PROCESS_VM_READ)` + `ReadProcessMemory`，不向客户端写任何东西）；macOS / Linux 上没有这条路，看板会显示输入框让用户**手动粘贴**一次——两种方式拿到的密钥都只留在网关进程内存、不落盘不写日志，且都要先过 keyblob 自检（protectorKeyId 对得上、能解出 32 字节主密钥）才生效。

手动粘贴接受两种写法（都不用自己换算）：64 位 hex 的保护密钥，或 44 字符 base64 的 `atRestSecretKey`。拿不到密钥时，macOS / Linux 上请直接用方式一走浏览器登录。提示权限不足时，以管理员身份启动网关再试一次。

### 方式三：Qoder 账号（国际版 qoder.com / 国内版 qoder.cn）

看板头部有 **WorkBuddy / Qoder** 切换：切到 Qoder 后，账号列表、积分、用量统计、默认出口与模型清单整页跟着走；每个提供商各自再分国内 / 国际两个版本（互不串数据）。Qoder 账号的添加入口就是账号区那个「添加」按钮，弹窗提供两条通道：

1. **浏览器设备码（仅国际版）**：点「添加」自动申请设备码并给出链接，浏览器里登录 qoder.com 后窗口内自动检测完成入池（与 WorkBuddy 的 OAuth 流程同一操作习惯）；
2. **PAT（国际 / 国内通用）**：在 qoder.com（或 qoder.com.cn）账号设置里生成个人访问令牌，粘进弹窗点「用 PAT 登录」，服务端兑换成登录态后入池，长期有效。国内版只有这条通道。

Qoder 账号与 WorkBuddy 账号在同一账号池里按 `provider` 隔离：签到 / 每日活跃 / 成长任务 / 出站身份这些 WorkBuddy 专属能力只作用于 WorkBuddy 账号；Qoder 账号支持启用停用、积分刷新与明细、凭证刷新、昵称同步、代理出口绑定、导出与删除。Qoder 的聊天通道已接入：显式点名 `provider=qoder`（`?provider=` 或 `X-Provider`）的 `/v1/chat/completions`、`/v1/completions`、`/v1/messages`、`/v1/responses` 会走 Qoder 上游（COSY 协议），与 WorkBuddy 共用同一套流式 / 非流式响应；没有显式提示时按模型归属兜底（顺序见下面的「四个网关之间怎么分流」）。没有可用 Qoder 账号时返回 503（`no usable account ...`），不会悄悄并入 WorkBuddy 流量。

**四个网关之间怎么分流（模型与请求互不影响）**：

- **显式提示最高优先**：`?provider=workbuddy|qoder|trae|egg` 或 `X-Provider` 头 —— 客户端配了哪家就直走那家，不受归属判定影响。一键配置给非 WorkBuddy 的客户端写的就是这个头。
- **没有提示时按模型归属**，顺序固定：WorkBuddy → Qoder → Trae → 鸡蛋。一个模型 id 只属于一家，谁也不许抢别人的 id。
  - WorkBuddy 的 id 一律小写（`deepseek-v4.1-flash`），Qoder / Trae 的带大写（`DeepSeek-V4-Flash`），所以归属按**原样**匹配，跨家只差大小写的 id 各归各家。
  - 鸡蛋只认它自己的 id：免费车道（`*-free`）、Kilo（`vendor/model:free`）、供应商池里的模型。池子与 WorkBuddy 目录撞名时默认归 WorkBuddy；要走池子就显式 `?provider=egg`。
  - Trae 的别名表只按**精确**命中算 Trae 的模型。
- **出口（国内 / 国际）也是各家自己的**：走 Qoder 就用 Qoder 记着的出口，走 Trae 就用 Trae 的，走 WorkBuddy 就用 WorkBuddy 的（面板顶部「默认出口」改的是当前提供商那一份）。显式 `?realm=` / `X-Realm` / Key 绑定的出口仍然最高优先。

**Qoder 某个模型排上队了**：上游会用 10605 `RESOURCE_LIMIT` 拒，意思是该模型在所选区域暂无可用容量 —— 这不是「请求有问题」，官方 qodercli 收到这个码是**等一会儿重试**。网关照着做：按上游给的 `retryAfterSeconds`（实测 30 秒）等一会儿重发，最多等 120 秒；等到了就正常把回答给客户端，等不到才把排队原样报出来。期间不占用账号故障额度 —— 账号是好的，只是模型暂时没容量。换个 Qoder 模型也能立刻绕开。

### 方式四：鸡蛋（免费白嫖，免账号 / 免密钥）

看板头部的第四个提供商 **鸡蛋** 聚合两路公开免费额度，不需要添加任何账号或密钥（协议与实测结论参考 [zen-gate](https://github.com/LAGcomcom/zen-gate)）：

1. **OpenCode Zen 免费车道**：`opencode.ai/zen` 里带 `free` 标记的模型。无 API Key 体系，上游按「是否为官方 OpenCode 客户端」做指纹校验，网关已按官方 `read` / `shell` 工具定义、会话头与 User-Agent 对齐；
2. **Kilo 免费池**：`api.kilo.ai/api/gateway` 里 `isFree` 的模型，普通 OpenAI chat 请求即可。免费池的提示词可能被上游记录，**不要传机密内容**。

切到鸡蛋时看板会自动强制刷新一次免费清单；网关也会在后台预热（目录 TTL 180 秒，落盘 `accounts/zen-catalog.json`）。某个源拉取失败时只保留它上一次的可用结果。聊天端点显式带 `?provider=egg`（或 `X-Provider: egg`）即可；不带提示时，`*-free` / `vendor/model:free` 这类模型名也会兜底到鸡蛋。免费源没有账号池可换号，被限流或地区拦截时按真实状态码返回。鸡蛋视图下账号、签到、任务与出口控件自动收起。

**每个模型属于哪个池子**：模型清单里有一列「免费池」，逐行标出这条模型走的是 **Zen 免费车道**（要过官方客户端指纹校验，部分出口 IP 会被判滥用而 403）还是 **Kilo 免费池**。两路的限流与地区门口径不同，这一列是必看的。

**自己加供应商（每个供应商一个池子）**：除两路内置免费源，还能在鸡蛋视图里自己加任意多个上游（NVIDIA NIM、Gemini、GitHub Models、Groq、Cerebras、Mistral、OpenRouter 等都有现成预设，点一下填好名称与 Base URL，Key 仍要自己去申请）。每个供应商是一个独立的**池子**，各有各的 Base URL、API Key、协议与一份自己挑过的模型清单：

- **协议**：OpenAI 兼容（`{base}/models` + `{base}/chat/completions`）与 Anthropic 兼容（`{base}/models` + `{base}/messages`）。Anthropic 协议的上游由网关自动翻成 OpenAI 形状，下游客户端不用改；
- **拉取模型列表**：填好地址与 Key 后点「拉取模型列表」，当场就能看到上游有哪些模型（地址写错、Key 不对会当场报错）；
- **挑选模型**：只把勾中的模型对外提供。挑选器可按模型 ID 搜索，或勾 **只看免费** 筛出 `:free` / `/free` 的模型；「全选」只作用于**当前看得见**的那些；
- **顶掉内置的模型**：一个模型 id 只能有一个去处。池子勾中的 id 若与内置免费源撞了，**这条模型就改走本池**，卡片上会写明「顶掉内置免费源 N 个模型」；取消勾选即还给内置源；
- **启用 / 停用**：停用的池子不接请求，模型也从清单里消失；
- **测试**：卡片上点「测试」真发一次最小请求（1 个 token），当场知道这把 Key 能不能推理。**「刷新」证明不了这个** —— 拉目录是 `GET /models`，垃圾 Key 也拉得到几百个模型，只有真发一次推理才知道。Key 拿错类型时面板会直接说清该怎么换；
- 池子存在 `accounts/egg-pools.json`（`os.replace` 原子替换），换机器时整份拷过去即可；API Key 明文保存，与 `settings.json` 里的 API Key 同一口径。

**平台账号池（登录一次就能用）**：自建池要自己填 Key，平台账号池不用 —— 登录一次，网关保管登录态并自动续期，模型直接进清单。鸡蛋视图里的「平台账号」区每张卡片是一个平台：

- **华为云 CodeArts**：点「添加账号」打开授权页，浏览器登录后自动回调本机入池（网关不在本机时可手动粘回调 URL）；卡片上能查余额、每日领取福利。CodeArts 的模型分免费福利通道（每日 1000 万 Tokens，需账号开通福利）与积分通道，账号没开通福利时面板会直说；
- **Cline**：设备码登录，浏览器输入设备码即成；凭据带 `workos:` 前缀才是上游认的写法（少了会一律 401）；
- 卡片上的「模型 (N)」可展开挑选（按 id 搜索、只看免费、全选 / 全不选），挑完点「保存选择」；
- 平台模型对外 id 带平台前缀（`codearts/GLM-5.2`、`cline/cline-free/minimax-m3.1-flash`），不与自建池或别的提供商撞名；发给上游时自动摘掉前缀；
- 账号可逐个停用、续期、删除；凭据与自建池的 Key 一样明文存在 `accounts/egg-pools.json`。

**按池子筛选模型清单**：模型清单上方有「池子」下拉，把内置的 Zen 免费车道 / Kilo 免费池与各个自建池子列在一起，每项带**池子名、模型数与模型名**；选一个就只看那一路的模型。

**同名模型归谁**：同一个模型 id 内置免费源与自建池都有时，**算自建池的**（请求本来就走自建池）。否则那一条会被内置源吞掉：面板把它标成「Kilo 免费池」，看着像走免费池，实际流量去了自建池。自建池的模型里只有**付费**的那些标「按供应商计费」（费用由该供应商结算）；上游明确标成免费的仍标「免费」。

**看鸡蛋的调用情况**：数据看板页头的「提供商筛选」新增 **鸡蛋** 一项，选中后整页只统计 `provider=egg` 的流量。鸡蛋没有账号池，它的请求记在一个伪账号上，看板的「按账号」里显示为 **鸡蛋（免账号）**。

> 说明：OpenCode Zen 免费车道会按出口 IP 判定滥用流量，部分网络会直接返回 403；此时改用 Kilo 免费池的模型即可。

---

## 四、客户端配置与接入

- **API 接口地址 (Base URL)**：`http://127.0.0.1:8788/v1`（局域网为 `http://<局域网IP>:8788/v1`）
- **API Key**：本机单机模式（未配置 Key 且未开 LAN）可留空或填任意字符；已在看板配置 Key 或 LAN 模式，请用看板「设置」页里绑好出口的 Key；
- **模型名称**：填 `/v1/models` 里列出的任意官方对齐模型 ID（如 `deepseek-v4.1-flash`、`gpt-6-astra`、`glm-5.3`）。

**Codex CLI / Claude Code（Responses API）**

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你在看板设置中添加并绑定的API_Key"
```

**Claude Code / Anthropic SDK（实验性 Anthropic Messages 适配，默认禁用）**

> ⚠️ **积分风险：默认拒绝调用。** 网关对外接受 Anthropic Messages，但对内将请求转换为 WorkBuddy 的 `/v2/chat/completions`；`cache_control` 被忽略。已有 [#367](https://github.com/ardeyouxipianyi/workbuddy2api-hub/issues/367) 报告长会话缓存命中率仅约 11%，单次消耗超过 100 积分。该问题的因果机制仍待定位，故暂时默认禁用整个 Anthropic Messages 入口，而不假装完全兼容。禁用时 `/v1/messages`、`/messages` 及对应 `count_tokens` 入口返回 Anthropic 格式的 HTTP 400（`invalid_request_error`），请求不会转发至上游。

只有确认理解风险后，才在**网关服务端**显式设置 `WB_ENABLE_ANTHROPIC_MESSAGES=1` 并重启。Docker Compose 可在 `wb-proxy.environment` 中加入 `- WB_ENABLE_ANTHROPIC_MESSAGES=1`；其他环境则在启动网关前设置此环境变量。客户端配置示例：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_API_KEY="你在看板设置中添加并绑定的API_Key"
```

实验性适配包含 `system`、`text`、`image`、`document`、`tool_use`、`tool_result`、`tools` 与 `tool_choice` 等字段转换，并将响应包装为 Anthropic 的流式事件序列或非流式消息。这不是向上游的原生 Anthropic 透传。

已知边界：服务端工具（`web_search` 等）上游不支持，会被丢弃并在 system 里注明；`thinking` / `redacted_thinking` 历史块不回放；`top_k`、`cache_control`、`context_management` 与 `betas` 被忽略；`/v1/messages/count_tokens` 在显式启用后只提供网关的 CJK 感知估算值，**不是**官方分词器的精确值。

---

## 五、看板与接口一览

访问 `http://127.0.0.1:8788/` 使用集成看板：顶部页签默认是「网关与账号 / 数据看板 / 智能体配置 / 设置 / 运行日志」，每个主页面左侧有一条按页面区块现场生成的导航（点击直达、滚动高亮、地址栏带锚点，可收起成窄轨）。

页面头部有 **WorkBuddy / Qoder / Trae / 鸡蛋 提供商切换**：切换后账号、积分、统计、默认出口与模型清单一并跟着提供商走（每家各自记着自己的国内 / 国际出口；鸡蛋不需要账号与出口），面板对服务端的每个请求都显式带 `?provider=`。直接用 API 时同样可以显式指定：账号、用量（`/usage/analytics`、`/usage/timeseries`、`/usage/remaining`、`/usage/by-account`）与模型清单端点接受 `?provider=workbuddy|qoder|trae|egg`（或 `X-Provider` 头）；**不提示时行为与此前完全一致**（WorkBuddy 口径），既有客户端无需改动。聊天端点也认这个提示：显式 `provider=qoder|trae|egg` 走对应上游；不提示时按模型归属兜底，顺序固定为 WorkBuddy → Qoder → Trae → 鸡蛋（一个 id 只属于一家，跨家同名归第一家）。看板也认 `?provider=egg` 这样的 URL 参数（只钉住本次会话的视图，不改服务端状态）。

设置页的「账号独立成一个页签」默认关闭，账号区留在「网关与账号」页；打开后账号与签到记录成为独立的「账号」页（添加 / 导入 / 导出、启用停用、凭证与积分刷新、代理出口绑定）。页内的「签到与活跃记录」读 `GET /activity/history`，可按**区间 / 任务 / 结果 / 账号**筛选，一行一次真实尝试（时间 · 账号 · 区域 · 任务 · 来源 · 结果 · 说明），最新在前，上限 1000 行。

「数据看板」页顶部可切换统计口径：**今日 / 本周 / 本月 / 全部历史 / 自定义**，切换后 KPI 卡片、账号用量透视表与模型性能表一起切到同一窗口。六张 KPI 卡片本身就是明细入口：点击打开「按账号 / 按模型」两列拆解，**数据与卡面同源**（同一份 `/usage/analytics`），弹窗开着时跟着 5 秒轮询一起刷新。每个模型行带**计费类型徽章**（`限时免费` 或目录倍率如 `0.29x`），限时免费模型不耗积分，不计入「积分消耗」与「API 等价花费」。成功率口径是 **成功 /（成功 + 失败）**；当前口径一次请求都没有时环形卡显示「—」。

看板的「提供商筛选」里还有一项 **本地**：统计的**不是本网关的流量**，而是本机各客户端自己的会话（OpenCode / Claude Code / Codex …），并**排除经本项目网关的请求**（那部分网关视图已经统计过）。页首的**客户端筛选**在本地口径下同样可用。账号池专属的区块（剩余用量 / 积分扣减 / 积分获取）在本地口径下整块收起。

顶栏右侧是**实时状态徽标 + 轮询控制**：绿=实时、琥珀=超过阈值没更新、灰=已暂停、红=最近一次取数失败；旁边的「暂停 / 继续」和 5 / 15 / 30 秒频率开关只改面板自己的轮询节奏，不影响网关转发普通流量。「数据看板」页的图表可用键盘读（← / → 逐点读数、Esc 清掉），并有屏幕阅读器专用的数据表兜底；系统开启「减弱动态效果」时动画自动归零。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | Web 用量与任务监控看板 |
| POST | /v1/chat/completions | 标准 Chat Completions 接口 |
| POST | /v1/responses | Responses API 协议接口 |
| POST | /v1/messages | 实验性 Anthropic Messages 适配，默认禁用（HTTP 400）；需显式启用并承担缓存风险 |
| POST | /v1/messages/count_tokens | 实验性 Anthropic 计数估算接口，随 Messages 入口默认禁用 |
| GET | /v1/models | 官方对齐模型列表（含能力与规格宣告）。面板会话可带 `include_disabled=1` 取回已停用的模型；普通 API Key 拿到的清单里不含停用的模型 |
| POST | /models/toggle | 启用 / 停用某条模型（需面板会话）：`{model, disabled, provider, realm}`。停用后直接调用被 400 拒绝 |
| GET | /u/\<ref\>/v1/models | 定址账号的模型清单：\<ref\> 支持完整 uid / uid 前 8 位 / 昵称，过滤该账号禁用模型 |
| POST | /u/\<ref\>/v1/chat/completions | 定址调用：锁定该账号，不参与会话亲和 / 轮换 / 自动换号；账号不可用直接报错 |
| POST | /u/\<ref\>/v1/responses | 定址 Responses API，语义同上 |
| GET | /v1/balance-summary | 当前 API Key 所属区域的启用账号积分汇总；只读内存快照、不发上游请求，不返回账号身份 |
| GET | /pricing | 定价状态：当前生效策略、上次/下次取价时间、未定价清单（分类 + 候选） |
| POST | /pricing/refresh | 立即取一次价（需面板会话） |
| POST | /pricing/mapping | 手填 / 清除「模型 → OpenRouter id」运行期映射，随后自动取价（需面板会话） |
| GET | /agents | 客户端探测概览、支持的模型清单及网关连接地址 |
| GET | /agents/cli | 智能体配置器源码（供远程机器直接 `curl` 取用，免面板鉴权；与仓库内 `wb_agents.py` 逐字节一致） |
| POST | /agents/apply | 一键写入客户端配置并备份原件（需面板会话） |
| POST | /agents/restore | 一键还原客户端至首次配置前的状态（需面板会话） |
| GET | /tasks | 国内版成长任务、连续打卡与猫猫日常状态 |
| POST | /tasks/run | 触发国内成长任务全自动点亮与领奖 |
| POST | /tasks/travel | 触发猫猫日常旅行（派出 / 领奖） |
| GET | /scheduler | 定时调度器运行状态与排程日志 |
| POST | /scheduler/trigger | 手动立即执行后台巡检保活 |
| GET | /activity/history | 账号每日活动历史：签到与每日活跃的每一次真实尝试（`range` / `uid` / `task` / `result` / `limit`，最新在前） |
| GET | /accounts/credits/grants | 积分获取历史：各账号积分包（发放时间 / 面额 / 剩余 / 到期 / 状态），只读内存快照、不发上游请求 |
| GET | /usage/remaining | 剩余用量估算：每个账号 × 模型在当前 24h 窗口的已用 / 预算 / 剩余（预算取撞线账号的平均用量；需面板会话） |

---

## 六、版本更新记录 (Changelog)

<!-- 发版时：把下面的 Unreleased 段落整理成新版本号（## vX.Y.Z），整体移入 docs/CHANGELOG.md 顶部 -->

### Unreleased

已发布版本的完整记录（v1.4.5 ~ v1.7.16，含每版的 PR 归属）见 **[docs/CHANGELOG.md](docs/CHANGELOG.md)**。

## 七、致谢与引用声明 (Credits & References)

协议兼容、风控规避与任务链路设计过程中，参考并吸纳了以下开源项目的经验与逆向成果：

- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**：成长任务全链路逆向、设备指纹稳定派生（`derive_id`）、整点排程调度（`Scheduler`）、指纹脱敏与 `reasoning_content` 回填；
- **[CangShui/workbuddy-cliproxy-fix](https://github.com/CangShui/workbuddy-cliproxy-fix)**：早期客户端代理修复与接口差异参考；
- **[lovingfish/workbuddy-cliproxy](https://github.com/lovingfish/workbuddy-cliproxy)** 与 **[mmqz/cpa-multi-plugins](https://github.com/mmqz/cpa-multi-plugins)**：网关通信与多插件管理原型参考；
- **[ardeyouxipianyi/workbuddy2api](https://github.com/ardeyouxipianyi/workbuddy2api)**：国内版分发包逆向分析与出站 User-Agent 规范参考；
- **[linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel)**：国内版连登管家（补签保连登、7d/14d/28d 档位兑换、抽奖闭环）、对话活跃上报的事件形状与「必须带 `userId`、每号每天一次」的风控口径、企业版门控（企业号无个人成长体系），以及新手礼包 / 活动补偿接口；
- **[aodianjun/workbuddy2api-hub](https://github.com/aodianjun/workbuddy2api-hub)**：OpenWrt 打包配方（`.ipk` / `.apk` 构建脚本、包 Makefile、init.d 与 uci 配置、面板缓存预热器），审计后并入 `wrt/`。

PR 贡献者（v1.4.5 之前的改动未进上方更新记录，这里一并列出）：

- **[@ddddd-ren](https://github.com/ddddd-ren)**：用量日志倒序检索与看板防堆叠（PR #14）、原子写入与并发竞争修复（PR #13）、账号池 JSON 导出导入（PR #5）；
- **[@wylftw0314-glitch](https://github.com/wylftw0314-glitch)**：Responses API custom 工具协议双向转译（PR #12）；
- **[@shuishuipingan](https://github.com/shuishuipingan)**：成长任务领取竞态与专家/团队事件 id 去重、猫猫旅行派出修复、夜猫子任务接入调度器、启动端口误判（PR #21）、按模型冷却限流（PR #22）、任务接取强化与轮询加速（PR #27）、网络抖动重试与 403 直通（PR #28）、HTTP 连接同步（PR #30）；
- **[@ayeaaaa](https://github.com/ayeaaaa)**：按账号绑定出口代理槽（PR #26）、DeepSeek `reasoning_content` 回填（PR #36）、看板移动端布局（PR #37）；
- **[@t-789](https://github.com/t-789)**：macOS 启动脚本与防火墙助手（PR #31）；
- **[@Cekxri](https://github.com/Cekxri)**：Codex App namespace 工具支持（PR #33）；
- **[@wiggins-kong](https://github.com/wiggins-kong)**：API Key 行 id 唯一化（PR #40）、Docker 镜像缺少运行时模块（PR #41）；
- **[@Pro-XK](https://github.com/Pro-XK)**：看板积分消耗与账号昵称（PR #45）；
- **[@teddyli18000](https://github.com/teddyli18000)**：单模型限流可视化（PR #50）、`/health` 鉴权状态修正（PR #52）；
- **[@LuFering](https://github.com/LuFering)**：Docker 部署下的 Linux 桌面凭据挂载说明（PR #55）；
- **[@zhangzm0](https://github.com/zhangzm0)**：`tool_choice="none"` 保留工具声明（PR #57）。

---

## 八、免责声明 (Disclaimer)

1. 本项目为非官方自托管网关，仅供技术研究、逆向协议学习与个人合法授权账号在私有环境测试使用。
2. 本项目不提供任何账号及额度。请严格遵守官方服务条款，禁止用于任何商业转售、恶意并发或违规滥用。
