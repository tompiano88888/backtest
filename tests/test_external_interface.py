"""External strategies must work using only the installed framework and inline inputs."""
import json
import subprocess
import sys
from pathlib import Path

from backtest.cli import demo_config
from backtest.models import Instruction
from backtest.runner import engine_from_config, save_result


def test_external_callback_entry_exports_standard_results(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / 'external.json'
    subprocess.run([sys.executable, str(root/'examples/external_callback.py'), '--output', str(output)],
                   cwd=root, check=True, capture_output=True, text=True)
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['provenance']['strategy']['params'] == {'quantity': 1}
    assert result['fills']
    assert 'residual' in result
    report = tmp_path/'external'/'standard_report'
    assert {p.name for p in report.iterdir()} == {'performance.png', 'metrics.json', 'annual.csv', 'daily.csv'}
    assert json.loads((report/'metrics.json').read_text(encoding='utf-8'))['pnl'] == result['final']['pnl']


def test_save_result_finalizes_manual_engine_once(tmp_path):
    from datetime import datetime, timezone
    from backtest.engine import Frame
    config = demo_config()
    engine = engine_from_config(config)
    engine.submit([Instruction('A', 1, 1, 'open', 'external')], datetime(2026, 9, 20, tzinfo=timezone.utc))
    engine.step(Frame(datetime(2026, 9, 21, tzinfo=timezone.utc), {}, frozenset()))
    # Explicit accounting date makes this empty frame's report unambiguous.
    config['frames'] = [{'accounting_day_end': '2026-09-21'}]
    first = save_result(engine, config, tmp_path/'first.json')
    second = save_result(engine, config, tmp_path/'second.json')
    assert sum(w['code'] == 'PENDING_OPERATION_AT_END' for w in first['warnings']) == 1
    assert first['warnings'] == second['warnings']
