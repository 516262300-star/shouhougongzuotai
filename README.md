# 利德仕电商自动化售后工作台

面向利德仕多平台、多店铺的售后中台。当前仓库已完成 Phase 1 工程骨架、全量数据库初始化、拼多多七店同步，以及模块 1、模块 3 的安全动作流转。

## 当前边界

- 已实现：配置加载、MySQL 连接池、Alembic 迁移、健康检查、Docker Compose、本地拼多多联调、七店售后增量同步、模块 1 在途拦截队列与快递 100 退款闸门、模块 3 未发货/已出包判定队列、企微机器人通知和受写开关保护的拼多多同意退款动作。
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
| `ERP_WRITE_ENABLED` | ERP 外部写操作总开关，当前未开放 | `false` |
| `QYWX_INTERCEPT_WEBHOOK_URL` | 模块 1 快递拦截群机器人 Webhook（密钥） | 无 |
| `QYWX_TIMEOUT_SECONDS` | 企微请求超时秒数 | `10` |
| `QYWX_WRITE_ENABLED` | 企微机器人发送开关 | `false` |
| `KUAIDI100_CUSTOMER` / `KUAIDI100_KEY` | 快递 100 实时查询授权（密钥） | 无 |
| `KUAIDI100_DEFAULT_PHONE` | 需要手机号校验的快递所用默认手机号 | 无 |
| `KUAIDI100_CARRIER_MAP` | 拼多多物流公司 ID 到快递 100 公司代码的 JSON 映射 | `{}` |
| `PDD_APP_1_CLIENT_ID` / `PDD_APP_1_CLIENT_SECRET` | 1–4 店共用的开放平台应用凭据 | 无 |
| `PDD_APP_2_CLIENT_ID` / `PDD_APP_2_CLIENT_SECRET` | 5–7 店共用的另一组应用凭据 | 无 |
| `PDD_SHOP_1_CODE` … `PDD_SHOP_7_CODE` | 1–7 店的本地稳定代号 | `pdd-shop-01` … `pdd-shop-07` |
| `PDD_SHOP_1_APP` … `PDD_SHOP_7_APP` | 店铺使用的应用组；1–4 店为 `1`，5–7 店为 `2` | `1` / `2` |
| `PDD_SHOP_1_ACCESS_TOKEN` … `PDD_SHOP_7_ACCESS_TOKEN` | 1–7 店各自的授权 Token | 无 |

生产环境不得使用示例密码，也不得将 `.env`、店铺 Secret 或 Token 提交到 Git。

1–4 店共用 `PDD_APP_1_CLIENT_ID` / `PDD_APP_1_CLIENT_SECRET`，5–7 店共用 `PDD_APP_2_CLIENT_ID` / `PDD_APP_2_CLIENT_SECRET`。每个店铺仍必须将自己的 Token 填入对应的 `PDD_SHOP_N_ACCESS_TOKEN`，不要将多个 Token 用逗号拼在同一行。单店的 `PDD_CLIENT_ID`、`PDD_CLIENT_SECRET` 和 `PDD_ACCESS_TOKEN` 暂时保留，仅用于旧联调命令回退。

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

模块 1 只扫描 `PENDING_CHECK`、发货状态为 `IN_TRANSIT`、具有发货运单号，且售后类型为仅退款或退货退款的订单；换货单不会进入自动退款链路。拼多多只负责售后读取和同意退款，物流状态由独立的快递 100 适配器读取，不依赖或修改旧管理系统代码。当前流转为：

1. 生成幂等的 `QYWX_INTERCEPT_NOTIFY` 动作；
2. 企微发送成功后，订单进入 `INTERCEPT_PUSHED`；
3. 企微发送成功后，快递 100 轨迹闸门可直接处理该订单，不需要人工再次确认“已受理”；普通在途允许生成 `PDD_AGREE_REFUND`；
4. 命中“派件/派送/投递”或“已签收但没有退回记录”时进入 `INTERCEPT_WAITING_RETURN`，不自动退款；查询失败或公司代码未映射时同样不放行；拦截失败则进入 `INTERCEPT_FAILED`；
5. 出现“退回/退件/拒收/原路返回”等明确退回记录后才解除派件冻结。若平台尚未退款则生成 `PDD_AGREE_REFUND`；若平台已经退款则跳过平台写接口；
6. 平台退款完成但包裹尚未回来时进入 `INTERCEPT_REFUNDED_WAITING_RETURN`。包裹出现退回记录后进入 `RETURN_WAITING_ERP_MATCH`，生成 `ERP_MATCH_RETURN_ORDER` 本地待办；该待办只预留后续“客户档案退货单精确匹配/暂存认领”接口，目前不会直接操作旧管理系统；
7. 后续 ERP 匹配规则以发货运单号、型号、颜色、数量、单价完全一致为自动处理前提；暂存认领流程等收到完整操作步骤后再接入。

先预览候选数量，再写入本地队列：

```powershell
.\.venv\Scripts\aftersales-process-module1.exe
.\.venv\Scripts\aftersales-process-module1.exe --shops pdd-shop-01 --limit 100 --apply
```

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

```powershell
.\.venv\Scripts\aftersales-check-intercept-logistics.exe --limit 100
.\.venv\Scripts\aftersales-check-intercept-logistics.exe --limit 100 --apply
```

只有物流闸门放行后才会产生拼多多退款待办。真实执行 `PDD_AGREE_REFUND` 前，执行器还会再次读取快递 100；如果此时已进入派件、已签收无退回记录，或者物流查询失败，待办会被冻结而不会调用拼多多。执行前必须把 `PDD_WRITE_ENABLED` 改为 `true`。写请求只发送一次，网络结果不明时不会自动重试，应先到平台核对，防止重复退款：

```powershell
.\.venv\Scripts\aftersales-execute-actions.exe --types PDD_AGREE_REFUND
.\.venv\Scripts\aftersales-execute-actions.exe --types PDD_AGREE_REFUND --apply
```

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

## 健康检查

- `GET /health/live`：进程存活，不访问外部依赖。
- `GET /health/ready`：执行 `SELECT 1` 验证数据库；失败时返回 HTTP 503。

## 验证

```powershell
ruff check .
pytest
```
