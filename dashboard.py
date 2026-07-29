"""
dashboard.py — Уеб dashboard с 1 общ логин за преглед на анализираните разговори.

Страници:
  /login              — вход с парола
  /                    — главна таблица с търсене (дата, служител, статус)
  /agents              — обобщение по служител
  /phone-numbers       — дневна проверка за пропуснати клиенти
  /reports             — история + генериране на анализи за подобрение (ръчно)
  /notifications       — списък с непрочетени флагнати разговори
  /export/excel        — генерира и сваля актуалния Excel отчет

Auth: сесийна бисквитка, 1 споделена парола (DASHBOARD_PASSWORD в .env).
Достатъчно за "клиентът сам си го ползва" сценария, за който се разбрахме.
Ако по-късно потрябват отделни логини на служители, тук се разширява.
"""

import os
import functools
from pathlib import Path

from flask import (
    Flask, request, session, redirect, url_for, render_template_string, send_file, flash
)

import db
from criteria_config import CRITERIA
from export_excel import generate_report
from improvement_report import generate_improvement_report

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "dev-secret-change-me")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


LOGIN_TEMPLATE = """
<!doctype html>
<html lang="bg"><head><meta charset="utf-8"><title>Вход</title>
<style>
body { font-family: Arial, sans-serif; background: #f2f4f7; display: flex;
       align-items: center; justify-content: center; height: 100vh; margin: 0; }
.box { background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 300px; }
h1 { font-size: 20px; margin-top: 0; }
input { width: 100%; padding: 10px; margin: 8px 0; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; }
button { width: 100%; padding: 10px; background: #2F5496; color: white; border: none; border-radius: 4px; cursor: pointer; }
.error { color: #c00; font-size: 14px; }
</style></head>
<body>
<div class="box">
  <h1>Анализ на разговори</h1>
  <form method="post">
    <input type="password" name="password" placeholder="Парола" autofocus required>
    <button type="submit">Вход</button>
  </form>
  {% with messages = get_flashed_messages() %}
    {% if messages %}<p class="error">{{ messages[0] }}</p>{% endif %}
  {% endwith %}
</div>
</body></html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        flash("Грешна парола.")
    return render_template_string(LOGIN_TEMPLATE)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Общ layout (streamlit-подобно е излишно тук — vanilla HTML е достатъчно)
# --------------------------------------------------------------------------

BASE_STYLE = """
<style>
body { font-family: Arial, sans-serif; background: #fff5f5; margin: 0; color: #2a2a2a; }
nav { background: #C41E1E; padding: 10px 24px; display: flex; align-items: center; gap: 20px; box-shadow: 0 2px 6px rgba(0,0,0,0.15); }
nav img.logo { height: 42px; width: auto; display: block; }
nav a { color: white; text-decoration: none; font-size: 15px; }
nav a:hover { text-decoration: underline; }
.badge { background: #ffffff; color: #C41E1E; border-radius: 12px; padding: 2px 9px; font-size: 12px; margin-left: 4px; font-weight: bold; }
.container { padding: 24px; max-width: 1400px; margin: 0 auto; }
table { border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #f0d5d5; }
th, td { padding: 8px 12px; border-bottom: 1px solid #f5e6e6; text-align: left; font-size: 14px; }
th { background: #C41E1E; color: white; position: sticky; top: 0; }
tr.flagged { background: #ffe0e0; }
tr.ok { background: #fff0f0; }
.filters { margin-bottom: 16px; background: white; padding: 14px; border-radius: 6px; border: 1px solid #f0d5d5; }
.filters input, .filters select { padding: 6px; margin-right: 10px; border: 1px solid #e0b8b8; border-radius: 4px; }
.filters button { padding: 6px 14px; background: #C41E1E; color: white; border: none; border-radius: 4px; cursor: pointer; }
.filters button:hover { background: #a51717; }
.btn-download { display: inline-block; padding: 8px 16px; background: #C41E1E; color: white;
                 text-decoration: none; border-radius: 4px; font-size: 14px; }
h1 { font-size: 22px; color: #C41E1E; }
.score { font-weight: bold; color: #C41E1E; }
.phone-group { background: white; border: 1px solid #f0d5d5; border-radius: 8px; margin-bottom: 16px; overflow: hidden; }
.phone-group-header { background: #C41E1E; color: white; padding: 10px 16px; font-weight: bold; display: flex; justify-content: space-between; align-items: center; }
.call-item { padding: 10px 16px; border-bottom: 1px solid #f5e6e6; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.call-item:last-child { border-bottom: none; }
.call-item a { color: #C41E1E; font-weight: bold; text-decoration: none; }
.call-item a:hover { text-decoration: underline; }
.mini-badge { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: #f5e6e6; color: #555; }
</style>
"""

NAV_TEMPLATE = """
<nav>
  <img class="logo" src="{{ url_for('static', filename='logo.png') }}" alt="Корект">
  <a href="{{ url_for('index') }}">📋 Разговори</a>
  <a href="{{ url_for('agents') }}">👥 Служители</a>
  <a href="{{ url_for('customers') }}">🔢 Клиенти</a>
  <a href="{{ url_for('phone_numbers') }}">📞 Проверка за деня</a>
  <a href="{{ url_for('reports') }}">📊 Анализи</a>
  <a href="{{ url_for('notifications') }}">🔔 Известия
    {% if unread > 0 %}<span class="badge">{{ unread }}</span>{% endif %}
  </a>
  <a href="{{ url_for('export_excel_route') }}" style="margin-left:auto">⬇️ Свали Excel</a>
  <a href="{{ url_for('logout') }}">Изход</a>
</nav>
"""


# --------------------------------------------------------------------------
# Главна таблица
# --------------------------------------------------------------------------

CATEGORY_LABELS = {
    "нова_поръчка": "Нова поръчка",
    "потвърждение": "Потвърждение",
    "нерелевантен": "Нерелевантен",
    "друго": "Друго",
}

CALL_TYPE_LABELS = {
    "бърза_оферта": "Бърза оферта",
    "пълна_консултация": "Пълна консултация",
}

INDEX_TEMPLATE = BASE_STYLE + NAV_TEMPLATE + """
<div class="container">
  <h1>Разговори</h1>
  <form class="filters" method="get">
    <input type="date" name="date_from" value="{{ request.args.get('date_from', '') }}" placeholder="От дата">
    <input type="date" name="date_to" value="{{ request.args.get('date_to', '') }}" placeholder="До дата">
    <input type="text" name="agent" value="{{ request.args.get('agent', '') }}" placeholder="Търси по служител">
    <select name="status">
      <option value="">Всички статуси</option>
      <option value="flagged" {% if request.args.get('status') == 'flagged' %}selected{% endif %}>Само флагнати</option>
      <option value="ok" {% if request.args.get('status') == 'ok' %}selected{% endif %}>Само успешни</option>
    </select>
    <select name="category">
      <option value="">Всички категории</option>
      <option value="нова_поръчка" {% if request.args.get('category') == 'нова_поръчка' %}selected{% endif %}>Нова поръчка</option>
      <option value="потвърждение" {% if request.args.get('category') == 'потвърждение' %}selected{% endif %}>Потвърждение</option>
      <option value="нерелевантен" {% if request.args.get('category') == 'нерелевантен' %}selected{% endif %}>Нерелевантен</option>
      <option value="друго" {% if request.args.get('category') == 'друго' %}selected{% endif %}>Друго</option>
    </select>
    <button type="submit">Филтрирай</button>
  </form>
  <p style="color:#666; font-size: 13px;">
    Типът разговор ("Бърза оферта" / "Пълна консултация") определя кои
    критерии реално участват в общата оценка.
  </p>

  <table>
    <tr>
      <th>ID на разговор</th><th>Дата</th><th>Служител</th><th>Спомената 'Корект'</th><th>Категория</th><th>Тип разговор</th>
      <th>Продължителност</th><th>Посока</th>
      <th>Статус</th><th>Резултат</th><th>Обобщение</th>
    </tr>
    {% for c in calls %}
    <tr class="{{ 'flagged' if c['is_flagged'] else ('ok' if c['call_category'] != 'нерелевантен' else '') }}">
      <td><a href="{{ url_for('call_detail', call_id=c['call_id']) }}">{{ c['external_call_id'] }}</a>
        {% if c['is_flagged'] %}<span title="Флагнат разговор - препоръчва се преслушване">⚠️</span>{% endif %}
      </td>
      <td>{{ c['call_date'] }}</td>
      <td>{{ c['agent_name'] }}</td>
      <td>{{ '✅ Да' if c['korekt_mentioned'] else '❌ Не' }}</td>
      <td>{{ category_labels.get(c['call_category'], c['call_category']) }}</td>
      <td>{{ call_type_labels.get(c['call_type'], c['call_type']) }}</td>
      <td>{{ (c['duration_seconds'] // 60) }} мин {{ c['duration_seconds'] % 60 }} сек</td>
      <td>{{ c['direction'] }}</td>
      <td>{{ 'Нерелевантен' if c['call_category'] == 'нерелевантен' else ('Флагнат' if c['is_flagged'] else 'Успешен') }}</td>
      <td class="score">{{ c['overall_score'] }}/10</td>
      <td>{{ c['overall_summary'] }}</td>
    </tr>
    {% endfor %}
  </table>
  {% if not calls %}<p>Няма разговори, отговарящи на филтъра.</p>{% endif %}
</div>
"""


@app.route("/")
@login_required
def index():
    calls = db.get_calls_filtered(
        date_from=request.args.get("date_from") or None,
        date_to=request.args.get("date_to") or None,
        agent_query=request.args.get("agent") or None,
        status=request.args.get("status") or None,
        category=request.args.get("category") or None,
    )
    unread = db.get_unacknowledged_flagged_count()
    return render_template_string(INDEX_TEMPLATE, calls=calls, unread=unread,
                                    request=request, category_labels=CATEGORY_LABELS,
                                    call_type_labels=CALL_TYPE_LABELS)


# --------------------------------------------------------------------------
# Обобщение по служител
# --------------------------------------------------------------------------

AGENTS_TEMPLATE = BASE_STYLE + NAV_TEMPLATE + """
<div class="container">
  <h1>Служители — обобщение</h1>
  <p style="color:#666; font-size: 13px;">
    Сортирано по среден резултат (най-слаб пръв). Имената се извличат
    автоматично от разговорите — вариации в изписването могат да разделят
    един и същ служител на няколко реда.
  </p>
  <table>
    <tr>
      <th>Служител</th><th>Брой разговори</th><th>Среден резултат</th>
      <th>Флагнати</th><th>% флагнати</th>
      {% for c in criteria %}<th>{{ c.label }}</th>{% endfor %}
    </tr>
    {% for a in agents %}
    <tr class="{{ 'flagged' if a.avg_score < 5 else ('ok' if a.avg_score >= 8 else '') }}">
      <td>{{ a.agent_name }}</td>
      <td>{{ a.total_calls }}</td>
      <td class="score">{{ a.avg_score }}</td>
      <td>{{ a.flagged_count }}</td>
      <td>{{ a.flagged_pct }}%</td>
      {% for c in criteria %}<td>{{ a.criteria_avg.get(c.key, '-') }}</td>{% endfor %}
    </tr>
    {% endfor %}
  </table>
</div>
"""


@app.route("/agents")
@login_required
def agents():
    agent_data = db.get_agent_summary()
    unread = db.get_unacknowledged_flagged_count()
    return render_template_string(AGENTS_TEMPLATE, agents=agent_data, criteria=CRITERIA, unread=unread)


# --------------------------------------------------------------------------
# Клиенти — номерирана листа по телефонен номер, с вложени разговори
# --------------------------------------------------------------------------

CUSTOMERS_TEMPLATE = BASE_STYLE + NAV_TEMPLATE + """
<div class="container">
  <h1>Клиенти</h1>
  <p style="color:#666; font-size: 13px;">
    Всеки номер получава пореден номер при първата си поява. Под него —
    всички разговори с този номер (входящи, изходящи, пропуснати), подредени
    по време. Индикаторите "Корект" и "Откъде научили" са чисто информационни.
  </p>

  {% for pn in phone_numbers %}
  <div class="phone-group">
    <div class="phone-group-header">
      <span>№ {{ pn.phone_number_id }} — {{ pn.phone_number }}</span>
      <span class="mini-badge">{{ pn.calls|length }} разговор{{ 'а' if pn.calls|length != 1 else '' }}</span>
    </div>
    {% for c in pn.calls %}
    <div class="call-item">
      <strong>Разговор {{ loop.index }}</strong>
      <span>{{ c['call_date'] }}</span>
      <span class="mini-badge">{{ 'Входящ' if c['direction'] == 'IN' else 'Изходящ' }}</span>
      {% if c['status'] == 'missed' %}
        <span class="mini-badge" style="background:#ffd7d7;">Пропуснато обаждане</span>
      {% else %}
        <span class="mini-badge">{{ '✅ Корект' if c['korekt_mentioned'] else '❌ Корект' }}</span>
        <span class="mini-badge">{{ '✅ Питано откъде' if c['referral_source_asked'] else '❌ Питано откъде' }}</span>
        <span class="mini-badge">👤 {{ c['agent_name'] or 'Неизвестен' }}</span>
        {% if c['overall_score'] is not none %}
        <span class="score">{{ c['overall_score'] }}/10</span>
        {% endif %}
        {% if c['is_flagged'] %}<span class="mini-badge" style="background:#ffd7d7;">⚠️ Флагнат</span>{% endif %}
        <a href="{{ url_for('call_detail', call_id=c['call_id']) }}">Виж детайли &rarr;</a>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  {% endfor %}
  {% if not phone_numbers %}<p>Няма разговори още.</p>{% endif %}
</div>
"""


@app.route("/customers")
@login_required
def customers():
    phone_numbers_data = db.get_phone_numbers_with_calls()
    unread = db.get_unacknowledged_flagged_count()
    return render_template_string(CUSTOMERS_TEMPLATE, phone_numbers=phone_numbers_data, unread=unread)


# --------------------------------------------------------------------------
# Детайлна страница на разговор — транскрипт, оценки, аудио плейър
# --------------------------------------------------------------------------

CALL_DETAIL_TEMPLATE = BASE_STYLE + NAV_TEMPLATE + """
<div class="container">
  <p><a href="{{ url_for('customers') }}">&larr; Обратно към клиентите</a></p>
  <h1>Разговор {{ call['external_call_id'] }}</h1>
  <p style="color:#666;">{{ call['call_date'] }} | {{ 'Входящ' if call['direction'] == 'IN' else 'Изходящ' }}</p>

  {% if call['audio_file_path'] %}
  <div style="background:white; padding:14px; border-radius:8px; border:1px solid #f0d5d5; margin-bottom:16px;">
    <audio controls style="width:100%;">
      <source src="{{ url_for('call_audio', call_id=call['id']) }}">
      Браузърът не поддържа аудио плейър.
    </audio>
  </div>
  {% endif %}

  {% if analysis %}
  <div style="background:white; padding:20px; border-radius:8px; border:1px solid #f0d5d5; margin-bottom:16px;">
    <p><strong>Служител:</strong> {{ call['agent_name'] or 'Неизвестен' }} (увереност: {{ analysis['agent_name_confidence'] }})</p>
    <p><strong>Категория:</strong> {{ analysis['call_category'] }} — {{ analysis['category_reasoning'] }}</p>
    <p><strong>Тип разговор:</strong> {{ analysis['call_type'] }} — {{ analysis['call_type_reasoning'] }}</p>
    <p><strong>Спомената 'Корект':</strong> {{ '✅ Да' if analysis['korekt_mentioned'] else '❌ Не' }}</p>
    <p><strong>Питано откъде е научено:</strong> {{ '✅ Да' if analysis['referral_source_asked'] else '❌ Не' }}</p>
    <p><strong>Услуга:</strong> {{ analysis['service_line'] }}</p>
    <p><strong>Изисква изходящо обаждане:</strong> {{ '🔴 Да' if analysis['followup_call_required'] else '✅ Не' }}</p>
    <p><strong>Общ резултат:</strong> <span class="score">{{ analysis['overall_score'] }}/10</span>
       {% if analysis['is_flagged'] %} — ⚠️ ФЛАГНАТ: {{ analysis['flag_reason'] }}{% endif %}</p>
    <p><strong>Обобщение:</strong> {{ analysis['overall_summary'] }}</p>
  </div>

  <h3>Диаризиран транскрипт</h3>
  <div style="background:white; padding:16px; border-radius:8px; border:1px solid #f0d5d5; white-space: pre-wrap; margin-bottom:16px;">{{ analysis['diarized_transcript'] }}</div>

  <h3>Оценки по критерий</h3>
  <table>
    <tr><th>Критерий</th><th>Оценка</th><th>Обосновка</th></tr>
    {% for c in criteria %}
    <tr>
      <td>{{ c['criterion_label'] }}</td>
      <td class="score">{{ c['score'] if c['applicable'] else 'N/A' }}</td>
      <td>{{ c['justification'] }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>Този разговор няма анализ (вероятно е пропуснато обаждане без запис).</p>
  {% endif %}
</div>
"""


@app.route("/call/<int:call_id>")
@login_required
def call_detail(call_id):
    detail = db.get_call_detail(call_id)
    if not detail:
        flash("Разговорът не е намерен.")
        return redirect(url_for("customers"))
    unread = db.get_unacknowledged_flagged_count()
    return render_template_string(CALL_DETAIL_TEMPLATE, call=detail["call"],
                                    analysis=detail["analysis"], criteria=detail["criteria"],
                                    unread=unread)


@app.route("/call/<int:call_id>/audio")
@login_required
def call_audio(call_id):
    detail = db.get_call_detail(call_id)
    if not detail or not detail["call"]["audio_file_path"]:
        return "Няма запис.", 404
    audio_path = detail["call"]["audio_file_path"]
    if not os.path.exists(audio_path):
        return "Файлът не е намерен на диска.", 404
    return send_file(audio_path)


# --------------------------------------------------------------------------
# Телефонни номера — дневна проверка "липсва последващо обаждане"
# --------------------------------------------------------------------------

PHONE_NUMBERS_TEMPLATE = BASE_STYLE + NAV_TEMPLATE + """
<div class="container">
  <h1>Телефонни номера — дневна проверка</h1>
  <form class="filters" method="get">
    <input type="date" name="date" value="{{ selected_date }}">
    <button type="submit">Покажи</button>
  </form>
  <p style="color:#666; font-size: 13px;">
    За всеки номер с входящ (вдигнат или пропуснат) разговор през деня,
    проверяваме дали има поне един изходящ разговор към него СЪЩИЯ ден
    (до 00:00). Липсващите последващи обаждания са маркирани червено и
    показани най-отгоре — това са потенциално изгубени клиенти.
  </p>

  <table>
    <tr>
      <th>ID</th><th>Телефонен номер</th><th>Входящи</th>
      <th>Пропуснати</th><th>Изходящи</th><th>Статус</th>
    </tr>
    {% for r in report %}
    <tr class="{{ 'flagged' if not r.has_followup else 'ok' }}">
      <td>{{ r.phone_number_id }}</td>
      <td>{{ r.phone_number }}</td>
      <td>{{ r.inbound_count }}</td>
      <td>{{ r.missed_count }}</td>
      <td>{{ r.outbound_count }}</td>
      <td>{{ '🔴 Липсва последващо обаждане' if not r.has_followup else '✅ OK' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% if not report %}<p>Няма входящи разговори за тази дата.</p>{% endif %}
</div>
"""


@app.route("/phone-numbers")
@login_required
def phone_numbers():
    from datetime import date as date_cls
    selected_date = request.args.get("date") or date_cls.today().isoformat()
    report = db.get_daily_followup_report(selected_date)
    unread = db.get_unacknowledged_flagged_count()
    return render_template_string(PHONE_NUMBERS_TEMPLATE, report=report,
                                    selected_date=selected_date, unread=unread)


# --------------------------------------------------------------------------
# Анализи за подобрение (ръчно генерирани, история + преглед + триене)
# --------------------------------------------------------------------------

REPORTS_TEMPLATE = BASE_STYLE + NAV_TEMPLATE + """
<div class="container">
  <h1>Анализи за подобрение</h1>

  <div style="background:#f5f5f5; padding:16px; border-radius:8px; margin-bottom:20px;">
    <h3 style="margin-top:0;">Генерирай нов анализ</h3>
    <form method="post" action="{{ url_for('generate_report_route') }}">
      <div class="filters">
        <label>От дата: <input type="date" name="date_from" required></label>
        <label>До дата: <input type="date" name="date_to" required></label>
        <select name="agent_filter">
          <option value="">Всички служители</option>
          {% for name in agent_names %}
          <option value="{{ name }}">{{ name }}</option>
          {% endfor %}
        </select>
        <button type="submit">Генерирай анализ</button>
      </div>
    </form>
    <p style="color:#666; font-size: 13px; margin-bottom:0;">
      Генерирането отнема няколко секунди и струва малка сума (Anthropic API) —
      затова е ръчно, не автоматично.
    </p>
  </div>

  <h3>История</h3>
  <table>
    <tr>
      <th>Заглавие</th><th>Създаден на</th><th>Действия</th>
    </tr>
    {% for r in reports %}
    <tr>
      <td>
        <a href="{{ url_for('view_report', report_id=r['id']) }}">
          Анализ за подобрения от {{ r['date_from'] }} до {{ r['date_to'] }}
          {% if r['agent_filter'] %} на {{ r['agent_filter'] }}{% else %} (всички служители){% endif %}
        </a>
      </td>
      <td>{{ r['created_at'] }}</td>
      <td>
        <form method="post" action="{{ url_for('delete_report_route', report_id=r['id']) }}"
              onsubmit="return confirm('Сигурни ли сте, че искате да изтриете този анализ?');" style="display:inline;">
          <button type="submit" style="background:#c0392b; color:white; border:none; border-radius:4px; padding:4px 10px; cursor:pointer;">
            Изтрий
          </button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% if not reports %}<p>Няма генерирани анализи още.</p>{% endif %}
</div>
"""

REPORT_DETAIL_TEMPLATE = BASE_STYLE + NAV_TEMPLATE + """
<div class="container">
  <p><a href="{{ url_for('reports') }}">&larr; Обратно към анализите</a></p>
  <h1>
    Анализ за подобрения от {{ report['date_from'] }} до {{ report['date_to'] }}
    {% if report['agent_filter'] %} на {{ report['agent_filter'] }}{% else %} (всички служители){% endif %}
  </h1>
  <p style="color:#666; font-size: 13px;">
    Генериран на {{ report['created_at'] }} | Модел: {{ report['model'] }} |
    Цена: ${{ '%.4f'|format(report['cost_usd']) }}
  </p>
  <div style="background:white; padding:20px; border-radius:8px; border:1px solid #ddd; white-space: pre-wrap; line-height: 1.6;">{{ report['report_text'] }}</div>
</div>
"""


@app.route("/reports")
@login_required
def reports():
    all_reports = db.get_all_reports()
    unread = db.get_unacknowledged_flagged_count()
    agent_names = db.get_distinct_agent_names()
    return render_template_string(REPORTS_TEMPLATE, reports=all_reports, unread=unread,
                                    agent_names=agent_names)


@app.route("/reports/generate", methods=["POST"])
@login_required
def generate_report_route():
    date_from = request.form.get("date_from")
    date_to = request.form.get("date_to")
    agent_filter = request.form.get("agent_filter") or None

    if not date_from or not date_to:
        flash("Трябва да зададеш начална и крайна дата.")
        return redirect(url_for("reports"))

    stats = db.get_period_summary_stats(date_from, date_to, agent_name=agent_filter)
    result = generate_improvement_report(stats)

    import json as json_module
    report_id = db.insert_report(
        date_from, date_to, agent_filter,
        result["report_text"], json_module.dumps(stats, ensure_ascii=False),
        result["model"], result["cost_usd"],
    )
    return redirect(url_for("view_report", report_id=report_id))


@app.route("/reports/<int:report_id>")
@login_required
def view_report(report_id):
    report = db.get_report(report_id)
    if not report:
        flash("Анализът не е намерен (вероятно вече е изтрит).")
        return redirect(url_for("reports"))
    unread = db.get_unacknowledged_flagged_count()
    return render_template_string(REPORT_DETAIL_TEMPLATE, report=report, unread=unread)


@app.route("/reports/<int:report_id>/delete", methods=["POST"])
@login_required
def delete_report_route(report_id):
    db.delete_report(report_id)
    return redirect(url_for("reports"))


# --------------------------------------------------------------------------
# Известия (замества премахнатия Telegram alerting)
# --------------------------------------------------------------------------

NOTIFICATIONS_TEMPLATE = BASE_STYLE + NAV_TEMPLATE + """
<div class="container">
  <h1>Непрочетени флагнати разговори</h1>
  {% if calls %}
  <form method="post" action="{{ url_for('acknowledge_notifications') }}">
    <button type="submit" style="margin-bottom:14px; padding:8px 14px; background:#555; color:white; border:none; border-radius:4px; cursor:pointer;">
      Маркирай всички като прочетени
    </button>
  </form>
  {% endif %}
  <table>
    <tr><th>Дата</th><th>Служител</th><th>Резултат</th><th>Причина за флага</th></tr>
    {% for c in calls %}
    <tr class="flagged">
      <td>{{ c['call_date'] }}</td>
      <td>{{ c['agent_name'] }}</td>
      <td class="score">{{ c['overall_score'] }}/10</td>
      <td>{{ c['flag_reason'] }}</td>
    </tr>
    {% endfor %}
  </table>
  {% if not calls %}<p>Няма непрочетени известия. 🎉</p>{% endif %}
</div>
"""


@app.route("/notifications")
@login_required
def notifications():
    calls = db.get_unacknowledged_flagged_calls()
    unread = db.get_unacknowledged_flagged_count()
    return render_template_string(NOTIFICATIONS_TEMPLATE, calls=calls, unread=unread)


@app.route("/notifications/acknowledge", methods=["POST"])
@login_required
def acknowledge_notifications():
    db.acknowledge_all_flagged()
    return redirect(url_for("notifications"))


# --------------------------------------------------------------------------
# Excel export
# --------------------------------------------------------------------------

@app.route("/export/excel")
@login_required
def export_excel_route():
    path = generate_report()
    if path is None:
        return "Няма данни за export.", 404
    return send_file(path, as_attachment=True, download_name="call_analysis_report.xlsx")


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("DASHBOARD_PORT", 8001))
    app.run(host="0.0.0.0", port=port)
