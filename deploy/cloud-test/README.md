# 阿里云宝塔工作台只读测试部署

此包用于已有网站的 Alibaba Cloud Linux 3 服务器。独立 Compose 项目为
`lds-aftersales-test`，API 绑定 `127.0.0.1:18000`，MySQL 8.4 不发布宿主机端口，
独立数据库为 `aftersales_test`，独立数据卷为 `lds-aftersales-test_mysql_data`。
现有 Apache、Node、Django 容器及其 80/8000/8001/8080 端口无需修改或停止。

本阶段交付网页、API、空数据库和数据库迁移。没有后台 worker、自动同步、退款、
ERP 写入或企微发送；不读取或上传本机 `.env`、数据库、令牌缓存、发送账本。
云端入口额外拒绝 POST/PUT/PATCH/DELETE，前端原有编辑按钮可能显示，但提交会返回 403。
页面首次显示零订单和后台未运行属于预期结果，不代表已经迁移历史数据或接入店铺。

## 构建部署包（开发机）

在仓库根目录执行：

```powershell
./.venv/Scripts/python.exe scripts/build_cloud_test_bundle.py
```

前置条件：Python、Node.js/npm 和 `frontend/node_modules` 已安装。
脚本执行 `vite build`，仅打包 Python 源码、迁移、前端静态产物与本目录部署文件，
输出 `dist/cloud-test/lds-aftersales-test-日期.tar.gz` 和 SHA-256 校验文件。
不使用旧的 `sites-deploy.tar.gz`，也不修改或发布 Sites 配置。
包内 `SOURCE_MANIFEST.json` 记录基础提交、工作区状态及逐文件哈希；这是当前工作区快照，
可能含尚未提交的业务/UI 改动，不应把基础提交号理解为全部文件已提交的版本。

## 首次安装（服务器）

1. 在宝塔“文件”中新建独立目录 `/www/wwwroot/aftersales-test`。
2. 将部署包和 `.sha256` 文件上传到该目录。首次使用应选择空目录，避免覆盖既有项目。
3. 在服务器终端执行（以 20260908 包为例）：

```bash
cd /www/wwwroot/aftersales-test
sha256sum -c lds-aftersales-test-20260908.tar.gz.sha256
tar -xzf lds-aftersales-test-20260908.tar.gz
bash deploy/cloud-test/start.sh
```

`init-env.sh` 使用 openssl 生成两组随机数据库密码，保存在本目录 `.env`，权限 600。
已有 `.env` 保留原值，不重新生成密码；不要把它提交到 Git 或贴入聊天。
Compose 只注入数据库连接及关闭写入的变量，不透传开发机业务 `.env`。
必须从本目录使用本 `compose.yaml`，不要误用仓库根目录旧的开发版 Compose。

启动顺序为 MySQL 健康检查 → `alembic upgrade head` → API。迁移失败时 API 不启动。
当前迁移头为 `20260908_0022`。首次构建会从 Docker Hub、PyPI 下载镜像和 Python 包；
若网络超时，保留完整错误，处理镜像/网络访问后重新执行启动脚本，不修改其他项目镜像源。
MySQL RSA 认证所需依赖在 Dockerfile 中通过 `PyMySQL[rsa]` 安装。

## 验收与访问

```bash
cd /www/wwwroot/aftersales-test/deploy/cloud-test
docker compose ps -a
curl --fail http://127.0.0.1:18000/health/ready
curl -I http://127.0.0.1:18000/
```

预期：mysql/api 为 running（随后 healthy），migrate 为 Exited (0)，
ready 返回 `{"status":"ok","database":"ok"}`，首页返回 200。
如果 API 尚在初始化，稍后重试 readiness；持续失败时执行：

```bash
docker compose logs --tail=100 mysql migrate api
```

首次从 Windows PowerShell 通过 SSH 隧道访问（在自己电脑执行）：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18000:127.0.0.1:18000 root@47.111.21.141
```

需要使用自己已有的 SSH 登录方式，密钥登录时按实际情况增加 `-i`。
首次连接应核对服务器指纹。保持窗口打开，用浏览器访问 `http://127.0.0.1:18000/`。
若电脑本机 18000 已占用，将 `-L` 第一个端口改为 18001，再访问本机 18001。
此阶段不建立公开 Apache 代理、不开放 18000/3306 的安全组规则。
服务器已有 Docker 26；Docker 官方说明 28 之前版本的 localhost 端口映射存在同二层网络
访问例外，因此还应保持云安全组的 18000 入方向关闭，不能只依赖回环地址作为访问保护。

## 运维、备份与恢复

以下命令均在 `deploy/cloud-test` 目录执行。没有新增系统定时任务；
API/MySQL 使用 `restart: unless-stopped`，Docker 服务恢复后会重启。
生产自动化仍在原 Windows 机器运行，本测试版不参与执行。

```bash
# 停止/恢复此测试项目
docker compose stop
docker compose start

# 备份测试数据库；命令中的变量在 MySQL 容器内展开
mkdir -p backups
chmod 700 backups
umask 077
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u"$MYSQL_USER" --single-transaction --no-tablespaces "$MYSQL_DATABASE"' > "backups/aftersales-test-$(date +%Y%m%d-%H%M%S).sql"
```

必须确认备份命令退出码为零并核验文件非空；敏感备份应另存受控位置，不能仅留在试用服务器。
试用到期前备份数据库和 `deploy/cloud-test/.env`。后者决定已有 MySQL 卷的访问密码，
不能通过删除 `.env` 后重新生成密码来重置已有数据库。

恢复 SQL 会写入测试库，先停止 api，确认备份文件来源和目标数据库后再执行：

```bash
docker compose stop api
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u"$MYSQL_USER" "$MYSQL_DATABASE"' < backups/已核验的备份.sql
docker compose run --rm migrate
docker compose start api
```

不要运行 `docker compose down -v`，它会删除测试数据卷。回退应用镜像前应备份数据库，
确认旧代码兼容当前迁移；不要自动执行降级迁移。导入本机历史订单和接入实时同步属于后续阶段，
需单独处理数据复制、授权、水位与 Windows 执行端衔接，不能直接把本机整份 `.env` 上传启用。

## 验证范围与文档同步

部署改动独立于本机业务代码。验证包括前端构建、只读入口对写请求的拦截、静态页面和存活端点、
Compose YAML 结构、Shell 语法及部署包清单/哈希。本开发机没有 Docker Engine，不能在本机
完成 Linux 镜像构建、MySQL 容器迁移和服务器就绪验收；以上三项以上传后的服务器输出为准。

本说明与部署脚本同步提交 GitHub。既有 Notion 说明没有可用连接器，本次未同步。

参考：[Compose 启动依赖](https://docs.docker.com/compose/how-tos/startup-order/)、
[Docker 端口映射](https://docs.docker.com/engine/network/port-publishing/)。
