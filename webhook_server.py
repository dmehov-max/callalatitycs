"""
webhook_server.py — Приема webhook известия от Callflow (Метод 3 "Call info")
и стартира автоматичната обработка (сваляне на запис -> транскрибация ->
AI анализ) веднага след приключване на всеки разговор.

Този процес трябва да работи ПОСТОЯННО (не cron) на сървър с публичен
HTTPS адрес, защото Callflow сам изпраща известия към него в реално време.

Endpoint, който трябва да регистрираш в Callflow настройките:
  https://<твоя-домейн>/webhook/callflow

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

# По спецификация: обработваме разговор само ако наистина е бил вдигнат
# и е имал реална продължителност — иначе няма аудио съдържание за анализ.
MIN_BILLSEC_TO_PROCESS = 5

# dialstatus стойности, означаващи че разговорът НЕ е бил вдигнат от
# служителя — важни за проследяване на пропуснати потенциални клиенти
# (виж insert_missed_call и get_daily_followup_report в db.py).
MISSED_DIALSTATUSES = {"NOANSWER", "BUSY", "CANCEL", "CONGESTION",
                        "EXTEN_NOANSWER", "EXTEN_BUSY"}


@app.route("/webhook/callflow", methods=["POST"])
def callflow_webhook():
    """
    Приема POST заявки от Callflow при всяко събитие на разговор.
    Отговорът ВИНАГИ трябва да е в описания в спецификацията формат,
    независимо дали сме обработили събитието или сме го игнорирали.
    """
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

        # За всички останали събития (startcall, answer, dial_device) само
        # потвърждаваме получаването — нямаме нужда да действаме по тях сега.
        return jsonify({"status": "accepted"}), 200

    except Exception:
        traceback.print_exc()
        return jsonify({"error": {"code": 500, "message": "Internal Server Error: unexpected exception"}}), 500


def _handle_missed_call(payload: dict):
    """
    Пропуснат/необvдигнат разговор — записваме САМО метаданни (номер, час,
    посока), БЕЗ да опитваме сваляне на запис (няма аудио за такъв
    разговор). Използва се за дневната проверка "липсва последващо
    обаждане" (виж db.get_daily_followup_report).
    """
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

    # Клиентският номер е anumber при входящ разговор, bnumber при изходящ.
    customer_number = payload.get("anumber") if direction == "IN" else payload.get("bnumber")

    call_datetime_raw = payload.get("endcall", "")
    call_date = call_datetime_raw.split(" ")[0] if call_datetime_raw else datetime.now().strftime("%Y-%m-%d")

    db_call = {
        "external_call_id": call_id,
        "call_date": call_date,
        "call_datetime": call_datetime_raw,
        "duration_seconds": billsec,
        "agent_name": None,          # ще се попълни от AI анализа на транскрипта
        "agent_extension": None,
        "customer_number": customer_number,
        "direction": direction,
        "audio_file_path": None,
        "audio_source_url": None,
    }
    db_call_id = db.insert_call(db_call)
    print(f"[webhook] Регистриран разговор {call_id} (db id={db_call_id}, "
          f"{billsec}s, {direction}). Стартирам обработка на заден фон...")

    # Обработката (сваляне+транскрибация+анализ) отнема секунди-минути —
    # не бива да блокира отговора към Callflow. Пуска се в отделна нишка.
    thread = threading.Thread(
        target=_process_call_pipeline,
        args=(db_call_id, call_id),
        daemon=True,
    )
    thread.start()


def _process_call_pipeline(db_call_id: int, external_call_id: str):
    """
    Пълната верига за един разговор: сваляне на записа -> транскрибация ->
    AI анализ (вкл. извличане на име на служителя) -> запис в базата.

    Изпълнява се в отделна нишка на всяко пристигнало известие — при
    висок обем разговори едновременно, това означава множество паралелни
    Whisper/Claude заявки. Ако това стане проблем (rate limits), се
    добавя опашка с ограничен брой worker-и вместо неограничени нишки.
    """
    try:
        # 1. Сваляне на аудиото (Callflow Метод 4)
        dest_dir = str(AUDIO_STORAGE_DIR / datetime.now().strftime("%Y-%m-%d"))
        audio_path = fetch_and_save_recording(external_call_id, dest_dir)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE calls SET audio_file_path = ? WHERE id = ?",
                (audio_path, db_call_id),
            )
        print(f"[pipeline] {external_call_id}: аудио свалено -> {audio_path}")

        # 2. Транскрибация (3 модела за по-надеждно съгласуване — виж
        #    analyze.py за защо: единичен модел може да "халюцинира" на
        #    труден/шумен пасаж, 3 независими гласа са по-надеждни от 1)
        transcript_result = transcribe_file(audio_path)
        transcript_result_2 = transcribe_file_diarized(audio_path)
        transcript_result_3 = transcribe_file(audio_path, model="gpt-4o-transcribe")
        db.insert_transcript(
            db_call_id, transcript_result["text"], transcript_result["language"],
            transcript_result["model"], transcript_result["cost_usd"],
        )
        print(f"[pipeline] {external_call_id}: транскрибиран (3 модела, "
              f"{transcript_result['duration_minutes']} мин)")

        # 3. AI анализ (извлича и името на служителя от транскрипта,
        #    съгласува трите транскрипта в един по-точен)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT duration_seconds, direction FROM calls WHERE id = ?",
                (db_call_id,),
            ).fetchone()
        metadata = {"duration_seconds": row["duration_seconds"], "direction": row["direction"]}

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

    except Exception:
        print(f"[pipeline] ГРЕШКА при обработка на {external_call_id}:")
        traceback.print_exc()
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET status = 'failed' WHERE id = ?", (db_call_id,))


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
