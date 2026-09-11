# 本机网页独立发布（2026-09-11）

本次只发布 Web 接口和前端，退款 worker 继续使用原已固定的安全版本。异常明细使用原业务库只读查询，原运行目录的日志、发送账本和监控观察历史；不新增资金动作、补单、群发送或解除人工锁。历史失败任务不得批量重置重试。

## 本次修正

- 空对象、缺少计数/分页/状态、异常 JSON、HTTP 失败不再显示成“没有异常”；12 秒读取超时后保留上次结果并提示过期，等待下一次展示刷新。
- 同售后号但明确不同店铺的异常不跳转到另一店的订单。未知结果不提供重试资金按钮。
- Web 发布代码与运行资料目录分离，监控和发送前安全恢复入口读取同一套真实账本。配置路径错误时抛错，不能读空目录冒充没有异常。
- 保留工作区已有的退款趋势需求：按申请日期归组，蓝绿为同批成功金额、浅橙为差额，金额后端和图形一并验证。详情保持 ERP 核验时间标签和独立物流查询时间。
- 不包含工作区另行开发的周期任务、环境开关和 ERP 详情状态改动，不切换退款执行版本。

## 启动和回退

`scripts/module1-autostart.ps1` 读取 `.runtime/workbench-web-release.json` 的 `source_path`。目录必须存在于 `.runtime/releases/`，同时包含 Web 入口、`core/runtime_paths.py` 和同一版本的 `frontend/dist/client/index.html`；配置存在但不完整时禁止回退开发代码。首次尚未配置版本的旧安装沿用原启动方式。

隐藏启动时设置 `PYTHONPATH` 和 `--app-dir` 指向验证后的源码，并设置 `AFTERSALES_RUNTIME_ROOT` 为原项目绝对目录；启动完恢复父进程环境变量，不把网页环境传给 worker。工作目录仍是原项目，以读取原 `.env`。前端由源码版本目录提供，不能直接覆盖在线开发目录产物。

发布前备份旧版本指针、守护配置、Web 入口脚本和前端；观察 SQLite 使用 SQLite backup API 一致性备份，保留业务账本。先在独立端口验证只读候选，再停止守护检查循环（`-Action StopWatch`，不停止业务 worker），核对 Web PID、完整命令和端口后切换网页，最后恢复守护（`-Action StartWatch`）。不迁移业务数据库。

回退必须选择已验证的配套后端/前端源码快照或恢复此次备份的旧网页启动方式；仅改浏览器缓存或删除版本指针不是完整回退。版本切换不覆盖 `.runtime/monitor-incidents.sqlite3`，不回退资金和发送账本，也不清理历史失败队列。

## 验证方法

在候选目录运行，`PYTHONPATH` 指向其 `src`；测试使用隔离临时目录，不能连接生产库写入：

```powershell
python -m pytest tests/test_runtime_issues.py tests/test_runtime_monitor_api.py tests/test_desktop_notice_recovery.py tests/test_integration_capabilities.py tests/test_refund_attribution.py tests/test_aftersales_records_api.py tests/test_web_runtime_paths.py tests/test_web_release_script.py
python -m pytest tests/test_autostart_recovery.py
# frontend 目录
npm run build
npm run test:sites
node --test tests/monitor-issues-response.test.mjs
```

自启动测试中的数据库别名恢复要求英文路径，需使用新的英文临时目录；配置夹具显式 UTF-8，避免中文路径被 PowerShell 默认编码破坏。此限制不意味着把中文生产路径改成空数据库。

本机 npm 包装器路径异常时，使用已安装 Node 和 npm CLI 的绝对路径执行同一个 `run build`，不安装依赖、不改锁文件。保留 Sites 兼容产物，本次只发布本机，不发布外网。

接口检查包括 `/health/ready`、首页和静态资源、`/api/v1/monitor/status`、`issues`、`capabilities`、七类筛选、明确无匹配结果、错误参数拒绝以及售后详情定位。真实业务库检查通过 SQL 执行拦截器禁止非查询语句；仅独立监控 SQLite 记录观察历史。订单、原响应、截图或配置备份只保存在 Git 忽略的 `.runtime/audits/web-release-20260911/`。

界面测试和 API 测试须分别记录，不以打开预览或构建成功冒充浏览器点击验收；网页发布也不等于整套售后自动化已通过无人值守验收。

## 本机发布结果

2026-09-11 11:30（Asia/Shanghai）已完成独立 Web 切换。应用安全基线为 `5926a0e`，本次 Web 代码提交为 `cbd2122`，运行目录为 `.runtime/releases/web-monitor-20260911/`，生产版本指针记录代码提交与前端索引哈希。主开发分支对应实现提交为 `921b4bd`。

- 61 项 Web/状态/店铺边界/退款统计相关 Python 回归通过；21 项自启动回归通过。3 项前端响应校验与超时测试、4 项 Sites 打包测试通过，生产构建通过。首次打包测试在产物尚未生成时失败，完成最终构建后复测通过；自启动夹具先修正 UTF-8，再使用符合已有英文数据库别名规则的隔离临时目录复测通过。
- 真库只读验证覆盖健康、监控、能力、七类筛选、空匹配、参数拒绝和对应售后详情，禁止非查询 SQL。在线首页、JS/CSS 均与候选文件哈希一致，生产接口返回 200。
- 旧 Web PID 17836 已切换为 48792；守护自然退出后恢复，检查状态 healthy。业务 worker 始终为 PID 50192，版本指针哈希与发布前相同；未停止 worker、迁移业务库或重置失败任务。
- 资金幂等记录总数仍为 2540；后台持续核验期间个别记录转为已确认，未据此解除其余 UNKNOWN 保护。已核验发送任务 3469、3478 仍为 SUCCEEDED/Sent，本次没有调用发送、退款或补单接口。持续运行的后台可能接入新业务，不能要求业务统计在网页发布期间静止。
- 独立只读预览已做 HTTP 验证并向应用提交打开请求；尚未完成浏览器点击和截图验收。异常到订单定位已有源码及真实接口验证，不能写成浏览器交互已验收。
- 原配置、入口脚本、旧前端、监控 SQLite 一致性备份及验证原响应存放于本机忽略目录。Notion 无可用连接器，本次同步仓库文档，不宣称更新 Notion。
