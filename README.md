# 利德仕电商自动化售后工作台

面向利德仕多平台、多店铺的售后中台。当前仓库完成 **Phase 1 / Step 1**：FastAPI 工程骨架和全量数据库初始化。

## 当前边界

- 已实现：配置加载、MySQL 连接池、Alembic 全量建表迁移、存活/就绪健康检查、Docker Compose 本地编排、拼多多单店只读联调。
- 已建立全局表：`shops`、`aftersales_orders`、`aftersales_items`、`return_scrap_records`、`negative_reviews`。
- 未实现：拼多多写操作、企微 Webhook、ERP 适配器、仓库 PDA 业务接口；它们属于后续 Step 2–5。

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
| `PDD_READ_MAX_ATTEMPTS` | 只读请求最大尝试次数 | `3` |
| `PDD_WRITE_ENABLED` | 拼多多写操作开关，当前未开放 | `false` |
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

只读请求在网关 HTTP 429/5xx 或网络异常时指数退避重试；返回平台业务错误时不重试，保留 `error_code` 和 `request_id` 供排查。当前命令不会调用 `pdd.refund.agree` 或任何写接口。

## 健康检查

- `GET /health/live`：进程存活，不访问外部依赖。
- `GET /health/ready`：执行 `SELECT 1` 验证数据库；失败时返回 HTTP 503。

## 验证

```powershell
ruff check .
pytest
```
