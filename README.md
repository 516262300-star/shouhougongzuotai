# 利德仕电商自动化售后工作台

面向利德仕多平台、多店铺的售后中台。当前仓库已完成 Phase 1 工程骨架、全量数据库初始化、拼多多七店同步、天猫六店售后同步及模块 1/2/3 安全试运行、淘宝/1688/京东/抖音可配置多店只读售后同步，以及模块 1–4 的核心流程。

## 当前边界

- 已实现：配置加载、MySQL 连接池、Alembic 迁移、健康检查、Docker Compose、本地拼多多联调、拼多多七店与天猫六店售后增量同步、淘宝/1688/京东/抖音官方 API 只读售后增量同步、模块 1 在途拦截队列与快递 100 退款闸门、模块 2 ERP 客户退货单/退货暂存核对、仓库验货、明细一致后自动退款与异常转人工、模块 1/2/3 常驻后台运行器、模块 3 未发货 ERP 补开退款单与异常待办、模块 4 售后原因自动分类与型号归因看板、模块 5 ERP 退货报废只读同步与损失核定、已出包判定队列、企微机器人通知和受写开关保护的拼多多同意退款动作。
- 已建立全局业务表及内部同步表：`shops`、`aftersales_orders`、`aftersales_items`、`return_scrap_records`、`negative_reviews`、`pdd_sync_cursors`、`tmall_sync_cursors`、`platform_sync_cursors`、`aftersales_action_tasks`、`warehouse_return_records`、`warehouse_return_items`。
- ERP 未提供独立 API；模块 3 已通过受双重写开关保护的管理系统网页适配器处理“未发货、已退款、有订单但未开退款单”。已出包锁单仍保留人工回填 CLI。模块 2 只读查询 ERP 客户退货单和退货暂存列表，不在 ERP 内新建或认领退货单；实收明细完全一致且独立退款开关开启时调用拼多多退款，明细不一致时冻结退款并转人工。
- 所有外部写入默认关闭。企微、拼多多、ERP 分别受 `QYWX_WRITE_ENABLED`、`PDD_WRITE_ENABLED`、`ERP_WRITE_ENABLED` 及对应功能开关保护。

## 本地启动

### Docker Compose（推荐）

```powershell
Copy-Item .env.example .env
docker compose up --build
```

`migrate` 容器会在 MySQL 健康后执行 `alembic upgrade head`；迁移成功后 API 才会启动。首次启动前请修改 `compose.yaml` 中的示例数据库密码。

### 本机 Python

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
uvicorn aftersales_workbench.main:app --reload
```

Swagger 文档：<http://127.0.0.1:8000/docs>

## 数据库迁移

```powershell
# 升级到最新结构
alembic upgrade head

# 回滚一个版本（会删除表，请先备份）
alembic downgrade -1
```

自动迁移的输入是 `migrations/versions/` 中的版本脚本，输出是 `DATABASE_URL` 指向的 MySQL schema。迁移失败时 API 容器不会启动；修复配置或数据库后重新执行 `docker compose run --rm migrate`。

本机没有 Docker 或已注册 MySQL 服务时，可使用本机 MySQL 8.4 的项目专用实例。当前联调实例只监听 `127.0.0.1:3306`，数据保存在被 Git 忽略的 `.mysql-data/`；因中文工作区路径兼容性，MySQL 配置与数据目录联接保存在用户 AppData 的 `lds-aftersales-mysql.ini` / `lds-aftersales-mysql-data`。不得对非空 `.mysql-data/` 再执行初始化；该实例不是 Windows 服务，电脑重启后需要重新启动进程，再执行 `alembic current` 确认迁移版本。

## 环境变量

| 变量 | 用途 | 默认值 |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy MySQL 连接串 | 本地 `aftersales` 数据库 |
| `DB_POOL_SIZE` | 常驻连接数 | `10` |
| `DB_MAX_OVERFLOW` | 额外连接数 | `20` |
| `DB_POOL_RECYCLE_SECONDS` | 连接回收秒数 | `1800` |
| `APP_ENV` | 运行环境标识 | `development` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `PDD_SHOP_CODE` | 本地店铺标识，不是密钥 | `pdd-test-shop` |
| `PDD_CLIENT_ID` | 拼多多开放平台应用 ClientId | 无 |
| `PDD_CLIENT_SECRET` | 拼多多应用 Secret | 无 |
| `PDD_ACCESS_TOKEN` | 单店铺授权 Token | 无 |
| `PDD_API_URL` | 拼多多官方网关 | `https://gw-api.pinduoduo.com/api/router` |
| `PDD_TIMEOUT_SECONDS` | 单次请求超时秒数 | `10` |
| `PDD_READ_MAX_ATTEMPTS` | 只读请求最大尝试次数（含平台频率限制重试） | `5` |
| `PDD_WRITE_ENABLED` | 拼多多同意退款写开关 | `false` |
| `PDD_SYNC_INITIAL_LOOKBACK_HOURS` | 新店铺无游标时的首次回溯小时数 | `72` |
| `PDD_SYNC_OVERLAP_SECONDS` | 续传时向前重叠秒数，用于防止边界漏单 | `300` |
| `PDD_SYNC_PAGE_SIZE` | 售后增量单页数量 | `100` |
| `ERP_WRITE_ENABLED` | ERP 外部写操作总开关；管理系统待办发布还需功能开关同时开启 | `false` |
| `ERP_READ_DATABASE_URL` | 旧管理系统 MySQL 只读连接串，用于按拼多多订单反查客户档案归属业务员 | 无 |
| `ERP_READ_CACHE_SECONDS` | 归属业务员查询在工作台内的缓存秒数 | `300` |
| `ERP_WEB_LOOKUP_ENABLED` | 未配置数据库时，启用管理系统网页登录只读查询 | `false` |
| `ERP_WEB_BASE_URL` | 管理系统网站根地址 | `https://ldswj.net` |
| `ERP_WEB_USERNAME` / `ERP_WEB_PASSWORD` | 管理系统员工登录凭据，只允许写入本机 `.env` | 无 |
| `ERP_WEB_TIMEOUT_SECONDS` | 管理系统网页请求超时秒数 | `15` |
| `ERP_SALES_OWNER_SYNC_ENABLED` | 模块 1 周期内自动刷新归属业务员缓存 | `false` |
| `ERP_SALES_OWNER_SYNC_BATCH_SIZE` | 每个模块 1 周期最多刷新多少笔售后订单 | `20` |
| `ERP_SALES_OWNER_REFRESH_SECONDS` | 已缓存归属业务员的正常刷新间隔 | `86400` |
| `ERP_TODO_PUBLISH_ENABLED` | 将模块 1 人工处理任务真实发布到管理系统待办页 | `false` |
| `ERP_TODO_MAX_ATTEMPTS` | 待办发布失败后允许安全重新入队的最大尝试次数 | `3` |
| `ERP_RETURN_MATCH_SYNC_ENABLED` | 周期只读核对拦截退回的 ERP 退货单与客户累计应收 | `false` |
| `ERP_SCRAP_SYNC_ENABLED` | 模块 5 周期只读同步 ERP 退货单并识别“报废+颜色” | `false` |
| `ERP_SCRAP_SYNC_REFRESH_SECONDS` | 模块 5 两次增量同步之间的最短间隔秒数 | `1800` |
| `ERP_SCRAP_SYNC_LOOKBACK_DAYS` | 首次回填及循环历史复核天数 | `90` |
| `MODULE1_ERP_REFUND_EXECUTION_ENABLED` | 模块 1 拦截退回补开 ERP 退款单功能开关；还需 `ERP_WRITE_ENABLED=true` | `false` |
| `MODULE2_WORKER_ENABLED` | 将模块 2 验货通过后的退款任务接入常驻后台周期 | `false` |
| `MODULE2_PDD_REFUND_EXECUTION_ENABLED` | 模块 2 平台退款功能开关；还需 `PDD_WRITE_ENABLED=true` | `false` |
| `MODULE2_REFUND_MIN_RETURN_ID` | 模块 2 自动退款上线水位，只处理不小于该收货记录 ID 的验货通过记录 | `0` |
| `MODULE2_ERP_INTAKE_MIN_ORDER_ID` | ERP退货单自动接入水位，只处理不小于该本地售后订单 ID 的记录 | `0` |
| `MODULE3_ERP_REFUND_EXECUTION_ENABLED` | 模块 3 未发货补开 ERP 退款单功能开关；还需 `ERP_WRITE_ENABLED=true` | `false` |
| `MODULE3_WORKER_ENABLED` | 将模块 3 接入现有常驻后台周期 | `false` |
| `MODULE3_WORKER_BATCH_LIMIT` | 模块 3 每周期最多新建及处理的订单数；首次上线保持 1 | `1` |
| `MODULE3_ERP_REFUND_RECHECK_SECONDS` | 同一未闭环 ERP 异常的最短复查间隔 | `1800` |
| `QYWX_INTERCEPT_WEBHOOK_URL` | 模块 1 快递拦截群机器人 Webhook（密钥） | 无 |
| `QYWX_TIMEOUT_SECONDS` | 企微请求超时秒数 | `10` |
| `QYWX_WRITE_ENABLED` | 企微机器人发送开关 | `false` |
| `MODULE1_WORKER_SHOP_NUMBERS` | 模块 1 后台运行店铺序号 JSON 数组 | `[1,2,3,4,5,6,7]` |
| `MODULE1_WORKER_INTERVAL_SECONDS` | 后台运行器每个完整周期结束后的等待秒数 | `60` |
| `MODULE1_WORKER_MAX_SYNC_WINDOWS` | 每店每周期最多处理的 30 分钟同步窗口数 | `2` |
| `MODULE1_WORKER_TASK_LIMIT` | 每周期最多准备、发送或退款的动作任务数 | `20` |
| `MODULE1_REFUND_BUSINESS_TIMEZONE` | 快递拦截客服工作时区 | `Asia/Shanghai` |
| `MODULE1_REFUND_BUSINESS_START_HOUR` / `MODULE1_REFUND_BUSINESS_END_HOUR` | 允许执行平台自动退款的工作时间，开始时刻包含、结束时刻不包含 | `9` / `21` |
| `MODULE1_NOTIFICATION_TRANSPORT` | 拦截通知出口；支持 `disabled` / `qywx_webhook` / `desktop` | `disabled` |
| `MODULE1_NOTIFICATION_MIN_TASK_ID` | 自动通知上线水位；仅预检和发送任务 ID 大于等于该值的拦截通知，防止历史积压批量补发 | `0` |
| `MODULE1_PDD_REFUND_EXECUTION_ENABLED` | 后台运行器的平台退款执行总开关 | `false` |
| `MODULE1_DESKTOP_GROUP_MAP` | 拼多多物流公司 ID 到企业微信外部群完整精确群名的 JSON 白名单 | `{}` |
| `MODULE1_DESKTOP_SEND_ENABLED` | 企业微信桌面自动发送总开关；版本库安全默认关闭 | `false` |
| `MODULE1_DESKTOP_PROCESS_NAME` | 允许接收键盘输入的企业微信进程名 | `WXWork.exe` |
| `MODULE1_DESKTOP_LEDGER_PATH` | 本机防重与恢复账本；只保存任务 ID、状态和消息哈希 | `.runtime/desktop-notice-ledger.jsonl` |
| `MODULE1_DESKTOP_LOCK_PATH` | 后台与人工桌面发送共享的跨进程单实例锁 | `.runtime/desktop-notice.lock` |
| `MODULE1_DESKTOP_BATCH_LIMIT` | 后台每周期最多发送的桌面消息数，首次上线保持 1 | `1` |
| `KUAIDI100_CUSTOMER` / `KUAIDI100_KEY` | 快递 100 实时查询授权（密钥） | 无 |
| `KUAIDI100_DEFAULT_PHONE` | 需要手机号校验的快递所用默认手机号 | 无 |
| `KUAIDI100_CARRIER_MAP` | 拼多多物流公司 ID 到快递 100 公司代码的 JSON 映射 | `{"85":"yuantong","131":"debangwuliu","384":"jtexpress"}` |
| `KUAIDI100_SUCCESS_REFRESH_SECONDS` | 成功取得轨迹后的最短刷新间隔 | `300` |
| `KUAIDI100_FAILURE_INITIAL_RETRY_SECONDS` / `KUAIDI100_FAILURE_MAX_RETRY_SECONDS` | 查询失败的指数退避起始/上限秒数 | `300` / `1800` |
| `KUAIDI100_MANUAL_AFTER_FAILURES` | 连续失败达到该次数后在工作台标记需人工核对 | `6` |
| `PDD_APP_1_CLIENT_ID` / `PDD_APP_1_CLIENT_SECRET` | 1–4 店共用的开放平台应用凭据 | 无 |
| `PDD_APP_2_CLIENT_ID` / `PDD_APP_2_CLIENT_SECRET` | 5–7 店共用的另一组应用凭据 | 无 |
| `PDD_SHOP_1_CODE` … `PDD_SHOP_7_CODE` | 1–7 店的本地稳定代号 | `pdd-shop-01` … `pdd-shop-07` |
| `PDD_SHOP_1_APP` … `PDD_SHOP_7_APP` | 店铺使用的应用组；1–4 店为 `1`，5–7 店为 `2` | `1` / `2` |
| `PDD_SHOP_1_ACCESS_TOKEN` … `PDD_SHOP_7_ACCESS_TOKEN` | 1–7 店各自的授权 Token | 无 |
| `TMALL_APP_KEY` / `TMALL_APP_SECRET` | 天猫六店共用的淘宝开放平台应用凭据 | 无 |
| `TMALL_SHOP_1_SESSION_KEY` … `TMALL_SHOP_6_SESSION_KEY` | 天猫六店各自的卖家授权 SessionKey | 无 |
| `TMALL_SHOP_1_CODE` … `TMALL_SHOP_6_CODE` | 天猫六店的本地稳定代号 | `tmall-shop-01` … `tmall-shop-06` |
| `TMALL_SYNC_ENABLED` | 将天猫只读售后同步接入常驻后台周期 | `false` |
| `TMALL_MODULE123_TRIAL_ENABLED` | 将天猫新售后接入模块 1/2/3 试运行；天猫平台退款仍由人工审核 | `false` |
| `TMALL_MODULE123_MIN_ORDER_ID` | 天猫模块 1/2/3 独立上线水位，只处理本地 ID 不小于该值的记录 | `0` |
| `TMALL_SYNC_INITIAL_LOOKBACK_HOURS` | 天猫新店铺无游标时的首次回溯小时数 | `72` |
| `TMALL_SYNC_OVERLAP_SECONDS` | 天猫增量续传时向前重叠秒数 | `300` |
| `TMALL_SYNC_WINDOW_HOURS` | 天猫单个修改时间窗口小时数 | `24` |
| `TAOBAO_API_URL` / `TAOBAO_REQUEST_METHOD` | 历史付费中转地址及请求方式 | `https://odiych.goldbrantech.com/forward.ashx` / `GET` |
| `TAOBAO_SHOPS_JSON` | 淘宝第三方中转配置；每店含中转应用凭据与 `session_key` | `[]` |
| `ALIBABA_1688_SHOPS_JSON` | 1688 任意多店配置；每店含 `app_key` / `app_secret` | `[]` |
| `JD_API_URL` / `JD_REQUEST_METHOD` | 历史付费中转地址及请求方式 | `https://odiych.goldbrantech.com/forward.ashx` / `GET` |
| `JD_SHOPS_JSON` | 京东第三方中转配置；每店含应用凭据与 `access_token` | `[]` |
| `DOUYIN_SHOPS_JSON` | 抖音第三方应用配置；可用静态 Token，或用店铺 ID 自动自授权 | `[]` |
| `DOUYIN_TOKEN_CACHE_PATH` | 抖音动态 Token 的本机缓存；被 Git 忽略 | `.runtime/douyin-access-token-cache.json` |
| `TAOBAO_SYNC_ENABLED` / `ALIBABA_1688_SYNC_ENABLED` / `JD_SYNC_ENABLED` / `DOUYIN_SYNC_ENABLED` | 将对应平台接入常驻只读售后同步 | `false` |
| `MARKETPLACE_SYNC_INITIAL_LOOKBACK_HOURS` | 新配置店铺首次同步回溯小时数 | `72` |
| `MARKETPLACE_SYNC_OVERLAP_SECONDS` | 新平台增量游标向前重叠秒数 | `300` |
| `MARKETPLACE_SYNC_WINDOW_HOURS` | 新平台单个修改时间窗口小时数 | `24` |

生产环境不得使用示例密码，也不得将 `.env`、店铺 Secret 或 Token 提交到 Git。

1–4 店共用 `PDD_APP_1_CLIENT_ID` / `PDD_APP_1_CLIENT_SECRET`，5–7 店共用 `PDD_APP_2_CLIENT_ID` / `PDD_APP_2_CLIENT_SECRET`。每个店铺仍必须将自己的 Token 填入对应的 `PDD_SHOP_N_ACCESS_TOKEN`，不要将多个 Token 用逗号拼在同一行。单店的 `PDD_CLIENT_ID`、`PDD_CLIENT_SECRET` 和 `PDD_ACCESS_TOKEN` 暂时保留，仅用于旧联调命令回退。

售后订单记录页的“归属业务员”来自旧管理系统客户档案。系统优先使用 `ERP_READ_DATABASE_URL`：将平台订单号转换为 `pdd{订单号}`，在 `00sobackup.客户编号` 精确找到客户，再读取 `kehu.归属业务员`；客户档案未填写时回退到订单快照。没有数据库只读账号时，可配置 `ERP_WEB_LOOKUP_ENABLED=true` 以及管理系统员工账号，工作台会登录 `/leedis/index.php/welcome/loginact`，再调用客户档案自动补全接口只读查询。网页查询结果默认缓存 5 分钟，登录失效只自动重登一次，不会访问客户修改接口。登录凭据只能保存在被 Git 忽略的本机 `.env`，严禁写入 README 或提交仓库。

启用 `ERP_SALES_OWNER_SYNC_ENABLED` 后，模块 1 每个周期会在拼多多增量同步后，将一小批客户名字、归属业务员、匹配状态和查询时间缓存到本地 `aftersales_orders`。页面基于本地缓存进行全量业务员筛选，不会在一次页面查询中批量请求旧管理系统。正常结果每天刷新；网页暂时不可用时 5 分钟后重试，归属查询失败不会阻断拦截、物流闸门或退款安全流程。首次接入可分批执行 `aftersales-sync-sales-owners.exe --limit 20`，每批完成即提交，不需要一次等待全部历史订单。

管理系统人工待办发布复用同一组 `ERP_WEB_BASE_URL`、`ERP_WEB_USERNAME`、`ERP_WEB_PASSWORD`。只有 `ERP_TODO_PUBLISH_ENABLED=true` 与 `ERP_WRITE_ENABLED=true` 同时满足时才会调用 `/leedis/index.php/wunderlist/stdnew`；其余情况下只在本地动作队列准备 `ERP_CREATE_MANUAL_TODO`。模块 1/3 的远端事项分别包含 `【售后工作台 M1:售后单号】` / `【售后工作台 M3:售后单号】` 幂等标识，发布前后都会按经办人回查，成功后将管理系统待办 ID 保存到动作任务 `payload.external_todo_id`，因此超时重试不会重复发布。归属业务员为空或冲突时不会猜测经办人，也不会发布。

售后工作台的“人工待办”导航和售后摘要中的“待人工”指标均可进入只读发送审计页。列表展示发送状态、对应业务员、触发原因、触发模块、平台订单号和售后单号；点击记录后，右侧详情会展示发送给业务员的完整事项、ERP 待办 ID、发送时间、尝试次数以及发送失败或取消原因。只有任务状态为 `SUCCEEDED` 且已取得远端待办 ID 时，页面才显示“已发送给业务员”；其余状态不会误报为发送成功。新任务会在动作载荷中保存明确的 `reason_text`，历史任务则按原因代码兼容映射中文原因。

## 拼多多单店只读联调

1. 在拼多多开放平台确认应用已获得 `pdd.refund.list.increment.get` 和 `pdd.refund.information.get` 权限。`pdd.mall.info.get` 仅在使用 `--with-mall-info` 时需要。
2. 复制 `.env.example` 为 `.env`，填入一个店铺的 `PDD_CLIENT_ID`、`PDD_CLIENT_SECRET`、`PDD_ACCESS_TOKEN`。
3. 执行只读测试：

```powershell
.\.venv\Scripts\pdd-check-shop.exe --minutes 30 --status 2 --page-size 20
```

命令校验店铺授权和近 30 分钟售后增量列表，只输出店铺标识和记录数。如需同时读取店铺名称，增加 `--with-mall-info`。如需测试某一售后详情：

```powershell
.\.venv\Scripts\pdd-check-shop.exe --order-sn "平台订单号" --after-sales-id 123456
```

只读请求在网关 HTTP 429/5xx、网络异常或平台频率限制码 `70031` 时指数退避重试；其他平台业务错误不重试，并保留 `error_code` 和 `request_id` 供排查。该联调命令不会调用 `pdd.refund.agree` 或任何写接口。

## 七店售后增量同步

同步器默认拉取状态 2（买家申请、待商家处理）、状态 3（等待商家确认收货）和状态 10（退款成功），按 30 分钟窗口分页读取。状态 10 必须同步：拼多多未发货仅退款通常会极速退款，若只读取待处理状态，模块 3 会漏掉已经退款但 ERP 仍需取消排单和平账的订单。每条售后单补查售后详情和订单详情，然后幂等写入 `aftersales_orders` 和 `aftersales_items`。每个已提交窗口的进度保存在 `pdd_sync_cursors`。

同步结果同时保存 `platform_after_sales_status`、订单 `platform_order_refund_status`、`is_speed_refund`、商品名、平台售后创建/更新时间、原始退款原因和买家留言。模块 4 会将原因和留言按可审计关键词归入“不喜欢/不想要、质量问题、规格/颜色不合适、发货/物流问题、描述不符、价格/优惠原因、其他/未说明”七个互斥类别。只有售后状态明确为 10，或订单退款状态明确为 4（退款成功），才作为“平台已退款”的事实；`speed_refund_flag=1` 只记录极速退款标记，不单独作为完成依据。

拼多多增量列表与售后详情的 `after_sales_type` 编码不同：同步器以增量列表的 2（仅退款）、3（退货退款）、4（换货）为准；仅当列表缺失该字段时，才按详情接口的 1/2/3 编码回退。遇到未支持类型时当前窗口会回滚并保留游标，修正后可直接续传。

执行前先升级数据库：

```powershell
alembic upgrade head
```

建议先用一店、两个窗口验证：

```powershell
.\.venv\Scripts\pdd-sync-refunds.exe --shops 1 --lookback-hours 72 --max-windows 2
```

验证无误后执行一店完整回溯，再执行所有店铺：

```powershell
.\.venv\Scripts\pdd-sync-refunds.exe --shops 1 --lookback-hours 72
.\.venv\Scripts\pdd-sync-refunds.exe --lookback-hours 72
```

可用 `--statuses 2 3 10`、`--shops 1 2 3` 限定范围。单店失败时该店当前窗口回滚，其他店继续；命令最终返回非零退出码。重新执行会从该店最后成功游标向前重叠 5 分钟续传，已存在的售后单按售后单号更新，不会重复新建。默认状态集合从 `2,3` 改为 `2,3,10` 后会使用新的同步游标范围，首次运行会按配置的回溯时长补齐退款成功记录。

同步器只向本地 MySQL 写入店铺和售后数据，不会调用拼多多写接口。任一店铺 Token 过期或接口异常时会被单店标记失败，不影响其他店铺。

## 天猫六店售后同步与模块 1/2/3 试运行

天猫接入直接调用淘宝开放平台正式 HTTPS 网关，不依赖旧管理系统网页或旧代码运行环境。六个店铺共用 `TMALL_APP_KEY` / `TMALL_APP_SECRET`，每店使用独立的 `TMALL_SHOP_N_SESSION_KEY`。同步器调用 `taobao.user.seller.get` 校验卖家身份，按修改时间分页读取 `taobao.refunds.receive.get`，再用 `taobao.refund.get` 和 `taobao.trade.fullinfo.get` 补齐退款原因、留言、商品、SKU、订单状态和退货运单。对已发货仅退款，另外调用 `taobao.logistics.orders.get` 读取原发货运单和物流公司；多包裹、无运单或物流接口失败时不猜测，记录失败关闭。结果与拼多多共用 `shops`、`aftersales_orders`、`aftersales_items`，进度单独保存在 `tmall_sync_cursors`。

默认仍是只读记录层。只有同时设置 `TMALL_SYNC_ENABLED=true`、`TMALL_MODULE123_TRIAL_ENABLED=true` 并配置独立上线水位后，水位之后的新天猫售后才进入模块 1/2/3：模块 1 对已发货全额仅退款执行物流预检和企业微信快递群拦截；模块 2 按客户退货运单核对 ERP 客户退货单或退货暂存列表，并逐项比较型号、颜色和数量；模块 3 对平台已经退款的未发货订单执行 ERP 取消履约及补开退款单。任一天猫店当轮同步失败时，该轮不放入天猫候选。

天猫平台退款在试运行期始终失败关闭：物流满足退款条件或模块 2 验货完全一致时，只将订单标记为“等待人工审核退款”，不会创建 `PDD_AGREE_REFUND` / `PDD_AGREE_RETURN_REFUND`，也不会调用任何拼多多写接口。原因是淘宝开放平台的天猫退款链路要求先审核再同意，且同意退款接口受授权子账号、短信验证及聚石塔调用环境约束；在这些条件完成真实验收前，不能把拼多多退款执行器复用于天猫。官方参考：[退款业务流程](https://developer.alibaba.com/docs/doc.htm?articleId=102594&docType=1&source=search&treeId=1)、[`taobao.rp.refunds.agree`](https://developer.alibaba.com/docs/api.htm?apiId=22465&scopeId=11527)、[`taobao.logistics.orders.get`](https://developer.alibaba.com/docs/api.htm?apiId=235)。

首次接入先升级数据库并校验六店授权：

```powershell
alembic upgrade head
.\.venv\Scripts\tmall-check-shops.exe
```

只同步一店最近一小时的一个窗口，确认后再同步全部六店：

```powershell
.\.venv\Scripts\tmall-sync-refunds.exe --shops 1 --lookback-hours 1 --max-windows 1
.\.venv\Scripts\tmall-sync-refunds.exe --lookback-hours 72
```

单店失败只回滚该店当前窗口，其他店继续；重新执行会从该店最后成功游标向前重叠 5 分钟幂等续传。若退款记录仍存在、但原交易已经无权查看或被平台归档，系统保留退款数据并将交易详情降级为空，不会让这一笔旧交易阻断整店同步。`TMALL_SYNC_ENABLED=true` 后，现有常驻后台运行器每个周期会附带执行天猫增量只读同步；凭据只能写入被 Git 忽略的本机 `.env`。

## 淘宝、1688、京东、抖音售后只读同步

四个平台沿用各自真实接入方式，不依赖旧管理系统进程在线运行。淘宝和京东继续使用历史购买的第三方转发服务；旧定时任务中的京东范围为默认 `p1` 与 `set_key('p2-')` 两店，迁移工具会把两组凭据分别写入 `jd-relay-01`、`jd-relay-02`。1688使用开放平台；抖音使用第三方应用授权，但 SDK 请求仍发往抖店官方 HTTPS 地址。新同步器独立完成签名、分页、详情补查、字段归一化和 MySQL 幂等入库：

- 淘宝把签名后的 `taobao.user.seller.get`、`taobao.refunds.receive.get`、`taobao.refund.get` 和 `taobao.trade.fullinfo.get` 以 GET 方式交给第三方 `forward.ashx` 中转；
- 1688 调用 `alibaba.trade.refund.queryOrderRefundList`、`alibaba.trade.refund.OpQueryOrderRefund` 和 `alibaba.trade.ec.getOrder.sellerView`；
- 京东通过同一第三方 `forward.ashx` 中转读取 `jingdong.pop.afs.soa.refundapply.queryPageList` 退款申请与 `jingdong.asc.serviceAndRefund.view` 退货售后服务单，并用 `jingdong.pop.order.get` 补齐订单和 SKU；
- 抖音调用 `/afterSale/List` 与 `/afterSale/Detail`，使用第三方应用 `app_key` / `app_secret` 和 HMAC-SHA256 签名；配置 `access_token_mode=authorization_self` 时，以店铺 ID 调用 `/token/create` 自动取得 Token，并在过期前刷新。

所有店铺配置使用 JSON 数组，因此不限制店铺数量。每个对象都要填写稳定且不能与其他平台重复的 `shop_code`，建议同时填写 `shop_name` 与平台店铺 ID。淘宝需要中转服务的 `app_key`、`app_secret`、`session_key`；1688 需要 `app_key`、`app_secret`；京东需要中转服务的 `app_key`、`app_secret`、`access_token`；抖音使用第三方应用的 `app_key`、`app_secret`，可填写静态 `access_token`，也可填写真实店铺 ID 并使用 `authorization_self`。示例：

```dotenv
TAOBAO_SHOPS_JSON=[{"shop_code":"taobao-shop-01","shop_name":"淘宝一店","platform_shop_id":"平台店铺ID","app_key":"填写值","app_secret":"填写值","session_key":"填写值"}]
ALIBABA_1688_SHOPS_JSON=[{"shop_code":"1688-shop-01","shop_name":"1688一店","platform_shop_id":"平台店铺ID","app_key":"填写值","app_secret":"填写值"}]
JD_SHOPS_JSON=[{"shop_code":"jd-shop-01","shop_name":"京东一店","platform_shop_id":"平台店铺ID","app_key":"填写值","app_secret":"填写值","access_token":"填写值"}]
DOUYIN_SHOPS_JSON=[{"shop_code":"douyin-shop-01","shop_name":"抖音一店","platform_shop_id":"数字店铺ID","app_key":"填写值","app_secret":"填写值","access_token_mode":"authorization_self"}]
```

从旧 PHP 服务迁移本机凭据时执行以下命令。工具只允许更新当前仓库根目录 `.env`，先把原配置备份到被 Git 忽略的 `.runtime/`，不会打印密钥；旧源码中的中转地址会自动升级为已验证可用的 HTTPS。首次迁移不打开常驻同步：

```powershell
.\scripts\migrate-legacy-marketplace-credentials.ps1 -LegacyRoot "D:\desktop\codex\daima\leedis2-main-a7580d396d7fa964463aad2886b8fe76bccf3825"
```

单窗口验证通过后，可只开启已通过的平台；例如淘宝、京东通过而抖音仍受 IP 白名单限制时：

```powershell
.\scripts\migrate-legacy-marketplace-credentials.ps1 -LegacyRoot "D:\desktop\codex\daima\leedis2-main-a7580d396d7fa964463aad2886b8fe76bccf3825" -EnableTaobao -EnableJd
```

填好一个平台后，先保持常驻开关关闭，单独同步最近一小时的一个窗口：

```powershell
.\.venv\Scripts\marketplace-sync-refunds.exe --platforms TAOBAO --lookback-hours 1 --max-windows 1
.\.venv\Scripts\marketplace-sync-refunds.exe --platforms 1688 --lookback-hours 1 --max-windows 1
.\.venv\Scripts\marketplace-sync-refunds.exe --platforms JD --lookback-hours 1 --max-windows 1
.\.venv\Scripts\marketplace-sync-refunds.exe --platforms DOUYIN --lookback-hours 1 --max-windows 1
```

真实记录核对无误后，再把对应的 `*_SYNC_ENABLED` 改为 `true` 并安全重启后台运行器。每个平台、每个店铺、每个已提交时间窗口的进度保存在 `platform_sync_cursors`；平台失败彼此隔离，也不会阻断拼多多后续动作链。原始平台售后状态与订单状态保存为文字字段，订单、退款金额、原因、留言、SKU、件数、平台时间和可取得的正向/退货运单统一进入 `shops`、`aftersales_orders`、`aftersales_items`。淘宝、京东已使用 HTTPS 中转；中转余额不足、旧授权失效、抖音运行 IP 未加入第三方应用白名单或自授权失败时，该店当前窗口回滚并保留游标，修复配置后可安全重跑。

售后订单页采用“平台 → 店铺”两级联动筛选：可先选拼多多、天猫、淘宝、1688、京东或抖音，再从该平台已经接入的店铺中选择具体店铺；未选平台时店铺框保持禁用，避免跨平台同名店铺造成误选。平台条件和店铺条件都会由后端执行，不只是前端隐藏。

淘宝、1688、京东、抖音当前严格限于“售后事实同步”：页面会显示对应平台订单，但不会生成企业微信拦截、平台退款、模块 2、模块 3 或 ERP 写入动作。拼多多运行完整自动化；天猫仅在独立开关和水位保护下进入模块 1/2/3 试运行，平台退款仍由人工审核；其余平台需逐个平台完成写接口权限、金额口径和物流安全闸门验收后才能另行开启自动处理。

## 模块 1：在途拦截与退款

模块 1 只扫描 `PENDING_CHECK`、发货状态为 `IN_TRANSIT`、具有发货运单号、售后类型严格为 `ONLY_REFUND`，并且“申请退款金额等于拼多多详情中的买家优惠后实付金额”的全额退款订单。金额统一保存为两位小数后精确比较：申请金额低于买家实付时标记为 `PARTIAL_REFUND_EXCLUDED`，视为补偿款、差价、运费或配件补偿，不拦截快递、不生成业务员待办；实付金额缺失或退款金额异常时失败关闭，冻结自动拦截。退款原因和留言仅用于展示，不能替代金额判断。退货退款留给模块 2，换货单也不会进入自动退款链路。拼多多只负责售后读取和同意退款，物流状态由独立的快递 100 适配器读取，不依赖或修改旧管理系统代码。

拼多多订单金额必须区分买家和商家两个口径：`platform_order_amount` 保存买家实付，`platform_discount_amount` 保存平台承担的优惠，`seller_discount_amount` 保存商家承担的优惠，`merchant_receivable_amount` 保存商家应收。买家是否全额退款仍按 `refund_amount == platform_order_amount` 判定；平台优惠不能因此被误判为部分退款。ERP 退款单及后续退货单平账按 `merchant_receivable_amount = platform_order_amount + platform_discount_amount` 核对。平台优惠字段缺失时商家应收保持为空，后续 ERP 自动平账必须失败关闭并转人工，不能把商家优惠或总优惠金额猜成平台补贴。当前流转为：

1. 生成幂等的本地 `QYWX_INTERCEPT_NOTIFY` 草稿动作；该动作尚未取得发送资格；
2. 发送前强制查询快递 100：运输中、派件中和查询无结果的任务保留；已签收任务取消并转 `MANUAL_PROCESSING`；已在退回途中或已经退回的任务取消，禁止重复通知；
3. 物流预检阶段整体失败或未配置快递 100 时采用失败关闭，当前周期禁止发送任何拦截通知；单票查询无结果时允许保留拦截通知，但退款闸门标记为 `HOLD`；
4. 企微发送成功后，订单进入 `INTERCEPT_PUSHED`；快递 100 退款闸门随即再次查询，不需要人工确认“已受理”。北京时间 09:00 含至 21:00 不含期间，普通在途可立即生成 `PDD_AGREE_REFUND`；
5. 21:00 至次日 09:00 只发送拦截消息，不生成或执行平台退款。订单的下次检查时间直接定在次日 09:00；到时必须重新查询最新物流，不使用夜间缓存结果；
6. 命中“派件/派送/投递”或“已签收”时不自动退款；查询失败、无可用轨迹或公司代码未映射时也一律失败关闭；拦截失败则进入 `INTERCEPT_FAILED`；
7. 出现“退回/退件/拒收/原路返回”等明确退回记录后才解除已触发的派件冻结。若平台尚未退款，在工作时间内生成 `PDD_AGREE_REFUND`，夜间则等到次日 09:00 复查；若平台已经退款则跳过平台写接口；
8. 平台退款完成但包裹尚在退回途中时进入 `INTERCEPT_REFUNDED_WAITING_RETURN`，并立即生成 `ERP_MATCH_RETURN_ORDER` 本地只读匹配任务；不再依赖快递 100 必须先返回“退回签收”，避免物流接口失败或轨迹延迟导致 ERP 闭环永远不启动；
9. 派件中、已签收无退回记录、拦截失败或订单进入 `MANUAL_PROCESSING` 时，按客户档案“归属业务员”幂等生成 `ERP_CREATE_MANUAL_TODO`；事项包含店铺、平台订单号、售后单号、发货运单和标准化物流状态，不复制快递轨迹中的电话、地址等原文；
10. ERP 匹配以发货运单号、型号、颜色和数量完全一致为自动处理前提；同一运单关联多笔已退款平台订单时，必须将这些订单的售后明细合计后与 ERP 整张退货单完全相等，才允许逐笔闭环。仅仅“包含其中一笔”的子集匹配不会放行；暂存认领仍不自动执行。

闭环进度统一在工作台“在途拦截”页查询。顶部指标可直接下钻“待仓库开单”“暂存待认领”“客户名下待平账”“售后已闭环”，列表同步显示 ERP 退货单号、累计应收和最近核对时间；订单详情的“拦截退回闭环”区保留 ERP 状态、退货单号、累计应收、核对说明与闭环时间。只有退货单已经从暂存归入客户名下、型号/颜色/数量完全匹配且客户累计应收归零时，订单才计入“售后已闭环”。

先预览候选数量，再写入本地队列：

```powershell
.\.venv\Scripts\aftersales-preview-module1.exe --shops pdd-shop-01 --limit 100
.\.venv\Scripts\aftersales-preview-module1.exe --shops pdd-shop-01 --limit 100 --details
.\.venv\Scripts\aftersales-process-module1.exe
.\.venv\Scripts\aftersales-process-module1.exe --shops pdd-shop-01 --limit 100 --apply
```

升级金额闸门后，先只读回填历史售后的买家实付、商品金额、平台优惠、商家优惠和商家应收，再确认写入。命令只调用拼多多读取接口；`--apply` 才会写本地金额、分类状态并取消部分退款遗留的待发送模块 1 动作。输出中的 `platform_coupon` 表示命中平台优惠的订单数：

```powershell
.\.venv\Scripts\pdd-backfill-refund-amounts.exe --limit 100
.\.venv\Scripts\pdd-backfill-refund-amounts.exe --limit 100 --apply
```

`aftersales-preview-module1` 是上线前的一键只读核对命令：它读取严格筛选后的真实订单并查询快递 100，汇总企微预计通知数、平台退款预计调用数、已退款跳过数、物流冻结数及查询失败数。`--details` 只显示脱敏后的订单号、售后单号和运单号。该命令没有 `--apply` 参数，不写数据库、不创建动作任务、不发送企微、不调用拼多多退款接口；即使 `.env` 中的写入开关已经打开也不会执行写操作。

配置 `QYWX_INTERCEPT_WEBHOOK_URL` 后，将 `QYWX_WRITE_ENABLED` 改为 `true`，先预览，再真实发送：

```powershell
.\.venv\Scripts\aftersales-execute-actions.exe --types QYWX_INTERCEPT_NOTIFY
.\.venv\Scripts\aftersales-execute-actions.exe --types QYWX_INTERCEPT_NOTIFY --apply
```

`RETURNED` 只作为人工核实已有明确退回轨迹时的兜底，不能只表示快递已受理拦截；拦截失败则回填 `FAILED`：

```powershell
.\.venv\Scripts\aftersales-confirm-intercept.exe --after-sales-sn "售后单号" --result RETURNED --note "已核实退回轨迹"
.\.venv\Scripts\aftersales-confirm-intercept.exe --after-sales-sn "售后单号" --result FAILED --note "快递拦截失败"
```

配置快递 100 后先只读预览物流闸门，再写入本地状态和待办。`KUAIDI100_CARRIER_MAP` 示例为 `{"拼多多物流公司ID":"kuaidi100公司代码"}`，真实映射应以当前订单数据为准。天猫返回快递公司名称时，桌面发送器先转换成同一快递 100 公司代码，再复用已经验收过的数字 ID 企业微信群白名单；无法转换或出现同一公司映射多个群时失败关闭：

2026-08-31 已使用真实近期售后运单完成只读联调：旧系统现有快递 100 授权可正常返回轨迹；通过拼多多官方物流公司列表确认 `85` 对应圆通 `yuantong`、`131` 对应德邦 `debangwuliu`、`384` 对应极兔 `jtexpress`。授权值只允许保存在被 Git 忽略的本机 `.env`，不得写入 README、`.env.example` 或提交记录；其他快递公司映射须逐一用真实订单确认，不能猜测。

```powershell
.\.venv\Scripts\aftersales-check-intercept-logistics.exe --limit 100
.\.venv\Scripts\aftersales-check-intercept-logistics.exe --limit 100 --apply
```

只有物流闸门放行后才会产生拼多多退款待办。真实执行 `PDD_AGREE_REFUND` 前，执行器还会再次读取快递 100；如果此时已进入派件、已签收无退回记录，或者物流查询失败，待办会被冻结而不会调用拼多多。执行前必须把 `PDD_WRITE_ENABLED` 改为 `true`。写请求只发送一次，网络结果不明时不会自动重试，应先到平台核对，防止重复退款：

```powershell
.\.venv\Scripts\aftersales-execute-actions.exe --types PDD_AGREE_REFUND
.\.venv\Scripts\aftersales-execute-actions.exe --types PDD_AGREE_REFUND --apply
```

管理系统人工待办先预览本地待发布数量，再开启双重写开关并执行。预览不会登录或写入管理系统；真实发布会先查经办人现有列表，远端已经存在相同售后标识时只回填原待办 ID：

```powershell
.\.venv\Scripts\aftersales-execute-actions.exe --types ERP_CREATE_MANUAL_TODO
# 确认 .env 中 ERP_TODO_PUBLISH_ENABLED=true 且 ERP_WRITE_ENABLED=true 后
.\.venv\Scripts\aftersales-execute-actions.exe --types ERP_CREATE_MANUAL_TODO --apply
```

2026-09-01 已通过真实管理系统页面完成一次人工待办发布与经办人列表回查测试，确认“经办人、发起时间、具体事项”和远端待办 ID 均可正确写入。测试账号、密码和业务订单数据未写入仓库。

2026-09-01 本机部署在单笔真实发布验收后启用了管理系统待办自动发布，即本机 `.env` 同时设置 `ERP_TODO_PUBLISH_ENABLED=true` 和 `ERP_WRITE_ENABLED=true`；版本库中的 `.env.example` 仍保持安全默认值 `false`。启用后，现有 `PENDING` 待办会在后台运行器下一个周期先发布，之后每个周期继续发布符合模块 1 人工处理条件且归属业务员唯一匹配的新待办。需要紧急暂停时，将 `ERP_TODO_PUBLISH_ENABLED=false` 后重启后台运行器；如需关闭全部 ERP 写入，再同时设置 `ERP_WRITE_ENABLED=false`。本机凭据和 `.env` 备份不得提交到仓库。

2026-09-01 随后根据真实补偿退款案例上线“全额退款金额闸门”。上线期间先暂停管理系统待办自动发布，真实只读预演和回填 60 笔历史记录，识别全额退款 47 笔、部分退款 13 笔、接口失败 0 笔；连同先行核验的案例，本地共有 14 笔部分退款被排除，且遗留待发布动作归零。旧规则此前已经发布的 3 条部分退款管理系统待办保留远端审计 ID，不自动删除，由业务员人工标记为误报或完结。

2026-08-31 以1店近72小时真实售后数据完成模块1只读预演：严格按 `ONLY_REFUND` 筛出2笔、均为极兔；快递100判定1笔 `IN_TRANSIT`（退款闸门可放行）、1笔 `DELIVERED` 且没有退回记录（退款闸门冻结），物流查询失败0笔。预演未创建动作任务、未发送企微消息、未调用拼多多退款接口。

### 售后实际后台运行（模块 1 + 模块 2 + 模块 3）

后台运行器沿用历史入口名 `aftersales-run-module1` 和脚本名 `module1-worker.ps1`，但现在同时承载模块 1、模块 2 与模块 3。固定周期顺序为：

1. 按同步游标增量读取指定拼多多店铺的状态 `2/3/10` 售后；已开启的平台再独立读取天猫、淘宝、1688、京东、抖音售后记录。天猫只有在试运行开关、水位和当轮同步成功三重条件满足时进入模块 1/2/3，且不进入拼多多退款执行器；其余非拼多多平台不进入后续动作链。京东 `serviceAndRefund` 返回的纯服务单没有退款对象，明确跳过；一旦存在退款对象但金额缺失或异常，仍失败关闭；
2. 启用归属同步时，小批量只读查询 ERP 客户档案并更新本地业务员缓存；
3. `MODULE2_WORKER_ENABLED=true` 时，按买家退货运单依次只读查询 ERP 客户名下退货单和“退货暂存列表”，将仓库已开具的退货单同步为模块 2 收货事实；型号、颜色、数量完全一致时验货通过，不一致时冻结退款并生成业务员人工待办；
4. `MODULE3_WORKER_ENABLED=true` 时，小批量生成模块 3 幂等待办；同时开启 `MODULE3_ERP_REFUND_EXECUTION_ENABLED=true` 与 `ERP_WRITE_ENABLED=true` 后，严格核验并补开未发货 ERP 退款单；
5. 对 ERP 未找到、金额/商品不一致或页面不可用的模块 3 任务保留本地异常，并按客户档案归属业务员幂等生成 `ERP_CREATE_MANUAL_TODO`；后续复用现有管理系统待办发布器；
6. 先要求“申请退款金额 = 优惠后实付金额”，再筛出“在途 + 全额仅退款”并幂等生成本地拦截通知任务；部分退款直接排除，金额缺失或异常失败关闭；
7. 对所有待发送任务执行快递 100 前置预检；已签收、退回中、已退回任务会保留审计记录但改为 `CANCELLED`，不会进入通知出口；
8. 根据 `MODULE1_NOTIFICATION_TRANSPORT` 处理通过预检的通知；默认 `disabled`，任务只保留在待发送队列；
9. 对已经成功发出拦截通知的订单再次查询快递 100，并按派件/签收/退回轨迹更新本地退款闸门；21:00–09:00 仅记录并等待，次日 09:00 重新查询后才能放行平台退款；
10. `MODULE1_ERP_REFUND_EXECUTION_ENABLED=true` 时，对“退货明细完全匹配、累计应收恰好等于负的商家应收、ERP 待处理记录完全一致”的订单补开 ERP 退款单，并再次回查收款单、退货明细和累计应收；该开关默认关闭，且仍需 `ERP_WRITE_ENABLED=true`；
11. 对派件中、已签收无退回、拦截失败、人工处理状态，以及 ERP 退货闭环中的“暂存待认领、累计应收未归零、退货明细不一致、客户档案冲突”，按归属业务员生成幂等的本地管理系统待办任务；
12. 根据 `ERP_TODO_PUBLISH_ENABLED` 处理管理系统待办；默认关闭，只有再同时开启 `ERP_WRITE_ENABLED` 才真实发布并回填远端待办 ID；
13. 分别处理模块 1 与模块 2 的拼多多退款动作。模块 1 需要 `MODULE1_PDD_REFUND_EXECUTION_ENABLED=true`，模块 2 需要 `MODULE2_PDD_REFUND_EXECUTION_ENABLED=true`；两者都必须同时满足 `PDD_WRITE_ENABLED=true` 才会真实调用平台退款。

因此，在企微机器人和桌面自动发送之间尚未确定时，后台运行器仍可持续同步订单并准备拦截任务，但会停在“待发送”，不会越过通知步骤自动退款。模块 1 的通知出口变化只替换通知步骤，不修改前后状态机。

先在前台执行一个周期核对输出。该命令会增量写入本地 MySQL 和动作队列；如果模块 3 或平台退款的外部写开关已经打开，也会按配置真实执行，因此上线前必须先检查 `.env`：

```powershell
.\.venv\Scripts\aftersales-run-module1.exe
```

确认后使用 Windows 启停脚本让它隐藏在后台持续运行：

```powershell
& .\scripts\module1-worker.ps1 -Action Start
& .\scripts\module1-worker.ps1 -Action Status
& .\scripts\module1-worker.ps1 -Action Stop
```

本机长期运行时，安装 Windows 登录自启动与 5 分钟守护。安装器会从当前运行的 MySQL 自动记录程序和配置文件路径到被 Git 忽略的 `.runtime/module1-autostart.json`，并把 MySQL 本机启动配置备份到同样被忽略的 `.runtime/mysql-defaults-backup.ini`；启动入口不保存平台 Token、管理系统账号或数据库密码，本机 MySQL 配置副本也不得提交或对外共享。守护脚本显式按 UTF-8 读取 JSON，且 Windows PowerShell 入口脚本保留 UTF-8 BOM，兼容 PowerShell 7 写入配置、Windows PowerShell 5.1 后台读取中文项目路径和脚本的场景。它优先注册 Windows 计划任务；当前进程没有计划任务权限时，自动回退到当前用户“启动”目录并运行一个隐藏的守护进程。两种方式都会在登录后检查，并每 5 分钟再次检查：MySQL 未监听时若原配置文件暂时缺失，会先从本地副本恢复到安装时的位置，再隐藏启动 MySQL；售后后台运行器未运行时调用上面的幂等启动脚本，工作台 Web 健康检查未通过时使用生产构建启动 `uvicorn`；已经运行时不会重复启动。Web 默认监听 `127.0.0.1:8000`，要求 `frontend/dist/client/index.html` 已由 `npm run build` 生成。

```powershell
& .\scripts\module1-autostart.ps1 -Action Install
& .\scripts\module1-autostart.ps1 -Action Status
# 可选：改用其他本机端口
& .\scripts\module1-autostart.ps1 -Action Install -WebPort 8080
# 不再需要时再显式卸载
& .\scripts\module1-autostart.ps1 -Action Uninstall
```

计划任务或用户启动项都使用当前 Windows 用户的交互登录令牌，因此电脑重启后至少需要登录一次；锁屏不影响同步，但休眠、关机和退出登录会停止本地运行。应在 Windows 电源设置中关闭自动休眠。若以后迁移到服务器，应先执行 `Uninstall`，避免两台机器同时处理同一售后。

默认运行拼多多 1–7 店；每店每周期最多追赶两个 30 分钟窗口。开启天猫或淘宝、1688、京东、抖音同步后，同一进程也会按平台和店铺的独立游标追赶最多两个配置时间窗口。模块 1 与模块 2 每周期分别最多处理 20 条动作任务，模块 3 默认每周期只处理 1 笔，完整周期结束后等待 60 秒。运行日志位于 `.runtime/module1-worker.log`，错误日志位于 `.runtime/module1-worker-error.log`，PID 和安全停止信号也保存在被 Git 忽略的 `.runtime/`。工作台 Web 的标准输出、错误输出和 PID 分别位于 `.runtime/workbench-web.log`、`.runtime/workbench-web-error.log`、`.runtime/workbench-web.pid`。`module1-autostart.ps1 -Action Status` 会同时显示 MySQL、后台运行器和 Web 健康状态；后台运行器的 `Stop` 会等待当前平台请求和数据库事务完成后退出，不会在请求中途强杀进程。

快递 100 不再跟随 60 秒后台周期重复查询同一运单；同一周期内多笔售后共用同一快递公司和运单号时也只查询一次。正常取得轨迹后默认 5 分钟再刷新；夜间退款闸门会把下次检查时间直接调度到次日 09:00。失败后按 5、10、20、30 分钟递增，之后保持 30 分钟上限。每次失败会保存快递 100 原始错误、连续失败次数和下次查询时间，同时保留最后一次成功轨迹；连续 6 次失败后，订单和待发送拦截任务会显示“需人工核对”。无论失败次数多少，自动退款均继续冻结。若之后恢复成功，失败次数和错误提示会自动清零。平台退款真正执行前会同时复查当前是否在 09:00–21:00，并强制进行一次不受退避时间限制的实时查询，避免使用夜间或缓存轨迹放款。

后台运行失败时按以下方式恢复：

- 单店拼多多读取失败：其他店继续完成；修复 Token 或网络后，下个周期从该店最后成功游标重试，不会跳过失败窗口；
- 单店天猫读取失败：其他天猫店与拼多多动作链继续完成；修复 SessionKey、接口权限或网络后，从 `tmall_sync_cursors` 的最后成功窗口重试；原交易不可查看但退款详情有效时会自动降级保留退款记录；
- 淘宝、1688、京东或抖音单店读取失败：其他平台、店铺和拼多多动作链继续完成；修复该店应用权限或 Token 后，从 `platform_sync_cursors` 的最后成功窗口重试。配置错误会在日志中显示平台和本地店铺代号，不输出密钥；
- 单票详情返回拼多多 `45001`“订单不属于当前店铺或订单不存在”：隔离并计入 `records_skipped`，其余订单继续处理且窗口游标正常推进；其他平台错误仍按整店失败处理；
- ERP 归属查询失败：该批记录保留失败状态并在 5 分钟后重试；该独立阶段不会阻断后续拦截安全流程；
- ERP 人工待办发布关闭：本地任务保留为 `PENDING`；开启双重写开关后下个周期继续。发布失败会使用远端售后标识先查重，再在 `ERP_TODO_MAX_ATTEMPTS` 范围内重新入队；超过次数后保留 `FAILED` 和错误原因供人工检查；
- 模块 3 ERP 核对不一致或找不到订单：不调用补单动作，原任务保留 `PENDING` 和明确错误；默认 30 分钟后复查，并按 ERP 归属业务员生成唯一管理系统待办。归属为空或冲突时不猜测经办人；待办尚未发布前异常若已解除，系统会自动取消本地待办；
- 模块 3 ERP 请求结果不明：下个复查周期先重新查询远端；若已生成退款单，只补齐本地审计，不重复调用“补开退款单”；需要紧急暂停时设置 `MODULE3_WORKER_ENABLED=false` 并安全重启运行器；
- 快递 100 返回“查询无结果”或网络异常：保留最后成功轨迹并冻结自动退款，按配置的退避间隔重试；工作台可查看原始错误、失败次数和下次查询时间。连续达到 `KUAIDI100_MANUAL_AFTER_FAILURES` 后人工核对运单号及快递官方轨迹，恢复成功后系统自动清除告警；
- 拼多多优惠后实付金额缺失：该售后不会进入拦截、人工待办或平台退款动作；先运行 `pdd-backfill-refund-amounts` 只读预演，确认接口能够返回金额后再使用 `--apply`；
- 通知出口为 `disabled`：属于预期暂停，待办保留，选择发送方式后可以继续执行；
- 快递 100 整体未配置或预检阶段异常：采用失败关闭，当前周期不发送任何通知；单票查询无结果时保留拦截通知但冻结自动退款，下个周期继续重查；
- 电脑重启：登录当前 Windows 用户后，自启动守护会依次恢复 MySQL、后台运行器和工作台 Web；打开 `http://127.0.0.1:8000/` 即可。若 Web 未恢复，先执行 `& .\scripts\module1-autostart.ps1 -Action Run`；
- 自启动守护失败：执行 `& .\scripts\module1-autostart.ps1 -Action Status`，再检查 `.runtime/module1-autostart.log`；Web 启动失败另查 `.runtime/workbench-web-error.log`。MySQL 原配置在开机阶段缺失时会自动从 `.runtime/mysql-defaults-backup.ini` 恢复并记入日志；若原配置与恢复副本都丢失，先手动启动 MySQL，再重新执行 `Install`。如果 MySQL、工作区路径或 Web 端口发生变化，也要重新执行 `Install` 刷新本机配置和恢复副本；
- 重复启动：启停脚本通过 PID 文件阻止第二个后台进程。不要绕过脚本同时启动多个 `--forever` 实例。

如需前台观察持续循环或临时覆盖店铺，可直接执行：

```powershell
.\.venv\Scripts\aftersales-run-module1.exe --forever --shops 1 2 3 4 6 7 --interval-seconds 60
```

未来若确定使用企微 Webhook，需要同时设置 `MODULE1_NOTIFICATION_TRANSPORT=qywx_webhook`、配置 Webhook 并开启 `QYWX_WRITE_ENABLED=true`。桌面发送已经接入后台循环。无论选择哪种发送方式，常规上线都应将 `MODULE1_NOTIFICATION_MIN_TASK_ID` 设置为当时“最大拦截通知任务 ID + 1”；若明确选中最新一笔作为上线验收单，可以在逐笔预览确认后将水位设为该任务 ID。后台物流预检、通知执行器和桌面预览都会应用同一水位，水位之前的历史任务继续留作查询但不会补发，也不会阻塞新任务。

2026-09-01 当前开发机开始真实运行模块 1：本机 `.env` 已启用 `MODULE1_NOTIFICATION_TRANSPORT=desktop`、`MODULE1_DESKTOP_SEND_ENABLED=true`、`MODULE1_PDD_REFUND_EXECUTION_ENABLED=true`、`PDD_WRITE_ENABLED=true`、`MODULE1_ERP_REFUND_EXECUTION_ENABLED=true` 和 `ERP_WRITE_ENABLED=true`；上线水位设为经只读预演确认的最新验收任务 `144`，因此更早的历史通知不会补发，桌面单周期批量上限仍为 `1`。版本库 `.env.example` 继续全部保持安全默认关闭。本机配置已在 `.runtime/env-before-production-automation-20260901.bak` 备份且不得提交仓库。

同日增加快递拦截客服工作时间闸门：北京时间 21:00 至次日 09:00 继续同步售后并发送拦截消息，但不执行拼多多退款；次日 09:00 强制刷新物流。只有当前不是派件/派送/投递、不是已签收，且物流查询成功时才放行；查询失败依旧冻结。本机启用与版本库默认值一致的 `Asia/Shanghai` 09:00–21:00，修改前 `.env` 已备份到 `.runtime/env-before-refund-business-hours-20260901.bak`。

同日真实验收结果：通知任务 `144` 已在正确极兔外部群发送，群机器人明确回复已登记拦截；拼多多退款任务 `173` 单次执行成功，平台随后回传退款完成，系统自动建立 ERP 匹配任务 `175` 并进入 `RETURN_WAITING_ERP_MATCH`。这证明“售后同步 → 全额仅退款筛选 → 实时物流闸门 → 企业微信拦截 → 拼多多退款 → ERP 回仓匹配任务”已经贯通。业务上的最终闭环仍需等待真实包裹回仓及仓库开具退货单，不能在验收时伪造。随后任务 `172` 启动时发现同一运单已经由业务员发群且机器人确认受理，系统清空未发草稿并按 `ManualHandled` 对账，没有重复发群；其拼多多退款任务 `178` 也已成功。

### 企业微信桌面发送准备

外部快递群不支持群机器人 Webhook，因此桌面发送使用拼多多物流公司 ID 到“完整精确群名”的本机白名单。真实群名只允许写入被 Git 忽略的 `.env`，示例仓库和日志不得输出完整订单消息。快递群消息只包含快递公司（Webhook 出口）、发货运单号和拦截要求；店铺名称、平台订单号、售后单号及内部任务编号仅保留在售后工作台中，禁止发送到快递群。2026-08-31 已在当前企业微信客户端中用键盘搜索逐一验证德邦、极兔、圆通和顺丰四个外部群均能被完整群名唯一命中；没有进入聊天、填写草稿或发送消息。拼多多官方物流公司列表对应关系为普通顺丰 `44/SF`、圆通 `85/YTO`、德邦 `131/DB`、极兔 `384/JTSD`。

先预览本地待发送任务能否全部解析到群白名单：

```powershell
.\.venv\Scripts\aftersales-preview-desktop-notices.exe --limit 20
```

该命令只读数据库，只输出脱敏后的售后单号、订单号和运单号，不激活企业微信、不填写草稿、不发送消息。输出中的 `notification_min_task_id` 是当前上线水位；预览不会读取水位以前的历史任务。`blocked_missing_group` 必须为 `0` 才能进入桌面自动化。

桌面预览和通用外部动作执行器都会再次校验任务中的物流预检凭证。`blocked_preflight` 与 `blocked_missing_group` 必须同时为 `0`；缺少预检时间、物流状态不允许发送或退款闸门标记不一致时一律失败关闭，不能通过手工执行旧命令绕过物流预检。

桌面文字发送器只在物流预检通过且确有待拦截任务时控制桌面；空闲周期不会激活企业微信或发送任何键盘输入。通知以“标准化快递公司代码 + 标准化发货运单号”为唯一投递组：同一合并包裹即使关联多个平台订单或售后单，也只向快递群发送一次；开始输入前会把该运单全部待通知任务一起声明为运行中，确认发送后一起转为成功并推进到 `INTERCEPT_PUSHED`。以后若同一运单新增售后任务，发送器会复用已有成功投递记录直接完成新任务，不再重复发群。开始发送时，程序按允许的 `WXWork.exe` 进程精确枚举可见窗口，拒绝安全验证窗口和多个同尺寸主窗口，并仅在尚未输入消息的2秒激活阶段有限重试，将唯一最大的企业微信主窗口切到前台；随后使用 Ctrl+1 回消息页、Ctrl+F 搜索完整群名、检测搜索画面变化、进入群聊、再次检查前台进程与验证窗口、输入消息、记录账本、按 Enter 发送并检测聊天画面变化。开始输入后、按下发送键之前不会再争抢焦点，任何前台切换都会失败关闭；按下发送键后以聊天区域画面变化作为最终确认点，确认后即使微信或通知弹窗再抢走前台，也不会把已经发出的消息误判为结果不明。确认发送成功后会尝试恢复操作者发送前使用的窗口；恢复失败不影响已经确认的发送结果。若发送结果不明，则保留企业微信在前台供人工核验，不自动切回或盲目重发。文字写入检测仅截取中间聊天输入框，避免两侧固定栏把短消息的画面变化稀释；发送后检测覆盖输入框和消息区，用于确认输入框清空及新消息气泡出现。底层 `SendInput` 使用 Windows SDK 完整 `INPUT` 联合体尺寸，兼容 32/64 位 Windows；系统拒绝或未完整接收任一组按键时立即在输入消息前暂停。任意阶段按 ESC 都停止；安全验证、登录验证、发送前的前台切换或画面未变化都会失败关闭。

只读检查使用新发送命令也不会碰企业微信：

```powershell
.\.venv\Scripts\aftersales-send-desktop-notices.exe --limit 1
```

首次真实验收必须先向操作者展示消息内容并取得发送确认，再临时设置 `MODULE1_DESKTOP_SEND_ENABLED=true`，且一次只发一笔：

```powershell
.\.venv\Scripts\aftersales-send-desktop-notices.exe --limit 1 --apply
```

本机账本按 `PasteStarted`、`SendPressed`、`Sent` 逐步追加并同步落盘，不保存群名、平台订单号、售后单号、运单号或完整消息。`Sent` 可用于本地状态对账，避免已经发出但数据库回写失败时重复发送。后台运行器与人工命令还会共同竞争 `MODULE1_DESKTOP_LOCK_PATH` 的非阻塞单实例锁；已有进程控制企业微信时，另一进程立即停止，不排队、不抢焦点。失败发生在输入消息之前时写入 `PausedBeforePaste`，人工确认确实没有输入后才可恢复：

```powershell
.\.venv\Scripts\aftersales-send-desktop-notices.exe --resume-before-paste 任务ID --limit 1 --apply
```

如果账本停在 `PasteStarted` 或 `SendPressed`，必须先回到同一快递群人工核验，程序拒绝恢复和盲目重发。完成单笔真实验收后，才允许同时设置 `MODULE1_NOTIFICATION_TRANSPORT=desktop`、`MODULE1_DESKTOP_SEND_ENABLED=true` 并重启后台运行器；后台每个完整周期只处理 `MODULE1_DESKTOP_BATCH_LIMIT` 条，当前安全默认值为 1。任一桌面任务暂停或结果不明时，本周期立即失败停止发送，后续周期只读取账本并继续失败关闭，不会再次按键。

若人工核验后明确看到消息已经出现在正确群聊中，可用以下命令将 `PausedBeforePaste`、`PasteStarted` 或 `SendPressed` 任务记为 `Sent` 并回写本地任务状态；这也覆盖“自动发送在输入前暂停、随后由操作员在正确群手工发送”的恢复场景。该命令不会再次控制企业微信，也不会再次发送消息。没有看到已发消息时禁止执行：

```powershell
.\.venv\Scripts\aftersales-send-desktop-notices.exe --confirm-sent 任务ID --apply
```

若自动草稿尚未发送，但同一运单已经由业务员手工发群且快递方/群机器人明确确认受理，应先在企业微信清空自动草稿，再执行以下命令记录 `ManualHandled`。该状态同样满足“先通知再退款”的前置条件，但审计上不会冒充自动发送；没有同时看到运单号和受理确认时禁止使用：

```powershell
.\.venv\Scripts\aftersales-send-desktop-notices.exe --confirm-manual-handled 任务ID --apply
```

企业微信截图必须使用物理像素坐标。桌面发送器启动时会声明 Per-Monitor V2 DPI 感知，避免 Windows 200% 等缩放比例下 `GetWindowRect` 与屏幕截图坐标不一致，导致底部输入框落在变化检测区域之外。

### 拦截退回后的 ERP 闭环匹配

模块 1 的最终完成条件不是“平台退款成功”，而是拦截包裹退回仓库后，ERP 客户档案中已经存在对应退货单且账务平衡。后台只读匹配器按以下顺序核对：

1. 用平台订单号从 ERP 客户自动补全接口唯一确定客户档案和归属业务员；
2. 用原发货运单号查询该客户的“发货销售单”，只接受编号以 `TH-` 开头的退货单；
3. 逐项比较退货单与售后申请的型号、颜色和数量；
4. 读取客户档案“累计应收”，绝对值不超过 `ERP_RETURN_MATCH_RECEIVABLE_TOLERANCE` 才视为归零；
5. 两项同时满足后，将 `ERP_MATCH_RETURN_ORDER` 标记完成，售后进入 `INTERCEPT_SUCCESS`（页面显示“售后已闭环”）。

退货单金额不与拼多多买家退款金额强制相等：平台优惠券可能导致买家退款额小于商家销售实收，ERP 退货价格也可能按历史销售价计算。因此闭环以“运单号 + 型号 + 颜色 + 数量 + 客户累计应收归零”为准。找不到退货单时继续等待，不发送业务员待办；ERP 页面暂时不可用时由系统重试，也不把系统故障误派给业务员。退货单仍在“退货暂存列表”、已经开到客户名下但累计应收未归零、退货明细不一致或客户档案冲突时，系统会保留待匹配并向唯一归属业务员发布一次幂等人工待办。业务员可见事项只保留店铺、平台订单号、异常原因和处理要求；售后单号、运单号、ERP 退货单号以及退货明细继续保存在本地任务结构化载荷中供查重与审计，不再堆入远端事项文字。归属为空或冲突时不猜测经办人，也不会误判闭环。

合并发货时允许“同运单多平台订单合计匹配”：只纳入平台已明确退款且模块 1 正在等待 ERP 的仅退款订单；要求每笔订单都已唯一匹配到同一客户档案、都有售后 SKU 明细，并且合计后的型号、颜色、数量与 ERP 同一退货单逐项完全相等。同运单任一任务到达复查时间时，会一起核对该运单下仍在等待的全部任务，避免逐笔处理造成遗漏。匹配成功后，动作任务会记录 `erp_match_mode=combined_tracking_orders` 及参与匹配的售后单号，便于审计。ERP 多退数量、多出无法归属的型号、客户冲突或累计应收未归零时仍保持待人工核对。

ERP 补开退款单使用独立双重写开关，默认只允许手工运行只读预演。预演必须同时满足：平台已明确全额退款、商家应收金额已从拼多多订单口径取得、ERP 退货单型号/颜色/数量完全一致、客户累计应收恰好为负的商家应收、待处理页售后单号/金额/客户/ERP 订单一致、发货销售单确实存在原订单。数量多退、金额混合了其他订单、商家应收缺失、退货暂存、未开退货单或 ERP 页面异常都会失败关闭。真实补单后还必须回查待处理记录消失、唯一退款收款单生成、退货明细仍一致且累计应收归零，才将售后标记闭环：

```powershell
# 默认只读；当前异常单会返回 0 个可执行候选，不会写 ERP
.\.venv\Scripts\aftersales-execute-module1-erp-refunds.exe --details

# 单笔验收通过后，先在 .env 开启 MODULE1_ERP_REFUND_EXECUTION_ENABLED=true
# 并确认 ERP_WRITE_ENABLED=true，再仅对指定订单真实执行
.\.venv\Scripts\aftersales-execute-module1-erp-refunds.exe --platform-order-sn "平台订单号" --details --apply
```

后台自动执行同样受这两个开关保护。开关关闭时只继续现有退货匹配和业务员待办，不会点击 ERP；请求结果不明时不盲目重发，后续先从已处理退款、退款收款单和累计应收恢复事实。

指定历史订单进行真实 ERP 只读预演，不会更新本地状态：

```powershell
.\.venv\Scripts\aftersales-sync-erp-returns.exe --platform-order-sn 260823-686827845971918
```

历史订单预演确认闭环后，可补记本地工作台状态；程序会再次要求平台退款已完成且 ERP 核对结果为闭环，并取消同一售后遗留的待发送拦截、待退款或人工待办，避免历史动作再次执行：

```powershell
.\.venv\Scripts\aftersales-sync-erp-returns.exe --platform-order-sn 260823-686827845971918 --apply
```

预览当前所有待匹配任务：

```powershell
.\.venv\Scripts\aftersales-sync-erp-returns.exe --force --limit 20
```

确认结果后，单次写回本地工作台状态：

```powershell
.\.venv\Scripts\aftersales-sync-erp-returns.exe --force --limit 20 --apply
```

持续运行时设置 `ERP_RETURN_MATCH_SYNC_ENABLED=true`。平台退款明确完成后，即使物流仍显示在途、派件或快递 100 暂时查询失败，后台也会先建立 ERP 只读匹配任务。后台运行器仍每 60 秒执行一个周期，但会读取任务中的上次核对时间，同一待匹配售后默认每 `ERP_RETURN_MATCH_REFRESH_SECONDS=1800`（30 分钟）才访问一次 ERP；服务器不可用时任务保持待匹配，下个间隔自动重试。该功能仅访问客户档案、发货销售单和退货暂存列表，不自动点击暂存单“认领”，也不要求打开 `ERP_WRITE_ENABLED`。需要恢复时修复网页登录凭据或 ERP 页面后等待下次周期，也可先用上述 `--force` 命令只读复查。

## 模块 2：仓库扫码收货与验货

模块 2 处理拼多多 `RETURN_AND_REFUND`（退货退款）包裹，仓库页面入口为左侧“仓库验货”。仓库实际收货以 ERP 已开具的退货单为准：系统先查客户名下退货单，客户名下未找到时再查“退货暂存列表”；手工扫码页面作为补录和核对入口。自动化只在 ERP 实收入库明细与平台申请完全一致时接管退款：

1. 使用买家退货运单号反查拼多多退货退款售后、店铺、客户、业务员和平台申请明细；拼多多历史数据若将规格保存为 `型号#颜色`，核对前会拆成独立型号与颜色，避免把一致商品误判为异常；
2. 后台先在 ERP 客户名下查询同运单退货单；未找到时继续查询“退货暂存列表”，不能仅因客户名下为空就判断仓库未收货；
3. ERP 退货单号作为本地唯一 `receipt_sn`，型号、颜色和入库数量作为仓库实收明细；同一运单关联多笔售后时暂停自动处理，不能猜测；平台已经明确退款成功（包括极速退款）的售后仍继续退款后追货，等待客户退货运单及仓库收货，但不会再次退款；
4. ERP 实收型号、颜色、数量与平台申请完全一致时自动登记验货通过；退货单仍在暂存列表也可以通过，不要求先认领到客户名下；
5. 型号、颜色、数量任一不一致时登记“验货异常”，并按 ERP 归属业务员幂等生成 `ERP_CREATE_MANUAL_TODO`；未退款订单冻结平台退款，已退款订单明确标记退款后少退、错退或未收到并转人工追责；待办会列出平台申请、ERP 实收、退货单号和具体缺少/多出明细；
6. 次品、报废或手工补录异常仍必须由仓库登记原因；验货结论一经提交不可覆盖，只允许同内容幂等重试；
7. 验货通过后，后台再次核对售后类型、仓库记录、平台售后状态和上线水位，满足条件才生成独立的 `PDD_AGREE_RETURN_REFUND` 动作；
8. 退款动作使用售后单级幂等键，执行前再次核对仓库验货事实；当轮拼多多同步必须完整成功，否则入队和执行均失败关闭；平台已经退款时只补齐本地审计，不再次调用退款接口；状态不明或不属于可退款状态时失败关闭，保留错误供人工处理。

退款后追货是独立于退款动作的持续核对链路：缺少买家退货运单时显示“退款后待退货运单”，已有运单但 ERP 客户退货单和退货暂存列表都未找到时显示“退款后待仓库收货”；ERP 收货后，实收型号、颜色、数量完全一致则显示“退款后验收一致”，少退、错退或数量不符则转人工处理。该链路覆盖普通退款成功和极速退款，不会生成第二次退款任务。

数据库升级：

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

接口：

- `POST /api/v1/warehouse/scan`：按买家退货运单号只读反查；
- `GET /api/v1/warehouse/returns`：按暂存位置、验货状态和关键词查询最近收货单；
- `POST /api/v1/warehouse/returns`：登记拆包实收明细，`receipt_sn` 与退货运单均唯一；
- `POST /api/v1/warehouse/returns/{receipt_sn}/assign-customer`：把暂存包裹认领到客户；
- `POST /api/v1/warehouse/returns/{receipt_sn}/inspection`：提交通过或异常验货结论。

防重规则：同一 `receipt_sn` 相同内容重复提交返回原记录并标记 `duplicate=true`；同一单号内容不同或同一退货运单被另一收货单使用时返回 HTTP 409。验货异常缺少原因、通过时明细不一致或存在次品/报废时返回 HTTP 422。所有失败都在事务内回滚，不会留下半张收货单。

模块 2 后台自动退款使用独立开关，不与模块 1 的已发货仅退款动作混用。版本库默认全部关闭；首次上线应将 `MODULE2_REFUND_MIN_RETURN_ID` 设为当前允许处理的最小仓库收货记录 ID，防止历史验货记录被批量补发：

```dotenv
MODULE2_WORKER_ENABLED=true
MODULE2_PDD_REFUND_EXECUTION_ENABLED=true
MODULE2_REFUND_MIN_RETURN_ID=1
MODULE2_ERP_INTAKE_MIN_ORDER_ID=3303
PDD_WRITE_ENABLED=true
```

只读预览或人工核对模块 2 的独立退款队列：

```powershell
.\.venv\Scripts\aftersales-execute-actions.exe --types PDD_AGREE_RETURN_REFUND
```

`--apply` 会执行真实平台退款，应只在仓库验货和平台状态核对通过后使用。常驻后台不需要手工运行该命令；开关启用后会在每个周期依次执行“ERP退货单核对 → 验货异常转人工/验货通过退款入队 → 平台退款”。紧急暂停时设置 `MODULE2_WORKER_ENABLED=false` 并安全重启后台运行器；已经成功退款的订单不会回滚。

ERP 页面不可用、暂存单只有运单号但解析不到完整明细、退货单号冲突或同一运单关联多笔售后时均失败关闭，不生成退款。`MODULE2_ERP_INTAKE_MIN_ORDER_ID` 是 ERP 自动接入的独立上线水位，应在首次启用时设置为当前 `aftersales_orders.id` 最大值加 1，避免历史退货单批量触发；历史个案经人工核对后单独处理。人工待办发布继续受 `ERP_TODO_PUBLISH_ENABLED=true` 与 `ERP_WRITE_ENABLED=true` 双重开关保护；模块 2 远端待办使用业务员可识别的平台订单号作为回查标识，本地仍用售后单号组成的 `idempotency_key` 防止重复建任务。

2026-09-03 本机在确认仓库不存在已验货通过的历史积压后启用模块 2 自动退款，上线水位为仓库收货记录 ID `1`。当时待处理记录仍处于待验货，不会在启用时提前生成退款任务；只有后续人工验货通过才进入退款队列。版本库 `.env.example` 继续保持安全默认关闭，本机 `.env` 及备份不得提交仓库。

## 模块 3：未发货退款与锁包

模块 3 处理的是“拼多多已经极速退款后，ERP 如何停止履约并完成平账”，不再尝试调用 `pdd.refund.agree`。它只扫描 `PENDING_CHECK`、售后类型为 `ONLY_REFUND`、平台发货状态为 `UNSHIPPED` 或 `PACKED_NOT_SHIPPED`，并且平台退款状态已经明确成功的售后单。拼多多的“未发货”不足以证明 ERP 尚未出包，因此仍须检查 ERP：

- `UNSHIPPED`：生成唯一的 `ERP_CHECK_FULFILLMENT` 待办。网页适配器只在 ERP 待处理页明确显示“有订单编号但未开退款单”时继续，并同时核对售后单号、商家应收、ERP 欠货型号/颜色/数量、客户累计应收和发货销售单；通过后使用 ERP 原有的“补开退款单”动作一次完成取消欠货与退款收款单；
- `PACKED_NOT_SHIPPED`：生成唯一的 `ERP_LOCK_PACKING` 待办；锁包成功后生成 ERP 退款流水；
- 在途和已签收订单不属于模块 3，本命令不会处理；
- `PACKED_NOT_SHIPPED` 和任何核对不一致的记录仍使用动作结果回填命令处理；自动化不会越级补单。

先执行迁移并进行只读预览：

```powershell
alembic upgrade head
.\.venv\Scripts\aftersales-process-module3.exe
```

预览结果确认后，写入本地动作队列：

```powershell
.\.venv\Scripts\aftersales-process-module3.exe --apply
.\.venv\Scripts\aftersales-process-module3.exe --shops pdd-shop-01 pdd-shop-02 --limit 500 --apply
.\.venv\Scripts\aftersales-process-module3.exe --platform-order-sn "平台订单号" --apply
```

命令输入来自 `aftersales_orders`，输出为不含订单号的汇总 JSON。动作使用唯一幂等键，已有动作不会重复创建。数据库异常时本批次回滚；修复后可直接重跑。

未发货补单必须先只读预演。`refund_amount` 采用 ERP 商家口径，必须等于本地 `merchant_receivable_amount`，不使用买家优惠后实付额替代：

```powershell
.\.venv\Scripts\aftersales-execute-module3-erp-refunds.exe --platform-order-sn "平台订单号" --details
```

预演必须是 `ready=1` 且 `blocked=0` / `unavailable=0`。确认后才在本机 `.env` 中同时设置 `MODULE3_ERP_REFUND_EXECUTION_ENABLED=true` 和 `ERP_WRITE_ENABLED=true`，再执行单笔真实补单：

```powershell
.\.venv\Scripts\aftersales-execute-module3-erp-refunds.exe --platform-order-sn "平台订单号" --details --apply
```

写入后程序必须同时回查：待处理记录已消失、ERP 收款记录存在唯一的负数退款单据、状态表原订单无欠货、客户累计应收归零。回查通过后才将本地订单转为 `UNSHIPPED_AUTO_REFUNDED`，并把 `ERP_CHECK_FULFILLMENT`、`ERP_CANCEL_UNSHIPPED_ORDER`、`ERP_CREATE_REFUND_RECORD` 三段审计动作记录为成功。请求结果不明时不得立即重发；先重跑只读预演，若返回 `completed` 只补记本地结果，不会再次调用 ERP。

单笔真实验收完成后，可接入现有常驻周期。首次上线保持每轮 1 笔：

```dotenv
MODULE3_WORKER_ENABLED=true
MODULE3_WORKER_BATCH_LIMIT=1
MODULE3_ERP_REFUND_RECHECK_SECONDS=1800
MODULE3_ERP_REFUND_EXECUTION_ENABLED=true
ERP_WRITE_ENABLED=true
```

后台先创建本地动作，再进行 ERP 核验和补单。`not_found`、`blocked`、`unavailable` 均不会越级写入 ERP；任务保留待处理，并遵守复查间隔。异常自动复用管理系统人工待办链路，无需额外企微机器人：

```dotenv
ERP_TODO_PUBLISH_ENABLED=true
ERP_WRITE_ENABLED=true
```

待办使用订单已缓存的 ERP 归属业务员作为经办人，内容包含模块 3 幂等标识、店铺、平台订单号、售后单号、ERP 订单号和标准化异常原因。发布前后会按标识查重；失败在 `ERP_TODO_MAX_ATTEMPTS` 范围内安全重试。异常解除且待办尚未发布时自动取消；已经发布的远端待办保留审计，由业务员确认完结。紧急暂停只需将 `MODULE3_WORKER_ENABLED=false`，再执行 `module1-worker.ps1 Stop` / `Start` 安全重启；已成功补开的退款单不会回滚。

2026-09-01 已使用一笔真实拼多多未发货仅退款完成单笔验收：平台退款与 ERP 商家应收一致，补单后负数退款收款单成功生成，状态表欠货移除、累计应收归零，本地工作流同步闭环。真实订单、客户和登录凭据不写入仓库。

使用下面的命令查看待办 ID，并根据 ERP 的真实操作结果逐步回填：

```powershell
.\.venv\Scripts\aftersales-list-actions.exe --status PENDING

# ERP_CHECK_FULFILLMENT：ERP 确认尚未打包
.\.venv\Scripts\aftersales-confirm-action.exe --task-id 123 --success --result-code NOT_PACKED

# ERP_CANCEL_UNSHIPPED_ORDER：ERP 已完成取消排单
.\.venv\Scripts\aftersales-confirm-action.exe --task-id 124 --success --result-code COMPLETED

# ERP_CHECK_FULFILLMENT：发现已经出包，系统转为锁包
.\.venv\Scripts\aftersales-confirm-action.exe --task-id 125 --success --result-code PACKED_NOT_SHIPPED

# ERP_LOCK_PACKING：打包台已锁单
.\.venv\Scripts\aftersales-confirm-action.exe --task-id 126 --success --result-code COMPLETED
```

平台在进入模块 3 前已经完成退款，所以 ERP 取消排单或锁包成功后直接生成 `ERP_CREATE_REFUND_RECORD`。确认财务流水完成后，订单才进入 `UNSHIPPED_AUTO_REFUNDED`：

```powershell
.\.venv\Scripts\aftersales-confirm-action.exe --task-id 127 --success --result-code COMPLETED --reference-sn "ERP退款流水号"
```

回填失败使用 `--failed --message "失败原因"`，系统记录错误并停止后续流转。模块 3 网页适配器不修改旧管理系统源码，只调用其现有的查询与“补开退款单”动作；其他 ERP 动作仍按人工回填流程执行。

## 模块 4：售后归因

模块 4 复用各平台已经同步入库的售后事实，不新增另一个爬取任务。每条售后进入同步窗口时，同步器自动读取平台、店铺、SKU、商品名、退款件数、平台原始原因、买家留言、申请金额、平台售后状态及平台申请/更新时间，并在同一数据库事务中幂等保存。`20260902_0013` 迁移会对已有历史售后的 `reason_category` 自动回填；`20260902_0016` 新增标准化退款财务状态、实际退款金额和退款成功时间。历史记录没有平台申请时间时，看板明确回退为首次入库时间。

工作台左侧“售后归因”页面提供：

- 按“平台 → 店铺”两级联动、月度/年度/自定义周期、型号/SKU 和原因大类筛选；
- 默认展示“实际退款成功金额”，并拆分为总退款、仅退款、退货退款；“申请退款金额”作为单独辅助口径保留，不与实际成功金额混算；
- 月度按天展示趋势；年度固定展示十二个月，并在金额卡显示可比环比/同比、在年度月份下显示月间环比和上年同比；自定义区间不超过 63 天时按天、超过时按月；
- 明确展示当前筛选范围的数据起止日期、平台退款状态识别覆盖率和环比/同比历史是否覆盖；基期为 0 或本地无历史时显示“暂无可比”，不将 0 伪装成增长率；
- 退款申请单、退款件数、涉及型号、质量类占比及主要原因；
- 将 `6050-中孔#青古铜` 等 SKU 稳定归并为 `6050` 型号，按退款申请单量排名；
- 点击型号后同屏下钻该型号的原因构成、平台原始原因和 SKU 变体；
- 原因分类为确定性规则，质量、物流、规格等明确信号优先于“其他原因”，便于复核和调整词典。

当前本地数据库只有售后事实，没有“同期全部已售订单”分母，因此页面会显示退款申请量和申请单占比，但不将其冒充为真实退款率；“退款率”列在销量分母接入前固定标记为“待接销量”。后续接入平台订单或 ERP 销售明细后，才能按“同店铺 + 同型号 + 同期间”计算 `退款申请订单数 / 已售订单数`。

退款金额按售后单去重，同一售后包含多个 SKU 时只计一次；换货不进入总退款、仅退款或退货退款金额。`refund_amount` 始终表示买家“申请退款金额”，只有平台明确返回退款成功时才将金额写入 `actual_refund_amount` 并计入默认指标。当前明确成功口径为：拼多多售后状态 `10` 或订单退款状态 `4`；天猫/淘宝退款状态 `SUCCESS`；1688 状态 `refundsuccess`；京东、抖音只接受接口返回的明确成功文字，不猜测未知数字状态。无法识别的历史状态保持 `UNKNOWN`，不计入实际退款成功金额，并由页面覆盖提示披露。

申请金额按平台申请时间归期，实际成功金额按退款成功时间归期。平台接口没有独立到账时刻时，以该笔售后的平台最后更新时间作为成功时间；该时间属于可审计的近似口径，页面和接口都会提示。迁移只对已有拼多多和 1688 明确成功记录回填；旧天猫记录此前未保存退款状态，因此保持未知，后续增量同步取得明确状态后自动补齐。上线升级顺序为先执行 `& .\scripts\module1-worker.ps1 -Action Stop`，再执行 `.\.venv\Scripts\alembic.exe upgrade head`，最后重新 `Start`；如需回退到 `20260902_0015`，必须同时回退依赖新字段的代码，不能只删除数据库字段。

自动化边界与恢复方式和七店售后增量同步一致：单店、单窗口内的映射或入库失败会回滚该窗口，不推进 `pdd_sync_cursors`；修复平台数据、网络或数据库后重新运行 `pdd-sync-refunds`，系统会从最后成功游标向前重叠 5 分钟幂等续传。拼多多增量列表的 `created_time`、`updated_time` 同时兼容 Unix 秒时间戳和平台返回的 `YYYY-MM-DD HH:MM:SS` / ISO 日期文字；无法识别的时间仍会失败关闭并保留游标，防止静默漏单。原因分类只影响看板归因，不参与拦截、退款或 ERP 动作判定。

## 售后订单记录中心

当前项目已包含独立的 React 网页工作台，并通过只读 API 查询本地 MySQL 中的真实售后订单、店铺、SKU 和自动化任务记录。页面提供：

- 售后订单按“工作台待处理 / 仅记录 / 全部售后”分层；默认进入工作台待处理，只展示仍需跟进、存在失败或待执行任务、或符合模块 1 候选条件的订单，“仅记录”保留部分退款补偿及已经闭环的历史售后，任何视图都不会删除原始记录；
- 今日新增、待拦截、待人工、已完成汇总；
- 按店铺、归属业务员、售后类型、处理状态、物流状态、申请时间和单号检索；
- 列表、详情和 CSV 同时显示申请退款金额、优惠后实付金额及“全额退款/部分退款补偿/待核实”范围；
- 左侧“在途拦截”独立页面只纳入全额退款：包括在途仅退款候选单、已经生成拦截通知任务的订单以及后续退回/退款/ERP 匹配状态；部分退款即使存在旧版遗留任务也只保留在“全部售后”及订单审计时间线中，不进入拦截页面和待拦截汇总；
- 在途拦截页集中显示归属业务员、目标快递群、运单、通知状态、物流轨迹、退款闸门和当前处理环节，并支持待发拦截、退款冻结、已退款待退回、待 ERP 匹配等筛选；
- 左侧“运行监控”页面每 15 秒只读刷新模块 1/2/3 常驻进程、最近完整周期、各处理阶段和当前启用范围内的企微拦截任务队列；页面不会触发退款、发送消息或控制企业微信，异常时仍按对应后台运行器恢复流程处理；
- 售后归因页自动汇总各平台店铺退款原因，支持平台与店铺联动筛选、型号排名、原因构成及 SKU 变体下钻；
- 15/30/50 条分页、当前页 CSV 导出和手动刷新；
- 选中订单后查看基础信息、当前处理决策、物流状态和自动化审计时间线；
- 1280 像素及以下把详情栏改为覆盖层，避免压缩订单表。

开发时分别启动 FastAPI 和 Vite：

```powershell
# 终端 1：项目根目录
.\.venv\Scripts\uvicorn.exe aftersales_workbench.main:app --host 127.0.0.1 --port 8000

# 终端 2：frontend 目录
cd frontend
npm install --prefer-offline --no-audit --no-fund
npm run dev -- --host 127.0.0.1 --port 4173 --strictPort
```

浏览器打开 `http://127.0.0.1:4173/`。Vite 会把 `/api` 和 `/health` 代理到本机 8000 端口。

需要由 FastAPI 单端口提供页面时，先构建前端，再启动 API：

```powershell
cd frontend
npm run build
cd ..
.\.venv\Scripts\uvicorn.exe aftersales_workbench.main:app --host 127.0.0.1 --port 8000
```

构建产物存在时，FastAPI 会在根路径挂载 `frontend/dist/client`，浏览器打开 `http://127.0.0.1:8000/`。售后记录只读接口为：

- `GET /api/v1/aftersales/orders`：已同步售后退款记录的汇总、筛选、分页、店铺与归属业务员选项；支持 `record_view=WORKBENCH|RECORD_ONLY|ALL`，默认 `WORKBENCH`，分别对应工作台待处理、仅记录和全部售后；
- `GET /api/v1/aftersales/intercepts`：仅限全额退款的模块 1 在途拦截汇总、阶段筛选、快递群与退款闸门状态；
- `GET /api/v1/aftersales/manual-todos`：模块 1/3 人工待办的发送审计、业务员与原因筛选、完整事项、远端待办 ID、失败或取消原因；
- `GET /api/v1/aftersales/orders/{after_sales_sn}`：订单详情、SKU、物流判断和动作时间线。
- `GET /api/v1/monitor/status`：只读返回常驻运行器 PID/周期新鲜度、模块 1/2/3 阶段结果、企微拦截任务状态汇总和关键执行开关；不执行任何外部动作。
- `GET /api/v1/attribution/overview`：模块 4 售后归因摘要、实际/申请退款金额、趋势、环比同比、数据覆盖、原因构成、型号排名、店铺分布和选中型号下钻；支持 `platform`、`shop_id`、`period_mode=MONTH|YEAR|CUSTOM`、`started_on`、`ended_on`、`model_keyword`、`reason_category` 和 `focus_model`。
- `GET /api/v1/scrap/overview`：模块 5 退货数量、报废数量、报废率、已核定损失、全部退货型号（含零报废型号）、原因/颜色分布、趋势和型号明细；支持日期、型号、原因、责任、数据状态和焦点型号筛选。
- `PATCH /api/v1/scrap/records/{source_row_id}/decision`：补录报废原因、责任归属、确认单位成本、损失金额、成本来源和复核人；仅写工作台核定层，不回写 ERP。

售后订单、在途拦截、人工待办和运行监控页面都只读，不会直接调用拼多多退款、企微发送或 ERP 写接口。在途拦截页没有“发送”或“退款”按钮，真实外部动作仍只能由后台运行器在对应总开关打开后执行。运行监控显示“未运行”或“周期长时间未完成”时，先用 `& .\scripts\module1-worker.ps1 -Action Status` 核对，再按 `Stop` / `Start` 安全恢复；它不会从网页强制重启进程。仓库验货页面会写入本地 `warehouse_return_*` 表和对应售后状态，但不会触发任何外部写操作。数据库暂未保存买家昵称时，详情明确显示“平台未返回”，不会虚构客户信息。

## 模块 5：ERP 退货报废

模块 5 读取 ERP `b4refund/v2` 退货单页面，按天同步全部退货行，并以“颜色字段是否以 `报废` 开头”作为唯一自动识别规则，例如 `报废铜拉丝` 会保存原值，同时标准化为颜色 `铜拉丝`。同步只使用 ERP 行 ID、状态、退货单号、完成日期、经办人、型号、颜色、数量和原始单价；寄件人、电话、运单号等非必要个人信息不进入工作台数据库。

ERP 原始退货行保存在 `erp_return_rows`，人工核定保存在 `erp_return_scrap_decisions`，两层通过 ERP 行 ID 幂等关联。原始 ERP 单价只用于追溯，不参与损失统计。数据状态按以下规则计算：

- 未填写报废原因为“待补原因”；
- 已填写原因，但缺少核定损失或复核人为“待核成本”；
- 原因、损失和复核人齐全才是“已确认”，只有这部分进入“已核定损失”。

首次接入必须先升级数据库，再按“试跑—确认—写入”执行：

```powershell
.\.venv\Scripts\alembic.exe upgrade head

# 只访问 ERP 并输出数量，不写本地数据库
.\.venv\Scripts\aftersales-sync-erp-scrap.exe --days 90

# 试跑结果合理后，回填近 90 天到本地工作台
.\.venv\Scripts\aftersales-sync-erp-scrap.exe --days 90 --apply
```

持续运行设置 `ERP_SCRAP_SYNC_ENABLED=true`。常驻后台仍每 60 秒进入一次完整周期，但同步状态会把模块 5 限制为每 `ERP_SCRAP_SYNC_REFRESH_SECONDS` 秒执行一次；每次读取今天、昨天和一个轮换历史日，在控制 ERP 页面负载的同时持续复核近 `ERP_SCRAP_SYNC_LOOKBACK_DAYS=90` 天。某天 ERP 行被撤销时，本地只标为非活动，不删除已有人工核定记录。ERP 页面或登录不可用时该阶段失败且不提交半批数据，修复凭据后等待下一周期即可恢复；也可以再次运行上述只读命令诊断。

报废率的分母是同期 ERP 全部退货数量，分子是识别出的报废数量。原因、责任和数据状态筛选只影响报废分子；界面会明确保留待补原因记录，避免只看“已确认”而漏掉真实报废。模块 5 不需要 `ERP_WRITE_ENABLED`，也不会点击 ERP 页面按钮或写回退货单。

## 健康检查

- `GET /health/live`：进程存活，不访问外部依赖。
- `GET /health/ready`：执行 `SELECT 1` 验证数据库；失败时返回 HTTP 503。

## 验证

```powershell
ruff check .
pytest
```
