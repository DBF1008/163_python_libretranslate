# 统一 API Key 鉴权策略 & 修复漂移

## 问题概述

API key 放行规则分散在三处：后端 `access_check` 装饰器、`/frontend/settings` 接口、前端 `app.js.template`。已确认 5 处漂移/缺陷：

| # | 问题 | 文件 | 行号 |
|---|------|------|------|
| 1 | `keyRequired` 只看 `require_api_key_origin`，遗漏 secret/fingerprint/under_attack | `app.py` | 1211 |
| 2 | 文件翻译漏了 `atob()` 解码，base64 原文 vs 明文 secret 永远不匹配 | `app.js.template` | 412 |
| 3 | `flood.report()` 放在 `abort()` 后面，永远不执行（死代码） | `app.py` | 388 |
| 4 | 前端 `disableInput` 只看 `under_attack`，忽略其他强制要 key 的场景 | `app.js.template` | 179-181 |
| 5 | `/frontend/settings` 不返回 `underAttack` 标志 | `app.py` | 1206-1226 |

## 修改计划

### 步骤 1：`libretranslate/app.py` — 新增 `is_key_required()` + 修复三处问题

**1a. 新增模块级函数**（插入到 `get_fingerprint()` 之后，约第 116 行后）

```python
def is_key_required(args):
    """配置级判断：当前服务器设置是否要求未认证请求必须携带 API key。
    access_check 装饰器与 /frontend/settings 共用此逻辑，保持一致。"""
    if not args.api_keys:
        return False
    if args.under_attack:
        return True
    if args.require_api_key_origin:
        return True
    if args.require_api_key_secret:
        return True
    if args.require_api_key_fingerprint:
        return True
    return False
```

**1b. 重构 `access_check`**（第 333-388 行）
- `if args.api_keys:` → `if is_key_required(args):`（语义等价，因为 `is_key_required` 内部先检查 `api_keys`，且无 enforcement 标志时 `need_key` 本来就保持 `False`）
- 将 `flood.report(get_remote_address())` 从 `abort()` **之后**移到 **之前**，修复死代码

**1c. 修复 `/frontend/settings` 响应**（第 1211 行）
```python
# 旧：
"keyRequired": bool(args.api_keys and args.require_api_key_origin),
# 新：
"keyRequired": is_key_required(args),
"underAttack": args.under_attack,
```

### 步骤 2：`libretranslate/templates/app.js.template` — 修复两处前端问题

**2a. 修复文件翻译缺失 `atob()`**（第 412 行）
```javascript
// 旧：
if (self.apiSecret) data.append("secret", self.apiSecret);
// 新：
if (self.apiSecret) data.append("secret", atob(self.apiSecret));
```

**2b. 更新 `disableInput` 计算属性**（第 179-181 行）
```javascript
// 旧：
disableInput: function(){
    return {% if under_attack %}true{% else %}false{% endif %} && this.apiKey === "";
}
// 新：
disableInput: function(){
    return (this.settings.keyRequired || {% if under_attack %}true{% else %}false{% endif %}) && this.apiKey === "";
}
```
> `this.settings.keyRequired` 在 settings XHR 完成前为 `undefined`（falsy），此时仍靠模板渲染的 `under_attack` 兜底。XHR 完成后 `keyRequired` 接管所有 enforcement 模式。

### 步骤 3：`libretranslate/tests/test_api/conftest.py` — 增加工厂 fixture

新增 `make_app` / `make_client` 工厂 fixture，支持用不同 CLI 参数创建测试 app：

```python
@pytest.fixture()
def make_app():
    def _make_app(extra_args=None):
        argv = ['', '--load-only', 'en,es']
        if extra_args:
            argv.extend(extra_args)
        sys.argv = argv
        return create_app(get_args())
    return _make_app

@pytest.fixture()
def make_client(make_app):
    def _make_client(extra_args=None):
        app = make_app(extra_args)
        return app.test_client()
    return _make_client
```

### 步骤 4：新增回归测试

**4a. `test_api/test_auth.py`（新文件）**— `is_key_required()` 纯函数单元测试

用 `SimpleNamespace` mock args 对象，覆盖：
- 无 api_keys → False
- api_keys 但无 enforcement 标志 → False
- under_attack → True
- require_api_key_origin → True
- require_api_key_secret → True
- require_api_key_fingerprint → True
- 多标志组合 → True
- enforcement 标志但无 api_keys → False

**4b. 扩展 `test_api/test_api_frontend_settings.py`** — 集成测试

通过 HTTP 验证：
- 默认配置：`keyRequired=false`, `underAttack=false`
- `--api-keys --require-api-key-origin`：`keyRequired=true`
- `--api-keys --require-api-key-secret`：`keyRequired=true`
- `--api-keys --require-api-key-fingerprint`：`keyRequired=true`
- `--api-keys --under-attack`：`keyRequired=true` + `underAttack=true`
- `--api-keys` 无 enforcement：`keyRequired=false`
- under_attack 下无 key 的 translate 请求返回 400
- under_attack 下带无效 key 返回 403（不是 400）
- 默认配置下 translate 正常 200

## 修改文件清单

| 文件 | 变更类型 |
|------|----------|
| `libretranslate/app.py` | 修改（新增函数 + 重构 access_check + 修复 settings） |
| `libretranslate/templates/app.js.template` | 修改（atob 修复 + disableInput 增强） |
| `libretranslate/tests/test_api/conftest.py` | 修改（增加工厂 fixture） |
| `libretranslate/tests/test_api/test_api_frontend_settings.py` | 修改（扩展测试） |
| `libretranslate/tests/test_api/test_auth.py` | 新增 |

## 验证方式

```bash
pytest libretranslate/tests/test_api/test_auth.py -v          # 单元测试
pytest libretranslate/tests/test_api/test_api_frontend_settings.py -v  # 集成测试
pytest libretranslate/tests/ -v                                # 全套回归
```
