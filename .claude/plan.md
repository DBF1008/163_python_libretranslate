# 文件翻译异步任务能力 — 实现计划

## 背景

当前 `POST /translate_file` 是同步阻塞的：客户端上传文件后必须等待翻译完成才能拿到结果。对于大文件，`argostranslatefiles.translate_file()` 可能耗时数十秒甚至数分钟，导致 HTTP 请求超时、Worker 线程被长时间占用、客户端无法查询进度或获取失败原因。

**目标**：新增可选的异步任务接口，客户端可以提交任务→轮询状态→完成后下载，同时保持原有同步接口完全不变。

## 设计方案

### 核心思路
- **新增独立端点**，不修改现有 `/translate_file` 的语义
- 任务状态存储在已有的 `storage.py` 抽象层（Memory / Redis），不引入新数据库
- 翻译工作在 `threading.Thread(daemon=True)` 中执行，用 `Semaphore` 控制并发
- TTL 与现有文件清理周期对齐（30 分钟），无需额外文件清理逻辑

### 新增 API

| 端点 | 方法 | 用途 |
|------|------|------|
| `/translate_file_async` | POST | 提交异步翻译任务，返回 `taskId` |
| `/tasks/<task_id>` | GET | 查询任务状态（pending/processing/completed/failed） |

**现有端点不变**：`POST /translate_file`（同步）、`GET /download_file/<filename>`（下载）

### 任务状态流转

```
提交 → pending → processing → completed（可下载）
                             → failed（含 error 信息）
```

### 任务记录（JSON，存于 storage key `task:<uuid>`）

```python
{
    "task_id": str,
    "status": "pending" | "processing" | "completed" | "failed",
    "source_lang": str,
    "target_lang": str,
    "filename": str,           # 上传源文件 UUID 名
    "req_cost": int,
    "api_key": str | None,
    "client_ip": str,
    "error": str | None,
    "translated_filename": str | None,
    "created": float           # time.time()
}
```

### 并发与清理

- `threading.Semaphore(max_tasks)` 限制同时翻译数
- 任务 key 设 TTL=1800s（30 分钟），自动过期
- 定时清理（5 分钟）检测卡在 `processing` 超过 15 分钟的任务，标记为 `failed`
- 文件清理由现有 `remove_translated_files.py` 完成（30 分钟周期），无需修改

### 速率限制

在提交时（非执行时）应用，与同步端点使用相同的 `request.req_cost` 公式，通过已有的 `@access_check` + Flask-Limiter 机制生效。

## 文件变更清单

### 1. 新建 `libretranslate/file_tasks.py`（~150 行）

任务引擎核心模块：
- `TaskStatus` 常量类：`PENDING / PROCESSING / COMPLETED / FAILED`
- `create_task(source_lang, target_lang, filename, req_cost, api_key, client_ip)` → 生成 UUID，写入 storage，返回 task dict
- `get_task(task_id)` → 从 storage 读取，反序列化 JSON，TTL 过期则返回 None
- `update_task(task_id, **fields)` → 读取-修改-写回
- `submit_translation(task_id, translator, filepath, upload_dir)` → 创建 daemon 线程执行翻译
- `_do_translation(task_id, translator, filepath, upload_dir)` → 线程目标：acquire semaphore → 更新为 processing → 调用 `argostranslatefiles.translate_file()` → 更新为 completed/failed → release semaphore
- `cleanup_stale_tasks()` → 供 scheduler 调用，将超时的 processing 任务标记为 failed
- `setup(max_concurrent_tasks)` → 初始化 semaphore

### 2. 修改 `libretranslate/app.py`

**a) 新增 import**（第 24 行附近）：
```python
from libretranslate import file_tasks
```

**b) 在 `create_app()` 中初始化**（第 209 行附近，`remove_translated_files.setup` 之后）：
```python
if not args.disable_files_translation:
    file_tasks.setup(getattr(args, 'max_async_tasks', 4))
```

**c) 新增路由 `POST /translate_file_async`**（在第 1027 行 `translate_file` 路由之后）：
- 复用与 `translate_file` 相同的验证逻辑（文件、语言、格式检查）
- 保存上传文件、计算 req_cost
- 调用 `file_tasks.create_task()` + `file_tasks.submit_translation()`
- 返回 `{"taskId": "...", "status": "pending", "statusUrl": "/tasks/..."}`

**d) 新增路由 `GET /tasks/<task_id>`**（在 download_file 路由之后）：
- 调用 `file_tasks.get_task(task_id)`
- 404 if None
- 返回任务状态 JSON（completed 时包含 `translatedFileUrl`，failed 时包含 `error`）
- 此端点不需要 `@access_check`（查询不消耗翻译配额）

### 3. 修改 `libretranslate/default_values.py`

在 `_default_options_objects` 列表末尾（`URL_PREFIX` 之后）添加：
```python
{
    'name': 'MAX_ASYNC_TASKS',
    'default_value': 4,
    'value_type': 'int'
},
```

### 4. 修改 `libretranslate/main.py`

在 `--disable-files-translation` 参数之后添加：
```python
parser.add_argument(
    "--max-async-tasks",
    default=DEFARGS['MAX_ASYNC_TASKS'],
    type=int,
    metavar="<number>",
    help="Maximum number of concurrent async file translation tasks (%(default)s)",
)
```

### 5. 修改 `libretranslate/scheduler.py`

在 `setup()` 函数中，`rotate_secrets` 之后添加清理定时任务：
```python
if not args.secondary:
    from libretranslate.file_tasks import cleanup_stale_tasks
    scheduler.add_job(func=cleanup_stale_tasks, trigger="interval", minutes=5)
```

### 6. 新建 `libretranslate/tests/test_api/test_api_translate_file_async.py`（~250 行）

使用 Flask test client + monkeypatch `argostranslatefiles.translate_file`，覆盖：

| 场景 | 验证点 |
|------|--------|
| 提交成功 | 返回 200 + taskId + statusUrl |
| 轮询 pending/processing | status 字段正确 |
| 翻译完成后轮询 | status=completed + translatedFileUrl |
| 下载已完成翻译 | 文件内容正确 |
| 翻译失败 | status=failed + error 字段 |
| 查询不存在的任务 | 返回 404 |
| 缺少文件参数 | 返回 400 |
| 不支持的文件格式 | 返回 400 |
| 无效语言 | 返回 400 |
| 任务过期清理 | TTL 过期后查询返回 404 |
| 同步端点回归 | `POST /translate_file` 仍然正常工作 |

## 实现顺序

1. `libretranslate/file_tasks.py` — 核心任务引擎
2. `libretranslate/default_values.py` + `libretranslate/main.py` — 配置项
3. `libretranslate/app.py` — 路由与初始化
4. `libretranslate/scheduler.py` — 清理定时任务
5. `libretranslate/tests/test_api/test_api_translate_file_async.py` — 回归测试

## 验证方式

1. 运行测试套件：`python -m pytest libretranslate/tests/test_api/test_api_translate_file_async.py -v`
2. 确认现有测试不受影响：`python -m pytest libretranslate/tests/ -v`
3. 手动验证同步端点 `/translate_file` 行为不变
