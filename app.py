import os, json, sqlite3, secrets, functools, datetime, threading, time, tempfile, shutil, asyncio, io, base64, subprocess, re
from urllib.parse import quote
import edge_tts
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

import anthropic
import requests as http
from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, flash, Response, send_file, after_this_request, stream_with_context
)
from werkzeug.security import generate_password_hash, check_password_hash

from styles import STYLES, DEFAULT_STYLE, get as get_style, list_public as styles_public

app = Flask(__name__)

def _load_or_create_secret_key():
    """SECRET_KEY policy:
    - If env var SECRET_KEY is set, use it (production / multi-process correct).
    - Else, persist a generated key to .flask_secret next to this file so sessions
      survive restarts on a single dev box. Refusing to start would be safer for
      production but breaks the documented `python app.py` quickstart, so we warn.
    """
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flask_secret")
    try:
        with open(secret_path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    new_key = secrets.token_hex(32)
    try:
        with open(secret_path, "w", encoding="utf-8") as f:
            f.write(new_key)
        try:
            os.chmod(secret_path, 0o600)
        except (OSError, NotImplementedError):
            pass  # Windows: best-effort
    except OSError as exc:
        print(f"[anime_app] WARN: could not persist SECRET_KEY to {secret_path}: {exc}", flush=True)
        print("[anime_app] WARN: sessions will be invalidated on every restart.", flush=True)
        return new_key
    print(f"[anime_app] Generated and persisted SECRET_KEY to {secret_path}.", flush=True)
    print("[anime_app] For production, set SECRET_KEY in the environment instead.", flush=True)
    return new_key

app.secret_key = _load_or_create_secret_key()

# Session-cookie hardening. SameSite=Lax blocks the browser from sending the session
# cookie on cross-site POSTs, which is the practical CSRF defense for an app that
# doesn't use CSRF tokens. Secure=True (HTTPS-only) is gated on FLASK_ENV so dev
# over http://localhost still works.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('FLASK_ENV', 'development') == 'production',
)

PAYPAL_CLIENT_ID   = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET      = os.environ.get("PAYPAL_SECRET", "")
# Default to sandbox so a misconfigured server cannot accidentally charge real cards.
# Production must explicitly set PAYPAL_MODE=live.
PAYPAL_MODE        = os.environ.get("PAYPAL_MODE", "sandbox").lower()
if PAYPAL_MODE not in ("sandbox", "live"):
    raise SystemExit(f"Refusing to start: PAYPAL_MODE must be 'sandbox' or 'live', got {PAYPAL_MODE!r}.")
if PAYPAL_MODE == "live" and not (PAYPAL_CLIENT_ID and PAYPAL_SECRET):
    raise SystemExit("Refusing to start: PAYPAL_MODE=live requires both PAYPAL_CLIENT_ID and PAYPAL_SECRET.")
# Credit packs (one-time purchases, works with personal PayPal)
PACK_HUNTER_CREDITS  = 20
PACK_HUNTER_PRICE    = "9.99"
PACK_MONARCH_CREDITS = 60
PACK_MONARCH_PRICE   = "19.99"
PACKS = {
    "hunter":  {"credits": PACK_HUNTER_CREDITS,  "price": PACK_HUNTER_PRICE,  "tier": "hunter"},
    "monarch": {"credits": PACK_MONARCH_CREDITS, "price": PACK_MONARCH_PRICE, "tier": "monarch"},
}

FAL_KEY = os.environ.get("FAL_KEY", "")

# Local ComfyUI bridge (Justen's RTX 4070 Ti SUPER exposed via cloudflared tunnel).
# Opt-in only. Default routing is fal.ai Wan API for everyone because the local
# Wan 2.2 5B has visibly stiff motion vs. fal.ai's hosted Wan 2.5; we don't want
# admin previews to look worse than what paying users get. URL changes each time
# the cloudflared quick tunnel restarts; token is a long random string the local
# startup script generates once and AnimeWonder sends in every request.
COMFY_LOCAL_URL   = os.environ.get("COMFY_LOCAL_URL", "").rstrip("/")
COMFY_LOCAL_TOKEN = os.environ.get("COMFY_LOCAL_TOKEN", "")

# Validate at startup so a missing/typo'd key fails loudly during deploy
# instead of producing cryptic 500s for the first user who hits a Claude-backed route.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
if not ANTHROPIC_API_KEY:
    raise SystemExit(
        "Refusing to start: ANTHROPIC_API_KEY is unset.\n"
        "Get a key at https://console.anthropic.com/settings/keys and set it in .env or the environment."
    )
if not ANTHROPIC_API_KEY.startswith("sk-ant-"):
    print(
        f"[anime_app] WARN: ANTHROPIC_API_KEY does not start with 'sk-ant-' — "
        f"this looks malformed. Continuing anyway in case the key format changed.",
        flush=True,
    )

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
_ADMIN_PASS_DEFAULTS = {"", "changeme", "password", "admin", "123456"}
if ADMIN_PASS in _ADMIN_PASS_DEFAULTS:
    raise SystemExit(
        "Refusing to start: ADMIN_PASS is unset or set to a known-default value.\n"
        "Set a strong ADMIN_PASS in the environment (or .env) before launching.\n"
        "Example PowerShell:  $env:ADMIN_PASS = 'a-long-random-string'\n"
        "Example bash:        export ADMIN_PASS='a-long-random-string'"
    )

DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

# Allow override for production hosts (Render, Fly, etc.) where the only writeable
# location may be a mounted persistent disk, not the source folder.
# ANIMEFORGE_DB_PATH kept as the legacy fallback so the live Render env var
# keeps working through the rename; ANIMEWONDER_DB_PATH wins if both are set.
DB_PATH = os.environ.get("ANIMEWONDER_DB_PATH",
                         os.environ.get("ANIMEFORGE_DB_PATH", DB_PATH))

TIER_LIMITS = {"free": 2, "hunter": 5, "monarch": 100}
SEASON_TIERS = {"monarch", "admin"}
TIER_MODES   = {
    "free":    {"episode"},
    "hunter":  {"episode", "short"},
    "monarch": {"episode", "short", "movie"},
    "admin":   {"episode", "short", "movie"},
}

export_jobs = {}

# Disk-backed export manifest so downloads survive in-memory job loss
# (Render free tier can restart workers; this keeps the file findable)
EXPORT_READY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".export_ready")
try:
    os.makedirs(EXPORT_READY_DIR, exist_ok=True)
except OSError:
    pass


def extract_json(text):
    text = text.strip()
    # Strip markdown code fences
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            if part.startswith("json"):
                part = part[4:]
            part = part.strip()
            if part.startswith("{"):
                text = part
                break
    # Find outermost JSON object
    start = text.find("{")
    if start == -1:
        return text
    # Walk to find the matching closing brace
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    # Response was cut off — return what we have and let the caller handle it
    return text[start:]


def _fal_image(prompt, width, height, model="hunyuan", seed=None):
    """
    Generate a single anime-quality image via fal.ai. Returns JPEG bytes or None.

    Why this exists: Pollinations.ai (free default) produces unreliable anime
    faces — eyes drift off, mouth/nose distort, hair detail muddies. fal.ai's
    Hunyuan 3.0 and ByteDance Seedream 4.5 are the 2026 SOTA for anime
    character art. Admin and paid tiers route through here; free still uses
    Pollinations (cost: $0).

    Models:
      - "hunyuan"  → fal-ai/hunyuan-image/v3/text-to-image  (best for anime / character art)
      - "seedream" → fal-ai/bytedance/seedream/v4/text-to-image  (best general / cinematic)
    Cost is roughly $0.04 per image at the time of writing.
    """
    if not FAL_KEY or _fal_locked():
        return None
    import fal_client

    endpoint = (
        "fal-ai/hunyuan-image/v3/text-to-image"
        if model == "hunyuan"
        else "fal-ai/bytedance/seedream/v4/text-to-image"
    )

    if width == height:
        image_size = "square_hd"
    elif width > height:
        image_size = "landscape_16_9"
    else:
        image_size = "portrait_16_9"

    args = {
        "prompt": prompt[:1500],
        "image_size": image_size,
        "num_images": 1,
        "enable_safety_checker": True,
    }
    if seed is not None:
        args["seed"] = int(seed)

    try:
        result = fal_client.subscribe(endpoint, arguments=args, with_logs=False)
        images = result.get("images") or []
        if images:
            url = images[0].get("url")
            if url:
                r = http.get(url, timeout=90)
                if r.status_code == 200:
                    return r.content
    except Exception as exc:
        msg = str(exc)
        print(f"[fal-image] {model} failed: {msg}", flush=True)
        # Trip the circuit breaker for known unrecoverable conditions so the
        # rest of the export doesn't pay the API latency on every scene.
        if "User is locked" in msg or "Exhausted balance" in msg:
            _mark_fal_locked(f"image gen — {msg[:80]}")
    return None


def _pollinations_image(prompt: str, w: int, h: int, seed: int) -> bytes | None:
    """Fetch a single scene image from Pollinations.ai. Races turbo + flux
    in parallel and returns whichever responds first with valid bytes.

    Pollinations is the free fallback used when fal.ai is locked / no key,
    and used by every free-tier export. Tail latency is bad — individual
    flux requests can hang 60-90 sec under load. Sequential retry just
    burns the budget. Instead we fire BOTH a fast 'turbo' request and a
    quality 'flux' request at the same time and take whichever lands
    first. Worst-case wall-clock = max(turbo_p99, flux_p99) ≈ 60s
    instead of (turbo + flux) ≈ 120s. Best case (turbo wins) ≈ 5-10s.

    A 2-scene export that previously took 86s on a slow Pollinations day
    now finishes in 10-15s when turbo arrives first.

    Returns JPEG bytes on success, None on total failure (caller falls
    back to a black placeholder so the export still ships)."""
    encoded = quote(prompt[:2000])
    base = f"https://image.pollinations.ai/prompt/{encoded}?width={w}&height={h}&seed={seed}&nologo=true&enhance=true"
    candidates = [
        f"{base}&model=turbo",  # fast, usually 5-10 sec
        f"{base}&model=flux",   # quality, slower but better detail
    ]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _fetch(url: str) -> bytes | None:
        try:
            r = http.get(url, timeout=60)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=len(candidates)) as ex:
        futures = [ex.submit(_fetch, u) for u in candidates]
        try:
            for fut in as_completed(futures, timeout=65):
                result = fut.result()
                if result:
                    # First good response wins; cancel the others to save bandwidth
                    for other in futures:
                        if other is not fut:
                            other.cancel()
                    return result
        except Exception:
            pass
    return None


# Bounded in-memory cache for /scene-art so the browser hitting the same prompt+seed
# twice (back-button, re-watch, autoplay loop) doesn't re-spend $0.04 each time.
# Keyed on (prompt, seed, w, h). Cleared whole when it crosses ~200 entries.
_SCENE_ART_CACHE: dict = {}
_SCENE_ART_CACHE_MAX = 200

# Fal.ai outage circuit-breaker. When we see "User is locked" (exhausted balance)
# or repeated network errors, mark fal.ai unavailable for 10 minutes so subsequent
# calls skip the failing path immediately rather than each one paying ~1 sec of
# failed API latency. Resets automatically after the cooldown.
_FAL_LOCKED_UNTIL: float = 0.0

def _fal_locked() -> bool:
    return time.time() < _FAL_LOCKED_UNTIL

def _mark_fal_locked(reason: str = "") -> None:
    global _FAL_LOCKED_UNTIL
    _FAL_LOCKED_UNTIL = time.time() + 600
    print(f"[fal] circuit open for 10 min — {reason}", flush=True)


# Same pattern for the local ComfyUI tunnel. Justen's PC may be asleep, the
# tunnel may have dropped, or the GPU may be busy with another job. When that
# happens we don't want every export to wait the full HTTP timeout — we open
# the circuit for 5 minutes (shorter than fal's 10 because home PCs come back
# online faster than account balance) and fall through to the fal.ai path.
_COMFY_LOCKED_UNTIL: float = 0.0

def _comfy_locked() -> bool:
    return time.time() < _COMFY_LOCKED_UNTIL

def _mark_comfy_locked(reason: str = "") -> None:
    global _COMFY_LOCKED_UNTIL
    _COMFY_LOCKED_UNTIL = time.time() + 300
    print(f"[comfy-local] circuit open for 5 min — {reason}", flush=True)


# ── Per-scene live video (admin tier) ─────────────────────────────────────────
# State for the in-browser scene viewer playing real Wan 2.2 motion clips
# instead of static images. Videos are generated on Justen's home GPU via the
# same comfy_local path the exporter uses, but cached on disk so navigating
# back to a scene replays instantly without re-spending generation time.
#
# Files land in SCENE_VIDEO_CACHE_DIR keyed by sha256(prompt + seed). The
# directory is shared across requests for the lifetime of the worker — on
# Render free tier that's wiped on every redeploy (no persistent disk), which
# is fine: regen on first view is the existing cost model.
import hashlib as _hashlib
SCENE_VIDEO_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scene_video_cache")
try:
    os.makedirs(SCENE_VIDEO_CACHE_DIR, exist_ok=True)
except OSError as _exc:
    print(f"[scene-video] WARN: could not create cache dir {SCENE_VIDEO_CACHE_DIR}: {_exc}", flush=True)

scene_video_jobs: dict = {}
_SCENE_VIDEO_JOBS_LOCK = threading.Lock()
_SCENE_VIDEO_JOBS_MAX = 100  # bounded so a long uptime can't OOM the dict

def _scene_video_key(prompt: str, seed: int) -> str:
    h = _hashlib.sha256()
    h.update(prompt.encode("utf-8", errors="ignore"))
    h.update(f"|{int(seed)}".encode("ascii"))
    return h.hexdigest()[:32]

def _scene_video_cached_path(key: str) -> str | None:
    p = os.path.join(SCENE_VIDEO_CACHE_DIR, f"{key}.mp4")
    return p if os.path.exists(p) and os.path.getsize(p) > 1024 else None


def paypal_base():
    return "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"

def paypal_token():
    r = http.post(f"{paypal_base()}/v1/oauth2/token",
                  auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
                  data={"grant_type": "client_credentials"}, timeout=15)
    return r.json().get("access_token", "")


# ── Database ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # SQLite ignores `FOREIGN KEY ... ON DELETE CASCADE` unless this pragma is set
    # per-connection. Without it, deleting a season leaves orphaned season_episodes,
    # and deleting a user via admin leaves orphaned episodes for that user's seasons.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                email                 TEXT    UNIQUE NOT NULL,
                password_hash         TEXT    NOT NULL,
                tier                  TEXT    NOT NULL DEFAULT 'free',
                episodes_used         INTEGER NOT NULL DEFAULT 0,
                period_month          TEXT    DEFAULT (strftime('%Y-%m','now')),
                paypal_subscription_id TEXT,
                credits               INTEGER NOT NULL DEFAULT 0,
                created_at            TEXT    DEFAULT (datetime('now'))
            )""")

        # Saved projects (episodes, short films, movies)
        db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                type        TEXT    NOT NULL DEFAULT 'episode',
                title       TEXT,
                genre       TEXT,
                data        TEXT,    -- JSON story blob
                created_at  TEXT    DEFAULT (datetime('now')),
                updated_at  TEXT    DEFAULT (datetime('now'))
            )""")

        # Season series bible + per-episode data
        db.execute("""
            CREATE TABLE IF NOT EXISTS seasons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                title       TEXT,
                genre       TEXT,
                bible       TEXT,    -- JSON series bible
                created_at  TEXT    DEFAULT (datetime('now')),
                updated_at  TEXT    DEFAULT (datetime('now'))
            )""")

        db.execute("""
            CREATE TABLE IF NOT EXISTS season_episodes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id       INTEGER NOT NULL,
                episode_number  INTEGER NOT NULL,
                title           TEXT,
                data            TEXT,    -- JSON episode story (scenes, dialogue, etc.)
                created_at      TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (season_id) REFERENCES seasons(id) ON DELETE CASCADE
            )""")

        # Migration: add credits column for existing deployments
        try:
            db.execute("ALTER TABLE users ADD COLUMN credits INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
        db.commit()


def get_user(uid):
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def quota_check(user):
    tier    = user["tier"] or "free"
    credits = user["credits"] if "credits" in user.keys() else 0

    # Paid users: credits are the currency. No monthly reset.
    if tier in ("hunter", "monarch"):
        return credits, credits, credits > 0  # used=credits_left, limit=same, allowed=has_any

    # Free users: 2/month rolling window
    current = datetime.date.today().strftime("%Y-%m")
    if user["period_month"] != current:
        with get_db() as db:
            db.execute("UPDATE users SET episodes_used=0, period_month=? WHERE id=?",
                       (current, user["id"]))
            db.commit()
        used = 0
    else:
        used = user["episodes_used"]
    limit = TIER_LIMITS.get(tier, 2)
    return used, limit, used < limit


# ── Auth ───────────────────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def w(*a, **kw):
        if not session.get("user_id") and not session.get("is_admin"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return w


@app.route("/health")
def health():
    """Ultra-light health endpoint for Render's probe.
    Returns immediately without touching the DB, Jinja, sessions, or any
    state — even under heavy CPU pressure from a concurrent ffmpeg encode
    the response should land in <10ms, well under Render's 30s probe
    timeout. Previously the probe hit /login which rendered the full
    Jinja template; under CPU starvation that response went over the
    probe timeout and Render flagged the server as failed, killing the
    container mid-export."""
    return "ok", 200, {"Content-Type": "text/plain"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        pw    = request.form.get("password","")
        if email == ADMIN_USER.lower() and secrets.compare_digest(pw, ADMIN_PASS):
            session.clear(); session["is_admin"]=True; session["email"]=ADMIN_USER
            return redirect(url_for("index"))
        with get_db() as db:
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], pw):
            session.clear()
            session["user_id"]=user["id"]; session["email"]=user["email"]; session["tier"]=user["tier"]
            return redirect(url_for("index"))
        flash("Invalid email or password.")
    return render_template("login.html")


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        pw    = request.form.get("password","")
        if not email or not _EMAIL_RE.match(email) or len(email) > 254:
            flash("Enter a valid email address.")
            return render_template("register.html")
        if len(pw) < 6:
            flash("Password must be at least 6 characters.")
            return render_template("register.html")
        try:
            with get_db() as db:
                db.execute("INSERT INTO users (email, password_hash) VALUES (?,?)",
                           (email, generate_password_hash(pw)))
                db.commit()
                user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            session.clear()
            session["user_id"]=user["id"]; session["email"]=email; session["tier"]="free"
            return redirect(url_for("index"))
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.")
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── PayPal one-time credit packs ───────────────────────────────────────────────
# Personal PayPal accounts can't create Subscription Plans — only Orders.
# Instead of monthly subscriptions, users buy generation credit packs:
#   Hunter Pack  — 20 generations / $9.99
#   Monarch Pack — 60 generations / $19.99
# Credits never expire. When they run out the user buys another pack.

@app.route("/create-paypal-order", methods=["POST"])
@login_required
def create_paypal_order():
    pack = (request.json or {}).get("pack", "")
    if pack not in PACKS:
        return jsonify({"error": "Invalid pack"}), 400
    if not PAYPAL_CLIENT_ID:
        return jsonify({"error": "PayPal not configured"}), 500
    p = PACKS[pack]
    try:
        token = paypal_token()
        r = http.post(
            f"{paypal_base()}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "amount": {"currency_code": "USD", "value": p["price"]},
                    "description": f"AnimeWonder {pack.title()} Pack — {p['credits']} story generations",
                }],
            },
            timeout=15,
        )
        r.raise_for_status()
        return jsonify({"order_id": r.json().get("id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/capture-paypal-order", methods=["POST"])
@login_required
def capture_paypal_order():
    data     = request.json or {}
    order_id = data.get("order_id", "")
    pack     = data.get("pack", "")
    if pack not in PACKS or not order_id:
        return jsonify({"error": "Invalid request"}), 400
    if not PAYPAL_CLIENT_ID:
        return jsonify({"error": "PayPal not configured"}), 500
    try:
        token = paypal_token()
        r = http.post(
            f"{paypal_base()}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={},
            timeout=15,
        )
        r.raise_for_status()
        result = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if result.get("status") == "COMPLETED":
        p   = PACKS[pack]
        uid = session.get("user_id")
        with get_db() as db:
            db.execute(
                "UPDATE users SET tier=?, credits=credits+? WHERE id=?",
                (p["tier"], p["credits"], uid),
            )
            db.commit()
            user = db.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()
        session["tier"] = p["tier"]
        new_credits = user["credits"] if user else p["credits"]
        return jsonify({"success": True, "tier": p["tier"],
                        "credits_added": p["credits"], "credits_total": new_credits})
    return jsonify({"error": f"Payment status: {result.get('status')}"}), 400


# ── Upgrade page ────────────────────────────────────────────────────────────────

@app.route("/upgrade")
@login_required
def upgrade():
    user  = get_user(session["user_id"]) if session.get("user_id") else None
    tier  = "admin" if session.get("is_admin") else (user["tier"] if user else "free")
    credits = (user["credits"] if user and "credits" in user.keys() else 0) if user else 0
    used, limit, _ = quota_check(user) if user else (0, 2, True)
    return render_template("upgrade.html", tier=tier, used=used, limit=limit,
                           credits=credits,
                           email=session.get("email",""),
                           paypal_client_id=PAYPAL_CLIENT_ID,
                           pack_hunter_credits=PACK_HUNTER_CREDITS,
                           pack_hunter_price=PACK_HUNTER_PRICE,
                           pack_monarch_credits=PACK_MONARCH_CREDITS,
                           pack_monarch_price=PACK_MONARCH_PRICE)


# ── Project persistence ────────────────────────────────────────────────────────

@app.route("/projects")
@login_required
def list_projects():
    uid = session.get("user_id")
    with get_db() as db:
        if session.get("is_admin"):
            projects = db.execute(
                "SELECT id,type,title,genre,created_at,updated_at FROM projects ORDER BY updated_at DESC LIMIT 50"
            ).fetchall()
        else:
            projects = db.execute(
                "SELECT id,type,title,genre,created_at,updated_at FROM projects WHERE user_id=? ORDER BY updated_at DESC",
                (uid,)
            ).fetchall()
    return jsonify({"projects": [dict(p) for p in projects]})


@app.route("/save-project", methods=["POST"])
@login_required
def save_project():
    uid  = session.get("user_id") or 0
    data = request.json or {}
    story = data.get("story") or {}
    ptype = data.get("type","episode")
    pid   = data.get("project_id")  # if updating existing

    if not story:
        return jsonify({"error":"No story data"}), 400

    now = datetime.datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        if pid:
            if session.get("is_admin"):
                db.execute("UPDATE projects SET data=?, title=?, genre=?, updated_at=? WHERE id=?",
                           (json.dumps(story), story.get("title",""), story.get("genre",""), now, pid))
            else:
                db.execute("UPDATE projects SET data=?, title=?, genre=?, updated_at=? WHERE id=? AND user_id=?",
                           (json.dumps(story), story.get("title",""), story.get("genre",""), now, pid, uid))
        else:
            cur = db.execute(
                "INSERT INTO projects (user_id, type, title, genre, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (uid, ptype, story.get("title","Untitled"), story.get("genre",""),
                 json.dumps(story), now, now)
            )
            pid = cur.lastrowid
        db.commit()
    return jsonify({"success":True, "project_id":pid})


@app.route("/load-project/<int:pid>")
@login_required
def load_project(pid):
    uid = session.get("user_id") or 0
    with get_db() as db:
        if session.get("is_admin"):
            row = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        else:
            row = db.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, uid)).fetchone()
    if not row:
        return jsonify({"error":"Not found"}), 404
    return jsonify({"story": json.loads(row["data"]), "type": row["type"], "project_id": pid})


@app.route("/delete-project/<int:pid>", methods=["DELETE"])
@login_required
def delete_project(pid):
    uid = session.get("user_id") or 0
    with get_db() as db:
        if session.get("is_admin"):
            db.execute("DELETE FROM projects WHERE id=?", (pid,))
        else:
            db.execute("DELETE FROM projects WHERE id=? AND user_id=?", (pid, uid))
        db.commit()
    return jsonify({"success":True})


# ── Season routes ──────────────────────────────────────────────────────────────

@app.route("/generate-season-bible", methods=["POST"])
@login_required
def generate_season_bible():
    tier = "admin" if session.get("is_admin") else session.get("tier","free")
    if tier not in SEASON_TIERS:
        return jsonify({"error":"Season mode requires Shadow Monarch (S-Rank) or Admin account."}), 403

    api_key = ANTHROPIC_API_KEY  # validated at module load

    data    = request.json or {}
    concept = (data.get("concept") or "").strip()
    if not concept:
        return jsonify({"error":"No concept provided"}), 400

    client = anthropic.Anthropic(api_key=api_key)

    # Streaming required by Anthropic SDK at this max_tokens / opus combination
    stream_kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        extra_headers={"anthropic-beta": "output-128k-2025-02-19"},
        system="You are a master anime showrunner. Always respond with valid JSON only — no markdown, no code blocks.",
        messages=[{"role":"user","content":f"""Create a 12-episode anime season plan for: "{concept}"

Return ONLY this JSON (be concise in each field):
{{
  "title": "Series title",
  "subtitle": "Japanese subtitle",
  "genre": "Genre tags",
  "synopsis": "1-2 sentence series overview",
  "world": "Setting description (1 sentence)",
  "main_conflict": "Central conflict (1 sentence)",
  "characters": [
    {{"name":"Name","role":"Main/Supporting/Antagonist","arc":"One sentence arc","description":"Brief description"}}
  ],
  "episodes": [
    {{
      "episode_number": 1,
      "title": "Episode Title",
      "synopsis": "1-2 sentence summary",
      "key_events": ["Event 1","Event 2"],
      "character_focus": "Character name",
      "tone": "tone/mood",
      "cliffhanger": "One sentence cliffhanger",
      "image_prompt": "anime key visual, [scene], Solo Leveling aesthetic, cinematic, 4K"
    }}
  ],
  "season_arc": {{
    "act1": "Episodes 1-4 summary",
    "act2": "Episodes 5-8 summary",
    "act3": "Episodes 9-12 summary"
  }}
}}

Include all 12 episodes. Keep every field short — full detail comes when each episode is generated."""}]
    )

    try:
        with client.messages.stream(**stream_kwargs) as stream:
            response = stream.get_final_message()
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude API error: {str(e)[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error calling Claude: {str(e)[:200]}"}), 500

    try:
        bible = json.loads(extract_json(response.content[0].text))
    except json.JSONDecodeError as e:
        return jsonify({"error":f"Parse failed: {e}"}), 500

    # Save as season
    uid = session.get("user_id") or 0
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO seasons (user_id, title, genre, bible, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (uid, bible.get("title","Untitled"), bible.get("genre",""), json.dumps(bible), now, now)
        )
        season_id = cur.lastrowid
        db.commit()

    bible["season_id"] = season_id
    return jsonify(bible)


@app.route("/generate-season-episode", methods=["POST"])
@login_required
def generate_season_episode():
    tier = "admin" if session.get("is_admin") else session.get("tier","free")
    if tier not in SEASON_TIERS:
        return jsonify({"error":"Season mode requires Shadow Monarch or Admin."}), 403

    # Quota check
    if not session.get("is_admin"):
        uid  = session.get("user_id")
        user = get_user(uid)
        if user:
            used, limit, has_quota = quota_check(user)
            if not has_quota:
                return jsonify({"error":f"Quota reached ({used}/{limit}). Upgrade to generate more.","quota_exceeded":True}), 403

    api_key = ANTHROPIC_API_KEY  # validated at module load

    data      = request.json or {}
    bible     = data.get("bible") or {}
    ep_outline= data.get("episode_outline") or {}
    ep_num    = ep_outline.get("episode_number", 1)
    season_id = data.get("season_id")

    client = anthropic.Anthropic(api_key=api_key)

    series_context = (
        f"Series: {bible.get('title','')} | Genre: {bible.get('genre','')} | "
        f"World: {bible.get('world','')} | Main Conflict: {bible.get('main_conflict','')} | "
        f"Characters: {', '.join(c.get('name','?') for c in bible.get('characters',[]))}"
    )

    stream_kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=12000,
        extra_headers={"anthropic-beta": "output-128k-2025-02-19"},
        system="You are a master anime writer. Always respond with valid JSON only — no markdown, no code blocks.",
        messages=[{"role":"user","content":f"""Write Episode {ep_num} of this anime series.

SERIES CONTEXT: {series_context}

THIS EPISODE:
Title: {ep_outline.get('title','')}
Synopsis: {ep_outline.get('synopsis','')}
Key Events: {', '.join(ep_outline.get('key_events',[]))}
Character Focus: {ep_outline.get('character_focus','')}
Tone: {ep_outline.get('tone','')}
Cliffhanger: {ep_outline.get('cliffhanger','')}

Return ONLY a JSON object:
{{
  "title": "Episode {ep_num}: {ep_outline.get('title','')}",
  "episode_number": {ep_num},
  "subtitle": "Japanese subtitle",
  "genre": "{bible.get('genre','').replace('"', '')}",
  "synopsis": "Episode synopsis",
  "characters": {json.dumps(bible.get('characters',[]))},
  "scenes": [
    {{
      "number": 1,
      "title": "Scene Title",
      "setting": "Where and when",
      "mood": "tense/dramatic/epic/mysterious/peaceful",
      "action": "3-4 vivid sentences describing what happens, respecting the series continuity",
      "dialogue": [
        {{"speaker":"Name","line":"Dialogue","emotion":"emotion"}}
      ],
      "image_prompt": "anime art, [detailed scene], Solo Leveling aesthetic, dark fantasy, cinematic, 4K"
    }}
  ]
}}

Create exactly 6 scenes. Keep it consistent with the series characters and world. End on the specified cliffhanger."""}]
    )

    try:
        with client.messages.stream(**stream_kwargs) as stream:
            response = stream.get_final_message()
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude API error: {str(e)[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error calling Claude: {str(e)[:200]}"}), 500

    try:
        episode = json.loads(extract_json(response.content[0].text))
    except json.JSONDecodeError as e:
        return jsonify({"error":f"Parse failed: {e}"}), 500

    # Save episode to DB — but only if the caller actually owns the season.
    # Without this check, any logged-in user could pass another user's season_id and
    # write into that user's season_episodes table (IDOR).
    if season_id:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        uid_save = session.get("user_id") or 0
        with get_db() as db:
            if session.get("is_admin"):
                owned = db.execute("SELECT id FROM seasons WHERE id=?", (season_id,)).fetchone()
            else:
                owned = db.execute("SELECT id FROM seasons WHERE id=? AND user_id=?",
                                   (season_id, uid_save)).fetchone()
            if owned:
                existing = db.execute(
                    "SELECT id FROM season_episodes WHERE season_id=? AND episode_number=?",
                    (season_id, ep_num)
                ).fetchone()
                if existing:
                    db.execute("UPDATE season_episodes SET data=?, title=? WHERE id=?",
                               (json.dumps(episode), episode.get("title",""), existing["id"]))
                else:
                    db.execute(
                        "INSERT INTO season_episodes (season_id, episode_number, title, data, created_at) VALUES (?,?,?,?,?)",
                        (season_id, ep_num, episode.get("title",""), json.dumps(episode), now)
                    )
                db.execute("UPDATE seasons SET updated_at=? WHERE id=?", (now, season_id))
                db.commit()

    # Count against quota / deduct credits
    if not session.get("is_admin") and session.get("user_id"):
        uid  = session["user_id"]
        u2   = get_user(uid)
        tier2 = (u2["tier"] or "free") if u2 else "free"
        with get_db() as db:
            if tier2 in ("hunter", "monarch"):
                db.execute("UPDATE users SET credits=MAX(0,credits-1) WHERE id=?", (uid,))
            else:
                db.execute("UPDATE users SET episodes_used=episodes_used+1 WHERE id=?", (uid,))
            db.commit()

    return jsonify(episode)


@app.route("/load-season/<int:sid>")
@login_required
def load_season(sid):
    uid = session.get("user_id") or 0
    with get_db() as db:
        if session.get("is_admin"):
            season = db.execute("SELECT * FROM seasons WHERE id=?", (sid,)).fetchone()
        else:
            season = db.execute("SELECT * FROM seasons WHERE id=? AND user_id=?", (sid,uid)).fetchone()
        if not season:
            return jsonify({"error":"Not found"}), 404
        episodes = db.execute(
            "SELECT episode_number, title, data FROM season_episodes WHERE season_id=? ORDER BY episode_number",
            (sid,)
        ).fetchall()

    bible = json.loads(season["bible"])
    bible["season_id"] = sid
    return jsonify({
        "bible": bible,
        "generated_episodes": {
            str(e["episode_number"]): {"title":e["title"], "data":json.loads(e["data"])}
            for e in episodes
        }
    })


@app.route("/seasons")
@login_required
def list_seasons():
    uid = session.get("user_id") or 0
    with get_db() as db:
        if session.get("is_admin"):
            rows = db.execute("SELECT id,title,genre,created_at,updated_at FROM seasons ORDER BY updated_at DESC LIMIT 30").fetchall()
        else:
            rows = db.execute("SELECT id,title,genre,created_at,updated_at FROM seasons WHERE user_id=? ORDER BY updated_at DESC",(uid,)).fetchall()
        # Count episodes per season
        result = []
        for r in rows:
            count = db.execute("SELECT COUNT(*) as c FROM season_episodes WHERE season_id=?",(r["id"],)).fetchone()["c"]
            d = dict(r); d["episodes_generated"] = count; result.append(d)
    return jsonify(result)


@app.route("/delete-season/<int:sid>", methods=["DELETE"])
@login_required
def delete_season(sid):
    uid = session.get("user_id") or 0
    with get_db() as db:
        if session.get("is_admin"):
            db.execute("DELETE FROM season_episodes WHERE season_id=?", (sid,))
            db.execute("DELETE FROM seasons WHERE id=?", (sid,))
        else:
            owned = db.execute("SELECT id FROM seasons WHERE id=? AND user_id=?", (sid, uid)).fetchone()
            if owned:
                db.execute("DELETE FROM season_episodes WHERE season_id=?", (sid,))
                db.execute("DELETE FROM seasons WHERE id=?", (sid,))
        db.commit()
    return jsonify({"success":True})


# ── Neural TTS ────────────────────────────────────────────────────────────────

VOICE_MAP = {
    "narrator":    "en-US-GuyNeural",
    "protagonist": "en-US-ChristopherNeural",
    "antagonist":  "en-US-DavisNeural",
    "female":      "en-US-JennyNeural",
    "female_ant":  "en-US-NancyNeural",
    "support_m1":  "en-US-EricNeural",
    "support_m2":  "en-US-RogerNeural",
    "support_f1":  "en-US-AriaNeural",
    "support_f2":  "en-US-SaraNeural",
}

@app.route("/speak", methods=["POST"])
@login_required
def speak():
    data  = request.json or {}
    text  = (data.get("text") or "").strip()[:1000]
    voice = (data.get("voice") or "en-US-GuyNeural").strip()[:80]
    rate  = (data.get("rate") or "+0%").strip()[:8]
    if not text:
        return "", 400
    # edge-tts voice ids are like "en-US-GuyNeural" — letters, digits, hyphens only.
    # Reject anything else so we never proxy weird input to the upstream service.
    if not re.fullmatch(r"[A-Za-z0-9\-]+", voice):
        return jsonify({"error": "Invalid voice id."}), 400
    # rate format: "+N%" or "-N%" where N is 0-200.
    if not re.fullmatch(r"[+\-]\d{1,3}%", rate):
        return jsonify({"error": "Invalid rate format. Use '+N%' or '-N%'."}), 400
    try:
        async def gen():
            comm = edge_tts.Communicate(text, voice, rate=rate)
            chunks = []
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)
        # Use a fresh event loop per thread to avoid conflicts in Flask threaded mode
        loop = asyncio.new_event_loop()
        try:
            audio = loop.run_until_complete(gen())
        finally:
            loop.close()
        return Response(audio, mimetype="audio/mpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Story generation ───────────────────────────────────────────────────────────

def build_prompt(concept, mode, style_key=DEFAULT_STYLE):
    """
    Builds the Claude system prompt for story generation.

    - Anchors the structure on classic anime story shapes (kishōtenketsu for
      episode, 3-act for short, hero's-journey for movie). The model needs an
      explicit shape, otherwise it defaults to generic Western 3-act all the time.
    - Threads the chosen art style's vibe into Claude's worldbuilding so the
      dialogue and pacing actually match the visual look (Ghibli SHOULDN'T sound
      like Berserk just because the artist picked Ghibli).
    - Tells Claude to write a `character_anchor` for every character — a fixed
      visual descriptor like "spiky white hair, scar on right cheek, black trench
      coat with silver buckles". We re-inject this into every scene's image
      prompt during export so the protagonist actually LOOKS like the same person
      across all scenes. Without this, Pollinations/Seedream roll a new face every
      scene and the result feels like a slideshow of strangers.
    """
    style = get_style(style_key)
    style_name = style["name"]
    style_vibe = style["vibe_hint"]

    configs = {
        "episode": (
            5,
            "single anime episode with a complete arc",
            (
                "5 scenes following kishōtenketsu: "
                "Scene 1 KI (introduce world+protagonist), "
                "Scene 2 SHŌ (escalate stakes), "
                "Scene 3 TEN (a twist that reframes everything), "
                "Scene 4 KETSU-A (confrontation), "
                "Scene 5 KETSU-B (resolution + hook for next episode). "
                "Each scene: 2-3 vivid sentences of action, 3-5 dialogue lines."
            ),
        ),
        "short": (
            15,
            "anime short film (4 setup · 7 confrontation · 4 resolution)",
            (
                "15 scenes. Open with a cold-open hook (scene 1) before introducing the protagonist. "
                "Each scene: 2 sentence action max, 2-3 dialogue lines max. "
                "Scene 12 must be the lowest moment for the protagonist."
            ),
        ),
        "movie": (
            24,
            "full anime feature film",
            (
                "24 scenes structured as: Act 1 (scenes 1-6: ordinary world, inciting incident, refusal, call accepted), "
                "Act 2A (scenes 7-12: trials, allies, false victory), "
                "Act 2B (scenes 13-18: the ordeal, dark night of the soul, revelation), "
                "Act 3 (scenes 19-24: climax, sacrifice, transformation, return). "
                "Scene 1 is a cold-open. Scene 12 is a false victory. Scene 18 is the lowest point. "
                "Per scene: 2 sentence action, 2-3 dialogue lines."
            ),
        ),
    }
    count, structure, hint = configs.get(mode, configs["episode"])

    return f"""Write an anime {structure} based on: "{concept}"

ART STYLE: {style_name}
TONE TO MATCH THE STYLE: {style_vibe}

STRUCTURE: {hint}

CHARACTER ANCHORS — CRITICAL:
For each character, write a `character_anchor` field: a short, fixed visual descriptor
(hair color/style, eye color, defining feature, signature outfit, age). Examples:
  - "spiky white hair, golden eyes, scar across left cheek, black hooded trench coat, late teens"
  - "long crimson braid, jade eyes, kimono with crane pattern, mid-20s"
This anchor MUST be re-stated in every scene's `image_prompt` whenever that character appears,
so the artwork shows the same face across scenes.

Return ONLY valid JSON — no markdown, no code fences:
{{
  "title": "",
  "subtitle": "Japanese subtitle in romaji",
  "genre": "",
  "synopsis": "2 sentences with a hook",
  "characters": [
    {{
      "name": "",
      "role": "Protagonist/Antagonist/Supporting",
      "description": "personality + arc in 1-2 sentences",
      "character_anchor": "fixed visual descriptor (see above)"
    }}
  ],
  "scenes": [
    {{
      "number": 1,
      "title": "",
      "setting": "where and when (1 line)",
      "mood": "one word: dramatic/tense/violent/epic/triumphant/sorrowful/melancholic/peaceful/mysterious/unsettling",
      "action": "2-3 vivid sentences. Dark/mature themes welcome. Show, don't tell.",
      "dialogue": [
        {{"speaker": "Character Name", "line": "What they say", "emotion": "neutral/angry/grieving/excited/whispered/shouting/cold/desperate"}}
      ],
      "image_prompt": "anime art, [scene composition including any present character's anchor descriptors verbatim], [setting], [mood lighting]"
    }}
  ]
}}

Produce exactly {count} scenes. Maximise tension, moral complexity, and emotional depth.
Every scene's image_prompt MUST include the character_anchor for any character that appears in it."""


@app.route("/")
@login_required
def index():
    user  = get_user(session["user_id"]) if session.get("user_id") else None
    tier  = "admin" if session.get("is_admin") else (user["tier"] if user else "free")
    if session.get("is_admin"):
        used, limit = 0, 999
    else:
        used, limit, _ = quota_check(user) if user else (0, 2, True)
    return render_template("index.html",
                           is_admin=session.get("is_admin",False),
                           email=session.get("email",""),
                           tier=tier, used=used, limit=limit,
                           paypal_client_id=PAYPAL_CLIENT_ID,
                           fal_ok=bool(FAL_KEY),
                           comfy_local_ok=bool(COMFY_LOCAL_URL))


@app.route("/generate", methods=["POST"])
@login_required
def generate():
    if not session.get("is_admin"):
        uid  = session.get("user_id")
        user = get_user(uid) if uid else None
        if not user:
            return jsonify({"error":"Not logged in"}), 401
        used, limit, has_quota = quota_check(user)
        if not has_quota:
            return jsonify({"error":f"Quota reached ({used}/{limit}). Upgrade for more.","quota_exceeded":True}), 403

    api_key = ANTHROPIC_API_KEY  # validated at module load

    data    = request.json or {}
    concept = (data.get("concept") or "").strip()
    mode    = data.get("mode","episode")
    style_key = (data.get("style") or DEFAULT_STYLE).strip()
    if style_key not in STYLES:
        style_key = DEFAULT_STYLE

    if mode not in ("episode","short","movie"):
        return jsonify({"error":"Invalid mode. Use episode, short, or movie."}), 400
    if not concept:
        return jsonify({"error":"No concept provided"}), 400

    # Server-side tier enforcement for mode
    if not session.get("is_admin"):
        uid2  = session.get("user_id")
        user2 = get_user(uid2) if uid2 else None
        tier2 = user2["tier"] if user2 else "free"
        if mode not in TIER_MODES.get(tier2, {"episode"}):
            return jsonify({"error":f"Your plan does not include {mode} mode. Upgrade to unlock.","quota_exceeded":True}), 403

    client   = anthropic.Anthropic(api_key=api_key)
    # Why these numbers: the new kishōtenketsu-aware prompt with character_anchor
    # fields per character is verbose. Episode at 4k tokens was getting truncated
    # mid-string in late scenes (Justen hit `Unterminated string ... char 14667`
    # in production). Bump everything; the 128k beta lifts the ceiling so we have
    # room without paying for what we don't use.
    max_toks = {"episode": 8000, "short": 24000, "movie": 48000}.get(mode, 8000)

    # Monarch and Admin get the stronger reasoning model — opus produces noticeably
    # better dramatic structure and dialogue subtext on longer pieces. Sonnet stays
    # default for Free/Hunter (faster + cheaper, fine for episode mode).
    tier_now = "admin" if session.get("is_admin") else session.get("tier", "free")
    story_model = "claude-opus-4-7" if (tier_now in ("monarch", "admin") and mode in ("short", "movie")) else "claude-sonnet-4-6"

    # Content policy: admins (and Monarch tier) can push dark/violent/mature
    # themes — that's the whole point of Justen's "dark fantasy hunter" tone.
    # Explicit sexual content stays off across all tiers (Anthropic's safety
    # filter would block it anyway; making it explicit avoids hard-to-debug
    # rejections mid-generation).
    is_mature = tier_now in ("monarch", "admin")
    mature_policy = (
        "\nCONTENT POLICY: graphic violence, dark themes, moral ambiguity, blood, "
        "body horror, and intense psychological drama are all welcome — match the "
        "tone of a TV-MA anime (Berserk, Tokyo Ghoul, Attack on Titan). "
        "AVOID: explicit sexual content, nudity, or sexualized minors. "
        "Suggestive tension is fine; explicit acts are not."
        if is_mature else
        "\nKeep the tone PG-13: action and tension are fine, but no explicit gore "
        "or sexual content."
    )

    system_prompt = (
        "You are a master anime writer and director. "
        "Always respond with valid JSON only — no markdown, no code blocks. "
        "Keep field values short enough that the full JSON fits inside the "
        "max_tokens budget — truncated responses are worse than slightly shorter ones."
        + mature_policy
    )

    create_kwargs = dict(
        model=story_model,
        max_tokens=max_toks,
        system=system_prompt,
        messages=[{"role": "user", "content": build_prompt(concept, mode, style_key)}],
        # Use extended-output beta on every mode now that episode also exceeds the
        # default 8k Sonnet cap. Harmless if the actual response fits comfortably.
        extra_headers={"anthropic-beta": "output-128k-2025-02-19"},
    )

    # Use streaming — the SDK requires it once max_tokens × model-speed could exceed
    # 10 minutes of wall time. Movie mode at 48k tokens on opus-4-7 hits that limit.
    # Streaming behavior identical to .create() from our perspective: we still want
    # the full message at the end; we just accumulate via the stream context manager.
    try:
        with client.messages.stream(**create_kwargs) as stream:
            response = stream.get_final_message()
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude API error: {str(e)[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error calling Claude: {str(e)[:200]}"}), 500

    raw_text = response.content[0].text if response.content else ""
    try:
        story = json.loads(extract_json(raw_text))
    except json.JSONDecodeError as e:
        # If we ran out of token budget the response is truncated mid-JSON.
        # Surface a clean, actionable error rather than a parser exception.
        stop_reason = getattr(response, "stop_reason", "")
        if stop_reason == "max_tokens":
            return jsonify({
                "error": (f"Generation hit the {max_toks}-token cap before finishing. "
                          f"Try a shorter concept, or pick Episode if you used Movie/Short.")
            }), 500
        return jsonify({"error": f"Story parse failed: {e}"}), 500

    # Stamp the style onto the story so re-opening from the library
    # or exporting later uses the same visual treatment.
    story["style"] = style_key

    if not session.get("is_admin") and session.get("user_id"):
        uid  = session["user_id"]
        user = get_user(uid)
        tier = (user["tier"] or "free") if user else "free"
        with get_db() as db:
            if tier in ("hunter", "monarch"):
                db.execute("UPDATE users SET credits=MAX(0,credits-1) WHERE id=?", (uid,))
            else:
                db.execute("UPDATE users SET episodes_used=episodes_used+1 WHERE id=?", (uid,))
            db.commit()

    return jsonify(story)


@app.route("/animation-preview")
def animation_preview():
    """
    Standalone showcase page that runs Justen's React fighter animation demo.
    Loaded via React + framer-motion + Babel JSX on the CDN so it can live
    inside this Flask template repo without a build step. Public — no auth gate,
    since the goal is to let visitors see the in-motion anime style before
    creating an account.
    """
    return render_template("animation_preview.html")


@app.route("/styles")
def styles_list():
    """Public list for the frontend style picker. No auth required —
    style metadata is not sensitive and the home page reads this."""
    return jsonify({"styles": styles_public(), "default": DEFAULT_STYLE})


def _comfy_workflow_txt2img(prompt: str, neg: str, w: int, h: int, seed: int) -> dict:
    """ComfyUI API-format workflow for Animagine XL text-to-image.

    Nodes: CheckpointLoaderSimple → CLIPTextEncode×2 → EmptyLatentImage
           → KSampler → VAEDecode → SaveImage
    """
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "animagine-xl-3.1.safetensors"}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["1", 1], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["1", 1], "text": neg}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["2", 0],
                         "negative": ["3", 0], "latent_image": ["4", 0],
                         "seed": seed, "steps": 28, "cfg": 7.0,
                         "sampler_name": "dpmpp_2m", "scheduler": "karras",
                         "denoise": 1.0}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0], "filename_prefix": "animeforge_img"}},
    }


def _comfy_image(prompt: str, w: int, h: int, seed: int) -> bytes | None:
    """Generate an image via local ComfyUI (Animagine XL).

    Returns JPEG bytes on success, None on failure (circuit breaker trips).
    Falls back gracefully to Pollinations if the model isn't loaded yet.
    """
    if not COMFY_LOCAL_URL or _comfy_locked():
        return None

    # Quick check: is the model file present? Skip if not yet downloaded.
    model_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)).replace("anime_app", ""),
        "ai", "ComfyUI_windows_portable", "ComfyUI", "models", "checkpoints",
        "animagine-xl-3.1.safetensors",
    )
    # Also try the path relative to the script (production Render won't have this)
    if not os.path.exists(model_path):
        return None

    neg = ("worst quality, low quality, jpeg artifacts, ugly, duplicate, "
           "morbid, mutilated, extra fingers, mutated hands, poorly drawn hands, "
           "poorly drawn face, mutation, blurry, bad anatomy, bad proportions, watermark")
    seed_val = int(seed) % 2147483647
    # Snap to SDXL-friendly multiples of 64
    w64 = max(512, (w // 64) * 64)
    h64 = max(512, (h // 64) * 64)
    base = COMFY_LOCAL_URL
    headers = _comfy_headers()
    try:
        workflow = _comfy_workflow_txt2img(prompt, neg, w64, h64, seed_val)
        sub = http.post(f"{base}/prompt",
                        json={"prompt": workflow, "client_id": "animeforge-img"},
                        headers=headers, timeout=30)
        sub.raise_for_status()
        prompt_id = sub.json().get("prompt_id")
        if not prompt_id:
            _mark_comfy_locked("img: no prompt_id")
            return None

        # Poll history — short deadline so we don't block the request thread
        # while ComfyUI is busy with a Wan video job. Falls through to
        # Pollinations if GPU isn't free within 25 sec.
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(2)
            try:
                h_resp = http.get(f"{base}/history/{prompt_id}", headers=headers, timeout=8)
                hist = h_resp.json().get(prompt_id) or {}
                out = hist.get("outputs") or {}
                imgs = (out.get("7") or {}).get("images") or []
                if imgs:
                    fname = imgs[0].get("filename")
                    subfolder = imgs[0].get("subfolder", "")
                    break
            except Exception:
                continue
        else:
            # GPU busy — don't lock the circuit, just return None so Pollinations kicks in
            return None

        r = http.get(f"{base}/view",
                     params={"filename": fname, "subfolder": subfolder, "type": "output"},
                     headers=headers, timeout=30)
        r.raise_for_status()
        return r.content  # PNG bytes from ComfyUI
    except Exception as exc:
        _mark_comfy_locked(f"img: {str(exc)[:80]}")
        return None


def _comfy_workflow_photoreal(prompt: str, neg: str, w: int, h: int, seed: int) -> dict:
    """ComfyUI workflow for CyberRealistic Pony with Hi-Res Fix + FaceDetailer.

    This is the photoreal pipeline built 2026-05-22, tightened 2026-05-23 after
    Justen said the artwork was "sub par". Used when style_key=="photoreal".
    Four stages:
      1. Base render @ w×h with dpmpp_3m_sde karras, 40 steps, CFG 6.5
      2. Hi-Res Fix: 4x_NMKD-Siax_200k → downscale to 1.5x → 25-step refine @ denoise 0.5
      3. FaceDetailer: YOLOv8m → per-face inpaint @ 640 guide, 28 steps, denoise 0.55
      4. HandDetailer: YOLOv8s → per-hand inpaint @ 384 guide (broken hands kill realism)

    Requires custom_nodes: ComfyUI-Impact-Pack + ComfyUI-Impact-Subpack.
    Requires models:
      - CyberRealisticPony.safetensors
      - 4x_NMKD-Siax_200k.pth (better for skin/realism than 4x-UltraSharp)
      - ultralytics/bbox/face_yolov8m.pt
      - ultralytics/bbox/hand_yolov8s.pt
    Per-image time: ~50 sec on RTX 4070 Ti SUPER (was 30 sec before tightening).
    """
    target_w = int(w * 1.5)
    target_h = int(h * 1.5)
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "CyberRealisticPony.safetensors"}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["1", 1], "text": prompt}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["1", 1], "text": neg}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["2", 0],
                         "negative": ["3", 0], "latent_image": ["4", 0],
                         "seed": int(seed), "steps": 40, "cfg": 6.5,
                         "sampler_name": "dpmpp_3m_sde", "scheduler": "karras",
                         "denoise": 1.0}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        # Hi-Res Fix stage
        "20": {"class_type": "UpscaleModelLoader",
               "inputs": {"model_name": "4x_NMKD-Siax_200k.pth"}},
        "21": {"class_type": "ImageUpscaleWithModel",
               "inputs": {"upscale_model": ["20", 0], "image": ["6", 0]}},
        "22": {"class_type": "ImageScale",
               "inputs": {"image": ["21", 0], "upscale_method": "lanczos",
                          "width": target_w, "height": target_h, "crop": "disabled"}},
        "23": {"class_type": "VAEEncode",
               "inputs": {"pixels": ["22", 0], "vae": ["1", 2]}},
        "24": {"class_type": "KSampler",
               "inputs": {"model": ["1", 0], "positive": ["2", 0],
                          "negative": ["3", 0], "latent_image": ["23", 0],
                          "seed": int(seed) + 7777, "steps": 25, "cfg": 6.5,
                          "sampler_name": "dpmpp_3m_sde", "scheduler": "karras",
                          "denoise": 0.5}},
        "25": {"class_type": "VAEDecode",
               "inputs": {"samples": ["24", 0], "vae": ["1", 2]}},
        # FaceDetailer stage
        "30": {"class_type": "UltralyticsDetectorProvider",
               "inputs": {"model_name": "bbox/face_yolov8m.pt"}},
        "32": {"class_type": "FaceDetailer",
               "inputs": {
                   "image": ["25", 0], "model": ["1", 0], "clip": ["1", 1],
                   "vae": ["1", 2], "positive": ["2", 0], "negative": ["3", 0],
                   "bbox_detector": ["30", 0],
                   "guide_size": 640, "guide_size_for": True, "max_size": 1024,
                   "seed": int(seed) + 11111, "steps": 28, "cfg": 6.5,
                   "sampler_name": "dpmpp_3m_sde", "scheduler": "karras",
                   "denoise": 0.55, "feather": 5, "noise_mask": True,
                   "force_inpaint": True, "bbox_threshold": 0.5,
                   "bbox_dilation": 10, "bbox_crop_factor": 3.0,
                   "sam_detection_hint": "center-1", "sam_dilation": 0,
                   "sam_threshold": 0.93, "sam_bbox_expansion": 0,
                   "sam_mask_hint_threshold": 0.7,
                   "sam_mask_hint_use_negative": "False",
                   "drop_size": 10, "wildcard": "", "cycle": 1,
               }},
        # HandDetailer stage — broken hands are the OTHER big quality killer
        "33": {"class_type": "UltralyticsDetectorProvider",
               "inputs": {"model_name": "bbox/hand_yolov8s.pt"}},
        "34": {"class_type": "FaceDetailer",  # node reused for hand bbox inpaint
               "inputs": {
                   "image": ["32", 0], "model": ["1", 0], "clip": ["1", 1],
                   "vae": ["1", 2], "positive": ["2", 0], "negative": ["3", 0],
                   "bbox_detector": ["33", 0],
                   "guide_size": 384, "guide_size_for": True, "max_size": 768,
                   "seed": int(seed) + 22222, "steps": 24, "cfg": 6.5,
                   "sampler_name": "dpmpp_3m_sde", "scheduler": "karras",
                   "denoise": 0.5, "feather": 5, "noise_mask": True,
                   "force_inpaint": True, "bbox_threshold": 0.5,
                   "bbox_dilation": 10, "bbox_crop_factor": 3.0,
                   "sam_detection_hint": "center-1", "sam_dilation": 0,
                   "sam_threshold": 0.93, "sam_bbox_expansion": 0,
                   "sam_mask_hint_threshold": 0.7,
                   "sam_mask_hint_use_negative": "False",
                   "drop_size": 10, "wildcard": "", "cycle": 1,
               }},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["34", 0],
                         "filename_prefix": "animeforge_photoreal"}},
    }


def _comfy_image_photoreal(prompt: str, w: int, h: int, seed: int) -> bytes | None:
    """Generate a PHOTOREAL scene via local ComfyUI (CyberRealistic Pony).

    Returns PNG bytes on success, None on failure. The 3-stage pipeline takes
    ~30 sec on the 4070 Ti SUPER. Caller should provide longer timeout than the
    anime path (which is single-pass ~10 sec).
    """
    if not COMFY_LOCAL_URL or _comfy_locked():
        return None
    # Photoreal-specific negative — Pony score tags + anti-anime
    neg = (
        "score_4, score_3, score_2, score_1, "
        "bad quality, worst quality, low quality, blurry, jpeg artifacts, "
        "watermark, signature, text, logo, ugly face, deformed face, "
        "deformed hands, malformed hands, fused fingers, extra fingers, "
        "extra limbs, multiple heads, mutated, "
        "cartoon, anime, 2d, sketch, painting, illustration, drawing, "
        "doll face, mannequin, plastic skin, oversaturated, washed out, "
        "asymmetric eyes, cross-eyed, child, underage, teen"
    )
    seed_val = int(seed) % 2147483647
    w64 = max(512, (w // 64) * 64)
    h64 = max(512, (h // 64) * 64)
    base = COMFY_LOCAL_URL
    headers = _comfy_headers()
    try:
        workflow = _comfy_workflow_photoreal(prompt, neg, w64, h64, seed_val)
        sub = http.post(f"{base}/prompt",
                        json={"prompt": workflow, "client_id": "animeforge-photoreal"},
                        headers=headers, timeout=30)
        sub.raise_for_status()
        prompt_id = sub.json().get("prompt_id")
        if not prompt_id:
            _mark_comfy_locked("photoreal img: no prompt_id")
            return None
        # Photoreal is ~50 sec end-to-end (4-stage pipeline) — generous deadline
        deadline = time.time() + 150
        fname = subfolder = None
        while time.time() < deadline:
            time.sleep(3)
            try:
                h_resp = http.get(f"{base}/history/{prompt_id}", headers=headers, timeout=8)
                hist = h_resp.json().get(prompt_id) or {}
                out = hist.get("outputs") or {}
                imgs = (out.get("7") or {}).get("images") or []
                if imgs:
                    fname = imgs[0].get("filename")
                    subfolder = imgs[0].get("subfolder", "")
                    break
            except Exception:
                continue
        if not fname:
            return None
        r = http.get(f"{base}/view",
                     params={"filename": fname, "subfolder": subfolder, "type": "output"},
                     headers=headers, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as exc:
        _mark_comfy_locked(f"photoreal img: {str(exc)[:80]}")
        return None


@app.route("/scene-art")
@login_required
def scene_art():
    """
    Image proxy for scene/thumbnail/hero art.

    Priority order (admin tier):
      1. Local ComfyUI Animagine XL (free, GPU quality) — when tunnel is up
      2. fal.ai Hunyuan Image 3.0 (paid, $0.04/img) — when fal has balance
      3. Pollinations.ai (free fallback, flat 2D)

    Non-admin tiers go straight to Pollinations.

    Results are cached in-memory so reloading the same scene is free.
    """
    prompt = (request.args.get("prompt") or "").strip()[:1500]
    seed = request.args.get("seed", type=int)
    w = max(64, min(2048, request.args.get("w", default=512, type=int)))
    h = max(64, min(2048, request.args.get("h", default=288, type=int)))
    if not prompt:
        return "missing prompt", 400

    tier = "admin" if session.get("is_admin") else session.get("tier", "free")
    style_key = (request.args.get("style") or "").strip()
    cache_key = (prompt, seed or 0, w, h, style_key)

    # PHOTOREAL path (admin + local GPU). When the project's style_key is
    # "photoreal", route the scene-art viewer to the CyberRealistic Pony
    # workflow instead of the default anime stack. Takes ~30 sec (vs ~10
    # sec for anime) so we only invoke it when style explicitly opts in.
    if (style_key == "photoreal" and tier == "admin"
            and COMFY_LOCAL_URL and not _comfy_locked()):
        cached = _SCENE_ART_CACHE.get(cache_key)
        if cached:
            return Response(cached, mimetype="image/png",
                            headers={"Cache-Control": "public, max-age=3600",
                                     "X-Image-Source": "comfy-photoreal-cached"})
        img_bytes = _comfy_image_photoreal(prompt, w, h, seed or 0)
        if img_bytes:
            if len(_SCENE_ART_CACHE) >= _SCENE_ART_CACHE_MAX:
                _SCENE_ART_CACHE.clear()
            _SCENE_ART_CACHE[cache_key] = img_bytes
            return Response(img_bytes, mimetype="image/png",
                            headers={"Cache-Control": "public, max-age=3600",
                                     "X-Image-Source": "comfy-photoreal"})
        # photoreal failed → fall through to fal.ai / Pollinations below

    # Scene-art (anime path) skips ComfyUI to keep the viewer fast and non-blocking.
    # Animagine XL is used for export frame generation (do_export) where
    # we can afford to wait. For the viewer, Pollinations is instant and
    # the Wan animation clips overlay it anyway.

    # fal.ai Hunyuan (admin/monarch, paid — only when fal has balance)
    use_fal = tier in ("monarch", "admin") and bool(FAL_KEY)
    if use_fal:
        cached = _SCENE_ART_CACHE.get(cache_key)
        if cached:
            return Response(cached, mimetype="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600",
                                     "X-Image-Source": "fal-hunyuan-cached"})
        img_bytes = _fal_image(prompt, w, h, model="hunyuan", seed=seed)
        if img_bytes:
            if len(_SCENE_ART_CACHE) >= _SCENE_ART_CACHE_MAX:
                _SCENE_ART_CACHE.clear()
            _SCENE_ART_CACHE[cache_key] = img_bytes
            return Response(img_bytes, mimetype="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600",
                                     "X-Image-Source": "fal-hunyuan"})
        # fal failed → fall through to Pollinations

    # 3. Pollinations (free fallback for all tiers)
    pollinations_url = (
        f"https://image.pollinations.ai/prompt/{quote(prompt)}"
        f"?width={w}&height={h}&seed={seed or 0}&nologo=true&enhance=true&model=flux"
    )
    return redirect(pollinations_url, code=302)


# ── Per-scene live video (admin tier) ──────────────────────────────────────────
# Endpoints that drive the in-viewer motion playback. The viewer POSTs /scene-video/start
# when a scene loads; if a cached MP4 exists for that prompt+seed the response is
# instant. Otherwise a background thread fetches the scene image and runs Wan 2.2
# 5B image-to-video on the home GPU via the existing animate_scene_comfy helper.
# Browser then polls /scene-video/status/<key> every ~3s and swaps the <video>
# src to /scene-video/file/<key>.mp4 when ready.

def _do_scene_video(key: str, prompt: str, seed: int, image_url: str,
                     cookie_header: str, host_url: str):
    """Background worker that produces a single scene video and parks it in the
    on-disk cache. Driven by /scene-video/start. Idempotent: if the cache file
    already exists we just mark the job ready without re-running comfy.

    host_url must be captured from request.host_url in the route handler — Flask
    request context does not propagate into background threads.
    """
    job = scene_video_jobs.get(key) or {}
    tmp = tempfile.mkdtemp(prefix="anime_scene_vid_")
    try:
        # Short-circuit if another worker beat us to it.
        cached = _scene_video_cached_path(key)
        if cached:
            job.update({"status": "ready", "url": f"/scene-video/file/{key}.mp4",
                        "progress": 100, "message": "Cached"})
            return

        # Resolve the scene-art URL to absolute form using the host captured at
        # request time (background threads have no request context).
        img_url_full = image_url
        if image_url.startswith("/"):
            img_url_full = host_url.rstrip("/") + image_url if host_url else image_url
        try:
            headers = {"Cookie": cookie_header} if cookie_header else {}
            r = http.get(img_url_full, headers=headers, timeout=120, allow_redirects=True)
            r.raise_for_status()
            img_bytes = r.content
        except Exception as exc:
            job.update({"status": "failed", "message": f"image fetch failed: {str(exc)[:80]}"})
            return

        img_path = os.path.join(tmp, "src.jpg")
        with open(img_path, "wb") as fh:
            fh.write(img_bytes)

        # Fake "job" dict shape both backends expect (they write message /
        # elapsed_seconds into it for the export status feed).
        sub_job = {"start_time": time.time(), "message": "", "elapsed_seconds": 0}

        # Try local GPU first — it's free for admin and Justen's fal.ai balance
        # is the gating issue on this account. The 60fps interpolation pass
        # downstream smooths the 5B output so motion is clean either way.
        # fal.ai stays as a backup when the home GPU is offline.
        vid_path = None
        if COMFY_LOCAL_URL and not _comfy_locked():
            # Scene viewer uses 81 frames (3.4 sec @ 24fps) — longer than the
            # 49-frame export default because it's a single clip, not a multi-scene
            # MoviePy assembly. More frames = more native motion to interpolate from.
            job.update({"status": "generating", "progress": 30,
                        "message": "Generating motion on local GPU…"})
            vid_path = animate_scene_comfy(img_path, prompt, tmp, 0, sub_job, length=81)

        if (not vid_path or not os.path.exists(vid_path)) and FAL_KEY and not _fal_locked():
            job.update({"status": "generating", "progress": 30,
                        "message": "Local GPU offline, falling back to fal.ai Wan 2.5…"})
            vid_path = animate_scene_fal(img_path, prompt, tmp, 0, sub_job, model="wan")

        if not vid_path or not os.path.exists(vid_path):
            job.update({"status": "failed",
                        "message": "Animation unavailable — local GPU offline and fal.ai exhausted"})
            return

        # Post-pass: ffmpeg motion-interpolate native 24fps → TARGET_FPS (60) for
        # truly fluent motion. minterpolate (mci+bidir+aobmc+vsbmc) synthesizes
        # intermediate frames using motion estimation; for anime content with
        # smooth camera+effects motion it produces clean inbetweens. Falls back
        # to the raw clip if anything goes wrong — never block the user's video
        # on a smoothness post-process.
        job.update({"progress": 80, "message": f"Smoothing to {TARGET_FPS}fps…"})
        smooth_path = os.path.join(tmp, f"smooth_{int(time.time())}.mp4")
        if _interpolate_to_target_fps(vid_path, smooth_path):
            vid_path = smooth_path

        # Move into the persistent cache atomically (rename within same filesystem).
        dst = os.path.join(SCENE_VIDEO_CACHE_DIR, f"{key}.mp4")
        try:
            shutil.move(vid_path, dst)
        except Exception:
            shutil.copyfile(vid_path, dst)
        job.update({"status": "ready", "progress": 100,
                    "url": f"/scene-video/file/{key}.mp4",
                    "message": "Ready"})
    except Exception as exc:
        job.update({"status": "failed", "message": f"worker crashed: {str(exc)[:120]}"})
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def _gc_scene_video_jobs():
    """Trim oldest entries when the jobs dict exceeds the bound.
    Must be called while _SCENE_VIDEO_JOBS_LOCK is already held —
    do NOT acquire the lock here (threading.Lock is not reentrant)."""
    if len(scene_video_jobs) <= _SCENE_VIDEO_JOBS_MAX:
        return
    items = sorted(scene_video_jobs.items(), key=lambda kv: kv[1].get("created_at", 0))
    excess = len(scene_video_jobs) - _SCENE_VIDEO_JOBS_MAX
    for k, _ in items[:excess]:
        scene_video_jobs.pop(k, None)


@app.route("/scene-video/start", methods=["POST"])
@login_required
def scene_video_start():
    """Kick off (or look up) a Wan 2.2 motion clip for a single scene.

    Gated to admin tier: it spends Justen's home-GPU electricity and is shaped
    around his single-card serial throughput. Paying users still get static
    images in the viewer; their motion comes from fal.ai during MP4 export.

    Body: {prompt: str, seed: int, image_url: str}
    Response: {key, status, url?, message?, cached: bool}
    """
    tier = "admin" if session.get("is_admin") else session.get("tier", "free")
    if tier != "admin":
        return jsonify({"error": "Live motion preview is admin-only.",
                        "status": "denied"}), 403
    if not COMFY_LOCAL_URL:
        return jsonify({"status": "unavailable",
                        "message": "Local GPU tunnel is not configured."}), 503
    if _comfy_locked():
        return jsonify({"status": "unavailable",
                        "message": "Local GPU recently failed — try again in a few minutes."}), 503

    data = request.json or {}
    prompt = (data.get("prompt") or "").strip()[:1500]
    seed = int(data.get("seed") or 0)
    image_url = (data.get("image_url") or "").strip()
    if not prompt or not image_url:
        return jsonify({"error": "missing prompt or image_url"}), 400

    key = _scene_video_key(prompt, seed)

    # Cache hit — return immediately, no worker needed.
    if _scene_video_cached_path(key):
        return jsonify({"key": key, "status": "ready", "cached": True,
                        "url": f"/scene-video/file/{key}.mp4"})

    # Existing job for this key — return its current state without re-spawning.
    with _SCENE_VIDEO_JOBS_LOCK:
        existing = scene_video_jobs.get(key)
        if existing and existing.get("status") in ("queued", "generating", "ready"):
            return jsonify({"key": key, "cached": False, **existing})

        scene_video_jobs[key] = {
            "status": "queued", "progress": 5, "message": "Queued for local GPU…",
            "created_at": time.time(),
        }
        _gc_scene_video_jobs()

    cookie_header = request.headers.get("Cookie", "")
    host_url = request.host_url  # captured here — worker has no request ctx
    threading.Thread(
        target=_do_scene_video,
        args=(key, prompt, seed, image_url, cookie_header, host_url),
        daemon=True,
    ).start()

    return jsonify({"key": key, "status": "queued", "cached": False,
                    "message": "Queued for local GPU…"})


@app.route("/scene-video/status/<key>")
@login_required
def scene_video_status(key):
    """Polling endpoint for the viewer. Cheap — just reads the dict."""
    if not re.fullmatch(r"[a-f0-9]{32}", key or ""):
        return jsonify({"status": "not_found"}), 404
    # File-on-disk wins over in-memory state — survives worker restarts.
    if _scene_video_cached_path(key):
        return jsonify({"status": "ready", "url": f"/scene-video/file/{key}.mp4"})
    job = scene_video_jobs.get(key)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify({"key": key, **job})


@app.route("/scene-video/file/<key>.mp4")
@login_required
def scene_video_file(key):
    """Serve a cached scene MP4. Browser <video> element fetches this once
    /scene-video/status returns ready."""
    if not re.fullmatch(r"[a-f0-9]{32}", key or ""):
        return "bad key", 404
    path = _scene_video_cached_path(key)
    if not path:
        return "not ready", 404
    return send_file(path, mimetype="video/mp4", conditional=True,
                     download_name=f"scene_{key[:8]}.mp4")


# ── Video export ───────────────────────────────────────────────────────────────

MIN_DUR = {"episode":15,"short":30,"movie":90}
ANIM_CLIP_DUR = 5   # seconds per scene clip

# Wan 2.5 native output resolution. fal.ai prices it tiered:
#   480p  → $0.05/sec ($0.25 per 5-sec clip)  — low res, motion looks rougher
#   720p  → $0.10/sec ($0.50 per 5-sec clip)  — default, what create.wan.video uses
#   1080p → $0.15/sec ($0.75 per 5-sec clip)  — sharpest, 3x cost
# Override via env var WAN_RESOLUTION when willing to pay more for sharper motion.
# Resolution affects perceived smoothness: higher res = less aliasing on fast
# motion, so 720p reads noticeably more fluent than 480p even with the same fps.
WAN_RESOLUTION = os.environ.get("WAN_RESOLUTION", "720p").strip().lower()
if WAN_RESOLUTION not in ("480p", "720p", "1080p"):
    WAN_RESOLUTION = "720p"


def _build_scene_image_prompt(scene, characters_by_name, style_suffix):
    """
    Compose the final image prompt for a scene by:
      1) Starting from Claude's scene.image_prompt
      2) Re-injecting the character_anchor for every character that speaks or
         is named in this scene's dialogue/setting. This is the single biggest
         lever for visual consistency — without it, every Pollinations call rolls
         a fresh face for the protagonist.
      3) Appending the chosen art style's suffix.

    Why we re-inject instead of trusting Claude to do it: Claude does include
    anchors when prompted, but it abbreviates in later scenes ("the hunter" instead
    of the full visual descriptor) which loses anchor signal at the image model.
    Re-injecting verbatim is cheap and dramatically improves cross-scene continuity.
    """
    base = scene.get("image_prompt") or f"anime, {scene.get('setting','')}"

    anchors_to_add = []
    if characters_by_name:
        dlg = scene.get("dialogue") or []
        speakers = {(d.get("speaker") or "").strip() for d in dlg}
        for name, anchor in characters_by_name.items():
            if not anchor:
                continue
            if name in speakers or (name and name in base):
                # Only inject if the anchor isn't already substantially present
                if anchor[:30] not in base:
                    anchors_to_add.append(f"{name}: {anchor}")

    parts = [base]
    if anchors_to_add:
        parts.append("featuring " + "; ".join(anchors_to_add))
    parts.append(style_suffix)
    return ", ".join(parts)


def _get_ffmpeg():
    """Return path to the bundled ffmpeg binary."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def animate_scene_local(img_path, scene, tmp_dir, scene_idx, job):
    """
    Cinematic anime-style animation via FFmpeg zoompan.
    Sharp & clean — no AI waviness. Mood-driven camera + color grade.
    24fps · 5 sec · ~2 MB per clip · renders in 3-8 seconds.
    """
    ffmpeg   = _get_ffmpeg()
    mood     = (scene.get("mood") or "dramatic").lower() if isinstance(scene, dict) else "dramatic"
    out_path = os.path.join(tmp_dir, f"anim_{scene_idx}.mp4")
    fps, dur = 24, 5
    nf       = fps * dur           # 120 frames
    W, H     = 1920, 1080
    PW, PH   = 2560, 1440          # overscan canvas (room to zoom)

    # Center-crop helpers (FFmpeg expression vars: iw/ih=input dims, zoom=current zoom)
    CTR  = f"x=(iw-iw/zoom)/2:y=(ih-ih/zoom)/2"
    PANR = f"x=min(iw*(1-1/zoom)\\,on*1.1):y=(ih-ih/zoom)/2"   # pan right
    PANL = f"x=max(0\\,iw*(1-1/zoom)-on*1.1):y=(ih-ih/zoom)/2"  # pan left

    # Note: commas inside min()/max() must be escaped as \, in FFmpeg filter strings
    MOTIONS = {
        "dramatic":   f"z=min(zoom+0.002\\,1.3):{CTR}:d={nf}:s={W}x{H}",
        "tense":      f"z=min(zoom+0.0025\\,1.35):{CTR}:d={nf}:s={W}x{H}",
        "violent":    f"z=min(zoom+0.003\\,1.4):{CTR}:d={nf}:s={W}x{H}",
        "epic":       f"z=if(lte(on\\,1)\\,1.3\\,max(1.0\\,zoom-0.002)):{CTR}:d={nf}:s={W}x{H}",
        "triumphant": f"z=if(lte(on\\,1)\\,1.25\\,max(1.0\\,zoom-0.0018)):{CTR}:d={nf}:s={W}x{H}",
        "sorrowful":  f"z=1.12:{PANR}:d={nf}:s={W}x{H}",
        "melancholic":f"z=1.1:{PANR}:d={nf}:s={W}x{H}",
        "peaceful":   f"z=1.1:{PANL}:d={nf}:s={W}x{H}",
        "mysterious": f"z=min(zoom+0.001\\,1.15):{CTR}:d={nf}:s={W}x{H}",
        "unsettling": f"z=min(zoom+0.002\\,1.28):{CTR}:d={nf}:s={W}x{H}",
        "seductive":  f"z=1.1:{PANL}:d={nf}:s={W}x{H}",
    }
    zm = MOTIONS.get(mood, MOTIONS["dramatic"])

    # eq filter: contrast / saturation / brightness  (no hue filter — not in essentials build)
    GRADES = {
        "dramatic":   "eq=contrast=1.28:saturation=0.82",
        "tense":      "eq=contrast=1.35:saturation=0.65:brightness=-0.05",
        "violent":    "eq=contrast=1.45:saturation=0.55:brightness=-0.08",
        "epic":       "eq=contrast=1.22:saturation=1.35:brightness=0.06",
        "triumphant": "eq=contrast=1.18:saturation=1.45:brightness=0.08",
        "sorrowful":  "eq=contrast=1.12:saturation=0.35:brightness=-0.12",
        "melancholic":"eq=contrast=1.10:saturation=0.40:brightness=-0.10",
        "peaceful":   "eq=contrast=1.05:saturation=1.15:brightness=0.04",
        "mysterious": "eq=contrast=1.25:saturation=0.55:brightness=-0.07",
        "unsettling": "eq=contrast=1.30:saturation=0.50:brightness=-0.08",
        "seductive":  "eq=contrast=1.20:saturation=1.20:brightness=0.02",
    }
    grade = GRADES.get(mood, GRADES["dramatic"])

    vf = f"scale={PW}:{PH},zoompan={zm},{grade},format=yuv420p"
    job["message"] = f"Scene {scene_idx+1} — cinematic motion ({mood})…"

    try:
        res = subprocess.run(
            [ffmpeg, "-y", "-loop", "1", "-i", img_path,
             "-vf", vf, "-r", str(fps), "-t", str(dur),
             "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-an",
             out_path],
            capture_output=True, timeout=120,
        )
        if res.returncode == 0 and os.path.exists(out_path):
            job["elapsed_seconds"] = int(time.time() - job["start_time"])
            return out_path
    except Exception:
        pass
    return None


def animate_scene_fal(img_path, prompt, tmp_dir, scene_idx, job, model="wan"):
    """
    Animates a scene using fal.ai.

    Models (chosen by cost/quality tradeoff):
      - "wan"  -> fal-ai/wan-25-preview/image-to-video at 480p
                  ~$0.05/sec × 5s = $0.25/clip. Default for Hunter tier.
                  Solid anime motion, fast turnaround.
      - "kling"-> fal-ai/kling-video/v1.6/pro/image-to-video
                  ~$0.07/sec × 5s = $0.35/clip. Premium / Monarch tier.
                  Stronger camera choreography and consistency.

    Why two tiers: an animated movie has 24 clips. At Kling pricing a single
    movie export costs ~$8.40 in fal calls; at Wan pricing it's ~$6.00. We
    default to the cheaper model and only fall back to Kling on Wan failure.
    """
    if not FAL_KEY or _fal_locked():
        return None

    import fal_client

    clean_prompt = prompt[:400].split(",")[0] + ", fluid anime motion, cinematic camera"

    try:
        job["message"] = f"Scene {scene_idx+1} — uploading to fal.ai…"
        fal_img_url = fal_client.upload_file(img_path)

        if model == "kling":
            endpoint = "fal-ai/kling-video/v1.6/pro/image-to-video"
            args = {
                "image_url":    fal_img_url,
                "prompt":       clean_prompt,
                "duration":     str(ANIM_CLIP_DUR),
                "aspect_ratio": "16:9",
            }
            job["message"] = f"Scene {scene_idx+1} — animating with Kling Pro…"
        else:
            # Wan 2.5 — same model create.wan.video runs in their explore page.
            # Resolution is configurable via WAN_RESOLUTION env var (480p / 720p /
            # 1080p). Default 720p because 480p had visibly rough motion and
            # 1080p triples the cost; 720p matches what the official site uses.
            endpoint = "fal-ai/wan-25-preview/image-to-video"
            args = {
                "image_url":  fal_img_url,
                "prompt":     clean_prompt,
                "resolution": WAN_RESOLUTION,
                "duration":   str(ANIM_CLIP_DUR),
            }
            job["message"] = f"Scene {scene_idx+1} — animating with Wan 2.5 @ {WAN_RESOLUTION}…"

        # subscribe() blocks until complete and handles all polling internally
        result = fal_client.subscribe(endpoint, arguments=args, with_logs=False)

        # Response shape varies; both Wan and Kling return {"video": {"url": ...}}
        vid_url = (result.get("video") or {}).get("url")
        if not vid_url:
            vids    = result.get("videos") or []
            vid_url = vids[0].get("url") if vids else None

        if not vid_url:
            return None

        job["message"] = f"Scene {scene_idx+1} — downloading clip…"
        vid_bytes = http.get(vid_url, timeout=120).content
        out_path  = os.path.join(tmp_dir, f"anim_{scene_idx}.mp4")
        with open(out_path, "wb") as fh:
            fh.write(vid_bytes)

        job["elapsed_seconds"] = int(time.time() - job["start_time"])
        return out_path

    except Exception as exc:
        msg = str(exc)
        if "User is locked" in msg or "Exhausted balance" in msg:
            _mark_fal_locked(f"animation — {msg[:80]}")
        return None


# ── Local ComfyUI bridge (Justen's GPU) ────────────────────────────────────────
#
# When `COMFY_LOCAL_URL` env var is set, admin tier routes animation calls to
# Justen's home RTX 4070 Ti SUPER instead of paying fal.ai. The local GPU runs
# Wan 2.2 5B I2V via ComfyUI's HTTP API, exposed to the public internet via a
# Cloudflare tunnel. A shared bearer token gates access so nobody else can
# burn his electricity.
#
# Workflow shape (Wan 2.2 5B image-to-video, 24fps, 720p, ~5s clip):
#   UNETLoader → wan2.2_ti2v_5B_fp16
#   CLIPLoader → umt5_xxl_fp8 (type=wan)
#   VAELoader  → wan2.2_vae
#   LoadImage  → uploaded still
#   CLIPTextEncode × 2 (positive + negative)
#   Wan22ImageToVideoLatent → conditions the latent on the uploaded image
#   KSampler   → 20 steps, cfg 5, euler/simple
#   VAEDecode  → frames
#   CreateVideo + SaveVideo → MP4 in ComfyUI's output dir

_WAN22_NEGATIVE_PROMPT = (
    "low quality, blurry, distorted, jpeg artifacts, watermark, text, logo, "
    "extra limbs, deformed face, broken anatomy, static photo, frozen frame"
)

def _comfy_workflow_wan22_i2v(image_filename: str, prompt: str, seed: int,
                              width: int = 1280, height: int = 704,
                              length: int = 49) -> dict:
    """Build the API-format graph ComfyUI's /prompt endpoint expects.

    length=49 frames at 24fps ≈ 2 sec clips. We used to do 121 (5 sec) but
    that crashed Render's 512MB free-tier worker: MoviePy holds each clip's
    frames in RAM and 3 × 5-sec × 720p clips overflowed memory mid-export.
    49 frames cuts per-clip memory to ~140MB so a 15-scene movie fits.
    Constraint: (length - 1) %% 4 == 0, so valid values are 49, 81, 121, 161.
    """
    clean = (prompt or "")[:380].strip()
    return {
        "1":  {"class_type": "UNETLoader",
               "inputs": {"unet_name": "wan2.2_ti2v_5B_fp16.safetensors",
                          "weight_dtype": "default"}},
        "2":  {"class_type": "CLIPLoader",
               "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                          "type": "wan"}},
        "3":  {"class_type": "VAELoader",
               "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
        "4":  {"class_type": "LoadImage",
               "inputs": {"image": image_filename}},
        "5":  {"class_type": "CLIPTextEncode",
               "inputs": {"clip": ["2", 0],
                          "text": f"{clean}, fluid anime motion, cinematic camera, detailed character"}},
        "6":  {"class_type": "CLIPTextEncode",
               "inputs": {"clip": ["2", 0],
                          "text": _WAN22_NEGATIVE_PROMPT}},
        "7":  {"class_type": "Wan22ImageToVideoLatent",
               "inputs": {"vae": ["3", 0],
                          "width": width, "height": height, "length": length,
                          "batch_size": 1, "start_image": ["4", 0]}},
        "8":  {"class_type": "KSampler",
               "inputs": {"model": ["1", 0], "seed": int(seed),
                          "steps": 20, "cfg": 5.0,
                          "sampler_name": "euler", "scheduler": "simple",
                          "denoise": 1.0,
                          "positive": ["5", 0], "negative": ["6", 0],
                          "latent_image": ["7", 0]}},
        "9":  {"class_type": "VAEDecode",
               "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo",
               "inputs": {"images": ["9", 0], "fps": 24.0}},
        "11": {"class_type": "SaveVideo",
               "inputs": {"video": ["10", 0],
                          "filename_prefix": "animeforge/clip",
                          "format": "mp4", "codec": "h264"}},
    }


def _comfy_headers() -> dict:
    """Authorization header for every call. cloudflared passes it through
    unchanged; ComfyUI doesn't care about it; a small middleware proxy on
    the local machine could enforce it. For now we send it on every call so
    we can lock down the tunnel later without code changes."""
    h = {}
    if COMFY_LOCAL_TOKEN:
        h["Authorization"] = f"Bearer {COMFY_LOCAL_TOKEN}"
    return h


_FFMPEG_BIN: str | None = None

def _ffmpeg_path() -> str | None:
    """Resolve ffmpeg lazily and cache. Tries PATH first, then the binary
    that ships with imageio-ffmpeg (already a transitive dep via moviepy)."""
    global _FFMPEG_BIN
    if _FFMPEG_BIN is not None:
        return _FFMPEG_BIN or None
    found = shutil.which("ffmpeg")
    if not found:
        try:
            import imageio_ffmpeg
            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            found = None
    _FFMPEG_BIN = found or ""
    return found


# Target output frame rate for every animated clip — both per-scene previews
# and the final exported movie. Wan 2.5 / Wan 2.2 both emit 24fps native, so
# we motion-interpolate every clip up to TARGET_FPS before encoding. 60 is the
# minimum that feels truly fluent; 48 still reads as cinema-cadence on fast
# motion which Justen flagged as wonky.
TARGET_FPS = 60

def _interpolate_to_target_fps(src: str, dst: str, target_fps: int = TARGET_FPS) -> bool:
    """Motion-interpolate src mp4 -> dst mp4 at target_fps using ffmpeg
    minterpolate (mci + bilat + obmc + hexagonal search).

    Returns True on success, False on any failure so callers can fall back to
    the original clip without raising.

    Tuning history (2026-05-23): benchmarked 10 minterpolate variants on a
    5-sec 720p clip (production Wan resolution). The original aobmc/vsbmc
    flags I suspected were no-ops on cost — the real bottleneck is
    motion-estimation. Switching me_mode=bidir -> bilat and adding me=hexbs
    (the same hex search x264 itself uses) drops a 720p clip from 45s to
    26s (1.73x faster, repeatable across runs). True motion-compensated
    interpolation (mi_mode=mci) is preserved, so synthesized frames remain
    crisp on real Wan/Kling motion — only the search algorithm got cheaper.

    Considered and rejected: mi_mode=blend (1s/clip but visible ghosting
    on action scenes); mb_size=32 (errored on this ffmpeg build)."""
    ff = _ffmpeg_path()
    if not ff or not os.path.exists(src):
        return False
    cmd = [
        ff, "-y", "-loglevel", "error",
        "-i", src,
        "-vf", f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=obmc:me_mode=bilat:me=hexbs",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "20",
        "-an",
        dst,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 10_000:
            return True
        return False
    except Exception:
        return False


# Kept as a back-compat alias so any external caller / future code referencing
# the old name still works after the 48→60 bump.
def _interpolate_to_48fps(src: str, dst: str) -> bool:
    return _interpolate_to_target_fps(src, dst, target_fps=TARGET_FPS)


def _comfy_workflow_cogvideox_i2v(image_filename: str, prompt: str, seed: int,
                                   num_frames: int = 49, width: int = 720,
                                   height: int = 480) -> dict:
    """ComfyUI workflow for CogVideoX-5B-I2V GGUF Q4 via kijai's wrapper.

    Higher-quality alternative to Wan 2.2 5B for image-to-video. Built 2026-05-23
    after Justen called Wan output "ok" but not Noct.Co tier. Q4 quantization
    drops VRAM use enough to stay fully on GPU on the 4070 Ti SUPER.

    Per-clip time: ~8-12 min (vs ~100 sec for Wan). Quality tradeoff is worth
    it for hero scenes; default to Wan for fast iteration.

    Requires custom_nodes: ComfyUI-CogVideoXWrapper.
    Requires models:
      - CogVideo/CogVideoX_5b_I2V_GGUF_Q4_0.safetensors (3.4GB)
      - text_encoders/t5/google_t5-v1_1-xxl_encoderonly-fp8_e4m3fn.safetensors (4.8GB)

    Workflow output is at node id "11" (SaveVideo) so the existing
    animate_scene_comfy poll logic works unchanged.
    """
    negative = ("The video is not of a high quality, it has a low resolution. "
                "Watermark present in each frame. Strange motion trajectory. "
                "Blurry, low quality, distorted, jpeg artifacts.")
    return {
        # GGUF loader outputs [model, vae]
        "1": {"class_type": "DownloadAndLoadCogVideoGGUFModel",
              "inputs": {
                  "model": "CogVideoX_5b_I2V_GGUF_Q4_0.safetensors",
                  "vae_precision": "bf16",
                  "fp8_fastmode": False,
                  "load_device": "main_device",
                  "enable_sequential_cpu_offload": False,
                  "attention_mode": "sdpa",
              }},
        # T5-XXL encoder
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": r"t5\google_t5-v1_1-xxl_encoderonly-fp8_e4m3fn.safetensors",
                         "type": "sd3"}},
        "3": {"class_type": "CogVideoTextEncode",
              "inputs": {"clip": ["2", 0], "prompt": prompt,
                         "strength": 1.0, "force_offload": True}},
        "4": {"class_type": "CogVideoTextEncode",
              "inputs": {"clip": ["2", 0], "prompt": negative,
                         "strength": 1.0, "force_offload": True}},
        "5": {"class_type": "LoadImage",
              "inputs": {"image": image_filename}},
        "6": {"class_type": "ImageScale",
              "inputs": {"image": ["5", 0], "upscale_method": "lanczos",
                         "width": width, "height": height, "crop": "disabled"}},
        # I2V image conditioning latent (vae from loader output 1)
        "7": {"class_type": "CogVideoImageEncode",
              "inputs": {"vae": ["1", 1], "start_image": ["6", 0],
                         "enable_tiling": False}},
        # Sampler — image_cond_latents required for I2V
        "8": {"class_type": "CogVideoSampler",
              "inputs": {
                  "model": ["1", 0],
                  "positive": ["3", 0],
                  "negative": ["4", 0],
                  "image_cond_latents": ["7", 0],
                  "num_frames": num_frames,
                  "steps": 25,
                  "cfg": 6.0,
                  "seed": int(seed),
                  "scheduler": "CogVideoXDDIM",
                  "denoise_strength": 1.0,
              }},
        # Decode latent — vae from loader output 1
        "9": {"class_type": "CogVideoDecode",
              "inputs": {"vae": ["1", 1], "samples": ["8", 0],
                         "enable_vae_tiling": True,
                         "tile_sample_min_height": 240,
                         "tile_sample_min_width": 360,
                         "tile_overlap_factor_height": 0.2,
                         "tile_overlap_factor_width": 0.2,
                         "auto_tile_size": True}},
        "10": {"class_type": "CreateVideo",
               "inputs": {"images": ["9", 0], "fps": 8.0}},
        # SaveVideo — id "11" matches the Wan workflow so the polling loop
        # in animate_scene_comfy doesn't need to change.
        "11": {"class_type": "SaveVideo",
               "inputs": {"video": ["10", 0],
                          "filename_prefix": "animeforge/cogvideo",
                          "format": "mp4", "codec": "h264"}},
    }


def animate_scene_comfy(img_path, prompt, tmp_dir, scene_idx, job, length: int = 49,
                        backend: str = "wan"):
    """Animate a scene using the local ComfyUI tunnel (admin tier path).

    Returns the MP4 path on success, None on failure (circuit breaker trips
    on connection errors so callers can fall through to fal.ai or static art).
    Total request takes ~3-5 minutes on the 4070 Ti SUPER for a 5-sec clip.

    `length` is the Wan22ImageToVideoLatent frame count; valid values satisfy
    (length-1) %% 4 == 0 (49 = 2s, 81 = 3.4s, 121 = 5s at 24fps). Export path
    keeps length=49 to fit Render's 512 MB worker; scene-viewer path uses 81
    for a smoother feel since it's only one clip at a time.
    """
    if not COMFY_LOCAL_URL or _comfy_locked():
        return None

    import json, mimetypes
    base = COMFY_LOCAL_URL
    headers = _comfy_headers()

    try:
        # Upload the scene image — ComfyUI saves it under its `input/` dir and
        # returns the filename we then reference from the LoadImage node.
        with open(img_path, "rb") as fh:
            mime = mimetypes.guess_type(img_path)[0] or "image/png"
            files = {"image": (os.path.basename(img_path), fh.read(), mime)}
        job["message"] = f"Scene {scene_idx+1} — uploading to local GPU…"
        up = http.post(f"{base}/upload/image", files=files, headers=headers, timeout=60)
        up.raise_for_status()
        uploaded_name = up.json().get("name") or os.path.basename(img_path)

        # Submit the workflow. ComfyUI returns a prompt_id we then poll for.
        seed = abs(hash((img_path, scene_idx))) % 100000
        if backend == "cogvideox":
            # CogVideoX-5B-I2V GGUF Q4 — slower (~8-12 min/clip) but higher
            # quality than Wan 2.2 5B. Justen opt-in for hero scenes.
            workflow = _comfy_workflow_cogvideox_i2v(uploaded_name, prompt, seed,
                                                     num_frames=length)
            job["message"] = f"Scene {scene_idx+1} — generating with CogVideoX on local GPU…"
        else:
            workflow = _comfy_workflow_wan22_i2v(uploaded_name, prompt, seed, length=length)
            job["message"] = f"Scene {scene_idx+1} — generating on local GPU…"
        sub = http.post(f"{base}/prompt",
                        json={"prompt": workflow, "client_id": "animeforge"},
                        headers=headers, timeout=30)
        sub.raise_for_status()
        prompt_id = sub.json().get("prompt_id")
        if not prompt_id:
            _mark_comfy_locked("no prompt_id returned from /prompt")
            return None

        # Poll history. Wan 2.2 5B takes ~3-5 min on a 4070 Ti SUPER for 5 sec
        # of 720p video; CogVideoX GGUF Q4 takes ~8-12 min for similar length.
        # Caller's animate_scene loop is serialized so we won't queue-bomb the GPU.
        deadline = time.time() + (1200 if backend == "cogvideox" else 600)
        result_filename = None
        result_subfolder = ""
        while time.time() < deadline:
            time.sleep(3)
            try:
                h = http.get(f"{base}/history/{prompt_id}", headers=headers, timeout=15)
                if h.status_code != 200:
                    continue
                hist = h.json().get(prompt_id) or {}
                outputs = hist.get("outputs") or {}
                # SaveVideo node id = "11" — its output entry contains the saved file
                save_out = outputs.get("11") or {}
                files = save_out.get("videos") or save_out.get("images") or []
                if files:
                    result_filename = files[0].get("filename")
                    result_subfolder = files[0].get("subfolder", "")
                    break
            except Exception:
                continue

        if not result_filename:
            _mark_comfy_locked(f"generation timed out ({backend})")
            return None

        # Fetch the rendered MP4 from ComfyUI's /view endpoint.
        job["message"] = f"Scene {scene_idx+1} — downloading clip from local GPU…"
        v = http.get(f"{base}/view",
                     params={"filename": result_filename,
                             "subfolder": result_subfolder,
                             "type": "output"},
                     headers=headers, timeout=120)
        v.raise_for_status()
        out_path = os.path.join(tmp_dir, f"anim_{scene_idx}.mp4")
        with open(out_path, "wb") as fh:
            fh.write(v.content)

        job["elapsed_seconds"] = int(time.time() - job["start_time"])
        return out_path

    except Exception as exc:
        _mark_comfy_locked(f"animate — {str(exc)[:80]}")
        return None


def do_export(job_id, story, mode, quality="1080p", animate=False, style_key=DEFAULT_STYLE, anim_model="wan"):
    job = export_jobs[job_id]
    job["start_time"] = time.time()
    tmp = tempfile.mkdtemp(prefix="animeforge_")
    try:
        from moviepy import ImageClip, AudioFileClip, concatenate_videoclips
        import moviepy
        from PIL import Image
        from gtts import gTTS

        scenes  = story.get("scenes", [])
        n       = len(scenes)
        min_dur = MIN_DUR.get(mode, 20)
        clips   = []

        # Resolve style suffix and build the character anchor map once for the run
        style_meta   = get_style(style_key)
        style_suffix = style_meta["image_suffix"]
        characters_by_name = {}
        for c in (story.get("characters") or []):
            name = (c.get("name") or "").strip()
            anchor = (c.get("character_anchor") or "").strip()
            if name and anchor:
                characters_by_name[name] = anchor

        # Target output dims. The 4K branch is what the user pays for.
        OUT_W, OUT_H = (3840, 2160) if quality == "4k" else (1920, 1080)
        # Per-scene RENDER dims. Capped at 720p on Render free tier — even
        # 1080p per-scene encode + accumulated SDK/httpx state crosses the
        # 512MB worker cap on real Claude stories (matrix test showed
        # SIGKILL at scene 1 even at 1080p). 720p reduces PIL/ffmpeg RAM by
        # ~4x, then we upscale at the final concat pass. Override via env
        # var SCENE_RENDER_W on Starter+ plans to get 1080p per-scene.
        _render_w = int(os.environ.get("SCENE_RENDER_W", "0") or 0)
        if _render_w >= 1920:
            W, H = 1920, 1080
        else:
            W, H = 1280, 720
        job["scenes_total"] = n
        # Use a stable per-export seed so re-runs of the same story produce the same characters
        run_seed = abs(hash((story.get("title",""), style_key))) % 100000

        for i, sc in enumerate(scenes):
            job["progress"]            = int(10 + 75 * i / n)
            job["scenes_done"]         = i
            job["current_scene_title"] = sc.get("title", "")

            elapsed = time.time() - job["start_time"]
            job["elapsed_seconds"] = int(elapsed)
            if i > 0:
                job["eta_seconds"] = int(elapsed / i * (n - i))

            # ── Step 1: Generate scene image (live preview shown here) ──
            # Order: photoreal-local (admin only) -> fal.ai Hunyuan -> Pollinations.
            # The job message reflects what will actually fire — saying
            # "Hunyuan 3.0" when fal is circuit-locked just confuses users.
            if FAL_KEY and not _fal_locked():
                gen_label = "Hunyuan 3.0"
            else:
                gen_label = "Pollinations"
            job["message"] = f"Scene {i+1}/{n} — generating art ({gen_label})…"
            img_path = os.path.join(tmp, f"img_{i}.jpg")
            raw_prompt = _build_scene_image_prompt(sc, characters_by_name, style_suffix)
            # Stable per-scene seed bound to the run seed → same characters across rerolls
            seed = run_seed + i * 17 + 3

            img_bytes = None
            # PHOTOREAL path (admin + comfy_local): CyberRealistic Pony with
            # HiRes + FaceDetailer. Justen called this output "great" 2026-05-22.
            # Only kicks in when style_key=="photoreal" — the Pony score tags
            # in the photoreal style suffix would muddy any other style pack.
            if (style_key == "photoreal"
                    and COMFY_LOCAL_URL and not _comfy_locked()):
                job["message"] = f"Scene {i+1}/{n} — generating photoreal art on local GPU…"
                img_bytes = _comfy_image_photoreal(raw_prompt, W, H, seed)
                if img_bytes:
                    job["message"] = f"Scene {i+1}/{n} — photoreal art ready ✓"
            if not img_bytes and FAL_KEY and not _fal_locked():
                img_bytes = _fal_image(raw_prompt, W, H, model="hunyuan", seed=seed)
                if img_bytes:
                    job["message"] = f"Scene {i+1}/{n} — Hunyuan art ready ✓"
            if not img_bytes:
                # Pollinations fallback — retry + model fallback (flux x2 -> turbo)
                # so one slow Pollinations request can't stall the whole export.
                img_bytes = _pollinations_image(raw_prompt, W, H, seed)
                if img_bytes:
                    job["message"] = f"Scene {i+1}/{n} — Pollinations art ready ✓"

            try:
                if img_bytes:
                    with open(img_path, "wb") as f:
                        f.write(img_bytes)
                    img = Image.open(img_path).convert("RGB").resize((W, H), Image.LANCZOS)
                    img.save(img_path, "JPEG", quality=92)
                    # Stream 320×180 thumbnail to browser (live preview — step 2 visible to user)
                    thumb = img.copy()
                    thumb.thumbnail((320, 180))
                    buf = io.BytesIO()
                    thumb.save(buf, "JPEG", quality=60)
                    job["current_img_b64"] = ("data:image/jpeg;base64,"
                                              + base64.b64encode(buf.getvalue()).decode())
                else:
                    Image.new("RGB", (W, H), (5, 10, 25)).save(img_path, "JPEG")
            except Exception:
                Image.new("RGB", (W, H), (5, 10, 25)).save(img_path, "JPEG")

            # ── Step 2: Animate ──
            # Three animation paths:
            #   anim_model="wan"    → fal.ai Wan 2.5 (cheap, ~$0.25/clip)
            #   anim_model="kling"  → fal.ai Kling 1.6 Pro (premium, ~$0.35/clip)
            #   anim_model="local"  → FFmpeg cinematic zoompan (free but SLOW on
            #                          Render free tier — ~60s/scene shared vCPU)
            #
            # When fal.ai is the chosen path and fails (balance exhausted, network),
            # we *don't* fall back to local FFmpeg anymore — it's so slow on shared
            # CPU that users think the export hung. Instead we use the static image
            # for that scene and surface a clear message so the user knows fal.ai
            # is the gating issue. Total export time on free tier drops from
            # 5-10 min back to ~1-2 min when balance is the only blocker.
            vid_path = None
            if animate:
                # Dispatch order matters: comfy_local first because it's free for admin,
                # fal.ai second because paying users fund it, FFmpeg zoompan last because
                # it's slow on Render shared CPU. Each path falls through to the next
                # only when the previous one is unavailable / circuit-broken / explicitly
                # not chosen — we never silently swap a paid model for a free one mid-run.
                if anim_model in ("comfy_local", "comfy_cogvideox") and COMFY_LOCAL_URL and not _comfy_locked():
                    backend = "cogvideox" if anim_model == "comfy_cogvideox" else "wan"
                    vid_path = animate_scene_comfy(img_path, raw_prompt, tmp, i, job,
                                                   backend=backend)
                    label = "CogVideoX" if backend == "cogvideox" else "Wan"
                    if vid_path:
                        job["message"] = f"Scene {i+1}/{n} — {label} local GPU animation done ✓"
                    elif FAL_KEY and not _fal_locked():
                        # Justen's PC offline / tunnel dropped → fall through to fal.ai
                        # so an in-flight admin export doesn't die when the home GPU
                        # disconnects mid-render.
                        job["message"] = f"Scene {i+1}/{n} — local GPU unreachable, trying fal.ai…"
                        vid_path = animate_scene_fal(img_path, raw_prompt, tmp, i, job, model="wan")
                        if vid_path:
                            job["message"] = f"Scene {i+1}/{n} — fal.ai fallback animation done ✓"
                        else:
                            job["message"] = f"Scene {i+1}/{n} — both local GPU and fal.ai unavailable. Using static art."
                    else:
                        job["message"] = f"Scene {i+1}/{n} — local GPU unreachable, using static art"
                elif anim_model in ("wan", "kling") and FAL_KEY and not _fal_locked():
                    vid_path = animate_scene_fal(img_path, raw_prompt, tmp, i, job, model=anim_model)
                    if vid_path:
                        job["message"] = f"Scene {i+1}/{n} — {anim_model} animation done ✓"
                    else:
                        job["message"] = f"Scene {i+1}/{n} — fal.ai unavailable (top up balance for real motion). Using static art."
                elif anim_model == "local":
                    # User explicitly picked local — honor it even though it's slow.
                    vid_path = animate_scene_local(img_path, sc, tmp, i, job)
                    if vid_path:
                        job["message"] = f"Scene {i+1}/{n} — zoom motion ready ✓"
                    else:
                        job["message"] = f"Scene {i+1}/{n} — animation failed, using static art"
                elif _fal_locked():
                    job["message"] = f"Scene {i+1}/{n} — fal.ai balance exhausted, using static art"
                # Else: no FAL_KEY and not local — just use static

                # ── Step 2b: motion-interpolate animated clips to TARGET_FPS ──
                # Wan / Kling deliver 24fps natively; the FFmpeg zoompan path is
                # already 30fps. Interpolating zoompan would be wasted work, so
                # only smooth the real-motion paths. Silent fallback to the raw
                # clip on any failure so a single bad scene can't tank the export.
                if vid_path and anim_model in ("wan", "kling", "comfy_local", "comfy_cogvideox"):
                    smooth_path = os.path.join(tmp, f"smooth_{i:03d}.mp4")
                    job["message"] = f"Scene {i+1}/{n} — smoothing to {TARGET_FPS}fps…"
                    if _interpolate_to_target_fps(vid_path, smooth_path):
                        vid_path = smooth_path

            # ── Step 3: Build TTS audio ──
            audio_path = os.path.join(tmp, f"audio_{i}.mp3")
            tts_parts  = [f"{sc.get('title','')}. {sc.get('action','')}"]
            for line in sc.get("dialogue", []):
                tts_parts.append(f"{line.get('speaker','')}: {line.get('line','')}")
            audio = None
            duration = min_dur
            try:
                gTTS(" ".join(tts_parts), lang="en").save(audio_path)
                # Read duration via a lightweight ffmpeg probe instead of
                # loading the full AudioFileClip. Loading the clip held the
                # decoded mp3 in MoviePy's internal buffer for the rest of
                # the scene's processing — a ~5MB-per-scene RAM hit that
                # compounded with the ffmpeg subprocess on Render's 512MB
                # free worker. We only need the duration; the file path
                # gets passed to ffmpeg directly below.
                try:
                    ff_probe = subprocess.run(
                        [_get_ffmpeg(), "-i", audio_path],
                        capture_output=True, timeout=10)
                    txt = (ff_probe.stderr or b"").decode("utf-8", "replace")
                    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
                    if m:
                        audio_dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    else:
                        audio_dur = min_dur
                except Exception:
                    audio_dur = min_dur
                duration = max(audio_dur + 3, min_dur)
                # For the MoviePy video-scene path below we still need an
                # AudioFileClip object so we can attach + fade audio. For
                # the static (ffmpeg-direct) path we just need the path.
                if vid_path:
                    audio = AudioFileClip(audio_path)
            except Exception:
                pass

            # ── Build clip + write per-scene MP4 immediately ──
            # MEMORY: Render's free tier worker is 512MB. Holding 5+ MoviePy
            # clips and then concatenating them inside Python kept blowing
            # past the limit and silently SIGKILLing gunicorn mid-export. The
            # fix is per-scene streaming: build one clip, write it to disk at
            # the target W/H/fps with H.264, close everything to release frame
            # buffers, then ffmpeg-concat all the per-scene MP4s at the end.
            # Memory now stays flat (~140MB peak) regardless of scene count.
            scene_out = os.path.join(tmp, f"final_scene_{i:03d}.mp4")
            # Two code paths:
            #   - vid_path set (Wan/Kling/local-GPU animation succeeded): use
            #     MoviePy so we get the audio attach + resize semantics for
            #     small 720p video clips where it's cheap.
            #   - vid_path None (static-image scene, including the "animate
            #     requested but backend offline" degradation): skip MoviePy
            #     entirely and let ffmpeg render the still directly. MoviePy's
            #     ImageClip → numpy frame buffer → libx264 reference frames
            #     was OOM-killing the 512 MB Render free-tier worker on every
            #     4K export. ffmpeg streams frames natively and uses ~50 MB
            #     regardless of resolution. The same pad+scale+setsar filter
            #     keeps every scene at exact (W,H) so the final concat works.
            ffmpeg_bin = _get_ffmpeg()
            if vid_path:
                from moviepy import VideoFileClip
                clip = VideoFileClip(vid_path)
                if audio:
                    audio_trimmed = audio.subclipped(0, min(audio.duration, clip.duration))
                    clip = clip.with_audio(audio_trimmed)
                # Normalize to target dims so ffmpeg concat-demuxer can
                # stream-copy. Wan clips come back at 1280×720; siblings may
                # be at W×H. Silent failures here previously crashed the
                # final concat — now we PIL-rebuild as a safety net.
                try:
                    clip = clip.resized((W, H))
                except Exception:
                    try:
                        from PIL import Image as _PIL
                        fallback_path = os.path.join(tmp, f"resize_fallback_{i}.jpg")
                        _PIL.open(img_path).convert("RGB").resize(
                            (W, H), _PIL.LANCZOS).save(fallback_path, "JPEG", quality=88)
                        if clip is not None:
                            try: clip.close()
                            except Exception: pass
                        clip = ImageClip(fallback_path,
                                         duration=duration if audio is None else audio.duration)
                        if audio:
                            clip = clip.with_audio(audio)
                    except Exception:
                        pass

                write_kwargs = dict(
                    fps=TARGET_FPS, codec="libx264", audio_codec="aac",
                    threads=2, preset="ultrafast", logger=None,
                )
                if clip.audio is None:
                    # Inject silent stereo AAC so every scene MP4 has a
                    # consistent v+a layout for the concat-demuxer.
                    try:
                        from moviepy import AudioArrayClip
                        import numpy as _np
                        _sr = 44100
                        _silent = AudioArrayClip(
                            _np.zeros((int(max(0.05, clip.duration) * _sr), 2),
                                      dtype="float32"),
                            fps=_sr,
                        )
                        clip = clip.with_audio(_silent)
                    except Exception:
                        write_kwargs.pop("audio_codec", None)
                clip.write_videofile(scene_out, **write_kwargs)
                try: clip.close()
                except Exception: pass
            else:
                # Static-image scene — render via ffmpeg directly to avoid
                # MoviePy's per-frame numpy buffer (the 4K-OOM offender).
                # The pad+scale filter guarantees output is exactly W×H.
                #   -loop 1 -i image     : repeat the still
                #   -t duration          : stop after `duration` seconds
                #   -r TARGET_FPS        : output framerate
                #   -vf scale,pad,setsar : normalize to W×H, square pixels
                #   pix_fmt yuv420p      : H.264 baseline-friendly
                #   shortest+anullsrc OR audio.mp3 : audio is always present
                vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
                # Wrap ffmpeg with `nice` when available so gunicorn can
                # still serve health checks under CPU pressure. On Render
                # free tier we have ~0.1 vCPU shared — if ffmpeg saturates
                # it the external health-check probe to /login times out
                # and Render kills the worker mid-export. nice doesn't help
                # cgroup throttling, but it does help when the kernel is
                # picking which runnable thread to schedule next on a
                # shared vCPU.
                nice_bin = shutil.which("nice")
                ff_prefix = [nice_bin, "-n", "19"] if nice_bin else []
                cmd = [*ff_prefix, ffmpeg_bin, "-y", "-loglevel", "error",
                       "-threads", "1",  # single-thread so we don't pile
                                          # work onto the shared vCPU
                       "-loop", "1", "-i", img_path]
                if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                    cmd.extend(["-i", audio_path])
                else:
                    # Silent stereo source — duration controlled by -t below.
                    cmd.extend(["-f", "lavfi", "-i",
                                "anullsrc=channel_layout=stereo:sample_rate=44100"])
                cmd.extend([
                    "-vf", vf,
                    "-r", str(TARGET_FPS),
                    "-t", f"{duration:.3f}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                    # Force stereo 44.1kHz AAC so audio params match the
                    # MoviePy video-scene path. Without -ar/-ac the gTTS
                    # mono 24kHz source passes through verbatim and the
                    # concat-demuxer trips on mixed exports.
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    "-shortest", "-movflags", "+faststart",
                    scene_out,
                ])
                # Spawn via Popen + poll loop instead of subprocess.run so
                # we can periodically update the job heartbeat. The poll
                # interval also yields the GIL so gunicorn's other threads
                # get scheduled and can answer health checks while ffmpeg
                # is encoding.
                try:
                    import gc as _gc
                    _gc.collect()
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
                    heartbeat_deadline = time.time() + 600
                    while proc.poll() is None:
                        if time.time() > heartbeat_deadline:
                            proc.kill()
                            break
                        # Refresh job state every iteration so the polling
                        # client sees we're still alive even during the
                        # long single-scene encode.
                        job["elapsed_seconds"] = int(time.time() - job["start_time"])
                        time.sleep(1.5)
                    rc = proc.returncode if proc.returncode is not None else -1
                    if rc != 0:
                        # ffmpeg path failed — fall back to MoviePy (legacy
                        # path). May OOM at 4K but at least we tried.
                        clip = ImageClip(img_path, duration=duration)
                        if audio:
                            audio = audio.with_effects([
                                moviepy.afx.AudioFadeIn(0.5),
                                moviepy.afx.AudioFadeOut(1.0)])
                            clip = clip.with_audio(audio)
                        try: clip = clip.resized((W, H))
                        except Exception: pass
                        clip.write_videofile(
                            scene_out, fps=TARGET_FPS, codec="libx264",
                            audio_codec="aac", threads=2, preset="ultrafast",
                            logger=None)
                        try: clip.close()
                        except Exception: pass
                except Exception:
                    pass

            if audio:
                try: audio.close()
                except Exception: pass
            clips.append(scene_out)

        job["progress"]    = 88
        anim_tag = "_animated" if animate else ""
        job["message"]     = f"Stitching {n} scenes into final {quality.upper()} video…"
        job["scenes_done"] = n
        safe     = "".join(c for c in story.get("title", "anime") if c.isalnum() or c in " _-")[:40]
        out_name = f"{safe.strip()}_{quality}{anim_tag}_{TARGET_FPS}fps.mp4"
        out_path = os.path.join(tmp, out_name)

        encode_done = threading.Event()
        _msgs = [
            f"Stitching {quality.upper()} scenes…",
            "Concatenating with FFmpeg…",
            "Writing final audio…",
            "Merging — almost there…",
        ]

        def _progress_ticker():
            job["progress"] = 92
            idx = 0
            while not encode_done.is_set():
                job["elapsed_seconds"] = int(time.time() - job["start_time"])
                job["message"] = _msgs[idx % len(_msgs)]
                idx += 1
                time.sleep(2.5)

        ticker = threading.Thread(target=_progress_ticker, daemon=True)
        ticker.start()

        # FFmpeg concat-demuxer: takes a list file of MP4 paths, stream-copies
        # them into one output. No re-decode, no frame buffers, near-zero RAM.
        # Resolve the ffmpeg binary the same way the rest of the app does —
        # bare "ffmpeg" raises FileNotFoundError on any host where it isn't on
        # PATH (e.g. local Windows dev, where ffmpeg lives inside imageio_ffmpeg).
        ffmpeg = _get_ffmpeg()

        # For 4K output we upscale each 1080p per-scene MP4 to 3840×2160
        # ONE AT A TIME (separate ffmpeg processes), then concat-demuxer
        # the upscaled scenes with -c copy. The previous filter_complex
        # approach held decoders for every scene in parallel and pushed
        # the 512MB Render worker over the edge during stitching. Doing
        # the upscale per-scene keeps ffmpeg's peak at ~150MB regardless
        # of scene count.
        if (OUT_W, OUT_H) != (W, H):
            upscaled: list[str] = []
            for idx, p in enumerate(clips):
                up_out = os.path.join(tmp, f"upscaled_{idx:03d}.mp4")
                job["message"] = f"Upscaling scene {idx+1}/{len(clips)} to {OUT_W}×{OUT_H}…"
                vf = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
                      f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
                # Aggressive minimum-RAM x264 config — needed because the
                # Render 512MB free worker dies on default ultrafast 4K
                # encoder state (~250MB encoder RAM at 3840×2160). This
                # config disables every memory-hungry feature: no B-frames,
                # no lookahead, no CABAC, 1 ref frame, single thread.
                # Quality is acceptable for upscaled-from-1080p content
                # since we're not adding real detail anyway.
                x264_min = (
                    "no-cabac=1:no-deblock=1:partitions=none:me=dia:"
                    "subme=1:ref=1:bframes=0:scenecut=0:rc-lookahead=0:"
                    "keyint=60:min-keyint=60:trellis=0:no-mixed-refs=1:"
                    "no-weightb=1:weightp=0:8x8dct=0:sliced-threads=0"
                )
                up_res = subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-i", p,
                     "-vf", vf, "-r", str(TARGET_FPS),
                     "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                     "-x264-params", x264_min, "-threads", "1",
                     "-c:a", "copy",
                     up_out],
                    capture_output=True, timeout=900,
                )
                if up_res.returncode != 0:
                    # If upscale fails on a single scene, fall through to the
                    # filter_complex path below which handles drift. The
                    # original 1080p clip is kept so concat still has something.
                    upscaled = clips
                    break
                upscaled.append(up_out)
            clips = upscaled  # downstream concat ops use the upscaled set

        concat_list = os.path.join(tmp, "concat.txt")
        with open(concat_list, "w") as fh:
            for p in clips:
                # ffmpeg concat requires escaped single quotes for safety
                fh.write(f"file '{p.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n")
        try:
            # Now that all clips are at OUT_W×OUT_H (either natively at 1080p
            # or via the per-scene upscale loop above), -c copy works for
            # both quality tiers.
            res = subprocess.run(
                [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                 "-i", concat_list, "-c", "copy", "-movflags", "+faststart",
                 out_path],
                capture_output=True, timeout=600,
            )
            if res.returncode != 0:
                # Stream-copy fell over (usually mismatched stream params
                # across scenes, OR we deliberately skipped it to do a 4K
                # upscale). Try a same-demuxer re-encode first because it's
                # the cheapest path that still works for matching layouts —
                # but only if no upscale is required (concat demuxer can't
                # change resolution mid-stream).
                if (OUT_W, OUT_H) == (W, H):
                    res2 = subprocess.run(
                        [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                         "-i", concat_list,
                         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                         "-c:a", "aac", "-movflags", "+faststart",
                         out_path],
                        capture_output=True, timeout=900,
                    )
                else:
                    class _R: returncode = 1; stderr = b"upscale required"
                    res2 = _R()
                if res2.returncode != 0:
                    # Real "always works" path: concat FILTER (not demuxer)
                    # with per-input scale + sar + format + resample so any
                    # drift in resolution / sample rate / channel count gets
                    # normalized to the target before concat. More expensive
                    # but immune to the layout-must-match constraint that
                    # tripped both attempts above.
                    #
                    # For any input that lacks an audio stream we substitute
                    # silence of the matching duration from a global anullsrc
                    # input — [N:a:0] would otherwise fail to bind and abort
                    # the whole filtergraph.
                    def _has_audio(path: str) -> bool:
                        try:
                            r = subprocess.run([ffmpeg, "-i", path],
                                               capture_output=True, timeout=20)
                            return b"Audio:" in (r.stderr or b"")
                        except Exception:
                            return False

                    def _duration(path: str) -> float:
                        try:
                            r = subprocess.run([ffmpeg, "-i", path],
                                               capture_output=True, timeout=20)
                            txt = (r.stderr or b"").decode("utf-8", "replace")
                            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
                            if m:
                                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                        except Exception:
                            pass
                        return 5.0

                    inputs: list[str] = []
                    has_audio_per_clip: list[bool] = []
                    for p in clips:
                        inputs.extend(["-i", p])
                        has_audio_per_clip.append(_has_audio(p))

                    # Add one silent source we can slice for missing-audio scenes.
                    silent_idx = len(clips)
                    inputs.extend(["-f", "lavfi", "-i",
                                   "anullsrc=channel_layout=stereo:sample_rate=44100"])

                    parts: list[str] = []
                    for idx in range(len(clips)):
                        parts.append(
                            f"[{idx}:v:0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
                            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                            f"fps={TARGET_FPS}[v{idx}];"
                        )
                        if has_audio_per_clip[idx]:
                            parts.append(
                                f"[{idx}:a:0]aresample=44100,"
                                f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{idx}];"
                            )
                        else:
                            dur = _duration(clips[idx])
                            parts.append(
                                f"[{silent_idx}:a]atrim=duration={dur:.3f},"
                                f"asetpts=PTS-STARTPTS,"
                                f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{idx}];"
                            )
                    concat_inputs = "".join(f"[v{idx}][a{idx}]" for idx in range(len(clips)))
                    filtergraph = (
                        "".join(parts)
                        + f"{concat_inputs}concat=n={len(clips)}:v=1:a=1[v][a]"
                    )
                    subprocess.run(
                        [ffmpeg, "-y", *inputs,
                         "-filter_complex", filtergraph,
                         "-map", "[v]", "-map", "[a]",
                         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                         "-c:a", "aac", "-movflags", "+faststart",
                         out_path],
                        check=True, capture_output=True, timeout=1500,
                    )
        finally:
            encode_done.set()
            ticker.join(timeout=2)

        elapsed = time.time() - job["start_time"]
        job.update({
            "file_path": out_path, "file_name": out_name, "status": "complete",
            "progress": 100, "message": f"Your {quality.upper()} {TARGET_FPS}fps video is ready!",
            "tmp_dir": tmp, "elapsed_seconds": int(elapsed), "eta_seconds": 0,
        })
        # Write disk manifest so the download endpoint can find the file
        # even if the in-memory dict is wiped by a worker restart.
        try:
            manifest = {"file_path": out_path, "file_name": out_name}
            with open(os.path.join(EXPORT_READY_DIR, f"{job_id}.json"), "w") as mf:
                json.dump(manifest, mf)
        except Exception:
            pass
    except Exception as e:
        job.update({"status": "error", "message": str(e), "error": str(e)})
        shutil.rmtree(tmp, ignore_errors=True)


@app.route("/start-export", methods=["POST"])
@login_required
def start_export():
    tier = "admin" if session.get("is_admin") else session.get("tier", "free")
    if tier == "free":
        return jsonify({"error": "Video export requires Hunter or Monarch plan."}), 403
    data    = request.json or {}
    story   = data.get("story")
    mode    = data.get("mode", "episode")
    quality = data.get("quality", "1080p")
    animate = bool(data.get("animate", False))
    style_key  = (data.get("style") or DEFAULT_STYLE).strip()
    if style_key not in STYLES:
        style_key = DEFAULT_STYLE
    # Animation model selection. "local" = free FFmpeg zoompan (Ken Burns; not real
    # motion — what Justen called "back and forth"). "wan" = fal.ai Wan 2.5 real
    # anime motion (~$0.25/clip), fluent and the default for everyone. "kling" =
    # premium Kling 1.6 Pro (~$0.35/clip). "comfy_local" = Justen's home RTX 4070
    # Ti SUPER running Wan 2.2 5B — opt-in only, never the default, because the
    # 5B model produces visibly stiff motion compared to fal.ai's hosted Wan 2.5
    # and admin should see the same fluent output everyone else sees.
    anim_model = (data.get("anim_model") or "").strip()
    # comfy_cogvideox added 2026-05-23: same routing as comfy_local but uses
    # CogVideoX-5B-I2V GGUF Q4 instead of Wan 2.2 5B. Slower (~8-12 min/clip)
    # but higher quality. Admin-only since it ties up the local GPU longer.
    if anim_model not in ("local", "wan", "kling", "comfy_local", "comfy_cogvideox"):
        anim_model = ""  # empty → tier default below
    # Only admin can request the local-GPU paths — it's his hardware; paying users
    # get consistent latency by going through fal.ai's hosted SLA.
    if anim_model in ("comfy_local", "comfy_cogvideox") and tier != "admin":
        anim_model = "wan"
    if not anim_model:
        # Admin always prefers the home GPU when its tunnel URL is set —
        # that's the whole point of the local rig: free unlimited animation.
        # Paying tiers go to fal.ai Wan for consistent latency. The 60fps
        # motion-interpolation pass applies to comfy_local output too, so
        # the home GPU's 5B clips get smoothed before they reach the user.
        if tier == "admin" and COMFY_LOCAL_URL:
            anim_model = "comfy_local"
        elif FAL_KEY and tier in ("admin", "monarch", "hunter"):
            anim_model = "wan"
        else:
            anim_model = "local"
    # Tier-gate the premium paid models
    if anim_model == "kling" and tier not in ("monarch", "admin"):
        anim_model = "wan"
    if anim_model in ("wan", "kling") and not FAL_KEY:
        anim_model = "local"   # no key → free fallback
    # Admin override: if the frontend defaulted to "wan"/"kling" but the home
    # GPU tunnel is up, route to local instead — admin shouldn't burn fal.ai
    # credits when free local hardware is available. Defaults to Wan
    # (comfy_local) not CogVideoX since Wan is 5× faster — admin opts into
    # CogVideoX explicitly when they want hero-scene quality.
    if tier == "admin" and COMFY_LOCAL_URL and anim_model in ("wan", "kling"):
        anim_model = "comfy_local"
    if quality == "4k" and tier not in ("monarch", "admin"):
        quality = "1080p"
    # Hard cap when the host can't deliver 4K. Render's 512MB free-tier
    # worker is OOM-killed during any 4K libx264 encode no matter how
    # aggressively we tune x264 (the encoder needs ~150MB at 3840×2160
    # and Python+Flask+MoviePy already use ~300MB of the 512MB cap).
    # Setting MAX_RENDER_QUALITY=1080p in the deploy env vars silently
    # downgrades 4K to 1080p so users get a clean download instead of a
    # cryptic "not_found" after the worker dies. Unset (or set to "4k")
    # on Starter+ plans where the worker has enough RAM.
    _max_quality = os.environ.get("MAX_RENDER_QUALITY", "").strip().lower()
    if _max_quality == "1080p" and quality == "4k":
        quality = "1080p"
    if animate and not FAL_KEY and anim_model not in ("local", "comfy_local", "comfy_cogvideox"):
        # Animate requested with paid model but no FAL key — degrade gracefully.
        # Local-GPU paths are exempt because they use Justen's GPU, not fal.ai.
        anim_model = "local"
    if not story or not story.get("scenes"):
        return jsonify({"error": "No story provided"}), 400
    job_id = secrets.token_urlsafe(16)
    export_jobs[job_id] = {
        "status": "running", "progress": 5, "message": "Starting...",
        "file_path": None, "file_name": None, "error": None, "tmp_dir": None,
        "start_time": time.time(), "scenes_total": len(story.get("scenes", [])),
        "scenes_done": 0, "eta_seconds": None, "elapsed_seconds": 0,
        "current_img_b64": None, "current_scene_title": "", "quality": quality,
        "animate": animate, "style": style_key, "anim_model": anim_model,
    }
    threading.Thread(
        target=do_export,
        args=(job_id, story, mode, quality, animate, style_key, anim_model),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "quality": quality, "animate": animate,
                    "style": style_key, "anim_model": anim_model})


@app.route("/export-status/<job_id>")
@login_required
def export_status(job_id):
    """
    JSON status snapshot — browser polls this every ~1.5s rather than holding
    a long-lived SSE. Plays much better with Render's edge proxy + browser tab
    lifecycle (background tabs were dropping SSE mid-export).

    Returns the full job dict. Browser stops polling when status == complete |
    error | not_found.
    """
    job = export_jobs.get(job_id, {"status": "not_found"})
    return jsonify(job)


@app.route("/export-status-sse/<job_id>")
@login_required
def export_status_sse(job_id):
    """Legacy SSE endpoint kept around in case anything still references it.
    The polling endpoint above is the new path."""
    def stream():
        while True:
            job = export_jobs.get(job_id,{"status":"not_found"})
            yield f"retry: 2000\ndata: {json.dumps(job)}\n\n"
            if job["status"] in ("complete","error","not_found"): break
            time.sleep(1.5)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/download/<job_id>")
@login_required
def download(job_id):
    job = export_jobs.get(job_id)
    file_path = None
    file_name = "anime.mp4"
    tmp_dir = None

    if job and job.get("status") == "complete" and job.get("file_path"):
        file_path = job.get("file_path")
        file_name = job.get("file_name", file_name)
        tmp_dir = job.get("tmp_dir")
    else:
        # Fallback: check disk manifest (survives in-memory wipe from deploys)
        manifest_path = os.path.join(EXPORT_READY_DIR, f"{job_id}.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path) as mf:
                    m = json.load(mf)
                fp = m.get("file_path", "")
                if fp and os.path.exists(fp):
                    file_path = fp
                    file_name = m.get("file_name", file_name)
            except Exception:
                pass

    if file_path and os.path.exists(file_path):
        @after_this_request
        def cleanup(response):
            try:
                if tmp_dir:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                export_jobs.pop(job_id, None)
                mpath = os.path.join(EXPORT_READY_DIR, f"{job_id}.json")
                if os.path.exists(mpath):
                    os.remove(mpath)
            except Exception:
                pass
            return response
        return send_file(file_path, as_attachment=True,
                         download_name=file_name, mimetype="video/mp4")
    return "File not ready.", 404


# ── Admin panel ────────────────────────────────────────────────────────────────

def admin_required(f):
    @functools.wraps(f)
    def w(*a, **kw):
        if not session.get("is_admin"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return w


@app.route("/admin")
@admin_required
def admin_panel():
    with get_db() as db:
        users = db.execute(
            "SELECT id, email, tier, episodes_used, period_month, paypal_subscription_id, created_at "
            "FROM users ORDER BY created_at DESC"
        ).fetchall()
        stats = {
            "total":   db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "free":    db.execute("SELECT COUNT(*) FROM users WHERE tier='free'").fetchone()[0],
            "hunter":  db.execute("SELECT COUNT(*) FROM users WHERE tier='hunter'").fetchone()[0],
            "monarch": db.execute("SELECT COUNT(*) FROM users WHERE tier='monarch'").fetchone()[0],
            "projects": db.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "seasons":  db.execute("SELECT COUNT(*) FROM seasons").fetchone()[0],
        }
    pp_ok  = bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET and PAYPAL_PLAN_HUNTER and PAYPAL_PLAN_MONARCH)
    fal_ok = bool(FAL_KEY)
    return render_template("admin.html",
                           users=[dict(u) for u in users],
                           stats=stats,
                           pp_ok=pp_ok,
                           pp_mode=PAYPAL_MODE,
                           fal_ok=fal_ok,
                           email=session.get("email", ""))


@app.route("/admin/set-tier", methods=["POST"])
@admin_required
def admin_set_tier():
    data = request.json or {}
    uid  = data.get("user_id")
    tier = data.get("tier")
    if not uid or tier not in ("free", "hunter", "monarch"):
        return jsonify({"error": "Invalid input"}), 400
    with get_db() as db:
        db.execute("UPDATE users SET tier=? WHERE id=?", (tier, uid))
        db.commit()
    return jsonify({"success": True})


@app.route("/admin/delete-user/<int:uid>", methods=["DELETE"])
@admin_required
def admin_delete_user(uid):
    with get_db() as db:
        db.execute("DELETE FROM projects WHERE user_id=?", (uid,))
        db.execute("DELETE FROM seasons WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()
    return jsonify({"success": True})


# Schema bootstrap. Runs at module import so it works under both `python app.py`
# (local dev) and `gunicorn app:app` (Render / Fly / any WSGI host) — gunicorn never
# triggers __main__ so we can't rely on it. init_db is idempotent (CREATE TABLE IF
# NOT EXISTS) so re-running is safe.
init_db()


if __name__ == "__main__":
    # Flask's `debug=True` enables the Werkzeug debugger, which gives anyone hitting
    # an error page a Python shell on the server (RCE). Opt-in via env var only —
    # never hard-coded so a pasted-into-prod app.py can't accidentally expose it.
    debug_enabled = os.environ.get("FLASK_DEBUG", "0") == "1"
    # Honor $PORT in case someone runs `python app.py` on a managed host;
    # defaults to 5000 for local dev.
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=debug_enabled, host="0.0.0.0", port=port, threaded=True)
