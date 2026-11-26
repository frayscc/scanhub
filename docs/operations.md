# 核心版本上线与验收

当前核心版本为 0.3.0：Phase 0–2 的功能加本轮上线收尾。原 Phase 3 的 SMB/FTP、OCR/OMR、Webhook 等为可选扩展，尚未实现。**代码阶段结束不等于真实设备和目标服务器已经验收。**

## 升级前与正式启动

1. 在 Debian 主机保留完整项目和现有 `.env`，检查 `data` 挂载位置，停止旧服务。
2. 按下节创建离线备份，记录备份路径；数据库升级后不能直接用旧程序打开，应从升级前备份回退。
3. 确认管理员密码哈希和上传令牌已配置。生产环境不设置 `SCANHUB_INSTANCE=acceptance`。
4. `data` 目录所有者应为 UID/GID 10001，应用容器以普通用户运行。
5. 执行 `docker compose up -d --build --wait --wait-timeout 120`。
6. 查看 `docker compose ps`、`docker compose logs --tail=100`，并访问 `/api/ready`。首次配置密码后登录网页，确认收件箱和历史原件可读取。

`GET /api/health` 只检查应用进程是否响应，返回版本和实例角色；`GET /api/ready` 检查数据库读取、临时目录写入以及最低剩余磁盘空间（`MIN_FREE_MB`，默认 64 MiB）。Docker 健康检查使用 ready。未就绪时返回 503，不对未登录用户暴露路径或数据库内容。`unhealthy` 不会让 Docker 自动重启，需处理磁盘、权限或数据库问题。

图片验证、预览、缩略图、旋转导出、ZIP/PDF 和密码校验在受限线程任务中运行，同一进程同时执行一个重任务；等待超过 10 秒会返回 503。HTTP 请求、SSE 和健康检查仍可响应。扫描仪对 503/超时的实际重试行为仍需验证。当前仅支持单 Uvicorn 工作进程；不要自行加 `--workers` 来绕过资源限制。

## 离线备份

以下命令在项目根目录的 Debian 主机执行；维护 CLI 只使用 Python 标准库，不需要安装完整后端依赖。

```bash
docker compose stop
sudo python3 -m scanhub.maintenance check --data-dir ./data --verify-hashes
sudo python3 -m scanhub.maintenance backup \
  --data-dir ./data \
  --output ./backups/scanhub-20261126.zip \
  --offline-confirmed
docker compose start
```

备份目标必须在 `data` 之外且不存在；请使用每次不同的名称。`--offline-confirmed` 表示操作者已停服，工具无法隔空检测其他进程是否仍在写文件。数据库用 SQLite 备份接口读取一致快照，包含 WAL 中已提交内容，不把 WAL/SHM 直接当作独立数据库复制。归档包含数据库、原件、缩略图和调试证据，排除 tmp；每个归档条目记录大小和 SHA-256。文件分块复制，归档发布后才算成功，不覆盖旧备份。

备份会核对原件大小和摘要，失败退出码非零。不能因备份命令失败就删除原数据。备份包含试卷、二维码及可能含凭据的调试体，权限为仅创建者可读写；归档本身没有加密，应另存于受保护的备份介质。`.env` 不在数据归档中，需要独立安全备份，不能提交到代码仓库。

## 恢复与回退

恢复只允许写入**尚不存在的新目录**，不会覆盖现有目录，也不使用不受控的 ZIP 解压路径。条目数量、名称、大小、摘要与数据库引用全部检查通过后才发布新目录；校验失败不留下可被误认为成功的恢复目录。

```bash
docker compose stop
sudo python3 -m scanhub.maintenance restore \
  --archive ./backups/scanhub-20261126.zip \
  --data-dir ./data-restored \
  --offline-confirmed
sudo python3 -m scanhub.maintenance check --data-dir ./data-restored --verify-hashes
sudo chown -R 10001:10001 ./data-restored
sudo chmod 700 ./data-restored
```

确认恢复报告成功后，将当前 `data` 改名保留，再把 `data-restored` 改名为 `data`，恢复配套 `.env` 并启动 Compose。不要把恢复目录直接合并到现有数据中。恢复会撤销备份中的浏览器会话，必须重新登录；上传令牌摘要、文件旋转角度和系统设置保留。已有非零保留策略在下次启动仍会执行，恢复前应核对策略。磁盘删除待重试任务同样保留，启动后会继续已确认的永久删除。

## 隔离 Docker 验收

验收环境使用单独 Compose 项目、测试账号、目录和 127.0.0.1:18686 端口，不挂载正式 `.env` 或 `data`。

```bash
python3 scripts/acceptance.py prepare
mkdir -p ./test-results/acceptance-data
sudo chown 10001:10001 ./test-results/acceptance-data
sudo chmod 700 ./test-results/acceptance-data
docker compose -f docker-compose.acceptance.yml up -d --build --wait --wait-timeout 120
python3 scripts/acceptance.py run --confirm-isolated --requests 20 --workers 2
docker compose -f docker-compose.acceptance.yml restart
docker compose -f docker-compose.acceptance.yml up -d --wait --wait-timeout 120
python3 scripts/acceptance.py verify --confirm-isolated
docker stats --no-stream
```

`prepare` 生成随机测试凭据且拒绝覆盖已有文件。`run` 会先验证服务器的 acceptance 实例角色及专用测试账号，拒绝正式实例；测试真实 HTTP 上传、原件字节、旋转、软删除恢复、PDF、ZIP 及完整性检查，保存文件 ID 和时延报告。`verify` 用原报告检查容器重启后的原件及角度。重复运行 run 时通过 `--report` 指定新报告路径，保留上一次记录。脚本不会输出密码、Token 或文件正文。

```bash
# 仅删除报告所属的测试文件；先验证实例角色、账号与文件名前缀
python3 scripts/acceptance.py cleanup --confirm-isolated
docker compose -f docker-compose.acceptance.yml down
```

验收图片是很小的合成 PNG，时延仅用于冒烟验证，不代表真实扫描吞吐量。J1900 上还需使用无个人信息的实际扫描测试纸，观察大图预览、200 页以内 PDF、连续上传的内存峰值与响应时间。CPU/内存限制仍为 1 核/256 MiB，不能用开发电脑结果代替目标机测量。

## 真实设备签收记录

完成以下检查后再判定核心版本可交付；记录日期、固件和服务器版本，协议结果填写 `docs/scanner-protocol.md`。

- [ ] 单页、双面、多页、PDF、二维码的真实上传行为与响应要求。
- [ ] 失败、超时、重试及重复文件保留；扫描批次标识是否可信。
- [ ] 单图/批量左右旋转、原件和修正方向下载、PDF 页序/A4。
- [ ] Windows、macOS、真实 iPhone/iPad/Android 的登录、预览、下载与多选。
- [ ] 容器重启、主机重启和镜像升级后原件、角度、设置仍在。
- [ ] 磁盘接近最低空间时 ready 返回 503，恢复空间后变为 ready。
- [ ] 离线备份恢复演练、密码/令牌保存、目录权限和默认永久保留。
- [ ] 记录 J1900 CPU/内存和较长时间连续使用结果。

## 自动验证与依赖维护

本地验证：`python -m pytest -q`，前端 `npm ci`、`npm run build`、`npx playwright test`。已新增真实进程重启和离线备份恢复测试，不把这些结果等同于 Docker 或真实硬件验证。

`requirements.lock` 固定本轮验证的运行依赖，`requirements.txt` 使用该约束；前端通过 package-lock 固定。更新依赖后应重新测试，包括 PDF 渲染。此次核对的 23 个运行依赖在 PyPI 版本元数据中未报告已知公告，npm audit 报告为 0；这只是检查时的数据，并不保证以后没有新问题。CI 配置包含测试、前端构建、Docker 构建及隔离容器重启验收；当前未在远端执行。
