from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aftersales_workbench.core.config import Settings, get_settings
from aftersales_workbench.db.models import AfterSalesOrder, Platform, Shop
from aftersales_workbench.integrations.marketplace.models import (
    MarketplaceConfigurationError,
)
from aftersales_workbench.integrations.marketplace.runner import SUPPORTED_PLATFORMS
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops
from aftersales_workbench.integrations.pdd.client import PddConfigurationError
from aftersales_workbench.integrations.pdd.shops import load_configured_pdd_shops
from aftersales_workbench.integrations.tmall.client import TmallConfigurationError
from aftersales_workbench.integrations.tmall.shops import load_configured_tmall_shops

PLATFORM_META: dict[Platform, tuple[str, str]] = {
    Platform.PDD: ("拼多多", "官方开放平台"),
    Platform.TMALL: ("天猫", "淘宝开放平台·官方接口"),
    Platform.TAOBAO: ("淘宝", "第三方中转"),
    Platform.ALIBABA_1688: ("1688", "官方开放平台"),
    Platform.JD: ("京东", "第三方中转"),
    Platform.DOUYIN: ("抖音", "官方开放平台"),
}

CAPABILITY_DEFINITIONS = (
    ("sync", "售后同步", "持续同步平台售后单及退款状态"),
    ("attribution", "售后归因", "平台、店铺、型号及原因归因"),
    ("financial", "退款统计", "申请金额与实际退款成功金额"),
    ("refund_permission", "退款权限", "平台退款写入凭证与总开关"),
    ("module1", "模块1·拦截退款", "已发货仅退款拦截及平台退款"),
    ("module1_erp", "模块1·退回平账", "退回后 ERP 认领与退款单闭环"),
    ("module2", "模块2·验货退款", "退货验收一致后平台退款"),
    ("module3", "模块3·未发货平账", "平台已退款后的 ERP 退款闭环"),
)

_PLATFORM_ORDER = (
    Platform.PDD,
    Platform.TMALL,
    Platform.TAOBAO,
    Platform.ALIBABA_1688,
    Platform.JD,
    Platform.DOUYIN,
)

_MARKETPLACE_SYNC_FIELDS = {
    Platform.TAOBAO: "taobao_sync_enabled",
    Platform.ALIBABA_1688: "alibaba_1688_sync_enabled",
    Platform.JD: "jd_sync_enabled",
    Platform.DOUYIN: "douyin_sync_enabled",
}


@dataclass(frozen=True, slots=True)
class ShopSnapshot:
    platform: Platform
    shop_code: str
    shop_name: str
    platform_shop_id: str | None
    is_active: bool
    record_count: int = 0
    last_record_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ConfiguredShop:
    platform: Platform
    shop_number: int
    shop_code: str
    configured_name: str
    platform_shop_id: str | None
    refund_credential_configured: bool


def _secret_configured(value: Any) -> bool:
    return bool(value and value.get_secret_value().strip())


def _enabled(detail: str) -> dict[str, str]:
    return {"state": "enabled", "label": "已开启", "detail": detail}


def _disabled(detail: str) -> dict[str, str]:
    return {"state": "disabled", "label": "未开启", "detail": detail}


def _unsupported(detail: str) -> dict[str, str]:
    return {"state": "unsupported", "label": "未接入", "detail": detail}


def _requirements(
    requirements: tuple[tuple[bool, str], ...],
    success_detail: str,
) -> dict[str, str]:
    for satisfied, missing_detail in requirements:
        if not satisfied:
            return _disabled(missing_detail)
    return _enabled(success_detail)


def _configured_shops(
    settings: Settings,
) -> tuple[dict[Platform, list[ConfiguredShop]], dict[Platform, str]]:
    result = {platform: [] for platform in _PLATFORM_ORDER}
    errors: dict[Platform, str] = {}

    try:
        result[Platform.PDD] = [
            ConfiguredShop(
                platform=Platform.PDD,
                shop_number=shop.shop_number,
                shop_code=shop.shop_code,
                configured_name=f"拼多多{shop.shop_number}店",
                platform_shop_id=None,
                refund_credential_configured=True,
            )
            for shop in load_configured_pdd_shops(settings, require_all=False)
        ]
    except PddConfigurationError as exc:
        errors[Platform.PDD] = str(exc)

    try:
        result[Platform.TMALL] = [
            ConfiguredShop(
                platform=Platform.TMALL,
                shop_number=shop.shop_number,
                shop_code=shop.shop_code,
                configured_name=f"天猫{shop.shop_number}店",
                platform_shop_id=None,
                refund_credential_configured=_secret_configured(
                    shop.refund_session_key
                ),
            )
            for shop in load_configured_tmall_shops(settings, require_all=False)
        ]
    except TmallConfigurationError as exc:
        errors[Platform.TMALL] = str(exc)

    for platform in SUPPORTED_PLATFORMS:
        try:
            result[platform] = [
                ConfiguredShop(
                    platform=platform,
                    shop_number=shop.shop_number,
                    shop_code=shop.shop_code,
                    configured_name=shop.shop_name,
                    platform_shop_id=shop.platform_shop_id,
                    refund_credential_configured=False,
                )
                for shop in load_marketplace_shops(settings, platform)
            ]
        except MarketplaceConfigurationError as exc:
            errors[platform] = str(exc)
    return result, errors


def _sync_enabled(settings: Settings, platform: Platform) -> bool:
    if platform is Platform.PDD:
        return True
    if platform is Platform.TMALL:
        return settings.tmall_sync_enabled
    return bool(getattr(settings, _MARKETPLACE_SYNC_FIELDS[platform]))


def _notification_enabled(settings: Settings) -> bool:
    return bool(
        (
            settings.module1_notification_transport == "desktop"
            and settings.module1_desktop_send_enabled
        )
        or (
            settings.module1_notification_transport == "qywx_webhook"
            and settings.qywx_write_enabled
        )
    )


def _shop_capabilities(
    settings: Settings,
    configured: ConfiguredShop,
    *,
    sync_enabled: bool,
) -> dict[str, dict[str, str]]:
    platform = configured.platform
    supports_modules = platform in {Platform.PDD, Platform.TMALL}
    tmall_modules_enabled = (
        platform is not Platform.TMALL or settings.tmall_module123_trial_enabled
    )
    refund_whitelisted = (
        configured.shop_number in set(settings.tmall_refund_enabled_shop_numbers)
        if platform is Platform.TMALL
        else platform is Platform.PDD
    )
    refund_credential_ready = (
        configured.refund_credential_configured and refund_whitelisted
    )
    platform_write_enabled = (
        settings.pdd_write_enabled
        if platform is Platform.PDD
        else settings.tmall_write_enabled
    )
    module1_refund_enabled = (
        settings.module1_pdd_refund_execution_enabled
        if platform is Platform.PDD
        else settings.module1_tmall_refund_execution_enabled
    )
    module2_refund_enabled = (
        settings.module2_pdd_refund_execution_enabled
        if platform is Platform.PDD
        else settings.module2_tmall_refund_execution_enabled
    )

    sync = (
        _enabled("店铺凭证已配置，自动同步开关已开启")
        if sync_enabled
        else _disabled("平台自动同步开关未开启")
    )
    attribution = (
        _enabled("同步数据自动进入平台、店铺、型号和原因归因")
        if sync_enabled
        else _disabled("开启售后同步后才能持续形成归因数据")
    )
    financial = (
        _enabled("记录申请退款金额和实际退款成功金额")
        if sync_enabled
        else _disabled("开启售后同步后才能持续更新退款金额")
    )
    if not supports_modules:
        return {
            "sync": sync,
            "attribution": attribution,
            "financial": financial,
            "refund_permission": _unsupported("当前接入为只读同步，不调用平台退款接口"),
            "module1": _unsupported("当前平台尚未接入模块 1 自动化"),
            "module1_erp": _unsupported("当前平台尚未接入模块 1 退回平账"),
            "module2": _unsupported("当前平台尚未接入模块 2 自动化"),
            "module3": _unsupported("当前平台尚未接入模块 3 自动化"),
        }

    refund_permission = _requirements(
        (
            (sync_enabled, "售后同步未开启"),
            (tmall_modules_enabled, "天猫模块 1/2/3 接入开关未开启"),
            (refund_credential_ready, "未配置退款凭证或未加入退款店铺白名单"),
            (platform_write_enabled, "平台退款写总开关未开启"),
        ),
        "退款凭证与平台写总开关均已配置",
    )
    module1 = _requirements(
        (
            (sync_enabled, "售后同步未开启"),
            (tmall_modules_enabled, "天猫模块 1/2/3 接入开关未开启"),
            (refund_credential_ready, "该店没有可用的退款授权"),
            (_notification_enabled(settings), "企业微信拦截通知出口未开启"),
            (module1_refund_enabled, "模块 1 平台退款执行开关未开启"),
            (platform_write_enabled, "平台退款写总开关未开启"),
        ),
        "物流拦截确认后自动执行平台退款",
    )
    module1_erp = _requirements(
        (
            (module1["state"] == "enabled", "模块 1 拦截退款尚未完整开启"),
            (settings.erp_return_match_sync_enabled, "ERP 退货认领核对未开启"),
            (
                settings.module1_erp_refund_execution_enabled,
                "模块 1 ERP 退款单执行开关未开启",
            ),
            (settings.erp_write_enabled, "ERP 写总开关未开启"),
        ),
        "退回后自动核对认领、应收和平账结果",
    )
    module2 = _requirements(
        (
            (sync_enabled, "售后同步未开启"),
            (tmall_modules_enabled, "天猫模块 1/2/3 接入开关未开启"),
            (refund_credential_ready, "该店没有可用的退款授权"),
            (settings.module2_worker_enabled, "模块 2 后台运行未开启"),
            (module2_refund_enabled, "模块 2 平台退款执行开关未开启"),
            (platform_write_enabled, "平台退款写总开关未开启"),
        ),
        "仓库验货和退货明细一致后自动平台退款",
    )
    module3 = _requirements(
        (
            (sync_enabled, "售后同步未开启"),
            (tmall_modules_enabled, "天猫模块 1/2/3 接入开关未开启"),
            (settings.module3_worker_enabled, "模块 3 后台运行未开启"),
            (
                settings.module3_erp_refund_execution_enabled,
                "模块 3 ERP 退款单执行开关未开启",
            ),
            (settings.erp_write_enabled, "ERP 写总开关未开启"),
        ),
        "平台已退款后自动核对未发货订单并完成 ERP 平账",
    )
    return {
        "sync": sync,
        "attribution": attribution,
        "financial": financial,
        "refund_permission": refund_permission,
        "module1": module1,
        "module1_erp": module1_erp,
        "module2": module2,
        "module3": module3,
    }


def build_integration_capabilities(
    settings: Settings,
    database_shops: list[ShopSnapshot],
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    configured_by_platform, errors = _configured_shops(settings)
    database_by_code = {shop.shop_code: shop for shop in database_shops}
    platforms: list[dict[str, Any]] = []
    all_shops: list[dict[str, Any]] = []

    for platform in _PLATFORM_ORDER:
        label, connection_mode = PLATFORM_META[platform]
        platform_sync_enabled = _sync_enabled(settings, platform)
        shops: list[dict[str, Any]] = []
        for configured in configured_by_platform[platform]:
            database = database_by_code.get(configured.shop_code)
            capabilities = _shop_capabilities(
                settings,
                configured,
                sync_enabled=platform_sync_enabled,
            )
            if platform_sync_enabled and database and database.is_active:
                connection = {
                    "state": "enabled",
                    "label": "运行中",
                    "detail": "配置已生效，本地店铺已建立",
                }
            elif platform_sync_enabled:
                connection = {
                    "state": "warning",
                    "label": "待首轮同步",
                    "detail": "配置与同步开关已开启，尚未建立有效本地店铺",
                }
            else:
                connection = {
                    "state": "disabled",
                    "label": "未开启",
                    "detail": "店铺凭证已配置，但平台自动同步开关关闭",
                }
            item = {
                "shop_number": configured.shop_number,
                "shop_code": configured.shop_code,
                "shop_name": (
                    database.shop_name if database else configured.configured_name
                ),
                "platform_shop_id": (
                    database.platform_shop_id
                    if database and database.platform_shop_id
                    else configured.platform_shop_id
                ),
                "connection": connection,
                "record_count": database.record_count if database else 0,
                "last_record_at": (
                    database.last_record_at.isoformat()
                    if database and database.last_record_at
                    else None
                ),
                "capabilities": capabilities,
            }
            shops.append(item)
            all_shops.append(item)

        sync_count = sum(
            shop["capabilities"]["sync"]["state"] == "enabled" for shop in shops
        )
        refund_count = sum(
            shop["capabilities"]["refund_permission"]["state"] == "enabled"
            for shop in shops
        )
        full_count = sum(
            all(
                shop["capabilities"][key]["state"] == "enabled"
                for key in ("module1", "module1_erp", "module2", "module3")
            )
            for shop in shops
        )
        if not shops:
            platform_state, platform_state_label = "missing", "未配置店铺"
        elif full_count == len(shops):
            platform_state, platform_state_label = "enabled", "模块全开"
        elif sync_count:
            platform_state, platform_state_label = "partial", "部分功能"
        else:
            platform_state, platform_state_label = "disabled", "未启用"
        platforms.append(
            {
                "platform": platform.value,
                "platform_label": label,
                "connection_mode": connection_mode,
                "state": platform_state,
                "state_label": platform_state_label,
                "configuration_error": errors.get(platform),
                "configured_shop_count": len(shops),
                "sync_enabled_shop_count": sync_count,
                "refund_enabled_shop_count": refund_count,
                "full_module_shop_count": full_count,
                "shops": shops,
            }
        )

    return {
        "checked_at": (checked_at or datetime.now(UTC)).isoformat(),
        "source_note": (
            "状态来自当前运行配置、店铺凭证是否存在及本地店铺登记；"
            "页面不返回 AppSecret、SessionKey 或 AccessToken。"
        ),
        "capability_definitions": [
            {"id": key, "label": label, "description": description}
            for key, label, description in CAPABILITY_DEFINITIONS
        ],
        "summary": {
            "platform_count": len(_PLATFORM_ORDER),
            "configured_shop_count": len(all_shops),
            "sync_enabled_shop_count": sum(
                shop["capabilities"]["sync"]["state"] == "enabled"
                for shop in all_shops
            ),
            "refund_enabled_shop_count": sum(
                shop["capabilities"]["refund_permission"]["state"] == "enabled"
                for shop in all_shops
            ),
            "full_module_shop_count": sum(
                all(
                    shop["capabilities"][key]["state"] == "enabled"
                    for key in ("module1", "module1_erp", "module2", "module3")
                )
                for shop in all_shops
            ),
        },
        "platforms": platforms,
    }


class IntegrationCapabilityService:
    """汇总平台、店铺与功能开通状态；不返回任何凭证明文。"""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def get_capabilities(self) -> dict[str, Any]:
        rows = self.session.execute(
            select(
                Shop.shop_id,
                Shop.platform,
                Shop.shop_code,
                Shop.shop_name,
                Shop.platform_shop_id,
                Shop.is_active,
            ).order_by(Shop.platform, Shop.shop_id)
        ).all()
        activity = {
            shop_id: (count, latest)
            for shop_id, count, latest in self.session.execute(
                select(
                    AfterSalesOrder.shop_id,
                    func.count(AfterSalesOrder.id),
                    func.max(AfterSalesOrder.updated_at),
                ).group_by(AfterSalesOrder.shop_id)
            ).all()
        }
        snapshots = [
            ShopSnapshot(
                platform=(
                    row.platform
                    if isinstance(row.platform, Platform)
                    else Platform(str(row.platform))
                ),
                shop_code=row.shop_code,
                shop_name=row.shop_name,
                platform_shop_id=row.platform_shop_id,
                is_active=bool(row.is_active),
                record_count=int(activity.get(row.shop_id, (0, None))[0]),
                last_record_at=activity.get(row.shop_id, (0, None))[1],
            )
            for row in rows
        ]
        return build_integration_capabilities(self.settings, snapshots)
