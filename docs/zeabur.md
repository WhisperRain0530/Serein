# 在 Zeabur 部署单实例

此部署把 Python 记忆服务和 Node 网关放在一个容器内，适合个人使用。只有网关 8080 端口对外；Python 8011 和网页预览 4173 仅监听容器内的 loopback。保留一个副本，使用一个持久卷。

## 首次部署

1. 在 Zeabur 新建服务，选择本仓库。构建根目录保持仓库根目录。`zbpack.json` 会选择 `deploy/Zeabur.Dockerfile`，不使用仅提供 Python 服务的根 Dockerfile。也可在 Zeabur 环境变量设置 `ZBPACK_DOCKERFILE_PATH=deploy/Zeabur.Dockerfile`。
2. **首次启动前**挂载持久卷到 `/data`。主库、原话、图片、派生索引、OAuth 状态和凭据都放在此目录。不要把已有数据目录当临时构建目录，也不要在已有数据后再挂空卷覆盖它。
3. 对外 HTTP 端口设为 **8080**，`PORT=8080`；健康检查路径设为 **`/ready`**。首页要求登录，不能用 `/` 检查存活。
4. 添加 HTTPS 域名，将 `SEREIN_PUBLIC_ORIGIN` 设为完整 origin，例如 `https://your-instance.zeabur.app`，不加 `/v1`、查询参数或其他路径。域名尚未设置时聊天 API 可运行，但远程 OAuth 不启用。
5. 等待 `/ready` 返回成功，再打开页面配置上游和模型。空实例未配置上游时，模型列表为空，聊天返回配置提示；服务健康不等于模型已接通。

Zeabur 的[自定义 Dockerfile 路径](https://zeabur.com/docs/en-US/deploy/methods/dockerfile)、[持久卷](https://zeabur.com/docs/en-US/data-management/volumes)和[健康检查](https://zeabur.com/docs/en-US/operations/monitoring/health-checks)说明可供核对。带持久卷的更新会先停止旧实例，再启动新实例，有短暂中断；不要为 SQLite 实例开启多副本。

## 取得自己的连接凭据

首次启动自动生成两个不同的随机秘密：网页登录密码和 Gateway Key。默认网页登录用户名为 `kitty`；新实例可通过 `SEREIN_WEB_USERNAME` 指定其他用户名。已有实例重启保留原账号、密码和 Key，不因环境变量改名而重置它们。

启动日志不会显示这些值。仅在 Zeabur 的服务终端中显式执行：

```sh
python /app/scripts/zeabur_runtime.py credentials
```

返回 JSON 的 `username` / `password` 用于网页登录，`gateway_key` 用于 API；已配置公开域名时也返回 `web_url` 和 `api_base_url`。不要把终端输出贴进公开日志、Issue、代码或截图。

凭据保存在 `/data/secrets/api-token`、`web-auth.json` 和 `web-password`。网页登录校验使用加盐 scrypt 哈希；为了让实例所有者能通过上述命令取回随机初始密码，`web-password` 仍是明文私密文件。目录和文件权限限制为运行账户可读写。已有数据库却丢失凭据时启动会拒绝自动生成替代值，应恢复匹配的 secrets 备份。

## Kelivo / OpenAI 兼容客户端

- 接口类型选择 OpenAI 兼容的 **Chat Completions**。
- API Base URL：`https://your-instance.zeabur.app/v1`。
- API Key：本实例的 `gateway_key`，不是网页密码，也不是厂商上游 Key。
- 拉取模型时请求应为 `/v1/models`，聊天应为 `/v1/chat/completions`；检查客户端是否自动追加 `/v1`，避免重复路径。
- 在 Serein 网页中填写真实厂商地址、Key 和模型，再到客户端拉取列表。自动记忆注入需要另外完成 Embedding / Reranker / 索引准备；未准备时保留关闭。

## 启停和数据边界

容器启动脚本只在缺少文件的全新实例中创建配置与凭据，之后由 `serein.launch` 初始化或迁移数据库。它等待 Python 服务就绪再启动网关；记忆服务或网关意外退出时会停止另一个，让平台重启整个实例。可选 Codex worker 的故障单独退避，不因此重启聊天服务。SIGTERM / SIGINT 会停止子进程，超过退出等待时间才强制终止。

`/data/config.toml` 使用 `/data/serein.db` 和 `/data/recall-index.sqlite`；模型索引、图片及 OAuth 数据库与主库同属持久目录。路由编辑草稿也保存在 `/data/semantic-route-draft.json`。旧库路径导入只允许 `/data/legacy-input` 内的来源。

更新前备份完整 `/data`，并记录对应源码版本。该目录及数据库可能包含私人对话、模型 Key 和连接密码，不能公开分享。在线数据库备份使用网页一致性备份；复制整个卷前应停止实例，避免直接复制正在写入的 SQLite 文件。首次验证至少包含网页鉴权、API 鉴权、模型列表、合成问答和重启后设置仍在；不要用私人记忆作为测试数据。

根目录 Dockerfile 和 `deploy/compose.yaml` 继续用于原来的双服务部署，未改成这个单容器方案。

## 可选：用 Codex 订阅整理 Event

镜像内置固定版本 `@openai/codex@0.160.1`，通过已有 Node 运行。Codex worker 默认关闭；开启后与现有记忆服务和网关运行在同一容器内，串行领取 Event 整理任务，使用 `gpt-5.6-sol`。它通过容器内 `127.0.0.1:8011` 领取和提交任务，不需要公开 `/v1/extensions` 或新增公网端口。主聊天模型仍由聊天客户端选择。

1. 在 Zeabur 服务环境中设置 `SEREIN_CODEX_WORKER_ENABLED=1` 并重新部署。未登录时 worker 等待登录，聊天服务照常运行。
2. 在该服务的私有终端执行设备码登录：

   ```sh
   python /app/scripts/zeabur_runtime.py codex-login
   ```

   按终端提示，在自己的浏览器打开登录地址并输入一次性设备码。账户需要允许设备码登录；不要把设备码、登录令牌或认证文件贴进聊天、公开日志或 Issue。
3. 在同一终端检查登录状态：

   ```sh
   python /app/scripts/zeabur_runtime.py codex-status
   ```

4. 在 Serein 的“设置 → 配置 → 自动摘要配置”选择 **Agent** 执行方式，再在“功能”页开启自动摘要。先用合成对话核对归线、切分和 Event 写作；查看原话整理进度与生成正文。登录成功不等于账户已获该型号访问权，也不等于流水线已完成。

登录管理命令和 worker 使用专用 `CODEX_HOME=/data/codex-worker/auth`，登录状态随 `/data` 持久卷保留，更新容器后不必每次重新登录。该目录需保持可写，以便 Codex 刷新登录状态；不要复制桌面 Codex 的完整配置、会话或插件目录。设备码登录与凭据存储见 [OpenAI 官方认证说明](https://learn.chatgpt.com/docs/auth)。

启动器分别构造子进程环境：Codex 登录配置只交给 worker，记忆服务和网关不继承 Codex／OpenAI 认证环境变量。专用目录限制文件权限，但当前单容器内进程沿用同一 root 账户，**这不是强进程隔离**；容器管理员及其他 root 进程仍可读取这些文件。完整 `/data` 备份也会包含 Codex 登录凭据，按私密凭据保管。

worker 串行处理任务，无待办时等待；登录失效、额度限制或模型调用失败时暂停或退避，不自动改用其他型号或 API Key。订阅调用仍受账户额度和模型可用性约束。`/ready` 检查聊天服务，不证明 worker 已登录或摘要成功；登录看 `codex-status`，任务结果看 Serein 原话整理页面。设回 `SEREIN_CODEX_WORKER_ENABLED=0` 并重启服务可停止 worker，已有 Event 和登录状态保留。
