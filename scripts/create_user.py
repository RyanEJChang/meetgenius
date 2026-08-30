"""建立 MeetGenius standalone 版的登入帳號。

用法：
    python scripts/create_user.py <username> <email> <password>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth.models import init_schema, User  # noqa: E402


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    username, email, password = sys.argv[1:4]

    init_schema()

    # create() 底層用的 get_db() 依賴 flask.g，需要在 app context 內執行。
    from app import create_app
    app = create_app()
    with app.app_context():
        user = User.create(username, email, password)
        print(f"已建立使用者：{user.username} ({user.email})")


if __name__ == "__main__":
    main()
