import os
import re
import csv
import io
import hmac
import sqlite3
import secrets
from datetime import datetime, timezone

from flask import Flask, render_template, jsonify, request, Response
import paho.mqtt.client as mqtt

app = Flask(__name__)

# --- EMQX Cloud Configuration (set these as environment variables on Render) ---
MQTT_BROKER = os.environ.get("MQTT_BROKER", "n6966951.ala.us-east-1.emqxsl.com")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 8883))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC = "giftbox/trigger"

# --- Storage / admin configuration ---
# On Render, point DB_PATH at a persistent disk (e.g. /var/data/giftbox.db),
# otherwise the file is wiped on every deploy/restart.
DB_PATH = os.environ.get("DB_PATH", "giftbox.db")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")  # required to download the export


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
def db():
    # isolation_level=None -> we control transactions explicitly (BEGIN IMMEDIATE)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                surname     TEXT NOT NULL,
                email       TEXT,
                phone       TEXT,
                email_norm  TEXT,
                phone_norm  TEXT,
                token       TEXT UNIQUE,
                claimed     INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                claimed_at  TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email "
            "ON users(email_norm) WHERE email_norm IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_phone "
            "ON users(phone_norm) WHERE phone_norm IS NOT NULL"
        )
    finally:
        conn.close()


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# Validation / normalisation
# ----------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def norm_email(value):
    value = (value or "").strip().lower()
    return value or None


def norm_phone(value):
    """Digits only; keep the last 10 digits so +90 532..., 0532... and 532...
    all count as the same number."""
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return None
    return digits[-10:] if len(digits) > 10 else digits


# ----------------------------------------------------------------------------
# MQTT
# ----------------------------------------------------------------------------
def get_mqtt_client(client_id):
    """Compatible with both paho-mqtt v1.x and v2.x"""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        return mqtt.Client(client_id=client_id)


def send_open_command():
    """Connects, transmits the payload to EMQX, and cleanly disconnects."""
    client_id = f"RenderKiosk-{os.urandom(4).hex()}"
    client = get_mqtt_client(client_id)

    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.tls_set()
    client.tls_insecure_set(True)

    client.connect(MQTT_BROKER, MQTT_PORT, 10)
    client.loop_start()

    msg_info = client.publish(MQTT_TOPIC, "open", qos=1)
    msg_info.wait_for_publish(timeout=5.0)
    delivered = msg_info.is_published()

    client.loop_stop()
    client.disconnect()

    if not delivered:
        raise RuntimeError("Broker did not confirm delivery")


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/register", methods=["POST"])
def register():
    """Called when the user clicks the Instagram button.
    - Already claimed (same email OR phone)  -> not eligible
    - Otherwise store the user and return a one-time claim token."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    surname = (data.get("surname") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()

    email_n = norm_email(email)
    phone_n = norm_phone(phone)

    # Basic server-side validation (never trust the browser)
    if not name or not surname or len(name) > 80 or len(surname) > 80:
        return jsonify({"eligible": False, "message": "Please enter your name and surname."}), 400
    if not email_n and not phone_n:
        return jsonify({"eligible": False, "message": "Please enter an email or a phone number."}), 400
    if email_n and not EMAIL_RE.match(email_n):
        return jsonify({"eligible": False, "message": "That email address doesn't look right."}), 400
    if phone_n and not (7 <= len(phone_n) <= 15):
        return jsonify({"eligible": False, "message": "That phone number doesn't look right."}), 400

    token = secrets.token_urlsafe(24)
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")  # serialises concurrent registrations

        rows = conn.execute(
            """
            SELECT id, claimed FROM users
            WHERE (email_norm IS NOT NULL AND email_norm = ?)
               OR (phone_norm IS NOT NULL AND phone_norm = ?)
            """,
            (email_n, phone_n),
        ).fetchall()

        if any(r["claimed"] for r in rows):
            conn.execute("ROLLBACK")
            return jsonify({
                "eligible": False,
                "message": "This email or phone number has already claimed a gift box.",
            }), 200

        if rows:
            # Registered earlier but never claimed (e.g. connection dropped):
            # let them continue with a fresh token.
            conn.execute("UPDATE users SET token = ? WHERE id = ?", (token, rows[0]["id"]))
        else:
            conn.execute(
                """
                INSERT INTO users
                    (name, surname, email, phone, email_norm, phone_norm, token, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, surname, email or None, phone or None, email_n, phone_n, token, now_iso()),
            )
        conn.execute("COMMIT")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print(f"Register error: {e}")
        return jsonify({"eligible": False, "message": "Something went wrong. Please try again."}), 500
    finally:
        conn.close()

    return jsonify({"eligible": True, "token": token}), 200


@app.route("/claim", methods=["POST"])
def claim_box():
    """The claim is enforced here on the server, so a disabled button in the
    browser can't be bypassed."""
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"status": "error", "message": "Please complete the steps first."}), 403

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id, claimed FROM users WHERE token = ?", (token,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return jsonify({"status": "error", "message": "Invalid or expired claim."}), 403
        if row["claimed"]:
            conn.execute("ROLLBACK")
            return jsonify({"status": "error", "message": "This gift box was already claimed."}), 409
        conn.execute(
            "UPDATE users SET claimed = 1, claimed_at = ? WHERE id = ?",
            (now_iso(), row["id"]),
        )
        conn.execute("COMMIT")
        user_id = row["id"]
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print(f"Claim DB error: {e}")
        return jsonify({"status": "error", "message": "Something went wrong."}), 500
    finally:
        conn.close()

    try:
        send_open_command()
    except Exception as e:
        # Machine didn't open -> give the claim back so they can retry
        print(f"MQTT Publish Error: {e}")
        conn = db()
        try:
            conn.execute("UPDATE users SET claimed = 0, claimed_at = NULL WHERE id = ?", (user_id,))
        finally:
            conn.close()
        return jsonify({"status": "error", "message": "Could not reach the machine."}), 500

    return jsonify({"status": "success", "message": "Gift box triggered!"}), 200


def csv_safe(value):
    """Stop spreadsheet apps from running user-typed text as a formula."""
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@app.route("/admin/export")
def export_users():
    """Download all users as a CSV (opens in Excel / Google Sheets).
    Usage: /admin/export?key=YOUR_ADMIN_KEY   (or send an X-Admin-Key header)"""
    supplied = request.args.get("key") or request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY or not hmac.compare_digest(supplied, ADMIN_KEY):
        return "Not found", 404

    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, name, surname, email, phone, claimed, created_at, claimed_at "
            "FROM users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Name", "Surname", "Email", "Phone", "Claimed", "Registered (UTC)", "Claimed (UTC)"])
    for r in rows:
        writer.writerow([
            r["id"],
            csv_safe(r["name"]),
            csv_safe(r["surname"]),
            csv_safe(r["email"] or ""),
            r["phone"] or "",
            "Yes" if r["claimed"] else "No",
            r["created_at"],
            r["claimed_at"] or "",
        ])

    filename = f"giftbox_users_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    # utf-8-sig adds a BOM so Excel shows non-English characters correctly
    return Response(
        "\ufeff" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
