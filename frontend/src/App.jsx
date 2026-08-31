import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowCounterClockwise,
  ArrowsClockwise,
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
    after_sales_type: "",
    workflow_status: "",
    logistics_state: "",
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

const formatCurrency = (value) =>
  new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
  }).format(value ?? 0);

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

function Sidebar() {
  const nav = [
    { label: "售后订单", icon: ClipboardText, active: true },
    { label: "在途拦截", icon: Truck },
    { label: "人工待办", icon: User },
    { label: "运行监控", icon: ChartBar },
  ];
  return (
    <aside className="sidebar">
      <div className="brand">利德仕售后工作台</div>
      <nav aria-label="主导航">
        {nav.map(({ label, icon: Icon, active }) => (
          <button key={label} className={`nav-item ${active ? "active" : ""}`} type="button" aria-current={active ? "page" : undefined}>
            <Icon size={21} weight={active ? "fill" : "regular"} />
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

function FilterPanel({ draft, setDraft, onSubmit, onReset, shops, busy }) {
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
      <table>
        <thead>
          <tr>
            <th>店铺</th><th>售后单号</th><th>类型</th><th>退款金额</th><th>发货运单</th>
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
              <td>{item.after_sales_type_label}</td>
              <td>{formatCurrency(item.refund_amount)}</td>
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
              <DetailRow label="快递单号" value={detail.tracking_number} copyable onCopy={onCopy} />
              <DetailRow label="申请时间" value={formatDateTime(detail.created_at, true)} />
              <DetailRow label="售后类型" value={detail.after_sales_type} />
              <DetailRow label="退款金额" value={formatCurrency(detail.refund_amount)} />
              <DetailRow label="商品名称" value={detail.product_name} />
              <DetailRow label="买家昵称" value={detail.buyer_name} />
            </dl>
          </section>
          <section className="detail-section decision-section">
            <h3>当前处理决策</h3>
            <dl>
              <DetailRow label="拦截策略" value={detail.decision.strategy} />
              <div className="detail-row"><dt>当前状态</dt><dd><StatusTag tone={detail.decision.status_tone}>{detail.decision.status}</StatusTag></dd></div>
              <DetailRow label="处理人" value={detail.decision.handler} />
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

export function App() {
  const [draftFilters, setDraftFilters] = useState(createInitialFilters);
  const [filters, setFilters] = useState(createInitialFilters);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(15);
  const [data, setData] = useState({ summary: {}, shops: [], items: [], pagination: { page: 1, page_size: 15, total: 0, pages: 1 }, last_synced_at: null });
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
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
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
  }, [filters, page, pageSize, refreshKey]);

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

  const changePage = (nextPage) => {
    if (nextPage >= 1 && nextPage <= data.pagination.pages && nextPage !== page) setPage(nextPage);
  };

  const changePageSize = (nextSize) => {
    setPageSize(nextSize);
    setPage(1);
  };

  const exportRows = () => {
    const rows = [["店铺", "售后单号", "类型", "退款金额", "发货运单", "物流状态", "拦截状态", "平台退款", "最近更新"]];
    data.items.forEach((item) => rows.push([
      item.shop_name, item.after_sales_sn, item.after_sales_type_label, item.refund_amount,
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
    <div className={`app-shell ${detailOpen ? "" : "without-detail"}`}>
      <Sidebar />
      <main className="workspace">
        <header className="topbar">
          <div className="page-title"><ListBullets size={22} /><h1>售后订单记录</h1></div>
          <div className="sync-status"><span />后台扫描正常 · 最近同步 {formatDateTime(data.last_synced_at)}</div>
        </header>
        <div className="workspace-body">
          <SummaryStrip summary={data.summary} />
          <FilterPanel draft={draftFilters} setDraft={setDraftFilters} onSubmit={submitFilters} onReset={resetFilters} shops={data.shops} busy={loading} />
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
    </div>
  );
}
