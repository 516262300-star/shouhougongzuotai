"""配置能力不冒充个案验收，也不把有限支持标成全自动。"""


def capabilities(settings, configured, sync, attribution, financial, sync_enabled):
    from aftersales_workbench.services.integration_capabilities import (
        _disabled,
        _notification_enabled,
        _requirements,
    )

    whitelisted = configured.shop_code in {f"douyin-third-party-{n:02d}" for n in range(1, 5)}
    common = (
        (sync_enabled, "抖音同步未开启"), (whitelisted, "不在抖音四店执行范围"),
        (configured.shop_code in settings.douyin_module12_shop_codes, "未加入模块1/2白名单"),
        (settings.erp_web_username and settings.erp_web_password, "ERP核验凭据未配置"),
    )
    funds = _requirements((*common,
        (settings.douyin_refund_execution_enabled, "抖音专用资金写开关关闭")),
        "专用API单次请求，不留言；结果不明只回查，不重发")
    m1 = _requirements((*common, (settings.douyin_module1_enabled, "抖音模块1关闭"),
        (_notification_enabled(settings), "企业微信通知出口未开启"),
        (funds["state"] == "enabled", funds["detail"])),
        "有限开启：独立整包裹先核验再拦截；首期须真实TH退回才退款，不按无轨迹放款")
    m2 = _requirements((*common, (settings.douyin_module2_enabled, "抖音模块2关闭"),
        (settings.module2_worker_enabled, "模块2后台关闭"),
        (funds["state"] == "enabled", funds["detail"])),
        "有限开启：客户名下TH、独立仓库质检、完整SKU/数量及精确原收款一致后退款；"
        "暂存认领、部分退款、多订单/多包裹须人工")
    erp = _requirements((*common,
        (settings.douyin_module1_enabled or settings.douyin_module2_enabled, "模块1/2关闭"),
        (settings.module1_erp_refund_execution_enabled, "退回ERP补单总开关关闭"),
        (settings.erp_write_enabled, "ERP写总开关关闭")),
        "平台已成功且独立退货原收款一致时单次补开退款单；唯一流水和零应收确认才闭环")
    m3 = _requirements((
        (sync_enabled, "抖音同步未开启"),
        (whitelisted and configured.shop_code in settings.douyin_module3_shop_codes,
         "未加入模块3白名单"),
        (settings.douyin_module3_enabled, "抖音模块3关闭"),
        (settings.module3_worker_enabled, "模块3后台关闭"),
        (settings.module3_erp_refund_execution_enabled, "模块3ERP补单总开关关闭"),
        (settings.erp_write_enabled, "ERP写总开关关闭"),
        (settings.erp_web_username and settings.erp_web_password, "ERP凭据缺失")),
        "有限开启：独立整单未发货且平台已全额退款，核对原收款后单次ERP补单")
    for cap in (m1, m2, m3, erp):
        if cap["state"] == "enabled":
            cap["label"] = "有限开启"
    return dict(sync=sync, attribution=attribution, financial=financial,
                shipment_reminder=_disabled("普通发货提醒独立配置，本次模块开启不改变它"),
                refund_permission=funds, module1=m1, module2=m2, module1_erp=erp, module3=m3)
