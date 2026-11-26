# ScanHub

[GitHub](https://github.com/frayscc/scanhub) · [Docker Hub](https://hub.docker.com/r/frayscc/scanhub)

**0.4.0：免登录的临时扫描工作台，FTP 接收。** 单个 Python 服务集成网页与 FTP，SQLite 存储，无需外部数据库。

## 使用方式

1. 打开 HTTPS 网页，无需账号密码；每个页面创建独立工作台。
2. 点击“开始扫描”领取扫描仪。同一时间只能一个页面领取，其他页面看不到本次文件。
3. 在扫描仪上扫描并点击 FTP 上传；文件自动出现在领取页面。
4. 多选图片，批量左旋/右旋90°；预览、下载原件、方向修正PNG、原件ZIP或合并PDF。
5. 下载后点击“结束并清理”，撤销访问并删除本次数据。没有历史记录、回收站或恢复入口。

刷新或关闭页面后不会恢复原工作台。关页发送尽力而为的结束通知；崩溃、断网等情况下，服务端在停止心跳5分钟后清理（每5秒检查）。刷新也会开始新工作台。下载到自己电脑的文件不受清理影响。磁盘删除失败有持久化重试队列。

扫描期间保持页面打开。每个文件开始传输时绑定当前领取者；已开始传输的文件不会转交下一会话。共享FTP无法识别人，迟到的全新上传属于当时领取者，因此应等扫描和上传全部完成再结束。

## Docker部署（现成npm网络）

仓库的 docker-compose.yml / docker-compose.image.yml 已按服务器10.0.0.49配置：网页8686、FTP21、被动端口21000–21010，外部网络npm。不同IP请改 FTP_PUBLIC_HOST。

```bash
cp .env.example .env
# 编辑.env，填入扫描仪使用的SCANNER_FTP_PASSWORD
mkdir -p data
sudo chown 10001:10001 data
sudo chmod 700 data
docker compose pull
docker compose up -d --wait --wait-timeout 120
```

.env最小配置：

```dotenv
SCANNER_FTP_PASSWORD=替换为扫描仪FTP密码
FTP_DEVICE_NAME=P-6010Pro
MAX_FILE_MB=50
MIN_FREE_MB=64
```

网页没有密码，不再使用 ADMIN_USERNAME、ADMIN_PASSWORD_HASH、ADMIN_TOKEN、UPLOAD_TOKEN、COOKIE_SECURE。页面隔离使用仅在内存中的随机凭证，不写Cookie、浏览器存储或URL。旧HTTP上传、调试、登录、历史、系统设置及回收站API已移除。

Nginx Proxy Manager继续将HTTPS域名代理到 http://scanhub:8080。FTP直接连接宿主机：扫描仪填写10.0.0.49、端口21、用户名scanner、上述密码、目录 /，优先被动模式。FTP用于可信局域网，账号只能上传，不能下载/删除原件。

## 从旧版升级

先停服备份整个data与.env，再拉取新镜像。旧版文件保留在磁盘，**不会出现在公开页面，也不会被新会话清理**；旧data不需要清空。数据库升级到v4，回退必须使用升级前备份。

如果旧测试容器scanhub-ftp-test占用21端口，先执行 docker stop scanhub-ftp-test；其ftp-test文件夹保持不动。新服务直接接收FTP，不监视旧测试目录。

## 文件处理

支持JPG/JPEG、PNG、TIFF、PDF，验证真实内容与大小，随机内部文件名避免覆盖。图片角度仅保存元数据，不改写原件；ZIP下载原件，方向修正PNG应用旋转。PDF支持页序、旋转、移除、原比例/A4；JPEG直接嵌入，TIFF展开所有页，每次最多200页/64MiB。已有PDF不参与图片旋转或图片合并。

FTP完整传输后才入库；中断/无效文件不进入工作台，随所属会话清理。未领取工作台上传返回FTP450。单文件默认50MiB。服务限单工作进程，图片/PDF/ZIP重任务串行在线程执行，采用1核/256MiB配置；真实J1900负载仍需验收。

## 开发与验证

```bash
python -m venv .venv
# 激活虚拟环境后：
pip install -r requirements-dev.txt
python -m pytest -q
cd frontend
npm ci
npm run build
npx playwright test
```

浏览器测试用本机Edge、独立临时目录和本地FTP/HTTP端口。开发后端设置 DATA_DIR、FTP_PASSWORD、FTP_PUBLIC_HOST 后运行 uvicorn scanhub.app:app --host 127.0.0.1 --port 8080 --no-access-log；前端 npm run dev。本地Python不自动读取.env。

目标机升级、备份和容器验收见[运维说明](docs/operations.md)，设备观测见[协议记录](docs/scanner-protocol.md)。
