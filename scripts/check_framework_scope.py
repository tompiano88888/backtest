"""Check the explicitly reviewed framework delivery manifest, without reading local research."""
import json
from pathlib import Path
import subprocess


def check(root):
    root = Path(root).resolve()
    expected = set(json.loads((root/'framework_files.json').read_text(encoding='utf-8')))
    missing = sorted(name for name in expected if not (root/name).is_file())
    actual = set()
    for directory in ('backtest', 'scripts', 'tests', 'docs', 'examples'):
        actual.update(p.relative_to(root).as_posix() for p in (root/directory).rglob('*')
                      if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    extra = sorted(actual - expected)
    # In a real checkout inspect the index as well: ignored files forced into Git must not pass.
    try:
        top = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=root,
                                      stderr=subprocess.DEVNULL, text=True).strip()
        if Path(top).resolve() == root:
            tracked = set(subprocess.check_output(['git', 'ls-files'], cwd=root, text=True).splitlines())
            extra = sorted(set(extra) | (tracked - expected))
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    if missing or extra:
        raise ValueError(f'Framework manifest mismatch: missing={missing}, unreviewed={extra}')
    return len(expected)


if __name__ == '__main__':
    print(f'Framework scope checked: {check(Path(__file__).resolve().parents[1])} files')
