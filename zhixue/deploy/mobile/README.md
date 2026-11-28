# 智学手机 HTTPS 部署

部署包包含手机网页、单进程实时中继、锁定依赖和 Caddy 配置。手机通过 HTTPS 打开网页并配对。电脑继续提供课程、同步和推送控制。Caddy 在同一台公网主机上按两个域名分流，保护中继请求和静态网页。 

## 解压和启动

使用已安装 Caddy 与 uv 的 Linux 或 macOS 主机。将两个域名解析到该主机，使公网能连接 TCP 80 和 443，使 Caddy 可以获取和续签证书。以下示例域名需替换为自己的两个域名。 [Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)

在解压后的目录安装依赖并启动中继。

```sh
export ZHIXUE_PHONE_HOST=study.example.com
export ZHIXUE_RELAY_HOST=relay.example.com
uv venv .venv --python 3.11
uv pip sync --require-hashes --python .venv/bin/python requirements.txt
.venv/bin/python -m backend.mobile_relay --origin "https://$ZHIXUE_PHONE_HOST"
```

在同一个目录的另一个终端启动 Caddy。

```sh
export ZHIXUE_PHONE_HOST=study.example.com
export ZHIXUE_RELAY_HOST=relay.example.com
caddy validate --config Caddyfile
caddy run --config Caddyfile
```

两个进程可由主机的服务管理器保持常驻。Caddy 的工作目录设为解压目录，TLS 证书保存在其配置的数据目录。一个中继来源对应一个中继进程。默认回环端口为 3221。调整端口时，中继的 `--port` 与 Caddy 环境变量 `ZHIXUE_RELAY_PORT` 使用相同值。

## 上线后核对

在已核对归档摘要的解包目录运行以下命令。它使用系统信任校验证书和域名，要求两域名解析到公网地址，并对系统当次解析得到的每个 IPv4 或 IPv6 地址分别校验证书、网页和 Service Worker。检查固定连接到该地址，继续使用原域名验证 HTTPS。

```sh
.venv/bin/python verify.py --phone "https://$ZHIXUE_PHONE_HOST" --relay "https://$ZHIXUE_RELAY_HOST" > https-verification.json
```

结果记录两端的实际连接地址、TLS 版本、证书到期时间、发布清单摘要和资源数量，并校验域名隔离、缓存响应头、中继健康和配对入口的来源限制。脚本在每个中继地址建立一个临时随机会话，检查手机来源的交换预检，用随机字节双向验证等待、交换和答复，随后确认邀请已关闭。会话由中继暂存在内存中，最多占用一个会话席位，无需课程或真实配对数据。全部通过时报告标记为 `relayRoundTripVerified`。任何地址返回旧版、缺少中继路径或传输内容有差异时，退出码为非零，错误指出具体来源、地址及出错环节。本机临时 CA 验收可增加 `--ca-file`，报告会标明所用信任方式。

在智学电脑端的「手机离线 → 同步」填入两个 HTTPS 地址。用手机打开生成的链接，核对配对码，下载课程。随后验证手机离线编辑、回到联网后同步，以及「同步 → 手机系统推送」的订阅和停用。验证通知实收时，让电脑保持运行，在固定课程时间或选择「30 分钟后」后关闭手机网页，再通过通知打开相应页面。iOS 的推送开关位于主屏幕上的智学 Web App。 [Apple Web Push 开发文档](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers)

## 更新手机网页

先在新目录解压并核对 `SHA256SUMS.txt`，再将 Caddy 的手机站点切换到完整的新目录。保持两个域名地址不变。浏览器装入新 Service Worker 时会核对预缓存文件的摘要。若代理仍返回旧入口或其他资源校验失败，已装好的旧版继续提供离线页面。文件就绪后，关闭全部智学手机页面，再重新打开，以便浏览器完成换版。已下载课程及待同步记录继续从原来源的本机数据库读取。
