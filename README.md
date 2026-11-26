# ScanHub

[GitHub 源码](https://github.com/frayscc/scanhub) · [Docker Hub 镜像](https://hub.docker.com/r/frayscc/scanhub) · [自动发布说明](docs/publishing.md)

轻量、自托管的局域网扫描文件管理系统。当前包含 **Phase 0 调试接收器、Phase 1 通用协议 MVP 与 Phase 2 完善功能**。P-6010Pro 仍未实测，不能视为已兼容。

当前核心版本 **0.3.0** 已增加上线收尾工具：可校验的离线备份恢复、存储就绪检查、独立验收环境和资源限制。部署/升级/回退及目标机签收步骤见 [上线与运维说明](docs/operations.md)。原 Phase 3 的业务扩展按需单独开发；正式交付仍需真实设备及服务器验收。

本轮验证：51 项后端自动化测试、8 项桌面/手机浏览器测试通过，包含真实 HTTP、进程重启和备份恢复演练；前端构建与 PDF 渲染复查通过。Docker/CI 和 J1900 验收尚未在目标环境执行。

## 当前功能

- FastAPI 单服务、Vue 3 + TypeScript + Vite，SQLite/SQLAlchemy；不使用 Redis 或外部数据库。
- 管理员登录、scrypt 密码哈希、12 小时 Cookie 会话、CSRF、独立上传 Token。
- JPG/JPEG、PNG、TIFF、PDF 上传；内容校验、唯一文件名、识别重复但全部保留、原件不重新编码。
- 收件箱、今日/全部文件、搜索文件名及二维码、日期/来源/格式筛选、排序、分页、网格/列表。
- 图片缩略图、缩放/适应/100% 预览、键盘切换，PDF 使用浏览器原生多页预览。
- 复选框、Ctrl/Command、Shift 连选、背景拖框、本页全选；SSE 更新保留当前选择和预览。
- 原件下载、磁盘临时 ZIP、软删除及恢复。
- **批量左旋/右旋 90°**，角度持久化，预览和缩略图同步更新；JPG/PNG 可下载方向修正 PNG。
- 浅色/深色/跟随系统、移动端布局、显示设置、真实磁盘信息。
- PDF 合并、页序编辑、单页旋转/移除、原始页面比例或 A4，TIFF 自动展开多页。
- 批次浏览、重命名、整批 ZIP/PDF/删除，手动合并或拆分，明确区分设备提供/时间推断/手动批次。
- 回收站永久删除、清空、持久化失败重试队列；可选保留天数，默认永久保留。
- 系统设置、设备别名和位置、上传日志、协议调试日志、上传令牌生成/重置及复制。

暂未实现 Phase 3 的 SMB/FTP/OCR/OMR/Webhook 等扩展。TIFF 图片预览仅显示首页；修正方向 PNG 导出支持 JPG/PNG。现有 PDF 文件不属于图片批量旋转或图片合并范围。

## Debian 13 / Docker 部署

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32)); print(secrets.token_urlsafe(32))'
python3 -m scanhub.security
```

随机值分别填入 `.env` 的 `UPLOAD_TOKEN` 和 `ADMIN_TOKEN`（调试日志管理令牌）。交互生成的完整 `scrypt:...` 填入 `ADMIN_PASSWORD_HASH`，不要填明文密码。默认账号 `admin`，可通过 `ADMIN_USERNAME` 修改；**没有默认密码**。正常运行保持 `DEBUG_ENABLED=false`。

无 Python 时可先 `docker compose build`，再执行 `docker compose run --rm --no-deps scanhub python -m scanhub.security` 交互生成哈希。随机令牌也可同样使用容器中的 Python 生成。

```bash
mkdir -p data
sudo chown 10001:10001 data
sudo chmod 700 data
docker compose up -d --build --wait --wait-timeout 120
curl http://localhost:8686/api/health
curl http://localhost:8686/api/ready
```

访问 `http://SERVER_IP:8686` 登录。容器以 UID 10001 运行，目录必须可写。升级沿用原 `data/` 和 `.env`，执行同样命令。Node 仅在前端构建阶段使用，最终镜像只运行 Python。

## 批量旋转

1. 选中图片（复选框、Shift、Ctrl/Command 或背景框选）。“本页全选”不选择未加载文件。
2. 点击“左旋 90°”或“右旋 90°”。四次回到原始方向。
3. 角度存入数据库，刷新和重启后保留；不改写原件。
4. “下载原件”和“原件 ZIP”保持扫描仪原始文件及方向；预览中的“下载方向修正 PNG”应用 EXIF 与旋转角度，保存无损 PNG，不再引入 JPEG 压缩损失。

混选 PDF 或回收站文件时禁止旋转。后端整批校验后统一提交，避免只旋转部分文件。手机使用复选框和同一工具栏。

## PDF、批次与系统设置

选中图片后点击“合并 PDF”：拖拽或使用上下按钮调整顺序，按文件名或扫描时间排序，旋转/移除指定图片，自定义名称，选择原始比例或统一 A4。旋转在文件已保存的角度基础上叠加，只作用于本次 PDF，不修改原件或收件箱角度。JPEG 直接嵌入，PNG/TIFF 使用无损处理；TIFF 自动展开所有页（编辑器中每项代表一个源文件，对多页 TIFF 的顺序/旋转会作用于该文件全部页面）。A4 为固定纵向尺寸，等比例居中，不裁切。默认生成下载，不保存新原件。为限制 J1900 资源，每次最多 200 页、所选原件合计 64 MiB（重复选取计入总量），同时只生成一个 PDF。

上传附带 `batch_id` 时，同一 `device` 与 `batch_id` 归为“设备提供”批次。**只有实际协议提供可确认的任务标识时才传入该字段**；没有时按同一来源的上传时间间隔推断，默认 30 秒，可在设置修改。“按时间推断”不能当成真实任务。手动归类/拆分会建立“手动整理”批次，不伪装成设备批次。扫描批次页支持合并所选批次；进入批次选择部分文件再点“拆分 / 归类”，即可移到新批次。旧版本未归类文件在批次页有独立入口。整批 ZIP 最多 10000 文件；整批 PDF 仍受 200 页/64 MiB 限制，含现有 PDF 的批次需进入后只选择图片。

顶部“系统设置”提供默认显示方式、批次间隔、上传限额、设备来源别名与位置、存储信息及上传日志。来源标识对应 `device`，别名不改写历史原件或批次身份。上传日志最多 1000 条，网页显示最近 30 条，不记录 Token/密码/二维码正文。

上传令牌重置后仅显示一次，旧令牌立即失效，需要更新扫描仪配置；数据库只保存新令牌摘要。**数据库中保存的设置及新令牌优先于 `.env` 初始值**。未在网页保存过的配置仍以 `.env` 为初始值。上传令牌供扫描仪写入文件；网页登录与调试管理令牌仍分离。

保留天数 `0` 表示永久保留，可选/输入 1、3、7、30 或自定义天数。保存非零策略前网页会确认。启动和每 5 分钟执行检查；原件超过保留期先软删除，回收站从移入时间计算保留期，达到期限才永久删除。恢复的过期原件仍受原件保留策略约束。可以按已保存策略手动触发检查。清空回收站或永久删除前须确认，仅删除回收站文件；磁盘清理失败会保留任务，在重启或定期维护时重试。

## 通用正式上传

`POST /api/upload` 当前接受 `multipart/form-data`，允许同一或不同字段携带多个文件，建议 `paper`。普通参数 `device`、`text`（二维码）及附加参数存入数据库，凭据不保存。每个请求的文件全部验证后一起入库；一个无效则全部拒绝。

```bash
export UPLOAD_TOKEN='your-upload-token'
curl -H "X-Upload-Token: $UPLOAD_TOKEN" \
  -F 'paper=@page1.jpg' -F 'paper=@page2.jpg' \
  -F 'device=P-6010Pro-test' -F 'text=test-qr' \
  http://SERVER_IP:8686/api/upload
```

也接受表单 `token` 或 URL 参数 `?token=...`，优先用请求头/表单。响应为 `{"success":true,"files":[{"id":"...","duplicate_of":null}]}`。相同内容返回已有 ID，但仍保留新文件。此格式是通用实现，**扫描仪是否接受须先用调试接口确认**。

默认整个请求上限 50 MiB（含表单开销），可设 `MAX_UPLOAD_MB`；默认最多 2 个活动接收请求，繁忙返回 503。正式表单最多各 100 个文件/字段，每普通字段 64 KiB；图片最多 2500 万像素，PDF 不接受加密且最多 2000 页。无效类型 415、超限 413、中断 400、存储失败 507。扫描仪 Token 不能读取或管理文件；网页会话上传需 CSRF。

## 真实扫描仪协议验证

首次启用可设置 `DEBUG_ENABLED=true`，确保两个令牌非空且不同，然后 `docker compose up -d --force-recreate`。若已保存系统设置，应在网页设置中切换调试开关（数据库配置优先）；网页也提供脱敏协议记录查看。

1. 地址先设为 `http://SERVER_IP:8686/api/debug/upload?token=YOUR_UPLOAD_TOKEN`；设备若支持表单自定义 `token`，优先改用表单并移除 URL 令牌。
2. 保留预设 `paper`、`text`，添加 `device=P-6010Pro-test`，仅用无个人信息测试纸。
3. 依次测试单页、双面、多页、PDF、二维码、连续任务和失败重试，记录页序、次数、时间及设备提示。
4. 用独立管理令牌查最近记录，或本地检查 `data/debug/<请求ID>/manifest.json` 及文件。
5. 测试不同响应状态/正文/Content-Type，将脱敏结论写入 [协议记录](docs/scanner-protocol.md)，再切换正式接口。HTTP 200 不等于设备认可响应。
6. 测试完成后在系统设置关闭调试；首次仅用环境变量开启的，可改回 false 并重新创建容器。不要分享令牌或未经检查的原始请求体。

```bash
export ADMIN_TOKEN='your-debug-admin-token'
curl -H "Authorization: Bearer $ADMIN_TOKEN" 'http://localhost:8686/api/debug/requests?limit=20'
curl -H "X-Upload-Token: $UPLOAD_TOKEN" -F 'paper=@test.jpg' -F 'text=test-qr' http://localhost:8686/api/debug/upload
curl -X PUT -H "X-Upload-Token: $UPLOAD_TOKEN" -H 'Content-Type: application/octet-stream' --data-binary @test.jpg http://localhost:8686/api/debug/upload
```

支持 GET/POST/PUT/PATCH/DELETE/OPTIONS/HEAD、重复文件字段、未知原始请求体。`DEBUG_RESPONSE_STATUS`、`DEBUG_RESPONSE_BODY`、`DEBUG_RESPONSE_CONTENT_TYPE` 控制响应。默认 200 / `{"success":true}` 只是测试起点。请求间隔是当前进程收到请求的间隔。

## 持久化、安全与备份

```text
data/
  scanhub.db                  # 元数据、批次、设置、会话、日志、清理队列、迁移版本
  uploads/YYYY/MM/DD/<UUID>    # 原件，软删除后保留原位置
  thumbnails/<UUID>.jpg       # 可重新生成
  tmp/                        # 请求暂存及临时 ZIP/PNG
  debug/<UTC时间_随机ID>/      # 调试清单、文件、未知协议 body.bin
```

完整接收后原子移动文件，再提交数据库事务；普通异常会回滚并清除暂存。突然断电可能留下暂存或未入库原件，**不会自动删除潜在用户数据**。登录后 `GET /api/integrity` 返回缺失原件和孤立文件清单。数据库用 SQLite `user_version` 顺序迁移，后续版本须追加迁移，不能覆盖已有数据库。

推荐停服后使用 `python3 -m scanhub.maintenance backup` 创建带校验清单的归档，使用 `restore` 恢复到新目录，具体命令见运维说明；不要手工漏掉仍含已提交事务的 WAL 文件。`.env` 需单独安全备份，恢复目录须恢复 UID 权限。Phase 0 文件不自动混入正式收件箱。

默认永久保留，自动清理仅在保存非零保留天数后启用；尚无总存储配额。ZIP 写磁盘后按块发送，不把所有原件一次载入内存；需要额外磁盘空间。回收站软删除不搬动原件。永久删除先以同一数据库事务删除元数据并写入持久化磁盘清理任务，再删除原件和缩略图，失败保留任务供重试。缩略图生成失败不删除原件。

调试头部、参数和普通字段只记录名称及长度，敏感字段脱敏。文件名、正式数据库中的二维码正文、原件以及未知请求的 `body.bin` 仍可能包含个人信息或凭据，须保护数据目录。调试工具有意接受任意类型供分析，不执行或公开提供这些调试文件；正式接口验证真实内容。PDF 使用沙箱预览，原件下载设置为附件。

HTTP 无法防止内容与 Token 被窃听，只用于可信隔离局域网。跨不可信网络应启用 HTTPS 代理并确认设备支持，HTTPS 时设 `COOKIE_SECURE=true`。只开放必要防火墙来源。应用访问日志默认关闭，代理也应避免记录 URL 令牌。管理会话使用 HttpOnly/SameSite=Strict Cookie，修改操作校验 CSRF，退出时撤销会话。会话最长 12 小时；更改密码后已有会话仍可用至到期，紧急撤销需停服清空数据库 sessions 表。

## 开发与测试

```bash
python -m venv .venv
# Linux: source .venv/bin/activate；Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pytest -q
cd frontend
npm ci
npm run build
npx playwright test
```

浏览器测试使用本机 Edge（含手机视口模拟）。测试服务 `scripts/e2e_server.py` 仅绑定 127.0.0.1:18767，使用系统临时目录，测试凭据不作用于正式目录。更换浏览器需修改配置并安装相应浏览器。

本地后端：设置 `DATA_DIR`、`UPLOAD_TOKEN`、`ADMIN_PASSWORD_HASH` 等环境变量后运行 `uvicorn scanhub.app:app --host 127.0.0.1 --port 8080 --no-access-log`。前端运行 `npm run dev`，Vite 代理 `/api` 到 8080。构建后也可设置 `STATIC_DIR` 为 `frontend/dist` 绝对路径，统一由后端提供。Python 本地不自动加载 `.env`，Docker Compose 会加载。

模拟脚本只生成测试 PNG，请先启动独立 `DATA_DIR` 的调试接收器，例如 8687 端口，并设置 `SCANHUB_TEST_TOKEN`：

```bash
python scripts/simulate_scanner.py --confirm-test-target --url http://localhost:8687/api/debug/upload --count 20 --pages 2 --workers 2
```

当前环境没有 Docker；容器构建/重启、Debian/J1900 性能及真实扫描仪协议仍待目标环境验证。自动化结果及剩余范围见 [ROADMAP.md](ROADMAP.md)。

Phase 2 本地验收：40 项后端测试及 8 项 Edge 桌面/手机视口测试通过，前端类型检查与生产构建通过；PDF 经 Poppler 渲染检查。测试覆盖 v1→v2 升级、PDF 页序/方向/A4/原 JPEG 数据一致、多页 TIFF、批次推断与手动整理、永久删除重试、默认永久保留及可选分阶段清理、启动维护、令牌摘要持久化。正式升级前先停止旧服务备份 data；v2 数据库不能直接交给旧版程序运行，应通过升级前备份回退。
