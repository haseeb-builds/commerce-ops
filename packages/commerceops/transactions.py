"""Composable SQLite write transactions; helpers never commit caller work."""
from contextlib import contextmanager
import uuid


@contextmanager
def atomic(conn):
    """Serialize top-level read/modify/write operations and isolate nested failures.

    BEGIN IMMEDIATE reserves the writer before reading task/evidence state.
    Within a caller-owned transaction a savepoint preserves unrelated work on
    failure. The caller remains responsible for committing that transaction.
    SQLite lock errors propagate; callers may retry the whole operation.
    """
    owns_transaction = not conn.in_transaction
    name = "ops_" + uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE" if owns_transaction else f"SAVEPOINT {name}")
    try:
        yield
        if owns_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        if owns_transaction:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
