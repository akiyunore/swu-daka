import { FormEvent, useState } from "react";

import { apiPost } from "../services/api";
import { adminApi, adminPagePath } from "../services/adminRuntime";

type Session = { username: string; csrf_token: string; expires_at: string };

export default function AdminLoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    try {
      const session = await apiPost<Session>(adminApi("/auth/login"), { username, password });
      window.sessionStorage.setItem("admin_csrf", session.csrf_token);
      window.location.assign(`${adminPagePath()}/dashboard`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="admin-login-shell">
      <section className="form-card compact">
        <p className="eyebrow">受保护入口</p><h1>管理员登录</h1>
        <form onSubmit={submit}>
          <label>用户名<input required value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" /></label>
          <label>密码<input required type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
          <button className="button primary full" disabled={submitting}>{submitting ? "登录中..." : "登录"}</button>
        </form>
        {message ? <p className="notice error">{message}</p> : null}
      </section>
    </main>
  );
}
