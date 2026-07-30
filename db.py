"""
db.py — Схема и достъп до базата данни за анализ на разговори.

Използва SQLite за проста локална употреба. За production с много
конкурентни писания (напр. cron + web dashboard едновременно),
смени connection string-а с Postgres (psycopg2) — схемата е
съвместима с малки промени в типовете (SERIAL вместо AUTOINCREMENT).
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "calls.db"

SCHEMA = """
-- Телефонни номера — всеки уникален номер получава пореден ID при
-- първата си поява. Всички разговори (вх./изх./пропуснати) с този
-- номер се свързват към него.
CREATE TABLE IF NOT EXISTS phone_numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number TEXT UNIQUE NOT NULL,
    first_seen_date TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Основна таблица: един ред = един разговор (вкл. пропуснати - без аудио)
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_call_id TEXT UNIQUE,
    phone_number_id INTEGER REFERENCES phone_numbers(id),
    call_date TEXT NOT NULL,
    call_datetime TEXT,
    duration_seconds INTEGER,
    agent_name TEXT,
    agent_extension TEXT,
    customer_number TEXT,
    direction TEXT,
    audio_file_path TEXT,
    audio_source_url TEXT,
    status TEXT NOT NULL DEFAULT 'downloaded',
        -- downloaded -> transcribed -> analyzed -> failed
        -- 'missed' -> пропуснат разговор, без аудио/анализ
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Транскрипти
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(id),
    transcript_text TEXT NOT NULL,
    language TEXT,
    transcription_model TEXT,
    transcription_cost_usd REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Анализ: общ ред на разговор (обобщение + краен статус)
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(id),
    korekt_mentioned INTEGER,
    referral_source_asked INTEGER,
    service_line TEXT,
    followup_call_required INTEGER,
    call_category TEXT,
    call_type TEXT,
    call_type_reasoning TEXT,
    category_reasoning TEXT,
    diarized_transcript TEXT,
    agent_name_confidence TEXT,
    overall_summary TEXT,
    overall_score REAL,
    is_flagged INTEGER DEFAULT 0,
    flag_reason TEXT,
    acknowledged INTEGER DEFAULT 0,
    analysis_model TEXT,
    analysis_cost_usd REAL,
    raw_response_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Оценки по отделен критерий (1 разговор -> N реда, по един на критерий)
CREATE TABLE IF NOT EXISTS criteria_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    criterion_key TEXT NOT NULL,
    criterion_label TEXT NOT NULL,
    score INTEGER NOT NULL,
    justification TEXT,
    applicable INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_calls_date ON calls(call_date);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_phone_number ON calls(phone_number_id);
CREATE INDEX IF NOT EXISTS idx_analyses_flagged ON analyses(is_flagged);

-- Генерирани анализи за подобрение
CREATE TABLE IF NOT EXISTS analysis_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    agent_filter TEXT,
    report_text TEXT NOT NULL,
    stats_json TEXT,
    model TEXT,
    cost_usd REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_exists(conn, table, column):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def _migrate_schema(conn):
    """
    Добавя колони нужни за reconciliation (виж reconcile.py), без да трие
    съществуващи данни. SQLite няма 'ADD COLUMN IF NOT EXISTS', затова
    проверяваме ръчно преди всяка ALTER TABLE.
    """
    if not _column_exists(conn, "calls", "updated_at"):
        conn.execute("ALTER TABLE calls ADD COLUMN updated_at TEXT DEFAULT CURRENT_TIMESTAMP")
    if not _column_exists(conn, "calls", "retry_count"):
        conn.execute("ALTER TABLE calls ADD COLUMN retry_count INTEGER DEFAULT 0")
    if not _column_exists(conn, "calls", "last_error"):
        conn.execute("ALTER TABLE calls ADD COLUMN last_error TEXT")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
    print(f"[db] Схемата е инициализирана в {DB_PATH}")


def get_or_create_phone_number(phone_number: str, call_date: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id FROM phone_numbers WHERE phone_number = ?",
            (phone_number,),
        )
        existing = cur.fetchone()
        if existing:
            return existing["id"]

        cur = conn.execute(
            "INSERT INTO phone_numbers (phone_number, first_seen_date) VALUES (?, ?)",
            (phone_number, call_date),
        )
        return cur.lastrowid


def is_first_contact(phone_number_id: int, call_datetime: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM calls
            WHERE phone_number_id = ? AND call_datetime < ?
            """,
            (phone_number_id, call_datetime),
        )
        return cur.fetchone()["cnt"] == 0


def insert_call(call: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id FROM calls WHERE external_call_id = ?",
            (call["external_call_id"],),
        )
        existing = cur.fetchone()
        if existing:
            return existing["id"]

        phone_number_id = None
        if call.get("customer_number"):
            phone_number_id = get_or_create_phone_number(
                call["customer_number"], call["call_date"]
            )

        cur = conn.execute(
            """
            INSERT INTO calls (
                external_call_id, phone_number_id, call_date, call_datetime,
                duration_seconds, agent_extension, customer_number,
                direction, audio_file_path, audio_source_url, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'downloaded')
            """,
            (
                call["external_call_id"],
                phone_number_id,
                call["call_date"],
                call.get("call_datetime"),
                call.get("duration_seconds"),
                call.get("agent_extension"),
                call.get("customer_number"),
                call.get("direction"),
                call.get("audio_file_path"),
                call.get("audio_source_url"),
            ),
        )
        return cur.lastrowid


def insert_missed_call(call: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id FROM calls WHERE external_call_id = ?",
            (call["external_call_id"],),
        )
        existing = cur.fetchone()
        if existing:
            return existing["id"]

        phone_number_id = None
        if call.get("customer_number"):
            phone_number_id = get_or_create_phone_number(
                call["customer_number"], call["call_date"]
            )

        cur = conn.execute(
            """
            INSERT INTO calls (
                external_call_id, phone_number_id, call_date, call_datetime,
                duration_seconds, agent_extension, customer_number,
                direction, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'missed')
            """,
            (
                call["external_call_id"],
                phone_number_id,
                call["call_date"],
                call.get("call_datetime"),
                call.get("duration_seconds", 0),
                call.get("agent_extension"),
                call.get("customer_number"),
                call.get("direction"),
            ),
        )
        return cur.lastrowid


def insert_transcript(call_id: int, text: str, language: str,
                       model: str, cost_usd: float):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO transcripts (call_id, transcript_text, language,
                                      transcription_model, transcription_cost_usd)
            VALUES (?, ?, ?, ?, ?)
            """,
            (call_id, text, language, model, cost_usd),
        )
        conn.execute(
            "UPDATE calls SET status = 'transcribed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (call_id,),
        )


def insert_analysis(call_id: int, agent_name: str, agent_name_confidence: str,
                     korekt_mentioned: bool, referral_source_asked: bool, service_line: str,
                     followup_call_required: bool,
                     call_category: str, category_reasoning: str,
                     call_type: str, call_type_reasoning: str, diarized_transcript: str,
                     summary: str, overall_score: float,
                     is_flagged: bool, flag_reason: str, model: str,
                     cost_usd: float, raw_response: dict,
                     criteria_scores: list[dict]) -> int:
    with get_conn() as conn:
        conn.execute(
            "UPDATE calls SET agent_name = ? WHERE id = ?",
            (agent_name, call_id),
        )

        cur = conn.execute(
            """
            INSERT INTO analyses (call_id, agent_name_confidence, korekt_mentioned,
                                   referral_source_asked, service_line, followup_call_required,
                                   call_category, category_reasoning, call_type,
                                   call_type_reasoning, diarized_transcript, overall_summary,
                                   overall_score, is_flagged, flag_reason, analysis_model,
                                   analysis_cost_usd, raw_response_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (call_id, agent_name_confidence, int(korekt_mentioned),
             int(referral_source_asked), service_line, int(followup_call_required),
             call_category, category_reasoning,
             call_type, call_type_reasoning, diarized_transcript, summary,
             overall_score, int(is_flagged), flag_reason, model, cost_usd,
             json.dumps(raw_response, ensure_ascii=False)),
        )
        analysis_id = cur.lastrowid

        for c in criteria_scores:
            conn.execute(
                """
                INSERT INTO criteria_scores (analysis_id, criterion_key,
                                              criterion_label, score, justification, applicable)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (analysis_id, c["key"], c["label"], c["score"],
                 c.get("justification", ""), int(c.get("applicable", True))),
            )

        conn.execute(
            "UPDATE calls SET status = 'analyzed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (call_id,),
        )
        return analysis_id


def get_pending_transcription() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM calls WHERE status = 'downloaded'"
        ).fetchall()


def get_pending_analysis() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT c.*, t.transcript_text
            FROM calls c
            JOIN transcripts t ON t.call_id = c.id
            WHERE c.status = 'transcribed'
            """
        ).fetchall()


def get_all_analyzed_calls() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT c.id as call_id, c.external_call_id, c.call_date,
                   c.agent_name, c.duration_seconds, c.direction,
                   a.id as analysis_id, a.overall_score, a.overall_summary,
                   a.is_flagged, a.flag_reason, a.korekt_mentioned, a.call_category, a.category_reasoning, a.call_type, a.call_type_reasoning
            FROM calls c
            JOIN analyses a ON a.call_id = c.id
            ORDER BY c.call_date, c.agent_name
            """
        ).fetchall()


def get_criteria_scores_for_analysis(analysis_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT criterion_key, criterion_label, score, justification, applicable "
            "FROM criteria_scores WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchall()


def get_calls_filtered(date_from: str = None, date_to: str = None,
                        agent_query: str = None, status: str = None,
                        category: str = None) -> list[sqlite3.Row]:
    query = """
        SELECT c.id as call_id, c.external_call_id, c.call_date, c.call_datetime,
               c.agent_name, c.duration_seconds, c.direction, c.customer_number,
               a.id as analysis_id, a.overall_score, a.overall_summary,
               a.is_flagged, a.flag_reason, a.agent_name_confidence,
               a.korekt_mentioned, a.call_category, a.category_reasoning, a.call_type, a.call_type_reasoning
        FROM calls c
        JOIN analyses a ON a.call_id = c.id
        WHERE 1=1
    """
    params = []
    if date_from:
        query += " AND c.call_date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND c.call_date <= ?"
        params.append(date_to)
    if agent_query:
        query += " AND c.agent_name LIKE ?"
        params.append(f"%{agent_query}%")
    if status == "flagged":
        query += " AND a.is_flagged = 1"
    elif status == "ok":
        query += " AND a.is_flagged = 0"
    if category:
        query += " AND a.call_category = ?"
        params.append(category)

    query += " ORDER BY c.call_date DESC, c.call_datetime DESC"

    with get_conn() as conn:
        return conn.execute(query, params).fetchall()


def get_agent_summary() -> list[dict]:
    calls = get_all_analyzed_calls()
    agents = {}
    for call in calls:
        if call["call_category"] == "нерелевантен":
            continue
        if not call["agent_name"] or call["agent_name"] == "Неизвестен служител":
            continue

        name = call["agent_name"]
        if name not in agents:
            agents[name] = {"agent_name": name, "total": 0, "flagged": 0, "score_sum": 0.0,
                             "criteria_sums": {}, "criteria_counts": {}}
        a = agents[name]
        a["total"] += 1
        a["score_sum"] += call["overall_score"] or 0
        if call["is_flagged"]:
            a["flagged"] += 1
        for cs in get_criteria_scores_for_analysis(call["analysis_id"]):
            if not cs["applicable"]:
                continue
            key = cs["criterion_key"]
            a["criteria_sums"][key] = a["criteria_sums"].get(key, 0) + cs["score"]
            a["criteria_counts"][key] = a["criteria_counts"].get(key, 0) + 1

    result = []
    for name, a in agents.items():
        avg_score = round(a["score_sum"] / a["total"], 2) if a["total"] else 0
        pct_flagged = round(100 * a["flagged"] / a["total"], 1) if a["total"] else 0
        criteria_avg = {
            key: round(a["criteria_sums"][key] / a["criteria_counts"][key], 2)
            for key in a["criteria_sums"]
        }
        result.append({
            "agent_name": name,
            "total_calls": a["total"],
            "avg_score": avg_score,
            "flagged_count": a["flagged"],
            "flagged_pct": pct_flagged,
            "criteria_avg": criteria_avg,
        })

    result.sort(key=lambda x: x["avg_score"])
    return result


def get_period_summary_stats(date_from: str, date_to: str, agent_name: str = None) -> dict:
    with get_conn() as conn:
        query = """
            SELECT c.id as call_id, c.agent_name, c.call_date,
                   a.id as analysis_id, a.overall_score, a.is_flagged, a.flag_reason,
                   a.call_category, a.call_type, a.korekt_mentioned, a.referral_source_asked,
                   a.followup_call_required, a.service_line
            FROM calls c
            JOIN analyses a ON a.call_id = c.id
            WHERE c.call_date >= ? AND c.call_date <= ?
        """
        params = [date_from, date_to]
        if agent_name:
            query += " AND c.agent_name = ?"
            params.append(agent_name)
        rows = conn.execute(query, params).fetchall()

    irrelevant_count = sum(1 for r in rows if r["call_category"] == "нерелевантен")
    relevant = [r for r in rows if r["call_category"] != "нерелевантен"]

    total = len(relevant)
    if total == 0:
        return {
            "date_from": date_from, "date_to": date_to, "agent_filter": agent_name,
            "total_calls": 0, "irrelevant_excluded": irrelevant_count,
        }

    avg_score = round(sum(r["overall_score"] or 0 for r in relevant) / total, 2)
    flagged = [r for r in relevant if r["is_flagged"]]

    category_counts = {}
    call_type_counts = {}
    service_line_counts = {}
    for r in relevant:
        category_counts[r["call_category"]] = category_counts.get(r["call_category"], 0) + 1
        call_type_counts[r["call_type"]] = call_type_counts.get(r["call_type"], 0) + 1
        service_line_counts[r["service_line"]] = service_line_counts.get(r["service_line"], 0) + 1

    korekt_yes = sum(1 for r in relevant if r["korekt_mentioned"])
    referral_yes = sum(1 for r in relevant if r["referral_source_asked"])

    criteria_sums = {}
    criteria_counts = {}
    for r in relevant:
        for cs in get_criteria_scores_for_analysis(r["analysis_id"]):
            if not cs["applicable"]:
                continue
            key = cs["criterion_key"]
            criteria_sums[key] = criteria_sums.get(key, 0) + cs["score"]
            criteria_counts[key] = criteria_counts.get(key, 0) + 1
    criteria_avg = {
        key: round(criteria_sums[key] / criteria_counts[key], 2)
        for key in criteria_sums
    }

    missed_followups = 0
    checked_days = 0
    d = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d")
    while d <= end:
        day_str = d.strftime("%Y-%m-%d")
        for entry in get_daily_followup_report(day_str):
            checked_days += 1
            if not entry["has_followup"]:
                missed_followups += 1
        d += timedelta(days=1)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "agent_filter": agent_name,
        "total_calls": total,
        "irrelevant_excluded": irrelevant_count,
        "avg_overall_score": avg_score,
        "flagged_count": len(flagged),
        "flagged_pct": round(100 * len(flagged) / total, 1),
        "flag_reasons": [r["flag_reason"] for r in flagged if r["flag_reason"]],
        "category_counts": category_counts,
        "call_type_counts": call_type_counts,
        "service_line_counts": service_line_counts,
        "korekt_mentioned_pct": round(100 * korekt_yes / total, 1),
        "referral_source_asked_pct": round(100 * referral_yes / total, 1),
        "criteria_avg": criteria_avg,
        "missed_followups_count": missed_followups,
        "phone_numbers_checked_for_followup": checked_days,
    }


def insert_report(date_from: str, date_to: str, agent_filter: str,
                   report_text: str, stats_json: str, model: str, cost_usd: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO analysis_reports (date_from, date_to, agent_filter,
                                           report_text, stats_json, model, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (date_from, date_to, agent_filter, report_text, stats_json, model, cost_usd),
        )
        return cur.lastrowid


def get_all_reports() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, date_from, date_to, agent_filter, model, cost_usd, created_at "
            "FROM analysis_reports ORDER BY created_at DESC"
        ).fetchall()


def get_report(report_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM analysis_reports WHERE id = ?", (report_id,)
        ).fetchone()


def delete_report(report_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM analysis_reports WHERE id = ?", (report_id,))


def get_call_detail(call_id: int) -> dict | None:
    with get_conn() as conn:
        call = conn.execute(
            "SELECT * FROM calls WHERE id = ?", (call_id,)
        ).fetchone()
        if not call:
            return None
        analysis = conn.execute(
            "SELECT * FROM analyses WHERE call_id = ?", (call_id,)
        ).fetchone()
        criteria = []
        if analysis:
            criteria = conn.execute(
                "SELECT * FROM criteria_scores WHERE analysis_id = ?",
                (analysis["id"],),
            ).fetchall()
        return {"call": call, "analysis": analysis, "criteria": criteria}


def get_phone_numbers_with_calls() -> list[dict]:
    with get_conn() as conn:
        numbers = conn.execute(
            "SELECT id, phone_number FROM phone_numbers ORDER BY id"
        ).fetchall()

        result = []
        for pn in numbers:
            calls = conn.execute(
                """
                SELECT c.id as call_id, c.external_call_id, c.call_date, c.call_datetime,
                       c.direction, c.status, c.agent_name,
                       a.id as analysis_id, a.overall_score, a.is_flagged,
                       a.korekt_mentioned, a.referral_source_asked, a.call_category
                FROM calls c
                LEFT JOIN analyses a ON a.call_id = c.id
                WHERE c.phone_number_id = ?
                ORDER BY c.call_datetime
                """,
                (pn["id"],),
            ).fetchall()
            if not calls:
                continue
            result.append({
                "phone_number_id": pn["id"],
                "phone_number": pn["phone_number"],
                "calls": calls,
            })
        return result


def get_distinct_agent_names() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT agent_name FROM calls "
            "WHERE agent_name IS NOT NULL AND agent_name != 'Неизвестен служител' "
            "ORDER BY agent_name"
        ).fetchall()
        return [r["agent_name"] for r in rows]


def get_unacknowledged_flagged_count() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM analyses WHERE is_flagged = 1 AND acknowledged = 0"
        ).fetchone()
        return row["cnt"]


def get_unacknowledged_flagged_calls() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT c.external_call_id, c.call_date, c.agent_name,
                   a.id as analysis_id, a.overall_score, a.flag_reason
            FROM calls c
            JOIN analyses a ON a.call_id = c.id
            WHERE a.is_flagged = 1 AND a.acknowledged = 0
            ORDER BY c.call_date DESC
            """
        ).fetchall()


def acknowledge_all_flagged():
    with get_conn() as conn:
        conn.execute("UPDATE analyses SET acknowledged = 1 WHERE is_flagged = 1")


def get_flagged_calls(call_date: str = None) -> list[sqlite3.Row]:
    query = """
        SELECT c.*, a.overall_score, a.flag_reason, a.overall_summary
        FROM calls c
        JOIN analyses a ON a.call_id = c.id
        WHERE a.is_flagged = 1
    """
    params = ()
    if call_date:
        query += " AND c.call_date = ?"
        params = (call_date,)
    with get_conn() as conn:
        return conn.execute(query, params).fetchall()


def get_daily_followup_report(call_date: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.phone_number_id, pn.phone_number, c.direction, c.status,
                   a.followup_call_required
            FROM calls c
            JOIN phone_numbers pn ON pn.id = c.phone_number_id
            LEFT JOIN analyses a ON a.call_id = c.id
            WHERE c.call_date = ? AND c.phone_number_id IS NOT NULL
            """,
            (call_date,),
        ).fetchall()

    numbers = {}
    for row in rows:
        pid = row["phone_number_id"]
        if pid not in numbers:
            numbers[pid] = {
                "phone_number_id": pid,
                "phone_number": row["phone_number"],
                "inbound_count": 0,
                "missed_count": 0,
                "outbound_count": 0,
                "needs_check": False,
            }
        entry = numbers[pid]
        if row["direction"] == "IN":
            if row["status"] == "missed":
                entry["missed_count"] += 1
                entry["needs_check"] = True
            else:
                entry["inbound_count"] += 1
                if row["followup_call_required"]:
                    entry["needs_check"] = True
        elif row["direction"] == "OUT":
            entry["outbound_count"] += 1

    result = []
    for entry in numbers.values():
        if not entry["needs_check"]:
            continue
        entry["has_followup"] = entry["outbound_count"] > 0
        del entry["needs_check"]
        result.append(entry)

    result.sort(key=lambda x: x["has_followup"])
    return result


def get_call_by_id(call_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()


def get_transcript_text(call_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT transcript_text FROM transcripts WHERE call_id = ? ORDER BY id LIMIT 1",
            (call_id,),
        ).fetchone()
        return row["transcript_text"] if row else None


def set_audio_path(call_id: int, audio_path: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE calls SET audio_file_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (audio_path, call_id),
        )


def mark_call_failed(call_id: int, error_message: str):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE calls
            SET status = 'failed', retry_count = retry_count + 1,
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error_message[:2000], call_id),
        )


def get_stuck_calls(stale_minutes: int = 30, max_retries: int = 3) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM calls
            WHERE retry_count < ?
              AND (
                    (status IN ('downloaded', 'transcribed')
                     AND updated_at <= datetime('now', ?))
                    OR status = 'failed'
              )
            ORDER BY call_datetime
            """,
            (max_retries, f"-{stale_minutes} minutes"),
        ).fetchall()


def get_permanently_failed_calls() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM calls WHERE status = 'failed' AND retry_count >= 3 ORDER BY call_date DESC"
        ).fetchall()


if __name__ == "__main__":
    init_db()
