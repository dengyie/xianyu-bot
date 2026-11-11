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
# 失败可见性（fail loud）：
#   * docker images 取不到数据          -> exit 2，不假装“清理成功”
#   * 有标签删不掉                      -> exit 1，CI / systemd 变红
#   * prune / journal vacuum 失败      -> WARN 落日志，不影响退出码
#   * DRY_RUN=1 永远 exit 0（只演练）
#
# 手动用法：
#   DRY_RUN=1 bash scripts/docker_retention.sh   # 只打印计划，不删除
#   bash scripts/docker_retention.sh             # 实际清理
#
# 主机安装/更新（幂等，含 systemd 定时器与 journald 封顶）：
#   bash scripts/install_retention_units.sh
# ============================================================

set -uo pipefail

# 按命名空间匹配，而非写死 registry 前缀：镜像源会随部署脚本变动
# （ghcr.io / ghcr.nju.edu.cn / ghcr.dockerproxy.net 均已实际出现过），
# 前缀白名单会漏掉新源、让旧源副本越过保留策略永久堆积（实测 3.7GB 一次）。
# 匹配规则：剥掉 registry 主机段后，仓库路径首段 == <REPO_NAMESPACE> 即纳入管理。
# 用首段锚点而不是 */ns/*：后者会误伤 library/dengyie/foo 这类第三方仓库。
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
# 查询失败不中止：docker image rm 不带 --force，对在用镜像必然拒绝，是第二道保险。
# 但保护清单变空要告警——否则只能靠 daemon 拒绝兜底，日志里看不出异常。
protected=""
if cids=$("$DOCKER" ps -aq 2>/dev/null) && [ -n "$cids" ]; then
    if raw=$("$DOCKER" inspect --format '{{.Image}}' $cids 2>&1); then
        protected=$(printf '%s\n' "$raw" | sed 's/^sha256://' | cut -c1-12 | sort -u)
    else
        log "WARN: 查询容器在用镜像失败，仅依赖 image rm 拒绝删除在用镜像兜底: $(printf '%s' "$raw" | head -c 200)"
        protected=""
    fi
fi

rows=$("$DOCKER" images --format '{{.Repository}}|{{.Tag}}|{{.ID}}|{{.CreatedAt}}' 2>/dev/null)
img_rc=$?

# 空结果不能当成“清理成功”：daemon 抖动 / 权限异常 / 存储驱动异常都会返回空表，
# 静默跑完会让 CI 报告与一次健康的清理无法区分。直接判失败（fail loud）。
# --format 只输出数据行，无表头，出空即真无镜像。
if [ "$img_rc" -ne 0 ]; then
    log "ERROR: docker images 执行失败 (rc=$img_rc)"
    exit 2
fi
if [ -z "$rows" ]; then
    log "ERROR: docker images 返回空结果，无法判定保留范围"
    exit 2
fi

TMP=$(mktemp)
PLAN=$(mktemp)
PLAN_RAW="$TMP.plan"
trap 'rm -f "$TMP" "$PLAN" "$PLAN_RAW"' EXIT
: > "$TMP"

IN_SCOPE=0
while IFS='|' read -r repo tag id created; do
    [ -z "$repo" ] && continue
    [ "$tag" = "<none>" ] && continue
    # 同时覆盖带 registry 的（ghcr.io/dengyie/...、ghcr.nju.edu.cn/dengyie/...）
    # 和裸命名空间的（dengyie/...）。判定：首段是 registry 主机时剥掉，再比首段。
    # 按 Docker 引用语法，含 '/' 的首段满足其一即为 registry：含 '.'、含 ':'（端口）、
    # 或恰为 'localhost'。避免误伤 library/dengyie/foo 这类第三方仓库。
    path="$repo"
    host="${path%%/*}"
    if [[ "$host" == *.* || "$host" == *:* || "$host" == "localhost" ]]; then
        path="${path#*/}"
    fi
    if [ "${path%%/*}" = "$REPO_NAMESPACE" ]; then
        printf '%s|%s|%s|%s\n' "$repo" "$tag" "$id" "$created" >> "$TMP"
        IN_SCOPE=$((IN_SCOPE + 1))
    fi
done <<< "$rows"

log "扫描到 ${IN_SCOPE} 个受管标签（命名空间: $REPO_NAMESPACE）"

PLANNED=0
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
        printf '%s|%s\n' "$id" "$repo:$tag" >> "$PLAN_RAW"
        PLANNED=$((PLANNED + 1))
    done
done

# 按镜像短 ID 归并待删标签：同一镜像常被多个源各打一个标签（ghcr.io 与
# ghcr.nju.edu.cn 指向同一层）。逐标签 rm 只解绑不改本体，对同一镜像做 N 次
# daemon 往返，且“删了 N 个标签”与“腾出 N 个镜像”对不上；一次 rm 传齐同 ID
# 的引用即可整体 untag 并在最后一个标签消失时删层。
awk -F'|' 'NF{ refs[$1] = (refs[$1] ? refs[$1] " " : "") $2 } END { for (i in refs) printf "%s|%s\n", i, refs[i] }' \
    "$PLAN_RAW" 2>/dev/null | sort > "$PLAN"

log "计划删除 ${PLANNED} 个旧标签"
removed=0
failed=0

if [ "$DRY_RUN" = "1" ]; then
    while IFS='|' read -r img_id refs; do
        [ -z "$refs" ] && continue
        log "would remove [${img_id}]: $refs"
    done < "$PLAN"
else
    while IFS='|' read -r img_id refs; do
        [ -z "$refs" ] && continue
        read -r -a ref_arr <<< "$refs"
        out=$("$DOCKER" image rm "${ref_arr[@]}" 2>&1)
        rc=$?
        # rc!=0 也可能是部分成功（层被并发删除等），以引用是否仍存在为准
        still=0
        for r in "${ref_arr[@]}"; do
            "$DOCKER" image inspect "$r" >/dev/null 2>&1 && still=$((still + 1))
        done
        if [ "$rc" -eq 0 ] || [ "$still" -eq 0 ]; then
            removed=$((removed + ${#ref_arr[@]}))
            log "removed ${#ref_arr[@]} tag(s) [${img_id}]: ${ref_arr[*]}"
        else
            failed=$((failed + still))
            log "WARN: 删除失败 [${img_id}] 仍剩 ${still}/${#ref_arr[@]}: $out"
        fi
    done < "$PLAN"

    if ! pout=$("$DOCKER" image prune -f 2>&1); then
        log "WARN: image prune 失败: $(printf '%s' "$pout" | grep -v 'Total reclaimed' | tail -1)"
    fi
    if ! bout=$("$DOCKER" builder prune -f 2>&1); then
        log "WARN: builder prune 失败: $(printf '%s' "$bout" | grep -v '^Total:' | tail -1)"
    fi
    # journald vacuum 必须 root；以前 2>/dev/null 把权限失败吞了，手工跑“清理”
    # 实际什么都没发生。现在失败留 WARN。
    if command -v journalctl >/dev/null 2>&1; then
        if jout=$(journalctl --vacuum-size="$JOURNAL_MAX" 2>&1); then
            log "journal vacuum: $(printf '%s\n' "$jout" | tail -1)"
        else
            log "WARN: journal vacuum 失败: $(printf '%s\n' "$jout" | tail -1)"
        fi
    fi
fi

log "已删除标签 ${removed} 个，失败 ${failed} 个"
log "清理后磁盘: $(df -h / | tail -1)"
"$DOCKER" system df

[ "$failed" -eq 0 ] || exit 1
