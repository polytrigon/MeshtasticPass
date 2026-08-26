"""Read-only diagnostic: list persisted outgoing SENDING/INTERRUPTED rows.

Opens the CHAT database in SQLite read-only mode -- never through
ChatStore.open(), which (as of the startup reconciliation fix) rewrites
any abandoned SENDING row to INTERRUPTED as a side effect of opening
it. Running that here would erase the exact evidence this tool exists
to show: whether a stuck "SENDING" message on screen means the
database itself still says SENDING, or the database already says
INTERRUPTED and something downstream is still displaying SENDING.

Uses chat_store.default_chat_db_path() for path resolution, so this
always inspects the same database file the app itself would open --
never a guessed location.
"""

import argparse
import sqlite3

from chat_store import default_chat_db_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List persisted outgoing SENDING/INTERRUPTED CHAT rows (read-only)"
    )
    parser.add_argument(
        "--db",
        default=None,
        help="override the CHAT database path (default: the app's normal path)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = args.db or default_chat_db_path()
    print(f"Reading: {path}")

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, text, direction, delivery_state, channel_index
            FROM messages
            WHERE direction = 'outgoing'
                AND delivery_state IN ('SENDING', 'INTERRUPTED')
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()

    if not rows:
        print("No outgoing SENDING/INTERRUPTED rows found.")
        return 0

    print(f"{'id':>6}  {'delivery_state':<12}  {'channel':>7}  text")
    for row in rows:
        preview = row["text"][:60].replace("\n", " ")
        print(f"{row['id']:>6}  {row['delivery_state']:<12}  {row['channel_index']:>7}  {preview!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
