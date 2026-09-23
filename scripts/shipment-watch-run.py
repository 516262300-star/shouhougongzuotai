"""计划任务无窗口入口；只导入经过核验的独立发布目录。"""

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    runtime = root / ".runtime"
    pointer = json.loads((runtime / "shipment-watch-release.json").read_text(encoding="utf-8-sig"))
    if pointer.get("enabled") is not True:
        return
    source = (root / pointer["source_path"]).resolve(strict=True)
    if not source.is_relative_to((runtime / "releases").resolve()) or source.name != "src":
        raise ValueError("提醒版本必须位于独立发布目录")
    sys.path.insert(0, str(source))
    os.environ["AFTERSALES_RUNTIME_ROOT"] = str(root)
    from aftersales_workbench.core.config import get_settings
    from aftersales_workbench.workflows.shipment_watch_cli import run

    # 缩短单轮核验，避免旧批次占用十余分钟导致新到20小时的订单迟迟不能入队。
    options = {}
    if "JD" in pointer.get("platforms", []):
        options = {"platforms": pointer["platforms"],
                   "jd_carrier_map": pointer.get("jd_carrier_map", {}),
                   "jd_seller_ids": pointer.get("jd_seller_ids", {})}
    result = run(get_settings(), publish=True, max_windows=16, limit=200, **options)
    result["completed_at"] = datetime.now().isoformat()
    line = json.dumps(result, ensure_ascii=False)
    with (runtime / "shipment-watch.log").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    (runtime / "shipment-watch-status.json").write_text(line, encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        path = Path(__file__).resolve().parents[1] / ".runtime/shipment-watch-error.log"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(datetime.now().isoformat() + "\n" + traceback.format_exc() + "\n")
        raise
