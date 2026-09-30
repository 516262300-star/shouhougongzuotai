"""从已核对正式源码生成残留问题修复候选；只生成新目录，不切换服务。"""

import argparse
import hashlib
import json
import pathlib
import shutil


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build(source, destination, *, component):
    root = pathlib.Path(__file__).resolve().parents[1]
    source, destination = source.resolve(), destination.resolve()
    if (
        destination.exists()
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise ValueError("候选目录必须不存在，且不能与源目录嵌套")
    spec = json.loads((root / "patches/residual-production-20260930.json").read_text("utf-8"))
    package = source / "aftersales_workbench"
    changes = {}
    for name, expected in spec["shared_before_sha256"].items():
        original = (package / name).read_text("utf-8-sig")
        if digest(original) != expected:
            raise ValueError(f"正式源已变化，必须重新合并：{name}")
        changes[name] = (root / "src/aftersales_workbench" / name).read_text("utf-8-sig")
    for name in spec["new_files"]:
        if (package / name).exists():
            raise ValueError(f"候选新增文件已存在，必须复核：{name}")
        changes[name] = (root / "src/aftersales_workbench" / name).read_text("utf-8-sig")
    if component == "worker":
        for name, patch in spec["worker_patches"].items():
            value = (package / name).read_text("utf-8-sig")
            if digest(value) != patch["before_sha256"]:
                raise ValueError(f"Worker源已变化，必须重新合并：{name}")
            for before, after in patch["replacements"]:
                if value.count(before) != 1:
                    raise ValueError(f"补丁上下文不唯一：{name}")
                value = value.replace(before, after)
            if digest(value) != patch["after_sha256"]:
                raise ValueError(f"补丁校验失败：{name}")
            changes[name] = value
    for name, value in changes.items():
        compile(value, name, "exec")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name, value in changes.items():
        target = destination / "aftersales_workbench" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    manifest = {
        "component": component,
        "source": str(source),
        "deployed": False,
        "changed_files": {k: digest(v) for k, v in changes.items()},
    }
    (destination / "residual-candidate.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--destination", type=pathlib.Path, required=True)
    parser.add_argument("--component", choices=["worker", "web"], required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            build(args.source, args.destination, component=args.component), ensure_ascii=False
        )
    )
