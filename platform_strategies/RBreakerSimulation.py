"""PythonGO-loadable simulation entry point; strategy class matches filename."""
import json
from pathlib import Path
import sys
from datetime import datetime

# Installed bootstrap points to the workspace. Never modifies backtest packages.
_bootstrap=Path(__file__).with_suffix('.runtime.json')
if not _bootstrap.is_file():
    raise RuntimeError('Run scripts.package_infinitrader_adapter to create the deployment bundle')
_settings=json.loads(_bootstrap.read_text(encoding='utf-8'))
sys.path.insert(0,_settings['project_root'])

from pythongo.base import BaseParams, BaseState, BaseStrategy, Field
from pythongo.core import MarketCenter
from pythongo.utils import Scheduler
from pythongo import infini
from infinitrader_adapter.controller import Controller
from infinitrader_adapter.gateway import TZ


class Params(BaseParams):
    config_file: str = Field(default=_settings['config_file'],title='模拟策略配置文件')


class State(BaseState):
    status: str = Field(default='未启动',title='运行状态')
    counter_risk: float = Field(default=0,title='柜台风险度(比例)')
    actual_commission: float = Field(default=0,title='柜台当日手续费')


class RBreakerSimulation(BaseStrategy):
    """当前R-Breaker节点，双重资金准入，避免平今对锁；仅模拟盘。"""
    def __init__(self):
        super().__init__();self.params_map=Params();self.state_map=State()
        self.controller=None;self.scheduler=None;self.failure=None

    def safely(self,function,*args,allow_after_failure=False):
        if self.failure and not allow_after_failure:return
        try:
            result=function(*args)
            if self.controller and self.controller.runtime.account:
                a=self.controller.runtime.account
                self.state_map.counter_risk=float(a.risk);self.state_map.actual_commission=float(a.commission)
            self.update_status_bar()
            return result
        except Exception as exc:
            self.failure=f'{type(exc).__name__}: {exc}'
            self.state_map.status=self.failure;self.trading=False
            if self.controller:
                self.controller.runtime.halt(self.failure)
                self.controller.log('halted',self.failure)
            else:self.output('启动失败',self.failure)
            self.update_status_bar()

    def on_start(self):
        if self.controller is not None:
            if self.controller.runtime.live_orders():
                self.output('仍有未完成订单，请先在模拟客户端核对');return
            self.controller.close();self.controller=None
        self.failure=None
        def start():
            backend=infini.INFINIGO
            if getattr(backend,'__module__','').startswith('pythongo.backtesting'):
                raise RuntimeError('PythonGO real client backend is not loaded; fake backend forbidden')
            self.controller=Controller(self,self.params_map.config_file,MarketCenter())
            # Controller subscribes its explicit universe. Base on_start would
            # otherwise subscribe inherited empty fields and alter unrelated jobs.
            self.trading=True
            self.scheduler=Scheduler('RBreakerSimulation-'+str(self.strategy_id))
            self.scheduler.add_job(self.on_clock,'interval',id='poll',seconds=1,max_instances=1,coalesce=True)
            self.scheduler.start()
            self.state_map.status='等待交易日行情与柜台参数'
        self.safely(start)

    def on_clock(self):
        if self.controller:self.safely(self.controller.run_timer,datetime.now(TZ))

    def on_tick(self,tick):
        if self.controller:self.safely(self.controller.tick,tick)

    def on_contract_status(self,status):
        if self.controller:self.controller.gateway.status[status.instrument_id]=status.status

    def on_order(self,order):
        if self.controller:self.safely(self.controller.order,order,allow_after_failure=True)

    def on_cancel(self,order):
        if self.controller:self.safely(self.controller.order,order,True,allow_after_failure=True)

    def on_trade(self,trade,log=False):
        if self.controller:self.safely(self.controller.trade,trade,allow_after_failure=True)

    def on_error(self,error):
        self.failure='柜台或策略错误，请核对活动订单后恢复'
        self.trading=False;self.state_map.status=self.failure
        if self.controller:
            self.controller.runtime.halt(self.failure)
            self.controller.log('counter_error',str(error))
        self.output(self.failure);self.update_status_bar()

    def on_stop(self):
        if self.scheduler:self.scheduler.stop()
        if self.controller:
            for order in self.controller.runtime.live_orders():
                if order['broker_id'] is not None and not order['cancel_requested']:
                    self.controller.runtime.record(dict(kind='cancel_request',client_id=order['client_id']))
                    self.safely(self.controller.gateway.cancel,order['broker_id'],allow_after_failure=True)
        self.trading=False
        # Keep journal and ownership lock alive: late fills still MUST be recorded.
        self.state_map.status='已停止，保留持仓；等待真实撤单与成交回报'
        self.save_instance_file()
        if self.controller:
            for code in self.controller.subscriptions:
                self.unsub_market_data(exchange='CFFEX',instrument_id=code)
        self.update_status_bar()
