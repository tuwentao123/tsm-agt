#!/usr/bin/env bash
#
# 一键重启 tsm-agt（本地开发 / 演示环境）。
#
# 用法:
#   scripts/restart.sh                 # 重启 Web UI（默认 127.0.0.1:8080）
#   scripts/restart.sh restart --mode api --port 8765
#   scripts/restart.sh stop            # 只停止
#   scripts/restart.sh start           # 只启动
#   scripts/restart.sh status          # 查看状态
#   scripts/restart.sh logs            # 跟踪日志
#
# 选项:
#   --mode web|api|chat   运行模式，默认 web
#   --host HOST           绑定地址，默认 127.0.0.1
#   --port PORT           端口；web 默认 8080，api 默认 8765
#   --workspace DIR       工作区，默认仓库根目录（.env 也从此目录加载）
#   --foreground          前台运行（不写 pid 文件、不重定向日志；chat 强制前台）
#   --grace SECONDS       SIGTERM 后等待秒数，默认 10，超时后 SIGKILL
#   --timeout SECONDS     启动健康检查超时秒数，默认 40
#   --no-clean-orphans    不清理遗留的 multiprocessing 孤儿进程
#   --dry-run             只打印将要执行的命令，不真正执行
#
# 说明: 必须在仓库根目录（或任意目录）用 bash 执行；脚本会自动 cd 到仓库根，
# 因为 Runtime 从当前工作目录的 .env 读取配置。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next }
       NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

# ---------------------------------------------------------------- parse args
ACTION="restart"
if [[ $# -gt 0 && "$1" != -* ]]; then
  ACTION="$1"
  shift
fi

MODE="web"
HOST="127.0.0.1"
PORT=""
WORKSPACE="$ROOT"
FOREGROUND=0
DRY_RUN=0
CLEAN_ORPHANS=1
GRACE=10
HEALTH_TIMEOUT=40

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)              MODE="${2:-}"; shift 2 ;;
    --host)              HOST="${2:-}"; shift 2 ;;
    --port)              PORT="${2:-}"; shift 2 ;;
    --workspace)         WORKSPACE="${2:-}"; shift 2 ;;
    --foreground)        FOREGROUND=1; shift ;;
    --grace)             GRACE="${2:-}"; shift 2 ;;
    --timeout)           HEALTH_TIMEOUT="${2:-}"; shift 2 ;;
    --no-clean-orphans)  CLEAN_ORPHANS=0; shift ;;
    --dry-run)           DRY_RUN=1; shift ;;
    -h|--help)           usage; exit 0 ;;
    *) echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$ACTION" in
  start|stop|restart|status|logs) ;;
  help) usage; exit 0 ;;
  *) echo "未知动作: $ACTION（可用 start|stop|restart|status|logs）" >&2; exit 2 ;;
esac

case "$MODE" in
  web)  [[ -z "$PORT" ]] && PORT=8080 ;;
  api)  [[ -z "$PORT" ]] && PORT=8765 ;;
  chat) MODE_IS_CHAT=1 ;;
  *) echo "未知模式: $MODE（可用 web|api|chat）" >&2; exit 2 ;;
esac
: "${MODE_IS_CHAT:=0}"

WORKSPACE="$(cd "$WORKSPACE" 2>/dev/null && pwd)" || {
  echo "工作区不存在: $WORKSPACE" >&2; exit 2;
}

RUN_DIR="$WORKSPACE/.agent/run"
LOG_DIR="$WORKSPACE/.agent/logs"
PID_FILE="$RUN_DIR/tsm-agt-$MODE.pid"
LOG_FILE="$LOG_DIR/tsm-agt-$MODE.log"

# ------------------------------------------------------------- interpreter
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON" ]]; then
  echo "找不到 python3；请先执行 uv sync 生成 .venv" >&2
  exit 1
fi

# ----------------------------------------------------------------- command
case "$MODE" in
  # web 入口内部固定 8080；这里直接跑 uvicorn，才能让 --host/--port 生效。
  web)  CMD=("$PYTHON" -m uvicorn tsm_agt.web.app:app \
              --host "$HOST" --port "$PORT" --log-level info) ;;
  api)  CMD=("$PYTHON" -m tsm_agt.cli api serve \
              --workspace "$WORKSPACE" --host "$HOST" --port "$PORT") ;;
  chat) CMD=("$PYTHON" -m tsm_agt.cli chat --workspace "$WORKSPACE") ;;
esac

mode_patterns() {
  case "$MODE" in
    web)  printf '%s\n' "tsm_agt.web.app" "uvicorn tsm_agt.web.app" ;;
    api)  printf '%s\n' "tsm_agt.cli api serve" ;;
    chat) printf '%s\n' "tsm_agt.cli chat" ;;
  esac
}

cwd_of() {
  # lsof -F 会把非 ASCII 字节转义成 \xNN，printf %b 还原成真实路径。
  local raw
  raw="$(lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
  [[ -n "$raw" ]] && printf '%b' "$raw"
}

command_of() {
  ps -o command= -p "$1" 2>/dev/null | sed 's/^[[:space:]]*//' | head -1
}

# Runtime 可能在多个 workspace 各跑一份；只处理本 workspace 的进程。
belongs_to_workspace() {
  local cwd
  cwd="$(cwd_of "$1")"
  [[ -z "$cwd" || "$cwd" == "$WORKSPACE" ]]
}

children_of() { pgrep -P "$1" 2>/dev/null || true; }

# 递归收集进程树（父进程被 kill 后，多进程子进程会变成孤儿，必须一起收）。
collect_tree() {
  local pid="$1" child
  echo "$pid"
  for child in $(children_of "$pid"); do
    collect_tree "$child"
  done
}

ppid_of() { ps -o ppid= -p "$1" 2>/dev/null | tr -d ' '; }

collect_pids() {
  local merged="" pid pattern

  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] && merged+="$pid"$'\n'
  fi

  # 监听目标端口的进程一定是本模式的当前实例。
  if [[ -n "$PORT" ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && merged+="$pid"$'\n'
    done < <(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  fi

  # 命令行匹配（覆盖重复启动、旧实例、uvicorn 直启）。
  while IFS= read -r pattern; do
    [[ -z "$pattern" ]] && continue
    while IFS= read -r pid; do
      [[ -z "$pid" ]] && continue
      belongs_to_workspace "$pid" && merged+="$pid"$'\n'
    done < <(pgrep -f "$pattern" 2>/dev/null || true)
  done < <(mode_patterns)

  # 历史遗留：父进程已死、被 launchd 收养的 multiprocessing 辅助进程。
  if [[ "$CLEAN_ORPHANS" == 1 ]]; then
    while IFS= read -r pid; do
      [[ -z "$pid" ]] && continue
      local ppid; ppid="$(ppid_of "$pid")"
      [[ -n "$ppid" && "$ppid" != "1" ]] && continue
      belongs_to_workspace "$pid" && merged+="$pid"$'\n'
    done < <(pgrep -f "multiprocessing.spawn|multiprocessing.resource_tracker" 2>/dev/null || true)
  fi

  printf '%s' "$merged" | awk -v self="$$" 'NF && $1 != self {print $1}' | sort -un
}

# ------------------------------------------------------------------- stop
do_stop() {
  local roots pids tree pid
  roots="$(collect_pids)"
  if [[ -z "$roots" ]]; then
    echo "没有发现运行中的 tsm-agt ($MODE)。"
    rm -f "$PID_FILE"
    return 0
  fi

  pids=""
  for pid in $roots; do
    pids+="$(collect_tree "$pid")"$'\n'
  done
  pids="$(printf '%s' "$pids" | awk -v self="$$" 'NF && $1 != self {print $1}' | sort -un | tr '\n' ' ')"

  echo "将要停止以下进程:"
  for pid in $pids; do
    printf '  PID %-7s %s\n' "$pid" "$(command_of "$pid")"
  done

  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[dry-run] kill -TERM $pids"
    return 0
  fi

  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null || true

  local i alive
  for ((i = 1; i <= GRACE; i++)); do
    alive=""
    for pid in $pids; do
      kill -0 "$pid" 2>/dev/null && alive+=" $pid"
    done
    [[ -z "$alive" ]] && break
    sleep 1
  done

  if [[ -n "$alive" ]]; then
    echo "宽限期内未退出，强制结束:$alive"
    # shellcheck disable=SC2086
    kill -KILL $alive 2>/dev/null || true
    sleep 1
  fi

  rm -f "$PID_FILE"
  if [[ -n "$PORT" && -n "$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)" ]]; then
    echo "警告: 端口 $PORT 仍被占用" >&2
    return 1
  fi
  echo "已停止。"
}

# ---------------------------------------------------------------- health
wait_ready() {
  local i code pid
  for ((i = 1; i <= HEALTH_TIMEOUT; i++)); do
    if [[ "$MODE" == "web" ]]; then
      code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://$HOST:$PORT/" 2>/dev/null || true)"
      [[ "$code" == "200" ]] && return 0
    else
      if [[ -n "$PORT" ]] && lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        return 0
      fi
    fi
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && ! kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    sleep 1
  done
  return 1
}

# ------------------------------------------------------------------ start
do_start() {
  if [[ "$DRY_RUN" != 1 && -n "$(collect_pids)" ]]; then
    echo "已有实例在运行；请先执行: scripts/restart.sh stop --mode $MODE" >&2
    return 1
  fi

  mkdir -p "$RUN_DIR" "$LOG_DIR"
  export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

  if [[ "$FOREGROUND" == 1 || "$MODE_IS_CHAT" == 1 ]]; then
    echo "前台运行: (cd $WORKSPACE && ${CMD[*]})"
    [[ "$DRY_RUN" == 1 ]] && return 0
    cd "$WORKSPACE" || exit 1
    exec "${CMD[@]}"
  fi

  echo "启动: (cd $WORKSPACE && ${CMD[*]})"
  echo "日志: $LOG_FILE"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[dry-run] nohup ... >> $LOG_FILE 2>&1 &"
    return 0
  fi

  cd "$WORKSPACE" || exit 1
  nohup "${CMD[@]}" </dev/null >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_FILE"

  if wait_ready; then
    if [[ "$MODE" == "web" ]]; then
      echo "已启动 (PID $pid)，后台运行。访问 http://$HOST:$PORT/"
    else
      echo "已启动 (PID $pid)，后台运行，监听 $HOST:$PORT"
    fi
    echo "提示: 后台进程不会占用当前终端；用 '$0 status' 查状态，'$0 logs' 看日志。"
    return 0
  fi

  echo "启动失败：$HEALTH_TIMEOUT 秒内未通过健康检查。最后日志:" >&2
  tail -n 40 "$LOG_FILE" >&2 2>/dev/null || true
  return 1
}

# ----------------------------------------------------------------- status
do_status() {
  echo "== tsm-agt 状态 =="
  echo "模式   : $MODE"
  echo "地址   : ${HOST}${PORT:+:$PORT}"
  echo "工作区 : $WORKSPACE"
  echo "解释器 : $PYTHON"

  local pids
  pids="$(collect_pids)"
  if [[ -z "$pids" ]]; then
    echo "进程   : 未运行"
  else
    echo "进程   :"
    for pid in $pids; do
      printf '  PID %-7s %s\n' "$pid" "$(command_of "$pid")"
    done
  fi

  if [[ -n "$PORT" ]]; then
    local listeners
    listeners="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
    echo "端口   : ${listeners:-无}"
  fi

  if [[ "$MODE" == "web" && -n "$PORT" ]]; then
    local code
    code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://$HOST:$PORT/" 2>/dev/null || true)"
    echo "HTTP   : ${code:-无响应}"
  fi

  if [[ -f "$PID_FILE" ]]; then
    echo "PID 文件: $PID_FILE -> $(cat "$PID_FILE" 2>/dev/null)"
  fi
  echo "日志   : $LOG_FILE"
}

# ------------------------------------------------------------------- main
cd "$ROOT" || exit 1

case "$ACTION" in
  stop)    do_stop ;;
  start)   do_start ;;
  restart) do_stop || exit 1; echo; do_start ;;
  status)  do_status ;;
  logs)
    mkdir -p "$LOG_DIR"
    touch "$LOG_FILE"
    tail -n 100 -f "$LOG_FILE"
    ;;
esac
