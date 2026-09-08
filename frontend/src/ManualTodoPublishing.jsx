import { useEffect, useState } from "react";
import { readPublishControl, savePublishControl } from "./manualTodoPublishing.mjs";
import "./ManualTodoPublishing.css";

const displayTime = (value) => value ? value.replace("T", " ").slice(0, 19) : "尚未通过网页调整";

export function ManualTodoPublishing() {
  const [control, setControl] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (saving) return;
    const controller = new AbortController();
    let latestRequest = 0;
    const load = async () => {
      const requestId = ++latestRequest;
      try {
        const state = await readPublishControl(controller.signal);
        if (!controller.signal.aborted && requestId === latestRequest) {
          setControl(state);
          setError("");
        }
      } catch (failure) {
        if (!controller.signal.aborted && requestId === latestRequest) {
          setControl(null);
          setError(`开关状态无法确认：${failure.message}。请刷新后再操作。`);
        }
      }
    };
    load();
    const timer = setInterval(load, 10000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [saving, refresh]);

  const toggle = async () => {
    if (!control || saving) return;
    const next = !control.enabled;
    const message = next
      ? `开启人工待办自动发布？\n当前有 ${control.pending_count} 条待发送。开启后，后台会按原规则处理积压和新待办，并真实发布到对应业务员的 ERP 待办中。`
      : `关闭人工待办自动发布？\n本地待办继续保留，售后同步、退款和 ERP 平账不受影响。已发送的待办不撤回，已开始提交的任务可能继续完成。`;
    if (!window.confirm(message)) return;
    setSaving(true); setError(""); setNotice("");
    try {
      const state = await savePublishControl(next, control.version, AbortSignal.timeout(12000));
      setControl(state);
      setNotice("开关已保存，无需重启。当前是否发布以上方实时状态为准。");
    } catch (failure) {
      setControl(null);
      setError(`操作未确认：${failure.message}。正在重新读取实际状态；请勿盲目重复切换。`);
    } finally { setSaving(false); }
  };

  const stateText = !control ? "状态待确认" : control.effective_enabled ? "已开启" : control.enabled ? "已开启，但被总开关或配置阻止" : "已关闭 · 仅保留本地待办";
  return (
    <section className="todo-publishing" aria-label="人工待办自动发布设置" aria-busy={saving}>
      <div className="todo-publishing-main">
        <div className="todo-publishing-copy">
          <h2 id="todo-publishing-label">自动发布给业务员</h2>
          <p>控制模块 1 / 2 / 3 的 ERP 人工待办发送。关闭不停止生成本地待办，也不影响其他自动化。</p>
          <span className={`todo-publishing-state ${control?.effective_enabled ? "is-enabled" : ""}`}>{saving ? "正在保存…" : stateText}</span>
        </div>
        <div className="todo-publishing-actions">
          <button type="button" role="switch" aria-checked={control?.enabled ?? false} aria-labelledby="todo-publishing-label" className="todo-publishing-switch" disabled={saving || !control || (!control.enabled && !control.can_enable)} onClick={toggle}>
            <span aria-hidden="true" />
          </button>
          <button className="link-button" type="button" disabled={saving} onClick={() => { setError(""); setRefresh((v) => v + 1); }}>刷新状态</button>
        </div>
      </div>
      {control && <div className="todo-publishing-meta"><span>待发送 {control.pending_count} 条</span><span>发送中 {control.running_count} 条</span><span>失败 {control.failed_count} 条</span><span>最近调整：{displayTime(control.updated_at)}</span></div>}
      {control?.blocked_reason && <p className="todo-publishing-warning">{control.blocked_reason}。关闭操作仍可使用。</p>}
      {control?.running_count > 0 && <p className="todo-publishing-warning">已有 {control.running_count} 条任务进入发送流程；关闭后请在发送审计中核实最终结果。</p>}
      {error && <p className="todo-publishing-error" role="alert">{error}</p>}
      {notice && <p className="todo-publishing-notice" role="status">{notice}</p>}
      {control?.recent_changes?.length > 0 && <details className="todo-publishing-history"><summary>最近开关操作记录</summary><ul>{control.recent_changes.map((event) => <li key={event.version}>{displayTime(event.changed_at)} · {event.enabled ? "开启" : "关闭"} · 本机网页</li>)}</ul></details>}
    </section>
  );
}
