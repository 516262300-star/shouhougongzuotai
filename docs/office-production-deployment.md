# 办公室正式运行机与开发机分离

## 目标与当前阶段

正式运行机独立承载 MySQL、工作台网页、业务后台、企业微信和登录守护。开发机只负责开发、测试及发布；开发机关机不能影响正式业务。正式数据集中保存，开发使用独立测试库和关闭外部写入的配置。GitHub 只交换代码及说明，测试数据不回灌正式库。

当前只准备了部署连接工具 `scripts/office-deployment-access.ps1`，没有迁移数据库，没有停止旧后台，也没有声称新机已接管。现有未提交业务改动不进入部署连接包。

## 部署连接工具

Windows PowerShell 5.1 或更高版本。首次安装在正式运行机的本地管理员账户下以管理员身份执行。需要联网安装 Windows OpenSSH Server 可选功能；不依赖 Python、Git 或 MySQL。

```powershell
# 默认仅检查，不安装、不写入
.\scripts\office-deployment-access.ps1 -Action Inspect

# 管理员运行：只读采集服务、版本、目录 ACL 和最近错误，TXT 保存在脚本旁
.\scripts\office-deployment-access.ps1 -Action Diagnose

# 先验证参数并预演；不会安装、修改配置或启动服务
.\scripts\office-deployment-access.ps1 -Action Install -DevelopmentAddress <开发机IPv4> -PublicKeyFile <公钥文件> -WhatIf

# 正式运行机建立连接
.\scripts\office-deployment-access.ps1 -Action Install -DevelopmentAddress <开发机IPv4> -PublicKeyFile <公钥文件>

# 在正式运行机关闭部署连接，保留配置
.\scripts\office-deployment-access.ps1 -Action Disable
```

工具只接受单个私有 IPv4 地址和单条 Ed25519 公钥。检测到不属于本工具的 SSH 服务、配置、防火墙规则、部署目录或 22 端口占用时停止，不接管既有远程管理设置。部署连接包只包含脚本、启动入口、说明和公钥；私钥单独保存在开发机 Git 忽略的 `.runtime/`，限制文件权限，不能随包发送或提交 Git。

安装会创建或修改以下本工具管理的内容：

- Windows OpenSSH Server 可选功能及 `sshd` 服务；成功后自动启动，以支持运行机重启后的代码维护。
- `%ProgramData%\LdsAftersalesDeployment\access-state.json`：来源标记、开发机地址、公钥文件哈希、账户和安装状态。配置改写前的副本也保存在此目录，权限限 SYSTEM 和 Administrators。
- `%ProgramData%\ssh\sshd_config` 与 `lds_aftersales_authorized_keys`：仅指定本地账户从指定开发机 IP 以公钥认证登录；关闭密码登录、代理转发、TCP 转发和交互 PTY。此入口允许以该管理员账户执行部署命令及传输文件，应保管好开发机私钥。
- `LDS-Aftersales-Deployment-SSH` 防火墙规则：仅对指定开发机地址允许 OpenSSH 进程入站 TCP 22。禁用新安装产生的通用 `OpenSSH-Server-In-TCP` 规则；不关闭 Windows 防火墙，不开放整个局域网。

SSH 主目录与 `logs` 目录在启动前先保存 ACL 副本，再设置 SYSTEM/Administrators 完全控制，以及 Authenticated Users 对本层目录的只读和遍历权限；目录只读权限不继承到主机私钥或授权文件。不修改 `%ProgramData%` 父目录权限。配置、公钥授权文件和部署状态目录仍仅 SYSTEM/Administrators 可访问。

参数相同可以重新执行；地址、公钥或账户不同会停止，避免静默替换部署授权。开发机 DHCP 地址变化后需要在正式运行机本地核对再调整，不能临时扩大到整个网段。后续宜在路由器设置两台电脑的 DHCP 地址保留。

安装中途失败会停止并禁用受管 SSH 服务、禁用对应规则，保留错误和配置供排查。Windows 可选功能可能已经安装，不能把失败理解为完全没有系统变更。提示重启时先重启登录，再用同一个包重试。`Disable` 同样停止服务并禁止自动启动，但不卸载可选功能、不删除主机密钥，也不影响未来部署的 MySQL 或售后进程。再次执行相同 `Install` 可以恢复受管连接。

首次连接先从正式运行机安装窗口核对主机公钥指纹，再写入开发机专用 `known_hosts`。不禁用主机身份校验、不传 Windows 密码。连接成功后只读确认电脑名、账户、软件、磁盘及现有服务，再安装匹配当前发布版本的业务依赖。

## 后续业务迁移顺序

1. 清点实际在运行的 Web/Worker 发布指针、数据库迁移版本、Python 与桌面 OCR 依赖、配置来源及所有独立状态库。数据库不止要看主 MySQL；监控状态、发送本机账本和未完成动作也应清点。
2. 在正式运行机准备可重建的稳定代码、环境和前端构建。先关闭全部业务外部写入；不复制开发机 `.venv`、PID、锁文件、机器相关自启动 JSON 或运行中的 `.mysql-data`。
3. 新机基础检查通过后，停掉旧守护并等待旧后台当前周期结束；停止所有会写正式状态的旧进程，再做最终一致性数据库备份及本地账本快照。保留订单 ID、同步游标、水位、包裹投递记录和结果不明记录，不清空账本恢复发送。
4. 恢复并核对正式数据库及其他状态，重建新机器路径、登录启动入口和守护。仅保持一个正式业务执行端，旧机防重文件锁不提供跨机器互斥。
5. 企业微信 GUI 不能在 SSH 的后台会话中验收。必须由正式运行机已登录且未锁屏的 Windows 桌面会话启动企微及其发送器，核对完整群白名单、OCR、待发任务及未知结果；真实业务按既有授权范围接管，不为测试重复发群、退款或清账本。
6. 配置局域网 Web 访问；现有管理写接口的本机限制不能直接移除，远程管理功能需配套认证和权限。正式 MySQL 可继续仅监听运行机本地，不向所有办公电脑开放。
7. 开发机保留关闭外部写入的测试环境，通过正式网页查实时结果。新版经验证、提交 GitHub 后，以指定版本更新正式机；涉及 schema 时配套备份、迁移和兼容性检查。回退不得覆盖升级后新增的业务数据。
8. 验收开发机关机时新机仍能同步并执行、正式机重启登录后守护恢复，且旧机不会自动重新加入正式运行；建立正式数据和本地账本的受控备份。

## 验证与外部说明

部署连接工具需执行 PowerShell 5.1 语法检查、Inspect、WhatIf 和非法参数检查；这些不代表已在目标电脑实际安装。目标安装、主机指纹、SSH 实际登录及业务迁移必须分别记录，不能用静态检查冒充验收。

本次已完成语法解析、开发机 Inspect、Windows PowerShell 5.1 的 Install/WhatIf，以及 7 类非法地址和错误密钥输入检查；预演未创建系统部署状态。连接包逐项核对只包含两个命令入口、连接脚本、使用说明和公钥，未包含私钥或业务凭据。目标机实际安装与连接验收尚待用户运行连接包。

目标机首次运行已生成主机密钥，但在 `Start-Service sshd` 失败，尚未连通。更新版补上上述目录 ACL 初始化，并在失败关闭后自动保存 `deployment-diagnostics-时间.txt`；亦提供独立 `3-Diagnose-Deployment.cmd` 入口。报告仅采集操作系统版本、服务启动配置/退出码、程序版本、目录与配置 ACL、22 端口监听和最近45分钟 OpenSSH/相关系统错误，不读取密钥正文、密码或业务 `.env`。本机 ACL 对象检查验证目录只读规则不向子项继承，配置与授权文件没有普通用户规则；实际服务能否启动仍待目标机验证。

目前只确认服务启动失败，不能仅凭通用错误判定根因。目录权限是已发现的初始化缺项，也是微软记录的可能原因，参见[OpenSSH 服务启动失败与目录权限](https://learn.microsoft.com/en-us/troubleshoot/windows-server/system-management-components/error-1053-1067-7034-after-update-openssh-doesnt-start)。失败重跑使用原公钥和原状态，不卸载组件、不重建已存在的主机密钥、不清理售后数据。诊断模式只写报告，不启动服务或改变 ACL/防火墙。

更新版已通过 Windows PowerShell 5.1 语法/预演、目录与文件 ACL 对象检查，以及开发机只读诊断报告生成检查（六类诊断章节完整）。新包核对公钥与原包字节一致、脚本与仓库一致，未含私钥。检查没有在开发机安装或启动 SSH，也没有在目标机执行更新版。

通用脚本和本文随任务提交 GitHub。个人地址、公钥、私钥及安装包放在 Git 忽略的本机目录。已有 Notion 说明本会话没有可用连接器，本次不宣称同步 Notion；部署进度以本文件和实际验收记录为准。

依据：[Microsoft OpenSSH 安装说明](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)、[Windows OpenSSH 配置](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)、[密钥认证与权限](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement)。
