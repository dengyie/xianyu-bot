#!/usr/bin/env bash
# ============================================================
# Docker 镜像保留策略 / 磁盘清理（腾讯轻量云生产）
# ============================================================
# 背景：CI 每次发版都会在 VPS 留下 latest + sha-<commit> 标签，旧 sha 标签
# 从不清理；ai-novel 等项目同样按 commit 累积，40G 盘 2026-11 已到 81%。
# 周清理只 prune dangling，对“有 tag 的旧镜像”无效。
#
# 保留规则（按仓库独立计算）：
#   1) 任意容器（含已停止）引用的镜像        -> 永不删除
#   2) :latest 标签                          -> 永不删除
#   3) 每个仓库按创建时间保留最新
#      KEEP_PREVIOUS_TAGS 个非 latest 标签
#      （默认 1，即每个项目留一个可回滚的上一版）
#   4) 其余旧标签删除；随后清理 dangling 镜像与无引用 build cache
#   5) journald 日志超过上限时 vacuum（默认 500M）
#
# 手动用法：
#   DRY_RUN=1 bash scripts/docker_retention.sh   # 只打印计划，不删除
#   bash scripts/docker_retention.sh             # 实际清理
#
# 主机安装（systemd 定时器，每日 05:30 本地时间）见下方单元内容；
# 生产安装命令：
#   install -m 0755 scripts/docker_retention.sh /usr/local/sbin/docker-retention.sh
# ============================================================

set -uo pipefail

REPO_PREFIXES="${REPO_PREFIXES:-ghcr.io/dengyie/ ghcr.nju.edu.cn/dengyie/}"
KEEP_PREVIOUS_TAGS="${KEEP_PREVIOUS_TAGS:-1}"
DRY_RUN="${DRY_RUN:-0}"
JOURNAL_MAX="${JOURNAL_MAX:-500M}"

DOCKER="${DOCKER:-docker}"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# 防并发：定时器与 CI 清理互斥（也避免与运行中的镜像拉取相互干扰）
if command -v flock >/dev/null 2>&1; then
    exec 9>/var/lock/docker-retention.lock
    if ! flock -n 9; then
        log "另一轮清理正在运行，退出"
        exit 0
    fi
fi

if ! "$DOCKER" version >/dev/null 2>&1; then
    log "docker 不可用，退出"
    exit 1
fi

DF_BEFORE=$(df -h / | tail -1)
log "清理前磁盘: $DF_BEFORE"

# 容器引用的镜像 ID（全量 -> 短 ID，与 docker images 的 .ID 对齐）
protected=$("$DOCKER" ps -aq | xargs -r "$DOCKER" inspect --format '{{.Image}}' 2>/dev/null \
    | sed 's/^sha256://' | cut -c1-12 | sort -u)

rows=$("$DOCKER" images --format '{{.Repository}}|{{.Tag}}|{{.ID}}|{{.CreatedAt}}' 2>/dev/null)

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
: > "$TMP"

IN_SCOPE=0
while IFS='|' read -r repo tag id created; do
    [ -z "$repo" ] && continue
    [ "$tag" = "<none>" ] && continue
    for p in $REPO_PREFIXES; do
        case "$repo" in
            "$p"*)
                printf '%s|%s|%s|%s\n' "$repo" "$tag" "$id" "$created" >> "$TMP"
                IN_SCOPE=$((IN_SCOPE + 1))
                break
                ;;
        esac
    done
done <<< "$rows"

log "扫描到 ${IN_SCOPE} 个受管标签（前缀: $REPO_PREFIXES）"

REMOVE=()
repos=$(awk -F'|' 'NF{print $1}' "$TMP" | sort -u)

for repo in $repos; do
    # 最新在前；latest 与在用镜像不占“上一版”配额
    mapfile -t repo_lines < <(awk -F'|' -v r="$repo" '$1==r' "$TMP" | sort -t'|' -k4,4r)
    kept_prev=0
    for line in "${repo_lines[@]}"; do
        IFS='|' read -r _ tag id _created <<< "$line"
        if [ "$tag" = "latest" ]; then
            log "保留 $repo:latest（latest 标签）"
            continue
        fi
        if [ -n "$protected" ] && grep -qxF "$id" <<< "$protected"; then
            log "保留 $repo:$tag（容器在用）"
            continue
        fi
        if [ "$kept_prev" -lt "$KEEP_PREVIOUS_TAGS" ]; then
            kept_prev=$((kept_prev + 1))
            log "保留 $repo:$tag（最近第 ${kept_prev} 个非 latest，可回滚）"
            continue
        fi
        REMOVE+=("$repo:$tag")
    done
done

log "计划删除 ${#REMOVE[@]} 个旧标签"
removed=0
for ref in "${REMOVE[@]:-}"; do
    [ -z "$ref" ] && continue
    if [ "$DRY_RUN" = "1" ]; then
        log "would remove $ref"
        continue
    fi
    if "$DOCKER" image rm "$ref" >/dev/null 2>&1; then
        log "removed $ref"
        removed=$((removed + 1))
    else
        log "WARN: 删除失败 $ref（可能仍被引用），跳过"
    fi
done

if [ "$DRY_RUN" = "1" ]; then
    log "dry-run: 跳过 prune / journal vacuum"
else
    "$DOCKER" image prune -f >/dev/null 2>&1 || true
    "$DOCKER" builder prune -f >/dev/null 2>&1 || true
    if command -v journalctl >/dev/null 2>&1; then
        journalctl --vacuum-size="$JOURNAL_MAX" 2>/dev/null | tail -1 || true
    fi
fi

log "已删除标签 ${removed} 个"
log "清理后磁盘: $(df -h / | tail -1)"
"$DOCKER" system df
