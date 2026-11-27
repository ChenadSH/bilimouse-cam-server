#!/usr/bin/env bash
# bilimouse-cam-server 一键更新 / 重启脚本
#
#   拉取 origin 最新代码 -> 重新构建镜像 -> 用新镜像重启容器 -> 健康检查
#
# 用法:
#   ./update.sh                 拉代码 + 按需重建镜像 + 重启 + 健康检查（默认）
#                              有代码更新才重建镜像；没更新就直接重启，不碰网络
#   ./update.sh --build         强制重新构建镜像（即使代码没有更新）
#   ./update.sh --no-build      只拉代码并重启容器，不重建镜像（最快）
#   ./update.sh --down          先 down 再 up，彻底重建容器和网络
#   ./update.sh --force         本地有改动时强制同步远端（会丢弃本地已跟踪的修改）
#   ./update.sh --wait 60       健康检查最多等 60 秒（默认 30）
#   ./update.sh --prune         更新完成后清理悬空镜像
#   ./update.sh --help
#
# 关于 .env（含 CAM_API_KEY）：脚本全程只读不写、绝不 cp .env.example 覆盖，
# 更新前后还会整份备份/比对，一旦被改动就自动恢复并打印密钥指纹。
# 录制文件在 data/recordings/，属于运行时数据，不在 git 跟踪范围，更新不受影响。
#
# 环境变量:
#   HOST_PORT=39110        改端口请写进 .env，compose 和本脚本会同时跟随
#   PUBLIC_URL=...         服务器对外地址（公网 IP/域名），用于打印设备端该填的 URL
#   PORT=39110             临时覆盖「健康检查探测的宿主机端口」（一般不用设）
#   CONTAINER_PORT=39110   容器内端口（宿主机没有 curl 时的兜底检查用）
#   COMPOSE_FILE=...       指定 compose 文件（默认 docker-compose.yml）
#   SERVICE=cam-server     compose 里的服务名
#   IMAGE=bilimouse-cam-server:latest   compose 构建出的镜像名（判断是否需要构建）
# ==============================================================================
set -Eeuo pipefail

usage() {
    sed -n '2,/^# =====/p' "$0" | sed 's/^# \{0,1\}//'
}

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }
trap 'rc=$?; printf "\033[1;31mxx \033[0m 脚本在第 %s 行失败 (exit %s)\n" "$LINENO" "$rc" >&2' ERR

# .env 备份文件（更新前）。EXIT 时清理，绝不残留。
# 清理失败（受限沙箱 / 只读临时目录）只该留个临时文件，不该把一次
# 已经成功的更新判为失败，所以这里必须吞掉错误并显式返回 0。
ENV_BAK=""
cleanup_env_bak() {
    [[ -n "$ENV_BAK" && -f "$ENV_BAK" ]] || return 0
    rm -f -- "$ENV_BAK" 2>/dev/null || true
    return 0
}
trap cleanup_env_bak EXIT

# 打印某个 .env 文件里 CAM_API_KEY 的短指纹（不泄露密钥明文），
# 用来向用户证明「更新前后密钥没被换掉」。
key_fingerprint() {
    local f="$1" k
    [[ -f "$f" ]] || { printf '(无 .env)'; return 0; }
    k="$(sed -n 's/^[[:space:]]*CAM_API_KEY[[:space:]]*=[[:space:]]*//p' "$f" \
         | tail -n 1 | tr -d '\r')"
    [[ -n "$k" ]] || { printf '(未配置)'; return 0; }
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$k" | sha256sum | cut -c1-8
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s' "$k" | shasum -a 256 | cut -c1-8
    else
        printf '%s' "$k" | cksum | cut -d' ' -f1
    fi
}

# 无论从哪个目录调用，都切到脚本所在目录（仓库根目录）
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

FORCE_BUILD=0
NO_BUILD=0
DO_DOWN=0
FORCE=0
PRUNE=0
WAIT_SECS=30
while (($#)); do
    case "$1" in
        --build)    FORCE_BUILD=1 ;;
        --no-build) NO_BUILD=1 ;;
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
if ((FORCE_BUILD && NO_BUILD)); then
    die "--build 和 --no-build 不能同时用"
fi

# 端口优先级：命令行 PORT > .env 里的 HOST_PORT > 默认 39110。
# compose 读同一个 .env 的 HOST_PORT，两边永远一致，不会健康检查打错端口。
HOST_PORT=""
if [[ -f .env ]]; then
    HOST_PORT="$(grep -E '^[[:space:]]*HOST_PORT=' .env | tail -n 1 | cut -d= -f2 | tr -dc '0-9')" || HOST_PORT=""
fi
PORT="${PORT:-${HOST_PORT:-39110}}"
CONTAINER_PORT="${CONTAINER_PORT:-39110}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
SERVICE="${SERVICE:-cam-server}"
IMAGE="${IMAGE:-bilimouse-cam-server:latest}"

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

# ---------- 密钥预检 ----------
# .env 被 .gitignore 排除，git pull / --force 的 reset 都不会动它。
# 但 compose 里写了 ${CAM_API_KEY:?...}，没配置会直接启动失败，先给个明白话。
if [[ -z "${CAM_API_KEY:-}" ]]; then
    [[ -f .env ]] || die "未找到 .env，无法启动服务。先执行: cp .env.example .env 并填写 CAM_API_KEY（设备端用 'cam key' 设成同一个值）"
    grep -Eq '^[[:space:]]*CAM_API_KEY=[^#[:space:]]+' .env \
        || die ".env 里没有有效的 CAM_API_KEY，请填写后重试"
    if grep -q 'please-change-me' .env; then
        warn ".env 里还是示例密钥，上线前请换成随机值"
    fi
fi

# ---------- 保护本地 .env（绝不替换用户已有的密钥）----------
# 平时 .env 被 .gitignore 排除，git pull / reset --hard 都不会动它。
# 但万一远端误把 .env 提交进仓库，--force 的 reset --hard 就会用远端版本
# 覆盖本地密钥。所以更新前先整份备份 + 记下密钥指纹，更新后逐字节比对，
# 有任何变化都无条件恢复成用户原来的那份。
ENV_FP_BEFORE=""
if [[ -f .env ]]; then
    # Windows/Git Bash 的 TMPDIR 常是 "C:\Users\...\Temp"，生成的备份路径
    # 带盘符，某些受限环境的删除钩子会拒绝；统一退回 MSYS 的 /tmp 更稳。
    BAK_DIR="${TMPDIR:-/tmp}"
    case "$BAK_DIR" in
        *\\*|[A-Za-z]:*) BAK_DIR="/tmp" ;;
    esac
    ENV_BAK="$(mktemp "${BAK_DIR}/bilimouse-env.XXXXXX")" \
        || die "无法创建 .env 备份临时文件"
    cp -p .env "$ENV_BAK" || die "备份 .env 失败"
    ENV_FP_BEFORE="$(key_fingerprint .env)"
    log "已备份本地 .env（CAM_API_KEY 指纹 ${ENV_FP_BEFORE}）"
fi

# ---------- 拉取代码 ----------
log "拉取远端更新（分支 $BRANCH）..."
git fetch --prune origin

# 远端若把 .env 纳入版本控制，明确告知：本地版本会被保留、不会被覆盖。
if git ls-tree -r --name-only "origin/$BRANCH" 2>/dev/null | grep -qx '.env'; then
    warn "远端 $BRANCH 里存在被跟踪的 .env；本脚本会在更新后恢复你的本地 .env（密钥不会被替换）"
fi

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

# ---------- 校验并恢复 .env ----------
# 只要 .env 内容有变动（被远端版本覆盖 / 被 pull 删掉），就用备份原样写回。
# 这样无论远端代码怎么变，用户自己配的 CAM_API_KEY 始终不动。
if [[ -n "$ENV_BAK" && -f "$ENV_BAK" ]]; then
    if [[ ! -f .env ]]; then
        cp -p "$ENV_BAK" .env
        warn ".env 在更新过程中丢失，已从备份恢复（密钥不变）"
    elif ! cmp -s .env "$ENV_BAK"; then
        cp -p "$ENV_BAK" .env
        warn ".env 在更新过程中被改动，已恢复为更新前的本地版本（密钥不变）"
    fi
    ENV_FP_AFTER="$(key_fingerprint .env)"
    if [[ "$ENV_FP_BEFORE" == "$ENV_FP_AFTER" ]]; then
        log ".env 密钥保持不变（指纹 ${ENV_FP_AFTER}）"
    else
        die ".env 密钥指纹不一致（${ENV_FP_BEFORE} -> ${ENV_FP_AFTER}），已中止，请手动检查 .env"
    fi
fi

CHANGED=0
if [[ "$OLD" == "$NEW" ]]; then
    log "代码已是最新 (${NEW:0:7})"
else
    CHANGED=1
    log "已更新 ${OLD:0:7} -> ${NEW:0:7}："
    git --no-pager log --oneline --no-decorate "$OLD..$NEW" | sed 's/^/    /' || true
fi

# ---------- 决定是否重新构建镜像 ----------
# 代码没变且本地已有镜像时跳过 build：避免每次都去 Docker Hub 拉基础镜像元数据
# （国内网络常见 "Head ... EOF"），也让「只想重启服务」变成秒级操作。
# 首次部署或代码有更新时才构建；--build / --no-build 可强制指定。
if ((NO_BUILD)); then
    DO_BUILD=0
elif ((FORCE_BUILD)); then
    DO_BUILD=1
elif ((CHANGED)); then
    DO_BUILD=1
elif docker image inspect "$IMAGE" >/dev/null 2>&1; then
    DO_BUILD=0
    log "无代码更新且镜像已存在，跳过构建，直接重启容器"
else
    DO_BUILD=1
    log "本地没有 $IMAGE，需要先构建镜像"
fi

UP_ARGS=(-d --force-recreate --remove-orphans)
if ((DO_BUILD)); then
    UP_ARGS+=(--build)
fi

if ((DO_DOWN)); then
    log "停止并移除旧容器..."
    "${COMPOSE[@]}" down
fi

if ((DO_BUILD)); then
    BUILD_TXT=yes
else
    BUILD_TXT=no
fi
log "启动服务（build=$BUILD_TXT，host port=$PORT）..."
if ! "${COMPOSE[@]}" up "${UP_ARGS[@]}"; then
    die "启动失败。若日志里是 pip 拉包超时，在 .env 里设 PIP_INDEX_URL 换国内源后重试；若是 Docker Hub 拉 python:3.12-slim 超时（国内常见），可先手动 'docker pull python:3.12-slim'，或代码无更新时用 --no-build 直接重启"
fi

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
    die "服务启动失败，请检查上面的日志（常见原因：${PORT} 端口被占用、镜像构建失败、.env 的 HOST_PORT 与 PORT 不一致）"
fi

if ((PRUNE)); then
    log "清理悬空镜像..."
    docker image prune -f | tail -n 3 || true
fi

# 设备端实际访问的地址：优先 .env 里的 PUBLIC_URL（云服务器填公网 IP/域名），
# 没配才退回 hostname -I 的局域网 IP + HOST_PORT。
if [[ -z "${PUBLIC_URL:-}" && -f .env ]]; then
    PUBLIC_URL="$(sed -n 's/^[[:space:]]*PUBLIC_URL[[:space:]]*=[[:space:]]*//p' .env \
                  | tail -n 1 | tr -d '\r' | sed 's/[[:space:]]*$//')" || PUBLIC_URL=""
fi
if [[ -z "${PUBLIC_URL:-}" ]]; then
    LAN_IP=""
    if command -v hostname >/dev/null 2>&1; then
        LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    fi
    [[ -n "$LAN_IP" ]] || LAN_IP="<服务器IP>"
    PUBLIC_URL="http://${LAN_IP}:${PORT}"
fi
PUBLIC_URL="${PUBLIC_URL%/}"

log "更新完成，服务已就绪"
printf '    本机预览 : http://127.0.0.1:%s/\n' "$PORT"
printf '    设备端填 : %s   （串口: cam url %s）\n' "$PUBLIC_URL" "$PUBLIC_URL"
printf '    改地址   : 服务端 .env 的 PUBLIC_URL / 固件 main/app/user_config.h\n'
printf '    查看日志 : %s logs -f\n' "${COMPOSE[*]}"
