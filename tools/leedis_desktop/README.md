# ERP 登录客户端维护

本目录基于用户提供的 `客户端登录/Leedis-Windows/源码/网页登录客户端` 保存客户端源码。保留原 PKCE 登录、Windows 凭据管理器、单实例锁、续期、退出和窗口流程；`session_bridge.py` 与 `workbench_bridge.py` 的 `/session` 是本次售后工作台接入扩展。

## 构建与验证

在独立 Python 3.12 环境安装 `requirements-build.txt`，不要修改正式工作台虚拟环境。

```powershell
python -m pip install -r tools/leedis_desktop/requirements-build.txt
python -m unittest discover -s tools/leedis_desktop -p 'test_*.py'
python -m PyInstaller --noconfirm --onefile --windowed --name LeedisClient --hidden-import keyring.backends.Windows --collect-all certifi --distpath .runtime/client-build/dist --workpath .runtime/client-build/build --specpath .runtime/client-build tools/leedis_desktop/desktop_app.py
```

在目标机部署为 `LeedisClient-Workbench.exe`，快捷方式和当前用户登录自启动均指向此同一个文件，参数 `--server https://ldswj.net`。构建输出、凭据、桥接 JSON、浏览器回调和真实授权证据不提交到 Git。升级应等待客户端网络操作结束后正常关闭窗口，再替换文件和启动，避免终止正在轮换的续期请求。

`/session` 仅供本机售后进程使用，不增加局域网端口，不接受网页请求，不提供访问令牌或续期令牌导出功能。`/command` 沿用原登录/打开能力，原“打开系统”工作台命令还依赖专用 ERP Chrome；售后后台的无密码接入不依赖浏览器调试端口。

运行配置、失败恢复和安全边界见 [ERP 客户端接入说明](../../docs/erp-desktop-login-20260929.md)。
