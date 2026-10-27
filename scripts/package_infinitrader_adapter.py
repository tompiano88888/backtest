"""Build/install a separate PythonGO simulation bundle without starting trading."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import csv
from dataclasses import asdict
from datetime import date

ROOT=Path(__file__).resolve().parents[1]


def write_new(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as h:h.write(data)


def checkpoint_estimates():
    from backtest.fees import HistoricalFees
    checkpoint=json.loads((ROOT/'configs/rbreaker.current_checkpoint.json').read_text(encoding='utf-8'))
    cfg=checkpoint['config'];fee=cfg['historical_fees']
    provider=HistoricalFees(ROOT/fee['root'],fee['version'],fee['as_of'])
    rows=list(csv.DictReader((ROOT/checkpoint['result']/'main_mapping.csv').open(encoding='utf-8-sig')))
    last=max(r['day'] for r in rows);products={};contracts={}
    def record(code,rule):
        return {**{k:str(v) for k,v in asdict(rule).items()},'source_contract':code}
    for code,values in provider.records.items():
        contracts[code.removesuffix('.CFE')]=record(code,values[-1][2])
    for r in rows:
        if r['day']==last:products[r['product']]=record(r['instrument'],provider.resolve(r['instrument'],date.fromisoformat(last)))
    return dict(fee_version=fee['version'],as_of=fee['as_of'],observed_through=last,
                research_margin_rate=cfg['margin_rate'],contracts=contracts,products=products,
                status='user-authorized planning estimates; actual account risk and commission queried separately')


def bundle(destination,investor='',mode='observe'):
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('New bundle directory required; existing code/state is never overwritten')
    destination.mkdir(parents=True)
    runtime=destination/'runtime'
    for package in ('backtest','infinitrader_adapter'):
        for source in (ROOT/package).rglob('*.py'):
            write_new(runtime/source.relative_to(ROOT),source.read_bytes())
    conf=destination/'configuration'
    config=json.loads((ROOT/'configs/infinitrader.simulation.example.json').read_text(encoding='utf-8'))
    config['investor']=investor
    config['mode']=mode
    write_new(conf/'simulation.json',json.dumps(config,ensure_ascii=False,indent=2).encode())
    write_new(conf/'checkpoint_estimates.json',json.dumps(checkpoint_estimates(),ensure_ascii=False,indent=2).encode())
    write_new(conf/'counter_confirmed_rules.json',(ROOT/'configs/infinitrader.counter_rules.example.json').read_bytes())
    source=ROOT/'platform_strategies/RBreakerSimulation.py'
    write_new(destination/'RBreakerSimulation.py',source.read_bytes())
    bootstrap=dict(project_root=str(runtime),config_file=str(conf/'simulation.json'))
    write_new(destination/'RBreakerSimulation.runtime.json',json.dumps(bootstrap,ensure_ascii=False,indent=2).encode())
    write_new(destination/'README.md',(ROOT/'docs/RBREAKER_INFINITRADER_SIMULATION.md').read_bytes())
    hashes={str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in destination.rglob('*') if p.is_file()}
    write_new(destination/'manifest.json',json.dumps(hashes,ensure_ascii=False,indent=2).encode())
    return destination


def inspect_sdk(platform_root):
    root=Path(platform_root)/'pyStrategy'/'pythongo'
    files=['base.py','infini.py','classdef/account.py','classdef/instrument.py','core.pyi']
    methods={};hashes={}
    for file in files:
        path=root/file
        source=path.read_text(encoding='utf-8-sig')
        hashes[file]=hashlib.sha256(path.read_bytes()).hexdigest()
        methods[file]=sorted({n.name for n in ast.walk(ast.parse(source)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))})
    required={'get_account_fund_data','get_instrument_data','get_instruments_by_product','make_order_req','cancel_order'}
    missing=sorted(required-set(methods['base.py']))
    return dict(status='passed' if not missing else 'missing_required_methods',missing=missing,files_sha256=hashes,
                methods=methods,per_contract_fee_margin_query='not found in inspected PythonGO API; counter-confirmed configuration required',
                account_risk_unit='ratio: installed AccountData.risk returns margin/dynamic_rights',
                order_status='source inspection only; no native SDK loaded and no account connection')


def install(platform_root,investor='',mode='observe'):
    platform=Path(platform_root).resolve()
    if 'Simulation' not in platform.name or not (platform/'pyStrategy/pythongo/base.py').is_file():
        raise ValueError('Explicit installed simulation platform required')
    report=inspect_sdk(platform)
    if report['missing']:raise ValueError(str(report['missing']))
    root=platform/'pyStrategy/rbreaker_adapter_20261026'
    target=platform/'pyStrategy/self_strategy/RBreakerSimulation.py'
    boot=target.with_suffix('.runtime.json')
    if root.exists() or target.exists() or boot.exists():raise ValueError('Deployment already exists; refusing to overwrite')
    bundle(root,investor,mode)
    write_new(target,(root/'RBreakerSimulation.py').read_bytes())
    write_new(boot,(root/'RBreakerSimulation.runtime.json').read_bytes())
    return dict(entry=str(target),configuration=str(root/'configuration/simulation.json'),
                confirmed_rules=str(root/'configuration/counter_confirmed_rules.json'),sdk=report,
                started=False,orders_sent=0)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path)
    p.add_argument('--inspect-platform',type=Path)
    p.add_argument('--install-platform',type=Path)
    p.add_argument('--investor',default='')
    p.add_argument('--mode',choices=('observe','simulation'),default='observe')
    a=p.parse_args()
    if a.inspect_platform:print(json.dumps(inspect_sdk(a.inspect_platform),ensure_ascii=False,indent=2))
    if a.output:print(bundle(a.output,a.investor,a.mode))
    if a.install_platform:print(json.dumps(install(a.install_platform,a.investor,a.mode),ensure_ascii=False,indent=2))
    if not any((a.inspect_platform,a.output,a.install_platform)):p.error('Choose inspection, bundle, or installation')
