# wb-proxy 与同类项目的区别

> 对比依据各项目公开的 README 与文档，只说明**定位与接入方式的差异**，不评判优劣，也不保证对方功能的现状。最后核对：2026-11。

## 一句话区别

**wb-proxy 的核心不是「多把 API Key 分发」，而是「没有 Key 的通道」**：把 WorkBuddy / Qoder / Trae 这类只有客户端登录态的第三方服务，变成标准 OpenAI 接口，并替你把签到、积分、Token 保活这些运营动作跑完。

## 总表

| | wb-proxy | workbuddy2api-hub | LiteLLM | one-api / new-api 类 | magpie |
|---|---|---|---|---|---|
| 主要用途 | 本机 / NAS 自用的统一网关 | 同左（上游基座） | SDK + 代理层，统一调用各家 API | 多 Key 分发、计费、团队配额 | 本地模型路由组 |
| 上游凭据 | **账号登录态**（OAuth / 设备码 / 桌面端导入），另有免费源与自建 Key 池 | WorkBuddy 账号 | 各家 API Key | 各家 API Key | API Key / 本地模型 |
| 免账号可用 | 有（`?provider=egg`，Zen + Kilo 免费池） | 无 | 无 | 无 | 无 |
| 账号运营自动化 | 签到、连登、成长 / 积分任务、猫猫旅行、Token 保活、限额护栏 | 基础调度 | 无 | 无 | 无 |
| 多上游合并 | 四路（WorkBuddy / Qoder / Trae / 鸡蛋）+ 自建池，模型 id 带「 · 供应商」后缀 | 双区域（国际 / 国内） | 100+ provider | 多 channel | 多成员路由组 |
| 路由策略 | smart / order / rotate / usage / pace + `route/auto` | 双区域切换 | 负载均衡与 fallback | 权重 / 优先级 | 路由组 |
| 客户端一键配置 | 7 个客户端，自动备份与字节级还原 | 无 | 无 | 无 | 无 |
| 部署 | Python 3.9+ 标准库 / Docker（GHCR，双架构） | 同左 | Python 包 / 容器 | Go 单二进制 | 桌面应用 |
| 许可 | MIT | MIT | MIT | MIT | 见仓库 |

## 与上游基座 workbuddy2api-hub

wb-proxy 以 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) **v1.7.16** 为基座，完整保留其能力，增量包括：

- **更多通道**：Qoder（国际 / 国内，设备码 + PAT）、Trae（国际 / 国内，OAuth）、鸡蛋（Zen 免费车道 + Kilo 免费池 + 自建供应商池 + 华为云 CodeArts / Cline 平台账号）。
- **总路由**：四个网关合成一个模型池，模型 id 为 `模型名 · 供应商`，后缀优先决定去向；五种策略 + `route/auto`。
- **省 token**：提示词压缩、工具输出压缩、本地探针就地应答。
- **自助接入**：7 个客户端一键配置，写入前自动备份、可字节级还原；远程机器用 `wb_agents.py`。
- **运维可视化**：剩余用量估算、积分获取历史、可用性实测、更新通知中心。

如果你只需要 WorkBuddy 双区域账号池，直接用上游即可；需要多通道合并与账号自动化，用本项目。

## 与 LiteLLM

[LiteLLM](https://github.com/BerriAI/litellm) 是成熟的 **SDK + 代理**，强项是覆盖面（100+ provider）、统一的请求 / 响应格式、预算与团队管理、生产级部署。它的接入前提是你手上有各家的 **API Key**。

wb-proxy 不解决「统一 SDK」问题，解决的是另一件事：**某些服务根本不发 API Key，只认客户端登录态**。所以两者不是替代关系——wb-proxy 也可以作为 LiteLLM 的一个 OpenAI 兼容上游。

## 与 one-api / new-api 类

这类项目是「**多 Key 分发与计费**」的经典方案：把多家 API Key 挂在 channel 上，对外发子 Key，做额度、分组、团队配额与账单。

差异在凭据形态：一个账号 OAuth 登录态会过期、需要保活、有设备指纹与风控，还可能带签到 / 积分体系。wb-proxy 把这些当成一等公民（调度器、限额护栏、指纹隔离），而 one-api 类的模型假设凭据是长期有效的 Key。

如果你的需求是「团队共用一个 OpenAI Key 并计费」，用 one-api / new-api；如果是「自己有若干客户端账号，想在本机合成一个接口」，用 wb-proxy。

## 与 magpie

[magpie](https://github.com/yetone/magpie) 是本地模型路由工具。wb-proxy 的「路由」页在策略设计上参考了它的路由组（`smart` / `order` / `rotate` / `usage` / `pace` 的语义），但 wb-proxy 的路由对象是**多账号 × 多渠道的上游候选**，且要考虑会话亲和以保住提示词缓存。

## 什么时候不该用 wb-proxy

- 你要的是**公开对外提供 API 服务**：本项目面向本机 / NAS 自用，没有多租户、计费与 SLA。
- 你只用**一家的官方 API Key**：直接配客户端即可，多一层网关只会增加故障点。
- 你不接受**账号登录态**的风险：本项目依赖上游客户端协议，上游改协议或风控收紧时会失效，需要跟进修复。
- 你要求**生产级稳定性**：这是个人自托管项目，无商业支持。
