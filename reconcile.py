"""
reconcile.py — Довършва заседнали/провалени разговори.

Причина за съществуването на този скрипт: webhook_server.py обработва
всеки разговор в daemon thread (fire-and-forget) — ако процесът рестартира
по средата (deploy, срив, systemctl restart), нишката умира без следа и
разговорът остава завинаги в междинен статус. Този скрипт периодично
проверява за такива случаи и ги довършва от правилната стъпка нататък,
без да преповтаря вече свършена (платена) работа.

Стартиране: през systemd timer на всеки 10 минути (виж
reconcile.timer / reconcile.service). Може да се пусне и ръчно:
  python reconcile.py
"""

import traceback
from datetime import datetime
from pathlib import Path

import db
from transcribe import transcribe_file, transcribe_file_diarized
from analyze import analyze_transcript
from connectors.callflow_client import fetch_and_save_recording

AUDIO_STORAGE_DIR = Path(__file__).parent / "audio_storage"

STALE_MINUTES = 30
MAX_RETRIES = 3


def reconcile_one(call_row):
    call_id = call_row["id"]
    external_call_id = call_row["external_call_id"]
    status = call_row["status"]

    print(f"[reconcile] {external_call_id} (db id={call_id}, статус={status}, "
          f"опити={call_row['retry_count']}) — довършвам...")

    try:
        # Стъпка 1: аудио — прескачаме, ако вече е свалено
        audio_path = call_row["audio_file_path"]
        if not audio_path:
            dest_dir = str(AUDIO_STORAGE_DIR / datetime.now().strftime("%Y-%m-%d"))
            audio_path = fetch_and_save_recording(external_call_id, dest_dir)
            db.set_audio_path(call_id, audio_path)
            print(f"[reconcile] {external_call_id}: аудио свалено -> {audio_path}")

        # Стъпка 2: транскрипция — прескачаме, ако вече е 'transcribed'/'analyzed'
        transcript_text = db.get_transcript_text(call_id)
        if status == "downloaded" or not transcript_text:
            transcript_result = transcribe_file(audio_path)
            transcript_result_2 = transcribe_file_diarized(audio_path)
            transcript_result_3 = transcribe_file(audio_path, model="gpt-4o-transcribe")
            db.insert_transcript(
                call_id, transcript_result["text"], transcript_result["language"],
                transcript_result["model"], transcript_result["cost_usd"],
            )
            transcript_text = transcript_result["text"]
            secondary_text = transcript_result_2["text"]
            tertiary_text = transcript_result_3["text"]
            print(f"[reconcile] {external_call_id}: транскрибиран (3 модела)")
        else:
            # Вече имаме основния транскрипт от предишен опит — за анализа
            # ползваме само него и на трите позиции (по-добре с лек
            # компромис в съгласуването, отколкото да плащаме повторно
            # за транскрибация, която вече имаме).
            secondary_text = transcript_text
            tertiary_text = transcript_text

        # Стъпка 3: анализ (ако вече е 'analyzed', reconcile_one изобщо не
        # би трябвало да е бил повикан за този ред — виж филтъра в main())
        call_fresh = db.get_call_by_id(call_id)
        metadata = {"duration_seconds": call_fresh["duration_seconds"], "direction": call_fresh["direction"]}

        analysis = analyze_transcript(
            transcript_text, metadata,
            secondary_transcript=secondary_text,
            tertiary_transcript=tertiary_text,
        )
        db.insert_analysis(
            call_id, analysis["agent_name"], analysis["agent_name_confidence"],
            analysis["korekt_mentioned"], analysis["referral_source_asked"], analysis["service_line"],
            analysis["followup_call_required"],
            analysis["call_category"], analysis["category_reasoning"],
            analysis["call_type"], analysis["call_type_reasoning"],
            analysis["diarized_transcript"],
            analysis["overall_summary"], analysis["overall_score"],
            analysis["is_flagged"], analysis["flag_reason"], analysis["model"],
            analysis["cost_usd"], analysis["raw_response"], analysis["criteria_scores"],
        )
        print(f"[reconcile] {external_call_id}: ГОТОВО — анализиран успешно.")

    except Exception as exc:
        print(f"[reconcile] {external_call_id}: ГРЕШКА при опит {call_row['retry_count'] + 1}:")
        traceback.print_exc()
        db.mark_call_failed(call_id, f"{type(exc).__name__}: {exc}")


def main():
    stuck = db.get_stuck_calls(stale_minutes=STALE_MINUTES, max_retries=MAX_RETRIES)

    if not stuck:
        print("[reconcile] Няма заседнали/провалени разговори за довършване.")
        return

    print(f"[reconcile] Намерени {len(stuck)} разговора за довършване.")
    for call_row in stuck:
        # Прескачаме разговори, вече завършени между заявката и обработката
        # (напр. webhook нишката ги е довършила междувременно).
        fresh = db.get_call_by_id(call_row["id"])
        if fresh["status"] == "analyzed":
            continue
        reconcile_one(fresh)

    permanently_failed = db.get_permanently_failed_calls()
    if permanently_failed:
        print(f"[reconcile] ВНИМАНИЕ: {len(permanently_failed)} разговора са "
              f"изчерпали {MAX_RETRIES} опита и изискват ръчна проверка "
              f"(виж dashboard -> Известия).")


if __name__ == "__main__":
    main()
