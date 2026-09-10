"""
服务配额校验服务（公众号端）。

调用 WeChatRegister 的 /api/usage/check 接口完成配额校验与消耗，
确保公众号上的实际使用次数与网页/数据库记录的剩余次数连通：
  - 文生图（t2i）：免费次数用完且订阅过期 → 拒绝并提示购买订阅
  - 图生图（i2i）：免费次数用完且订阅过期 → 拒绝并提示购买订阅
  - 图片分析（image_analysis）：注册免费期过后 → 拒绝并提示
  - 文字对话（text_chat）：永久免费，不做配额检查

注意：本模块通过 WeChatRegister 的 HTTP API 消耗额度（写操作），
数据一致性由 WeChatRegister 权威逻辑（entitlements 表 + usage_logs）保证。
"""
import logging
from typing import Optional, Tuple

import requests

from config.settings import Config
from app.services.user_info import get_user_by_openid, get_user_entitlements

logger = logging.getLogger("wechat-robot")


def _register_backend_url() -> str:
    """从 WeChatRegister 前端地址推导后端 API 地址（5173 → 5000）。"""
    return Config.WECHAT_REGISTER_FRONTEND_URL.replace(":5173", ":5000").rstrip("/")


def check_quota(openid: str, service: str):
    """
    校验并消耗一次服务配额（由 WeChatRegister 权威执行）。

    返回 (allowed, reason, detail, user):
      - allowed: bool                     是否允许使用
      - reason:  str                      拒绝原因：
          "not_bound"   微信号未绑定网站账号
          "denied"      配额不足 / 订阅过期 / 免费期已过
          "error"       配额服务调用异常（网络 / 后端故障）
          "allowed"     允许使用
      - detail:  dict                     WeChatRegister 返回的 detail
      - user:    dict | None              查询到的用户信息（未绑定为 None）
    """
    user = None
    try:
        user = get_user_by_openid(openid)
    except Exception as e:
        logger.warning("配额校验-查询用户失败: %s", str(e))
        return False, "error", {}, None
    if not user:
        return False, "not_bound", {}, None

    try:
        resp = requests.post(
            f"{_register_backend_url()}/api/usage/check",
            json={"user_id": user["id"], "service": service},
            timeout=5,
        )
        data = resp.json()
    except Exception as e:
        logger.warning("配额校验-请求失败: openid=%s, service=%s, err=%s",
                       openid[:8], service, str(e))
        return False, "error", {}, user

    if not data.get("ok"):
        logger.warning("配额校验-后端返回异常: %s", data)
        return False, "error", data.get("detail", {}), user

    if data.get("allowed"):
        logger.info("配额校验-允许: user_id=%s, service=%s, detail=%s",
                    user["id"], service, data.get("detail"))
        return True, "allowed", data.get("detail", {}), user

    logger.info("配额校验-拒绝: user_id=%s, service=%s, detail=%s",
                user["id"], service, data.get("detail"))
    return False, "denied", data.get("detail", {}), user


def precheck_quota(openid: str, service: str) -> Optional[str]:
    """
    只读预检（不消耗配额）。返回拒绝文案，或 None 表示可继续。
    用于同步模式下首次收到消息时即时回复，避免用户空等 5 秒重试。
    """
    from datetime import datetime

    try:
        user = get_user_by_openid(openid)
    except Exception as e:
        logger.warning("配额预检-查询用户失败: %s", str(e))
        return "配额服务暂时异常，请稍后重试。"
    if not user:
        return "⚠️ 您还未绑定微信号。\n\n请先在网页端生成绑定码并发送给公众号：\n%s" % (
            Config.WECHAT_REGISTER_FRONTEND_URL
        )

    try:
        ent = get_user_entitlements(user["id"]) or {}
    except Exception as e:
        logger.warning("配额预检-读取额度失败: %s", str(e))
        return "配额服务暂时异常，请稍后重试。"

    now = datetime.now()
    if service == "t2i":
        free_left = int(ent.get("t2i_free_left") or 0)
        expire = ent.get("t2i_sub_expire_at")
        if free_left <= 0 and (not expire or expire <= now):
            return (
                "⛔ 文生图服务不可用\n\n"
                "您的免费次数已用完，且订阅已过期（或未订阅）。\n\n"
                f"👉 请到网页端购买订阅（30元/月）后继续使用：\n"
                f"{Config.WECHAT_REGISTER_FRONTEND_URL}\n\n"
                "回复「我的订单」可查看当前订阅状态。"
            )
    elif service == "i2i":
        free_left = int(ent.get("i2i_free_left") or 0)
        expire = ent.get("i2i_sub_expire_at")
        if free_left <= 0 and (not expire or expire <= now):
            return (
                "⛔ 图生图服务不可用\n\n"
                "您的免费次数已用完，且订阅已过期（或未订阅）。\n\n"
                f"👉 请到网页端购买订阅（20元/月）后继续使用：\n"
                f"{Config.WECHAT_REGISTER_FRONTEND_URL}\n\n"
                "回复「我的订单」可查看当前订阅状态。"
            )
    elif service == "image_analysis":
        free_until = ent.get("image_free_until")
        if not free_until or free_until <= now:
            return (
                "⛔ 图片分析服务已过期\n\n"
                "免费期为注册后 30 天，现已过期。\n\n"
                f"👉 如需继续使用图片分析服务，请访问：\n"
                f"{Config.WECHAT_REGISTER_FRONTEND_URL}\n\n"
                "文字对话仍可免费使用。"
            )
    return None