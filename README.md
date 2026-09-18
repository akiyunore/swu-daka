# SWU Daka：Linux Docker Web 版

非官方西南大学查寝打卡辅助项目。本仓库只提供 Linux Docker 多用户 Web 部署所需的源码、Compose 与说明；Web 服务复用 `legacy/` 中的校园登录和任务处理核心。仓库不包含任何现有实例的数据、Secret、域名或管理员入口配置。

> 仅在本人获准使用的账号和场景下部署。项目与学校及相关平台无隶属关系；是否打卡成功应以官方系统记录为准。

**[Linux Docker 详细部署过程](docs/deploy-linux.md)** 包含服务器准备、私有配置、镜像启动、HTTPS 反向代理、初始化、验证、备份与升级。

镜像：[`akiyunore/swu-daka:2026-09-18.2`](https://hub.docker.com/r/akiyunore/swu-daka)，平台为 Linux amd64。发布包中的 `Dockerfile` 可用于自行构建同一应用。

首次部署的最短流程：

```sh
git clone https://github.com/akiyunore/swu-daka.git
cd swu-daka
python3 scripts/prepare_docker.py
# 在本机私有 .env 中加入站点域名，然后按部署文档设置文件权限
docker compose config --quiet
docker compose pull
docker compose up -d --no-build
docker compose ps
```

准备脚本只在文件缺失时创建 Secret、数据目录及随机管理员页面/API 路径；**不会覆盖已有配置，也不会输出路径值或 Secret**。容器只映射到宿主机 `127.0.0.1:18080`，生产访问必须通过 HTTPS 反向代理。新安装不会自动创建普通用户或开启自动打卡计划。

`docker-data/`、`secrets/`、`.env`、日志及备份均不应提交到 Git。升级必须保留数据库和加密主密钥，详见部署文档。

本仓库基于 [dan-cun/swu-daka](https://github.com/dan-cun/swu-daka) 修改并保留原项目的 [LICENSE](LICENSE)。使用与再分发须遵守原作者的署名、非营利及免责声明。Web 版会在部署者自己的数据目录中保存账号资料、加密凭据、邮箱及运行记录；启用第三方邮件或云端 OCR 时，相关邮件内容或验证码图片将由部署者选用的服务商处理。
