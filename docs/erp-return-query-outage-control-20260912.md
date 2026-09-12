# ERP 退货查询短时故障控制

## 问题与根因

“在途拦截”页的“ERP 查询失败”来自模块1退货闭环只读查询，不是平台退款失败，也不是客户归属查询失败。2026-09-12 现场记录中共有27笔失败缓存：24笔由ERP登录页短时返回HTTP 500造成，3笔由TLS连接被服务端提前断开造成。ERP恢复后连续5次只读访问登录页均返回HTTP 200，说明故障具有短时、服务级特征。

旧逻辑在一次ERP服务故障后仍逐笔继续访问，因此同一次故障会把一批订单都写成失败；失败项与正常30分钟复查项同序排队，也会让红色状态恢复较慢。

## 新处理规则

- ERP超时、HTTP错误、网络/TLS错误以及登录会话异常统一标记为`failure_scope=service`，错误文本只保留分类和HTTP状态码，不保存响应正文、请求URL、账号、密码或Cookie。
- 同一批查询遇到第一笔服务级故障后立即熔断，只把该笔记为失败；本批其余订单保持原状态，下个周期再处理，避免把一次ERP宕机扩散成几十条红记录。
- `unavailable`失败项固定在5分钟后具备重查资格，并排在普通30分钟复查项之前。旧失败记录没有新分类字段时也按5分钟兼容重查。
- 单笔返回结构无法识别、订单号或运单号缺失仍按记录级问题处理，不触发整批熔断。
- 所有查询均为只读；本规则不调用退货认领、补开退款单、平台退款或企微发送。

运行日志新增`service_unavailable`和`deferred_after_service_failure`，分别表示触发熔断的服务故障数以及本批因熔断而保留到后续周期的去重运单数。

## 验证与恢复

```powershell
.\.venv\Scripts\pytest.exe tests/test_erp_return_match.py
.\.venv\Scripts\ruff.exe check src/aftersales_workbench/integrations/erp/return_match.py tests/test_erp_return_match.py
```

部署时使用`& .\scripts\module1-worker.ps1 -Action Stop`等待当前事务完成，再执行`Start`。本次没有数据库迁移。若ERP持续返回服务级故障，保持自动退款和ERP写入原有安全边界，先修复ERP服务或网络；不要把查询失败改成未找到、已退回或已平账。
