import requests, logging
from config.settings import Config

logger = logging.getLogger("wechat-robot")

def chat(prompt: str) -> str:
    if not Config.DASHSCOPE_API_KEY:
        return "AI服务未配置"
    try:
        resp = requests.post(
            Config.DASHSCOPE_API_URL,
            headers={"Authorization": f"Bearer {Config.DASHSCOPE_API_KEY}", "Content-Type": "application/json"},
            json={"model": Config.QWEN_TEXT_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 200},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "AI思考时间过长，请稍后再试。"
    except Exception:
        logger.exception("Qwen文本API异常")
        return "AI服务暂时不可用"

def vision(image_url: str, prompt: str, timeout=30) -> str:
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