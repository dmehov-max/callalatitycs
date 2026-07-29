# Call Analysis System

Автоматизиран pipeline: сваля разговори от телефонна централа → транскрибира
→ анализира по зададени критерии → флагва слабите → пълни база данни →
изпраща дневен отчет.

## Архитектура

```
[PBX Connector] → [db: calls] → [transcribe.py: Whisper] → [db: transcripts]
                                        ↓
                            [analyze.py: Claude] → [db: analyses, criteria_scores]
                                        ↓
                              [alert.py: Telegram отчет]
```

Всичко в проекта е **независимо от конкретната телефонна централа**,
освен `connectors/`. Това е нарочно — щом знаем коя централа използва
клиентът, пишем нов файл `connectors/xxx_connector.py`, който имплементира
`PBXConnector` интерфейса от `connectors/base.py`, и сменяме 2 реда в
`main.py`. Нищо друго не се пипа.

# Call Analysis System

Автоматизиран pipeline: Callflow PBX известява при всеки завършен разговор
(webhook) → сваля записа → транскрибира → анализира по зададени критерии
(вкл. извлича името на служителя от самия разговор) → флагва слабите →
пълни база данни → вижда се в dashboard + Excel export.

## Архитектура (актуална, базирана на Callflow/Omnilinx API v2.0.0)

```
Callflow PBX --webhook (endcall, ANSWER)--> webhook_server.py
                                                  ↓
                                    db.insert_call() [status=pending]
                                                  ↓ (фонова нишка)
                        connectors/callflow_client.py: сваля записа (Метод 4)
                                                  ↓
                                transcribe.py: Whisper транскрибация
                                                  ↓
                    analyze.py: Claude анализ (ИЗВЛИЧА agent_name от текста!)
                                                  ↓
                                          db.insert_analysis()
                                                  ↓
                    dashboard.py (таблица, търсене, обобщение, известия, Excel)
```

**Важна архитектурна особеност:** Callflow няма "pull" API (не можем да
попитаме "дай ми разговорите за дата Х"). Вместо това те ни известяват
(push/webhook) при всяко събитие на разговор. Затова системата не е cron
job, а **постоянно работещ сървър** (`webhook_server.py`), който чака
известия 24/7.

**Извличане на служител:** Callflow API не дава ясна връзка extension→име
(няколко служители могат да вдигат на един номер, а един служител — от
няколко номера). Затова Claude извлича името директно от транскрипта
(къде служителят се представя). Ограничение: вариации в изписването
("Мария" vs "Мария Иванова") ще се третират като различни хора в
обобщението — вижте бележката в dashboard-а.

## Файлове

| Файл | Отговорност |
|---|---|
| `db.py` | Схема и достъп до SQLite базата |
| `transcribe.py` | Транскрибация чрез OpenAI (Whisper/gpt-4o-mini-transcribe) |
| `criteria_config.py` | **ТУК СЕ РЕДАКТИРАТ КРИТЕРИИТЕ** за оценка + прагове за алармиране |
| `analyze.py` | Claude анализ — оценки по критерий + извличане на agent_name от транскрипта |
| `export_excel.py` | Генерира `call_analysis_report.xlsx` (обобщение по служител + детайл) |
| `connectors/callflow_client.py` | Callflow API клиент (Метод 4 — сваляне на запис) |
| `webhook_server.py` | **Постоянно работещ** сървър, приема Callflow известия в реално време |
| `dashboard.py` | Уеб dashboard — вход, таблица с търсене, обобщение, известия, Excel бутон |
| `main.py` | Помощен скрипт за batch тест/reprocessing с локални файлове (не е основният път) |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # попълни API ключовете + Callflow credentials + DASHBOARD_PASSWORD
export $(cat .env | xargs)

# Стартиране на webhook сървъра (приема известия от Callflow):
python webhook_server.py          # локално, порт 8000

# Стартиране на dashboard-а (за преглед от клиента):
python dashboard.py               # локално, порт 8001
```

**За production:** и двата процеса трябва да работят постоянно (не еднократно) —
чрез `systemd` service или `gunicorn`/`supervisor`, зад nginx с HTTPS
(Callflow изисква публично достъпен HTTPS адрес за webhook-а).

Endpoint, който се регистрира в Callflow настройките:
```
https://<домейн>/webhook/callflow
```

## Преди да пуснете в реална работа

1. **Критериите в `criteria_config.py` са ПРИМЕРНИ** — трябва финализиране с клиента.
2. **Callflow credentials** (`CALLFLOW_USERNAME/PASSWORD/CODE/CLIENTID`) — от клиента, след като получи API достъпа.
3. **Публичен HTTPS домейн** — нужен е сървър с валиден SSL сертификат (Let's Encrypt е безплатен вариант), на който Callflow да изпраща webhook известията.
4. **DASHBOARD_PASSWORD** — смени от `.env.example` стойността по подразбиране.
5. Цените на моделите (в `transcribe.py`/`analyze.py`) са към юли 2026 — проверете преди production.

## Разходи (приблизително, при 120 мин/ден разговори)

- Транскрибация: ~$22/месец
- Анализ (Claude Sonnet): ~$8-12/месец
- **Общо AI токени: ~30-70€/месец** (плаща се директно от клиента с неговите ключове)
- Отделно: PBX API такса към Callflow + малък VPS хостинг за webhook_server.py + dashboard.py
 (приблизително, при 120 мин/ден разговори)

- Транскрибация: ~$22/месец
- Анализ (Claude Sonnet): ~$8-12/месец
- **Общо токени: ~30-70€/месец**, отделно от таксата на централата за API достъп.
