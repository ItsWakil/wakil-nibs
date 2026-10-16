"""
swatch_colors.py — color extraction and color search for the Wakil Nibs swatch gallery.

Pipeline:
    photo -> downsize -> white-balance from the paper -> drop paper pixels
          -> k-means in Lab space -> roles (dominant, pooling, ...) -> SQLite

Dependencies: Pillow, numpy  (pip install pillow numpy)
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

# --------------------------------------------------------------------------
# Color space conversions (sRGB <-> CIE Lab, D65), vectorized with numpy
# --------------------------------------------------------------------------
_M_RGB2XYZ = np.array([[0.4124, 0.3576, 0.1805],
                       [0.2126, 0.7152, 0.0722],
                       [0.0193, 0.1192, 0.9505]])
_M_XYZ2RGB = np.linalg.inv(_M_RGB2XYZ)
_WHITE = np.array([0.95047, 1.0, 1.08883])


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """rgb: (..., 3) array of 0-255 values -> (..., 3) Lab."""
    c = np.asarray(rgb, dtype=float) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = (c @ _M_RGB2XYZ.T) / _WHITE
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    """(..., 3) Lab -> (..., 3) 0-255 sRGB (clipped)."""
    lab = np.asarray(lab, dtype=float)
    fy = (lab[..., 0] + 16) / 116
    fx = fy + lab[..., 1] / 500
    fz = fy - lab[..., 2] / 200
    f = np.stack([fx, fy, fz], axis=-1)
    xyz = np.where(f ** 3 > 0.008856, f ** 3, (f - 16 / 116) / 7.787) * _WHITE
    c = xyz @ _M_XYZ2RGB.T
    c = np.clip(c, 0, 1)
    c = np.where(c > 0.0031308, 1.055 * c ** (1 / 2.4) - 0.055, 12.92 * c)
    return np.clip(np.round(c * 255), 0, 255)


def hex_to_rgb(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=float)


def rgb_to_hex(rgb) -> str:
    r, g, b = (int(v) for v in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def hex_to_lab(h: str) -> tuple[float, float, float]:
    return tuple(float(v) for v in rgb_to_lab(hex_to_rgb(h)))


# --------------------------------------------------------------------------
# CIEDE2000 color difference
#   ~1   : barely perceptible
#   ~2-3 : noticeable side by side
#   10+  : clearly different inks
# --------------------------------------------------------------------------
def ciede2000(lab1, lab2) -> float:
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cb7 = ((C1 + C2) / 2) ** 7
    G = 0.5 * (1 - math.sqrt(Cb7 / (Cb7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360

    dLp = L2 - L1
    dCp = C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp / 2))

    Lbp = (L1 + L2) / 2
    Cbp = (C1p + C2p) / 2
    if C1p * C2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) > 180:
        hbp = (h1p + h2p + 360) / 2 if (h1p + h2p) < 360 else (h1p + h2p - 360) / 2
    else:
        hbp = (h1p + h2p) / 2

    T = (1 - 0.17 * math.cos(math.radians(hbp - 30))
         + 0.24 * math.cos(math.radians(2 * hbp))
         + 0.32 * math.cos(math.radians(3 * hbp + 6))
         - 0.20 * math.cos(math.radians(4 * hbp - 63)))
    d_theta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    Cbp7 = Cbp ** 7
    Rc = 2 * math.sqrt(Cbp7 / (Cbp7 + 25 ** 7))
    Sl = 1 + (0.015 * (Lbp - 50) ** 2) / math.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -math.sin(math.radians(2 * d_theta)) * Rc

    return math.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
                     + Rt * (dCp / Sc) * (dHp / Sh))


# --------------------------------------------------------------------------
# Image prep
# --------------------------------------------------------------------------
def load_image(path: str | Path, max_side: int = 600,
               crop: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """Open a photo, fix phone rotation, optionally crop (left, top, right, bottom),
    and shrink so the longest side is max_side. Returns an (H, W, 3) float array."""
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if crop:
        img = img.crop(crop)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    return np.asarray(img, dtype=float)


def white_balance(arr: np.ndarray, target: float = 248.0):
    """Estimate the paper color from the brightest low-saturation pixels and
    scale each channel so the paper reads as neutral white.
    Returns (corrected_array, gains)."""
    px = arr.reshape(-1, 3)
    mx, mn = px.max(axis=1), px.min(axis=1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    lum = px @ np.array([0.2126, 0.7152, 0.0722])

    neutral = sat < 0.25
    pool = lum[neutral] if neutral.sum() > 100 else lum
    cutoff = np.percentile(pool, 95)
    mask = (lum >= cutoff) & (neutral if neutral.sum() > 100 else True)
    paper = px[mask].mean(axis=0)

    gains = target / np.maximum(paper, 1)
    # Guard against absurd corrections from a bad photo.
    gains = np.clip(gains, 0.6, 1.8)
    return np.clip(arr * gains, 0, 255), tuple(float(g) for g in gains)


def ink_pixels(lab_px: np.ndarray, paper_l: float = 82.0, paper_c: float = 12.0) -> np.ndarray:
    """Drop pixels that look like bare paper (very light and nearly neutral)."""
    chroma = np.hypot(lab_px[:, 1], lab_px[:, 2])
    keep = ~((lab_px[:, 0] > paper_l) & (chroma < paper_c))
    return lab_px[keep]


# --------------------------------------------------------------------------
# k-means (k-means++ init), no scikit-learn needed
# --------------------------------------------------------------------------
def kmeans(X: np.ndarray, k: int, iters: int = 30, seed: int = 0):
    rng = np.random.default_rng(seed)
    centers = [X[rng.integers(len(X))]]
    for _ in range(1, k):
        d = ((X[:, None, :] - np.array(centers)[None]) ** 2).sum(-1).min(axis=1)
        if d.sum() == 0:
            break
        centers.append(X[rng.choice(len(X), p=d / d.sum())])
    C = np.array(centers)

    for _ in range(iters):
        labels = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(axis=1)
        newC = np.array([X[labels == j].mean(axis=0) if np.any(labels == j) else C[j]
                         for j in range(len(C))])
        if np.allclose(newC, C, atol=0.01):
            C = newC
            break
        C = newC
    dist = ((X[:, None, :] - C[None]) ** 2).sum(-1)
    labels = dist.argmin(axis=1)
    inertia = float(dist.min(axis=1).sum())
    return C, labels, inertia


def best_kmeans(X: np.ndarray, k: int, n_init: int = 8, seed: int = 0,
                min_share: float = 0.02):
    """Run k-means from several starting points and keep the tightest fit,
    preferring runs where no cluster is just a sliver of stray pixels."""
    best = None
    for i in range(n_init):
        C, labels, inertia = kmeans(X, k, seed=seed + i)
        shares = np.bincount(labels, minlength=len(C)) / len(labels)
        slivers = int((shares < min_share).sum())
        key = (slivers, inertia)
        if best is None or key < best[0]:
            best = (key, C, labels)
    _, C, labels = best
    # Drop any remaining slivers and reassign their pixels.
    shares = np.bincount(labels, minlength=len(C)) / len(labels)
    C = C[shares >= min_share] if np.any(shares >= min_share) else C
    labels = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(axis=1)
    return C, labels


# --------------------------------------------------------------------------
# The main entry point
# --------------------------------------------------------------------------
def extract_swatch(path: str | Path, k: int = 4,
                   crop: tuple[int, int, int, int] | None = None,
                   max_samples: int = 20000, seed: int = 0) -> dict:
    """Return {'gains': (r,g,b), 'colors': [ {rank, role, hex, lab, share}, ... ]}."""
    arr = load_image(path, crop=crop)
    arr, gains = white_balance(arr)
    lab_px = rgb_to_lab(arr.reshape(-1, 3))
    ink = ink_pixels(lab_px)
    if len(ink) < 50:
        raise ValueError("Couldn't find enough ink in this photo — try a tighter crop.")

    rng = np.random.default_rng(seed)
    if len(ink) > max_samples:
        ink = ink[rng.choice(len(ink), max_samples, replace=False)]

    centers, labels = best_kmeans(ink, k, seed=seed)
    shares = np.bincount(labels, minlength=len(centers)) / len(labels)

    order = np.argsort(-shares)
    centers, shares = centers[order], shares[order]
    roles = _assign_roles(centers)

    colors = []
    for rank, (c, s, role) in enumerate(zip(centers, shares, roles)):
        colors.append({
            "rank": rank,
            "role": role,
            "hex": rgb_to_hex(lab_to_rgb(c)),
            "lab": tuple(round(float(v), 2) for v in c),
            "share": round(float(s), 4),
        })
    return {"gains": gains, "colors": colors}


def _assign_roles(centers: np.ndarray) -> list[str]:
    """centers are sorted by share (largest first)."""
    n = len(centers)
    roles = ["Mid strokes"] * n
    roles[0] = "Dominant"
    rest = list(range(1, n))
    if rest:
        darkest = min(rest, key=lambda i: centers[i][0])
        roles[darkest] = "Pooling"
        rest.remove(darkest)
    if rest:
        lightest = max(rest, key=lambda i: centers[i][0])
        roles[lightest] = "Light strokes"
    return roles


def shading_level(colors: list[dict]) -> str:
    """Rough shading rating from the lightness spread between clusters."""
    Ls = [c["lab"][0] for c in colors]
    spread = max(Ls) - min(Ls)
    return "Strong" if spread > 20 else "Moderate" if spread > 10 else "Low"


# --------------------------------------------------------------------------
# SQLite helpers (match schema.sql). Assumes inks(id, name, brand).
# --------------------------------------------------------------------------
def save_swatch(conn: sqlite3.Connection, ink_id: int, photo_path: str,
                result: dict, paper: str | None = None, notes: str | None = None,
                crop: tuple[int, int, int, int] | None = None) -> int:
    gr, gg, gb = result["gains"]
    cur = conn.execute(
        "INSERT INTO swatches (ink_id, photo_path, paper, notes, wb_r, wb_g, wb_b, crop) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ink_id, photo_path, paper, notes, gr, gg, gb,
         ",".join(str(int(v)) for v in crop) if crop else None),
    )
    swatch_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO swatch_colors (swatch_id, rank, role, hex, lab_l, lab_a, lab_b, share) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(swatch_id, c["rank"], c["role"], c["hex"], *c["lab"], c["share"])
         for c in result["colors"]],
    )
    conn.commit()
    return swatch_id


def closest_inks(conn: sqlite3.Connection, target_hex: str, limit: int = 5,
                 exclude_ink_id: int | None = None) -> list[dict]:
    """Rank inks by CIEDE2000 distance from their dominant color to target_hex.
    A collection of hundreds of inks is instant in plain Python."""
    target = hex_to_lab(target_hex)
    rows = conn.execute(
        "SELECT d.ink_id, i.name, i.brand, d.hex, d.lab_l, d.lab_a, d.lab_b "
        "FROM ink_dominant d JOIN inks i ON i.id = d.ink_id"
    ).fetchall()
    scored = []
    for ink_id, name, brand, hx, L, a, b in rows:
        if ink_id == exclude_ink_id:
            continue
        scored.append({"ink_id": ink_id, "name": name, "brand": brand, "hex": hx,
                       "delta_e": round(ciede2000(target, (L, a, b)), 1)})
    scored.sort(key=lambda r: r["delta_e"])
    return scored[:limit]


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python swatch_colors.py photo.jpg [k]")
        sys.exit(1)
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    res = extract_swatch(sys.argv[1], k=k)
    print(json.dumps(res, indent=2))
    print("Shading:", shading_level(res["colors"]))
