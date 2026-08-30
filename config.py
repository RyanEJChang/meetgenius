import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB（MeetGenius 會議檔案上限）
