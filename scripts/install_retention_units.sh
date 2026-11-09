#!/usr/bin/env bash
# ============================================================
# 安装/更新 VPS 侧镜像保留自动化（幂等，可重复执行）
# ============================================================
# 内容：
#   1) 校验脚本本体 /usr/local/sbin/docker-retention.sh 已就位
#      （CI 与手动都应先把 scripts/docker_retention.sh 同步过去）
#   2) systemd service + timer（每日 05:30 本地时间，0-30min 随机抖动，
#      Persistent 补跑错过的轮次）
#   3) journald 封顶（SystemMaxUse=500M / SystemKeepFree=1G）——
#      默认按卷 10% 封顶，40G 盘会涨到 4G
#
# 文件内容有变化才 daemon-reload / restart journald，重复执行无副作用。
# ============================================================

set -euo pipefail

SCRIPT_PATH=/usr/local/sbin/docker-retention.sh
UNIT_DIR=/etc/systemd/system
JOURNAL_CONF_DIR=/etc/systemd/journald.conf.d

[ "$(id -u)" -eq 0 ] || { echo "ERROR: 需要 root"; exit 1; }
[ -x "$SCRIPT_PATH" ] || { echo "ERROR: 缺少 $SCRIPT_PATH（先同步 scripts/docker_retention.sh）"; exit 1; }

changed_units=0
changed_journal=0

# write_file <dest> ：stdin 写入 dest，内容与现文件一致则跳过
write_file() {
    local dest=$1 tmp
    tmp=$(mktemp)
    cat > "$tmp"
    mkdir -p "$(dirname "$dest")"
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
        rm -f "$tmp"
        return 1
    fi
    install -m 0644 "$tmp" "$dest"
    rm -f "$tmp"
    return 0
}

if write_file "$UNIT_DIR/docker-retention.service" <<'UNIT'; then changed_units=1; fi
[Unit]
Description=Docker 镜像保留策略清理（旧 sha 标签 / dangling / build cache / journal）
Documentation=https://github.com/dengyie/xianyu-auto-bot/blob/main/scripts/docker_retention.sh
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/docker-retention.sh
Nice=10
IOSchedulingClass=idle
UNIT

if write_file "$UNIT_DIR/docker-retention.timer" <<'UNIT'; then changed_units=1; fi
[Unit]
Description=每日 Docker 镜像保留清理（05:30 本地时间）

[Timer]
OnCalendar=05:30
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

if write_file "$JOURNAL_CONF_DIR/90-retention.conf" <<'CONF'; then changed_journal=1; fi
# 2026-11-09 磁盘治理：journald 默认按卷的 10% 封顶（40G 盘 -> 4G），
# 实测曾涨到 3.9G。压到 500M，并保留 1G 空闲余量。
[Journal]
SystemMaxUse=500M
SystemKeepFree=1G
CONF

if [ "$changed_units" -eq 1 ]; then
    systemctl daemon-reload
    echo "updated: systemd units"
fi
if [ "$changed_journal" -eq 1 ]; then
    systemctl restart systemd-journald
    echo "updated: journald retention conf"
fi

systemctl enable --now docker-retention.timer
systemctl list-timers docker-retention.timer --no-pager | sed -n '1,2p'
echo "install done (units_changed=$changed_units journal_changed=$changed_journal)"
