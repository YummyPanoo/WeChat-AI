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