#!/usr/bin/env bash
# bilimouse-cam-server 录制内容清理脚本
#
#   删除 data/recordings/ 下按日期归档的 .mjpg 录制文件。
#   只动这一个目录，绝不碰代码、.env 或别的任何文件。
#
# 用法:
#   ./clean-recordings.sh                  列出将删除的内容并确认后删除全部
#   ./clean-recordings.sh --yes            不询问，直接删除全部（cron / 一键用）
#   ./clean-recordings.sh --dry-run        只统计、不删除
#   ./clean-recordings.sh --date 2026-11-27   只删某一天
#   ./clean-recordings.sh --older 7        只删 7 天前的日期文件夹
#   ./clean-recordings.sh --stop           删除前先让服务端停止当前录制
#   ./clean-recordings.sh --help
#
# 说明:
#   - 录制中的文件会处于打开状态；加 --stop 会先调用 /api/record/stop
#     （从 .env 读 CAM_API_KEY），停稳了再删，最干净。不加也能删，
#     服务端在录制超时/设备掉线/服务重启时会自动收尾。
#   - 服务端网页「录制文件」面板里的「清空全部」调用的是 DELETE /api/recordings，
#     和本脚本效果一致；本脚本用于命令行 / 定时清理。
# ==============================================================================
set -Eeuo pipefail

usage() { sed -n '2,/^# =====/p' "$0" | sed 's/^# \{0,1\}//'; }

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

YES=0
DRY=0
STOP=0
ONLY_DATE=""
OLDER_DAYS=""
while (($#)); do
    case "$1" in
        -y|--yes)       YES=1 ;;
        --dry-run)      DRY=1 ;;
        --stop)         STOP=1 ;;
        --date)         shift; (($#)) || die "--date 后要跟 YYYY-MM-DD"; ONLY_DATE="$1" ;;
        --date=*)       ONLY_DATE="${1#*=}" ;;
        --older)        shift; (($#)) || die "--older 后要跟天数"; OLDER_DAYS="$1" ;;
        --older=*)      OLDER_DAYS="${1#*=}" ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "未知参数: $1（用 --help 查看用法）" ;;
    esac
    shift
done

# 参数校验（严格限定目录名格式，避免路径穿越 / 误删）
[[ -z "$ONLY_DATE" || "$ONLY_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
    || die "--date 需要 YYYY-MM-DD 格式，收到: $ONLY_DATE"
[[ -z "$OLDER_DAYS" || "$OLDER_DAYS" =~ ^[0-9]+$ ]] \
    || die "--older 需要正整数天数，收到: $OLDER_DAYS"

# 录制目录。服务端默认 /data/recordings（compose 把 ./data 挂成 /data）。
# 允许用 RECORD_DIR 覆盖成绝对路径。
REC_DIR="${RECORD_DIR:-$SCRIPT_DIR/data/recordings}"

# 二次确认：无论怎么配置，都必须在仓库范围内的 recordings 目录，防止误删。
case "$REC_DIR" in
    "$SCRIPT_DIR"/data/recordings|/data/recordings|*/bilimouse-cam-server/data/recordings) ;;
    *) die "RECORD_DIR 看起来不是本项目的录制目录，已拒绝: $REC_DIR" ;;
esac

if [[ ! -d "$REC_DIR" ]]; then
    log "录制目录不存在或已为空：$REC_DIR"
    exit 0
fi

# ---------- 先停止当前录制（可选）----------
if ((STOP)); then
    KEY=""
    if [[ -f .env ]]; then
        KEY="$(sed -n 's/^[[:space:]]*CAM_API_KEY[[:space:]]*=[[:space:]]*//p' .env \
              | tail -n 1 | tr -d '\r')"
    fi
    PORT="$(grep -E '^[[:space:]]*HOST_PORT=' .env 2>/dev/null | tail -n 1 | cut -d= -f2 | tr -dc '0-9')"
    PORT="${PORT:-39110}"
    if [[ -n "$KEY" ]] && command -v curl >/dev/null 2>&1; then
        if curl -fsS --max-time 3 -X POST -H "X-Api-Key: $KEY" \
             "http://127.0.0.1:${PORT}/api/record/stop" >/dev/null 2>&1; then
            log "已通知服务端停止当前录制"
        else
            warn "通知服务端停止录制失败（服务没在跑？）；继续按文件清理"
        fi
    else
        warn "没有可用的 CAM_API_KEY / curl，跳过在线停止"
    fi
fi

# ---------- 收集要删的日期目录 ----------
TODAY="$(date +%Y-%m-%d)"
declare -a TARGETS=()
for d in "$REC_DIR"/*; do
    [[ -d "$d" ]] || continue
    name="$(basename "$d")"
    [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue  # 只认纯日期目录
    if [[ -n "$ONLY_DATE" && "$name" != "$ONLY_DATE" ]]; then continue; fi
    if [[ -n "$OLDER_DAYS" ]]; then
        # 按目录名字符串比较即可（YYYY-MM-DD 字典序 = 时间序）
        cutoff="$(date -d "-${OLDER_DAYS} days" +%Y-%m-%d 2>/dev/null \
                 || date -v-"${OLDER_DAYS}"d +%Y-%m-%d 2>/dev/null || echo "")"
        [[ -n "$cutoff" ]] || die "当前系统 date 不支持 -d/-v，无法计算 --older"
        [[ "$name" < "$cutoff" ]] || continue
    fi
    TARGETS+=("$d")
done

if ((${#TARGETS[@]} == 0)); then
    log "没有符合条件的录制内容，无需清理"
    exit 0
fi

# ---------- 统计 ----------
NFILES=0
NBYTES=0
for d in "${TARGETS[@]}"; do
    while IFS= read -r f; do
        [[ -f "$f" ]] || continue
        NFILES=$((NFILES + 1))
        sz="$(wc -c < "$f" | tr -dc '0-9')"
        NBYTES=$((NBYTES + ${sz:-0}))
    done < <(find "$d" -maxdepth 1 -type f \( -name '*.mjpg' -o -name '*.json' \) 2>/dev/null)
done
HUMAN="$(awk -v b="$NBYTES" 'BEGIN{
    if(b>=1073741824) printf "%.2f GB", b/1073741824;
    else if(b>=1048576) printf "%.1f MB", b/1048576;
    else if(b>=1024) printf "%.0f KB", b/1024;
    else printf "%d B", b }')"

log "将删除 ${#TARGETS[@]} 个日期目录、${NFILES} 个文件（约 ${HUMAN}）："
for d in "${TARGETS[@]}"; do
    printf '    %s  (%s 个文件)\n' "$(basename "$d")" \
        "$(find "$d" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -dc '0-9')"
done

if ((DRY)); then
    log "--dry-run：以上仅为预览，未删除任何文件"
    exit 0
fi

# ---------- 确认 ----------
if (( ! YES )); then
    printf '\n\033[1;33m确认删除以上录制内容？此操作不可恢复 [y/N]: \033[0m'
    read -r ans || ans=""
    [[ "$ans" == "y" || "$ans" == "Y" || "$ans" == "yes" ]] \
        || { log "已取消，未删除任何文件"; exit 0; }
fi

# ---------- 删除（严格限定在日期目录内）----------
REMOVED=0
for d in "${TARGETS[@]}"; do
    # 逐个删匹配文件，再删空目录；不对整个 REC_DIR 用 rm -rf
    find "$d" -maxdepth 1 -type f \( -name '*.mjpg' -o -name '*.json' \) -delete 2>/dev/null || true
    rmdir "$d" 2>/dev/null || true   # 目录里有别的东西就保留，不强制
    REMOVED=$((REMOVED + 1))
done
rmdir "$REC_DIR" 2>/dev/null || true   # 全空则连父目录一起收掉

log "清理完成：处理了 ${REMOVED} 个日期目录"
