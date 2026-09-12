"""Package the selected running releases; never include .env, databases or local edits."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


def build(root: Path, output: Path) -> dict:
    root = root.resolve()
    if output.exists():
        raise ValueError('Output already exists; choose a new release filename')
    files: dict[str, bytes] = {}
    pointers = {}
    for name in ('module1-worker-release', 'workbench-web-release'):
        pointer = root / '.runtime' / f'{name}.json'
        data = json.loads(pointer.read_text(encoding='utf-8-sig'))
        source = (root / data['source_path']).resolve()
        if not source.is_relative_to(root / '.runtime' / 'releases') or source.name != 'src':
            raise ValueError('Release source must be .runtime/releases/<release>/src')
        if not (source / 'aftersales_workbench' / '__init__.py').is_file():
            raise ValueError('Missing release package')
        pointers[name] = data
        files[f'.runtime/{name}.json'] = pointer.read_bytes()
        for item in source.parent.rglob('*'):
            if not item.is_file() or '__pycache__' in item.parts or item.suffix in ('.pyc', '.pyo'):
                continue
            rel = item.relative_to(source.parent)
            if rel.parts[0] not in ('src', 'frontend') or item.is_symlink():
                raise ValueError(f'Unexpected release artifact: {rel}')
            if item.name == '.env' or item.suffix in ('.sqlite3', '.sql', '.pem', '.key'):
                raise ValueError(f'Private state is not a release artifact: {rel}')
            files[item.relative_to(root).as_posix()] = item.read_bytes()
        if name == 'module1-worker-release':
            for item in source.rglob('*.py'):
                if '__pycache__' not in item.parts:
                    files['src/' + item.relative_to(source).as_posix()] = item.read_bytes()
    # Packaging metadata and migrations come from the recorded source commit, not dirty files.
    commit = pointers['module1-worker-release']['code_commit']
    tracked = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', commit], cwd=root, text=True).splitlines()
    for path in tracked:
        if path in ('pyproject.toml', 'README.md', 'alembic.ini') or path.startswith('migrations/'):
            files[path] = subprocess.check_output(['git', 'show', f'{commit}:{path}'], cwd=root)
    for name in ('module1-autostart.ps1', 'module1-autostart-hidden.py', 'module1-worker.ps1'):
        files['scripts/' + name] = (root / 'scripts' / name).read_bytes()
    manifest = {'format': 1, 'releases': pointers, 'files': {p: hashlib.sha256(b).hexdigest() for p, b in sorted(files.items())}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
        archive.writestr('office-release-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.root, args.output)
    print(json.dumps({'files': len(result['files']), 'output': str(args.output), 'releases': list(result['releases'])}))
