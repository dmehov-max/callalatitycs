"""
transcribe.py — Транскрибация на аудио записи чрез OpenAI API.

Независим от телефонната централа — приема път до локален аудио файл
и връща текст. Централата (стъпка 1) само трябва да свали файла някъде
на диска; оттам нататък този модул не се интересува откъде идва.
"""

import os
import time
from pathlib import Path
from openai import OpenAI
from vocabulary_hints import build_hint

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# $0.006/минута към юли 2026. Виж README за актуализация на цената.
WHISPER_PRICE_PER_MINUTE_USD = 0.006

# gpt-4o-mini-transcribe е ~2x по-евтин от whisper-1 за сравнимо качество
# при чист говор (телефонни разговори). Смени на "whisper-1" ако имаш
# проблеми с точността при силен шум/акценти.
DEFAULT_MODEL = "gpt-4o-mini-transcribe"


def get_audio_duration_minutes(file_path: str) -> float:
    """Взима продължителността на аудио файла в минути чрез ffprobe."""
    import subprocess
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file_path,
        ],
        capture_output=True, text=True,
    )
    seconds = float(result.stdout.strip())
    return seconds / 60.0


def normalize_audio(file_path: str) -> str:
    """
    Изравнява силата на звука (loudness normalization) преди транскрибация —
    усилва тихи гласове спрямо силни, полезно когато един от говорителите
    (типично диспечер с по-тих микрофон) се чува значително по-слабо от
    другия. Използва ffmpeg loudnorm филтър (EBU R128 стандарт),
    ДВУПРОХОДНО (measure -> apply) за по-точна корекция — особено важно
    при файлове с тишина в началото (типично при телефонни записи, където
    минава кратко закъснение преди да започне говоренето), защото
    еднопроходната версия понякога подценява необходимото усилване.

    Връща път до НОВ временен файл с нормализиран звук — оригиналният
    файл НЕ се пипа. При грешка (напр. ffmpeg липсва) връща оригиналния
    път непроменен, за да не спре целия pipeline заради този опционален
    подобряващ стъпка.
    """
    import subprocess
    import tempfile
    import json as json_module

    src = Path(file_path)
    fd, tmp_path = tempfile.mkstemp(suffix="_normalized.wav", prefix="calln_")
    os.close(fd)

    try:
        # Стъпка 1: измерваме реалните нива на целия файл
        measure = subprocess.run(
            [
                "ffmpeg", "-i", str(src),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
        # ffmpeg пише JSON измерванията в stderr
        json_start = measure.stderr.rfind("{")
        json_end = measure.stderr.rfind("}") + 1
        measured = json_module.loads(measure.stderr[json_start:json_end])

        # Стъпка 2: прилагаме точна корекция на база реалните измервания
        loudnorm_filter = (
            f"loudnorm=I=-16:TP=-1.5:LRA=11:"
            f"measured_I={measured['input_i']}:"
            f"measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:"
            f"measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:"
            f"linear=true"
        )
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-af", loudnorm_filter,
                "-ar", "16000", "-ac", "1",
                tmp_path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"[transcribe] Нормализацията (стъпка 2) се провали за "
                  f"{src.name}, използвам оригиналния файл: {result.stderr[-300:]}")
            os.remove(tmp_path)
            return str(src)
        return tmp_path
    except Exception as e:
        print(f"[transcribe] Грешка при двупроходна нормализация на "
              f"{src.name} ({e}), пробвам еднопроходен вариант...")
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(src),
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", "16000", "-ac", "1",
                    tmp_path,
                ],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                return tmp_path
        except Exception:
            pass
        print(f"[transcribe] Нормализацията напълно се провали за "
              f"{src.name}, използвам оригиналния файл.")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return str(src)


def transcribe_file(file_path: str, language: str = None,
                     model: str = DEFAULT_MODEL, max_retries: int = 3,
                     use_vocabulary_hint: bool = True,
                     normalize: bool = True) -> dict:
    """
    Транскрибира един аудио файл.

    language: ISO код (напр. "bg", "en"). Ако се остави None (по подразбиране),
    Whisper САМ разпознава езика от аудиото — препоръчително за реална
    употреба, тъй като разговорите могат да бъдат на различни езици.

    use_vocabulary_hint: ако True (по подразбиране), подава hint от
    vocabulary_hints.py, съобразен с лимита на конкретния model. Виж
    vocabulary_hints.py за това как да добавяш нови термини.

    normalize: ако True (по подразбиране), изравнява силата на звука
    преди транскрибация (виж normalize_audio) — помага при тихи гласове.

    Връща: {"text": str, "language": str, "model": str, "cost_usd": float}

    Хвърля изключение след max_retries неуспешни опита — трябва да се
    хване от orchestrator-а и разговорът да се маркира status='failed',
    НЕ да спира целия dневен batch.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Аудио файлът не съществува: {file_path}")

    duration_min = get_audio_duration_minutes(str(path))
    cost = round(duration_min * WHISPER_PRICE_PER_MINUTE_USD, 6)

    transcribe_path = normalize_audio(str(path)) if normalize else str(path)
    is_temp_file = normalize and transcribe_path != str(path)

    last_error = None
    try:
        for attempt in range(1, max_retries + 1):
            try:
                with open(transcribe_path, "rb") as audio_file:
                    api_kwargs = {
                        "model": model,
                        "file": audio_file,
                        "response_format": "text",
                    }
                    # Само ако езикът е ИЗРИЧНО зададен го подаваме — иначе
                    # оставяме Whisper да разпознае сам от аудиото.
                    if language:
                        api_kwargs["language"] = language
                    if use_vocabulary_hint:
                        api_kwargs["prompt"] = build_hint(model)

                    response = client.audio.transcriptions.create(**api_kwargs)

                text = response if isinstance(response, str) else response.text
                detected_language = language or "auto"
                return {
                    "text": text,
                    "language": detected_language,
                    "model": model,
                    "cost_usd": cost,
                    "duration_minutes": round(duration_min, 2),
                }
            except Exception as e:
                last_error = e
                wait = 2 ** attempt  # exponential backoff: 2s, 4s, 8s
                print(f"[transcribe] Опит {attempt}/{max_retries} неуспешен за "
                      f"{path.name}: {e}. Изчакване {wait}s...")
                time.sleep(wait)
    finally:
        if is_temp_file and os.path.exists(transcribe_path):
            os.remove(transcribe_path)

    raise RuntimeError(
        f"Транскрибацията се провали {max_retries} пъти за {file_path}: {last_error}"
    )


def transcribe_file_diarized(file_path: str, normalize: bool = True,
                              max_retries: int = 3) -> dict:
    """
    ЕКСПЕРИМЕНТАЛНО — заместник на whisper-1 (модел 2), пробен опит.
    Ако не даде добри резултати на практика, ТУК Е ЕДИНСТВЕНОТО МЯСТО,
    което трябва да се премахне (плюс извикването му в test_single_call.py
    и webhook_server.py) — при връщане на 2 модела вместо 3.

    Транскрибира чрез 'gpt-4o-transcribe-diarize' — нов OpenAI модел с
    ВГРАДЕНО разпознаване на говорители (връща структурирани сегменти с
    'speaker' етикет — 'A', 'B' и т.н., не 'Служител'/'Клиент' — самата
    роля пак се определя от Claude при съгласуването, но с готови
    граници между говорителите, вместо да гадае от нулата).

    ВАЖНИ ОГРАНИЧЕНИЯ на този модел (за разлика от другите):
      - НЕ поддържа 'prompt' параметър — vocabulary_hints.py НЕ важи тук.
      - Изисква chunking_strategy за записи >30 сек — подаваме 'auto'.
      - Докладвани реални проблеми от потребители: пропуснати изречения,
        нестабилно поведение при смесени езици. Пробваме на практика.

    Връща СЪЩИЯ формат dict като transcribe_file(), плюс extra поле
    'diarization_available'=True, за информация.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Аудио файлът не съществува: {file_path}")

    duration_min = get_audio_duration_minutes(str(path))
    cost = round(duration_min * WHISPER_PRICE_PER_MINUTE_USD, 6)  # горе-долу същата цена

    transcribe_path = normalize_audio(str(path)) if normalize else str(path)
    is_temp_file = normalize and transcribe_path != str(path)

    last_error = None
    try:
        for attempt in range(1, max_retries + 1):
            try:
                with open(transcribe_path, "rb") as audio_file:
                    response = client.audio.transcriptions.create(
                        model="gpt-4o-transcribe-diarize",
                        file=audio_file,
                        response_format="diarized_json",
                        chunking_strategy="auto",
                        # БЕЗ prompt - моделът не го поддържа.
                    )

                segments = getattr(response, "segments", None) or response.get("segments", [])
                text_parts = []
                for seg in segments:
                    speaker = seg.get("speaker", "?") if isinstance(seg, dict) else getattr(seg, "speaker", "?")
                    seg_text = seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", "")
                    text_parts.append(f"Говорител {speaker}: {seg_text}")
                text = "\n".join(text_parts)

                return {
                    "text": text,
                    "language": "auto",
                    "model": "gpt-4o-transcribe-diarize",
                    "cost_usd": cost,
                    "duration_minutes": round(duration_min, 2),
                    "diarization_available": True,
                }
            except Exception as e:
                last_error = e
                wait = 2 ** attempt
                print(f"[transcribe] Опит {attempt}/{max_retries} неуспешен (diarize) за "
                      f"{path.name}: {e}. Изчакване {wait}s...")
                time.sleep(wait)
    finally:
        if is_temp_file and os.path.exists(transcribe_path):
            os.remove(transcribe_path)

    raise RuntimeError(
        f"Диаризираната транскрибация се провали {max_retries} пъти за {file_path}: {last_error}"
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Употреба: python transcribe.py <път_до_аудио_файл>")
        sys.exit(1)
    result = transcribe_file(sys.argv[1])
    print(f"Език: {result['language']} | Модел: {result['model']} | "
          f"Цена: ${result['cost_usd']}")
    print("---")
    print(result["text"])
