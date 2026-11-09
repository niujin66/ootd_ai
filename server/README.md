# 后端运行与联调

在仓库根目录打开 PowerShell。后端提供目录、GLB 下载、整包导入和推荐接口。代码默认使用模拟推荐。

## 建环境与测试

已有 `server/.venv` 时直接使用；首次在已安装的 Python 3.12 下执行：

```powershell
python -m venv server/.venv
& .\server\.venv\Scripts\python.exe -m pip install -r server/requirements.txt
& .\server\.venv\Scripts\python.exe -m unittest discover -s server/tests -p 'test_*.py'
```

2026-11-09 队长本机执行 73 项，72 项通过、1 项因符号链接权限跳过。测试生成合成几何输入，C 记录自己机器的实际结果。本机依赖记录为 FastAPI 0.162.2、Uvicorn 0.54.0、httpx 0.28.1、trimesh 5.2.0、NumPy 2.5.4；其他版本组合需复测。

## 校验和导入内部包

先取得队长通过内部流程交接的完整包，按以下路径放置。当前演示人物版本为 `fixture-face-01`。

```powershell
$package = '.\outputs\handoff\person-001\fixture-face-01'
$python = '.\server\.venv\Scripts\python.exe'
& $python .\server\validate_asset_package.py $package --report .\outputs\asset-report.json
& $python .\server\import_package.py $package
```

命令退出码 0 表示成功，1 表示校验或版本冲突，2 表示用法错误。清单必填字段、ID 唯一、GLB 可读性、路径边界、大小及 SHA-256 会被检查。报告另列本人相似度和衣物贴合等人工检查。

整包导入先复制资产再在 SQLite 发布。相同版本和相同内容可以重复导入；相同版本不同内容需更换版本号。默认数据库为 `outputs/ootd.db`，存储为 `outputs/server-assets/`，可用 `OOTD_DB_PATH`、`OOTD_ASSETS_ROOT` 指定其他本地目录。

发布前校验副本 SHA-256，并通过写事务串行处理同版本导入。复制失败时只清理本次临时目录，已有未登记目录会报告冲突。人物、姿势、版本和资产 ID 使用 1–64 位字母、数字、短横线或下划线，并以字母或数字开头。

## 启动与请求

局域网使用前在本机设置访问令牌。下面的示例值需由团队在本地替换。

```powershell
$env:OOTD_AUTH_TOKEN = 'replace-with-your-team-token'
$env:OOTD_AI_MODE = 'mock'
& .\server\.venv\Scripts\python.exe -m uvicorn app:app --app-dir server --host 0.0.0.0 --port 8000
```

在另一个终端执行以下请求。手机与电脑连接同一局域网，手机配置的地址使用电脑局域网 IP，令牌与服务端一致。

```powershell
$base = 'http://127.0.0.1:8000'
$headers = @{Authorization = 'Bearer replace-with-your-team-token'}
Invoke-RestMethod "$base/v1/health"
$catalog = Invoke-RestMethod "$base/v1/catalog?person_id=person-001" -Headers $headers
$catalog
$body = @{
    request_id = 'request-001'
    person_id = 'person-001'
    catalog_version = $catalog.catalog_version
    scene = '下午上课'
    preference = '优先浅色'
} | ConvertTo-Json
Invoke-RestMethod "$base/v1/recommendations" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

`GET /v1/health` 是公开健康检查。设置令牌后，目录、文件和推荐接口需要 Bearer 令牌。未设置令牌时服务以本地开发模式运行。

## Android 要用的字段

| 接口 | 返回或用途 |
| --- | --- |
| GET /v1/catalog?person_id=... | person_id、catalog_version、pose_id、assets |
| GET /v1/assets/{person_id}/{catalog_version}/{asset_id} | 已发布 GLB 文件 |
| POST /v1/recommendations | 回显 request_id/person_id/catalog_version，返回 status/garment_id/reason/mode |

每项目录资产有 `asset_id`、`asset_type`、`display_name`、`tags`、`available`、`asset_url`、`size_bytes`、`sha256`。`asset_url` 是相对于服务地址的路径。人物条目也需要下载；available 用来筛选可推荐衣物。

mock 选择按偏好中的浅/白/亮、深/黑/暗、红/粉匹配已有标签。无指定偏好选第一件可用衣物，指定颜色没有候选时返回 `status=no_match`、`garment_id=null`。手机显示模拟模式，并使用返回 ID 调用同一个换衣入口。

常见状态码包括 401 未授权、404 人物/版本/资产不存在、409 已发布旧版本的推荐请求过期、422 字段错误、502 模型输出不合格、503 模型未配置/超时/限流。错误体为 `{"error":{"code":"...","message":"...","request_id":"..."}}`。

## 真实 AI 接入状态

适配器支持兼容 `/chat/completions` 的服务，只传衣物 ID、标签、场合和偏好。结构、未知 ID 与禁用衣物由服务端独立校验。自动化测试模拟供应商输出，真实服务调用与效果仍待验证。

实际接入时由队长核实服务商文档并在本机配置：

| 环境变量 | 用途 |
| --- | --- |
| OOTD_AI_MODE | ai 启用模型，mock 用于联调 |
| OOTD_AI_BASE_URL | 服务商基础地址 |
| OOTD_AI_API_KEY | 服务端凭据 |
| OOTD_AI_MODEL | 服务商支持的模型名 |
| OOTD_AI_TIMEOUT_SECONDS | 调用超时 |
| OOTD_AI_MAX_CALLS_PER_DAY | 每日调用次数上限 |
| OOTD_AI_MAX_CALLS_PER_MINUTE | 分钟级次数上限 |
| OOTD_CALL_LOG | 本地调用记录路径 |

成功调用且结果通过校验才返回 `mode=ai`。费用、供应商用量、响应耗时和推荐效果在真实验证时记录。
