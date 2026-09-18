import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import get_settings

_PASSWORD_SCHEME = "scrypt-v1"
_CREDENTIAL_AAD = b"swu-daka:credential:v1"
_NOTIFICATION_AAD = b"swu-daka:notification-code:v1"
_EMAIL_AAD = b"swu-daka:email:v1"
_EMAIL_PREFIX = "aesgcm-email-v1:"
_INVITE_TOKEN_AAD = b"swu-daka:invite-token:v1"
_CLOUD_API_KEY_AAD = b"swu-daka:cloud-api-key:v1"


def _hash_password(password: str, minimum_length: int, label: str) -> str:
    if len(password) < minimum_length:
        raise ValueError(f"{label}至少需要 {minimum_length} 个字符")
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "$".join(
        (_PASSWORD_SCHEME, base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode())
    )


def hash_web_password(password: str) -> str:
    return _hash_password(password, 12, "管理员密码")


def hash_user_password(password: str) -> str:
    return _hash_password(password, 1, "校园密码")


def verify_web_password(password: str, password_hash: str) -> bool:
    try:
        scheme, salt_text, expected_text = password_hash.split("$", 2)
        if scheme != _PASSWORD_SCHEME:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(expected_text.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _credential_key() -> bytes:
    value = get_settings().credential_key
    if not value:
        raise RuntimeError("未配置 CHECKIN_WEB_CREDENTIAL_KEY 或其 _FILE secret")
    try:
        key = base64.urlsafe_b64decode(value.encode())
    except Exception as exc:
        raise RuntimeError("凭据主密钥必须是 URL-safe Base64") from exc
    if len(key) != 32:
        raise RuntimeError("凭据主密钥解码后必须恰好为 32 字节")
    return key


def credential_key_id() -> str:
    return hashlib.sha256(_credential_key()).hexdigest()[:16]


def encrypt_school_password(plain_password: str) -> tuple[str, str]:
    key = _credential_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plain_password.encode("utf-8"), _CREDENTIAL_AAD)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode(), credential_key_id()


def decrypt_school_password(encrypted_password: str, key_id: str) -> str:
    if key_id != credential_key_id():
        raise RuntimeError("凭据主密钥与数据库记录不匹配")
    try:
        payload = base64.urlsafe_b64decode(encrypted_password.encode())
        return AESGCM(_credential_key()).decrypt(payload[:12], payload[12:], _CREDENTIAL_AAD).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("校园凭据解密失败") from exc


def encrypt_notification_code(code: str) -> str:
    key = _credential_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, code.encode("utf-8"), _NOTIFICATION_AAD)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_notification_code(encrypted_code: str) -> str:
    try:
        payload = base64.urlsafe_b64decode(encrypted_code.encode())
        return AESGCM(_credential_key()).decrypt(payload[:12], payload[12:], _NOTIFICATION_AAD).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("邮箱验证码解密失败") from exc


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_encrypted_email(value: str | None) -> bool:
    return bool(value and value.startswith(_EMAIL_PREFIX))


def encrypt_email(email: str) -> str:
    normalized = normalize_email(email)
    nonce = os.urandom(12)
    ciphertext = AESGCM(_credential_key()).encrypt(nonce, normalized.encode("utf-8"), _EMAIL_AAD)
    return _EMAIL_PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_email(encrypted_email: str) -> str:
    if not is_encrypted_email(encrypted_email):
        raise RuntimeError("邮箱地址不是受支持的加密格式")
    try:
        payload = base64.urlsafe_b64decode(encrypted_email.removeprefix(_EMAIL_PREFIX).encode())
        return AESGCM(_credential_key()).decrypt(payload[:12], payload[12:], _EMAIL_AAD).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("邮箱地址解密失败") from exc


def hash_email(email: str) -> str:
    return hmac.new(_credential_key(), normalize_email(email).encode("utf-8"), hashlib.sha256).hexdigest()


def hash_verification_code(user_id: int, email: str, code: str) -> str:
    message = f"{user_id}:{normalize_email(email)}:{code}".encode("utf-8")
    return hmac.new(_credential_key(), message, hashlib.sha256).hexdigest()


def new_random_token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def encrypt_invite_token(token: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_credential_key()).encrypt(
        nonce, token.encode("utf-8"), _INVITE_TOKEN_AAD
    )
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_invite_token(encrypted_token: str) -> str:
    try:
        payload = base64.urlsafe_b64decode(encrypted_token.encode())
        return AESGCM(_credential_key()).decrypt(
            payload[:12], payload[12:], _INVITE_TOKEN_AAD
        ).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("邀请 Token 解密失败") from exc


def encrypt_cloud_api_key(api_key: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_credential_key()).encrypt(
        nonce, api_key.encode("utf-8"), _CLOUD_API_KEY_AAD
    )
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_cloud_api_key(encrypted_api_key: str) -> str:
    try:
        payload = base64.urlsafe_b64decode(encrypted_api_key.encode())
        return AESGCM(_credential_key()).decrypt(
            payload[:12], payload[12:], _CLOUD_API_KEY_AAD
        ).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("云端大模型 API Key 解密失败") from exc
