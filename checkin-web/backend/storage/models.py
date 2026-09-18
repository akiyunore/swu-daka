SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    student_no TEXT,
    admin_note TEXT,
    password_hash TEXT NOT NULL DEFAULT '',
    has_agreed_terms INTEGER NOT NULL DEFAULT 0,
    agreed_at TEXT,
    credential_verified_at TEXT,
    invite_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (invite_id) REFERENCES invite_tokens(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    school_username TEXT NOT NULL,
    encrypted_school_password TEXT NOT NULL,
    key_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auto_checkin_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    schedule_time TEXT NOT NULL DEFAULT '21:10',
    next_run_at TEXT,
    last_run_at TEXT,
    last_status TEXT,
    last_detail TEXT,
    last_audit_log_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (admin_user_id) REFERENCES admin_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invite_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    encrypted_token TEXT,
    label TEXT,
    expires_at TEXT NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT,
    created_by_admin_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (created_by_admin_id) REFERENCES admin_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checkin_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    trigger_source TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    audit_log_path TEXT,
    log_tail TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_notification_settings (
    user_id INTEGER PRIMARY KEY,
    email TEXT,
    email_hash TEXT,
    email_verified_at TEXT,
    notify_on_failure INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    email_hash TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    encrypted_code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    used_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    run_id INTEGER,
    dedupe_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    recipient TEXT NOT NULL,
    recipient_hash TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    locked_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES checkin_runs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS cloud_ocr_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'xiaomi_mimo',
    base_url TEXT NOT NULL DEFAULT 'https://api.xiaomimimo.com/v1/chat/completions',
    resolved_addresses TEXT,
    model TEXT NOT NULL DEFAULT 'mimo-v2.5',
    encrypted_api_key TEXT,
    updated_at TEXT NOT NULL,
    updated_by_admin_id INTEGER,
    FOREIGN KEY (updated_by_admin_id) REFERENCES admin_users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS cloud_ocr_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    context TEXT NOT NULL,
    base_url_host TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    cloud_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    image_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS storage_cleanup_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    quarantine_name TEXT NOT NULL UNIQUE,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_integrity_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_table TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    error_code TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(entity_table, entity_key, field_name)
);

CREATE TABLE IF NOT EXISTS scheduled_run_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'claimed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(schedule_id, scheduled_for),
    FOREIGN KEY (schedule_id) REFERENCES auto_checkin_schedules(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_token ON admin_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_user_sessions_token ON user_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_invites_token ON invite_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_runs_user_started ON checkin_runs(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_tokens_user ON email_verification_tokens(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due ON notification_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON auto_checkin_schedules(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_cloud_ocr_calls_created ON cloud_ocr_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_storage_cleanup_pending ON storage_cleanup_queue(updated_at);
CREATE INDEX IF NOT EXISTS idx_migration_integrity_issues_seen ON migration_integrity_issues(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_claims_status ON scheduled_run_claims(status, id);
"""
