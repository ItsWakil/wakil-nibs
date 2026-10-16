"""Swatch gallery for Wakil Nibs.

Photograph an ink swatch, and this white-balances the photo, pulls out the ink's
colors, and lets you browse the collection by hue or search it by color.

Registered from app.py with init_swatches(...). Templates live in
templates/swatches/, styles in static/css/swatches.css.

Routes (all under /swatches):
    GET  /                       gallery; ?color=7A1F2B searches, ?sort=brand,
                                 ?f=sheen&f=inked filters
    GET  /ink/<ink_id>           swatch analysis for one ink
    GET  /new                    upload form                    (login)
    POST /new                    save a swatch                  (login)
    POST /api/preview            extract colors without saving  (login) -> JSON
    GET  /api/match?color=hex    closest inks -> JSON
    POST /<swatch_id>/reextract  rerun extraction               (login)
    POST /<swatch_id>/use-color  set the ink's color to this    (login)
    POST /<swatch_id>/delete     delete a swatch photo          (login)
"""
from __future__ import annotations

import math
import re
import sqlite3
import uuid
from functools import wraps
from pathlib import Path

from flask import (Blueprint, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from PIL import Image, ImageOps, UnidentifiedImageError

from .swatch_colors import (ciede2000, extract_swatch, hex_to_lab, save_swatch,
                            shading_level)

bp = Blueprint("swatches", __name__, url_prefix="/swatches")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
STORE_SUBFOLDER = "uploads/swatches"  # relative to static/, like your other uploads

# Preset colors on the gallery's "Find by color" panel.
TARGETS = [
    ("Garnet", "#7A1F2B"), ("Teal", "#1F7A78"), ("Ocean blue", "#1F5FA8"),
    ("Forest", "#2F5A36"), ("Amber", "#D9822B"), ("Plum", "#5A2E6E"),
]
FILTERS = [("sheen", "Sheen"), ("shimmer", "Shimmer"), ("shading", "Shading"),
           ("inked", "Inked now"), ("measured", "Has swatch photo")]


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
def init_swatches(app, db_path, static_dir):
    """Register the blueprint and create its tables if they don't exist."""
    app.config["SWATCH_DB"] = str(db_path)
    app.config["SWATCH_STATIC_DIR"] = str(static_dir)
    (Path(static_dir) / STORE_SUBFOLDER).mkdir(parents=True, exist_ok=True)

    schema = (Path(__file__).parent / "schema.sql").read_text()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
        # Databases created by an earlier draft of this feature lack `crop`.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(swatches)")]
        if "crop" not in cols:
            conn.execute("ALTER TABLE swatches ADD COLUMN crop TEXT")

    app.register_blueprint(bp)
    app.teardown_appcontext(_close_db)


def login_required(view):
    """Same behavior as app.py's login_required; JSON routes get a 403 instead."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            if "/api/" in request.path:
                return jsonify(error="Log in to make changes."), 403
            flash("Log in to make changes.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def get_db() -> sqlite3.Connection:
    if "swatch_db" not in g:
        conn = sqlite3.connect(current_app.config["SWATCH_DB"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.swatch_db = conn
    return g.swatch_db


def _close_db(_exc=None):
    conn = g.pop("swatch_db", None)
    if conn is not None:
        conn.close()


def delete_photos_for_ink(ink_id: int) -> None:
    """Remove an ink's swatch photo files from disk. app.py calls this before
    deleting an ink; the database rows go by ON DELETE CASCADE."""
    conn = sqlite3.connect(current_app.config["SWATCH_DB"])
    try:
        for (path,) in conn.execute("SELECT photo_path FROM swatches WHERE ink_id = ?", (ink_id,)):
            _abs(path).unlink(missing_ok=True)
    finally:
        conn.close()


def swatch_summary(ink_id: int):
    """Newest swatch's colors and the photo count for one ink, for the strip on
    the regular ink page. Returns None when the ink has no swatch photos."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM swatches WHERE ink_id = ?", (ink_id,)).fetchone()[0]
    if not count:
        return None
    colors = conn.execute("""
        SELECT c.hex, c.role, c.share FROM swatch_colors c
        WHERE c.swatch_id = (SELECT MAX(id) FROM swatches WHERE ink_id = ?)
        ORDER BY c.rank""", (ink_id,)).fetchall()
    return {"count": count, "colors": [dict(c) for c in colors]}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _abs(rel_path: str) -> Path:
    return Path(current_app.config["SWATCH_STATIC_DIR"]) / rel_path


def _norm_hex(value):
    if not value:
        return None
    m = HEX_RE.match(value.strip())
    return f"#{m.group(1).upper()}" if m else None


def _hue_key(lab):
    """Sort key for a spectrum: by hue angle, low-chroma inks (blacks, greys) last."""
    L, a, b = lab
    if math.hypot(a, b) < 8:
        return (1, -L)
    hue = math.degrees(math.atan2(b, a)) % 360
    return (0, (hue - 350) % 360)  # start at deep red so reds lead and purples close


def _load_collection(conn) -> list[dict]:
    """Every ink, with its newest swatch's colors. Inks without a swatch photo
    fall back to the color picked on the ink form (marked measured=False)."""
    inks = {}
    for r in conn.execute("""
        SELECT i.id, i.name, i.brand, i.color_hex, i.sheen, i.shimmer, i.shading, i.status,
               EXISTS (SELECT 1 FROM pens p WHERE p.current_ink_id = i.id) AS inked
        FROM inks i
    """):
        hx = _norm_hex(r["color_hex"]) or "#22223B"
        inks[r["id"]] = {
            "ink_id": r["id"], "name": r["name"], "brand": r["brand"] or "",
            "status": r["status"], "sheen": r["sheen"], "shimmer": r["shimmer"],
            "shading": r["shading"], "inked": bool(r["inked"]),
            "hex": hx, "lab": hex_to_lab(hx), "shades": [], "measured": False,
        }
    for r in conn.execute("""
        SELECT s.ink_id, c.rank, c.hex, c.lab_l, c.lab_a, c.lab_b
        FROM swatches s JOIN swatch_colors c ON c.swatch_id = s.id
        WHERE s.id = (SELECT MAX(id) FROM swatches WHERE ink_id = s.ink_id)
        ORDER BY s.ink_id, c.rank
    """):
        ink = inks.get(r["ink_id"])
        if ink is None:
            continue
        if r["rank"] == 0:
            ink.update(hex=r["hex"], lab=(r["lab_l"], r["lab_a"], r["lab_b"]), measured=True)
        elif len(ink["shades"]) < 3:
            ink["shades"].append(r["hex"])
    return list(inks.values())


def _rank_by(collection, target_lab, limit, exclude=None):
    scored = [dict(ink, delta_e=round(ciede2000(target_lab, ink["lab"]), 1))
              for ink in collection if ink["ink_id"] != exclude]
    scored.sort(key=lambda k: k["delta_e"])
    for k in scored:
        k["closeness"] = max(4, round(100 - k["delta_e"] * 2))  # bar width for the UI
    return scored[:limit]


def _crop_from_form(form, img_size):
    """The upload page sends the crop as fractions 0..1 (cx, cy, cw, ch)."""
    try:
        cx, cy, cw, ch = (float(form.get(k, "")) for k in ("cx", "cy", "cw", "ch"))
    except ValueError:
        return None
    if cw <= 0.02 or ch <= 0.02:
        return None
    W, H = img_size
    box = (max(0, int(cx * W)), max(0, int(cy * H)),
           min(W, int((cx + cw) * W)), min(H, int((cy + ch) * H)))
    return box if box[2] > box[0] and box[3] > box[1] else None


def _open_photo(file_storage, max_side):
    ext = Path(file_storage.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("Swatch photo must be a JPG, PNG or WEBP file.")
    try:
        img = Image.open(file_storage.stream)
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError("That file doesn't look like an image.")
    img = ImageOps.exif_transpose(img).convert("RGB")  # phone photos come in sideways
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img


def _write_colors(conn, swatch_id, result):
    conn.execute("DELETE FROM swatch_colors WHERE swatch_id = ?", (swatch_id,))
    conn.executemany(
        "INSERT INTO swatch_colors (swatch_id, rank, role, hex, lab_l, lab_a, lab_b, share) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(swatch_id, c["rank"], c["role"], c["hex"], *c["lab"], c["share"])
         for c in result["colors"]])


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@bp.get("/")
def gallery():
    conn = get_db()
    collection = _load_collection(conn)
    sort = "brand" if request.args.get("sort") == "brand" else "spectrum"
    active = [f for f in request.args.getlist("f") if f in dict(FILTERS)]
    target_hex = _norm_hex(request.args.get("color")) or TARGETS[0][1]
    target_label = next((n for n, h in TARGETS if h == target_hex), "Custom color")

    shown = [k for k in collection if all(k[f] for f in active)]
    by_hue = sorted(shown, key=lambda k: _hue_key(k["lab"]))
    grid = by_hue if sort == "spectrum" else sorted(
        shown, key=lambda k: (k["brand"].lower(), k["name"].lower()))

    # Query-string builder for links that change one setting and keep the rest.
    def qs(**changes):
        params = {"color": target_hex[1:], "sort": sort, "f": active}
        params.update(changes)
        return url_for("swatches.gallery", **{k: v for k, v in params.items() if v})

    filters = [{"key": k, "label": label, "on": k in active,
                "url": qs(f=[x for x in active if x != k] if k in active else active + [k])}
               for k, label in FILTERS]

    return render_template(
        "swatches/gallery.html",
        spectrum=by_hue, grid=grid, matches=_rank_by(shown, hex_to_lab(target_hex), 5),
        sort=sort, filters=filters, qs=qs, any_filter=bool(active),
        target={"hex": target_hex, "label": target_label}, targets=TARGETS,
        total=len(collection), measured=sum(k["measured"] for k in collection),
    )


@bp.get("/ink/<int:ink_id>")
def ink_detail(ink_id):
    conn = get_db()
    ink = conn.execute("SELECT * FROM inks WHERE id = ?", (ink_id,)).fetchone()
    if ink is None:
        flash("That ink doesn't exist.", "error")
        return redirect(url_for("swatches.gallery"))
    swatches = conn.execute(
        "SELECT * FROM swatches WHERE ink_id = ? ORDER BY id DESC", (ink_id,)).fetchall()
    if not swatches:
        if session.get("logged_in"):
            return redirect(url_for("swatches.new_swatch", ink=ink_id))
        flash("No swatch photo for that ink yet.", "error")
        return redirect(url_for("ink_detail", ink_id=ink_id))

    wanted = request.args.get("swatch", type=int)
    current = next((s for s in swatches if s["id"] == wanted), swatches[0])
    colors = [dict(r) for r in conn.execute(
        "SELECT * FROM swatch_colors WHERE swatch_id = ? ORDER BY rank", (current["id"],))]
    for c in colors:
        c["lab"] = (c["lab_l"], c["lab_a"], c["lab_b"])

    similar = _rank_by(_load_collection(conn), colors[0]["lab"], 4, exclude=ink_id) if colors else []
    return render_template(
        "swatches/detail.html", ink=ink, swatch=current, swatches=swatches,
        colors=colors, similar=similar,
        shading=shading_level(colors) if colors else "—",
        color_matches=bool(colors) and (ink["color_hex"] or "").upper() == colors[0]["hex"],
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_swatch():
    conn = get_db()
    inks = conn.execute("SELECT id, name, brand FROM inks ORDER BY brand, name").fetchall()

    if request.method == "POST":
        ink_id = request.form.get("ink_id", type=int)
        photo = request.files.get("photo")
        if not ink_id or not conn.execute("SELECT 1 FROM inks WHERE id = ?", (ink_id,)).fetchone():
            flash("Pick which ink this swatch is.", "error")
        elif not photo or not photo.filename:
            flash("Choose a photo of the swatch.", "error")
        else:
            try:
                img = _open_photo(photo, 2400)
            except ValueError as e:
                flash(str(e), "error")
            else:
                rel = f"{STORE_SUBFOLDER}/{uuid.uuid4().hex}.jpg"
                img.save(_abs(rel), "JPEG", quality=90)
                crop = _crop_from_form(request.form, img.size)
                try:
                    result = extract_swatch(_abs(rel), crop=crop)
                except ValueError as e:
                    _abs(rel).unlink(missing_ok=True)
                    flash(str(e), "error")
                else:
                    save_swatch(conn, ink_id, rel, result, crop=crop,
                                paper=(request.form.get("paper") or "").strip() or None,
                                notes=(request.form.get("notes") or "").strip() or None)
                    flash("Swatch added.", "success")
                    return redirect(url_for("swatches.ink_detail", ink_id=ink_id))

    return render_template("swatches/upload.html", inks=inks,
                           preselect=request.args.get("ink", type=int)
                           or request.form.get("ink_id", type=int))


def _get_swatch_or_404(swatch_id):
    sw = get_db().execute("SELECT * FROM swatches WHERE id = ?", (swatch_id,)).fetchone()
    if sw is None:
        flash("That swatch doesn't exist.", "error")
    return sw


@bp.post("/<int:swatch_id>/reextract")
@login_required
def reextract(swatch_id):
    sw = _get_swatch_or_404(swatch_id)
    if sw is None:
        return redirect(url_for("swatches.gallery"))
    crop = tuple(int(v) for v in sw["crop"].split(",")) if sw["crop"] else None
    try:
        result = extract_swatch(_abs(sw["photo_path"]), crop=crop)
    except (ValueError, OSError) as e:
        flash(f"Couldn't re-extract: {e}", "error")
    else:
        conn = get_db()
        gr, gg, gb = result["gains"]
        conn.execute("UPDATE swatches SET wb_r = ?, wb_g = ?, wb_b = ? WHERE id = ?",
                     (gr, gg, gb, swatch_id))
        _write_colors(conn, swatch_id, result)
        conn.commit()
        flash("Colors re-extracted.", "success")
    return redirect(url_for("swatches.ink_detail", ink_id=sw["ink_id"], swatch=swatch_id))


@bp.post("/<int:swatch_id>/use-color")
@login_required
def use_color(swatch_id):
    """Copy this swatch's dominant color onto the ink, so the bottle icons
    everywhere else in the app show the measured color."""
    sw = _get_swatch_or_404(swatch_id)
    if sw is None:
        return redirect(url_for("swatches.gallery"))
    conn = get_db()
    row = conn.execute("SELECT hex FROM swatch_colors WHERE swatch_id = ? AND rank = 0",
                       (swatch_id,)).fetchone()
    if row:
        conn.execute("UPDATE inks SET color_hex = ? WHERE id = ?", (row["hex"], sw["ink_id"]))
        conn.commit()
        flash(f"Ink color set to {row['hex']}.", "success")
    return redirect(url_for("swatches.ink_detail", ink_id=sw["ink_id"], swatch=swatch_id))


@bp.post("/<int:swatch_id>/delete")
@login_required
def delete_swatch(swatch_id):
    sw = _get_swatch_or_404(swatch_id)
    if sw is None:
        return redirect(url_for("swatches.gallery"))
    conn = get_db()
    conn.execute("DELETE FROM swatches WHERE id = ?", (swatch_id,))
    conn.commit()
    _abs(sw["photo_path"]).unlink(missing_ok=True)
    flash("Swatch photo removed.", "success")
    if conn.execute("SELECT 1 FROM swatches WHERE ink_id = ?", (sw["ink_id"],)).fetchone():
        return redirect(url_for("swatches.ink_detail", ink_id=sw["ink_id"]))
    return redirect(url_for("ink_detail", ink_id=sw["ink_id"]))


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------
@bp.post("/api/preview")
@login_required
def api_preview():
    """Extract colors from an uploaded photo without saving anything."""
    photo = request.files.get("photo")
    if not photo:
        return jsonify(error="No photo"), 400
    tmp = _abs(f"{STORE_SUBFOLDER}/preview-{uuid.uuid4().hex}.jpg")
    try:
        img = _open_photo(photo, 1200)
        img.save(tmp, "JPEG", quality=90)
        result = extract_swatch(tmp, crop=_crop_from_form(request.form, img.size))
    except ValueError as e:
        return jsonify(error=str(e)), 422
    finally:
        tmp.unlink(missing_ok=True)

    measured = [k for k in _load_collection(get_db()) if k["measured"]]
    suggestions = _rank_by(measured, result["colors"][0]["lab"], 3)
    return jsonify(
        colors=result["colors"], shading=shading_level(result["colors"]),
        suggestions=[{"ink_id": s["ink_id"], "name": s["name"], "brand": s["brand"],
                      "delta_e": s["delta_e"]} for s in suggestions],
    )


@bp.get("/api/match")
def api_match():
    target = _norm_hex(request.args.get("color"))
    if not target:
        return jsonify(error="Pass ?color=RRGGBB"), 400
    limit = min(request.args.get("limit", 5, type=int), 50)
    matches = _rank_by(_load_collection(get_db()), hex_to_lab(target), limit)
    return jsonify(target=target, matches=[
        {"ink_id": m["ink_id"], "name": m["name"], "brand": m["brand"], "hex": m["hex"],
         "measured": m["measured"], "delta_e": m["delta_e"]} for m in matches])
