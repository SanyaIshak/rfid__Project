# ==================================================================
# RFID Access Control — сервер (Python / Flask)
#
# Это точный порт server.js (Node/Express) на Python.
# Вся логика, все эндпоинты, все правила поведения — без изменений.
# Визуальная часть (index.html, admin.html) НЕ менялась вообще.
#
# 1) "При открытой регистрации скан карты сразу логинит меня в систему"
#    -> Скан на reader=2 при открытой регистрации только помечает
#       pending_uid ("карта ждёт форму"). Пользователь создаётся
#       только через POST /confirm-registration, и is_inside при
#       этом НЕ включается. Чтобы войти — нужно отдельно сканировать
#       карту на считывателе входа (reader=1).
#
# 2) "Не могу выйти из системы"
#    -> exit (reader=2 при ЗАКРЫТОЙ регистрации) БЕЗУСЛОВНО
#       ставит is_inside = false и пишет событие exit.
# ==================================================================

import json
import os
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DB_FILE = os.path.join(BASE_DIR, "db.json")

app = Flask(__name__, static_folder=None)

# ---------------------- Состояние в памяти ----------------------
state = {
    "registration_open": False,
    "pending_uid": None,
    "users": {},   # uid -> { uid, name, surname, registered, is_inside, created_at }
    "events": []   # [{ timestamp, action, uid }], новые — в начале списка
}


# ---------------------- Персистентность (best-effort) ----------------------
def load_state():
    global state
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            state.update(parsed)
            print("✅ Состояние загружено из db.json")
    except Exception as e:
        print(f"⚠️ Не удалось загрузить db.json: {e}")


def save_state():
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить db.json: {e}")


load_state()


# ---------------------- Вспомогательные функции ----------------------
def now_str():
    # Аналог toLocaleString('ru-RU', {day/month/year 2-digit, hour/min/sec 2-digit})
    return datetime.now().strftime("%d.%m.%y, %H:%M:%S")


def push_event(action, uid):
    state["events"].insert(0, {"timestamp": now_str(), "action": action, "uid": uid})
    # ограничим ленту, чтобы файл не рос бесконечно
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
    state["registration_open"] = not state["registration_open"]
    # при закрытии регистрации сбрасываем "зависшую" карту, если её не успели подтвердить
    if not state["registration_open"]:
        state["pending_uid"] = None
    save_state()
    return jsonify({"status": "ok", "registration_open": state["registration_open"]})


@app.post("/confirm-registration")
def confirm_registration():
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    surname = body.get("surname")

    if not state["registration_open"]:
        return jsonify({"status": "error", "message": "Регистрация закрыта"}), 400
    if not state["pending_uid"]:
        return jsonify({"status": "error", "message": "Сначала приложите карту к считывателю №3"}), 400
    if not name or not surname:
        return jsonify({"status": "error", "message": "Заполните имя и фамилию"}), 400

    uid = state["pending_uid"]

    state["users"][uid] = {
        "uid": uid,
        "name": name.strip(),
        "surname": surname.strip(),
        "registered": True,
        "is_inside": False,  # ВАЖНО: регистрация НЕ = вход. Входить нужно отдельно через reader 1.
        "created_at": datetime.now().isoformat(),
    }

    push_event("registration_confirm", uid)
    state["pending_uid"] = None  # карта больше не "висит" в ожидании

    save_state()
    return jsonify({"status": "ok", "uid": uid})


# ==================================================================
#                    ЭНДПОИНТ ДЛЯ ESP32 (/rfid)
# ==================================================================
# reader=1 -> физический считыватель ВХОДА  (rfid2 в прошивке)
# reader=2 -> физический считыватель ВЫХОДА / РЕГИСТРАЦИИ (rfid3 в прошивке,
#             в публичном HTML он называется "считыватель №3")
#
# ИСПРАВЛЕНО (важно): раньше скан регистрации обрабатывался ТОЛЬКО если
# reader == "2". Если по факту работал/использовался только один физический
# считыватель (reader=1), то при открытой регистрации сервер пытался
# обработать скан как ВХОД, получал "card not registered" и просто молча
# отбрасывал карту — карта "проскакивала" мимо формы регистрации, и в базе
# не оставалось никакого следа (ни pending_uid, ни события).
#
# Теперь: пока регистрация ОТКРЫТА — сканирование НА ЛЮБОМ считывателе
# (reader=1 ИЛИ reader=2) всегда трактуется как скан для регистрации.
# Это не ломает нормальную работу: как только администратор закрывает
# регистрацию, оба считывателя возвращаются к своей обычной роли —
# reader=1 = вход, reader=2 = выход.
@app.get("/rfid")
def rfid():
    reader = request.args.get("reader", "")
    uid = request.args.get("uid", "").upper()

    if not uid:
        return "ERROR: no uid", 400

    if reader not in ("1", "2"):
        return "ERROR: unknown reader", 400

    # ---------- Регистрация ОТКРЫТА: любой считыватель = скан для регистрации ----------
    if state["registration_open"]:
        state["pending_uid"] = uid
        push_event("registration_scan", uid)
        save_state()
        print(f"📇 Скан для регистрации (reader={reader}): {uid}")
        return "OK: registration pending, fill the form"

    # ---------- Регистрация ЗАКРЫТА: обычная работа считывателей ----------

    # Считыватель №1 — ВХОД
    if reader == "1":
        user = state["users"].get(uid)
        if not user:
            return "ERROR: card not registered", 404

        user["is_inside"] = True
        push_event("entry", uid)
        save_state()
        print(f"➡️ ENTRY: {uid} ({user['name']} {user['surname']})")
        return "OK: entry"

    # Считыватель №2 — ВЫХОД
    user = state["users"].get(uid)
    if not user:
        return "ERROR: card not registered", 404

    # безусловный выход, без "залипающего" toggle
    user["is_inside"] = False
    push_event("exit", uid)
    save_state()
    print(f"⬅️ EXIT: {uid} ({user['name']} {user['surname']})")
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