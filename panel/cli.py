import sys
import bcrypt
from . import db


def main():
    if len(sys.argv) != 4 or sys.argv[1] != "reset-password":
        print("usage: mypanel reset-password <username> <new-password>")
        sys.exit(1)
    user, pw = sys.argv[2], sys.argv[3]
    if len(pw) < 8:
        print("Password must be at least 8 characters")
        sys.exit(1)
    db.init()
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    with db.db() as c:
        cur = c.execute("UPDATE users SET pw_hash=? WHERE username=?", (h, user))
        if cur.rowcount == 0:
            print(f"No such user: {user}")
            sys.exit(1)
    print(f"Password updated for {user}")


if __name__ == "__main__":
    main()
