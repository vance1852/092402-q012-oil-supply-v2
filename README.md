# 油气供应韧性与现场准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录原油基准报价、油田与终端设施、输送线路、库存批次、日提名和供应情景，并保留油田巡检机器人统计准入流程。系统面向价格连续波动、关键输油线路恢复、库存调拨和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 原油基准报价按交易日和来源修订登记，历史版本不会被覆盖；
- 油田、储罐、终端与炼厂设施建档，线路保存日能力、在途时间和损耗规则；
- 线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 库存批次保留油品、牌号、数量、单位成本和接收时间，可计算加权库存成本；
- 托运提名支持载荷级幂等、优先级分配、库存扣减和在途交接；
- 供应情景保存价格变化、线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 区域 × 油品 × 营业日的需求预测按草稿合并、审批冻结、滚动替代治理，每版都可追到批准人和生效时间；
- 截单任务按可注入时钟解析当时有效版本，同键重复执行得到同一选择，引用版本的输入摘要随运行永久保留；
- 实际出库到齐后，预测偏差在数量闭合下分解为价格变化、供应受限和未解释三部分，迟到实绩只生成后继分析；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

现场准入子域位于 `robot_trials` 包，负责油田巡检机器人的设备构建登记、不可变试验协议、观测分片导入、异常观测复核、统计任务租约、准入决定和审计报告。该子域不连接机器人硬件，只处理已经结构化的试验记录。

## 目录

- `src/oil_supply/`：报价、设施、线路、库存、提名、供应情景、预测版本治理、HTTP API、命令行与离线验收；
- `src/robot_trials/`：油田巡检机器人试验与统计准入；
- `fixtures/`：现场准入演示协议和结构化观测；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

在依赖已经准备好的容器中安装：

```bash
python3 -m pip install --no-index --no-deps .
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m oil_supply.acceptance --workspace .
```

该命令会在内存数据库中登记六个交易日的布伦特报价，创建油田、终端和输送线路，完成库存入账、提名分配、发运及供应情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

现场准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m robot_trials.acceptance --workspace .
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m oil_supply.api --database oil_supply.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖报价、设施、线路、停运事件、库存批次、提名、能力分配、发运、供应情景、预测版本（`/forecasts/...`）和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

## 命令行

预测版本治理同时提供命令行入口，输出单行 JSON，便于脚本化和交接核对：

```bash
# 销售提交草稿行（同键自动合并进当前草稿版本）
PYTHONPATH=src python3 -m oil_supply.cli --database oil_supply.sqlite3 --actor sales \
  forecast-draft --data '{"region_id":"east","product":"gasoline-92","business_date":"2026-09-25","lines":[{"line_key":"retail","quantity_barrels":"1200","expected_price_usd":"100","price_elasticity":"-0.5"}]}'

# 风控审批冻结，旧版随即被滚动替代
PYTHONPATH=src python3 -m oil_supply.cli --database oil_supply.sqlite3 --actor risk \
  forecast-approve --version-id 1 --expected-revision 1

# 调度执行截单，--at 注入解析时钟；同键重复执行得到同一选择
PYTHONPATH=src python3 -m oil_supply.cli --database oil_supply.sqlite3 --actor dispatch \
  cutoff-run --region east --product gasoline-92 --business-date 2026-09-25 --supply-cap 5000

# 销售和调度比较任意两版，并追到批准人和生效时间
PYTHONPATH=src python3 -m oil_supply.cli --database oil_supply.sqlite3 --actor sales \
  forecast-compare --a 1 --b 2
```

其余子命令包括 `forecast-show`、`forecast-timeline`、`cutoff-show`、`actual-record`、`variance-analyze` 和 `variance-list`，均可通过 `--help` 查看。
