"""Persistent PX ID registry.

IDs are assigned once per (org, repo) and never change. Format: YYMM.NNNNN
(e.g., 2604.00001 = first paper of April 2026). The numbering pool is
shared across all approved orgs — external papers and internal papers are
indistinguishable by ID alone; provenance is tracked via the ``org`` column.
"""

import re
import sqlite3


def get_or_assign_id(conn: sqlite3.Connection, org: str, repo: str, date: str) -> str:
    """Look up or assign a PX ID for the given (org, repo).

    If the pair already has an ID, return it. Otherwise, derive YYMM from
    *date* (YYYY-MM-DD ...), atomically allocate the next sequence number
    from the shared pool, and persist the mapping.
    """
    row = conn.execute(
        "SELECT px_id FROM id_registry WHERE org = ? AND repo = ?", (org, repo)
    ).fetchone()
    if row:
        return row["px_id"]

    yymm = _date_to_yymm(date)

    # Atomically allocate next number
    conn.execute(
        "INSERT INTO id_sequence (yymm, next_n) VALUES (?, 1) "
        "ON CONFLICT(yymm) DO NOTHING",
        (yymm,),
    )
    cur = conn.execute(
        "UPDATE id_sequence SET next_n = next_n + 1 WHERE yymm = ? RETURNING next_n - 1",
        (yymm,),
    )
    seq = cur.fetchone()[0]
    px_id = f"{yymm}.{seq:05d}"

    conn.execute(
        "INSERT INTO id_registry (org, repo, px_id, yymm) VALUES (?, ?, ?, ?)",
        (org, repo, px_id, yymm),
    )
    conn.commit()
    return px_id


def get_id_for_repo(conn: sqlite3.Connection, org: str, repo: str) -> str | None:
    """Pure lookup — returns None if (org, repo) has no assigned ID."""
    row = conn.execute(
        "SELECT px_id FROM id_registry WHERE org = ? AND repo = ?", (org, repo)
    ).fetchone()
    return row["px_id"] if row else None


def _date_to_yymm(date_str: str) -> str:
    """Extract YYMM from a date string like '2026-04-05 ...'."""
    match = re.match(r"(\d{4})-(\d{2})", date_str)
    if match:
        return match.group(1)[2:] + match.group(2)
    return "0000"
