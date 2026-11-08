"""Tiny SQLite persistence for InterviewCoach roles and sessions.

Uses only the Python standard library. The DB file lives at
interviewcoach.db in the project root (next to .env).
"""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent  # backend/
DB_PATH = HERE.parent / "interviewcoach.db"  # project root

AUTH_TOKEN_TTL_SECONDS = 14 * 24 * 3600


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate() -> None:
    """Add newer columns to existing DBs without wiping data."""
    with _connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(roles)").fetchall()}
        if "jd_text" not in cols:
            conn.execute("ALTER TABLE roles ADD COLUMN jd_text TEXT DEFAULT ''")
        if "topics_json" not in cols:
            conn.execute("ALTER TABLE roles ADD COLUMN topics_json TEXT DEFAULT '[]'")
        if "owner_id" not in cols:
            conn.execute("ALTER TABLE roles ADD COLUMN owner_id INTEGER")
        # Backfill: roles created before ownership existed belong to the
        # oldest admin account. Fresh seed/demo roles (no users yet) stay
        # ownerless = shared demos visible to every admin.
        first = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if first:
            conn.execute("UPDATE roles SET owner_id = ? WHERE owner_id IS NULL", (first["id"],))


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                pass_threshold REAL NOT NULL DEFAULT 6.0,
                onboarding_url TEXT,
                questions_json TEXT NOT NULL,
                jd_text TEXT DEFAULT '',
                topics_json TEXT DEFAULT '[]',
                owner_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role_slug TEXT NOT NULL,
                candidate_name TEXT,
                candidate_email TEXT,
                average_score REAL,
                passed INTEGER,
                answers_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (role_slug) REFERENCES roles(slug)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_role ON sessions(role_slug);

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                pw_salt TEXT NOT NULL,
                pw_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS auth_tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS onboarding_passes (
                token TEXT PRIMARY KEY,
                role_slug TEXT NOT NULL,
                role_name TEXT DEFAULT '',
                candidate_name TEXT DEFAULT '',
                candidate_email TEXT DEFAULT '',
                average_score REAL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (role_slug) REFERENCES roles(slug)
            );

            CREATE INDEX IF NOT EXISTS idx_passes_role ON onboarding_passes(role_slug);
            """
        )
    _migrate()


# ---------------------------------------------------------------------------
# Auth helpers (stdlib only: PBKDF2 + secrets tokens)
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 210_000)
    return salt, dk.hex()


def create_user(email: str, password: str, name: str = "") -> dict[str, Any]:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("Enter a valid email address.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt, pw_hash = _hash_password(password)
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (email, name, pw_salt, pw_hash) VALUES (?, ?, ?, ?)",
                (email, name.strip(), salt, pw_hash),
            )
        except sqlite3.IntegrityError:
            raise ValueError("An account with this email already exists.") from None
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _user_from_row(row)


def verify_user(email: str, password: str) -> dict[str, Any] | None:
    email = email.strip().lower()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return None
    _, trial = _hash_password(password, row["pw_salt"])
    if not secrets.compare_digest(trial, row["pw_hash"]):
        return None
    return _user_from_row(row)


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_from_row(row) if row else None


def count_users() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"])


def create_auth_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO auth_tokens (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + AUTH_TOKEN_TTL_SECONDS),
        )
    return token


def get_user_by_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            """SELECT u.* FROM users u JOIN auth_tokens t ON t.user_id = u.id
               WHERE t.token = ? AND t.expires_at > ?""",
            (token, now),
        ).fetchone()
    return _user_from_row(row) if row else None


def delete_auth_token(token: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))


def _user_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"] or "",
        "created_at": row["created_at"],
    }


def seed_default_roles() -> None:
    """Insert the built-in roles if the roles table is empty."""
    defaults = [
        {
            "slug": "junior-software-engineer",
            "name": "Junior Software Engineer",
            "description": "Entry-level software engineering position.",
            "pass_threshold": 6.5,
            "onboarding_url": "https://calendly.com/example/junior-software-engineer",
            "jd_text": "",
            "topics": ["debugging", "code quality", "system design basics", "teamwork"],
            "questions": [
                "Tell me about a time you had to debug a tricky production issue. How did you approach it?",
                "Describe a situation where you disagreed with a technical decision made by your team.",
                "Walk me through how you would design a URL shortening service like bit.ly.",
                "Tell me about the most complex system you have built or contributed to.",
                "How do you ensure the quality of your code? Give me an example of a time that process caught a serious bug.",
            ],
        },
        {
            "slug": "ux-designer",
            "name": "UX Designer",
            "description": "User experience and interface design role.",
            "pass_threshold": 6.5,
            "onboarding_url": "https://calendly.com/example/ux-designer",
            "jd_text": "",
            "topics": ["design process", "user research", "stakeholder feedback"],
            "questions": [
                "Walk me through your design process from brief to final delivery.",
                "Tell me about a time user research changed the direction of your design significantly.",
                "How do you handle feedback from stakeholders that conflicts with what users actually need?",
                "Describe your most challenging design project. What made it hard, and how did you work through it?",
                "How do you measure whether a design is successful after it ships?",
            ],
        },
        {
            "slug": "marketing-specialist",
            "name": "Marketing Specialist",
            "description": "Marketing campaigns, content, and growth role.",
            "pass_threshold": 6.0,
            "onboarding_url": "https://calendly.com/example/marketing-specialist",
            "jd_text": "",
            "topics": ["campaign ownership", "metrics", "experimentation"],
            "questions": [
                "Tell me about a marketing campaign you ran from start to finish. What was the outcome?",
                "Describe a time you had to market a product with a very limited budget.",
                "How do you measure the success of a marketing campaign?",
                "Tell me about a time you had to pivot a campaign based on data.",
                "How do you stay up to date with marketing trends and tools?",
            ],
        },
    ]
    with _connect() as conn:
        for role in defaults:
            questions = _normalize_questions(role["questions"])
            conn.execute(
                """
                INSERT OR IGNORE INTO roles
                (slug, name, description, pass_threshold, onboarding_url, questions_json,
                 jd_text, topics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    role["slug"],
                    role["name"],
                    role["description"],
                    role["pass_threshold"],
                    role["onboarding_url"],
                    json.dumps(questions),
                    role.get("jd_text", ""),
                    json.dumps(role.get("topics", [])),
                ),
            )


def _normalize_topics(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").replace("\n", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    return []


def _normalize_questions(value: Any) -> list[dict[str, str]]:
    """Accept list of strings or dicts -> list of {question, criteria, competency}."""
    out: list[dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, str):
            if item.strip():
                out.append({"question": item.strip(), "criteria": "", "competency": ""})
        elif isinstance(item, dict):
            q = str(item.get("question") or item.get("q") or item.get("text") or "").strip()
            if q:
                out.append({
                    "question": q,
                    "criteria": str(item.get("criteria") or item.get("what_to_look_for") or ""),
                    "competency": str(item.get("competency") or item.get("skill") or ""),
                })
    return out


def list_roles(owner_id: int | None = None, mine_only: bool = False) -> list[dict[str, Any]]:
    """List roles. With mine_only=True (admin dashboard), an admin sees only
    their own roles plus shared ownerless demo roles."""
    with _connect() as conn:
        if mine_only and owner_id is not None:
            rows = conn.execute(
                "SELECT * FROM roles WHERE owner_id IS NULL OR owner_id = ? ORDER BY name",
                (owner_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM roles ORDER BY name").fetchall()
        return [_role_from_row(r) for r in rows]


def get_role(slug: str) -> dict[str, Any] | None:
    """Look up a role by slug, falling back to a name match.

    The voice agent is told the role as a human title ("Junior Software
    Engineer"), so a slug-only lookup would miss and silently serve the
    generic question bank.
    """
    key = (slug or "").strip()
    if not key:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM roles WHERE slug = ?", (key,)).fetchone()
        if row:
            return _role_from_row(row)
        row = conn.execute(
            "SELECT * FROM roles WHERE lower(name) = lower(?)", (key,)
        ).fetchone()
        if row:
            return _role_from_row(row)
        # Last resort: normalise "Junior Software Engineer" -> "junior-software-engineer".
        guess = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
        if guess and guess != key:
            row = conn.execute("SELECT * FROM roles WHERE slug = ?", (guess,)).fetchone()
            if row:
                return _role_from_row(row)
        return None


def create_role(role: dict[str, Any]) -> dict[str, Any]:
    questions = _normalize_questions(role.get("questions", []))
    if not questions:
        raise ValueError("Add at least one question.")
    topics = _normalize_topics(role.get("topics", []))
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO roles (slug, name, description, pass_threshold, onboarding_url,
                               questions_json, jd_text, topics_json, owner_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                role["slug"],
                role["name"],
                role.get("description", ""),
                role.get("pass_threshold", 6.0),
                role.get("onboarding_url", ""),
                json.dumps(questions),
                str(role.get("jd_text", "") or ""),
                json.dumps(topics),
                role.get("owner_id"),
            ),
        )
    result = get_role(role["slug"])
    if not result:
        raise ValueError("Could not create role.")
    return result


def _can_manage(role: dict[str, Any] | None, owner_id: int | None) -> bool:
    """Owner-only for owned roles; shared ownerless demo roles are manageable by all."""
    if not role:
        return False
    if owner_id is None:
        return True  # internal callers (no admin context)
    return role.get("owner_id") is None or role.get("owner_id") == owner_id


def update_role(slug: str, updates: dict[str, Any], owner_id: int | None = None) -> dict[str, Any] | None:
    existing = get_role(slug)
    if not _can_manage(existing, owner_id):
        return None
    allowed = {"name", "description", "pass_threshold", "onboarding_url", "questions", "jd_text", "topics"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return get_role(slug)
    if "questions" in fields:
        questions = _normalize_questions(fields.pop("questions"))
        if not questions:
            raise ValueError("Add at least one question.")
        fields["questions_json"] = json.dumps(questions)
    if "topics" in fields:
        fields["topics_json"] = json.dumps(_normalize_topics(fields.pop("topics")))
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [slug]
    with _connect() as conn:
        conn.execute(f"UPDATE roles SET {set_clause} WHERE slug = ?", values)
    return get_role(slug)


def delete_role(slug: str, owner_id: int | None = None) -> bool:
    if not _can_manage(get_role(slug), owner_id):
        return False
    with _connect() as conn:
        cur = conn.execute("DELETE FROM roles WHERE slug = ?", (slug,))
        return cur.rowcount > 0


def save_session(
    role_slug: str,
    answers: list[dict[str, Any]],
    average_score: float,
    passed: bool,
    candidate_name: str | None = None,
    candidate_email: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO sessions
            (role_slug, candidate_name, candidate_email, average_score, passed, answers_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                role_slug,
                candidate_name,
                candidate_email,
                average_score,
                1 if passed else 0,
                json.dumps(answers),
            ),
        )
        return int(cur.lastrowid)


def list_sessions(role_slug: str | None = None, owner_id: int | None = None,
                  mine_only: bool = False) -> list[dict[str, Any]]:
    """With mine_only=True, only sessions for the admin's own roles
    (plus shared demo roles) are returned."""
    query = "SELECT s.* FROM sessions s"
    params: list = []
    clauses: list[str] = []
    if mine_only and owner_id is not None:
        clauses.append(
            "s.role_slug IN (SELECT slug FROM roles WHERE owner_id IS NULL OR owner_id = ?)")
        params.append(owner_id)
    if role_slug:
        clauses.append("s.role_slug = ?")
        params.append(role_slug)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY s.created_at DESC"
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        return [_session_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Next-round passes — auto-generated onboarding links for passed candidates
# ---------------------------------------------------------------------------

def create_onboarding_pass(role_slug: str, role_name: str, average_score: float,
                           candidate_name: str | None = None,
                           candidate_email: str | None = None) -> dict[str, Any]:
    token = secrets.token_urlsafe(16)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO onboarding_passes
            (token, role_slug, role_name, candidate_name, candidate_email, average_score)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token, role_slug, role_name or role_slug,
             candidate_name or "", candidate_email or "", average_score),
        )
    result = get_onboarding_pass(token)
    if not result:
        raise RuntimeError("Could not create onboarding pass.")
    return result


def get_onboarding_pass(token: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM onboarding_passes WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    return {
        "token": row["token"],
        "path": f"/onboard/{row['token']}",
        "role_slug": row["role_slug"],
        "role_name": row["role_name"],
        "candidate_name": row["candidate_name"],
        "candidate_email": row["candidate_email"],
        "average_score": row["average_score"],
        "created_at": row["created_at"],
    }


def list_onboarding_passes(role_slug: str | None = None, owner_id: int | None = None,
                           mine_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT p.* FROM onboarding_passes p"
    params: list = []
    clauses: list[str] = []
    if mine_only and owner_id is not None:
        clauses.append(
            "p.role_slug IN (SELECT slug FROM roles WHERE owner_id IS NULL OR owner_id = ?)")
        params.append(owner_id)
    if role_slug:
        clauses.append("p.role_slug = ?")
        params.append(role_slug)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY p.created_at DESC"
    with _connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [
        {
            "token": r["token"],
            "path": f"/onboard/{r['token']}",
            "role_slug": r["role_slug"],
            "role_name": r["role_name"],
            "candidate_name": r["candidate_name"],
            "candidate_email": r["candidate_email"],
            "average_score": r["average_score"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _role_from_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        raw_questions = json.loads(row["questions_json"])
    except (json.JSONDecodeError, TypeError):
        raw_questions = []
    questions = _normalize_questions(raw_questions)
    try:
        topics = json.loads(row["topics_json"]) if "topics_json" in row.keys() else []
    except (json.JSONDecodeError, TypeError):
        topics = []
    jd_text = row["jd_text"] if "jd_text" in row.keys() else ""
    # Back-compat: API consumers expect questions as objects now, plus a
    # plain string list for older clients.
    try:
        owner_id = row["owner_id"] if "owner_id" in row.keys() else None
    except (KeyError, IndexError):
        owner_id = None
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "pass_threshold": row["pass_threshold"],
        "onboarding_url": row["onboarding_url"],
        "questions": questions,
        "question_texts": [q["question"] for q in questions],
        "jd_text": jd_text or "",
        "topics": _normalize_topics(topics),
        "owner_id": owner_id,
        "created_at": row["created_at"],
    }


def _session_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "role_slug": row["role_slug"],
        "candidate_name": row["candidate_name"],
        "candidate_email": row["candidate_email"],
        "average_score": row["average_score"],
        "passed": bool(row["passed"]),
        "answers": json.loads(row["answers_json"]),
        "created_at": row["created_at"],
    }


if __name__ == "__main__":
    init_db()
    seed_default_roles()
    print(f"Database ready at {DB_PATH}")
    print(f"Roles: {len(list_roles())}")
