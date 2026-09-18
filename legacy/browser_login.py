import asyncio
import os
import re
import signal
import subprocess
import time
from urllib.parse import urlsplit

from .config import *
from .common import (
    async_playwright,
    mask_secret,
    port_open,
    require_playwright,
    reset_chrome_profile,
    resolve_chrome_exe,
)
from .captcha_ocr import (
    add_mimo_usage,
    format_ocr_usage,
    new_ocr_usage,
    recognize_captcha_with_mimo,
)


def terminate_chrome_process(proc: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """Terminate the Chrome process tree started by this module."""
    if proc is None:
        return

    def signal_process_tree(sig) -> None:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
        else:
            try:
                if sig == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
            except OSError:
                pass

    signal_process_tree(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass

    signal_process_tree(signal.SIGKILL)
    try:
        proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        pass

def launch_chrome(
    chrome_exe: str | None = None,
    debug_port: int = DEBUG_PORT,
    user_data_dir: str = USER_DATA_DIR,
    fresh_profile: bool = True,
    startup_timeout: float = CHROME_STARTUP_TIMEOUT_SECONDS,
):
    if fresh_profile:
        reset_chrome_profile(user_data_dir)
    if port_open(debug_port):
        print(f"[i] 端口 {debug_port} 已被占用, 直接连接现有 Chrome")
        if fresh_profile:
            print("[!] 端口仍被占用, 当前连接可能不是干净 profile")
        return None
    chrome_path = resolve_chrome_exe(chrome_exe)
    os.makedirs(user_data_dir, exist_ok=True)
    args = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-component-update",
        "--remote-debugging-address=127.0.0.1",
    ]
    if CHROME_HEADLESS:
        args.extend(
            [
                "--headless=new",
            ]
        )
    args.append("about:blank")
    print(f"[i] 启动 Chrome: {chrome_path}")
    print(f"[i] CDP 端口: {debug_port}, 用户目录: {user_data_dir}")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name == "posix",
    )
    try:
        checks = max(1, int(startup_timeout / 0.25))
        for _ in range(checks):
            if port_open(debug_port):
                time.sleep(0.5)
                return proc
            time.sleep(0.25)
        raise RuntimeError(f"Chrome 调试端口未就绪（等待 {startup_timeout:g} 秒）")
    except Exception:
        terminate_chrome_process(proc)
        raise


def load_ocr():
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        try:
            # ddddocr 2.x accepts an explicit charset here.  Passing integer
            # 0 configures an empty range in current releases and makes every
            # captcha classification return an empty string.
            ocr.set_ranges("0123456789")
        except Exception:
            pass
        return ocr
    except Exception as e:
        print(f"[!] ddddocr 加载失败({e}), 回退到手动输入验证码")
        return None


def normalize_captcha_code(raw_code: str) -> str:
    return "".join(char for char in raw_code.strip() if char.isascii() and char.isdigit())


async def recognize_captcha(
    page,
    captcha_el,
    ocr,
    attempts: int = 3,
    initial_image_bytes: bytes | None = None,
) -> str:
    """Wait for the image to decode and tolerate a transient blank render."""
    try:
        await page.wait_for_function(
            """() => {
                const image = document.getElementById('kaptchaImage');
                return Boolean(
                    image && image.complete && image.naturalWidth > 0 && image.naturalHeight > 0
                );
            }""",
            timeout=3000,
        )
    except Exception:
        print("[!] 验证码图片尚未完整加载")

    code = ""
    for attempt in range(max(1, attempts)):
        img_bytes = (
            initial_image_bytes
            if attempt == 0 and initial_image_bytes is not None
            else await captcha_el.screenshot()
        )
        raw_code = ocr.classification(img_bytes) or ""
        code = normalize_captcha_code(raw_code)
        if len(code) == 4:
            return code
        if attempt + 1 < attempts:
            await asyncio.sleep(0.5)
    return code


class IdmCaptchaGatewayError(RuntimeError):
    """IDM rejected the HTTP login transport before a business decision."""


class IdmCaptchaRejected(RuntimeError):
    """IDM accepted the HTTP request but rejected the submitted captcha."""


class IdmLoginRejected(RuntimeError):
    """IDM returned a normal business response but did not accept the login."""


def _is_trusted_swu_cookie_domain(domain: str) -> bool:
    normalized = (domain or "").lstrip(".").lower()
    return normalized == "swu.edu.cn" or normalized.endswith(".swu.edu.cn")


def _copy_browser_cookies_to_requests(session, cookies: list[dict]) -> None:
    """Preserve the browser cookie scope when seeding the HTTP login session."""
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if not _is_trusted_swu_cookie_domain(domain):
            continue
        rest = {}
        if cookie.get("httpOnly"):
            rest["HttpOnly"] = True
        same_site = cookie.get("sameSite")
        if same_site in {"Lax", "Strict", "None"}:
            rest["SameSite"] = same_site
        expires = cookie.get("expires")
        session.cookies.set(
            str(cookie.get("name") or ""),
            str(cookie.get("value") or ""),
            domain=domain,
            path=str(cookie.get("path") or "/"),
            secure=bool(cookie.get("secure")),
            expires=int(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
            rest=rest,
        )


def _copy_requests_cookies_to_browser(session) -> list[dict]:
    """Return authenticated cookies with their original host/path scope intact."""
    browser_cookies = []
    for cookie in session.cookies:
        domain = str(cookie.domain or "idm.swu.edu.cn")
        if not _is_trusted_swu_cookie_domain(domain):
            continue
        item = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
        }
        same_site = cookie.get_nonstandard_attr("SameSite")
        if same_site in {"Lax", "Strict", "None"}:
            item["sameSite"] = same_site
        if cookie.expires and cookie.expires > 0:
            item["expires"] = float(cookie.expires)
        browser_cookies.append(item)
    return browser_cookies


def _idm_compat_request_sync(
    cookies: list[dict],
    page_url: str,
    form_action: str,
    form_entries: list[list[str]],
    username: str,
    password: str,
    ocr,
    prefer_mimo: bool = False,
) -> dict:
    """Fetch the captcha and submit IDM outside the browser WAF runtime."""
    try:
        import requests
    except Exception as exc:
        raise IdmCaptchaGatewayError("IDM 兼容传输组件不可用") from exc

    action_url = urlsplit(form_action)
    if (
        action_url.scheme != "https"
        or action_url.hostname != "idm.swu.edu.cn"
        or action_url.path != "/am/UI/Login"
    ):
        raise IdmCaptchaGatewayError("IDM 登录表单地址不受信任")
    page_parts = urlsplit(page_url)
    if page_parts.scheme != "https" or page_parts.hostname != "idm.swu.edu.cn":
        raise IdmCaptchaGatewayError("IDM 登录页面地址不受信任")

    session = None
    try:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": IDM_HTTP_USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": page_url,
                "Origin": "https://idm.swu.edu.cn",
            }
        )
        # IDM sets host-only cookies for idm.swu.edu.cn.  Flattening them to
        # name/value pairs and later widening the domain to .swu.edu.cn leaves
        # the browser with two same-name cookies.  OAuth can then consume the
        # stale host-only value even though the HTTP login returned success.
        _copy_browser_cookies_to_requests(session, cookies)
        captcha_response = session.get(
            "https://idm.swu.edu.cn/am/validate.code",
            headers={"Accept": "image/*,*/*;q=0.8"},
            timeout=IDM_HTTP_TIMEOUT_SECONDS,
        )
        if captcha_response.status_code != 200:
            raise IdmCaptchaGatewayError(
                f"IDM 验证码图片接口异常: HTTP {captcha_response.status_code}"
            )
        content_type = captcha_response.headers.get("content-type", "").lower()
        if "image/" not in content_type or not captcha_response.content:
            raise IdmCaptchaGatewayError("IDM 验证码图片接口返回无效数据")

        image_bytes = captcha_response.content
        raw_code = ocr.classification(image_bytes) if ocr is not None else ""
        local_code = normalize_captcha_code(raw_code or "")
        selected_code = local_code
        source = "local"
        cloud = None
        if prefer_mimo or len(local_code) != 4:
            cloud = recognize_captcha_with_mimo(image_bytes)
            cloud_code = cloud.get("code")
            if cloud.get("status") == "success" and cloud_code:
                selected_code = cloud_code
                source = "mimo"

        result_meta = {
            "local_code": local_code,
            "selected_code": selected_code,
            "source": source,
            "cloud": cloud,
        }
        if len(selected_code) != 4:
            return {**result_meta, "login": "captcha_unreadable"}

        form_body = {name: value for name, value in form_entries}
        form_body.update(
            {
                "IDToken1": username,
                "IDToken2": password,
                "IDToken3": "",
                "validateCode": selected_code,
            }
        )
        login_response = session.post(
            form_action,
            data=form_body,
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            allow_redirects=False,
            timeout=IDM_HTTP_TIMEOUT_SECONDS,
        )
        auth_error = login_response.headers.get("X-AuthErrorCode")
        if auth_error != "0":
            if login_response.status_code != 200:
                raise IdmCaptchaGatewayError(
                    f"IDM 登录接口异常: HTTP {login_response.status_code}"
                )
            visible_text = re.sub(
                r"\s+",
                " ",
                re.sub(r"<[^>]+>", " ", login_response.text or ""),
            ).strip()
            if "动态口令验证失败" in visible_text or "验证码" in visible_text:
                return {**result_meta, "login": "captcha_rejected"}
            reason = "学校认证未接受校园账号或密码"
            for marker in (
                "用户名或密码错误",
                "账号或密码错误",
                "帐户将被锁定",
                "账户将被锁定",
            ):
                if marker in visible_text:
                    reason = marker
                    break
            return {**result_meta, "login": "rejected", "reason": reason}

        if login_response.status_code not in {302, 303, 307, 308}:
            raise IdmCaptchaGatewayError(
                f"IDM 登录接口异常: HTTP {login_response.status_code}"
            )
        browser_cookies = _copy_requests_cookies_to_browser(session)
        return {
            **result_meta,
            "login": "accepted",
            "cookies": browser_cookies,
        }
    except IdmCaptchaGatewayError:
        raise
    except Exception as exc:
        raise IdmCaptchaGatewayError("IDM 兼容传输请求失败") from exc
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


async def submit_idm_login_compat(
    page,
    ocr,
    username: str,
    password: str,
    ocr_usage: dict[str, int] | None = None,
    prefer_mimo: bool = False,
) -> str:
    """Fetch the live captcha and submit the dynamic form via plain HTTP."""
    form = await page.evaluate(
        """() => {
            const form = document.forms['Login'];
            if (!form || !form.IDToken1 || !form.IDToken2 || !form.validateCode) return null;
            return {
                action: new URL(form.action, location.href).href,
                entries: [...new FormData(form).entries()].map(
                    ([name, value]) => [name, String(value)]
                ),
            };
        }"""
    )
    if not form:
        raise IdmCaptchaGatewayError("IDM 登录表单不可用")
    cookies = await page.context.cookies("https://idm.swu.edu.cn")
    result = await asyncio.to_thread(
        _idm_compat_request_sync,
        cookies,
        page.url,
        form["action"],
        form["entries"],
        username,
        password,
        ocr,
        prefer_mimo,
    )
    if ocr_usage is not None:
        ocr_usage["local_attempts"] += 1
        cloud = result.get("cloud")
        if cloud is not None:
            add_mimo_usage(ocr_usage, cloud.get("usage") or {})
    local_code = result.get("local_code") or ""
    print(f"[OCR] 本地识别验证码: {local_code or '(无有效数字)'}")
    if result.get("cloud") is not None:
        cloud = result["cloud"]
        if cloud.get("status") == "success":
            relation = "一致" if cloud.get("code") == local_code else "不同"
            print(f"[OCR] MiMo 与本地结果{relation}，本轮使用 MiMo 结果")
        else:
            print(f"[OCR] MiMo 后备不可用: {cloud.get('status', 'unknown')}")
    if result.get("login") == "captcha_unreadable":
        raise IdmCaptchaRejected("验证码识别结果不是四位数字")
    if result.get("login") == "captcha_rejected":
        raise IdmCaptchaRejected("学校认证判定验证码错误")
    if result.get("login") == "rejected":
        raise IdmLoginRejected(result.get("reason") or "学校认证未接受校园账号或密码")
    browser_cookies = result.get("cookies") or []
    if browser_cookies:
        await page.context.add_cookies(browser_cookies)
    return "accepted"


async def validate_captcha_code(page, code: str) -> str:
    response_info = None
    try:
        async with page.expect_response(
            lambda response: "/am/validatecode/verify.do" in response.url,
            timeout=5000,
        ) as response_info:
            await page.fill("#validateCode", "")
            await page.type("#validateCode", code, delay=50)
        response = await response_info.value
        if response.status != 200:
            raise IdmCaptchaGatewayError(
                f"IDM 验证码校验接口异常: HTTP {response.status}"
            )
    except IdmCaptchaGatewayError:
        raise
    except Exception as exc:
        if response_info is not None:
            raise IdmCaptchaGatewayError(
                "IDM 验证码校验接口异常: 未收到响应"
            ) from exc
        # Lightweight page doubles used outside Playwright do not expose
        # expect_response; preserve the DOM-marker fallback for those callers.
        await page.fill("#validateCode", "")
        await page.type("#validateCode", code, delay=50)
    await asyncio.sleep(1.5)
    tishi_src = await page.evaluate(
        "() => document.getElementById('tishi')?.getAttribute('src') || ''"
    )
    return "rejected" if "code_error" in tishi_src else "accepted"


async def reload_idm_login_page(page) -> bool:
    """Reload the login page because IDM's image-click refresh can leave a broken image."""
    try:
        await page.reload(wait_until="commit", timeout=30000)
        await page.wait_for_selector("#loginName", timeout=15000)
        await asyncio.sleep(0.5)
        return True
    except Exception:
        print("[!] 重新加载 IDM 验证码失败")
        return False


async def recover_idm_login_page(page) -> bool:
    """Re-enter the dynamic SSO flow when IDM returns a page without its form."""
    if "idm.swu.edu.cn/am/UI/Login" in page.url:
        if await reload_idm_login_page(page):
            return True
    return await bootstrap_idm_login(page)


async def detect_login_failure(page) -> str | None:
    try:
        text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return None
    if not text:
        return None
    if not any(keyword in text for keyword in LOGIN_FAILURE_KEYWORDS):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matched = [line for line in lines if any(keyword in line for keyword in LOGIN_FAILURE_KEYWORDS)]
    return " ".join(matched[:3])[:300]


async def login_once(
    page,
    ocr,
    username: str,
    password: str,
    ocr_usage: dict[str, int] | None = None,
    prefer_mimo: bool = False,
) -> bool:
    if IDM_HTTP_LOGIN_ENABLED:
        await submit_idm_login_compat(
            page,
            ocr,
            username,
            password,
            ocr_usage,
            prefer_mimo,
        )
        print("[*] IDM 已接受认证表单，正在完成登录...")
        return True

    await page.wait_for_selector("#loginName", timeout=15000)
    await page.fill("#loginName", "")
    await page.fill("#loginName", username)
    await page.fill("#password", "")
    await page.fill("#password", password)

    captcha_el = await page.query_selector("#kaptchaImage")
    if not captcha_el:
        print("[!] 找不到验证码图片")
        return False

    img_bytes = await captcha_el.screenshot()
    if ocr:
        if ocr_usage is not None:
            ocr_usage["local_attempts"] += 1
        code = await recognize_captcha(
            page,
            captcha_el,
            ocr,
            initial_image_bytes=img_bytes,
        )
        print(f"[OCR] 本地识别验证码: {code or '(无有效数字)'}")
    else:
        os.makedirs(os.path.dirname(CAPTCHA_FILE) or ".", exist_ok=True)
        with open(CAPTCHA_FILE, "wb") as f:
            f.write(img_bytes)
        code = input(f"请查看 {CAPTCHA_FILE} 并输入验证码: ").strip()

    if len(code) == 4:
        validation = await validate_captcha_code(page, code)
        if validation == "accepted":
            await page.click("#button")
        if validation == "accepted":
            print("[*] 验证码正确, 已提交登录...")
            return True

    if len(code) != 4:
        print(f"[!] 验证码长度异常 (got {len(code)}), 刷新重试")
    else:
        print("[!] 验证码校验失败, 刷新重试")

    if (
        ocr is None
        or ocr_usage is None
        or ocr_usage["mimo_calls"] >= MIMO_OCR_MAX_CALLS_PER_LOGIN
    ):
        return False

    print("[OCR] 本地结果未通过学校校验，调用 MiMo 2.5 后备")
    cloud = await asyncio.to_thread(recognize_captcha_with_mimo, img_bytes)
    add_mimo_usage(ocr_usage, cloud.get("usage") or {})
    cloud_code = cloud.get("code")
    if cloud.get("status") != "success" or not cloud_code:
        print(f"[OCR] MiMo 后备不可用: {cloud.get('status', 'unknown')}")
        return False
    if cloud_code == code:
        print("[OCR] MiMo 与本地结果一致，当前验证码仍被学校拒绝")
        return False
    print("[OCR] MiMo 返回不同的四位结果，进行一次学校侧校验")
    cloud_validation = await validate_captcha_code(page, cloud_code)
    if cloud_validation == "accepted":
        await page.click("#button")
    if cloud_validation != "accepted":
        print("[!] MiMo 验证码结果也未通过学校校验")
        return False
    print("[*] MiMo 后备验证码正确, 已提交登录...")
    return True


async def login_idm(
    page,
    ocr,
    username: str,
    password: str,
    ocr_usage: dict[str, int] | None = None,
) -> bool:
    """在动态生成的 IDM 页面完成用户名、密码和四位图片验证码登录。"""
    gateway_failures = 0
    prefer_mimo = False
    for attempt in range(1, MAX_CAPTCHA_RETRY + 1):
        print(f"\n--- IDM 登录尝试 #{attempt} ---")
        failure = await detect_login_failure(page)
        if failure:
            print(f"[!] 登录失败: {failure}")
            return False

        try:
            ok = await login_once(
                page,
                ocr,
                username,
                password,
                ocr_usage,
                prefer_mimo,
            )
        except IdmCaptchaGatewayError as exc:
            print(f"[!] {exc}")
            gateway_failures += 1
            if gateway_failures > MAX_IDM_GATEWAY_RETRY:
                return False
            print(
                "[i] 学校认证网关临时拒绝请求，"
                f"重新生成动态认证 state 后重试 ({gateway_failures}/{MAX_IDM_GATEWAY_RETRY})"
            )
            if not await bootstrap_idm_login(page):
                print("[!] 无法重新生成 IDM 动态认证会话")
                return False
            continue
        except IdmCaptchaRejected as exc:
            print(f"[!] {exc}")
            prefer_mimo = bool(
                ocr is not None
                and ocr_usage is not None
                and ocr_usage["mimo_calls"] < MIMO_OCR_MAX_CALLS_PER_LOGIN
            )
            if prefer_mimo:
                print("[OCR] 下一组全新认证会话启用 MiMo 2.5 后备")
            if not await bootstrap_idm_login(page):
                print("[!] 无法重新生成 IDM 动态认证会话")
                return False
            continue
        except IdmLoginRejected as exc:
            print(f"[!] {exc}")
            return False
        except Exception:
            failure = await detect_login_failure(page)
            if failure:
                print(f"[!] 登录失败: {failure}")
                return False
            print("[!] IDM 登录页加载异常，重新获取认证页面")
            await recover_idm_login_page(page)
            continue
        if not ok:
            if not await recover_idm_login_page(page):
                print("[!] 无法恢复 IDM 登录页")
                return False
            continue

        # The HTTP compatibility path has already received X-AuthErrorCode=0
        # and copied the authenticated IDM cookies into Chromium.  Do not
        # follow IDM's opaque Location header: re-entering INIT_URL in
        # exchange_of_token() lets the normal dynamic SSO chain consume the
        # cookies without trusting or replaying that response target.
        if IDM_HTTP_LOGIN_ENABLED:
            print("[OK] IDM 已接受登录，正在完成 SSO 回调")
            return True

        for _ in range(60):
            await asyncio.sleep(0.5)
            if "idm.swu.edu.cn/am/UI/Login" not in page.url:
                print("[OK] IDM 已接受登录，正在完成 SSO 回调")
                return True
            failure = await detect_login_failure(page)
            if failure:
                print(f"[!] 登录失败: {failure}")
                return False
            try:
                tishi_src = await page.evaluate(
                    "() => document.getElementById('tishi')?.getAttribute('src') || ''"
                )
            except Exception:
                tishi_src = ""
            if "code_error" in tishi_src:
                print("[!] 验证码校验失败, 刷新重试")
                if not await recover_idm_login_page(page):
                    print("[!] 无法恢复 IDM 登录页")
                    return False
                break
        else:
            print("[!] IDM 登录提交后未跳转, 刷新验证码重试")
            if not await recover_idm_login_page(page):
                print("[!] 无法恢复 IDM 登录页")
                return False
    return False


async def bootstrap_idm_login(page) -> bool:
    """从稳定入口动态生成一次性 state，并进入 IDM 登录页。"""
    print("[1] 通过一网通办入口生成动态认证 state...")
    try:
        # A rejected captcha can leave IDM/UAAAP cookies tied to an expired
        # one-time state. Each bootstrap must start a fresh authentication
        # transaction inside this isolated browser profile.
        await page.context.clear_cookies()
        await page.goto(IDM_BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_url(
            "**/uaaap.swu.edu.cn/cas/login**",
            wait_until="commit",
            timeout=30000,
        )
    except Exception as exc:
        print(f"[!] 未到达 UAAAP 认证选择页: {exc}")
        return False

    print("[2] 触发 UAAAP 联邦认证 -> IDM...")
    try:
        has_fn = await page.evaluate("() => typeof _goLogin === 'function'")
        if not has_fn:
            print("[!] UAAAP 页面缺少联邦认证入口")
            return False
        await page.evaluate("_goLogin()")
        # IDM may keep a subresource pending for a long time. Reaching the
        # one-time login URL plus its form is sufficient; waiting for the
        # default full-load event can falsely time out after navigation.
        await page.wait_for_url(
            "**/idm.swu.edu.cn/am/UI/Login**",
            wait_until="commit",
            timeout=60000,
        )
        await page.wait_for_selector("#loginName", timeout=15000)
        print("[3] 已到达 IDM 四位验证码登录页")
        return True
    except Exception as exc:
        print(f"[!] 等待 IDM 页超时: {exc}")
        return False


async def exchange_of_token(page, token: dict) -> str | None:
    """复用已经建立的 UAAAP/IDM SSO 会话换取 of.swu Token。"""
    print("[4] IDM 登录成功，等待一网通办 SSO 回调...")
    if not IDM_HTTP_LOGIN_ENABLED:
        for _ in range(120):
            if "idm.swu.edu.cn" not in page.url and "uaaap.swu.edu.cn" not in page.url:
                break
            await asyncio.sleep(0.5)

    print("[5] 使用 SSO 会话进入 of.swu 并换取 Token...")
    try:
        if IDM_HTTP_LOGIN_ENABLED:
            # Match the upstream http_login.goto_sso() success path.  The
            # requests POST only authenticates IDM; after copying its cookies
            # Chromium must re-enter UAAAP and explicitly trigger _goLogin()
            # so the federation callback and exchange-token request occur.
            try:
                await page.goto(INIT_URL, wait_until="commit", timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(1.5)
            try:
                await page.goto(FEDERAL_URL, wait_until="commit", timeout=30000)
            except Exception:
                pass
            try:
                await page.wait_for_url(
                    "**/uaaap.swu.edu.cn/cas/login**",
                    wait_until="commit",
                    timeout=30000,
                )
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=30000
                )
            except Exception:
                pass
            await asyncio.sleep(2)
            if "uaaap.swu.edu.cn/cas/login" in page.url:
                has_fn = await page.evaluate("() => typeof _goLogin === 'function'")
                if has_fn:
                    await page.evaluate("_goLogin()")
                else:
                    current = page.url
                    separator = "&" if "?" in current else "?"
                    await page.goto(
                        f"{current}{separator}federalEnable=true",
                        wait_until="commit",
                        timeout=30000,
                    )
        else:
            await page.goto(INIT_URL, wait_until="domcontentloaded", timeout=60000)
            federal_link = page.locator("a[href*='SWU_CAS2_FEDERAL']").first
            if await federal_link.count():
                await federal_link.click(force=True)
            else:
                await page.goto(FEDERAL_URL, wait_until="commit", timeout=30000)
    except Exception as exc:
        print(f"[!] 进入 of.swu 联邦认证失败: {exc}")

    for _ in range(120):
        if token["value"]:
            return token["value"]
        await asyncio.sleep(0.5)
    current = urlsplit(page.url)
    safe_location = f"{current.scheme}://{current.netloc}{current.path}"
    if current.hostname == "idm.swu.edu.cn" and current.path == "/am/oauth2/authorize":
        print(f"[!] IDM OAuth 授权回调未完成，未捕获 Token: {safe_location}")
    else:
        print(f"[!] SSO 已完成但未捕获 Token，当前页面: {safe_location}")
    return token["value"]


async def do_login(
    username: str,
    password: str,
    chrome_exe: str | None = None,
    debug_port: int = DEBUG_PORT,
    user_data_dir: str = USER_DATA_DIR,
    fresh_profile: bool = True,
) -> str | None:
    """执行完整登录流程, 返回 fighter-auth-token."""
    require_playwright()
    ocr = load_ocr()
    ocr_usage = new_ocr_usage()
    chrome_proc = None
    try:
        chrome_proc = launch_chrome(chrome_exe, debug_port, user_data_dir, fresh_profile)
        try:
            return await asyncio.wait_for(
                _do_login_with_chrome(username, password, debug_port, ocr, ocr_usage),
                timeout=LOGIN_FLOW_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            print(f"[!] 校园登录流程超时（等待 {LOGIN_FLOW_TIMEOUT_SECONDS:g} 秒）")
            return None
    finally:
        terminate_chrome_process(chrome_proc)
        print(format_ocr_usage(ocr_usage))


async def _do_login_with_chrome(
    username: str,
    password: str,
    debug_port: int,
    ocr,
    ocr_usage: dict[str, int],
) -> str | None:
    token = {"value": None}

    async with async_playwright() as p:
        print(f"[i] 连接 CDP http://localhost:{debug_port}")
        browser = await p.chromium.connect_over_cdp(f"http://localhost:{debug_port}")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        async def on_response(resp):
            if "exchange-token" in resp.url and resp.status == 200:
                h = resp.headers.get("fighter-auth-token")
                if h and not token["value"]:
                    token["value"] = h
                    print(f"[OK] 响应头捕获 Token: {mask_secret(h)}")
                    return
                try:
                    body = await resp.json()
                    if body.get("code") == 200 and body.get("data") and not token["value"]:
                        token["value"] = body["data"]
                        print(f"[OK] 响应体捕获 Token: {mask_secret(body['data'])}")
                except Exception:
                    pass

        page.on("response", on_response)

        if not await bootstrap_idm_login(page):
            return None
        await asyncio.sleep(1)
        if not await login_idm(page, ocr, username, password, ocr_usage):
            return None
        return await exchange_of_token(page, token)

    return token["value"]


async def enter_uaaap_if_qr_login(page, fallback_url: str = BAIDAFORM_FEDERAL_URL) -> bool:
    """of.swu CAS 扫码页出现时, 自动进入 uaaap 联邦认证页."""
    try:
        url = page.url
    except Exception:
        return False
    if "of.swu.edu.cn/cas/login" not in url:
        return False

    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        body_text = ""
    if "请在指定应用内扫码" not in body_text and "统一身份认证平台" not in body_text:
        return False

    print("[i] 检测到 of.swu 扫码页, 自动进入 uaaap 校内认证...")
    selectors = [
        "#federal",
        "#federal a",
        "a[href*='SWU_CAS2_FEDERAL']",
        "a[href*='uaaap.swu.edu.cn']",
        "img.login-img",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.click(timeout=3000, force=True)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            await asyncio.sleep(1)
            if "uaaap.swu.edu.cn" in page.url or "idm.swu.edu.cn" in page.url:
                print(f"[i] 已进入认证页: {page.url}")
                return True
        except Exception:
            continue

    print("[i] 页面点击入口失败, 使用联邦认证 URL 直接跳转")
    try:
        await page.goto(fallback_url, wait_until="commit", timeout=30000)
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(1)
    return "uaaap.swu.edu.cn" in page.url or "idm.swu.edu.cn" in page.url


async def select_uaaap_unified_login(page) -> bool:
    """在 uaaap 登录方式选择页点击“统一认证登录”."""
    try:
        url = page.url
    except Exception:
        return False
    if "uaaap.swu.edu.cn/cas/login" not in url:
        return False

    print("[i] 检测到 uaaap 登录方式选择页, 选择统一认证登录...")
    try:
        has_fn = await page.evaluate("() => typeof _goLogin === 'function'")
    except Exception:
        has_fn = False
    if has_fn:
        try:
            await page.evaluate("_goLogin()")
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await asyncio.sleep(1)
            return True
        except Exception:
            pass

    selectors = [
        "text=统一认证登录",
        "img[src*='unified_button']",
        "div[onclick*='_goLogin']",
        ".loginMethd",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.click(timeout=3000, force=True)
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await asyncio.sleep(1)
            return True
        except Exception:
            continue
    print("[!] 未能自动点击统一认证登录")
    return False


async def auto_login_for_monitor(page, username: str, password: str, token_state: dict, max_seconds: int = 45) -> None:
    """监听模式内自动完成 uaaap -> IDM 登录, 不负责最终签到提交."""
    ocr = load_ocr()
    deadline = time.time() + max_seconds
    attempt = 0
    while time.time() < deadline and not token_state.get("value"):
        url = page.url
        if "of.swu.edu.cn/cas/login" in url:
            await enter_uaaap_if_qr_login(page)
        elif "uaaap.swu.edu.cn/cas/login" in url:
            await select_uaaap_unified_login(page)
        elif "idm.swu.edu.cn/am/UI/Login" in url:
            attempt += 1
            print(f"[i] IDM 自动登录尝试 #{attempt}")
            ok = await login_once(page, ocr, username, password)
            if not ok:
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(3)
        elif "of.swu.edu.cn" in url and "/cas/" not in url:
            return
        else:
            await asyncio.sleep(1)


async def auto_probe_baida_routes(page, max_wait_seconds: int = 120) -> None:
    """登录回到 of.swu 后, 自动访问几个 baidaForm 候选路由以触发任务接口."""
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        url = page.url
        if (
            "of.swu.edu.cn" in url
            and "/cas/" not in url
            and "uaaap.swu.edu.cn" not in url
            and "idm.swu.edu.cn" not in url
        ):
            print(f"[i] 检测到已回到 of.swu: {url}")
            break
        await asyncio.sleep(2)
    else:
        print("[!] 等待回到 of.swu 超时, 未自动探测 baidaForm 路由")
        return

    for url in BAIDAFORM_DISCOVERY_URLS:
        try:
            print(f"[i] 探测页面: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(8)
        except Exception as e:
            print(f"[!] 探测页面失败: {url} ({e})")
