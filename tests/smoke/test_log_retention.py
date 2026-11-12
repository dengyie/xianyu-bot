"""风控日志保留策略：只保留 N 天，且写日志时按小时门限触发清理。"""

import datetime as dt

import reply_server


def _insert_risk_log(db, cookie_id, days_ago):
    created_at = (dt.datetime.now() - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.lock:
        cursor = db.conn.cursor()
        cursor.execute(
            "INSERT INTO risk_control_logs (cookie_id, event_type, created_at) VALUES (?, ?, ?)",
            (cookie_id, "retention_test", created_at),
        )
        db.conn.commit()


def _count(db, cookie_id):
    with db.lock:
        return db.conn.execute(
            "SELECT COUNT(*) FROM risk_control_logs WHERE cookie_id = ?", (cookie_id,)
        ).fetchone()[0]


def test_cleanup_risk_control_logs_deletes_only_expired_rows():
    db = reply_server.db_manager
    cid = "retention-unit-test"
    _insert_risk_log(db, cid, 100)
    _insert_risk_log(db, cid, 89)
    try:
        deleted = db.cleanup_risk_control_logs(90)
        assert deleted >= 1
        # 100 天前的那条没了，89 天前的那条还在
        with db.lock:
            remaining = db.conn.execute(
                "SELECT created_at FROM risk_control_logs WHERE cookie_id = ?", (cid,)
            ).fetchall()
        assert len(remaining) == 1
        assert remaining[0][0] >= (dt.datetime.now() - dt.timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    finally:
        with db.lock:
            db.conn.execute("DELETE FROM risk_control_logs WHERE cookie_id = ?", (cid,))
            db.conn.commit()


def test_retention_disabled_keeps_everything():
    db = reply_server.db_manager
    cid = "retention-disabled-test"
    _insert_risk_log(db, cid, 400)
    try:
        assert db.cleanup_risk_control_logs(0) == 0
        assert _count(db, cid) == 1
    finally:
        with db.lock:
            db.conn.execute("DELETE FROM risk_control_logs WHERE cookie_id = ?", (cid,))
            db.conn.commit()


def test_write_path_triggers_hourly_cleanup():
    """写日志是最频繁的动作，也是清理触发点；门限内不应重复扫描。"""
    db = reply_server.db_manager
    cid = "retention-hook-test"
    _insert_risk_log(db, cid, 120)
    try:
        db._last_risk_log_cleanup_at = 0
        db.add_risk_control_log(cid, event_type="retention_test")
        assert _count(db, cid) == 1  # 120 天前的旧行被清掉，只剩刚写的一条

        # 门限内再写：旧行不该再被处理，也不应重复全表扫描
        db._last_risk_log_cleanup_at  # 已由上次写入更新为 now
        _insert_risk_log(db, cid, 120)
        db.add_risk_control_log(cid, event_type="retention_test")
        assert _count(db, cid) == 3
    finally:
        with db.lock:
            db.conn.execute("DELETE FROM risk_control_logs WHERE cookie_id = ?", (cid,))
            db.conn.commit()
        db._last_risk_log_cleanup_at = 0.0
