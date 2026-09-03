import time, threading, logging
from .auth import wechat_api

logger = logging.getLogger("wechat-robot")
_seen_msgs = {}
_seen_lock = threading.Lock()

def is_duplicate(msg_id: str) -> bool:
    if not msg_id: return False
    now = time.time()
    with _seen_lock:
        # 清理10分钟前的记录
        expired = [k for k, t in _seen_msgs.items() if now - t > 600]
        for k in expired: _seen_msgs.pop(k, None)
        if msg_id in _seen_msgs: return True
        _seen_msgs[msg_id] = now
        return False

def send_customer_text(openid: str, content: str, max_retries: int = 3) -> bool:
    """通过客服接口发送文本消息，失败时自动重试（含 Token 刷新重试）。
    返回 True 表示发送成功或已确认为权限问题（调用方应改用同步 XML 回复）。
    返回 False 表示需要重试（网络/临时错误）。
    """
    from config.settings import Config

    for attempt in range(1, max_retries + 1):
        result = wechat_api("POST", "/cgi-bin/message/custom/send", json={
            "touser": openid, "msgtype": "text", "text": {"content": content}
        })
        if isinstance(result, dict) and result.get("errcode", 0) != 0:
            errcode = result.get("errcode")
            # 48001 = api unauthorized：账号无此接口权限（如个人订阅号），永久失败
            if errcode == 48001:
                logger.warning("客服接口无权限(48001)，自动切换为同步 XML 回复模式")
                Config._customer_api_unavailable = True
                return True  # 告知调用方不要再尝试异步发送
            logger.warning("客服消息发送失败 (第%d次): %s", attempt, result)
            if attempt < max_retries:
                time.sleep(1)
                continue
        else:
            return True  # 发送成功
    logger.error("客服消息发送最终失败: openid=%s", openid)
    return False


def send_customer_image(openid, media_id, max_retries=3) -> bool:
    """通过客服接口发送图片消息（media_id 为已上传的微信素材）。
    返回 True 表示发送成功或已确认为权限问题（调用方应改用同步 XML 回复）。"""
    from config.settings import Config

    for attempt in range(1, max_retries + 1):
        result = wechat_api("POST", "/cgi-bin/message/custom/send", json={
            "touser": openid, "msgtype": "image", "image": {"media_id": media_id}
        })
        if isinstance(result, dict) and result.get("errcode", 0) != 0:
            errcode = result.get("errcode")
            if errcode == 48001:
                logger.warning("客服图片接口无权限(48001)，自动切换为同步 XML 回复模式")
                Config._customer_api_unavailable = True
                return True
            logger.warning("客服图片发送失败 (第%d次): %s", attempt, result)
            if attempt < max_retries:
                time.sleep(1)
                continue
        else:
            return True
    logger.error("客服图片发送最终失败: openid=%s", openid)
    return False