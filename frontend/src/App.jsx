import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowCounterClockwise,
  ArrowsClockwise,
  Barcode,
  CalendarBlank,
  CaretLeft,
  CaretRight,
  ChartBar,
  CheckCircle,
  ClipboardText,
  Copy,
  DownloadSimple,
  ListBullets,
  MagnifyingGlass,
  Package,
  Plus,
  Trash,
  Truck,
  User,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { ScrapWorkspace } from "./ScrapWorkspace.jsx";

const PAGE_SIZE_OPTIONS = [15, 30, 50];
const PLATFORM_OPTIONS = [
  { value: "PDD", label: "拼多多" },
  { value: "TMALL", label: "天猫" },
  { value: "TAOBAO", label: "淘宝" },
  { value: "1688", label: "1688" },
  { value: "JD", label: "京东" },
  { value: "DOUYIN", label: "抖音" },
];

function inputDate(date) {
  const offset = date.getTimezoneOffset();
  return new Date(date.getTime() - offset * 60_000).toISOString().slice(0, 10);
}

function createInitialFilters() {
  const end = new Date();
  const start = new Date(end);
  start.setDate(end.getDate() - 7);
  return {
    platform: "",
    shop_id: "",
    sales_owner: "",
    after_sales_type: "",
    workflow_status: "",
    logistics_state: "",
    started_on: inputDate(start),
    ended_on: inputDate(end),
    keyword: "",
  };
}

function createInterceptFilters() {
  return {
    shop_id: "",
    sales_owner: "",
    stage: "",
    keyword: "",
  };
}

function createAttributionFilters() {
  const current = new Date();
  const currentDate = inputDate(current);
  return {
    platform: "",
    shop_id: "",
    reason_category: "",
    period_mode: "MONTH",
    period_month: currentDate.slice(0, 7),
    period_year: currentDate.slice(0, 4),
    started_on: `${currentDate.slice(0, 7)}-01`,
    ended_on: currentDate,
    model_keyword: "",
  };
}

function attributionRequestFilters(filters) {
  const currentDate = inputDate(new Date());
  const request = {
    platform: filters.platform,
    shop_id: filters.shop_id,
    reason_category: filters.reason_category,
    period_mode: filters.period_mode,
    model_keyword: filters.model_keyword,
  };
  if (filters.period_mode === "MONTH") {
    const [year, month] = filters.period_month.split("-").map(Number);
    const monthEnd = inputDate(new Date(year, month, 0));
    request.started_on = `${filters.period_month}-01`;
    request.ended_on = filters.period_month === currentDate.slice(0, 7) ? currentDate : monthEnd;
  } else if (filters.period_mode === "YEAR") {
    request.started_on = `${filters.period_year}-01-01`;
    request.ended_on = filters.period_year === currentDate.slice(0, 4) ? currentDate : `${filters.period_year}-12-31`;
  } else {
    request.started_on = filters.started_on;
    request.ended_on = filters.ended_on;
  }
  return request;
}

function createManualFilters() {
  const end = new Date();
  const start = new Date(end);
  start.setDate(end.getDate() - 30);
  return {
    task_status: "",
    assignee: "",
    origin: "",
    started_on: inputDate(start),
    ended_on: inputDate(end),
    keyword: "",
  };
}

const formatDateTime = (value, includeYear = false) => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const options = includeYear
    ? { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }
    : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
  return new Intl.DateTimeFormat("zh-CN", options).format(date).replaceAll("/", "-");
};

const formatCurrency = (value) => {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
  }).format(value);
};

function IconButton({ label, children, onClick, className = "" }) {
  return (
    <button className={`icon-button ${className}`} type="button" aria-label={label} title={label} onClick={onClick}>
      {children}
    </button>
  );
}

function StatusTag({ tone = "neutral", children }) {
  return <span className={`status-tag status-${tone}`}>{children}</span>;
}

function SummaryStrip({ summary, onManual }) {
  const items = [
    { label: "今日新增", value: summary.today_new ?? 0, tone: "blue" },
    { label: "待拦截", value: summary.pending_intercept ?? 0, tone: "orange" },
    { label: "待人工", value: summary.manual ?? 0, tone: "orange", onClick: onManual },
    { label: "已完成", value: summary.completed ?? 0, tone: "green" },
  ];
  return (
    <section className="summary-strip" aria-label="售后订单摘要">
      {items.map((item) => item.onClick ? (
        <button className="summary-item summary-button" type="button" key={item.label} onClick={item.onClick}>
          <span>{item.label}</span>
          <strong className={`metric-${item.tone}`}>{item.value}</strong>
          <small>查看待办明细</small>
        </button>
      ) : (
        <div className="summary-item" key={item.label}><span>{item.label}</span><strong className={`metric-${item.tone}`}>{item.value}</strong></div>
      ))}
    </section>
  );
}

const RECORD_VIEWS = [
  { id: "WORKBENCH", label: "工作台待处理", countKey: "workbench" },
  { id: "RECORD_ONLY", label: "仅记录", countKey: "record_only" },
  { id: "ALL", label: "全部售后", countKey: "all" },
];

function RecordViewTabs({ activeView, counts, onChange }) {
  return (
    <div className="record-view-tabs" role="tablist" aria-label="售后记录分类">
      {RECORD_VIEWS.map((view) => (
        <button
          type="button"
          role="tab"
          aria-selected={activeView === view.id}
          className={activeView === view.id ? "active" : ""}
          key={view.id}
          onClick={() => onChange(view.id)}
        >
          <span>{view.label}</span>
          <strong>{counts?.[view.countKey] ?? 0}</strong>
        </button>
      ))}
    </div>
  );
}

function Sidebar({ activeView, onNavigate }) {
  const nav = [
    { id: "orders", label: "售后订单", icon: ClipboardText, enabled: true },
    { id: "intercepts", label: "在途拦截", icon: Truck, enabled: true },
    { id: "warehouse", label: "仓库验货", icon: Package, enabled: true },
    { id: "attribution", label: "售后归因", icon: ChartBar, enabled: true },
    { id: "scrap", label: "退货报废", icon: Trash, enabled: true },
    { id: "manual", label: "人工待办", icon: User, enabled: true },
    { id: "monitor", label: "运行监控", icon: ChartBar, enabled: true },
  ];
  return (
    <aside className="sidebar">
      <div className="brand">利德仕售后工作台</div>
      <nav aria-label="主导航">
        {nav.map(({ id, label, icon: Icon, enabled }) => (
          <button
            key={id}
            className={`nav-item ${activeView === id ? "active" : ""}`}
            type="button"
            aria-current={activeView === id ? "page" : undefined}
            disabled={!enabled}
            title={enabled ? label : `${label}将在后续阶段开放`}
            onClick={() => enabled && onNavigate(id)}
          >
            <Icon size={21} weight={activeView === id ? "fill" : "regular"} />
            <span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <span className="sidebar-dot" />
        模块 1/2/3 状态见运行监控
      </div>
    </aside>
  );
}

function FilterPanel({ draft, setDraft, onSubmit, onReset, shops, salesOwners, busy }) {
  const update = (key) => (event) => setDraft((current) => ({ ...current, [key]: event.target.value }));
  const changePlatform = (event) => setDraft((current) => ({ ...current, platform: event.target.value, shop_id: "" }));
  const platformShops = draft.platform ? shops.filter((shop) => shop.platform === draft.platform) : [];
  const selectedPlatformLabel = PLATFORM_OPTIONS.find((item) => item.value === draft.platform)?.label;
  return (
    <form className="filters" onSubmit={onSubmit}>
      <div className="filter-row filter-row-primary">
        <label>
          <span>平台</span>
          <select value={draft.platform} onChange={changePlatform}>
            <option value="">全部平台</option>
            {PLATFORM_OPTIONS.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}
          </select>
        </label>
        <label>
          <span>店铺</span>
          <select value={draft.shop_id} onChange={update("shop_id")} disabled={!draft.platform}>
            <option value="">{draft.platform ? `全部${selectedPlatformLabel}店铺` : "请先选择平台"}</option>
            {platformShops.map((shop) => <option value={shop.shop_id} key={shop.shop_id}>{shop.shop_name}</option>)}
            {draft.platform && !platformShops.length && <option value="" disabled>暂无已接入店铺</option>}
          </select>
        </label>
        <label>
          <span>归属业务员</span>
          <select value={draft.sales_owner} onChange={update("sales_owner")}>
            <option value="">全部</option>
            {salesOwners.map((owner) => <option value={owner} key={owner}>{owner}</option>)}
          </select>
        </label>
        <label>
          <span>售后类型</span>
          <select value={draft.after_sales_type} onChange={update("after_sales_type")}>
            <option value="">全部</option>
            <option value="ONLY_REFUND">仅退款</option>
            <option value="RETURN_AND_REFUND">退货退款</option>
            <option value="EXCHANGE">换货</option>
          </select>
        </label>
        <label>
          <span>处理状态</span>
          <select value={draft.workflow_status} onChange={update("workflow_status")}>
            <option value="">全部</option>
            <option value="PENDING_CHECK">待系统判定</option>
            <option value="INTERCEPT_WAITING_RETURN">已签收转人工</option>
            <option value="INTERCEPT_REFUNDED_WAITING_RETURN">已退款待退回</option>
            <option value="MANUAL_PROCESSING">人工处理中</option>
          </select>
        </label>
        <label>
          <span>物流状态</span>
          <select value={draft.logistics_state} onChange={update("logistics_state")}>
            <option value="">全部</option>
            <option value="IN_TRANSIT">运输中</option>
            <option value="OUT_FOR_DELIVERY">派件中</option>
            <option value="DELIVERED">已签收</option>
            <option value="RETURNING">退回中</option>
            <option value="RETURNED">已退回</option>
          </select>
        </label>
      </div>
      <div className="filter-row filter-row-secondary">
        <div className="date-field">
          <span>申请时间</span>
          <div className="date-range">
            <CalendarBlank size={16} />
            <input type="date" value={draft.started_on} onChange={update("started_on")} aria-label="开始日期" />
            <b>~</b>
            <input type="date" value={draft.ended_on} onChange={update("ended_on")} aria-label="结束日期" />
          </div>
        </div>
        <label className="search-field">
          <MagnifyingGlass size={17} />
          <input value={draft.keyword} onChange={update("keyword")} placeholder="订单号/售后单号/快递单号" />
        </label>
        <button type="button" className="button secondary" onClick={onReset}>
          <ArrowCounterClockwise size={16} />重置
        </button>
        <button type="submit" className="button primary" disabled={busy}>
          <MagnifyingGlass size={16} />查询
        </button>
      </div>
    </form>
  );
}

function EmptyTable({ loading, error, onRetry }) {
  if (loading) {
    return (
      <div className="table-state" role="status">
        <ArrowsClockwise className="spin" size={26} />正在读取售后订单…
      </div>
    );
  }
  if (error) {
    return (
      <div className="table-state error-state">
        <WarningCircle size={28} />
        <strong>售后记录暂时无法读取</strong>
        <span>{error}</span>
        <button type="button" className="button secondary" onClick={onRetry}>重新加载</button>
      </div>
    );
  }
  return (
    <div className="table-state">
      <ListBullets size={28} />当前筛选条件下没有售后订单
    </div>
  );
}

function OrdersTable({ items, selected, onSelect, loading, error, onRetry }) {
  return (
    <div className="table-wrap">
      <table className="orders-table">
        <thead>
          <tr>
            <th>店铺</th><th>售后单号</th><th>平台订单号</th><th>归属业务员</th><th>类型</th><th>退款金额</th><th>退款范围</th><th>发货运单</th>
            <th>物流状态</th><th>拦截状态</th><th>平台退款</th><th>最近更新</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr
              key={item.after_sales_sn}
              className={selected === item.after_sales_sn ? "selected" : ""}
              onClick={() => onSelect(item.after_sales_sn)}
            >
              <td title={`${item.platform_label || ""} ${item.shop_name}`}><div className="stacked-cell"><b className="truncate shop-cell">{item.shop_name}</b><small>{item.platform_label || "—"}</small></div></td>
              <td className="mono">{item.after_sales_sn}</td>
              <td className="mono" title={item.platform_order_sn}>{item.platform_order_sn}</td>
              <td title={item.erp_customer_name}><StatusTag tone={item.sales_owner_tone}>{item.sales_owner}</StatusTag></td>
              <td>{item.after_sales_type_label}</td>
              <td>{formatCurrency(item.refund_amount)}</td>
              <td title={`买家实付 ${formatCurrency(item.platform_order_amount)} · 平台优惠 ${formatCurrency(item.platform_discount_amount)} · 商家应收 ${formatCurrency(item.merchant_receivable_amount)}`}><StatusTag tone={item.refund_scope === "全额退款" ? "success" : item.refund_scope === "部分退款/补偿" ? "warning" : "neutral"}>{item.refund_scope}</StatusTag></td>
              <td title={`${item.carrier_name} ${item.tracking_number}`}><span className="truncate tracking-cell">{item.tracking_number}</span></td>
              <td><StatusTag tone={item.logistics_tone}>{item.logistics_label}</StatusTag></td>
              <td><StatusTag tone={item.intercept_tone}>{item.intercept_label}</StatusTag></td>
              <td><span className={`refund-text refund-${item.platform_refund_tone}`}>{item.platform_refund_label}</span></td>
              <td>{formatDateTime(item.updated_at)}</td>
              <td><button type="button" className="link-button" onClick={(event) => { event.stopPropagation(); onSelect(item.after_sales_sn); }}>查看详情</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {!items.length && <EmptyTable loading={loading} error={error} onRetry={onRetry} />}
      {loading && items.length > 0 && <div className="table-loading"><ArrowsClockwise className="spin" size={18} />刷新中</div>}
    </div>
  );
}

function Pagination({ pagination, onPage, onPageSize }) {
  const { page, pages, total, page_size: pageSize } = pagination;
  const visiblePages = useMemo(() => {
    const values = [];
    const start = Math.max(1, Math.min(page - 2, pages - 4));
    const end = Math.min(pages, Math.max(5, page + 2));
    for (let value = start; value <= end; value += 1) values.push(value);
    return values;
  }, [page, pages]);
  return (
    <div className="table-footer">
      <div className="table-total">共 <strong>{total}</strong> 条</div>
      <div className="pagination">
        <select aria-label="每页记录数" value={pageSize} onChange={(event) => onPageSize(Number(event.target.value))}>
          {PAGE_SIZE_OPTIONS.map((size) => <option value={size} key={size}>{size}条/页</option>)}
        </select>
        <IconButton label="上一页" onClick={() => onPage(page - 1)} className={page <= 1 ? "disabled" : ""}><CaretLeft size={16} /></IconButton>
        {visiblePages[0] > 1 && <><button type="button" onClick={() => onPage(1)}>1</button><span>…</span></>}
        {visiblePages.map((value) => <button type="button" className={value === page ? "current" : ""} key={value} onClick={() => onPage(value)}>{value}</button>)}
        {visiblePages.at(-1) < pages && <><span>…</span><button type="button" onClick={() => onPage(pages)}>{pages}</button></>}
        <IconButton label="下一页" onClick={() => onPage(page + 1)} className={page >= pages ? "disabled" : ""}><CaretRight size={16} /></IconButton>
      </div>
    </div>
  );
}

function InterceptSummaryStrip({ summary, onStage }) {
  const items = [
    { label: "待发拦截", value: summary.waiting_notice ?? 0, tone: "orange", stage: "WAITING_NOTICE" },
    { label: "退款冻结", value: summary.refund_blocked ?? 0, tone: "orange", stage: "REFUND_BLOCKED" },
    { label: "已退款待退回", value: summary.waiting_return ?? 0, tone: "blue", stage: "WAITING_RETURN" },
    { label: "待仓库开单", value: summary.waiting_warehouse_order ?? 0, tone: "blue", stage: "ERP_NOT_FOUND" },
    { label: "暂存待认领", value: summary.staged ?? 0, tone: "orange", stage: "ERP_STAGED" },
    { label: "客户名下待平账", value: summary.receivable_open ?? 0, tone: "orange", stage: "ERP_RECEIVABLE_OPEN" },
    { label: "售后已闭环", value: summary.closed_loop ?? 0, tone: "green", stage: "CLOSED_LOOP" },
  ];
  return (
    <section className="summary-strip intercept-summary" aria-label="在途拦截和退货闭环摘要">
      {items.map((item) => (
        <button className="summary-item summary-button" type="button" key={item.label} onClick={() => onStage(item.stage)}>
          <span>{item.label}</span>
          <strong className={`metric-${item.tone}`}>{item.value}</strong>
          <small>点击查看</small>
        </button>
      ))}
    </section>
  );
}

function InterceptFilterPanel({ draft, setDraft, onSubmit, onReset, shops, salesOwners, busy }) {
  const update = (key) => (event) => setDraft((current) => ({ ...current, [key]: event.target.value }));
  return (
    <form className="filters intercept-filters" onSubmit={onSubmit}>
      <div className="filter-row filter-row-primary">
        <label>
          <span>店铺</span>
          <select value={draft.shop_id} onChange={update("shop_id")}>
            <option value="">全部</option>
            {shops.filter((shop) => !shop.platform || shop.platform === "PDD").map((shop) => <option value={shop.shop_id} key={shop.shop_id}>{shop.platform_label ? `${shop.platform_label} · ` : ""}{shop.shop_name}</option>)}
          </select>
        </label>
        <label>
          <span>归属业务员</span>
          <select value={draft.sales_owner} onChange={update("sales_owner")}>
            <option value="">全部</option>
            {salesOwners.map((owner) => <option value={owner} key={owner}>{owner}</option>)}
          </select>
        </label>
        <label>
          <span>当前环节</span>
          <select value={draft.stage} onChange={update("stage")}>
            <option value="">全部</option>
            <option value="WAITING_NOTICE">待发拦截</option>
            <option value="NOTICE_SENT">拦截已发送</option>
            <option value="REFUND_BLOCKED">退款冻结</option>
            <option value="WAITING_RETURN">已退款待退回</option>
            <option value="ERP_MATCH">全部待匹配ERP退货单</option>
            <option value="ERP_NOT_FOUND">待仓库开退货单</option>
            <option value="ERP_STAGED">暂存列表待认领</option>
            <option value="ERP_RECEIVABLE_OPEN">客户名下待平账</option>
            <option value="ERP_EXCEPTION">ERP匹配异常</option>
            <option value="CLOSED_LOOP">售后已闭环</option>
            <option value="MANUAL">人工处理</option>
          </select>
        </label>
        <label className="search-field intercept-search">
          <MagnifyingGlass size={17} />
          <input value={draft.keyword} onChange={update("keyword")} placeholder="订单号/售后单号/快递单号" />
        </label>
        <button type="button" className="button secondary" onClick={onReset}>
          <ArrowCounterClockwise size={16} />重置
        </button>
        <button type="submit" className="button primary" disabled={busy}>
          <MagnifyingGlass size={16} />查询
        </button>
      </div>
    </form>
  );
}

function InterceptTable({ items, selected, onSelect, loading, error, onRetry }) {
  return (
    <div className="table-wrap intercept-table-wrap">
      <table className="intercept-table">
        <thead>
          <tr>
            <th>店铺</th><th>归属业务员</th><th>售后单号</th><th>平台订单号</th><th>快递群 / 运单</th><th>拦截通知</th>
            <th>物流状态</th><th>退款闸门</th><th>ERP退货 / 平账</th><th>当前环节</th><th>最近更新</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr
              key={item.after_sales_sn}
              className={selected === item.after_sales_sn ? "selected" : ""}
              onClick={() => onSelect(item.after_sales_sn)}
              title={item.latest_error || item.logistics_context}
            >
              <td title={item.shop_name}><span className="truncate">{item.shop_name}</span></td>
              <td><StatusTag tone={item.sales_owner_tone}>{item.sales_owner}</StatusTag></td>
              <td className="mono">{item.after_sales_sn}</td>
              <td className="mono" title={item.platform_order_sn}>{item.platform_order_sn}</td>
              <td title={`${item.target_group} / ${item.carrier_name} ${item.tracking_number}`}>
                <span className="stacked-cell"><b>{item.target_group}</b><small>{item.tracking_number}</small></span>
              </td>
              <td><StatusTag tone={item.notice_tone}>{item.notice_label}</StatusTag></td>
              <td title={item.logistics_context}><StatusTag tone={item.logistics_tone}>{item.logistics_label}</StatusTag></td>
              <td><StatusTag tone={item.refund_gate_tone}>{item.refund_gate_label}</StatusTag></td>
              <td title={`${item.erp_match_context || ""}${item.erp_match_checked_at ? ` · 核对于 ${formatDateTime(item.erp_match_checked_at, true)}` : ""}`}>
                <span className="stacked-cell"><StatusTag tone={item.erp_match_tone}>{item.erp_match_label}</StatusTag><small>{item.erp_return_order_sn || (item.erp_receivable_amount !== null && item.erp_receivable_amount !== undefined ? `累计应收 ${formatCurrency(Number(item.erp_receivable_amount))}` : "—")}</small></span>
              </td>
              <td><StatusTag tone={item.workflow_tone}>{item.workflow_label}</StatusTag></td>
              <td>{formatDateTime(item.updated_at)}</td>
              <td><button type="button" className="link-button" onClick={(event) => { event.stopPropagation(); onSelect(item.after_sales_sn); }}>查看详情</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {!items.length && <EmptyTable loading={loading} error={error} onRetry={onRetry} />}
      {loading && items.length > 0 && <div className="table-loading"><ArrowsClockwise className="spin" size={18} />刷新中</div>}
    </div>
  );
}

function InterceptWorkspace({ detailOpen, setDetailOpen }) {
  const [draftFilters, setDraftFilters] = useState(createInterceptFilters);
  const [filters, setFilters] = useState(createInterceptFilters);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(15);
  const [data, setData] = useState({ summary: {}, shops: [], sales_owners: [], items: [], pagination: { page: 1, page_size: 15, total: 0, pages: 1 }, last_synced_at: null });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  const loadIntercepts = useCallback(async (signal) => {
    setLoading(true);
    setError("");
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    try {
      const response = await fetch(`/api/v1/aftersales/intercepts?${params}`, { signal });
      if (!response.ok) throw new Error(`服务返回 ${response.status}`);
      const payload = await response.json();
      setData(payload);
      setSelected((current) => (
        payload.items.some((item) => item.after_sales_sn === current)
          ? current
          : (payload.items[0]?.after_sales_sn ?? "")
      ));
    } catch (requestError) {
      if (requestError.name !== "AbortError") setError("模块1拦截记录暂时无法读取，请检查本地服务。");
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, [filters, page, pageSize, refreshKey]);

  useEffect(() => {
    const controller = new AbortController();
    loadIntercepts(controller.signal);
    return () => controller.abort();
  }, [loadIntercepts]);

  useEffect(() => {
    if (!selected) { setDetail(null); return undefined; }
    const controller = new AbortController();
    setDetailLoading(true);
    fetch(`/api/v1/aftersales/orders/${encodeURIComponent(selected)}`, { signal: controller.signal })
      .then((response) => { if (!response.ok) throw new Error(String(response.status)); return response.json(); })
      .then(setDetail)
      .catch((requestError) => { if (requestError.name !== "AbortError") setDetail(null); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selected]);

  const chooseOrder = (afterSalesSn) => { setSelected(afterSalesSn); setDetailOpen(true); };
  const submitFilters = (event) => { event.preventDefault(); setPage(1); setFilters(draftFilters); };
  const resetFilters = () => { const initial = createInterceptFilters(); setDraftFilters(initial); setFilters(initial); setPage(1); };
  const applyStage = (stage) => {
    setDraftFilters((current) => ({ ...current, stage }));
    setFilters((current) => ({ ...current, stage }));
    setPage(1);
  };
  const copyValue = async (value) => { await navigator.clipboard.writeText(String(value)); setCopied(true); window.setTimeout(() => setCopied(false), 1500); };

  return (
    <>
      <main className="workspace">
        <header className="topbar">
          <div className="page-title"><Truck size={22} /><h1>在途拦截</h1><span className="read-only-badge">只读监控</span></div>
          <div className="sync-status"><span />模块1后台运行 · 最近同步 {formatDateTime(data.last_synced_at)}</div>
        </header>
        <div className="workspace-body">
          <InterceptSummaryStrip summary={data.summary} onStage={applyStage} />
          <InterceptFilterPanel draft={draftFilters} setDraft={setDraftFilters} onSubmit={submitFilters} onReset={resetFilters} shops={data.shops} salesOwners={data.sales_owners ?? []} busy={loading} />
          <InterceptTable items={data.items} selected={selected} onSelect={chooseOrder} loading={loading} error={error} onRetry={() => setRefreshKey((key) => key + 1)} />
          <div className="workspace-actions">
            <button type="button" className="button secondary" onClick={() => setRefreshKey((key) => key + 1)} disabled={loading}><ArrowsClockwise size={16} />刷新</button>
            <Pagination
              pagination={data.pagination}
              onPage={(nextPage) => { if (nextPage >= 1 && nextPage <= data.pagination.pages) setPage(nextPage); }}
              onPageSize={(nextSize) => { setPageSize(nextSize); setPage(1); }}
            />
          </div>
        </div>
      </main>
      {detailOpen && <DetailPanel detail={detail} loading={detailLoading} onClose={() => setDetailOpen(false)} onCopy={copyValue} copied={copied} />}
      {!detailOpen && selected && <button type="button" className="open-detail" onClick={() => setDetailOpen(true)}>打开售后详情</button>}
    </>
  );
}

const reasonColors = {
  DISLIKE: "#3979cf",
  QUALITY: "#e95b55",
  SPEC_MISMATCH: "#8a63d2",
  LOGISTICS: "#ee8a2f",
  DESCRIPTION: "#36a38a",
  PRICE: "#d4a72c",
  OTHER: "#8b96a7",
};

const formatDelta = (value) => {
  if (value === null || value === undefined) return "暂无可比基期";
  if (value === 0) return "持平";
  return `${value > 0 ? "+" : ""}${value}%`;
};

function FinancialSummary({ financial }) {
  const summary = financial.summary ?? {};
  const comparison = financial.comparison ?? {};
  const items = [
    { key: "actual_total", label: "实际退款成功金额", value: summary.actual_total, primary: true },
    { key: "actual_only_refund", label: "实际仅退款金额", value: summary.actual_only_refund },
    { key: "actual_return_refund", label: "实际退货退款金额", value: summary.actual_return_refund },
    { key: "application_total", label: "申请退款金额", value: summary.application_total, requested: true },
  ];
  return (
    <section className="financial-summary" aria-label="退款金额摘要">
      {items.map((item) => {
        const previous = comparison.previous?.deltas?.[item.key];
        const yearOverYear = comparison.year_over_year?.deltas?.[item.key];
        return (
          <article key={item.key} className={`${item.primary ? "primary" : ""} ${item.requested ? "requested" : ""}`}>
            <div className="financial-label"><span>{item.label}</span>{item.primary && <b>默认口径</b>}{item.requested && <b>辅助口径</b>}</div>
            <strong>{formatCurrency(item.value ?? 0)}</strong>
            <div className="financial-deltas">
              {comparison.previous && <span className={previous > 0 ? "worse" : previous < 0 ? "better" : "neutral"}>{comparison.previous.label} {formatDelta(previous)}</span>}
              {comparison.year_over_year && <span className={yearOverYear > 0 ? "worse" : yearOverYear < 0 ? "better" : "neutral"}>{comparison.year_over_year.label} {formatDelta(yearOverYear)}</span>}
            </div>
          </article>
        );
      })}
      <div className="financial-counts"><span>退款成功 <strong>{summary.successful_orders ?? 0}</strong> 单</span><span>退款申请 <strong>{summary.application_orders ?? 0}</strong> 单</span></div>
    </section>
  );
}

function RefundTrend({ financial, period }) {
  const rows = financial.trend ?? [];
  const maxValue = Math.max(1, ...rows.flatMap((row) => [row.actual_total ?? 0, row.application_total ?? 0]));
  const title = period.mode === "YEAR" ? `${period.started_on?.slice(0, 4) || "年度"} 十二个月退款趋势` : period.mode === "MONTH" ? `${period.started_on?.slice(0, 7) || "月度"} 每日退款趋势` : "自定义周期退款趋势";
  return (
    <section className="attribution-card refund-trend-card">
      <div className="card-heading">
        <div><h2>{title}</h2><p>柱形为实际退款成功金额，橙色短线为申请退款金额</p></div>
        <div className="trend-legend"><span><i className="only" />仅退款</span><span><i className="returned" />退货退款</span><span><i className="applied" />申请金额</span></div>
      </div>
      <div className="refund-trend-scroll">
        <div className={`refund-trend ${financial.granularity === "MONTH" ? "monthly" : "daily"}`}>
          {rows.map((row) => {
            const onlyHeight = Math.max(0, (row.actual_only_refund ?? 0) * 100 / maxValue);
            const returnHeight = Math.max(0, (row.actual_return_refund ?? 0) * 100 / maxValue);
            const applicationBottom = Math.max(0, (row.application_total ?? 0) * 100 / maxValue);
            const tooltip = `${row.label}｜实际 ${formatCurrency(row.actual_total)}（仅退款 ${formatCurrency(row.actual_only_refund)}，退货退款 ${formatCurrency(row.actual_return_refund)}）｜申请 ${formatCurrency(row.application_total)}`;
            return (
              <div key={row.key} className={`trend-column ${row.is_future ? "future" : ""}`} title={tooltip}>
                <div className="trend-plot">
                  <i className="application-marker" style={{ bottom: `${applicationBottom}%` }} />
                  <b className="return-bar" style={{ height: `${returnHeight}%`, bottom: `${onlyHeight}%` }} />
                  <b className="only-bar" style={{ height: `${onlyHeight}%` }} />
                </div>
                <span>{row.label}</span>
                {period.mode === "YEAR" && !row.is_future && <small><em className={row.mom_delta > 0 ? "worse" : row.mom_delta < 0 ? "better" : "neutral"}>环 {formatDelta(row.mom_delta)}</em><em className={row.yoy_delta > 0 ? "worse" : row.yoy_delta < 0 ? "better" : "neutral"}>同 {formatDelta(row.yoy_delta)}</em></small>}
              </div>
            );
          })}
          {!rows.length && <div className="attribution-empty">当前周期暂无退款金额趋势</div>}
        </div>
      </div>
    </section>
  );
}

function CoverageNotice({ coverage }) {
  const activePlatforms = (coverage.by_platform ?? []).filter((item) => item.application_orders > 0);
  return (
    <div className={`coverage-note ${coverage.period_complete ? "complete" : "partial"}`}>
      <WarningCircle size={17} />
      <div><strong>数据覆盖：</strong><span>{coverage.note || "暂无覆盖信息。"}</span>{activePlatforms.length > 1 && <p>{activePlatforms.map((item) => <b key={item.platform}>{item.platform_label} 状态覆盖 {item.status_coverage_rate}%</b>)}</p>}</div>
    </div>
  );
}

function AttributionSummary({ summary }) {
  const items = [
    { label: "退款申请单", value: summary.refund_applications ?? 0, suffix: "单" },
    { label: "涉及退款件数", value: summary.refund_units ?? 0, suffix: "件" },
    { label: "涉及型号", value: summary.model_count ?? 0, suffix: "款" },
    { label: "质量类占退款申请", value: summary.quality_issue_share ?? 0, suffix: "%", tone: "danger" },
  ];
  return (
    <section className="attribution-summary" aria-label="售后归因摘要">
      {items.map((item) => (
        <div key={item.label}>
          <span>{item.label}</span>
          <strong className={item.tone ? `attribution-${item.tone}` : ""}>{item.value}<small>{item.suffix}</small></strong>
        </div>
      ))}
      <div className="dominant-reason"><span>当前主要原因</span><strong>{summary.dominant_reason || "—"}</strong></div>
    </section>
  );
}

function AttributionFilters({ draft, setDraft, shops, categories, busy, onSubmit, onReset }) {
  const update = (key) => (event) => setDraft((current) => ({ ...current, [key]: event.target.value }));
  const changePlatform = (event) => setDraft((current) => ({ ...current, platform: event.target.value, shop_id: "" }));
  const platformShops = draft.platform ? shops.filter((shop) => shop.platform === draft.platform) : [];
  const selectedPlatformLabel = PLATFORM_OPTIONS.find((item) => item.value === draft.platform)?.label;
  return (
    <form className="attribution-filters" onSubmit={onSubmit}>
      <label><span>平台</span><select value={draft.platform} onChange={changePlatform}><option value="">全部平台</option>{PLATFORM_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
      <label><span>店铺</span><select value={draft.shop_id} onChange={update("shop_id")} disabled={!draft.platform}><option value="">{draft.platform ? `全部${selectedPlatformLabel}店铺` : "请先选择平台"}</option>{platformShops.map((shop) => <option key={shop.shop_id} value={shop.shop_id}>{shop.shop_name}</option>)}{draft.platform && !platformShops.length && <option value="" disabled>暂无已接入店铺</option>}</select></label>
      <label><span>原因大类</span><select value={draft.reason_category} onChange={update("reason_category")}><option value="">全部原因</option>{categories.map((category) => <option key={category.code} value={category.code}>{category.label}</option>)}</select></label>
      <label className="period-mode"><span>统计周期</span><select value={draft.period_mode} onChange={update("period_mode")}><option value="MONTH">月度</option><option value="YEAR">年度</option><option value="CUSTOM">自定义</option></select></label>
      {draft.period_mode === "MONTH" && <label className="period-value"><span>选择月份</span><input type="month" required value={draft.period_month} onChange={update("period_month")} /></label>}
      {draft.period_mode === "YEAR" && <label className="period-value"><span>选择年份</span><input type="number" min="2020" max="2100" required value={draft.period_year} onChange={update("period_year")} /></label>}
      {draft.period_mode === "CUSTOM" && <label className="attribution-date"><span>自定义日期</span><div><input type="date" required value={draft.started_on} onChange={update("started_on")} /><b>~</b><input type="date" required value={draft.ended_on} onChange={update("ended_on")} /></div></label>}
      <label className="attribution-search"><span>型号 / SKU</span><div><MagnifyingGlass size={15} /><input value={draft.model_keyword} onChange={update("model_keyword")} placeholder="例如 6050" /></div></label>
      <button type="button" className="button secondary" onClick={onReset}><ArrowCounterClockwise size={16} />重置</button>
      <button type="submit" className="button primary" disabled={busy}><MagnifyingGlass size={16} />查询</button>
    </form>
  );
}

function ModelRankingChart({ rows, focusModel, onFocus }) {
  const visible = rows.slice(0, 10);
  const maxValue = Math.max(1, ...visible.map((row) => row.refund_orders));
  return (
    <section className="attribution-card ranking-card">
      <div className="card-heading"><div><h2>高退款申请型号</h2><p>按退款申请单量排名，点击型号查看原因</p></div><span>TOP {visible.length}</span></div>
      <div className="ranking-bars">
        {visible.map((row, index) => (
          <button type="button" key={row.model_code} className={focusModel === row.model_code ? "active" : ""} onClick={() => onFocus(row.model_code)}>
            <b>{index + 1}</b><strong>{row.model_code}</strong>
            <span className="bar-track"><i style={{ width: `${Math.max(4, row.refund_orders * 100 / maxValue)}%` }} /></span>
            <em>{row.refund_orders}单</em>
          </button>
        ))}
        {!visible.length && <div className="attribution-empty">当前筛选条件下暂无型号数据</div>}
      </div>
    </section>
  );
}

function ReasonBreakdown({ rows, title = "退款原因构成" }) {
  const maxValue = Math.max(1, ...rows.map((row) => row.refund_orders));
  return (
    <section className="attribution-card reason-card">
      <div className="card-heading"><div><h2>{title}</h2><p>互斥分类，同一退款申请只计一次</p></div></div>
      <div className="reason-bars">
        {rows.map((row) => (
          <div key={row.code}>
            <span><i style={{ background: reasonColors[row.code] }} />{row.label}</span>
            <div><b style={{ width: `${Math.max(row.refund_orders ? 3 : 0, row.refund_orders * 100 / maxValue)}%`, background: reasonColors[row.code] }} /></div>
            <strong>{row.share}%</strong><em>{row.refund_orders}单</em>
          </div>
        ))}
      </div>
    </section>
  );
}

function FocusModelPanel({ focus }) {
  return (
    <section className="attribution-card focus-card">
      <div className="card-heading"><div><h2>{focus.model_code || "未选择型号"} 原因下钻</h2><p>{focus.refund_orders ?? 0} 笔退款申请的平台原始原因与 SKU 变体</p></div></div>
      <div className="focus-columns">
        <div><h3>平台原始原因</h3><ol>{(focus.raw_reasons ?? []).slice(0, 6).map((row) => <li key={row.reason}><span title={row.reason}>{row.reason}</span><strong>{row.refund_orders}单</strong></li>)}</ol></div>
        <div><h3>SKU 变体</h3><ol>{(focus.variants ?? []).slice(0, 6).map((row) => <li key={row.sku_code}><span title={row.sku_code}>{row.sku_code}</span><strong>{row.refund_orders}单 / {row.refund_units}件</strong></li>)}</ol></div>
      </div>
    </section>
  );
}

function AttributionTable({ rows, focusModel, onFocus }) {
  return (
    <section className="attribution-card attribution-table-card">
      <div className="card-heading"><div><h2>型号归因明细</h2><p>退款率仅在接入同期销售分母后计算</p></div></div>
      <div className="attribution-table-wrap"><table className="attribution-table"><thead><tr><th>型号</th><th>退款申请</th><th>退款件数</th><th>申请单占比</th><th>主要原因</th><th>该原因占比</th><th>覆盖店铺</th><th>退款率</th></tr></thead>
        <tbody>{rows.slice(0, 30).map((row) => <tr key={row.model_code} className={focusModel === row.model_code ? "selected" : ""} onClick={() => onFocus(row.model_code)}><td><strong>{row.model_code}</strong><small>{row.variant_count} 个 SKU</small></td><td>{row.refund_orders}单</td><td>{row.refund_units}件</td><td>{row.application_share}%</td><td><span className="reason-pill" style={{ color: reasonColors[row.top_reason_code], borderColor: `${reasonColors[row.top_reason_code]}55`, background: `${reasonColors[row.top_reason_code]}10` }}>{row.top_reason_label}</span></td><td>{row.top_reason_share}%</td><td>{row.shop_count}店</td><td><StatusTag tone="warning">待接销量</StatusTag></td></tr>)}</tbody>
      </table></div>
    </section>
  );
}

function AttributionWorkspace() {
  const [draft, setDraft] = useState(createAttributionFilters);
  const [filters, setFilters] = useState(createAttributionFilters);
  const [focusModel, setFocusModel] = useState("");
  const [data, setData] = useState({ summary: {}, financial: { summary: {}, comparison: {}, trend: [] }, period: {}, coverage: {}, reason_breakdown: [], model_ranking: [], focus: {}, shops: [], reason_categories: [], denominator: {} });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  const load = useCallback(async (signal) => {
    setLoading(true); setError("");
    const params = new URLSearchParams();
    Object.entries(attributionRequestFilters(filters)).forEach(([key, value]) => { if (value) params.set(key, value); });
    if (focusModel) params.set("focus_model", focusModel);
    try {
      const response = await fetch(`/api/v1/attribution/overview?${params}`, { signal });
      if (!response.ok) throw new Error(`服务返回 ${response.status}`);
      const payload = await response.json();
      setData(payload);
      if (!focusModel && payload.focus?.model_code) setFocusModel(payload.focus.model_code);
    } catch (requestError) {
      if (requestError.name !== "AbortError") setError("售后归因数据暂时无法读取，请检查数据库迁移和本地服务。");
    } finally { if (!signal.aborted) setLoading(false); }
  }, [filters, focusModel, refreshKey]);

  useEffect(() => { const controller = new AbortController(); load(controller.signal); return () => controller.abort(); }, [load]);
  const submit = (event) => { event.preventDefault(); setFocusModel(""); setFilters(draft); };
  const reset = () => { const initial = createAttributionFilters(); setDraft(initial); setFilters(initial); setFocusModel(""); };

  return (
    <main className="workspace attribution-workspace">
      <header className="topbar"><div className="page-title"><ChartBar size={22} /><h1>售后归因</h1><span className="read-only-badge">自动汇总</span></div><div className="sync-status"><span />最近同步 {formatDateTime(data.last_synced_at)}</div></header>
      <div className="attribution-body">
        <AttributionFilters draft={draft} setDraft={setDraft} shops={data.shops ?? []} categories={data.reason_categories ?? []} busy={loading} onSubmit={submit} onReset={reset} />
        {error ? <div className="attribution-error"><WarningCircle size={20} />{error}<button type="button" onClick={() => setRefreshKey((key) => key + 1)}>重试</button></div> : <>
          <FinancialSummary financial={data.financial ?? {}} />
          <RefundTrend financial={data.financial ?? {}} period={data.period ?? {}} />
          <CoverageNotice coverage={data.coverage ?? {}} />
          <AttributionSummary summary={data.summary ?? {}} />
          <div className="denominator-note"><WarningCircle size={17} /><span><strong>口径提示：</strong>{data.denominator?.note}</span></div>
          <div className="attribution-grid"><ModelRankingChart rows={data.model_ranking ?? []} focusModel={data.focus?.model_code} onFocus={setFocusModel} /><ReasonBreakdown rows={data.reason_breakdown ?? []} /></div>
          <div className="attribution-grid lower-grid"><ReasonBreakdown rows={data.focus?.reason_breakdown ?? []} title={`${data.focus?.model_code || "选中型号"} 原因构成`} /><FocusModelPanel focus={data.focus ?? {}} /></div>
          <AttributionTable rows={data.model_ranking ?? []} focusModel={data.focus?.model_code} onFocus={setFocusModel} />
          <div className="attribution-meta"><span>{data.date_basis}</span><button type="button" className="button secondary" disabled={loading} onClick={() => setRefreshKey((key) => key + 1)}><ArrowsClockwise className={loading ? "spin" : ""} size={16} />刷新</button></div>
        </>}
      </div>
    </main>
  );
}

function ManualTodoSummary({ summary }) {
  const items = [
    { label: "待发送", value: summary.waiting ?? 0, tone: "orange" },
    { label: "已发送给业务员", value: summary.sent ?? 0, tone: "green" },
    { label: "发送失败", value: summary.failed ?? 0, tone: "orange" },
    { label: "已取消", value: summary.cancelled ?? 0, tone: "blue" },
  ];
  return (
    <section className="summary-strip manual-summary" aria-label="人工待办发送摘要">
      {items.map((item) => <div className="summary-item" key={item.label}><span>{item.label}</span><strong className={`metric-${item.tone}`}>{item.value}</strong></div>)}
    </section>
  );
}

function ManualTodoFilters({ draft, setDraft, assignees, busy, onSubmit, onReset }) {
  const update = (key) => (event) => setDraft((current) => ({ ...current, [key]: event.target.value }));
  return (
    <form className="filters manual-filters" onSubmit={onSubmit}>
      <div className="filter-row filter-row-primary">
        <label><span>发送状态</span><select value={draft.task_status} onChange={update("task_status")}><option value="">全部</option><option value="PENDING">待发送</option><option value="RUNNING">发送中</option><option value="SUCCEEDED">已发送</option><option value="FAILED">发送失败</option><option value="CANCELLED">已取消</option></select></label>
        <label><span>对应业务员</span><select value={draft.assignee} onChange={update("assignee")}><option value="">全部</option>{assignees.map((name) => <option value={name} key={name}>{name}</option>)}</select></label>
        <label><span>触发模块</span><select value={draft.origin} onChange={update("origin")}><option value="">全部</option><option value="module1">模块1·在途拦截</option><option value="module2">模块2·退货验收</option><option value="module3">模块3·未发货退款</option></select></label>
        <div className="date-field"><span>待办生成时间</span><div className="date-range"><CalendarBlank size={16} /><input type="date" value={draft.started_on} onChange={update("started_on")} aria-label="待办开始日期" /><b>~</b><input type="date" value={draft.ended_on} onChange={update("ended_on")} aria-label="待办结束日期" /></div></div>
      </div>
      <div className="filter-row filter-row-secondary">
        <label className="search-field"><MagnifyingGlass size={17} /><input value={draft.keyword} onChange={update("keyword")} placeholder="订单号 / 售后单 / 店铺 / 业务员 / 事项" /></label>
        <button type="button" className="button secondary" onClick={onReset}><ArrowCounterClockwise size={16} />重置</button>
        <button type="submit" className="button primary" disabled={busy}><MagnifyingGlass size={16} />查询</button>
      </div>
    </form>
  );
}

function ManualTodoTable({ items, selected, onSelect, loading, error, onRetry }) {
  return (
    <div className="table-wrap manual-table-wrap">
      <table className="manual-table">
        <thead><tr><th>发送状态</th><th>对应业务员</th><th>什么原因发待办</th><th>触发模块</th><th>平台订单号</th><th>售后单号</th><th>店铺</th><th>最近更新</th><th>操作</th></tr></thead>
        <tbody>{items.map((item) => (
          <tr key={item.task_id} className={selected === item.task_id ? "selected" : ""} onClick={() => onSelect(item.task_id)}>
            <td><StatusTag tone={item.status_tone}>{item.status_label}</StatusTag></td>
            <td><div className="stacked-cell"><b>{item.assignee}</b><small>{item.sent_to_assignee ? "已确认发送" : "尚未确认发送"}</small></div></td>
            <td className="manual-reason-cell" title={item.reason}>{item.reason}</td>
            <td>{item.origin_label}</td>
            <td className="mono">{item.platform_order_sn}</td>
            <td className="mono">{item.after_sales_sn}</td>
            <td title={item.shop_name}><span className="truncate">{item.shop_name}</span></td>
            <td>{formatDateTime(item.updated_at, true)}</td>
            <td><button className="link-button" type="button" onClick={(event) => { event.stopPropagation(); onSelect(item.task_id); }}>查看详情</button></td>
          </tr>
        ))}</tbody>
      </table>
      {!items.length && (loading ? <div className="table-state"><ArrowsClockwise className="spin" size={26} />正在读取人工待办…</div> : error ? <div className="table-state error-state"><WarningCircle size={28} /><strong>人工待办暂时无法读取</strong><span>{error}</span><button type="button" className="button secondary" onClick={onRetry}>重新加载</button></div> : <div className="table-state"><User size={28} />当前条件下没有人工待办</div>)}
      {loading && items.length > 0 && <div className="table-loading"><ArrowsClockwise className="spin" size={18} />刷新中</div>}
    </div>
  );
}

function ManualTodoDetail({ item }) {
  if (!item) return <aside className="detail-panel"><div className="detail-heading"><h2>人工待办详情</h2></div><div className="detail-state"><User size={28} />选择一笔待办查看发送与原因</div></aside>;
  return (
    <aside className="detail-panel manual-detail-panel">
      <div className="detail-heading"><h2>人工待办详情</h2><StatusTag tone={item.status_tone}>{item.status_label}</StatusTag></div>
      <div className="detail-scroll">
        <section className={`manual-delivery-card ${item.sent_to_assignee ? "delivery-sent" : "delivery-unsent"}`}>
          {item.sent_to_assignee ? <CheckCircle size={22} /> : <WarningCircle size={22} />}
          <div><strong>{item.sent_to_assignee ? `已发送给 ${item.assignee}` : `尚未发送给 ${item.assignee}`}</strong><span>{item.sent_to_assignee ? `发送时间 ${formatDateTime(item.sent_at, true)}` : `当前状态：${item.status_label}`}</span></div>
        </section>
        <section className="detail-section"><h3>触发原因</h3><p className="manual-reason-detail">{item.reason}</p><dl><DetailRow label="原因代码" value={item.reason_code} /><DetailRow label="触发模块" value={item.origin_label} /><DetailRow label="对应业务员" value={item.assignee} /></dl></section>
        <section className="detail-section"><h3>发送给业务员的具体事项</h3><p className="manual-content-detail">{item.content}</p></section>
        <section className="detail-section"><h3>关联订单</h3><dl><DetailRow label="平台订单号" value={item.platform_order_sn} /><DetailRow label="售后单号" value={item.after_sales_sn} /><DetailRow label="店铺" value={item.shop_name} /><DetailRow label="任务编号" value={item.task_id} /></dl></section>
        <section className="detail-section"><h3>发送审计</h3><dl><DetailRow label="是否已发送" value={item.sent_to_assignee ? "是" : "否"} /><DetailRow label="ERP待办 ID" value={item.external_todo_id || "—"} /><DetailRow label="已尝试次数" value={item.attempts} /><DetailRow label="待办发起时间" value={formatDateTime(item.started_at, true)} /><DetailRow label="本地创建时间" value={formatDateTime(item.created_at, true)} /><DetailRow label="最近更新" value={formatDateTime(item.updated_at, true)} /></dl>{item.last_error && <div className={item.task_status === "CANCELLED" ? "manual-cancel-box" : "manual-error-box"}><strong>{item.task_status === "CANCELLED" ? "取消/未发送原因" : "发送失败原因"}</strong><span>{item.last_error}</span></div>}{item.cancel_reason && <div className="manual-cancel-box"><strong>取消原因</strong><span>{item.cancel_reason}</span></div>}</section>
      </div>
    </aside>
  );
}

function ManualTodoWorkspace() {
  const [draft, setDraft] = useState(createManualFilters);
  const [filters, setFilters] = useState(createManualFilters);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(15);
  const [selectedId, setSelectedId] = useState(null);
  const [data, setData] = useState({ summary: {}, assignees: [], items: [], pagination: { page: 1, page_size: 15, total: 0, pages: 1 }, last_updated_at: null });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);

  const load = useCallback(async (signal) => {
    setLoading(true); setError("");
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    try {
      const response = await fetch(`/api/v1/aftersales/manual-todos?${params}`, { signal });
      if (!response.ok) throw new Error(`服务返回 ${response.status}`);
      const payload = await response.json();
      setData(payload);
      setSelectedId((current) => payload.items.some((item) => item.task_id === current) ? current : (payload.items[0]?.task_id ?? null));
    } catch (requestError) {
      if (requestError.name !== "AbortError") setError("请确认 FastAPI 和本地数据库正常运行。");
    } finally { if (!signal.aborted) setLoading(false); }
  }, [filters, page, pageSize, refreshKey]);

  useEffect(() => { const controller = new AbortController(); load(controller.signal); return () => controller.abort(); }, [load]);
  const selected = data.items.find((item) => item.task_id === selectedId) ?? null;
  const submit = (event) => { event.preventDefault(); setPage(1); setFilters(draft); };
  const reset = () => { const initial = createManualFilters(); setDraft(initial); setFilters(initial); setPage(1); };
  return (
    <>
      <main className="workspace manual-workspace"><header className="topbar"><div className="page-title"><User size={22} /><h1>人工待办</h1><span className="read-only-badge">发送审计</span></div><div className="sync-status"><span />最近更新 {formatDateTime(data.last_updated_at, true)}</div></header><div className="workspace-body"><ManualTodoSummary summary={data.summary} /><ManualTodoFilters draft={draft} setDraft={setDraft} assignees={data.assignees ?? []} busy={loading} onSubmit={submit} onReset={reset} /><ManualTodoTable items={data.items ?? []} selected={selectedId} onSelect={setSelectedId} loading={loading} error={error} onRetry={() => setRefreshKey((key) => key + 1)} /><div className="workspace-actions"><span className="table-total">共 <strong>{data.pagination.total}</strong> 条人工待办</span><button type="button" className="button secondary" disabled={loading} onClick={() => setRefreshKey((key) => key + 1)}><ArrowsClockwise className={loading ? "spin" : ""} size={16} />刷新</button><Pagination pagination={data.pagination} onPage={(nextPage) => { if (nextPage >= 1 && nextPage <= data.pagination.pages) setPage(nextPage); }} onPageSize={(size) => { setPageSize(size); setPage(1); }} /></div></div></main>
      <ManualTodoDetail item={selected} />
    </>
  );
}

const MONITOR_STAGE_LABELS = {
  sync: "拼多多同步",
  tmall_sync: "天猫同步与物流补全",
  intercept_tasks: "生成拦截任务",
  notification_preflight: "发送前物流复核",
  notification: "企业微信发送",
  logistics_gate: "退款物流闸门",
  module1_erp_refunds: "模块1 ERP退款闭环",
  pdd_refund: "平台退款执行",
  module2_erp_intake: "ERP退货单核对",
  module2_refund_tasks: "验货通过退款入队",
  module2_exception_todos: "验货异常人工待办",
  module2_pdd_refunds: "退货退款执行",
  module3_tasks: "未发货退款识别",
  module3_erp_refunds: "模块3 ERP退款处理",
  module3_exception_todos: "异常人工待办",
};

const MONITOR_DETAIL_LABELS = {
  scanned: "扫描",
  tasks_created: "新建任务",
  tasks_existing: "已有任务",
  notices_ready: "可发送",
  notices_cancelled: "已取消",
  logistics_query_failed: "物流查询失败",
  succeeded: "成功",
  failed: "失败",
  blocked: "阻断",
  applied: "已执行",
  receipts_created: "登记收货",
  inspections_passed: "验货通过",
  inspections_failed: "验货异常",
  post_refund_waiting_tracking: "退款后待退货运单",
  post_refund_waiting_receipt: "退款后待仓库收货",
  post_refund_verified: "退款后验收一致",
  tmall_refunds_held: "天猫待人工退款",
  unavailable: "核对失败",
  ambiguous: "运单冲突",
  skipped_missing_owner: "缺少负责人",
  records_created: "新增记录",
  logistics_unavailable: "天猫物流接口失败",
  logistics_ambiguous_or_missing: "天猫发货运单不唯一",
  shops_ok: "店铺正常",
  shops_failed: "店铺失败",
};

const monitorTone = (status) => ({ healthy: "success", completed: "success", warning: "warning", starting: "info", skipped: "neutral", disabled: "neutral", stopped: "danger", failed: "danger", missing: "neutral" }[status] ?? "neutral");
const monitorStageStatus = (status) => ({ completed: "正常", skipped: "跳过", failed: "失败", missing: "暂无周期" }[status] ?? status);
const formatAge = (seconds) => {
  if (seconds === null || seconds === undefined) return "暂无记录";
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
};

function monitorStageSummary(stage) {
  if (stage.error) return stage.error;
  if (stage.reason) return stage.reason;
  const entries = Object.entries(stage)
    .filter(([key, value]) => !["id", "status", "error", "reason"].includes(key) && value !== null && value !== undefined && MONITOR_DETAIL_LABELS[key]);
  const details = entries
    .filter(([, value]) => value !== 0 && value !== false)
    .slice(0, 6)
    .map(([key, value]) => `${MONITOR_DETAIL_LABELS[key]} ${value}`);
  return details.join(" · ") || "本周期已完成";
}

function MonitorWorkspace() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [retryingTaskId, setRetryingTaskId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/v1/monitor/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`服务返回 ${response.status}`);
      setData(await response.json());
      setError("");
    } catch (requestError) {
      setError(`监控状态读取失败：${requestError.message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 15_000);
    return () => window.clearInterval(timer);
  }, [load, refreshKey]);

  const retryDesktopNotification = async (taskId) => {
    const confirmed = window.confirm(
      `确认任务 ${taskId} 尚未在企业微信中输入消息，并重新进入发送队列？后台将在下一个周期尝试发送。`,
    );
    if (!confirmed) return;
    setRetryingTaskId(taskId);
    setActionError("");
    setActionMessage("");
    try {
      const response = await fetch(`/api/v1/monitor/desktop-notifications/${taskId}/retry`, { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? `服务返回 ${response.status}`);
      setActionMessage(payload.message ?? `任务 ${taskId} 已重新进入发送队列`);
      setRefreshKey((key) => key + 1);
    } catch (requestError) {
      setActionError(`重试失败：${requestError.message}`);
    } finally {
      setRetryingTaskId(null);
    }
  };

  const queue = data?.notification_queue ?? {};
  const recovery = data?.desktop_notification_recovery ?? {};
  const config = data?.configuration ?? {};
  const worker = data?.worker ?? {};
  return (
    <main className="workspace monitor-workspace">
      <header className="topbar">
        <div className="page-title"><ChartBar size={22} /><h1>运行监控</h1><span className="read-only-badge">安全重试</span></div>
        <div className={`sync-status monitor-sync monitor-${data?.state ?? "starting"}`}><span />{data?.state_label ?? "正在读取运行状态"}</div>
      </header>
      <div className="monitor-body">
        {error && <div className="monitor-alert monitor-alert-danger"><WarningCircle size={18} />{error}</div>}
        {actionError && <div className="monitor-alert monitor-alert-danger"><WarningCircle size={18} />{actionError}</div>}
        {actionMessage && <div className="monitor-alert monitor-alert-success"><CheckCircle size={18} />{actionMessage}</div>}
        {recovery.blocking_task_id && (
          <section className={`monitor-recovery ${recovery.can_retry ? "monitor-recovery-safe" : "monitor-recovery-verify"}`}>
            <WarningCircle size={22} weight="fill" />
            <div>
              <strong>企业微信发送已暂停 · 任务 {recovery.blocking_task_id}</strong>
              <span>{recovery.message}{recovery.error ? `；失败原因：${recovery.error}` : ""}</span>
            </div>
            {recovery.can_retry ? (
              <button type="button" className="button primary" disabled={retryingTaskId === recovery.blocking_task_id} onClick={() => retryDesktopNotification(recovery.blocking_task_id)}>
                <ArrowsClockwise className={retryingTaskId === recovery.blocking_task_id ? "spin" : ""} size={16} />
                {retryingTaskId === recovery.blocking_task_id ? "重新入队中" : "重新尝试发送"}
              </button>
            ) : <small>为防止重复发送，工作台不会提供直接重试。</small>}
          </section>
        )}
        <section className={`monitor-overview monitor-overview-${data?.state ?? "starting"}`}>
          <div>
            {data?.state === "healthy" ? <CheckCircle size={26} weight="fill" /> : <WarningCircle size={26} weight="fill" />}
            <div><strong>{data?.state_label ?? "正在读取运行状态"}</strong><span>每 15 秒自动刷新；只有明确停在发送前的任务允许人工重新入队。</span></div>
          </div>
          <button type="button" className="button secondary" disabled={loading} onClick={() => setRefreshKey((key) => key + 1)}><ArrowsClockwise className={loading ? "spin" : ""} size={16} />立即刷新</button>
        </section>
        <section className="monitor-metrics">
          <article><span>后台运行器</span><strong className={worker.running ? "monitor-good" : "monitor-bad"}>{worker.running ? "运行中" : "未运行"}</strong><small>{worker.pid ? `PID ${worker.pid}` : "未发现有效进程"}</small></article>
          <article><span>最近完整周期</span><strong>{formatAge(worker.last_cycle_age_seconds)}</strong><small>{worker.last_cycle_ok === false ? "周期存在失败" : `间隔 ${worker.interval_seconds ?? "—"} 秒`}</small></article>
          <article><span>企微待发送</span><strong className={queue.pending ? "monitor-warn" : "monitor-good"}>{queue.pending ?? "—"}</strong><small>发送中 {queue.running ?? 0} · 已成功 {queue.succeeded ?? 0}</small></article>
          <article><span>发送失败</span><strong className={queue.failed ? "monitor-bad" : "monitor-good"}>{queue.failed ?? "—"}</strong><small>当前启用范围共 {queue.total ?? 0} 条任务</small></article>
        </section>
        <section className="monitor-modules">
          {(data?.modules ?? []).map((module) => (
            <article className="monitor-module-card" key={module.id}>
              <header><div><strong>{{ module1: "模块 1 · 已发货仅退款拦截", module2: "模块 2 · 退货验收退款", module3: "模块 3 · 未发货退款处理" }[module.id] ?? module.id}</strong><span>{{ module1: "识别、企微拦截、物流闸门与退款闭环", module2: "ERP 实收核对一致后退款，明细不一致转人工", module3: "ERP 履约核验、退款补单与异常待办" }[module.id] ?? "自动化运行阶段"}</span></div><StatusTag tone={monitorTone(module.status)}>{module.status_label}</StatusTag></header>
              <div className="monitor-stage-list">
                {module.stages.map((stage) => (
                  <div className="monitor-stage" key={stage.id}>
                    <span className={`monitor-stage-dot stage-${stage.status}`} />
                    <div><strong>{MONITOR_STAGE_LABELS[stage.id] ?? stage.id}</strong><small title={stage.error ?? ""}>{monitorStageSummary(stage)}</small></div>
                    <StatusTag tone={monitorTone(stage.status)}>{monitorStageStatus(stage.status)}</StatusTag>
                  </div>
                ))}
              </div>
            </article>
          ))}
        </section>
        <section className="monitor-config">
          <div><strong>自动化开关</strong><span>企业微信发送：{(config.notification_transport === "desktop" && config.desktop_send_enabled) || (config.notification_transport === "qywx_webhook" && config.qywx_write_enabled) ? "已开启" : "未开启"}</span><span>模块1平台退款：{config.module1_refund_enabled ? "已开启" : "未开启"}</span><span>模块2验货退款：{config.module2_worker_enabled && config.module2_refund_enabled ? "已开启" : "未开启"}</span><span>模块3 ERP退款：{config.module3_erp_refund_enabled && config.erp_write_enabled ? "已开启" : "未开启"}</span></div>
          <small>状态检查时间：{formatDateTime(data?.checked_at, true)}</small>
        </section>
      </div>
    </main>
  );
}

function DetailRow({ label, value, copyable = false, onCopy }) {
  return (
    <div className="detail-row">
      <dt>{label}</dt>
      <dd title={String(value ?? "—")}>
        <span>{value ?? "—"}</span>
        {copyable && value && value !== "—" && <IconButton label={`复制${label}`} onClick={() => onCopy(value)}><Copy size={14} /></IconButton>}
      </dd>
    </div>
  );
}

function DetailPanel({ detail, loading, onClose, onCopy, copied }) {
  return (
    <aside className="detail-panel">
      <div className="detail-heading">
        <h2>售后详情</h2>
        <IconButton label="关闭详情" onClick={onClose}><X size={20} /></IconButton>
      </div>
      {loading && !detail ? (
        <div className="detail-state"><ArrowsClockwise className="spin" size={24} />正在加载详情…</div>
      ) : detail ? (
        <div className="detail-scroll">
          <section className="detail-section">
            <h3>基础信息</h3>
            <dl>
              <DetailRow label="店铺" value={detail.shop_name} />
              <DetailRow label="平台" value={detail.platform_label} />
              <DetailRow label="售后单号" value={detail.after_sales_sn} copyable onCopy={onCopy} />
              <DetailRow label="订单号" value={detail.platform_order_sn} copyable onCopy={onCopy} />
              <DetailRow label="归属业务员" value={detail.erp_customer.sales_owner} />
              <DetailRow label="ERP客户" value={detail.erp_customer.customer_name} />
              <DetailRow label="快递单号" value={detail.tracking_number} copyable onCopy={onCopy} />
              <DetailRow label="申请时间" value={formatDateTime(detail.created_at, true)} />
              <DetailRow label="售后类型" value={detail.after_sales_type} />
              <DetailRow label="退款金额" value={formatCurrency(detail.refund_amount)} />
              <DetailRow label="买家实付" value={formatCurrency(detail.platform_order_amount)} />
              <DetailRow label="平台优惠" value={formatCurrency(detail.platform_discount_amount)} />
              <DetailRow label="商家优惠" value={formatCurrency(detail.seller_discount_amount)} />
              <DetailRow label="商家应收" value={formatCurrency(detail.merchant_receivable_amount)} />
              <DetailRow label="退款范围" value={detail.refund_scope} />
              <DetailRow label="商品名称" value={detail.product_name} />
              <DetailRow label="原因分类" value={detail.buyer_reason_category} />
              <DetailRow label="买家昵称" value={detail.buyer_name} />
            </dl>
          </section>
          <section className="detail-section decision-section">
            <h3>当前处理决策</h3>
            <dl>
              <DetailRow label="拦截策略" value={detail.decision.strategy} />
              <div className="detail-row"><dt>当前状态</dt><dd><StatusTag tone={detail.decision.status_tone}>{detail.decision.status}</StatusTag></dd></div>
              <DetailRow label="当前处理人" value={detail.decision.handler} />
              <DetailRow label="处理时间" value={formatDateTime(detail.decision.handled_at, true)} />
              <DetailRow label="备注" value={detail.decision.note} />
            </dl>
          </section>
          {detail.closed_loop && <section className="detail-section decision-section">
            <h3>拦截退回闭环</h3>
            <dl>
              <div className="detail-row"><dt>ERP状态</dt><dd><StatusTag tone={detail.closed_loop.tone}>{detail.closed_loop.label}</StatusTag></dd></div>
              <DetailRow label="ERP退货单" value={detail.closed_loop.return_order_sn} copyable onCopy={onCopy} />
              <DetailRow label="累计应收" value={detail.closed_loop.receivable_amount === null || detail.closed_loop.receivable_amount === undefined ? "—" : formatCurrency(Number(detail.closed_loop.receivable_amount))} />
              <DetailRow label="最近核对" value={formatDateTime(detail.closed_loop.checked_at, true)} />
              <DetailRow label="闭环时间" value={formatDateTime(detail.closed_loop.closed_loop_at, true)} />
              <DetailRow label="核对说明" value={detail.closed_loop.message} />
            </dl>
          </section>}
          <section className="detail-section timeline-section">
            <h3>处理流程</h3>
            <ol className="timeline">
              {detail.timeline.map((event, index) => (
                <li key={`${event.title}-${event.occurred_at}-${index}`} className={`timeline-${event.tone}`}>
                  <span className="timeline-marker">{event.tone === "success" && <CheckCircle size={16} weight="fill" />}</span>
                  <div className="timeline-title"><strong>{event.title}</strong><time>{formatDateTime(event.occurred_at, true)}</time></div>
                  <p>{event.description}</p>
                </li>
              ))}
            </ol>
            <button type="button" className="button secondary logistics-button"><Truck size={16} />查看物流轨迹</button>
          </section>
        </div>
      ) : (
        <div className="detail-state"><Package size={28} />请选择一条售后订单</div>
      )}
      {copied && <div className="copy-toast"><CheckCircle size={17} weight="fill" />已复制</div>}
    </aside>
  );
}

const inspectionLabels = { PENDING: "待验货", PASS: "验货通过", FAIL: "验货异常" };
const inspectionTones = { PENDING: "warning", PASS: "success", FAIL: "danger" };

function newReceiptSn() {
  const now = new Date();
  const day = inputDate(now).replaceAll("-", "");
  const time = [now.getHours(), now.getMinutes(), now.getSeconds()].map((value) => String(value).padStart(2, "0")).join("");
  return `WR-${day}-${time}`;
}

async function responseError(response) {
  try {
    const payload = await response.json();
    return typeof payload.detail === "string" ? payload.detail : `服务返回 ${response.status}`;
  } catch {
    return `服务返回 ${response.status}`;
  }
}

function WarehouseWorkspace() {
  const [rows, setRows] = useState([]);
  const [selectedReceipt, setSelectedReceipt] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [keyword, setKeyword] = useState("");
  const [scanNumber, setScanNumber] = useState("");
  const [scanResult, setScanResult] = useState(null);
  const [scanBusy, setScanBusy] = useState(false);
  const [feedback, setFeedback] = useState({ tone: "", text: "" });
  const [operator, setOperator] = useState(() => window.localStorage.getItem("warehouse.operator") || "");
  const [receiptForm, setReceiptForm] = useState(null);
  const [inspectionItems, setInspectionItems] = useState([]);
  const [inspectionNote, setInspectionNote] = useState("");
  const [actionBusy, setActionBusy] = useState(false);

  const selected = rows.find((row) => row.receipt_sn === selectedReceipt) ?? null;

  const loadRows = useCallback(async (signal) => {
    setLoading(true);
    const params = new URLSearchParams({ limit: "200" });
    if (statusFilter) params.set("inspection_status", statusFilter);
    if (keyword.trim()) params.set("keyword", keyword.trim());
    try {
      const response = await fetch(`/api/v1/warehouse/returns?${params}`, { signal });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      setRows(payload);
      setSelectedReceipt((current) => payload.some((row) => row.receipt_sn === current) ? current : (payload[0]?.receipt_sn ?? ""));
    } catch (error) {
      if (error.name !== "AbortError") setFeedback({ tone: "danger", text: error.message });
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, [statusFilter, keyword, refreshKey]);

  useEffect(() => {
    const controller = new AbortController();
    loadRows(controller.signal);
    return () => controller.abort();
  }, [loadRows]);

  useEffect(() => {
    setInspectionItems(selected?.items.map((item) => ({ ...item })) ?? []);
    setInspectionNote(selected?.inspection_note ?? "");
  }, [selectedReceipt, selected?.inspection_status]);

  const persistOperator = (value) => {
    setOperator(value);
    window.localStorage.setItem("warehouse.operator", value);
  };

  const chooseCandidate = (candidate) => {
    setReceiptForm({
      receipt_sn: newReceiptSn(),
      return_tracking_number: scanResult.return_tracking_number,
      after_sales_sn: candidate?.after_sales_sn ?? "",
      destination: "STAGING",
      items: candidate?.expected_items.map((item) => ({
        product_code: item.product_code,
        color: item.color ?? "",
        quantity: item.applied_quantity,
        item_status: "NORMAL",
        remark: "",
      })) ?? [{ product_code: "", color: "", quantity: 1, item_status: "NORMAL", remark: "" }],
    });
  };

  const scan = async (event) => {
    event.preventDefault();
    if (!scanNumber.trim()) return;
    setScanBusy(true);
    setFeedback({ tone: "", text: "" });
    try {
      const response = await fetch("/api/v1/warehouse/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ return_tracking_number: scanNumber.trim() }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      setScanResult(payload);
      if (payload.recorded_receipt_sn) {
        setSelectedReceipt(payload.recorded_receipt_sn);
        setReceiptForm(null);
        setFeedback({ tone: "warning", text: `该运单已登记：${payload.recorded_receipt_sn}` });
      } else {
        setReceiptForm({
          receipt_sn: newReceiptSn(),
          return_tracking_number: payload.return_tracking_number,
          after_sales_sn: payload.candidates.length === 1 ? payload.candidates[0].after_sales_sn : "",
          destination: "STAGING",
          items: payload.candidates.length === 1
            ? payload.candidates[0].expected_items.map((item) => ({ product_code: item.product_code, color: item.color ?? "", quantity: item.applied_quantity, item_status: "NORMAL", remark: "" }))
            : [{ product_code: "", color: "", quantity: 1, item_status: "NORMAL", remark: "" }],
        });
      }
    } catch (error) {
      setFeedback({ tone: "danger", text: error.message });
    } finally {
      setScanBusy(false);
    }
  };

  const updateReceiptItem = (index, key, value) => {
    setReceiptForm((current) => ({ ...current, items: current.items.map((item, itemIndex) => itemIndex === index ? { ...item, [key]: value } : item) }));
  };

  const submitReceipt = async (event) => {
    event.preventDefault();
    if (!operator.trim()) { setFeedback({ tone: "danger", text: "请先填写仓库操作员" }); return; }
    setActionBusy(true);
    try {
      const response = await fetch("/api/v1/warehouse/returns", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...receiptForm, operator: operator.trim(), after_sales_sn: receiptForm.after_sales_sn || null }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      setFeedback({ tone: "success", text: `收货登记成功：${payload.receipt_sn}` });
      setSelectedReceipt(payload.receipt_sn);
      setReceiptForm(null);
      setScanResult(null);
      setScanNumber("");
      setRefreshKey((key) => key + 1);
    } catch (error) {
      setFeedback({ tone: "danger", text: error.message });
    } finally {
      setActionBusy(false);
    }
  };

  const inspect = async (result) => {
    if (!selected || !operator.trim()) { setFeedback({ tone: "danger", text: "请填写验货员" }); return; }
    setActionBusy(true);
    try {
      const response = await fetch(`/api/v1/warehouse/returns/${encodeURIComponent(selected.receipt_sn)}/inspection`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ result, inspected_by: operator.trim(), note: inspectionNote || null, items: inspectionItems }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setFeedback({ tone: "success", text: result === "PASS" ? "验货已通过" : "验货异常已登记" });
      setRefreshKey((key) => key + 1);
    } catch (error) {
      setFeedback({ tone: "danger", text: error.message });
    } finally {
      setActionBusy(false);
    }
  };

  return (
    <>
      <main className="workspace warehouse-workspace">
        <header className="topbar">
          <div className="page-title"><Package size={22} /><h1>仓库验货</h1><span className="read-only-badge">模块 2</span></div>
          <div className="sync-status"><span />收货与验货本地留痕 · 不自动退款</div>
        </header>
        <div className="workspace-body">
          <form className="warehouse-toolbar" onSubmit={(event) => { event.preventDefault(); setRefreshKey((key) => key + 1); }}>
            <label><span>验货状态</span><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="">全部</option><option value="PENDING">待验货</option><option value="PASS">验货通过</option><option value="FAIL">验货异常</option></select></label>
            <label className="warehouse-keyword"><span>检索</span><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="收货单 / 退货运单 / 售后单 / 客户" /></label>
            <button className="button secondary" type="submit"><MagnifyingGlass size={16} />查询</button>
            <button className="button secondary" type="button" onClick={() => setRefreshKey((key) => key + 1)}><ArrowsClockwise size={16} />刷新</button>
          </form>
          <div className="table-wrap warehouse-table-wrap">
            <table className="warehouse-table">
              <thead><tr><th>收货单</th><th>退货运单</th><th>售后 / 平台订单</th><th>客户归档</th><th>实收明细</th><th>验货状态</th><th>收货时间</th></tr></thead>
              <tbody>{rows.map((row) => (
                <tr key={row.receipt_sn} className={selectedReceipt === row.receipt_sn ? "selected" : ""} onClick={() => setSelectedReceipt(row.receipt_sn)}>
                  <td className="mono">{row.receipt_sn}</td><td className="mono">{row.return_tracking_number}</td>
                  <td><div className="stacked-cell"><b>{row.after_sales_sn || "未匹配售后"}</b><small>{row.platform_order_sn || "—"}</small></div></td>
                  <td>{row.customer_name || (row.destination === "STAGING" ? "退货暂存" : row.customer_reference) || "—"}</td>
                  <td>{row.items.map((item) => `${item.product_code}${item.color ? `/${item.color}` : ""} ×${item.quantity}`).join("；")}</td>
                  <td><StatusTag tone={inspectionTones[row.inspection_status]}>{inspectionLabels[row.inspection_status]}</StatusTag></td>
                  <td>{formatDateTime(row.created_at, true)}</td>
                </tr>
              ))}</tbody>
            </table>
            {loading && <div className="table-loading"><ArrowsClockwise className="spin" size={14} />读取中</div>}
            {!loading && !rows.length && <div className="table-state"><Package size={28} />暂无仓库退货记录</div>}
          </div>
          <div className="workspace-actions"><span className="table-total">共 <strong>{rows.length}</strong> 笔收货记录</span></div>
        </div>
      </main>
      <aside className="detail-panel warehouse-panel">
        <div className="detail-heading"><h2>扫码收货 / 验货</h2></div>
        <div className="warehouse-panel-scroll">
          {feedback.text && <div className={`warehouse-feedback feedback-${feedback.tone}`}>{feedback.text}</div>}
          <section className="warehouse-section">
            <h3><Barcode size={18} />扫描退货运单</h3>
            <form className="warehouse-scan" onSubmit={scan}><input autoFocus value={scanNumber} onChange={(event) => setScanNumber(event.target.value)} placeholder="扫描或输入买家退货运单号" /><button className="button primary" disabled={scanBusy}>{scanBusy ? "查询中" : "反查"}</button></form>
            {scanResult && !scanResult.recorded_receipt_sn && <div className="candidate-list">
              {scanResult.candidates.length ? scanResult.candidates.map((candidate) => <button type="button" key={candidate.after_sales_sn} className={receiptForm?.after_sales_sn === candidate.after_sales_sn ? "selected" : ""} onClick={() => chooseCandidate(candidate)}><strong>{candidate.shop_name}</strong><span>{candidate.after_sales_sn}</span><small>{candidate.expected_items.map((item) => `${item.product_code}/${item.color || "无颜色"} ×${item.applied_quantity}`).join("；")}</small></button>) : <p>未匹配到平台退货退款单，可先按未知包裹暂存。</p>}
            </div>}
          </section>
          {receiptForm && <section className="warehouse-section"><h3>拆包实收登记</h3><form className="warehouse-form" onSubmit={submitReceipt}>
            <label><span>收货单号</span><input value={receiptForm.receipt_sn} onChange={(event) => setReceiptForm({ ...receiptForm, receipt_sn: event.target.value })} /></label>
            <label><span>仓库操作员</span><input value={operator} onChange={(event) => persistOperator(event.target.value)} placeholder="必填" /></label>
            <div className="receipt-items">{receiptForm.items.map((item, index) => <div className="receipt-item" key={`${index}-${item.product_code}`}><input value={item.product_code} onChange={(event) => updateReceiptItem(index, "product_code", event.target.value)} placeholder="型号" /><input value={item.color} onChange={(event) => updateReceiptItem(index, "color", event.target.value)} placeholder="颜色" /><input type="number" min="1" value={item.quantity} onChange={(event) => updateReceiptItem(index, "quantity", Number(event.target.value))} /><button type="button" className="icon-button" onClick={() => setReceiptForm({ ...receiptForm, items: receiptForm.items.filter((_, itemIndex) => itemIndex !== index) })}><Trash size={15} /></button></div>)}</div>
            <button type="button" className="link-button add-item" onClick={() => setReceiptForm({ ...receiptForm, items: [...receiptForm.items, { product_code: "", color: "", quantity: 1, item_status: "NORMAL", remark: "" }] })}><Plus size={14} />增加实收明细</button>
            <button className="button primary warehouse-submit" disabled={actionBusy}>确认收货并进入待验货</button>
          </form></section>}
          {selected && <section className="warehouse-section inspection-section"><h3>验货结论</h3>
            <dl><div><dt>收货单</dt><dd>{selected.receipt_sn}</dd></div><div><dt>关联售后</dt><dd>{selected.after_sales_sn || "未匹配"}</dd></div></dl>
            <label className="operator-field"><span>验货员</span><input value={operator} onChange={(event) => persistOperator(event.target.value)} placeholder="必填" /></label>
            <div className="inspection-items">{inspectionItems.map((item, index) => <div className="inspection-item" key={`${item.product_code}-${item.color}`}><div><strong>{item.product_code}</strong><span>{item.color || "无颜色"} · ×{item.quantity}</span></div><select value={item.item_status} disabled={selected.inspection_status !== "PENDING"} onChange={(event) => setInspectionItems((current) => current.map((entry, itemIndex) => itemIndex === index ? { ...entry, item_status: event.target.value } : entry))}><option value="NORMAL">正常</option><option value="DEFECTIVE">次品</option><option value="SCRAPPED">报废</option></select></div>)}</div>
            <label className="inspection-note"><span>异常说明</span><textarea value={inspectionNote} disabled={selected.inspection_status !== "PENDING"} onChange={(event) => setInspectionNote(event.target.value)} placeholder="验货异常时必填；通过可留空" /></label>
            {selected.inspection_status === "PENDING" ? <div className="inspection-actions"><button className="button secondary danger-button" type="button" disabled={actionBusy} onClick={() => inspect("FAIL")}>登记异常</button><button className="button primary" type="button" disabled={actionBusy} onClick={() => inspect("PASS")}>验货通过</button></div> : <div className={`inspection-final final-${selected.inspection_status.toLowerCase()}`}><CheckCircle size={18} />{inspectionLabels[selected.inspection_status]} · {selected.inspected_by || "—"}</div>}
          </section>}
        </div>
      </aside>
    </>
  );
}

export function App() {
  const [activeView, setActiveView] = useState("orders");
  const [interceptDetailOpen, setInterceptDetailOpen] = useState(true);
  const [draftFilters, setDraftFilters] = useState(createInitialFilters);
  const [filters, setFilters] = useState(createInitialFilters);
  const [recordView, setRecordView] = useState("ALL");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(15);
  const [data, setData] = useState({ summary: {}, view_counts: {}, shops: [], sales_owners: [], items: [], pagination: { page: 1, page_size: 15, total: 0, pages: 1 }, last_synced_at: null });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailOpen, setDetailOpen] = useState(true);
  const [copied, setCopied] = useState(false);

  const loadOrders = useCallback(async (signal) => {
    setLoading(true);
    setError("");
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), record_view: recordView });
    Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    try {
      const response = await fetch(`/api/v1/aftersales/orders?${params}`, { signal });
      if (!response.ok) throw new Error(`服务返回 ${response.status}`);
      const payload = await response.json();
      setData(payload);
      setSelected((current) => (
        payload.items.some((item) => item.after_sales_sn === current)
          ? current
          : (payload.items[0]?.after_sales_sn ?? "")
      ));
    } catch (requestError) {
      if (requestError.name !== "AbortError") setError("请确认本地数据库和 FastAPI 服务正在运行。");
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, [filters, page, pageSize, recordView, refreshKey]);

  useEffect(() => {
    const controller = new AbortController();
    loadOrders(controller.signal);
    return () => controller.abort();
  }, [loadOrders]);

  useEffect(() => {
    if (!selected) { setDetail(null); return undefined; }
    const controller = new AbortController();
    setDetailLoading(true);
    fetch(`/api/v1/aftersales/orders/${encodeURIComponent(selected)}`, { signal: controller.signal })
      .then((response) => { if (!response.ok) throw new Error(String(response.status)); return response.json(); })
      .then(setDetail)
      .catch((requestError) => { if (requestError.name !== "AbortError") setDetail(null); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selected]);

  const chooseOrder = (afterSalesSn) => {
    setSelected(afterSalesSn);
    setDetailOpen(true);
  };

  const submitFilters = (event) => {
    event.preventDefault();
    setPage(1);
    setFilters(draftFilters);
  };

  const resetFilters = () => {
    const initial = createInitialFilters();
    setDraftFilters(initial);
    setFilters(initial);
    setPage(1);
  };

  const changeRecordView = (nextView) => {
    if (nextView === recordView) return;
    setRecordView(nextView);
    setPage(1);
    setSelected("");
  };

  const changePage = (nextPage) => {
    if (nextPage >= 1 && nextPage <= data.pagination.pages && nextPage !== page) setPage(nextPage);
  };

  const changePageSize = (nextSize) => {
    setPageSize(nextSize);
    setPage(1);
  };

  const exportRows = () => {
    const rows = [["店铺", "售后单号", "平台订单号", "归属业务员", "ERP客户", "类型", "退款金额", "买家实付", "平台优惠", "商家优惠", "商家应收", "退款范围", "发货运单", "物流状态", "拦截状态", "平台退款", "最近更新"]];
    data.items.forEach((item) => rows.push([
      item.shop_name, item.after_sales_sn, item.platform_order_sn, item.sales_owner, item.erp_customer_name,
      item.after_sales_type_label, item.refund_amount, item.platform_order_amount,
      item.platform_discount_amount, item.seller_discount_amount, item.merchant_receivable_amount,
      item.refund_scope,
      item.tracking_number, item.logistics_label, item.intercept_label, item.platform_refund_label,
      formatDateTime(item.updated_at, true),
    ]));
    const csv = rows.map((row) => row.map((cell) => `"${String(cell).replaceAll('"', '""')}"`).join(",")).join("\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }));
    link.download = `售后订单记录-${inputDate(new Date())}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  const copyValue = async (value) => {
    await navigator.clipboard.writeText(String(value));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const detailVisible = (
    (activeView === "orders" && detailOpen)
    || (activeView === "intercepts" && interceptDetailOpen)
    || activeView === "warehouse"
    || activeView === "manual"
    || activeView === "scrap"
  );

  return (
    <div className={`app-shell ${detailVisible ? "" : "without-detail"} ${activeView === "scrap" ? "scrap-layout" : ""}`}>
      <Sidebar activeView={activeView} onNavigate={setActiveView} />
      {activeView === "orders" ? (
        <>
          <main className="workspace">
            <header className="topbar">
              <div className="page-title"><ListBullets size={22} /><h1>售后订单记录</h1></div>
              <div className="sync-status"><span />后台扫描正常 · 最近同步 {formatDateTime(data.last_synced_at)}</div>
            </header>
            <div className="workspace-body">
              <RecordViewTabs activeView={recordView} counts={data.view_counts} onChange={changeRecordView} />
              <SummaryStrip summary={data.summary} onManual={() => setActiveView("manual")} />
              <FilterPanel draft={draftFilters} setDraft={setDraftFilters} onSubmit={submitFilters} onReset={resetFilters} shops={data.shops} salesOwners={data.sales_owners ?? []} busy={loading} />
              <OrdersTable items={data.items} selected={selected} onSelect={chooseOrder} loading={loading} error={error} onRetry={() => setRefreshKey((key) => key + 1)} />
              <div className="workspace-actions">
                <button type="button" className="button secondary" onClick={exportRows} disabled={!data.items.length}><DownloadSimple size={16} />导出</button>
                <button type="button" className="button secondary" onClick={() => setRefreshKey((key) => key + 1)} disabled={loading}><ArrowsClockwise size={16} />刷新</button>
                <Pagination pagination={data.pagination} onPage={changePage} onPageSize={changePageSize} />
              </div>
            </div>
          </main>
          {detailOpen && <DetailPanel detail={detail} loading={detailLoading} onClose={() => setDetailOpen(false)} onCopy={copyValue} copied={copied} />}
          {!detailOpen && selected && <button type="button" className="open-detail" onClick={() => setDetailOpen(true)}>打开售后详情</button>}
        </>
      ) : activeView === "intercepts" ? (
        <InterceptWorkspace detailOpen={interceptDetailOpen} setDetailOpen={setInterceptDetailOpen} />
      ) : activeView === "attribution" ? (
        <AttributionWorkspace />
      ) : activeView === "scrap" ? (
        <ScrapWorkspace onClose={() => setActiveView("orders")} />
      ) : activeView === "manual" ? (
        <ManualTodoWorkspace />
      ) : activeView === "monitor" ? (
        <MonitorWorkspace />
      ) : (
        <WarehouseWorkspace />
      )}
    </div>
  );
}
