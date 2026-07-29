"""
test_single_call.py — Самостоятелен тест на транскрибация + анализ върху
ЕДИН локален аудио файл, без база данни, без webhook, без dashboard.

Употреба:
  python test_single_call.py path/to/razgovor.mp3
  python test_single_call.py path/to/razgovor.mp3 IN     (ако знаеш посоката)
  python test_single_call.py path/to/razgovor.mp3 OUT

Посоката (IN = клиентът е звъннал, OUT = фирмата е звъннала) помага на
Claude да разреши неясноти в диаризацията (напр. кой казва 'връщам
обаждане') — в реалната Callflow интеграция тази информация идва
автоматично; тук е опционална, за ръчно тестване на тази логика.

Изисква в .env (или в environment): ANTHROPIC_API_KEY, OPENAI_API_KEY,
SONIOX_API_KEY (виж .env.example — регистрация: console.soniox.com)
"""

import sys
import json
from pathlib import Path

from transcribe import transcribe_file, transcribe_file_diarized
from analyze import analyze_transcript
from criteria_config import CRITERIA

# Троична транскрибация: вика ТРИ различни модела на едно и също аудио,
# Claude ги сравнява (гласуване по съгласие) и съставя обединен, по-точен
# транскрипт. Модел 2 беше whisper-1 (OpenAI), но той системно
# "халюцинираше" (безсмислени повторения) на голяма част от тестовите
# разговори. ЕКСПЕРИМЕНТАЛНО заменен с gpt-4o-transcribe-diarize (виж
# transcribe_file_diarized в transcribe.py) — ако и той не се справи
# добре на практика, връщаме се на 2 модела (виж коментара в transcribe.py
# точно откъде да се маха).
TERTIARY_MODEL = "gpt-4o-transcribe"


def print_header(text: str):
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def main():
    if len(sys.argv) < 2:
        print("Употреба: python test_single_call.py path/to/audio.mp3 [IN|OUT]")
        sys.exit(1)

    audio_path = sys.argv[1]
    if not Path(audio_path).exists():
        print(f"ГРЕШКА: файлът не съществува: {audio_path}")
        sys.exit(1)

    direction = None
    if len(sys.argv) >= 3:
        direction_arg = sys.argv[2].upper()
        if direction_arg in ("IN", "OUT"):
            direction = direction_arg
        else:
            print(f"ГРЕШКА: посоката трябва да е 'IN' или 'OUT', получих '{sys.argv[2]}'")
            sys.exit(1)

    # --- Стъпка 1: Транскрибация (ТРИ модела) ---
    print_header("1. ТРАНСКРИБАЦИЯ (модел 1)")
    print(f"Файл: {audio_path}")
    print("Изчакай, викам Whisper API (модел 1)...")

    transcript_result = transcribe_file(audio_path)

    print(f"Език: {transcript_result['language']} | Модел: {transcript_result['model']} | "
          f"Продължителност: {transcript_result['duration_minutes']} мин | "
          f"Цена: ${transcript_result['cost_usd']}")
    print(f"\n--- Транскрипт (модел 1) ---\n{transcript_result['text']}")

    print_header("1Б. ТРАНСКРИБАЦИЯ (модел 2: gpt-4o-transcribe-diarize, ЕКСПЕРИМЕНТАЛЕН)")
    print("Изчакай, викам Whisper API (модел 2)...")

    transcript_result_2 = transcribe_file_diarized(audio_path)

    print(f"Модел: {transcript_result_2['model']} | Цена: ${transcript_result_2['cost_usd']}")
    print(f"\n--- Транскрипт (модел 2, с вградени граници между говорители) ---\n{transcript_result_2['text']}")

    print_header(f"1В. ТРАНСКРИБАЦИЯ (модел 3: {TERTIARY_MODEL})")
    print("Изчакай, викам Whisper API (модел 3)...")

    transcript_result_3 = transcribe_file(audio_path, model=TERTIARY_MODEL)

    print(f"Модел: {transcript_result_3['model']} | Цена: ${transcript_result_3['cost_usd']}")
    print(f"\n--- Транскрипт (модел 3) ---\n{transcript_result_3['text']}")

    # --- Стъпка 2: AI анализ (Claude съгласува трите транскрипта) ---
    print_header("2. АНАЛИЗ (Claude съгласува трите транскрипта)")
    print("Изчакай, викам Claude API...")

    call_metadata = {"direction": direction} if direction else None
    if direction:
        print(f"(Посока зададена ръчно: {direction})")

    analysis = analyze_transcript(
        transcript_result["text"],
        call_metadata=call_metadata,
        secondary_transcript=transcript_result_2["text"],
        tertiary_transcript=transcript_result_3["text"],
    )

    # --- Индикатор "Корект" (отделен, НЕ участва в оценката) ---
    print_header("3. ИНДИКАТОР: СПОМЕНАТО ЛИ Е 'КОРЕКТ'")
    korekt_label = "✅ ДА" if analysis["korekt_mentioned"] else "❌ НЕ"
    print(f"{korekt_label} — този индикатор е ЧИСТО информационен, "
          f"НЕ участва в оценката на разговора.")

    referral_label = "✅ ДА" if analysis["referral_source_asked"] else "❌ НЕ"
    print(f"\nПитано/казано ли е откъде е научено за фирмата: {referral_label} "
          f"— също чисто информационен индикатор.")

    service_line_labels = {
        "преместване": "🚚 Преместване",
        "извозване_на_боклук": "🗑️ Извозване на боклук",
        "сглобяване": "🔧 Сглобяване",
        "опаковане": "📦 Опаковане",
        "продажба_от_склад": "🏬 Продажба от склад",
        "друго": "❔ Друго",
    }
    print(f"\nУслуга (коридор): {service_line_labels.get(analysis['service_line'], analysis['service_line'])} "
          f"— чисто информационно, не влияе на критериите.")

    followup_label = "🔴 ДА — фирмата трябва да звънне обратно" if analysis["followup_call_required"] else "✅ НЕ — топката е в полето на клиента"
    print(f"\nИзисква ли се изходящо обаждане от фирмата: {followup_label}")
    print("(използва се за дневната проверка за пропуснати клиенти, не влияе на оценката)")

    # --- Категория на разговора ---
    print_header("4. КАТЕГОРИЯ НА РАЗГОВОРА")
    category_labels = {
        "нова_поръчка": "🆕 НОВА ПОРЪЧКА",
        "потвърждение": "✅ ПОТВЪРЖДЕНИЕ",
        "нерелевантен": "🚫 НЕРЕЛЕВАНТЕН (изключва се от статистиките)",
        "друго": "❔ ДРУГО",
    }
    print(f"Категория: {category_labels.get(analysis['call_category'], analysis['call_category'])}")
    print(f"Причина: {analysis['category_reasoning']}")

    call_type_labels = {
        "бърза_оферта": "⚡ БЪРЗА ОФЕРТА (само цена + ETA)",
        "пълна_консултация": "📋 ПЪЛНА КОНСУЛТАЦИЯ (обсъждане на детайли)",
    }
    print(f"\nТип разговор: {call_type_labels.get(analysis['call_type'], analysis['call_type'])}")
    print(f"Причина: {analysis['call_type_reasoning']}")

    if analysis["call_category"] == "нерелевантен":
        print("\n⚠️  Разговорът е маркиран като НЕРЕЛЕВАНТЕН — не се оценява "
              "съдържателно и не влиза в статистиките по служители.")
        print(f"\nОбобщение: {analysis['overall_summary']}")
        print_header("5. РАЗХОДИ")
        total_cost = (transcript_result["cost_usd"] + transcript_result_2["cost_usd"]
                       + transcript_result_3["cost_usd"] + analysis["cost_usd"])
        print(f"Общо: ${total_cost:.6f}")
        return

    # --- Диаризиран транскрипт (Служител / Клиент) ---
    print_header("5. ДИАРИЗИРАН ТРАНСКРИПТ")
    print(analysis["diarized_transcript"])

    print_header("6. ИЗВЛЕЧЕНО ИМЕ НА СЛУЖИТЕЛ")
    print(f"{analysis['agent_name']} (увереност: {analysis['agent_name_confidence']})"
          f"{'  ⚠️ ФЛАГНАТ РАЗГОВОР - ПРЕСЛУШАЙ' if analysis['is_flagged'] else ''}")

    print_header("7. ОЦЕНКИ ПО КРИТЕРИЙ")
    for c in CRITERIA:
        score_data = next(s for s in analysis["criteria_scores"] if s["key"] == c["key"])
        if score_data["applicable"]:
            score_label = f"{score_data['score']:>2}/10"
        else:
            score_label = " N/A "
        print(f"  [{score_label}] {score_data['label']}")
        print(f"           → {score_data['justification']}")

    print_header("8. ОБЩ РЕЗУЛТАТ")
    print(f"Общ резултат (претеглен): {analysis['overall_score']}/10")
    print(f"Статус: {'🔴 ФЛАГНАТ' if analysis['is_flagged'] else '✅ Успешен'}")
    if analysis["is_flagged"]:
        print(f"Причина за флага: {analysis['flag_reason']}")
    print(f"\nОбобщение: {analysis['overall_summary']}")

    print_header("9. РАЗХОДИ")
    total_cost = (transcript_result["cost_usd"] + transcript_result_2["cost_usd"]
                   + transcript_result_3["cost_usd"] + analysis["cost_usd"])
    print(f"Транскрибация (модел 1): ${transcript_result['cost_usd']} | "
          f"Транскрибация (модел 2): ${transcript_result_2['cost_usd']} | "
          f"Транскрибация (модел 3): ${transcript_result_3['cost_usd']} | "
          f"Анализ: ${analysis['cost_usd']} | ОБЩО: ${total_cost:.6f}")


if __name__ == "__main__":
    main()
