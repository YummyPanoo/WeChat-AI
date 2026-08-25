import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # 微信
    WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "")
    WECHAT_APPID = os.getenv("WECHAT_APPID", "")
    WECHAT_APPSECRET = os.getenv("WECHAT_APPSECRET", "")
    WECHAT_API_BASE = "https://api.weixin.qq.com"
    WECHAT_ASYNC_MODE = os.getenv("WECHAT_ASYNC", "0") == "1"
    
    # AI
    DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
    DASHSCOPE_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    QWEN_TEXT_MODEL = os.getenv("QWEN_TEXT_MODEL", "qwen3.7-flash")
    QWEN_VL_MODEL = os.getenv("QWEN_VL_MODEL", "qwen3.8-max")
    
    # 服务
    PORT = int(os.getenv("PORT", 8080))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")