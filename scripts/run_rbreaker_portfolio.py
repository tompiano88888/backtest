import argparse
import json
from pathlib import Path
from datetime import date
from backtest.models import D
from backtest.rbreaker_portfolio import run

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='R-Breaker 1000万元统一资金池，策略保证金预算90%/50%')
    parser.add_argument('--output',default='outputs/rbreaker_portfolio')
    parser.add_argument('--start',type=date.fromisoformat,default=date(2020,1,1))
    parser.add_argument('--end',type=date.fromisoformat,default=date(2026,10,8))
    parser.add_argument('--margin-rate',type=D,default=D('.15'))
    parser.add_argument('--stop-risk',type=D,help='例如 .01：单笔开仓权益风险预算，同时启用ATR止损')
    parser.add_argument('--atr-window',type=int,default=20)
    parser.add_argument('--atr-multiple',type=D,default=D(1))
    parser.add_argument('--include-auction',action='store_true',help='09:30集合竞价事件，竞价前指令零滑点')
    parser.add_argument('--batch-margin',type=D,help='每批以前一交易日结算权益的此比例作为保证金预算')
    parser.add_argument('--batch-stop',type=D,help='每批固定止损预算占同一权益基数的比例')
    parser.add_argument('--rolling-source',help='固定ATR基准结果目录：前三个月表现动态分配')
    parser.add_argument('--lock-today',action='store_true',help='平今以反向开仓对锁，5%%保证金批次＋ATR20止损')
    parser.add_argument('--historical-fees',type=Path,help='包含historical_fees固定版本/root/as_of的JSON配置；策略设计保持')
    parser.add_argument('--avoid-close-today',action='store_true',help='ATR/1%%节点：所有解锁/退出先查平今计费余额，以反向开仓替代不安全平仓')
    args=parser.parse_args()
    if args.margin_rate<=0 or args.start>args.end:
        parser.error('Invalid margin/date bounds')
    if args.stop_risk is not None and (not 0<args.stop_risk<1 or args.atr_window<1 or args.atr_multiple<=0):
        parser.error('Invalid stop parameters')
    if args.stop_risk is not None and args.output=='outputs/rbreaker_portfolio':
        args.output='outputs/rbreaker_portfolio_stop_1pct'
    if args.include_auction and args.output in ('outputs/rbreaker_portfolio','outputs/rbreaker_portfolio_stop_1pct'):
        args.output += '_auction'
    if not args.lock_today and ((args.batch_margin is None)!=(args.batch_stop is None) or (args.batch_margin is not None and
       (not 0<args.batch_margin<=D('.5') or not 0<args.batch_stop<1 or args.stop_risk is not None))):
        parser.error('Batch mode requires valid --batch-margin and --batch-stop, without --stop-risk')
    if args.batch_margin is not None and args.output in ('outputs/rbreaker_portfolio','outputs/rbreaker_portfolio_auction'):
        args.output='outputs/rbreaker_batch_5pct_stop_0p5pct_auction'
    if args.rolling_source and (args.stop_risk is None or args.batch_margin is not None):
        parser.error('--rolling-source requires --stop-risk without batch mode')
    if args.rolling_source and args.output in ('outputs/rbreaker_portfolio','outputs/rbreaker_portfolio_stop_1pct','outputs/rbreaker_portfolio_stop_1pct_auction'):
        args.output='outputs/rbreaker_rolling_3m_atr_auction'
    if args.lock_today:
        if args.batch_margin is None or not 0<args.batch_margin<=D('.5') or args.stop_risk is not None or args.batch_stop is not None or args.rolling_source or not args.include_auction or args.atr_window<1 or args.atr_multiple!=1:
            parser.error('--lock-today requires --batch-margin .05 --include-auction, without cash/risk/rolling options')
        if args.output in ('outputs/rbreaker_portfolio','outputs/rbreaker_portfolio_auction','outputs/rbreaker_batch_5pct_stop_0p5pct_auction'):
            args.output='outputs/rbreaker_lock_5pct_atr20_auction'
    historical_fees=None
    if args.historical_fees is not None:
        historical_fees=json.loads(args.historical_fees.read_text(encoding='utf-8'))['historical_fees']
        # Separate actual-fee replays from every historical fixed-fee result.
        if not args.output.endswith('_historical_fees'):
            args.output += '_historical_fees'
    if args.avoid_close_today:
        if historical_fees is None or args.stop_risk is None or not args.include_auction or args.batch_margin is not None or args.rolling_source or args.lock_today:
            parser.error('--avoid-close-today requires historical fees + stop-risk + include-auction, without batch/rolling/lock-today')
        if args.output=='outputs/rbreaker_portfolio_stop_1pct_auction_historical_fees':
            args.output='outputs/rbreaker_atr20_1pct_fee_safe_locks_historical_fees'
    run(args.output,args.start,args.end,args.margin_rate,args.stop_risk,args.atr_window,args.atr_multiple,args.include_auction,
        args.batch_margin,args.batch_stop,args.rolling_source,args.lock_today,historical_fees=historical_fees,avoid_close_today=args.avoid_close_today)
