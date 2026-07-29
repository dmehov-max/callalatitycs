"""
main.py — Дневен оркестратор. Пуска се от cron в 00:00 за предходния ден.

Стъпки:
  1. Изтегля метаданни + аудио за целевата дата (през активния конектор)
  2. Записва разговорите в базата
  3. Транскрибира всеки нов запис (Whisper)
  4. Анализира всеки транскрипт (Claude)
  5. Праща обобщено известие за флагнатите разговори

Всяка стъпка е с try/except на ниво разговор — един провален запис не
трябва да спре целия дневен batch. Провалите се логват и разговорът
остава със status='failed' в базата за ръчна проверка.
"""

import argparse
import traceback
from datetime import date, timedelta
from pathlib import Path

import db
from transcribe import transcribe_file
from analyze import analyze_transcript
from export_excel import generate_report

# --- Избор на активен конектор -------------------------------------------
# ЩОМ ЗНАЕМ ЦЕНТРАЛАТА: смени тези 2 реда с реалния конектор, нищо друго
# в този файл не се пипа.
from connectors.stub_connector import StubConnector
ACTIVE_CONNECTOR = StubConnector()
# ---------------------------------------------------------------------------

AUDIO_STORAGE_DIR = Path(__file__).parent / "audio_storage"


def run_pipeline(target_date: date):
    print(f"\n{'='*60}")
    print(f"Стартиране на pipeline за {target_date.isoformat()}")
    print(f"{'='*60}\n")

    db.init_db()

    # --- Стъпка 1+2: извличане и запис в базата ---
    print("[1/4] Извличане на разговори от централата...")
    calls_metadata = ACTIVE_CONNECTOR.fetch_calls_for_date(target_date)
    print(f"  Намерени {len(calls_metadata)} разговора.")

    call_ids = []
    for meta in calls_metadata:
        try:
            dest_dir = str(AUDIO_STORAGE_DIR / target_date.isoformat())
            audio_path = ACTIVE_CONNECTOR.download_audio(meta, dest_dir)
            meta["audio_file_path"] = audio_path
            call_id = db.insert_call(meta)
            call_ids.append(call_id)
        except Exception:
            print(f"  [ГРЕШКА] Неуспешно сваляне на {meta.get('external_call_id')}:")
            traceback.print_exc()

    # --- Стъпка 3: транскрибация ---
    print(f"\n[2/4] Транскрибация на {len(db.get_pending_transcription())} разговора...")
    total_transcribe_cost = 0.0
    for call in db.get_pending_transcription():
        try:
            result = transcribe_file(call["audio_file_path"])
            db.insert_transcript(
                call["id"], result["text"], result["language"],
                result["model"], result["cost_usd"],
            )
            total_transcribe_cost += result["cost_usd"]
            print(f"  ✓ {call['external_call_id']} транскрибиран "
                  f"({result['duration_minutes']} мин, ${result['cost_usd']})")
        except Exception:
            print(f"  [ГРЕШКА] Транскрибация неуспешна за {call['external_call_id']}:")
            traceback.print_exc()
            _mark_failed(call["id"])

    # --- Стъпка 4: анализ ---
    pending = db.get_pending_analysis()
    print(f"\n[3/4] Анализ на {len(pending)} транскрипта...")
    total_analysis_cost = 0.0
    flagged = []
    for call in pending:
        try:
            metadata_for_analysis = {
                "duration_seconds": call["duration_seconds"],
                "direction": call["direction"],
            }
            result = analyze_transcript(call["transcript_text"], metadata_for_analysis)
            db.insert_analysis(
                call["id"], result["agent_name"], result["agent_name_confidence"],
                result["korekt_mentioned"], result["referral_source_asked"], result["service_line"],
                result["followup_call_required"],
                result["call_category"], result["category_reasoning"],
                result["call_type"], result["call_type_reasoning"],
                result["diarized_transcript"],
                result["overall_summary"], result["overall_score"],
                result["is_flagged"], result["flag_reason"], result["model"],
                result["cost_usd"], result["raw_response"], result["criteria_scores"],
            )
            total_analysis_cost += result["cost_usd"]
            status = ("🚫 НЕРЕЛЕВАНТЕН" if result["call_category"] == "нерелевантен"
                      else ("🔴 ФЛАГНАТ" if result["is_flagged"] else "✓ ОК"))
            print(f"  {status} {call['external_call_id']} — {result['agent_name']} — "
                  f"резултат {result['overall_score']}/10 (${result['cost_usd']})")
            if result["is_flagged"]:
                flagged.append({
                    "agent_name": result["agent_name"],
                    "external_call_id": call["external_call_id"],
                    "overall_score": result["overall_score"],
                    "flag_reason": result["flag_reason"],
                })
        except Exception:
            print(f"  [ГРЕШКА] Анализ неуспешен за {call['external_call_id']}:")
            traceback.print_exc()
            _mark_failed(call["id"])

    # --- Стъпка 5: Excel export (прегенерира се изцяло от базата) ---
    print(f"\n[4/4] Обновяване на Excel отчета...")
    generate_report()

    print(f"\n{'='*60}")
    print(f"Готово. Транскрибация: ${total_transcribe_cost:.4f} | "
          f"Анализ: ${total_analysis_cost:.4f} | "
          f"Общо: ${total_transcribe_cost + total_analysis_cost:.4f}")
    print(f"{'='*60}\n")


def _mark_failed(call_id: int):
    with db.get_conn() as conn:
        conn.execute("UPDATE calls SET status = 'failed' WHERE id = ?", (call_id,))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Дневен pipeline за анализ на разговори")
    parser.add_argument(
        "--date", type=str, default=None,
        help="Целева дата YYYY-MM-DD (по подразбиране: вчера)",
    )
    args = parser.parse_args()

    if args.date:
        target = date.fromisoformat(args.date)
    else:
        target = date.today() - timedelta(days=1)

    run_pipeline(target)
