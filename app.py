"""Wakil Nibs: a small Flask app for tracking fountain pens and inks.

Run locally with `python app.py`, then open http://127.0.0.1:5000.
"""

import hmac
import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
import database as db
from swatches import init_swatches, delete_photos_for_ink, swatch_summary

app = Flask(__name__)

# Signs session cookies. Set PENTRACKER_SECRET_KEY, don't rely on the default.
app.secret_key = os.environ.get(
    "PENTRACKER_SECRET_KEY",
    "change-this-to-something-random-before-sharing-publicly",
)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set PENTRACKER_SECURE_COOKIES=1 once you're serving over HTTPS.
    SESSION_COOKIE_SECURE=os.environ.get("PENTRACKER_SECURE_COOKIES") == "1",
)

# Behind nginx every request looks like it came from 127.0.0.1. Set
# PENTRACKER_TRUST_PROXY=1 so the real client IP (from X-Forwarded-For) is used
# for the login lockout. Only do this when a proxy you control is in front.
if os.environ.get("PENTRACKER_TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Editing password. Viewing needs no login.
ADMIN_PASSWORD_HASH = generate_password_hash(
    os.environ.get("PENTRACKER_PASSWORD", "changeme")
)

# The defaults are in the source, so warn if they're still in use.
if "PENTRACKER_PASSWORD" not in os.environ:
    print("WARNING: PENTRACKER_PASSWORD is not set; the editing password is "
          "'changeme'. Set it before exposing this to anyone.", file=sys.stderr)
if "PENTRACKER_SECRET_KEY" not in os.environ:
    print("WARNING: PENTRACKER_SECRET_KEY is not set; login cookies are signed "
          "with a publicly known key. Set it before exposing this to anyone.",
          file=sys.stderr)

# Uploads
STATIC_DIR = Path(__file__).parent / "static"
PEN_SAMPLE_DIR = STATIC_DIR / "uploads" / "pens"
INK_SAMPLE_DIR = STATIC_DIR / "uploads" / "inks"
PEN_PHOTO_DIR = STATIC_DIR / "uploads" / "pen_photos"
INK_PHOTO_DIR = STATIC_DIR / "uploads" / "ink_photos"
ALLOWED_UPLOAD_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "webm"}
VIDEO_EXTENSIONS = {"webm"}

# 25 MB upload cap (Flask returns a 413).
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

# Swatch gallery (/swatches). Photos go in static/uploads/swatches/.
init_swatches(app, db.DB_PATH, STATIC_DIR)


def _allowed_upload(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOAD_EXTENSIONS


def _save_upload(file_storage, target_dir, url_subfolder, error_label="Image"):
    """
    Saves an uploaded image or .webm video to disk with a random,
    collision-proof filename. Returns the path to store in the database
    (relative to the static/ folder), or None if no valid file was given.
    """
    if not file_storage or file_storage.filename == "":
        return None
    if not _allowed_upload(file_storage.filename):
        flash(f"{error_label} must be a PNG, JPG, GIF, WEBP, or WEBM file.", "error")
        return None
    extension = file_storage.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{extension}"
    target_dir.mkdir(parents=True, exist_ok=True)
    file_storage.save(target_dir / filename)
    return f"uploads/{url_subfolder}/{filename}"


def _delete_upload(relative_path):
    """Removes a previously-uploaded file from disk, if it exists."""
    if not relative_path:
        return
    full_path = STATIC_DIR / relative_path
    if full_path.exists():
        try:
            full_path.unlink()
        except OSError:
            pass


def _resolve_image_field(existing_row, field_name, remove_flag_name, file_field_name,
                          target_dir, url_subfolder, error_label):
    """
    Shared logic for both the handwriting-sample and photo fields on both
    forms: start from whatever's already saved, clear it if the "remove"
    checkbox was ticked, then let a newly-uploaded file replace it. Returns
    the path that should end up in the database.
    """
    current_path = existing_row[field_name] if existing_row else None

    if request.form.get(remove_flag_name):
        _delete_upload(current_path)
        current_path = None

    uploaded_file = request.files.get(file_field_name)
    new_path = _save_upload(uploaded_file, target_dir, url_subfolder, error_label)
    if new_path:
        _delete_upload(current_path)  # replace, don't accumulate old files
        current_path = new_path

    return current_path


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not hmac.compare_digest(sent, session.get("_csrf", "")):
            return ("Your session expired or the form was invalid. "
                    "Go back, reload the page and try again.", 400)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def safe_next(url):
    """Only follow redirects to a path on this site."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return url_for("dashboard")


# Login lockout: 5 bad passwords from one IP locks that IP out for 15 minutes.
# Kept in memory per worker, so it resets on restart and each gunicorn worker
# counts separately. Good enough to stop casual guessing.
FAIL_LIMIT = 5
LOCKOUT_SECONDS = 15 * 60
_login_failures = {}


def _locked_out(ip):
    entry = _login_failures.get(ip)
    if not entry:
        return False
    count, first = entry
    if time.time() - first > LOCKOUT_SECONDS:
        _login_failures.pop(ip, None)
        return False
    return count >= FAIL_LIMIT


def login_required(view_func):
    """Decorator for routes that add/edit/delete data. Redirects to /login
    if the visitor hasn't entered the password yet."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            flash("Log in to make changes.", "error")
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_logged_in():
    # `logged_in` in every template
    return dict(logged_in=session.get("logged_in", False))


@app.context_processor
def inject_asset_version():
    """
    Makes an `asset_version()` function available in templates, used as a
    cache-busting query string on the stylesheet link (e.g. style.css?v=173...).
    It's based on the CSS file's last-modified time, so every deploy that
    actually changes style.css gets a new version automatically -- no more
    "I updated the CSS but the browser/nginx is still showing the old one."
    """
    def asset_version():
        try:
            css_path = STATIC_DIR / "css" / "style.css"
            return str(int(css_path.stat().st_mtime))
        except OSError:
            return "1"
    return dict(asset_version=asset_version)


@app.template_filter("trim_zero")
def trim_zero(value):
    """Display 50.0 as '50' but keep 33.5 as '33.5' -- just tidies up numbers in templates."""
    if value is None:
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else str(value)


@app.template_filter("is_video")
def is_video_filter(path):
    """True if an uploaded file path is a video (.webm) rather than an image."""
    if not path:
        return False
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return extension in VIDEO_EXTENSIONS


@app.template_filter("money")
def money_filter(value):
    """Formats a numeric value as currency, e.g. 125 -> '$125.00'. Blank/None -> '—'."""
    if value is None or value == "":
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    return "${:,.2f}".format(value)


NIB_SIZES = ["EF", "F", "M", "B", "BB", "Stub", "Italic", "Flex", "Music", "Other"]
NIB_MATERIALS = ["Steel", "Gold", "Titanium", "Palladium", "Bronze", "Other"]
INK_TYPES = ["Dye", "Pigment", "Iron Gall", "Bulletproof", "Other"]

# Status flags. "active" gets no badge; only the exceptions do.
PEN_STATUSES = [
    ("active", "Active"),
    ("loaned", "Loaned Out"),
    ("servicing", "Being Serviced"),
    ("broken", "Broken"),
    ("sold", "Sold"),
]
INK_STATUSES = [
    ("active", "Active"),
    ("sold", "Sold"),
    ("empty", "Empty"),
]
PEN_STATUS_LABELS = dict(PEN_STATUSES)
INK_STATUS_LABELS = dict(INK_STATUSES)


@app.context_processor
def inject_status_labels():
    # status value -> display label, for templates
    return dict(pen_status_labels=PEN_STATUS_LABELS, ink_status_labels=INK_STATUS_LABELS)


@app.before_request
def ensure_db():
    # make sure tables and new columns exist
    db.init_db()
    db.migrate_db()


# Login / logout

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _locked_out(ip):
            flash("Too many wrong passwords. Try again in a few minutes.", "error")
            return render_template("login.html", next_url=""), 429
        submitted = request.form.get("password", "")
        if check_password_hash(ADMIN_PASSWORD_HASH, submitted):
            _login_failures.pop(ip, None)
            session.clear()
            session["logged_in"] = True
            flash("Logged in.", "success")
            return redirect(safe_next(request.form.get("next")))
        count, first = _login_failures.get(ip, (0, time.time()))
        _login_failures[ip] = (count + 1, first)
        flash("Wrong password.", "error")
    next_url = safe_next(request.args.get("next")) if request.args.get("next") else ""
    return render_template("login.html", next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("logged_in", None)
    flash("Logged out.", "success")
    return redirect(url_for("dashboard"))


# Dashboard

@app.route("/")
def dashboard():
    inked_pens = db.get_inked_pens()
    all_pens = db.get_all_pens()
    all_inks = db.get_all_inks()
    pen_value_total = db.get_total_pen_value()
    ink_value_total = db.get_total_ink_value()
    ink_ml_total = db.get_total_ink_ml()
    return render_template(
        "index.html",
        inked_pens=inked_pens,
        pen_count=len(all_pens),
        ink_count=len(all_inks),
        pen_value_total=pen_value_total,
        ink_value_total=ink_value_total,
        collection_value_total=pen_value_total + ink_value_total,
        ink_ml_total=ink_ml_total,
    )


# Pens

@app.route("/pens")
def pens_list():
    sort = request.args.get("sort")
    direction = request.args.get("dir", "asc")
    view = request.args.get("view", "list")
    if view not in ("list", "grid"):
        view = "list"
    pens = db.get_all_pens(sort=sort, direction=direction)
    inks = db.get_all_inks()
    return render_template(
        "pens.html", pens=pens, inks=inks,
        current_sort=sort, current_dir=direction, current_view=view,
    )


@app.route("/pens/<int:pen_id>")
def pen_detail(pen_id):
    pen = db.get_pen(pen_id)
    if pen is None:
        flash("That pen doesn't exist.", "error")
        return redirect(url_for("pens_list"))
    ink = db.get_ink(pen["current_ink_id"]) if pen["current_ink_id"] else None
    images = db.get_pen_images(pen_id)
    return render_template("pen_detail.html", pen=pen, ink=ink, images=images)


@app.route("/pens/new", methods=["GET", "POST"])
@login_required
def pen_new():
    if request.method == "POST":
        _save_pen_form()
        flash("Pen added to your collection.", "success")
        return redirect(url_for("pens_list"))
    inks = db.get_all_inks()
    return render_template(
        "pen_form.html", pen=None, inks=inks, images=[],
        nib_sizes=NIB_SIZES, nib_materials=NIB_MATERIALS, pen_statuses=PEN_STATUSES,
    )


@app.route("/pens/<int:pen_id>/edit", methods=["GET", "POST"])
@login_required
def pen_edit(pen_id):
    if request.method == "POST":
        _save_pen_form(pen_id)
        flash("Pen updated.", "success")
        return redirect(url_for("pens_list"))
    pen = db.get_pen(pen_id)
    if pen is None:
        flash("That pen doesn't exist.", "error")
        return redirect(url_for("pens_list"))
    inks = db.get_all_inks()
    images = db.get_pen_images(pen_id)
    return render_template(
        "pen_form.html", pen=pen, inks=inks, images=images,
        nib_sizes=NIB_SIZES, nib_materials=NIB_MATERIALS, pen_statuses=PEN_STATUSES,
    )


@app.route("/pens/<int:pen_id>/images/<int:image_id>/delete", methods=["POST"])
@login_required
def pen_image_delete(pen_id, image_id):
    image_path = db.delete_pen_image(image_id)
    _delete_upload(image_path)
    flash("Photo removed.", "success")
    return redirect(request.referrer or url_for("pen_edit", pen_id=pen_id))


@app.route("/pens/<int:pen_id>/delete", methods=["POST"])
@login_required
def pen_delete(pen_id):
    pen = db.get_pen(pen_id)
    if pen:
        _delete_upload(pen["handwriting_sample_path"])
        # remove gallery files from disk first; rows go by cascade
        for image in db.get_pen_images(pen_id):
            _delete_upload(image["image_path"])
    db.delete_pen(pen_id)
    flash("Pen removed.", "success")
    return redirect(url_for("pens_list"))


@app.route("/pens/<int:pen_id>/ink", methods=["POST"])
@login_required
def pen_ink(pen_id):
    """Quick-action from the dashboard/pens table: load or empty a pen."""
    ink_id = request.form.get("ink_id") or None
    db.set_pen_ink(pen_id, ink_id)
    flash("Pen updated.", "success")
    return redirect(request.referrer or url_for("pens_list"))


def _save_pen_form(pen_id=None):
    ink_id = request.form.get("current_ink_id") or None
    existing_pen = db.get_pen(pen_id) if pen_id else None

    sample_path = _resolve_image_field(
        existing_pen, "handwriting_sample_path", "remove_sample",
        "handwriting_sample", PEN_SAMPLE_DIR, "pens", "Handwriting sample",
    )

    value_raw = request.form.get("value", "").strip()
    status = request.form.get("status", "active").strip()
    if status not in PEN_STATUS_LABELS:
        status = "active"

    data = {
        "name": request.form["name"].strip(),
        "brand": request.form.get("brand", "").strip(),
        "description": request.form.get("description", "").strip(),
        "nib_size": request.form.get("nib_size", "").strip(),
        "nib_material": request.form.get("nib_material", "").strip(),
        "model_url": request.form.get("model_url", "").strip(),
        "date_acquired": request.form.get("date_acquired", "").strip(),
        "notes": request.form.get("notes", "").strip(),
        "current_ink_id": ink_id,
        "handwriting_sample_path": sample_path,
        "value": float(value_raw) if value_raw else None,
        "status": status,
    }
    if pen_id is None:
        pen_id = db.create_pen(data)
    else:
        db.update_pen(pen_id, data)

    # getlist(): the multiple-file input sends one entry per file
    for uploaded_file in request.files.getlist("pen_photos"):
        new_path = _save_upload(uploaded_file, PEN_PHOTO_DIR, "pen_photos", "Pen photo/video")
        if new_path:
            db.add_pen_image(pen_id, new_path)


# Inks

@app.route("/inks")
def inks_list():
    sort = request.args.get("sort")
    direction = request.args.get("dir", "asc")
    view = request.args.get("view", "grid")
    if view not in ("list", "grid"):
        view = "grid"
    inks = db.get_all_inks(sort=sort, direction=direction)
    return render_template(
        "inks.html", inks=inks,
        current_sort=sort, current_dir=direction, current_view=view,
    )


@app.route("/inks/<int:ink_id>")
def ink_detail(ink_id):
    ink = db.get_ink(ink_id)
    if ink is None:
        flash("That ink doesn't exist.", "error")
        return redirect(url_for("inks_list"))
    pens_using = db.get_pens_by_ink(ink_id)
    return render_template("ink_detail.html", ink=ink, pens_using=pens_using,
                           swatch=swatch_summary(ink_id))


@app.route("/inks/new", methods=["GET", "POST"])
@login_required
def ink_new():
    if request.method == "POST":
        _save_ink_form()
        flash("Ink added to your collection.", "success")
        return redirect(url_for("inks_list"))
    return render_template("ink_form.html", ink=None, ink_types=INK_TYPES, ink_statuses=INK_STATUSES)


@app.route("/inks/<int:ink_id>/edit", methods=["GET", "POST"])
@login_required
def ink_edit(ink_id):
    if request.method == "POST":
        _save_ink_form(ink_id)
        flash("Ink updated.", "success")
        return redirect(url_for("inks_list"))
    ink = db.get_ink(ink_id)
    if ink is None:
        flash("That ink doesn't exist.", "error")
        return redirect(url_for("inks_list"))
    return render_template("ink_form.html", ink=ink, ink_types=INK_TYPES, ink_statuses=INK_STATUSES)


@app.route("/inks/<int:ink_id>/delete", methods=["POST"])
@login_required
def ink_delete(ink_id):
    ink = db.get_ink(ink_id)
    if ink:
        _delete_upload(ink["handwriting_sample_path"])
        _delete_upload(ink["photo_path"])
        delete_photos_for_ink(ink_id)  # swatch rows go by cascade, files don't
    db.delete_ink(ink_id)
    flash("Ink removed.", "success")
    return redirect(url_for("inks_list"))


def _save_ink_form(ink_id=None):
    bottle_size = request.form.get("bottle_size_ml", "").strip()
    existing_ink = db.get_ink(ink_id) if ink_id else None

    sample_path = _resolve_image_field(
        existing_ink, "handwriting_sample_path", "remove_sample",
        "handwriting_sample", INK_SAMPLE_DIR, "inks", "Handwriting sample",
    )
    photo_path = _resolve_image_field(
        existing_ink, "photo_path", "remove_photo",
        "ink_photo", INK_PHOTO_DIR, "ink_photos", "Ink photo",
    )

    value_raw = request.form.get("value", "").strip()
    status = request.form.get("status", "active").strip()
    if status not in INK_STATUS_LABELS:
        status = "active"

    data = {
        "name": request.form["name"].strip(),
        "brand": request.form.get("brand", "").strip(),
        "color_hex": request.form.get("color_hex", "#22223B").strip(),
        "color_name": request.form.get("color_name", "").strip(),
        "ink_type": request.form.get("ink_type", "Dye").strip(),
        "sheen": 1 if request.form.get("sheen") else 0,
        "shimmer": 1 if request.form.get("shimmer") else 0,
        "shading": 1 if request.form.get("shading") else 0,
        "bottle_size_ml": float(bottle_size) if bottle_size else None,
        "ink_url": request.form.get("ink_url", "").strip(),
        "notes": request.form.get("notes", "").strip(),
        "handwriting_sample_path": sample_path,
        "photo_path": photo_path,
        "value": float(value_raw) if value_raw else None,
        "status": status,
    }
    if ink_id is None:
        db.create_ink(data)
    else:
        db.update_ink(ink_id, data)


if __name__ == "__main__":
    db.init_db()
    db.migrate_db()
    db.seed_sample_data()
    app.run(debug=True)
