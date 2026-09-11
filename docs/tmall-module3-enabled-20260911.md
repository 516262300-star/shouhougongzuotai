# 天猫模块3开启记录（2026-09-11）

用户明确要求“模块3可以开启”。本次将已有天猫模块3适配接进实际生产worker，并开启本机专用执行开关；不是只修改能力面板。前五店按限定范围执行，适家不在本期范围。天猫模块1/2、已接入的认领补单、拼多多业务及其开关不变。

## 开关与触发

- `TMALL_MODULE3_ERP_REFUND_ENABLED=true`：本机开启；版本库模板保持默认false。
- `TMALL_MODULE3_ERP_READ_MODE=existing_admin`：复用现有ERP管理列表、只读详情、客户收退款和发货账页；不需要新ERP账号或专用服务器接口。
- 仍需 `TMALL_SYNC_ENABLED`、`TMALL_MODULE123_TRIAL_ENABLED`、`MODULE3_WORKER_ENABLED`、`MODULE3_ERP_REFUND_EXECUTION_ENABLED`、`ERP_WRITE_ENABLED` 和现有ERP查询凭据有效。
- 在原后台周期运行，拼多多、天猫分别执行独立预检，各自最多 `MODULE3_WORKER_BATCH_LIMIT` 笔；本机保持1。不混用两个平台的预检规则。
- 后台模块3日志新增 `tmall_scanned / tmall_ready / tmall_applied / tmall_blocked`，用于确认天猫分支真的运行。未决订单按现有 `MODULE3_ERP_REFUND_RECHECK_SECONDS` 复查，不删除请求未知记录。
- 店铺能力显示“有限开启·未发货平账”。这是ERP账务功能，不代表子账号已有平台退款权限，也不代表某笔已完成平账。

## 放行与阻断

仅前五店、原上线水位后的独立订单、单子单、整单等额退款；必须明确未发货、平台退款已成功，完整型号/颜色/数量匹配，ERP原收款唯一，原订单及客户关联明确，没有其他售后、人工处理锁或资金请求占用。

发货状态UNKNOWN、TRADE_CLOSED本身、缺少发货时间都不能证明未发货；不会为开启功能而批量回填成UNSHIPPED。多子单、部分退款、优惠差额、已发货、其他收退款流水或未适配数据继续阻断。现有ERP检查启用/编辑冲突，不伪造服务端状态。

ERP补单前再次复核并先提交统一的 `ERP_REFUND` 资金账本，只发送一次请求，禁止自动跟随重定向和资金请求重试。该账本与模块1共用，不能换模块重复退款。回查本笔SK退款流水、无欠货、零应收一致，才登记财务闭环；不伪造取消订单回执、退货单或验货通过。

## 验收与当前业务结果

启用前真实只读预检 `scanned=0, ready=0, applied=0`：目前没有满足“明确未发货”等条件的待处理天猫候选。用户授权开启后，仅让后续符合条件的订单进入上述流程，不把历史未知状态当成已通过。

离线合成订单验证正常补单、现有ERP页面、错店/错单/金额与SKU差异、已发货保护、唯一资金账本、超时回查及不重发。真实ERP补单成功尚无本次验收样本，不能把“功能已开启”说成“已自动完成真实闭环”。

只读命令（不加 `--apply`）：

```powershell
.venv/Scripts/python.exe -m aftersales_workbench.workflows.module3_erp_refund_cli --platform TMALL --limit 5 --details
```

可用 `--platform-order-sn` 限定单笔。即使用户已授权开启，候选也必须满足业务校验，不能手动清空历史锁或账本来放行。

## 发布、恢复与回退

基于上一版 `tmall-return-claim-worker-20260911` 创建隔离副本 `tmall-module3-enabled-worker-20260911`，只增加天猫模块3调度、单次传输防护和统计字段。Web基于上一版认领补单副本创建 `tmall-module3-enabled-web-20260911`，仅更新模块3能力文案，前端构建不变。未复制其他未提交开发改动，无数据库迁移。

配置与原发布指针先备份到本机Git忽略的 `.runtime/backups/tmall-module3-enable-20260911/`，内含凭据，不得上传。安全等待旧worker结束当前周期，切换新副本后恢复原登录自启动及守护。资金/认领账本仍使用原持久化数据库和项目运行目录，重启不重发已有资金请求。

暂停只关闭 `TMALL_MODULE3_ERP_REFUND_ENABLED`，安全重启worker；不要关闭全平台ERP总开关影响拼多多或模块1。回退恢复备份的发布指针，保留全部资金请求和UNKNOWN审计，不回滚真实ERP业务单据。

README、本记录及原适配说明同步提交GitHub；未提供Notion页面，不宣称已同步Notion。
