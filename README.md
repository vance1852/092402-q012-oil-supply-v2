# 油气供应韧性与现场准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录原油基准报价、油田与终端设施、输送线路、库存批次、日提名和供应情景，并保留油田巡检机器人统计准入流程。系统面向价格连续波动、关键输油线路恢复、库存调拨和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 原油基准报价按交易日和来源修订登记，历史版本不会被覆盖；
- 油田、储罐、终端与炼厂设施建档，线路保存日能力、在途时间和损耗规则；
- 线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 库存批次保留油品、牌号、数量、单位成本和接收时间，可计算加权库存成本；
- 托运提名支持载荷级幂等、优先级分配、库存扣减和在途交接；
- 供应情景保存价格变化、线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 区域×油品×营业日的需求预测按草稿合并、审批冻结和滚动替代管理，批准人和生效时间随版本保留；
- 截单任务按可注入时钟解析当时有效的预测版本，重复执行得到同一选择，分配运行引用版本时永久保留输入摘要；
- 实绩到齐后结账，把实绩与预测的数量偏差闭合分解为价格变化、供应受限和未解释部分，迟到实绩只生成后继分析；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

现场准入子域位于 `robot_trials` 包，负责油田巡检机器人的设备构建登记、不可变试验协议、观测分片导入、异常观测复核、统计任务租约、准入决定和审计报告。该子域不连接机器人硬件，只处理已经结构化的试验记录。

## 目录

- `src/oil_supply/`：报价、设施、线路、库存、提名、供应情景、预测版本、HTTP API、命令行与离线验收；
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

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖报价、设施、线路、停运事件、库存批次、提名、能力分配、发运、供应情景、预测版本、截单、实绩、偏差分析和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

## 预测版本与偏差分解

每个区域、油品和营业日的需求预测都按版本管理，任何一版被引用后都可以回溯到具体数字，而无需翻查旧日志：

- `POST /forecasts` 提交草稿；同一区域、油品、营业日的未批准草稿自动合并为一版，合并历史进入审计链；
- `POST /forecasts/{id}/approve` 由 `risk` 角色批准，版本内容随即冻结，记录批准人、批准时间和生效时间，同键旧版自动滚动替代为 `superseded`；
- `POST /forecast-cutoffs` 由调度在截单时运行，按服务时钟解析当时有效版本并永久保存版本摘要，同一营业日重复执行返回同一选择；
- `POST /routes/{id}/allocate` 可携带 `forecast_version_id`，分配运行一旦引用某版，`GET /allocations/{id}/forecast` 永远返回引用时点的输入摘要；
- `POST /forecast-actuals` 登记实绩（幂等）；`POST /forecast-closes` 在实绩到齐后结账，把 实绩-预测 的总偏差按数量闭合分解为价格变化、供应受限和未解释部分；
- 结账后到达的迟到实绩不会改动原分析，只生成修订号递增的后继分析，`GET /forecast-analyses` 可查看完整链条；
- `GET /forecasts` 和 `GET /forecasts/compare?a=&b=` 供销售和调度比较任意两版，并直接看到批准人和生效时间。

同一流程也可通过命令行完成（输出单行 JSON）：

```bash
PYTHONPATH=src python3 -m oil_supply.cli --database oil_supply.sqlite3 forecast-submit --actor sales1 --region east --product gasoline-92 --business-day 2026-09-25 --quantity 15000 --price 102 --elasticity -0.4
PYTHONPATH=src python3 -m oil_supply.cli --database oil_supply.sqlite3 forecast-approve --actor risk1 --version-id 1
PYTHONPATH=src python3 -m oil_supply.cli --database oil_supply.sqlite3 forecast-cutoff --actor disp1 --region east --product gasoline-92 --business-day 2026-09-25
PYTHONPATH=src python3 -m oil_supply.cli --database oil_supply.sqlite3 forecast-compare --actor sales1 --a 1 --b 2
```

角色分工：销售侧以 `planner` 提交和查看预测，`risk` 批准版本，`dispatcher` 执行截单、实绩登记和结账，`auditor` 只读。
