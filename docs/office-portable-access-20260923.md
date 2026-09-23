# Windows 可选组件安装停滞时的临时部署入口

2026-09-23 新机两次安装 OpenSSH Windows 可选组件都长时间停在 Running，正常重启后未恢复。用户提供的 CBS 日志显示 10:35:45 下载 247073875 字节完成，Windows Update 在 10:35:50 记录事件 41；没有后续安装成功/失败证据，尚不能确定根因。新机还未迁移业务，旧正式机继续运行。

## 入口与边界

`scripts/office-portable-access.ps1` 提供独立的临时维护入口。使用微软 Win32-OpenSSH 官方 `10.0.0.0p2-Preview` ZIP，固定 SHA-256 为 `23f50f3458c4c5d0b12217c6a5ddfde0137210a30fa870e98b29827f7b43aba5`，并在启动前检查 sshd、ssh-keygen 的微软有效数字签名。该上游包标记为 Preview，仅用于有人值守的临时连接，不将它作为已验收的正式服务。

包在开发机预先下载，包含脚本、启停 CMD、官方原始 ZIP、公钥和说明；没有私钥、客户数据、正式配置。个性化地址与交付包保留在 Git 忽略位置，不提交远端。按 `sync-automation-docs` 规则同步本说明及代码。

新机以本地管理员账户启动，文件保存于 `%ProgramData%\LdsAftersalesPortableAccess`，权限仅 SYSTEM/Administrators。独立主机密钥、授权文件、配置和日志不写入 `%ProgramData%\ssh`。仅对指定开发机单个私有 IPv4 放行程序的 TCP 22222，SSH 同时限定当前本地账户及来源地址、公钥认证，禁止密码、交互密码、端口转发和 PTY。不安装 Windows 可选组件，不注册 SSH 服务，不改原服务、原 SSH 配置、原防火墙规则及原安装状态，不停止 DISM、TrustedInstaller 或 Windows Update。

```powershell
# 预演校验参数、公钥格式和官方 ZIP 哈希，无系统修改。
.\scripts\office-portable-access.ps1 -DevelopmentAddress <开发机IPv4> -PublicKeyFile <公钥路径> -Archive <官方ZIP路径> -WhatIf

# 新机管理员执行，默认60分钟，最多60分钟。
.\scripts\office-portable-access.ps1 -DevelopmentAddress <开发机IPv4> -PublicKeyFile <公钥路径> -Archive <官方ZIP路径>

# 同一管理员账户在另一个窗口关闭临时入口。
.\scripts\office-portable-access.ps1 -Action Stop
```

保持启动窗口打开。进程实际监听后才输出 READY、账户、端口和新主机 SHA256 指纹；必须通过用户提供的指纹核验新主机，使用独立 known_hosts 记录，不复用旧机指纹。调试模式一次接受一个连接，结束后重启监听器；这不自动重试远端命令或资金请求。

正常到期或异常退出会结束自建监听进程并移除本轮专属防火墙规则；Stop 还可在核对进程路径、启动时间后结束遗留监听器。强关窗口或断电不能保证 finally 清理，必须用停止入口核对并清理；不要只凭窗口消失认定已关闭。再次启动保留同一主机密钥，来源、公钥或账户不同则拒绝覆盖。已有端口占用、并发运行、哈希不符、签名无效、配置验证失败均停止，不接管其他进程。

通过临时入口连接后，先只读采集组件状态、安装进程、CBS/DISM/Windows Update 日志和重启状态，再修复或重建正式部署服务。临时连接不等于正式业务部署；仍按迁移说明完成停写、最终一致性备份、幂等账本迁移及唯一执行端切换。

## 验证与当前状态

已完成官方 ZIP 哈希和微软签名核验、Windows PowerShell 5.1 解析与 Start/Stop WhatIf、输入拒绝测试，以及用真实 sshd 对脚本实际配置表达式执行 `-t`/`-T` 校验。确认限定账户来源、22222 端口、公钥认证及转发/密码禁用均生效于解析后的配置。开发机未开放监听、防火墙或安装服务；新机实际启动、主机身份及密钥登录仍待用户运行连接包后验证。

上游依据：[官方发布](https://github.com/PowerShell/Win32-OpenSSH/releases/tag/10.0.0.0p2-Preview)、[官方安装说明](https://github.com/PowerShell/Win32-OpenSSH/wiki/Install-Win32-OpenSSH)。相关：[新机迁移](office-machine-migration-20260922.md)。
