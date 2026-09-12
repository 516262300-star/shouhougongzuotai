# 正式机验收与日常备份

## 2026-09-12 实际验收结果

- 正式机 SSH、MySQL 自动服务正常；局域网工作台健康接口 HTTP 200。开发机本地业务启用开关均关闭。
- 已核对修复上线后两条真实企微通知：北京时间 14:56、14:59 完成，账本依次为 PasteStarted → SendPressed → Sent，数据库任务 SUCCEEDED、包裹凭证 Sent、无错误；对应业务周期通知阶段完成。本次没有创建测试通知或重复发送既有包裹。个案证据仅存受保护的 `.runtime/audits/`。
- 备份时安全等待后台当前周期结束，守护任务保持启用；备份后由原交互会话守护恢复 Worker，健康检查通过。此为后台停止及恢复验收，不等于整机重启验收。
- 通知队列待发 0、运行中 0、无发送阻塞。模块 1、2、3 总体 healthy；拼多多同步仍有 1 笔异常单隔离提示，正常同步继续，不将该提示当成已解决。
- 第一份日常备份于 15:15:42 完成；MySQL 21 张表、schema `20260910_0027`，数据库、账本、配置、监控库与运行代码校验通过。本机副本通过相同 SHA-256、JSONL、SQLite 和 ZIP 检查。
- 已将此备份导入独立 MySQL 实例（回环端口 33317、全新数据目录），21 张表的数量、schema、CHECK TABLE 全部通过。演练实例已关闭、临时验收任务已移除；没有恢复到正式库，没有启动演练业务后台。
- 整机重启验收待现场人员能重新登录 Windows 和企业微信时进行；本次没有重启机器。真正关闭开发机的物理实验也未执行。

## 正式机每天备份

任务 `LDS Aftersales Daily Backup`：每天 **04:30（运行机本地时间）**，SYSTEM 运行；错过时间后补跑，不要求开发机在线。已实际运行，LastTaskResult=0。

入口 `scripts/office-daily-backup.ps1`，调用 `office_verified_backup.py` 与 `office_state_snapshot.py`。部署参数：

```powershell
.\scripts\office-daily-backup.ps1 `
  -BackupRoot D:\LDSAftersales\backups\daily `
  -MySqlDump D:\LDSAftersales\mysql\bin\mysqldump.exe `
  -AdminClientFile D:\LDSAftersales\incoming\root-client.ini
```

支持 `-WhatIf`，预演不暂停后台、不写备份。管理员连接文件由受保护路径读取，不在命令行、日志或 Git 存放密码。

执行顺序：

1. 检查备份盘剩余至少 20 GB、守护已启用，取得原有 `module1-autostart-cycle.lock`，阻止守护与备份同时启动后台。
2. 若已有停止标记则退出，避免接管其他维护；否则发出自己的正常停止请求，等待完整 Worker 进程树结束，默认最多 15 分钟。超时不强杀业务进程。
3. 取得企微发送锁及 MySQL 全局读锁，复制数据库与防重账本。数据库写入在短暂快照期间等待；监控 SQLite 使用在线备份。首次数据快照及打包约 3 秒。MySQL 导出设置 180 秒超时，读锁随连接释放。
4. 保存正式 `.env`、当前 Web/Worker 发布指针、运行日志、可用的令牌缓存，以及 `release-code.zip`（当前两套源代码、前端构建、脚本、迁移及项目元数据）。不包含 Python/MySQL 安装程序、整个历史审计目录或所有旧版本。
5. 检查逐文件 SHA-256、账本可解析、SQLite 完整性、ZIP CRC；全部通过才写 `backup-complete.json` 并更新 `latest.json`。
6. 正常或异常退出均撤销自己的停止标记、释放守护锁，并请求原有交互会话守护恢复。守护从不被禁用。无登录会话时业务仍须等 Windows 登录后恢复。

备份目录为 `D:\LDSAftersales\backups\daily\daily-时间`，状态文件为 `D:\LDSAftersales\app\.runtime\office-backup-status.json`。失败保留上次成功的 `latest.json` 和已有备份；当前不自动删除历史备份。检查任务结果及状态文件，空间不足时先安排受控归档，不能删除唯一可用副本。

首次完整备份和后来发布的脚本可能版本不同；代码更新仍按正常发布管理。部署、数据库迁移及人工恢复应避开备份或取得同一维护锁，不要在复制代码时同时改发布目录。

## 开发机自动保存异机副本

任务 `LDS Aftersales Backup Pull`：当前用户登录时，以及每 **2 小时**运行一次，Interactive/Limited；已实际运行，LastTaskResult=0。

入口 `scripts/office-backup-pull.ps1`。SSH 使用原来固定主机身份、公钥认证和限制来源的连接。任务参数包含运行机地址、远端备份根目录、公钥认证私钥路径、known_hosts 路径及本机目标目录；不包含业务密码。

本机目标为项目下 `.runtime/office-deployment/backups`，ACL 限当前用户、SYSTEM 和 Administrators。通过 SSH 传输最新成功的完整备份，先下载到独立临时目录，校验通过才发布本机副本；相同备份只复核，不重复复制。失败临时目录保留，不覆盖成功副本。

开发机在家、关机或网络不通时不会有新的异机副本；正式机每天备份继续执行。下一次开发机开机且能连接办公室时，再复制最新备份。这里只拉取最新成功快照，不逐一追补离线期间的全部每日历史。状态为 `pull-status.json`；无法连接记为 deferred，传输或校验失败记为 failed。

两机副本都含业务和凭据，依赖目录访问权限保护，未宣称做磁盘静态加密；不要提交 GitHub、普通共享盘或复制进代码包。这是每日快照，不是实时数据库复制；正式机每日备份最坏可能丢失上次成功备份之后的数据，异机副本还取决于最近一次成功连接时间。

## 恢复与复查

只验证已有备份：

```powershell
.\.venv\Scripts\python.exe scripts\office_verified_backup.py --output <备份目录> --verify-only
```

验证实际可恢复性：

```powershell
.\.venv\Scripts\python.exe scripts\office_backup_restore_check.py `
  --backup <已校验备份目录> --mysql-bin <MySQL程序bin目录> `
  --work-dir <不存在的受保护演练目录> --port 33317
```

恢复检查只允许 33300–33399 端口和全新目录；连接后核对实际 datadir/port，才导入和关闭自建实例。输出 `restore-result.json`，保留演练数据供审计，不做递归清理，不连接正式 3306 端口。不运行 `.env` 内的业务开关或任何发群/退款动作。

真正灾难恢复需先停止正式业务写入，将数据库、发送账本、配置、兼容版本一起恢复；重新核对所有未知发送/资金结果，不能清空防重记录直接重跑。先在隔离环境核验，再按原部署流程恢复正式运行。

## 代码验证与说明同步

6 项针对性测试通过：正常校验保留结果不明账本、数据库损坏拒绝、清单越界拒绝、缺少关键文件哈希拒绝、正式端口拒绝、既有演练目录拒绝。PowerShell 5.1 语法/WhatIf 及两端真实计划任务执行通过；异机副本和独立恢复均完成。对应使用说明、部署说明已更新，随脚本提交 GitHub；本次没有可访问的 Notion 目标，未声称同步 Notion。
