"""天猫原生物流轨迹证据；状态文字和空响应不能冒充实际物流节点。"""

from aftersales_workbench.integrations.tmall.client import TmallApiError
from aftersales_workbench.workflows.module1_logistics import resolve_logistics_carrier


def tmall_trace_evidence(client, parcel, carrier_map):
    carrier = resolve_logistics_carrier(parcel.carrier, carrier_map)
    params = {"tid": int(parcel.order_sn)}
    if parcel.sub_order_ids:
        if len(parcel.sub_order_ids) > 50:
            raise ValueError("天猫拆单子单超过轨迹接口上限，需核验")
        params.update(is_split=1, sub_tid=",".join(parcel.sub_order_ids))
    evidence = {"source": "TMALL_TRACE", "order_sn": parcel.order_sn,
                "tracking_number": parcel.tracking_number, "carrier_code": carrier}
    try:
        response = client.execute_read("taobao.logistics.trace.search", **params)
    except TmallApiError as exc:
        # 官方明确的业务无记录码；仍须快递100独立确认，不接受权限/拆单/取数失败。
        if exc.sub_code not in {"isv.order-no-trace", "isp.order-no-trace"}:
            raise
        return {**evidence, "result": "NO_TRACE", "return_code": exc.sub_code,
                "request_id": exc.request_id}
    body = response.get("logistics_trace_search_response")
    if not isinstance(body, dict):
        raise ValueError("天猫物流轨迹响应缺失，不能判定为无物流")
    if (str(body.get("tid")) != parcel.order_sn
            or str(body.get("out_sid") or "").strip() != parcel.tracking_number
            or resolve_logistics_carrier(body.get("company_name"), carrier_map) != carrier):
        raise ValueError("天猫物流轨迹的订单、运单或快递公司不匹配")
    node = body.get("trace_list")
    steps = node.get("transit_step_info") if isinstance(node, dict) else None
    if not isinstance(steps, list):
        raise ValueError("天猫未返回完整轨迹列表，等待核验")
    # 线下发货的顶层“已签收”可能是占位状态，仅真实节点可作已出现轨迹证据。
    if not steps:
        raise ValueError("天猫轨迹为空但没有明确无记录业务码，等待核验")
    if any(not isinstance(step, dict) or not step.get("status_time")
           or not str(step.get("status_desc") or "").strip() for step in steps):
        raise ValueError("天猫物流节点不完整，等待核验")
    return {**evidence, "result": "HAS_TRACE", "event_count": len(steps),
            "first_event_at": min(str(step["status_time"]) for step in steps),
            "latest_event_at": max(str(step["status_time"]) for step in steps),
            "actions": sorted({str(step.get("action") or "") for step in steps}),
            "request_id": body.get("request_id")}
