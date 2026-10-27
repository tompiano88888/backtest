from decimal import Decimal as D
from scripts.report_rbreaker_trade_types import stats


def trade(pnl,closed=True,stopped=False):
    return dict(net_pnl=str(pnl),closed=str(closed),stopped=str(stopped),budget='100',holding_sessions='1',fees='0')


def test_open_trade_mtm_does_not_inflate_closed_win_rate_or_r():
    result=stats([trade(100),trade(-50,stopped=True),trade(1000,closed=False)])
    assert result['net_pnl']==1050 and result['closed_net_pnl']==50
    assert result['win_rate']==.5 and result['average_r']==D('.25')
    assert result['stop_rate_observed']==1/3
    assert result['stop_rate_closed']==.5


def test_stop_loss_share_uses_gross_losses_even_when_net_pnl_is_zero():
    result=stats([trade(100),trade(-40,stopped=True),trade(-60),trade(0)])
    assert result['net_pnl']==0 and result['profit_factor']==1
    assert result['stopped_share_of_losses']==D('.4')
    assert result['stop_probability_given_loss']==.5
