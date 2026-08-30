import os
import uuid
from pathlib import Path
from threading import Thread
from datetime import datetime
import traceback

from flask import (
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename
from flask_login import login_required, current_user

from app.additional import blueprint as bp

from app.additional.meetgenius.db import (
    add_meeting,
    get_all_meetings,
    get_meeting_by_id,
    get_meeting_summary,
    get_speaker_names,
    get_available_translations,
    get_translation_summary,
    get_translation_transcript,
)
from app.additional.meetgenius.services.processing import process_meeting
from app.additional.meetgenius.utils.export_utils import (
    format_summary_for_export,
    create_summary_docx,
    create_transcript_docx,
)
from app.additional.meetgenius.utils.subtitle_processing import parse_srt_content
from app.additional.meetgenius.utils.document_parser import SUPPORTED_EXTENSIONS
from io import BytesIO
import mimetypes

# ===============================================
# 網頁路由
# ===============================================

@bp.route('/meetgenius')
@login_required
def index():
    """首頁 - 顯示上傳介面"""
    return render_template('meetgenius/index.html')

@bp.route('/meetgenius/upload', methods=['POST'])
def upload_file():
    """處理檔案上傳（前端表單提交）"""
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

    # 處理輔助文件
    supplementary_file = request.files.get('supplementary_file')
    supplementary_file_path = None
    if supplementary_file and supplementary_file.filename != '':
        supp_file_ext = Path(supplementary_file.filename).suffix.lower()
        if supp_file_ext not in SUPPORTED_EXTENSIONS:
            supported = '、'.join(sorted(SUPPORTED_EXTENSIONS))
            return jsonify({
                'status': 'error',
                'message': f'不支援的參考文件格式，目前支援：{supported}'
            }), 400
        
        # 儲存輔助文件
        supp_filename = secure_filename(f"{meeting_id}_supp_{supplementary_file.filename}")
        supplementary_file_path = str(uploads_folder / supp_filename)
        try:
            supplementary_file.save(supplementary_file_path)
        except Exception as e:
            print(f"[ERROR] 儲存輔助文件失敗: {e}")
            return jsonify({'status': 'error', 'message': '儲存輔助文件時發生錯誤'}), 500

    filename = secure_filename(f"{meeting_id}_{file.filename}")
    file_path = uploads_folder / filename
    
    try:
        # 檢查檔案大小
        file.seek(0, os.SEEK_END)
        file_length = file.tell()
        file.seek(0, 0)
        if file_length > max_content_length:
            if supplementary_file_path and Path(supplementary_file_path).exists():
                Path(supplementary_file_path).unlink()
            return jsonify({'status': 'error', 'message': f'檔案大小超過 {max_content_length // 1024 // 1024}MB 的限制'}), 413
            
        file.save(file_path)
    except Exception as e:
        print(f"[ERROR] 儲存檔案失敗: {e}")
        if supplementary_file_path and Path(supplementary_file_path).exists():
            Path(supplementary_file_path).unlink()
        return jsonify({'status': 'error', 'message': '儲存檔案時發生錯誤'}), 500

    num_speakers = request.form.get('num_speakers')
    if num_speakers and num_speakers.isdigit():
        num_speakers = int(num_speakers)
    else:
        num_speakers = None
    
    add_meeting(meeting_id, filename, file.filename, supplementary_file_path)
    
    # --- 解決競爭條件：在啟動線程前建立並註冊佇列 ---
    progress_queue = None
    # 初始化最新進度，供輪詢端點使用
    current_app.progress_latest[meeting_id] = {
        'step': 'queued',
        'progress': 0,
        'message': '已加入佇列，準備開始處理...'
    }
    # --- ---
    
    app = current_app._get_current_object()
    
    thread = Thread(target=process_meeting, args=(app, meeting_id, str(file_path), progress_queue, num_speakers, supplementary_file_path))
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'success', 'meeting_id': meeting_id})

@bp.route('/meetgenius/meetings/<meeting_id>/progress')
def meeting_progress(meeting_id):
    """提供最新的處理進度（輪詢用）。"""
    latest = current_app.progress_latest.get(meeting_id)
    if not latest:
        # 回退到資料庫狀態
        meeting = get_meeting_by_id(meeting_id)
        if not meeting:
            return jsonify({'status': 'error', 'message': '找不到指定的會議'}), 404
        state = meeting['status']
        mapped = {
            'uploaded': {'step': 'queued', 'progress': 0, 'message': '等待開始處理...'},
            'processing': {'step': 'processing', 'progress': 50, 'message': '處理中...'},
            'completed': {'step': 'completed', 'progress': 100, 'message': '處理完成！'},
            'failed': {'step': 'failed', 'progress': 100, 'message': meeting.get('error_message') or '處理失敗'}
        }.get(state, {'step': state, 'progress': -1, 'message': f'狀態：{state}'})
        return jsonify({'status': 'success', 'data': mapped})
    return jsonify({'status': 'success', 'data': latest})

@bp.route('/meetgenius/meetings')
@login_required
def meetings_list():
    """會議列表頁面"""
    meetings = get_all_meetings()
    return render_template('meetgenius/meetings.html', meetings=meetings)

@bp.route('/meetgenius/meetings/<meeting_id>')
@login_required
def meeting_detail(meeting_id):
    """會議詳情頁面"""
    meeting_dict = get_meeting_by_id(meeting_id)
    if not meeting_dict:
        flash('找不到指定的會議記錄', 'error')
        return redirect(url_for('additional_blueprint.meetings_list'))
    
    # 將 meeting 從 Row 物件轉換為可修改的字典
    meeting = dict(meeting_dict)

    # 獲取此會議所有可用的翻譯
    available_translations = get_available_translations(meeting_id)
    if available_translations:
        meeting['translations'] = {}
        for lang in available_translations:
            summary = get_translation_summary(meeting_id, lang)
            transcript = get_translation_transcript(meeting_id, lang)
            meeting['translations'][lang] = {
                'summary': summary,
                'transcript': transcript
            }
            
    return render_template('meetgenius/meeting_detail.html', meeting=meeting)

@bp.route('/meetgenius/meetings/<meeting_id>/status')
def meeting_status(meeting_id):
    """檢查會議處理狀態 API"""
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return jsonify({'status': 'error', 'message': '找不到指定的會議'}), 404
    
    return jsonify({
        'status': 'success',
        'meeting_status': meeting['status'],
        'error_message': meeting.get('error_message'),
        'processed_at': meeting.get('processed_at')
    })

# ===============================================
# 檔案操作路由
# ===============================================

def _send_file_with_range(file_path: Path, default_mime: str):
    """支援 Range 的串流回應（fallback 為整檔）。"""
    file_size = file_path.stat().st_size
    range_header = request.headers.get('Range', None)
    mime, _ = mimetypes.guess_type(str(file_path))
    content_type = mime or default_mime or 'application/octet-stream'

    if not range_header:
        rv = send_file(file_path, mimetype=content_type)
        rv.headers['Accept-Ranges'] = 'bytes'
        return rv

    # Range: bytes=start-end
    try:
        units, range_spec = range_header.split('=')
        if units.strip() != 'bytes':
            raise ValueError('Unsupported units')
        start_str, end_str = range_spec.split('-')
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        start = max(0, start)
        end = min(file_size - 1, end)
        if start > end:
            start, end = 0, file_size - 1
        length = end - start + 1
        with file_path.open('rb') as f:
            f.seek(start)
            data = f.read(length)
        rv = Response(data, 206, mimetype=content_type, direct_passthrough=True)
        rv.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        rv.headers['Accept-Ranges'] = 'bytes'
        rv.headers['Content-Length'] = str(length)
        return rv
    except Exception:
        rv = send_file(file_path, mimetype=content_type)
        rv.headers['Accept-Ranges'] = 'bytes'
        return rv


@bp.route('/meetgenius/stream_audio/<meeting_id>')
def stream_audio(meeting_id):
    """串流處理後或可用的音訊檔案（自動回退）"""
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return "Meeting not found", 404

    # --- 硬編碼設定 ---
    app_root = Path(current_app.root_path).parent
    processed_folder = app_root / "app" / "additional" / "meetgenius" / "processed"
    uploads_folder = app_root / "app" / "additional" / "meetgenius" / "uploads"
    # --- 硬編碼設定結束 ---

    # 定義 MIME 白名單，避免猜測錯誤
    mime_map = {
        'mp3': 'audio/mpeg',
        'm4a': 'audio/mp4',
        'wav': 'audio/wav',
        'ogg': 'audio/ogg',
        'webm': 'audio/webm',
        'mp4': 'video/mp4',
    }

    # 1) 優先播放高相容格式：mp3 -> m4a -> wav
    for ext in ['mp3', 'm4a', 'wav']:
        # 播放用 mp3 檔名：<meeting_id>_playback.mp3
        specific = processed_folder / f"{meeting_id}_playback.{ext}"
        if specific.exists():
            return _send_file_with_range(specific, mime_map[ext])
        # 傳統命名：<meeting_id>.<ext>
        legacy = processed_folder / f"{meeting_id}.{ext}"
        if legacy.exists():
            return _send_file_with_range(legacy, mime_map[ext])

    # 2) 回退找以 meeting_id_ 開頭的壓縮/轉檔音訊（ogg/webm）
    for ext, default_mime in [('ogg', 'audio/ogg'), ('webm', 'audio/webm')]:
        try:
            for candidate in processed_folder.glob(f"{meeting_id}_*.{ext}"):
                return _send_file_with_range(candidate, default_mime)
        except Exception:
            pass

    # 3) 最後回退：直接提供上傳的原始檔案（若存在）
    try:
        if meeting.get('filename'):
            original = uploads_folder / meeting['filename']
            if original.exists():
                # 使用白名單 MIME
                ext = original.suffix.lower().lstrip('.')
                return _send_file_with_range(original, mime_map.get(ext, 'application/octet-stream'))
    except Exception:
        pass

    return "Processed audio file not found", 404

@bp.route('/meetgenius/download/<meeting_id>/summary/<file_type>')
def download_summary(meeting_id, file_type):
    """下載會議摘要檔案 (txt 或 docx)"""
    print(f"開始下載摘要：meeting_id={meeting_id}, file_type={file_type}")

    try:
        # 檢查支援的檔案類型
        if file_type not in ['txt', 'docx']:
            print(f"不支援的檔案類型：{file_type}")
            flash('不支援的檔案類型', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))
            
        # 獲取會議資訊和摘要
        meeting = get_meeting_by_id(meeting_id)
        if not meeting:
            print(f"找不到會議記錄：meeting_id={meeting_id}")
            flash('找不到指定的會議記錄', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))
        print(f"成功獲取會議記錄：{meeting['id']}")

        summary_data = get_meeting_summary(meeting_id)
        if not summary_data:
            print(f"找不到會議摘要：meeting_id={meeting_id}")
            flash('找不到會議摘要', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))
        print("成功獲取摘要資料")

        # 獲取發言者名稱映射
        speaker_names = get_speaker_names(meeting_id)
        print(f"獲取發言者名稱映射：{speaker_names}")
            
        original_name = Path(meeting['original_filename']).stem
        print(f"原始檔案名稱 (stem): {original_name}")
        
        if file_type == 'txt':
            print("正在生成 TXT 檔案...")
            content = format_summary_for_export(summary_data, meeting, speaker_names)
            print(f"TXT 內容已生成，長度為 {len(content)} 字元")

            # 將字串內容轉換為 in-memory file-like object
            file_buffer = BytesIO(content.encode('utf-8'))
            file_buffer.seek(0)
            
            download_name = f"{original_name}_summary.txt"
            print(f"準備回傳 TXT 檔案，檔名: {download_name}")
            
            return send_file(
                file_buffer,
                as_attachment=True,
                download_name=download_name,
                mimetype='text/plain; charset=utf-8'
            )
        
        elif file_type == 'docx':
            print("正在生成 DOCX 檔案...")
            docx_file = create_summary_docx(summary_data, meeting, speaker_names)
            docx_file.seek(0, os.SEEK_END)
            file_size = docx_file.tell()
            docx_file.seek(0)
            print(f"DOCX 檔案已生成，大小為 {file_size} bytes")

            download_name = f"{original_name}_summary.docx"
            print(f"準備回傳 DOCX 檔案，檔名: {download_name}")
            
            return send_file(
                docx_file,
                as_attachment=True,
                download_name=download_name,
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            )

    except Exception as e:
        print(f"下載摘要時發生未預期的錯誤：{e}")
        traceback.print_exc()
        flash(f'下載摘要時發生錯誤: {e}', 'error')
        return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))

@bp.route('/meetgenius/download/<meeting_id>/<file_type>')
def download_file_route(meeting_id, file_type):
    """
    下載指定的原始檔案，如 SRT 字幕檔。
    也支援將逐字稿轉換為 TXT 格式下載。
    """
    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        flash('找不到指定的會議記錄', 'error')
        return redirect(url_for('additional_blueprint.meetings_list'))

    # --- 硬編碼設定 ---
    base_dir = Path(current_app.root_path).parent
    # --- 硬編碼設定結束 ---

    path_key = None
    if file_type == 'srt':
        path_key = 'srt_path'
    elif file_type == 'transcript_txt':
        # 對於 TXT 檔案，我們使用帶有發言者資訊的 SRT 作為來源
        srt_path_str = meeting.get('srt_path')
        if not srt_path_str:
            flash('找不到字幕檔', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))
        
        srt_file_path = base_dir / srt_path_str
        if not srt_file_path.exists():
            flash('字幕檔案遺失', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))
            
        try:
            # 讀取並解析 SRT 檔案
            srt_content = srt_file_path.read_text(encoding='utf-8')
            
            # 獲取發言者名稱映射
            speaker_names = get_speaker_names(meeting_id)
            
            # 傳遞映射來解析 SRT，直接獲得替換後的發言者名稱
            parsed_transcript = parse_srt_content(srt_content, speaker_names)
            
            # 轉換為純文字格式
            txt_lines = []
            for segment in parsed_transcript:
                speaker = segment.get('speaker', '未知發言人')
                content = segment.get('content', '').strip()
                txt_lines.append(f"{speaker}: {content}")
            
            txt_content = "\n".join(txt_lines)

            # 將字串內容轉換為 in-memory file-like object
            file_buffer = BytesIO(txt_content.encode('utf-8'))
            file_buffer.seek(0)
            
            # 準備檔名
            original_name = Path(meeting['original_filename']).stem
            download_name = f"{original_name}_transcript_speaker.txt"

            return send_file(
                file_buffer,
                as_attachment=True,
                download_name=download_name,
                mimetype='text/plain; charset=utf-8'
            )
        except Exception as e:
            print(f"[ERROR] 轉換 SRT 為 TXT 時發生錯誤: {e}")
            flash('轉換為 TXT 檔案時發生錯誤', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))
    elif file_type == 'transcript_docx':
        # 由 SRT 檔產出帶語者逐字稿的 DOCX
        srt_path_str = meeting.get('srt_path')
        if not srt_path_str:
            flash('找不到字幕檔', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))

        srt_file_path = base_dir / srt_path_str
        if not srt_file_path.exists():
            flash('字幕檔案遺失', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))

        try:
            srt_content = srt_file_path.read_text(encoding='utf-8')
            speaker_names = get_speaker_names(meeting_id)
            parsed_transcript = parse_srt_content(srt_content, speaker_names)

            docx_stream = create_transcript_docx(parsed_transcript, meeting)
            original_name = Path(meeting['original_filename']).stem
            download_name = f"{original_name}_transcript_speaker.docx"

            return send_file(
                docx_stream,
                as_attachment=True,
                download_name=download_name,
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            )
        except Exception as e:
            print(f"[ERROR] 轉換 SRT 為 DOCX 時發生錯誤: {e}")
            flash('轉換為 DOCX 檔案時發生錯誤', 'error')
            return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))
    else:
        flash('不支援的檔案類型', 'error')
        return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))

    file_path_str = meeting.get(path_key)
    if not file_path_str:
        flash('找不到指定的檔案路徑', 'error')
        return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))
        
    full_path = base_dir / file_path_str
    
    if not full_path.exists():
        flash('檔案不存在', 'error')
        return redirect(url_for('additional_blueprint.meeting_detail', meeting_id=meeting_id))

    original_name = Path(meeting['original_filename']).stem
    download_name = f"{original_name}_transcript.srt"
    return send_file(full_path, as_attachment=True, download_name=download_name)

@bp.route('/meetgenius/meetings/<meeting_id>/summary_data')
def get_summary_data(meeting_id):
    # Implementation of get_summary_data function
    pass



# ===============================================
# Demo 版本路由
# ===============================================

@bp.route('/meetgenius/demo')
@login_required
def demo_upload():
    """Demo版本上傳頁面"""
    return render_template('meetgenius/demo_upload.html')

@bp.route('/meetgenius/demo/upload', methods=['POST'])
def demo_upload_process():
    """處理Demo版本的檔案上傳（假的處理）"""
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': '沒有選擇檔案'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': '沒有選擇檔案'}), 400
    
    # 生成Demo ID
    demo_id = str(uuid.uuid4())
    meeting_type = request.form.get('meeting_type', 'business')
    
    # 在session中存儲demo信息，用於後續展示
    from flask import session
    session[f'demo_{demo_id}'] = {
        'original_filename': file.filename,
        'meeting_type': meeting_type,
        'upload_time': datetime.now().isoformat()
    }
    
    return jsonify({'status': 'success', 'demo_id': demo_id})

@bp.route('/meetgenius/demo/result/<demo_id>')
@login_required
def demo_result(demo_id):
    """展示Demo版本的結果頁面"""
    from flask import session
    
    demo_info = session.get(f'demo_{demo_id}')
    if not demo_info:
        flash('Demo會話已過期，請重新上傳', 'error')
        return redirect(url_for('additional_blueprint.demo_upload'))
    
    # 根據會議類型選擇不同的示範資料
    demo_data = get_demo_data(demo_info['meeting_type'])
    demo_data['original_filename'] = demo_info['original_filename']
    demo_data['upload_time'] = demo_info['upload_time']
    demo_data['demo_id'] = demo_id
    
    return render_template('meetgenius/demo_result.html', meeting=demo_data)

def get_demo_data(meeting_type):
    """根據會議類型返回相應的示範資料"""
    
    if meeting_type == 'business':
        return {
            'title': '季度業務檢討會議',
            'summary': {
                'overview': '本次會議針對Q3業績進行全面檢討，討論了銷售策略調整、市場拓展計劃以及下季度目標設定。會議中確認了多項重要決策，包括新產品發布時程和團隊擴編計劃。',
                'key_points': [
                    'Q3銷售目標達成率為105%，超越預期',
                    '決定在北部地區增設兩個新據點',
                    '新產品發布時程確定為下月15日',
                    '客服團隊將擴編3名新成員',
                    '預算分配需要在本週內完成審核'
                ],
                'action_items': [
                    {
                        'task': '準備新據點選址報告',
                        'assignee': '張經理',
                        'deadline': '2024-03-15'
                    },
                    {
                        'task': '完成新產品發布計劃書',
                        'assignee': '李主任',
                        'deadline': '2024-03-10'
                    },
                    {
                        'task': '提交客服團隊招聘需求',
                        'assignee': '人資部 王組長',
                        'deadline': '2024-03-12'
                    }
                ],
                'decisions': [
                    '核准北部地區擴點計劃，預算800萬',
                    '新產品發布日期定為3月15日',
                    '客服團隊擴編案通過，開始招聘程序'
                ],
                'keywords': ['業績檢討', '擴點計劃', '新產品發布', '團隊擴編', '預算審核']
            },
            'transcript': [
                {
                    'speaker': '張總經理',
                    'content': '各位同事好，今天我們召開Q3業績檢討會議。首先，我很高興宣布我們超越了季度目標，達成率為105%。這是全團隊努力的成果。',
                    'timestamp': '00:00:30'
                },
                {
                    'speaker': '李銷售經理',
                    'content': '謝謝張總。這季度我們在北部市場表現特別突出，我建議我們考慮在那邊增設據點。',
                    'timestamp': '00:01:15'
                },
                {
                    'speaker': '張總經理',
                    'content': '這個提議很好。請你準備一份詳細的選址報告，我們需要在3月15號前完成評估。',
                    'timestamp': '00:01:45'
                },
                {
                    'speaker': '王產品經理',
                    'content': '關於新產品發布，我們的準備工作已經進入最後階段。建議發布日期定在下月15日。',
                    'timestamp': '00:02:30'
                },
                {
                    'speaker': '陳客服主管',
                    'content': '隨著業務增長，我們的客服壓力也在增加。希望能夠擴編團隊，增加3名客服人員。',
                    'timestamp': '00:03:10'
                }
            ],
            'duration': '45分鐘',
            'participants': ['張總經理', '李銷售經理', '王產品經理', '陳客服主管', '人資部 劉組長']
        }
    
    elif meeting_type == 'academic':
        return {
            'title': 'AI技術研討會',
            'summary': {
                'overview': '本次研討會探討了人工智慧在各領域的最新應用，重點討論了機器學習模型的優化方法、自然語言處理的突破，以及AI倫理相關議題。與會專家分享了各自的研究成果並提出未來發展方向。',
                'key_points': [
                    'Transformer模型在多模態任務上的突破性進展',
                    'GPU運算資源優化可提升訓練效率30%',
                    'AI倫理框架需要更完善的監管機制',
                    '跨領域合作是AI應用成功的關鍵',
                    '開源模型社群貢獻顯著增長'
                ],
                'action_items': [
                    {
                        'task': '整理多模態模型研究文獻',
                        'assignee': '博士生 小王',
                        'deadline': '2024-03-20'
                    },
                    {
                        'task': '撰寫GPU優化技術報告',
                        'assignee': '李教授',
                        'deadline': '2024-03-25'
                    },
                    {
                        'task': '制定下期研討會主題',
                        'assignee': '研究小組',
                        'deadline': '2024-03-30'
                    }
                ],
                'decisions': [
                    '成立多模態AI研究小組',
                    '申請GPU運算資源擴充',
                    '下期研討會聚焦AI倫理議題'
                ],
                'keywords': ['人工智慧', 'Transformer', '多模態', 'GPU優化', 'AI倫理']
            },
            'transcript': [
                {
                    'speaker': '李教授',
                    'content': '歡迎各位參加今天的AI技術研討會。今天我們將討論最新的技術發展，特別是Transformer模型的新進展。',
                    'timestamp': '00:00:20'
                },
                {
                    'speaker': '張博士',
                    'content': '我想分享一下我們實驗室在多模態任務上的研究成果。通過改進注意力機制，我們在影像-文字理解任務上有了重大突破。',
                    'timestamp': '00:01:30'
                },
                {
                    'speaker': '王研究員',
                    'content': '這很有趣！關於運算效率，我們發現通過GPU運算資源的優化，可以將訓練時間縮短30%。',
                    'timestamp': '00:02:45'
                },
                {
                    'speaker': '陳教授',
                    'content': '技術發展固然重要，但我們也不能忽視AI倫理問題。我認為需要建立更完善的監管框架。',
                    'timestamp': '00:03:20'
                }
            ],
            'duration': '120分鐘',
            'participants': ['李教授', '張博士', '王研究員', '陳教授', '博士生 小王']
        }
    
    elif meeting_type == 'interview':
        return {
            'title': '軟體工程師面試',
            'summary': {
                'overview': '本次面試針對高級軟體工程師職位，評估候選人的技術能力、問題解決能力以及團隊合作精神。面試涵蓋了程式設計、系統設計、以及過往專案經驗的深度討論。',
                'key_points': [
                    '候選人具備紮實的演算法和資料結構基礎',
                    '在微服務架構設計方面有豐富經驗',
                    '具備良好的溝通能力和團隊合作精神',
                    '對新技術學習有強烈興趣和自主性',
                    '過往專案經驗與公司需求高度契合'
                ],
                'action_items': [
                    {
                        'task': '進行技術能力二面',
                        'assignee': '技術主管',
                        'deadline': '2024-03-18'
                    },
                    {
                        'task': '聯繫推薦人進行背景調查',
                        'assignee': 'HR',
                        'deadline': '2024-03-20'
                    },
                    {
                        'task': '準備錄取通知書',
                        'assignee': 'HR',
                        'deadline': '2024-03-22'
                    }
                ],
                'decisions': [
                    '候選人通過初面，進入二面階段',
                    '薪資範圍符合候選人期望',
                    '預計兩週內完成所有面試流程'
                ],
                'keywords': ['軟體工程師', '技術面試', '微服務', '演算法', '團隊合作']
            },
            'transcript': [
                {
                    'speaker': '面試官A',
                    'content': '歡迎來到我們公司面試。請先自我介紹一下，並談談你的技術背景。',
                    'timestamp': '00:00:10'
                },
                {
                    'speaker': '候選人',
                    'content': '謝謝！我是李小明，有5年的軟體開發經驗，主要專精於後端開發和微服務架構設計。',
                    'timestamp': '00:00:30'
                },
                {
                    'speaker': '面試官B',
                    'content': '很好。能否談談你在處理高併發系統時遇到的挑戰？',
                    'timestamp': '00:01:15'
                },
                {
                    'speaker': '候選人',
                    'content': '在上個專案中，我們面臨每秒萬級請求的挑戰。我們採用了Redis快取和資料庫分片來解決性能瓶頸。',
                    'timestamp': '00:01:45'
                }
            ],
            'duration': '60分鐘',
            'participants': ['面試官A', '面試官B', '候選人 李小明', 'HR 王小姐']
        }
    
    else:  # brainstorm
        return {
            'title': '新產品創意發想會議',
            'summary': {
                'overview': '團隊進行了一場富有創意的腦力激盪會議，針對下一代智慧型產品進行構思。會議採用設計思考方法，從用戶需求出發，產生了多個創新概念，並初步評估了可行性。',
                'key_points': [
                    'IoT設備整合是主要發展方向',
                    '語音控制功能獲得團隊一致認同',
                    '環保概念需要納入產品設計',
                    '用戶體驗設計是差異化關鍵',
                    '需要跨部門合作來實現創新'
                ],
                'action_items': [
                    {
                        'task': '完成市場調研報告',
                        'assignee': '行銷部',
                        'deadline': '2024-03-25'
                    },
                    {
                        'task': '製作產品原型',
                        'assignee': '設計團隊',
                        'deadline': '2024-04-01'
                    },
                    {
                        'task': '技術可行性評估',
                        'assignee': '工程部',
                        'deadline': '2024-03-28'
                    }
                ],
                'decisions': [
                    '確定以IoT整合為核心概念',
                    '語音控制列為必備功能',
                    '預算初步設定為500萬研發經費'
                ],
                'keywords': ['創意發想', 'IoT整合', '語音控制', '環保設計', '用戶體驗']
            },
            'transcript': [
                {
                    'speaker': '產品經理',
                    'content': '各位創意夥伴，今天我們要為下一代產品進行腦力激盪。讓我們從用戶的痛點開始思考。',
                    'timestamp': '00:00:15'
                },
                {
                    'speaker': '設計師A',
                    'content': '我觀察到用戶希望所有設備都能無縫連接。也許我們可以考慮IoT整合平台？',
                    'timestamp': '00:01:00'
                },
                {
                    'speaker': '工程師B',
                    'content': '技術上這是可行的。而且我們可以加入語音控制，讓操作更直觀。',
                    'timestamp': '00:01:30'
                },
                {
                    'speaker': '行銷專員',
                    'content': '從市場角度看，環保概念正在興起。我們的產品設計應該考慮永續發展。',
                    'timestamp': '00:02:10'
                }
            ],
            'duration': '90分鐘',
            'participants': ['產品經理', '設計師A', '工程師B', '行銷專員', 'UX研究員']
        }
