import time

def escape_cdata(text: str) -> str:
    """防止 CDATA 截断"""
    return (text or "").replace("]]>", "]]]]><![CDATA[>")

def build_text_xml(to_user: str, from_user: str, content: str) -> str:
    ts = int(time.time())
    c = escape_cdata(content)
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{escape_cdata(to_user)}]]></ToUserName>"
        f"<FromUserName><![CDATA[{escape_cdata(from_user)}]]></FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{c}]]></Content>"
        "</xml>"
    )

def build_image_xml(to_user: str, from_user: str, media_id: str) -> str:
    ts = int(time.time())
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{escape_cdata(to_user)}]]></ToUserName>"
        f"<FromUserName><![CDATA[{escape_cdata(from_user)}]]></FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        "<MsgType><![CDATA[image]]></MsgType>"
        f"<Image><MediaId><![CDATA[{escape_cdata(media_id)}]]></MediaId></Image>"
        "</xml>"
    )