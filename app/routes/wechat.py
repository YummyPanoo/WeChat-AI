import re, time, threading, logging, base64
from flask import Blueprint, request
from defusedxml import ElementTree as SafeET
from config.settings import Config
from app.services.auth import check_signature
from app.services.ai_client import chat, vision, generate_image
from app.services.message import send_customer_text, send_customer_image
from app.services.pending import get_or_start, drain_ready, get_or_start_image, pop_pending_image, process_later
from app.services.media import download_media, upload_temp_media, fetch_image_bytes
from app.services.session import session_store
from app.utils.xml_helper import build_text_xml, build_image_xml

bp = Blueprint("wechat", __name__)
logger = logging.getLogger("wechat-robot")

IMAGE_PROMPT = "请简要描述图片内容，提取文字并给出解读。用简洁中文回复。"

# 同步模式下 AI 调用的超时时间（微信只等 5 秒，留 0.5s 余量）
SYNC_TIMEOUT = 5


def _async(func, *args):
    threading.Thread(target=func, args=args, daemon=True).start()


def _use_async() -> bool:
    """判断是否使用异步模式（客服消息接口）。
    优先走异步；若检测到 48001 权限不足则自动回退到同步 XML 回复。
    """
    return Config.WECHAT_ASYNC_MODE and not Config._customer_api_unavailable


def _xml_reply(to_user: str, from_user: str, text: str):
    """构建同步 XML 响应的便捷方法。to_user=消息接收者，from_user=消息发送者"""
    return build_text_xml(to_user, from_user, text), 200, {"Content-Type": "application/xml"}


def _with_backfill(openid: str, reply: str) -> str:
    """当前回复优先（放最前，用户先看到最新答案）；若之前有超时未送达的结果，
    在其后附上补发。这样既保证"当前消息不延迟一条"，又保证"超时结果不丢失"。
    """
    old = drain_ready(openid)
    if not old:
        return reply
    combined = f"{reply}\n\n———— 以下为之前超时的回复补发 ————\n\n{old}"
    return combined[:2000] if len(combined) > 2000 else combined


def parse_image_gen(content):
    """解析文生图指令，返回绘图 prompt；不是指令则返回 None。
    支持: 画：xxx / 画 xxx / 生成图片 xxx / 文生图 xxx / image xxx / draw xxx
    单字"画"需带冒号或空格，避免误伤"画面很好"这类普通句子。
    """
    content = (content or "").strip()
    if not content:
        return None
    for kw in ("生成图片", "文生图", "画图"):
        if content.startswith(kw):
            rest = content[len(kw):].strip(" \t:：").strip()
            if rest:
                return rest
    low = content.lower()
    for kw in ("image", "draw", "t2i"):
        if low.startswith(kw):
            rest = content[len(kw):].strip(" \t:：").strip()
            if rest:
                return rest
    if content.startswith("画"):
        rest = content[1:]
        if rest.startswith(("：", ":")):
            rest = rest[1:].strip()
        elif rest[:1].isspace():
            rest = rest.lstrip()
        else:
            return None
        return rest if rest else None
    return None


def _handle_image_gen(from_user, to_user, msg_id, prompt):
    """文生图：生成图片并上传微信拿 media_id，再回图片 XML（同步）或客服消息（异步）。"""
    def _process_gen():
        url = generate_image(prompt)
        if not url:
            return None
        img_bytes = fetch_image_bytes(url)
        if not img_bytes:
            logger.warning("生成图片下载失败: %s", url)
            return None
        return upload_temp_media("image", img_bytes, "gen.jpg")

    if _use_async():
        def _handle_gen_async():
            mid = _process_gen()
            if mid:
                send_customer_image(from_user, mid)
            else:
                send_customer_text(from_user, "图片生成失败，请重试。")
        _async(_handle_gen_async)
        return "success"

    # 同步模式（订阅号）：复用微信重试机制等待生成结果（文生图通常 10-30 秒）
    pending_key = msg_id or f"gen:{from_user}"
    status, mid = get_or_start_image(pending_key, from_user, _process_gen, wait=4.8)
    if status == "done":
        if mid:
            logger.info("图片生成完成: msg_id=%s, media_id=%s", msg_id, mid)
            return build_image_xml(from_user, to_user, mid), 200, {"Content-Type": "application/xml"}
        return _xml_reply(from_user, to_user, "图片生成失败，请重试。")
    else:
        time.sleep(0.3)  # 4.8 + 0.3 = 5.1 秒 > 5 秒，确保微信判定超时并重试
        logger.info("图片生成中，触发微信超时重试: msg_id=%s", msg_id)
        return ""


@bp.route("/wechat", methods=["GET", "POST"])
def handle():
    if request.method == "GET":
        sig = request.args.get("signature")
        ts = request.args.get("timestamp")
        nonce = request.args.get("nonce")
        echostr = request.args.get("echostr")
        return echostr if check_signature(sig, ts, nonce) else ("Forbidden", 403)

    try:
        tree = SafeET.fromstring(request.data)
        msg_type = tree.findtext("MsgType")
        from_user = tree.findtext("FromUserName", "")
        to_user = tree.findtext("ToUserName", "")
        msg_id = tree.findtext("MsgId")
        logger.info("收到消息: type=%s, msg_id=%s, user=%s", msg_type, msg_id, from_user[:8] if from_user else "")

        # 1. 关注事件
        if msg_type == "event" and tree.findtext("Event") == "subscribe":
            welcome = "你好！我是AI助手。\n发文字对话，发图片识别内容。\n回复 画：描述 可生成图片。"
            if _use_async():
                send_customer_text(from_user, welcome)
                return "success"
            return _xml_reply(from_user, to_user, welcome)

        # 2. 图片消息
        if msg_type == "image":
            pic_url = tree.findtext("PicUrl")
            media_id = tree.findtext("MediaId")

            def _process_img():
                """下载图片并调用视觉模型分析，写入会话历史，返回分析文本"""
                img_ref = pic_url
                if not img_ref and media_id:
                    content, ctype = download_media(media_id)
                    if content:
                        mime = (ctype or "image/jpeg").split(";")[0]
                        img_ref = f"data:{mime};base64,{base64.b64encode(content).decode()}"
                reply = vision(img_ref, IMAGE_PROMPT) if img_ref else "图片读取失败"
                reply = reply or "（模型未返回）"
                # 写入会话历史，让后续文字对话能引用图片内容
                session_store.add_message(from_user, "user", "[用户发送了一张图片]")
                session_store.add_message(from_user, "assistant", f"[图片分析]: {reply}")
                return reply

            # --- 异步模式（有客服权限）：后台分析 + 客服消息推送 ---
            if _use_async():
                def _handle_img_async():
                    reply = _process_img()
                    send_customer_text(from_user, reply)
                _async(_handle_img_async)
                return "success"

            # --- 同步模式（订阅号）：复用微信重试机制等待分析结果 ---
            # 与文本消息相同：第一次启动后台分析等 4.8 秒
            #   - 分析完成：立即返回 XML 结果给用户
            #   - 未完成：空响应触发微信超时重试，重试时返回分析结果
            pending_key = msg_id or f"img:{from_user}"
            status, reply = get_or_start(pending_key, from_user, _process_img, wait=4.8)

            if status == "done":
                logger.info("图片分析回复: %s", (reply or "")[:80])
                return _xml_reply(from_user, to_user, _with_backfill(from_user, reply))
            else:
                time.sleep(0.3)  # 4.8 + 0.3 = 5.1 秒 > 5 秒，确保微信判定超时并重试
                logger.info("图片分析中，触发微信超时重试: msg_id=%s", msg_id)
                return ""

        # 3. 文本消息（带上下文记忆）
        if msg_type == "text":
            content = tree.findtext("Content", "").strip()

            # 图片指令
            m = re.match(r"^图片\s*[:：]?\s*(https?://\S+)$", content)
            if m:
                return _xml_reply(from_user, to_user, "图片发送功能处理中...")

            # 交付上一轮超时未送达的生成图片（文生图超过重试窗口时的补发）
            overflow_mid = pop_pending_image(from_user)
            if overflow_mid:
                def _q_overflow():
                    h = session_store.get_history(from_user)
                    r = chat(content, history=h, timeout=30)
                    if len(r) > 2000:
                        r = r[:2000] + "\n...(已截断)"
                    session_store.add_message(from_user, "user", content)
                    session_store.add_message(from_user, "assistant", r)
                    return r
                process_later(from_user, _q_overflow)  # 当前文字转入后台，其回复下次补发
                return build_image_xml(from_user, to_user, overflow_mid), 200, {"Content-Type": "application/xml"}

            # 文生图指令：画：xxx / 生成图片 xxx / image xxx ...
            gen_prompt = parse_image_gen(content)
            if gen_prompt:
                logger.info("文生图指令: %s", gen_prompt[:60])
                return _handle_image_gen(from_user, to_user, msg_id, gen_prompt)

            # --- 异步模式（有客服权限）：后台处理 + 客服消息推送 ---
            if _use_async():
                def _handle_text_async():
                    history = session_store.get_history(from_user)
                    reply = chat(content, history=history, timeout=30)
                    if len(reply) > 2000:
                        reply = reply[:2000] + "\n...(已截断)"
                    session_store.add_message(from_user, "user", content)
                    session_store.add_message(from_user, "assistant", reply)
                    send_customer_text(from_user, reply)

                _async(_handle_text_async)
                return "success"

            # --- 同步模式（订阅号）：利用微信重试机制争取更多处理时间 ---
            # 第一次收到：启动后台 AI 处理，等 4.8 秒
            #   - AI 完成：立即返回 XML 回复
            #   - AI 未完成：返回空（5.1秒），微信 5 秒超时后会重试
            # 重试收到同一 MsgId：检查后台是否完成
            #   - 完成：返回 XML 回复
            #   - 未完成：再次返回空，等下次重试
            # 最多可争取 ~20 秒处理时间（首次 + 3 次重试 × 5 秒）
            def _process_text():
                history = session_store.get_history(from_user)
                reply = chat(content, history=history, timeout=30)
                if len(reply) > 2000:
                    reply = reply[:2000] + "\n...(已截断)"
                session_store.add_message(from_user, "user", content)
                session_store.add_message(from_user, "assistant", reply)
                return reply

            pending_key = msg_id or f"{from_user}:{content}"
            # wait=4.8 接近微信 5 秒限制，给 AI 尽量多的首次完成时间
            status, reply = get_or_start(pending_key, from_user, _process_text, wait=4.8)

            if status == "done":
                logger.info("AI回复: %s", (reply or "")[:80])
                # 当前回复优先；若上一条超时未送达，补发拼接在前面
                return _xml_reply(from_user, to_user, _with_backfill(from_user, reply))
            else:
                # 关键修复：必须让请求超过微信 5 秒限制才会触发重试
                # 微信文档明确：返回 success 或空字符串"不会重试"，只有 5 秒超时不响应才重试
                # 因此 sleep 确保请求总时长 > 5 秒，微信判定超时后会重试
                # 重试时 get_or_start 会从 pending 取出后台已完成的结果
                time.sleep(0.3)  # 4.8 + 0.3 = 5.1 秒 > 5 秒，确保微信判定超时
                logger.info("AI处理中，触发微信超时重试: msg_id=%s", msg_id)
                return ""

    except Exception:
        logger.exception("处理微信消息出错")
        return "success"
    logger.warning("未匹配的消息类型: %s", msg_type)
    return "success"