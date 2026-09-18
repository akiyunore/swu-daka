import { useEffect, useState } from "react";

import { ApiError, apiGet, apiPost } from "../services/api";

type User = {
  id: number;
  username: string;
  display_name?: string | null;
  active: number;
  has_credential: number;
  schedule_enabled: number;
  next_run_at?: string | null;
  last_run_at?: string | null;
  last_status?: string | null;
  last_detail?: string | null;
};
type Session = { csrf_token: string; expires_at: string; user: User };
type NotificationSettings = {
  email?: string | null;
  email_verified_at?: string | null;
  email_verified: boolean;
  notify_on_failure: boolean;
  schedule_enabled: boolean;
  verification_pending: boolean;
  verification_resend_available_at?: string | null;
  verification_resend_cooldown_seconds: number;
  email_service_available: boolean;
};

export default function UserDashboardPage() {
  const [session, setSession] = useState<Session | null>(null);
  const [notifications, setNotifications] = useState<NotificationSettings | null>(null);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [editingEmail, setEditingEmail] = useState(false);
  const [clock, setClock] = useState(() => Date.now());

  async function refresh() {
    const [sessionData, notificationData] = await Promise.all([
      apiGet<Session>("/api/user/auth/me"),
      apiGet<NotificationSettings>("/api/user/notifications"),
    ]);
    window.sessionStorage.setItem("user_csrf", sessionData.csrf_token);
    setSession(sessionData);
    setNotifications(notificationData);
    setEmail(notificationData.email || "");
  }

  useEffect(() => {
    refresh().catch((error: ApiError) => {
      if (error.status === 401) window.location.assign("/");
      else setMessage(error.message);
    });
  }, []);

  useEffect(() => {
    const availableAt = Date.parse(notifications?.verification_resend_available_at || "");
    if (!Number.isFinite(availableAt) || availableAt <= Date.now()) {
      setClock(Date.now());
      return;
    }
    setClock(Date.now());
    const timer = window.setInterval(() => {
      const now = Date.now();
      setClock(now);
      if (now >= availableAt) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [notifications?.verification_resend_available_at]);

  async function runAction(action: () => Promise<unknown>, successMessage: string): Promise<boolean> {
    setSaving(true);
    try {
      await action();
      await refresh();
      setMessage(successMessage);
      return true;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "更新失败");
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function toggleSchedule() {
    if (!session) return;
    const enabling = !Boolean(session.user.schedule_enabled);
    await runAction(
      () => apiPost("/api/user/schedule", { enabled: enabling }),
      enabling ? "自动打卡已开启。你现在可以选择开启失败邮件提醒。" : "自动打卡及失败邮件提醒均已关闭。",
    );
  }

  async function sendVerification() {
    const cooldown = notifications?.verification_resend_cooldown_seconds || 90;
    await runAction(
      () => apiPost("/api/user/notifications/email", { email }),
      `验证码已进入发送队列。邮件可能延迟 1–2 分钟，请等待 ${cooldown} 秒后再考虑重新发送，并检查垃圾邮件。`,
    );
  }

  async function verifyEmail() {
    const succeeded = await runAction(
      () => apiPost("/api/user/notifications/verify", { code }),
      "邮箱验证成功。",
    );
    if (succeeded) {
      setCode("");
      setEditingEmail(false);
    }
  }

  function beginEmailChange() {
    setEmail("");
    setCode("");
    setEditingEmail(true);
    setMessage("更换邮箱后需要重新验证；提交新邮箱时，原邮箱的验证状态和失败提醒会立即关闭。");
  }

  function cancelEmailChange() {
    setEmail(notifications?.email || "");
    setCode("");
    setEditingEmail(false);
    setMessage("");
  }

  async function toggleNotification() {
    if (!notifications) return;
    const enabling = !notifications.notify_on_failure;
    await runAction(
      () => apiPost("/api/user/notifications/settings", { notify_on_failure: enabling }),
      enabling ? "自动打卡失败邮件提醒已开启。" : "自动打卡失败邮件提醒已关闭。",
    );
  }

  async function logout() {
    await apiPost("/api/user/auth/logout");
    window.sessionStorage.removeItem("user_csrf");
    window.location.assign("/");
  }

  const user = session?.user;
  const resendAvailableAt = Date.parse(notifications?.verification_resend_available_at || "");
  const resendWaitSeconds = Number.isFinite(resendAvailableAt)
    ? Math.max(0, Math.ceil((resendAvailableAt - clock) / 1000))
    : 0;
  const showEmailEditor = !notifications?.email_verified || editingEmail;
  const resendLabel = resendWaitSeconds > 0
    ? `${resendWaitSeconds} 秒后可重新发送`
    : notifications?.verification_pending
      ? "重新发送验证码"
      : notifications?.email_verified
        ? "发送新邮箱验证码"
        : "发送验证码";
  return (
    <main className="account-shell">
      <section className="account-card">
        <header className="account-header"><div><p className="eyebrow">SWU DAKA</p><h1>{user?.display_name || "用户中心"}</h1></div><button className="text-button" onClick={logout}>退出登录</button></header>
        <p className="account-id">校园账号：{user?.username ?? "—"}</p>
        <div className={`schedule-panel ${user?.schedule_enabled ? "enabled" : ""}`}>
          <div><span>自动打卡</span><strong>{user?.schedule_enabled ? "已开启" : "已关闭"}</strong><p>{user?.schedule_enabled ? `下次计划：${user.next_run_at ? new Date(user.next_run_at).toLocaleString() : "等待调度"}` : "开启后将在默认时段内错峰执行"}</p></div>
          <button className={`button ${user?.schedule_enabled ? "secondary" : "primary"}`} disabled={!user || saving} onClick={toggleSchedule}>{saving ? "保存中..." : user?.schedule_enabled ? "关闭自动打卡" : "开启自动打卡"}</button>
        </div>

        <section className="notification-panel">
          <div className="section-heading"><div><h2>失败邮件提醒</h2><p>仅向已开启自动打卡的用户提供；成功不发信，23:00 仍未确认成功或可能漏跑时才提醒。</p></div><b className={`status ${notifications?.notify_on_failure ? "success" : ""}`}>{notifications?.notify_on_failure ? "已开启" : "未开启"}</b></div>
          {!notifications?.email_service_available ? <p className="notice">邮件服务尚未由管理员配置，邮箱会保留为未验证状态。</p> : null}
          {notifications?.email_verified && !editingEmail ? (
            <div className="verified-email-card">
              <div><span>当前邮箱</span><strong>{notifications.email}</strong><small>已验证，可用于接收失败提醒</small></div>
              <button className="button secondary" disabled={saving} onClick={beginEmailChange}>更换邮箱</button>
            </div>
          ) : null}
          {showEmailEditor ? (
            <>
              {notifications?.email_verified && editingEmail ? (
                <div className="email-change-heading">
                  <p>输入新邮箱并完成验证。提交后，原邮箱和已开启的失败提醒将立即停用。</p>
                  <button className="text-button" disabled={saving} onClick={cancelEmailChange}>取消更换</button>
                </div>
              ) : null}
              <div className="notification-form">
                <label>{editingEmail ? "新接收邮箱" : "接收邮箱"}<input type="email" value={email} placeholder="name@example.com" onChange={(event) => setEmail(event.target.value)} disabled={saving} /></label>
                <button className="button secondary" disabled={saving || !notifications?.email_service_available || !email || resendWaitSeconds > 0} onClick={sendVerification}>{resendLabel}</button>
              </div>
              {!notifications?.email_verified ? (
                <>
                  {notifications?.verification_pending ? (
                    <p className="resend-hint">邮件投递可能需要 1–2 分钟，请耐心等待并检查垃圾邮件。{resendWaitSeconds > 0 ? ` ${resendWaitSeconds} 秒后才能重新发送。` : " 仍未收到时可以重新发送。"}</p>
                  ) : null}
                  <div className="notification-form">
                    <label>六位验证码<input inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} disabled={saving} /></label>
                    <button className="button secondary" disabled={saving || code.length !== 6} onClick={verifyEmail}>确认邮箱</button>
                  </div>
                </>
              ) : null}
            </>
          ) : null}
          <div className="notification-status">
            <span>邮箱状态：{notifications?.email_verified ? `已验证（${notifications.email}）` : notifications?.email ? `待验证（${notifications.email}）` : "未填写"}</span>
            <button className={`button ${notifications?.notify_on_failure ? "secondary" : "primary"}`} disabled={saving || !notifications?.email_verified || !user?.schedule_enabled} onClick={toggleNotification}>{notifications?.notify_on_failure ? "关闭失败提醒" : "开启失败提醒"}</button>
          </div>
          <p className="fine-print">邮箱地址和必要的通知内容会交由第三方邮件服务处理。邮件可能延迟或投递失败，不能替代你在学校官方系统或钉钉端复核。</p>
        </section>

        <dl className="account-details">
          <div><dt>凭据状态</dt><dd>{user?.has_credential ? "已验证并加密保存" : "未绑定"}</dd></div>
          <div><dt>最近运行</dt><dd>{user?.last_status || "暂无记录"}</dd></div>
          <div><dt>运行说明</dt><dd>{user?.last_detail || "—"}</dd></div>
        </dl>
        {message ? <p className="notice">{message}</p> : null}
        <p className="fine-print">自动执行结果仍须在学校官方系统或钉钉端复核。本页不提供立即打卡，避免误触真实提交。</p>
      </section>
    </main>
  );
}
