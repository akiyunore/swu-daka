import html
import smtplib
import ssl
from email.message import EmailMessage

from core.config import Settings, get_settings


def _send_email(
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
    *,
    channel: str,
) -> None:
    settings: Settings = get_settings()
    if channel == "verification" and settings.verification_smtp_enabled:
        configured = settings.verification_email_configured
        host = settings.verification_smtp_host
        port = settings.verification_smtp_port
        use_ssl = settings.verification_smtp_ssl
        use_starttls = settings.verification_smtp_starttls
        username = settings.verification_smtp_username
        password = settings.verification_smtp_password
        from_address = settings.verification_smtp_from
    else:
        configured = settings.failure_email_configured
        host = settings.smtp_host
        port = settings.smtp_port
        use_ssl = settings.smtp_ssl
        use_starttls = settings.smtp_starttls
        username = settings.smtp_username
        password = settings.smtp_password
        from_address = settings.smtp_from
    if not settings.email_notifications_enabled or not configured:
        raise RuntimeError("邮件服务尚未配置")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"SWU Daka <{from_address}>"
    message["To"] = recipient
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    if use_ssl:
        client_factory = smtplib.SMTP_SSL
        client_args = (host, port)
        client_kwargs = {"context": context, "timeout": 20}
    else:
        client_factory = smtplib.SMTP
        client_args = (host, port)
        client_kwargs = {"timeout": 20}
    with client_factory(*client_args, **client_kwargs) as client:
        if use_starttls:
            client.starttls(context=context)
        if username and password:
            client.login(username, password)
        client.send_message(message)


def send_email(recipient: str, subject: str, text_body: str, html_body: str) -> None:
    """Send an automatic-check-in failure notification through the existing channel."""
    _send_email(recipient, subject, text_body, html_body, channel="failure")


def send_verification_email(
    recipient: str, subject: str, text_body: str, html_body: str
) -> None:
    """Send verification mail through Alibaba when enabled, otherwise the legacy channel."""
    _send_email(recipient, subject, text_body, html_body, channel="verification")


def safe_email_error(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP 认证失败"
    if isinstance(exc, (smtplib.SMTPConnectError, TimeoutError, ConnectionError, OSError)):
        return "SMTP 连接失败"
    if isinstance(exc, smtplib.SMTPException):
        return "SMTP 投递失败"
    return "邮件投递失败"


def verification_message(code: str, ttl_minutes: int) -> tuple[str, str, str]:
    subject = "[SWU Daka] 邮箱验证码"
    text = f"你的邮箱验证码是：{code}\n有效期 {ttl_minutes} 分钟。\n若非本人操作，请忽略本邮件。"
    body = (
        "<h1>SWU Daka 邮箱验证</h1>"
        f"<p>你的验证码是：<strong>{html.escape(code)}</strong></p>"
        f"<p>有效期 {ttl_minutes} 分钟。若非本人操作，请忽略本邮件。</p>"
    )
    return subject, text, body


def failure_message(payload: dict) -> tuple[str, str, str]:
    labels = {
        "failed": "自动任务执行失败",
        "timeout": "自动任务执行超时",
        "busy": "系统繁忙，自动任务未确认成功",
        "unconfirmed": "自动任务结果未确认",
        "running": "自动任务仍未确认完成",
        "missed": "自动任务可能未执行",
    }
    category = labels.get(payload.get("category"), "自动任务未确认成功")
    event_date = str(payload.get("date", ""))
    run_id = payload.get("run_id")
    run_text = f"运行编号：{run_id}" if run_id is not None else "运行编号：无"
    account_url = str(payload.get("account_url", ""))
    subject = "[SWU Daka] 自动打卡未确认成功，请及时复核"
    text = (
        f"日期：{event_date}\n状态：{category}\n{run_text}\n"
        f"用户中心：{account_url}\n请立即在学校官方系统或钉钉端确认，邮件本身不代表最终状态。"
    )
    body = (
        "<h1>自动打卡未确认成功</h1>"
        f"<p>日期：{html.escape(event_date)}</p>"
        f"<p>状态：{html.escape(category)}</p>"
        f"<p>{html.escape(run_text)}</p>"
        f"<p><a href=\"{html.escape(account_url, quote=True)}\">打开用户中心</a></p>"
        "<p><strong>请立即在学校官方系统或钉钉端确认，邮件本身不代表最终状态。</strong></p>"
    )
    return subject, text, body
