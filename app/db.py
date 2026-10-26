"""SQLite 数据层。

并发模型（重要）：
- 生产是 `gunicorn -w 2 -k gthread --threads 8` → 2 个**进程** × 8 线程。
- `_db_lock` 是模块级 RLock，**只在单进程内有效，跨 worker 等于不存在**。
- 所以正确性只能靠「SQLite 唯一索引 + IntegrityError 仲裁 + 单事务原子性」，
  任何「先 SELECT 判断再 INSERT」的写法在 2 进程下都是错的（TOCTOU）。
- `_db_lock` 只用来降低同进程内的写冲突和 "database is locked" 概率。
"""
import os
import sqlite3
import threading
import json
import time
import datetime

import config

_db_lock = threading.RLock()  # 可重入锁：create_user/redeem_code 会嵌套获取
_conn_local = threading.local()

# 签到日期 / 订单号日期一律用固定 UTC+8，不依赖服务器时区
# （腾讯云机器常为 UTC，用本地时区会让北京时间 08:00 出现"双签窗口"）
_TZ_CST = datetime.timezone(datetime.timedelta(hours=8))


def now_ts():
    return int(time.time())


def today_str():
    """当前北京时间的日期字符串，如 '2026-10-25'。"""
    return datetime.datetime.now(_TZ_CST).strftime("%Y-%m-%d")


def get_conn():
    if not hasattr(_conn_local, "conn"):
        conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _conn_local.conn = conn
    return _conn_local.conn


# 新列（ALTER 加列不能带 UNIQUE，唯一索引见 _INDEX_DDL）
_MIGRATIONS = [
    ("usage_logs", "order_no",
     "ALTER TABLE usage_logs ADD COLUMN order_no TEXT"),
    ("usage_logs", "charged",
     "ALTER TABLE usage_logs ADD COLUMN charged INTEGER NOT NULL DEFAULT 0"),
    ("usage_logs", "balance_after",
     "ALTER TABLE usage_logs ADD COLUMN balance_after INTEGER"),
    ("transactions", "kind",
     "ALTER TABLE transactions ADD COLUMN kind TEXT NOT NULL DEFAULT 'adjust'"),
    ("transactions", "order_no",
     "ALTER TABLE transactions ADD COLUMN order_no TEXT"),
    ("transactions", "ref",
     "ALTER TABLE transactions ADD COLUMN ref TEXT"),
]

# 依赖新列的索引：必须在 ALTER 之后建
_INDEX_DDL = [
    # order_no 为 NULL 的行不受唯一约束限制（SQLite 中 NULL 互不相等），历史数据安全。
    # 因此「没有订单号」一律存 NULL，绝不能存空串。
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_order_no ON usage_logs(order_no)",
    "CREATE INDEX IF NOT EXISTS idx_usage_order ON usage_logs(order_no)",
    "CREATE INDEX IF NOT EXISTS idx_tx_order ON transactions(order_no)",
    "CREATE INDEX IF NOT EXISTS idx_tx_user_time ON transactions(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tx_kind ON transactions(user_id, kind, created_at)",
]


def _exec_with_retry(conn, sql, args=(), retries=3):
    """单条语句执行，遇到 "database is locked" 短暂重试（2 个 worker 同时迁移时会发生）。"""
    for i in range(retries):
        try:
            return conn.execute(sql, args)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < retries - 1:
                time.sleep(0.3 * (i + 1))
                continue
            raise


def _script_with_retry(conn, script, retries=3):
    for i in range(retries):
        try:
            conn.executescript(script)
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < retries - 1:
                time.sleep(0.3 * (i + 1))
                continue
            raise


def init_db():
    """建表 + 迁移 + 播种。**必须能在多 worker 同时启动时安全并发执行**。

    实测（4 进程同时 init 空库）会撞两类错误：
      - OperationalError: attempt to write a readonly database（并发切 WAL / 建文件）
      - IntegrityError: UNIQUE constraint failed: users.username（seed 先查后插）
    前者靠「重连 + 指数退避重试」，后者靠 seed() 里的 INSERT OR IGNORE / try-except。
    绝不能让这里抛异常——那会让 gunicorn worker 起不来，全站 502。
    """
    os.makedirs(config.DATA_DIR, exist_ok=True)
    last = None
    for i in range(5):
        try:
            _init_db_once()
            return
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if not any(k in msg for k in ("locked", "readonly", "busy")):
                raise
            last = e
            # 卡住的连接必须丢弃重建，否则后续重试仍然失败
            try:
                if hasattr(_conn_local, "conn"):
                    _conn_local.conn.close()
            except Exception:
                pass
            if hasattr(_conn_local, "conn"):
                del _conn_local.conn
            time.sleep(0.4 * (i + 1))
    raise last


def _init_db_once():
    conn = get_conn()
    cur = conn.cursor()
    _script_with_retry(conn, """
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
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at INTEGER NOT NULL DEFAULT 0,
        updated_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        checkin_date TEXT NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        transaction_id INTEGER,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_logs(user_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
    CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_checkins_user_date ON checkins(user_id, checkin_date);
    CREATE INDEX IF NOT EXISTS idx_checkins_user ON checkins(user_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_checkins_date ON checkins(checkin_date);
    """)
    conn.commit()
    _migrate(conn)
    seed()


def _migrate(conn):
    """结构与数据迁移。必须可重复执行：init_db() 在每次 create_app 都会跑，
    且 gunicorn 2 个 worker 会**同时**跑（并发 ALTER 有 ~50% 概率撞车）。"""
    # ── 步骤 1：加列（列存在性预判 + 异常兜底，双保险）──
    for table, col, ddl in _MIGRATIONS:
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        except sqlite3.OperationalError:
            continue
        if col in cols:
            continue
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            # "duplicate column name" = 另一个 worker 抢先建好了；"locked" = 并发写锁
            if "duplicate column" not in msg and "locked" not in msg:
                raise

    # ── 步骤 2：老库补列（原有逻辑保留）──
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "nickname" not in cols:
        try:
            conn.execute("ALTER TABLE users ADD COLUMN nickname TEXT")
        except sqlite3.OperationalError:
            pass
    rcols = [r[1] for r in conn.execute("PRAGMA table_info(redeem_codes)")]
    if "note" not in rcols:
        try:
            conn.execute("ALTER TABLE redeem_codes ADD COLUMN note TEXT")
        except sqlite3.OperationalError:
            pass

    # ── 步骤 3：建索引（依赖新列，必须在 ALTER 之后）──
    for ddl in _INDEX_DDL:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "exists" not in msg:
                raise

    # ── 步骤 4：数据回填（每条都是幂等 UPDATE，重复执行结果不变）──
    # 现有 api.py 只在 success 分支扣分并写 transactions，
    # 所以「success=1」与「已产生流水行」是等价集合，不会漏也不会多。
    conn.execute("UPDATE usage_logs SET charged=1 WHERE charged=0 AND success=1")
    conn.execute("UPDATE transactions SET kind='solve'  WHERE kind='adjust' AND reason LIKE 'api:%'")
    conn.execute("UPDATE transactions SET kind='redeem' WHERE kind='adjust' AND reason LIKE '兑换码%'")
    conn.execute("UPDATE transactions SET kind='signup' WHERE kind='adjust' AND reason='注册赠送'")
    # 充值类流水（历史「初始充值」等正向调整）归到 adjust，展示层再按 change 正负区分「积分充值」
    conn.commit()
    _backfill_legacy_order_no(conn)


def _legacy_order_no(ts):
    """为历史打码事件补一个订单号：CS + 事件当天日期(UTC+8) + 10 位 hex。"""
    import secrets
    day = datetime.datetime.fromtimestamp(int(ts), _TZ_CST).strftime("%Y%m%d")
    for _ in range(3):
        no = "%s%s%s" % (ORDER_NO_PREFIX, day, secrets.token_hex(5).upper())
        if not conn_has_order_no(no):
            return no
    return "%s%s%s" % (ORDER_NO_PREFIX, day, secrets.token_hex(8).upper())


def conn_has_order_no(no):
    return bool(qone("SELECT 1 AS x FROM usage_logs WHERE order_no=?", (no,)))


_BACKFILL_FLAG = "migrated_legacy_order_no"


def _backfill_legacy_order_no(conn):
    """给老库里「已扣费但没有订单号」的打码事件补号，使合并页能显示类型/耗时。

    **只跑一次，且多 worker 只跑一个**：用 settings 里的标记位做原子认领
    （INSERT OR IGNORE + rowcount 判定），认领成功的那一个 worker 才执行，
    其余直接跳过，避免 2 个 worker 同时给同一行写不同的号导致两表不一致。

    安全性：
      - 只处理 order_no 为 NULL/'' 的行，**绝不覆盖已有的真实订单号**
      - 只在候选**唯一**时配对（同用户、成功、积分相等、时间差 ≤5 秒），宁可漏配也不错配
      - 候选不唯一或中途异常，该行保持原样（列表里仍可见，只是没有耗时/类型）
      - 标记位先认领后干活（best-effort）：万一半途失败不会反复重试，属有意取舍
    """
    try:
        if conn.execute("SELECT 1 AS x FROM settings WHERE key=?",
                        (_BACKFILL_FLAG,)).fetchone():
            return
        cur = conn.execute(
            "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
            (_BACKFILL_FLAG, "1", now_ts()))
        conn.commit()
        if cur.rowcount == 0:   # 另一个 worker 抢先认领
            return
    except sqlite3.OperationalError:
        return
    def _pair(window):
        """在一批未补号的 transactions 里，按「候选唯一」配对 usage_logs。
        window=0 表示要求 created_at 完全相等。返回本次配对成功条数。"""
        rows = conn.execute(
            "SELECT t.id AS id, t.user_id AS user_id, t.created_at AS created_at, "
            "-t.change AS pts FROM transactions t "
            "WHERE t.kind='solve' AND (t.order_no IS NULL OR t.order_no='')").fetchall()
        paired = 0
        for tx in rows:
            cands = conn.execute(
                "SELECT id FROM usage_logs WHERE user_id=? AND success=1 AND points=? "
                "AND (order_no IS NULL OR order_no='') AND created_at BETWEEN ? AND ?",
                (tx["user_id"], tx["pts"],
                 tx["created_at"] - window, tx["created_at"] + window)).fetchall()
            if len(cands) != 1:
                continue
            no = _legacy_order_no(tx["created_at"])
            conn.execute("UPDATE usage_logs SET order_no=?, charged=1 WHERE id=?",
                         (no, cands[0]["id"]))
            conn.execute("UPDATE transactions SET order_no=? WHERE id=?", (no, tx["id"]))
            paired += 1
        return paired

    try:
        # 两轮：先精确同秒（api.py 里两条记录用同一个 int(time.time())，命中率最高，
        # 且能正确处理「同一用户每秒一次」的高频调用），再用 ±5 秒兜底。
        _pair(0)
        _pair(5)
        conn.commit()
    except sqlite3.OperationalError:
        try:
            conn.rollback()
        except Exception:
            pass


def _insert_ignore(cur, sql, args):
    """幂等插入：主键/唯一键冲突时静默跳过（多 worker 并发 seed 的必需手段）。"""
    try:
        cur.execute(sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1), args)
    except sqlite3.IntegrityError:
        pass


def seed():
    conn = get_conn()
    cur = conn.cursor()
    now = int(time.time())
    # 管理员：先查后插在 2 个 worker 并发时必炸（UNIQUE username），
    # 所以插入一律 try/except IntegrityError，插入后再回查 id。
    cur.execute("SELECT id FROM users WHERE username=?", (config.INIT_ADMIN_USERNAME,))
    if not cur.fetchone():
        from werkzeug.security import generate_password_hash
        try:
            cur.execute(
                "INSERT INTO users(username,email,password_hash,credits,role,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (config.INIT_ADMIN_USERNAME, config.INIT_ADMIN_EMAIL,
                 generate_password_hash(config.INIT_ADMIN_PASSWORD),
                 config.OWNER_CREDITS, "admin", "active", now, now))
        except sqlite3.IntegrityError:
            pass  # 另一个 worker 抢先建好了
    admin_row = cur.execute("SELECT id FROM users WHERE username=?",
                            (config.INIT_ADMIN_USERNAME,)).fetchone()
    admin_id = admin_row["id"] if admin_row else None
    # 兼容旧 owner token（仅当配置了 OWNER_TOKEN 时才创建）
    if admin_id and config.OWNER_TOKEN:
        _insert_ignore(cur,
                       "INSERT INTO tokens(user_id,token,note,status,created_at)"
                       " VALUES(?,?,?,?,?)",
                       (admin_id, config.OWNER_TOKEN, "owner(兼容旧token)", "active", now))
    # 定价（type 是主键，INSERT OR IGNORE 天然幂等且保留管理员改过的单价/启停）
    for t, v in config.DEFAULT_PRICING.items():
        _insert_ignore(cur,
                       "INSERT INTO pricing(type,points,enabled,note) VALUES(?,?,?,?)",
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
            # WHERE NOT EXISTS 把并发窗口压到最小（真撞上也只会多一条，不会报错）
            try:
                cur.execute(
                    "INSERT INTO transactions(user_id,change,balance_after,reason,by_user_id,"
                    "created_at,kind) SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS"
                    " (SELECT 1 FROM transactions WHERE user_id=?)",
                    (admin_row["id"], admin_row["credits"], admin_row["credits"],
                     "初始充值", None, now, "adjust", admin_row["id"]))
            except sqlite3.IntegrityError:
                pass
    # 系统配置默认值：只在缺失时写入（INSERT OR IGNORE），**绝不覆盖管理员改过的值**
    for s in config.SETTINGS_SCHEMA:
        cur.execute(
            "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
            (s["key"], str(s["default"]), now))
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


# ---------- 系统配置（settings 键值表） ----------
# 只做「按请求读一次」，绝不进程内长缓存：
# gunicorn 2 个 worker 各有一份内存，缓存会导致后台改了开关另一个 worker 仍走老值。
_TRUE_STRINGS = {"1", "true", "on", "yes", "y"}


def to_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE_STRINGS:
        return True
    if s in {"0", "false", "off", "no", "n", ""}:
        return False
    return default


def to_int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def get_settings():
    """一次查询取全部配置（表只有几行，单条 SELECT 约几十微秒）。"""
    return {r["key"]: r["value"] for r in q("SELECT key,value FROM settings")}


def get_setting(key, default=None):
    row = qone("SELECT value FROM settings WHERE key=?", (key,))
    if not row or row["value"] is None:
        return default
    return row["value"]


def get_setting_bool(key, default=False):
    return to_bool(get_setting(key), default)


def get_setting_int(key, default=0):
    return to_int(get_setting(key), default)


def _coerce_setting(spec, raw):
    """按 schema 校验/转型。返回 (value, error_msg)。"""
    t = spec["type"]
    label = spec.get("label") or spec["key"]
    if t == "bool":
        return (1 if to_bool(raw) else 0), None
    if t == "int":
        try:
            iv = int(str(raw).strip())
        except (TypeError, ValueError):
            return None, "%s 需要是整数" % label
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and iv < lo:
            return None, "%s 不能小于 %s" % (label, lo)
        if hi is not None and iv > hi:
            return None, "%s 不能大于 %s" % (label, hi)
        return iv, None
    if t == "select":
        opts = [str(o["value"]) for o in (spec.get("options") or [])]
        sv = str(raw if raw is not None else "").strip()
        if opts and sv not in opts:
            return None, "%s 取值不在允许范围" % label
        return sv, None
    sv = "" if raw is None else str(raw).strip()
    return sv[: spec.get("max_len", 500)], None


def set_settings(values, by_user_id=None):
    """批量保存配置。全有或全无：任一校验失败则整批不落库。
    返回 (saved_keys, errors)。reserved 的预留项拒绝写入。"""
    schema = {s["key"]: s for s in config.SETTINGS_SCHEMA}
    clean, errors = {}, []
    for k, v in (values or {}).items():
        spec = schema.get(k)
        if not spec:
            errors.append("未知配置项: %s" % k)
            continue
        if spec.get("reserved"):
            errors.append("%s 为预留项，暂不可配置" % (spec.get("label") or k))
            continue
        val, err = _coerce_setting(spec, v)
        if err:
            errors.append(err)
            continue
        clean[k] = val
    if errors:
        return [], errors
    if not clean:
        return [], []
    now = now_ts()
    with _db_lock:
        conn = get_conn()
        for k, val in clean.items():
            cur = conn.execute(
                "UPDATE settings SET value=?, updated_at=?, updated_by=? WHERE key=?",
                (str(val), now, by_user_id, k))
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value,updated_at,updated_by)"
                    " VALUES(?,?,?,?)", (k, str(val), now, by_user_id))
        conn.commit()
    return sorted(clean.keys()), []


def get_settings_view(include_reserved=True):
    """给前端动态渲染的元数据 + 当前值。"""
    vals = get_settings()
    group_labels = dict(config.SETTINGS_GROUPS)
    out = []
    for spec in config.SETTINGS_SCHEMA:
        reserved = bool(spec.get("reserved"))
        if reserved and not include_reserved:
            continue
        raw = vals.get(spec["key"], spec["default"])
        t = spec["type"]
        if t == "bool":
            value = 1 if to_bool(raw, bool(spec["default"])) else 0
        elif t == "int":
            value = to_int(raw, spec["default"])
        else:
            value = raw if raw is not None else spec["default"]
        out.append({
            "key": spec["key"],
            "label": spec.get("label") or spec["key"],
            "type": t,
            "value": value,
            "default": spec["default"],
            "group": spec.get("group", "general"),
            "group_label": group_labels.get(spec.get("group"), "其它"),
            "min": spec.get("min"),
            "max": spec.get("max"),
            "options": spec.get("options"),
            "description": spec.get("description", ""),
            "reserved": reserved,
            "editable": not reserved,
        })
    return out


class _GroupDict(dict):
    """配置分组容器：同时支持 `g['items']` 和 `g.items` 两种写法。

    Jinja 里 `{{ g.items|length }}` 会解析成 dict 的 `.items` **方法**（同名键被方法遮蔽），
    直接报 `object of type 'builtin_function_or_method' has no len()`。
    这里用 property 遮蔽掉同名方法，让两种访问都返回列表。
    """

    @property
    def items(self):
        return self["items"]


def get_settings_groups(include_reserved=True):
    """按 group 聚合，管理页可直接 for 循环渲染。"""
    groups, order = {}, []
    for item in get_settings_view(include_reserved):
        g = item["group"]
        if g not in groups:
            groups[g] = _GroupDict(group=g, group_label=item["group_label"], items=[])
            order.append(groups[g])
        groups[g]["items"].append(item)
    return order


# ---------- 用户 ----------
def create_user(username, email, password, role="user", credits=None, by_user_id=None):
    """创建用户。credits 为 None 时按 settings.signup_bonus 赠送并记流水；
    管理员建号可指定初始积分（kind='adjust'，不受注册赠送配置影响）。
    注册即生成唯一 token。"""
    from werkzeug.security import generate_password_hash
    now = int(time.time())
    is_signup = credits is None
    # 配置项 signup_bonus 是唯一权威；config.SIGNUP_BONUS 仅在 settings 行缺失时兜底
    init_credits = get_setting_int("signup_bonus", config.SIGNUP_BONUS) if is_signup else credits
    with _db_lock:
        uid = execute(
            "INSERT INTO users(username,email,password_hash,credits,role,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (username, email, generate_password_hash(password), init_credits,
             role, "active", now, now))
        if is_signup:
            add_transaction(uid, init_credits, "注册赠送", by_user_id, kind="signup")
        else:
            add_transaction(uid, init_credits, "管理员创建账号", by_user_id, kind="adjust")
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
    """删除用户及其关联数据（tokens/流水/用量/签到），兑换码占用记录解除。

    注意顺序：checkins 声明了 FOREIGN KEY(user_id)->users(id)，而连接上开了
    PRAGMA foreign_keys=ON，所以必须先删 checkins 再删 users，否则抛 IntegrityError。
    """
    with _db_lock:
        conn = get_conn()
        conn.execute("DELETE FROM usage_logs WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM transactions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM checkins WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
        conn.execute("UPDATE redeem_codes SET used_by=NULL, used_at=NULL WHERE used_by=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()


def adjust_credits(uid, delta, reason, by_user_id=None, kind="adjust",
                   order_no=None, ref=None):
    """增减积分并记流水。delta 可为负。返回操作后余额。

    注意：本函数**自己 commit**，所以不能嵌进别的事务里复用（见 do_checkin）。
    """
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
            "INSERT INTO transactions(user_id,change,balance_after,reason,by_user_id,created_at,"
            "kind,order_no,ref) VALUES(?,?,?,?,?,?,?,?,?)",
            (uid, delta, new_balance, reason, by_user_id, int(time.time()),
             kind, order_no, ref))
        conn.commit()
        return new_balance


def add_transaction(uid, change, reason, by_user_id, kind="adjust",
                    order_no=None, ref=None, balance_after=None):
    conn = get_conn()
    u = get_user(uid)
    conn.execute(
        "INSERT INTO transactions(user_id,change,balance_after,reason,by_user_id,created_at,"
        "kind,order_no,ref) VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, change, (u["credits"] if u else 0) if balance_after is None else balance_after,
         reason, by_user_id, int(time.time()), kind, order_no, ref))
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
    """兑换码兑换。

    并发安全：**不能用「先 SELECT 判断 used_by 再 UPDATE」**——2 个 gunicorn worker
    下同一张码会被兑两次（_db_lock 跨进程无效）。改为条件 UPDATE 抢占，
    rowcount==0 即表示被别人抢走，全程单事务。
    """
    code = (code or "").strip().upper()
    if not code:
        return False, "兑换码不存在"
    with _db_lock:
        conn = get_conn()
        now = int(time.time())
        try:
            cur = conn.execute(
                "UPDATE redeem_codes SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL",
                (uid, now, code))
            if cur.rowcount == 0:
                conn.rollback()
                r = conn.execute("SELECT used_by FROM redeem_codes WHERE code=?", (code,)).fetchone()
                return (False, "兑换码已被使用") if r else (False, "兑换码不存在")
            r = conn.execute("SELECT points FROM redeem_codes WHERE code=?", (code,)).fetchone()
            points = r["points"] if r else 0
            u = conn.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
            if not u:
                conn.rollback()
                return False, "用户不存在"
            new_balance = u["credits"] + points
            conn.execute("UPDATE users SET credits=?, updated_at=? WHERE id=?",
                         (new_balance, now, uid))
            conn.execute(
                "INSERT INTO transactions(user_id,change,balance_after,reason,by_user_id,"
                "created_at,kind,ref) VALUES(?,?,?,?,?,?,?,?)",
                (uid, points, new_balance, "兑换码充值", None, now, "redeem", code))
            conn.commit()
            return True, new_balance
        except Exception:
            conn.rollback()
            raise


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


# ---------- 打码订单号 & 单事务结算 ----------
ORDER_NO_PREFIX = "CS"


def gen_order_no():
    """订单号：CS + YYYYMMDD + 10 位大写十六进制（固定 20 字符），例 CS20261025A1B2C3D4E5。

    唯一性的最终保证是 usage_logs.order_no 的唯一索引，这里再做一次预检把碰撞概率压到极低
    （走索引，成本可忽略）。**不依赖 _db_lock**——它跨 worker 无效。
    """
    import secrets
    for _ in range(3):
        no = "%s%s%s" % (ORDER_NO_PREFIX,
                         datetime.datetime.now(_TZ_CST).strftime("%Y%m%d"),
                         secrets.token_hex(5).upper())
        if not qone("SELECT 1 AS x FROM usage_logs WHERE order_no=?", (no,)):
            return no
    raise RuntimeError("订单号生成失败，请重试")


_USAGE_INSERT = (
    "INSERT INTO usage_logs(user_id,token_id,type,points,success,duration_ms,detail,"
    "created_at,order_no,charged,balance_after) VALUES(?,?,?,?,?,?,?,?,?,?,?)")


def settle_solve(user_id, token_id, type_name, points, success, duration_ms,
                 detail, order_no):
    """打码结算（单事务，替代原来的 adjust_credits + log_usage 两段式）。

    - success=True ：扣积分 + 写 transactions(kind='solve', order_no) + usage_logs(charged=1)
    - success=False：只写 usage_logs(charged=0)，不扣积分
    两张表同生共死，避免「扣了钱没流水」或「有流水没扣钱」。
    `charged` 是合并流水的去重开关：charged=1 表示该次打码已由 transactions 行代表。

    返回 (实际使用的 order_no, 结算后余额)。调用方**必须使用返回的 order_no**，
    极小概率下因唯一索引冲突会换一个新号。
    """
    conn = None
    for _attempt in range(2):
        try:
            with _db_lock:
                conn = get_conn()
                now = now_ts()
                det = json.dumps(detail, ensure_ascii=False) if detail else None
                u = conn.execute("SELECT credits FROM users WHERE id=?",
                                 (user_id,)).fetchone()
                balance = u["credits"] if u else 0
                if not success:
                    conn.execute(_USAGE_INSERT,
                                 (user_id, token_id, type_name, 0, 0, duration_ms, det,
                                  now, order_no, 0, balance))
                    conn.commit()
                    return order_no, balance
                if balance >= points:
                    new_balance = balance - points
                    conn.execute("UPDATE users SET credits=?, updated_at=? WHERE id=?",
                                 (new_balance, now, user_id))
                    conn.execute(
                        "INSERT INTO transactions(user_id,change,balance_after,reason,"
                        "by_user_id,created_at,kind,order_no) VALUES(?,?,?,?,?,?,?,?)",
                        (user_id, -points, new_balance, "api:%s" % type_name, None, now,
                         "solve", order_no))
                    charged = 1
                else:
                    # 结算瞬间余额已被并发请求花掉（概率极低）：不扣费，如实记为成功但未扣费，
                    # 保持 charged=1 <=> 存在对应 transactions 行的不变量。
                    new_balance = balance
                    charged = 0
                conn.execute(_USAGE_INSERT,
                             (user_id, token_id, type_name, points if charged else 0, 1,
                              duration_ms, det, now, order_no, charged, new_balance))
                conn.commit()
                return order_no, new_balance
        except sqlite3.IntegrityError as e:
            try:
                (conn or get_conn()).rollback()
            except Exception:
                pass
            if "order_no" not in str(e).lower():
                raise
            order_no = gen_order_no()
    raise RuntimeError("订单号写入冲突，请重试")


# ---------- 每日签到 ----------
def get_checkin_state(uid):
    """右上角签到入口需要的一次性状态。

    键名与 templates/base.html 对齐（enabled / checked / reward），
    同时保留 done / points 两个别名，避免其它调用点回归。
    """
    s = get_settings()
    today = today_str()
    row = qone("SELECT created_at FROM checkins WHERE user_id=? AND checkin_date=?",
               (uid, today))
    total = qone("SELECT COUNT(*) c FROM checkins WHERE user_id=?", (uid,))["c"]
    enabled = to_bool(s.get("checkin_enabled"), True)
    points = to_int(s.get("checkin_points"), 100)
    done = bool(row)
    return {
        "enabled": enabled,
        "checked": done,
        "reward": points,
        # 别名
        "done": done,
        "points": points,
        "today": today,
        "total_days": total,
        "done_at": row["created_at"] if row else None,
    }


def do_checkin(uid):
    """原子签到。返回 (ok, code, payload)，code: ok / already / disabled / error。

    幂等性靠 checkins 上的 UNIQUE(user_id, checkin_date)：
    2 个 worker 同时 POST，一个 INSERT 成功、另一个必然 IntegrityError。
    **不复制 adjust_credits**——那个函数自带 conn.commit()，会把 checkins 行提前提交，
    导致「已签到但没积分」且当天无法重试。
    """
    with _db_lock:
        s = get_settings()
        if not to_bool(s.get("checkin_enabled"), True):
            return False, "disabled", {"error": "签到功能暂未开放"}
        points = to_int(s.get("checkin_points"), 100)
        if points < 0:
            points = 0
        today = today_str()
        conn = get_conn()
        now = now_ts()
        try:
            cur = conn.execute(
                "INSERT INTO checkins(user_id,checkin_date,points,created_at) VALUES(?,?,?,?)",
                (uid, today, points, now))
            cid = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.rollback()  # 必须回滚，否则这条线程连接会卡在失败事务上
            row = conn.execute(
                "SELECT created_at FROM checkins WHERE user_id=? AND checkin_date=?",
                (uid, today)).fetchone()
            if row is None:
                # 不是唯一索引冲突（例如用户行已被删除导致外键失败），别误判成"已签到"
                return False, "error", {"error": "签到失败，请重新登录后重试"}
            return True, "already", {"done_at": row["created_at"]}
        try:
            u = conn.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
            if not u:
                conn.rollback()
                return False, "error", {"error": "用户不存在"}
            new_balance = u["credits"] + points
            conn.execute("UPDATE users SET credits=?, updated_at=? WHERE id=?",
                         (new_balance, now, uid))
            tcur = conn.execute(
                "INSERT INTO transactions(user_id,change,balance_after,reason,by_user_id,"
                "created_at,kind) VALUES(?,?,?,?,?,?,?)",
                (uid, points, new_balance, "每日签到", None, now, "checkin"))
            conn.execute("UPDATE checkins SET transaction_id=? WHERE id=?",
                         (tcur.lastrowid, cid))
            conn.commit()  # 全程唯一一次提交
        except Exception:
            conn.rollback()
            raise
        return True, "ok", {"credited": points, "balance": new_balance, "done_at": now}


# ---------- 合并流水（用量 + 积分流水） ----------
RECORD_KIND_LABELS = {
    "solve": "验证码打码",
    "checkin": "每日签到",
    "signup": "注册赠送",
    "redeem": "积分兑换",
    "adjust": "系统调整",
}

# 去重核心：transactions 是「积分变动事件」的唯一来源（打码成功同时会写 usage_logs，
# 直接 UNION 会让一条打码变两行）；usage_logs 只补 charged=0 的未扣费事件（失败/0 积分）。
_RECORDS_UNION = """
SELECT 'credit' AS source, t.id AS row_id, t.kind AS kind,
       COALESCE(t.order_no,'') AS order_no,
       COALESCE(u.type,'') AS biz_type,
       CASE WHEN t.kind='solve' THEN COALESCE(u.success, 1) END AS success,
       t.change AS change,
       t.balance_after AS balance_after,
       COALESCE(t.ref,'') AS ref,
       COALESCE(t.reason,'') AS reason,
       COALESCE(a.username,'') AS operator,
       u.duration_ms AS duration_ms,
       t.created_at AS created_at,
       t.user_id AS user_id,
       COALESCE(tu.username,'') AS username
  FROM transactions t
  LEFT JOIN usage_logs u
         ON (t.order_no IS NOT NULL AND t.order_no <> '' AND u.order_no = t.order_no)
  LEFT JOIN users a ON a.id = t.by_user_id
  LEFT JOIN users tu ON tu.id = t.user_id
 WHERE 1=1 {USER_TX}
UNION ALL
SELECT 'usage', u.id, 'solve', COALESCE(u.order_no,''), COALESCE(u.type,''),
       u.success, 0, u.balance_after, '', '', '',
       u.duration_ms, u.created_at, u.user_id, COALESCE(uu.username,'')
  FROM usage_logs u
  LEFT JOIN users uu ON uu.id = u.user_id
 WHERE COALESCE(u.charged,0)=0 {USER_UL}
"""


def day_start_ts(s):
    """'YYYY-MM-DD' → 当天 00:00:00（固定 UTC+8）的时间戳。非法返回 None。"""
    try:
        d = datetime.datetime.strptime(str(s).strip(), "%Y-%m-%d")
        return int(d.replace(tzinfo=_TZ_CST).timestamp())
    except (ValueError, TypeError):
        return None


def day_end_ts(s):
    ts = day_start_ts(s)
    return None if ts is None else ts + 86399


def _decorate_record(row):
    """给模板/前端算好展示字段，避免模板里对 None 做算术导致 500。"""
    d = dict(row)
    kind = d.get("kind") or "adjust"
    change = to_int(d.get("change"), 0)
    bal = d.get("balance_after")
    order_no = d.get("order_no") or ""
    ref = d.get("ref") or ""
    biz_type = d.get("biz_type") or ""
    dur = d.get("duration_ms")
    success = d.get("success")

    if kind == "adjust":
        label = "积分充值" if change > 0 else "系统调整"
    else:
        label = RECORD_KIND_LABELS.get(kind, kind)
    d["kind"] = kind
    d["kind_label"] = label
    d["change"] = change
    d["change_label"] = "%+d" % change
    d["balance_label"] = ("%d (%+d)" % (bal, change)) if bal is not None else "— (%+d)" % change
    d["order_no"] = order_no
    d["ref"] = ref
    d["biz_type"] = biz_type
    # id 列：打码 → 订单号；兑换 → 兑换码字符串；签到/注册/调整 → 空
    d["id_label"] = order_no if kind == "solve" else (ref if kind == "redeem" else "")
    d["duration_label"] = ("%d ms" % dur) if (dur is not None and kind == "solve") else ""
    if kind == "solve" and success is not None:
        d["result"] = "success" if success else "fail"
        d["result_label"] = "成功" if success else "失败"
    else:
        d["result"] = None
        d["result_label"] = "—"
    d["created_at"] = d.get("created_at") or 0
    d["user_id"] = d.get("user_id")
    d["username"] = d.get("username") or ""
    return d


def list_records(user_id=None, kind=None, result=None, keyword=None,
                 date_from=None, date_to=None, limit=20, offset=0):
    """用量 + 积分流水合并列表。user_id=None 表示全局（管理后台）。

    返回 (rows, total)。rows 已过 _decorate_record，字段见 Phase 2 契约 D.2。
    """
    tx_where = "" if user_id is None else " AND t.user_id=?"
    ul_where = "" if user_id is None else " AND u.user_id=?"
    union = (_RECORDS_UNION.replace("{USER_TX}", tx_where)
             .replace("{USER_UL}", ul_where))
    base_args = [] if user_id is None else [user_id, user_id]

    where, args = [], []
    if kind:
        where.append("x.kind = ?")
        args.append(kind)
    if result == "success":
        where.append("x.success = 1")
    elif result == "fail":
        where.append("x.success = 0")
    if keyword:
        kw = "%%%s%%" % keyword
        where.append("(x.order_no LIKE ? OR x.ref LIKE ? OR x.biz_type LIKE ?"
                     " OR x.reason LIKE ? OR x.operator LIKE ?)")
        args.extend([kw] * 5)
    if date_from:
        ts = day_start_ts(date_from)
        if ts is not None:
            where.append("x.created_at >= ?")
            args.append(ts)
    if date_to:
        ts = day_end_ts(date_to)
        if ts is not None:
            where.append("x.created_at <= ?")
            args.append(ts)
    w = ("WHERE " + " AND ".join(where)) if where else ""

    all_args = tuple(base_args + args)
    total = qone("SELECT COUNT(*) c FROM (%s) x %s" % (union, w), all_args)["c"]
    rows = q("SELECT * FROM (%s) x %s "
             "ORDER BY x.created_at DESC, x.source DESC, x.row_id DESC LIMIT ? OFFSET ?"
             % (union, w), all_args + (limit, offset))
    return [_decorate_record(r) for r in rows], total


def records_summary(user_id=None):
    """合并页顶部汇总。"""
    w = "WHERE user_id=?" if user_id is not None else ""
    a = (user_id,) if user_id is not None else ()
    u = qone("SELECT COUNT(*) c, "
             "COALESCE(SUM(CASE WHEN success=1 THEN 1 ELSE 0 END),0) ok, "
             "COALESCE(SUM(CASE WHEN success=1 THEN points ELSE 0 END),0) spent "
             "FROM usage_logs %s" % w, a)
    t = qone("SELECT COALESCE(SUM(CASE WHEN change>0 THEN change ELSE 0 END),0) earned, "
             "COALESCE(SUM(CASE WHEN change<0 THEN -change ELSE 0 END),0) spent "
             "FROM transactions %s" % w, a)
    return {
        "total_calls": u["c"] if u else 0,
        "success_calls": u["ok"] if u else 0,
        "total_spent": t["spent"] if t else 0,
        "total_earned": t["earned"] if t else 0,
    }
