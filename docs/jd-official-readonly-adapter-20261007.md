# 京东官方售后只读适配（2026-10-07，离线待验收）

## 当前状态与边界

已新增独立的京东 SP-API 售后列表、详情客户端，并完成模拟响应测试。**尚未通过真实京东业务接口验收，未部署到云鼎，未接入正式同步或退款动作。** 查询公开的京东接口文档不等于业务接口连通测试。

- 代码：`src/aftersales_workbench/integrations/marketplace/jd_official.py`。
- 测试：`tests/test_jd_official.py`；假凭据、假订单、`httpx.MockTransport`，本测试模块禁止真实 socket 连接和 DNS 查询。
- 本次不读取正式凭据、不执行 SSH、不访问实际售后数据，不改变数据库、同步游标、20 小时提醒、退款或模块 1/2/3 开关。
- 新客户端直连固定官方 HTTPS 地址，不使用第三方中转，不跟随重定向，不读取环境代理配置；TLS 校验保持开启。
- 正式 `marketplace/runner.py` 仍注册旧 `JdReadClient`。本次没有把官方凭据混入旧配置，也没有改变一店、二店的既有链路。

## 入口与配置

这是供后续验收使用的 Python 适配模块，**没有新增 CLI、环境变量、定时任务或自动触发入口**。

显式构造 `JdOfficialCredentials`：

| 字段 | 要求 |
| --- | --- |
| `vender_id` | 预期 POP 商家 ID 的数字字符串；不是店铺 ID、`shop_code` 或旧中转标签 |
| `app_key` | 官方应用的 `SecretStr` 值 |
| `app_secret` | 官方应用的 `SecretStr` 值，不进入请求内容或对象 repr |
| `access_token` | 对应商家授权的 `SecretStr` 值，仅进入认证头 |

不自动读取 `.env` / `JD_SHOPS_JSON`，不接受或保存 `refresh_token`，不自动刷新 Token。不把凭据写进文档、测试、提交或命令行。实际凭据加载与授权有效期验证留给后续受控接入步骤。

用 `JdOfficialReadClient(credentials)` 的上下文管理器保证连接池关闭。调用顺序：

1. `list_aftersales(start_modified_ms=..., end_modified_ms=..., page=1, page_size=50)`：返回 `JdOfficialPage`，仅保留通过归属校验的售后/订单引用。
2. `get_aftersale(afs_order_id)`：只接受同一客户端已通过列表核验的售后 ID，返回 `JdOfficialAftersale`。
3. 批量调用可使用 `read_window(...)`：先校验完整列表，再逐笔取详情，所有步骤成功才返回完整 tuple；完成或失败后均清空本轮的详情查询准入记录。

仅实例化客户端不会发出业务请求；上述查询方法会发请求，不能把真实凭据调用误当作离线测试。客户端的 `transport` 和 `now_ms` 可注入供单测使用。

## 官方接口与签名

仅支持两个固定 GET 接口：

| 操作 | 路径（固定域名 `https://api-cn.jd.com`） | 参数 |
| --- | --- | --- |
| `listAfsOrders` | `/rest/sp-aftercare/v0/afs-orders` | `updateStartTime`、`updateEndTime`、`page`、`pageSize` |
| `getAfsOrder` | `/rest/sp-aftercare/v0/afs-orders/{afsOrderId}` | `scopeSet=refundInfo,skuExtInfo` |

签名按京东公开 SP-API 签名指南：三个固定认证头 `X-JOS-App-Key`、`X-JOS-Access-Token`、`X-JOS-Timestamp`，连同路径参数和查询参数按字段名 ASCII 排序，拼接原始值，前后加入 App Secret，计算大写 MD5。签名计算不预先 URL 编码；详情的 `afsOrderId` 也参与签名。时间戳使用 13 位 Unix 毫秒。请求另带 `X-JOS-Request-Identity: vender`，它不属于指南中的三个固定签名头。

当前按文档兼容顶层响应与 `Response` 封装；若两层同时出现相互歧义的业务字段则拒绝解析。只有 HTTP 成功且业务 `success` 为 `true` / `"true"`、没有非空 `errorList`，才继续解析。

本地保护限制：单次时间窗口大于 0 且不超过 24 小时、每页最多 50 条、每个窗口最多 100 页。这些是本适配器的保护上限，**不是对京东官方限额的承诺**。

没有售后操作、退款写入、通用方法转发或服务单日志入口；请求无自动重试。

## 归属、分页与字段规则

- 每条列表记录必须包含 `afsOrderBaseInfo.buId`，且与显式配置的 `vender_id` 相等。整页校验完成后才允许查询其中的详情。
- 当前详情文档不包含 `buId`，因此不能单靠详情验证商家。先取得列表中的归属证据，再检查详情售后号、关联订单号一致；如果详情额外返回 `buId`，也必须一致。
- 顶层 `orderId`、`orderInfo.orderId`、`relationInfo.orderId` 如同时存在必须一致；不将 `srcOrderId` 或 `newOrderId` 当成本售后的当前订单。
- 必须有合法分页元信息、与请求一致的页码和页大小、完整的本页条数。列表字段缺失、空响应、跨页重复、总数变化或超出预算均失败，不能伪装成“无售后”。
- 文档参数表中的 SKU 和运单字段为数组。遇到与参数表不符的对象形状先失败，留待真实只读验收核对，不悄悄丢弃记录。
- `JdOfficialAftersale` 是待验收事实快照，不是现有数据库入库或退款动作使用的 `NormalizedMarketplaceRefund`。

| 信息 | 本次处理方式 |
| --- | --- |
| 金额单位 | SP-API 为元，使用 `Decimal`，不能沿用旧接口除以 100 的规则 |
| 申请金额 | `applyRefundAmount` 或 `applyRefundDetail.refundAmount`；缺失保持 `None`，同时存在但不一致则失败 |
| 预估金额 | `estimateRefundDetail.maxRefundAmount` 独立保存，不代替申请金额或实退金额 |
| 平台报告的实退金额 | `actualRefundAmount` 或 `actualRefundDetail.actualRefundAmount`；同时存在但不一致则失败 |
| 确认实退金额 | 仅 `refundStatus=20` 时，`confirmed_actual_refund_amount` 返回平台报告的实退金额；金额缺失仍为 `None` |
| 服务单主状态 | 保留原始值；`mainStatus=100` 不等于资金退款成功 |
| 时间 | 保留申请、修改毫秒时间，不伪造独立的退款到账时间 |
| 收货/出库状态 | 保留原始字段；“未收货仅退款”不等于“未发货” |
| 商品 | 保留所有 SKU、数量、商品类型与可选备件码；不把备件码当 ERP 型号、颜色或已完成的仓库核验 |
| 运单 | 保留全部运单；类型 2 为消费者退货，3 为商家二次发货；不把二次发货当原订单发货，也不只取第一包裹 |
| 未知枚举/缺失信息 | 保留未知或 `None`，不默认为可退款、未发货或质检通过 |
| 客户资料 | 不保留完整原始响应、客户姓名、地址、手机号；运单号等必要字段不进入对象 repr |

## 失败与恢复

网络错误、HTTP 错误、业务错误及协议校验失败均抛异常，不自动重试，不返回部分成功窗口，不写入数据库或推进游标。

异常只保留安全的 HTTP 状态与数字错误码，不回显服务器 `message/details` 或整个响应。错误码 `99904030005` 单独标记 `requires_yunding=True`，不自动切换地址或尝试绕过来源限制。

后续真实验收出现失败时，先区分网络、授权、来源限制和响应字段差异；确认问题已修复后，由明确授权的流程重新查询。窗口过大时缩小时间范围，不将失败窗口标记为完成。不要记录带认证头的请求或客户端内部对象。

当前未接入运行器，因此无需停掉正式后台或回滚数据库；不要为测试此模块更改正式开关。

## 离线验证

2026-10-07 验证结果：本模块 **95 项测试通过**，加上以下相关回归，共 **126 项通过**。Ruff 检查和格式检查均通过。

在仓库根目录执行（无需任何真实平台凭据）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_jd_official.py -o addopts='' -q
.\.venv\Scripts\python.exe -m ruff check src/aftersales_workbench/integrations/marketplace/jd_official.py tests/test_jd_official.py
.\.venv\Scripts\python.exe -m ruff format --check src/aftersales_workbench/integrations/marketplace/jd_official.py tests/test_jd_official.py
```

相关回归文件：`test_marketplace_clients.py`、`test_marketplace_signing.py`、`test_marketplace_mappers.py`、`test_marketplace_shops.py`、`test_marketplace_sync.py`、`test_jd_shipment_source.py`。

本机完整回归首次遇到 Windows 默认 pytest 临时目录权限错误，改用仓库 `.runtime/` 下新建的唯一空目录作为 `--basetemp` 后通过；未修改系统目录权限。若复现该问题，必须只指定新建的专用空目录：pytest 会清理该目录，**不得把工作区、用户下载目录或已有审计目录作为 basetemp**。

覆盖固定签名向量、原始参数编码、路径参数签名、无环境代理/重定向、跨店防护、完整分页、无实退误判、多 SKU/包裹、金额冲突、错误脱敏、无重试，以及正式工厂仍未切换到新客户端。

## 后续接入的前置验收

1. 在允许的云鼎运行环境中，另行执行明确授权的官方只读测试，核对应用授权、商家归属、实际列表/详情响应、分页和金额。当前离线测试不能证明这一步成功，也不能绕过执行工具的限制。
2. 确认店铺映射、SKU/ERP 映射、状态和金额缺失处理，再实现数据库归一化、事务与游标适配；目前只读客户端不具备这些能力。
3. 对指定店铺小范围切换同步并观察；未验收前保留既有生产链路，不混用两套凭据。
4. 20 小时物流提醒和退款模块分别验收。只读售后查询通过不代表具有退款权限，也不等于可以开启自动退款。

文档依据：京东官方 [listAfsOrders](https://open.jd.com/v2/#/doc/apiAuthPackage?apiCateId=3797&apiId=100246&apiName=listAfsOrders&gwType=1)、[getAfsOrder](https://open.jd.com/v2/#/doc/apiAuthPackage?apiCateId=3797&apiId=100237&apiName=getAfsOrder&gwType=1)，以及官网公开开发指南（文档编号 `1100604` 签名指南、`1100587` SP-API 调用指南），于 2026-10-07 核对。接口仍需以授权环境中的实际只读验收为准。
