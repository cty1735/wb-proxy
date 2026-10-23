# 新功能通知

<!--
往这个文件里追加一节「## 日期 标题」，GitHub 上的这份就是唯一源：用户本地
的网关最多每 20 秒回源一次，面板每 30 秒轮询（点右上角铃铛会立即回源），
不需要重启、不需要发版。直连 GitHub 失败时会自动走 gh-proxy / Gitee / 本地副本。

标题下面紧跟的字段都可省略：

  id: notices-center      稳定 id；不写则按「日期 + 标题」生成哈希。
                          写了 id 之后改标题不会让「不再显示」失效。
  level: feature          feature / improve / fix / notice（默认 notice）
  title_en: ...           英文界面用；正體中文用 title_hant
  body_en: ...            正文同理

正文按空行分段；连续的「- 」行渲染成列表。行内支持 **粗体**、`代码` 与
[链接](https://example.com)。HTML 注释（像这段）不会显示。列表按日期倒序，
同一天后追加的排在最前面。
-->

## 2026-10-23 本次更新
id: release-2026-10-23
level: feature
title_en: What's new in this release
body_en: |
  - New "Routing" tab: WorkBuddy / Qoder / Trae / Egg models in one pool, with route/auto and five strategies
  - The model list always shows the full pool, no longer follows the gateway tab
  - Pool changes sync to connected clients automatically; no manual apply needed
  - New notice center: bell icon in the top-right, and "Don't show again" is remembered
  - Browser confirm / alert / prompt replaced with in-panel dialogs
  - One-click enable / disable from the model list header

本次更新：

- **路由总网关**：顶部新增「路由」页签，WorkBuddy / Qoder / Trae / 鸡蛋 的模型合成一个池子，支持 `route/auto` 和 5 种策略
- **模型清单固定为总池子**：不再随顶部网关标签变化，一眼看全所有可用模型
- **自动同步到客户端**：池子变化后自动写回已接入的 Agent，不用再手动点「同步」
- **新功能通知**：右上角铃铛，登录后自动展开；点「不再显示」后不再打扰
- **面板内弹框**：确认 / 提示 / 输入不再弹浏览器原生框
- **一键启用 / 停用**：模型清单表头可直接批量开关
