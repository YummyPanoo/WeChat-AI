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

def send_customer_text(openid: str, content: str):
    result = wechat_api("POST", "/cgi-bin/message/custom/send", json={
        "touser": openid, "msgtype": "text", "text": {"content": content}
    })
    if isinstance(result, dict) and result.get("errcode", 0) != 0:
        logger.error("客服消息发送失败: %s", result)