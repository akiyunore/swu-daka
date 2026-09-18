import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, apiGet, apiPost } from "../services/api";
import { adminApi, adminPagePath } from "../services/adminRuntime";

type Section = "users" | "tokens" | "api" | "runs" | "logs";
type Run = { id: number; user_id: number; display_name?: string | null; student_no?: string | null; trigger_source: string; status: string; detail: string; started_at: string; finished_at?: string | null };
type User = {
  id: number; display_name?: string | null; student_no?: string | null; school_username?: string | null;
  active: number; has_credential: number; login_enabled: number; schedule_enabled: number;
  next_run_at?: string | null; last_status?: string | null; invite_label?: string | null; admin_note?: string | null; created_at: string;
  notification_email?: string | null; notification_email_unreadable?: boolean; email_verified: number; notify_on_failure: number;
};
type Invite = {
  id: number; label?: string | null; status: "active" | "used" | "expired" | "revoked";
  expires_at: string; max_uses: number; use_count: number; revoked_at?: string | null; created_at: string;
  token_revealable: number; created_by: string; used_by_user_id?: number | null; used_by_display_name?: string | null; used_by_username?: string | null;
};
type Dashboard = { users: number; active_users: number; enabled_schedules: number; recent_runs: Run[] };
type Session = { username: string; csrf_token: string; expires_at: string };
type InviteCreated = { token: string; registration_path: string; expires_at: string; label?: string | null };
type CloudOcrSettings = { enabled: boolean; base_url: string; model: string; api_key_configured: boolean; updated_at?: string | null };
type CloudOcrCall = {
  id: number; user_id?: number | null; display_name?: string | null; student_no?: string | null; context: string;
  base_url_host: string; model: string; status: string; cloud_calls: number; prompt_tokens: number; image_tokens: number;
  completion_tokens: number; reasoning_tokens: number; total_tokens: number; latency_ms: number; created_at: string;
};
type SystemLog = { id: number; module: string; level: string; message: string; created_at: string };
type ThemeMode = "auto" | "light" | "dark";

const sections: { id: Section; label: string; description: string }[] = [
  { id: "users", label: "用户管理", description: "展开用户详情并管理备注、登录、计划与账号状态。" },
  { id: "tokens", label: "Token 创建与管理", description: "创建一次性注册 Token，并管理有效、已用或已撤销的记录。" },
  { id: "api", label: "API 调用管理", description: "通过完整 Base URL 直接调用兼容接口，并查看实际用量。" },
  { id: "runs", label: "最近运行", description: "查看自动计划与管理员触发的运行结果。" },
  { id: "logs", label: "系统日志", description: "查看脱敏后的任务、邮件队列与云端识别事件。" },
];
const inviteStatusText: Record<Invite["status"], string> = { active: "可使用", used: "已使用", expired: "已过期", revoked: "已撤销" };

function initialSection(): Section {
  const value = window.location.pathname.slice((adminPagePath() ?? "").length).split("/").filter(Boolean)[1] as Section | undefined;
  return sections.some((item) => item.id === value) ? value! : "users";
}

function initialTheme(): ThemeMode {
  const value = window.localStorage.getItem("swu-theme");
  return value === "light" || value === "dark" ? value : "auto";
}

export default function AdminDashboardPage() {
  const [section, setSection] = useState<Section>(initialSection);
  const [session, setSession] = useState<Session | null>(null);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [ocrCalls, setOcrCalls] = useState<CloudOcrCall[]>([]);
  const [logs, setLogs] = useState<SystemLog[]>([]);
  const [label, setLabel] = useState("");
  const [expiresInHours, setExpiresInHours] = useState(24);
  const [newToken, setNewToken] = useState("");
  const [message, setMessage] = useState("");
  const [expandedUserId, setExpandedUserId] = useState<number | null>(null);
  const [noteDrafts, setNoteDrafts] = useState<Record<number, string>>({});
  const [savingNoteId, setSavingNoteId] = useState<number | null>(null);
  const [revealedTokens, setRevealedTokens] = useState<Record<number, string>>({});
  const hoveredTokenCells = useRef(new Set<number>());
  const [cloudOcr, setCloudOcr] = useState<CloudOcrSettings | null>(null);
  const [cloudEnabled, setCloudEnabled] = useState(false);
  const [cloudBaseUrl, setCloudBaseUrl] = useState("https://api.xiaomimimo.com/v1/chat/completions");
  const [cloudModel, setCloudModel] = useState("mimo-v2.5");
  const [cloudApiKey, setCloudApiKey] = useState("");
  const [clearCloudApiKey, setClearCloudApiKey] = useState(false);
  const [savingCloud, setSavingCloud] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [theme, setTheme] = useState<ThemeMode>(initialTheme);
  const refreshInFlight = useRef(false);
  const refreshPending = useRef(false);
  const sectionRef = useRef(section);

  const refresh = useCallback(async (syncCloudForm = false, requestedSection?: Section) => {
    if (refreshInFlight.current) { refreshPending.current = true; return; }
    refreshInFlight.current = true;
    try {
      let target = requestedSection;
      do {
        refreshPending.current = false;
        const activeSection = target ?? sectionRef.current;
        target = undefined;
        if (activeSection === "users") {
          const [summary, userList] = await Promise.all([apiGet<Dashboard>(adminApi("/dashboard")), apiGet<User[]>(adminApi("/users"))]);
          setDashboard(summary); setUsers(userList);
        } else if (activeSection === "tokens") {
          setInvites(await apiGet<Invite[]>(adminApi("/invites")));
        } else if (activeSection === "api") {
          const [cloudSettings, callList] = await Promise.all([apiGet<CloudOcrSettings>(adminApi("/cloud-ocr")), apiGet<CloudOcrCall[]>(adminApi("/cloud-ocr/calls"))]);
          setCloudOcr(cloudSettings); setOcrCalls(callList);
          if (syncCloudForm) { setCloudEnabled(cloudSettings.enabled); setCloudBaseUrl(cloudSettings.base_url); setCloudModel(cloudSettings.model); }
        } else if (activeSection === "runs") {
          setRuns(await apiGet<Run[]>(adminApi("/runs")));
        } else {
          setLogs(await apiGet<SystemLog[]>(adminApi("/logs")));
        }
      } while (refreshPending.current);
    } finally { refreshInFlight.current = false; }
  }, []);

  useEffect(() => { sectionRef.current = section; }, [section]);

  useEffect(() => {
    apiGet<Session>(adminApi("/auth/me"))
      .then((data) => { setSession(data); window.sessionStorage.setItem("admin_csrf", data.csrf_token); })
      .catch((error: ApiError) => { if (error.status === 401) window.location.assign(adminPagePath()!); else setMessage(error.message); });
  }, [refresh]);

  useEffect(() => {
    if (!session) return;
    const events = new EventSource(adminApi("/events"));
    let debounceTimer = 0;
    const update = () => {
      if (document.visibilityState !== "visible") return;
      window.clearTimeout(debounceTimer);
      debounceTimer = window.setTimeout(() => refresh(false).catch(() => undefined), 500);
    };
    events.addEventListener("update", update);
    document.addEventListener("visibilitychange", update);
    const fallback = window.setInterval(update, 60_000);
    return () => { window.clearTimeout(debounceTimer); events.removeEventListener("update", update); events.close(); document.removeEventListener("visibilitychange", update); window.clearInterval(fallback); };
  }, [refresh, session]);

  useEffect(() => {
    if (session) refresh(section === "api", section).catch(() => undefined);
  }, [refresh, section, session]);

  useEffect(() => {
    if (theme === "auto") { document.documentElement.removeAttribute("data-theme"); window.localStorage.removeItem("swu-theme"); }
    else { document.documentElement.dataset.theme = theme; window.localStorage.setItem("swu-theme", theme); }
  }, [theme]);

  const total = section === "users" ? users.length : section === "tokens" ? invites.length : section === "api" ? ocrCalls.length : section === "runs" ? runs.length : logs.length;
  function changeSection(next: Section) { setSection(next); setExpandedUserId(null); setMobileNavOpen(false); setMessage(""); window.history.replaceState(null, "", `${adminPagePath()}/dashboard/${next}`); }

  async function createInvite() {
    try { const invite = await apiPost<InviteCreated>(adminApi("/invites"), { label: label || undefined, expires_in_hours: expiresInHours }); setNewToken(invite.token); setLabel(""); setMessage("邀请 Token 已创建，请安全发送给用户。"); await refresh(false); }
    catch (error) { setMessage(error instanceof Error ? error.message : "创建邀请失败"); }
  }
  async function toggleActive(user: User) { try { await apiPost(adminApi(`/users/${user.id}/active`), { enabled: !Boolean(user.active) }); await refresh(false); } catch (error) { setMessage(error instanceof Error ? error.message : "更新失败"); } }
  async function toggleSchedule(user: User) { try { await apiPost(adminApi(`/users/${user.id}/schedule`), { enabled: !Boolean(user.schedule_enabled) }); await refresh(false); } catch (error) { setMessage(error instanceof Error ? error.message : "更新失败"); } }
  async function resetLogin(user: User) {
    const name = user.display_name || user.student_no || `用户 ${user.id}`;
    if (!window.confirm(`确定重置 ${name} 的网页登录校验吗？现有会话会立即失效。`)) return;
    try { await apiPost(adminApi(`/users/${user.id}/reset-login`)); setMessage("用户登录已重置，请创建新 Token 供其重新激活。"); await refresh(false); } catch (error) { setMessage(error instanceof Error ? error.message : "重置失败"); }
  }
  async function saveUserNote(user: User) {
    setSavingNoteId(user.id);
    try { const note = noteDrafts[user.id] ?? user.admin_note ?? ""; await apiPost(adminApi(`/users/${user.id}/note`), { note: note || null }); setNoteDrafts((current) => { const next = { ...current }; delete next[user.id]; return next; }); await refresh(false); }
    catch (error) { setMessage(error instanceof Error ? error.message : "备注保存失败"); } finally { setSavingNoteId(null); }
  }
  async function deleteUser(user: User) {
    const name = user.display_name || user.student_no || `用户 ${user.id}`;
    if (!window.confirm(`确定永久删除 ${name} 吗？其关联数据将一并删除且无法撤销。`)) return;
    try { const result = await apiPost<{ storage_cleanup_pending: boolean }>(adminApi(`/users/${user.id}/delete`), { confirm: true }); setExpandedUserId(null); setMessage(result.storage_cleanup_pending ? "用户已删除；运行目录已隔离，将由服务启动时继续清理。" : "用户及其运行数据已删除。"); await refresh(false); } catch (error) { setMessage(error instanceof Error ? error.message : "删除失败"); }
  }
  async function saveCloudOcr() {
    setSavingCloud(true);
    try {
      const updated = await apiPost<CloudOcrSettings>(adminApi("/cloud-ocr"), { enabled: cloudEnabled, base_url: cloudBaseUrl, model: cloudModel, api_key: cloudApiKey || null, clear_api_key: clearCloudApiKey });
      setCloudOcr(updated); setCloudBaseUrl(updated.base_url); setCloudApiKey(""); setClearCloudApiKey(false); setMessage("云端 OCR 配置已加密保存，将在下一次任务中生效。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "云端 OCR 配置保存失败"); } finally { setSavingCloud(false); }
  }
  async function runNow(user: User) { setMessage(`正在为 ${user.display_name || user.student_no || "该用户"} 执行任务...`); try { const result = await apiPost<{ detail: string }>(adminApi(`/users/${user.id}/run`)); setMessage(result.detail); await refresh(false); } catch (error) { setMessage(error instanceof Error ? error.message : "执行失败"); } }
  async function revoke(invite: Invite) { try { await apiPost(adminApi(`/invites/${invite.id}/revoke`)); await refresh(false); } catch (error) { setMessage(error instanceof Error ? error.message : "撤销失败"); } }
  async function revealAndCopyToken(invite: Invite) {
    try { const result = await apiPost<{ token: string }>(adminApi(`/invites/${invite.id}/reveal`)); if (hoveredTokenCells.current.has(invite.id)) setRevealedTokens((current) => ({ ...current, [invite.id]: result.token })); await navigator.clipboard.writeText(result.token); setMessage("Token 已复制；页面明文会在指针移出或失焦后隐藏。"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Token 查看失败"); }
  }
  function hideToken(inviteId: number) { hoveredTokenCells.current.delete(inviteId); setRevealedTokens((current) => { if (!(inviteId in current)) return current; const next = { ...current }; delete next[inviteId]; return next; }); }
  async function logout() { await apiPost(adminApi("/auth/logout")); window.sessionStorage.removeItem("admin_csrf"); window.location.assign(adminPagePath()!); }

  const activeMeta = sections.find((item) => item.id === section)!;

  return <main className="admin-app-shell">
    <aside className={`admin-sidebar ${mobileNavOpen ? "open" : ""}`} aria-label="管理员功能导航"><div className="admin-brand"><span>SWU DAKA</span><strong>管理后台</strong></div><nav>{sections.map((item) => <button key={item.id} className={section === item.id ? "active" : ""} aria-current={section === item.id ? "page" : undefined} onClick={() => changeSection(item.id)}>{item.label}</button>)}</nav><div className="admin-sidebar-foot">仅限管理员会话</div></aside>
    {mobileNavOpen ? <button className="admin-nav-backdrop" aria-label="关闭导航" onClick={() => setMobileNavOpen(false)} /> : null}
    <section className="admin-workspace">
      <header className="admin-toolbar"><button className="admin-menu-button" aria-label="打开管理导航" aria-expanded={mobileNavOpen} onClick={() => setMobileNavOpen((open) => !open)}>☰</button><div><p className="eyebrow">SWU DAKA</p><strong>管理后台</strong></div><div className="admin-toolbar-actions"><label><span>外观</span><select value={theme} onChange={(event) => setTheme(event.target.value as ThemeMode)}><option value="auto">跟随设备</option><option value="light">浅色</option><option value="dark">深色</option></select></label><span>{session?.username}</span><button className="text-button" onClick={logout}>退出</button></div></header>
      <section className="admin-page"><header className="admin-page-heading"><div><h1>{activeMeta.label}</h1><p>{activeMeta.description}</p></div><span>{total} 条</span></header>{message ? <p className="admin-action-notice">{message}<button aria-label="关闭提示" onClick={() => setMessage("")}>×</button></p> : null}

        {section === "users" ? <div className="admin-section-layout"><div className="admin-mini-stats"><span>用户 <b>{dashboard?.users ?? "—"}</b></span><span>启用 <b>{dashboard?.active_users ?? "—"}</b></span><span>自动任务 <b>{dashboard?.enabled_schedules ?? "—"}</b></span></div><div className="admin-list-region" role="region" aria-label="用户列表" tabIndex={0}><div className="user-management-grid">{users.map((user) => {
          const expanded = expandedUserId === user.id; const userName = user.display_name || user.student_no || `用户 ${user.id}`;
          return <article className={`user-management-card ${expanded ? "expanded" : ""}`} data-user-id={user.id} key={user.id}><button className="user-card-summary" type="button" aria-expanded={expanded} aria-controls={`user-details-${user.id}`} onClick={() => setExpandedUserId(expanded ? null : user.id)}><span className="user-card-identity"><strong>{userName}{user.admin_note ? <span className="user-summary-note">（{user.admin_note}）</span> : null}</strong><small>#{user.id} · {new Date(user.created_at).toLocaleDateString()}</small></span><span className="user-card-account">{user.school_username || "未绑定账号"}</span><span className="user-card-summary-state"><b className={`status ${user.active ? "success" : "failed"}`}>{user.active ? "启用" : "停用"}</b><span className="user-card-chevron" aria-hidden="true">⌄</span></span></button>
            {expanded ? <div className="user-card-expanded" id={`user-details-${user.id}`}><dl className="user-management-details"><div><dt>注册状态</dt><dd>{user.login_enabled ? "可登录" : "待 Token 激活"}</dd></div><div><dt>自动任务</dt><dd>{user.schedule_enabled ? "已开启" : "已关闭"}<small>{user.next_run_at ? new Date(user.next_run_at).toLocaleString() : "暂无计划"}</small></dd></div><div><dt>邮件提醒</dt><dd>{user.notify_on_failure ? "已开启" : "未开启"}<small>{user.notification_email_unreadable ? "邮箱密文异常" : `${user.notification_email || "未填写"}${user.email_verified ? " · 已验证" : " · 未验证"}`}</small></dd></div><div><dt>最近状态</dt><dd>{user.last_status || "—"}</dd></div><div className="wide"><dt>备注</dt><dd><div className="user-note-editor"><input aria-label={`${userName}的备注`} maxLength={120} placeholder="无备注" value={noteDrafts[user.id] ?? user.admin_note ?? ""} onChange={(event) => setNoteDrafts((current) => ({ ...current, [user.id]: event.target.value }))} /><button disabled={savingNoteId === user.id || (noteDrafts[user.id] ?? user.admin_note ?? "") === (user.admin_note ?? "")} onClick={() => saveUserNote(user)}>{savingNoteId === user.id ? "保存中" : "保存"}</button></div></dd></div></dl><div className="user-management-actions"><button onClick={() => toggleActive(user)}>{user.active ? "停用用户" : "启用用户"}</button><button disabled={!user.login_enabled} onClick={() => resetLogin(user)}>重置登录</button><button disabled={!user.active || !user.has_credential} onClick={() => toggleSchedule(user)}>{user.schedule_enabled ? "关闭自动" : "开启自动"}</button><button disabled={!user.active || !user.has_credential} onClick={() => runNow(user)}>立即执行</button><button className="danger-button" onClick={() => deleteUser(user)}>删除用户</button></div></div> : null}</article>;
        })}</div></div></div> : null}

        {section === "tokens" ? <div className="admin-section-layout"><section className="admin-compact-form"><label>备注<input maxLength={120} placeholder="例如：张同学" value={label} onChange={(event) => setLabel(event.target.value)} /></label><label>有效期<select value={expiresInHours} onChange={(event) => setExpiresInHours(Number(event.target.value))}><option value={24}>24 小时</option><option value={72}>3 天</option><option value={168}>7 天</option></select></label><button className="button primary" onClick={createInvite}>生成 Token</button>{newToken ? <div className="invite-result"><code>{newToken}</code><button className="text-button" onClick={() => navigator.clipboard.writeText(newToken)}>复制</button></div> : null}</section><div className="admin-list-region" role="region" aria-label="Token 列表" tabIndex={0}><div className="admin-table-frame"><table><thead data-list-header><tr><th>ID / 备注</th><th>Token</th><th>状态</th><th>使用</th><th>到期时间</th><th>操作</th></tr></thead><tbody>{invites.map((invite) => <tr key={invite.id}><td data-label="ID / 备注">#{invite.id}<small>{invite.label || "无备注"}</small></td><td data-label="Token" className="token-cell" onMouseEnter={() => hoveredTokenCells.current.add(invite.id)} onMouseLeave={() => hideToken(invite.id)}>{invite.token_revealable && invite.status === "active" ? <button className={`token-reveal ${revealedTokens[invite.id] ? "revealed" : ""}`} onFocus={() => hoveredTokenCells.current.add(invite.id)} onBlur={() => hideToken(invite.id)} onClick={() => revealAndCopyToken(invite)}>{revealedTokens[invite.id] || "••••••••…••••"}</button> : <span className="token-unavailable">{invite.status === "used" ? "已使用，明文已销毁" : "不可查看"}</span>}</td><td data-label="状态"><b className={`status invite-${invite.status}`}>{inviteStatusText[invite.status]}</b></td><td data-label="使用">{invite.use_count}/{invite.max_uses}</td><td data-label="到期时间">{new Date(invite.expires_at).toLocaleString()}</td><td data-label="操作" className="actions"><button disabled={invite.status !== "active"} onClick={() => revoke(invite)}>撤销</button></td></tr>)}</tbody></table></div></div></div> : null}

        {section === "api" ? <div className="admin-section-layout"><section className="admin-compact-form cloud-direct-form"><label className="cloud-toggle"><input type="checkbox" checked={cloudEnabled} onChange={(event) => setCloudEnabled(event.target.checked)} />启用云端后备</label><label className="wide">Base URL（完整 Chat Completions 地址）<input type="url" maxLength={512} value={cloudBaseUrl} onChange={(event) => setCloudBaseUrl(event.target.value)} /></label><label>模型<input maxLength={120} value={cloudModel} onChange={(event) => setCloudModel(event.target.value)} /></label><label>API Key<input type="password" autoComplete="new-password" value={cloudApiKey} onChange={(event) => { setCloudApiKey(event.target.value); if (event.target.value) setClearCloudApiKey(false); }} placeholder={cloudOcr?.api_key_configured ? "已配置；留空保持" : "尚未配置"} /></label><label className="cloud-toggle"><input type="checkbox" checked={clearCloudApiKey} onChange={(event) => { setClearCloudApiKey(event.target.checked); if (event.target.checked) setCloudApiKey(""); }} />清除已保存 Key</label><button className="button primary" disabled={savingCloud} onClick={saveCloudOcr}>{savingCloud ? "保存中" : "保存配置"}</button></section><div className="admin-list-region" role="region" aria-label="API 调用记录" tabIndex={0}><div className="admin-table-frame"><table><thead data-list-header><tr><th>地址 / 模型</th><th>用户</th><th>场景</th><th>结果</th><th>用量</th><th>耗时</th><th>时间</th></tr></thead><tbody>{ocrCalls.map((call) => <tr key={call.id}><td data-label="地址 / 模型"><strong>{call.base_url_host}</strong><small>{call.model}</small></td><td data-label="用户">{call.display_name || call.student_no || (call.user_id ? `用户 ${call.user_id}` : "注册验证")}</td><td data-label="场景">{call.context === "login" ? "登录" : "打卡"}</td><td data-label="结果"><b className={`status ${call.status}`}>{call.status}</b></td><td data-label="用量">{call.total_tokens} tokens<small>{call.cloud_calls} 次调用 · 图像 {call.image_tokens}</small></td><td data-label="耗时">{call.latency_ms} ms</td><td data-label="时间">{new Date(call.created_at).toLocaleString()}</td></tr>)}</tbody></table></div></div></div> : null}

        {section === "runs" ? <div className="admin-section-layout"><div className="admin-list-region" role="region" aria-label="运行记录" tabIndex={0}><div className="admin-table-frame"><table><thead data-list-header><tr><th>用户</th><th>来源</th><th>状态</th><th>说明</th><th>开始时间</th></tr></thead><tbody>{runs.map((run) => <tr key={run.id}><td data-label="用户">{run.display_name || run.student_no || `用户 ${run.user_id}`}<small>#{run.id}</small></td><td data-label="来源">{run.trigger_source}</td><td data-label="状态"><b className={`status ${run.status}`}>{run.status}</b></td><td data-label="说明">{run.detail}</td><td data-label="开始时间">{new Date(run.started_at).toLocaleString()}</td></tr>)}</tbody></table></div></div></div> : null}

        {section === "logs" ? <div className="admin-section-layout"><div className="admin-list-region" role="region" aria-label="系统日志" tabIndex={0}><div className="admin-table-frame"><table><thead data-list-header><tr><th>级别</th><th>模块</th><th>内容</th><th>时间</th></tr></thead><tbody>{logs.map((log) => <tr key={`${log.module}-${log.id}`}><td data-label="级别"><b className={`status log-${log.level.toLowerCase()}`}>{log.level}</b></td><td data-label="模块"><code>{log.module}</code></td><td data-label="内容">{log.message}</td><td data-label="时间">{new Date(log.created_at).toLocaleString()}</td></tr>)}</tbody></table></div></div></div> : null}
      </section>
    </section>
  </main>;
}
