#!/usr/bin/env bash
# bilimouse-cam-server 一键更新 / 重启脚本
#
#   拉取 origin 最新代码 -> 重新构建镜像 -> 用新镜像重启容器 -> 健康检查
#
# 用法:
#   ./update.sh                 拉代码 + 重建镜像 + 重启 + 健康检查（默认）
#   ./update.sh --no-build      只拉代码并重启容器，不重建镜像（最快）
#   ./update.sh --down          先 down 再 up，彻底重建容器和网络
#   ./update.sh --force         本地有改动时强制同步远端（会丢弃本地已跟踪的修改）
#   ./update.sh --wait 60       健康检查最多等 60 秒（默认 30）
#   ./update.sh --prune         更新完成后清理悬空镜像
#   ./update.sh --help
#
# 环境变量:
#   PORT=8080              宿主机端口（健康检查用，需与 compose 的端口映射一致）
#   CONTAINER_PORT=8080    容器内端口（宿主机没有 curl 时的兜底检查用）
#   COMPOSE_FILE=...       指定 compose 文件（默认 docker-compose.yml）
#   SERVICE=cam-server     compose 里的服务名
# ==============================================================================
set -Eeuo pipefail

usage() {
    sed -n '2,/^# =====/p' "$0" | sed 's/^# \{0,1\}//'
}

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }
trap 'rc=$?; printf "\033[1;31mxx \033[0m 脚本在第 %s 行失败 (exit %s)\n" "$LINENO" "$rc" >&2' ERR

# 无论从哪个目录调用，都切到脚本所在目录（仓库根目录）
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DO_BUILD=1
DO_DOWN=0
FORCE=0
PRUNE=0
WAIT_SECS=30
while (($#)); do
    case "$1" in
        --no-build) DO_BUILD=0 ;;
        --down)     DO_DOWN=1 ;;
        --force)    FORCE=1 ;;
        --prune)    PRUNE=1 ;;
        --wait)     shift; (($#)) || die "--wait 后面要跟一个数字"; WAIT_SECS="$1" ;;
        --wait=*)   WAIT_SECS="${1#*=}" ;;
        -h|--help)  usage; exit 0 ;;
        *)          die "未知参数: $1（用 --help 查看用法）" ;;
    esac
    shift
done
[[ "$WAIT_SECS" =~ ^[0-9]+$ ]] || die "--wait 需要正整数，收到: $WAIT_SECS"

PORT="${PORT:-8080}"
CONTAINER_PORT="${CONTAINER_PORT:-8080}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
SERVICE="${SERVICE:-cam-server}"

# ---------- 环境自检 ----------
command -v git >/dev/null 2>&1    || die "未找到 git"
command -v docker >/dev/null 2>&1 || die "未找到 docker"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "当前目录不是 git 仓库: $SCRIPT_DIR"
[[ -f "$COMPOSE_FILE" ]] || die "找不到 compose 文件: $COMPOSE_FILE"
docker info >/dev/null 2>&1 || die "Docker 服务没在运行，或当前用户无权访问 docker socket（可试 sudo）"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose -f "$COMPOSE_FILE")
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose -f "$COMPOSE_FILE")
else
    die "未找到 'docker compose' 或 'docker-compose'，请安装 Docker Compose v2"
fi

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
[[ -n "$BRANCH" ]] || die "当前是 detached HEAD，请先切回 main 分支"

# ---------- 拉取代码 ----------
log "拉取远端更新（分支 $BRANCH）..."
git fetch --prune origin

# data/ 里的运行时快照是未跟踪文件，不阻塞更新；只检查已跟踪文件
DIRTY="$(git status --porcelain --untracked-files=no)"
if [[ -n "$DIRTY" ]]; then
    if ((FORCE)); then
        warn "--force：将丢弃本地已跟踪文件的改动"
        printf '%s\n' "$DIRTY"
    else
        printf '%s\n' "$DIRTY" >&2
        die "本地有未提交的改动，已中止（不会覆盖你的修改）。先提交/暂存，或加 --force 强制同步远端"
    fi
fi

OLD="$(git rev-parse HEAD)"
if ((FORCE)); then
    git reset --hard "origin/$BRANCH"
else
    # --ff-only：本地有分叉时直接报错，不自动生成 merge commit
    git pull --ff-only origin "$BRANCH"
fi
NEW="$(git rev-parse HEAD)"

if [[ "$OLD" == "$NEW" ]]; then
    log "代码已是最新 (${NEW:0:7})"
else
    log "已更新 ${OLD:0:7} -> ${NEW:0:7}："
    git --no-pager log --oneline --no-decorate "$OLD..$NEW" | sed 's/^/    /' || true
fi

# ---------- 重建并重启服务 ----------
UP_ARGS=(-d --force-recreate --remove-orphans)
if ((DO_BUILD)); then
    UP_ARGS+=(--build)
fi

if ((DO_DOWN)); then
    log "停止并移除旧容器..."
    "${COMPOSE[@]}" down
fi

log "启动服务（build=$([[ $DO_BUILD == 1 ]] && echo yes || echo no)）..."
"${COMPOSE[@]}" up "${UP_ARGS[@]}"

# ---------- 健康检查 ----------
health_ok() {
    local url="http://127.0.0.1:${PORT}/healthz"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 2 "$url" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q --timeout=2 -O /dev/null "$url" >/dev/null 2>&1
    else
        # 宿主机没有 curl/wget 时，从容器内部探测（镜像里一定有 python）
        "${COMPOSE[@]}" exec -T "$SERVICE" \
            python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:${CONTAINER_PORT}/healthz',timeout=2)" \
            >/dev/null 2>&1
    fi
}

log "等待服务就绪（最多 ${WAIT_SECS}s）..."
OK=0
for ((i = 0; i < WAIT_SECS; i++)); do
    if health_ok; then OK=1; break; fi
    sleep 1
done

if ((!OK)); then
    warn "健康检查未通过，最近的日志："
    "${COMPOSE[@]}" logs --tail=80 || true
    die "服务启动失败，请检查上面的日志（常见原因：8080 端口被占用、镜像构建失败）"
fi

if ((PRUNE)); then
    log "清理悬空镜像..."
    docker image prune -f | tail -n 3 || true
fi

LAN_IP=""
if command -v hostname >/dev/null 2>&1; then
    LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
[[ -n "$LAN_IP" ]] || LAN_IP="<服务器IP>"

log "更新完成，服务已就绪"
printf '    本机预览 : http://127.0.0.1:%s/\n' "$PORT"
printf '    设备端填 : http://%s:%s   （串口: cam url http://%s:%s）\n' \
    "$LAN_IP" "$PORT" "$LAN_IP" "$PORT"
printf '    查看日志 : %s logs -f\n' "${COMPOSE[*]}"
