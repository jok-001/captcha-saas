import os
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
        if "user_id" in session:
            u = db.get_user(session["user_id"])
        return dict(current_user=u, APP_NAME=config.APP_NAME, APP_DESC=config.APP_DESC, SIGNUP_BONUS=config.SIGNUP_BONUS)

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
