# ==================================================================
# RFID Access Control — сервер (Python / Flask)
# Исправленная версия:
# 1) Вход только если снаружи, выход только если внутри.
#    Повторный вход / выход без входа отклоняется кодом 409.
# 2) Антиспам: повторный скан той же карты игнорируется 3 сек.
# 3) Добавлен POST /reset-all для полной очистки (админка).
# ==================================================================

import json
import os
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DB_FILE = os.path.join(BASE_DIR, "db.json")

app = Flask(__name__, static_folder=None)
db_lock = threading.Lock()

# Антиспам: (reader, uid) -> timestamp последнего принятого скана
_last_scan = {}

SCAN_COOLDOWN_SEC = 3.0

# ---------------------- Состояние в памяти ----------------------
state = {
    "registration_open": False,
    "pending_uid": None,
    "users": {},   # uid -> { uid, name, surname, registered, is_inside, created_at }
    "events": []   # [{ timestamp, action, uid }], новые — в начале списка
}


# ---------------------- Персистентность (best-effort) ----------------------
def load_state():
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            # аккуратно мержим, чтобы не потерять ключи при старом db.json
            for k in ("registration_open", "pending_uid", "users", "events"):
                if k in parsed:
                    state[k] = parsed[k]
            print("OK: state loaded from db.json")
    except Exception as e:
        print(f"WARN: load db.json failed: {e}")


def save_state():
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"WARN: save db.json failed: {e}")


load_state()


# ---------------------- Вспомогательные функции ----------------------
def now_str():
    return datetime.now().strftime("%d.%m.%y, %H:%M:%S")


def push_event(action, uid, reader=None):
    state["events"].insert(0, {"timestamp": now_str(), "action": action, "uid": uid, "reader": reader})
    if len(state["events"]) > 500:
        del state["events"][500:]


def get_stats():
    users_arr = list(state["users"].values())
    return {
        "total_events": len(state["events"]),
        "inside_count": sum(1 for u in users_arr if u.get("is_inside")),
        "entries": sum(1 for e in state["events"] if e["action"] == "entry"),
        "exits": sum(1 for e in state["events"] if e["action"] == "exit"),
    }


def is_duplicate_scan(reader, uid):
    """ESP32 шлёт повтор пока карта лежит. Режем повторы по кулдауну."""
    key = (reader, uid)
    now = time.monotonic()
    last = _last_scan.get(key, 0)
    if now - last < SCAN_COOLDOWN_SEC:
        return True
    _last_scan[key] = now
    return False


# ==================================================================
#                          ПУБЛИЧНОЕ API
# ==================================================================

@app.get("/api/registration-status")
def registration_status():
    return jsonify({
        "registration_open": state["registration_open"],
        "pending_uid": state["pending_uid"],
    })


@app.get("/api/public-data")
def public_data():
    last = state["events"][0] if state["events"] else None
    return jsonify({
        "inside_count": get_stats()["inside_count"],
        "last_event_time": last["timestamp"] if last else None,
        "last_event_action": last["action"] if last else None,
        "last_event_uid": last["uid"] if last else None,
        "last_event_reader": last.get("reader") if last else None,
    })


@app.get("/api/get-users")
def get_users():
    return jsonify({
        "stats": get_stats(),
        "events": state["events"],
        "users": list(state["users"].values()),
    })


@app.post("/toggle-registration")
def toggle_registration():
    with db_lock:
        state["registration_open"] = not state["registration_open"]
        if not state["registration_open"]:
            state["pending_uid"] = None
        save_state()
    return jsonify({"status": "ok", "registration_open": state["registration_open"]})


@app.post("/confirm-registration")
def confirm_registration():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    surname = (body.get("surname") or "").strip()

    with db_lock:
        if not state["registration_open"]:
            return jsonify({"status": "error", "message": "Регистрация закрыта"}), 400
        if not state["pending_uid"]:
            return jsonify({"status": "error", "message": "Сначала приложите карту к считывателю"}), 400
        if not name or not surname:
            return jsonify({"status": "error", "message": "Заполните имя и фамилию"}), 400

        uid = state["pending_uid"]
        state["users"][uid] = {
            "uid": uid,
            "name": name,
            "surname": surname,
            "registered": True,
            "is_inside": False,  # регистрация НЕ = вход
            "created_at": datetime.now().isoformat(),
        }
        push_event("registration_confirm", uid, None)
        state["pending_uid"] = None
        save_state()
    return jsonify({"status": "ok", "uid": uid})


@app.post("/reset-all")
def reset_all():
    """Полная очистка: пользователи, события, pending, регистрация закрывается."""
    with db_lock:
        state["users"] = {}
        state["events"] = []
        state["pending_uid"] = None
        state["registration_open"] = False
        _last_scan.clear()
        save_state()
        print("RESET-ALL: all data cleared via admin")
    return jsonify({"status": "ok", "message": "Всё очищено"})


# ==================================================================
#                    ЭНДПОИНТ ДЛЯ ESP32 (/rfid)
# ==================================================================
# reader=1 -> ВХОД, reader=2 -> ВЫХОД (или скан регистрации если открыта)
@app.get("/rfid")
def rfid():
    reader = request.args.get("reader", "")
    uid = request.args.get("uid", "").upper()

    if not uid:
        return "ERROR: no uid", 400
    if reader not in ("1", "2"):
        return "ERROR: unknown reader", 400

    with db_lock:
        # ---------- Регистрация ОТКРЫТА: любой считыватель = скан ----------
        if state["registration_open"]:
            # антиспам: не плодим registration_scan пока карту держат
            key = ("reg", uid)
            now = time.monotonic()
            if state["pending_uid"] == uid and (now - _last_scan.get(key, 0) < SCAN_COOLDOWN_SEC):
                return "OK: registration pending, fill the form"
            _last_scan[key] = now
            state["pending_uid"] = uid
            push_event("registration_scan", uid, reader)
            save_state()
            print(f"REG-SCAN (reader={reader}): {uid}")
            return "OK: registration pending, fill the form"

        # ---------- Регистрация ЗАКРЫТА ----------
        if is_duplicate_scan(reader, uid):
            return "OK: duplicate ignored"

        # Считыватель №1 — ВХОД: только если снаружи.
        # СТРОГО: reader=1 никогда не делает выход.
        if reader == "1":
            user = state["users"].get(uid)
            if not user:
                return "ERROR: card not registered", 404
            if user.get("is_inside"):
                push_event("entry_denied", uid, reader)
                save_state()
                print(f"ENTRY DENIED (already inside): {uid}")
                return "DENY_ALREADY_INSIDE", 409
            user["is_inside"] = True
            push_event("entry", uid, reader)
            save_state()
            print(f"ENTRY: {uid} ({user['name']} {user['surname']})")
            return "OK: entry"

        # Считыватель №2 — ВЫХОД: только если внутри.
        # СТРОГО: reader=2 никогда не делает вход.
        user = state["users"].get(uid)
        if not user:
            return "ERROR: card not registered", 404
        if not user.get("is_inside"):
            push_event("exit_denied", uid, reader)
            save_state()
            print(f"EXIT DENIED (not inside): {uid}")
            return "DENY_NOT_INSIDE", 409
        user["is_inside"] = False
        push_event("exit", uid, reader)
        save_state()
        print(f"EXIT: {uid} ({user['name']} {user['surname']})")
        return "OK: exit"


# ==================================================================
#                    СТАТИКА (index.html, admin.html, ...)
# ==================================================================
@app.get("/")
def serve_index():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/admin")
def serve_admin():
    return send_from_directory(PUBLIC_DIR, "admin.html")


@app.get("/<path:filename>")
def serve_static(filename):
    return send_from_directory(PUBLIC_DIR, filename)


# ==================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
