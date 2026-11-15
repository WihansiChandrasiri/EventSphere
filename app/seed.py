import os

from dotenv import load_dotenv
from pwdlib import PasswordHash
from sqlalchemy import select

from app import database as db

load_dotenv()
passwords = PasswordHash.recommended()


def main():
    accounts = [
        (
            "admin@eventsphere.test",
            "Demo Admin",
            "admin",
            os.getenv("DEMO_ADMIN_PASSWORD", ""),
        ),
        (
            "staff@eventsphere.test",
            "Demo Gate Staff",
            "staff",
            os.getenv("DEMO_STAFF_PASSWORD", ""),
        ),
        (
            "attendee@eventsphere.test",
            "Demo Attendee",
            "attendee",
            os.getenv("DEMO_ATTENDEE_PASSWORD", ""),
        ),
    ]

    for email, name, role, password in accounts:
        if len(password) < 8:
            raise RuntimeError(
                f"Set a password of at least eight characters for {role} "
                "in your .env file."
            )

    db.initialize()

    with db.transaction() as session:
        for email, name, role, password in accounts:
            existing = session.scalar(
                select(db.User).where(db.User.email == email)
            )

            if existing:
                print(f"Already exists: {email}")
                continue

            session.add(db.User(
                name=name,
                email=email,
                role=role,
                password_hash=passwords.hash(password),
            ))
            print(f"Created {role}: {email}")

    print("Demo account setup complete.")


if __name__ == "__main__":
    main()