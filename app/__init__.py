import os
import warnings
import config
from flask import Flask, session, redirect, url_for, request, jsonify, g, flash

from app import db


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(config.BASE_DIR, "templates"),
        static_folder=os.path.join(config.BASE_DIR, "static"),
        static_url_path="/static",
    )
    app.secret_key = config.SECRET_KEY
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    db.init_db()

    from app import web, api, admin
    app.register_blueprint(web.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(admin.bp)

    @app.context_processor
    def inject_globals():
        u = None
        checkin = None
        # 配置项 signup_bonus 为唯一权威，config.SIGNUP_BONUS 仅兜底
        signup_bonus = db.get_setting_int("signup_bonus", config.SIGNUP_BONUS)
        checkin_enabled = db.get_setting_bool("checkin_enabled", True)
        checkin_points = db.get_setting_int("checkin_points", 100)
        if "user_id" in session:
            u = db.get_user(session["user_id"])
            if u:
                checkin = db.get_checkin_state(u["id"])
        return dict(current_user=u, APP_NAME=config.APP_NAME, APP_DESC=config.APP_DESC,
                    SIGNUP_BONUS=signup_bonus, checkin=checkin,
                    # base.html 里的降级兜底变量（checkin 为 None 时仍能正确渲染入口）
                    CHECKIN_URL="/api/checkin",
                    CHECKIN_ENABLED=checkin_enabled,
                    CHECKIN_POINTS=checkin_points,
                    PER_PAGE_CHOICES=PER_PAGE_CHOICES)

    @app.template_filter("ctime")
    def _ctime(ts):
        import datetime
        try:
            return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)

    return app


# ---- 鉴权辅助（供 web / admin 蓝图复用） ----
def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*a, **k):
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        g.user = db.get_user(session["user_id"])
        if not g.user or g.user["status"] != "active":
            session.clear()
            return redirect(url_for("web.login"))
        return f(*a, **k)
    return wrapper


def admin_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*a, **k):
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        g.user = db.get_user(session["user_id"])
        if not g.user or g.user["role"] != "admin":
            flash("需要管理员权限", "error")
            return redirect(url_for("web.login"))
        return f(*a, **k)
    return wrapper


# ---- 分页 ----
# 全站统一：page / per_page / total / pages / has_prev / has_next / prev_page /
#           next_page / first_item / last_item / offset
PER_PAGE_CHOICES = (10, 20, 50, 100)


def parse_paging(args, default_per_page=20):
    """① 先解析（此时 total 还未知）。非法值一律兜底，绝不抛 ValueError。

    注意：`default_per_page` **必须**落在 PER_PAGE_CHOICES 里，否则会被静默降级成 20
    （线上踩过：/admin/records 传 30 → 页面「每页」实际显示 20，很难发现）。
    这里用 warnings 留一条可见的痕迹，而不是抛异常——不能因为一个配置笔误让整页 500。
    """
    if default_per_page not in PER_PAGE_CHOICES:
        warnings.warn(
            "parse_paging(default_per_page=%r) 不在 PER_PAGE_CHOICES %r 内，将被静默降级为 20"
            % (default_per_page, PER_PAGE_CHOICES), RuntimeWarning, stacklevel=2)
    try:
        page = int(str(args.get("page") or "1").strip())
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(str(args.get("per_page") or default_per_page).strip())
    except (TypeError, ValueError):
        per_page = default_per_page
    if per_page not in PER_PAGE_CHOICES:
        per_page = default_per_page if default_per_page in PER_PAGE_CHOICES else 20
    if page < 1:
        page = 1
    return page, per_page


def pagination(page, per_page, total):
    """② 拿到 total 之后再算元数据，并把 page 夹紧到 [1, pages]（避免筛选后落到空白页）。"""
    total = max(0, int(total or 0))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, int(page or 1)), pages)
    offset = (page - 1) * per_page
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev_page": page - 1 if page > 1 else 1,
        "next_page": page + 1 if page < pages else pages,
        "first_item": (offset + 1) if total > 0 else 0,
        "last_item": min(offset + per_page, total) if total > 0 else 0,
        "offset": offset,
    }
