"""
improvement_report.py — Генерира писмен анализ с препоръки за подобрение
на база агрегирана статистика за избран период (и опционално служител).

Задейства се РЪЧНО от dashboard-а (бутон "Генерирай анализ"), не автоматично
по разписание — клиентът избира период + служител и решава кога му трябва.
"""

import os
import json
import anthropic

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-4-6"
PRICE_INPUT_PER_M = 3.00
PRICE_OUTPUT_PER_M = 15.00

SYSTEM_PROMPT = """Ти си опитен консултант по качество на обслужването в
call center. Получаваш агрегирана статистика за телефонни разговори на
фирма "Корект - преместване за домът и офиса" за избран период (и
опционално за конкретен служител), и трябва да напишеш ясен, практичен
анализ с конкретни препоръки за подобрение.

Правила:
- Пиши на български, ясен бизнес език, без излишен жаргон.
- Структурирай отговора с кратки секции (напр. 'Обща картина',
  'Силни страни', 'Слаби страни', 'Конкретни препоръки').
- Бъди конкретен и практичен — препоръките трябва да са неща, които
  реално може да се приложат (напр. 'обучение за затваряне на сделка
  при положителна реакция', не общи фрази като 'подобрете обслужването').
- Ако данните показват само няколко разговора (малка извадка), отбележи
  това изрично — не прави твърде категорични изводи от малко данни.
- Ако е зададен конкретен служител (agent_filter), фокусирай анализа
  върху него лично — насочи препоръките директно към него.
- Ако agent_filter е None, анализирай цялостната картина на фирмата/екипа.
- Не измисляй данни, които не са ти дадени — работи само с подадената
  статистика.
- Дръж отговора разумно кратък — до около 400-600 думи, ясен и четим,
  не безкраен доклад.
"""


def generate_improvement_report(stats: dict) -> dict:
    """
    stats: dict от db.get_period_summary_stats()
    Връща: {"report_text": str, "cost_usd": float, "model": str}
    """
    if stats.get("total_calls", 0) == 0:
        return {
            "report_text": (
                "Няма анализирани разговори за избрания период"
                + (f" за служител '{stats['agent_filter']}'" if stats.get("agent_filter") else "")
                + ". Провери дали периодът е правилен и дали има обработени разговори в него."
            ),
            "cost_usd": 0.0,
            "model": MODEL,
        }

    stats_text = json.dumps(stats, ensure_ascii=False, indent=2)

    user_message = f"""Ето агрегираната статистика за периода:

{stats_text}

Напиши анализ с конкретни препоръки за подобрение на база тези данни."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    report_text = "".join(
        block.text for block in response.content if block.type == "text"
    )

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost = round(
        (input_tokens / 1_000_000) * PRICE_INPUT_PER_M
        + (output_tokens / 1_000_000) * PRICE_OUTPUT_PER_M,
        6,
    )

    return {
        "report_text": report_text,
        "cost_usd": cost,
        "model": MODEL,
    }
