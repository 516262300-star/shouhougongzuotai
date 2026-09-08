"""Build a source-and-static-assets bundle without local secrets or business data."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_FILES = (
    "compose.yaml", "Dockerfile", "Dockerfile.dockerignore", "cloud_app.py",
    "init-env.sh", "start.sh", "README.md",
)


def collect_files() -> list[Path]:
    files = [ROOT / name for name in ("pyproject.toml", "README.md", "alembic.ini")]
    files.extend(ROOT / "deploy/cloud-test" / name for name in DEPLOY_FILES)
    for directory in ("src", "migrations"):
        files.extend(
            path for path in (ROOT / directory).rglob("*")
            if path.is_file() and path.suffix in {".py", ".mako"}
            and not any(p == "__pycache__" or p.endswith(".egg-info") for p in path.parts)
        )
    client = ROOT / "frontend/dist/client"
    if not (client / "index.html").is_file():
        raise RuntimeError("Missing frontend build")
    allowed_assets = {".html", ".js", ".css", ".woff", ".woff2", ".ttf", ".svg", ".png", ".ico"}
    files.extend(
        path for path in client.rglob("*") if path.is_file() and path.suffix in allowed_assets
    )
    result = sorted(set(files))
    for path in result:
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"Refusing file outside the source tree: {path}")
    return result


def main() -> None:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError("Node.js/npm is required to build the frontend")
    subprocess.run([npm, "exec", "--", "vite", "build"], cwd=ROOT / "frontend", check=True)
    files = collect_files()
    created = datetime.now(UTC)
    output_dir = ROOT / "dist/cloud-test"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"lds-aftersales-test-{created:%Y%m%d}.tar.gz"
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "-c", "core.quotepath=false", "status", "--short"], cwd=ROOT, text=True,
        encoding="utf-8",
    ).splitlines()
    manifest = {
        "created_utc": created.isoformat(),
        "base_commit": revision,
        "working_tree_status": dirty,
        "note": (
            "Current working-tree snapshot; may include uncommitted application changes. "
            "No credentials or database included."
        ),
        "files": {},
    }
    with tarfile.open(output, "w:gz") as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            if path.suffix == ".sh":
                data = data.replace(b"\r\n", b"\n")
            manifest["files"][relative] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o755 if path.suffix == ".sh" else 0o644
            archive.addfile(info, io.BytesIO(data))
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("SOURCE_MANIFEST.json")
        info.size = len(data)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(data))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{checksum}  {output.name}\n", encoding="ascii",
    )
    print(json.dumps({"archive": str(output), "bytes": output.stat().st_size,
                      "files": len(files), "sha256": checksum}, ensure_ascii=False))


if __name__ == "__main__":
    main()
