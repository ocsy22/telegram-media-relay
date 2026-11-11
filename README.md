# TG 频道媒体组转发工具 · Ubuntu

通过网页管理 Telegram 频道转发。支持 Bot API 和 Telegram 账号 API，可将一个源频道转到最多 20 个目标频道。后台侧栏提供转发设置、历史迁移、运行进度和自动刷新的日志。

## 一键部署

在 Ubuntu 22.04 或更新版本的 root 终端粘贴：

```bash
setup=$(mktemp) && wget -O "$setup" https://raw.githubusercontent.com/ocsy22/telegram-media-relay/main/deploy.sh && bash "$setup"
```

按提示填 VPS 公网地址并自设管理员用户名、密码。安装会输出后台地址 `http://VPS地址:8766/`。放行 TCP 8766 后可直接访问，与客服工具的 8765 端口分开。HTTP 会明文传输登录信息和 Bot Token，请限制入站来源为管理端 IP，或通过 SSH 隧道或 HTTPS 反代访问。

重复部署会保留配置。需要重设后台账号时，在执行脚本时传入 `VPS地址 --configure-admin`。数据库保存在 `/var/lib/telegram-media-relay/relay.sqlite3`，账号配置保存在 `/etc/telegram-media-relay/service.env`。

## 配置和运行

1. 在 BotFather 创建 Bot，将其加入源频道，并在每个目标频道授予发帖权限。每个 Token 只运行一个轮询实例。启用实时转发前先停用旧 Webhook。
2. 输入 Token、源频道和目标频道。频道用 `@用户名` 或 `-100...` 数字 ID。多个目标用逗号分隔。点击「测试连接」核对权限。
3. 实时转发选择复制或带引用，点击「开始实时转发」。相册短暂缓冲，按消息 ID 排序后整批提交。实时任务会在服务重启后恢复。
4. 历史迁移先停止实时任务，再填写起止消息 ID。历史使用无引用复制，建议选在相册边界外的完整区间。省略结束 ID 时，连续 300 个 ID 没有可复制消息即停止，因此遇到大段删除时请填明确的结束 ID。手动重新运行历史任务会再次复制所选范围。
5. 刷新网页查看日志。不可复制的消息由 Telegram 跳过。服务日志可用 `journalctl -u telegram-media-relay -f` 查看。

## 账号管理

侧栏「账号管理」可保存多组 Bot 和 Telegram 账号。添加 Bot 时填写备注和 Token，后台验证后保存。添加个人账号时先在 [Telegram API 管理页面](https://my.telegram.org/apps) 获取 API ID 和 API Hash，再在后台填写备注、API ID、API Hash 和带国家区号的手机号。提交验证码，有需要时再提交两步验证密码。

登录后回到「转发设置」，从「使用账号或 Bot」中选择已保存的连接，填写源频道和目标频道，再启动任务。升级时，原有 Bot 会自动出现在列表，已有终端账号也可继续选择。

请通过 HTTPS 或 SSH 隧道填写登录凭据。使用 SSH 时，在管理电脑运行 `ssh -N -L 8766:127.0.0.1:8766 root@VPS地址` 后打开 `http://127.0.0.1:8766/accounts`。网页登录的会话保存在服务的 SQLite 数据库中。

进度与日志每 1.5 秒更新。有结束 ID 时显示区间扫描进度。实时或开放区间任务显示已处理数与各目标累计发送数。

Bot API 不提供按频道读取完整历史的接口。历史功能向 Telegram 提交指定 ID 范围，不会绕过受保护内容的限制。实时任务会保存已接收的更新和各目标进度。若进程恰好在 Telegram 成功处理请求后、写入本地进度前退出，重启后可能重复发送该批次。

## 一键卸载

在 Ubuntu 的 root 终端运行下列命令。保留管理员账号、配置、日志和待发送记录。

```bash
systemctl disable --now telegram-media-relay.service && rm -f -- /etc/systemd/system/telegram-media-relay.service /opt/telegram-media-relay/app.py && systemctl daemon-reload
```
