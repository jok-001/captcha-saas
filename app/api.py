import time
import functools
import app.db as db
import config
from app.solver import dispatch, UnsupportedType, SolverError
from flask import Blueprint, request, jsonify

bp = Blueprint("api", __name__, url_prefix="/api")


def _get_api_token():
    # 三选一：X-API-Key / Authorization: Bearer / ?key=
    tok = request.headers.get("X-API-Key") or request.headers.get("X-Api-Key")
    if not tok:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            tok = auth[7:].strip()
    if not tok:
        tok = request.args.get("key") or (request.get_json(silent=True) or {}).get("key")
    return tok


def _auth_token(tok):
    if not tok:
        return None, None, ("missing service token", 401)
    row = db.get_token_row(tok)
    if not row:
        return None, None, ("invalid service token", 401)
    if row["status"] != "active":
        return None, None, ("token disabled", 403)
    u = db.get_user(row["user_id"])
    if not u:
        return None, None, ("user not found", 401)
    if u["status"] != "active":
        return None, None, ("user disabled", 403)
    return u, row, None


def _error(msg, code):
    return jsonify({"success": False, "error": msg, "code": code}), code


def _do(type_name, param, solve_only):
    t0 = time.time()
    tok = _get_api_token()
    user, token_row, err = _auth_token(tok)
    # 鉴权失败不记录（无有效用户/Token）
    if err:
        return _error(err[0], err[1])

    def _log_fail(points, detail, code):
        dur = int((time.time() - t0) * 1000)
        db.log_usage(user["id"], token_row["id"], type_name, 0, 0, dur, detail)
        return _error(detail.get("error", ""), code)

    price_row = db.get_price(type_name)
    if not price_row:
        return _log_fail(None, {"error": "unknown type: %s" % type_name}, 400)
    if not price_row["enabled"]:
        return _log_fail(price_row["points"], {"error": "type not available yet: %s" % type_name}, 501)

    points = price_row["points"]
    if user["credits"] < points:
        return _log_fail(points, {"error": "insufficient credits (have %d, need %d)" % (user["credits"], points)}, 402)

    try:
        proxy = param.get("proxy")
        res = dispatch(type_name, param, proxy=proxy, solve_only=solve_only)
    except UnsupportedType as e:
        return _log_fail(points, {"error": str(e)}, 501)
    except SolverError as e:
        return _log_fail(points, {"error": str(e)}, 400)
    except Exception as e:
        return _log_fail(points, {"error": repr(e)}, 500)

    dur = int((time.time() - t0) * 1000)
    success = bool(res.get("success")) if isinstance(res, dict) else False
    if success:
        db.adjust_credits(user["id"], -points, "api:%s" % type_name, None)
        db.log_usage(user["id"], token_row["id"], type_name, points, 1, dur, res)
    else:
        # 未成功不扣积分，仅记录
        db.log_usage(user["id"], token_row["id"], type_name, 0, 0, dur, res)
    db.touch_token(token_row["id"])

    return jsonify({
        "success": success,
        "type": type_name,
        "credits_spent": points if success else 0,
        "credits_left": user["credits"] - (points if success else 0),
        "result": res,
    }), 200


@bp.route("/health", methods=["GET"])
def health():
    pricing = db.get_pricing()
    available = [p["type"] for p in pricing if p["enabled"]]
    return jsonify({"status": "ok", "available_types": available})


@bp.route("/balance", methods=["GET", "POST"])
def balance():
    """用服务 token 查询余额/用量（GET 或 POST 均可）。"""
    tok = _get_api_token()
    user, token_row, err = _auth_token(tok)
    if err:
        return _error(err[0], err[1])
    total, by_type = db.usage_summary(user["id"])
    return jsonify({
        "success": True,
        "username": user["username"],
        "email": user.get("email"),
        "credits": user["credits"],
        "token": "%s…%s" % (token_row["token"][:8], token_row["token"][-4:]),
        "token_status": token_row["status"],
        "usage": {
            "total_calls": total["c"],
            "success": total["s"],
            "total_spent": total["p"],
        },
        "by_type": by_type,
    })


@bp.route("/huya_send_phone", methods=["POST"])
def huya_send_phone():
    data = request.get_json(silent=True) or {}
    param = data.get("param", data)
    return _do("huya_v3", param, solve_only=False)


@bp.route("/huya_captcha", methods=["POST"])
def huya_captcha():
    data = request.get_json(silent=True) or {}
    param = data.get("param", data)
    return _do("huya_v3", param, solve_only=True)


@bp.route("/solve", methods=["POST"])
def solve_compat():
    data = request.get_json(silent=True) or {}
    param = data.get("param", data)
    t = data.get("type", "huya_v3")
    solve_only = data.get("solve_only", False)
    return _do(t, param, solve_only=solve_only)


@bp.route("/v1/solve", methods=["POST"])
def solve_v1():
    data = request.get_json(silent=True) or {}
    t = data.get("type", "huya_v3")
    param = data.get("param", {})
    solve_only = data.get("solve_only", False)
    return _do(t, param, solve_only=solve_only)
