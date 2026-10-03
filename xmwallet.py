#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xmwallet.py —— 小米钱包「看视频得会员」每日任务自动化（Termux / 安卓版）

融合两个开源项目的可用部分，并针对 Termux 无图形界面环境重写：

  登录流程  来自 kai648846760/xiaomiwallet 的 login.py
            （account.xiaomi.com 长轮询扫码，无需抓包，二维码直接打进终端）
  任务流程  来自 3056810551/xiaomi-wallet-vip 的 xiaomi_wallet_auto.py
            （自适应状态机 / remainChance 防漏领 / 0~60 秒动态时长）

本脚本新增：
  · 去掉 Tkinter / Flet / Pillow 依赖，纯终端运行
  · 双任务发现路径（getTask 失败自动回落 getTaskList）
  · 凭据与设备指纹持久化，票据失效自动换票重试
  · Termux 原生通知（跑完推一条到状态栏）
  · 日志落盘 + 自动轮转
  · 多账号支持，账号间长随机间隔降低风控概率

用法：
  python3 xmwallet.py login <账号别名>     # 扫码登录（只需一次）
  python3 xmwallet.py run                  # 执行每日任务
  python3 xmwallet.py status               # 只看会员时长与今日流水
  python3 xmwallet.py cron                 # 写入 crontab 每日自动跑

依赖：
  pkg install python
  pip install requests qrcode              # qrcode 可选，缺失则只给登录链接
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    sys.exit("缺少依赖 requests，请先执行: pip install requests")

try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

# ---------------------------------------------------------------- 常量

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNT_FILE = os.path.join(BASE_DIR, "accounts.json")
DEVICE_FILE = os.path.join(BASE_DIR, "device.json")
LOG_FILE = os.path.join(BASE_DIR, "xmwallet.log")
MAX_LOG_BYTES = 512 * 1024

API_HOST = "m.jr.airstarfinance.net"
ACTIVITY_CODE = "2211-videoWelfare"
TASK_CODE = "BROWSE_GROUP_TASK1"

APP_VERSION_NAME = "6.114.0.5756.2747"
APP_VERSION_CODE = "20577694"

UA_MOBILE = (
    "Mozilla/5.0 (Linux; U; Android 14; zh-CN; 22041216C Build/UP1A.231005.007; "
    "AppBundle/com.mipay.wallet; AppVersionName/%s; AppVersionCode/%s; "
    "MiuiVersion/V816.0.12.0.ULOCNXM; DeviceId/xagapro; NetworkType/WIFI; "
    "mix_version; WebViewVersion/146.0.7680.119) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Mobile Safari/537.36 XiaoMi/MiuiBrowser/4.3"
) % (APP_VERSION_NAME, APP_VERSION_CODE)

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

USER_EXTRA = json.dumps({
    "platformType": 1,
    "com.miui.player": "4.27.0.4",
    "com.miui.video": "v2024090290(MiVideo-UN)",
    "com.mipay.wallet": APP_VERSION_NAME,
}, separators=(",", ":"))

# 换票地址：小米账号 -> 天星数科 STS，两边项目使用的是同一条
STS_LOGIN_URL = (
    "https://account.xiaomi.com/pass/serviceLogin?callback=https%3A%2F%2Fapi.jr.airstarfinance.net%2Fsts"
    "%3Fsign%3D1dbHuyAmee0NAZ2xsRw5vhdVQQ8%253D%26followup%3Dhttps%253A%252F%252Fm.jr.airstarfinance.net"
    "%252Fmp%252Fapi%252Flogin%253Ffrom%253Dmipay_indexicon_TVcard%2526deepLinkEnable%253Dfalse"
    "%2526requestUrl%253Dhttps%25253A%25252F%25252Fm.jr.airstarfinance.net%25252Fmp%25252Factivity"
    "%25252FvideoActivity%25253Ffrom%25253Dmipay_indexicon_TVcard%252526_noDarkMode%25253Dtrue"
    "%252526_transparentNaviBar%25253Dtrue%252526cUserId%25253Dusyxgr5xjumiQLUoAKTOgvi858Q"
    "%252526_statusBarHeight%25253D137&sid=jrairstar&_group=DEFAULT&_snsNone=true&_loginType=ticket"
)

QR_URL = "https://account.xiaomi.com/longPolling/loginUrl"
QR_QUERY = {
    "_group": "DEFAULT",
    "_qrsize": "240",
    "qs": ("?callback=https%3A%2F%2Faccount.xiaomi.com%2Fsts%3Fsign%3DZvAtJIzsDsFe60LdaPa76nNNP58%253D"
           "%26followup%3Dhttps%253A%252F%252Faccount.xiaomi.com%252Fpass%252Fauth%252Fsecurity%252Fhome"
           "%26sid%3Dpassport&sid=passport&_group=DEFAULT"),
    "bizDeviceType": "",
    "callback": ("https://account.xiaomi.com/sts?sign=ZvAtJIzsDsFe60LdaPa76nNNP58="
                 "&followup=https://account.xiaomi.com/pass/auth/security/home&sid=passport"),
    "_hasLogo": "false",
    "theme": "",
    "sid": "passport",
    "needTheme": "false",
    "showActiveX": "false",
    "serviceParam": '{"checkSafePhone":false,"checkSafeAddress":false,"lsrp_score":0.0}',
    "_locale": "zh_CN",
    "_sign": "2&V1_passport&BUcblfwZ4tX84axhVUaw8t6yi2E=",
}

STATUS_TEXT = {
    0: "登录成功",
    700: "等待扫码",
    701: "已扫码，请在手机上确认",
    702: "二维码已过期",
}


# ---------------------------------------------------------------- 基础工具

def log(msg: str = "", echo: bool = True):
    """同时写日志与标准输出。"""
    line = msg if msg else ""
    if echo:
        print(line, flush=True)
    try:
        if os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
            os.rename(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read().strip()
            return json.loads(s) if s else default
    except Exception:
        return default


def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def in_termux() -> bool:
    return os.path.isdir("/data/data/com.termux/files/usr")


def notify(title: str, content: str):
    """Termux 原生通知，非 Termux 环境静默跳过。"""
    if not in_termux():
        return
    try:
        subprocess.run(
            ["termux-notification", "--title", title, "--content", content],
            timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def jitter(a: float, b: float) -> float:
    return random.uniform(a, b)


# ---------------------------------------------------------------- 设备指纹

def get_device(user_id: str) -> Dict[str, str]:
    """每账号固定一份设备指纹，避免指纹频繁变化触发风控。"""
    all_dev = load_json(DEVICE_FILE, {})
    key = "device_%s" % user_id
    if key in all_dev:
        return all_dev[key]
    dev = {
        "imei": "86219%011d" % random.randint(0, 99999999999),
        "deviceId": "22041216UC",
        "longitude": "%.6f" % (116.3 + random.random() * 0.1),
        "latitude": "%.6f" % (39.9 + random.random() * 0.1),
    }
    all_dev[key] = dev
    save_json(DEVICE_FILE, all_dev)
    return dev


# ---------------------------------------------------------------- 账号存储

def load_accounts() -> List[Dict[str, Any]]:
    data = load_json(ACCOUNT_FILE, [])
    return data if isinstance(data, list) else []


def save_accounts(accounts: List[Dict[str, Any]]):
    save_json(ACCOUNT_FILE, accounts)


def find_account(name: str) -> Optional[Dict[str, Any]]:
    for a in load_accounts():
        if a.get("name") == name:
            return a
    return None


def upsert_account(name: str, user_id: str, pass_token: str, ssecurity: str = ""):
    accounts = load_accounts()
    for a in accounts:
        if a.get("name") == name:
            a.update({"userId": str(user_id or ""), "passToken": pass_token,
                      "ssecurity": ssecurity, "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            break
    else:
        accounts.append({
            "name": name, "userId": str(user_id or ""), "passToken": pass_token,
            "ssecurity": ssecurity, "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    save_accounts(accounts)


# ---------------------------------------------------------------- 扫码登录

def fetch_qr() -> Optional[Dict[str, Any]]:
    q = dict(QR_QUERY)
    q["_dc"] = str(int(time.time() * 1000))
    try:
        r = requests.get(QR_URL, params=q, headers={"User-Agent": UA_DESKTOP}, timeout=20)
        t = r.text
        if "&&&START&&&" in t:
            t = t.split("&&&START&&&", 1)[-1].strip()
        d = json.loads(t)
        # 源站/网关可能直接返回拒绝（403 policy denied），不是二维码数据
        if d.get("status") and d.get("status") != 200:
            log("  源站拒绝: %s %s" % (d.get("status"), d.get("title") or d.get("detail") or ""))
            return None
        if not d.get("qr"):
            log("  返回中没有二维码字段，接口可能已变更")
            return None
        return d
    except Exception as e:
        log("  获取二维码失败: %s" % e)
        return None


def show_qr(url: str):
    """在终端输出二维码；同时给出可用浏览器直接打开的链接。"""
    log("")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        try:
            qr.print_tty()
        except Exception:
            log("\n".join("".join("##" if c else "  " for c in row)
                          for row in qr.get_matrix()))
    except ImportError:
        log("  （未安装 qrcode，跳过图形二维码。可执行 pip install qrcode）")
    except Exception as e:
        log("  二维码渲染异常: %s" % e)

    log("")
    log("  若手机上扫不方便，长按复制下面链接，用手机浏览器打开授权：")
    log("  %s" % url)
    log("")


def poll_login(lp_url: str, timeout: int = 300) -> Optional[Dict[str, str]]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        left = int(deadline - time.time())
        try:
            r = requests.get(lp_url, timeout=60)
            t = r.text
            if "&&&START&&&" in t:
                t = t.split("&&&START&&&", 1)[-1].strip()
            res = json.loads(t)
            code = res.get("code", -1)
            msg = STATUS_TEXT.get(code, "未知状态 %s" % code)
            if msg != last:
                log("  状态: %s（剩余 %d 秒）" % (msg, left))
                last = msg
            if code == 0:
                return {"userId": res.get("userId"),
                        "ssecurity": res.get("ssecurity"),
                        "passToken": res.get("passToken")}
            if code == 702:
                return None
        except requests.exceptions.Timeout:
            log("\r  等待扫码中... 剩余 %d 秒   " % left, echo=True)
            continue
        except Exception:
            time.sleep(3)
    return None


def cmd_login(name: str):
    log("=" * 56)
    log("  为账号「%s」扫码登录" % name)
    log("=" * 56)
    if find_account(name):
        ans = input("  该账号已存在，覆盖重新登录？[y/N] ").strip().lower()
        if ans != "y":
            log("  已取消。")
            return
    data = fetch_qr()
    if not data or not data.get("qr"):
        log("")
        log("  扫码通道不可用时的备选方案：")
        log("    1) 手机浏览器登录https://account.xiaomi.com，登录后从 Cookie 取 userId/passToken")
        log("    2) 或沿用 kai648846760/xiaomiwallet 生成的 xiaomiconfig.json")
        log("    3) 然后执行: python3 xmwallet.py import <别名> <文件或JSON>")
        return
    show_qr(data["qr"])
    log("  请用小米手机「扫一扫」或小米账号 App 扫码，并在手机上点【允许登录】")
    res = poll_login(data.get("lp", ""), int(data.get("timeout", 300)))
    if not res or not res.get("passToken"):
        log("  登录失败或超时。")
        return
    upsert_account(name, res.get("userId", ""), res.get("passToken", ""), res.get("ssecurity", ""))
    log("")
    log("  ✔ 登录成功，凭据已保存到 %s" % os.path.basename(ACCOUNT_FILE))
    log("    userId = %s" % res.get("userId"))
    log("  下一步执行: python3 xmwallet.py run")


def cmd_import(name: str, src: str):
    """从文件 / JSON 字符串 / 交互输入导入凭据。

    兼容来源：
      · kai648846760/xiaomiwallet 的 xiaomiconfig.json
      · 3056810551/xiaomi-wallet-vip 的 xiaomi_account.json
      · 直接粘贴 JSON
    """
    data = None
    if os.path.isfile(src):
        data = load_json(src, None)
    else:
        try:
            data = json.loads(src)
        except Exception:
            data = None
    if data is None:
        log("  无法解析，改为手动输入。")
        uid = input("  userId: ").strip()
        pt = input("  passToken: ").strip()
        data = {"userId": uid, "passToken": pt}

    # 兼容嵌套结构
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        data = {**data, **data["data"]}
    if isinstance(data, list) and data:
        data = data[0]

    uid = str(data.get("userId") or data.get("uid") or "").strip()
    pt = (data.get("passToken") or data.get("passTokenV2")
          or data.get("token") or data.get("securityToken") or "").strip()
    ss = (data.get("ssecurity") or data.get("securityToken") or "").strip()
    if not (uid and pt):
        log("  缺少 userId 或 passToken，导入失败。")
        return
    upsert_account(name, uid, pt, ss)
    log("  ✔ 已导入账号「%s」userId=%s" % (name, uid))
    log("  下一步: python3 xmwallet.py status  验证凭据是否有效")


# ---------------------------------------------------------------- 会话与接口

class Wallet:
    """封装 STS 换票与任务接口。"""

    def __init__(self, user_id: str, pass_token: str):
        self.user_id = str(user_id or "")
        self.pass_token = pass_token or ""
        self.session = requests.Session()
        self.session.headers.update({
            "Host": API_HOST,
            "User-Agent": UA_MOBILE,
            "Referer": "https://m.jr.airstarfinance.net/mp/activity/videoActivity",
            "Origin": "https://m.jr.airstarfinance.net",
            "Accept": "application/json, text/plain, */*",
        })
        self.dev = get_device(self.user_id)

    # ---- 换票 ----
    def login_by_ticket(self) -> bool:
        if not (self.user_id and self.pass_token):
            return False
        try:
            s = requests.Session()
            s.get(STS_LOGIN_URL, headers={
                "User-Agent": UA_DESKTOP,
                "Cookie": "passToken=%s; userId=%s;" % (self.pass_token, self.user_id),
            }, timeout=25, allow_redirects=True, verify=False)
            ck = s.cookies.get_dict()
            c_uid = ck.get("cUserId")
            st = ck.get("serviceToken") or ck.get("jrairstar_serviceToken")
            if not (c_uid and st):
                return False
            self.session.cookies.set("cUserId", c_uid, domain=API_HOST)
            self.session.cookies.set("jrairstar_serviceToken", st, domain=API_HOST)
            self.session.cookies.set("serviceToken", st, domain=API_HOST)
            return True
        except Exception as e:
            log("    换票异常: %s" % e)
            return False

    def _get(self, path: str, params: Dict[str, Any], retry_ticket: bool = True):
        url = "https://%s/mp/api/generalActivity/%s" % (API_HOST, path)
        base = {
            "activityCode": ACTIVITY_CODE,
            "app": "com.mipay.wallet",
            "isNfcPhone": "true",
            "channel": "mipay_indexicon_TVcard",
            "deviceType": "2",
            "system": "1",
            "visitEnvironment": "2",
            "userExtra": USER_EXTRA,
        }
        base.update(params)
        try:
            r = self.session.get(url, params=base, timeout=20, verify=False)
            return r.json()
        except Exception as e:
            log("    请求 %s 异常: %s" % (path, e))
            if retry_ticket and self.login_by_ticket():
                try:
                    r = self.session.get(url, params=base, timeout=20, verify=False)
                    return r.json()
                except Exception:
                    pass
            return None

    # ---- 查询 ----
    def balance(self) -> Optional[Dict[str, Any]]:
        d = self._get("queryUserBalanceWithFrozen", {})
        if d and d.get("code") == 0:
            v = d.get("value") or {}
            total = int(v.get("totalBalance") or v.get("balance") or 0)
            avail = int(v.get("availableBalance") or v.get("totalBalance") or 0)
            return {"ok": True, "days": total / 100.0, "avail": avail / 100.0, "raw": v}
        if d and d.get("code") != 0:
            # 兼容另一套字段名
            d2 = self._get("queryUserGoldRichSum", {})
            if d2 and d2.get("code") == 0:
                return {"ok": True, "days": int(d2.get("value", 0)) / 100.0,
                        "avail": int(d2.get("value", 0)) / 100.0, "raw": d2}
        return None

    def history(self) -> List[Dict[str, Any]]:
        d = self._get("queryUserJoinList", {"pageNum": 1, "pageSize": 30})
        if not d or d.get("code") != 0:
            return []
        v = d.get("value") or {}
        rows = v.get("data") if isinstance(v, dict) else v
        return rows if isinstance(rows, list) else []

    # ---- 任务发现（双路径）----
    def get_task(self) -> Optional[Dict[str, Any]]:
        d = self._get("getTask", {"taskCode": TASK_CODE})
        if d and d.get("code") == 0 and d.get("value"):
            v = d["value"]
            return v.get("taskInfo") if "taskInfo" in v else v
        # 回落：列表接口里挑「浏览组浏览任务」
        url = "https://%s/mp/api/generalActivity/getTaskList" % API_HOST
        try:
            r = self.session.post(url, data={"activityCode": ACTIVITY_CODE},
                                  headers={"User-Agent": UA_MOBILE}, timeout=20, verify=False)
            j = r.json()
            if j.get("code") == 0:
                lst = (j.get("value") or {}).get("taskInfoList") or []
                for t in lst:
                    if "浏览" in (t.get("taskName") or ""):
                        return t
        except Exception:
            pass
        return None

    # ---- 任务动作 ----
    def click_task(self, task_id, brows_task_id, brows_click_url_id) -> bool:
        d = self._get("clickTask", {
            "taskId": task_id, "browsTaskId": brows_task_id,
            "browsClickUrlId": brows_click_url_id, "clickEntryType": "undefined",
            "festivalStatus": "0",
        })
        return bool(d and d.get("code") == 0)

    def complete_task(self, task_id, brows_task_id, brows_click_url_id, seconds: int):
        return self._get("completeTask", {
            "taskId": task_id, "taskCode": TASK_CODE, "browsTaskId": brows_task_id,
            "browsClickUrlId": brows_click_url_id, "clickEntryType": "undefined",
            "festivalStatus": "0", "completeTime": str(int(time.time() * 1000)),
            "browseTime": str(seconds * 1000 if seconds > 0 else 0),
        })

    def luck_draw(self, user_task_id: str = ""):
        p = {}
        if user_task_id:
            p["userTaskId"] = user_task_id
        return self._get("luckDraw", p)

    # ---- 时长解析 ----
    @staticmethod
    def parse_seconds(task: Dict[str, Any], url_info: Dict[str, Any]) -> int:
        url_info = url_info or {}
        raw = task.get("browseTime")
        if raw in (None, ""):
            raw = url_info.get("browseTime")
        for src in (task.get("taskName", ""), task.get("taskDesc", "")):
            m = re.search(r"浏览\s*(\d+)\s*秒", src or "")
            if m:
                return int(m.group(1))
        if raw in (None, ""):
            return 10
        try:
            v = int(raw)
            sec = int(round(v / 1000.0)) if v >= 1000 else v
            return max(0, min(sec, 120))
        except (ValueError, TypeError):
            return 10


# ---------------------------------------------------------------- 执行流程

def draw_prize(res) -> Optional[str]:
    if not res or res.get("code") != 0:
        return None
    v = res.get("value") or {}
    info = v.get("prizeInfo") or {}
    name = info.get("prizeName") or v.get("prizeName") or "会员时长"
    amount = info.get("amount") or v.get("prizeAmount") or 0
    try:
        amount = int(amount)
    except Exception:
        amount = 0
    return "%s (+%.2f天)" % (name, amount / 100.0)


def run_account(acc: Dict[str, Any]) -> str:
    name = acc.get("name", "?")
    uid = acc.get("userId", "")
    pt = acc.get("passToken", "")
    if not (uid and pt):
        return "账号 %s 凭据不完整，跳过" % name

    log("")
    log("=" * 56)
    log("  账号: %s (ID %s)" % (name, uid))
    log("=" * 56)

    w = Wallet(uid, pt)
    if not w.login_by_ticket():
        return "账号 %s 换票失败，passToken 可能已失效，请重新 login" % name
    log("  会话就绪")

    bal = w.balance()
    if bal:
        log("  当前会员时长: %.2f 天（可用 %.2f 天）" % (bal["days"], bal["avail"]))
    else:
        log("  会员时长查询失败，继续尝试任务")

    gained: List[str] = []
    rounds = 0
    while rounds < 10:
        task = w.get_task()
        if not task:
            log("  未取到任务，结束")
            break

        url_info = task.get("generalActivityUrlInfo") or {}
        status = task.get("completeStatus", 0)
        period_done = task.get("periodCompleteCount", 0)
        period_all = task.get("periodCount", 2)
        remain = task.get("remainChance", 0)
        user_task_id = str(task.get("userTaskId", "") or "")
        today_status = url_info.get("todayUserTaskStatus", 1)

        log("  [状态] code=%s 完成=%s/%s 抽奖机会=%s" % (status, period_done, period_all, remain))

        # A. 有遗留抽奖机会，先补领
        if remain and remain > 0:
            log("  检测到 %s 次未领取开奖，补领..." % remain)
            got = draw_prize(w.luck_draw(user_task_id))
            if got:
                gained.append(got)
                log("  ✔ 补领成功: %s" % got)
            time.sleep(jitter(2.0, 3.5))
            continue

        # B. 已达终态
        if status in (3, 4) or today_status == 2:
            log("  今日任务已完成（code=%s）" % status)
            break

        brows_click_url_id = url_info.get("browsClickUrlId", "")
        brows_task_id = url_info.get("id", 30)
        task_id = task.get("taskId", 813)
        if not brows_click_url_id:
            log("  服务端未下发广告标识，判定今日无可用广告")
            break

        seconds = Wallet.parse_seconds(task, url_info)
        rounds += 1
        log("  --- 第 %d 轮 ---" % (period_done + 1))

        w.click_task(task_id, brows_task_id, brows_click_url_id)
        if seconds == 0:
            wait = round(jitter(2.0, 3.5), 1)
            log("  0 秒即时任务，拟真缓冲 %.1f 秒" % wait)
        else:
            wait = seconds + random.randint(2, 5)
            log("  任务时长 %d 秒 + 缓冲，共等待 %d 秒" % (seconds, wait))
        time.sleep(wait)

        w.complete_task(task_id, brows_task_id, brows_click_url_id, seconds)
        time.sleep(jitter(1.5, 2.5))

        got = draw_prize(w.luck_draw(user_task_id))
        if got:
            gained.append(got)
            log("  ✔ 领取: %s" % got)
        else:
            log("  本轮上报完毕")
        time.sleep(jitter(2.5, 4.5))

    # 今日流水
    today = time.strftime("%Y-%m-%d")
    rows = [r for r in w.history() if (r.get("createTime") or "").startswith(today)]
    log("")
    log("  今日流水 (%d 条):" % len(rows))
    for i, r in enumerate(rows[:10], 1):
        try:
            val = int(r.get("value", 0))
        except Exception:
            val = 0
        log("   %d. %s  +%.2f天  %s" % (i, r.get("createTime"), val / 100.0, r.get("desc") or ""))

    bal2 = w.balance()
    if bal2:
        log("  结算后会员时长: %.2f 天" % bal2["days"])

    summary = "账号 %s：今日领取 %d 笔 %s" % (name, len(gained), "、".join(gained[-3:]) if gained else "（无）")
    log("  " + summary)
    return summary


def cmd_run(only: str = ""):
    accounts = load_accounts()
    if not accounts:
        log("还没有账号，先执行: python3 xmwallet.py login <别名>")
        return
    if only:
        accounts = [a for a in accounts if a.get("name") == only]
        if not accounts:
            log("未找到账号 %s" % only)
            return

    log("")
    log("############ 小米钱包每日任务 %s ############" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    results = []
    for i, acc in enumerate(accounts):
        results.append(run_account(acc))
        if i < len(accounts) - 1:
            d = random.randint(30, 90)
            log("")
            log("  账号间随机间隔 %d 秒，降低风控概率..." % d)
            time.sleep(d)
    log("")
    log("############ 全部执行完毕 ############")
    for r in results:
        log("  · " + r)
    notify("小米钱包任务完成", "；".join(results)[:180])


def cmd_status():
    accounts = load_accounts()
    if not accounts:
        log("还没有账号，先执行: python3 xmwallet.py login <别名>")
        return
    for acc in accounts:
        w = Wallet(acc.get("userId", ""), acc.get("passToken", ""))
        log("")
        log("账号: %s" % acc.get("name"))
        if not w.login_by_ticket():
            log("  换票失败，凭据可能已失效")
            continue
        bal = w.balance()
        if bal:
            log("  会员时长: %.2f 天（可用 %.2f 天）" % (bal["days"], bal["avail"]))
        today = time.strftime("%Y-%m-%d")
        rows = [r for r in w.history() if (r.get("createTime") or "").startswith(today)]
        log("  今日流水: %d 条" % len(rows))
        for r in rows[:10]:
            try:
                val = int(r.get("value", 0))
            except Exception:
                val = 0
            log("    %s  +%.2f天  %s" % (r.get("createTime"), val / 100.0, r.get("desc") or ""))
        log("  凭据更新于: %s" % (acc.get("updatedAt") or acc.get("createdAt") or "未知"))


def cmd_cron():
    if not in_termux():
        log("当前不在 Termux 环境，仅打印参考配置：")
    py = sys.executable or "python3"
    script = os.path.abspath(__file__)
    line = "0 9 * * * %s %s run >> %s 2>&1" % (py, script, LOG_FILE)
    log("")
    log("建议的 crontab 行（每天 9:00 执行）：")
    log("  " + line)
    log("")
    if in_termux():
        try:
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15).stdout or ""
        except Exception:
            cur = ""
        if script in cur:
            log("crontab 中已存在该任务，无需重复添加。")
        else:
            new = cur.rstrip("\n") + "\n" + line + "\n"
            p = subprocess.run(["crontab", "-"], input=new, text=True, timeout=20,
                               capture_output=True)
            if p.returncode == 0:
                log("✔ 已写入 crontab。请先执行一次: crond  或在 ~/.bashrc 加上 crond")
            else:
                log("写入失败，请手动执行: crontab -e  然后粘贴上面的行")
    log("")
    log("提示：Termux 需先安装 cronie  → pkg install cronie")
    log("      Android 8+ 也可用 termux-job-scheduler 更省电。")


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="小米钱包每日任务（Termux 版）")
    ap.add_argument("cmd", nargs="?", default="run",
                    choices=["login", "run", "status", "cron", "import"], help="子命令")
    ap.add_argument("name", nargs="?", default="", help="账号别名（login/指定账号 run 时用）")
    ap.add_argument("src", nargs="?", default="", help="凭据来源（import 时用：文件路径或 JSON）")
    args = ap.parse_args()

    if args.cmd == "login":
        if not args.name:
            sys.exit("用法: python3 xmwallet.py login <账号别名>")
        cmd_login(args.name)
    elif args.cmd == "run":
        cmd_run(args.name)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "import":
        if not args.name:
            sys.exit("用法: python3 xmwallet.py import <别名> <文件路径或JSON>")
        cmd_import(args.name, args.src)
    elif args.cmd == "cron":
        cmd_cron()


if __name__ == "__main__":
    main()
