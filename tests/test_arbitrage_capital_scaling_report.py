import csv
from pathlib import Path

import pytest

from scripts.compare_arbitrage_floor import risk


def write_csv(path, rows):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)


def test_risk_uses_actual_starting_capital_and_is_scale_invariant(tmp_path):
    results=[]
    for capital in (10_000_000,100_000_000):
        root=tmp_path/str(capital);root.mkdir()
        write_csv(root/'summary.csv',[dict(initial_capital=capital)])
        write_csv(root/'daily.csv',[
            dict(equity=capital*9//10,is_market_day=True),
            dict(equity=capital*99//100,is_market_day=True)])
        results.append(risk(root))
    assert results[0]==results[1]
    assert float(results[1]['max_drawdown'])==pytest.approx(.1)
    assert results[1]['sharpe']==pytest.approx(0)
