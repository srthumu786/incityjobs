"""Generate a password hash for a Manager Console account.

Usage (from the repository root):
    python scripts/hash_password.py

Paste the printed line under [managers] in Streamlit Cloud > App settings > Secrets:
    [managers]
    alice = "pbkdf2_sha256$600000$..."
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth  # noqa: E402


def main() -> int:
    username = input("Manager username (letters, numbers, _ or -): ").strip()
    if not username or not all(ch.isalnum() or ch in "_-" for ch in username):
        print("Invalid username.")
        return 1
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Repeat password: ")
    problem = auth.password_problem(password, confirm)
    if problem:
        print(problem)
        return 1
    print("\nAdd this under [managers] in your Streamlit secrets:\n")
    print(f'{username} = "{auth.hash_password(password)}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
