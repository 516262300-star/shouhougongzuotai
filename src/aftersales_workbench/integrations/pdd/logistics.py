"""官方轨迹接口只读适配；错误码组合必须精确，禁止把技术失败认作无轨迹。"""

from aftersales_workbench.integrations.pdd.client import PddApiError


class PddTraceNeedsReview(ValueError):
    """成功响应不是已核实的无轨迹格式，锁存保护，不能后续又用空响应放款。"""


def query_no_trace(client, *, carrier_id: str, tracking_number: str) -> dict:
    body = client.execute_read("pdd.logistics.companies.get")
    rows = body.get("logistics_companies_get_response", {}).get("logistics_companies")
    if not isinstance(rows, list):
        raise ValueError("拼多多快递公司列表响应不完整")
    matches = [r for r in rows if isinstance(r, dict) and str(r.get("id")) == carrier_id]
    if len(matches) != 1 or not matches[0].get("code") or matches[0].get("available") != 1:
        raise ValueError("拼多多快递公司未唯一匹配或不支持")
    code = str(matches[0]["code"])
    try:
        client.execute_read(
            "pdd.logistics.ordertrace.get", company_code=code, mail_no=tracking_number, cache=False
        )
    except PddApiError as exc:
        if str(exc.error_code) != "50001" or exc.sub_code != "ISV_TRACK_ERROR":
            raise
        # 已通过官方 /pop/error/solution 核对：该子码为“轨迹不存在/该运单暂无轨迹”。
        return {
            "source": "PDD",
            "result": "NO_TRACE",
            "error_code": "50001",
            "sub_code": "ISV_TRACK_ERROR",
            "request_id": exc.request_id,
            "carrier_id": carrier_id,
            "company_code": code,
            "tracking_number": tracking_number,
        }
    # 即使是空对象也不猜测含义；当前分支只使用明确无轨迹业务码。
    raise PddTraceNeedsReview("拼多多返回了非明确无轨迹响应，需核查历史物流后再处理")
