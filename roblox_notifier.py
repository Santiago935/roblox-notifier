#!/usr/bin/env python3
"""
Avisa por Gmail cuando usuarios específicos de Roblox pasan a estar conectados.
 
Uso:
  python roblox_notifier.py          # una sola revisión (ideal para GitHub Actions / cron)
  python roblox_notifier.py --loop   # revisa cada POLL_SECONDS (ideal para una VM / Raspberry)
 
Variables de entorno necesarias:
  GMAIL_USER          tu dirección de Gmail (remitente)
  GMAIL_APP_PASSWORD  contraseña de aplicación de Google (requiere verificación en 2 pasos)
  MAIL_TO             (opcional) destinatario; por defecto GMAIL_USER
  ROBLOX_COOKIE       (opcional, NO recomendado) ver notas en el README / respuesta
"""
import json
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
 
import requests
 
# ----------------------------------------------------------------------------
# CONFIGURACIÓN (usuarios hardcodeados: {userId: "nombre para el correo"})
# El userId lo ves en la URL del perfil: roblox.com/users/<ID>/profile
# ----------------------------------------------------------------------------
USERS = {
    138778630: "McKyami",
    858250928: "AnaB_128",
    377176745: "Butter",
    135600010: "Gamer",
}
POLL_SECONDS = 300  # 5 min. No bajes de 60 s: es una API con límite de peticiones.
STATE_FILE = Path(__file__).with_name("state.json")
 
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"
STATUS = {0: "Desconectado", 1: "En línea", 2: "Jugando", 3: "En Roblox Studio"}
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "personal-presence-notifier/1.0 (uso personal, 1 consulta cada 5 min)",
}
 
 
def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
 
 
# ----------------------------------------------------------------------------
# Roblox
# ----------------------------------------------------------------------------
def fetch_presence(user_ids: list[int]) -> dict[int, dict]:
    """Una sola petición por ciclo con TODOS los usuarios (la API admite hasta 50)."""
    headers = dict(HEADERS)
    cookie = os.environ.get("ROBLOX_COOKIE")
    cookies = {".ROBLOSECURITY": cookie} if cookie else None
    body = {"userIds": user_ids}
 
    for attempt in range(4):
        r = requests.post(PRESENCE_URL, json=body, headers=headers, cookies=cookies, timeout=20)
 
        if r.status_code == 200:
            return {p["userId"]: p for p in r.json().get("userPresences", [])}
 
        if r.status_code == 403 and "x-csrf-token" in r.headers:
            headers["x-csrf-token"] = r.headers["x-csrf-token"]  # flujo normal de Roblox
            continue
 
        if r.status_code == 429:  # límite de peticiones: respetar y esperar
            wait = int(r.headers.get("Retry-After", 2 ** (attempt + 4)))
            log(f"429 Too Many Requests, esperando {wait}s")
            time.sleep(wait)
            continue
 
        if r.status_code in (401, 403):
            raise RuntimeError(
                f"Roblox respondió {r.status_code}: este endpoint puede exigir autenticación. "
                f"Detalle: {r.text[:200]}"
            )
        r.raise_for_status()
 
    raise RuntimeError("No se pudo consultar Roblox tras varios intentos")
 
 
# ----------------------------------------------------------------------------
# Gmail
# ----------------------------------------------------------------------------
def send_email(subject: str, body: str) -> None:
    user = os.environ["GMAIL_USER"].strip()
    password = "".join(os.environ["GMAIL_APP_PASSWORD"].split())  # quita espacios/saltos de línea
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = (os.environ.get("MAIL_TO") or user).strip()
    msg["Subject"] = subject
    msg.set_content(body)
 
    # Intento 1: SSL directo (465). Intento 2: STARTTLS (587), por si Google corta el primero.
    last_error: Exception | None = None
    for port in (465, 587):
        try:
            if port == 465:
                smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
            else:
                smtp = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
                smtp.starttls()
            with smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
            return
        except (smtplib.SMTPServerDisconnected, OSError) as e:
            log(f"Gmail puerto {port} falló: {e}")
            last_error = e
    raise RuntimeError(f"No se pudo enviar el correo por Gmail: {last_error}")
 
 
# ----------------------------------------------------------------------------
# Lógica
# ----------------------------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
 
 
def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
 
 
def run_once() -> None:
    presences = fetch_presence(list(USERS))
    state = load_state()
    new_state = dict(state)
 
    for uid, name in USERS.items():
        p = presences.get(uid)
        if p is None:
            log(f"{name} ({uid}): sin datos en la respuesta de Roblox")
            continue
 
        ptype = p.get("userPresenceType", 0)
        online = ptype != 0
        was_online = bool(state.get(str(uid), False))
        last_online = p.get("lastOnline")
 
        # Diagnóstico: una línea por usuario en cada corrida
        log(f"{name} ({uid}): {STATUS.get(ptype, ptype)} | lastOnline={last_online}")
 
        new_state[str(uid)] = online
 
        if online and not was_online:  # desconectado -> conectado
            estado = STATUS.get(ptype, "Conectado")
            lugar = p.get("lastLocation") or "—"
            log(f"{name} se conectó ({estado})")
            send_email(
                subject=f"🟢 {name} está conectado en Roblox",
                body=(
                    f"{name} acaba de conectarse.\n"
                    f"Estado: {estado}\nUbicación: {lugar}\n"
                    f"Perfil: https://www.roblox.com/users/{uid}/profile\n"
                ),
            )
        elif not online and was_online:
            log(f"{name} se desconectó")
 
    if new_state != state:
        save_state(new_state)
 
 
def main() -> None:
    if "--loop" not in sys.argv:
        run_once()
        return
    backoff = POLL_SECONDS
    while True:
        try:
            run_once()
            backoff = POLL_SECONDS
        except Exception as e:  # no matar el proceso 24/7 por un error puntual
            log(f"Error: {e}")
            backoff = min(backoff * 2, 3600)
        time.sleep(backoff)
 
 
if __name__ == "__main__":
    main()
