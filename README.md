# 利德仕电商自动化售后工作台

面向利德仕多平台、多店铺的售后中台。当前仓库已完成 Phase 1 工程骨架、全量数据库初始化、拼多多七店同步，以及模块 1、模块 3 的安全动作流转。

## 当前边界

- 已实现：配置加载、MySQL 连接池、Alembic 迁移、健康检查、Docker Compose、本地拼多多联调、七店售后增量同步、模块 1 在途拦截队列与快递 100 退款闸门、模块 1 常驻后台运行器、模块 3 未发货/已出包判定队列、企微机器人通知和受写开关保护的拼多多同意退款动作。
- 已建立全局业务表及内部同步表：`shops`、`aftersales_orders`、`aftersales_items`、`return_scrap_records`、`negative_reviews`、`pdd_sync_cursors`、`aftersales_action_tasks`。
- 暂未直连：真实 ERP API。模块 3 先使用人工回填 CLI 完成 ERP 动作确认，待 ERP 接口规则明确后替换为适配器。模块 2 仓库退货流程按当前决定延后，不在本阶段实现。
- 所有外部写入默认关闭。只有分别配置并打开 `QYWX_WRITE_ENABLED`、`PDD_WRITE_ENABLED` 后，执行命令才会真正发送企微通知或同意退款。

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
| `QYWX_INTERCEPT_WEBHOOK_URL` | 模块 1 快递拦截群机器人 Webhook（密钥） | 无 |
| `QYWX_TIMEOUT_SECONDS` | 企微请求超时秒数 | `10` |
| `QYWX_WRITE_ENABLED` | 企微机器人发送开关 | `false` |
| `MODULE1_WORKER_SHOP_NUMBERS` | 模块 1 后台运行店铺序号 JSON 数组；5 店 Token 失效期间默认排除 | `[1,2,3,4,6,7]` |
| `MODULE1_WORKER_INTERVAL_SECONDS` | 后台运行器每个完整周期结束后的等待秒数 | `60` |
| `MODULE1_WORKER_MAX_SYNC_WINDOWS` | 每店每周期最多处理的 30 分钟同步窗口数 | `2` |
| `MODULE1_WORKER_TASK_LIMIT` | 每周期最多准备、发送或退款的动作任务数 | `20` |
| `MODULE1_NOTIFICATION_TRANSPORT` | 拦截通知出口；当前支持 `disabled` / `qywx_webhook` | `disabled` |
| `MODULE1_NOTIFICATION_MIN_TASK_ID` | 自动通知上线水位；仅预检和发送任务 ID 大于等于该值的拦截通知，防止历史积压批量补发 | `0` |
| `MODULE1_PDD_REFUND_EXECUTION_ENABLED` | 后台运行器的平台退款执行总开关 | `false` |
| `MODULE1_DESKTOP_GROUP_MAP` | 拼多多物流公司 ID 到企业微信外部群完整精确群名的 JSON 白名单 | `{}` |
| `MODULE1_DESKTOP_SEND_ENABLED` | 企业微信桌面自动发送总开关；当前保持关闭 | `false` |
| `KUAIDI100_CUSTOMER` / `KUAIDI100_KEY` | 快递 100 实时查询授权（密钥） | 无 |
| `KUAIDI100_DEFAULT_PHONE` | 需要手机号校验的快递所用默认手机号 | 无 |
| `KUAIDI100_CARRIER_MAP` | 拼多多物流公司 ID 到快递 100 公司代码的 JSON 映射 | `{"85":"yuantong","131":"debangwuliu","384":"jtexpress"}` |
| `PDD_APP_1_CLIENT_ID` / `PDD_APP_1_CLIENT_SECRET` | 1–4 店共用的开放平台应用凭据 | 无 |
| `PDD_APP_2_CLIENT_ID` / `PDD_APP_2_CLIENT_SECRET` | 5–7 店共用的另一组应用凭据 | 无 |
| `PDD_SHOP_1_CODE` … `PDD_SHOP_7_CODE` | 1–7 店的本地稳定代号 | `pdd-shop-01` … `pdd-shop-07` |
| `PDD_SHOP_1_APP` … `PDD_SHOP_7_APP` | 店铺使用的应用组；1–4 店为 `1`，5–7 店为 `2` | `1` / `2` |
| `PDD_SHOP_1_ACCESS_TOKEN` … `PDD_SHOP_7_ACCESS_TOKEN` | 1–7 店各自的授权 Token | 无 |

生产环境不得使用示例密码，也不得将 `.env`、店铺 Secret 或 Token 提交到 Git。

1–4 店共用 `PDD_APP_1_CLIENT_ID` / `PDD_APP_1_CLIENT_SECRET`，5–7 店共用 `PDD_APP_2_CLIENT_ID` / `PDD_APP_2_CLIENT_SECRET`。每个店铺仍必须将自己的 Token 填入对应的 `PDD_SHOP_N_ACCESS_TOKEN`，不要将多个 Token 用逗号拼在同一行。单店的 `PDD_CLIENT_ID`、`PDD_CLIENT_SECRET` 和 `PDD_ACCESS_TOKEN` 暂时保留，仅用于旧联调命令回退。

售后订单记录页的“归属业务员”来自旧管理系统客户档案。系统优先使用 `ERP_READ_DATABASE_URL`：将平台订单号转换为 `pdd{订单号}`，在 `00sobackup.客户编号` 精确找到客户，再读取 `kehu.归属业务员`；客户档案未填写时回退到订单快照。没有数据库只读账号时，可配置 `ERP_WEB_LOOKUP_ENABLED=true` 以及管理系统员工账号，工作台会登录 `/leedis/index.php/welcome/loginact`，再调用客户档案自动补全接口只读查询。网页查询结果默认缓存 5 分钟，登录失效只自动重登一次，不会访问客户修改接口。登录凭据只能保存在被 Git 忽略的本机 `.env`，严禁写入 README 或提交仓库。

启用 `ERP_SALES_OWNER_SYNC_ENABLED` 后，模块 1 每个周期会在拼多多增量同步后，将一小批客户名字、归属业务员、匹配状态和查询时间缓存到本地 `aftersales_orders`。页面基于本地缓存进行全量业务员筛选，不会在一次页面查询中批量请求旧管理系统。正常结果每天刷新；网页暂时不可用时 5 分钟后重试，归属查询失败不会阻断拦截、物流闸门或退款安全流程。首次接入可分批执行 `aftersales-sync-sales-owners.exe --limit 20`，每批完成即提交，不需要一次等待全部历史订单。

管理系统人工待办发布复用同一组 `ERP_WEB_BASE_URL`、`ERP_WEB_USERNAME`、`ERP_WEB_PASSWORD`。只有 `ERP_TODO_PUBLISH_ENABLED=true` 与 `ERP_WRITE_ENABLED=true` 同时满足时才会调用 `/leedis/index.php/wunderlist/stdnew`；其余情况下只在本地动作队列准备 `ERP_CREATE_MANUAL_TODO`。每条远端事项包含 `【售后工作台 M1:售后单号】` 幂等标识，发布前后都会按经办人回查，成功后将管理系统待办 ID 保存到动作任务 `payload.external_todo_id`，因此超时重试不会重复发布。归属业务员为空或冲突时不会猜测经办人，也不会发布。

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

同步结果同时保存 `platform_after_sales_status`、订单 `platform_order_refund_status` 和 `is_speed_refund`。只有售后状态明确为 10，或订单退款状态明确为 4（退款成功），才作为“平台已退款”的事实；`speed_refund_flag=1` 只记录极速退款标记，不单独作为完成依据。

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

## 模块 1：在途拦截与退款

模块 1 只扫描 `PENDING_CHECK`、发货状态为 `IN_TRANSIT`、具有发货运单号、售后类型严格为 `ONLY_REFUND`，并且“申请退款金额等于拼多多详情中的优惠后实付金额”的全额退款订单。金额统一保存为两位小数后精确比较：申请金额低于实付金额时标记为 `PARTIAL_REFUND_EXCLUDED`，视为补偿款、差价、运费或配件补偿，不拦截快递、不生成业务员待办；实付金额缺失或退款金额异常时失败关闭，冻结自动拦截。退款原因和留言仅用于展示，不能替代金额判断。退货退款留给模块 2，换货单也不会进入自动退款链路。拼多多只负责售后读取和同意退款，物流状态由独立的快递 100 适配器读取，不依赖或修改旧管理系统代码。当前流转为：

1. 生成幂等的本地 `QYWX_INTERCEPT_NOTIFY` 草稿动作；该动作尚未取得发送资格；
2. 发送前强制查询快递 100：运输中、派件中和查询无结果的任务保留；已签收任务取消并转 `MANUAL_PROCESSING`；已在退回途中或已经退回的任务取消，禁止重复通知；
3. 物流预检阶段整体失败或未配置快递 100 时采用失败关闭，当前周期禁止发送任何拦截通知；单票查询无结果时允许保留拦截通知，但退款闸门标记为 `HOLD`；
4. 企微发送成功后，订单进入 `INTERCEPT_PUSHED`；快递 100 退款闸门随即再次查询，不需要人工确认“已受理”。普通在途允许生成 `PDD_AGREE_REFUND`；
5. 命中“派件/派送/投递”时进入 `INTERCEPT_WAITING_RETURN`，不自动退款；查询失败或公司代码未映射时同样不放行；拦截失败则进入 `INTERCEPT_FAILED`；
6. 出现“退回/退件/拒收/原路返回”等明确退回记录后才解除派件冻结。若平台尚未退款则生成 `PDD_AGREE_REFUND`；若平台已经退款则跳过平台写接口；
7. 平台退款完成但包裹尚在退回途中时进入 `INTERCEPT_REFUNDED_WAITING_RETURN`。只有物流明确显示退回件已经签收后，才进入 `RETURN_WAITING_ERP_MATCH` 并生成 `ERP_MATCH_RETURN_ORDER` 本地待办；该待办只预留后续“客户档案退货单精确匹配/暂存认领”接口，目前不会直接操作旧管理系统；
8. 派件中、已签收无退回记录、拦截失败或订单进入 `MANUAL_PROCESSING` 时，按客户档案“归属业务员”幂等生成 `ERP_CREATE_MANUAL_TODO`；事项包含店铺、平台订单号、售后单号、发货运单和标准化物流状态，不复制快递轨迹中的电话、地址等原文；
9. 后续 ERP 匹配规则以发货运单号、型号、颜色、数量、单价完全一致为自动处理前提；暂存认领流程等收到完整操作步骤后再接入。

先预览候选数量，再写入本地队列：

```powershell
.\.venv\Scripts\aftersales-preview-module1.exe --shops pdd-shop-01 --limit 100
.\.venv\Scripts\aftersales-preview-module1.exe --shops pdd-shop-01 --limit 100 --details
.\.venv\Scripts\aftersales-process-module1.exe
.\.venv\Scripts\aftersales-process-module1.exe --shops pdd-shop-01 --limit 100 --apply
```

升级全额退款闸门后，先只读回填历史售后的优惠后实付金额，再确认写入。命令只调用拼多多读取接口；`--apply` 才会写本地金额、分类状态并取消部分退款遗留的待发送模块 1 动作：

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

配置快递 100 后先只读预览物流闸门，再写入本地状态和待办。`KUAIDI100_CARRIER_MAP` 示例为 `{"拼多多物流公司ID":"kuaidi100公司代码"}`，真实映射应以当前订单数据为准：

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

### 模块 1 实际后台运行

后台运行器把现有的一次性命令串成固定周期，执行顺序为：

1. 按同步游标增量读取指定拼多多店铺的状态 `2/3/10` 售后；
2. 启用归属同步时，小批量只读查询 ERP 客户档案并更新本地业务员缓存；
3. 先要求“申请退款金额 = 优惠后实付金额”，再筛出“在途 + 全额仅退款”并幂等生成本地拦截通知任务；部分退款直接排除，金额缺失或异常失败关闭；
4. 对所有待发送任务执行快递 100 前置预检；已签收、退回中、已退回任务会保留审计记录但改为 `CANCELLED`，不会进入通知出口；
5. 根据 `MODULE1_NOTIFICATION_TRANSPORT` 处理通过预检的通知；默认 `disabled`，任务只保留在待发送队列；
6. 对已经成功发出拦截通知的订单再次查询快递 100，并按派件/签收/退回轨迹更新本地退款闸门；
7. 对派件中、已签收无退回、拦截失败和人工处理状态按归属业务员生成幂等的本地管理系统待办任务；
8. 根据 `ERP_TODO_PUBLISH_ENABLED` 处理管理系统待办；默认关闭，只有再同时开启 `ERP_WRITE_ENABLED` 才真实发布并回填远端待办 ID；
9. 预览已经放行的拼多多退款任务。只有 `MODULE1_PDD_REFUND_EXECUTION_ENABLED=true` 与 `PDD_WRITE_ENABLED=true` 同时满足时，后台进程才会真实调用平台退款。

因此，在企微机器人和桌面自动发送之间尚未确定时，后台运行器仍可持续同步订单并准备拦截任务，但会停在“待发送”，不会越过通知步骤自动退款。以后新增桌面发送适配器只替换第 4 步，不修改前后状态机。

先在前台执行一个周期核对输出。该命令会增量写入本地 MySQL 和本地动作队列，但不会发送通知或调用平台退款：

```powershell
.\.venv\Scripts\aftersales-run-module1.exe
```

确认后使用 Windows 启停脚本让它隐藏在后台持续运行：

```powershell
& .\scripts\module1-worker.ps1 -Action Start
& .\scripts\module1-worker.ps1 -Action Status
& .\scripts\module1-worker.ps1 -Action Stop
```

本机长期运行时，安装 Windows 登录自启动与 5 分钟守护。安装器会从当前运行的 MySQL 自动记录程序和配置文件路径到被 Git 忽略的 `.runtime/module1-autostart.json`，启动入口不保存平台 Token、管理系统账号或数据库密码。它优先注册 Windows 计划任务；当前进程没有计划任务权限时，自动回退到当前用户“启动”目录并运行一个隐藏的守护进程。两种方式都会在登录后检查，并每 5 分钟再次检查：MySQL 未监听时隐藏启动 MySQL，模块 1 未运行时调用上面的幂等启动脚本；已经运行时不会重复启动。

```powershell
& .\scripts\module1-autostart.ps1 -Action Install
& .\scripts\module1-autostart.ps1 -Action Status
# 不再需要时再显式卸载
& .\scripts\module1-autostart.ps1 -Action Uninstall
```

计划任务或用户启动项都使用当前 Windows 用户的交互登录令牌，因此电脑重启后至少需要登录一次；锁屏不影响同步，但休眠、关机和退出登录会停止本地运行。应在 Windows 电源设置中关闭自动休眠。若以后迁移到服务器，应先执行 `Uninstall`，避免两台机器同时处理同一售后。

默认运行 1、2、3、4、6、7 店，暂时跳过 Token 已失效的 5 店；每店每周期最多追赶两个 30 分钟窗口，每周期最多处理 20 条动作任务，完整周期结束后等待 60 秒。运行日志位于 `.runtime/module1-worker.log`，错误日志位于 `.runtime/module1-worker-error.log`，PID 和安全停止信号也保存在被 Git 忽略的 `.runtime/`。`Status` 同时显示最近一个周期的精简摘要；`Stop` 会等待当前平台请求和数据库事务完成后退出，不会在请求中途强杀进程。

后台运行失败时按以下方式恢复：

- 单店拼多多读取失败：其他店继续完成；修复 Token 或网络后，下个周期从该店最后成功游标重试，不会跳过失败窗口；
- 单票详情返回拼多多 `45001`“订单不属于当前店铺或订单不存在”：隔离并计入 `records_skipped`，其余订单继续处理且窗口游标正常推进；其他平台错误仍按整店失败处理；
- ERP 归属查询失败：该批记录保留失败状态并在 5 分钟后重试；该独立阶段不会阻断后续拦截安全流程；
- ERP 人工待办发布关闭：本地任务保留为 `PENDING`；开启双重写开关后下个周期继续。发布失败会使用远端售后标识先查重，再在 `ERP_TODO_MAX_ATTEMPTS` 范围内重新入队；超过次数后保留 `FAILED` 和错误原因供人工检查；
- 拼多多优惠后实付金额缺失：该售后不会进入拦截、人工待办或平台退款动作；先运行 `pdd-backfill-refund-amounts` 只读预演，确认接口能够返回金额后再使用 `--apply`；
- 通知出口为 `disabled`：属于预期暂停，待办保留，选择发送方式后可以继续执行；
- 快递 100 整体未配置或预检阶段异常：采用失败关闭，当前周期不发送任何通知；单票查询无结果时保留拦截通知但冻结自动退款，下个周期继续重查；
- 电脑重启：本地 MySQL 不是 Windows 服务时先启动 MySQL，再重新执行 `Start`；
- 自启动守护失败：执行 `& .\scripts\module1-autostart.ps1 -Action Status`，再检查 `.runtime/module1-autostart.log`；如果 MySQL 或工作区路径发生变化，重新执行 `Install` 刷新本机配置；
- 重复启动：启停脚本通过 PID 文件阻止第二个后台进程。不要绕过脚本同时启动多个 `--forever` 实例。

如需前台观察持续循环或临时覆盖店铺，可直接执行：

```powershell
.\.venv\Scripts\aftersales-run-module1.exe --forever --shops 1 2 3 4 6 7 --interval-seconds 60
```

未来若确定使用企微 Webhook，需要同时设置 `MODULE1_NOTIFICATION_TRANSPORT=qywx_webhook`、配置 Webhook 并开启 `QYWX_WRITE_ENABLED=true`；若采用企业微信桌面自动发送，则新增独立适配器和总开关，现阶段不要把通知出口设置为未支持的值。无论选择哪种发送方式，上线前都应将 `MODULE1_NOTIFICATION_MIN_TASK_ID` 设置为当时“最大拦截通知任务 ID + 1”；后台物流预检、通用通知执行器和桌面预览都会应用同一水位，水位之前的历史任务继续留作查询但不会补发，也不会阻塞新任务。

### 企业微信桌面发送准备

外部快递群不支持群机器人 Webhook，因此桌面发送使用拼多多物流公司 ID 到“完整精确群名”的本机白名单。真实群名只允许写入被 Git 忽略的 `.env`，示例仓库和日志不得输出完整订单消息。2026-08-31 已在当前企业微信客户端中用键盘搜索逐一验证德邦、极兔、圆通和顺丰四个外部群均能被完整群名唯一命中；没有进入聊天、填写草稿或发送消息。拼多多官方物流公司列表对应关系为普通顺丰 `44/SF`、圆通 `85/YTO`、德邦 `131/DB`、极兔 `384/JTSD`。

先预览本地待发送任务能否全部解析到群白名单：

```powershell
.\.venv\Scripts\aftersales-preview-desktop-notices.exe --limit 20
```

该命令只读数据库，只输出脱敏后的售后单号、订单号和运单号，不激活企业微信、不填写草稿、不发送消息。输出中的 `notification_min_task_id` 是当前上线水位；预览不会读取水位以前的历史任务。`blocked_missing_group` 必须为 `0` 才能进入桌面自动化。当前 `MODULE1_DESKTOP_SEND_ENABLED=false`，桌面发送适配器完成群标题、输入框和发送结果验证前不得开启。

桌面预览和通用外部动作执行器都会再次校验任务中的物流预检凭证。`blocked_preflight` 与 `blocked_missing_group` 必须同时为 `0`；缺少预检时间、物流状态不允许发送或退款闸门标记不一致时一律失败关闭，不能通过手工执行旧命令绕过物流预检。

## 模块 3：未发货退款与锁包

模块 3 处理的是“拼多多已经极速退款后，ERP 如何停止履约并完成平账”，不再尝试调用 `pdd.refund.agree`。它只扫描 `PENDING_CHECK`、售后类型为 `ONLY_REFUND`、平台发货状态为 `UNSHIPPED` 或 `PACKED_NOT_SHIPPED`，并且平台退款状态已经明确成功的售后单。拼多多的“未发货”不足以证明 ERP 尚未出包，因此仍须检查 ERP：

- `UNSHIPPED`：生成唯一的 `ERP_CHECK_FULFILLMENT` 待办，等待 ERP 返回未打包或已出包；确认未打包后取消排单，再生成 ERP 退款流水；
- `PACKED_NOT_SHIPPED`：生成唯一的 `ERP_LOCK_PACKING` 待办；锁包成功后生成 ERP 退款流水；
- 在途和已签收订单不属于模块 3，本命令不会处理；
- ERP 尚未直连时，使用动作结果回填命令推进状态；任何一步失败都会停留在失败任务，不会越级退款。

先执行迁移并进行只读预览：

```powershell
alembic upgrade head
.\.venv\Scripts\aftersales-process-module3.exe
```

预览结果确认后，写入本地动作队列：

```powershell
.\.venv\Scripts\aftersales-process-module3.exe --apply
.\.venv\Scripts\aftersales-process-module3.exe --shops pdd-shop-01 pdd-shop-02 --limit 500 --apply
```

命令输入来自 `aftersales_orders`，输出为不含订单号的汇总 JSON。动作使用唯一幂等键，已有动作不会重复创建。数据库异常时本批次回滚；修复后可直接重跑。

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

回填失败使用 `--failed --message "失败原因"`，系统记录错误并停止后续流转。当前不会直接写入旧管理系统源码或复用其登录页面；旧系统仅作为 ERP 业务规则参考。

## 售后订单记录中心

当前项目已包含独立的 React 网页工作台，并通过只读 API 查询本地 MySQL 中的真实售后订单、店铺、SKU 和自动化任务记录。页面提供：

- 今日新增、待拦截、待人工、已完成汇总；
- 按店铺、归属业务员、售后类型、处理状态、物流状态、申请时间和单号检索；
- 列表、详情和 CSV 同时显示申请退款金额、优惠后实付金额及“全额退款/部分退款补偿/待核实”范围；
- 左侧“在途拦截”独立页面只纳入全额退款：包括在途仅退款候选单、已经生成拦截通知任务的订单以及后续退回/退款/ERP 匹配状态；部分退款即使存在旧版遗留任务也只保留在“全部售后”及订单审计时间线中，不进入拦截页面和待拦截汇总；
- 在途拦截页集中显示归属业务员、目标快递群、运单、通知状态、物流轨迹、退款闸门和当前处理环节，并支持待发拦截、退款冻结、已退款待退回、待 ERP 匹配等筛选；
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

构建产物存在时，FastAPI 会在根路径挂载 `frontend/dist/client`，浏览器打开 `http://127.0.0.1:8000/`。只读接口为：

- `GET /api/v1/aftersales/orders`：全部已同步售后退款记录的汇总、筛选、分页、店铺与归属业务员选项，包含全额退款和部分退款；
- `GET /api/v1/aftersales/intercepts`：仅限全额退款的模块 1 在途拦截汇总、阶段筛选、快递群与退款闸门状态；
- `GET /api/v1/aftersales/orders/{after_sales_sn}`：订单详情、SKU、物流判断和动作时间线。

两个页面都只读，不会直接调用拼多多退款、企微发送或 ERP 写接口。在途拦截页没有“发送”或“退款”按钮，真实外部动作仍只能由后台运行器在对应总开关打开后执行。数据库暂未保存买家昵称时，详情明确显示“平台未返回”，不会虚构客户信息。

## 健康检查

- `GET /health/live`：进程存活，不访问外部依赖。
- `GET /health/ready`：执行 `SELECT 1` 验证数据库；失败时返回 HTTP 503。

## 验证

```powershell
ruff check .
pytest
```
