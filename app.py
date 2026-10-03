#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os,re,sys,time,random,requests,json,datetime,urllib.request
from playwright.sync_api import sync_playwright

# --- 环境变量 ---
COOKIE_VALUE = os.environ.get('COOKIE_VALUE') or ""    # remember_web cookie 值，必填
EMAIL        = os.environ.get('EMAIL') or ""           # 登录邮箱,可选，作为备用,TG通知需要填写
PASSWORD     = os.environ.get('PASSWORD') or ""        # 登录密码,可选，作为备用
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""    # Telegram Bot Token,可选
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""      # Telegram Chat ID,可选
CRON_JOB     = os.environ.get('CRON_JOB') or ""       # Cron-Job.org: API_KEY,JOB_ID

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
window.chrome = { runtime: {} };
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
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 

    text = (
        f"🎉 HidenCloud 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {masked_email}\n"
        f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🕒 续期时间：{now}"
    )
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

def update_cronjob_schedule(run_time):
    """将 Cron-Job.org 下一次执行时间设置为指定的北京时间"""
    if not CRON_JOB or "," not in CRON_JOB:
        log("⚠️ 未配置 CRON_JOB，跳过写回调度")
        return False

    try:
        api_key, job_id = [x.strip() for x in CRON_JOB.split(",", 1)]

        data = {
            "job": {
                "schedule": {
                    "timezone": "Asia/Shanghai",
                    "expiresAt": 0,
                    "hours": [run_time.hour],
                    "minutes": [run_time.minute],
                    "mdays": [run_time.day],
                    "months": [run_time.month],
                    "wdays": [-1],
                }
            }
        }

        url = f"https://api.cron-job.org/jobs/{job_id}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = json.dumps(data).encode("utf-8")

        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url, data=payload, headers=headers, method="PATCH"
                )
                with urllib.request.urlopen(req, timeout=15):
                    pass
                log(
                    f"🔁 Cron 写回成功：下次触发 "
                    f"{run_time.strftime('%Y-%m-%d %H:%M')}（北京时间）"
                )
                return True
            except Exception as e:
                log(f"⚠️ Cron 写回第{attempt + 1}次失败：{e}")
                if attempt < 2:
                    time.sleep(5)

        return False
    except Exception as e:
        log(f"❌ Cron 写回异常：{e}")
        return False


def schedule_next_run_before_due(due_date_str):
    """只有到期日前一天才能续期，自动计算下一次可续期时间。"""
    try:
        due_date = datetime.datetime.strptime(due_date_str, "%d %b %Y").date()
        allowed_date = due_date - datetime.timedelta(days=1)

        bj_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        today = bj_now.date()

        if today < allowed_date:
            # 在可续期日前：安排到可续期日 00:05（北京时间）
            # 在可续期日 08:00-08:59 随机安排执行时间
            random_hour = 8
            random_minute = random.randint(0, 59)
            next_run = datetime.datetime.combine(
                allowed_date,
                datetime.time(hour=random_hour, minute=random_minute)
            )
            log(
                f"⏳ 当前还不能续期：到期日 {due_date.strftime('%Y-%m-%d')}，"
                f"可续期日为 {allowed_date.strftime('%Y-%m-%d')}，"
                f"Cron 随机安排至 {next_run.strftime('%Y-%m-%d %H:%M')}"
            )
        else:
            # 无论何时发现“未到续期时间”，统一安排到可续期日期当天
            # 08:00-08:59 随机执行
            random_hour = 8
            random_minute = random.randint(0, 59)
            next_run = datetime.datetime.combine(
                allowed_date,
                datetime.time(hour=random_hour, minute=random_minute)
            )
            log(
                f"📅 可续期日为 {allowed_date.strftime('%Y-%m-%d')}，"
                f"Cron 随机安排至 {next_run.strftime('%Y-%m-%d %H:%M')}"
            )

        update_cronjob_schedule(next_run)

    except Exception as e:
        log(f"❌ 计算下次可续期时间失败：{e}")
        bj_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        update_cronjob_schedule(bj_now + datetime.timedelta(minutes=5))

def inspect_turnstile(page, label=""):
    """只检查 Turnstile 状态，不写入/伪造 token。"""
    try:
        iframe_count = page.locator('iframe[src*="challenges.cloudflare.com"]').count()
    except Exception:
        iframe_count = -1

    token_count = 0
    token_lengths = []
    try:
        token_loc = page.locator(
            'input[name="cf-turnstile-response"], '
            'textarea[name="cf-turnstile-response"]'
        )
        token_count = token_loc.count()
        for i in range(token_count):
            try:
                value = token_loc.nth(i).input_value(timeout=1000)
                token_lengths.append(len(value or ""))
            except Exception:
                token_lengths.append(-1)
    except Exception:
        pass

    prefix = f" [{label}]" if label else ""
    log(
        f"🔎 Turnstile{prefix}: "
        f"iframe={iframe_count}, response字段={token_count}, "
        f"token长度={token_lengths}"
    )
    return iframe_count, token_lengths


def handle_cloudflare(page):
    """等待 Cloudflare/Turnstile 正常完成；不绕过验证。"""
    iframe_selector = 'iframe[src*="challenges.cloudflare.com"]'
    if page.locator(iframe_selector).count() == 0:
        return True

    log("⚠️ 检测到 Cloudflare/Turnstile 验证...")
    inspect_turnstile(page, "开始")

    start_time = time.time()
    clicked = False

    while time.time() - start_time < 60:
        try:
            if page.locator(iframe_selector).count() == 0:
                log("✅ Cloudflare 验证界面已消失")
                inspect_turnstile(page, "验证结束")
                return True

            frame = page.frame_locator(iframe_selector)
            checkbox = frame.locator('input[type="checkbox"]')

            if not clicked:
                try:
                    if checkbox.is_visible(timeout=1000):
                        log("🖱️ 检测到可见验证复选框，执行正常点击...")
                        time.sleep(random.uniform(0.5, 1.5))
                        checkbox.click()
                        clicked = True
                        log("⏳ 已点击验证，等待浏览器正常生成结果...")
                except Exception:
                    pass

            inspect_turnstile(page, "等待中")

            # 如果隐藏响应字段已有非空值，说明浏览器端已经生成 token。
            try:
                token_loc = page.locator(
                    'input[name="cf-turnstile-response"], '
                    'textarea[name="cf-turnstile-response"]'
                )
                for i in range(token_loc.count()):
                    try:
                        if len(token_loc.nth(i).input_value(timeout=500) or "") > 0:
                            log("✅ 检测到非空 Turnstile response")
                            return True
                    except Exception:
                        pass
            except Exception:
                pass

        except Exception:
            pass

        time.sleep(2)

    log("⚠️ Cloudflare/Turnstile 等待超时，未检测到完成状态")
    inspect_turnstile(page, "超时")
    return False

def login(page):
    # 1. Cookie 登录尝试
    if COOKIE_VALUE:
        log("📇 尝试 Cookie 登录...")
        try:
            page.context.add_cookies([{
                'name': 'remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d',
                'value': COOKIE_VALUE,
                'domain': 'dash.hidencloud.com',
                'path': '/',
                'expires': int(time.time()) + 3600 * 24 * 365,
                'httpOnly': True,
                'secure': True,
                'sameSite': 'Lax'
            }])
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            page_title = page.title()
            log(f"📝 当前Title: {page_title}")
            if "auth/login" not in page.url:
                log(f"✅ Cookie 登录成功！当前已到达dashboard页面")
                return True
            log("❌ Cookie 失效，请更换")
        except:
            pass

    # 2. 账号密码登录
    if not EMAIL or not PASSWORD:
        return False
    log("💣 尝试账号密码登录...")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        page.fill('input[name="email"]', EMAIL)
        page.fill('input[name="password"]', PASSWORD)
        time.sleep(0.5)
        handle_cloudflare(page)
        page.click('button[type="submit"]')
        time.sleep(3)
        handle_cloudflare(page)
        page.wait_for_url(f"{BASE_URL}/*", timeout=30000)
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        page_title = page.title()
        log(f"📝 当前Title: {page_title}")
        if "auth/login" in page.url:
            log("❌ 登录失败。")
            return False
        log(f"✅ 账号密码登录成功！当前已到达dashboard页面")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        page.screenshot(path="login_fail.png")
        return False

def get_server_id(page):
    try:
        handle_cloudflare(page)
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
        handle_cloudflare(page)
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


def diagnose_turnstile(page, label=""):
    """诊断 Turnstile 初始化状态；不写入、不伪造 token。"""
    prefix = f" [{label}]" if label else ""
    result = {
        "iframe": -1,
        "response_count": 0,
        "token_lengths": [],
        "container_count": 0,
        "sitekey_count": 0,
        "api_loaded": False,
        "turnstile_type": "unknown",
    }

    try:
        result["iframe"] = page.locator(
            'iframe[src*="challenges.cloudflare.com"]'
        ).count()
    except Exception:
        pass

    try:
        loc = page.locator(
            'input[name="cf-turnstile-response"], '
            'textarea[name="cf-turnstile-response"]'
        )
        result["response_count"] = loc.count()
        for i in range(result["response_count"]):
            try:
                result["token_lengths"].append(
                    len(loc.nth(i).input_value(timeout=500) or "")
                )
            except Exception:
                result["token_lengths"].append(-1)
    except Exception:
        pass

    try:
        result["container_count"] = page.locator(
            ".cf-turnstile, [class*='cf-turnstile'], "
            "[data-sitekey]"
        ).count()
    except Exception:
        pass

    try:
        result["sitekey_count"] = page.locator("[data-sitekey]").count()
    except Exception:
        pass

    try:
        result["api_loaded"] = bool(page.evaluate(
            """() => !!(
                window.turnstile &&
                typeof window.turnstile === 'object'
            )"""
        ))
        result["turnstile_type"] = str(page.evaluate(
            """() => window.turnstile
                ? typeof window.turnstile
                : 'undefined'"""
        ))
    except Exception:
        pass

    log(
        f"🧪 Turnstile诊断{prefix}: "
        f"iframe={result['iframe']}, "
        f"response字段={result['response_count']}, "
        f"token长度={result['token_lengths']}, "
        f"容器={result['container_count']}, "
        f"sitekey={result['sitekey_count']}, "
        f"api={result['api_loaded']}({result['turnstile_type']})"
    )

    return result



def inspect_turnstile_render_state(page, label=""):
    """检查 Turnstile 容器是否真正进入渲染状态；不主动调用 render/execute。"""
    prefix = f" [{label}]" if label else ""
    try:
        data = page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll(
                    '.cf-turnstile, [data-sitekey]'
                ));
                return nodes.map((el, i) => ({
                    index: i,
                    tag: el.tagName,
                    id: el.id || '',
                    className: el.className || '',
                    sitekey: el.getAttribute('data-sitekey') || '',
                    callback: el.getAttribute('data-callback') || '',
                    errorCallback: el.getAttribute('data-error-callback') || '',
                    expiredCallback: el.getAttribute('data-expired-callback') || '',
                    action: el.getAttribute('data-action') || '',
                    mode: el.getAttribute('data-mode') || '',
                    size: el.getAttribute('data-size') || '',
                    theme: el.getAttribute('data-theme') || '',
                    childCount: el.children.length,
                    innerLength: el.innerHTML.length,
                    rect: (() => {
                        const r = el.getBoundingClientRect();
                        return {
                            x: Math.round(r.x),
                            y: Math.round(r.y),
                            width: Math.round(r.width),
                            height: Math.round(r.height)
                        };
                    })()
                }));
            }"""
        )
        log(f"🧩 Turnstile容器状态{prefix}: {json.dumps(data, ensure_ascii=False)}")
        return data
    except Exception as e:
        log(f"⚠️ 读取 Turnstile 容器状态失败{prefix}: {e}")
        return []


def install_page_diagnostics(page):
    """安装浏览器级诊断监听，重点观察 Turnstile 是否报错或加载失败。"""
    def on_console(msg):
        try:
            txt = msg.text or ""
            low = txt.lower()
            if (
                "turnstile" in low
                or "cloudflare" in low
                or "challenge" in low
                or msg.type in ("error", "warning")
            ):
                log(f"🖥️ 浏览器 {msg.type}: {txt[:1000]}")
        except Exception:
            pass

    def on_page_error(exc):
        try:
            log(f"💥 页面 JS 错误: {exc}")
        except Exception:
            pass

    def on_request_failed(request):
        try:
            url = request.url
            low = url.lower()
            if (
                "cloudflare" in low
                or "turnstile" in low
                or "challenge" in low
            ):
                log(f"❌ Turnstile/Cloudflare 请求失败: {url}")
        except Exception:
            pass

    def on_response(response):
        try:
            url = response.url
            low = url.lower()
            if (
                "cloudflare" in low
                or "turnstile" in low
                or "challenge" in low
            ):
                if response.status >= 400:
                    log(
                        f"❌ Turnstile/Cloudflare HTTP异常: "
                        f"{response.status} {url}"
                    )
                elif "api.js" in low:
                    log(
                        f"📦 Turnstile api.js 响应: "
                        f"{response.status} {url}"
                    )
        except Exception:
            pass

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)

    return on_console, on_page_error, on_request_failed, on_response


def wait_for_turnstile_ready(page, max_wait=90):
    """
    等待网站自己的 Turnstile 正常渲染并产生 response。
    不主动调用 turnstile.render/execute，不写入 token。
    """
    log(f"⏳ 等待 Turnstile 正常渲染，最长 {max_wait} 秒...")

    start = time.time()
    last_state = None
    last_detail = 0

    while time.time() - start < max_wait:
        state = diagnose_turnstile(page, "渲染等待")
        detail = inspect_turnstile_render_state(page, "渲染等待")

        # 浏览器已经产生 response。
        if any(x > 0 for x in state["token_lengths"]):
            log("✅ 检测到非空 cf-turnstile-response")
            return True

        # 重点：如果容器存在、API存在，但没有 iframe/子节点，
        # 就继续观察 JS 是否真的在执行，而不是认为“验证很慢”。
        if state["api_loaded"] and state["container_count"] > 0:
            if time.time() - last_detail >= 10:
                log(
                    "🔄 Turnstile API + sitekey 容器均存在，"
                    "但尚未生成 iframe；继续等待并收集前端状态..."
                )
                last_detail = time.time()

        # 页面状态发生变化时立即记录。
        compact = (
            state["iframe"],
            state["response_count"],
            tuple(state["token_lengths"]),
            state["container_count"],
            state["sitekey_count"],
            state["api_loaded"],
            json.dumps(detail, ensure_ascii=False, sort_keys=True)
            if detail else ""
        )
        if compact != last_state:
            last_state = compact

        time.sleep(3)

    final = diagnose_turnstile(page, "渲染超时")
    inspect_turnstile_render_state(page, "渲染超时")

    if any(x > 0 for x in final["token_lengths"]):
        return True

    log("❌ Turnstile 未完成正常渲染，response 仍为空")
    return False


def renew_service(page, server_id=None):
    renew_request_seen = False
    renew_response_seen = False
    renew_response_status = None
    renew_response_location = None
    renew_request_data = None

    def on_request(request):
        nonlocal renew_request_seen, renew_request_data
        if request.url.endswith(f"/service/{server_id}/renew") if server_id else request.url.endswith("/renew"):
            renew_request_seen = True
            try:
                renew_request_data = request.post_data or ""
            except Exception:
                renew_request_data = ""
            log(f"📤 捕获续期 POST: {request.method} {request.url}")
            # 不打印 CSRF token 本身，只打印字段和值是否为空。
            try:
                fields = {}
                for part in (renew_request_data or "").split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k == "_token":
                            fields[k] = f"<len={len(v)}>"
                        elif k == "cf-turnstile-response":
                            fields[k] = f"<len={len(v)}>"
                        else:
                            fields[k] = v
                log(f"🧾 /renew 表单字段摘要: {fields}")
            except Exception:
                pass

    def on_response(response):
        nonlocal renew_response_seen, renew_response_status, renew_response_location
        if response.url.endswith(f"/service/{server_id}/renew") if server_id else response.url.endswith("/renew"):
            renew_response_seen = True
            renew_response_status = response.status
            try:
                renew_response_location = (
                    response.headers.get("location")
                    or response.headers.get("Location")
                )
            except Exception:
                renew_response_location = None

            log(f"📥 /renew 响应: HTTP {response.status}")
            if renew_response_location:
                log(f"📍 /renew 302 Location: {renew_response_location}")

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        log("➡ 进入续期流程...")
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)

        handle_cloudflare(page)
        page.wait_for_timeout(3000)

        page_text = page.locator("body").inner_text()
        if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
            log("⚠️ 未到续期时间，无法续期。")
            return "NOT_TIME"

        log("🖱️ 准备点击 'Renew' 按钮...")

        modal_opened = False

        # 网站较慢：不刷新页面；单页最多等待 60 秒。
        for i in range(10):
            try:
                handle_cloudflare(page)

                renew_btn = page.locator('button:has-text("Renew"):visible').first
                renew_btn.wait_for(state="visible", timeout=15000)
                renew_btn.scroll_into_view_if_needed()
                page.wait_for_timeout(500)

                log(f"🖱️ 第 {i + 1} 次尝试点击 'Renew'...")
                renew_btn.click(force=True)

                create_btn = page.locator('button:has-text("Create Invoice"):visible').first

                log("🖲️ 等待续费弹窗，最长 60 秒（不刷新页面）...")
                modal_start = time.time()

                while time.time() - modal_start < 60:
                    try:
                        if create_btn.is_visible(timeout=1000):
                            modal_opened = True
                            log("✅ 续费弹窗已成功弹出！")
                            break
                    except Exception:
                        pass

                    # 页面可能很慢，但只继续等待，不刷新。
                    time.sleep(1)

                if modal_opened:
                    break

                log("⚠️ 60 秒内弹窗未出现，检查 Renew 是否仍存在...")
                try:
                    if page.locator('button:has-text("Renew"):visible').count() > 0:
                        log("🔁 Renew 仍存在，下一次继续点击，不刷新页面")
                        continue
                except Exception:
                    pass

            except Exception as e:
                log(f"⚠️ 第 {i + 1} 次 Renew 操作异常: {e}")
                time.sleep(2)

        if not modal_opened:
            log("❌ 10 次尝试后，续费弹窗仍未出现。")
            page.screenshot(path="renew_modal_failed.png")

            bj_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            retry_time = bj_now + datetime.timedelta(minutes=10)
            log(
                f"⏰ 连续10次续费尝试失败，Cron 将在10分钟后重试："
                f"{retry_time.strftime('%Y-%m-%d %H:%M')}（北京时间）"
            )
            update_cronjob_schedule(retry_time)

            send_telegram_notification(
                "❌ 续期失败：连续10次尝试均未打开续费弹窗，已安排10分钟后重试",
                getattr(sys.modules[__name__], "_CURRENT_OLD_DUE", "未知"),
                getattr(sys.modules[__name__], "_CURRENT_OLD_DUE", "未知")
            )
            return "RETRY_10M"

        # 在提交前详细检查 Turnstile。
        before_ts = diagnose_turnstile(page, "Create Invoice前")

        # 如果已经有非空 token，直接进入正常点击。
        token_ready = any(x > 0 for x in before_ts["token_lengths"])

        # 如果没有 token，等待页面自己的 Turnstile 正常初始化。
        if not token_ready:
            inspect_turnstile_render_state(page, "首次初始化检查")
            token_ready = wait_for_turnstile_ready(page, max_wait=90)

        # 再检查一次，确保不是误判。
        final_ts = diagnose_turnstile(page, "Create Invoice点击前")
        token_ready = any(x > 0 for x in final_ts["token_lengths"])

        if not token_ready:
            log("❌ Create Invoice 前 Turnstile response 仍为空")
            log("❌ 为避免再次提交空 cf-turnstile-response，本次不提交续期表单")
            page.screenshot(path="turnstile_not_ready.png")
            try:
                with open("turnstile_not_ready.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                log("💾 已保存 Turnstile 异常页面: turnstile_not_ready.html")

                # 额外保存 Turnstile 容器的 outerHTML，便于直接比较网页实际结构。
                snippet = page.evaluate(
                    """() => Array.from(document.querySelectorAll(
                        '.cf-turnstile, [data-sitekey]'
                    )).map((e, i) => `<!-- TURNSTILE ${i} -->\\n${e.outerHTML}`)
                    .join('\\n')"""
                )
                with open("turnstile_container.html", "w", encoding="utf-8") as f:
                    f.write(snippet or "<!-- no turnstile container -->")
                log("💾 已保存 Turnstile 容器: turnstile_container.html")
            except Exception as e:
                log(f"⚠️ 保存 Turnstile 诊断文件失败: {e}")
            return False

        create_btn = page.locator('button:has-text("Create Invoice"):visible').first
        create_btn.wait_for(state="visible", timeout=15000)
        create_btn.scroll_into_view_if_needed()
        page.wait_for_timeout(1000)

        # 只进行一次正常浏览器点击；不直接构造/提交 form。
        log("🖱️ 点击 'Create Invoice'（Turnstile 已有 response，仅提交一次）...")
        create_btn.click(force=True)

        # 持续观察网络状态。
        log("⏳ 等待 /renew 网络请求及后续跳转，最长 150 秒...")
        start_wait = time.time()
        last_status = None
        last_url = page.url
        last_turnstile_log = 0

        while time.time() - start_wait < 150:
            current_url = page.url

            if current_url != last_url:
                log(f"🌐 页面 URL 变化: {last_url} -> {current_url}")
                last_url = current_url

            if renew_request_seen and not renew_response_seen and last_status != "request":
                log("📤 已发出 /renew 请求，等待服务器响应...")
                last_status = "request"

            if renew_response_seen and last_status != f"response:{renew_response_status}":
                log(f"📥 已收到 /renew 响应: HTTP {renew_response_status}")
                if renew_response_location:
                    log(f"📍 服务器要求跳转到: {renew_response_location}")
                last_status = f"response:{renew_response_status}"

            # 每 10 秒打印一次 Turnstile 状态，避免刷屏。
            if time.time() - last_turnstile_log >= 10:
                inspect_turnstile(page, "续期等待")
                last_turnstile_log = time.time()

            # 真正的 Invoice 成功条件：URL。
            if "/payment/invoice/" in current_url:
                log(f"🎉 已进入发票页面: {current_url}")
                new_invoice_url = current_url
                break

            # 如果服务器明确把请求重定向回登录页，直接失败。
            if "/auth/login" in current_url:
                log("❌ /renew 后被重定向到登录页，Cookie/会话可能失效。")
                page.screenshot(path="renew_redirect_login.png")
                return False

            time.sleep(1)
        else:
            new_invoice_url = None

        # 如果收到了 302 但浏览器最终仍停在 manage 页面，明确诊断。
        if not new_invoice_url:
            log("❌ 150 秒内未进入发票页面。")
            log(
                f"🔍 最终诊断: renew_request={renew_request_seen}, "
                f"renew_response={renew_response_seen}, "
                f"status={renew_response_status}, "
                f"location={renew_response_location or '无'}, "
                f"url={page.url}"
            )
            diagnose_turnstile(page, "续期失败最终状态")

            if renew_request_data and "cf-turnstile-response=" in renew_request_data:
                try:
                    ts_part = renew_request_data.split(
                        "cf-turnstile-response=", 1
                    )[1].split("&", 1)[0]
                    if not ts_part:
                        log("❌ 实际 POST 中 cf-turnstile-response 为空")
                    else:
                        log(f"ℹ️ 实际 POST 中 Turnstile 字段长度: {len(ts_part)}")
                except Exception:
                    pass

            # 输出安全的表单结构摘要，不输出 CSRF/token 内容。
            try:
                form = page.locator(f'form#renew-form-{server_id}').first
                if form.count():
                    log(
                        "🧾 renew-form: "
                        f"method={form.get_attribute('method')}, "
                        f"action={form.get_attribute('action')}"
                    )
                    for name in ["days", "cf-turnstile-response"]:
                        loc = form.locator(f'[name="{name}"]')
                        count = loc.count()
                        if count:
                            value_len = []
                            for j in range(count):
                                try:
                                    value_len.append(
                                        len(loc.nth(j).input_value(timeout=500) or "")
                                    )
                                except Exception:
                                    value_len.append(-1)
                            log(f"🧾 字段 {name}: count={count}, value长度={value_len}")
                        else:
                            log(f"🧾 字段 {name}: 不存在")
            except Exception as e:
                log(f"⚠️ 读取续期表单结构失败: {e}")

            page.screenshot(path="renew_stuck_invoice.png")
            try:
                with open("renew_stuck_invoice.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                log("💾 已保存失败页面 HTML: renew_stuck_invoice.html")
            except Exception:
                pass

            return False

        # 支付
        if page.url != new_invoice_url:
            page.goto(new_invoice_url, wait_until="domcontentloaded", timeout=60000)

        handle_cloudflare(page)
        page.wait_for_timeout(5000)

        log("🔎 查找 'Pay' 按钮...")
        pay_btn = page.locator(
            'a:has-text("Pay"):visible, button:has-text("Pay"):visible'
        ).first
        pay_btn.wait_for(state="visible", timeout=30000)
        pay_btn.scroll_into_view_if_needed()
        pay_btn.click(force=True)
        log("✅ 'Pay' 按钮已点击。")

        time.sleep(5)

        page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        try:
            page.screenshot(path="renew_error.png")
        except Exception:
            pass
        return False
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)


def main():
    # 检查必要环境变量
    if not COOKIE_VALUE and not (EMAIL and PASSWORD):
        log("❌ 缺少登录凭证")
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
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars']
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                proxy={"server": PROXY_SERVER} if IS_PROXY else None
            )
            page = context.new_page()
            page.add_init_script(STEALTH_JS)
            install_page_diagnostics(page)

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

            # 保存当前 Due Date，供连续10次失败时的TG通知使用
            global _CURRENT_OLD_DUE
            _CURRENT_OLD_DUE = old_due

            # 执行续费
            renew_result = renew_service(page, server_id)

            new_due = old_due
            if renew_result == "RETRY_10M":
                # renew_service() 已经完成：
                # 1. Cron 写回10分钟后
                # 2. Telegram 推送
                log("🔁 已安排10分钟后重试，本次任务正常结束")
                sys.exit(0)

            if renew_result == "NOT_TIME":
                log("⏳ 未到续期时间，目前无法续期")
                status = "⏳ 未到续期时间"
            elif renew_result is False:
                log("❌ 续费失败，脚本退出。")
                status = "❌ 续期失败"
            else:  # renew_result is True
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")
                status = "✅ 续期成功"

            # 发送 Telegram 通知
            send_telegram_notification(status, old_due, new_due)

            if renew_result == "NOT_TIME":
                if old_due != "未知":
                    schedule_next_run_before_due(old_due)
                else:
                    log("⚠️ 无法获取 Due Date，5分钟后重试")
                    bj_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
                    update_cronjob_schedule(bj_now + datetime.timedelta(minutes=5))
                sys.exit(0)
            elif renew_result is False:
                sys.exit(1)
            else:
                # 正常续期成功：第7天 08:00-08:59 随机执行
                bj_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
                target_date = (bj_now + datetime.timedelta(days=7)).date()
                random_hour = 8
                random_minute = random.randint(0, 59)
                next_run = datetime.datetime.combine(
                    target_date,
                    datetime.time(hour=random_hour, minute=random_minute)
                )
                log(
                    f"📅 续期成功，Cron 安排至 "
                    f"{next_run.strftime('%Y-%m-%d %H:%M')}（北京时间）"
                )
                update_cronjob_schedule(next_run)
                sys.exit(0)
        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if 'browser' in locals() and browser:
                browser.close()
                
if __name__ == "__main__":
    main()
