from pathlib import Path

from flask import Blueprint

blueprint = Blueprint(
    'additional_blueprint',
    __name__,
    url_prefix='/additional',
    template_folder='templates',
    static_folder='static'
)

# 註冊 MeetGenius 路由
from .meetgenius.main import routes
from .meetgenius.api import routes as api_routes

from app.additional.meetgenius import db as meetgenius_db


def init_meetgenius(app):
    """初始化 MeetGenius：建立資料表 schema、確保上傳/處理/輸出資料夾存在。"""
    meetgenius_db.init_app(app)

    try:
        base_dir = Path(app.root_path).parent
        folders_to_check = [
            base_dir / "app" / "additional" / "meetgenius" / "uploads",
            base_dir / "app" / "additional" / "meetgenius" / "processed",
            base_dir / "app" / "additional" / "meetgenius" / "output",
        ]
        for folder in folders_to_check:
            folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        app.logger.error(f"Failed to create MeetGenius folders: {e}", exc_info=True)
