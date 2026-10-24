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
_GEETEST_DOCS = {'geetestV4': {'title': '极验4代打码（自动识别类型）',
               'endpoints': [{'path': '/api/v1/solve', 'desc': '通用打码入口，type 传 geetestV4'}],
               'params': [{'name': 'captcha_id',
                           'type': 'string',
                           'required': False,
                           'default': '极验官方demo',
                           'desc': '极验4代 captcha_id（目标站点的公开标识）'},
                          {'name': 'riskType',
                           'type': 'string',
                           'required': False,
                           'default': 'auto',
                           'desc': 'auto=自动探测类型并求解（推荐）；也可显式传 slide/word/...'},
                          {'name': 'proxy',
                           'type': 'string',
                           'required': False,
                           'default': '—',
                           'desc': '本次调用专用 HTTP 代理，如 http://user:pass@ip:port（机房 IP '
                                   '直连易被极验风控，建议传住宅代理）'},
                          {'name': 'timeout',
                           'type': 'int',
                           'required': False,
                           'default': '240',
                           'desc': '单次求解超时秒数'}],
               'examples': [{'lang': 'curl',
                             'note': '自动识别类型并求解（推荐）',
                             'code': 'curl -X POST {api_base}/api/v1/solve \\\n'
                                     '  -H "X-API-Key: 你的TOKEN" \\\n'
                                     '  -H "Content-Type: application/json" \\\n'
                                     '  -d '
                                     '\'{"type":"geetestV4","param":{"captcha_id":"54088bb07d2df3c46b79f80300b0abbe"}}\''},
                            {'lang': 'Python (requests)',
                             'note': '带代理调用（机房 IP 建议传住宅代理）',
                             'code': 'import requests\n'
                                     '\n'
                                     'r = requests.post("{api_base}/api/v1/solve",\n'
                                     '    headers={"X-API-Key": "你的TOKEN"},\n'
                                     '    json={"type": "geetestV4",\n'
                                     '          "param": {"captcha_id": '
                                     '"54088bb07d2df3c46b79f80300b0abbe",\n'
                                     '                    "proxy": "http://user:pass@ip:port"}})\n'
                                     'print(r.json())'}],
               'response_example': '{\n'
                                   '  "success": true,\n'
                                   '  "type": "geetestV4",\n'
                                   '  "credits_spent": 20,\n'
                                   '  "result": {\n'
                                   '    "success": true,\n'
                                   '    "message": "验证码已通过",\n'
                                   '    "captcha_type": "slide",\n'
                                   '    "data": {\n'
                                   '      "captcha_id": "54088bb07d2df3c46b79f80300b0abbe",\n'
                                   '      "lot_number": "...",\n'
                                   '      "pass_token": "...",\n'
                                   '      "gen_time": "...",\n'
                                   '      "captcha_output": "..."\n'
                                   '    }\n'
                                   '  }\n'
                                   '}',
               'response_fields': [{'name': 'success', 'desc': '是否解码成功'},
                                   {'name': 'captcha_type', 'desc': '实际求解的验证类型（slide/word/...）'},
                                   {'name': 'data.captcha_id', 'desc': 'captcha_id'},
                                   {'name': 'data.lot_number', 'desc': '本次验证流水号'},
                                   {'name': 'data.pass_token', 'desc': '验证通过令牌（二次校验用）'},
                                   {'name': 'data.gen_time', 'desc': '令牌生成时间戳'},
                                   {'name': 'data.captcha_output', 'desc': '核心验证凭证（提交给业务后端）'}]},
 'geetestV3': {'title': '极验3代打码（自动识别类型）',
               'endpoints': [{'path': '/api/v1/solve', 'desc': '通用打码入口，type 传 geetestV3'}],
               'params': [{'name': 'gt',
                           'type': 'string',
                           'required': 'demo免填',
                           'default': '—',
                           'desc': '极验3代 gt（站点分配）。target=demo 且不传 gt/challenge 时由官方 demo 自动注册'},
                          {'name': 'challenge',
                           'type': 'string',
                           'required': '与gt成对',
                           'default': '—',
                           'desc': '极验3代 challenge（与 gt 成对传入）'},
                          {'name': 'target',
                           'type': 'string',
                           'required': False,
                           'default': 'demo',
                           'desc': 'demo=官方demo（自动注册 gt/challenge）；douyu=斗鱼登录'},
                          {'name': 'cookies',
                           'type': 'object',
                           'required': False,
                           'default': '—',
                           'desc': '斗鱼目标所需的登录 cookies'},
                          {'name': 'proxy',
                           'type': 'string',
                           'required': False,
                           'default': '—',
                           'desc': '本次调用专用 HTTP 代理'},
                          {'name': 'timeout',
                           'type': 'int',
                           'required': False,
                           'default': '240',
                           'desc': '单次求解超时秒数'}],
               'examples': [{'lang': 'curl',
                             'note': '官方 demo（自动注册 gt/challenge）',
                             'code': 'curl -X POST {api_base}/api/v1/solve \\\n'
                                     '  -H "X-API-Key: 你的TOKEN" \\\n'
                                     '  -H "Content-Type: application/json" \\\n'
                                     '  -d \'{"type":"geetestV3","param":{"target":"demo"}}\''},
                            {'lang': 'curl',
                             'note': '自有站点（自带 gt/challenge）',
                             'code': 'curl -X POST {api_base}/api/v1/solve \\\n'
                                     '  -H "X-API-Key: 你的TOKEN" \\\n'
                                     '  -H "Content-Type: application/json" \\\n'
                                     '  -d '
                                     '\'{"type":"geetestV3","param":{"gt":"你的gt","challenge":"你的challenge"}}\''}],
               'response_example': '{\n'
                                   '  "success": true,\n'
                                   '  "type": "geetestV3",\n'
                                   '  "credits_spent": 15,\n'
                                   '  "result": {\n'
                                   '    "success": true,\n'
                                   '    "message": "验证码已通过",\n'
                                   '    "captcha_type": "slide",\n'
                                   '    "data": {\n'
                                   '      "challenge": "...",\n'
                                   '      "validate": "...",\n'
                                   '      "seccode": "...|jordan"\n'
                                   '    }\n'
                                   '  }\n'
                                   '}',
               'response_fields': [{'name': 'success', 'desc': '是否解码成功'},
                                   {'name': 'captcha_type',
                                    'desc': '实际求解的验证类型（slide/click/success）'},
                                   {'name': 'data.challenge', 'desc': 'challenge（可能被服务端刷新）'},
                                   {'name': 'data.validate', 'desc': '核心验证凭证（提交给业务后端）'},
                                   {'name': 'data.seccode', 'desc': 'validate 的 seccode 形式'}]}}

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
}
''',
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

# ── 极验 V3/V4（算法在 /data/jiyan，文档随定价表动态组装） ──────
DOCS_REGISTRY.update(_GEETEST_DOCS)

# ── 新增：易盾 / 极验4 / 阿里云 / 数美（纯协议打码） ────────────
DOCS_REGISTRY.update({
    "yidun": {
        "title": "网易易盾滑块打码（纯协议）",
        "endpoints": [
            {"path": "/api/v1/solve", "desc": "通用入口（type=yidun）"},
        ],
        "params": [
            {"name": "captcha_id", "type": "string", "required": False, "default": "官方体验页 ID", "desc": "易盾业务 captchaId（如 <code>6e2c5b8f1a9d4c3e8b7a6f5d4c3b2a19</code>）；不传则使用官方体验页 ID"},
            {"name": "referer", "type": "string", "required": False, "default": "https://dun.163.com/trial/jigsaw", "desc": "触发验证的业务页面 URL"},
            {"name": "width", "type": "int", "required": False, "default": "320", "desc": "滑块渲染宽度（px），影响轨迹与缺口计算"},
            {"name": "max_retries", "type": "int", "required": False, "default": "2", "desc": "识别失败最大重试次数"},
            {"name": "proxy", "type": "string", "required": False, "default": "—", "desc": "HTTP 代理，如 <code>http://user:pass@ip:port</code>"},
        ],
        "examples": [
            {
                "lang": "Python (requests)",
                "note": "通用入口",
                "code": 'import requests\n\nr = requests.post("{api_base}/api/v1/solve",\n    headers={{"X-API-Key": "你的TOKEN"}},\n    json={{"type": "yidun",\n           "param": {{"captcha_id": "你的业务ID"}}}})\nprint(r.json())'
            },
            {
                "lang": "curl",
                "note": "命令行调用",
                "code": 'curl -X POST {api_base}/api/v1/solve \\\n  -H "X-API-Key: 你的TOKEN" \\\n  -H "Content-Type: application/json" \\\n  -d \'{{"type":"yidun","param":{{"captcha_id":"你的业务ID"}}}}\''
            },
        ],
        "response_example": '''{
  "success": true,
  "type": "yidun",
  "credits_spent": 15,
  "credits_left": 985,
  "result": {
    "success": true,
    "message": "ok",
    "data": {
      "token": "xxxxx.yyyyy.zzzzz",
      "captcha_id": "6e2c5b8f...",
      "attempt": 1,
      "elapsed_s": 1.82
    }
  }
}''',
        "response_fields": [
            {"name": "success", "type": "bool", "desc": "请求是否成功（业务层面）"},
            {"name": "type", "type": "string", "desc": "调用的验证码类型"},
            {"name": "credits_spent", "type": "int", "desc": "本次成功消耗的积分"},
            {"name": "credits_left", "type": "int", "desc": "扣除后账户剩余积分"},
            {"name": "result.data.token", "type": "string", "desc": "核心验证凭证 validate（提交给业务后端）"},
            {"name": "result.data.captcha_id", "type": "string", "desc": "实际使用的易盾业务 ID"},
            {"name": "result.data.attempt", "type": "int", "desc": "第几次尝试成功"},
            {"name": "result.data.elapsed_s", "type": "float", "desc": "解题耗时（秒）"},
        ],
    },
    "geetest4": {
        "title": "极验4 risk_type=ai 一键通过（纯协议）",
        "endpoints": [
            {"path": "/api/v1/solve", "desc": "通用入口（type=geetest4）"},
        ],
        "params": [
            {"name": "captcha_id", "type": "string", "required": False, "default": "官方体验页 ID", "desc": "极验4 captchaId（32 位十六进制，如 <code>24f2e1a0b3c4d5e6f7a8b9c0d1e2f3a4</code>）；不传则使用官方体验页 ID"},
            {"name": "max_retries", "type": "int", "required": False, "default": "2", "desc": "识别失败最大重试次数"},
            {"name": "proxy", "type": "string", "required": False, "default": "—", "desc": "HTTP 代理"},
        ],
        "examples": [
            {
                "lang": "Python (requests)",
                "note": "通用入口",
                "code": 'import requests\n\nr = requests.post("{api_base}/api/v1/solve",\n    headers={{"X-API-Key": "你的TOKEN"}},\n    json={{"type": "geetest4",\n           "param": {{"captcha_id": "你的captchaId", "risk_type": "ai"}}}})\nprint(r.json())'
            },
            {
                "lang": "curl",
                "note": "命令行调用",
                "code": 'curl -X POST {api_base}/api/v1/solve \\\n  -H "X-API-Key: 你的TOKEN" \\\n  -H "Content-Type: application/json" \\\n  -d \'{{"type":"geetest4","param":{{"captcha_id":"你的captchaId"}}}}\''
            },
        ],
        "response_example": '''{
  "success": true,
  "type": "geetest4",
  "credits_spent": 10,
  "credits_left": 990,
  "result": {
    "success": true,
    "message": "ok",
    "data": {
      "token": "{...seccode...}",
      "lot_number": "...",
      "result": "success",
      "attempt": 1,
      "elapsed_s": 1.35
    }
  }
}''',
        "response_fields": [
            {"name": "success", "type": "bool", "desc": "请求是否成功（业务层面）"},
            {"name": "result.data.token", "type": "string", "desc": "核心验证凭证 seccode / pass_token（提交给业务后端）"},
            {"name": "result.data.lot_number", "type": "string", "desc": "本次验证流水号（与 seccode 配对使用）"},
            {"name": "result.data.result", "type": "string", "desc": "极验接口返回的验证结论"},
            {"name": "result.data.attempt", "type": "int", "desc": "第几次尝试成功"},
        ],
    },
    "aliyun_captcha": {
        "title": "阿里云 Captcha 2.0 滑块打码（纯协议）",
        "endpoints": [
            {"path": "/api/v1/solve", "desc": "通用入口（type=aliyun_captcha）"},
        ],
        "params": [
            {"name": "keyword", "type": "string", "required": False, "default": "java", "desc": "触发验证码的业务搜索关键词（驱动业务腿流程）"},
            {"name": "max_retries", "type": "int", "required": False, "default": "2", "desc": "识别失败最大重试次数"},
            {"name": "proxy", "type": "string", "required": False, "default": "—", "desc": "HTTP 代理"},
        ],
        "examples": [
            {
                "lang": "Python (requests)",
                "note": "通用入口",
                "code": 'import requests\n\nr = requests.post("{api_base}/api/v1/solve",\n    headers={{"X-API-Key": "你的TOKEN"}},\n    json={{"type": "aliyun_captcha",\n           "param": {{"keyword": "java"}}}})\nprint(r.json())'
            },
            {
                "lang": "curl",
                "note": "命令行调用",
                "code": 'curl -X POST {api_base}/api/v1/solve \\\n  -H "X-API-Key: 你的TOKEN" \\\n  -H "Content-Type: application/json" \\\n  -d \'{{"type":"aliyun_captcha","param":{{"keyword":"java"}}}}\''
            },
        ],
        "response_example": '''{
  "success": true,
  "type": "aliyun_captcha",
  "credits_spent": 20,
  "credits_left": 980,
  "result": {
    "success": true,
    "message": "ok",
    "data": {
      "token": "....captchaVerifyParam....",
      "keyword": "java",
      "stage": "done",
      "total": 128
    }
  }
}''',
        "response_fields": [
            {"name": "success", "type": "bool", "desc": "请求是否成功（业务层面）"},
            {"name": "result.data.token", "type": "string", "desc": "核心验证凭证 captchaVerifyParam（提交给业务后端）"},
            {"name": "result.data.keyword", "type": "string", "desc": "本次使用的业务关键词"},
            {"name": "result.data.stage", "type": "string", "desc": "业务腿结束阶段"},
            {"name": "result.data.total", "type": "int", "desc": "业务接口返回的数据总量（佐证请求已通过验证）"},
        ],
    },
    "shumei": {
        "title": "数美智能验证码打码（空间推理 / 文字点选 / 图标点选）",
        "endpoints": [
            {"path": "/api/v1/solve", "desc": "通用入口（type=shumei）"},
        ],
        "params": [
            {"name": "mode", "type": "string", "required": False, "default": "spatial_select", "desc": "题目类型：<code>spatial_select</code> 空间推理 ✅ / <code>select</code> 文字点选 ✅ / <code>icon_select</code> 图标点选（实验） / <code>slide</code> 滑块（实验） / <code>seq_select</code> 语序点选（需配置 VLM）"},
            {"name": "max_retries", "type": "int", "required": False, "default": "2", "desc": "识别失败最大重试次数"},
            {"name": "proxy", "type": "string", "required": False, "default": "—", "desc": "HTTP 代理"},
        ],
        "examples": [
            {
                "lang": "Python (requests)",
                "note": "通用入口（默认空间推理）",
                "code": 'import requests\n\nr = requests.post("{api_base}/api/v1/solve",\n    headers={{"X-API-Key": "你的TOKEN"}},\n    json={{"type": "shumei",\n           "param": {{"mode": "spatial_select"}}}})\nprint(r.json())'
            },
            {
                "lang": "curl",
                "note": "命令行调用",
                "code": 'curl -X POST {api_base}/api/v1/solve \\\n  -H "X-API-Key: 你的TOKEN" \\\n  -H "Content-Type: application/json" \\\n  -d \'{{"type":"shumei","param":{{"mode":"spatial_select"}}}}\''
            },
        ],
        "response_example": '''{
  "success": true,
  "type": "shumei",
  "credits_spent": 25,
  "credits_left": 975,
  "result": {
    "success": true,
    "message": "ok",
    "data": {
      "token": "....rid....",
      "mode": "spatial_select",
      "riskLevel": "PASS",
      "code": 1100
    }
  }
}''',
        "response_fields": [
            {"name": "success", "type": "bool", "desc": "请求是否成功（业务层面）"},
            {"name": "result.data.token", "type": "string", "desc": "核心验证凭证 rid（提交给业务后端做二次验证）"},
            {"name": "result.data.mode", "type": "string", "desc": "本次实际解出的题目类型"},
            {"name": "result.data.riskLevel", "type": "string", "desc": "数美返回的风险等级（PASS / REJECT 等）"},
            {"name": "result.data.code", "type": "int", "desc": "数美返回码（1100 表示通过）"},
        ],
    },
})


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
