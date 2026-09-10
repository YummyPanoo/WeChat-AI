import json
import requests, logging, time
from typing import List, Optional
from config.settings import Config

logger = logging.getLogger("wechat-robot")

# 复用 TCP 连接，减少每次请求的握手开销
_session = requests.Session()

# 系统提示词：声明机器人具备文生图能力，避免 AI 在对话中否认自己会画画。
# 实际的绘画事件（用户指令 + 生成结果）由路由层写入会话历史，AI 据此回答"你画了什么"。
SYSTEM_PROMPT = (
    "你是微信公众号里的智能助手。除了文字聊天，你还具备两个能力："
    "1) 分析用户发送的图片；"
    "2) 根据用户描述生成图片（用户说\"画：xxx\"等指令时，系统会为你调用绘图功能，"
    "绘画记录会出现在对话历史中）。"
    "3) 查询用户的订阅/订购信息（当用户询问订单、订阅、配额、剩余次数等问题时，"
    "系统会帮你从数据库中读取用户的订购状态，包括文生图剩余免费次数、订阅到期时间、历史订单等。"
    "注意：你只能读取用户信息，不能修改任何数据）。"
    "请用简洁自然的中文回复。"
)


def chat(prompt: str, history: Optional[List[dict]] = None, timeout: int = 120) -> str:
    """文本对话，支持多轮上下文。
    history: 历史消息列表 [{"role":..., "content":...}, ...]
    timeout: 超时秒数（异步模式建议 30s，同步模式建议 4-5s 以适应微信 5 秒限制）
    """
    if not Config.DASHSCOPE_API_KEY:
        return "AI服务未配置"
    try:
        messages = list(history or [])
        # 会话历史里不含 system 消息，每次请求时注入能力声明
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
        messages.append({"role": "user", "content": prompt})
        start = time.time()
        resp = _session.post(
            Config.DASHSCOPE_API_URL,
            headers={"Authorization": f"Bearer {Config.DASHSCOPE_API_KEY}", "Content-Type": "application/json"},
            json={"model": Config.QWEN_TEXT_MODEL, "messages": messages, "max_tokens": 1024},
            timeout=timeout,
        )
        resp.raise_for_status()
        elapsed = time.time() - start
        content = resp.json()["choices"][0]["message"]["content"]
        logger.info("AI请求耗时 %.2fs, 模型=%s", elapsed, Config.QWEN_TEXT_MODEL)
        return content
    except requests.exceptions.Timeout:
        logger.warning("AI请求超时: 模型=%s, timeout=%ds", Config.QWEN_TEXT_MODEL, timeout)
        return "AI思考时间过长，请稍后再试。"
    except Exception:
        logger.exception("Qwen文本API异常")
        return "AI服务暂时不可用"

def vision(image_url: str, prompt: str, timeout=120) -> str:
    if not Config.DASHSCOPE_API_KEY:
        return "AI服务未配置"
    try:
        resp = requests.post(
            Config.DASHSCOPE_API_URL,
            headers={"Authorization": f"Bearer {Config.DASHSCOPE_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": Config.QWEN_VL_MODEL,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": prompt}
                ]}],
                "max_tokens": 800
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "图片分析超时"
    except Exception:
        logger.exception("Qwen VL API异常")
        return "图片分析服务不可用"


def classify_draw_intent(text: str, timeout: int = 5) -> Optional[str]:
    """轻量 LLM 意图判断：用户这句话是不是在要一张图。
    是则返回清理后的绘图 prompt；否则返回 None（含调用失败）。
    仅当正则拿不准时调用；耗时一般 < 300ms（qwen-turbo）。
    """
    if not Config.DASHSCOPE_API_KEY:
        return None
    system = (
        "你是微信消息意图分类器，判断用户这句话是否想要生成/绘制一张图片。"
        "注意：'看图片/评价图片/问图片内容/问怎么画画/讲一幅画'等只是看图，不是绘图请求；"
        "'画面很美/画展好看'等是评价，也不是绘图请求。"
        "若是绘图请求，提取核心绘图描述（去掉'给我''帮我''请''画一张'等指令词和量词），"
        "只输出 JSON：{\"draw\": true, \"prompt\": \"核心描述\"}；否则 {\"draw\": false}。不要输出任何其他内容。"
    )
    try:
        resp = _session.post(
            Config.DASHSCOPE_API_URL,
            headers={"Authorization": f"Bearer {Config.DASHSCOPE_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": Config.QWEN_CLASSIFY_MODEL,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
                "max_tokens": 40,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        if data.get("draw"):
            prompt = (data.get("prompt") or "").strip()
            return prompt or None
        return None
    except Exception:
        logger.warning("绘图意图分类失败（按非绘图处理）: %s", (text or "")[:30])
        return None


def generate_image(prompt: str, size: str = "1024*1024", timeout: int = 120):
    """文生图（ModelScope 魔搭 API-Inference），返回生成图片的 URL；失败返回 None。

    流程：POST /v1/images/generations 提交异步任务拿 task_id，
    再轮询 GET /v1/tasks/{task_id} 直到 task_status == "SUCCEED"。
    """
    if not Config.MODELSCOPE_API_KEY:
        logger.warning("MODELSCOPE_API_KEY 未配置")
        return None
    key = Config.MODELSCOPE_API_KEY
    base = "https://api-inference.modelscope.cn"
    try:
        # 1. 提交异步任务
        resp = _session.post(
            f"{base}/v1/images/generations",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-ModelScope-Async-Mode": "true",
            },
            json={"model": Config.MODELSCOPE_T2I_MODEL, "prompt": prompt},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        task_id = data.get("task_id")
        if not task_id:
            logger.warning("ModelScope 文生图提交失败: %s", data)
            return None
        logger.info("ModelScope 文生图任务已提交: task_id=%s", task_id)
        # 2. 轮询任务状态
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            pr = _session.get(
                f"{base}/v1/tasks/{task_id}",
                headers={
                    "Authorization": f"Bearer {key}",
                    "X-ModelScope-Task-Type": "image_generation",
                },
                timeout=timeout,
            )
            pr.raise_for_status()
            p = pr.json()
            status = p.get("task_status")
            if status == "SUCCEED":
                images = p.get("output_images") or []
                if images:
                    logger.info("ModelScope 文生图完成: %s", str(images[0])[:80])
                    return images[0]
                logger.warning("ModelScope 文生图成功但无图片: %s", p)
                return None
            if status == "FAILED":
                logger.warning("ModelScope 文生图失败: %s", p)
                return None
            # PENDING / RUNNING 继续轮询
        logger.warning("ModelScope 文生图轮询超时: task_id=%s", task_id)
        return None
    except requests.exceptions.Timeout:
        logger.warning("ModelScope 文生图请求超时")
        return None
    except Exception:
        logger.exception("ModelScope 文生图异常")
        return None


# ==================== 图生图（风格转换） ====================
# 风格预设：name 用于菜单展示，prompt 为喂给绘图模型的英文风格描述
IMAGE_STYLES = {
    "1": {"name": "漫画风", "prompt": "Japanese anime comic style, clean bold outlines, vibrant flat colors, cel shading, manga aesthetic"},
    "2": {"name": "线条风", "prompt": "minimalist black and white line art, clean ink sketch, fine contour lines, no color fill, white background"},
    "3": {"name": "真实风", "prompt": "hyper-realistic photography style, natural lighting, true-to-life colors, high detail, 8k photo quality"},
    "4": {"name": "水彩风", "prompt": "soft watercolor painting, gentle color bleeding, delicate brush strokes, artistic paper texture"},
    "5": {"name": "油画风", "prompt": "classical oil painting, thick impasto brush strokes, rich warm palette, canvas texture"},
    "6": {"name": "像素风", "prompt": "retro pixel art, 16-bit video game style, crisp square pixels, limited vibrant palette"},
    "7": {"name": "赛博朋克", "prompt": "cyberpunk style, neon glow, futuristic high-tech atmosphere, cyan and magenta lighting, dark tones"},
}

# 中文关键词 → 风格编号，方便用户直接说"水彩风"而不必记数字
_STYLE_KEYWORDS = {
    "漫画": "1", "动漫": "1", "二次元": "1", "卡通": "1",
    "线条": "2", "线稿": "2", "素描": "2", "简笔画": "2",
    "真实": "3", "写实": "3", "摄影": "3", "实拍": "3",
    "水彩": "4",
    "油画": "5",
    "像素": "6",
    "赛博": "7", "朋克": "7", "科幻": "7", "霓虹": "7",
}


def parse_style(text: str) -> str:
    """把用户回复解析成风格编号；无法识别返回空串。"""
    t = (text or "").strip()
    if t in IMAGE_STYLES:
        return t
    for kw, code in _STYLE_KEYWORDS.items():
        if kw in t:
            return code
    return ""


def style_menu_text() -> str:
    """生成风格选择菜单文本。"""
    lines = ["🎨 请选择图生图风格（回复数字或风格名）：", ""]
    for code in sorted(IMAGE_STYLES.keys()):
        lines.append(f"{code}. {IMAGE_STYLES[code]['name']}")
    lines.append("")
    lines.append("回复「取消」放弃本次图生图。")
    return "\n".join(lines)


def _build_i2i_prompt(style_code: str, base_desc: str = "") -> str:
    """拼接图生图提示词：保留原图主体构图 + 目标风格化。"""
    style = IMAGE_STYLES.get(style_code) or IMAGE_STYLES["1"]
    parts = [
        f"Redraw the input image in {style['prompt']}.",
        "Keep the original subject, composition and layout recognizable.",
        "High quality, detailed, well composed.",
    ]
    if base_desc:
        parts.append(f"Original image content: {base_desc[:300]}")
    return " ".join(parts)



def generate_image_from_image(image_url: str, style_code: str, timeout: int = 150):
    """图生图（ModelScope API-Inference 图像编辑），返回生成图片 URL；失败返回 None。

    image_url: 原图的 base64 data URL（data:image/jpeg;base64,...）或公网 URL。
    与文生图共用异步任务接口，额外把原图作为参考图一起提交。
    """
    if not Config.MODELSCOPE_API_KEY:
        logger.warning("MODELSCOPE_API_KEY 未配置，无法图生图")
        return None
    key = Config.MODELSCOPE_API_KEY
    base = "https://api-inference.modelscope.cn"
    model = Config.MODELSCOPE_I2I_MODEL
    prompt = _build_i2i_prompt(style_code)
    style_name = (IMAGE_STYLES.get(style_code) or {}).get("name", style_code)

    # 不同图像编辑模型对"参考图"字段命名不统一，按顺序尝试已知写法
    payloads = [
        {"model": model, "prompt": prompt, "image_url": image_url},
        {"model": model, "prompt": prompt, "image": image_url},
        {"model": model, "prompt": prompt, "input_image": image_url},
        {"model": model, "prompt": prompt, "image_urls": [image_url]},
    ]
    logger.info("提交图生图任务: model=%s, style=%s(%s), 候选payload=%d",
                model, style_code, style_name, len(payloads))

    for idx, payload in enumerate(payloads, 1):
        try:
            resp = _session.post(
                f"{base}/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "X-ModelScope-Async-Mode": "true",
                },
                json=payload,
                timeout=60,
            )
            if resp.status_code >= 400:
                logger.warning("图生图 payload#%d 被拒绝: HTTP %d, %s",
                               idx, resp.status_code, resp.text[:200])
                continue
            data = resp.json()
            task_id = data.get("task_id")
            if not task_id:
                logger.warning("图生图 payload#%d 未返回 task_id: %s", idx, str(data)[:200])
                continue
            logger.info("图生图任务已提交: task_id=%s, payload#%d", task_id, idx)
            url = _poll_image_task(base, key, task_id, timeout)
            if url:
                logger.info("图生图完成: style=%s, url=%s", style_name, str(url)[:80])
            else:
                logger.warning("图生图任务未产出图片: style=%s, task_id=%s", style_name, task_id)
            # 任务已被服务端接受，再换 payload 重试意义不大，直接返回结果
            return url
        except requests.exceptions.Timeout:
            logger.warning("图生图 payload#%d 提交超时", idx)
        except Exception:
            logger.exception("图生图 payload#%d 异常", idx)
    logger.warning("图生图全部 payload 变体均失败: model=%s, style=%s", model, style_name)
    return None


def _poll_image_task(base: str, key: str, task_id: str, timeout: int):
    """轮询 ModelScope 图像任务直到成功/失败/超时，返回图片 URL 或 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        try:
            pr = _session.get(
                f"{base}/v1/tasks/{task_id}",
                headers={
                    "Authorization": f"Bearer {key}",
                    "X-ModelScope-Task-Type": "image_generation",
                },
                timeout=30,
            )
            pr.raise_for_status()
            p = pr.json()
        except Exception:
            logger.warning("图生图轮询异常，继续重试: task_id=%s", task_id)
            continue
        status = p.get("task_status")
        if status == "SUCCEED":
            images = p.get("output_images") or []
            if images:
                return images[0]
            logger.warning("图生图任务成功但无图片: %s", str(p)[:200])
            return None
        if status in ("FAILED", "CANCELED"):
            logger.warning("图生图任务失败: status=%s, %s", status, str(p)[:200])
            return None
        # PENDING / RUNNING 继续轮询
    logger.warning("图生图轮询超时: task_id=%s", task_id)
    return None
