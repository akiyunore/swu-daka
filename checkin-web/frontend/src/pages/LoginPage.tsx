import { FormEvent, useState } from "react";

import { apiPost } from "../services/api";

type Session = {
  csrf_token: string;
  expires_at: string;
  user: { id: number; username: string; display_name?: string | null };
};

function initialMode(): "login" | "register" {
  return new URLSearchParams(window.location.search).get("mode") === "register" ? "register" : "login";
}

export default function LoginPage() {
  const params = new URLSearchParams(window.location.search);
  const [mode, setMode] = useState<"login" | "register">(initialMode);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [invitationToken, setInvitationToken] = useState(params.get("token") ?? "");
  const [agreed, setAgreed] = useState(false);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  function switchMode(nextMode: "login" | "register") {
    setMode(nextMode);
    setMessage("");
    window.history.replaceState(null, "", nextMode === "register" ? "/?mode=register" : "/");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setMessage(mode === "register" ? "正在通过学校统一认证验证账号，可能需要一两分钟..." : "");
    try {
      const session = mode === "login"
        ? await apiPost<Session>("/api/user/auth/login", { username, password })
        : await apiPost<Session>("/api/user/auth/register", {
            invitation_token: invitationToken,
            school_username: username,
            school_password: password,
            display_name: displayName || undefined,
            email,
            has_agreed_terms: agreed,
          });
      window.sessionStorage.setItem("user_csrf", session.csrf_token);
      setPassword("");
      window.location.assign("/account");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "操作失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="public-shell">
      <section className="form-card login-card">
        <p className="eyebrow">SWU DAKA · 非官方打卡助手</p>
        <h1>统一登录</h1>
        <div className="mode-tabs" role="tablist" aria-label="登录方式">
          <button className={mode === "login" ? "active" : ""} type="button" onClick={() => switchMode("login")}>用户登录</button>
          <button className={mode === "register" ? "active" : ""} type="button" onClick={() => switchMode("register")}>使用 Token 注册</button>
        </div>
        <form onSubmit={submit}>
          {mode === "register" ? (
            <>
              <label>管理员提供的邀请 Token<input required minLength={16} value={invitationToken} onChange={(e) => setInvitationToken(e.target.value.trim())} autoComplete="off" /></label>
              <label>称呼（可选）<input value={displayName} onChange={(e) => setDisplayName(e.target.value)} autoComplete="name" /></label>
              <label>接收失败提醒的邮箱<input required type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" /></label>
            </>
          ) : null}
          <label>校园账号<input required value={username} onChange={(e) => setUsername(e.target.value.trim())} autoComplete="username" /></label>
          <label>校园密码<input required type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
          {mode === "register" ? (
            <>
              <label className="checkbox-row"><input type="checkbox" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />我已阅读并同意<a href="/terms" target="_blank" rel="noreferrer">用户须知</a></label>
            </>
          ) : null}
          <button className="button primary full" disabled={submitting || (mode === "register" && !agreed)}>{submitting ? "处理中..." : mode === "login" ? "登录" : "验证、注册并登录"}</button>
        </form>
        {message ? <p className="notice error">{message}</p> : null}
        <div className="login-footer">
          <a href="/terms">用户须知</a>
          <span className="source-credit">
            源项目：<a href="https://github.com/dan-cun/swu-daka" target="_blank" rel="noreferrer">dan-cun/swu-daka</a>
          </span>
        </div>
        <p className="fine-print">校园账号信息和邮箱地址会受到保护，管理员无法查看你的密码。邮箱验证后，仅在你开启自动打卡和失败提醒时用于发送通知。</p>
      </section>
    </main>
  );
}
