"""Database engine, session factory, declarative Base, and helpers.

A handful of small affordances live here so the rest of the code can stay clean:

  * `init_database()` is idempotent and called from the FastAPI lifespan so a
    fresh checkout boots without a manual `init_db` step. (`init_db.py` still
    exists for the demo-seed flow, but you don't *need* it to start the server.)

  * `session_scope()` is the context-managed session for background jobs
    (commits on success, rolls back on exception).

  * `get_db()` is the FastAPI request-scoped session dependency. It does NOT
    auto-commit; mutation routes wrap themselves in `committing_db()` instead,
    which commits on success and rolls back on exception. That keeps the
    "intent to write" explicit at the route boundary.
"""
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# SQLite needs check_same_thread=False to be used across threads (FastAPI + scheduler).
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    echo=False,
    future=True,
)

# SQLite skips ondelete="CASCADE" unless this pragma is set on every connection.
# Without it, deleting a Commitment leaves orphaned Reassignment rows behind.
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def init_database() -> None:
    """Create any missing tables. Idempotent — safe to call on every boot."""
    # Import models so the metadata is populated before create_all.
    from app import models  # noqa: F401
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations() -> None:
    """Add columns introduced after the original schema. SQLite-only for now."""
    if not settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        cols = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
        }
        if "tz" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN tz VARCHAR(64) NOT NULL DEFAULT 'UTC'"))
        if "signed_in_at" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN signed_in_at DATETIME"))
        if "auto_resume_hours_before_deadline" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN auto_resume_hours_before_deadline "
                "INTEGER NOT NULL DEFAULT 24"
            ))
        if "auto_resume_hours_before_deadline" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN "
                "auto_resume_hours_before_deadline INTEGER NOT NULL DEFAULT 24"
            ))
        # A previous version of this migration renamed
        # auto_delete_completed_after_days → auto_archive_completed_after_days.
        # The semantics turned out wrong: the field's intent is "auto-purge
        # old completed items," with `0` as the only special case (archive
        # instead). Rename back if we find the wrong column. Existing values
        # carry forward.
        if (
            "auto_archive_completed_after_days" in cols
            and "auto_delete_completed_after_days" not in cols
        ):
            conn.execute(text(
                "ALTER TABLE users RENAME COLUMN auto_archive_completed_after_days "
                "TO auto_delete_completed_after_days"
            ))

        ra_cols = {
            row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info(reassignments)"
            ).fetchall()
        }
        if ra_cols:  # table exists (it does, post-create_all)
            if "note" not in ra_cols:
                conn.execute(text("ALTER TABLE reassignments ADD COLUMN note TEXT"))
            if "notice_channel_id" not in ra_cols:
                conn.execute(text(
                    "ALTER TABLE reassignments ADD COLUMN notice_channel_id VARCHAR(32)"
                ))
            if "notice_message_ts" not in ra_cols:
                conn.execute(text(
                    "ALTER TABLE reassignments ADD COLUMN notice_message_ts VARCHAR(32)"
                ))

        c_cols = {
            row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info(commitments)"
            ).fetchall()
        }
        if "outcome" not in c_cols:
            conn.execute(text("ALTER TABLE commitments ADD COLUMN outcome VARCHAR(16)"))
        if "prior_state" not in c_cols:
            conn.execute(text("ALTER TABLE commitments ADD COLUMN prior_state VARCHAR(16)"))
        # Agent provenance — only set for source=AGENT captures, NULL otherwise.
        if "agent_confidence" not in c_cols:
            conn.execute(text("ALTER TABLE commitments ADD COLUMN agent_confidence FLOAT"))
        if "agent_rationale" not in c_cols:
            conn.execute(text("ALTER TABLE commitments ADD COLUMN agent_rationale TEXT"))

        # Per-user agent settings. Defaults match the model: OFF by default.
        if "agent_enabled" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN agent_enabled BOOLEAN NOT NULL DEFAULT 0"
            ))
        if "agent_confidence_floor_pct" not in cols:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN agent_confidence_floor_pct INTEGER"
            ))
        # Self-heal any rows left with a lowercase outcome from an earlier
        # iteration of this migration. SAEnum reads/writes the enum NAME
        # (uppercase), so a stored 'success' triggers a LookupError on read.
        conn.execute(text(
            "UPDATE commitments SET outcome = 'SUCCESS' WHERE outcome = 'success'"
        ))
        conn.execute(text(
            "UPDATE commitments SET outcome = 'FAILED' WHERE outcome = 'failed'"
        ))
        # Always run the outcome backfill — guards against the case where the
        # column existed (added by a prior init_database call) but rows were
        # never classified. Idempotent: only touches outcome IS NULL rows.
        # Note: SAEnum stores the enum NAME (uppercase), not the value, so the
        # state strings here are 'COMPLETE' / 'ARCHIVED' / 'DELETED'.
        # CommitmentOutcome is stored the same way ('SUCCESS' / 'FAILED').
        conn.execute(text("""
            UPDATE commitments
            SET outcome = 'SUCCESS'
            WHERE outcome IS NULL
              AND state IN ('COMPLETE', 'ARCHIVED')
              AND completed_at IS NOT NULL
              AND (deadline IS NULL OR completed_at <= deadline)
        """))
        conn.execute(text("""
            UPDATE commitments
            SET outcome = 'FAILED'
            WHERE outcome IS NULL
              AND state IN ('COMPLETE', 'ARCHIVED', 'DELETED')
        """))

        # NOTE: a previous version of this migration tried to "clean up"
        # rows in the REASSIGNED state, assuming they were limbo leftovers
        # from an earlier model. That assumption is WRONG under the current
        # design — REASSIGNED is a legitimate post-acceptance live state
        # (Bob owns the commitment). Running that cleanup on every boot
        # silently demoted accepted commitments back to ACTIVE, losing the
        # "this was handed to me" label. The cleanup is intentionally NOT
        # present here. If we ever need a one-time data fix again, gate it
        # on an explicit migration-version marker instead of state-pattern
        # matching.


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed DB session. Commits on success, rolls back on exception."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency for request-scoped DB sessions (read-only by default)."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def committing_db() -> Iterator[Session]:
    """
    FastAPI dependency for routes that mutate. Commits on success, rolls back
    on exception, and always closes the session. Use in place of `get_db` for
    POST/PUT/DELETE endpoints so the route body never has to remember to commit.
    """
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
