import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "saas.db")

SECRET_KEY = os.environ.get("HUYA_SECRET_KEY", "changeme-secret-key")
ADMIN_KEY = os.environ.get("HUYA_ADMIN_KEY", "changeme-admin-key")

# 新用户注册赠送积分（测试额度，可在 .env 里改）
SIGNUP_BONUS = int(os.environ.get("SIGNUP_BONUS", "20"))

# 管理后台初始管理员账号（首次启动建库时写入；请登录后尽快改密）
INIT_ADMIN_USERNAME = os.environ.get("INIT_ADMIN_USERNAME", "admin")
INIT_ADMIN_PASSWORD = os.environ.get("INIT_ADMIN_PASSWORD", "admin123456")
INIT_ADMIN_EMAIL = os.environ.get("INIT_ADMIN_EMAIL", "admin@localhost")

# 兼容旧的 owner token（旧客户端仍可继续用）
# 默认留空：生产环境请在 .env 里通过 OWNER_TOKEN 显式配置，避免把真实凭据写进仓库
OWNER_TOKEN = os.environ.get("OWNER_TOKEN", "")
OWNER_CREDITS = int(os.environ.get("OWNER_CREDITS", "9999999"))

APP_NAME = "CaptchaSaaS"
APP_DESC = "验证码打码 / 绕过 SaaS 服务平台"

# 默认定价（积分/次）。enabled=0 的类型尚未接入真实 solver，调用返回 501。
DEFAULT_PRICING = {
    "huya_v3": {"points": 10, "enabled": 1, "note": "虎牙 V3 验证码绕过（解验证码 + 发短信）"},
    "tencent_tcaptcha": {"points": 15, "enabled": 1, "note": "腾讯 TCaptcha 多类型自动分类"},
    "yidun": {"points": 15, "enabled": 1, "note": "网易易盾滑块（纯协议，传 captcha_id，返回 validate）"},
    "aliyun_captcha": {"points": 20, "enabled": 1, "note": "阿里云 Captcha 2.0（传 captcha_id+scene+certify_id，返回 captchaVerifyParam）"},
    "shumei": {"points": 25, "enabled": 1, "note": "数美智能验证码（传 organization，mode：spatial_select ✅/select ✅/icon_select、slide 实验/seq_select 需 VLM）"},
    "geetestV4": {"points": 20, "enabled": 1, "note": "极验4（传 captcha_id；riskType 支持 ai 无感 ✅、slide 滑块 ✅、match/voice/winlinze、auto 自动探测；返回 pass_token/captcha_output 或 seccode）"},
    "geetestV3": {"points": 15, "enabled": 1, "note": "极验3（传 gt+challenge；支持 slide 滑块、click 点选等；返回 validate/seccode）"},
    "google": {"points": 15, "enabled": 0, "note": "Google reCAPTCHA（规划中）"},
    "cf": {"points": 30, "enabled": 0, "note": "Cloudflare Turnstile（规划中）"},
}

API_BASE = os.environ.get("API_BASE", "http://localhost:9000")
