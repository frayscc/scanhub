# GitHub 与 Docker Hub 发布

- 公开源码仓库：`frayscc/scanhub`。
- 镜像：`frayscc/scanhub`，当前构建 `linux/amd64`（J1900 / x86_64）。
- 推送 main：执行完整验证，成功后发布 latest 和提交 SHA 标签。
- 推送 vX.Y.Z：执行验证，成功后发布 X.Y.Z、X.Y 和 SHA 标签。
- Pull request 只验证，不登录 Docker Hub，不访问发布令牌。

首次发布已于 2026-11-26 完成：[GitHub Actions 运行记录](https://github.com/frayscc/scanhub/actions/runs/45745918012)。Docker 构建、独立容器验收、容器重启和 Docker Hub 推送均通过；镜像仓库公开且已校验匿名镜像清单访问。仍需在实际 J1900 和扫描仪上验收。

## 一次性凭据配置

在 Docker Hub 为 frayscc 创建具有此镜像推送权限的访问令牌。在 GitHub 仓库 Settings → Secrets and variables → Actions → New repository secret 中添加：

```text
Name: DOCKERHUB_TOKEN
Secret: Docker Hub 访问令牌
```

不要将令牌提交到文件、聊天或 workflow 明文。Docker Hub 仓库应为公开仓库，以便服务器匿名拉取。设置完成后到 Actions → Publish Docker Hub image → Run workflow 运行 main，或重跑先前失败的发布 job。

验证内容包括 Python 自动化测试、前端类型检查/构建、Docker 镜像构建、独立容器上传/下载/旋转/PDF/ZIP 验收及容器重启后持久化核对。只有 validate 成功，publish 才能推送。每次发布在 Actions 摘要中记录镜像 digest。

## 使用已发布镜像

按 README 配置 `.env` 和 data 目录权限后运行：

```bash
docker compose -f docker-compose.image.yml up -d --pull always --wait --wait-timeout 120
```

固定版本可设置 `SCANHUB_IMAGE_TAG=0.3.0`（须该版本标签已经成功发布）。生产升级前备份数据，避免不经检查直接跟随 latest。

当前公开源码尚未指定开源许可证；公开可见不等于授予任意再分发或商用授权，许可证可由项目所有者后续选择。
