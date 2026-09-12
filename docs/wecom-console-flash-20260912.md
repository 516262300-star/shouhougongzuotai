# 正式机守护任务黑窗闪现修复

2026-09-12 用户反馈正式机偶尔出现黑色命令行窗口，随后消失。检查发现 `Leedis Aftersales Module1 Watchdog` 直接启动 `powershell.exe -WindowStyle Hidden`，在当前 Windows Terminal 默认终端环境下仍会先显示窗口。实际桌面会话基线观察中，主动触发一次原守护任务后约 2.079 秒捕获到可见的 Windows Terminal 控制台窗口。

## 修改

- 新增 `scripts/module1-autostart-hidden.py`，由 `.venv\Scripts\pythonw.exe` 启动，再以 `CREATE_NO_WINDOW` 创建 PowerShell 进程。仍执行原来的 `module1-autostart.ps1 -Action Run`，子进程退出码传回计划任务；输出和异常记录到 `.runtime/module1-autostart-launcher.log`。
- 正式机只替换已有守护任务的 Action，保留账户、Interactive/Limited 会话、登录和每五分钟触发、设置与授权范围。不停止或重启正在运行的 Web、Worker，也不修改企微发送规则或业务数据库。
- `module1-autostart.ps1` 的后续安装入口同步使用无控制台启动器，启动目录回退方式也使用同一入口的 `--action Watch`；缺少 pythonw 或启动器时，在改写配置前拒绝安装。
- `build_office_release.py` 将新启动器纳入代码包，避免重新部署时遗漏文件。SYSTEM 会话中的每日备份任务保持原设置，不属于本次桌面闪窗来源。

## 验证

- 24 项测试通过：22 项原有启动恢复检查，以及真实 PowerShell 无控制台句柄/非零退出码传递、缺少脚本拒绝启动两项检查。
- 更新后在同一实际桌面会话再次触发同一个守护任务，18 秒窗口观察中可见控制台数量为 0；任务 LastTaskResult=0，16:06:02 守护 healthy。
- Web 和 Worker 的进程 ID 与更新前一致，业务没有因修复重启。未额外发群消息或重试退款。本次没有再次做整机重启实验。
- 实际生成代码包，共 582 个清单文件，并核对包含新启动器及对应哈希。两次窗口观察结果、原任务 XML 和原脚本备份存于受保护且 Git 忽略的 `.runtime/audits/console-flash-20260912`。

现有日志与守护状态仍使用原位置；若启动器失败，检查任务退出码与 `module1-autostart-launcher.log`，不要以关闭守护来消除窗口。需要回退时使用审计目录中的原任务 Action 和脚本，保留既有业务进程及数据库。
