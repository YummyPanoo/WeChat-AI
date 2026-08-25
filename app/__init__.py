from flask import Flask
from config.settings import Config
from app.utils.logger import setup_logger


def create_app():
    app = Flask(__name__)

    # 初始化日志
    setup_logger("wechat-robot", Config.LOG_LEVEL)

    # 注册蓝图
    from app.routes.wechat import bp as wechat_bp
    app.register_blueprint(wechat_bp)

    @app.route("/")
    def index():
        return "WeChat AI Bot Running ✅"

    return app