"""Solver dispatch: 直接调用 solvers/ 下的类函数。

solvers/ 目录包含各打码平台的实现代码，每个平台导出一个类，
实现统一的 run(param, proxy, solve_only) -> dict 接口。

本文件通过注册表将 type_name 映射到具体的 solver 实现。
新增打码平台只需：1) 在 solvers/ 下新建目录并实现类  2) 在此注册。
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SOLVER_DIR = os.path.join(BASE_DIR, "solvers")
if _SOLVER_DIR not in sys.path:
    sys.path.insert(0, _SOLVER_DIR)


class SolverError(Exception):
    """打码失败（参数错误、求解未成功等）"""


class UnsupportedType(SolverError):
    """不支持的验证码类型"""


# ── 注册表 ──────────────────────────────────────────
# type_name → adapter 函数
_SOLVERS = {}


def _register(*type_names):
    """装饰器：将一个 adapter 函数注册到一个或多个 type_name 下。"""
    def decorator(func):
        for name in type_names:
            _SOLVERS[name.lower()] = func
        return func
    return decorator


# ── 虎牙 V3 ──────────────────────────────────────────
@_register("huya_v3", "huya", "v3")
def _solve_huya(param, proxy=None, solve_only=False):
    """param: {phone, max_retries?, proxy?}

    phone 支持带区号格式：
      - 国内: "13800138000" → 默认 +86
      - 境外: "+85212345678" → 自动提取区号 852
    不再需要单独传 area_code 参数。
    """
    raw_phone = param.get("phone")
    if not raw_phone:
        raise SolverError("缺少参数 phone")

    phone = raw_phone.strip()
    area_code = None

    # 自动解析区号
    if phone.startswith("+"):
        idx = 1
        while idx < len(phone) and phone[idx].isdigit():
            idx += 1
        if idx > 1:
            area_code = phone[1:idx]
            phone = phone[idx:]
    elif phone.startswith("00") and len(phone) > 4:
        idx = 2
        while idx < len(phone) and idx < 6 and phone[idx].isdigit():
            idx += 1
        area_code = phone[2:idx]
        phone = phone[idx:]

    from solvers.huya.huya_v3 import HuyaV3
    return HuyaV3.run(
        phone,
        proxy=proxy or param.get("proxy"),
        area_code=area_code,  # None 时 solver 内部默认 "86"
        max_retries=param.get("max_retries"),
        solve_only=solve_only,
    )


# ── 腾讯 TCaptcha ────────────────────────────────────
@_register("tencent_tcaptcha", "tencent", "tcaptcha", "qq_captcha")
def _solve_tencent(param, proxy=None, solve_only=False):
    """param: {appid (必填), entry_url?, subsid?, max_retries?}"""
    if not isinstance(param, dict):
        raise SolverError("param 必须是 dict")
    if not (param.get("appid") or param.get("app_id")):
        raise SolverError("缺少参数 appid")
    from solvers.tencent.tcaptcha import TencentTCaptcha
    return TencentTCaptcha.run(
        param,
        proxy=proxy or param.get("proxy"),
        solve_only=solve_only,
    )


# ── 统一入口 ─────────────────────────────────────────
def dispatch(type_name, param, proxy=None, solve_only=False):
    """统一分发入口。返回 solver 原始结果 dict。"""
    t = (type_name or "").lower()
    solver = _SOLVERS.get(t)
    if not solver:
        raise UnsupportedType("不支持的验证码类型: %s" % type_name)
    return solver(param, proxy=proxy, solve_only=solve_only)
