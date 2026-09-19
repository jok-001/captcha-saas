import re
import time
import app.db as db
import config
from app import admin_required
from flask import Blueprint, render_template, request, redirect, url_for, flash, session

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/")
@bp.route("/overview")
@admin_required
def overview():
    total_users = db.qone("SELECT COUNT(*) c FROM users")["c"]
    active_users = db.qone("SELECT COUNT(*) c FROM users WHERE status='active'")["c"]
    total_credits = db.qone("SELECT COALESCE(SUM(credits),0) c FROM users")["c"]
    usage_total, _ = db.usage_summary()
    logs, _ = db.usage_stats(limit=15)
    users = {u["id"]: u["username"] for u in db.q("SELECT id,username FROM users")}
    return render_template("admin/overview.html",
                           total_users=total_users, active_users=active_users,
                           total_credits=total_credits, usage_total=usage_total,
                           logs=[dict(r) for r in logs], users=users)


@bp.route("/users")
@admin_required
def users():
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        kw = "%%%s%%" % keyword
        rows = db.q("SELECT * FROM users WHERE username LIKE ? OR IFNULL(email,'') LIKE ? ORDER BY id DESC",
                    (kw, kw))
    else:
        rows = db.q("SELECT * FROM users ORDER BY id DESC")
    return render_template("admin/users.html", users=[dict(r) for r in rows], keyword=keyword)


@bp.route("/user/create", methods=["POST"])
@admin_required
def user_create():
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role", "user")
    try:
        credits = int(request.form.get("credits", 0) or 0)
    except ValueError:
        credits = 0
    if not re.match(r"^[A-Za-z0-9_]{3,20}$", username):
        flash("用户名需为 3-20 位字母/数字/下划线", "error")
    elif len(password) < 6:
        flash("密码至少 6 位", "error")
    elif db.get_user_by_username(username):
        flash("用户名已存在", "error")
    elif credits < 0:
        flash("初始积分不能为负", "error")
    else:
        db.create_user(username, email, password, role=role, credits=credits, by_user_id=session["user_id"])
        flash("已创建用户 %s（初始积分 %d，已记流水）" % (username, credits), "success")
    return redirect(url_for("admin.users"))


@bp.route("/user/<int:uid>/edit", methods=["POST"])
@admin_required
def user_edit(uid):
    """编辑弹窗统一提交：邮箱/角色/状态 + 积分增减（带原因，记流水）。"""
    email = (request.form.get("email") or "").strip()
    role = request.form.get("role", "user")
    status = request.form.get("status", "active")
    delta_str = (request.form.get("delta") or "").strip()
    reason = (request.form.get("reason") or "").strip() or "管理员调整"
    u = db.get_user(uid)
    if not u:
        flash("用户不存在", "error")
        return redirect(url_for("admin.users"))
    if u["username"] == config.INIT_ADMIN_USERNAME and status != "active":
        flash("不能停用超级管理员", "error")
        return redirect(url_for("admin.users"))
    db.execute("UPDATE users SET email=?, role=?, status=?, updated_at=? WHERE id=?",
               (email or None, role, status, int(time.time()), uid))
    if delta_str:
        try:
            delta = int(delta_str)
        except ValueError:
            delta = 0
        if delta != 0:
            try:
                db.adjust_credits(uid, delta, reason, session["user_id"])
                flash("已保存，并调整 %+d 积分（%s）" % (delta, reason), "success")
            except ValueError as e:
                flash(str(e), "error")
    flash("用户信息已更新", "success")
    return redirect(url_for("admin.users"))


@bp.route("/user/<int:uid>/status", methods=["POST"])
@admin_required
def user_status(uid):
    status = request.form.get("status", "active")
    u = db.get_user(uid)
    if u and u["username"] == config.INIT_ADMIN_USERNAME and status != "active":
        flash("不能停用超级管理员", "error")
        return redirect(url_for("admin.users"))
    db.set_user_status(uid, status)
    flash("用户状态已设为 %s" % status, "success")
    return redirect(url_for("admin.users"))


@bp.route("/user/<int:uid>/delete", methods=["POST"])
@admin_required
def user_delete(uid):
    u = db.get_user(uid)
    if not u:
        flash("用户不存在", "error")
        return redirect(url_for("admin.users"))
    if u["username"] == config.INIT_ADMIN_USERNAME:
        flash("不能删除超级管理员", "error")
        return redirect(url_for("admin.users"))
    db.delete_user(uid)
    flash("已删除用户 %s（含其 Token / 用量 / 流水）" % u["username"], "success")
    return redirect(url_for("admin.users"))


@bp.route("/user/<int:uid>/transactions")
@admin_required
def user_transactions(uid):
    """查看单个用户的积分流水。"""
    u = db.get_user(uid)
    if not u:
        flash("用户不存在", "error")
        return redirect(url_for("admin.users"))
    page = int(request.args.get("page", 1) or 1)
    limit = 30
    offset = (page - 1) * limit
    keyword = (request.args.get("keyword") or "").strip()
    change_type = request.args.get("change_type", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    rows, total = db.list_transactions(uid, keyword=keyword or None,
                                      change_type=change_type or None,
                                      date_from=date_from or None,
                                      date_to=date_to or None,
                                      limit=limit, offset=offset)
    admins = {r["id"]: r["username"] for r in db.q("SELECT id,username FROM users WHERE role='admin'")}
    pages = (total + limit - 1) // limit
    return render_template("admin/transactions.html", target=u, rows=rows,
                           total=total, page=page, pages=pages, users={u["id"]: u["username"]},
                           admins=admins, filter_uid=str(uid), keyword=keyword,
                           change_type=change_type, date_from=date_from, date_to=date_to)


@bp.route("/pricing", methods=["GET", "POST"])
@admin_required
def pricing():
    if request.method == "POST":
        t = request.form.get("type", "").strip()
        try:
            points = int(request.form.get("points", 0))
        except ValueError:
            points = 0
        enabled = 1 if request.form.get("enabled") else 0
        note = request.form.get("note", "").strip()
        if not t:
            flash("类型名不能为空", "error")
        elif points < 0:
            flash("单价不能为负", "error")
        else:
            db.upsert_price(t, points, enabled, note)
            flash("定价已保存: %s = %d 积分" % (t, points), "success")
        return redirect(url_for("admin.pricing"))
    rows = db.get_pricing()
    return render_template("admin/pricing.html", pricing=rows)


@bp.route("/pricing/delete", methods=["POST"])
@admin_required
def pricing_delete():
    t = (request.form.get("type") or "").strip()
    if not t:
        flash("类型名不能为空", "error")
    else:
        db.delete_price(t)
        flash("已删除类型 %s（历史用量记录不受影响）" % t, "success")
    return redirect(url_for("admin.pricing"))


@bp.route("/usage")
@admin_required
def usage():
    type_filter = request.args.get("type", "")
    success_filter = request.args.get("success", "")
    keyword = (request.args.get("keyword") or "").strip()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    page = int(request.args.get("page", 1) or 1)
    limit = 30
    offset = (page - 1) * limit
    rows, total = db.usage_stats(type_filter=type_filter or None,
                                 success_filter=success_filter or None,
                                 keyword=keyword or None,
                                 date_from=date_from or None,
                                 date_to=date_to or None,
                                 limit=limit, offset=offset)
    users = {u["id"]: u["username"] for u in db.q("SELECT id,username FROM users")}
    pricing = db.get_pricing()
    pages = (total + limit - 1) // limit
    return render_template("admin/usage.html", rows=[dict(r) for r in rows], total=total,
                           page=page, pages=pages, type_filter=type_filter,
                           success_filter=success_filter, keyword=keyword,
                           date_from=date_from, date_to=date_to,
                           users=users, pricing=pricing)


@bp.route("/transactions")
@admin_required
def transactions():
    """全局积分流水（可按用户、关键词、收支类型、时间范围筛选）。"""
    uid = request.args.get("uid", "")
    keyword = (request.args.get("keyword") or "").strip()
    change_type = request.args.get("change_type", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    page = int(request.args.get("page", 1) or 1)
    limit = 30
    offset = (page - 1) * limit
    if uid and uid.isdigit():
        rows, total = db.list_transactions(int(uid), keyword=keyword or None,
                                           change_type=change_type or None,
                                           date_from=date_from or None,
                                           date_to=date_to or None,
                                           limit=limit, offset=offset)
    else:
        uid = ""
        rows, total = db.list_transactions(None, keyword=keyword or None,
                                           change_type=change_type or None,
                                           date_from=date_from or None,
                                           date_to=date_to or None,
                                           limit=limit, offset=offset)
    users = {r["id"]: r["username"] for r in db.q("SELECT id,username FROM users")}
    admins = {r["id"]: r["username"] for r in db.q("SELECT id,username FROM users WHERE role='admin'")}
    pages = (total + limit - 1) // limit
    return render_template("admin/transactions.html", rows=rows, total=total, page=page,
                           pages=pages, users=users, admins=admins, filter_uid=uid, keyword=keyword,
                           change_type=change_type, date_from=date_from, date_to=date_to)


@bp.route("/redeem", methods=["GET", "POST"])
@admin_required
def redeem():
    if request.method == "POST":
        action = request.form.get("action", "generate")
        if action == "batch_delete":
            codes = request.form.getlist("codes")
            if not codes:
                flash("未选择要删除的兑换码", "error")
            else:
                db.delete_redeem_codes(codes)
                flash("已删除 %d 个兑换码" % len(codes), "success")
            return redirect(url_for("admin.redeem", **_redeem_query(request.form)))
        # 生成
        try:
            points = int(request.form.get("points", 0))
        except ValueError:
            points = 0
        try:
            count = int(request.form.get("count", 1))
        except ValueError:
            count = 1
        note = (request.form.get("note") or "").strip()
        if points <= 0 or count <= 0:
            flash("积分和数量必须为正", "error")
            return redirect(url_for("admin.redeem"))
        codes = db.create_redeem_code(points, session["user_id"], count, note=note)
        flash("已生成 %d 个兑换码（每个 %d 积分）" % (count, points), "success")
        # 生成后仍要带列表上下文，避免模板渲染列表区时变量未定义
        ctx = _redeem_ctx(request.args)
        rows, total = db.list_redeem_codes(status=ctx["status"], used_by=ctx["used_by"],
                                           limit=ctx["limit"], offset=(ctx["page"] - 1) * ctx["limit"])
        users = {r["id"]: r["username"] for r in db.q("SELECT id,username FROM users")}
        ctx.update(rows=rows, total=total, pages=(total + ctx["limit"] - 1) // ctx["limit"], users=users)
        return render_template("admin/redeem.html", codes=codes, points=points, **ctx)
    # GET：列表 + 筛选
    ctx = _redeem_ctx(request.args)
    rows, total = db.list_redeem_codes(status=ctx["status"], used_by=ctx["used_by"],
                                      keyword=ctx["keyword"],
                                      limit=ctx["limit"], offset=(ctx["page"] - 1) * ctx["limit"])
    users = {r["id"]: r["username"] for r in db.q("SELECT id,username FROM users")}
    ctx.update(rows=rows, total=total, pages=(total + ctx["limit"] - 1) // ctx["limit"], users=users)
    return render_template("admin/redeem.html", codes=None, **ctx)


def _redeem_query(form):
    """从表单里还原筛选参数（批量删除后保持筛选上下文）。"""
    out = {}
    for k in ("status", "used_by", "keyword", "page"):
        v = form.get(k)
        if v:
            out[k] = v
    return out


def _redeem_ctx(args):
    status = args.get("status", "")  # '' / 'used' / 'unused'
    used_by = args.get("used_by", "")
    keyword = (args.get("keyword") or "").strip()
    page = int(args.get("page", 1) or 1)
    limit = 20
    return {"status": status, "used_by": used_by, "keyword": keyword, "page": page, "limit": limit}
