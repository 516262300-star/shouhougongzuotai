# 正式工作台桌面入口随地址变化恢复

2026-10-01，正式运行机 `DESKTOP-H91PVUK` 仍在线，但 IPv4 从 `192.168.2.46` 变为 `192.168.3.21`。原桌面网址与异机备份配置使用旧地址，访问超时。通过电脑名称解析到新地址，并用原已登记的 SSH 主机密钥严格校验，确认是同一台正式运行机；新地址的网页和数据库就绪检查正常。

电脑名称直接作为网页网址时，本机代理及 IPv6 连接未能正常完成，因此不修改全局代理、IPv6 或防火墙设置。桌面“售后工作台-正式版”改用 `scripts/open-office-workbench.py`：只解析指定电脑名称的 IPv4，检查现有服务的 `/health/ready`，成功后使用系统默认浏览器打开当前 IPv4 网址。不会启动本机业务、创建数据库或重启正式后台，也不执行退款、补单或发送消息。连接失败时显示错误，不回退到本机测试数据。

快捷方式使用项目虚拟环境的 `pythonw.exe` 隐藏运行：

```powershell
.\.venv\Scripts\pythonw.exe scripts/open-office-workbench.py --host DESKTOP-H91PVUK
# 仅验证解析与网页/数据库就绪，不打开浏览器：
.\.venv\Scripts\python.exe -X utf8 scripts/open-office-workbench.py --host DESKTOP-H91PVUK --check
```

旧桌面网址及变更验收保存在 Git 忽略的 `.runtime/audits/office-entry-repair-20261001/`。回退入口前须重新核实旧网址仍可访问，不能直接恢复已失效的固定地址。运行机自身的 `AfterSales-Local` 入口仍使用 `http://127.0.0.1:8000/`。

同一地址变化也阻断了开发机异机备份拉取。原 `LDS Aftersales Backup Pull` 任务改为按电脑名称连接，保持原登录及每两小时触发器和隐藏运行方式。`office-backup-pull.ps1` 对 SSH/SCP 使用 IPv4，并支持 `-HostKeyAlias` 指定旧 known_hosts 中已核验的身份名称；本机配置继续绑定原 `192.168.2.46` 的主机密钥，不关闭严格校验、不自动信任新密钥。该参数仅影响身份查找，实际网络地址来自 `-RemoteHost`。连接失败仍记录 deferred，等待既有下一次计划；没有增加计划任务。

验收：入口 `--check` 成功返回新地址，网页首页、JS/CSS 资源、售后列表接口及数据库就绪检查通过，已打开工作台。Python 编译、Windows PowerShell 5.1 解析和 8 项备份相关测试通过。原计划任务在北京时间 08:32 执行成功，已拉取并校验 `daily-20261001-043611`，包括 24 张表、13 项文件及 schema `20260930_0030`；任务结果为 0，原触发器、运行身份和任务设置逐项保持一致。
