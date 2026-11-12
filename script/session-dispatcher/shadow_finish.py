"""Copied unchanged into disposable shadow repositories; the only allowed shell action."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
case = json.loads((root / 'input.json').read_text())
value = json.loads((root / 'result.json').read_text())
assert set(value) == {'assessment', 'issues', 'escalation'}
assert value['assessment'] in ('routine', 'needs_escalation', 'blocked')
assert isinstance(value['issues'], list) and all(isinstance(x, str) for x in value['issues'])
assert isinstance(value['escalation'], list) and set(value['escalation']) <= set(case['risks'])
assert 0 < (root / 'review.md').stat().st_size <= 65536
assert (root / 'result.json').stat().st_size <= 65536
def git(*args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
paths = git('status', '--porcelain', '--untracked-files=all').splitlines()
assert {line[3:] for line in paths} == {'result.json', 'review.md'}, 'Unexpected report-repository changes'
for name, pin in case['files'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == pin, name
subprocess.run(['git', '-C', str(root), 'add', '--', 'result.json', 'review.md'], check=True)
subprocess.run(['git', '-C', str(root), 'commit', '-m', 'docs: advisory shadow review',
                '-m', 'OpenCode-Task: ' + case['shadow_task']], check=True)
