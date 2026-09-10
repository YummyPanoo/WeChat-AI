import time, threading, logging

logger = logging.getLogger("wechat-robot")

# 重试窗口内的待取结果: {msg_id: {"event", "reply", "done", "created_at"}}
_pending = {}
# 超时窗口外的待交付结果队列(per-user): {openid: [ {msg_id, reply, created_at}, ... ]}
# 用列表(队列)而非单条，避免"上一条超时结果"被下一条完成时覆盖而丢失
_ready = {}
_lock = threading.Lock()
TTL = 120        # _pending 记录 2 分钟后自动清理
READY_TTL = 600  # _ready 待交付结果 10 分钟后自动过期（10分钟内用户再发消息都能取到）
# 文生图结果（per-msg_id）: {msg_id: {"event","media_id","done","openid","created_at"}}
_pending_img = {}
IMG_TTL = 300    # 未送达的生成图片 5 分钟后过期


def _cleanup(now):
    """清理过期记录（需在锁内调用）"""
    for k, v in list(_pending.items()):
        if now - v["created_at"] > TTL:
            del _pending[k]
            logger.debug("清理过期 pending: %s", k)
    for k, items in list(_ready.items()):
        kept = [it for it in items if now - it["created_at"] <= READY_TTL]
        if kept:
            _ready[k] = kept
        else:
            del _ready[k]
            logger.debug("清理过期 ready: %s", k)
    for k, v in list(_pending_img.items()):
        if now - v["created_at"] > IMG_TTL:
            del _pending_img[k]
            logger.debug("清理过期 pending_img: %s", k)


def _save_ready(openid, msg_id, reply):
    """把结果追加到该用户的待交付队列（超时窗口外也能被取回）"""
    _ready.setdefault(openid, []).append(
        {"msg_id": msg_id, "reply": reply, "created_at": time.time()}
    )
    logger.debug("已存入待交付队列: openid=%s, msg_id=%s", openid[:8] if openid else "", msg_id)


def get_or_start(msg_id: str, openid: str, processor, wait: float = 4.8):
    """获取或启动一个消息的后台处理（利用微信重试机制延长 AI 处理时间）。

    msg_id: 微信消息唯一 ID（微信重试时携带同一 ID）
    openid: 用户 ID（用于超过重试窗口后的结果兜底交付）
    processor: 无参回调，返回处理结果字符串
    wait: 主线程等待秒数（默认 4.8，接近微信 5 秒限制）
    返回: (status, reply)  status 为 "done" 或 "pending"
    """
    now = time.time()
    with _lock:
        _cleanup(now)
        if msg_id in _pending:
            entry = _pending[msg_id]
            logger.info("收到重试请求: msg_id=%s, 后台done=%s", msg_id, entry["done"])
        else:
            entry = {
                "event": threading.Event(),
                "reply": None,
                "done": False,
                "created_at": now,
            }
            _pending[msg_id] = entry
            logger.info("启动后台 AI 处理: msg_id=%s", msg_id)

            def _run():
                try:
                    reply = processor()
                except Exception:
                    logger.exception("pending processor 异常")
                    reply = "AI服务暂时不可用"
                with _lock:
                    entry["reply"] = reply
                    entry["done"] = True
                    # 无论是否在重试窗口内完成，都存入该用户待交付区作为兜底
                    _save_ready(openid, msg_id, reply)
                    entry["event"].set()
                logger.info("后台处理完成: msg_id=%s, reply=%s", msg_id, (reply or "")[:50])

            threading.Thread(target=_run, daemon=True).start()

    # 等待结果（最多 wait 秒）
    entry["event"].wait(timeout=wait)

    with _lock:
        if entry["done"]:
            reply = entry["reply"]
            del _pending[msg_id]
            # 本条已在本响应内直接交付，从待交付队列移除，避免下条消息重复补发
            q = _ready.get(openid)
            if q:
                q = [it for it in q if it["msg_id"] != msg_id]
                if q:
                    _ready[openid] = q
                else:
                    del _ready[openid]
            return "done", reply
        return "pending", None


def drain_ready(openid: str):
    """取出该用户所有'已完成但未在重试窗口内交付'的 AI 结果（按时间顺序拼接），
    没有则返回 None。

    用于突破微信重试时间上限：即使 AI 处理超过 ~20 秒（首次+3次重试），
    用户之后发送的任何消息都可以取回这些超时结果，一次性补发。
    """
    now = time.time()
    with _lock:
        _cleanup(now)
        items = _ready.pop(openid, None)
        if not items:
            return None
        replies = [it["reply"] for it in items
                   if now - it["created_at"] <= READY_TTL and it["reply"]]
        return "\n\n".join(replies) if replies else None


def process_later(openid: str, processor):
    """后台执行 processor，完成后把结果存入该用户的待交付区。

    用于本次响应已被"上一条挂起结果"占用时，把当前消息排队处理，
    其结果将在用户下一条消息时被 pop_ready 取回并交付。
    """
    def _run():
        try:
            reply = processor()
        except Exception:
            logger.exception("process_later 异常")
            reply = "AI服务暂时不可用"
        with _lock:
            _save_ready(openid, "later", reply)
        logger.info("排队任务完成，已存入待交付区: user=%s, reply=%s",
                    openid[:8] if openid else "", (reply or "")[:50])

    threading.Thread(target=_run, daemon=True).start()


# ==================== 图生图交互状态机 ====================
# 用户发图 → 询问意图（分析/图生图）→ 询问风格 → 启动生成。
# 中间态存内存即可：TTL 很短（3分钟），且公众号为单实例部署。
# 结构: {openid: {"stage": "ask_intent"|"ask_style", "image_url": str|None, "created_at": float}}
# image_url 为原图的 base64 data URL，选完风格后直接复用，避免二次下载微信素材。

I2I_TTL = 180  # 交互状态有效期（秒），超时需重新发图

_i2i = {}


def _i2i_cleanup(now):
    for openid in [k for k, v in _i2i.items() if now - v["created_at"] > I2I_TTL]:
        _i2i.pop(openid, None)


def set_i2i_stage(openid: str, stage: str, image_url: str = None,
                  msg_id: str = None, kind: str = None, style_code: str = None):
    """设置/推进用户的图生图交互状态。

    stage: ask_intent 询问意图 | ask_style 询问风格 | busy 任务已派发
    image_url 传 None 表示沿用上一状态里已存的原图（推进到选风格阶段时用）。
    msg_id/kind/style_code 在 busy 阶段记录，用于识别微信对同一条选择消息的重试，
    从而续接同一个后台任务而不是当成新消息处理。
    """
    now = time.time()
    with _lock:
        _i2i_cleanup(now)
        prev = _i2i.get(openid) or {}
        _i2i[openid] = {
            "stage": stage,
            "image_url": image_url if image_url is not None else prev.get("image_url"),
            "msg_id": msg_id if msg_id is not None else prev.get("msg_id"),
            "kind": kind if kind is not None else prev.get("kind"),
            "style_code": style_code if style_code is not None else prev.get("style_code"),
            "created_at": now,
        }
    logger.info("图生图状态更新: user=%s, stage=%s, kind=%s",
                openid[:8] if openid else "", stage, kind or "-")


def get_i2i_stage(openid: str):
    """读取用户当前图生图交互状态；不存在或已过期返回 None。"""
    now = time.time()
    with _lock:
        _i2i_cleanup(now)
        st = _i2i.get(openid)
        return dict(st) if st else None


def clear_i2i(openid: str):
    """结束图生图交互（已启动生成 / 用户取消 / 改走图片分析）。"""
    with _lock:
        _i2i.pop(openid, None)



def get_or_start_image(msg_id, openid, processor, wait=4.8):
    """文生图的后台处理 + 复用微信重试机制。processor 返回微信 media_id（失败返回 None）。

    返回 (status, media_id)，status 为 "done"/"pending"。
    超时窗口内未完成的结果会留在队列，可用 pop_pending_image 在用户下次消息时补发交付。

    主动推送：图片生成完成后会立即尝试通过客服接口推送给用户（如果客服接口可用）。
    这样即使超过微信 5 秒同步超时窗口，用户也能及时收到图片。
    """
    now = time.time()
    with _lock:
        _cleanup(now)
        if msg_id in _pending_img:
            entry = _pending_img[msg_id]
            logger.info("图片生成重试: msg_id=%s, done=%s", msg_id, entry["done"])
        else:
            entry = {"event": threading.Event(), "media_id": None, "done": False,
                     "openid": openid, "created_at": now, "pushed": False}
            _pending_img[msg_id] = entry
            logger.info("启动后台图片生成: msg_id=%s", msg_id)

            def _run():
                try:
                    mid = processor()
                except Exception:
                    logger.exception("图片生成异常")
                    mid = None
                with _lock:
                    entry["media_id"] = mid
                    entry["done"] = True
                    entry["event"].set()
                logger.info("图片生成完成: msg_id=%s, media_id=%s", msg_id, mid)

                # 主动推送：图片生成完成后立即尝试通过客服接口发送给用户。
                # mid 可能是 ("url", 链接) 元组（微信素材上传失败、改用链接兜底），
                # 此时没有微信 media_id，无法推送图片，直接标记已处理。
                if mid and not entry.get("pushed"):
                    if isinstance(mid, tuple):
                        entry["pushed"] = True
                    else:
                        _try_push_image(openid, mid, msg_id, entry)

            threading.Thread(target=_run, daemon=True).start()

    entry["event"].wait(timeout=wait)
    with _lock:
        if entry["done"]:
            mid = entry["media_id"]
            # 本次已交付，从队列移除（避免重复）
            if _pending_img.get(msg_id) is entry:
                del _pending_img[msg_id]
            # 同步等待结束后，如果后台还未推送（生成较快，在 wait 内完成），也尝试推送
            # mid 为元组（链接兜底）时无 media_id，跳过推送
            if mid and not entry.get("pushed") and not isinstance(mid, tuple):
                _try_push_image_sync(openid, mid, msg_id, entry)
            return "done", mid
        return "pending", None


def _try_push_image(openid, media_id, msg_id, entry):
    """后台线程中尝试通过客服接口推送图片（异步模式）。"""
    try:
        from app.services.message import send_customer_image
        if send_customer_image(openid, media_id):
            entry["pushed"] = True
            logger.info("图片已通过客服接口主动推送: msg_id=%s", msg_id)
        else:
            logger.info("客服接口推送失败，将依赖补发机制: msg_id=%s", msg_id)
    except Exception:
        logger.exception("主动推送图片异常: msg_id=%s", msg_id)


def _try_push_image_sync(openid, media_id, msg_id, entry):
    """同步等待结束后尝试推送图片（双保险）。"""
    try:
        from app.services.message import send_customer_image
        if send_customer_image(openid, media_id):
            entry["pushed"] = True
            logger.info("图片已通过客服接口推送(同步): msg_id=%s", msg_id)
    except Exception:
        logger.exception("同步推送图片异常: msg_id=%s", msg_id)


def pop_pending_image(openid):
    """交付该用户上一轮超时未送达的生成图片，返回 media_id；没有则返回 None。

    注意：已通过客服接口主动推送的图片会被跳过，避免重复发送。
    """
    now = time.time()
    with _lock:
        _cleanup(now)
        best_key, best = None, None
        for k, v in list(_pending_img.items()):
            # 跳过已通过客服接口主动推送的图片
            if v.get("pushed"):
                del _pending_img[k]
                continue
            if v["openid"] == openid and v["done"] and v["media_id"]:
                if best is None or v["created_at"] > best["created_at"]:
                    best_key, best = k, v
        if best_key:
            del _pending_img[best_key]
            return best["media_id"]
        return None


def has_unfinished_image(openid: str) -> bool:
    """该用户是否有仍在后台生成的图片（已启动但未 done）。

    用于在用户发消息时追加提示（如"之前的图片还在生成中"），
    避免用户以为图片丢失。已完成（含失败）的图片不算未完成。
    """
    now = time.time()
    with _lock:
        _cleanup(now)
        for v in _pending_img.values():
            if v.get("openid") == openid and not v.get("done"):
                return True
        return False
