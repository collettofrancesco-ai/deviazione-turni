#!/usr/bin/env python3
"""Apply the weekly MacroDroid shift plan shortly before each shift starts."""
import json
import re
import sys
import time as time_module
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PLAN_FILE = ROOT / "turni-settimanali.json"
STATE_FILE = ROOT / "automation-state.json"
INDEX_FILE = ROOT / "index.html"
DEVICE_ID = "14510559-534a-4512-9e6d-8af51cc129f8"
SET_URL = f"https://trigger.macrodroid.com/{DEVICE_ID}/turni"
STATUS_URL = f"https://ask.macrodroid.com/{DEVICE_ID}/stato_turni"
EARLY_WINDOW = timedelta(minutes=90)
LATE_WINDOW = timedelta(minutes=4)
CATCHUP_WINDOW = timedelta(minutes=45)
DEVIA_URL = f"https://trigger.macrodroid.com/{DEVICE_ID}/devia"

# These keys and start times mirror FASCE in index.html.
SHIFT_RULES = {
    "mattina": (range(0, 5), time(9, 0), "MATTINA"),
    "pomeriggio": (range(0, 5), time(15, 0), "POMERIGGIO"),
    "notte": (range(0, 5), time(22, 0), "NOTTE"),
    "weekend": ((5,), time(8, 0), "WE_MATTINA"),
    "we_pom": ((5,), time(14, 0), "WE_POMERIGGIO"),
    "we_notte": ((5,), time(20, 0), "WE_NOTTE"),
    "dom_mattina": ((6,), time(8, 0), "DOM_MATTINA"),
    "dom_pom": ((6,), time(16, 0), "DOM_POMERIGGIO"),
    # The Sunday-night variable starts at 00:00 on Monday.
    "dom_notte": ((0,), time(0, 0), "DOM_NOTTE"),
}
NAME_PATTERN = re.compile(r'^\s*"([^"]+)"\s*:\s*("(?:[^"\\]|\\.)*")\s*,?\s*$')

def annotate(level, message):
    safe = str(message).replace("\n", " ").replace("\r", " ")
    print(f"::{level}::{safe}")

def load_people():
    source = INDEX_FILE.read_text(encoding="utf-8")
    match = re.search(r"const\s+PERSONE\s*=\s*\{([\s\S]*?)\};", source)
    if not match:
        raise ValueError("Dizionario PERSONE non trovato in index.html.")
    people = {}
    for line in match.group(1).splitlines():
        found = NAME_PATTERN.match(line)
        if found:
            people[found.group(1)] = json.loads(found.group(2))
    if not people:
        raise ValueError("Dizionario PERSONE vuoto o non leggibile.")
    return people

def load_fasce():
    source = INDEX_FILE.read_text(encoding="utf-8")
    match = re.search(r"const\s+FASCE\s*=\s*\[([\s\S]*?)\];", source)
    if not match:
        raise ValueError("Array FASCE non trovato in index.html.")
    return set(re.findall(r'\bkey\s*:\s*"([^"]+)"', match.group(1)))

def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))

def save_state(state):
    state["updated_at"] = datetime.now(ZoneInfo("Europe/Rome")).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def request_text(url, timeout=15):
    request = Request(url, headers={"User-Agent": "deviazione-turni-scheduler/1.0", "Cache-Control": "no-cache"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return exc.code, ""
    except URLError:
        return 0, ""

def parse_status(text):
    result = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.strip().upper()] = value.strip()
    return result

def record_for(entry, field, status, message=None, sent_at=None):
    out = {
        "data": entry["data"],
        "fascia": entry["fascia"],
        "operatore": entry["operatore"],
        "campo": field,
        "status": status,
    }
    if message:
        out["messaggio"] = message
    if sent_at:
        out["inviato_il"] = sent_at
    return out

def valid_start(entry, zone, valid_keys):
    day = datetime.strptime(entry["data"], "%Y-%m-%d").date()
    key = entry["fascia"]
    if key not in valid_keys or key not in SHIFT_RULES:
        raise ValueError(f"Fascia non riconosciuta: {key}")
    allowed_days, start_time, field = SHIFT_RULES[key]
    if day.weekday() not in allowed_days:
        raise ValueError(f"La fascia {key} non è prevista per la data {entry['data']}.")
    return datetime.combine(day, start_time, tzinfo=zone), field

def verify(entry, field, expected, zone, start, state, event_id, previous):
    code, body = request_text(STATUS_URL)
    now = datetime.now(zone)
    if code == 200:
        actual = parse_status(body).get(field, "")
        if actual.casefold() == expected.casefold():
            state["events"][event_id] = record_for(entry, field, "verificata", sent_at=previous.get("inviato_il"))
            print(f"Verificata {entry['data']} {entry['fascia']}: {expected}.")
            return True
    if now >= start - LATE_WINDOW:
        state["events"][event_id] = record_for(entry, field, "errore_verifica", "Il campo letto non coincide con l'operatore previsto.", sent_at=previous.get("inviato_il"))
        annotate("error", f"Verifica fallita per {entry['data']} {entry['fascia']}: atteso {expected}; campo {field} non coincidente.")
        return False
    state["events"][event_id] = record_for(entry, field, "in_attesa_verifica", "Comando inviato; lettura MacroDroid non ancora coincidente.", sent_at=previous.get("inviato_il"))
    print(f"Verifica ancora in attesa per {entry['data']} {entry['fascia']}.")
    return True

def main():
    plan = load_json(PLAN_FILE, {})
    zone_name = plan.get("timezone", "Europe/Rome")
    zone = ZoneInfo(zone_name)
    entries = plan.get("turni", [])
    if not isinstance(entries, list):
        raise ValueError("Il campo 'turni' deve essere una lista.")
    people = load_people()
    fasce = load_fasce()
    state = load_json(STATE_FILE, {"events": {}})
    state.setdefault("events", {})
    original_events = json.dumps(state["events"], ensure_ascii=False, sort_keys=True)
    now = datetime.now(zone)
    seen = set()
    errors = []

    for entry in entries:
        if not isinstance(entry, dict) or not all(k in entry for k in ("data", "fascia", "operatore")):
            raise ValueError("Ogni turno deve contenere data, fascia e operatore.")
        event_id = f"{entry['data']}|{entry['fascia']}"
        if event_id in seen:
            raise ValueError(f"Turno duplicato: {event_id}")
        seen.add(event_id)
        if not isinstance(entry["operatore"], str) or not entry["operatore"].strip():
            raise ValueError(f"Operatore vuoto per {event_id}.")
        start, field = valid_start(entry, zone, fasce)
        previous = state["events"].get(event_id, {})
        status = previous.get("status")
        if previous and previous.get("operatore") != entry["operatore"]:
            if now >= start - LATE_WINDOW:
                annotate("error", f"Turno {entry['data']} {entry['fascia']} modificato troppo tardi; nessun nuovo comando inviato.")
                errors.append(event_id)
                continue
            state["events"].pop(event_id, None)
            previous = {}
            status = None

        if status in {"verificata", "saltata_operatore_mancante", "errore_verifica", "mancata_finestra", "operatore_non_in_rubrica"}:
            continue
        if status == "in_attesa_verifica":
            verify(entry, field, entry["operatore"], zone, start, state, event_id, previous)
            if state["events"][event_id]["status"] == "errore_verifica":
                errors.append(event_id)
            continue

        remaining = start - now
        catchup = remaining <= LATE_WINDOW and now - start <= CATCHUP_WINDOW
        if remaining <= LATE_WINDOW and not catchup:
            state["events"][event_id] = record_for(entry, field, "mancata_finestra", "Finestra di invio superata; nessun comando inviato.")
            if now - start <= timedelta(hours=24):
                annotate("error", f"Fascia non impostata in tempo: {entry['data']} {entry['fascia']}.")
                errors.append(event_id)
            continue
        if remaining > EARLY_WINDOW:
            continue

        name = entry["operatore"].strip()
        code = people.get(name)
        if not code:
            state["events"][event_id] = record_for(entry, field, "saltata_operatore_mancante", "Nome assente da PERSONE; fascia lasciata invariata.")
            annotate("error", f"Operatore {name} non presente in PERSONE: fascia {entry['data']} {entry['fascia']} saltata, valore precedente lasciato invariato.")
            errors.append(event_id)
            continue
        if not re.fullmatch(r"\*\*21\*\+39\d{9,11}#", code):
            state["events"][event_id] = record_for(entry, field, "operatore_non_in_rubrica", "Codice non valido in PERSONE; fascia lasciata invariata.")
            annotate("error", f"Codice per {name} non valido in PERSONE; fascia {entry['data']} {entry['fascia']} saltata.")
            errors.append(event_id)
            continue

        params = {f"op_{entry['fascia']}": code, f"nome_{entry['fascia']}": name}
        if catchup:
            params["dial_now"] = code
        http_code, _ = request_text(f"{SET_URL}?{urlencode(params)}")
        if catchup and 200 <= http_code < 300:
            time_module.sleep(2)
            request_text(DEVIA_URL)
            annotate("warning", f"Fascia {entry['data']} {entry['fascia']} impostata in ritardo con deviazione immediata.")
        if http_code < 200 or http_code >= 300:
            annotate("error", f"MacroDroid non ha accettato il comando per {entry['data']} {entry['fascia']} (HTTP {http_code or 'rete'}).")
            errors.append(event_id)
            continue
        sent_at = now.isoformat(timespec="seconds")
        state["events"][event_id] = record_for(entry, field, "in_attesa_verifica", "Comando accettato; verifica stato in corso.", sent_at=sent_at)
        verify(entry, field, name, zone, start, state, event_id, state["events"][event_id])
        if state["events"][event_id]["status"] == "errore_verifica":
            errors.append(event_id)

    # Keep the public status file compact.
    cutoff = (now.date() - timedelta(days=60)).isoformat()
    state["events"] = {key: value for key, value in state["events"].items() if value.get("data", "") >= cutoff}
    if json.dumps(state["events"], ensure_ascii=False, sort_keys=True) != original_events:
        save_state(state)
    if errors:
        return 1
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        annotate("error", f"Automazione turni interrotta: {type(exc).__name__}: {exc}")
        sys.exit(1)
