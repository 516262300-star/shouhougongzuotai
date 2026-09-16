"""淘宝模块1/2/3第一阶段：独立只读证据预演，不创建任务、不认领、不退款。

预演通过只表示已查证的部分条件满足，不构成资金授权、质检通过或闭环完成。
"""

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import or_, select

from aftersales_workbench.db.models import (
    AftersalesActionTask,
    AfterSalesOrder,
    AfterSalesType,
    AutomationActionType,
    AutomationTaskStatus,
    MoneyOperation,
    Platform,
    ShippingStatus,
    Shop,
)
from aftersales_workbench.integrations.erp.package_orders import ErpPackageOrderSource
from aftersales_workbench.integrations.erp.return_claim import read_staged_rows, validate_row
from aftersales_workbench.integrations.erp.tmall_returned import (
    customer_rows,
    inspect_return_account,
)
from aftersales_workbench.integrations.erp.tmall_unshipped import amount, inspect_tmall_unshipped
from aftersales_workbench.integrations.erp.unshipped_refund import ErpWebUnshippedRefundClient
from aftersales_workbench.integrations.marketplace.shops import load_marketplace_shops
from aftersales_workbench.integrations.tmall.client import (
    TmallApiError,
    TmallClient,
    TmallCredentials,
)
from aftersales_workbench.integrations.tmall.mapper import (
    normalize_forward_logistics,
    unwrap_refund,
    unwrap_trade,
)
from aftersales_workbench.integrations.tmall.shipping import classify_shipping
from aftersales_workbench.workflows.refund_snapshot import refund_snapshot
from aftersales_workbench.workflows.sync_safety import require_sync_safe_order

READ_METHODS = frozenset(
    {
        "taobao.user.seller.get",
        "taobao.refund.get",
        "taobao.special.refund.get",
        "taobao.trade.fullinfo.get",
        "taobao.logistics.orders.get",
    }
)


def build_readonly_erp(settings, *, sales=False):
    if not settings.erp_web_username or not settings.erp_web_password:
        raise ValueError("缺少ERP只读登录配置")
    client = httpx.Client(
        base_url=settings.erp_web_base_url,
        timeout=settings.erp_web_timeout_seconds,
        follow_redirects=True,
    )
    base = client.base_url

    def guard(request):
        if (request.url.scheme, request.url.host, request.url.port) != (
            base.scheme,
            base.host,
            base.port,
        ):
            raise ValueError("预演不允许ERP请求重定向至其他服务")
        allowed_get = {
            "/leedis/index.php/welcome/loginpage",
            "/leedis2/public/customer/GetCustomerName",
            "/leedis2/public/customer/stdview",
            "/leedis2/public/customer/shipment",
            "/leedis2/public/b4refund",
            "/leedis2/public/admin/refunds",
        }
        allowed = request.method == "GET" and (
            request.url.path in allowed_get
            or re.fullmatch(r"/leedis2/public/admin/refunds/\d+", request.url.path)
        )
        allowed = allowed or (
            request.method == "POST" and request.url.path == "/leedis/index.php/welcome/loginact"
        )
        if not allowed or any(k in request.url.params for k in ("action", "actionid", "apply")):
            raise ValueError("淘宝预演禁止访问ERP业务写入口")

    client.event_hooks["request"] = [guard]
    cls = ErpPackageOrderSource if sales else ErpWebUnshippedRefundClient
    return cls(
        base_url=settings.erp_web_base_url,
        username=settings.erp_web_username.get_secret_value(),
        password=settings.erp_web_password.get_secret_value(),
        http_client=client,
        **({"platform": "TAOBAO"} if sales else {}),
    )


class TaobaoPreviewClient(TmallClient):
    """额外限制可调用方法；即使误传写开关，也不允许任何写接口或通用方法探测。"""

    def __init__(self, *args, **kwargs):
        kwargs["write_enabled"] = False
        kwargs["read_max_attempts"] = 1
        super().__init__(*args, **kwargs)

    def execute_read(self, method, **parameters):
        if method not in READ_METHODS:
            raise ValueError("淘宝预演只允许指定的只读接口")
        try:
            return super().execute_read(method, **parameters)
        except TmallApiError as exc:
            exc.preview_method = method
            raise

    def execute_write(self, *args, **kwargs):
        raise ValueError("淘宝预演禁止资金及其他写入")

    def agree_refund(self, *args, **kwargs):
        raise ValueError("淘宝预演禁止同意退款")


def build_preview_client(settings, shop):
    configured = {s.shop_code: s for s in load_marketplace_shops(settings, Platform.TAOBAO)}
    entry = configured.get(shop.shop_code)
    raw = next((s for s in settings.taobao_shops_json if s.get("shop_code") == shop.shop_code), {})
    explicit_id = str(raw.get("platform_shop_id") or "").strip()
    # 老中转配置可能不填卖家ID；加载器的shop_code兜底不是平台身份。
    # 不猜测真实ID，必须继续通过实时seller查询核对数据库店铺归属。
    if not entry or (explicit_id not in {"", shop.shop_code, shop.platform_shop_id}):
        raise ValueError("淘宝只读配置与数据库店铺身份不一致")
    return TaobaoPreviewClient(
        TmallCredentials(
            shop_code=entry.shop_code,
            app_key=entry.app_key,
            app_secret=entry.app_secret,
            session_key=entry.session_key,
        ),
        api_url=settings.taobao_api_url,
        request_method=settings.taobao_request_method,
        timeout_seconds=settings.marketplace_timeout_seconds,
    )


def inspect_platform(client, order, shop, *, receipt_only=False):
    """全额、单子单、单SKU；以实时平台事实分流，不用缺字段推断未发货。"""
    seller = client.get_seller().get("user_seller_get_response", {}).get("user", {})
    if not shop.platform_shop_id or str(seller.get("user_id") or "") != shop.platform_shop_id:
        raise ValueError("淘宝授权卖家身份不一致")
    refund = unwrap_refund(client.get_refund(refund_id=int(order.after_sales_sn)))
    trade = unwrap_trade(client.get_trade_fullinfo(tid=int(order.platform_order_sn)))
    if not trade.get("seller_nick"):
        raise ValueError("中转订单详情缺少seller_nick，无法交叉核实本单卖家归属")
    if (
        str(refund.get("refund_id") or "") != order.after_sales_sn
        or str(refund.get("tid") or "") != order.platform_order_sn
        or str(trade.get("tid") or "") != order.platform_order_sn
        or not seller.get("nick")
        or trade.get("seller_nick") != seller["nick"]
    ):
        raise ValueError("淘宝订单、售后或卖家归属不一致")
    if refund.get("special_refund_type") or refund.get("operation_contraint"):
        raise ValueError("特殊售后或操作限制尚未适配，不能套用普通退款")
    returned = refund.get("has_good_return")
    if type(returned) is not bool and returned not in ("true", "false"):
        raise ValueError("淘宝售后是否退货字段不明确")
    has_return = returned in (True, "true")
    expected_type = AfterSalesType.RETURN_AND_REFUND if has_return else AfterSalesType.ONLY_REFUND
    if order.after_sales_type != expected_type:
        raise ValueError("淘宝售后类型与本地记录不一致")
    children = trade.get("orders", {}).get("order")
    if not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], dict):
        raise ValueError("多子单或子单缺失，须整批人工核验")
    child = children[0]
    if not child.get("oid") or str(child["oid"]) != str(refund.get("oid") or ""):
        raise ValueError("退款子单与原销售子单不一致")
    sku = str(child.get("outer_sku_id") or "").strip()
    if sku.count("#") != 1 or any(not v.strip() for v in sku.split("#")):
        raise ValueError("缺少完整型号、颜色SKU")
    product, color = (v.strip() for v in sku.split("#"))
    quantity = amount(child.get("num"))
    if (
        quantity <= 0
        or quantity != quantity.to_integral_value()
        or amount(refund.get("num")) != quantity
    ):
        raise ValueError("部分数量退款或商品数量无效")
    if len(order.items) != 1 or (order.items[0].sku_code, order.items[0].applied_quantity) != (
        sku,
        quantity,
    ):
        raise ValueError("本地型号数量与平台实时申请不一致")
    expected = amount(refund.get("refund_fee"))
    if expected <= 0 or any(
        amount(v) != expected
        for v in (
            order.refund_amount,
            trade.get("payment"),
            trade.get("total_fee"),
            child.get("payment"),
        )
    ):
        raise ValueError("非整单等额退款，优惠、运费或部分退款须另行核验")
    status = str(refund.get("status") or "")
    allowed = (
        {"SUCCESS", "WAIT_SELLER_CONFIRM_GOODS"} if has_return else {"SUCCESS", "WAIT_SELLER_AGREE"}
    )
    if status not in allowed:
        raise ValueError("当前售后状态不在退款/已退款核账预演范围")
    if order.refund_financial_status == "SUCCESS" and status != "SUCCESS":
        raise ValueError("本地与平台退款成功事实冲突")
    if status == "SUCCESS" and (
        order.refund_financial_status != "SUCCESS"
        or order.actual_refund_amount is None
        or amount(order.actual_refund_amount) != expected
    ):
        raise ValueError("本地实际成功金额尚未与平台核对一致")
    if receipt_only:
        if not has_return:
            raise ValueError("实收独立核验仅用于退货退款，不支持仅退款/未发货推断")
        if not refund.get("sid") or str(refund["sid"]).strip() != order.return_tracking_number:
            raise ValueError("退货运单缺失或与本地不一致")
        return dict(
            module=2,
            status=status,
            amount=expected,
            quantity=quantity,
            product=product,
            color=color,
            child_id=str(child["oid"]),
            tracking=None,
            shipping="NOT_CHECKED",
            refund_request_metadata_present=False,
            items=Counter({(product, color): quantity}),
        )
    logistics = client.get_logistics_orders(tid=int(order.platform_order_sn))
    response = logistics.get("logistics_orders_get_response", {})
    shippings = response.get("shippings", {}).get("shipping")
    if not isinstance(shippings, list):
        raise ValueError("物流列表结构不完整，不能推断未发货")
    shipping = classify_shipping(refund, trade, logistics)
    tracking, carrier = normalize_forward_logistics(logistics)
    if has_return:
        module = 2
        if shipping not in {ShippingStatus.IN_TRANSIT, ShippingStatus.DELIVERED}:
            raise ValueError("退货退款缺少明确已发货事实")
        return_tracking = str(refund.get("sid") or "").strip()
        if not return_tracking or return_tracking != order.return_tracking_number:
            raise ValueError("退货运单缺失或与本地不一致")
    elif shipping == ShippingStatus.UNSHIPPED:
        module = 3
        if (
            shippings
            or order.forward_tracking_number
            or order.return_tracking_number
            or order.logistics_physical_seen_at
            or order.order_shipping_status != ShippingStatus.UNSHIPPED
        ):
            raise ValueError("存在历史发货或运单证据，不能按未发货平账")
        if status != "SUCCESS":
            raise ValueError("模块3须先有平台明确退款成功事实")
    elif shipping in {ShippingStatus.IN_TRANSIT, ShippingStatus.DELIVERED}:
        module = 1
        if refund.get("sid"):
            raise ValueError("仅退款出现退货运单，须人工确认场景")
    else:
        raise ValueError("发货事实不明确，不根据订单关闭状态猜测")
    if module in {1, 2} and (
        len(shippings) != 1
        or not tracking
        or not carrier
        or tracking != order.forward_tracking_number
        or carrier != order.carrier_code
    ):
        raise ValueError("不是唯一已核实发货包裹或运单已变化")
    return dict(
        module=module,
        status=status,
        amount=expected,
        quantity=quantity,
        product=product,
        color=color,
        child_id=str(child["oid"]),
        tracking=tracking,
        shipping=str(shipping),
        refund_request_metadata_present=bool(
            re.fullmatch(r"[1-9]\d*", str(refund.get("refund_version") or ""))
            and refund.get("refund_phase") in {"onsale", "aftersale"}
        ),
        items=Counter({(product, color): quantity}),
    )


def inspect_receipt(client, sales, order, facts):
    """检查正式TH或完整暂存列表，不搬单，不把实收一致当作独立质检通过。"""
    rows = customer_rows(client, sales.customer_id)
    originals = [r for r in rows if r["编号"].startswith("RC-")]
    returns = [r for r in rows if r["编号"].startswith("TH-")]
    if len(originals) != 1 or len(returns) > 1 or len(rows) != len(originals) + len(returns):
        raise ValueError("客户有多笔销售、退货或特殊行，须整批核验")
    sale = originals[0]
    price = amount(sale["单价"])
    if (
        sale["客户编号"] != order.platform_order_sn
        or not sale["订单编号"].isdigit()
        or (sale["型号"], sale["颜色"]) != (facts["product"], facts["color"])
        or amount(sale["入库化只"]) != facts["quantity"]
        or price <= 0
        or price * facts["quantity"] != facts["amount"]
    ):
        raise ValueError("ERP原销售与平台完整商品及金额不一致")
    tracking = order.return_tracking_number if facts["module"] == 2 else facts["tracking"]
    staged = read_staged_rows(client)
    matches = [r for r in staged if r["运单号"] == tracking]
    if returns:
        row = returns[0]
        if (
            matches
            or not re.fullmatch(r"TH-\d[\d-]*", row["编号"])
            or row["客户编号"] != sale["订单编号"]
            or row["订单编号"] != tracking
            or (row["型号"], row["颜色"]) != (facts["product"], facts["color"])
            or Decimal(row["入库化只"]) != -facts["quantity"]
            or amount(row["单价"]) != price
        ):
            raise ValueError("正式退货关联不符或暂存存在重复收货证据")
        return {"location": "customer_profile", "receipt": row["编号"]}
    if (
        len(matches) != 1
        or len([r for r in staged if matches and r["编号"] == matches[0]["编号"]]) != 1
    ):
        raise ValueError("尚无唯一单行退货实收，等待到货或整批人工核验")
    row = matches[0]
    validate_row(
        row,
        receipt=row["编号"],
        tracking=tracking,
        customer=sales.customer_name,
        product=facts["product"],
        color=facts["color"],
        quantity=facts["quantity"],
        unit_price=price,
    )
    return {"location": "staging", "receipt": row["编号"]}


class TaobaoPreviewService:
    def __init__(self, session, settings, erp, *, platform_factory=None, source_factory=None):
        self.session, self.settings, self.erp = session, settings, erp
        self.platform_factory = platform_factory or (
            lambda shop: build_preview_client(settings, shop)
        )
        self.source_factory = source_factory or (lambda: build_readonly_erp(settings, sales=True))

    def inspect(self, order, *, receipt_only=False):
        started = datetime.now(UTC)
        if self.session.new or self.session.dirty or self.session.deleted:
            raise ValueError("预演会话存在未提交改动，禁止带入")
        shop = self.session.get(Shop, order.shop_id)
        if not shop or shop.platform != Platform.TAOBAO or not shop.is_active:
            raise ValueError("只允许有效淘宝店铺，不接受天猫或其他平台身份")
        if order.after_sales_type not in {
            AfterSalesType.ONLY_REFUND,
            AfterSalesType.RETURN_AND_REFUND,
        }:
            raise ValueError("换货、补寄、维修不属于退款预演")
        require_sync_safe_order(self.session, order.after_sales_sn)
        snapshot = refund_snapshot(order)
        state_fields = (
            "refund_financial_status",
            "actual_refund_amount",
            "workflow_status",
            "erp_customer_name",
            "order_shipping_status",
            "carrier_code",
            "logistics_physical_seen_at",
        )
        state = tuple(getattr(order, field) for field in state_fields)
        tasks = self.session.scalars(
            select(AftersalesActionTask).where(
                AftersalesActionTask.after_sales_sn == order.after_sales_sn,
            )
        ).all()
        if any(
            (
                t.action_type == AutomationActionType.ERP_CREATE_MANUAL_TODO
                and t.action_status != AutomationTaskStatus.CANCELLED
            )
            or (t.attempts or 0) > 0
            or t.action_status == AutomationTaskStatus.RUNNING
            for t in tasks
        ):
            raise ValueError("有人工处理锁或历史执行任务，须先核验既有结果")
        if self.session.scalar(
            select(MoneyOperation.operation_key)
            .where(
                MoneyOperation.shop_id == order.shop_id,
                MoneyOperation.after_sales_sn == order.after_sales_sn,
            )
            .limit(1)
        ):
            raise ValueError("存在资金账本，须走专门结果回查，不能生成新执行建议")
        links = [AfterSalesOrder.platform_order_sn == order.platform_order_sn]
        for tracking in (order.forward_tracking_number, order.return_tracking_number):
            if tracking:
                links += [
                    AfterSalesOrder.forward_tracking_number == tracking,
                    AfterSalesOrder.return_tracking_number == tracking,
                ]
        if self.session.scalar(
            select(AfterSalesOrder.id)
            .where(
                AfterSalesOrder.id != order.id,
                or_(*links),
            )
            .limit(1)
        ):
            raise ValueError("同父订单或包裹有其他售后，须整批核验占用")
        client = self.platform_factory(shop)
        try:
            facts = inspect_platform(client, order, shop, receipt_only=receipt_only)
        finally:
            client.close()
        result = dict(
            order_id=order.id,
            module=facts["module"],
            platform_status=facts["status"],
            platform_evidence="verified",
            execution_ready=False,
            refund_permission="not_verified",
            warehouse_qc="not_verified",
            refund_request_metadata_present=facts["refund_request_metadata_present"],
            forward_logistics_evidence="not_checked" if receipt_only else "verified",
        )
        if facts["module"] == 3:
            lookup = inspect_tmall_unshipped(
                self.erp,
                order_sn=order.platform_order_sn,
                refund_sn=order.after_sales_sn,
                expected_amount=facts["amount"],
                items=facts["items"],
                child_id=facts["child_id"],
                source_mode="existing_admin",
                platform="TAOBAO",
            )
            if order.erp_customer_name and order.erp_customer_name != lookup.customer_name:
                raise ValueError("ERP退款客户与本地登记不一致")
            result["erp_evidence"] = str(lookup.status)
        else:
            source = self.source_factory()
            try:
                sales = source.read(order.platform_order_sn)
            finally:
                source.close()
            if not sales.rows or {r["order_sn"] for r in sales.rows} != {order.platform_order_sn}:
                raise ValueError("客户有其他订单或原销售不完整，须整批人工核验")
            if order.erp_customer_name and sales.customer_name != order.erp_customer_name:
                raise ValueError("ERP原销售客户与本地登记不一致")
            actual = Counter()
            seen = set()
            for row in sales.rows:
                identity = (row["sale_sn"], row["sale_id"], row["product"], row["color"])
                if identity in seen:
                    raise ValueError("原销售关联重复")
                seen.add(identity)
                actual[(row["product"], row["color"])] += amount(row["quantity"])
            if actual != facts["items"]:
                raise ValueError("平台与ERP原销售SKU数量不符")
            if facts["module"] == 1 and facts["status"] != "SUCCESS":
                result["erp_evidence"] = "sales_verified"
                result["next_gate"] = "拦截、物流退款闸门及退款写权限尚待适配验收"
            else:
                receipt = inspect_receipt(self.erp, sales, order, facts)
                result["erp_evidence"] = "receipt_items_verified"
                result["receipt_location"] = receipt["location"]
                result["next_gate"] = "实收占用、独立质检、资金及认领执行尚待验收"
                if facts["module"] == 1:
                    account = inspect_return_account(
                        self.erp,
                        order_sn=order.platform_order_sn,
                        refund_sn=order.after_sales_sn,
                        child_id=facts["child_id"],
                        expected=facts["amount"],
                        product=facts["product"],
                        color=facts["color"],
                        quantity=facts["quantity"],
                        customer_id=sales.customer_id,
                        tracking=facts["tracking"],
                        platform="TAOBAO",
                    )
                    result["account_evidence"] = account["state"]
        self.session.refresh(order)
        self.session.expire(order, ["items"])
        if (
            refund_snapshot(order) != snapshot
            or state != tuple(getattr(order, field) for field in state_fields)
            or datetime.now(UTC) - started > timedelta(seconds=75)
        ):
            raise ValueError("预演期间订单变化或证据超时，不能保留通过结果")
        result["result"] = "preview_evidence_only"
        return result
