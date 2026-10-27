import argparse
from datetime import date
from backtest.arbitrage_node_engine import run

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Current seasonal arbitrage node through unified Engine daily matching')
    parser.add_argument('--output',default='outputs/arbitrage_node_engine_20261022')
    parser.add_argument('--start',type=date.fromisoformat,default=date(2020,1,1))
    parser.add_argument('--end',type=date.fromisoformat,default=date(2026,10,8))
    args = parser.parse_args()
    if args.start != date(2020,1,1) or args.end < args.start:
        parser.error('Node accounting starts 2020-01-01; end must follow start')
    run(args.output,args.start,args.end)
