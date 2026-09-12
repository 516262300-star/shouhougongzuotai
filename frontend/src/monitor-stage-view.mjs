// 同步隔离单不代表同步程序失败；保留其他状态，不能把未知或失败改成正常。
export function runtimeStage(stage) {
  if (stage.id === "sync" && ["warning", "acknowledged"].includes(stage.status)
    && stage.shops_failed === 0) {
    return { ...stage, status: "completed", error: null, source_error: null };
  }
  return stage;
}

export function stageHasFailure(stage) {
  return stage.status === "failed";
}
