# 利德仕电商自动化售后工作台

面向利德仕多平台、多店铺的售后中台。当前仓库完成 **Phase 1 / Step 1**：FastAPI 工程骨架和全量数据库初始化。

## 当前边界

- 已实现：配置加载、MySQL 连接池、Alembic 全量建表迁移、存活/就绪健康检查、Docker Compose 本地编排。
- 已建立全局表：`shops`、`aftersales_orders`、`aftersales_items`、`return_scrap_records`、`negative_reviews`。
- 未实现：拼多多 API 适配器、企微 Webhook、ERP 适配器、仓库 PDA 业务接口；它们属于后续 Step 2–5。

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

生产环境不得使用示例密码，也不得将 `.env`、店铺 Secret 或 Token 提交到 Git。

## 健康检查

- `GET /health/live`：进程存活，不访问外部依赖。
- `GET /health/ready`：执行 `SELECT 1` 验证数据库；失败时返回 HTTP 503。

## 验证

```powershell
ruff check .
pytest
```
