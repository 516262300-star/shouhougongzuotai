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

const PAGE_SIZE_OPTIONS = [15, 30, 50];

function inputDate(date) {
  const offset = date.getTimezoneOffset();
  return new Date(date.getTime() - offset * 60_000).toISOString().slice(0, 10);
}

function createInitialFilters() {
  const end = new Date();
  const start = new Date(end);
  start.setDate(end.getDate() - 7);
  return {
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

function SummaryStrip({ summary }) {
  const items = [
    { label: "今日新增", value: summary.today_new ?? 0, tone: "blue" },
    { label: "待拦截", value: summary.pending_intercept ?? 0, tone: "orange" },
    { label: "待人工", value: summary.manual ?? 0, tone: "orange" },
    { label: "已完成", value: summary.completed ?? 0, tone: "green" },
  ];
  return (
    <section className="summary-strip" aria-label="售后订单摘要">
      {items.map((item) => (
        <div className="summary-item" key={item.label}>
          <span>{item.label}</span>
          <strong className={`metric-${item.tone}`}>{item.value}</strong>
        </div>
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
    { id: "manual", label: "人工待办", icon: User, enabled: false },
    { id: "monitor", label: "运行监控", icon: ChartBar, enabled: false },
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
        模块 1 后台运行中
      </div>
    </aside>
  );
}

function FilterPanel({ draft, setDraft, onSubmit, onReset, shops, salesOwners, busy }) {
  const update = (key) => (event) => setDraft((current) => ({ ...current, [key]: event.target.value }));
  return (
    <form className="filters" onSubmit={onSubmit}>
      <div className="filter-row filter-row-primary">
        <label>
          <span>店铺</span>
          <select value={draft.shop_id} onChange={update("shop_id")}>
            <option value="">全部</option>
            {shops.map((shop) => <option value={shop.shop_id} key={shop.shop_id}>{shop.shop_name}</option>)}
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
        <div className="date-field">
          <span>申请时间</span>
          <div className="date-range">
            <CalendarBlank size={16} />
            <input type="date" value={draft.started_on} onChange={update("started_on")} aria-label="开始日期" />
            <b>~</b>
            <input type="date" value={draft.ended_on} onChange={update("ended_on")} aria-label="结束日期" />
          </div>
        </div>
      </div>
      <div className="filter-row filter-row-secondary">
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
              <td title={item.shop_name}><span className="truncate shop-cell">{item.shop_name}</span></td>
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

function InterceptSummaryStrip({ summary }) {
  const items = [
    { label: "待发拦截", value: summary.waiting_notice ?? 0, tone: "orange" },
    { label: "退款冻结", value: summary.refund_blocked ?? 0, tone: "orange" },
    { label: "已退款待退回", value: summary.waiting_return ?? 0, tone: "blue" },
    { label: "待匹配ERP退货单", value: summary.waiting_erp_match ?? 0, tone: "green" },
  ];
  return (
    <section className="summary-strip" aria-label="在途拦截摘要">
      {items.map((item) => (
        <div className="summary-item" key={item.label}>
          <span>{item.label}</span>
          <strong className={`metric-${item.tone}`}>{item.value}</strong>
        </div>
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
            {shops.map((shop) => <option value={shop.shop_id} key={shop.shop_id}>{shop.shop_name}</option>)}
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
            <option value="ERP_MATCH">待匹配ERP退货单</option>
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
            <th>物流状态</th><th>退款闸门</th><th>当前环节</th><th>最近更新</th><th>操作</th>
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
  const copyValue = async (value) => { await navigator.clipboard.writeText(String(value)); setCopied(true); window.setTimeout(() => setCopied(false), 1500); };

  return (
    <>
      <main className="workspace">
        <header className="topbar">
          <div className="page-title"><Truck size={22} /><h1>在途拦截</h1><span className="read-only-badge">只读监控</span></div>
          <div className="sync-status"><span />模块1后台运行 · 最近同步 {formatDateTime(data.last_synced_at)}</div>
        </header>
        <div className="workspace-body">
          <InterceptSummaryStrip summary={data.summary} />
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
  const [recordView, setRecordView] = useState("WORKBENCH");
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

  return (
    <div className={`app-shell ${(
      activeView === "orders" ? detailOpen : activeView === "intercepts" ? interceptDetailOpen : true
    ) ? "" : "without-detail"}`}>
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
              <SummaryStrip summary={data.summary} />
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
      ) : (
        <WarehouseWorkspace />
      )}
    </div>
  );
}
