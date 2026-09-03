import requests, logging, time
from typing import List, Optional
from config.settings import Config

logger = logging.getLogger("wechat-robot")

# 复用 TCP 连接，减少每次请求的握手开销
_session = requests.Session()


def chat(prompt: str, history: Optional[List[dict]] = None, timeout: int = 120) -> str:
    """文本对话，支持多轮上下文。
    history: 历史消息列表 [{"role":..., "content":...}, ...]
    timeout: 超时秒数（异步模式建议 30s，同步模式建议 4-5s 以适应微信 5 秒限制）
    """
    if not Config.DASHSCOPE_API_KEY:
        return "AI服务未配置"
    try:
        messages = list(history or [])
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