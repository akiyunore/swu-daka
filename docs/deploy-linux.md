# Linux Docker 详细部署过程

本说明适用于一台 Linux amd64 服务器上的全新 Docker 部署。所有命令均在服务器的 Bash shell 执行。不要把现有生产实例的数据库、Secret、私有 `.env` 或反向代理配置复制到公开仓库。现有实例升级应先阅读第 8 节。

## 1. 准备服务器

1. 准备至少 2 核 CPU、2 GiB 内存和足够存放浏览器 profile、SQLite 与备份的磁盘空间。服务器应可访问 Docker Hub 和学校正常登录所需的站点。
2. 安装 Docker Engine 与 Docker Compose v2 插件；按 [Docker 官方 Linux 安装说明](https://docs.docker.com/engine/install/)选择发行版。让负责部署的账号有权运行 Docker。
3. 将站点域名解析到服务器；在反向代理处准备有效的 HTTPS 证书。仅开放反向代理需要的 80/443 端口。应用端口由 Compose 固定绑定到 `127.0.0.1`。
4. 核对工具和架构：

```sh
uname -m                         # 应为 x86_64
docker version
docker compose version
df -h
free -h
```

本镜像目前只发布 Linux amd64。其他架构需要使用仓库 `Dockerfile` 自行构建并验证。

## 2. 下载发布包并生成私有配置

在计划长期保存项目文件的目录执行：

```sh
git clone https://github.com/akiyunore/swu-daka.git
cd swu-daka
python3 scripts/prepare_docker.py
```

脚本会创建以下本机文件，已被 `.gitignore` 排除：

| 路径 | 作用 |
| --- | --- |
| `.env` | 镜像标签、允许的 Host，以及随机生成的管理员页面路径和 API 前缀 |
| `secrets/admin_password.txt` | 初始管理员密码 |
| `secrets/credential_key.txt` | 加密主密钥；丢失后现有加密凭据无法恢复 |
| `secrets/mimo_api_key.txt` | 可选云端 OCR Key；初始为空，后备关闭 |
| `docker-data/` | SQLite、会话、浏览器 profile、日志和邮件队列 |

准备脚本不覆盖任何已有文件，也不打印敏感内容。请在服务器本地用编辑器打开 `.env`，把 `SWU_DAKA_ALLOWED_HOSTS` 加入实际站点域名，同时保留 `localhost,127.0.0.1` 供健康检查使用。例如将该项设为 `localhost,127.0.0.1,example.org`；`example.org` 只是示例。请保留脚本生成的两个随机管理路径，不要将 `.env` 贴到工单、聊天或公开代码库。

Docker 容器以 UID/GID `1001:1001` 运行。Linux 宿主机上为新建目录设置访问权限：

```sh
sudo chown -R 1001:1001 docker-data
sudo chmod 0700 docker-data
sudo chgrp 1001 secrets/*.txt
chmod 0640 secrets/*.txt
chmod 0600 .env
```

执行 `chgrp` 的部署账号需要相应权限；若失败，可对该命令使用 `sudo`。管理员密码应在本机安全保存，数据库和 `credential_key.txt` 还应分开备份。不要在终端历史中直接输入密码或 Key。

## 3. 检查配置并启动

```sh
docker compose config --quiet
docker compose pull
docker compose up -d --no-build
docker compose ps
curl --fail --silent --show-error http://127.0.0.1:18080/health
```

Compose 配置缺少私有管理路径或允许的 Host 时会直接报错，不会悄悄使用公开默认路径。`docker compose ps` 应显示 `healthy`；健康接口应返回 `status: ok`，并显示管理员密码和加密主密钥已配置。基础部署的邮件通知默认关闭。请勿为 Uvicorn 增加第二个 worker，否则内置调度可能重复领取任务。

健康检查只访问本机服务，不会登录校园账号、发邮件或提交打卡。若容器未就绪，先运行 `docker compose logs --tail=100 swu-daka` 查看**脱敏后的**错误类别；不要公开粘贴包含用户资料的完整日志。

## 4. 配置 HTTPS 反向代理

应用只监听宿主机 `127.0.0.1:18080`。可复用现有 Nginx/宝塔 HTTPS 站点，将所有请求反代至这个地址。以下片段放入**服务器私有** Nginx 站点配置中，替换示例域名和证书路径；证书可由现有 ACME 客户端签发：

```nginx
server {
    listen 443 ssl http2;
    server_name example.org;
    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

在你自己的反向代理配置中，对 `.env` 所设管理 API 登录端点增加 IP 限流；对其 SSE 端点保持禁用缓冲和足够长的读取超时。不要将私有管理路径写进公开仓库或镜像构建参数。配置完成后运行 `sudo nginx -t`，再由你使用的服务管理方式平滑重载 Nginx。HTTP 到 HTTPS 的跳转及证书续期也应在反向代理处配置。

公网检查：普通用户首页应通过 HTTPS 打开；`/health` 应返回 `ok`；未登录访问管理 API 应拒绝请求。检查浏览器开发者工具，登录 Cookie 应为 `Secure` 且只在 HTTPS 传输。不要把容器内部 8000 端口或宿主机 18080 端口直接开放到公网。

## 5. 初始化管理员与用户

管理员用户名初始为 `admin`；初始密码保存在服务器的 `secrets/admin_password.txt`。只在服务器本地查看该文件。管理员页面入口由 `.env` 的私有值决定，普通首页不显示该链接。首次登录后先核对健康状态、站点域名和用户须知，再创建一次性邀请 Token。普通用户使用该 Token 验证本人校园账号并注册。

新数据库没有普通用户，也没有启用的自动打卡计划。计划默认在每日 21:10 后的 10 分钟窗口错峰运行。正式启用前，应由账号本人核对学校任务、签到地点和官方状态；初始化验收不要使用真实账号试打卡。邮箱验证和失败提醒在基础 Compose 中关闭；如需邮件功能，应单独准备私有 SMTP 覆盖配置、已验证发件域名及文件型 Secret，再做收件侧验收。

## 6. 日常检查

```sh
docker compose ps
curl --fail --silent --show-error http://127.0.0.1:18080/health
docker compose logs --tail=100 swu-daka
```

`docker compose ps` 的 `healthy` 与本地健康接口通过，只能证明 Web 服务、数据库和基本配置可用；学校登录、邮件收件、自动计划以及真实打卡需分别由实际使用者验收。不要把 Secret、完整账号、Cookie、Token、验证码或原始日志上传到 GitHub。

## 7. 备份

定期备份 `docker-data/`、`secrets/`、`.env` 和当前 Compose 文件。备份应放在仓库之外、限制访问，并与服务器本身分开保存。`credential_key.txt` 与数据库必须成对保留，且应分别保护。备份前停止应用容器，避免漏掉 SQLite 的 WAL/SHM 文件：

```sh
docker compose stop swu-daka
backup_dir="../swu-daka-backups/$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$backup_dir"
cp -a docker-data secrets .env docker-compose.yml "$backup_dir/"
test -f "$backup_dir/docker-data/app.db"
test -f "$backup_dir/secrets/credential_key.txt"
docker compose up -d --no-build
```

核对备份大小、文件数量和 SQLite 完整性，再把备份安全地移到另一处。不要将备份目录放进 Git 或公开云盘。

## 8. 升级与回退

升级前先确认当前镜像标签、容器健康、是否正在执行任务，并按第 7 节生成完整备份。只在停止目标容器后复制数据库目录；保留旧镜像标签。随后在私有 `.env` 中将 `SWU_DAKA_IMAGE` 改为明确的新版本标签，执行：

```sh
docker compose config --quiet
docker compose pull
docker compose up -d --no-build --force-recreate
docker compose ps
curl --fail --silent --show-error http://127.0.0.1:18080/health
```

升级后核对 SQLite 完整性、原有表的行数、文件权限、HTTPS 页面和用户可见行为。不要删除或重新初始化已有 `docker-data/app.db`。数据库迁移可能只支持向前；若必须回退，应停止目标容器，使用**同一次升级前备份**中的数据、Secret、私有配置和匹配的旧镜像一起恢复，而不是只换回旧镜像。未经使用者授权，不要把升级验证变成真实校园认证、发邮件或打卡。

## 9. 自行构建镜像

公开镜像目前只有 Linux amd64。若需修改源码，先在隔离环境构建并验证，再为自己的镜像指定新标签：

```sh
docker build --platform linux/amd64 -t local/swu-daka:custom .
```

本仓库的 `.dockerignore` 排除私有配置、运行数据与备份，Dockerfile 也仅复制运行所需的源码和构建后的前端。不要把 `.env`、`secrets/` 或 `docker-data/` 加入构建上下文、镜像层或 Git 历史。
