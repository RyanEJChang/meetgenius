import bcrypt
from flask_login import UserMixin

from app.additional.meetgenius.db import get_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT UNIQUE,
    password BYTEA NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init_schema():
    """建立 users 表（冪等）。"""
    import psycopg2
    from app.additional.meetgenius.db import get_db_config

    conn = psycopg2.connect(**get_db_config())
    try:
        with conn.cursor() as cursor:
            cursor.execute(SCHEMA)
        conn.commit()
    finally:
        conn.close()


class User(UserMixin):
    def __init__(self, id, username, email, enabled):
        self.id = id
        self.username = username
        self.email = email
        self.enabled = enabled

    def get_id(self):
        return str(self.id)

    @staticmethod
    def _from_row(row):
        if not row:
            return None
        return User(row["id"], row["username"], row["email"], row["enabled"])

    @staticmethod
    def get_by_id(user_id):
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, username, email, enabled FROM users WHERE id = %s", (user_id,))
            return User._from_row(cursor.fetchone())

    @staticmethod
    def authenticate(username, password):
        """驗證帳號密碼，成功回傳 User，失敗回傳 None。"""
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, username, email, enabled, password FROM users WHERE username = %s OR email = %s",
                (username, username),
            )
            row = cursor.fetchone()

        if not row or not row["enabled"]:
            return None
        if not bcrypt.checkpw(password.encode("utf-8"), bytes(row["password"])):
            return None
        return User._from_row(row)

    @staticmethod
    def create(username, email, password):
        """建立新使用者（bcrypt 雜湊密碼）。"""
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (username, email, password) VALUES (%s, %s, %s) RETURNING id, username, email, enabled",
                (username, email, password_hash),
            )
            row = cursor.fetchone()
        conn.commit()
        return User._from_row(row)
