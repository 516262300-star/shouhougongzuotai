# 利德仕电商自动化售后工作台

面向利德仕多平台、多店铺的售后中台。当前仓库已完成 Phase 1 工程骨架、全量数据库初始化、拼多多七店只读同步、模块 2 仓库人工退货建单，以及模块 3 的安全决策队列。

## 当前边界

- 已实现：配置加载、MySQL 连接池、Alembic 迁移、健康检查、Docker Compose、本地拼多多联调、七店售后增量同步、仓库人工退货扫码/暂存/客户认领接口、模块 3 未发货/已出包判定队列。
- 已建立全局业务表及内部同步表：`shops`、`aftersales_orders`、`aftersales_items`、`return_scrap_records`、`negative_reviews`、`pdd_sync_cursors`、`aftersales_action_tasks`、`warehouse_return_records`、`warehouse_return_items`。
- 未实现：拼多多写操作、企微 Webhook、真实 ERP API 适配器、客户主档同步、仓库验货后的合格/异常判定及自动退款；外部写能力保持关闭。
- `D:\desktop\codex\daima` 中的自研管理系统源码只作为业务规则参考，售后工作台不会修改、调用或直接写入该系统。

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
| `PDD_WRITE_ENABLED` | 拼多多写操作开关，当前未开放 | `false` |
| `PDD_SYNC_INITIAL_LOOKBACK_HOURS` | 新店铺无游标时的首次回溯小时数 | `72` |
| `PDD_SYNC_OVERLAP_SECONDS` | 续传时向前重叠秒数，用于防止边界漏单 | `300` |
| `PDD_SYNC_PAGE_SIZE` | 售后增量单页数量 | `100` |
| `ERP_WRITE_ENABLED` | ERP 外部写操作总开关，当前未开放 | `false` |
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

只读请求在网关 HTTP 429/5xx、网络异常或平台频率限制码 `70031` 时指数退避重试；其他平台业务错误不重试，并保留 `error_code` 和 `request_id` 供排查。当前命令不会调用 `pdd.refund.agree` 或任何写接口。

## 七店售后增量同步

同步器默认拉取状态 2（仅退款待商家处理）和状态 3（退货退款待商家处理），按 30 分钟窗口分页读取。每条售后单补查售后详情和订单详情，然后幂等写入 `aftersales_orders` 和 `aftersales_items`。每个已提交窗口的进度保存在 `pdd_sync_cursors`。

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

可用 `--statuses 2 3`、`--shops 1 2 3` 限定范围。单店失败时该店当前窗口回滚，其他店继续；命令最终返回非零退出码。重新执行会从该店最后成功游标向前重叠 5 分钟续传，已存在的售后单按售后单号更新，不会重复新建。

同步器只向本地 MySQL 写入店铺和售后数据，不会调用拼多多写接口。任一店铺 Token 过期或接口异常时会被单店标记失败，不影响其他店铺。

## 模块 2：仓库人工退货收货

当前实现遵循仓库实际操作，不在扫描快递单号时自动判断合格或异常：

1. 仓库人员扫描买家退货运单号，工作台反查已同步的售后单和平台申请 SKU；
2. 人工拆包并录入实际收到的型号、颜色和数量；
3. 无法确定客户时保存到工作台自己的退货暂存列表；
4. 已确定客户时直接填写本地 `customer_reference`，或将暂存单后续认领到该客户；
5. 当前步骤不会调用拼多多同意退款，不会写 ERP，也不会生成财务退款流水。

先执行数据库升级：

```powershell
alembic upgrade head
```

扫描运单号只读反查：

```powershell
$scanBody = @{ return_tracking_number = "买家退货运单号" } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/warehouse/scan" `
  -ContentType "application/json" -Body $scanBody
```

拆包清点后保存到退货暂存：

```powershell
$returnBody = @{
  receipt_sn = "WR-20260829-0001"
  return_tracking_number = "买家退货运单号"
  destination = "STAGING"
  operator = "仓库操作员"
  items = @(
    @{ product_code = "6805-96"; color = "黑"; quantity = 2 }
  )
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/warehouse/returns" `
  -ContentType "application/json" -Body $returnBody
```

直接归到客户档案时将 `destination` 改为 `CUSTOMER_PROFILE`，并填写 `customer_reference`；当前字段是售后工作台内部保存的客户引用，不会写入现有管理系统。暂存后认领客户：

```powershell
$assignBody = @{
  customer_reference = "客户唯一引用"
  customer_name = "客户显示名称"
  assigned_by = "认领人"
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/warehouse/returns/WR-20260829-0001/assign-customer" `
  -ContentType "application/json" -Body $assignBody
```

`receipt_sn` 是 PDA 每次收货提交生成的幂等单号；同一单号和相同内容重复提交会返回原结果，不会重复建单。同一个退货运单号只能登记一次。运单号匹配到多个售后单时，直接归档客户必须明确传入 `after_sales_sn`；未识别或暂时无法确认的包裹仍可进入暂存。

退货暂存列表使用 `GET /api/v1/warehouse/returns?destination=STAGING` 查询；客户档案需要展示其退货单时，使用 `GET /api/v1/warehouse/returns?destination=CUSTOMER_PROFILE&customer_reference=客户唯一引用` 查询。默认返回最近 100 单，`limit` 可设置为 1–500。

## 模块 3：未发货退款判定队列

模块 3 只扫描 `PENDING_CHECK` 且平台发货状态为 `UNSHIPPED` 或 `PACKED_NOT_SHIPPED` 的售后单。拼多多的“未发货”不足以证明 ERP 尚未出包，因此系统不会直接退款：

- `UNSHIPPED`：生成唯一的 `ERP_CHECK_FULFILLMENT` 待办，等待 ERP 返回未打包或已出包；
- `PACKED_NOT_SHIPPED`：生成唯一的 `ERP_LOCK_PACKING` 待办；
- 在途和已签收订单不属于模块 3，本命令不会处理；
- `PDD_WRITE_ENABLED=false`、`ERP_WRITE_ENABLED=false` 时不会调用任何外部写接口。

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

命令输入来自 `aftersales_orders`，输出为不含订单号的汇总 JSON。动作使用“模块 + 售后单号 + 动作类型”作为幂等键，已有动作的订单不会继续占用扫描额度；并发碰撞会计入 `tasks_existing`，不会重复创建。数据库异常时本批次回滚；修复后可直接重跑。目前只生成本地 ERP 待办，尚不执行取消排单、锁包、平台退款或 ERP 财务退款单。

## 健康检查

- `GET /health/live`：进程存活，不访问外部依赖。
- `GET /health/ready`：执行 `SELECT 1` 验证数据库；失败时返回 HTTP 503。

## 验证

```powershell
ruff check .
pytest
```
