# CaptchaSaaS — 验证码打码 SaaS 平台

> 开源的验证码打码服务平台，支持用户管理、积分制、Token 管理、API 调用计量。
> `solvers/` 目录包含打码算法实现（gitignore，不上传到仓库）。

---

## 架构设计

SaaS 系统通过直接 import 调用 `solvers/` 下的打码类函数，无需额外端口或独立服务：

```
captcha_saas/
├── app/
│   ├── solver.py         ← 统一调度入口（import 并调用 solvers/ 下的类函数）
│   ├── api.py            ← API 路由（鉴权、扣积分、记录用量）
│   ├── web.py            ← 用户 Web 页面
│   ├── admin.py          ← 管理后台
│   └── db.py             ← SQLite 数据层
├── solvers/              ← 打码算法（gitignore，不上传）
│   ├── huya/
│   │   └── huya_v3.py    ← 虎牙 V3（导出 HuyaV3 类）
│   └── tencent/
│       └── tcaptcha.py   ← 腾讯 TCaptcha（导出 TencentTCaptcha 类）
├── templates/            ← Jinja2 模板
├── static/               ← CSS/JS
├── data/saas.db          ← SQLite 数据库
├── config.py             ← 全局配置 + 默认定价
├── run.py                ← 应用入口
└── .gitignore            ← 排除 solvers/、data/、.env 等
```

**核心原则**：
- SaaS 系统开源（`solvers/` 已 gitignore），不含算法代码
- 统一调用入口：每个 solver 模块导出一个类，实现 `run(param, proxy, solve_only) -> dict`
- 直接函数调用，不开 HTTP 服务，不占额外端口
- 新增打码平台：在 `solvers/` 下新建目录 + 在 `solver.py` 注册一行

---

## 项目结构

```
captcha_saas/
├── app/
│   ├── __init__.py       # Flask 工厂，注册 web/admin/api 蓝图
│   ├── api.py            # API 路由（鉴权、扣积分、调用 solver、记录用量）
│   ├── web.py            # 用户 Web 页面（登录/注册/中心/用量/充值/文档）
│   ├── admin.py          # 管理后台页面（总览/用户/定价/流水/兑换码）
│   ├── db.py             # SQLite 数据层（用户/token/定价/用量/流水/兑换码）
│   └── solver.py         # Solver 调度器 — 直接 import 并调用 solvers/ 下的类函数
├── solvers/              # 打码算法实现（gitignore，不上传到仓库）
│   ├── huya/
│   │   └── huya_v3.py    # 虎牙 V3 — 导出 HuyaV3 类
│   └── tencent/
│       └── tcaptcha.py   # 腾讯 TCaptcha — 导出 TencentTCaptcha 类
├── templates/            # Jinja2 页面模板
├── static/               # CSS/JS
├── data/
│   └── saas.db           # SQLite 数据库
├── config.py             # 全局配置 + 默认定价表
├── run.py                # 应用入口
└── .gitignore            # 排除 solvers/、data/*.db、.env、venv/ 等
```

---

## Solver 统一接口规范

每个打码模块必须导出一个类，实现以下方法：

```python
class MySolver:
    @staticmethod
    def run(param: dict, proxy: str = None, solve_only: bool = False) -> dict:
        """
        param:      dict — 平台特定参数
        proxy:      str  — HTTP 代理地址（可选）
        solve_only: bool — True=只解验证码不执行后续动作，False=完整流程

        Returns:
            dict — 必须包含 'success' 键（bool），其余字段为平台特定结果
        """
        ...
```

**返回值约定**：
```python
{
    "success": True/False,    # 是否解码成功
    "message": "...",         # 结果描述
    "data": {...},            # 平台返回的原始数据
    # 可附带其他字段...
}
```

---

## 接入新打码平台 — 完整步骤

以接入「极验 GeeTest」为例：

### Step 1 — 在 solvers/ 下新建目录和模块

```
solvers/geetest/
└── geetest_v4.py
```

```python
# solvers/geetest/geetest_v4.py
class GeeTestV4:
    @staticmethod
    def run(param, proxy=None, solve_only=False):
        gt = param.get("gt")
        challenge = param.get("challenge")
        if not (gt and challenge):
            raise ValueError("缺少参数 gt/challenge")
        # ... 你的打码逻辑 ...
        return {"success": True, "message": "验证码已通过", "data": {...}}
```

### Step 2 — 在 solver.py 注册

```python
# app/solver.py
from solvers.geetest.geetest_v4 import GeeTestV4

@_register("geetest_v4", "geetest", "geetest4")
def _solve_geetest(param, proxy=None, solve_only=False):
    return GeeTestV4.run(param, proxy=proxy, solve_only=solve_only)
```

### Step 3 — 在管理后台启用

登录 `/admin/pricing`，新增或编辑：
- 类型名: `geetest_v4`
- 积分/次: `20`
- 启用: 勾选

### Step 4 — 调用

```bash
curl -X POST http://<ip>:9000/api/v1/solve \
  -H "X-API-Key: YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"geetest_v4","param":{"gt":"...","challenge":"..."}}'
```

---

## 数据库表结构

| 表 | 用途 | 关键字段 |
|---|---|---|
| `users` | 用户 | `id`, `username`, `email`, `credits`, `role`, `status` |
| `tokens` | API 密钥 | `user_id`, `token`, `status`（单用户单 active） |
| `pricing` | 定价 | `type`(PK), `points`, `enabled`, `note` |
| `usage_logs` | 用量记录 | `user_id`, `type`, `points`, `success`, `duration_ms` |
| `transactions` | 积分流水 | `user_id`, `change`, `balance_after`, `reason` |
| `redeem_codes` | 兑换码 | `code`, `points`, `used_by`, `note` |

---

## API 接口

所有 API 需要认证（`X-API-Key` / `Authorization: Bearer` / `?key=`）：

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/health` | GET | 健康检查，返回可用类型列表 |
| `/api/balance` | GET/POST | 查余额、用量 |
| `/api/v1/solve` | POST | 通用打码入口 `{type, param, solve_only}` |
| `/api/solve` | POST | 兼容旧版入口（同 /api/v1/solve） |
| `/api/huya_send_phone` | POST | 虎牙完整流程 |
| `/api/huya_captcha` | POST | 虎牙仅解验证码 |

成功才扣积分，失败不扣；积分不足返回 402，类型未接入返回 501。

### 手机号格式（虎牙 V3）

虎牙 V3 的 `phone` 参数支持自动区号解析，**无需单独传 `area_code`**：
- 国内号码：`"13800138000"` → 默认 +86
- 境外号码：`"+85212345678"` → 自动提取区号 852
- `00` 开头：`"0085212345678"` → 自动提取区号 852

---

## Web 页面

| 路径 | 说明 |
|---|---|
| `/login` | 登录 |
| `/register` | 注册（赠送积分） |
| `/dashboard` | 仪表盘 |
| `/profile` | 个人信息（Token / 邮箱 / 改密） |
| `/usage` | 调用记录（支持模糊搜索、结果过滤、时间范围） |
| `/transactions` | 积分流水（支持模糊搜索、收支过滤、时间范围） |
| `/recharge` | 兑换码充值 |
| `/docs` | **使用文档首页**（通用信息 + 接口列表 + 跳转按钮） |
| `/docs/<type>` | **单个接口详细文档**（参数、示例、响应格式） |
| `/admin/` | 管理后台（需 admin 角色） |

---

## 使用文档系统

`/docs` 页面重构为**文档索引 + 单接口详情页**两级结构：

### 1. `/docs` — 使用文档首页
- 通用信息：Base URL、协议、鉴权方式、积分模型
- 错误码速查表
- 已接入接口卡片列表（含积分单价、状态标签、跳转按钮）
- 管理员可见管理后台概览

### 2. `/docs/<type>` — 单接口详细文档
每个接入的打码接口都有独立详情页，包含：
- 接口名称 + 状态 + 积分单价
- 所有可用请求端点
- 参数表（字段名、类型、必填、默认值、说明）
- 请求示例（Python requests / curl_cffi / curl，语言 Tab 切换 + 一键复制）
- 响应格式示例
- 响应字段说明表

### 新增文档的方式

在 `app/web.py` 的 `DOCS_REGISTRY` 字典中添加类型条目：

```python
DOCS_REGISTRY = {
    "huya_v3": {
        "title": "虎牙 V3 验证码打码",
        "endpoints": [...],
        "params": [...],
        "examples": [...],
        "response_example": "...",
        "response_fields": [...],
    },
    "tencent_tcaptcha": { ... },
    # 新增类型在此添加
}
```

模板使用 `templates/docs_detail.html`，自动根据注册表渲染。

---

## 调用记录策略

所有 API 调用（含失败）均写入 `usage_logs`，便于用户排查问题：

| 场景 | 是否记录 |
|---|---|
| 调用成功 | ✅ 记录 + 扣积分 |
| 调用失败（solver 返回 success=false） | ✅ 记录，不扣积分 |
| 参数缺失 / solver 业务错误 | ✅ 记录 |
| 类型未接入 / 已停用 | ✅ 记录 |
| 积分不足 | ✅ 记录 |
| 服务端内部错误 | ✅ 记录 |
| Token 无效 / 缺失 | ❌ 不记录（无有效用户） |

---

## 部署

### SaaS 系统

```bash
# 创建 venv
python -m venv venv
source venv/bin/activate
pip install flask gunicorn

# 使用 gunicorn
gunicorn -w 2 -k gthread --threads 8 --timeout 300 -b 0.0.0.0:9000 run:app
```

环境变量（`.env`）：
- `HUYA_SECRET_KEY` — Flask 密钥
- `HUYA_ADMIN_KEY` — 管理员密钥
- `SIGNUP_BONUS` — 注册赠送积分（默认 20）
- `OWNER_TOKEN` — 兼容旧 token
- `INIT_ADMIN_USERNAME` — 初始管理员用户名（默认 admin）
- `INIT_ADMIN_PASSWORD` — 初始管理员密码（默认 admin123456）

### 打码算法

`solvers/` 目录下的各平台模块自包含，直接下载即可使用。
每个模块只需实现 `run(param, proxy, solve_only) -> dict` 接口。

---

## 许可证

MIT License
