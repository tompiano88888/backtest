# 本机工作流恢复

2026-10-27，项目位于 `E:\OneDrive\codex_project\backtest`。用户要求优先跑通回测；iFinD SDK、无限易客户端及账户连接留待后续。

## 已恢复与验证

- 备份的 `.venv` 能启动 Python 3.12.14，但多个包只有残留目录。已按 `requirements.lock` 原版本重新安装依赖，重新执行本目录可编辑安装，恢复 pip 与 `cn-backtest` 入口；`pip check` 通过。
- VS Code 默认解释器改为项目 `.venv`，启用 pytest，移除原 conda 默认设置。
- 完整测试 **406 passed**（包括新增两项迁移回归），编译通过；demo 为 PnL 52、4条警示。账户事件合成场景也成功运行。
- 套利指定冻结滚动来源时，直接读取该来源 `config.json` 的 `source_snapshots`。原入口无条件依赖已删除的 `outputs/front_contract_cash_arbitrage/config.json`，导致复现失败；现在冻结回放不再依赖该历史目录。不恢复被删除的旧方案，不变更策略或快照版本。未指定滚动来源的历史入口仍需要其原始来源配置，不能认为所有旧研究命令均已恢复。
- 当前套利全区间复现：PnL **3602477.184315470元**，1524份组合全部平仓，2473个自然日独立对账、零预算缺口；与当前节点 **21份CSV逐字节一致**。
- R-Breaker按当前历史手续费、ATR20/1%与避免平今对锁配置，试跑2020-01-01至2020-01-10：实际7个交易日、21笔成交，PnL **-164046.77904元**；独立账务、费用、路由和资金核验通过，平今计费0手、3409条资金观察零超限。本次未重新运行其2020—2026全历史。
- 两份R-Breaker封存分别72、196个文件，SHA256全部通过；未改写数据快照、旧结果或封存，未提交Git。工作区原有大量未提交/未跟踪文件均保留。
- 六个数据数据库只读打开成功，已发布清单引用的1937个行情文件全部通过SHA256检查（含1376个股指期货分钟分区）。检查不包含未发布的ETF分钟暂存数据，也不等于重新验证全部业务字段或历史PIT。

本次证据：

- [迁移核验目录](../outputs/workflow_restore_20261027/)，含封存校验、套利CSV对照与R-Breaker试跑。
- [套利重跑结果](../outputs/workflow_20261027_202708_6888991a/arbitrage/)，含独立 `verification.json`。
- [基础检查演示](../outputs/workflow_20261027_202330_44251277/demo.json)。

## 日常入口

在项目根目录的PowerShell运行；无需激活环境、无需iFinD登录：

```powershell
# 完整测试、编译与demo
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/workflow.ps1 check
# 两个合成示例
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/workflow.ps1 demo
# 当前套利全历史回放（显式5,6,7,8；复用当前冻结滚动参数）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/workflow.ps1 arbitrage
# 当前R-Breaker全历史回放及独立核验（耗时较长）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/workflow.ps1 rbreaker
```

每次生成 `outputs/workflow_时间_随机串`，不会覆盖原节点。绘图缓存也放入本次目录，避免沙箱写用户目录权限问题。PowerShell函数包装中的月份字符串必须加引号，脚本已处理。

若以后再次迁移，优先重新创建虚拟环境，不把复制来的 `.venv` 或 `.uv-cache` 当作完整依赖备份。可用Python 3.12+执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
```

上面的 `python` 必须是新机器实际安装的Python；现有正常环境无需重建。OneDrive按需文件首次读取需要下载到本地；目录存在不等于内容已离线可用。需要完全离线工作时，应在OneDrive中将项目数据设置为始终保留在此设备，并确认同步完成。

## 尚未恢复与既有边界

iFinD原 `.pth` 指向旧机器 `D:\THSDataInterface_Windows\bin\x64`，本机未安装SDK；按用户要求暂不处理，不登录、不下载新行情、不启动模拟交易。无限易适配源码已在备份中，404项原测试包含相关单元测试，但不代表本机柜台验证。

ETF分钟仍仅109/664分段，未发布完整快照。套利此次验证是原日频研究复现，不能当作统一分钟框架验收；R-Breaker此次小区间通过不替代全历史验收。策略、15%研究保证金假设与其余PIT/非Tick限制保持原文档定义。
