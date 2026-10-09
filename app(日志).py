#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os,re,sys,time,random,requests
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
try:
    from patchright.sync_api import sync_playwright
except ImportError:
    from playwright.sync_api import sync_playwright

# --- 环境变量 (可在Settings里设置secrets或者私库直接填写在双引号里)---
EMAIL        = os.environ.get('EMAIL') or ""           # 登录邮箱,可选，作为备用, 建议填写
PASSWORD     = os.environ.get('PASSWORD') or ""        # 登录密码,可选，作为备用, 建议填写
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""      # Telegram Chat ID,可选，通知
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""    # Telegram Bot Token,可选
CRON_JOB     = os.environ.get('CRON_JOB') or ""      # cron-job.org: API_KEY,JOB_ID

BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# --- 代理配置（由工作流 shell 脚本写入 $GITHUB_ENV）---
IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "socks5://127.0.0.1:1080"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

# 日志
def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// 补全 window.chrome（只补缺，不覆盖真实字段）
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
window.chrome.loadTimes = window.chrome.loadTimes || function () { return {}; };
window.chrome.csi = window.chrome.csi || function () { return {}; };
if (!window.chrome.app) {
  window.chrome.app = { isInstalled: false,
    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
    RunningState: { CANT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } };
}
// 修复自动化环境下 permissions.query 的 notifications 特征
try {
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
    window.navigator.permissions.query = (p) =>
      (p && p.name === 'notifications')
        ? Promise.resolve({ state: (window.Notification && Notification.permission) || 'prompt' })
        : origQuery(p);
  }
} catch (e) {}
"""

def get_current_ip(proxy_server=None):
    """获取当前出口IP"""
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        # log(f"请求出口IP完成, status={resp.status_code}")
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"

def send_telegram_notification(status, old_due, new_due):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False

    # 获取运行时间
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    masked_EMAIL = EMAIL if EMAIL else "未配置"

    text = (
        f"🎉 HidenCloud 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {masked_EMAIL}\n"
        f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🕒 续期时间：{now}"
    )
    if CRON_NEXT_RUN_TEXT:
        text += f"\n⏰ 下次任务：{CRON_NEXT_RUN_TEXT}"
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        else:
            log(f"❌ Telegram 通知失败: {resp.text}")
            return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False

# =========================================================
# cron-job.org 写回
# CRON_JOB 格式：API_KEY,JOB_ID
# 成功续期后：下一次执行安排在成功时间 + 7 天的 08:00~08:59（Asia/Shanghai）
# 使用 expiresAt 让该任务只执行这一次，避免按月/年重复执行。
# =========================================================
CRON_API_BASE = "https://api.cron-job.org"
CRON_TIMEZONE = "Asia/Shanghai"
CRON_NEXT_RUN_TEXT = ""


def parse_cron_job(value):
    if not value:
        return None, None
    parts = [x.strip() for x in value.split(",", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        log("⚠️ CRON_JOB 格式错误，应为 API_KEY,JOB_ID")
        return None, None
    return parts[0], parts[1]


def _cron_response_text(resp):
    try:
        data = resp.json()
        return str(data)
    except Exception:
        return (resp.text or "")[:500]


def update_cron_job_after_success(success_time=None):
    """续期成功后把 cron-job.org 改到下一次续期时间。"""
    global CRON_NEXT_RUN_TEXT
    api_key, job_id = parse_cron_job(CRON_JOB)
    if not api_key or not job_id:
        log("⚠️ 未配置 CRON_JOB，跳过 cron-job.org 写回")
        return None

    tz = ZoneInfo(CRON_TIMEZONE)
    now = success_time.astimezone(tz) if success_time else datetime.now(tz)

    # 续期成功后第 7 天，上海时间 08:00~08:59 随机。
    target_date = (now + timedelta(days=7)).date()
    hour = 8
    minute = random.randint(0, 59)
    next_run = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, 0, tzinfo=tz
    )

    # expiresAt 稍晚于计划时间，使这个“单日计划”执行一次后自动失效。
    expires_at = next_run + timedelta(hours=1)
    expires_str = expires_at.strftime("%Y%m%d%H%M%S")

    payload = {
        "job": {
            "enabled": True,
            "schedule": {
                "timezone": CRON_TIMEZONE,
                "expiresAt": int(expires_str),
                "hours": [hour],
                "mdays": [target_date.day],
                "minutes": [minute],
                "months": [target_date.month],
                "wdays": [-1]
            }
        }
    }

    url = f"{CRON_API_BASE}/jobs/{job_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "HidenCloud-Renew/1.0"
    }

    CRON_NEXT_RUN_TEXT = next_run.strftime("%Y-%m-%d %H:%M:%S") + f" ({CRON_TIMEZONE})"
    log(f"⏰ 准备写回 cron-job.org")
    log(f"📅 下次续期时间：{CRON_NEXT_RUN_TEXT}")
    log(f"🆔 Cron Job ID：{job_id}")

    # 最多 3 次；429 优先遵循 Retry-After。
    for attempt in range(1, 4):
        try:
            resp = requests.patch(
                url,
                headers=headers,
                json=payload,
                timeout=20,
                proxies=REQUESTS_PROXIES
            )

            if resp.status_code in (200, 204):
                log("✅ cron-job.org 写回成功")
                log(f"⏰ 下次运行：{CRON_NEXT_RUN_TEXT}")
                return True

            body = _cron_response_text(resp)
            log(f"⚠️ Cron 写回第{attempt}次失败：HTTP {resp.status_code}: {body}")

            if attempt >= 3:
                break

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "")
                try:
                    wait_sec = max(1, int(float(retry_after)))
                except Exception:
                    wait_sec = 15 * attempt
                wait_sec = min(wait_sec, 60)
                log(f"⏳ 429，等待 {wait_sec} 秒后重试...")
            else:
                wait_sec = 15 * attempt
                log(f"⏳ 等待 {wait_sec} 秒后重试...")
            time.sleep(wait_sec)

        except Exception as e:
            log(f"⚠️ Cron 写回第{attempt}次异常：{e}")
            if attempt < 3:
                wait_sec = 15 * attempt
                log(f"⏳ 等待 {wait_sec} 秒后重试...")
                time.sleep(wait_sec)

    log("❌ cron-job.org 写回最终失败")
    return False


# =========================================================
# 未到续期时间时的 Cron 写回
# =========================================================
def update_cron_job_before_due(due_date_str):
    """未续期时安排在 Due Date 前一天上海时间 08:00～08:59:59 随机执行。"""
    global CRON_NEXT_RUN_TEXT

    api_key, job_id = parse_cron_job(CRON_JOB)
    if not api_key or not job_id:
        log("⚠️ 未配置 CRON_JOB，无法安排下一次续期")
        return False

    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    try:
        due_date = datetime.strptime(due_date_str.strip(), "%d %b %Y").date()
    except Exception as e:
        log(f"❌ Due Date 无法解析，无法写回 Cron: {due_date_str!r} / {e}")
        return False

    target_date = due_date - timedelta(days=1)

    if target_date > now.date():
        # 到期前一天，上海时间 08:00:00～08:59:59 随机
        next_run = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            8,
            random.randint(0, 59),
            random.randint(0, 59),
            tzinfo=tz
        )
    else:
        # 已进入续期窗口，避免写入过去时间
        next_run = (now + timedelta(minutes=10)).replace(second=0, microsecond=0)

    expires_at = next_run + timedelta(hours=1)

    payload = {
        "job": {
            "enabled": True,
            "schedule": {
                "timezone": "Asia/Shanghai",
                "expiresAt": int(expires_at.strftime("%Y%m%d%H%M%S")),
                "hours": [next_run.hour],
                "mdays": [next_run.day],
                "minutes": [next_run.minute],
                "months": [next_run.month],
                "wdays": [-1]
            }
        }
    }

    url = f"{CRON_API_BASE}/jobs/{job_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "HidenCloud-Renew/1.0"
    }

    CRON_NEXT_RUN_TEXT = next_run.strftime("%Y-%m-%d %H:%M:%S") + " (Asia/Shanghai)"
    log("⏰ 未到续期时间，更新 cron-job.org")
    log(f"📅 Due Date：{due_date_str}")
    log(f"📅 下次运行：{CRON_NEXT_RUN_TEXT}")

    for attempt in range(1, 4):
        try:
            resp = requests.patch(
                url,
                headers=headers,
                json=payload,
                timeout=20,
                proxies=REQUESTS_PROXIES
            )

            if resp.status_code in (200, 204):
                log("✅ cron-job.org 写回成功")
                return True

            body = _cron_response_text(resp)
            log(f"⚠️ Cron 写回第{attempt}次失败：HTTP {resp.status_code}: {body}")

            if attempt < 3:
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After", "")
                    try:
                        wait_sec = min(max(1, int(float(retry_after))), 60)
                    except Exception:
                        wait_sec = min(15 * attempt, 60)
                else:
                    wait_sec = min(15 * attempt, 60)
                time.sleep(wait_sec)

        except Exception as e:
            log(f"⚠️ Cron 写回第{attempt}次异常：{e}")
            if attempt < 3:
                time.sleep(min(15 * attempt, 60))

    log("❌ cron-job.org 写回最终失败")
    return False


# =========================================================
# Cloudflare Turnstile 处理
# 实测要点（dash.hidencloud.com 登录页）：
# - 新版 Turnstile 把挑战 iframe 渲染在「闭包 shadow DOM」里，
#   page.locator('iframe[src*=...]') 和 querySelectorAll 都找不到，
#   但 page.frames（浏览器层 frame 树）能看到，frame_element() 能拿到元素。
# - 隐藏 token 输入框 input[name="cf-turnstile-response"] 和其 300x65
#   容器一定在 light DOM，可作为兜底定位。
# - 点击：首选 frame_element.click(position=复选框位置)（Playwright 会把
#   事件路由进跨进程 iframe），失败再用 CDP 底层鼠标事件兜底。
# 通过信号（任一满足）：调用方自定义回调 / 页面 widget 全部生成 token /
# 挑战框被点击处理后持续消失
# =========================================================

TURNSTILE_IFRAME_SEL = 'iframe[src*="challenges.cloudflare.com"], iframe[title*="Cloudflare"]'
TURNSTILE_FRAME_URL_MARKER = 'challenges.cloudflare.com'

TURNSTILE_STATE_JS = """
() => {
    try {
        let total = 0, solved = 0;
        document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach(n => {
            total += 1;
            if (n.value && n.value.length > 20) solved += 1;
        });
        return { total: total, solved: solved };
    } catch (e) { return { total: 0, solved: 0 }; }
}
"""

_CDP_SESSIONS = {}

def get_cdp_session(page):
    # CDP 会话必须与 page 一一对应，否则点击会发到别的页面
    session = _CDP_SESSIONS.get(page)
    if session is None:
        try:
            session = page.context.new_cdp_session(page)
        except Exception as e:
            log(f"⚠️ 创建 CDP 会话失败: {e}")
            return None
        _CDP_SESSIONS[page] = session
    return session

def reset_cdp_session(page):
    session = _CDP_SESSIONS.pop(page, None)
    try:
        if session is not None:
            session.detach()
    except Exception:
        pass

def cdp_click_at(page, x, y):
    """通过 CDP 在浏览器内核层注入真实鼠标事件（isTrusted=true）"""
    session = get_cdp_session(page)
    if not session:
        return False
    try:
        # 模拟真人：从附近位置分多步平滑移动到目标点
        sx = x - random.uniform(50, 110)
        sy = y - random.uniform(35, 75)
        steps = random.randint(8, 14)
        for i in range(1, steps + 1):
            ix = sx + (x - sx) * i / steps + random.uniform(-1.5, 1.5)
            iy = sy + (y - sy) * i / steps + random.uniform(-1.5, 1.5)
            session.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': ix, 'y': iy})
            time.sleep(random.uniform(0.01, 0.035))
        time.sleep(random.uniform(0.1, 0.25))
        session.send('Input.dispatchMouseEvent', {
            'type': 'mousePressed', 'x': x, 'y': y,
            'button': 'left', 'buttons': 1, 'clickCount': 1
        })
        time.sleep(random.uniform(0.05, 0.12))
        session.send('Input.dispatchMouseEvent', {
            'type': 'mouseReleased', 'x': x, 'y': y,
            'button': 'left', 'clickCount': 1
        })
        return True
    except Exception as e:
        log(f"⚠️ CDP 底层点击失败: {e}")
        reset_cdp_session(page)
        return False

def turnstile_state(page):
    try:
        st = page.evaluate(TURNSTILE_STATE_JS)
        if isinstance(st, dict):
            return {"total": int(st.get("total", 0)), "solved": int(st.get("solved", 0))}
    except Exception:
        pass
    return {"total": 0, "solved": 0}

def _overlaps(box, boxes, dx=25, dy=25, dw=60):
    for b in boxes:
        if (abs(b['x'] - box['x']) < dx and abs(b['y'] - box['y']) < dy
                and abs(b['width'] - box['width']) < dw):
            return True
    return False

def challenge_frames(page):
    """真实挑战 iframe：frame 树反查（覆盖 shadow DOM 内嵌）+ light DOM 兜底"""
    targets = []
    seen = []

    # 1) frame 树里反查挑战 iframe —— 唯一能覆盖 shadow DOM 内嵌 iframe 的方式
    try:
        for f in page.frames:
            if TURNSTILE_FRAME_URL_MARKER not in (f.url or ''):
                continue
            try:
                fe = f.frame_element()
                if fe.is_visible():
                    box = fe.bounding_box()
                    if box and box.get('width', 0) > 10 and box.get('height', 0) > 10:
                        seen.append(box)
                        targets.append((fe, box))
            except Exception:
                continue
    except Exception:
        pass

    # 2) light DOM 里的挑战 iframe（老版结构）
    try:
        for el in page.locator(TURNSTILE_IFRAME_SEL).all():
            try:
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if box and box.get('width', 0) > 10 and box.get('height', 0) > 10 \
                        and not _overlaps(box, seen):
                    seen.append(box)
                    targets.append((el, box))
            except Exception:
                continue
    except Exception:
        pass

    return targets

def challenge_containers(page):
    targets = []
    try:
        for el in page.locator(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').all():
            try:
                if el.evaluate("n => !!(n.value && n.value.length > 20)"):
                    continue
                box = el.evaluate("""n => {
                    let p = n.parentElement;
                    for (let i = 0; i < 4 && p; i++) {
                        const r = p.getBoundingClientRect();
                        if (r.width > 40 && r.height > 20)
                            return {x: r.x, y: r.y, width: r.width, height: r.height};
                        p = p.parentElement;
                    }
                    return null;
                }""")
                if box and not _overlaps(box, [b for _, b in targets]):
                    targets.append((None, box))
            except Exception:
                continue
    except Exception:
        pass
    return targets

def challenge_boxes(page):
    """合并挑战框目标（iframe 优先，容器兜底去重），供点击与弹窗检测使用"""
    frames = challenge_frames(page)
    seen = [b for _, b in frames]
    targets = list(frames)
    for el, box in challenge_containers(page):
        if not _overlaps(box, seen):
            targets.append((el, box))
    return targets

def page_ready(p):
    """页面不是 Cloudflare 拦截页 / 安全验证页"""
    try:
        t = (p.title() or "").lower()
        blocked = ("just a moment", "attention required", "checking your browser",
                   "请稍候", "security verification", "请验证")
        return bool(t) and not any(k in t for k in blocked)
    except Exception:
        return False

def solve_turnstile(page, timeout=120, success_check=None,
                    require_positive=False, appear_grace=5, reload_after=None,
                    shot_on_timeout="turnstile_timeout.png"):

    log(f"🛡️ 开始处理 Turnstile...")
    start = time.time()
    baseline = turnstile_state(page)
    had_iframe = False
    iframe_gone_since = None
    container_only_since = None
    click_count = 0
    reload_done = 0

    while time.time() - start < timeout:
        # 通过信号 1：调用方自定义判定
        if success_check is not None:
            try:
                if success_check(page):
                    log("✅ Turnstile 验证通过！")
                    return True
            except Exception:
                pass

        # 通过信号 2：出现了新 widget（或新增已解决数）且页面上 widget 全部拿到 token
        st = turnstile_state(page)
        if st["total"] > 0 and st["solved"] >= st["total"] and (
                st["total"] > baseline["total"] or st["solved"] > baseline["solved"]):
            log(f"✅ Turnstile 验证通过（token 已生成 {st['solved']}/{st['total']}）！")
            return True

        frames = challenge_frames(page)
        seen = [b for _, b in frames]
        targets = list(frames) + [(el, b) for el, b in challenge_containers(page)
                                  if not _overlaps(b, seen)]

        if frames:
            had_iframe = True
            iframe_gone_since = None
            container_only_since = None
        elif targets:
            had_iframe = True
            iframe_gone_since = None
            if container_only_since is None:
                container_only_since = time.time()
            elif time.time() - container_only_since >= 12:
                log("✅ Turnstile 验证通过（挑战已结束）！")
                return True
        else:
            container_only_since = None
            if had_iframe:
                if iframe_gone_since is None:
                    iframe_gone_since = time.time()
                elif time.time() - iframe_gone_since >= 8:
                    log("✅ Turnstile 验证通过（挑战框已消失）！")
                    return True
            elif (not require_positive and success_check is None
                    and time.time() - start >= appear_grace):
                log("ℹ️ 页面未出现 Turnstile，无需处理")
                return True
            time.sleep(1)
            continue

        # 逐个点击挑战框：Playwright 定点点击为主，CDP 底层点击兜底
        for el, box in targets:
            clicked = False
            try:
                off_x = min(30, box['width'] / 2)
                pos_y = box['height'] / 2
                if el is not None:
                    try:
                        el.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    try:
                        el.click(position={'x': off_x, 'y': pos_y}, timeout=5000)
                        clicked = True
                        log(f"🖱️ 点击 Turnstile 验证 ({box['x'] + off_x:.0f}, {box['y'] + pos_y:.0f}) ...")
                    except Exception as e:
                        log(f"⚠️ 挑战框点击失败,尝试底层点击...")
                if not clicked:
                    cx = box['x'] + off_x + random.uniform(-2, 2)
                    cy = box['y'] + pos_y + random.uniform(-2, 2)
                    log(f"🖱️ CDP 底层点击 Turnstile ({cx:.0f}, {cy:.0f}) ...")
                    clicked = cdp_click_at(page, cx, cy)
            except Exception as e:
                log(f"⚠️ 点击挑战框出错: {e}")
            click_count += 1
            # 间隔放宽：点击成功后 widget 会进入数秒的"验证中"状态，
            time.sleep(random.uniform(4.0, 6.0))

        # 多次点击仍未通过：刷新页面拿一个全新的挑战再试
        if reload_after and click_count >= reload_after and reload_done < 2:
            reload_done += 1
            log(f"🔄 累计点击 {click_count} 次未通过，刷新页面重试（第 {reload_done}/2 次）...")
            click_count = 0
            had_iframe = False
            iframe_gone_since = None
            container_only_since = None
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"⚠️ 刷新失败: {e}")
            time.sleep(random.uniform(3.0, 5.0))

    log(f"❌ Turnstile 处理超时（{timeout}s）")
    try:
        cf = [f.url[:100] for f in page.frames if TURNSTILE_FRAME_URL_MARKER in (f.url or '')]
        st = turnstile_state(page)
        log(f"🔍 超时现场: cf_frames={len(cf)} token={st} title={page.title()!r}")
        page.screenshot(path=shot_on_timeout)
        log(f"📸 已保存超时截图: {shot_on_timeout}")
    except Exception:
        pass
    return False

# ===== Invoice / Login session diagnostics =====
_REQUEST_LOG = []
_MAX_REQUEST_LOG = 250

def _short_url(url, max_len=220):
    try:
        from urllib.parse import urlsplit
        u = urlsplit(url)
        return f"{u.scheme}://{u.netloc}{u.path}"[:max_len]
    except Exception:
        return str(url)[:max_len]

def _interesting_url(url):
    u = (url or '').lower()
    return any(k in u for k in ('/renew','/payment/','/invoice','/auth/login','/login','challenge-platform','challenges.cloudflare.com','/service/','csrf','logout'))

def attach_network_debug(page):
    def on_request(req):
        try:
            if not _interesting_url(req.url): return
            rec={'kind':'REQ','ts':time.strftime('%H:%M:%S'),'method':req.method,'url':_short_url(req.url),'resource':req.resource_type}
            _REQUEST_LOG.append(rec)
            if len(_REQUEST_LOG)>_MAX_REQUEST_LOG: del _REQUEST_LOG[:-_MAX_REQUEST_LOG]
            log(f"🌐 [REQ] {req.method} {req.resource_type} {_short_url(req.url)}")
        except Exception: pass
    def on_response(resp):
        try:
            if not _interesting_url(resp.url): return
            location=(resp.headers or {}).get('location','')
            rec={'kind':'RESP','ts':time.strftime('%H:%M:%S'),'status':resp.status,'url':_short_url(resp.url),'location':_short_url(location) if location else ''}
            _REQUEST_LOG.append(rec)
            if len(_REQUEST_LOG)>_MAX_REQUEST_LOG: del _REQUEST_LOG[:-_MAX_REQUEST_LOG]
            if location:
                log(f"🌐 [RESP] {resp.status} {_short_url(resp.url)} -> Location: {_short_url(location)}")
            else:
                log(f"🌐 [RESP] {resp.status} {_short_url(resp.url)}")
        except Exception: pass
    page.on('request',on_request)
    page.on('response',on_response)

def cookie_snapshot(context):
    try:
        cookies=context.cookies([BASE_URL])
        return {c.get('name'):{k:c.get(k) for k in ('domain','path','httpOnly','secure','sameSite','expires')} for c in cookies}
    except Exception as e:
        log(f"⚠️ Cookie 快照失败: {e}")
        return {}

def log_cookie_diff(before, after, label='Cookie'):
    b=set(before or {}); a=set(after or {})
    added=sorted(a-b); removed=sorted(b-a)
    changed=sorted(k for k in a&b if before.get(k)!=after.get(k))
    log(f"🍪 {label}: 新增={added or '无'}, 删除={removed or '无'}, 属性变化={changed or '无'}")

def save_login_diagnostics(page, context, prefix='invoice_login_redirect'):
    try: page.screenshot(path=f'{prefix}.png', full_page=True)
    except Exception: pass
    try: Path(f'{prefix}.html').write_text(page.content(),encoding='utf-8')
    except Exception: pass
    try:
        with open(f'{prefix}_network.txt','w',encoding='utf-8') as f:
            f.write(f'URL: {page.url}\nTitle: {page.title()}\n')
            f.write('--- interesting requests/responses ---\n')
            for rec in _REQUEST_LOG[-150:]: f.write(repr(rec)+'\n')
            f.write('--- cookies (names only) ---\n')
            for name,meta in cookie_snapshot(context).items(): f.write(f'{name}: {meta}\n')
        log(f"📦 已保存诊断: {prefix}.png / {prefix}.html / {prefix}_network.txt")
    except Exception as e: log(f"⚠️ 保存诊断失败: {e}")

def login(page):
    # 始终使用账号密码登录，不再使用 Cookie 登录或 Cookie 写回。
    if not EMAIL or not PASSWORD:
        log("❌ 未配置 EMAIL/PASSWORD，无法进行账号密码登录")
        return False

    log("💣 使用账号密码登录...")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

        # --- 第一道 Turnstile：验证通过后才会显示账号密码输入框 ---
        def login_form_visible(p):
            try:
                return p.locator('input[type="password"]').first.is_visible()
            except Exception:
                return False

        log("🛡️ 处理登录页第一道 Turnstile验证...")
        if not solve_turnstile(page, timeout=180, success_check=login_form_visible,
                               reload_after=8,
                               shot_on_timeout="login_turnstile1_fail.png"):
            log("❌ 第一道 Turnstile 未通过，无法进入登录表单")
            page.screenshot(path="login_turnstile1_fail.png")
            return False

        # --- 填写账号密码 ---
        email_sel = ('input[name="username"], input#username, input[name="email"], '
                     'input[type="email"], input[name="EMAIL"]')
        pwd_sel = ('input[name="password"], input#password, '
                   'input[name="PASSWORD"], input[type="password"]')
        email_input = page.locator(email_sel).first
        pwd_input = page.locator(pwd_sel).first
        email_input.wait_for(state="visible", timeout=60000)
        log("⌨️ 输入账号...")
        email_input.click()
        email_input.fill(EMAIL)
        time.sleep(random.uniform(0.8, 1.5))
        log("⌨️ 输入密码...")
        pwd_input.click()
        pwd_input.fill(PASSWORD)

        # --- 等待第二道 Turnstile 出现 ---
        log("⏳ 输入完成，等待turnstile加载...")
        time.sleep(8)
        log("🛡️ 处理第二道 Turnstile...")
        if not solve_turnstile(page, timeout=90, require_positive=True,
                               shot_on_timeout="login_turnstile2_fail.png"):
            log("⚠️ 第二道 Turnstile 未确认通过，仍尝试点击登录...")

        # --- 点击登录按钮 ---
        submit_btn = page.locator('button[type="submit"], button:has-text("Login"), '
                                  'button:has-text("Sign in"), button:has-text("登录")').first
        log("🖱️ 点击登录按钮...")
        try:
            submit_btn.click(timeout=15000)
        except Exception as e:
            log(f"⚠️ 点击登录按钮失败: {e}")
            page.screenshot(path="login_submit_fail.png")
            return False

        # 提交后若再出现 Turnstile，边处理边等待跳转
        solve_turnstile(page, timeout=45, success_check=lambda p: "auth/login" not in p.url,
                        shot_on_timeout="login_turnstile3_fail.png")
        try:
            page.wait_for_url(lambda u: "auth/login" not in u, timeout=30000)
        except Exception:
            pass

        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)
        page_title = page.title()
        log(f"📝 当前Title: {page_title}")
        if "auth/login" in page.url:
            log("❌ 登录失败，账号密码错误或被封禁")
            page.screenshot(path="login_fail.png")
            return False
        log("✅ 账号密码登录成功！当前已到达dashboard页面")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        page.screenshot(path="login_fail.png")
        return False

def get_server_id(page):
    try:
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)
        time.sleep(3)
        html = page.content()
        log(f"📝 页面长度: {len(html)}, URL: {page.url}")

        # 方案1: 从 href 链接中提取 /service/数字/manage
        matches = re.findall(r'/service/(\d+)/manage', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从链接中获取到 Server ID: {server_id}")
            return server_id

        # 方案2: 从 span 标签中提取 #数字 (如 "Free Server #218079")
        matches = re.findall(r'#(\d{4,})', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从文本 #号中获取到 Server ID: {server_id}")
            return server_id

        log("❌ 所有 URL 均未找到 Server ID")
        return None
    except Exception as e:
        log(f"❌ 获取 Server ID 失败: {e}")
        page.screenshot(path="server_id_error.png")
        return None

def get_due_date(page):
    try:
        if SERVICE_URL not in page.url:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)
        body_text = page.locator("body").inner_text()
        patterns = [
            r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date.*?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
            if match:
                due_date = match.group(1).strip()
                log(f"📅 获取到Due Date: {due_date}")
                return due_date
    except Exception as e:
        log(f"❌ 获取Due Date失败: {e}")
    return "未知"

def renew_service(page):
    try:
        log("➡ 进入续期流程...")
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        solve_turnstile(page, timeout=60, success_check=page_ready, reload_after=8)

        log("🖱️ 准备点击 'Renew' 按钮...")
        renew_btn=page.locator('button:has-text("Renew")')
        create_btn=page.locator('button:has-text("Create Invoice")')
        modal_opened=False
        for i in range(6):
            try:
                renew_btn.wait_for(state="visible",timeout=10000)
                renew_btn.scroll_into_view_if_needed()
                log(f"🖱️ 第 {i+1} 次尝试点击 'Renew'...")
                renew_btn.click(); time.sleep(2)
                page_text=page.locator("body").inner_text()
                if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                    log("⚠️ 未到续期时间，无法续期。"); page.screenshot(path="renew_not_allowed.png"); return "NOT_TIME"
                log("🖲️ 等待弹窗出现...")
                try:
                    create_btn.wait_for(state="visible",timeout=5000); modal_opened=True; log("✅ 弹窗已成功弹出！"); break
                except Exception:
                    if challenge_boxes(page): modal_opened=True; log("✅ 弹窗已弹出（先出现 Turnstile 验证）！"); break
                    log("⚠️ 弹窗未出现，可能是点击未响应，准备重试..."); time.sleep(2)
            except Exception as e: log(f"❌ 点击尝试出错: {e}")
        if not modal_opened:
            log("❌ 错误：尝试多次后，续费弹窗仍未出现。"); page.screenshot(path="renew_modal_failed.png"); return False

        log("🛡️ 处理弹窗内的 Turnstile...")
        token_ok=solve_turnstile(page,timeout=90,require_positive=True,shot_on_timeout="modal_turnstile_fail.png")
        st=turnstile_state(page); log(f"🔐 Create Invoice 前 Turnstile 状态: {st}")
        if not token_ok or st['total']<=0 or st['solved']<st['total']:
            log("❌ Create Invoice 前没有确认有效 Turnstile token，停止提交")
            page.screenshot(path="create_invoice_blocked_no_token.png"); return False
        try: create_btn.wait_for(state="visible",timeout=30000)
        except Exception: pass
        if not create_btn.is_visible():
            log("❌ Create Invoice 按钮不可见"); page.screenshot(path="create_invoice_not_visible.png"); return False

        before_url=page.url; before_cookies=cookie_snapshot(page.context)
        log(f"📌 Create Invoice 前 URL: {before_url}"); log(f"📌 Create Invoice 前 Cookie 数量: {len(before_cookies)}")
        log(f"📌 Create Invoice 前 Turnstile: {turnstile_state(page)}")
        try:
            log("🖱️ 点击 'Create Invoice'（第 1 次）..."); create_btn.click(timeout=10000)
        except Exception as e:
            log(f"❌ 点击 'Create Invoice' 失败: {e}"); page.screenshot(path="create_invoice_failed.png"); return False

        log("⏳ Create Invoice 已点击，观察后端响应/页面跳转（最长 120 秒）...")
        start_wait=time.time(); last_url=page.url; last_cookie=time.time()
        while time.time()-start_wait<120:
            current=page.url
            if current!=last_url:
                log(f"🔀 页面 URL 变化: {last_url} -> {current}"); last_url=current
            if "/payment/invoice/" in current:
                log(f"🎉 已进入 Invoice 页面: {current}"); new_invoice_url=current; break
            if "/auth/login" in current:
                log("❌ Create Invoice 后被重定向到 Login！")
                log_cookie_diff(before_cookies,cookie_snapshot(page.context),"Create Invoice 前后 Cookie")
                save_login_diagnostics(page,page.context,"invoice_login_redirect")
                return False
            if time.time()-last_cookie>=3:
                log_cookie_diff(before_cookies,cookie_snapshot(page.context),"Create Invoice Cookie"); last_cookie=time.time()
            frames=challenge_frames(page)
            if frames:
                log(f"🛡️ Create Invoice 后检测到 {len(frames)} 个 Cloudflare challenge frame，正常处理...")
                solve_turnstile(page,timeout=45,require_positive=True,shot_on_timeout="invoice_turnstile_fail.png")
            time.sleep(1)
        else:
            new_invoice_url=None

        if not new_invoice_url:
            log("❌ 120 秒内未进入 Invoice 页面")
            log(f"🔎 最终 URL: {page.url}"); log(f"🔎 最终 Title: {page.title()!r}")
            log_cookie_diff(before_cookies,cookie_snapshot(page.context),"最终 Cookie")
            if "/auth/login" in page.url: save_login_diagnostics(page,page.context,"invoice_login_timeout")
            else:
                try:
                    page.screenshot(path="renew_stuck_invoice.png",full_page=True); Path("renew_stuck_invoice.html").write_text(page.content(),encoding="utf-8")
                except Exception: pass
            return False

        if page.url!=new_invoice_url: page.goto(new_invoice_url,wait_until="domcontentloaded",timeout=60000)
        solve_turnstile(page,timeout=60,success_check=page_ready,reload_after=8)
        log("🔎 查找 'Pay' 按钮...")
        pay_btn=page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first
        try:
            pay_btn.wait_for(state="visible",timeout=30000); pay_btn.click(); log("✅ 'Pay' 按钮已点击。")
        except Exception as e:
            log(f"❌ Pay 按钮未找到/无法点击: {e}"); page.screenshot(path="pay_button_failed.png"); return False
        time.sleep(5)
        page.goto(SERVICE_URL,wait_until="domcontentloaded",timeout=60000)
        solve_turnstile(page,timeout=60,success_check=page_ready,reload_after=8)
        return True
    except Exception as e:
        log(f"❌ 续费异常: {e}")
        try: page.screenshot(path="renew_error.png",full_page=True)
        except Exception: pass
        return False

def main():
    # 检查必要环境变量：仅使用账号密码登录
    log(f"🔍 凭证检测: EMAIL={'已配置' if EMAIL else '未配置'}, PASSWORD={'已配置' if PASSWORD else '未配置'}")
    if not (EMAIL and PASSWORD):
        log("❌ 缺少 EMAIL/PASSWORD 登录凭证")
        sys.exit(1)

    global SERVICE_URL

    with sync_playwright() as p:
        try:
            if IS_PROXY:
                log(f"⚙️ 代理已启用: {PROXY_SERVER}")
            else:
                log("🌐 直连模式（未使用代理）")

            # 获取当前出口ip
            current_ip = get_current_ip(PROXY_SERVER)
            log(f"🎯 当前出口IP: {current_ip}")

            log("🚀 启动浏览器...")
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled',
                      '--disable-infobars', '--window-size=1920,1080']
            )

            context = browser.new_context(
                no_viewport=True,
                proxy={"server": PROXY_SERVER} if IS_PROXY else None
            )
            page = context.new_page()
            page.add_init_script(STEALTH_JS)
            attach_network_debug(page)
            log("🔎 已启用 Create Invoice / Login 网络诊断")

            if not login(page):
                sys.exit(1)

            # 登录成功后，自动获取 Server ID
            server_id = get_server_id(page)
            if not server_id:
                log("❌ 无法获取 Server ID，退出。")
                sys.exit(1)
            SERVICE_URL = f"{BASE_URL}/service/{server_id}/manage"

            # 获取旧到期时间
            old_due = get_due_date(page)
            log(f"📆 续费前到期时间：{old_due}")

            # 执行续费
            success_time = datetime.now(ZoneInfo(CRON_TIMEZONE))
            renew_result = renew_service(page)

            new_due = old_due
            cron_result = None
            verified_success = False

            if renew_result == "NOT_TIME":
                log("⏳ 未到续期时间，目前无法续期")
                cron_result = update_cron_job_before_due(old_due)
                if cron_result:
                    status = "⏳ 未到续期时间\n⏰ Cron 已更新"
                else:
                    status = "⏳ 未到续期时间\n❌ Cron 更新失败"
            elif renew_result is False:
                log("❌ 续费失败，脚本退出。")
                status = "❌ 续期失败"
            else:  # renew_result is True
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")

                # 最终成功判定：Due Date 必须实际增加 7 天。
                try:
                    old_dt = datetime.strptime(old_due, "%d %b %Y")
                    new_dt = datetime.strptime(new_due, "%d %b %Y")
                    delta_days = (new_dt - old_dt).days
                    log(f"🔎 Due Date 实际变化：{delta_days} 天")
                    verified_success = (delta_days == 7)
                except Exception as e:
                    log(f"⚠️ 无法计算 Due Date 变化：{e}")
                    verified_success = False

                if verified_success:
                    status = "✅ 续期成功"
                    log("✅ 已确认 Due Date 实际增加 7 天")
                    cron_result = update_cron_job_after_success(success_time)
                    if cron_result is True:
                        status += "\n⏰ Cron 已更新"
                    elif cron_result is False:
                        status += "\n⚠️ Cron 更新失败"
                    else:
                        status += "\nℹ️ 未配置 Cron"
                else:
                    status = "❌ 续期结果未确认"
                    log("❌ Due Date 没有确认增加 7 天，因此不会修改 cron-job")

            # 发送 Telegram 通知
            send_telegram_notification(status, old_due, new_due)

            if renew_result == "NOT_TIME":
                if cron_result is False:
                    sys.exit(1)
                sys.exit(0)
            elif renew_result is False:
                sys.exit(1)
            elif not verified_success:
                sys.exit(1)
            elif cron_result is False:
                # 续期本身成功，但 Cron 写回失败；让 GitHub Actions 明确标红，避免悄悄错过下次任务。
                sys.exit(1)
            else:
                sys.exit(0)
        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if 'browser' in locals() and browser:
                browser.close()

if __name__ == "__main__":
    main()
