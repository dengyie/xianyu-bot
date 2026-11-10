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
#   3) 未过 MIN_AGE_HOURS（默认 48h）观察期的新标签 -> 不删
#   4) 每个仓库按创建时间保留最新
#      KEEP_PREVIOUS_TAGS 个非 latest 标签
#      （默认 1，即每个项目留一个可回滚的上一版）
#   5) 其余旧标签删除；随后清理 dangling 镜像与无引用 build cache
#   6) journald 日志超过上限时 vacuum（默认 500M）
#
# 手动用法：
#   DRY_RUN=1 bash scripts/docker_retention.sh   # 只打印计划，不删除
#   bash scripts/docker_retention.sh             # 实际清理
#
# 主机安装/更新（幂等，含 systemd 定时器与 journald 封顶）：
#   bash scripts/install_retention_units.sh
# ============================================================

set -uo pipefail

# 按命名空间匹配，而非写死镜 registry 前缀：镜像源会随部署脚本变动
# （ghcr.io / ghcr.nju.edu.cn / ghcr.dockerproxy.net 均已实际出现过），
# 前缀白名单会漏掉新源、让旧源副本越过保留策略永久堆积（实测 3.7GB 一次）。
# 匹配规则：仓库路径含 /<REPO_NAMESPACE>/ 即纳入管理。
REPO_NAMESPACE="${REPO_NAMESPACE:-dengyie}"
KEEP_PREVIOUS_TAGS="${KEEP_PREVIOUS_TAGS:-1}"
DRY_RUN="${DRY_RUN:-0}"
JOURNAL_MAX="${JOURNAL_MAX:-500M}"
# 新标签最短观察期：CreatedAt 取的是镜像构建时间而非拉取/打标时间，
# 手动回拉旧版本做回滚演练时若无宽限期，下一轮定时任务会立刻把它删掉。
MIN_AGE_HOURS="${MIN_AGE_HOURS:-48}"

DOCKER="${DOCKER:-docker}"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# 防并发：定时器与 CI 清理互斥。/var/lock 不可写（非 root 手动跑）时退回 /tmp，
# 否则 exec 重定向失败会直接终止脚本。
if command -v flock >/dev/null 2>&1; then
    LOCK_FILE=/var/lock/docker-retention.lock
    [ -w /var/lock ] || LOCK_FILE=/tmp/docker-retention.lock
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log "另一轮清理正在运行，退出"
        exit 0
    fi
fi

# 与部署互斥：deploy 用 setsid 拉起的跨境 pull 可运行 25-50 分钟（镜像层一旦
# 中断就整层重来），rmi/prune 撞上在途拉取可能搅动其层缓存。检测到即让路。
if pgrep -f 'xianyu_pull_helper' >/dev/null 2>&1 \
    || pgrep -f 'compose .*pull xianyu-app' >/dev/null 2>&1; then
    log "检测到部署拉取正在进行，本轮跳过"
    exit 0
fi

if ! "$DOCKER" version >/dev/null 2>&1; then
    log "docker 不可用，退出"
    exit 1
fi

# 与 docker images 的 CreatedAt 同为 daemon 本地时间，可直接字典序比较
AGE_CUTOFF=$(date -d "-${MIN_AGE_HOURS} hours" '+%F %T' 2>/dev/null || true)

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
    # 同时覆盖带 registry 的（ghcr.io/dengyie/...）和裸命名空间的（dengyie/...）
    case "$repo" in
        "$REPO_NAMESPACE"/*|*/"$REPO_NAMESPACE"/*)
            printf '%s|%s|%s|%s\n' "$repo" "$tag" "$id" "$created" >> "$TMP"
            IN_SCOPE=$((IN_SCOPE + 1))
            ;;
    esac
done <<< "$rows"

log "扫描到 ${IN_SCOPE} 个受管标签（命名空间: $REPO_NAMESPACE）"

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
        # 观察期放在配额之前：新标签不占“上一版”名额，旧回滚版等观察期过了再让位
        if [ -n "$AGE_CUTOFF" ]; then
            created_ts=${_created:0:19}
            if [[ "$created_ts" > "$AGE_CUTOFF" ]]; then
                log "保留 $repo:$tag（创建于 ${created_ts}，未过 ${MIN_AGE_HOURS}h 观察期）"
                continue
            fi
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
    if err=$("$DOCKER" image rm "$ref" 2>&1); then
        log "removed $ref"
        removed=$((removed + 1))
    else
        log "WARN: 删除失败 $ref: $err"
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
