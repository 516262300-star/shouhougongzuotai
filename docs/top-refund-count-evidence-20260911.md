# 2026-09-11 TOP退款列表明确空结果修复

安全后台首轮运行时，天猫部分店铺及淘宝的空列表被拒绝。只读样本确认：原请求使用`use_has_next=true`，空窗口响应仅包含`has_next=false`及请求编号，没有退款列表或总数。新版严格校验没有把这种缺字段响应当成明确空结果，因此同步失败且不推进水位。

修正共享`TmallClient.get_refunds`为`use_has_next=false`，要求平台返回明确`total_results`。同一店铺只读对照请求已取得整数`total_results=0`，无需放宽解析器。天猫和淘宝共用此客户端，两者均继续执行只读同步；没有扩大资金操作权限。

新增回归覆盖明确零总数可推进空窗口，正总数缺列表、只含has_next而缺总数仍报错且不推进水位；签名请求参数断言同步更新。针对性79项全部通过，修改文件Ruff通过。失败窗口由既有水位继续回补，不人为推进或清空游标。测试中本机默认Temp目录权限错误通过工作区独立basetemp排除，不修改业务断言掩盖失败。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tmall_client.py tests/test_tmall_sync.py tests/test_marketplace_clients.py tests/test_audit_safety_regressions.py -q --basetemp=.runtime/audits/top-count-check-new-temp
```

部署版本及首轮观察结果见[生产升级记录](safety-production-upgrade-20260911.md)。真实接口样本保留于Git忽略的`.runtime/audits/production-safety-upgrade-20260911/`，不提交远端。本修正不处理已有1688异常单，也不解除企业微信发送结果未知的防重锁。文档随代码提交GitHub；Notion当前无可调用连接器，未同步。

最终独立提交`6baec59`全量1067项全部通过；部署后09:52:37结束的周期中，天猫6店同步全部成功，淘宝列表错误消除。真实部署仍保留1688既有隔离和桌面发送前台限制，不宣称全链路无人值守验收通过。
