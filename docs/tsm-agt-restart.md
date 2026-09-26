# tsm-agt 一键重启指南

本地开发 / 演示环境的重启入口是仓库里的 `scripts/restart.sh`。它做三件事：**彻底清理同 workspace 的旧进程 → 重新启动 → 健康检查**。

```bash
./scripts/restart.sh            # 重启 Web UI（默认 127.0.0.1:8080）
./scripts/restart.sh status     # 看状态
./scripts/restart.sh logs       # 跟踪日志
```

---

## 1. 为什么之前“重启失败”

Web UI 入口 `python -m tsm_agt.web.app` 内部固定绑定 `127.0.0.1:8080`。旧进程没退干净时，新进程会直接报：

```
ERROR: [Errno 48] error while attempting to bind on address ('127.0.0.1', 8080): address already in use
```

常见坑：

- **重复实例**：每次重启都新起一个，旧的还占着 8080，只有最早那个能 bind 成功。
- **孤儿进程**：只 `kill` 父进程时，它用 `multiprocessing` 拉起的辅助子进程会被 launchd 收养（PPID=1），继续占端口、持有 `.agent/runtime.db`。
- **端口误判**：`ps` 看不到时以为已经停干净，实际 `8080` 仍被占用。

本仓库实测（`restart.sh status` 的真实输出）曾经同时存在：**3 个 web 实例 + 10 个孤儿 multiprocessing 进程**，只有其中一个真正持有 8080。这就是重启反复失败的根因。

结论：重启不能只 kill 一个 PID，必须“先清干净同 workspace 的全部相关进程，再启动，再验证 HTTP 200”。

---

## 2. 脚本用法

脚本只依赖 macOS 自带的 `bash / pgrep / lsof / ps / curl`，无需额外安装。

```text
scripts/restart.sh [start|stop|restart|status|logs] [选项]
```

| 选项 | 说明 |
| --- | --- |
| `--mode web\|api\|chat` | 运行模式，默认 `web` |
| `--host HOST` | 绑定地址，默认 `127.0.0.1` |
| `--port PORT` | 端口；`web` 默认 `8080`，`api` 默认 `8765` |
| `--workspace DIR` | 工作区，默认仓库根目录（`.env` 也从这里加载） |
| `--foreground` | 前台运行（不写 PID、不重定向日志；`chat` 强制前台） |
| `--grace SECONDS` | `SIGTERM` 后等待秒数，默认 10，超时后 `SIGKILL` |
| `--timeout SECONDS` | 启动健康检查超时秒数，默认 40 |
| `--no-clean-orphans` | 不清理遗留的 `multiprocessing` 孤儿进程 |
| `--dry-run` | 只打印将要执行的命令，不真正执行 |

常用命令：

```bash
# 默认：重启 Web UI
./scripts/restart.sh

# 重启到自定义端口
./scripts/restart.sh restart --mode web --port 8090

# 只停 / 只启 / 看状态 / 看日志
./scripts/restart.sh stop
./scripts/restart.sh start --mode web
./scripts/restart.sh status
./scripts/restart.sh logs

# 重启 API 服务（带 Bearer Token 的本地 HTTP/SSE）
./scripts/restart.sh restart --mode api --port 8765

# 先看看会杀哪些进程，不动手
./scripts/restart.sh restart --dry-run
```

`stop` / `restart` 的处理顺序：

1. 收集 PID：PID 文件 + 监听目标端口的进程 + 命令行匹配 `tsm_agt.web.app` / `tsm_agt.cli api serve` / `tsm_agt.cli chat` 的进程 + 本 workspace 的 `multiprocessing` 孤儿。
2. 每个 PID 递归展开整棵进程树（父 + 子一起）。
3. 只保留 cwd 属于当前 `--workspace` 的进程，避免误杀其它项目的 tsm-agt。
4. `SIGTERM` 优雅退出，`--grace` 秒后仍在的 `SIGKILL`。
5. 确认目标端口已释放，再启动新实例并轮询健康检查。

---

## 3. 三种运行模式

| 模式 | 启动命令 | 默认地址 | 健康检查 |
| --- | --- | --- | --- |
| `web` | `python -m uvicorn tsm_agt.web.app:app --host H --port P` | `127.0.0.1:8080` | `GET /` 返回 200 |
| `api` | `python -m tsm_agt.cli api serve --workspace . --host H --port P` | `127.0.0.1:8765` | TCP 端口可连接 |
| `chat` | `python -m tsm_agt.cli chat --workspace .` | 无端口 | 前台交互，不后台化 |

> 说明：`web` 模式的 `main()` 里端口写死 8080，所以脚本改用 `uvicorn tsm_agt.web.app:app --port` 启动，`--port` 才能真正生效。`api serve` 启动时会打印一次 Bearer Token，注意保存。

---

## 4. 不用脚本时的手动重启

```bash
cd /Users/tusima/Documents/学习/tsm-agt

# 1) 找出占用者和实例
lsof -nP -iTCP:8080 -sTCP:LISTEN
pgrep -fl "tsm_agt.web.app"
pgrep -fl "multiprocessing"

# 2) 停：先优雅，再强制；把 multiprocessing 孤儿一并清掉
kill -TERM <pids>
sleep 2
kill -KILL <still_alive_pids> 2>/dev/null

# 3) 确认端口已释放（应无输出）
lsof -iTCP:8080 -sTCP:LISTEN

# 4) 启动（必须在仓库根目录，Runtime 从 cwd/.env 读取模型配置）
export PYTHONPATH="$PWD/src"
.venv/bin/python -m uvicorn tsm_agt.web.app:app \
  --host 127.0.0.1 --port 8080 --log-level info
```

也可以沿用 README 推荐入口（固定 8080、前台运行）：

```bash
python scripts/run_in_project_env.py web
```

---

## 5. 注意事项

- **必须在仓库根目录运行**：Runtime 从当前工作目录的 `.env` 读取模型配置；脚本会自动 `cd` 到仓库根。
- **优雅停止**：`SIGTERM` 给 SQLite 收尾机会，脚本默认等 10 秒再 `SIGKILL`。
- **数据不丢**：会话 / 任务都在 `.agent/runtime.db`，重启不丢；正在执行的 Task 会被安全落成 `INTERRUPTED`，可后续恢复。
- **PID 与日志位置**：
  - PID：`.agent/run/tsm-agt-<mode>.pid`
  - 日志：`.agent/logs/tsm-agt-<mode>.log`
- **误杀防护**：只处理 cwd 等于当前 `--workspace` 的进程，其它项目（例如 `AppHitchMainH5`）的 tsm-agt 不受影响。
- **孤儿清理**：默认只清理 `PPID=1` 的 `multiprocessing` 孤儿；需要保留时加 `--no-clean-orphans`。

---

## 6. 重启后验证

```bash
./scripts/restart.sh status
# 期望看到：端口 = 一个 PID；HTTP = 200；PID 文件指向同一 PID

curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/
# 期望：200
```

如果 `HTTP` 不是 200，先看日志：

```bash
./scripts/restart.sh logs
```
