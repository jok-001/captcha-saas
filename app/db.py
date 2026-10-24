"""SQLite 数据层。所有写入用全局锁串行化，避免多 worker 并发写冲突。"""
import os
import sqlite3
import threading
import json
import time

import config

_db_lock = threading.RLock()  # 可重入锁：create_user/redeem_code 会嵌套获取
_conn_local = threading.local()


def get_conn():
    if not hasattr(_conn_local, "conn"):
        conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _conn_local.conn = conn
    return _conn_local.conn


def init_db():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT,
        password_hash TEXT NOT NULL,
        credits INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER NOT NULL,
        last_used_at INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS pricing (
        type TEXT PRIMARY KEY,
        points INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 0,
        note TEXT
    );
    CREATE TABLE IF NOT EXISTS usage_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        token_id INTEGER,
        type TEXT,
        points INTEGER NOT NULL DEFAULT 0,
        success INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER,
        detail TEXT,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        change INTEGER NOT NULL,
        balance_after INTEGER NOT NULL,
        reason TEXT,
        by_user_id INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        points INTEGER NOT NULL,
        used_by INTEGER,
        used_at INTEGER,
        created_by INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_logs(user_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
    CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, id);
    """)
    conn.commit()
    # 迁移：老库补 nickname 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "nickname" not in cols:
        try:
            conn.execute("ALTER TABLE users ADD COLUMN nickname TEXT")
        except sqlite3.OperationalError:
            pass
    # 迁移：redeem_codes 补 note 列
    rcols = [r[1] for r in conn.execute("PRAGMA table_info(redeem_codes)")]
    if "note" not in rcols:
        try:
            conn.execute("ALTER TABLE redeem_codes ADD COLUMN note TEXT")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    seed()


def seed():
    conn = get_conn()
    cur = conn.cursor()
    now = int(time.time())
    # 管理员
    cur.execute("SELECT id FROM users WHERE username=?", (config.INIT_ADMIN_USERNAME,))
    if not cur.fetchone():
        from werkzeug.security import generate_password_hash
        cur.execute(
            "INSERT INTO users(username,email,password_hash,credits,role,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (config.INIT_ADMIN_USERNAME, config.INIT_ADMIN_EMAIL,
             generate_password_hash(config.INIT_ADMIN_PASSWORD),
             config.OWNER_CREDITS, "admin", "active", now, now))
        admin_id = cur.lastrowid
        # 兼容旧 owner token（仅当配置了 OWNER_TOKEN 时才创建）
        if config.OWNER_TOKEN:
            cur.execute("SELECT id FROM tokens WHERE token=?", (config.OWNER_TOKEN,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO tokens(user_id,token,note,status,created_at) VALUES(?,?,?,?,?)",
                    (admin_id, config.OWNER_TOKEN, "owner(兼容旧token)", "active", now))
    # 定价
    for t, v in config.DEFAULT_PRICING.items():
        cur.execute("SELECT type FROM pricing WHERE type=?", (t,))
        if not cur.fetchone():
            cur.execute("INSERT INTO pricing(type,points,enabled,note) VALUES(?,?,?,?)",
                        (t, v["points"], v["enabled"], v["note"]))
    # 迁移 1：曾标记"规划中"但已实际交付的类型，升级为正式启用（2026-10-25 四平台接入）
    cur.execute(
        "UPDATE pricing SET enabled=1, note=? WHERE type=? AND note LIKE '%规划中%'",
        ("数美智能验证码（空间推理/文字点选/图标点选，mode 参数切换）", "shumei"))
    # 迁移 2：同步已存在类型的说明文案（管理员在后台改过的单价/启停保持不变）
    for t, v in config.DEFAULT_PRICING.items():
        cur.execute("UPDATE pricing SET note=? WHERE type=?", (v["note"], t))
    # 迁移 3：geetest4 已并入 geetestV4（同一极验4，只是内部按题型分派），
    #         历史用量/流水一并归并，避免管理后台出现查无此类型的孤儿记录
    cur.execute("UPDATE usage_logs SET type='geetestV4' WHERE type='geetest4'")
    cur.execute("DELETE FROM pricing WHERE type IN ('geetest4', 'geetest')")
    # 管理员初始积分流水（新建或历史库一次性补记）
    admin_row = cur.execute("SELECT id,credits FROM users WHERE username=?",
                            (config.INIT_ADMIN_USERNAME,)).fetchone()
    if admin_row:
        cnt = cur.execute("SELECT COUNT(*) c FROM transactions WHERE user_id=?",
                          (admin_row["id"],)).fetchone()["c"]
        if cnt == 0:
            cur.execute(
                "INSERT INTO transactions(user_id,change,balance_after,reason,by_user_id,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (admin_row["id"], admin_row["credits"], admin_row["credits"], "初始充值", None, now))
    conn.commit()


# ---------- 通用 ----------
def q(sql, args=()):
    conn = get_conn()
    return conn.execute(sql, args).fetchall()


def qone(sql, args=()):
    conn = get_conn()
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def execute(sql, args=()):
    with _db_lock:
        conn = get_conn()
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


# ---------- 用户 ----------
def create_user(username, email, password, role="user", credits=None, by_user_id=None):
    """创建用户。credits 为 None 时按 SIGNUP_BONUS 赠送并记流水；管理员建号可指定初始积分。注册即生成唯一 token。"""
    from werkzeug.security import generate_password_hash
    now = int(time.time())
    init_credits = config.SIGNUP_BONUS if credits is None else credits
    with _db_lock:
        uid = execute(
            "INSERT INTO users(username,email,password_hash,credits,role,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (username, email, generate_password_hash(password), init_credits,
             role, "active", now, now))
        reason = "注册赠送" if credits is None else "管理员创建账号"
        add_transaction(uid, init_credits, reason, by_user_id)
        reset_token(uid)  # 注册即有唯一 API token
        return uid


def get_user(uid):
    return qone("SELECT * FROM users WHERE id=?", (uid,))


def get_user_by_username(username):
    return qone("SELECT * FROM users WHERE username=?", (username,))


def authenticate(username, password):
    from werkzeug.security import check_password_hash
    u = get_user_by_username(username)
    if not u:
        return None
    if u["status"] != "active":
        return None
    if not check_password_hash(u["password_hash"], password):
        return None
    return u


def change_password(uid, new_password):
    from werkzeug.security import generate_password_hash
    execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (generate_password_hash(new_password), int(time.time()), uid))


def update_profile(uid, email=None):
    """更新邮箱。传 None 表示不修改。"""
    sets, args = [], []
    if email is not None:
        sets.append("email=?")
        args.append(email)
    if not sets:
        return
    sets.append("updated_at=?")
    args.append(int(time.time()))
    args.append(uid)
    execute("UPDATE users SET %s WHERE id=?" % ",".join(sets), args)


def set_user_status(uid, status):
    execute("UPDATE users SET status=?, updated_at=? WHERE id=?", (status, int(time.time()), uid))


def delete_user(uid):
    """删除用户及其关联数据（tokens/流水/用量），兑换码占用记录解除。"""
    with _db_lock:
        conn = get_conn()
        conn.execute("DELETE FROM usage_logs WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM transactions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
        conn.execute("UPDATE redeem_codes SET used_by=NULL, used_at=NULL WHERE used_by=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()


def adjust_credits(uid, delta, reason, by_user_id=None):
    """增减积分并记流水。delta 可为负。返回操作后余额。"""
    with _db_lock:
        u = get_user(uid)
        if not u:
            raise ValueError("用户不存在")
        new_balance = u["credits"] + delta
        if new_balance < 0:
            raise ValueError("积分不能为负")
        conn = get_conn()
        conn.execute("UPDATE users SET credits=?, updated_at=? WHERE id=?",
                     (new_balance, int(time.time()), uid))
        conn.execute(
            "INSERT INTO transactions(user_id,change,balance_after,reason,by_user_id,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (uid, delta, new_balance, reason, by_user_id, int(time.time())))
        conn.commit()
        return new_balance


def add_transaction(uid, change, reason, by_user_id):
    conn = get_conn()
    u = get_user(uid)
    conn.execute(
        "INSERT INTO transactions(user_id,change,balance_after,reason,by_user_id,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (uid, change, u["credits"] if u else 0, reason, by_user_id, int(time.time())))
    conn.commit()


# ---------- Token ----------
def create_token(uid, note=""):
    import secrets
    tok = secrets.token_hex(16)
    now = int(time.time())
    execute("INSERT INTO tokens(user_id,token,note,status,created_at) VALUES(?,?,?,?,?)",
            (uid, tok, note, "active", now))
    return tok


def list_tokens(uid):
    return [dict(r) for r in q("SELECT * FROM tokens WHERE user_id=? ORDER BY id DESC", (uid,))]


def get_token_row(token_str):
    return qone("SELECT * FROM tokens WHERE token=?", (token_str,))


def set_token_status(tid, status):
    execute("UPDATE tokens SET status=? WHERE id=?", (status, tid))


def delete_token(tid):
    with _db_lock:
        conn = get_conn()
        conn.execute("DELETE FROM usage_logs WHERE token_id=?", (tid,))
        conn.execute("DELETE FROM tokens WHERE id=?", (tid,))
        conn.commit()


def touch_token(tid):
    execute("UPDATE tokens SET last_used_at=? WHERE id=?", (int(time.time()), tid))


def get_user_token(uid):
    """单 token 语义：返回该用户当前启用的唯一 token 行。"""
    return qone("SELECT * FROM tokens WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1", (uid,))


def reset_token(uid):
    """重置用户唯一 token：旧值失效，返回新 token（若无则创建）。"""
    import secrets
    tok = secrets.token_hex(16)
    now = int(time.time())
    with _db_lock:
        row = qone("SELECT id FROM tokens WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
        if row:
            execute("UPDATE tokens SET token=?, status='active', created_at=?, last_used_at=NULL, note=? WHERE id=?",
                    (tok, now, "个人中心重置", row["id"]))
        else:
            execute("INSERT INTO tokens(user_id,token,note,status,created_at) VALUES(?,?,?,?,?)",
                    (uid, tok, "", "active", now))
        # 防御：确保该用户只剩一个启用 token
        conn = get_conn()
        conn.execute("UPDATE tokens SET status='disabled' WHERE user_id=? AND token<>?", (uid, tok))
        conn.commit()
    return tok


# ---------- 定价 ----------
def get_pricing():
    return [dict(r) for r in q("SELECT * FROM pricing ORDER BY type")]


def get_price(type_name):
    r = qone("SELECT * FROM pricing WHERE type=?", (type_name,))
    return dict(r) if r else None


def upsert_price(type_name, points, enabled, note=""):
    with _db_lock:
        conn = get_conn()
        cur = conn.execute("SELECT type FROM pricing WHERE type=?", (type_name,))
        if cur.fetchone():
            conn.execute("UPDATE pricing SET points=?,enabled=?,note=? WHERE type=?",
                         (points, enabled, note, type_name))
        else:
            conn.execute("INSERT INTO pricing(type,points,enabled,note) VALUES(?,?,?,?)",
                         (type_name, points, enabled, note))
        conn.commit()


def delete_price(type_name):
    execute("DELETE FROM pricing WHERE type=?", (type_name,))


# ---------- 用量 ----------
def log_usage(user_id, token_id, type_name, points, success, duration_ms, detail=None):
    execute(
        "INSERT INTO usage_logs(user_id,token_id,type,points,success,duration_ms,detail,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (user_id, token_id, type_name, points, 1 if success else 0, duration_ms,
         json.dumps(detail, ensure_ascii=False) if detail else None, int(time.time())))


def usage_stats(user_id=None, type_filter=None, success_filter=None,
                keyword=None, date_from=None, date_to=None,
                limit=50, offset=0):
    where = []
    args = []
    if user_id is not None:
        where.append("user_id=?")
        args.append(user_id)
    if type_filter:
        where.append("type=?")
        args.append(type_filter)
    if success_filter is not None and success_filter != "":
        where.append("success=?")
        args.append(int(success_filter))
    if keyword:
        kw = "%%%s%%" % keyword
        where.append("(IFNULL(type,'') LIKE ? OR IFNULL(detail,'') LIKE ?)")
        args.extend([kw, kw])
    if date_from:
        try:
            import datetime as _dt
            ts = int(_dt.datetime.strptime(date_from, "%Y-%m-%d").timestamp())
            where.append("created_at>=?")
            args.append(ts)
        except Exception:
            pass
    if date_to:
        try:
            import datetime as _dt
            ts = int(_dt.datetime.strptime(date_to, "%Y-%m-%d").timestamp()) + 86399
            where.append("created_at<=?")
            args.append(ts)
        except Exception:
            pass
    w = ("WHERE " + " AND ".join(where)) if where else ""
    rows = q("SELECT * FROM usage_logs %s ORDER BY id DESC LIMIT ? OFFSET ?" % w,
             tuple(args) + (limit, offset))
    total = qone("SELECT COUNT(*) c FROM usage_logs %s" % w, tuple(args))["c"]
    return [dict(r) for r in rows], total


def usage_summary(user_id=None):
    where = "WHERE user_id=?" if user_id is not None else ""
    args = [user_id] if user_id is not None else []
    total = qone("SELECT COUNT(*) c, COALESCE(SUM(points),0) p, "
                 "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) s FROM usage_logs %s" % where, args)
    by_type = q("SELECT type, COUNT(*) c, COALESCE(SUM(points),0) p, "
                "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) s FROM usage_logs %s GROUP BY type" % where, args)
    return dict(total), [dict(r) for r in by_type]


# ---------- 兑换码 ----------
def create_redeem_code(points, by_user_id, count=1, note=""):
    import secrets
    codes = []
    with _db_lock:
        conn = get_conn()
        for _ in range(count):
            code = secrets.token_hex(8).upper()
            conn.execute(
                "INSERT OR IGNORE INTO redeem_codes(code,points,created_by,created_at,note)"
                " VALUES(?,?,?,?,?)",
                (code, points, by_user_id, int(time.time()), note))
            codes.append(code)
        conn.commit()
    return codes


def redeem_code(code, uid):
    with _db_lock:
        r = qone("SELECT * FROM redeem_codes WHERE code=?", (code.upper(),))
        if not r:
            return False, "兑换码不存在"
        if r["used_by"]:
            return False, "兑换码已被使用"
        new_balance = adjust_credits(uid, r["points"], "兑换码 %s" % r["code"], None)
        conn = get_conn()
        conn.execute("UPDATE redeem_codes SET used_by=?, used_at=? WHERE code=?",
                     (uid, int(time.time()), r["code"]))
        conn.commit()
        return True, new_balance


def list_redeem_codes(status=None, used_by=None, keyword=None, limit=30, offset=0):
    """兑换码列表（含创建者/使用者用户名）。status: 'used'/'unused'/None。keyword 模糊匹配 code/备注/创建者/使用者用户名。"""
    where, args = [], []
    if status == "used":
        where.append("r.used_by IS NOT NULL")
    elif status == "unused":
        where.append("r.used_by IS NULL")
    if used_by:
        where.append("r.used_by=?")
        args.append(int(used_by))
    if keyword:
        kw = "%%%s%%" % keyword
        where.append("(r.code LIKE ? OR IFNULL(r.note,'') LIKE ? OR IFNULL(cu.username,'') LIKE ? OR IFNULL(uu.username,'') LIKE ?)")
        args.extend([kw, kw, kw, kw])
    w = ("WHERE " + " AND ".join(where)) if where else ""
    sql = ("SELECT r.code,r.points,r.note,r.created_at,r.used_at,r.used_by,r.created_by,"
           "cu.username AS created_by_name,uu.username AS used_by_name "
           "FROM redeem_codes r "
           "LEFT JOIN users cu ON r.created_by=cu.id "
           "LEFT JOIN users uu ON r.used_by=uu.id "
           "%s ORDER BY r.created_at DESC LIMIT ? OFFSET ?" % w)
    rows = q(sql, tuple(args) + (limit, offset))
    total = qone("SELECT COUNT(*) c FROM redeem_codes r "
                 "LEFT JOIN users cu ON r.created_by=cu.id "
                 "LEFT JOIN users uu ON r.used_by=uu.id "
                 "%s" % w, tuple(args))["c"]
    return [dict(r) for r in rows], total


def delete_redeem_codes(codes):
    """批量删除兑换码（按 code 列表）。"""
    if not codes:
        return 0
    with _db_lock:
        conn = get_conn()
        placeholders = ",".join("?" * len(codes))
        cur = conn.execute("DELETE FROM redeem_codes WHERE code IN (%s)" % placeholders,
                           tuple(str(c) for c in codes))
        conn.commit()
        return cur.rowcount


# ---------- 积分流水 ----------
def list_transactions(uid=None, keyword=None, change_type=None,
                      date_from=None, date_to=None,
                      limit=30, offset=0):
    """积分流水。uid 为 None 时返回全局流水（管理员用）。
    keyword 模糊搜索用户名/原因/操作者。change_type: 'in'/'out'/None。
    date_from/date_to: 'YYYY-MM-DD' 字符串。
    """
    if uid is not None:
        where, args = [], []
        if keyword:
            kw = "%%%s%%" % keyword
            where.append("(IFNULL(reason,'') LIKE ?)")
            args.append(kw)
        if change_type == "in":
            where.append("change>0")
        elif change_type == "out":
            where.append("change<0")
        if date_from:
            try:
                import datetime as _dt
                ts = int(_dt.datetime.strptime(date_from, "%Y-%m-%d").timestamp())
                where.append("created_at>=?")
                args.append(ts)
            except Exception:
                pass
        if date_to:
            try:
                import datetime as _dt
                ts = int(_dt.datetime.strptime(date_to, "%Y-%m-%d").timestamp()) + 86399
                where.append("created_at<=?")
                args.append(ts)
            except Exception:
                pass
        w = ("WHERE user_id=? AND " + " AND ".join(where)) if where else ("WHERE user_id=?")
        rows = q("SELECT * FROM transactions %s ORDER BY id DESC LIMIT ? OFFSET ?" % w,
                 (uid,) + tuple(args) + (limit, offset))
        total = qone("SELECT COUNT(*) c FROM transactions %s" % w, (uid,) + tuple(args))["c"]
    else:
        where, args = [], []
        if keyword:
            kw = "%%%s%%" % keyword
            where.append("(IFNULL(u.username,'') LIKE ? OR IFNULL(t.reason,'') LIKE ? OR IFNULL(a.username,'') LIKE ?)")
            args.extend([kw, kw, kw])
        if change_type == "in":
            where.append("t.change>0")
        elif change_type == "out":
            where.append("t.change<0")
        if date_from:
            try:
                import datetime as _dt
                ts = int(_dt.datetime.strptime(date_from, "%Y-%m-%d").timestamp())
                where.append("t.created_at>=?")
                args.append(ts)
            except Exception:
                pass
        if date_to:
            try:
                import datetime as _dt
                ts = int(_dt.datetime.strptime(date_to, "%Y-%m-%d").timestamp()) + 86399
                where.append("t.created_at<=?")
                args.append(ts)
            except Exception:
                pass
        w = ("WHERE " + " AND ".join(where)) if where else ""
        sql = ("SELECT t.* FROM transactions t "
               "LEFT JOIN users u ON t.user_id=u.id "
               "LEFT JOIN users a ON t.by_user_id=a.id "
               "%s ORDER BY t.id DESC LIMIT ? OFFSET ?" % w)
        rows = q(sql, tuple(args) + (limit, offset))
        total = qone("SELECT COUNT(*) c FROM transactions t "
                     "LEFT JOIN users u ON t.user_id=u.id "
                     "LEFT JOIN users a ON t.by_user_id=a.id "
                     "%s" % w, tuple(args))["c"]
    return [dict(r) for r in rows], total
