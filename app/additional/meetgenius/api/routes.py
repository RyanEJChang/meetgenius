import os
import uuid
import io
from pathlib import Path
from threading import Thread

from flask import Response, current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

from app.additional import blueprint as bp
from app.additional.meetgenius import db as meetgenius_db
from app.additional.meetgenius.services.processing import process_meeting
from app.additional.meetgenius.utils.document_parser import read_document_text
from app.additional.meetgenius.utils.export_utils import (
    create_bilingual_transcript_docx,
    create_bilingual_transcript_txt,
)
from app.additional.meetgenius.utils.subtitle_processing import (
    parse_srt_content,
    srt_to_vtt,
    apply_speaker_names_to_srt,
)

# 使用本地 SQLite 資料庫的函數
add_meeting = meetgenius_db.add_meeting
delete_meeting_by_id = meetgenius_db.delete_meeting_by_id
delete_speaker_name = meetgenius_db.delete_speaker_name
get_all_meetings = meetgenius_db.get_all_meetings
get_meeting_by_id = meetgenius_db.get_meeting_by_id
get_meeting_metadata_by_id = meetgenius_db.get_meeting_metadata_by_id
get_meeting_summary = meetgenius_db.get_meeting_summary
get_speaker_names = meetgenius_db.get_speaker_names
get_translation_summary = meetgenius_db.get_translation_summary
get_translation_transcript = meetgenius_db.get_translation_transcript
save_meeting_summary = meetgenius_db.save_meeting_summary
update_speaker_name = meetgenius_db.update_speaker_name
update_supplementary_summary = meetgenius_db.update_supplementary_summary
get_or_create_translation_job = meetgenius_db.get_or_create_translation_job
update_translation_job_status = meetgenius_db.update_translation_job_status
get_translation_job = meetgenius_db.get_translation_job
update_translation_job_progress = meetgenius_db.update_translation_job_progress

# ===============================================
# 會議基本管理 API
# ===============================================

@bp.route('/meetgenius/meeting', methods=['POST'])
def create_meeting():
    """API: 創建新會議並上傳檔案"""
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': '沒有選擇檔案'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': '沒有選擇檔案'}), 400
    
    allowed_extensions = {'.m4a', '.mp3', '.wav', '.flac', '.webm', '.mp4'}
    file_ext = Path(file.filename).suffix.lower()
    
    if file_ext not in allowed_extensions:
        return jsonify({'status': 'error', 'message': '不支援的檔案格式'}), 400
    
    # --- 硬編碼設定 ---
    app_root = Path(current_app.root_path).parent
    uploads_folder = app_root / "app" / "additional" / "meetgenius" / "uploads"
    max_content_length = 500 * 1024 * 1024  # 500 MB
    
    # 確保資料夾存在
    uploads_folder.mkdir(parents=True, exist_ok=True)
    # --- 硬編碼設定結束 ---

    meeting_id = str(uuid.uuid4())
    filename = secure_filename(f"{meeting_id}_{file.filename}")
    file_path = uploads_folder / filename
    
    try:
        # 檢查檔案大小
        file.seek(0, os.SEEK_END)
        file_length = file.tell()
        file.seek(0, 0)
        if file_length > max_content_length:
            return jsonify({'status': 'error', 'message': f'檔案大小超過 {max_content_length // 1024 // 1024}MB 的限制'}), 413
            
        file.save(file_path)
    except Exception as e:
        print(f"[ERROR] 儲存檔案失敗: {e}")
        return jsonify({'status': 'error', 'message': '儲存檔案時發生錯誤'}), 500

    num_speakers = request.form.get('num_speakers')
    if num_speakers and num_speakers.isdigit():
        num_speakers = int(num_speakers)
    else:
        num_speakers = None
    
    add_meeting(meeting_id, filename, file.filename)
    
    # 啟動後台處理
    app = current_app._get_current_object()
    thread = Thread(target=process_meeting, args=(app, meeting_id, str(file_path), num_speakers))
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'success', 'meeting_id': meeting_id})

@bp.route('/meetgenius/meeting/<meeting_id>/status')
def get_meeting_status(meeting_id):
    """API: 獲取會議處理狀態"""
    meeting = get_meeting_by_id(meeting_id)
    if meeting:
        return jsonify({'status': meeting['status'], 'error_message': meeting['error_message']})
    else:
        return jsonify({'status': 'not_found'}), 404

@bp.route('/meetgenius/meeting/<meeting_id>/delete', methods=['DELETE'])
def delete_meeting(meeting_id):
    """API: 刪除指定的會議記錄及其相關檔案"""
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '會議記錄不存在'}), 404

    try:
        # --- 硬編碼設定 ---
        app_root = Path(current_app.root_path).parent
        uploads_folder = app_root / "app" / "additional" / "meetgenius" / "uploads"
        processed_folder = app_root / "app" / "additional" / "meetgenius" / "processed"
        base_dir = app_root
        # --- 硬編碼設定結束 ---
        
        # 刪除上傳的原始檔案
        if meeting['filename']:
            uploaded_file = uploads_folder / meeting['filename']
            if uploaded_file.exists():
                uploaded_file.unlink()
                print(f"[INFO] 已刪除上傳檔案: {uploaded_file}")

        # 刪除處理過程中的檔案（依 processing.py 實際輸出命名）
        # 包含：*_faststart.*, *_16k_mono.opus.ogg, {meeting_id}_playback.mp3 等
        try:
            deleted_any = False
            for file_path in processed_folder.iterdir():
                try:
                    if file_path.is_file() and meeting_id in file_path.name:
                        file_path.unlink()
                        deleted_any = True
                        print(f"[INFO] 已刪除處理檔案: {file_path}")
                except Exception as fe:
                    print(f"[WARNING] 無法刪除處理檔案: {file_path}, 原因: {fe}")
            if not deleted_any:
                print(f"[INFO] 未找到需刪除的處理檔案（processed）: meeting_id={meeting_id}")
        except Exception as e2:
            print(f"[WARNING] 刪除 processed 檔案時發生例外: {e2}")

        # 刪除輸出結果檔案（目前資料庫僅有 srt_path）
        if meeting.get('srt_path'):
            output_file = base_dir / meeting['srt_path']
            if output_file.exists():
                output_file.unlink()
                print(f"[INFO] 已刪除輸出檔案: {output_file}")
        
        delete_meeting_by_id(meeting_id)
        
        print(f"[INFO] 已成功刪除會議記錄: {meeting_id}")
        return jsonify({'status': 'success', 'message': '會議記錄已成功刪除'})
    except Exception as e:
        print(f"[ERROR] 刪除會議時發生未知錯誤 (ID: {meeting_id}): {e}")
        return jsonify({'status': 'error', 'message': '伺服器內部錯誤'}), 500

# ===============================================
# 輔助文件處理 API
# ===============================================

@bp.route('/meetgenius/meeting/<meeting_id>/process-supplement', methods=['POST'])
def process_supplementary_file(meeting_id):
    """API: 為參考文件單獨產生一份摘要，存入 supplementary_summaries。

    注意：目前前端沒有任何地方呼叫此端點，需以 API 手動觸發。
    參考文件在會議處理流程中已另外被使用——見 services/processing.py，
    那裡會把文件內容作為專有名詞與背景脈絡傳給主摘要。
    此端點是額外功能（單獨閱讀文件摘要），若確定不需要即可移除。
    """
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '找不到會議記錄'}), 404

    if not meeting['supplementary_file_path']:
        return jsonify({'status': 'error', 'message': '此會議沒有輔助文件'}), 400

    file_path = meeting['supplementary_file_path']
    if not Path(file_path).exists():
        return jsonify({'status': 'error', 'message': '輔助文件檔案遺失'}), 404

    try:
        # 1. 讀取文件內容
        document_text = read_document_text(file_path)
        if not document_text:
            return jsonify({'status': 'success', 'message': '文件內容為空，無需處理'})

        # 2. 呼叫 OpenAI 產生摘要
        client = current_app.audio_processor.openai_client
        prompt = f"""
        你是一個專業的會議助理。請閱讀以下文件內容，並為其產生一份簡潔的摘要，提煉出最重要的核心重點。
        摘要應包含：
        1.  文件的主要目的或主題。
        2.  關鍵的論點、數據或發現。
        3.  任何重要的結論或建議。

        摘要內容請以條列式呈現，力求精簡扼要，總長度不要超過 200 字。

        文件內容如下：
        ---
        {document_text[:8000]}
        """
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "你是一個專業的會議助理。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
        )
        summary = response.choices[0].message.content

        # 3. 儲存摘要到資料庫
        update_supplementary_summary(meeting_id, summary)

        return jsonify({'status': 'success', 'summary': summary})

    except ValueError as e:
        print(f"[WARNING] 處理輔助文件失敗 (會議 ID: {meeting_id}): {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 400
    except Exception as e:
        print(f"[ERROR] 處理輔助文件時發生未知錯誤 (會議 ID: {meeting_id}): {e}")
        return jsonify({'status': 'error', 'message': '伺服器內部錯誤'}), 500

# ===============================================
# 字幕與逐字稿 API
# ===============================================

def _get_subtitle_content(meeting_id):
    """輔助函式：獲取字幕內容，返回 (內容, 原始檔名) 或 (錯誤JSON, 狀態碼)。"""
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        print(f"[WARNING] API請求：找不到會議記錄: {meeting_id}")
        return jsonify({'error': '會議記錄不存在', 'code': 'MEETING_NOT_FOUND'}), 404
    
    if meeting['status'] != 'completed':
        print(f"[INFO] API請求：會議 {meeting_id} 尚未完成，狀態: {meeting['status']}")
        return jsonify({
            'error': '會議尚未處理完成', 
            'code': 'PROCESSING_NOT_COMPLETE', 
            'current_status': meeting['status']
        }), 202
        
    # 目前流程僅保存 srt_path
    srt_path_str = meeting.get('srt_path')
    if not srt_path_str:
        print(f"[ERROR] API請求：會議 {meeting_id} 已完成但找不到字幕路徑(srt_path)")
        return jsonify({'error': '處理完成但找不到字幕檔路徑', 'code': 'PATH_NOT_FOUND'}), 404
        
    # --- 硬編碼設定 ---
    base_dir = Path(current_app.root_path).parent
    # --- 硬編碼設定結束 ---
    
    subtitle_file = base_dir / srt_path_str
    
    if not subtitle_file.exists():
        print(f"[ERROR] API請求：字幕檔案不存在: {subtitle_file}")
        return jsonify({'error': '字幕檔案遺失', 'code': 'FILE_NOT_FOUND'}), 404

    try:
        content = subtitle_file.read_text(encoding='utf-8')
        return content, meeting.get('original_filename')
    except Exception as e:
        print(f"[ERROR] 讀取字幕檔案時發生錯誤 ({subtitle_file}): {e}")
        return jsonify({'error': '讀取字幕檔案失敗', 'code': 'FILE_READ_ERROR'}), 500

@bp.route('/meetgenius/meeting/<meeting_id>/subtitle')
def get_meeting_subtitle(meeting_id):
    """API: 獲取原始純文字字幕"""
    result = _get_subtitle_content(meeting_id)
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
        content, original_filename = result
        return Response(content, mimetype='text/plain; charset=utf-8', headers={
            'X-Meeting-ID': meeting_id,
            'X-Original-Filename': original_filename
        })
    return result # 這是錯誤回應 (jsonify, status)

@bp.route('/meetgenius/meeting/<meeting_id>/subtitle/formatted')
def get_meeting_subtitle_formatted(meeting_id):
    """API: 獲取格式化為 JSON 的字幕"""
    result = _get_subtitle_content(meeting_id)
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
        content, _ = result
        # 獲取發言者名稱映射
        speaker_names = get_speaker_names(meeting_id)
        segments = parse_srt_content(content, speaker_names)
        return jsonify({
            'meeting_id': meeting_id,
            'total_segments': len(segments),
            'segments': segments
        })
    return result # 這是錯誤回應

@bp.route('/meetgenius/meeting/<meeting_id>/transcription')
def get_meeting_transcription(meeting_id):
    try:
        srt_content, _ = _get_subtitle_content(meeting_id)
        if isinstance(srt_content, Response): # _get_subtitle_content returns Response on error
            return srt_content

        speaker_names = get_speaker_names(meeting_id)
        transcription = _parse_srt_to_transcription(srt_content, speaker_names)
        return jsonify(transcription)
    except Exception as e:
        return jsonify({'error': str(e), 'code': 'TRANSCRIPTION_ERROR'}), 500

def _parse_srt_to_transcription(srt_content, speaker_names=None):
    """
    將 SRT 字幕內容解析為結構化的逐字稿列表。
    如果提供了 speaker_names 映射，則會替換發言人名稱。
    """
    # parse_srt_content 返回一個字典列表，例如:
    # [{'speaker': 'SPEAKER_00', 'content': 'Hello world.', 'start_time': 5.5, ...}]
    parsed_segments = parse_srt_content(srt_content)
    transcription = []
    
    for i, segment_data in enumerate(parsed_segments):
        raw_speaker = segment_data.get('speaker', 'UNKNOWN')
        display_speaker = speaker_names.get(raw_speaker, raw_speaker) if speaker_names else raw_speaker

        transcription.append({
            'id': i,
            'speaker': display_speaker,
            'content': segment_data.get('content', ''),
            'start_time': segment_data.get('start_time'),
            'end_time': segment_data.get('end_time'),
            'start_time_formatted': segment_data.get('start_time_formatted'),
            'end_time_formatted': segment_data.get('end_time_formatted'),
        })
        
    return transcription

# ===============================================
# 發言者管理 API
# ===============================================

@bp.route('/meetgenius/meeting/<meeting_id>/speaker-names', methods=['GET', 'POST'])
def handle_speaker_names_api(meeting_id):
    """API: 獲取或更新發言者名稱"""
    if request.method == 'GET':
        try:
            meeting = get_meeting_metadata_by_id(meeting_id)
            if not meeting:
                return jsonify({'status': 'error', 'message': '找不到會議記錄'}), 404

            # 處理流程目前僅輸出 srt_path
            srt_path_str = meeting.get('srt_path')
            if not srt_path_str:
                return jsonify({
                    'status': 'error',
                    'message': '找不到會議的字幕檔案'
                }), 404

            base_dir = Path(current_app.root_path).parent
            srt_file_path = base_dir / srt_path_str

            if not srt_file_path.exists():
                 return jsonify({
                    'status': 'error',
                    'message': '字幕檔案遺失，無法讀取發言人列表'
                }), 404
            
            # 以同一套解析器取得原始語者 ID，確保鍵值一致（如：發言者01）
            all_speakers = set()
            with srt_file_path.open('r', encoding='utf-8') as f:
                content = f.read()
                segments = parse_srt_content(content)
                for seg in segments:
                    speaker_id = seg.get('original_speaker')
                    if speaker_id and speaker_id != '發言者未識別':
                        all_speakers.add(speaker_id)

            custom_names = get_speaker_names(meeting_id)
            final_speaker_names = {speaker: custom_names.get(speaker, speaker) for speaker in sorted(list(all_speakers))}

            return jsonify({
                'status': 'success',
                'speaker_names': final_speaker_names
            })
        except Exception as e:
            print(f"[ERROR] 獲取發言者名稱時發生錯誤: {e}")
            return jsonify({
                'status': 'error',
                'message': '獲取發言者名稱失敗'
            }), 500

    if request.method == 'POST':
        try:
            data = request.get_json()
            
            if not data or 'original_speaker_id' not in data or 'custom_name' not in data:
                return jsonify({'status': 'error', 'message': '缺少必要參數'}), 400
            
            original_speaker_id = data['original_speaker_id']
            custom_name = data['custom_name'].strip()
            
            if not custom_name:
                return jsonify({'status': 'error', 'message': '發言者名稱不能為空'}), 400
            
            update_speaker_name(meeting_id, original_speaker_id, custom_name)
            
            return jsonify({'status': 'success', 'message': '發言者名稱更新成功'})
        
        except Exception as e:
            print(f"[ERROR] 更新發言者名稱時發生錯誤: {e}")
            return jsonify({'status': 'error', 'message': '更新發言者名稱失敗'}), 500

@bp.route('/meetgenius/meeting/<meeting_id>/speaker-names/<original_speaker_id>', methods=['DELETE'])
def delete_speaker_name_api(meeting_id, original_speaker_id):
    """API: 刪除發言者名稱映射，恢復原始名稱"""
    try:
        delete_speaker_name(meeting_id, original_speaker_id)
        
        return jsonify({
            'status': 'success',
            'message': '發言者名稱已恢復為原始名稱'
        })
    
    except Exception as e:
        print(f"[ERROR] 刪除發言者名稱映射時發生錯誤: {e}")
        return jsonify({
            'status': 'error',
            'message': '恢復原始名稱失敗'
        }), 500

@bp.route('/meetgenius/api/meetings/<meeting_id>', methods=['DELETE'])
def delete_meeting_api(meeting_id):
    """
    API endpoint to delete a meeting and its associated files.
    """
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '會議記錄不存在'}), 404

    try:
        # --- File deletion logic copied from delete_meeting function ---
        app_root = Path(current_app.root_path).parent
        uploads_folder = app_root / "app" / "additional" / "meetgenius" / "uploads"
        processed_folder = app_root / "app" / "additional" / "meetgenius" / "processed"
        base_dir = app_root

        # Delete uploaded original file
        if meeting['filename']:
            uploaded_file = uploads_folder / meeting['filename']
            if uploaded_file.exists():
                uploaded_file.unlink()
                print(f"[INFO] Deleted uploaded file: {uploaded_file}")

        # Delete processed files (match actual outputs per processing.py)
        try:
            deleted_any = False
            for file_path in processed_folder.iterdir():
                try:
                    if file_path.is_file() and meeting_id in file_path.name:
                        file_path.unlink()
                        deleted_any = True
                        print(f"[INFO] Deleted processed file: {file_path}")
                except Exception as fe:
                    print(f"[WARNING] Failed to delete processed file: {file_path}, reason: {fe}")
            if not deleted_any:
                print(f"[INFO] No processed files found to delete: meeting_id={meeting_id}")
        except Exception as e2:
            print(f"[WARNING] Exception while deleting processed files: {e2}")

        # Delete output result files
        for path_key in ['srt_path']:
            if meeting.get(path_key) and meeting[path_key]:
                output_file = base_dir / meeting[path_key]
                if output_file.exists():
                    output_file.unlink()
                    print(f"[INFO] Deleted output file: {output_file}")
        
        # Delete from database
        delete_meeting_by_id(meeting_id)
        
        print(f"[INFO] Successfully deleted meeting record: {meeting_id}")
        return jsonify({'status': 'success', 'message': '會議記錄已成功刪除'}), 200
    except Exception as e:
        print(f"[ERROR] An unknown error occurred while deleting the meeting (ID: {meeting_id}): {e}")
        return jsonify({'status': 'error', 'message': '伺服器內部錯誤'}), 500

@bp.route('/meetgenius/api/meetings/<meeting_id>/subtitles')
def get_subtitles_api(meeting_id):
    """(API) 獲取 VTT 格式的字幕"""
    meeting = get_meeting_by_id(meeting_id)
    if not meeting or not meeting.get('srt_path'):
        return jsonify({'error': 'Subtitles not found'}), 404

    try:
        base_dir = Path(current_app.root_path).parent
        srt_file_path = base_dir / meeting['srt_path']
        
        if not srt_file_path.exists():
            return jsonify({'error': 'Subtitle file does not exist'}), 404

        with srt_file_path.open('r', encoding='utf-8') as f:
            srt_content = f.read()

        vtt_content = srt_to_vtt(srt_content)
        
        return Response(vtt_content, mimetype='text/vtt; charset=utf-8')

    except Exception as e:
        print(f"Error getting subtitles for {meeting_id}: {e}")
        return jsonify({'error': 'Internal server error'}), 500

 

@bp.route('/meetgenius/meeting/<meeting_id>/subtitle.srt')
def download_srt_unified(meeting_id):
    """API: 單一路由自動判斷回傳原始或已改名的 SRT。

    規則：若此會議在資料庫中已有發言者自訂名稱，則回傳改名後 SRT；否則回傳原始 SRT。
    """
    meeting = get_meeting_by_id(meeting_id)
    if not meeting or not meeting.get('srt_path'):
        return jsonify({'status': 'error', 'message': '找不到字幕檔案'}), 404

    try:
        base_dir = Path(current_app.root_path).parent
        srt_file_path = base_dir / meeting['srt_path']
        if not srt_file_path.exists():
            return jsonify({'status': 'error', 'message': '字幕檔案遺失'}), 404

        srt_content = srt_file_path.read_text(encoding='utf-8')
        speaker_names = get_speaker_names(meeting_id)

        if speaker_names:
            srt_content = apply_speaker_names_to_srt(srt_content, speaker_names)
            suffix = 'transcript_speaker'
        else:
            suffix = 'transcript'

        original_stem = Path(meeting.get('original_filename', 'meeting')).stem
        download_name = f"{original_stem}_{suffix}.srt"

        file_stream = io.BytesIO(srt_content.encode('utf-8'))
        file_stream.seek(0)

        return send_file(
            file_stream,
            as_attachment=True,
            download_name=download_name,
            mimetype='text/plain; charset=utf-8'
        )
    except Exception as e:
        print(f"[ERROR] 下載統一 SRT 路由時發生錯誤: {e}")
        return jsonify({'status': 'error', 'message': '伺服器內部錯誤'}), 500

@bp.route('/meetgenius/api/meetings/<meeting_id>/transcript')
def get_transcript_api(meeting_id):
    """API: 獲取會議逐字稿"""
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '會議記錄不存在'}), 404
    
    if meeting['status'] != 'completed':
        return jsonify({'status': 'error', 'message': '會議尚未處理完成'}), 400
    
    if not meeting.get('transcript'):
        return jsonify({'status': 'error', 'message': '逐字稿不存在'}), 404
    
    return jsonify({
        'status': 'success',
        'meeting_id': meeting_id,
        'transcript': meeting['transcript'],
        'speaker_names': meeting.get('speaker_names', {})
    })

# ===============================================
# 翻譯功能 API
# ===============================================

def _run_translation_job(app, meeting_id, target_language, force_retranslate):
    """背景執行完整的翻譯任務（摘要 + 逐字稿）"""
    with app.app_context():
        # 使用 list 來包裹數字，使其在閉包中可變
        progress_counter = [0]
        total_items = [0]

        try:
            from app.additional.meetgenius.db import (
                get_meeting_by_id,
                save_translation_summary,
                save_translation_transcript,
                update_translation_job_status,
                update_translation_job_progress,
            )
            from app.additional.meetgenius.services.translation import (
                TranslationError,
                get_translation_service,
            )

            print(f"[BG_JOB] 開始翻譯任務: 會議 {meeting_id}, 語言 {target_language}")
            
            meeting = get_meeting_by_id(meeting_id)
            if not meeting:
                raise ValueError("找不到會議記錄")
            
            # 計算總翻譯項目數
            if meeting.get('global_summary'):
                if meeting['global_summary'].get('summary'):
                    total_items[0] += 1
                total_items[0] += len(meeting['global_summary'].get('action_items', []))
                total_items[0] += len(meeting['global_summary'].get('keywords', []))
            if meeting.get('transcript'):
                total_items[0] += len(meeting.get('transcript', []))

            # 定義進度回呼函式
            def progress_callback():
                progress_counter[0] += 1
                print(f"[BG_JOB] 進度更新: {progress_counter[0]}/{total_items[0]}")
                update_translation_job_progress(
                    meeting_id, target_language,
                    current=progress_counter[0],
                    total=total_items[0]
                )

            translation_service = get_translation_service()
            if not translation_service.is_available():
                raise TranslationError("翻譯服務無法使用")

            # 翻譯摘要
            if meeting.get('global_summary'):
                print("[BG_JOB] 正在翻譯摘要...")
                translated_summary = translation_service.translate_meeting_summary(
                    meeting['global_summary'], target_language, progress_callback
                )
                save_translation_summary(
                    meeting_id, target_language,
                    translated_summary.get('summary', ''),
                    translated_summary.get('action_items', []),
                    translated_summary.get('keywords', [])
                )
                print("[BG_JOB] 摘要翻譯完成並儲存。")

            # 翻譯逐字稿
            if meeting.get('transcript'):
                print(f"[BG_JOB] 正在翻譯逐字稿 (共 {len(meeting['transcript'])} 段)...")
                translated_segments = translation_service.translate_transcript_segments(
                    meeting['transcript'], target_language, progress_callback
                )
                save_translation_transcript(meeting_id, target_language, translated_segments)
                print("[BG_JOB] 逐字稿翻譯完成並儲存。")
            
            # 更新任務狀態為完成
            update_translation_job_status(meeting_id, target_language, 'completed')
            print(f"[BG_JOB] 翻譯任務成功完成: 會議 {meeting_id}, 語言 {target_language}")

        except Exception as e:
            print(f"[BG_JOB] 翻譯任務失敗: 會議 {meeting_id}, 語言 {target_language}, 錯誤: {e}")
            # 更新任務狀態為錯誤
            update_translation_job_status(meeting_id, target_language, 'error', error_message=str(e))


@bp.route('/meetgenius/meeting/<meeting_id>/translate', methods=['POST'])
def translate_meeting_content(meeting_id):
    """API: 啟動一個翻譯任務"""
    # 檢查會議是否存在
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '會議記錄不存在'}), 404
    
    if meeting['status'] != 'completed':
        return jsonify({'status': 'error', 'message': '會議尚未處理完成，無法翻譯'}), 400
    
    # 獲取請求參數
    data = request.get_json()
    if not data or 'target_language' not in data:
        return jsonify({'status': 'error', 'message': '缺少目標語言參數'}), 400
    
    target_language = data['target_language']
    force_retranslate = data.get('force_retranslate', False)
    
    # 支援的語言列表（與 Fast Transcription locales 一致；中文改用 zh-TW 代表繁體不翻譯）
    supported_languages = ['en-US', 'zh-TW', 'ja-JP', 'ko-KR', 'es-ES', 'fr-FR', 'de-DE', 'ru-RU', 'pt-PT']
    if target_language not in supported_languages:
        return jsonify({
            'status': 'error', 
            'message': f'不支援的語言: {target_language}',
            'supported_languages': supported_languages
        }), 400
    
    try:
        print("\n[DEBUG] --- 翻譯流程開始 ---")
        # 若來源語言與目標語言相同，直接略過翻譯
        meeting_lang = meeting.get('language')
        if meeting_lang and meeting_lang == target_language and not force_retranslate:
            print(f"[DEBUG] 語言相同，略過翻譯: meeting_lang={meeting_lang}, target={target_language}")
            return jsonify({
                'status': 'completed',
                'message': '來源與目標語言相同，略過翻譯。'
            })
        # 如果強制重新翻譯，先刪除舊的快取和任務記錄
        if force_retranslate:
            print(f"[DEBUG] 步驟 1: 強制重新翻譯，正在刪除舊的快取: {meeting_id}, {target_language}")
            meetgenius_db.delete_translation_cache(meeting_id, target_language)

        # 獲取或創建翻譯任務
        print("[DEBUG] 步驟 2: 正在獲取或創建翻譯任務...")
        job, created = get_or_create_translation_job(meeting_id, target_language)
        print(f"[DEBUG] -> 任務獲取結果: job_status='{job.get('status') if job else 'None'}', created={created}")
        
        # 如果找不到任務實體(極端情況)
        if not job:
            print("[DEBUG] 錯誤: 找不到或無法創建任務。")
            return jsonify({'status': 'error', 'message': '無法獲取或創建翻譯任務'}), 500

        # 檢查任務狀態
        print("[DEBUG] 步驟 3: 檢查任務狀態...")
        # 情況1: 任務已完成，且不是強制重翻
        if job['status'] == 'completed' and not force_retranslate:
            print("[DEBUG] -> 判斷: 任務已完成且非強制。流程結束。")
            return jsonify({
                'status': 'completed',
                'message': '此語言的翻譯已存在。'
            })
            
        # 情況2: 任務已在處理中 (不是剛創建的)，且不是強制重翻
        if job['status'] == 'processing' and not created and not force_retranslate:
             print("[DEBUG] -> 判斷: 任務已在處理中。通知前端繼續等待。流程結束。")
             return jsonify({
                'status': 'processing',
                'message': '此語言的翻譯正在處理中，請稍候。'
            })

        # 情況3: 任務需要被執行 (剛創建的，或強制重翻的)
        print(f"[DEBUG] 步驟 4: 準備啟動背景翻譯任務 (created={created}, force_retranslate={force_retranslate})")
        app = current_app._get_current_object()
        thread = Thread(
            target=_run_translation_job, 
            args=(app, meeting_id, target_language, force_retranslate)
        )
        thread.daemon = True
        thread.start()
        
        print("[DEBUG] -> 背景任務已啟動。通知前端任務已開始。")
        return jsonify({
            'status': 'processing',
            'message': '翻譯任務已啟動，完成後頁面將會更新。'
        })
        
    except Exception as e:
        print(f"[ERROR] 啟動翻譯過程中發生未知錯誤: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': '啟動翻譯過程中發生錯誤'}), 500

@bp.route('/meetgenius/meeting/<meeting_id>/translation-status/<target_language>', methods=['GET'])
def get_translation_status(meeting_id, target_language):
    """API: 獲取指定翻譯任務的狀態"""
    job = get_translation_job(meeting_id, target_language)
    if not job:
        return jsonify({'status': 'not_found', 'message': '找不到指定的翻譯任務'}), 404
    
    return jsonify({'status': 'success', 'job': job})

@bp.route('/meetgenius/meeting/<meeting_id>/translation/<target_language>', methods=['GET'])
def get_translation(meeting_id, target_language):
    """API: 獲取指定語言的翻譯結果"""
    from app.additional.meetgenius.db import (
        get_translation_summary,
        get_translation_transcript,
    )
    
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '會議記錄不存在'}), 404
    
    result = {
        'status': 'success',
        'meeting_id': meeting_id,
        'target_language': target_language
    }
    
    # 獲取翻譯摘要
    translated_summary = get_translation_summary(meeting_id, target_language)
    if translated_summary:
        result['translated_summary'] = translated_summary
    
    # 獲取翻譯逐字稿
    translated_transcript = get_translation_transcript(meeting_id, target_language)
    if translated_transcript:
        result['translated_transcript'] = translated_transcript
    
    if not translated_summary and not translated_transcript:
        return jsonify({
            'status': 'error', 
            'message': f'沒有找到 {target_language} 的翻譯結果'
        }), 404
    
    return jsonify(result)

@bp.route('/meetgenius/meeting/<meeting_id>/translations', methods=['GET'])
def get_available_translations(meeting_id):
    """API: 獲取會議的所有可用翻譯語言"""
    from app.additional.meetgenius.db import (
        get_available_translations as db_get_available_translations,
    )
    
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '會議記錄不存在'}), 404
    
    available_languages = db_get_available_translations(meeting_id)
        
    return jsonify({
        'status': 'success',
        'meeting_id': meeting_id,
        'available_languages': available_languages
    })

@bp.route('/meetgenius/meeting/<meeting_id>/translation/<target_language>', methods=['DELETE'])
def delete_translation(meeting_id, target_language):
    """API: 刪除指定語言的翻譯快取"""
    from app.additional.meetgenius.db import delete_translation_cache
    
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '會議記錄不存在'}), 404
    
    try:
        delete_translation_cache(meeting_id, target_language)
        return jsonify({
            'status': 'success',
            'message': f'已刪除 {target_language} 翻譯快取'
        })
    except Exception as e:
        print(f"[ERROR] 刪除翻譯快取失敗: {e}")
        return jsonify({'status': 'error', 'message': '刪除翻譯快取失敗'}), 500

@bp.route('/meetgenius/translation/status')
def get_translation_service_status():
    """API: 檢查翻譯服務的可用狀態"""
    return jsonify({
        'status': 'available',
        'provider': current_app.audio_processor.get_provider_name(),
        'model': current_app.audio_processor.get_model_name(),
    })
    
@bp.route('/meetgenius/meeting/<string:meeting_id>/faq', methods=['GET'])
def get_meeting_faq(meeting_id):
    """API: 獲取指定會議的 FAQ 資料"""
    import json
    
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '找不到會議記錄'}), 404
        
    if meeting['status'] != 'completed':
        return jsonify({
            'status': 'processing', 
            'message': '會議尚未處理完成', 
            'current_status': meeting['status']
        }), 202

    faq_data = meeting.get('faq')
    
    # 資料庫中儲存的可能是 JSON 字串，也可能是已經解析過的 Python 物件
    if isinstance(faq_data, str):
        try:
            faq_list = json.loads(faq_data)
        except json.JSONDecodeError:
            faq_list = []
    elif isinstance(faq_data, list):
        faq_list = faq_data
    else:
        faq_list = []

    return jsonify({'status': 'success', 'faq': faq_list})

def _get_full_translation(meeting_id, target_language):
    """輔助函式：從資料庫獲取並組合完整的翻譯資料"""
    summary_data = get_translation_summary(meeting_id, target_language)
    transcript_data = get_translation_transcript(meeting_id, target_language)

    if not summary_data and not transcript_data:
        return None

    return {
        "summary": summary_data or {},
        "transcript": transcript_data or []
    }


def _passthrough_locale(code: str) -> str:
    """不做正規化，原樣回傳供檔名與顯示使用。"""
    return (code or '').strip()

@bp.route('/meetgenius/meeting/<meeting_id>/download_bilingual_file/<file_type>', methods=['GET'])
def download_bilingual_file(meeting_id, file_type):
    """API: 下載雙語對照的逐字稿檔案"""
    target_language = request.args.get('target_language')
    if not target_language:
        return jsonify({'status': 'error', 'message': '必須提供目標語言 (target_language)'}), 400

    if file_type not in ['docx', 'txt']:
        return jsonify({'status': 'error', 'message': '不支援的檔案類型'}), 400

    try:
        meeting_info = get_meeting_by_id(meeting_id)
        if not meeting_info:
            return jsonify({'status': 'error', 'message': '找不到會議記錄'}), 404

        # 獲取原文逐字稿
        srt_content, _ = _get_subtitle_content(meeting_id)
        if isinstance(srt_content, Response): # 處理錯誤情況
            return srt_content
        
        speaker_names = get_speaker_names(meeting_id)
        original_transcript = _parse_srt_to_transcription(srt_content, speaker_names)

        # 獲取翻譯逐字稿
        translated_transcript = get_translation_transcript(meeting_id, target_language)
        if not translated_transcript:
            return jsonify({'status': 'error', 'message': f'找不到 {target_language} 的翻譯逐字稿資料'}), 404

        original_filename_stem = Path(meeting_info.get('original_filename', 'meeting')).stem

        normalized_lang = _passthrough_locale(target_language)

        if file_type == 'docx':
            file_stream = create_bilingual_transcript_docx(
                original_transcript,
                translated_transcript,
                meeting_info,
                normalized_lang
            )
            download_name = f"{original_filename_stem}_bilingual_transcript_{normalized_lang}.docx"
            mimetype = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        
        else: # txt
            content = create_bilingual_transcript_txt(
                original_transcript,
                translated_transcript,
                meeting_info,
                normalized_lang
            )
            file_stream = io.BytesIO(content.encode('utf-8'))
            download_name = f"{original_filename_stem}_bilingual_transcript_{normalized_lang}.txt"
            mimetype = 'text/plain; charset=utf-8'

        return send_file(
            file_stream,
            as_attachment=True,
            download_name=download_name,
            mimetype=mimetype
        )

    except Exception as e:
        current_app.logger.error(f"下載雙語檔案時發生錯誤: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': f'伺服器內部錯誤: {e}'}), 500

# ===============================================
# 發言人重點提取 API
# ===============================================

@bp.route('/meetgenius/meeting/<meeting_id>/speaker-highlights', methods=['GET', 'POST'])
def handle_speaker_highlights(meeting_id):
    """API: 獲取或生成發言人重點"""
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '會議記錄不存在'}), 404
    
    if meeting['status'] != 'completed':
        return jsonify({'status': 'error', 'message': '會議尚未處理完成'}), 400
    
    if request.method == 'GET':
        # 檢查是否已有發言人重點
        if meeting.get('speaker_highlights'):
            import json
            speaker_highlights_data = meeting.get('speaker_highlights')
            speaker_highlights = None

            # 可能已在 DB 層解析為 Python 物件，也可能仍是 JSON 字串
            if isinstance(speaker_highlights_data, str):
                try:
                    speaker_highlights = json.loads(speaker_highlights_data)
                except (json.JSONDecodeError, TypeError):
                    speaker_highlights = None
            elif isinstance(speaker_highlights_data, (dict, list)):
                speaker_highlights = speaker_highlights_data

            if speaker_highlights is not None:
                return jsonify({
                    'status': 'success',
                    'speaker_highlights': speaker_highlights
                })
        
        return jsonify({'status': 'not_found', 'message': '尚未生成發言人重點'})
    
    if request.method == 'POST':
        # 生成發言人重點
        if not meeting.get('transcript'):
            return jsonify({'status': 'error', 'message': '找不到會議逐字稿'}), 400
        
        try:
            from app.additional.meetgenius.services.translation import get_translation_service
            
            translation_service = get_translation_service()
            if not translation_service.is_available():
                return jsonify({'status': 'error', 'message': '翻譯服務無法使用'}), 500
            
            # 提取發言人重點
            speaker_highlights = translation_service.extract_speaker_highlights(
                meeting['transcript']
            )
            
            # 儲存到資料庫
            import json
            from app.additional.meetgenius.db import update_meeting_speaker_highlights
            update_meeting_speaker_highlights(meeting_id, json.dumps(speaker_highlights, ensure_ascii=False))
            
            return jsonify({
                'status': 'success',
                'speaker_highlights': speaker_highlights
            })
            
        except Exception as e:
            print(f"[ERROR] 生成發言人重點失敗: {e}")
            return jsonify({'status': 'error', 'message': '生成發言人重點時發生錯誤'}), 500