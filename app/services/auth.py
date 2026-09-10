import hashlib, hmac, time, threading, requests, logging
from typing import Optional
from config.settings import Config

logger = logging.getLogger("wechat-robot")
_token_lock = threading.Lock()
_token_cache = {"token": "", "expires_at": 0.0}


def check_signature(signature, timestamp, nonce) -> bool:
    if not all((signature, timestamp, nonce, Config.WECHAT_TOKEN)):
        return False
    tmp = "".join(sorted([Config.WECHAT_TOKEN, timestamp, nonce])).encode()
    digest = hashlib.sha1(tmp).hexdigest()
    return hmac.compare_digest(digest, signature)


def get_access_token(force_refresh=False) -> Optional[str]:
    if not (Config.WECHAT_APPID and Config.WECHAT_APPSECRET):
        return None
    with _token_lock:
        if not force_refresh and _token_cache["token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["token"]
        try:
            resp = requests.post(
                f"{Config.WECHAT_API_BASE}/cgi-bin/stable_token",
                json={"grant_type": "client_credential", "appid": Config.WECHAT_APPID,
                      "secret": Config.WECHAT_APPSECRET},
                timeout=30,
            )
            data = resp.json()
            token = data.get("access_token")
            if not token:
                hint = ""
                if data.get("errcode") == 40125:
                    hint = ("【提示】AppSecret 无效：请核对 .env 的 WECHAT_APPSECRET 是否与公众平台"
                            "（mp.weixin.qq.com → 开发 → 基本配置）当前值一致；若曾重置过 AppSecret，"
                            "旧值会立即失效并报 40125，需同步更新部署文件并重启。")
                elif data.get("errcode") in (40013, 41002):
                    hint = "【提示】WECHAT_APPID 无效或与 AppSecret 不匹配，请检查 .env。"
                logger.error("获取 access_token 失败: %s %s", data, hint)
                return None
            _token_cache["token"] = token
            _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 7200)) - 200
            logger.info("access_token 已刷新")
            return token
        except Exception:
            logger.exception("请求 access_token 失败")
            return None


def wechat_api(method, path, retry=True, **kwargs):
    """统一微信API调用入口，自动处理Token过期重试"""
    token = get_access_token()
    if not token:
        return {"errcode": -1, "errmsg": "access_token 未获取"}
    sep = "&" if "?" in path else "?"
    url = f"{Config.WECHAT_API_BASE}{path}{sep}access_token={token}"
    try:
        resp = requests.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
    except Exception:
        logger.exception("微信API请求失败: %s", path)
        return {"errcode": -1, "errmsg": "请求异常"}

    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype or "text/plain" in ctype:
        data = resp.json()
        if data.get("errcode") in (40001, 42001, 40014) and retry:
            logger.warning("Token失效，刷新重试")
            get_access_token(force_refresh=True)
            return wechat_api(method, path, retry=False, **kwargs)
        return data
    return resp