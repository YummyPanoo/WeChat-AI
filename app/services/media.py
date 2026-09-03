import logging
import requests
from .auth import wechat_api

logger = logging.getLogger(__name__)


# ================= 素材管理：下载 / 上传临时素材 =================
def download_media(media_id):
    """下载用户发来的临时素材。返回 (bytes, content_type)，失败返回 (None, None)。"""
    result = wechat_api("GET", f"/cgi-bin/media/get?media_id={media_id}")
    if isinstance(result, dict):
        logger.error("下载素材失败: %s", result)
        return None, None
    return result.content, result.headers.get("Content-Type", "image/jpeg")


def upload_temp_media(media_type, file_bytes, filename="image.jpg"):
    """上传临时素材，返回 media_id（3天有效），失败返回 None。"""
    result = wechat_api(
        "POST",
        f"/cgi-bin/media/upload?type={media_type}",
        files={"media": (filename, file_bytes)},
    )
    if isinstance(result, dict) and result.get("media_id"):
        return result["media_id"]
    logger.error("上传素材失败: %s", result)
    return None


def fetch_image_bytes(url):
    """下载指定 URL 的图片字节，失败返回 None。"""
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception:
        logger.exception("下载图片失败: %s", url)
        return None