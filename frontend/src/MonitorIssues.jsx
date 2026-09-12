import { useEffect, useState } from "react";
import { ArrowsClockwise, CaretDown, CaretUp, WarningCircle } from "@phosphor-icons/react";
import "./monitor-issues.css";
import { issuesRequestParams, loadIssuesSnapshot } from "./monitor-issues-response.mjs";

const labels = { OPEN: "未解决", ACKNOWLEDGED: "人工跟进", RESOLVED: "已恢复", STOPPED: "已停止（非成功）", ALL: "全部历史" };
const platforms = { PDD: "拼多多", TMALL: "天猫", TAOBAO: "淘宝", "1688": "1688", JD: "京东", DOUYIN: "抖音" };
const initial = { state: "OPEN", category: "", platform: "", shop_id: "", keyword: "" };
const time = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }) : "—";

export function MonitorIssues({ onOpenOrder, focus = null, onClearFocus }) {
  const [filters, setFilters] = useState(initial);
  const [draft, setDraft] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [expanded, setExpanded] = useState(null);
  const [ackItem, setAckItem] = useState(null);
  const [ackReason, setAckReason] = useState("");
  const [saving, setSaving] = useState(false);
  const acknowledge = async (event) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    try {
      const response = await fetch("/api/v1/monitor/issues/acknowledge", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Workbench-Action": "acknowledge-sync-issue" },
        body: JSON.stringify({ key: ackItem.key, expected_revision: ackItem.revision, reason: ackReason.trim() }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "保存未确认，请刷新核实");
      setAckItem(null); setAckReason(""); setRefresh((v) => v + 1);
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  };

  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    let hasLoaded = false;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      setLoading(true);
      const params = issuesRequestParams(filters, page, focus);
      try {
        const result = await loadIssuesSnapshot(`/api/v1/monitor/issues?${params}`, { signal: controller.signal, expectedStageId: focus?.stageId });
        if (controller.signal.aborted) return;
        setData(result);
        if (focus && !hasLoaded && result.items.length === 1) setExpanded(result.items[0].key);
        hasLoaded = true;
        setError("");
      } catch (e) {
        if (!controller.signal.aborted && e.name !== "AbortError") setError(typeof e.message === "string" ? e.message : "异常明细暂时无法读取");
      } finally {
        inFlight = false;
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    load();
    const timer = window.setInterval(load, 15000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [filters, page, refresh, focus]);

  const change = (key, value) => {
    setFilters((current) => ({ ...current, [key]: value, ...(key === "platform" ? { shop_id: "" } : {}) }));
    setPage(1);
    setExpanded(null);
  };
  const shops = (data?.shops || []).filter((shop) => !filters.platform || shop.platform === filters.platform);

  return <section className="monitor-issues" aria-labelledby="monitor-issues-title">
    <header className="issues-heading">
      <div><h2 id="monitor-issues-title">{focus ? `${focus.label} · 当前告警明细` : "异常明细"}</h2><p>刷新仅更新展示，不执行退款、补单或发消息。{!focus && "未解决问题持续保留，不随下一轮正常运行自动清零。"}</p></div>
      <button type="button" className="button secondary" disabled={loading} onClick={() => setRefresh((v) => v + 1)}><ArrowsClockwise size={16} className={loading ? "spin" : ""} />刷新明细</button>
    </header>
    {focus && <div className="issues-focus" role="status"><div><strong>已定位：{focus.label}</strong><p>{data?.focus?.message ?? "正在查找这条告警对应的具体异常…"}</p>{!data?.items?.length && data?.focus?.stage_error && <p>{data.focus.stage_error}</p>}{data?.focus?.cycle_changed && <small>运行周期已更新，下方为该阶段最新核验结果。</small>}</div><button type="button" className="button secondary" onClick={onClearFocus}>查看全部异常</button></div>}
    {!focus && <><div className="issues-tabs" aria-label="异常处理状态">
      {Object.entries(labels).map(([key, label]) => <button type="button" key={key} aria-pressed={filters.state === key} className={filters.state === key ? "is-active" : ""} onClick={() => change("state", key)}>{label}{key !== "ALL" && <strong>{data?.counts?.[key] ?? "—"}</strong>}</button>)}
    </div>
    <form className="issues-filters" onSubmit={(event) => { event.preventDefault(); change("keyword", draft.trim()); }}>
      <label>异常类型<select value={filters.category} onChange={(e) => change("category", e.target.value)}><option value="">全部类型</option>{(data?.categories || []).map((item) => <option key={item.id} value={item.id}>{item.label}（未解决 {data.category_counts[item.id] || 0}）</option>)}</select></label>
      <label>平台<select value={filters.platform} onChange={(e) => change("platform", e.target.value)}><option value="">全部平台</option>{Object.entries(platforms).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>店铺<select value={filters.shop_id} onChange={(e) => change("shop_id", e.target.value)}><option value="">全部店铺</option>{shops.map((shop) => <option key={shop.shop_id} value={shop.shop_id}>{shop.shop_name}</option>)}</select></label>
      <label className="issues-search">搜索<input value={draft} maxLength={100} onChange={(e) => setDraft(e.target.value)} placeholder="订单号 / 运单 / 业务员 / 原因 / 任务号" /></label>
      <button type="submit" className="button primary">查询</button>
      <button type="button" className="button secondary" onClick={() => { setFilters(initial); setDraft(""); setPage(1); setExpanded(null); }}>重置</button>
    </form></>}
    {error && <div className="issues-error" role="alert"><WarningCircle size={18} /><span>{error} {data ? "下方为上次成功读取的结果，不能当作最新状态。" : "未能读取，不代表没有异常。"}</span></div>}
    {ackItem && <form className="issue-help" onSubmit={acknowledge}>
      <div><h3>已知悉，转人工跟进 · {ackItem.platform_order_sn}</h3><p>仅移出未解决告警，保留后台同步重查和异常隔离；不退款、不补单、不发送 ERP 待办。原因变化会重新告警，真正同步恢复后自动转为已恢复。</p>
      <label>人工跟进说明<input required maxLength={500} value={ackReason} onChange={(e) => setAckReason(e.target.value)} placeholder="填写处理人或后续跟进安排" /></label>
      <button className="button primary" disabled={saving || !ackReason.trim()}>确认转人工跟进</button><button type="button" className="button secondary" disabled={saving} onClick={() => setAckItem(null)}>取消</button></div>
    </form>}
    <div className="issues-table-wrap" aria-busy={loading}>
      <table className="issues-table"><thead><tr><th>异常 / 状态</th><th>平台订单号 / 店铺</th><th>归属业务员</th><th>原因</th><th>最近核验</th><th>操作</th></tr></thead>
        <tbody>{(data?.items || []).map((item) => <IssueRows key={item.key} item={item} expanded={expanded === item.key} onExpand={() => setExpanded(expanded === item.key ? null : item.key)} onOpenOrder={onOpenOrder} onAcknowledge={() => { setAckItem(item); setAckReason(""); }} />)}</tbody>
      </table>
      {!data?.items?.length && <div className="issues-empty">{loading ? "正在读取异常明细…" : error ? "请恢复读取后再确认异常数量" : focus ? data?.focus?.message : "当前筛选下没有异常记录"}</div>}
    </div>
    <footer className="issues-footer"><span>共 {data?.pagination?.total ?? "—"} 项 · 最近读取 {time(data?.checked_at)}</span><div><button type="button" className="button secondary" disabled={page <= 1 || loading} onClick={() => setPage(page - 1)}>上一页</button><span>{page} / {data?.pagination?.pages || 1}</span><button type="button" className="button secondary" disabled={loading || page >= (data?.pagination?.pages || 1)} onClick={() => setPage(page + 1)}>下一页</button></div></footer>
    <p className="issues-note">{data?.history_note || "历史从监控首次观察开始保留；同一订单可能存在不同异常，数量按异常项统计。"}</p>
  </section>;
}

function IssueRows({ item, expanded, onExpand, onOpenOrder, onAcknowledge }) {
  return <>
    <tr>
      <td><strong>{item.category_label}</strong><span className={`issue-state issue-state-${item.pending_confirmation && item.state === "OPEN" ? "pending" : item.state.toLowerCase()}`}>{item.pending_confirmation && item.state === "OPEN" ? "退款结果待确认" : labels[item.state]}</span></td>
      <td><span className="issue-order-number">{item.platform_order_sn || "无对应平台订单"}</span><small>{platforms[item.platform] || "系统"} · {item.shop_name || "运行阶段"}</small></td>
      <td>{item.sales_owner || "—"}</td>
      <td className="issue-reason">{item.reason}{item.acknowledgement_reason && <p>人工跟进：{item.acknowledgement_reason}</p>}{item.can_acknowledge && <button type="button" className="button secondary" onClick={onAcknowledge}>已知悉，转人工跟进</button>}</td>
      <td><time>{time(item.checked_at)}</time></td>
      <td><div className="issue-actions">{item.can_open_order ? <button type="button" className="button secondary" onClick={() => onOpenOrder(item)}>查看订单</button> : <small>{item.scope_label || "尚未同步到订单列表"}</small>}<button type="button" className="issue-expand" aria-expanded={expanded} onClick={onExpand}>{expanded ? "收起" : "如何处理"}{expanded ? <CaretUp size={14} /> : <CaretDown size={14} />}</button></div></td>
    </tr>
    {expanded && <tr className="issue-expanded"><td colSpan={6}>
      <div className="issue-help"><div><h3>处理建议</h3><p>{item.suggestion}</p><p>处理后由对应后台重新核验。这里不提供一键退款、强制补单、删除错误或把未知发送结果直接重试。</p></div><dl><dt>售后单号</dt><dd>{item.after_sales_sn || "—"}</dd><dt>任务编号</dt><dd>{item.task_id || "—"}</dd><dt>发货运单</dt><dd>{item.tracking_number || "—"}</dd>{item.target_group && <><dt>目标群</dt><dd>{item.target_group}</dd></>}<dt>首次发现</dt><dd>{time(item.first_seen_at)}</dd><dt>计划复查</dt><dd>{time(item.next_check_at)}</dd>{item.resolved_at && <><dt>记录恢复 / 停止</dt><dd>{time(item.resolved_at)}</dd></>}</dl></div>
      <h3>异常变化记录</h3><ol className="issue-events">{(item.events || []).slice(-20).reverse().map((event, index) => <li key={`${event.at}-${index}`}><time>{time(event.at)}</time><strong>{labels[event.state]}</strong><span>{event.reason}</span></li>)}</ol>
    </td></tr>}
  </>;
}
