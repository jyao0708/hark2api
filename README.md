# hark2api

<p align="center">
  <strong>将 Hark 网页端能力转换为 OpenAI 兼容 API</strong>
</p>

<p align="center">
  <a href="https://github.com/jyao0708/hark2api"><img src="https://img.shields.io/badge/GitHub-jyao0708%2Fhark2api-181717?logo=github" alt="GitHub Repository"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white" alt="Docker ready">
  <img src="https://img.shields.io/badge/OpenAI-Compatible-2F855A" alt="OpenAI compatible">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-F59E0B" alt="MIT License"></a>
</p>

<p align="center">
  <a href="https://github.com/jyao0708/hark2api">项目主页</a>
  ·
  <a href="#社区交流">加入 QQ 群 1124104343</a>
</p>

> [!WARNING]
> Hark2API 只适合个人学习、研究和自托管使用。请遵守 Hark 及相关服务的使用条款和所在地法律法规。Cookie 与 API Key 都是敏感凭据，不要提交到 Git、截图或公开分享。

## 项目定位

hark2api 是一个轻量的本地 API 网关。它直接调用 Hark 的会话、消息历史和附件接口，不依赖 Chromium 或浏览器自动化，并提供一个用于账号、任务、媒体和图片工作台的 Web 控制台。

项目的目标是让支持 OpenAI API 的客户端能够接入 Hark，同时把 Hark 当前网页端已经验证的能力边界明确保留下来，而不是虚构上游没有的模型能力。

## 能力范围

| 能力 | 接口或入口 | 状态 | 说明 |
| --- | --- | --- | --- |
| 文本对话 | `/v1/chat/completions` | 支持 | 支持同步和 SSE 流式响应 |
| Responses API | `/v1/responses` | 支持 | 支持文本输入、instructions 和流式响应 |
| 文生图 | `/v1/images/generations` | 支持 | 支持 URL、Base64 和异步任务模式 |
| 图片编辑 | `/v1/images/edits` | 支持 | 单张参考图；使用 multipart/form-data |
| 异步图片任务 | `/v1/images/tasks` | 支持 | 支持轮询、持久化和 `Idempotency-Key` |
| 媒体访问 | `/v1/media/{filename}` | 支持 | 生成结果保存到本地媒体目录 |
| 账号 | `/admin/accounts` | 支持 | 管理 Cookie、启用状态和账号切换 |
| Web 控制台 | `/`、`/admin` | 支持 | 总览、工作台、账号、任务、媒体和设置 |
| 视频生成 | `/v1/videos` | 不支持 | Hark 当前不是常规视频生成模型，项目不注册此接口 |

图片编辑遵循 Hark 的实际限制：每次只处理一张参考图并返回一张图片；`mask`、`size` 等 Hark 未确认支持的参数会被拒绝。Hark 页面可以上传或展示视频附件，但这不等于具备视频生成能力。

### 额度与任务进度

hark2api 直接读取 Hark 的 `/api/billing/summary`，从服务端返回的 meter 计算每日和月度剩余比例，并显示真实重置时间，不再通过普通对话询问额度。额度读取失败不会阻塞已经通过 `/api/profile` 验证的账号，账号页可以稍后手动刷新。

添加账号时会先读取 `/api/profile`。只有能够获取个人资料和邮箱的会话才会添加；额度接口失败时仍会保留账号，并标记额度稍后重试。账号列表显示邮箱、认证状态、日月额度以及各自的重置进度。

Hark 历史消息目前只提供 `isStreaming`、`jobStatus` 等状态，没有可验证的数值百分比。因此任务页和工作台展示排队、等待响应、生成中、整理结果等阶段，只有完成时显示 `100%`，不会伪造中间百分比。

## 快速开始

### 本地运行

环境要求：Python 3.10 或更高版本。

```bash
git clone https://github.com/jyao0708/hark2api.git
cd hark2api

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

将浏览器开发者工具复制出的完整 `Cookie` 请求头，或单独的 `__Secure-hark.session_token` 值写入 `.env` 的 `HARK2API_COOKIE`，或者使用 `HARK2API_COOKIE_FILE` 指向一个只允许当前用户读取的文件。推荐使用文件方式，避免凭据出现在 shell 历史中。

```bash
python -m uvicorn app:app --host 0.0.0.0 --port 18611
```

启动后访问：

- 控制台：<http://127.0.0.1:18611/>
- Swagger 文档：<http://127.0.0.1:18611/docs>
- 健康检查：<http://127.0.0.1:18611/healthz>
- 就绪检查：<http://127.0.0.1:18611/readyz>

### Docker Compose

```bash
cp .env.example .env
# 编辑 .env，至少配置 HARK2API_COOKIE 或 HARK2API_COOKIE_FILE
docker compose up -d --build
```

Compose 会把 `./data` 挂载到容器内的 `/app/data`，用于保存账号、任务和媒体数据。

## 配置

所有配置通过环境变量读取。`.env` 已被 Git 忽略，真实凭据不要写入示例文件。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HARK2API_KEY` | 空 | API 鉴权 Key。为空时不启用鉴权 |
| `HARK2API_COOKIE` | 空 | 完整浏览器 Cookie Header 或单独的 session token |
| `HARK2API_COOKIE_FILE` | 空 | Cookie 或 session token 文件路径，优先于环境变量中的长字符串 |
| `HARK2API_UPSTREAM` | `https://hark.com` | Hark 上游地址 |
| `HARK2API_HOST` | `0.0.0.0` | 监听地址 |
| `HARK2API_PORT` | `18611` | 监听端口 |
| `HARK2API_PUBLIC_BASE` | 空 | 对外访问地址；反向代理部署时建议设置 |
| `HARK2API_CHAT_TIMEOUT` | `300` | 对话请求超时秒数 |
| `HARK2API_REQUEST_TIMEOUT` | `30` | 单次上游 HTTP 请求超时秒数 |
| `HARK2API_TASK_QUEUE_LIMIT` | `8` | 未结束图片任务队列上限 |
| `HARK2API_MAX_MEDIA_BYTES` | `134217728` | 单个媒体文件大小上限 |

当 `HARK2API_KEY` 非空时，业务接口和管理接口都使用以下请求头：

```http
Authorization: Bearer <your-api-key>
```

控制台的“设置”页面只把 API Key 保存到当前浏览器会话的 `sessionStorage`，不会写入服务端文件。

## API 示例

### 文本对话

```bash
curl http://127.0.0.1:18611/v1/chat/completions \
  -H 'Authorization: Bearer h2a_change_me' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "hark",
    "messages": [{"role": "user", "content": "介绍一下你能做什么"}],
    "stream": false
  }'
```

### 文生图

```bash
curl http://127.0.0.1:18611/v1/images/generations \
  -H 'Authorization: Bearer h2a_change_me' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "hark-image",
    "prompt": "一座带完整室内结构的像素风木屋",
    "response_format": "url"
  }'
```

### 图片编辑

```bash
curl http://127.0.0.1:18611/v1/images/edits \
  -H 'Authorization: Bearer h2a_change_me' \
  -F 'model=hark-image' \
  -F 'prompt=保留主体构图，改成蓝绿色调并移除文字' \
  -F 'image=@reference.png'
```

### 异步图片任务

```bash
curl http://127.0.0.1:18611/v1/images/tasks \
  -H 'Authorization: Bearer h2a_change_me' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-image-001' \
  -d '{"prompt":"一座带完整室内结构的像素风木屋"}'

curl http://127.0.0.1:18611/v1/images/tasks/task_xxx \
  -H 'Authorization: Bearer h2a_change_me'
```

创建任务返回 HTTP `202`。客户端应轮询任务状态，查询失败时只重试查询，不要重复提交生成请求。相同 `Idempotency-Key` 和相同输入会返回原任务；相同 Key 搭配不同输入会返回 `409`。

## 控制台

控制台位于 `/` 和 `/admin`，包含：

- 总览：查看 Hark 上游、账号、API Key 和媒体概况
- 账号：添加、验证、启用、禁用、改标签和删除账号；支持完整 Cookie 或单独的 session token，也支持 JSON 导入导出和本地批量启用/禁用/删除
- 任务：查看异步图片任务、删除已结束任务、清理记录
- 媒体：预览、打开和清理本地生成文件
- 工作台：在提示词编辑器内添加参考图、拖拽上传、提取图片风格，或提交文生图和图片编辑
- 设置：配置当前浏览器会话中的 API Key，查看 GitHub 和 QQ 群入口

账号 JSON 导出文件包含 Cookie / session token，仅适合保存在可信设备上，不要上传到公开仓库或发送给他人。

## 数据与安全

- `data/accounts.json` 保存账号元数据和 Cookie，文件权限会限制为当前用户可读写。请保护整个 `data` 目录。
- `data/tasks.json` 保存异步任务元数据。
- `data/media/` 保存生成的图片附件。
- `.env`、Cookie 文件、API Key 和账号数据均不应提交到公开仓库。
- Cookie 失效后需要从已登录的 Hark 浏览器重新导入。当前 Hark 没有可供服务端调用的登录续期 API。
- 对外网部署时，请在反向代理层增加 TLS、访问控制和请求大小限制，不要直接暴露未鉴权实例。

## 开发与检查

```bash
python -m pytest -q
python tests/test_hark.py
python tests/test_state.py
python tests/test_api.py
```

测试使用 mock 和临时目录，不需要访问 Hark，也不会消耗真实生成额度。

## 贡献

欢迎通过 Issue 反馈问题或提交 Pull Request。提交前请：

1. 不要包含 Cookie、API Key、账号文件、生成媒体或其他个人数据。
2. 为行为变化补充或更新测试。
3. 在描述中说明 Hark 上游行为、复现步骤和影响范围。
4. 明确区分 Hark 已确认能力与本地兼容层能力。

## 社区交流

QQ群：**1124104343**

<p align="center">
  <img src="docs/QQ群.png" width="360" alt="QQ群 1124104343 二维码">
</p>

## 许可证

本项目基于 [MIT License](LICENSE) 开源。Hark 及其相关商标和服务归其各自权利人所有，本项目与 Hark 官方没有隶属或背书关系。
