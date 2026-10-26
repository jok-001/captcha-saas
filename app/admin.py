import re
import time
from urllib.parse import urlsplit, parse_qs

import app.db as db
import config
from app import admin_required, parse_paging, pagination
from flask import Blueprint, render_template, request, redirect, url_for, flash, session

bp = Blueprint("admin", __name__, url_prefix="/admin")

# 与 web.KIND_OPTIONS 保持一致（避免跨模块循环导入，这里单独定义）
RECORD_KINDS = [
    {"value": "solve", "label": "验证码打码"},
    {"value": "checkin", "label": "每日签到"},
    {"value": "signup", "label": "注册赠送"},
    {"value": "redeem", "label": "积分兑换"},
    {"value": "adjust", "label": "系统调整"},
]


def _pager(pg, endpoint, **args):
    """把 pagination() 的结果包成 templates/_pagination.html 的 render_pager 契约：
    {page, per_page, total, pages, endpoint, args}。
    args 里的空值直接丢掉，链接保持干净（?page=3 而不是 ?status=&page=3）。
    """
    return dict(pg, endpoint=endpoint,
                args={k: v for k, v in args.items() if v not in (None, "", 0)})


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
    """用户列表（分页 + 关键词），分页参数与 /records、/admin/records 同一套。"""
    a = request.args
    keyword = (a.get("keyword") or "").strip()
    page, per_page = parse_paging(a, 20)

    where, args = "", ()
    if keyword:
        kw = "%%%s%%" % keyword
        where = "WHERE (username LIKE ? OR IFNULL(email,'') LIKE ?)"
        args = (kw, kw)

    total = db.qone("SELECT COUNT(*) c FROM users %s" % where, args)["c"]
    # 顺序：先算 total → 钳制 page → 再算 offset → 才查库（否则页码与内容会错位）
    pg = pagination(page, per_page, total)
    rows = db.q("SELECT * FROM users %s ORDER BY id DESC LIMIT ? OFFSET ?" % where,
                tuple(args) + (pg["per_page"], pg["offset"]))

    return render_template("admin/users.html",
                           users=[dict(r) for r in rows],
                           keyword=keyword,
                           total=pg["total"], page=pg["page"], per_page=pg["per_page"],
                           pages=pg["pages"], first_item=pg["first_item"],
                           last_item=pg["last_item"],
                           pager=_pager(pg, "admin.users", keyword=keyword))


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


def _users_back():
    """回到「用户管理」，尽量保住筛选与分页（编辑/启停/删除之后）。

    取值优先级：表单隐藏域 → 当前 query string → Referer 里的 query。
    为什么需要 Referer 兜底：users.html 的删除表单是 `action=url_for(...)`（不带 query），
    编辑弹窗表单也没有筛选字段，所以 POST 时 `request.args` 是空的、`request.form` 里也没有。
    将来模板补上 `<input type="hidden" name="keyword/page/per_page">` 后会自动走第一条，
    这个 fallback 自然失效，无需改代码。
    Referer 必须**同 host**（netloc == request.host）且 path 恰为 /admin/users，
    page/per_page 必须是数字——避免跨站页面伪造 Referer 往跳转里塞参数。
    """
    q = {}
    for k in ("keyword", "page", "per_page"):
        v = request.form.get(k) or request.args.get(k)
        if v:
            q[k] = v
    if not q and request.referrer:
        try:
            p = urlsplit(request.referrer)
            if p.netloc == request.host and p.path.rstrip("/") == "/admin/users":
                qs = parse_qs(p.query)
                kw = (qs.get("keyword") or [""])[0].strip()
                if kw:
                    q["keyword"] = kw
                for k in ("page", "per_page"):
                    v = (qs.get(k) or [""])[0]
                    if v.isdigit():
                        q[k] = v
        except (ValueError, TypeError):
            pass
    return redirect(url_for("admin.users", **q))


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
        return _users_back()
    if u["username"] == config.INIT_ADMIN_USERNAME and status != "active":
        flash("不能停用超级管理员", "error")
        return _users_back()
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
    return _users_back()


@bp.route("/user/<int:uid>/status", methods=["POST"])
@admin_required
def user_status(uid):
    status = request.form.get("status", "active")
    u = db.get_user(uid)
    if u and u["username"] == config.INIT_ADMIN_USERNAME and status != "active":
        flash("不能停用超级管理员", "error")
        return _users_back()
    db.set_user_status(uid, status)
    flash("用户状态已设为 %s" % status, "success")
    return _users_back()


@bp.route("/user/<int:uid>/delete", methods=["POST"])
@admin_required
def user_delete(uid):
    u = db.get_user(uid)
    if not u:
        flash("用户不存在", "error")
        return _users_back()
    if u["username"] == config.INIT_ADMIN_USERNAME:
        flash("不能删除超级管理员", "error")
        return _users_back()
    db.delete_user(uid)
    flash("已删除用户 %s（含其 Token / 用量 / 流水 / 签到记录）" % u["username"], "success")
    return _users_back()


@bp.route("/user/<int:uid>/transactions")
@admin_required
def user_transactions(uid):
    """已合并进 /admin/records（带 uid 筛选），保留路由做兼容重定向。"""
    u = db.get_user(uid)
    if not u:
        flash("用户不存在", "error")
        return redirect(url_for("admin.users"))
    return redirect(url_for("admin.records", uid=uid))


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


_ADMIN_KEEP_ARGS = ("page", "per_page", "keyword", "date_from", "date_to",
                    "kind", "result", "uid")


def _to_admin_records(**extra):
    q = {k: v for k, v in request.args.items() if k in _ADMIN_KEEP_ARGS and v}
    q.update({k: v for k, v in extra.items() if v})
    return redirect(url_for("admin.records", **q))


@bp.route("/records")
@admin_required
def records():
    """全局合并流水（用量 + 积分流水），可按用户/类型/结果/关键词/时间筛选。"""
    a = request.args
    # 默认每页必须落在 PER_PAGE_CHOICES (10/20/50/100) 里：
    # 这里曾写 30，不在白名单 → 被 parse_paging 静默降级成 20，页面上「每页」高亮与意图不符。
    page, per_page = parse_paging(a, 20)
    uid_raw = (a.get("uid") or "").strip()
    uid = int(uid_raw) if uid_raw.isdigit() else None
    kind = (a.get("kind") or "").strip()
    if kind not in [k["value"] for k in RECORD_KINDS]:
        kind = ""
    result = (a.get("result") or "").strip()
    if result not in ("success", "fail"):
        result = ""
    keyword = (a.get("keyword") or "").strip()
    date_from = (a.get("date_from") or "").strip()
    date_to = (a.get("date_to") or "").strip()
    filt = dict(kind=kind or None, result=result or None, keyword=keyword or None,
                date_from=date_from or None, date_to=date_to or None)

    rows, total = db.list_records(uid, limit=per_page, offset=(page - 1) * per_page, **filt)
    pg = pagination(page, per_page, total)
    if pg["offset"] != (page - 1) * per_page:
        rows, total = db.list_records(uid, limit=pg["per_page"], offset=pg["offset"], **filt)
        pg = pagination(pg["page"], pg["per_page"], total)

    target = db.get_user(uid) if uid else None
    pager = _pager(pg, "admin.records", uid=uid_raw if uid else "",
                   kind=kind, result=result, keyword=keyword,
                   date_from=date_from, date_to=date_to)
    return render_template(
        "admin/records.html",
        rows=rows, total=pg["total"], page=pg["page"], per_page=pg["per_page"],
        pages=pg["pages"], has_prev=pg["has_prev"], has_next=pg["has_next"],
        prev_page=pg["prev_page"], next_page=pg["next_page"],
        first_item=pg["first_item"], last_item=pg["last_item"],
        pager=pager,
        kind=kind, result=result, keyword=keyword,
        date_from=date_from, date_to=date_to,
        filter_uid=uid_raw if uid else "",
        filters={"kind": kind, "result": result, "keyword": keyword,
                 "uid": uid_raw, "per_page": per_page,
                 "date_from": date_from, "date_to": date_to},
        kinds=RECORD_KINDS,
        target=target,
        users={r["id"]: r["username"] for r in db.q("SELECT id,username FROM users")},
        summary=db.records_summary(uid),
        pricing=db.get_pricing())


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    """系统管理：配置项按 config.SETTINGS_SCHEMA 动态渲染，键值存 settings 表。

    表单兼容两种形态（前端可整页一个 form，也可按分组拆多个 form）：
      - 非布尔项：**只在表单里出现时才保存**，没出现视为「本表单不涉及」而不是空值报错
      - 布尔项：checkbox 未勾选时浏览器不会提交该字段。因此「未勾选」只在
        「同分组里有其它字段被提交」时才判定为 0；整组都没提交说明是别的分组的表单，跳过。
        这样整页提交和分组提交都不会误清开关。
    """
    if request.method == "POST":
        form = request.form
        values = {}
        for spec in config.SETTINGS_SCHEMA:
            if spec.get("reserved") or spec["type"] == "bool":
                continue
            field = "s_" + spec["key"]
            if field in form:
                values[spec["key"]] = form.get(field, "")
        for spec in config.SETTINGS_SCHEMA:
            if spec.get("reserved") or spec["type"] != "bool":
                continue
            key, field = spec["key"], "s_" + spec["key"]
            if field in form:
                values[key] = 1 if form.get(field) else 0
                continue
            group = spec.get("group")
            group_posted = any(
                ("s_" + s["key"]) in form for s in config.SETTINGS_SCHEMA
                if s.get("group") == group and s["type"] != "bool"
                and not s.get("reserved"))
            if group_posted:
                values[key] = 0  # 同组表单已提交，未勾选即关闭
        saved, errors = db.set_settings(values, session["user_id"])
        if errors:
            for e in errors:
                flash(e, "error")
        if saved:
            flash("系统配置已保存（%d 项）" % len(saved), "success")
        if not errors and not saved:
            flash("没有可保存的配置项", "error")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html",
                           groups=db.get_settings_groups(),
                           settings_rows=db.get_settings_view())


@bp.route("/usage")
@admin_required
def usage():
    """已合并进 /admin/records，保留路由做兼容重定向。"""
    return _to_admin_records(kind="solve")


@bp.route("/transactions")
@admin_required
def transactions():
    """已合并进 /admin/records，保留路由做兼容重定向。"""
    return _to_admin_records()


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
            # 校验失败也要回到原来的筛选/分页，否则用户改完数值得重新筛一遍
            return redirect(url_for("admin.redeem", **_redeem_query(request.form)))
        codes = db.create_redeem_code(points, session["user_id"], count, note=note)
        flash("已生成 %d 个兑换码（每个 %d 积分）" % (count, points), "success")
        # 生成后仍要带完整列表上下文，避免模板渲染列表区时变量未定义
        return _redeem_render(new_codes=codes, new_points=points)
    return _redeem_render()


def _redeem_render(new_codes=None, new_points=0):
    """兑换码列表渲染（GET 与「生成后重渲染」共用，保证分页逻辑只有一处）。"""
    ctx = _redeem_ctx(request.args)
    per_page = ctx["per_page"]
    page = ctx["page"]
    rows, total = db.list_redeem_codes(status=ctx["status"], used_by=ctx["used_by"],
                                       keyword=ctx["keyword"],
                                       limit=per_page, offset=(page - 1) * per_page)
    pg = pagination(page, per_page, total)
    if pg["offset"] != (page - 1) * per_page:
        # 筛选后页码越界（如 ?page=9 但只剩 2 页）→ 回到末页，避免空白页
        rows, total = db.list_redeem_codes(status=ctx["status"], used_by=ctx["used_by"],
                                           keyword=ctx["keyword"],
                                           limit=pg["per_page"], offset=pg["offset"])
        pg = pagination(pg["page"], pg["per_page"], total)

    users = {r["id"]: r["username"] for r in db.q("SELECT id,username FROM users")}
    ctx.update(rows=rows, total=pg["total"], page=pg["page"], pages=pg["pages"],
               per_page=pg["per_page"], limit=pg["per_page"],
               first_item=pg["first_item"], last_item=pg["last_item"], users=users,
               # 翻页时保留全部有效筛选条件（status / used_by / keyword）
               pager=_pager(pg, "admin.redeem", status=ctx["status"],
                            used_by=ctx["used_by"], keyword=ctx["keyword"]))
    return render_template("admin/redeem.html",
                           codes=new_codes, points=new_points, **ctx)


def _redeem_query(form):
    """从表单里还原筛选参数（批次删除后保持筛选与分页上下文）。

    表单没有 action，会 POST 回当前 URL，所以 per_page 可能只在 query 里，
    这里两处都取，避免删完自动掉回每页 20 条。
    """
    out = {}
    for k in ("status", "used_by", "keyword", "page", "per_page"):
        v = form.get(k) or request.args.get(k)
        if v:
            out[k] = v
    return out


def _redeem_ctx(args):
    status = args.get("status", "")  # '' / 'used' / 'unused'
    used_by = args.get("used_by", "")
    keyword = (args.get("keyword") or "").strip()
    page, per_page = parse_paging(args, 20)
    return {"status": status, "used_by": used_by, "keyword": keyword,
            "page": page, "per_page": per_page}
