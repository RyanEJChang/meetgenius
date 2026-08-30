import os
from datetime import datetime

import psycopg2
import psycopg2.extras
from flask import g

SCHEMA = """
-- 會議主表
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'uploaded',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    summary_generated_at TIMESTAMP,
    duration REAL,
    num_speakers INTEGER,
    language TEXT,
    supplementary_file_path TEXT,
    srt_path TEXT,
    summary_id INTEGER,
    speaker_highlights TEXT
);

-- 全域摘要表
CREATE TABLE IF NOT EXISTS global_summaries (
    id SERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    summary TEXT,
    action_items TEXT, -- 儲存為 JSON 陣列
    keywords TEXT, -- 儲存為 JSON 陣列
    faq TEXT, -- 儲存為 JSON 陣列
    decisions TEXT, -- 會議決議（含被排除的選項），JSON 陣列
    positions TEXT, -- 各方立場與是否達成共識，JSON 陣列
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

-- 既有資料庫補上新欄位
ALTER TABLE global_summaries ADD COLUMN IF NOT EXISTS decisions TEXT;
ALTER TABLE global_summaries ADD COLUMN IF NOT EXISTS positions TEXT;

-- 會議表對全域摘要表的外鍵（分開建立，避免建表順序問題）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'meetings_summary_id_fkey'
    ) THEN
        ALTER TABLE meetings
            ADD CONSTRAINT meetings_summary_id_fkey
            FOREIGN KEY (summary_id) REFERENCES global_summaries(id);
    END IF;
END $$;

-- 發言人自訂名稱表
CREATE TABLE IF NOT EXISTS speaker_names (
    id SERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    original_speaker_id TEXT NOT NULL,
    custom_name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(meeting_id, original_speaker_id),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

-- 翻譯任務狀態表
CREATE TABLE IF NOT EXISTS translation_jobs (
    id SERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    target_language TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing', -- 'processing', 'completed', 'error'
    error_message TEXT,
    progress_current INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,
    UNIQUE(meeting_id, target_language)
);

-- 用於儲存與特定會議相關的補充摘要資訊
-- 註：原 SQLite 版本此表沒有 UNIQUE(meeting_id)，導致 INSERT OR REPLACE 從未真正觸發過，
-- 每次呼叫都會疊加新的一列。這裡補上 UNIQUE 約束以支援 ON CONFLICT 語意。
CREATE TABLE IF NOT EXISTS supplementary_summaries (
    id SERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL UNIQUE,
    summary TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

-- 翻譯快取表：儲存摘要的翻譯結果
CREATE TABLE IF NOT EXISTS translation_summaries (
    id SERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    target_language TEXT NOT NULL,
    summary TEXT,
    action_items TEXT, -- JSON 陣列的翻譯
    keywords TEXT, -- JSON 陣列的翻譯
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(meeting_id, target_language),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

-- 翻譯快取表：儲存逐字稿的翻譯結果
CREATE TABLE IF NOT EXISTS translation_transcripts (
    id SERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    target_language TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    translated_content TEXT NOT NULL,
    original_speaker TEXT,
    start_time REAL,
    end_time REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(meeting_id, target_language, segment_index),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
"""


def get_db_config():
    """從環境變數讀取本地 Postgres 連線設定"""
    return {
        "host": os.getenv("MEETGENIUS_DB_HOST", "localhost"),
        "port": os.getenv("MEETGENIUS_DB_PORT", "5432"),
        "dbname": os.getenv("MEETGENIUS_DB_NAME", "meetgenius"),
        "user": os.getenv("MEETGENIUS_DB_USER", "meetgenius"),
        "password": os.getenv("MEETGENIUS_DB_PASSWORD", ""),
    }


def get_db():
    """獲取資料庫連線（每個 request context 重用同一條連線）"""
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = psycopg2.connect(
            cursor_factory=psycopg2.extras.RealDictCursor,
            **get_db_config(),
        )
    return db


def close_db(e=None):
    """關閉資料庫連線"""
    db = g.pop("_database", None)
    if db is not None:
        db.close()


def init_app(app):
    """在 Flask app 中註冊資料庫關閉函式，並確保 schema 已建立"""
    app.teardown_appcontext(close_db)
    init_schema()


def init_schema():
    """以程式化方式初始化資料庫 schema（僅需在啟動時執行一次，具備冪等性）"""
    conn = psycopg2.connect(**get_db_config())
    try:
        with conn.cursor() as cursor:
            cursor.execute(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def add_meeting(meeting_id, filename, original_filename, supplementary_file_path=None):
    """新增一筆新的會議記錄到資料庫。"""
    sql = "INSERT INTO meetings (id, filename, original_filename, status, supplementary_file_path) VALUES (%s, %s, %s, %s, %s)"
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id, filename, original_filename, "uploaded", supplementary_file_path))
    conn.commit()


def get_meeting_by_id(meeting_id):
    """透過 ID 獲取一筆會議記錄，並包含從 SRT 檔案解析的逐字稿和發言人名稱。"""
    import json
    from pathlib import Path
    from flask import current_app
    from app.additional.meetgenius.utils.subtitle_processing import parse_srt_content

    sql_meeting = """
        SELECT m.*, gs.summary, gs.action_items, gs.keywords, gs.faq,
               gs.decisions, gs.positions
        FROM meetings m
        LEFT JOIN global_summaries gs ON m.summary_id = gs.id
        WHERE m.id = %s
    """
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql_meeting, (meeting_id,))
        meeting_row = cursor.fetchone()

    if not meeting_row:
        return None

    meeting = dict(meeting_row)

    if meeting.get("summary"):
        def _load(key):
            raw = meeting.get(key)
            if not raw:
                return []
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                print(f"[WARNING] 無法解析會議 {meeting_id} 的 {key}")
                return []

        meeting["global_summary"] = {
            "summary": meeting["summary"],
            "action_items": _load("action_items"),
            "keywords": _load("keywords"),
            "faq": _load("faq"),
            "decisions": _load("decisions"),
            "positions": _load("positions"),
        }
    else:
        meeting["global_summary"] = None

    meeting["speaker_names"] = get_speaker_names(meeting_id)

    meeting["transcript"] = []
    srt_path_key = "srt_path"
    if meeting.get(srt_path_key):
        try:
            base_dir = Path(current_app.root_path).parent
            file_path = base_dir / meeting[srt_path_key]

            if file_path.exists():
                srt_content = file_path.read_text(encoding="utf-8")
                parsed_transcript = parse_srt_content(srt_content, meeting["speaker_names"])
                meeting["transcript"] = parsed_transcript
            else:
                print(f"[WARNING] SRT file not found for meeting {meeting_id} at path: {file_path}")
        except Exception as e:
            print(f"[ERROR] Failed to read or parse SRT file for meeting {meeting_id}: {e}")

    if meeting.get("speaker_highlights"):
        try:
            meeting["speaker_highlights"] = json.loads(meeting["speaker_highlights"])
        except (json.JSONDecodeError, TypeError):
            print(f"[WARNING] 無法解析會議 {meeting_id} 的 speaker_highlights JSON")
            meeting["speaker_highlights"] = []

    return meeting


def get_meeting_metadata_by_id(meeting_id):
    """僅獲取會議的元數據，不包含關聯的摘要或逐字稿。"""
    sql = "SELECT * FROM meetings WHERE id = %s"
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id,))
        meeting_row = cursor.fetchone()
    return dict(meeting_row) if meeting_row else None


def get_all_meetings():
    """獲取所有會議記錄，依建立時間降序排列。"""
    sql = "SELECT * FROM meetings ORDER BY created_at DESC"
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def update_meeting_status(meeting_id, status, **kwargs):
    """更新會議的狀態和其他詳細資訊。"""
    conn = get_db()

    update_fields = ["status = %s"]
    params = [status]

    allowed_keys = [
        "processed_at", "duration", "num_speakers", "srt_path",
        "error_message", "summary_id", "language",
    ]

    for key, value in kwargs.items():
        if key in allowed_keys and value is not None:
            update_fields.append(f"{key} = %s")
            params.append(value)

    params.append(meeting_id)

    sql = f"UPDATE meetings SET {', '.join(update_fields)} WHERE id = %s"
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()
    except Exception as e:
        print(f"Error updating meeting status for {meeting_id}: {e}")
        conn.rollback()


def delete_meeting_by_id(meeting_id):
    """根據 ID 刪除會議記錄。"""
    sql = "DELETE FROM meetings WHERE id = %s"
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id,))
        deleted = cursor.rowcount > 0
    conn.commit()
    return deleted


def create_global_summary(meeting_id, summary, action_items, keywords, faq,
                          decisions=None, positions=None):
    """在資料庫中創建一筆新的全域摘要記錄。"""
    import json
    conn = get_db()

    action_items_json = json.dumps(action_items, ensure_ascii=False)
    keywords_json = json.dumps(keywords, ensure_ascii=False)
    faq_json = json.dumps(faq, ensure_ascii=False)
    decisions_json = json.dumps(decisions or [], ensure_ascii=False)
    positions_json = json.dumps(positions or [], ensure_ascii=False)

    sql = """
        INSERT INTO global_summaries
            (meeting_id, summary, action_items, keywords, faq, decisions, positions)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (meeting_id, summary, action_items_json, keywords_json,
                                 faq_json, decisions_json, positions_json))
            summary_id = cursor.fetchone()["id"]

            update_sql = "UPDATE meetings SET summary_id = %s WHERE id = %s"
            cursor.execute(update_sql, (summary_id, meeting_id))
        conn.commit()
        return summary_id
    except Exception as e:
        print(f"[ERROR] Error creating global summary for meeting {meeting_id}: {e}")
        conn.rollback()
        return None


def save_meeting_summary(meeting_id, summary_data: dict):
    """將會議的 AI 摘要儲存到資料庫（正規化結構）。"""
    import json

    conn = get_db()

    print(f"[DEBUG] 正在為會議 {meeting_id} 創建全域摘要...")
    with conn.cursor() as cursor:
        cursor.execute("SELECT summary_id FROM meetings WHERE id = %s", (meeting_id,))
        row = cursor.fetchone()
        summary_id = row["summary_id"] if row else None

        summary_text = summary_data.get("summary")
        action_items_json = json.dumps(summary_data.get("action_items", []), ensure_ascii=False)
        keywords_json = json.dumps(summary_data.get("keywords", []), ensure_ascii=False)
        faq_json = json.dumps(summary_data.get("faq", []), ensure_ascii=False)
        decisions_json = json.dumps(summary_data.get("decisions", []), ensure_ascii=False)
        positions_json = json.dumps(summary_data.get("positions", []), ensure_ascii=False)

        if summary_id:
            print(f"[DEBUG] 全域摘要已存在 (ID: {summary_id})，正在更新...")
            sql = """
                UPDATE global_summaries
                SET summary = %s, action_items = %s, keywords = %s, faq = %s,
                    decisions = %s, positions = %s
                WHERE id = %s
            """
            cursor.execute(sql, (summary_text, action_items_json, keywords_json, faq_json,
                                 decisions_json, positions_json, summary_id))
        else:
            sql = """
                INSERT INTO global_summaries
                    (meeting_id, summary, action_items, keywords, faq, decisions, positions)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """
            cursor.execute(sql, (meeting_id, summary_text, action_items_json, keywords_json,
                                 faq_json, decisions_json, positions_json))
            summary_id = cursor.fetchone()["id"]
            print(f"[DEBUG] 全域摘要創建成功 (ID: {summary_id})，正在更新 meetings 表...")

            cursor.execute("UPDATE meetings SET summary_id = %s WHERE id = %s", (summary_id, meeting_id))

    conn.commit()
    return summary_id


def get_meeting_summary(meeting_id):
    """透過會議 ID 獲取其摘要、待辦事項和關鍵字。"""
    import json

    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("SELECT summary_id FROM meetings WHERE id = %s", (meeting_id,))
        meeting_row = cursor.fetchone()

        if not meeting_row or not meeting_row["summary_id"]:
            return None

        summary_id = meeting_row["summary_id"]

        cursor.execute(
            """SELECT summary, action_items, keywords, faq, decisions, positions
               FROM global_summaries WHERE id = %s""",
            (summary_id,),
        )
        summary_row = cursor.fetchone()

    if not summary_row:
        return None

    summary_data = {"summary": summary_row["summary"]}

    for key in ("action_items", "keywords", "faq", "decisions", "positions"):
        try:
            summary_data[key] = json.loads(summary_row[key]) if summary_row[key] else []
        except (json.JSONDecodeError, TypeError):
            print(f"警告：無法解析會議 {meeting_id} 的 {key}。")
            summary_data[key] = []

    return summary_data


def get_speaker_names(meeting_id):
    """獲取指定會議的發言人自訂名稱映射。"""
    sql = "SELECT original_speaker_id, custom_name FROM speaker_names WHERE meeting_id = %s"
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id,))
        result = cursor.fetchall()
    return {row["original_speaker_id"]: row["custom_name"] for row in result}


def update_speaker_name(meeting_id, original_speaker_id, custom_name):
    """更新或新增發言者名稱"""
    conn = get_db()
    sql = """
        INSERT INTO speaker_names (meeting_id, original_speaker_id, custom_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (meeting_id, original_speaker_id)
        DO UPDATE SET custom_name = EXCLUDED.custom_name
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id, original_speaker_id, custom_name))
    conn.commit()


def delete_speaker_name(meeting_id, original_speaker_id):
    """刪除發言者名稱映射，恢復原始名稱"""
    conn = get_db()
    sql = "DELETE FROM speaker_names WHERE meeting_id = %s AND original_speaker_id = %s"
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id, original_speaker_id))
    conn.commit()


def update_supplementary_summary(meeting_id, summary):
    """更新輔助文件摘要"""
    sql = """
        INSERT INTO supplementary_summaries (meeting_id, summary)
        VALUES (%s, %s)
        ON CONFLICT (meeting_id)
        DO UPDATE SET summary = EXCLUDED.summary
    """
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id, summary))
    conn.commit()


# ===============================================
# 翻譯相關資料庫操作函數
# ===============================================

def save_translation_summary(meeting_id, target_language, summary, action_items, keywords):
    """儲存翻譯後的摘要到快取"""
    import json

    conn = get_db()
    sql = """
        INSERT INTO translation_summaries
        (meeting_id, target_language, summary, action_items, keywords)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (meeting_id, target_language)
        DO UPDATE SET summary = EXCLUDED.summary,
                      action_items = EXCLUDED.action_items,
                      keywords = EXCLUDED.keywords
    """

    action_items_json = json.dumps(action_items, ensure_ascii=False) if action_items else None
    keywords_json = json.dumps(keywords, ensure_ascii=False) if keywords else None

    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id, target_language, summary, action_items_json, keywords_json))
    conn.commit()
    print(f"[DEBUG] 已儲存 {target_language} 翻譯摘要到快取 (會議ID: {meeting_id})")


def get_translation_summary(meeting_id, target_language):
    """獲取翻譯後的摘要（從快取）"""
    import json

    sql = """
        SELECT summary, action_items, keywords, created_at
        FROM translation_summaries
        WHERE meeting_id = %s AND target_language = %s
    """

    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id, target_language))
        row = cursor.fetchone()

    if row:
        return {
            "summary": row["summary"],
            "action_items": json.loads(row["action_items"]) if row["action_items"] else [],
            "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
            "created_at": row["created_at"],
        }

    return None


def save_translation_transcript(meeting_id, target_language, segments):
    """批量儲存翻譯後的逐字稿到快取"""
    conn = get_db()

    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM translation_transcripts WHERE meeting_id = %s AND target_language = %s",
            (meeting_id, target_language),
        )

        insert_sql = """
            INSERT INTO translation_transcripts
            (meeting_id, target_language, segment_index, translated_content, original_speaker, start_time, end_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """

        for i, segment in enumerate(segments):
            cursor.execute(insert_sql, (
                meeting_id,
                target_language,
                i,
                segment["translated_content"],
                segment.get("speaker", ""),
                segment.get("start_time", 0),
                segment.get("end_time", 0),
            ))

    conn.commit()
    print(f"[DEBUG] 已儲存 {len(segments)} 個 {target_language} 翻譯逐字稿段落到快取 (會議ID: {meeting_id})")


def get_translation_transcript(meeting_id, target_language):
    """獲取翻譯後的逐字稿（從快取）"""
    sql = """
        SELECT segment_index, translated_content, original_speaker, start_time, end_time
        FROM translation_transcripts
        WHERE meeting_id = %s AND target_language = %s
        ORDER BY segment_index
    """

    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id, target_language))
        rows = cursor.fetchall()

    if rows:
        segments = []
        for row in rows:
            segments.append({
                "translated_content": row["translated_content"],
                "speaker": row["original_speaker"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
            })
        return segments

    return None


def get_available_translations(meeting_id):
    """獲取指定會議所有可用的翻譯語言列表。"""
    sql = "SELECT DISTINCT target_language FROM translation_jobs WHERE meeting_id = %s AND status = 'completed'"
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (meeting_id,))
        return [row["target_language"] for row in cursor.fetchall()]


def delete_translation_cache(meeting_id, target_language=None):
    """刪除指定會議的翻譯快取。如果未指定語言，則刪除所有語言的快取。"""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            if target_language:
                cursor.execute("DELETE FROM translation_summaries WHERE meeting_id = %s AND target_language = %s", (meeting_id, target_language))
                cursor.execute("DELETE FROM translation_transcripts WHERE meeting_id = %s AND target_language = %s", (meeting_id, target_language))
                cursor.execute("DELETE FROM translation_jobs WHERE meeting_id = %s AND target_language = %s", (meeting_id, target_language))
            else:
                cursor.execute("DELETE FROM translation_summaries WHERE meeting_id = %s", (meeting_id,))
                cursor.execute("DELETE FROM translation_transcripts WHERE meeting_id = %s", (meeting_id,))
                cursor.execute("DELETE FROM translation_jobs WHERE meeting_id = %s", (meeting_id,))

        conn.commit()
        return True, "快取已成功刪除"
    except Exception as e:
        conn.rollback()
        return False, f"刪除快取失敗: {e}"


def get_or_create_translation_job(meeting_id, target_language):
    """
    獲取或創建一個翻譯任務。
    如果現有任務，則返回它。
    如果不存在，則創建一個新的 'processing' 狀態的任務。
    返回一個元組 (job_dict, created_boolean)。
    """
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM translation_jobs WHERE meeting_id = %s AND target_language = %s",
            (meeting_id, target_language),
        )
        job = cursor.fetchone()

        if job:
            return dict(job), False

        try:
            sql = """
                INSERT INTO translation_jobs (meeting_id, target_language, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
            """
            current_time = datetime.now()
            cursor.execute(sql, (meeting_id, target_language, "processing", current_time, current_time))
            new_job = dict(cursor.fetchone())
            conn.commit()
            return new_job, True
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            cursor.execute(
                "SELECT * FROM translation_jobs WHERE meeting_id = %s AND target_language = %s",
                (meeting_id, target_language),
            )
            job = cursor.fetchone()
            return (dict(job), False) if job else (None, False)


def update_translation_job_status(meeting_id, target_language, status, error_message=None):
    """更新翻譯任務的狀態。"""
    sql = """
        UPDATE translation_jobs
        SET status = %s, error_message = %s, updated_at = %s
        WHERE meeting_id = %s AND target_language = %s
    """
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (status, error_message, datetime.now(), meeting_id, target_language))
    conn.commit()


def update_translation_job_progress(meeting_id, target_language, current, total):
    """更新翻譯任務的進度。"""
    sql = """
        UPDATE translation_jobs
        SET progress_current = %s, progress_total = %s, updated_at = %s
        WHERE meeting_id = %s AND target_language = %s
    """
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (current, total, datetime.now(), meeting_id, target_language))
    conn.commit()


def get_translation_job(meeting_id, target_language):
    """根據會議 ID 和目標語言獲取翻譯任務。"""
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM translation_jobs WHERE meeting_id = %s AND target_language = %s",
            (meeting_id, target_language),
        )
        job = cursor.fetchone()
    return dict(job) if job else None


def update_meeting_speaker_highlights(meeting_id, speaker_highlights_json):
    """更新會議的發言人重點"""
    sql = """
        UPDATE meetings
        SET speaker_highlights = %s, summary_generated_at = %s
        WHERE id = %s
    """
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(sql, (speaker_highlights_json, datetime.now(), meeting_id))
    conn.commit()
