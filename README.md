# 灰度升档演练（Flask + SQLite 单服务）

模拟灰度发布场景：候选版按分钟上报请求数/失败数，升档时按整段窗口合计核算错误率；
迟到的监控补报/更正可能让当前档依据时段超限或变成零请求，系统据此回退，
回退后新请求只走稳定版，直到重新升档成功。

## 架构（单服务）

```
浏览器页面
   │  HTTP（/ramp、/correct、/trial）
   ▼
Flask 应用（app/web.py）
   ├── 业务逻辑 app/services.py（升档核算 / 补报更正 / 回退）
   ├── SQLite（app/db.py）
   │     ├── minute_metrics   UTC 分钟明细（主键 minute，支持全年 527040 分钟）
   │     ├── daily_summary    按“天”的汇总组（一天一组，全年最多 366 组）
   │     ├── tier_state       当前档位（单行；active=0 即原档/回退态）
   │     └── action_log       升档/更正动作与核对计数
   └── 网关 app/gateway.py
         ├── 本地应答服务 stable   （127.0.0.1:18081，版本 stable-v1）
         └── 本地应答服务 candidate（127.0.0.1:18082，版本 candidate-v2）
```

两个应答服务是同进程内独立端口的真实 HTTP 服务；“试请求”由网关按已提交的
分流比例（基点整数，1bp=0.01%）转发，页面显示实际应答的版本。

## 关键规则与实现位置

| 需求 | 实现 |
| --- | --- |
| UTC 分钟补报/更正候选版请求数、失败数 | `services.submit_correction`，分钟表 upsert |
| 升档填分流比例、错误率上限，同年起止分钟（含两端） | `services.submit_ramp` + `timeutil` 校验 |
| 按整段请求/失败合计核算 | `services.window_totals`（只做 SUM/COUNT 聚合） |
| 漏报 / 总请求为零 / 超限 → 维持原档并提示原因 | `submit_ramp` 三种 reason，写 `action_log` |
| 一次核对最多 64 组汇总，未命中也计，不查分钟明细 | `submit_correction` 先按天查 `daily_summary`，超 64 组整体拒绝 |
| 更正只读所报分钟旧明细 | 仅对提交的分钟键执行 SELECT + upsert |
| 更正使当前档时段零请求/超限 → 回退，只走稳定版直到重新升档 | `submit_correction` 内档位重估 + `gateway.choose_target` |
| 分钟、汇总、档位一起生效；写入失败沿用最后提交状态 | 单事务 `BEGIN IMMEDIATE`，异常回滚；WAL 持久化，重启读库恢复 |
| 支持全年分钟数据 | 分钟键 TEXT 主键 + day 索引；闰年 527040 分钟，窗口跨度按分钟计算 |
| 页面显示核对查过/更正改过的汇总组数 | 最近更正计数 + 动作日志“核对/改动/未命中”列 |

错误率比较使用整数交叉相乘（`failures*10000 > limit_bp*requests`），
上限边界是“超过才超限”，恰好等于上限不算超限。

## 本地运行

需要 Python 3.11+，依赖见 `requirements.txt`：

```bash
pip install -r requirements.txt
python wsgi.py            # http://localhost:8000
# 或 gunicorn（容器内使用）
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 wsgi:app
```

环境变量：`GRAY_DB_PATH`（默认 ./data/gray.db）、`GRAY_STABLE_PORT`、
`GRAY_CANDIDATE_PORT`、`GRAY_SECRET_KEY`。

## Docker Compose

```bash
docker compose up --build
# 打开 http://localhost:8000 ，SQLite 数据持久化在 gray-data 卷的 /data/gray.db
```

> 单容器内同时提供 Web、网关与两个本地应答服务；gunicorn 固定 1 个 worker，
> 使内存中的两个应答线程与 SQLite 状态保持一致。

## 测试与演练

```bash
python -m unittest discover -s tests -v   # 20 个用例
python demo.py                            # 升档→迟到更正→回退 的脚本化演练
```

测试覆盖：升档成功与三种拒绝原因、上限边界、跨年/反向窗口、全年分钟跨度、
补报建组、更正超限/零请求回退、回退后路由只走稳定版、重新升档恢复、
64 组上限（65 拒绝不写入、64 接受、未命中计数）、事务回滚、重启持久化、
HTTP 端到端流程与真实应答服务调用。
