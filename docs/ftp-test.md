# FTP 最小连通性测试

目的：确认真实扫描仪能否将文件传到服务器，不依赖 HTTP、NPM、Cookie 或上传 Token。
此配置只让文件落在 `ftp-test/`，**不会自动导入 ScanHub 收件箱**，也不是已完成的 FTP 入库功能。

依据镜像维护者文档及启动脚本配置 USERS、ADDRESS、被动端口范围和用户目录：
https://github.com/delfer/docker-alpine-ftp-server

1. 在现有 `.env` 增加 `SCANNER_FTP_PASSWORD=自己设置的测试密码`，不修改原有 ScanHub 变量。
   此镜像的 USERS 格式以空格和 `|` 分隔，测试密码不要包含这些字符或 `$`，便于避免解析歧义。
2. 启动单独的测试服务：

```bash
mkdir -p ftp-test
chown 10001:10001 ftp-test
chmod 750 ftp-test
docker compose -f docker-compose.ftp-test.yml up -d
docker logs -f scanhub-ftp-test
```

扫描仪填写服务器 `10.0.0.49`、端口 `21`、普通 FTP、用户名 `scanner`、上面设置的密码、目录 `/`；若有模式选项，先选被动/PASV。服务器字段通常只填 IP，不要附带 http:// 或 /api/upload。

FTP 的命令连接与文件数据连接分开，因此需要允许扫描仪访问 TCP 21 和 21000–21010。
本例直接连接服务器发布端口，不经 NPM 的 HTTP Proxy Host；现成 npm Docker 网络不是连接所必需。
若 21 已被其他服务使用，先确认已有服务，不要随意停止或覆盖。

扫描一张无个人信息测试纸后检查：

```bash
find ./ftp-test -type f -printf '%f %s bytes\n'
```

如需先在服务器验证 FTP 本身（curl 会交互询问密码，不把密码写入命令历史）：

```bash
printf 'ScanHub FTP connectivity test\n' > /tmp/scanhub-ftp-probe.txt
curl --fail --show-error --user scanner --ftp-pasv \
  --upload-file /tmp/scanhub-ftp-probe.txt \
  ftp://10.0.0.49/scanhub-ftp-probe.txt
```

普通 FTP 不加密，仅用于可信局域网测试；不要扫描真实学生数据。该测试没有自动重命名/去重机制，同名文件可能被覆盖。测试结束关闭：

```bash
docker compose -f docker-compose.ftp-test.yml down
```

当前开发电脑无 Docker，只核对了上游配置接口；此第三方 FTP 镜像和真实扫描仪的配合尚待目标机测试。
