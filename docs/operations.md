# 部署、升级与验收

0.4.0使用匿名临时工作台和集成FTP，数据库v4。升级前停服备份整个data与.env；不要直接用旧程序打开升级后的数据库。

## 升级

1. 在服务器执行 docker compose stop，离线复制data与.env到新的备份位置，勿覆盖已有备份。
2. 若旧测试容器scanhub-ftp-test仍运行，执行 docker stop scanhub-ftp-test 释放21和被动端口；保留ftp-test目录。
3. 替换Compose为仓库版本（保留npm外部网络），.env填写SCANNER_FTP_PASSWORD，沿用已测成功的扫描仪FTP密码。旧admin/token/Cookie配置可移除。
4. 确认FTP_PUBLIC_HOST为扫描仪可到达的IP。

```bash
sudo chown -R 10001:10001 ./data
sudo chmod 700 ./data
docker compose pull
docker compose up -d --force-recreate --wait --wait-timeout 120
docker compose logs --tail=80
curl http://127.0.0.1:8686/api/ready
```

容器普通用户10001监听FTP2121，宿主机映射21；无需特权，保留cap_drop ALL。控制和被动端口都需可达。NPM只代理网页到scanhub:8080。

health检查进程，ready检查数据库、临时写入、剩余空间（MIN_FREE_MB默认64MiB）。Docker unhealthy不会自动重启，需处理权限、空间或服务问题。

## 生命周期

凭证仅页面内存。主动结束撤销访问并永久删除当前文件；关页通过sendBeacon通知，失败时最后心跳300秒后由每5秒检查回收。刷新不恢复旧工作台；后台节流/休眠也可能超时，扫描期间保持页面可用。

短时服务器重启保留未超时工作台，已打开页面可继续；没有重新打开恢复历史的入口。备份恢复撤销全部临时工作台，启动后清理其临时数据。磁盘删除失败队列重试，删除不等于介质安全擦除。旧版无workspace_id原件不公开，也不参与会话清理。

## 离线备份和回退

通常先下载并结束本次扫描。维护CLI可备份旧数据或故障现场；在有源码的目录执行，只需Python标准库：

```bash
docker compose stop
sudo python3 -m scanhub.maintenance backup --data-dir ./data --output ./backups/before-upgrade.zip --offline-confirmed
docker compose start
```

归档逐文件校验大小/SHA-256，SQLite备份包含已提交WAL，排除tmp。.env单独保存。恢复必须到尚不存在的新目录：

```bash
docker compose stop
sudo python3 -m scanhub.maintenance restore --archive ./backups/before-upgrade.zip --data-dir ./data-restored --offline-confirmed
sudo chown -R 10001:10001 ./data-restored
```

报告成功后再替换数据目录，保留原目录。回退旧程序需恢复旧版备份及配套Compose/.env。备份副本不会随工作台结束自动删除。

## 隔离容器验收

使用独立目录、随机FTP测试密码、127.0.0.1:18686网页，不挂载正式data/.env：

```bash
python3 scripts/acceptance.py prepare
mkdir -p test-results/acceptance-data
sudo chown 10001:10001 test-results/acceptance-data
docker compose -f docker-compose.acceptance.yml up -d --build --wait --wait-timeout 120
python3 scripts/acceptance.py run --confirm-isolated
docker compose -f docker-compose.acceptance.yml restart
docker compose -f docker-compose.acceptance.yml up -d --wait --wait-timeout 120
python3 scripts/acceptance.py verify --confirm-isolated
python3 scripts/acceptance.py cleanup --confirm-isolated
docker compose -f docker-compose.acceptance.yml down
```

restart/verify须在会话超时前完成。脚本只接受acceptance角色，测试真实FTP、原件、旋转、PDF、ZIP及短时重启连续性。报告含测试工作台凭证，仅保存于被忽略的test-results，不输出凭证。

目标设备须复核：网页领取后实际扫描上传；另一页面空白且不能抢占；批量左右旋转、PDF/ZIP下载、结束/关页/超时清理。新FTP已用标准客户端测试，仍需实际扫描仪验收；合成小图不能替代J1900大文件负载测量。
