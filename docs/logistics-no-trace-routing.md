# 快递100“无轨迹”分流与退款冻结

## 适用范围

本规则用于模块1的发送前物流预检和退款物流闸门。它只处理快递100正常响应但暂时没有可用轨迹的业务结果，不改变网络错误、HTTP错误、配置错误和响应格式错误的技术故障处理。

## 处理规则

- 同一运行周期内，相同快递公司、运单号和查询手机号只请求快递100一次，多笔售后共用缓存结果。
- “查询无结果”“暂无轨迹”“暂无物流”或成功响应中的空轨迹统一记为 `no_trace`，不再把整个物流闸门阶段判为失败。运行日志同时输出无轨迹售后数和去重后的无轨迹包裹数。
- 第1至第5次无轨迹继续按5、10、20、30、30分钟退避复查；自动退款始终冻结。
- 连续第6次无轨迹后，订单转为 `MANUAL_PROCESSING`，清空下次物流检查时间并退出自动物流查询队列；待发送的拦截通知和待执行的平台退款任务会被取消。
- 模块1人工待办阶段按现有幂等键创建业务员待办，说明连续无轨迹次数，并要求核对运单号和快递公司。已有失败次数达到阈值的旧记录会直接转人工，不会为了分流再请求一次快递100。
- 网络、HTTP、配置和返回格式异常仍计入 `failed`，用于运行监控告警；它们不会被误当成包裹无轨迹。

## 安全边界

- 无轨迹只会冻结退款，绝不会据此放行退款。
- 转人工不会伪造物流、签收或退回证据。
- 平台退款请求仍禁止自动重试；结果不明时只做只读回查。
- 若人工修正运单或快递公司并需要重新进入自动链路，应先确认平台仍未退款，再由维护人员恢复合适的模块1流程状态并清理旧查询错误。

## 验证与回退

上线前运行：

```powershell
.\.venv\Scripts\pytest.exe tests/test_kuaidi100_client.py tests/test_module1_logistics.py tests/test_module1_preflight.py tests/test_module1_manual_todo.py
.\.venv\Scripts\ruff.exe check src/aftersales_workbench/integrations/logistics/kuaidi100.py src/aftersales_workbench/workflows/module1_logistics.py src/aftersales_workbench/workflows/module1_preflight.py tests/test_kuaidi100_client.py tests/test_module1_logistics.py tests/test_module1_preflight.py tests/test_module1_manual_todo.py
```

部署时使用 `scripts/module1-worker.ps1 -Action Stop` 等待当前事务完成，再执行 `Start`。如需回退，停止后台运行器、回退本次代码提交后重新启动；本次不涉及数据库迁移。
