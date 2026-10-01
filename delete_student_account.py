"""Safely delete one student account from the AI Proctoring SQLite database.

Run this file from the project root:
    python delete_student_account.py

The script removes:
- the selected row from the user table
- the student's face signature (.npy)
- the student's ID photo, when the file exists

It intentionally keeps old exam-session/audit records so historical records
are not accidentally destroyed.
"""

from pathlib import Path
import shutil
import sqlite3
from datetime import datetime


PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "instance" / "proctoring.db"
SIGNATURES_DIR = PROJECT_ROOT / "face_signatures"


def main():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        print("Run this script from the folder containing instance/proctoring.db.")
        return

    backup_path = DB_PATH.with_name(
        f"proctoring_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
    )
    shutil.copy2(DB_PATH, backup_path)
    print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    try:
        users = connection.execute(
            """
            SELECT id, student_id, name, email, role, id_photo_path
            FROM user
            WHERE role = 'student'
            ORDER BY id
            """
        ).fetchall()

        if not users:
            print("No student accounts were found.")
            return

        print("\nRegistered student accounts:")
        for user in users:
            print(
                f"  Database ID: {user['id']} | "
                f"Student ID: {user['student_id']} | "
                f"Name: {user['name']} | Email: {user['email']}"
            )

        search_value = input(
            "\nEnter the student ID or email of the account to delete: "
        ).strip()

        user = connection.execute(
            """
            SELECT id, student_id, name, email, role, id_photo_path
            FROM user
            WHERE role = 'student'
              AND (student_id = ? OR email = ?)
            """,
            (search_value, search_value),
        ).fetchone()

        if user is None:
            print("No matching student account was found.")
            return

        print("\nAccount selected:")
        print(f"  Name: {user['name']}")
        print(f"  Email: {user['email']}")
        print(f"  Student ID: {user['student_id']}")

        confirmation = input(
            "Type DELETE to permanently remove this account: "
        ).strip()

        if confirmation != "DELETE":
            print("Deletion cancelled.")
            return

        student_id = user["student_id"]
        photo_path = user["id_photo_path"]

        connection.execute("DELETE FROM user WHERE id = ?", (user["id"],))
        connection.commit()

        deleted_signature = False
        if student_id:
            signature_path = SIGNATURES_DIR / f"{student_id}.npy"
            if signature_path.exists():
                signature_path.unlink()
                deleted_signature = True

        deleted_photo = False
        if photo_path:
            possible_photo_paths = [
                PROJECT_ROOT / photo_path,
                PROJECT_ROOT / "static" / "id_photos" / f"{student_id}.jpg",
            ]
            for candidate in possible_photo_paths:
                if candidate.exists() and candidate.is_file():
                    candidate.unlink()
                    deleted_photo = True
                    break

        print("\nAccount deleted successfully.")
        print(f"Deleted database user: {user['email']}")
        print(f"Deleted face signature: {deleted_signature}")
        print(f"Deleted ID photo: {deleted_photo}")
        print(f"Database backup kept at: {backup_path}")
        print("Historical exam/session records were not deleted.")

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
