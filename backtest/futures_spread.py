"""Offline calendar-spread research, independent of Engine accounting."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

PRODUCTS = ('IF', 'IH', 'IC', 'IM')


def contract_month(code):
    if not re.fullmatch(r'(IF|IH|IC|IM)\d{2}(0[1-9]|1[0-2])\.CFE', code):
        raise ValueError(f'Invalid contract: {code}')
    return date(2000 + int(code[2:4]), int(code[4:6]), 1)


def next_month(month):
    return date(month.year + (month.month == 12), month.month % 12 + 1, 1)


def read_deliveries(path):
    result = {}
    if path is None:
        return result
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            code = row['instrument']
            contract_month(code)
            day = date.fromisoformat(row['delivery'])
            if code in result or not row['source'].strip():
                raise ValueError(f'Duplicate contract or missing delivery source: {code}')
            if (day.year, day.month) != (contract_month(code).year, contract_month(code).month):
                raise ValueError(f'Delivery outside contract month: {code}')
            result[code] = day
    return result


def load_snapshot(root, version=None):
    import duckdb
    import pyarrow.parquet as pq
    root = Path(root).resolve()
    with duckdb.connect(str(root / 'metadata.duckdb'), read_only=True) as db:
        if version is None:
            item = db.execute('SELECT version, manifest FROM futures_daily_snapshots '
                              'ORDER BY created_at DESC, version DESC LIMIT 1').fetchone()
        else:
            item = db.execute('SELECT version, manifest FROM futures_daily_snapshots '
                              'WHERE version = ?', [version]).fetchone()
    if item is None:
        raise ValueError('Snapshot not found')
    rows = []
    for entry in json.loads(item[1]):
        path = (root / entry['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Manifest path outside data root')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError(f'Snapshot checksum mismatch: {path.name}')
        records = pq.read_table(path).to_pylist()
        if len(records) != entry['rows']:
            raise ValueError('Snapshot row count mismatch')
        rows.extend(records)
    return item[0], rows


def required_deliveries(rows, first_months=None):
    # Natural-month expiry controls whether the nearest pair has rolled.
    return sorted({f"{r['product']}{max(date(r['trading_date'].year, r['trading_date'].month, 1), (first_months or {}).get(r['product'], date.min)):%y%m}.CFE" for r in rows})


def snapshot_deliveries(root, daily_version, metadata_version=None):
    """Read fixed revisions as retrospective facts, not historical PIT signals."""
    import duckdb
    with duckdb.connect(str(Path(root) / 'metadata.duckdb'), read_only=True) as db:
        tables = {r[0] for r in db.execute('SHOW TABLES').fetchall()}
        if 'futures_contract_snapshots' not in tables:
            return {}, None, {}
        if metadata_version is None:
            found = db.execute('SELECT version, revisions FROM futures_contract_snapshots '
                               'WHERE daily_version=? ORDER BY created_at DESC, version DESC LIMIT 1',
                               [daily_version]).fetchone()
        else:
            found = db.execute('SELECT version, revisions FROM futures_contract_snapshots '
                               'WHERE daily_version=? AND version=?', [daily_version, metadata_version]).fetchone()
        if found is None:
            if metadata_version is not None:
                raise ValueError('Metadata snapshot does not match daily version')
            return {}, None, {}
        deliveries, first_months = {}, {}
        for code, revisions in json.loads(found[1]).items():
            record = db.execute("SELECT payload FROM metadata WHERE revision=? AND kind='instrument' AND key=?",
                                [revisions['instrument'], code]).fetchone()
            if record is None:
                raise ValueError(f'Missing metadata revision: {code}')
            fact = json.loads(record[0])
            expiry, delivery = date.fromisoformat(fact['expiry']), date.fromisoformat(fact['delivery'])
            if expiry != delivery:
                raise ValueError(f'Expiry differs from delivery; front-month trading needs explicit review: {code}')
            deliveries[code] = delivery
            month = contract_month(code)
            first_months[code[:2]] = min(first_months.get(code[:2], month), month)
        return deliveries, found[0], first_months


def pair_rows(rows, deliveries, first_months=None):
    needed = set(required_deliveries(rows, first_months)) - deliveries.keys()
    if needed:
        raise ValueError(f'Missing verified deliveries: {len(needed)} contracts')
    indexed = {}
    days = defaultdict(set)
    for row in rows:
        code, day = row['instrument'], row['trading_date']
        if code[:2] != row['product']:
            raise ValueError('Contract/product mismatch')
        contract_month(code)
        if (code, day) in indexed:
            raise ValueError('Duplicate daily key')
        indexed[code, day] = row
        days[row['product']].add(day)
    output = []
    for product in PRODUCTS:
        previous = None
        for day in sorted(days[product]):
            month = max(date(day.year, day.month, 1), (first_months or {}).get(product, date.min))
            natural = f'{product}{month:%y%m}.CFE'
            if day > deliveries[natural]:
                month = next_month(month)
            near = f'{product}{month:%y%m}.CFE'
            far = f'{product}{next_month(month):%y%m}.CFE'
            record = dict(product=product, trading_date=day, near=near, far=far,
                          roll=previous is not None and previous != near)
            previous = near
            issues = []
            for leg, code in [('near', near), ('far', far)]:
                raw = indexed.get((code, day))
                if raw is None:
                    issues.append(f'missing_{leg}_contract')
                for field in ('close', 'settlement_price'):
                    value = None if raw is None else raw.get(field)
                    if value is not None:
                        value = Decimal(str(value))
                        if not value.is_finite() or value <= 0:
                            raise ValueError(f'Invalid price: {code}/{day}/{field}')
                    elif raw is not None:
                        issues.append(f'missing_{leg}_{field}')
                    record[f'{leg}_{field}'] = value
            for field, name in [('close', 'spread'), ('settlement_price', 'settlement_spread')]:
                a, b = record[f'near_{field}'], record[f'far_{field}']
                record[name] = None if a is None or b is None else b - a
            record['relative_spread'] = (None if record['spread'] is None else
                                         record['spread'] / record['near_close'])
            record['issues'] = ';'.join(issues)
            output.append(record)
    return output


def quantile(values, fraction):
    ordered = sorted(values)
    offset = Decimal(len(ordered) - 1) * fraction
    lower = int(offset)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (offset - lower)


def statistics(rows, metric):
    pairs = [(r['trading_date'], r[metric]) for r in rows if r[metric] is not None]
    values = [v for _, v in pairs]
    count = len(values)
    result = dict(metric=metric, expected_days=len(rows), count=count,
                  missing_days=len(rows) - count,
                  start=min((d for d, _ in pairs), default=None),
                  end=max((d for d, _ in pairs), default=None))
    for key in ('mean', 'median', 'std', 'min', 'max', 'min_dates', 'max_dates',
                'p05', 'p25', 'p75', 'p95', 'positive_ratio', 'negative_ratio', 'zero_ratio'):
        result[key] = None
    if not count:
        return result
    mean = sum(values) / count
    result.update(mean=mean, median=quantile(values, Decimal('.5')),
                  std=(sum((v - mean) ** 2 for v in values) / (count - 1)).sqrt() if count > 1 else None,
                  min=min(values), max=max(values))
    for extremum in ('min', 'max'):
        result[f'{extremum}_dates'] = ';'.join(str(d) for d, v in pairs if v == result[extremum])
    for key, fraction in [('p05', '.05'), ('p25', '.25'), ('p75', '.75'), ('p95', '.95')]:
        result[key] = quantile(values, Decimal(fraction))
    for key, predicate in [('positive_ratio', lambda v: v > 0), ('negative_ratio', lambda v: v < 0),
                           ('zero_ratio', lambda v: v == 0)]:
        result[key] = Decimal(sum(predicate(v) for v in values)) / count
    return result


def write_csv(path, rows):
    if not rows:
        raise ValueError('No rows to export')
    with Path(path).open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def plot(rows, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(4, 1, figsize=(15, 13), constrained_layout=True)
    dist, hist_axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    for product, ax, hist in zip(PRODUCTS, axes, hist_axes.flat):
        group = [r for r in rows if r['product'] == product]
        for key, label in [('spread', 'Close'), ('settlement_spread', 'Settlement')]:
            ax.plot([r['trading_date'] for r in group],
                    [float(r[key]) if r[key] is not None else float('nan') for r in group],
                    label=label, linewidth=.7, alpha=.8)
        for row in group:
            if row['roll']:
                ax.axvline(row['trading_date'], color='grey', alpha=.12, linewidth=.5)
        ax.axhline(0, color='black', linewidth=.6)
        ax.set_title(f'{product}: next month minus front month (points); grey = roll')
        ax.legend()
        hist.hist([float(r['spread']) for r in group if r['spread'] is not None], bins=60)
        hist.axvline(0, color='black', linewidth=.6)
        hist.set_title(f'{product} close spread (points)')
    fig.savefig(output / 'spreads.png', dpi=150)
    dist.savefig(output / 'distributions.png', dpi=150)
    plt.close(fig)
    plt.close(dist)


def analyze(root, output, delivery_path=None, version=None, metadata_version=None):
    version, raw = load_snapshot(root, version)
    first_months = {}
    if delivery_path is None:
        deliveries, metadata_version, first_months = snapshot_deliveries(root, version, metadata_version)
    else:
        deliveries = read_deliveries(delivery_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    missing = sorted(set(required_deliveries(raw, first_months)) - deliveries.keys())
    provenance = dict(version=version, raw_rows=len(raw), metadata_version=metadata_version,
                      metadata_usage='Retrospective lifecycle facts; not historical strategy-visible PIT',
                      delivery_sha256=hashlib.sha256(Path(delivery_path).read_bytes()).hexdigest() if delivery_path else None,
                      missing_deliveries=missing,
                      calendar='Per-product observed date union; simultaneous missing dates cannot be detected')
    if missing:
        write_csv(output / 'missing_deliveries.csv',
                  [dict(instrument=c, delivery='', source='') for c in missing])
        provenance['status'] = 'blocked'
        (output / 'blocked.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
        (output / 'BLOCKED.md').write_text(
            f'# 跨期价差分析阻塞\n\n快照：{version}；原始行数：{len(raw)}。\n\n'
            f'缺少 {len(missing)} 个自然月合约的可靠交割日期，见 missing_deliveries.csv。'
            '请依据官方记录填写 delivery（YYYY-MM-DD）和 source（出处）；不能用行情结束日推断。'
            '\n\n尚未生成正式价差图或统计；所有合约同时缺行情的日期还需独立交易日历核对。\n', encoding='utf-8')
        return provenance
    rows = pair_rows(raw, deliveries, first_months)
    summaries = []
    for product in PRODUCTS:
        group = [r for r in rows if r['product'] == product]
        for year in ['all'] + sorted({str(r['trading_date'].year) for r in group}):
            subset = group if year == 'all' else [r for r in group if str(r['trading_date'].year) == year]
            for metric in ('spread', 'settlement_spread', 'relative_spread'):
                summaries.append(dict(product=product, year=year, **statistics(subset, metric)))
    plot(rows, output)
    write_csv(output / 'daily.csv', rows)
    write_csv(output / 'statistics.csv', summaries)
    provenance['status'] = 'complete'
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
    report = ['# 股指期货跨期价差', f'\n行情版本：{version}；元数据版本：{metadata_version}；交割文件 SHA256：{provenance["delivery_sha256"]}。',
              '\n合约资料用于事后描述性研究；known_at 为实际下载时间，未冒充当年的策略可知信息。上市首期使用最早挂牌月份。',
              '\n口径：下月减当月；收盘价为主，结算价对照；交割日保留旧合约，次交易日换月。',
              '\n相对价差以比例保存（0.01 = 1%）；分位数为线性插值，标准差为样本标准差。',
              '\n日期基准为各品种行情日期并集，并非独立交易所日历；全部合约同时缺失的交易日无法识别。',
              '\n|品种|有效区间|样本数|缺失日|均值（点）|中位数（点）|标准差（点）|',
              '|---|---|---:|---:|---:|---:|---:|']
    for s in summaries:
        if s['year'] == 'all' and s['metric'] == 'spread':
            report.append(f"|{s['product']}|{s['start']}–{s['end']}|{s['count']}|{s['missing_days']}|{s['mean']}|{s['median']}|{s['std']}|")
    report.extend(['\n完整极值日期、分位数、正负比例、逐年统计见 statistics.csv；缺失原因与换月标记见 daily.csv。',
                   '\n这是描述性研究，不是策略收益或可成交性检验。', '\n![价差](spreads.png)\n\n![分布](distributions.png)'])
    (output / 'REPORT.md').write_text('\n'.join(report), encoding='utf-8')
    if (output / 'BLOCKED.md').exists():
        (output / 'BLOCKED.md').write_text('# 历史阻塞已解除\n\n已接入合约元数据；最新结果见 REPORT.md 和 provenance.json。\n'
                                          'blocked.json 和 missing_deliveries.csv 为早期诊断记录，不代表本次状态。\n', encoding='utf-8')
    return provenance
