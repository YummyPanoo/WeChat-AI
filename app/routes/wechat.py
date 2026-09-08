import re, time, threading, logging, base64, requests
from typing import Optional
from flask import Blueprint, request
from defusedxml import ElementTree as SafeET
from config.settings import Config
from app.services.auth import check_signature
from app.services.ai_client import chat, vision, generate_image, classify_draw_intent
from app.services.message import send_customer_text, send_customer_image
from app.services.pending import get_or_start, drain_ready, get_or_start_image, pop_pending_image, process_later
from app.services.media import download_media, upload_temp_media, fetch_image_bytes
from app.services.session import session_store
from app.services.user_info import get_user_full_info, format_user_info_for_ai, get_user_by_openid
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


import re

# 紧跟“画”之后的这些汉字多为非绘画意图的词头（画面/画作/画廊/画展/画师/画家…）
_NON_DRAW_HEAD = set("面作廊展师家稿纸板框皮眉眼册报质法理卷轴画像画眉商家风工屏境坛舫架局谜")
# 成语（画龙点睛等修辞用法，不是真的让人画画）
_CHENGYU = ("画龙点睛", "画蛇添足", "画饼充饥", "画地为牢", "画虎类犬", "画影图形")
# 明显是“看图/评图/问图”而不是“要生成图”的查询词（命中直接返回 None，不调 LLM）
_LOOK_WORDS = ("看看", "看下", "看一下", "讲讲", "讲一下", "介绍", "识别", "分析", "解读",
               "是什么", "是什么意思", "啥意思", "评价", "评论", "描述一下", "怎么画", "如何",
               "怎么样", "咋样", "画的")
# 弱信号词：文本含这些词但拿不准 → 调用轻量 LLM 判断绘图意图（命中且非反意图词）
_WEAK_HINTS = ("画", "图", "图片", "照片", "壁纸", "插画", "插图", "生成", "绘制", "素描", "卡通",
               "画像", "图画", "图像", "美图", "图案", "背景", "头像", "漫画", "一幅", "一张")
# 强指令前缀（命中直接判定为绘图请求，不调 LLM）
_STRONG_PREFIX = ("生成图片", "文生图", "画图", "绘画", "作画")
_STRONG_EN = ("image", "draw", "t2i")
# ---- 绘图意图分类结果缓存（同一文本 5 分钟只调一次 LLM）----
_CLASSIFY_TTL = 300
_classify_cache = {}
_classify_lock = threading.Lock()


def _classify_cached(text: str) -> Optional[str]:
    """带缓存的 LLM 绘图意图判断；返回清理后的 prompt 或 None"""
    now = time.time()
    with _classify_lock:
        hit = _classify_cache.get(text)
        if hit and now - hit[1] <= _CLASSIFY_TTL:
            return hit[0]
    try:
        prompt = classify_draw_intent(text)
    except Exception:
        # classify_draw_intent 内部已捕获异常，这里再兜底一层，保证消息主流程绝不因分类失败而中断
        logger.warning("绘图意图分类异常（按非绘图处理）: %s", (text or "")[:30])
        return None
    with _classify_lock:
        _classify_cache[text] = (prompt, time.time())
        # 控制缓存大小：超 500 条清理最旧一半
        if len(_classify_cache) > 500:
            stale = sorted(_classify_cache.items(), key=lambda kv: kv[1][1])[:250]
            for k, _ in stale:
                _classify_cache.pop(k, None)
    if prompt:
        return _clean_prompt(prompt) or None
    return None


def _clean_prompt(text):
    """清理 prompt 开头的填充词与量词，尽量把语义核心留给绘图模型"""
    text = (text or "").strip(" \t:：,，、。.？?！!；;()（）")
    # 去掉开头的礼貌/引导/动词填充词（可连续剥多层，如“帮我做一张xxx”→“xxx”）
    # (?!的) 保护：若剥完剩“的…”开头（如“你的样子”），说明误剥，回退不剥
    text = re.sub(r"^(?:那就|就帮我|麻烦|帮我|给我|请你|你|我想|我要|我想要|来|请|那|就|"
                  r"出(?:个|张)?|做(?:一?[张幅个])?|整(?:一?[个张])?|搞(?:一?[个张])?)+(?!的)", "", text)
    # 量词短语整体剥掉（一张/一只/一头/一个…），只留语义核心
    text = re.sub(r"^(?:一张|一个|一只|一头|一条|一匹|一幅|一朵|一棵|一株|一座|一辆|一艘|"
                  r"一颗|一块|一对|一双|一组|一群|两张|两个|几只|几个|一下|一|个|只|张|幅|"
                  r"头|条|匹|朵|棵|株|座|辆|艘|颗|块|对|双|组|群|下)?", "", text)
    return text.strip()


def parse_image_gen(content):
    """解析文生图指令，返回绘图 prompt；不是指令则返回 None。

    支持的触发方式（更自由，不再要求必须带冒号/空格）：
      - 强指令: 生成图片 xxx / 文生图 xxx / 画图 xxx / image xxx / draw xxx / t2i xxx
      - 画 + 直接内容: 画一只小猪 / 画太阳 / 画个彩虹 / 画：xxx / 画 xxx
      - 动词引导: 给我画一只狗 / 帮我生成一个彩虹 / 来画只猫 / 我想画一条龙
      - 求图句式: 给我一张小狗在吃饭的图片 / 来一张夕阳的照片 / 我要一张可爱的猫咪插画
    为避免误伤，含“看看/识别/分析/是什么”等查看类词的一律不触发。
    """
    content = (content or "").strip()
    if not content:
        return None
    # 0) 语义过滤：明显是“看图/评图/问图”而不是“要生成图” → 不触发
    if any(w in content for w in _LOOK_WORDS):
        return None

    # 1) 强指令前缀（保留原有能力）
    for kw in ("生成图片", "文生图", "画图"):
        if content.startswith(kw):
            rest = content[len(kw):].strip(" \t:：").strip()
            return _clean_prompt(rest) if rest else None
    low = content.lower()
    for kw in ("image", "draw", "t2i"):
        if low.startswith(kw):
            rest = content[len(kw):].strip(" \t:：").strip()
            return _clean_prompt(rest) if rest else None

    # 2) “画”直接开头：画：xx / 画 xx / 画一只小猪 / 画太阳
    if content.startswith("画"):
        if any(content.startswith(c) for c in _CHENGYU):
            return None  # 画龙点睛等成语 → 不是绘画指令
        if len(content) > 1 and content[1] in ("：", ":"):
            return _clean_prompt(content[2:]) or None
        if len(content) > 1 and content[1].isspace():
            return _clean_prompt(content[2:]) or None
        if len(content) > 1 and content[1] not in _NON_DRAW_HEAD:
            # 画猫咪 / 画夜晚的城市  —— 只要不是“画面/画作…”这类词头就可触发
            return _clean_prompt(content[1:]) or None
        return None  # 画面/画作/画展… → 普通聊天

    # 3) 动词引导 + 画/生成/制作 等动作词
    m = re.match(r"^(?:给我|帮我|帮|来|要|请|我想|我要|我想要|想要|麻烦|能不能|我来|求你|请你|给我来(?=[画生])|我)"
                 r"(?P<body>[^，。!？!?；;]{0,14})$", content)
    if m:
        body = m.group("body")
        for kw in ("画", "生成", "制作", "绘制", "做出来", "出图"):
            idx = body.find(kw)
            if idx >= 0:
                rest = body[idx + len(kw):]
                # 防止“帮我看看这幅画面”“我今天画了一下午”这类“画了/画+名词”叙述
                if not rest or rest[0] in _NON_DRAW_HEAD or rest.startswith("了"):
                    continue
                cleaned = _clean_prompt(rest)
                if cleaned:
                    return cleaned
        # 3.5) “给我一张图”这类（有量词没画字）落入句式4

    # 4) “给我一张 xxx (的) 图片/照片/图” 句式
    m = re.match(
        r"^(?:给我来|请给我|帮我出|给我|帮我|来张|来|要|我要|我想要|想要|请|求|帮我生成|生成|出)"
        r"(?:一张|一|个|张|幅|份|组|两)?(?P<desc>[^。，！!？?；;]{1,40}?)"
        r"(?:的)?(?:图片|照片|美图|插画|壁纸|图画|图像|卡通图|图)$",
        content, re.I)
    if m:
        desc = m.group("desc").rstrip("的个张").strip()
        if desc:
            return _clean_prompt(desc) or None

    # ===== 正则全失败：用轻量 LLM 做意图判断（有弱信号词时才调用） =====
    # 单个字符（如只发一个“图”/“画”）不足以构成绘图请求，直接返回，避免无谓的 LLM 调用
    if len(content) < 2:
        return None
    # 如果文本不含任何绘画相关词汇，说明跟画图无关，直接返回 None
    if not any(w in content for w in _WEAK_HINTS):
        return None
    return _classify_cached(content) or None


def _query_user_subscription(username: str) -> str:
    """
    查询用户订阅信息（只读）。
    供公众号AI在对话中查询用户使用。
    注意：此函数仅读取数据，不做任何修改。
    """
    try:
        info = get_user_full_info(username)
        if not info:
            return f"未找到用户「{username}」的注册信息。该用户可能尚未注册。"
        return format_user_info_for_ai(info)
    except Exception as e:
        logger.warning("查询用户订阅信息失败: %s", str(e))
        return f"查询用户信息时出错，请稍后重试。"


def _call_wechat_register_bind_api(token: str, openid: str) -> dict:
    """
    调用 WeChatRegister 的绑定接口，将微信号与用户账号绑定。
    返回 {"ok": bool, "msg": str}
    """
    try:
        # WeChatRegister 后端地址（从前端地址推导，端口5000）
        register_url = Config.WECHAT_REGISTER_FRONTEND_URL
        # 将前端端口5173替换为后端端口5000
        backend_url = register_url.replace(":5173", ":5000").rstrip("/")
        api_url = f"{backend_url}/api/wechat/bind"

        resp = requests.post(api_url, json={"token": token, "openid": openid}, timeout=10)
        data = resp.json()
        return data
    except Exception as e:
        logger.warning("调用WeChatRegister绑定接口失败: %s", str(e))
        return {"ok": False, "msg": "绑定服务暂时不可用"}


def _get_user_info_by_openid(openid: str) -> Optional[dict]:
    """
    通过openid查询用户信息（直接读数据库，只读）。
    """
    try:
        return get_user_by_openid(openid)
    except Exception as e:
        logger.warning("通过openid查询用户失败: %s", str(e))
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

    def _gen_and_record():
        # 在后台线程执行；processor 每个 msg_id 只跑一次，重试不会重复记录
        mid = _process_gen()
        # 把绘画事件写入会话历史，让用户后续问"你刚刚画了什么"时 AI 有上下文
        session_store.add_message(from_user, "user", f"画：{prompt}")
        if mid:
            session_store.add_message(
                from_user, "assistant",
                f"好的，我已根据「{prompt}」生成了一张图片并发送给你。")
        else:
            session_store.add_message(
                from_user, "assistant",
                f"抱歉，「{prompt}」的图片生成失败了，请稍后重试。")
        return mid

    if _use_async():
        def _handle_gen_async():
            mid = _gen_and_record()
            if mid:
                send_customer_image(from_user, mid)
            else:
                send_customer_text(from_user, "图片生成失败，请重试。")
        _async(_handle_gen_async)
        return "success"

    # 同步模式（订阅号）：复用微信重试机制等待生成结果（文生图通常 10-30 秒）
    pending_key = msg_id or f"gen:{from_user}"
    status, mid = get_or_start_image(pending_key, from_user, _gen_and_record, wait=4.8)
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
            # 检查该微信号是否已绑定用户账号
            bound_user = _get_user_info_by_openid(from_user)
            register_url = Config.WECHAT_REGISTER_FRONTEND_URL

            if bound_user:
                # 已绑定：显示用户信息和订阅状态
                info = get_user_full_info(bound_user["username"])
                if info:
                    sub_info = format_user_info_for_ai(info)
                    welcome = (
                        f"🎉 欢迎回来，{bound_user['username']}！\n\n"
                        f"📋 您的订阅信息：\n{sub_info}\n\n"
                        f"【使用指南】\n"
                        f"• 直接发文字与我对话\n"
                        f"• 发图片可识别分析内容\n"
                        f"• 回复 画：描述 可生成图片\n"
                        f"• 回复 我的订单 可查看订购状态"
                    )
                else:
                    welcome = (
                        f"🎉 欢迎回来，{bound_user['username']}！\n\n"
                        f"👉 管理您的服务：{register_url}\n\n"
                        f"【使用指南】\n"
                        f"• 直接发文字与我对话\n"
                        f"• 发图片可识别分析内容\n"
                        f"• 回复 画：描述 可生成图片"
                    )
            else:
                # 未绑定：显示欢迎和注册引导
                welcome = (
                    "🎉 欢迎关注AI助手！\n\n"
                    "【服务配额说明】\n"
                    "✅ 文字对话：永久免费\n"
                    "✅ 图片分析：注册后免费30天\n"
                    "✅ 文生图：注册即送20次免费次数\n"
                    "   （用完后可订阅30元/月）\n\n"
                    f"👉 注册/登录/购买请访问：{register_url}\n\n"
                    "💡 注册后请扫码绑定微信号，即可同步您的购买信息\n\n"
                    "【使用指南】\n"
                    "• 直接发文字与我对话\n"
                    "• 发图片可识别分析内容\n"
                    "• 回复 画：描述 可生成图片\n"
                    "• 回复 我的订单 可查看订购状态"
                )
            if _use_async():
                send_customer_text(from_user, welcome)
                return "success"
            return _xml_reply(from_user, to_user, welcome)

        # 1.5 扫码事件（用户扫描绑定二维码）
        if msg_type == "event" and tree.findtext("Event") == "scan":
            event_key = tree.findtext("EventKey", "")
            # 扫码绑定：EventKey 以 "bind_" 开头，后面是 token
            if event_key.startswith("bind_"):
                token = event_key[5:]  # 去掉 "bind_" 前缀
                result = _call_wechat_register_bind_api(token, from_user)
                if result.get("ok"):
                    reply = "✅ 微信号绑定成功！\n\n您的公众号账号已与网站账号关联，现在可以同步查看您的订阅信息。\n\n回复「我的订单」查看订购状态。"
                else:
                    reply = f"❌ 绑定失败：{result.get('msg', '请重试')}\n\n请重新在网页端生成绑定二维码。"
                if _use_async():
                    send_customer_text(from_user, reply)
                    return "success"
                return _xml_reply(from_user, to_user, reply)
            # 其他扫码场景（非绑定）
            return "success"

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

            # 微信绑定码（6位数字）
            if re.match(r"^\d{6}$", content):
                # 用户发送了6位数字，尝试作为绑定码处理
                result = _call_wechat_register_bind_api(content, from_user)
                if result.get("ok"):
                    reply = f"✅ 微信号绑定成功！\n\n您的公众号账号已与网站账号「{result.get('username', '')}」关联。\n\n回复「我的订单」查看订购状态。"
                else:
                    reply = f"❌ 绑定失败：{result.get('msg', '请重试')}\n\n请重新在网页端生成绑定码。"
                if _use_async():
                    send_customer_text(from_user, reply)
                    return "success"
                return _xml_reply(from_user, to_user, reply)

            # 用户查询订购状态（只读，不修改任何数据）
            _order_query_patterns = (
                r"^(我的)?(订单|订阅|配额|余额|剩余|服务状态|订购状态|会员状态)$",
                r"^(查询|查看|我的)(订单|订阅|配额|服务)$",
                r"^(我还有|剩余).{0,4}(次数|免费|额度)$",
                r"^我的(订购|服务|账号)信息$",
            )
            is_order_query = any(re.match(p, content) for p in _order_query_patterns)
            if is_order_query:
                # 检查是否已绑定微信号
                bound_user = _get_user_info_by_openid(from_user)
                if bound_user:
                    # 已绑定：显示用户信息和订阅状态
                    info = get_user_full_info(bound_user["username"])
                    if info:
                        sub_info = format_user_info_for_ai(info)
                        reply = f"📋 您的订阅信息：\n\n{sub_info}"
                    else:
                        reply = "查询订阅信息失败，请稍后重试。"
                else:
                    # 未绑定：引导用户绑定
                    register_url = Config.WECHAT_REGISTER_FRONTEND_URL
                    reply = (
                        "📋 订购信息查询\n\n"
                        "您还未绑定微信号，无法直接查询。\n\n"
                        f"👉 请登录网页端生成绑定码：{register_url}\n\n"
                        "然后将6位绑定码发送给公众号即可完成绑定。"
                    )
                if _use_async():
                    send_customer_text(from_user, reply)
                    return "success"
                return _xml_reply(from_user, to_user, reply)

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

            # 查询指定用户的订阅信息（管理员/授权用途，只读）
            # 格式：查询用户xxx的订单 / 查看用户xxx的订阅
            _admin_query_match = re.match(
                r"^(?:查询|查看|查看用户|查询用户)\s*(.+?)\s*(?:的)?\s*(?:订单|订阅|配额|信息|服务状态)$",
                content
            )
            if _admin_query_match:
                target_username = _admin_query_match.group(1).strip()
                logger.info("查询用户订阅信息: username=%s", target_username)
                reply = _query_user_subscription(target_username)
                if _use_async():
                    send_customer_text(from_user, reply)
                    return "success"
                return _xml_reply(from_user, to_user, reply)

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