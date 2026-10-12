"""SQLite layer for Wakil Nibs. Everything lives in pentracker.db next to this file."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "pentracker.db"


def get_connection():
    """Open a connection to the database with dict-like row access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they don't already exist. Safe to call every startup."""
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT NOT NULL,
            color_hex TEXT NOT NULL DEFAULT '#22223B',
            color_name TEXT,
            ink_type TEXT DEFAULT 'Dye',
            sheen INTEGER NOT NULL DEFAULT 0,
            shimmer INTEGER NOT NULL DEFAULT 0,
            shading INTEGER NOT NULL DEFAULT 0,
            bottle_size_ml REAL,
            ink_url TEXT,
            notes TEXT,
            handwriting_sample_path TEXT,
            photo_path TEXT,
            value REAL,
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS pens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT,
            description TEXT,
            nib_size TEXT,
            nib_material TEXT,
            model_url TEXT,
            date_acquired TEXT,
            notes TEXT,
            current_ink_id INTEGER,
            handwriting_sample_path TEXT,
            value REAL,
            status TEXT NOT NULL DEFAULT 'active',
            FOREIGN KEY (current_ink_id) REFERENCES inks(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS pen_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pen_id INTEGER NOT NULL,
            image_path TEXT NOT NULL,
            FOREIGN KEY (pen_id) REFERENCES pens(id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    conn.close()


def _column_exists(conn, table, column):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def migrate_db():
    """
    Adds columns to existing databases that were created before a feature
    existed. CREATE TABLE IF NOT EXISTS (in init_db) only affects brand-new
    tables, so pre-existing pentracker.db files need this to catch up.
    Safe to call every startup -- it's a no-op once the columns exist.
    """
    conn = get_connection()
    if not _column_exists(conn, "pens", "handwriting_sample_path"):
        conn.execute("ALTER TABLE pens ADD COLUMN handwriting_sample_path TEXT")
    if not _column_exists(conn, "inks", "handwriting_sample_path"):
        conn.execute("ALTER TABLE inks ADD COLUMN handwriting_sample_path TEXT")
    if not _column_exists(conn, "pens", "description"):
        conn.execute("ALTER TABLE pens ADD COLUMN description TEXT")
    if not _column_exists(conn, "inks", "photo_path"):
        conn.execute("ALTER TABLE inks ADD COLUMN photo_path TEXT")
    if not _column_exists(conn, "pens", "value"):
        conn.execute("ALTER TABLE pens ADD COLUMN value REAL")
    if not _column_exists(conn, "pens", "status"):
        conn.execute("ALTER TABLE pens ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if not _column_exists(conn, "inks", "value"):
        conn.execute("ALTER TABLE inks ADD COLUMN value REAL")
    if not _column_exists(conn, "inks", "status"):
        conn.execute("ALTER TABLE inks ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    conn.commit()

    # One-time cleanup: pens used to have a single photo (a `photo_path`
    # column), before pens supported multiple photos via the pen_images
    # table. Databases created with that older version still have the
    # column -- move any photo still sitting in it into the gallery, then
    # drop the column entirely so it doesn't linger as dead schema.
    if _column_exists(conn, "pens", "photo_path"):
        legacy_photos = conn.execute(
            "SELECT id, photo_path FROM pens WHERE photo_path IS NOT NULL AND photo_path != ''"
        ).fetchall()
        for pen in legacy_photos:
            already_has_gallery = conn.execute(
                "SELECT COUNT(*) FROM pen_images WHERE pen_id = ?", (pen["id"],)
            ).fetchone()[0]
            if already_has_gallery == 0:
                conn.execute(
                    "INSERT INTO pen_images (pen_id, image_path) VALUES (?, ?)",
                    (pen["id"], pen["photo_path"]),
                )
        conn.commit()

        try:
            conn.execute("ALTER TABLE pens DROP COLUMN photo_path")
            conn.commit()
        except sqlite3.OperationalError:
            # DROP COLUMN needs SQLite 3.35+ (2021). On an older SQLite the
            # column just stays -- harmless, since nothing reads it anymore.
            pass

    conn.close()


# ---------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------
# Whitelists mapping a "sort key" (safe to put in a URL, e.g. ?sort=brand)
# to the actual SQL column to order by. We never build ORDER BY directly
# from user input -- only these pre-approved values ever reach SQL, which
# is what keeps this safe from SQL injection.

PEN_SORT_COLUMNS = {
    "name": "pens.name",
    "brand": "pens.brand",
    "nib_size": "pens.nib_size",
    "nib_material": "pens.nib_material",
    "ink": "inks.name",
    "date_acquired": "pens.date_acquired",
    "value": "pens.value",
    "status": "pens.status",
}

INK_SORT_COLUMNS = {
    "name": "name",
    "brand": "brand",
    "color": "color_name",
    "type": "ink_type",
    "bottle_size": "bottle_size_ml",
    "value": "value",
    "status": "status",
}


def seed_sample_data():
    """
    Adds a couple of example rows the first time the app runs, purely so
    the interface isn't empty when you first open it. Only runs if both
    tables are completely empty -- feel free to delete these afterward.
    """
    conn = get_connection()
    pen_count = conn.execute("SELECT COUNT(*) FROM pens").fetchone()[0]
    ink_count = conn.execute("SELECT COUNT(*) FROM inks").fetchone()[0]

    if pen_count == 0 and ink_count == 0:
        conn.execute(
            """INSERT INTO inks
               (name, brand, color_hex, color_name, ink_type, sheen, shimmer,
                shading, bottle_size_ml, ink_url, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "Troublemaker Sriracha",
                "Diamine",
                "#BC4749",
                "Brick Red",
                "Dye",
                0, 0, 1,
                50,
                "https://diamineink.co.uk/",
                "Great shading, dries fast on Tomoe River.",
            ),
        )
        ink_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            """INSERT INTO pens
               (name, brand, nib_size, nib_material, model_url,
                date_acquired, notes, current_ink_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "Metropolitan",
                "Pilot",
                "Fine",
                "Steel",
                "https://www.pilotpen.us/metropolitan-collection.html",
                "2024-03-01",
                "My first 'real' fountain pen. Reliable daily writer.",
                ink_id,
            ),
        )
        conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Ink helpers
# ---------------------------------------------------------------------

def get_all_inks(sort=None, direction="asc"):
    conn = get_connection()
    direction_sql = "DESC" if direction == "desc" else "ASC"
    if sort in INK_SORT_COLUMNS:
        order_clause = f"{INK_SORT_COLUMNS[sort]} {direction_sql}"
    else:
        order_clause = "brand ASC, name ASC"
    rows = conn.execute(f"SELECT * FROM inks ORDER BY {order_clause}").fetchall()
    conn.close()
    return rows


def get_ink(ink_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM inks WHERE id = ?", (ink_id,)).fetchone()
    conn.close()
    return row


def create_ink(data):
    conn = get_connection()
    conn.execute(
        """INSERT INTO inks
           (name, brand, color_hex, color_name, ink_type, sheen, shimmer,
            shading, bottle_size_ml, ink_url, notes, handwriting_sample_path,
            photo_path, value, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["name"], data["brand"], data["color_hex"], data["color_name"],
            data["ink_type"], data["sheen"], data["shimmer"], data["shading"],
            data["bottle_size_ml"], data["ink_url"], data["notes"],
            data["handwriting_sample_path"], data["photo_path"],
            data["value"], data["status"],
        ),
    )
    conn.commit()
    conn.close()


def update_ink(ink_id, data):
    conn = get_connection()
    conn.execute(
        """UPDATE inks SET name=?, brand=?, color_hex=?, color_name=?,
           ink_type=?, sheen=?, shimmer=?, shading=?, bottle_size_ml=?,
           ink_url=?, notes=?, handwriting_sample_path=?, photo_path=?,
           value=?, status=?
           WHERE id=?""",
        (
            data["name"], data["brand"], data["color_hex"], data["color_name"],
            data["ink_type"], data["sheen"], data["shimmer"], data["shading"],
            data["bottle_size_ml"], data["ink_url"], data["notes"],
            data["handwriting_sample_path"], data["photo_path"],
            data["value"], data["status"], ink_id,
        ),
    )
    conn.commit()
    conn.close()


def delete_ink(ink_id):
    conn = get_connection()
    conn.execute("DELETE FROM inks WHERE id = ?", (ink_id,))
    conn.commit()
    conn.close()


def get_total_ink_ml():
    """Total mL of ink on hand. Bottles flagged sold or empty don't count."""
    conn = get_connection()
    total = conn.execute(
        "SELECT COALESCE(SUM(bottle_size_ml), 0) FROM inks WHERE status NOT IN ('sold', 'empty')"
    ).fetchone()[0]
    conn.close()
    return total


def get_total_ink_value():
    """Sum of the `value` field across every ink (NULLs don't count)."""
    conn = get_connection()
    total = conn.execute("SELECT COALESCE(SUM(value), 0) FROM inks").fetchone()[0]
    conn.close()
    return total


# ---------------------------------------------------------------------
# Pen helpers
# ---------------------------------------------------------------------

def get_all_pens(sort=None, direction="asc"):
    """
    Returns pens joined with their current ink, plus their first gallery
    image (for thumbnails in list views) and total image count. With no
    sort given, this defaults to the dashboard's usual ordering
    (currently-inked pens first, then alphabetical). Pass a key from
    PEN_SORT_COLUMNS to sort by a specific column instead -- used by the
    Pens page's sortable table headers.
    """
    conn = get_connection()
    direction_sql = "DESC" if direction == "desc" else "ASC"
    if sort in PEN_SORT_COLUMNS:
        order_clause = f"{PEN_SORT_COLUMNS[sort]} {direction_sql}"
    else:
        order_clause = "(pens.current_ink_id IS NULL) ASC, pens.brand, pens.name"
    rows = conn.execute(
        f"""
        SELECT pens.*, inks.name AS ink_name, inks.brand AS ink_brand,
               inks.color_hex AS ink_color_hex,
               (SELECT image_path FROM pen_images
                WHERE pen_images.pen_id = pens.id ORDER BY pen_images.id LIMIT 1) AS first_image_path,
               (SELECT COUNT(*) FROM pen_images WHERE pen_images.pen_id = pens.id) AS image_count
        FROM pens
        LEFT JOIN inks ON pens.current_ink_id = inks.id
        ORDER BY {order_clause}
        """
    ).fetchall()
    conn.close()
    return rows


def get_inked_pens():
    """Only pens that currently have ink loaded -- used on the dashboard."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT pens.*, inks.name AS ink_name, inks.brand AS ink_brand,
               inks.color_hex AS ink_color_hex, inks.color_name AS ink_color_name,
               (SELECT image_path FROM pen_images
                WHERE pen_images.pen_id = pens.id ORDER BY pen_images.id LIMIT 1) AS first_image_path,
               (SELECT COUNT(*) FROM pen_images WHERE pen_images.pen_id = pens.id) AS image_count
        FROM pens
        JOIN inks ON pens.current_ink_id = inks.id
        ORDER BY pens.brand, pens.name
        """
    ).fetchall()
    conn.close()
    return rows


def get_pen(pen_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM pens WHERE id = ?", (pen_id,)).fetchone()
    conn.close()
    return row


def get_total_pen_value():
    """Sum of the `value` field across every pen (NULLs don't count)."""
    conn = get_connection()
    total = conn.execute("SELECT COALESCE(SUM(value), 0) FROM pens").fetchone()[0]
    conn.close()
    return total


def get_pens_by_ink(ink_id):
    """All pens currently loaded with a given ink -- shown on the ink's detail page."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM pens WHERE current_ink_id = ? ORDER BY brand, name",
        (ink_id,),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------
# Pen image gallery helpers
# ---------------------------------------------------------------------

def get_pen_images(pen_id):
    """All gallery images/videos for one pen, in upload order."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM pen_images WHERE pen_id = ? ORDER BY id", (pen_id,)
    ).fetchall()
    conn.close()
    return rows


def add_pen_image(pen_id, image_path):
    conn = get_connection()
    conn.execute(
        "INSERT INTO pen_images (pen_id, image_path) VALUES (?, ?)",
        (pen_id, image_path),
    )
    conn.commit()
    conn.close()


def delete_pen_image(image_id):
    """Deletes one gallery entry and returns its file path so the caller
    can also remove the actual file from disk."""
    conn = get_connection()
    row = conn.execute(
        "SELECT image_path FROM pen_images WHERE id = ?", (image_id,)
    ).fetchone()
    conn.execute("DELETE FROM pen_images WHERE id = ?", (image_id,))
    conn.commit()
    conn.close()
    return row["image_path"] if row else None

def create_pen(data):
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO pens
           (name, brand, description, nib_size, nib_material, model_url,
            date_acquired, notes, current_ink_id, handwriting_sample_path,
            value, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["name"], data["brand"], data["description"], data["nib_size"],
            data["nib_material"], data["model_url"], data["date_acquired"],
            data["notes"], data["current_ink_id"], data["handwriting_sample_path"],
            data["value"], data["status"],
        ),
    )
    conn.commit()
    new_pen_id = cursor.lastrowid
    conn.close()
    return new_pen_id


def update_pen(pen_id, data):
    conn = get_connection()
    conn.execute(
        """UPDATE pens SET name=?, brand=?, description=?, nib_size=?,
           nib_material=?, model_url=?, date_acquired=?, notes=?,
           current_ink_id=?, handwriting_sample_path=?, value=?, status=?
           WHERE id=?""",
        (
            data["name"], data["brand"], data["description"], data["nib_size"],
            data["nib_material"], data["model_url"], data["date_acquired"],
            data["notes"], data["current_ink_id"], data["handwriting_sample_path"],
            data["value"], data["status"], pen_id,
        ),
    )
    conn.commit()
    conn.close()


def delete_pen(pen_id):
    conn = get_connection()
    conn.execute("DELETE FROM pens WHERE id = ?", (pen_id,))
    conn.commit()
    conn.close()



def set_pen_ink(pen_id, ink_id):
    """Quick action: load a pen with a given ink (or empty it, ink_id=None)."""
    conn = get_connection()
    conn.execute("UPDATE pens SET current_ink_id = ? WHERE id = ?", (ink_id, pen_id))
    conn.commit()
    conn.close()
