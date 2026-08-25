import re, threading, logging, base64
from flask import Blueprint, request
from defusedxml import ElementTree as SafeET
from config.settings import Config
from app.services.auth import check_signature
from app.services.ai_client import chat, vision
from app.services.message import is_duplicate, send_customer_text
from app.services.media import download_media, upload_temp_media
from app.utils.xml_helper import build_text_xml, build_image_xml

bp = Blueprint("wechat", __name__)
logger = logging.getLogger("wechat-robot")

IMAGE_PROMPT = "请简要描述图片内容，提取文字并给出解读。用简洁中文回复。"


def _async(func, *args):
    threading.Thread(target=func, args=args, daemon=True).start()


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

        # 1. 关注事件
        if msg_type == "event" and tree.findtext("Event") == "subscribe":
            welcome = "你好！我是AI助手。\n发文字对话，发图片识别内容。"
            if Config.WECHAT_ASYNC_MODE:
                send_customer_text(from_user, welcome)
                return "success"
            return build_text_xml(from_user, to_user, welcome), 200, {"Content-Type": "application/xml"}

        # 2. 图片消息
        if msg_type == "image":
            if is_duplicate(msg_id): return "success"
            pic_url = tree.findtext("PicUrl")
            media_id = tree.findtext("MediaId")

            def _handle_img():
                img_ref = pic_url
                if not img_ref and media_id:
                    content, ctype = download_media(media_id)
                    if content:
                        mime = (ctype or "image/jpeg").split(";")[0]
                        img_ref = f"data:{mime};base64,{base64.b64encode(content).decode()}"
                reply = vision(img_ref, IMAGE_PROMPT) if img_ref else "图片读取失败"
                send_customer_text(from_user, reply or "（模型未返回）")

            if Config.WECHAT_ASYNC_MODE:
                _async(_handle_img)
                return "success"
            # 同步模式简化版（实际建议图片走异步）
            return build_text_xml(from_user, to_user, "图片处理中..."), 200, {"Content-Type": "application/xml"}

        # 3. 文本消息
        if msg_type == "text":
            content = tree.findtext("Content", "").strip()
            if is_duplicate(msg_id): return "success"

            # 图片指令
            m = re.match(r"^图片\s*[:：]?\s*(https?://\S+)$", content)
            if m:
                # 此处省略同步/异步图片发送逻辑，参考原代码 handle_image_send
                return build_text_xml(from_user, to_user, "图片发送功能处理中..."), 200, {
                    "Content-Type": "application/xml"}

            # AI 对话
            def _handle_text():
                reply = chat(content)
                if len(reply) > 2000: reply = reply[:2000] + "\n...(已截断)"
                send_customer_text(from_user, reply)

            if Config.WECHAT_ASYNC_MODE:
                _async(_handle_text)
                return "success"

            reply = chat(content)
            return build_text_xml(from_user, to_user, reply), 200, {"Content-Type": "application/xml"}

    except Exception:
        logger.exception("处理微信消息出错")
    return "success"