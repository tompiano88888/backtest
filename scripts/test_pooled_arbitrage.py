import argparse
import json
from pathlib import Path
from backtest.pooled_rolling_arbitrage import run
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='outputs/pooled_rolling_arbitrage');parser.add_argument('--initial',default='10000000');parser.add_argument('--margin',default='.15')
    parser.add_argument('--funding-exit',action='store_true')
    parser.add_argument('--etf',action='store_true')
    parser.add_argument('--cancel-new-openings',action='store_true')
    parser.add_argument('--dividends',action='store_true')
    parser.add_argument('--products',default='IF,IH,IC,IM')
    parser.add_argument('--nav-adjustment',action='store_true')
    parser.add_argument('--dividend-season-adjustment',action='store_true')
    parser.add_argument('--total-return-version')
    parser.add_argument('--dividend-season-months',help='Comma-separated forecast months, e.g. 5,6,7,8; omission preserves the prior full-year model')
    parser.add_argument('--etf-scoring',action='store_true')
    parser.add_argument('--min-expected-annual-return',default=None,help='Inclusive annualized entry floor under minimum-return-basis, decimal (0.015 = 1.5%%)')
    parser.add_argument('--minimum-return-basis',choices=['exit_target','zero_convergence'],default='exit_target')
    parser.add_argument('--entry-floors',help='JSON file mapping every active product to its entry floor, decimal')
    parser.add_argument('--rolling-source',help='Reuse exact dated rolling choices and registered trials from a matching fixed snapshot')
    args=parser.parse_args()
    floors=json.loads(Path(args.entry_floors).read_text(encoding='utf-8')) if args.entry_floors else None
    run(args.output,args.initial,args.margin,args.funding_exit,args.etf,args.cancel_new_openings,args.dividends,products=args.products.split(','),nav_adjustment=args.nav_adjustment,etf_scoring=args.etf_scoring,min_expected_annual_return=args.min_expected_annual_return,minimum_return_basis=args.minimum_return_basis,min_expected_annual_return_by_product=floors,rolling_source=args.rolling_source,dividend_season_adjustment=args.dividend_season_adjustment,total_return_version=args.total_return_version,dividend_season_months=[int(m) for m in args.dividend_season_months.split(',')] if args.dividend_season_months else None)
