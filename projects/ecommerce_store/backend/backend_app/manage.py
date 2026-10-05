"""Management commands (the graph defines no operation that creates users, so the first accounts are provisioned here).

    DATABASE_URL=... DATABASE_SCHEMA=... python -m backend_app.manage create-user --email a@b.c --full-name "Ada" --role manager
The password is read from $USER_PASSWORD or prompted; it is never accepted as a command-line argument.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import Settings
from .container import build_container
from .security import hash_password


def create_user(email: str, full_name: str, role: str, password: str) -> str:
    settings = Settings.from_env().validate()
    c = build_container(settings)
    auth = c.view.spec["auth"]
    if not auth:
        raise SystemExit("this project has no authentication entity")
    if role not in c.view.role_by_key:
        raise SystemExit(f"unknown role '{role}'; known roles: {sorted(c.view.role_by_key)}")
    if len(password) < 8:
        raise SystemExit("password must be at least 8 characters")
    c.db.open()
    try:
        repo = c.repos[auth["entity"]]
        with c.db.transaction() as conn:
            row = repo.insert(conn, {"email": email, "password_hash": hash_password(password), "full_name": full_name, "role": role})
        return str(row["id"])
    finally:
        c.db.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m backend_app.manage")
    sub = p.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--full-name", required=True)
    cu.add_argument("--role", required=True)
    args = p.parse_args(argv)
    password = os.environ.get("USER_PASSWORD") or getpass.getpass("Password: ")
    print(create_user(args.email, args.full_name, args.role, password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
