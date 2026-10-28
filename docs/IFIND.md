# iFinD SDK 接入与联调

本文所列历史下载规模、版本与本机验证仅为适配器开发记录；数据、SDK、凭据和运行输出不随Git交付。新使用者必须准备自己的输入和固定版本，不能假定这些路径已存在。

更新日期：2026-10-08。当前接入方式为用户选择的 **Windows Python SDK 账号登录**。
使用本机官方 SDK，不将 iFinDPy.py、DLL、许可证或凭据复制进仓库。

## 1. 安装与诊断

下载并安装官方 Windows 数据接口包，然后在项目根目录运行：

```powershell
powershell -NoProfile -File scripts\install-ifind.ps1 -SDKRoot 'D:\THSDataInterface_Windows'
.venv\Scripts\python.exe -m backtest ifind-check
```

SDKRoot 是安装位置示例；其他机器传入自己的路径。脚本按项目 Python 位数选择 x64/x86，调用官方安装程序将路径写入 **项目 .venv 的 iFinDPy.pth**，不修改系统 Python。
不能只临时修改 sys.path：当前官方包装器还会通过 purelib/iFinDPy.pth 定位 ShellExport.dll。

本次在当前机器已验证 SDK 导入与 DLL 加载成功。`ifind-check` 仅输出 SDK 是否可用、函数名及环境变量是否存在，不显示密码，不调用行情接口。
查询使用 SDK 的 `format:json`，不依赖 pandas，也不经过 SDK 的 DataFrame 浮点转换。

## 2. 在本机配置凭据

在准备运行下载的 PowerShell 中输入，密码不会进入命令历史：

```powershell
$env:IFIND_USERNAME = Read-Host 'iFinD account'
$ifindPassword = Read-Host 'iFinD password' -AsSecureString
$env:IFIND_PASSWORD = [System.Net.NetworkCredential]::new('', $ifindPassword).Password
.venv\Scripts\python.exe -m backtest ifind-check --login
```

环境变量只对该 PowerShell 和其子进程可见。若在另一个终端设置，已经运行的 Codex／定时任务进程不会自动获得变量；从已配置环境启动任务，或重新启动相关进程。
不要将密码发到聊天、写进任务 JSON、提交 .env。项目不会自动读取 .env；`.env*`、`*.local.json` 已加入忽略规则。

SDK 登录成功码为 0；-201 表示重复登录，适配器复用已有会话且不主动登出它。自己创建的会话在退出上下文时调用 THS_iFinDLogout；任务失败时也清理，不让登出错误覆盖原始错误。
官方接口账号可能有并发登录互斥，请使用适当的数据接口账号。

## 3. 先查询小样本

```powershell
.venv\Scripts\python.exe -m backtest ifind-query examples\ifind.basic.json --output outputs\ifind-basic.json
.venv\Scripts\python.exe -m backtest ifind-query examples\ifind.calendar.json --output outputs\ifind-calendar.json
.venv\Scripts\python.exe -m backtest ifind-query examples\ifind.template.json --output outputs\ifind-minute-sample.json
```

`ifind-query` 只使用配置中的 request，结果为 `{ "records": [...] }`，不发布行情版本、不自动将元数据写为历史事实。
行情记录统一展开为 `thscode`、`time` 和供应商指标列。基础数据和数据池可能没有 time，返回原有字段；交易日历返回 time 列。
不会把某标的时间轴套给其他标的；列长度不一致、返回错误或无法解析的结构会失败。

已接入的官方函数：

| function | request 字段 |
|---|---|
| THS_HQ / THS_HF | codes, indicators, parameters, start, end |
| THS_BD | codes, indicators, parameters |
| THS_DS | codes, indicators, parameters, global_parameters, start, end |
| THS_DP | name, parameters, options |
| THS_DateQuery | exchange, parameters, start, end |

省略 function 时，frequency=1d 选 THS_HQ，frequency=1m 选 THS_HF。不允许从配置执行任意 SDK 函数。
例子中的基础资料指标和行情列来自官方手册／示例；各资产结算价、持仓量、合约映射与历史成分的指标仍须通过 SuperCommand 和有权限的样本确认，不能猜测。

## 4. 行情标准化与落盘

```powershell
.venv\Scripts\python.exe -m backtest update examples\ifind.template.json --validate-only
.venv\Scripts\python.exe -m backtest update examples\ifind.daily.json --validate-only
```

这两条命令离线检查请求、代码映射及交易时段，不登录、不消耗额度。
样本核验之后再执行实际下载：

```powershell
.venv\Scripts\python.exe -m backtest update examples\ifind.template.json --root data
.venv\Scripts\python.exe -m backtest update examples\ifind.daily.json --root data
```

示例只选一个股票、一天／五分钟，便于联调；并不是全市场生产任务。特别是示例 `timestamp_at=end`、volume_multiplier=1 和 turnover_multiplier=1 是**明确可改的样本配置，尚未通过本账号真实响应核验**，不能据此认定其他资产或频率单位相同。

配置包括：

- `request`：官方函数参数。日期范围是供应商的闭区间；日线为 YYYY-MM-DD，分钟为 YYYY-MM-DD HH:MM:SS，按北京时间。
- `batch_size`、`chunk_days`：代码分批和时间分段，默认 20 个代码、5 天；按账号配额调小。边界不重叠。
- `normalization.instruments`：供应商代码 → 内部 id、asset、volume_multiplier；映射成交额时还须提供 turnover_multiplier。
- `normalization.columns`：标准字段 → 已验证的供应商列名，必须有 OHLCV。
- `normalization.timestamp_at`：分钟时间戳代表 start 还是 end，必须显式配置。
- `calendar_file`：相对任务配置文件的路径；JSONL 中每行包含 instrument、trading_date、带时区 start/end。午休分段记录，跨午夜和周末夜盘按实际交易日映射。

THS_DateQuery 提供交易日期，不提供完整品种夜盘／午休时段，不能直接代替 calendar_file。当前接口接受完整时段事实；自动从 DuckDB 的历史规则生成时段仍是后续 Provider 工作。

分钟 Bar 必须完整包含在某个交易时段内；夜盘不能用自然日期或“加一天”推断。日线起止由该交易日最早／最晚时段决定，可跨多个自然日。
期货、期权日线必须显式映射 settlement_price 且值非空，沿用一个结算价字段；不会用 close 补齐。分钟任务不接受日结算价列，避免把日终数据提前灌入盘中行情。

生产行情必须不复权、不前值填充：日线显式 `Interval:D,CPS:1,Fill:Omit`，分钟显式 `Interval:1,CPS:no,Fill:Original`。需要复权的数据由研究接口另行处理。

## 5. 发布与失败行为

每个批次请求及标准化全部成功后，一次性发布 Parquet/DuckDB 新版本；发布清单与成功日志在同一数据库事务中提交。下载／校验失败不推进有效版本。
相同 Bar 完全重复时合并；同批次相同主键内容冲突则失败；旧数据修订生成新版本，旧快照保持不变。
空批次、缺少请求标的、越界时间、坏 OHLCV、缺必需字段、成交量不是整数等会报错，不静默继续。
空批次可能是休市、停牌或无权限，需核实日历或缩小任务；当前不会自动把空响应视为成功。

网络超时／连接错误限次重试；供应商错误默认不重试，只有任务中显式指定的 `retry_codes` 才重试。代码值必须依据官方错误说明选择，避免反复请求错误凭据或无权限数据。
日志只记录失败阶段或数值错误码，不输出供应商 errmsg、账号密码或完整请求响应。

当前仍有边界：一次任务的标准化数据在内存中汇总；尚未实现全市场流式下载、动态配额探测、自动时间水位推进或逐分钟完整性审计。根据显式区间增量／补数和重叠修订已可重复执行；某标的部分日期或分钟缺失需要后续覆盖率报告进一步判断，不能把有返回值等同于全量完整。

## 6. 元数据与兼容性

基础资料、日期序列、数据池和日历通过 ifind-query 可查询并导出。将这些结果转成 Instrument、历史成分或主连事实时，必须明确历史有效时间及 known_at，再用已有 metadata 导入接口写 DuckDB；本次没有把当前快照冒充历史元数据。
旧 CLI update 的 column_mapping 模板依赖上游已经生成标准时间戳，现改为 normalization + calendar_file。旧 Python IFinDSource 导入路径 `backtest.pipeline` 仍保留，但 fetch 现在返回 JSON 展开后的供应商原始记录，而非依赖 pandas 的记录。旧通用 update/configured_normalizer 函数仍保留用于既有调用。

## 登录返回 -1 的排查

本机官方 SDK 的 `THS_GetErrorInfo(-1)` 只返回通用“操作失败”，不足以判断是密码、网络还是账号状态。不要把 -1 自动解释成密码错误，也不要循环重试登录。

先运行官方 `D:\THSDataInterface_Windows\SuperCommand.exe`，使用同一**数据接口账号**验证登录。若也失败，在官方工具中核查账号、接口权限及网络设置；若成功，在执行 CLI 的同一个 PowerShell 窗口重新设置 IFIND_USERNAME／IFIND_PASSWORD，再执行 `ifind-check --login`。其他窗口或用户环境变量的新设置不一定被当前进程继承；密码保持原样，不自动去掉空格。只反馈成功或错误码，不发送账号、密码或含凭据的截图。

## 7. 验证状态与依据

本机 SDK 加载、函数检查及离线配置校验已通过。自动测试使用模拟 SDK，覆盖接口签名、Decimal 解析、重复登录、登出、重试、凭据不泄露、夜盘、结算价、分批失败及不可变快照。2026-10-09 真实账号登录、股票五分钟样本及股指期货历史日线查询通过；IF/IC/IM/IH 日线含 settlement/preSettlement 已发布，详见 [股指期货历史日线](INDEX_FUTURES.md)。其他资产权限和指标仍待验证，不能据此认定全资产已接通。

官方参考：

- [iFinD 产品手册](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/manual.html)
- [官方 Python 应用示例](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/example.html)

另外已读取本机官方 iFinDPy.py 核对 format:json、THSData、DateQuery 和 DLL 加载方式；没有复制官方 SDK 实现到本仓库。
