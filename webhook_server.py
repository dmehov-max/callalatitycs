"""
webhook_server.py — Приема webhook известия от Callflow (Метод 3 "Call info")
и стартира автоматичната обработка (сваляне на запис -> транскрибация ->
AI анализ) веднага след приключване на всеки разговор.

Този процес трябва да работи ПОСТОЯННО (не cron) на сървър с публичен
HTTPS адрес, защото Callflow сам изпраща известия към него в реално време.

Endpoint, който трябва да регистрираш в Callflow настройките:
  https://<твоя-домейн>/webhook/callflow

ВАЖНО: обработката в отделна нишка (thread) е "fire-and-forget" — ако
процесът рестартира по средата, тя умира без следа. За да не се губят
разговори тихо, всеки заседнал/провален разговор се довършва по-късно
от reconcile.py (виж отделен systemd timer). Този файл само стартира
първия опит; reconcile.py покрива всички пропуснати случаи.

Стартиране (development):
  python webhook_server.py

Стартиране (production, зад nginx/systemd):
  gunicorn -w 2 -b 127.0.0.1:8000 webhook_server:app
"""

import os
import threading
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify

import db
from transcribe import transcribe_file, transcribe_file_diarized
from analyze import analyze_transcript
from connectors.callflow_client import fetch_and_save_recording

app = Flask(__name__)

AUDIO_STORAGE_DIR = Path(__file__).parent / "audio_storage"

MIN_BILLSEC_TO_PROCESS = 5

MISSED_DIALSTATUSES = {"NOANSWER", "BUSY", "CANCEL", "CONGESTION",
                        "EXTEN_NOANSWER", "EXTEN_BUSY"}


@app.route("/webhook/callflow", methods=["POST"])
def callflow_webhook():
    try:
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            return jsonify({"error": {"code": 400, "message": "Bad Request: invalid JSON"}}), 400

        event = payload.get("event")
        dialstatus = payload.get("dialstatus")

        if event == "endcall" and dialstatus == "ANSWER":
            _handle_completed_call(payload)
        elif event == "endcall" and dialstatus in MISSED_DIALSTATUSES:
            _handle_missed_call(payload)

        return jsonify({"status": "accepted"}), 200

    except Exception:
        traceback.print_exc()
        return jsonify({"error": {"code": 500, "message": "Internal Server Error: unexpected exception"}}), 500


def _handle_missed_call(payload: dict):
    call_id = payload.get("callId")
    direction = payload.get("direction", "")

    if not call_id:
        print("[webhook] Липсва callId в пропуснат разговор — пропускам запис.")
        return

    customer_number = payload.get("anumber") if direction == "IN" else payload.get("bnumber")
    call_datetime_raw = payload.get("endcall") or payload.get("startcall", "")
    call_date = call_datetime_raw.split(" ")[0] if call_datetime_raw else datetime.now().strftime("%Y-%m-%d")

    db.insert_missed_call({
        "external_call_id": call_id,
        "call_date": call_date,
        "call_datetime": call_datetime_raw,
        "duration_seconds": 0,
        "agent_extension": None,
        "customer_number": customer_number,
        "direction": direction,
    })
    print(f"[webhook] Пропуснат разговор записан: {call_id} "
          f"({customer_number}, {direction}, {payload.get('dialstatus')})")


def _handle_completed_call(payload: dict):
    call_id = payload.get("callId")
    billsec = int(payload.get("billsec", 0) or 0)
    direction = payload.get("direction", "")

    if not call_id:
        print("[webhook] Липсва callId в endcall събитие — пропускам.")
        return

    if billsec < MIN_BILLSEC_TO_PROCESS:
        print(f"[webhook] {call_id}: billsec={billsec} — твърде кратък "
              f"(вероятно без реален разговор), пропускам.")
        return

    customer_number = payload.get("anumber") if direction == "IN" else payload.get("bnumber")

    call_datetime_raw = payload.get("endcall", "")
    call_date = call_datetime_raw.split(" ")[0] if call_datetime_raw else datetime.now().strftime("%Y-%m-%d")

    db_call = {
        "external_call_id": call_id,
        "call_date": call_date,
        "call_datetime": call_datetime_raw,
        "duration_seconds": billsec,
        "agent_name": None,
        "agent_extension": None,
        "customer_number": customer_number,
        "direction": direction,
        "audio_file_path": None,
        "audio_source_url": None,
    }
    db_call_id = db.insert_call(db_call)

    existing = db.get_call_by_id(db_call_id)
    if existing and existing["status"] == "analyzed":
        print(f"[webhook] {call_id}: вече анализиран (db id={db_call_id}) — "
              f"дублиран webhook, пропускам повторна обработка.")
        return

    print(f"[webhook] Регистриран разговор {call_id} (db id={db_call_id}, "
          f"{billsec}s, {direction}). Стартирам обработка на заден фон...")

    thread = threading.Thread(
        target=_process_call_pipeline,
        args=(db_call_id, call_id),
        daemon=True,
    )
    thread.start()


def _process_call_pipeline(db_call_id: int, external_call_id: str):
    try:
        dest_dir = str(AUDIO_STORAGE_DIR / datetime.now().strftime("%Y-%m-%d"))
        audio_path = fetch_and_save_recording(external_call_id, dest_dir)
        db.set_audio_path(db_call_id, audio_path)
        print(f"[pipeline] {external_call_id}: аудио свалено -> {audio_path}")

        transcript_result = transcribe_file(audio_path)
        transcript_result_2 = transcribe_file_diarized(audio_path)
        transcript_result_3 = transcribe_file(audio_path, model="gpt-4o-transcribe")
        db.insert_transcript(
            db_call_id, transcript_result["text"], transcript_result["language"],
            transcript_result["model"], transcript_result["cost_usd"],
        )
        print(f"[pipeline] {external_call_id}: транскрибиран (3 модела, "
              f"{transcript_result['duration_minutes']} мин)")

        call_row = db.get_call_by_id(db_call_id)
        metadata = {"duration_seconds": call_row["duration_seconds"], "direction": call_row["direction"]}

        analysis = analyze_transcript(
            transcript_result["text"], metadata,
            secondary_transcript=transcript_result_2["text"],
            tertiary_transcript=transcript_result_3["text"],
        )
        db.insert_analysis(
            db_call_id, analysis["agent_name"], analysis["agent_name_confidence"],
            analysis["korekt_mentioned"], analysis["referral_source_asked"], analysis["service_line"],
            analysis["followup_call_required"],
            analysis["call_category"], analysis["category_reasoning"],
            analysis["call_type"], analysis["call_type_reasoning"],
            analysis["diarized_transcript"],
            analysis["overall_summary"], analysis["overall_score"],
            analysis["is_flagged"], analysis["flag_reason"], analysis["model"],
            analysis["cost_usd"], analysis["raw_response"], analysis["criteria_scores"],
        )
        status = "НЕРЕЛЕВАНТЕН" if analysis["call_category"] == "нерелевантен" else (
            "ФЛАГНАТ" if analysis["is_flagged"] else "OK")
        print(f"[pipeline] {external_call_id}: анализиран — {analysis['agent_name']} "
              f"({analysis['agent_name_confidence']}) — категория={analysis['call_category']} "
              f"— {analysis['overall_score']}/10 — {status}")

    except Exception as exc:
        print(f"[pipeline] ГРЕШКА при обработка на {external_call_id}:")
        traceback.print_exc()
        db.mark_call_failed(db_call_id, f"{type(exc).__name__}: {exc}")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
