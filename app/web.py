import re
import app.db as db
import config
from app import login_required
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, abort

bp = Blueprint("web", __name__)


@bp.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("web.dashboard"))
    return redirect(url_for("web.login"))


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if not username or not password:
            flash("用户名和密码不能为空", "error")
        elif not re.match(r"^[A-Za-z0-9_]{3,20}$", username):
            flash("用户名需为 3-20 位字母/数字/下划线", "error")
        elif len(password) < 6:
            flash("密码至少 6 位", "error")
        elif password != confirm:
            flash("两次密码不一致", "error")
        elif db.get_user_by_username(username):
            flash("用户名已存在", "error")
        else:
            db.create_user(username, email, password, role="user")
            flash("注册成功，赠送 %d 积分，请登录" % config.SIGNUP_BONUS, "success")
            return redirect(url_for("web.login"))
    return render_template("register.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        u = db.authenticate(username, password)
        if not u:
            flash("用户名或密码错误，或账号已被停用", "error")
        else:
            session["user_id"] = u["id"]
            session["role"] = u["role"]
            flash("欢迎回来，%s" % u["username"], "success")
            return redirect(url_for("web.dashboard"))
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@bp.route("/dashboard")
@login_required
def dashboard():
    uid = session["user_id"]
    user = db.get_user(uid)
    total, by_type = db.usage_summary(uid)
    recent, _ = db.usage_stats(uid, limit=8)
    pricing = db.get_pricing()
    return render_template("dashboard.html",
                           user=user, total=total, by_type=by_type,
                           recent=recent, pricing=pricing)


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """个人信息中心：唯一 token（重置）、邮箱、修改密码。"""
    uid = session["user_id"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_profile":
            email = (request.form.get("email") or "").strip()
            db.update_profile(uid, email=email or None)
            flash("个人信息已更新", "success")
        elif action == "reset_token":
            db.reset_token(uid)
            flash("Token 已重置，请立即复制保存（旧 Token 已失效）", "success")
        elif action == "change_password":
            old = request.form.get("old_password", "")
            new = request.form.get("new_password", "")
            confirm = request.form.get("confirm", "")
            u = db.get_user(uid)
            from werkzeug.security import check_password_hash
            if not check_password_hash(u["password_hash"], old):
                flash("原密码错误", "error")
            elif len(new) < 6:
                flash("新密码至少 6 位", "error")
            elif new != confirm:
                flash("两次新密码不一致", "error")
            else:
                db.change_password(uid, new)
                flash("密码已修改", "success")
        return redirect(url_for("web.profile"))
    user = db.get_user(uid)
    token_row = db.get_user_token(uid)
    return render_template("profile.html", user=user, token_row=token_row)


@bp.route("/transactions")
@login_required
def transactions():
    """用户自己的积分流水（支持模糊搜索、收支过滤、时间范围）。"""
    uid = session["user_id"]
    page = int(request.args.get("page", 1) or 1)
    limit = 20
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
    return render_template("transactions.html", rows=rows, total=total, page=page, pages=pages,
                           admins=admins, keyword=keyword, change_type=change_type,
                           date_from=date_from, date_to=date_to)


@bp.route("/usage")
@login_required
def usage():
    uid = session["user_id"]
    type_filter = request.args.get("type", "")
    success_filter = request.args.get("success", "")
    keyword = (request.args.get("keyword") or "").strip()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    page = int(request.args.get("page", 1) or 1)
    limit = 20
    offset = (page - 1) * limit
    rows, total = db.usage_stats(uid, type_filter=type_filter or None,
                                 success_filter=success_filter or None,
                                 keyword=keyword or None,
                                 date_from=date_from or None,
                                 date_to=date_to or None,
                                 limit=limit, offset=offset)
    total_summary, by_type = db.usage_summary(uid)
    pricing = db.get_pricing()
    pages = (total + limit - 1) // limit
    return render_template("usage.html", rows=rows, total=total, page=page, pages=pages,
                           type_filter=type_filter, success_filter=success_filter,
                           keyword=keyword, date_from=date_from, date_to=date_to,
                           total_summary=total_summary, by_type=by_type, pricing=pricing)


@bp.route("/recharge", methods=["GET", "POST"])
@login_required
def recharge():
    uid = session["user_id"]
    msg = None
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        ok, res = db.redeem_code(code, uid)
        if ok:
            flash("兑换成功！当前积分: %d" % res, "success")
        else:
            flash("兑换失败: %s" % res, "error")
        return redirect(url_for("web.recharge"))
    # 展示可购买的“套餐”（此处为示意；实际需管理员发放兑换码或充值）
    pricing = db.get_pricing()
    return render_template("recharge.html", pricing=pricing)


@bp.route("/docs")
def docs():
    pricing = db.get_pricing()
    return render_template("docs.html", pricing=pricing, api_base=config.API_BASE)


# ---------- 各类型接口文档注册表 ----------
DOCS_REGISTRY = {
    "huya_v3": {
        "title": "虎牙 V3 验证码打码",
        "endpoints": [
            {"path": "/api/huya_send_phone", "desc": "完整流程：解验证码 + 下发注册短信"},
            {"path": "/api/huya_captcha", "desc": "仅解验证码，返回通过验证的令牌"},
        ],
        "params": [
            {"name": "phone", "type": "string", "required": True, "default": "", "desc": "目标手机号。国内 11 位直接传；境外号码需带区号（如 +85212345678），不带区号默认 +86"},
            {"name": "max_retries", "type": "int", "required": False, "default": "1", "desc": "验证码识别失败时的最大重试次数"},
            {"name": "proxy", "type": "string", "required": False, "default": "—", "desc": "HTTP 代理，如 <code>http://user:pass@ip:port</code>"},
        ],
        "examples": [
            {
                "lang": "Python (requests)",
                "note": "完整流程：解验证码 + 发送短信",
                "code": 'import requests\n\nr = requests.post("{api_base}/api/huya_send_phone",\n    headers={{"X-API-Key": "你的TOKEN"}},\n    json={{"phone": "13800138000"}})\nprint(r.json())'
            },
            {
                "lang": "Python (curl_cffi)",
                "note": "绕过 TLS 指纹检测（推荐）",
                "code": 'from curl_cffi import requests as creq\n\nr = creq.post("{api_base}/api/huya_send_phone",\n    headers={{"X-API-Key": "你的TOKEN"}},\n    json={{"phone": "13800138000"}},\n    impersonate="chrome")\nprint(r.json())'
            },
            {
                "lang": "curl",
                "note": "仅解验证码（不下发短信）",
                "code": 'curl -X POST {api_base}/api/huya_captcha \\\n  -H "X-API-Key: 你的TOKEN" \\\n  -H "Content-Type: application/json" \\\n  -d \'{{"phone":"13800138000"}}\''
            },
        ],
        "response_example": '''{
  "success": true,
  "type": "huya_v3",
  "credits_spent": 10,
  "credits_left": 990,
  "result": {
    "success": true,
    "message": "验证码已发送",
    "phone": "13800138000",
    "data": {
      "session_data": "...",
      "guid": "...",
      "sdid": "...",
      "phone_hex": "...",
      "behavior": "...",
      "verify_response": { ... }
    }
  }
}''',
        "response_fields": [
            {"name": "success", "type": "bool", "desc": "请求是否成功（业务层面，HTTP 200 不代表成功）"},
            {"name": "type", "type": "string", "desc": "调用的验证码类型"},
            {"name": "credits_spent", "type": "int", "desc": "本次成功消耗的积分"},
            {"name": "credits_left", "type": "int", "desc": "扣除后账户剩余积分"},
            {"name": "result", "type": "object", "desc": "solver 原始返回对象"},
            {"name": "result.success", "type": "bool", "desc": "验证码是否成功解出"},
            {"name": "result.message", "type": "string", "desc": "状态描述（成功/失败原因）"},
            {"name": "result.data.session_data", "type": "string", "desc": "会话令牌（用于后续调用虎牙接口）"},
            {"name": "result.data.guid", "type": "string", "desc": "设备 GUID"},
            {"name": "result.data.sdid", "type": "string", "desc": "设备 SDID"},
            {"name": "result.data.phone_hex", "type": "string", "desc": "手机号十六进制编码"},
            {"name": "result.data.behavior", "type": "string", "desc": "行为数据（虎牙风控用）"},
            {"name": "result.data.verify_response", "type": "object", "desc": "虎牙验证接口原始响应"},
        ],
    },
    "tencent_tcaptcha": {
        "title": "腾讯 TCaptcha 多类型打码",
        "endpoints": [
            {"path": "/api/v1/solve", "desc": "通用入口（type=tencent_tcaptcha）"},
            {"path": "/api/solve", "desc": "通用入口（兼容旧接口）"},
        ],
        "params": [
            {"name": "appid", "type": "string", "required": True, "default": "", "desc": "腾讯 TCaptcha 应用的 AppID（如 <code>2039519451</code>）"},
            {"name": "entry_url", "type": "string", "required": False, "default": "—", "desc": "触发验证的页面 URL（用于获取动态配置）"},
            {"name": "subsid", "type": "string", "required": False, "default": "—", "desc": "业务方 SubID（如有）"},
            {"name": "max_retries", "type": "int", "required": False, "default": "3", "desc": "识别失败最大重试次数"},
            {"name": "proxy", "type": "string", "required": False, "default": "—", "desc": "HTTP 代理，如 <code>http://user:pass@ip:port</code>"},
        ],
        "examples": [
            {
                "lang": "Python (requests)",
                "note": "通用入口",
                "code": 'import requests\n\nr = requests.post("{api_base}/api/v1/solve",\n    headers={{"X-API-Key": "你的TOKEN"}},\n    json={{"type": "tencent_tcaptcha",\n           "param": {{"appid": "2039519451", "entry_url": "https://example.com"}}}})\nprint(r.json())'
            },
            {
                "lang": "Python (curl_cffi)",
                "note": "绕过 TLS 指纹检测",
                "code": 'from curl_cffi import requests as creq\n\nr = creq.post("{api_base}/api/v1/solve",\n    headers={{"X-API-Key": "你的TOKEN"}},\n    json={{"type": "tencent_tcaptcha",\n           "param": {{"appid": "2039519451", "entry_url": "https://example.com"}}}},\n    impersonate="chrome")\nprint(r.json())'
            },
            {
                "lang": "curl",
                "note": "POST /api/v1/solve",
                "code": 'curl -X POST {api_base}/api/v1/solve \\\n  -H "X-API-Key: 你的TOKEN" \\\n  -H "Content-Type: application/json" \\\n  -d \'{{"type":"tencent_tcaptcha","param":{{"appid":"2039519451","entry_url":"https://example.com"}}}}\''
            },
        ],
        "response_example": '''{
  "success": true,
  "type": "tencent_tcaptcha",
  "credits_spent": 30,
  "credits_left": 960,
  "result": {
    "success": true,
    "message": "ok (glyph-sequence)",
    "data": {
      "appid": "2039519451",
      "ticket": "tr03z20ZGWFnxlq-RoHl705yqzm31zVZ...",
      "randstr": "@wT3",
      "attempts": 2,
      "duration_ms": 13159,
      "solver": "glyph_sequence"
    },
    "solved": {
      "ticket": "tr03z20ZGWFnxlq-RoHl705yqzm31zVZ...",
      "randstr": "@wT3"
    }
  }
}''',
        "response_fields": [
            {"name": "success", "type": "bool", "desc": "请求是否成功"},
            {"name": "type", "type": "string", "desc": "调用的验证码类型"},
            {"name": "credits_spent", "type": "int", "desc": "本次成功消耗的积分"},
            {"name": "credits_left", "type": "int", "desc": "扣除后账户剩余积分"},
            {"name": "result", "type": "object", "desc": "solver 原始返回对象"},
            {"name": "result.success", "type": "bool", "desc": "验证码是否成功解出"},
            {"name": "result.message", "type": "string", "desc": "状态描述（含 solver 类型，如 <code>ok (glyph-sequence)</code>）"},
            {"name": "result.data.appid", "type": "string", "desc": "请求时传入的 AppID"},
            {"name": "result.data.ticket", "type": "string", "desc": "验证通过后的 ticket（提交给业务方使用）"},
            {"name": "result.data.randstr", "type": "string", "desc": "验证通过后的随机字符串（与 ticket 配对使用）"},
            {"name": "result.data.attempts", "type": "int", "desc": "本次解题尝试次数"},
            {"name": "result.data.duration_ms", "type": "int", "desc": "解题耗时（毫秒）"},
            {"name": "result.data.solver", "type": "string", "desc": "使用的解题算法名"},
            {"name": "result.solved", "type": "object", "desc": "快捷字段：<code>{{ticket, randstr}}</code>"},
        ],
    },
}


def _build_doc(type_name, api_base):
    """根据定价表 + 注册表组装详情页数据。"""
    price_row = db.get_price(type_name)
    if not price_row:
        abort(404)
    meta = DOCS_REGISTRY.get(type_name)
    if not meta:
        abort(404)
    doc = dict(meta)
    doc["type"] = type_name
    doc["points"] = price_row["points"]
    doc["enabled"] = bool(price_row["enabled"])
    doc["note"] = price_row.get("note") or ""
    # 模板变量替换
    for ex in doc["examples"]:
        ex["code"] = ex["code"].replace("{api_base}", api_base)
    doc["response_example"] = doc["response_example"].replace("{api_base}", api_base)
    return doc


@bp.route("/docs/<type_name>")
def docs_detail(type_name):
    doc = _build_doc(type_name, config.API_BASE)
    return render_template("docs_detail.html", doc=doc, api_base=config.API_BASE)
