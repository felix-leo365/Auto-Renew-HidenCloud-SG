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

def handle_cloudflare(page):
    iframe_selector = 'iframe[src*="challenges.cloudflare.com"]'
    if page.locator(iframe_selector).count() == 0:
        return True
    log("⚠️ 检测到 Cloudflare 验证...")
    start_time = time.time()
    while time.time() - start_time < 60:
        if page.locator(iframe_selector).count() == 0:
            log("✅ Cloudflare 验证通过！")
            return True
        try:
            frame = page.frame_locator(iframe_selector)
            checkbox = frame.locator('input[type="checkbox"]')
            if checkbox.is_visible():
                log("🖱️ 点击验证复选框...")
                time.sleep(random.uniform(0.5, 1.5))
                checkbox.click()
                log("⏳ 已点击，等待验证结果...")
                time.sleep(5)
            else:
                time.sleep(1)
        except Exception:
            pass
    log("❌ 验证超时。")
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

def renew_service(page, server_id=None):
    try:
        log("➡ 进入续期流程...")
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        page.wait_for_timeout(2000)

        # 检查是否有限制提示
        page_text = page.locator("body").inner_text()
        if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
            log("⚠️ 未到续期时间，无法续期。")
            return "NOT_TIME"

        log("🖱️ 准备点击 'Renew' 按钮...")

        # 仅使用真实浏览器点击，不使用 form.submit()
        renew_btn = page.locator('button:has-text("Renew")').first
        create_btn = page.locator('button:has-text("Create Invoice")').first

        modal_opened = False

        # HidenCloud 页面加载较慢：
        # - 不刷新当前页面
        # - Renew 点击后最多等待 60 秒
        # - 如果仍未弹出，再次点击，但继续使用当前页面
        for i in range(10):
            try:
                handle_cloudflare(page)

                renew_btn = page.locator('button:has-text("Renew")').first
                renew_btn.wait_for(state="visible", timeout=30000)
                renew_btn.scroll_into_view_if_needed()
                page.wait_for_timeout(1000)

                log(f"🖱️ 第 {i + 1} 次尝试点击 'Renew'...")
                renew_btn.click(force=True)

                log("🖲️ 等待续费弹窗（网站较慢，最多60秒，不刷新页面）...")

                modal_wait_start = time.time()
                create_btn = page.locator(
                    'button:has-text("Create Invoice"):visible'
                ).first

                while time.time() - modal_wait_start < 60:
                    # CF 出现时处理，但不因为暂时没有 CF 就判失败
                    if page.locator(
                        'iframe[src*="challenges.cloudflare.com"]'
                    ).count() > 0:
                        log("⚠️ Renew 弹窗加载期间检测到 Cloudflare，尝试处理...")
                        handle_cloudflare(page)

                    try:
                        if create_btn.count() > 0 and create_btn.is_visible():
                            modal_opened = True
                            log("✅ 续费弹窗已成功弹出！")
                            break
                    except Exception:
                        pass

                    # 检查限制提示
                    try:
                        current_text = page.locator(
                            "body"
                        ).inner_text(timeout=3000)

                        if (
                            "Renewal Restricted" in current_text
                            or "can only renew" in current_text.lower()
                        ):
                            log("⚠️ 未到续期时间，无法续期。")
                            return "NOT_TIME"
                    except Exception:
                        pass

                    time.sleep(1)

                if modal_opened:
                    break

                log("⚠️ 60秒内续费弹窗仍未出现，不刷新页面。")

                # 当前页面仍有 Renew 时继续点击
                try:
                    current_renew = page.locator(
                        'button:has-text("Renew"):visible'
                    ).first

                    if current_renew.count() > 0 and current_renew.is_visible():
                        log("🔄 Renew 按钮仍存在，继续点击...")
                        page.wait_for_timeout(2000)
                        continue
                except Exception:
                    pass

                log("⏳ Renew 暂时不可见，等待当前页面继续加载...")
                page.wait_for_timeout(5000)

            except Exception as e:
                log(f"❌ 第 {i + 1} 次点击 Renew 出错: {e}")
                if i < 9:
                    log("⏳ 网站较慢，不刷新页面，等待10秒后继续...")
                    page.wait_for_timeout(10000)

        if not modal_opened:
            log("❌ 错误：10次尝试后，续费弹窗仍未出现。")
            page.screenshot(path="renew_modal_failed.png")

            # 连续10次失败：10分钟后重新执行
            bj_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            retry_time = bj_now + datetime.timedelta(minutes=10)
            log(
                f"⏰ 连续10次续费尝试失败，Cron 将在10分钟后重试："
                f"{retry_time.strftime('%Y-%m-%d %H:%M')}（北京时间）"
            )
            update_cronjob_schedule(retry_time)

            # 立即推送 Telegram
            send_telegram_notification(
                "❌ 续期失败：连续10次尝试均未打开续费弹窗，已安排10分钟后重试",
                getattr(sys.modules[__name__], "_CURRENT_OLD_DUE", "未知"),
                getattr(sys.modules[__name__], "_CURRENT_OLD_DUE", "未知")
            )
            return "RETRY_10M"

        handle_cloudflare(page)

        # ============================================================
        # Create Invoice：慢网站模式 + 网络请求/响应监听
        # ============================================================
        log("🧾 准备建立 Invoice，开始监听网络请求...")

        invoice_candidates = []
        invoice_responses = []
        renew_request_seen = False
        renew_response_seen = False
        renew_response_status = None

        def on_request(request):
            nonlocal renew_request_seen
            try:
                url = request.url
                method = request.method

                if "/service/" in url and "/renew" in url:
                    renew_request_seen = True
                    log(f"🌐 RENEW REQUEST {method} {url}")

                    post_data = request.post_data or ""
                    compact = post_data[:2000].replace("\n", " ")
                    log(f"   📤 POST DATA: {compact}")

                    if "cf-turnstile-response=" in post_data:
                        m = re.search(
                            r'cf-turnstile-response=([^&]*)',
                            post_data
                        )
                        token = m.group(1) if m else ""
                        if token:
                            log(
                                f"🛡️ cf-turnstile-response: 已有Token "
                                f"(长度 {len(token)})"
                            )
                        else:
                            log(
                                "⚠️ cf-turnstile-response: 当前为空"
                            )

                elif re.search(
                    r"invoice|payment|renew|create|order",
                    url,
                    re.I
                ):
                    log(f"🌐 REQUEST {method} {url}")

            except Exception:
                pass

        def on_response(response):
            nonlocal renew_response_seen, renew_response_status

            try:
                url = response.url
                status = response.status

                if "/service/" in url and "/renew" in url:
                    renew_response_seen = True
                    renew_response_status = status
                    log(f"📥 RENEW RESPONSE {status} {url}")

                    if status in (301, 302, 303, 307, 308):
                        log(
                            f"🔀 /renew 返回 {status}，"
                            "继续等待浏览器导航，不立即判定失败..."
                        )

                elif re.search(
                    r"invoice|payment|renew|create|order",
                    url,
                    re.I
                ):
                    log(f"📥 RESPONSE {status} {url}")

                if "/payment/invoice/" in url:
                    if url not in invoice_candidates:
                        invoice_candidates.append(url)
                        log(f"🎯 发现 Invoice URL: {url}")

            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            log("🖱️ 点击 'Create Invoice'...")
            create_btn = page.locator(
                'button:has-text("Create Invoice"):visible'
            ).first
            create_btn.wait_for(state="visible", timeout=30000)
            create_btn.scroll_into_view_if_needed()
            page.wait_for_timeout(1000)

            before_invoice_url = page.url
            log(f"📍 点击前 URL: {before_invoice_url}")

            create_btn.click(force=True)

            # --------------------------------------------------------
            # 第一阶段：给前端 / Turnstile / JS 足够时间
            # --------------------------------------------------------
            log("⏳ Create Invoice 已点击，慢网站模式等待最多 90 秒...")

            phase1_start = time.time()

            while time.time() - phase1_start < 90:
                current_url = page.url

                # 已经导航到 Invoice
                if "/payment/invoice/" in current_url:
                    if current_url not in invoice_candidates:
                        invoice_candidates.append(current_url)
                    log(f"🎉 已进入 Invoice 页面: {current_url}")
                    break

                # CF
                if page.locator(
                    'iframe[src*="challenges.cloudflare.com"]'
                ).count() > 0:
                    log(
                        "⚠️ Create Invoice 期间检测到 Cloudflare，"
                        "尝试处理..."
                    )
                    handle_cloudflare(page)

                # /renew 已经发出
                if renew_request_seen:
                    log("📨 已捕获 /renew 请求，继续等待服务器响应/导航...")
                    # 不重复点击 Create Invoice

                # /renew 已响应
                if renew_response_seen:
                    log(
                        f"📥 已收到 /renew 响应："
                        f"{renew_response_status}，继续等待最终页面..."
                    )

                # 每 10 秒打印一次状态
                elapsed = int(time.time() - phase1_start)
                if elapsed > 0 and elapsed % 10 == 0:
                    log(
                        f"⏳ Invoice 等待中：{elapsed}/90秒，"
                        f"URL={page.url}"
                    )

                time.sleep(1)

            # --------------------------------------------------------
            # 第二阶段：如果90秒后仍未跳转，再给最多60秒
            # 但绝不重新点击 Create Invoice
            # --------------------------------------------------------
            if not invoice_candidates and "/payment/invoice/" not in page.url:
                log(
                    "⏳ 90秒后仍未进入 Invoice，"
                    "继续等待最多60秒，不重新点击 Create Invoice..."
                )

                phase2_start = time.time()

                while time.time() - phase2_start < 60:
                    current_url = page.url

                    if "/payment/invoice/" in current_url:
                        invoice_candidates.append(current_url)
                        log(
                            f"🎉 延长等待期间进入 Invoice: "
                            f"{current_url}"
                        )
                        break

                    if page.locator(
                        'iframe[src*="challenges.cloudflare.com"]'
                    ).count() > 0:
                        log("⚠️ 延长等待期间检测到 Cloudflare...")
                        handle_cloudflare(page)

                    elapsed = int(time.time() - phase2_start)
                    if elapsed > 0 and elapsed % 15 == 0:
                        log(
                            f"⏳ 延长等待：{elapsed}/60秒，"
                            f"URL={page.url}"
                        )

                    time.sleep(1)

            # --------------------------------------------------------
            # 最终确定 Invoice URL
            # --------------------------------------------------------
            new_invoice_url = None

            if "/payment/invoice/" in page.url:
                new_invoice_url = page.url

            elif invoice_candidates:
                for candidate in invoice_candidates:
                    if "/payment/invoice/" in candidate:
                        new_invoice_url = candidate
                        break

            if not new_invoice_url:
                log("❌ 最终仍未取得 Invoice URL。")
                log(f"📍 最终 URL: {page.url}")
                log(
                    f"📨 /renew 请求是否出现: {renew_request_seen}"
                )
                log(
                    f"📥 /renew 响应是否出现: {renew_response_seen}"
                )
                log(
                    f"📥 /renew 响应状态: {renew_response_status}"
                )

                page.screenshot(path="renew_stuck_invoice.png")

                try:
                    with open(
                        "renew_stuck_invoice.html",
                        "w",
                        encoding="utf-8"
                    ) as f:
                        f.write(page.content())
                    log("💾 已保存 renew_stuck_invoice.html")
                except Exception as save_error:
                    log(f"⚠️ 保存 HTML 失败: {save_error}")

                return False

            # --------------------------------------------------------
            # 打开 Invoice 页面
            # --------------------------------------------------------
            if page.url != new_invoice_url:
                log(f"➡️ 打开发票页面: {new_invoice_url}")
                page.goto(
                    new_invoice_url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

            log(f"🧾 当前 Invoice URL: {page.url}")

            # Invoice 页面给 CF 足够时间
            log("⏳ Invoice 页面加载中，等待 Cloudflare/页面稳定...")
            page.wait_for_timeout(5000)

            if page.locator(
                'iframe[src*="challenges.cloudflare.com"]'
            ).count() > 0:
                log("⚠️ Invoice 页面检测到 Cloudflare，尝试处理...")
                handle_cloudflare(page)

            page.wait_for_timeout(3000)

        finally:
            try:
                page.remove_listener("request", on_request)
                page.remove_listener("response", on_response)
            except Exception:
                pass

        # ============================================================
        # 支付
        # ============================================================
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

        if not new_invoice_url:
            log("❌ 未能进入发票页面，超时。")
            page.screenshot(path="renew_stuck_invoice.png")
            return False

        # 支付
        if page.url != new_invoice_url:
            page.goto(new_invoice_url, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)

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
        page.screenshot(path="renew_error.png")
        return False


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
            renew_result = renew_service(page)

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
