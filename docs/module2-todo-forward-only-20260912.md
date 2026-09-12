# 仓库验货异常待办：修复批次阻塞，仅发送新记录

## 行为与范围

2026-09-12 修复模块2异常待办生成：在数据库分页前排除已有相同幂等键的任务，再取本批新任务。原来每轮只取最早20条，随后才跳过已有待办，导致新验货异常长期无法入队。已发送、已取消、失败或发送中的任务均不因本次修复重建或恢复。

普通验货待办与平台退款后的异常申诉保持两个独立幂等键，按平台退款成功状态选择；生成本地待办不表示退款、验货通过或ERP平账完成。原业务员归属核对、发布总开关、快递100无轨迹排除和资金保护不变。

新增 `MODULE2_TODO_MIN_RETURN_ID`，默认0，必须是非负整数。后台只为收货记录ID不小于该值的模块2异常生成待办。它与 `MODULE2_REFUND_MIN_RETURN_ID` 相互独立，不改变退款范围。

本机按用户“老的不要补发”要求，于2026-09-12 08:22:52（北京时间）设置为666。现有665条收货记录全部排除在模块2待办新增范围外，其中无论是否曾生成待办、后来是否进入退款后申诉，都不补发；新收货记录中的少退、未收到及其他符合原模块2规则的实收异常照常入队。这个边界按本地收货记录ID，不按平台订单下单时间，也不代表旧异常已经解决。后续不要把起点重置为0。

## 发布与恢复

使用原后台周期与ERP员工凭据，没有新增计划任务。先备份 `.env`、版本指针与运行日志，在原发布开关保持可回查的情况下临时暂停发送；请求守护及worker自然退出，不强杀资金请求。保存历史界线、核查同类待发送队列，必要时取消旧队列任务并保留审计。

本次候选 `.runtime/releases/module2-todo-forward-20260912/src` 复制自正在运行的 `wecom-title-window-20260911`，仅调整 `core/config.py`、`workflows/module1_worker.py`、`workflows/module2_erp_intake.py`，不带入开发目录的其他未提交变更。更新 `.runtime/module1-worker-release.json` 后，用 `scripts/module1-worker.ps1 -Action Start` 启动，核对实际导入路径，再恢复数据库发布开关和 `scripts/module1-autostart.ps1 -Action StartWatch`。网页、MySQL及其他写开关不变，无数据库迁移。

回退也需安全停止worker，恢复旧发布指针。旧版不识别新起点，回退时必须保持人工待办发布关闭；不能恢复旧任务为待发送。完整业务证据、原配置与回查结果仅保存在Git忽略的 `.runtime/audits/module2-todo-fix-20260912/`。

## 验证

开发目录及独立发布目录均通过以下56项测试；修改的模块2代码及新测试Ruff通过：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_module2_todo_queue.py tests/test_module2_erp_intake.py tests/test_manual_todo_control.py tests/test_manual_todo_text.py
```

覆盖前25条已有任务不阻塞后续分批处理、历史未建待办记录不补发、新少退记录生成正确业务员事项、退款前后身份独立、重复运行不重建、只读预演不改变数据库。线上只做只读候选核查及已授权的发布恢复，不伪造新业务单测试发送。
