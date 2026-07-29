"""
connectors/callflow_client.py — Клиент за Callflow/Omnilinx PBX API.

Базиран на "API Specification Omnilinx PBX system", v2.0.0 (11.2025).

ВАЖНО архитектурно решение: Callflow НЯМА endpoint от типа "дай ми
разговорите за дата X" (pull). Вместо това ТЕ известяват НАС при всяко
събитие на разговор чрез webhook (виж Метод 3 "Call info" в
документацията) — ние трябва да имаме публично достъпен HTTPS endpoint,
на който да приемаме тези известия (виж webhook_server.py).

Този модул съдържа само Метод 4 ("Request a phone call record") —
сваляне на самия аудио файл, след като вече знаем callId от webhook-а.
"""

import os
import requests
from pathlib import Path

CALLFLOW_HOST = os.environ.get("CALLFLOW_HOST", "https://uhub.callflowlab.com")
CALLFLOW_USERNAME = os.environ.get("CALLFLOW_USERNAME")
CALLFLOW_PASSWORD = os.environ.get("CALLFLOW_PASSWORD")
CALLFLOW_CODE = os.environ.get("CALLFLOW_CODE")
CALLFLOW_CLIENTID = os.environ.get("CALLFLOW_CLIENTID")


def _auth_payload() -> dict:
    return {
        "username": CALLFLOW_USERNAME,
        "password": CALLFLOW_PASSWORD,
        "code": CALLFLOW_CODE,
        "clientid": CALLFLOW_CLIENTID,
    }


def request_call_recording(call_id: str = None, apicallid: str = None) -> str:
    """
    Метод 4 от спецификацията: POST [host]/v1/api/external-call/audio

    Подава се ИЛИ call_id (полето "callId" от webhook-а), ИЛИ apicallid
    (само ако разговорът е инициран от нас чрез Метод 1/2 — не е нашият
    случай, ние само слушаме входящи/изходящи разговори през клиента).

    Връща относителен URL към записа (напр. "/v1/tmp/call_recordings/xxx.mp3"),
    който после трябва да се свали отделно (виж download_recording).
    """
    if not call_id and not apicallid:
        raise ValueError("Трябва да се подаде call_id или apicallid.")

    payload = _auth_payload()
    if call_id:
        payload["callid"] = call_id
    else:
        payload["apicallid"] = apicallid

    resp = requests.post(
        f"{CALLFLOW_HOST}/v1/api/external-call/audio",
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "accepted":
        raise RuntimeError(f"Неочакван отговор от Callflow: {data}")

    return data["url"]


def download_recording(recording_url: str, dest_path: str) -> str:
    """
    Сваля аудио файла от относителния URL, върнат от request_call_recording,
    на локален диск. dest_path трябва да включва разширението (.mp3).
    """
    full_url = f"{CALLFLOW_HOST}{recording_url}" if recording_url.startswith("/") else recording_url

    resp = requests.get(full_url, timeout=60, stream=True)
    resp.raise_for_status()

    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    return dest_path


def fetch_and_save_recording(call_id: str, dest_dir: str) -> str:
    """Удобна обвивка: Метод 4 + сваляне в едно повикване."""
    recording_url = request_call_recording(call_id=call_id)
    dest_path = os.path.join(dest_dir, f"{call_id}.mp3")
    return download_recording(recording_url, dest_path)
