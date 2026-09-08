"""
用户订阅/订购信息读取服务（只读）。
连接 WeChatRegister 的 MySQL 数据库，查询用户的配额和订单信息。
仅供公众号AI查询用户订购状态，不具备任何修改权限。
"""
import time
import threading
import logging
from typing import Optional, Dict, List
from config.settings import Config

logger = logging.getLogger("wechat-robot")


class _UserInfoDB:
    """只读数据库连接：连接 WeChatRegister 的 MySQL 数据库查询用户信息。"""

    _conn = None
    _lock = threading.Lock()

    @classmethod
    def _connect(cls):
        import pymysql
        cls._conn = pymysql.connect(**Config.WECHAT_REGISTER_DB)
        cls._conn.autocommit(True)  # 只读，自动提交

    @classmethod
    def _ensure(cls):
        if cls._conn is None:
            cls._connect()
            return
        try:
            cls._conn.ping(reconnect=False)
        except Exception:
            cls._connect()

    @classmethod
    def query(cls, sql, args=None) -> List[dict]:
        """执行只读查询，返回字典列表。"""
        import pymysql
        with cls._lock:
            cls._ensure()
            cur = cls._conn.cursor(pymysql.cursors.DictCursor)
            try:
                cur.execute(sql, args)
                return cur.fetchall()
            finally:
                cur.close()

    @classmethod
    def query_one(cls, sql, args=None) -> Optional[dict]:
        """执行只读查询，返回单条字典或 None。"""
        rows = cls.query(sql, args)
        return rows[0] if rows else None


# ---------------- 公开接口 ----------------

def get_user_by_username(username: str) -> Optional[dict]:
    """根据用户名查找用户信息。"""
    return _UserInfoDB.query_one(
        "SELECT id, username, role, status, register_at FROM users WHERE username=%s",
        (username,),
    )


def get_user_by_id(user_id: int) -> Optional[dict]:
    """根据用户ID查找用户信息。"""
    return _UserInfoDB.query_one(
        "SELECT id, username, role, status, register_at FROM users WHERE id=%s",
        (user_id,),
    )


def get_user_by_openid(openid: str) -> Optional[dict]:
    """根据微信openid查找用户信息（用于公众号端通过openid查询用户）。"""
    return _UserInfoDB.query_one(
        "SELECT id, username, role, status, register_at, wechat_openid FROM users WHERE wechat_openid=%s",
        (openid,),
    )


def get_user_entitlements(user_id: int) -> Optional[dict]:
    """获取用户配额信息（剩余免费次数、订阅状态等）。"""
    return _UserInfoDB.query_one(
        "SELECT image_free_until, t2i_free_left, t2i_sub_expire_at "
        "FROM entitlements WHERE user_id=%s",
        (user_id,),
    )


def get_user_orders(user_id: int, limit: int = 10) -> List[dict]:
    """获取用户订单列表（最近的订单）。"""
    return _UserInfoDB.query(
        "SELECT order_no, service, months, amount, status, created_at, paid_at, expire_at "
        "FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    )


def get_user_usage_summary(user_id: int) -> dict:
    """获取用户使用记录统计。"""
    rows = _UserInfoDB.query(
        "SELECT service, result, COUNT(*) as count FROM usage_logs "
        "WHERE user_id=%s GROUP BY service, result",
        (user_id,),
    )
    summary = {}
    for row in rows:
        service = row["service"]
        if service not in summary:
            summary[service] = {}
        summary[service][row["result"]] = row["count"]
    return summary


def get_user_full_info(username: str) -> Optional[dict]:
    """
    获取用户完整信息（用户基本信息 + 配额 + 订单 + 使用统计）。
    供公众号AI查询用户订购状态使用。
    """
    user = get_user_by_username(username)
    if not user:
        return None

    user_id = user["id"]
    entitlements = get_user_entitlements(user_id)
    orders = get_user_orders(user_id)
    usage = get_user_usage_summary(user_id)

    return {
        "user": user,
        "entitlements": entitlements,
        "orders": orders,
        "usage_summary": usage,
    }


def format_user_info_for_ai(info: dict) -> str:
    """
    将用户信息格式化为AI可读的文本摘要。
    用于在对话中向用户展示其订购状态。
    """
    if not info:
        return "未找到该用户的注册信息。"

    user = info["user"]
    ent = info["entitlements"] or {}
    orders = info["orders"] or []

    lines = []
    lines.append(f"用户: {user['username']}")
    lines.append(f"注册时间: {user['register_at']}")
    lines.append(f"账号状态: {'正常' if user['status'] == 1 else '已禁用'}")

    # 配额信息
    lines.append("\n【服务配额】")
    if ent.get("image_free_until"):
        lines.append(f"图片分析免费截止: {ent['image_free_until']}")
    lines.append(f"文生图剩余免费次数: {ent.get('t2i_free_left', 0)}")
    if ent.get("t2i_sub_expire_at"):
        lines.append(f"文生图订阅到期: {ent['t2i_sub_expire_at']}")
    else:
        lines.append("文生图订阅: 未订阅")

    # 订单信息
    if orders:
        lines.append("\n【最近订单】")
        for o in orders[:5]:
            status_map = {"pending": "待支付", "paid": "已支付", "canceled": "已取消"}
            lines.append(
                f"  订单号: {o['order_no']}, "
                f"服务: {o['service']}, "
                f"金额: {o['amount']}元, "
                f"状态: {status_map.get(o['status'], o['status'])}, "
                f"创建时间: {o['created_at']}"
            )
    else:
        lines.append("\n【订单】暂无订单记录")

    return "\n".join(lines)
