# NeoQBot 资源编排指南

资源编排把账号、群和知识资源放在同一张可连接的画布中。它负责表达“谁管理谁、数据流向
哪里，以及某个 Bot 在某个群承担哪些事务”。QQ/飞书 Bot 的新增、删除、账号连接、登录和能力
配置全部在这个页面完成；系统设置只维护模型、默认策略、存储、运行时和安全等平台参数。
入群管理、消息记录与分析、公告同步等具体事务全部配置在 QQ Bot→QQ群连接上；QQ群节点还可以
单独覆盖默认的入群审核政策和关键词。

## 节点类型

| 节点 | 用途 | 点击后的详情 |
|---|---|---|
| QQ Bot | NapCat / OneBot 真人 QQ 账号 | 连接状态、扫码登录、单 Bot 详细配置、各群事务分工 |
| 飞书 Bot | 飞书 CLI 登录态与文档操作账号 | CLI 状态、登录/退出、单 Bot 详细配置 |
| QQ 群 | QQ 治理和消息采集范围 | 管理 Bot、逐 Bot 事务、消息、公告、风控分析、入群申请 |
| 飞书群 | 飞书协作或通知目标 | 平台标识、说明和连接关系 |
| 知识库 | 飞书、Notion、本地或其他知识资源 | Provider、访问地址、说明和连接关系 |

QQ Bot 单节点详细配置中的「管理员 QQ」用于身份校验与管理员私聊通知；同页新增的「管理群」是一组群号，当 NeoQBot 向管理员发送私聊通知（入群审核转人工、风控提醒、动作失败等）时，会把同一消息同时发到这些群里。管理群不要求启用任何编排连接，群号即 Bot 可发消息的 QQ 群；留空则只进行私聊通知。

在空白画布右键，或点击工具栏的“新建资源”即可创建节点。新建操作先打开中心配置窗口，只有
“保存并应用”成功后节点才正式写入配置。拖动节点主体调整位置，从节点右侧圆形端口拖到另一个
节点创建连接。选中 QQ 群节点后，右侧检查器保留入群申请、群聊消息、分析报告和公告版本的数据
看板，以及节点和连接的快捷开关；不会展开具体消息或公告。QQ群节点右键菜单中的“查看详细”会
打开独立群组工作台，也可以直接双击群节点；“详细编辑”用于策略、周期、阈值和目标等复杂配置。
其他菜单项用于定位、启用/停用或删除。双击连线或在右侧检查器中可以删除连接。

群组工作台按“消息、公告、申请、分析”分栏，保留类似 QQ 聊天窗口的时间顺序和成员上下文。
公告会显示当前版本、历史版本、归档状态以及“已在群聊删除”标签。每个分类都可以搜索、分页，
并选择全部、今天、一周、一月、半年或一年范围执行 JSON 导出和显式清理；消息清理会同时处理
SQLite 与按日 JSONL 归档。全部清理属于不可恢复操作，执行前需要二次确认并写入审计日志。

已有 Bot 的内部 ID 创建后不可修改。该 ID 同时绑定节点地址、OneBot Webhook、Secret 文件和
平台登录身份；显示名称可以自由修改。需要更换身份时，应新建 Bot、迁移连接并确认新账号登录，
再删除旧节点，避免把仍然有效的 NapCat 登录态误判成另一个账号。

QQ Bot 的“连接方式”分为两类：`bundled_napcat` 使用 Compose 自带的 `qq-bridge`、持久化
Secret 和二维码卷；整个部署最多只能有一个此类节点。其事件使用与 Bot ID 无关的
`/webhooks/onebot` 入口，因此修改昵称或迁移首个节点不会再留下 `/default` 404。其余 QQ Bot
必须选择 `external`，分别配置独立的 OneBot/NapCat 地址、Token 文件和二维码路径；一个
NapCat 进程不能同时代表两个真实 QQ 账号。

为兼容已经写入 NapCat 持久卷的旧部署，后端仍接受 `/webhooks/onebot/default` 并将其安全映射到
唯一的内置 NapCat Bot；`init-napcat` 会在每次初始化时删除所有指向旧 OneBot Webhook 路径的
客户端，只保留一个 `/webhooks/onebot` 上报客户端。若 NapCat 日志出现 HTTP 404，应先重新构建
NeoQBot，并重新运行 `init-volumes` 以刷新持久化配置。

NapCat 会在进程启动时读取 HTTP Client 的 URL 和 Token，配置文件写入后不会热重载。Compose 已将
`init-volumes` 的刷新关联到 `neoqbot` 与 `qq-bridge` 的重启；升级时仍建议显式执行：

```bash
docker compose up -d --build --force-recreate init-volumes neoqbot qq-bridge
```

这会保留命名卷中的 QQ 登录态和业务数据，只重建容器并让两端重新读取同一份 OneBot 凭据。若日志仍有
401，平台会写入不含 Token 明文的 `onebot_webhook_auth` 审计项，显示是否收到 Bearer Header 或
`X-Signature`、是否配置 HMAC Secret，以及当前 Bot 的连接方式；不要在排查过程中输出凭据。NapCat
HTTP Client 使用其 Token 对事件原文生成 HMAC-SHA1 `X-Signature`，不会发送 Bearer Header。

在 Compose 容器中运行 `neoqbot --config /app/data/config.yaml doctor` 会为内置 Bot 给出
`event_client_configuration`。该检查只返回 URL 路径、启用状态和一致性布尔值，`ok: true` 代表持久化
配置中的 HTTP Client 已与共享 OneBot Secret 对齐，绝不会返回 Token 或其指纹。

## 连接语义

| 连接 | 建议使用场景 | 对当前 Runtime 的影响 |
|---|---|---|
| 管理 `manages` | Bot 负责群治理、消息和公告事务 | QQ Bot → QQ 群形成可配置事务的群级分工 |
| 监听 `observes` | Bot 只采集或观察群事件 | 同样形成群级分工；若启用管理动作，诊断会提示关系语义不匹配 |
| 归档 `archives_to` | 群或 Bot 将公告、记录写入飞书/知识库 | 连接到飞书 Bot 时可映射公告归档目标 |
| 检索 `searches` | Bot 使用飞书 Bot 或知识库回答管理员查询 | 连接到飞书 Bot 时可映射检索账号 |
| 同步 `syncs` | 表达通用同步或后续适配器的数据流 | 当前主要作为声明式关系保存 |

一个 Bot 可以连接多个群，多个 Bot 也可以连接同一个群。业务记录使用 `bot_id + group_id`
隔离，因此相同消息 ID 在不同 Bot 下不会冲突。每条有效的 QQ Bot→QQ群连接都有独立 `tasks`：

```yaml
orchestration:
  edges:
    - id: recorder-observes-main
      source: qq-bot:recorder
      target: qq-group-main
      relation: observes
      enabled: true
      tasks:
        message_detection:
          record: true
          interval_minutes: 60
    - id: moderator-manages-main
      source: qq-bot:moderator
      target: qq-group-main
      relation: manages
      enabled: true
      tasks:
        join_management:
          execute_management: true
          minimum_confidence: 0.92
        message_detection:
          scheduled_analysis: true
          interval_minutes: 15
          window_minutes: 5
        announcement_sync:
          enabled: true
          sync_interval_minutes: 30
```

消息事务只有两个开关：`record` 实时备份文本消息的 QQ 号、群名片/昵称、发送时间与文本，并写入
SQLite 和按日 JSONL；`scheduled_analysis` 默认每 30 分钟分析最近 5 分钟。两者可任意单独开启；
定时分析单独开启时，消息只进入 SQLite 分析缓冲，不写长期 JSONL；两者同时开启时只保存一次，
分析直接读取这份 SQLite 窗口。风险达到阈值时会通知管理员，但不会自动禁言或踢人。

公告事务只有 `enabled` 一个开关。启用后 Runtime 会立即全量抓取群中已有公告，随后按
`sync_interval_minutes` 持续同步，不再区分“启动时同步”和“自动同步”。这些设置只影响当前
Bot→群连接，不会修改该 Bot 连接的其他群。

## 入群审核策略与开关

右键 QQ 群节点并选择“详细编辑”，可为该群填写专属审核政策、必要关键词和禁止关键词。未填写
的字段继承“系统设置 → 默认判断策略”；因此不同 QQ 群可以采用不同审核口径，而不影响其他群。

连接上的开关决定对应 Bot 是否执行该群的功能：

| 开关 | 行为 |
|---|---|
| 检测申请 | 接收并保存入群申请，不调用模型也不改变申请状态。 |
| 执行审核 | 按该群的策略调用模型，并把建议与原因通知管理员；同时自动开启“检测申请”。 |
| 自动同意 | 当模型建议同意且达到该连接的最低置信度时，调用 QQ 同意申请。 |
| 自动拒绝 | 当模型建议拒绝且达到该连接的最低置信度时，调用 QQ 拒绝申请。默认建议保持关闭。 |
> **监听管理员人工处理**：当某 QQ Bot→群连接开启“检测申请”时，NeoQBot 还会监听 OneBot 的 `group_increase`（`sub_type=approve`）通知。管理员在 QQ 客户端**同意**入群后，系统会把对应入群申请的处理结果记为 `approved_by_admin`、处理人记为事件中的 `operator_id`（系统自动通过时 `operator_id` 为 0，记为未知），并写入审计日志。注意：OneBot 11 / NapCat **不会**在管理员“拒绝”入群申请时推送任何事件，因此拒绝结果及其处理人无法从事件可靠记录。

同一 QQ 群可连接多个 Bot：群节点策略在这些 Bot 间共享，开关、最低置信度、消息周期和公告目标
仍按每条 Bot→群连接分别保存。

## 典型拓扑

### 单账号管理多个群

```text
QQ Bot ──管理──> 社区主群
      ├─管理──> 审核群
      └─监听──> 公告群
```

### 多账号协作同一群

```text
记录 Bot ──监听──> 社区主群 <──管理── 审核 Bot
```

在“社区主群”的连接上，记录 Bot 可以只开启“记录”，审核 Bot 则开启入群审核或“定时分析”。
两个账号使用独立 Webhook、Token 和审计身份；它们连接其他群时可以采用完全不同的事务。
多个 Bot 抓取同一群公告时，平台优先使用“群号 + 规范化标题/内容 + 发布时间分钟槽”识别同一
公告；即使 OneBot 返回不同公告 ID，同内容且同一发布时间也只保留一个群级档案，并记录所有来源
Bot 与来源公告 ID。不同时间再次发布的相同内容仍会单独留档。启动迁移会自动合并旧数据库中已经
产生的这类重复项。

### 公告归档与知识检索

```text
QQ Bot ──管理──> QQ 群 ──归档──> 知识库
   └────归档──> 飞书 Bot
   └────检索──> 飞书 Bot
```

## 旧配置迁移

加载旧版配置时，后端会在校验前读取每个 QQ Bot 的 `managed_group_ids` 与 `tasks`：

1. 为尚不存在的群号创建 QQ 群节点；
2. 为 Bot 和群创建 `manages` 连接；
3. 把原有 Bot 级 `tasks` 深拷贝到该 Bot 的每条 QQ 群连接，确保旧行为不丢失；
4. Webhook、Token 和管理员配置继续保留在资源编排的单 Bot 详细配置中；
5. 再次保存时只写入 `orchestration.resources`、`orchestration.edges[].tasks` 和
   `orchestration.layout`，不再持久化旧字段。

连接任务中的旧开关也会收敛迁移：`record_only` / `realtime_detection` 迁移到 `record`，
`polling_detection` / `analyze` / `handle` 迁移到 `scheduled_analysis`，公告的 `auto_sync` /
`sync_on_startup` 迁移到唯一的 `enabled`。保存后只输出新字段。

若旧版启用了事务却没有任何可迁移的群连接，配置会明确拒绝加载并提示先补充群归属，避免把有
副作用的任务静默扩散到未知范围。新配置中，编排边是唯一权威来源，不再维护 Bot 级镜像字段。

## 保存与并发修改

GUI 每次读取配置时都会取得一个修订号。保存时如果其他标签页或管理员已经更新配置，服务端
返回 `409 Conflict`，当前页面会提示刷新，而不会覆盖较新的配置。系统设置和资源编排使用不同
的保存接口：前者无法覆盖 `qq`、`feishu` 或 `orchestration`，后者也无法覆盖模型、策略、存储和
系统安全配置。离开有未保存修改的设置页或编排页时，浏览器也会要求确认。

OneBot 上游可能仍会把所有群事件发送到 Webhook。NeoQBot 在鉴权和 JSON 校验后立即按
`bot_id + group_id` 查找有效编排连接；未编排群返回 `reason: unmanaged_group`，不会进入运行时
队列、不会写数据库，相关记录只保留为 DEBUG 级别诊断信息。

## 操作建议

- 先创建和配置 Bot，再创建群和知识库节点；
- 连接 QQ Bot 与 QQ 群后，在右侧检查器快速切换功能；右键 QQ 群节点，在详细编辑中配置本群策略、阈值和周期；
- 使用搜索框按名称、内部 ID、群号或节点类型快速定位；
- 大量导入旧群号后先使用“自动布局”，再人工调整重点链路；
- 首次真实运行保持 `dry_run: true`，确认群详情中的消息、公告与分析记录正确；
- Bot ID 创建后不可修改；更换身份时新建 Bot 并迁移连接，删除飞书 Bot 时 GUI 会清理群级引用；
- 知识库连接当前首先用于拓扑和配置表达，真正的数据写入仍取决于对应飞书 CLI 或后续适配器。
