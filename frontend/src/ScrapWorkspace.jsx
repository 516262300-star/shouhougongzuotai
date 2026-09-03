import { useEffect, useMemo, useState } from "react";
import {
  CalendarBlank,
  CaretRight,
  DownloadSimple,
  Info,
  MagnifyingGlass,
  Trash,
  X,
} from "@phosphor-icons/react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const DAY = 86400000;
const today = new Date();
const isoDate = (value) => {
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${value.getFullYear()}-${month}-${day}`;
};
const initialFilters = {
  start: isoDate(new Date(today.getTime() - 89 * DAY)),
  end: isoDate(today),
  reason: "",
  responsibility: "",
  status: "",
  keyword: "",
};
const STATUS_LABELS = {
  MISSING_REASON: "待补原因",
  MISSING_COST: "待核成本",
  CONFIRMED: "已确认",
};
const COLORS = ["#337bd8", "#5794df", "#cf75ab", "#f2ad5b", "#b8c7de", "#80b8a4"];
const emptyData = {
  summary: { return_quantity: 0, scrap_quantity: 0, scrap_rate: 0, confirmed_loss: 0 },
  models: [], reasons: [], trend: [], focus: null,
  options: { reasons: [], responsibilities: [] }, sync: {},
};
const money = (value) => new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", minimumFractionDigits: 2 }).format(value || 0);
const quantity = (value) => Number(value || 0).toLocaleString("zh-CN", { maximumFractionDigits: 4 });

function ScrapTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return <div className="scrap-tooltip"><strong>{label}</strong><span>报废率 {Number(payload[0].value).toFixed(2)}%</span></div>;
}

function MetricCard({ label, value, tone, note }) {
  return <article><span>{label}</span><strong className={tone}>{value}</strong><small>{note}</small></article>;
}

function buildQuery(filters, focusModel) {
  const params = new URLSearchParams({ started_on: filters.start, ended_on: filters.end });
  if (filters.keyword) params.set("model_keyword", filters.keyword);
  if (filters.reason) params.set("reason", filters.reason);
  if (filters.responsibility) params.set("responsibility", filters.responsibility);
  if (filters.status) params.set("data_status", filters.status);
  if (focusModel) params.set("focus_model", focusModel);
  return params.toString();
}

export function ScrapWorkspace({ onClose }) {
  const [draft, setDraft] = useState(initialFilters);
  const [filters, setFilters] = useState(initialFilters);
  const [selectedModel, setSelectedModel] = useState("");
  const [data, setData] = useState(emptyData);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showDefinitions, setShowDefinitions] = useState(false);
  const [showRecords, setShowRecords] = useState(false);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);

  const load = async (activeFilters = filters, focusModel = selectedModel) => {
    setLoading(true); setError("");
    try {
      const response = await fetch(`/api/v1/scrap/overview?${buildQuery(activeFilters, focusModel)}`);
      if (!response.ok) throw new Error(`接口返回 ${response.status}`);
      const payload = await response.json();
      setData(payload);
      if (!focusModel && payload.models?.length) setSelectedModel(payload.models[0].model);
    } catch (reason) {
      setError(`读取 ERP 报废数据失败：${reason.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(initialFilters, ""); }, []);

  const selected = useMemo(
    () => data.models.find((row) => row.model === selectedModel) ?? data.models[0] ?? null,
    [data.models, selectedModel],
  );
  const focus = data.focus?.model === selected?.model ? data.focus : null;
  const reasons = data.reasons.map((row, index) => ({ ...row, color: COLORS[index % COLORS.length] }));
  const trends = data.trend.map((row) => ({ ...row, label: row.date.slice(5) }));
  const syncTime = data.sync?.last_run_at ? new Date(data.sync.last_run_at).toLocaleString("zh-CN", { hour12: false }) : "尚未完成首次同步";

  const applyFilters = (event) => {
    event.preventDefault();
    setFilters(draft); setSelectedModel(""); setShowRecords(false); load(draft, "");
  };
  const reset = () => {
    setDraft(initialFilters); setFilters(initialFilters); setSelectedModel(""); setShowRecords(false); load(initialFilters, "");
  };
  const selectRow = (model) => {
    setSelectedModel(model); setShowRecords(false); load(filters, model);
  };
  const exportRows = () => {
    const rows = [["型号/规格", "退货数量", "报废数量", "数量报废率", "已核定损失", "主要责任"], ...data.models.map((row) => [row.model, row.return_quantity, row.scrap_quantity, `${row.scrap_rate}%`, row.confirmed_loss, row.responsibility])];
    const csv = rows.map((row) => row.map((cell) => `"${cell}"`).join(",")).join("\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }));
    link.download = `退货报废型号诊断-${filters.start}-${filters.end}.csv`; link.click(); URL.revokeObjectURL(link.href);
  };
  const beginEdit = (record) => setEditing({ ...record, scrap_reason: record.reason || "", responsibility: record.responsibility || "", confirmed_unit_cost: record.confirmed_unit_cost ?? "", loss_amount: record.loss_amount ?? "", cost_source: record.cost_source || "", reviewer: record.reviewer || "" });
  const saveDecision = async (event) => {
    event.preventDefault(); setSaving(true); setError("");
    try {
      const payload = {
        scrap_reason: editing.scrap_reason || null,
        responsibility: editing.responsibility || null,
        confirmed_unit_cost: editing.confirmed_unit_cost === "" ? null : Number(editing.confirmed_unit_cost),
        loss_amount: editing.loss_amount === "" ? null : Number(editing.loss_amount),
        cost_source: editing.cost_source || null,
        reviewer: editing.reviewer || null,
      };
      const response = await fetch(`/api/v1/scrap/records/${encodeURIComponent(editing.source_row_id)}/decision`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (!response.ok) throw new Error(`保存失败（${response.status}）`);
      setEditing(null); await load(filters, selectedModel);
    } catch (reason) { setError(reason.message); } finally { setSaving(false); }
  };

  return <>
    <main className="workspace scrap-workspace">
      <header className="topbar">
        <div className="page-title"><Trash size={22} weight="duotone" /><h1>退货报废</h1><span className="read-only-badge">模块 5</span><span className="erp-badge">ERP 已接入 · 只读</span></div>
        <div className="scrap-top-actions"><div className="sync-status"><span />最近同步 {syncTime}</div><button type="button" className="definition-button" onClick={() => setShowDefinitions((value) => !value)}><Info size={16} />数据说明</button></div>
        {showDefinitions && <div className="definition-popover"><strong>指标口径</strong><p>颜色以“报废”开头即识别为报废。数量报废率 = 报废数量 ÷ 同期 ERP 退货数量；损失金额只统计已填写原因、损失和复核人的记录，ERP 单价不直接作为损失。</p></div>}
      </header>

      <div className="scrap-body">
        {error && <div className="scrap-error">{error}</div>}
        <section className="scrap-summary" aria-label="退货报废概览">
          <MetricCard label="退货数量" value={quantity(data.summary.return_quantity)} tone="blue" note={`${filters.start} 至 ${filters.end}`} />
          <MetricCard label="报废数量" value={quantity(data.summary.scrap_quantity)} tone="orange" note="ERP 颜色报废口径" />
          <MetricCard label="数量报废率" value={`${Number(data.summary.scrap_rate).toFixed(2)}%`} tone="green" note="同期退货数量口径" />
          <MetricCard label="已核定损失" value={money(data.summary.confirmed_loss)} tone="green" note="仅已确认记录" />
        </section>
        <div className="scrap-official-note">报废数量包含 ERP 已识别记录；财务损失只包含【数据状态 = 已确认】的记录。</div>

        <form className="scrap-filters" onSubmit={applyFilters}>
          <label className="scrap-date"><span>日期范围</span><div><CalendarBlank size={15} /><input type="date" value={draft.start} onChange={(event) => setDraft({ ...draft, start: event.target.value })} /><b>~</b><input type="date" value={draft.end} onChange={(event) => setDraft({ ...draft, end: event.target.value })} /></div></label>
          <label><span>型号/规格</span><input value={draft.keyword} onChange={(event) => setDraft({ ...draft, keyword: event.target.value })} placeholder="搜索型号" /></label>
          <label><span>原因分类</span><select value={draft.reason} onChange={(event) => setDraft({ ...draft, reason: event.target.value })}><option value="">全部</option>{data.options.reasons.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>责任归属</span><select value={draft.responsibility} onChange={(event) => setDraft({ ...draft, responsibility: event.target.value })}><option value="">全部</option>{data.options.responsibilities.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>数据状态</span><select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value })}><option value="">全部</option><option value="MISSING_REASON">待补原因</option><option value="MISSING_COST">待核成本</option><option value="CONFIRMED">已确认</option></select></label>
          <button className="button secondary compact" type="button" onClick={reset}>重置</button>
          <button className="button primary compact" type="submit"><MagnifyingGlass size={15} />查询</button>
          <button className="button secondary compact" type="button" onClick={exportRows}><DownloadSimple size={15} />导出</button>
        </form>

        <section className="scrap-ranking-card">
          <div className="scrap-card-heading"><div><h2>型号报废数量 TOP10</h2><p>优先显示报废数量较多的型号，点击行查看 ERP 明细和补录核定信息。</p></div><Info size={15} /></div>
          <div className="scrap-table-wrap"><table className="scrap-ranking-table"><thead><tr><th>排名</th><th>型号/规格</th><th>退货数量</th><th>报废数量</th><th>报废率</th><th>已核定损失</th><th>损失占比</th><th aria-label="查看" /></tr></thead><tbody>
            {data.models.map((row, index) => <tr key={row.model} className={selected?.model === row.model ? "selected" : ""} onClick={() => selectRow(row.model)}><td>{index + 1}</td><td><strong>{row.model}</strong><span className="model-bar"><i style={{ width: `${Math.max(8, Math.min(100, row.scrap_rate * 40))}%` }} /></span></td><td>{quantity(row.return_quantity)}</td><td>{quantity(row.scrap_quantity)}</td><td className={row.scrap_rate >= 1 ? "risk-high" : row.scrap_rate >= .6 ? "risk-mid" : "risk-low"}>{row.scrap_rate.toFixed(2)}%</td><td>{money(row.confirmed_loss)}</td><td>{row.loss_share.toFixed(2)}%</td><td><CaretRight size={15} /></td></tr>)}
          </tbody><tfoot><tr><td /><td>合计</td><td>{quantity(data.summary.return_quantity)}</td><td>{quantity(data.summary.scrap_quantity)}</td><td>{Number(data.summary.scrap_rate).toFixed(2)}%</td><td>{money(data.summary.confirmed_loss)}</td><td>{data.summary.confirmed_loss ? "100.00%" : "—"}</td><td /></tr></tfoot></table></div>
          {!loading && !data.models.length && <div className="scrap-empty">当前条件下没有 ERP 报废记录</div>}
          {loading && <div className="scrap-empty">正在读取 ERP 报废数据…</div>}
        </section>

        <div className="scrap-lower-grid">
          <section className="scrap-chart-card reason-card"><div className="scrap-card-heading"><div><h2>原因分布</h2><p>尚未补录的记录归入“待补原因”</p></div></div><div className="reason-chart-body"><div className="donut-wrap"><ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={reasons} dataKey="value" nameKey="name" innerRadius={52} outerRadius={76} paddingAngle={1} isAnimationActive={false}>{reasons.map((entry) => <Cell key={entry.name} fill={entry.color} />)}</Pie><Tooltip /></PieChart></ResponsiveContainer><div className="donut-center"><strong>{quantity(data.summary.scrap_quantity)}</strong><span>报废数量</span></div></div><div className="reason-legend">{reasons.slice(0, 6).map((row) => <div key={row.name}><i style={{ background: row.color }} /><span>{row.name}</span><strong>{quantity(row.value)}</strong><em>({row.share.toFixed(2)}%)</em></div>)}</div></div></section>
          <section className="scrap-chart-card"><div className="scrap-card-heading"><div><h2>报废率趋势</h2><p>数量口径</p></div></div><div className="main-trend"><ResponsiveContainer width="100%" height="100%"><AreaChart data={trends} margin={{ top: 10, right: 16, bottom: 0, left: -12 }}><CartesianGrid stroke="#e4e9ef" strokeDasharray="4 3" vertical={false} /><XAxis dataKey="label" tick={{ fontSize: 10, fill: "#778398" }} interval="preserveStartEnd" axisLine={false} tickLine={false} /><YAxis tickFormatter={(value) => `${value.toFixed(1)}%`} tick={{ fontSize: 10, fill: "#778398" }} axisLine={false} tickLine={false} /><Tooltip content={<ScrapTooltip />} /><Area type="linear" dataKey="rate" stroke="#1768d8" strokeWidth={2} fill="#dceaff" dot={false} activeDot={{ r: 4 }} isAnimationActive={false} /></AreaChart></ResponsiveContainer></div></section>
        </div>
        <div className="scrap-footnote">ERP 接入为只读；不保存寄件人、电话、运单号等非必要个人信息。人工核定不会回写 ERP。</div>
      </div>
    </main>

    <aside className="detail-panel scrap-detail-panel">
      <div className="detail-heading"><h2>型号诊断</h2><button type="button" className="icon-button" aria-label="关闭型号诊断" onClick={onClose}><X size={18} /></button></div>
      <div className="scrap-detail-scroll">
        <div className="scrap-detail-title"><div><h3>{selected?.model || "暂无型号"}</h3><span>ERP 实时数据</span></div></div>
        {focus && <>
          <section className="scrap-detail-section"><h4>基础信息</h4><dl className="scrap-facts"><div><dt>型号/规格</dt><dd>{focus.model}</dd></div><div><dt>同期退货数量</dt><dd>{quantity(focus.return_quantity)}</dd></div><div><dt>报废数量</dt><dd>{quantity(focus.scrap_quantity)}</dd></div><div><dt>数量报废率</dt><dd className="risk-high">{focus.scrap_rate.toFixed(2)}%</dd></div><div><dt>已核定损失</dt><dd>{money(focus.confirmed_loss)}</dd></div><div><dt>损失占比</dt><dd>{focus.loss_share.toFixed(2)}%</dd></div></dl></section>
          <section className="scrap-detail-section"><h4>报废颜色分布 <small>按报废数量</small></h4><table className="mini-table"><thead><tr><th>颜色</th><th>数量</th><th>占比</th><th>损失</th></tr></thead><tbody>{focus.colors.slice(0, 6).map((row) => <tr key={row.name}><td>{row.name}</td><td>{quantity(row.value)}</td><td>{row.share.toFixed(2)}%</td><td>{money(row.loss)}</td></tr>)}</tbody></table></section>
          <section className="scrap-detail-section"><h4>主要原因</h4><table className="mini-table"><thead><tr><th>原因</th><th>数量</th><th>占比</th><th>损失</th></tr></thead><tbody>{focus.reasons.slice(0, 6).map((row) => <tr key={row.name}><td>{row.name}</td><td>{quantity(row.value)}</td><td>{row.share.toFixed(2)}%</td><td>{money(row.loss)}</td></tr>)}</tbody></table></section>
        </>}
      </div>
      <button className="scrap-detail-button" type="button" disabled={!focus} onClick={() => setShowRecords(true)}>查看该型号明细与核定</button>
      <div className="scrap-detail-footnote">ERP 原始记录与人工核定信息分层保存</div>
      {showRecords && focus && <div className="scrap-records-drawer"><div className="records-heading"><div><strong>{focus.model} 报废明细</strong><span>共 {focus.records.length} 条 · 点击记录补录</span></div><button type="button" aria-label="关闭明细" onClick={() => { setShowRecords(false); setEditing(null); }}><X size={18} /></button></div>
        {editing ? <form className="scrap-decision-form" onSubmit={saveDecision}><h3>{editing.return_order_sn}</h3><p>{editing.raw_color} · 数量 {quantity(editing.quantity)}</p><label>报废原因<input required value={editing.scrap_reason} onChange={(event) => setEditing({ ...editing, scrap_reason: event.target.value })} /></label><label>责任归属<input value={editing.responsibility} onChange={(event) => setEditing({ ...editing, responsibility: event.target.value })} /></label><label>确认单位成本<input type="number" min="0" step="0.0001" value={editing.confirmed_unit_cost} onChange={(event) => setEditing({ ...editing, confirmed_unit_cost: event.target.value })} /></label><label>损失金额<input type="number" min="0" step="0.01" value={editing.loss_amount} onChange={(event) => setEditing({ ...editing, loss_amount: event.target.value })} placeholder="留空则按数量×单位成本" /></label><label>成本来源<input value={editing.cost_source} onChange={(event) => setEditing({ ...editing, cost_source: event.target.value })} /></label><label>复核人<input value={editing.reviewer} onChange={(event) => setEditing({ ...editing, reviewer: event.target.value })} /></label><div><button className="button secondary" type="button" onClick={() => setEditing(null)}>取消</button><button className="button primary" disabled={saving}>{saving ? "保存中…" : "保存核定"}</button></div></form>
          : <div className="records-list">{focus.records.map((row) => <article key={row.source_row_id} onClick={() => beginEdit(row)}><div><strong>{row.return_order_sn}</strong><time>{row.completed_on}</time></div><p>{row.raw_color} · {row.reason || "待补原因"}</p><span>数量 {quantity(row.quantity)} · {STATUS_LABELS[row.data_status]}</span><b>{row.loss_amount == null ? "待核损失" : money(row.loss_amount)}</b></article>)}</div>}
      </div>}
    </aside>
  </>;
}
