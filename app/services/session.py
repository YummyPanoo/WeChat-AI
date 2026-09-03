import time, threading, logging
from typing import List, Dict

logger = logging.getLogger("wechat-robot")

# --------------- 会话配置 ---------------
MAX_TURNS = 10           # 每用户最多保留对话轮数（1轮 = 1问 + 1答）
SESSION_TTL = 1800       # 会话过期时间（秒），30 分钟无活动自动清除
CLEANUP_INTERVAL = 300   # 过期清理间隔（秒）


class _SessionStore:
    """线程安全的 per-user 对话历史管理器"""

    def __init__(self):
        self._store: Dict[str, Dict] = {}   # openid -> {messages, updated_at}
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    # ---------- 公开方法 ----------
    def add_message(self, openid: str, role: str, content: str):
        """向指定用户的会话历史追加一条消息"""
        now = time.time()
        with self._lock:
            if openid not in self._store:
                self._store[openid] = {"messages": [], "updated_at": now}
            session = self._store[openid]
            # 超过 TTL 视为新会话，清空旧历史
            if now - session["updated_at"] > SESSION_TTL:
                session["messages"] = []
            session["messages"].append({"role": role, "content": content})
            # 保留最近 MAX_TURNS 轮（每轮 2 条消息）
            max_msgs = MAX_TURNS * 2
            if len(session["messages"]) > max_msgs:
                session["messages"] = session["messages"][-max_msgs:]
            session["updated_at"] = now

    def get_history(self, openid: str) -> List[dict]:
        """获取指定用户的对话历史副本（不含当前待发送的消息）"""
        now = time.time()
        with self._lock:
            self._maybe_cleanup(now)
            session = self._store.get(openid)
            if not session:
                return []
            if now - session["updated_at"] > SESSION_TTL:
                session["messages"] = []
                session["updated_at"] = now
            return list(session["messages"])  # 返回副本

    # ---------- 内部方法 ----------
    def _maybe_cleanup(self, now: float):
        """定期清理过期会话，减少内存占用（需在锁内调用）"""
        if now - self._last_cleanup < CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        expired = [
            uid for uid, s in self._store.items()
            if now - s["updated_at"] > SESSION_TTL
        ]
        for uid in expired:
            del self._store[uid]
        if expired:
            logger.debug("清理过期会话: %d 个用户", len(expired))


# 全局单例
session_store = _SessionStore()

