import os
import tempfile

# ============================== 默认配置 ==============================
DEFAULT_USERNAME = os.environ.pop("SWU_USERNAME", "")
DEFAULT_PASSWORD = os.environ.pop("SWU_PASSWORD", "")

DEFAULT_FORM_ID = "03acfcb3ccda4138bc017587f211cc30"
DEFAULT_CQFBID = "128c549bd26e4aa08b2dace3271b13f4"
DEFAULT_BUSINESS_KEY = "a49252033b2611f18ce10242ac130002"

DEFAULT_LAT = 29.816799
DEFAULT_LNG = 106.421665
DEFAULT_ADDRESS = "重庆市北碚区融汇南路8号靠近北碚区公安分局(西南大学综合服务大厅)"
DEFAULT_PROVINCE = "重庆市"
DEFAULT_CITY = "重庆市"
DEFAULT_DISTRICT = "北碚区"
DEFAULT_ROAD = "融汇南路"
DEFAULT_QSQDDD = "重庆市北碚区天生街道2号"
DEFAULT_QDBJ = "800米"
DEFAULT_QDSJ = ["21:00", "23:30"]

# 可由 Web/Docker 为每个用户注入独立运行目录。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.abspath(os.environ.get("SWU_RUNTIME_DIR", PROJECT_ROOT))
CHROME_EXE = os.environ.get(
    "SWU_CHROME_EXECUTABLE",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe" if os.name == "nt" else "",
)
DEBUG_PORT = 9222
USER_DATA_DIR = os.path.abspath(
    os.environ.get("SWU_USER_DATA_DIR", os.path.join(tempfile.gettempdir(), "ChromeSwuLoginProfile"))
)
CACHE_FILE = os.path.abspath(os.environ.get("SWU_CACHE_FILE", os.path.join(RUNTIME_DIR, "checkin_cache.json")))
TOKEN_FILE = os.path.abspath(os.environ.get("SWU_TOKEN_FILE", os.path.join(RUNTIME_DIR, "token.txt")))
CAPTCHA_FILE = os.path.abspath(os.environ.get("SWU_CAPTCHA_FILE", os.path.join(RUNTIME_DIR, "captcha.png")))
NETWORK_LOG_DIR = os.path.abspath(os.environ.get("SWU_NETWORK_LOG_DIR", os.path.join(RUNTIME_DIR, "network_logs")))
AUDIT_LOG_DIR = os.path.abspath(os.environ.get("SWU_AUDIT_LOG_DIR", os.path.join(RUNTIME_DIR, "audit_logs")))
CHROME_HEADLESS = os.environ.get("SWU_CHROME_HEADLESS", "0").lower() in {"1", "true", "yes", "on"}
CHROME_STARTUP_TIMEOUT_SECONDS = max(
    5.0, float(os.environ.get("SWU_CHROME_STARTUP_TIMEOUT_SECONDS", "45"))
)
LOGIN_FLOW_TIMEOUT_SECONDS = max(
    30.0, float(os.environ.get("SWU_LOGIN_FLOW_TIMEOUT_SECONDS", "180"))
)
IDM_HTTP_LOGIN_ENABLED = os.environ.get(
    "SWU_IDM_HTTP_LOGIN",
    "1" if os.environ.get("SWU_IDM_HTTP_IMPERSONATE", "").strip() else "0",
).lower() in {"1", "true", "yes", "on"}
IDM_HTTP_USER_AGENT = os.environ.get(
    "SWU_IDM_HTTP_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
)
IDM_HTTP_TIMEOUT_SECONDS = max(
    5.0, min(60.0, float(os.environ.get("SWU_IDM_HTTP_TIMEOUT_SECONDS", "20")))
)
MIMO_API_URL = os.environ.get(
    "MIMO_API_URL", "https://api.xiaomimimo.com/v1/chat/completions"
)
MIMO_MODEL = os.environ.get("MIMO_MODEL", "mimo-v2.5")
MIMO_API_TIMEOUT_SECONDS = max(
    5.0, min(60.0, float(os.environ.get("MIMO_API_TIMEOUT_SECONDS", "20")))
)
MIMO_OCR_MAX_CALLS_PER_LOGIN = max(
    0, min(8, int(os.environ.get("MIMO_OCR_MAX_CALLS_PER_LOGIN", "2")))
)

BAIDA_URL_KEYWORDS = (
    "/gateway/",
    "exchange-token",
    "fighter-baida",
    "fighter-middle",
    "cqlc",
    "form-instance",
    "businessKey",
    "formId",
    "cqfbid",
)
BAIDAFORM_DISCOVERY_URLS = (
    "https://of.swu.edu.cn/baidaForm/#/index",
    "https://of.swu.edu.cn/baidaForm/#/",
    "https://of.swu.edu.cn/baidaForm/",
    "https://of.swu.edu.cn/#/appCenter",
    "https://of.swu.edu.cn/#/casLogin?from=%2Findex",
)
SIGNED_QDJG_VALUE = "1"
DEFAULT_SUBMIT_QDJG_VALUE = "0"
DEFAULT_SUBMIT_QDJG_TEXT = "未签到"
DEFAULT_SUCCESS_MSG_KEYWORD = "保存成功"
DEFAULT_CHECKIN_RESULT_VALUE = SIGNED_QDJG_VALUE
DEFAULT_CHECKIN_RESULT_TEXT = "已签到"

SERVICE = (
    "https%3A%2F%2Fof.swu.edu.cn%2Fgateway%2Ffighter-middle%2Fapi%2Fintegrate"
    "%2Fuaap%2Fcas%2Fresolve-cas-return%3Fnext%3Dhttps%253A%252F%252Fof.swu.edu.cn"
    "%252F%2523%252FcasLogin%253Ffrom%253D%25252FappCenter"
)
INIT_URL = f"https://of.swu.edu.cn/cas/login?service={SERVICE}"
FEDERAL_URL = f"https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL?service={SERVICE}"
# 该入口会动态生成 UAAAP state，并通过 federalEnable 跳转到 IDM。
# 不可硬编码最终 IDM URL，其中的 state/goto/redirect_uri 均为一次性参数。
IDM_BOOTSTRAP_URL = os.environ.get("SWU_IDM_BOOTSTRAP_URL", "https://ywtb.swu.edu.cn/")
BAIDAFORM_SERVICE = (
    "https%3A%2F%2Fof.swu.edu.cn%2Fgateway%2Ffighter-middle%2Fapi%2Fintegrate"
    "%2Fuaap%2Fcas%2Fresolve-cas-return%3Fnext%3Dhttps%253A%252F%252Fof.swu.edu.cn"
    "%252FbaidaForm%252F%2523%252FcasLogin%253Ffrom%253D%25252Findex"
)
BAIDAFORM_INIT_URL = f"https://of.swu.edu.cn/cas/login?service={BAIDAFORM_SERVICE}"
BAIDAFORM_FEDERAL_URL = f"https://of.swu.edu.cn/cas/oauth/login/SWU_CAS2_FEDERAL?service={BAIDAFORM_SERVICE}"

CQTJ_TODAY_URL = "https://of.swu.edu.cn/gateway/fighter-baida/api/cqtj/getTransitionByToday"
FORM_INSTANCE_SELECT_URL = "https://of.swu.edu.cn/gateway/fighter-baida/api/form-instance/select"
CQLC_BASE_URL = "https://of.swu.edu.cn/gateway/fighter-baida/api/cqlc"
FORM_INSTANCE_SAVE_URL = "https://of.swu.edu.cn/gateway/fighter-baida/api/form-instance/save"
DINGTALK_USER_AGENT = (
    "Mozilla/5.0 (Linux; U; Android 12; zh-CN; ALN-AL80 Build/HUAWEIALN-AL80) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 "
    "UWS/5.12.11.0 Mobile Safari/537.36 AliApp(DingTalk/7.8.5.1) "
    "com.alibaba.android.rimet.diswu/PIS756181878951764992 "
    "Channel/exclusive_dingtalk_263387052 language/zh-CN 2ndType/exclusive abi/64 "
    "Hmos/4.2.0 xpn/huawei UT4Aplus/0.2.25 colorScheme/light"
)
DINGTALK_X_REQUESTED_WITH = "com.alibaba.android.rimet.diswu"

MAX_CAPTCHA_RETRY = 8
MAX_IDM_GATEWAY_RETRY = 4
LOGIN_FAILURE_KEYWORDS = (
    "用户名或密码错误",
    "账号或密码错误",
    "验证失败",
    "帐户将被锁定",
    "账户将被锁定",
)
