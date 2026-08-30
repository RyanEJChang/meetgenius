import logging
import os
import sys

from flask import Flask, redirect, url_for
from flask_login import LoginManager

from config import Config


class _OpenAIInfo:
    """提供 current_app.audio_processor 介面所需的最小資訊（供補充文件摘要 / 翻譯狀態查詢使用）。"""

    def __init__(self):
        from openai import OpenAI

        self.openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self._model = os.getenv("OPENAI_MODEL", "gpt-4o")

    def get_provider_name(self):
        return "OpenAI"

    def get_model_name(self):
        return self._model


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    for required in ("OPENAI_API_KEY", "AZURE_SPEECH_KEY"):
        if not os.getenv(required):
            logger.error(f"未設置 {required}，請在 .env 檔案中補上後再啟動。")
            sys.exit(1)

    # --- MeetGenius 進度追蹤 / 音訊處理器資訊 ---
    app.progress_tracker = {}
    app.progress_latest = {}
    app.audio_processor = _OpenAIInfo()

    # --- Blueprints ---
    from app.additional import blueprint as additional_blueprint, init_meetgenius
    app.register_blueprint(additional_blueprint)
    init_meetgenius(app)

    from app.auth import auth_bp
    app.register_blueprint(auth_bp)

    from app.auth.models import init_schema as init_auth_schema
    init_auth_schema()

    # --- Flask-Login ---
    login_manager = LoginManager()
    login_manager.login_view = "auth.login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        from app.auth.models import User
        return User.get_by_id(user_id)

    @app.route("/")
    def root():
        return redirect(url_for("additional_blueprint.index"))

    logger.info("MeetGenius standalone app 已建立並設定完成。")
    return app
