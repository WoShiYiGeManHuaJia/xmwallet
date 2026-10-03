# xmwallet.py — 小米钱包「看视频得会员」每日任务（Termux 版）

安卓 / Termux 专用，纯终端运行，不依赖图形界面。

## 这个脚本是怎么来的

融合了两个开源项目的可用部分，再针对 Termux 重写：

| 部分 | 来源 | 说明 |
|---|---|---|
| 扫码登录 | `kai648846760/xiaomiwallet` | `account.xiaomi.com` 长轮询扫码，不用抓包 |
| 任务状态机 | `3056810551/xiaomi-wallet-vip` | 自适应任务时长、防漏领、动态时长解析 |

在原基础上新增：

- **去掉一切 GUI 依赖**（Tkinter / Flet / Pillow / 桌面扫码），二维码直接打进终端
- **双任务发现路径**：`getTask` 拿不到就自动回落 `getTaskList`
- **票据失效自动换票重试**：请求失败时静默重新 STS 换票再试一次
- **多账号 + 账号间随机长间隔**（30~90 秒），降低被风控概率
- **Termux 原生通知**：跑完往状态栏推一条结果
- **日志落盘 + 自动轮转**（超过 512KB 自动另存）

## 安装

```bash
pkg update && pkg install python cronie termux-api
pip install requests qrcode        # qrcode 可选，不装就只能复制链接去浏览器
```

> `termux-api` 用于跑完推送通知，装不上也不影响主流程。

把 `xmwallet.py` 放到 Termux 家目录：

```bash
cd ~ && python3 xmwallet.py --help
```

## 首次登录（只需一次）

```bash
python3 xmwallet.py login 我的号
```

终端会打印二维码，用**小米手机系统扫一扫 / 小米钱包 App** 扫，手机上点【允许登录】。
终端不便于扫的话，把下面那行链接复制到手机浏览器打开授权，效果一样。

扫码获取的凭据存在 `accounts.json`，设备指纹存在 `device.json`。

### 扫码通道不可用时的备选

如果接口拒绝或返回异常，脚本会提示改用导入方式：

1. 手机浏览器登录 `https://account.xiaomi.com`
2. 从 Cookie 里取 `userId` 和 `passToken`
3. 导入：

```bash
python3 xmwallet.py import 我的号 '{"userId":"xxx","passToken":"yyy"}'
```

也支持直接吃另两个项目生成的配置文件：

```bash
python3 xmwallet.py import 我的号 ~/xiaomiconfig.json
python3 xmwallet.py import 我的号 ~/xiaomi_account.json
```

## 日常使用

```bash
python3 xmwallet.py run           # 执行每日任务（全部账号）
python3 xmwallet.py run 我的号     # 只跑指定账号
python3 xmwallet.py status        # 只看会员时长和今日流水，不做任务
```

## 设置每天自动跑

```bash
python3 xmwallet.py cron
```

会自动写入 crontab（每天 9:00）。然后启动守护进程：

```bash
crond
```

想开机自启就把 `crond` 加进 `~/.bashrc`。

> Android 8 以上更推荐 `termux-job-scheduler`，比常驻 crond 省电。

## 多账号

`accounts.json` 是个数组，重复 `login` 不同别名即可：

```json
[
  {"name": "我的号", "userId": "...", "passToken": "..."},
  {"name": "家里号", "userId": "...", "passToken": "..."}
]
```

## 风控相关说明

脚本已经做了这些降低风险的处理：

- 任务等待时长按服务端下发的实际时长来，再加 2~5 秒随机缓冲，不写死 10 秒
- 每轮之间随机 2.5~4.5 秒
- 账号之间随机 30~90 秒
- 设备指纹每个账号固定一份，不每次变化
- 全程本地 IP 运行，不走服务器

**能做的都做了，但风险不为零。** 上面两个原项目都有作者明确警告过封号 / 账号被限制参加活动的先例，请自行判断。

## 凭据安全

`accounts.json` 里的 `passToken` 等于账号凭证，**不要上传到任何公开仓库**，也不要发给别人。

## 已知限制

- 脚本无法验证"是否真的领到会员"，只有真实账号跑一遍才知道
- 小米随时可能改接口校验，改了就报错，届时看日志排查
- `completeStatus` / `remainChance` 等字段依赖服务端下发，服务端改字段名会导致判定失效
