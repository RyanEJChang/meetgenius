import re
import srt


def srt_to_vtt(srt_content):
    """
    將 SRT 格式轉換為 VTT 格式
    
    Args:
        srt_content (str): SRT 格式的字幕內容
    
    Returns:
        str: VTT 格式的字幕內容
    """
    try:
        # 解析 SRT
        subtitles = list(srt.parse(srt_content))
        
        # 構建 VTT 內容
        vtt_lines = ['WEBVTT\n']
        
        for subtitle in subtitles:
            # 轉換時間格式（SRT 使用逗號，VTT 使用點）
            start_time = str(subtitle.start).replace(',', '.')
            end_time = str(subtitle.end).replace(',', '.')
            
            # 確保時間格式正確（VTT 需要 HH:MM:SS.sss 格式）
            if '.' not in start_time:
                start_time += '.000'
            if '.' not in end_time:
                end_time += '.000'
                
            vtt_lines.append(f"{start_time} --> {end_time}")
            vtt_lines.append(subtitle.content)
            vtt_lines.append('')  # 空行分隔
        
        return '\n'.join(vtt_lines)
    except Exception as e:
        print(f"[ERROR] 轉換 SRT 到 VTT 時發生錯誤: {e}")
        return "WEBVTT\n\nERROR: Unable to convert subtitles"


def parse_srt_content(srt_content, speaker_name_mapping=None):
    """
    解析 SRT 格式的字幕內容，轉換為結構化資料
    
    這個函數將原始的 SRT 文字內容解析成包含時間戳記、語者資訊
    和文字內容的結構化資料，方便前端進行各種處理和顯示。
    
    Args:
        srt_content (str): SRT 格式的字幕內容
        speaker_name_mapping (dict): 發言者名稱映射，例如 {"發言者00": "Kevin", "發言者01": "Elvis"}
    """
    if speaker_name_mapping is None:
        speaker_name_mapping = {}
        
    try:
        # 使用 srt 函式庫解析內容
        subtitles = list(srt.parse(srt_content))
        
        segments = []
        for subtitle in subtitles:
            # 提取語者資訊（如果存在）
            original_speaker = extract_speaker_from_content(subtitle.content)
            
            # 使用自定義名稱（如果有的話）
            display_speaker = speaker_name_mapping.get(original_speaker, original_speaker)
            
            # 清理內容文字（移除語者標籤）
            clean_content = clean_subtitle_content(subtitle.content)
            
            segment = {
                'index': subtitle.index,
                'start_time': subtitle.start.total_seconds(),
                'end_time': subtitle.end.total_seconds(),
                'start_time_formatted': format_time(subtitle.start.total_seconds()),
                'end_time_formatted': format_time(subtitle.end.total_seconds()),
                'duration': (subtitle.end - subtitle.start).total_seconds(),
                'speaker': display_speaker,
                'original_speaker': original_speaker,
                'content': clean_content,
                'original_content': subtitle.content
            }
            segments.append(segment)
        
        return segments
    
    except Exception as e:
        print(f"[ERROR] 解析 SRT 內容時發生錯誤: {e}")
        return []


def extract_speaker_from_content(content):
    """
    從字幕內容中提取語者資訊
    
    這個修正版本能夠正確處理你實際的語者標籤格式，
    包括數字編號的語者標籤（如 [發言者00]、[發言者01] 等）
    """
    # 擴展的語者標籤匹配模式，涵蓋更多實際使用的格式
    speaker_patterns = [
        # 數字格式的語者標籤（你的實際格式）
        r'\[發言者(\d+)\]',       # [發言者00], [發言者01], [發言者02] 等
        r'\[語者(\d+)\]',         # [語者00], [語者01] 等
        r'\[Speaker(\d+)\]',      # [Speaker00], [Speaker01] 等
        r'\[SPEAKER_(\d+)\]',     # [SPEAKER_00], [SPEAKER_01] 等（原始 pyannote 格式）
        # 支援無底線/有空白或無空白且帶冒號的格式
        r'SPEAKER\s*(\d+):',       # SPEAKER 1: 或 SPEAKER1:
        r'Speaker\s*(\d+):',       # Speaker 1:
        r'SPEAKER_(\d+):',          # SPEAKER_1:
        
        # 字母格式的語者標籤（備用支援）
        r'\[發言者([A-Z])\]',     # [發言者A], [發言者B] 等
        r'\[Speaker ([A-Z])\]',   # [Speaker A], [Speaker B] 等
        r'\[語者([A-Z])\]',       # [語者A], [語者B] 等
        r'\[([A-Z])\]',           # [A], [B] 等
        
        # 冒號格式的語者標籤
        r'發言者(\d+):',          # 發言者00:, 發言者01: 等
        r'語者(\d+):',            # 語者00:, 語者01: 等
        r'Speaker(\d+):',         # Speaker00:, Speaker01: 等
        r'發言者([A-Z]):',        # 發言者A:, 發言者B: 等
        r'Speaker ([A-Z]):',      # Speaker A:, Speaker B: 等
        
        # 空格格式的語者標籤（新增）
        r'\[發言者(\d+)\] ',      # [發言者00] , [發言者01]  等
        r'\[語者(\d+)\] ',        # [語者00] , [語者01]  等
        r'\[Speaker(\d+)\] ',     # [Speaker00] , [Speaker01]  等
        r'\[SPEAKER_\d+\]\s*',    # [SPEAKER_00] , [SPEAKER_01]  等
        r'\[發言者([A-Z])\] ',    # [發言者A] , [發言者B]  等
        r'\[Speaker ([A-Z])\] ',  # [Speaker A] , [Speaker B]  等
        r'\[語者([A-Z])\] ',      # [語者A] , [語者B]  等
        r'\[([A-Z])\] ',          # [A] , [B]  等
    ]
    
    for pattern in speaker_patterns:
        match = re.search(pattern, content)
        if match:
            speaker_id = match.group(1)
            
            # 處理數字格式的語者ID
            if speaker_id.isdigit():
                # 將數字轉換為更友善的顯示格式
                speaker_number = int(speaker_id)
                # 你可以選擇保持數字格式或轉換為字母格式
                return f"發言者{speaker_number:02d}"  # 保持數字格式：發言者00, 發言者01
                # 或者使用下面這行轉換為字母格式：
                # return f"發言者{chr(65 + speaker_number)}"  # 轉換為：發言者A, 發言者B
            else:
                # 處理字母格式的語者ID
                return f"發言者{speaker_id}"
    
    # 如果沒有匹配到任何模式，嘗試更寬鬆的匹配
    # 這是為了處理一些邊緣情況或格式變異
    fallback_patterns = [
        r'(\d+):', # 純數字後跟冒號，如 "00:", "01:"
        r'([A-Z]):', # 純字母後跟冒號，如 "A:", "B:"
    ]
    
    for pattern in fallback_patterns:
        match = re.search(pattern, content)
        if match:
            speaker_id = match.group(1)
            if speaker_id.isdigit():
                return f"發言者{int(speaker_id):02d}"
            else:
                return f"發言者{speaker_id}"
    
    # 如果完全無法識別，返回基於內容位置的預設值
    # 這比直接返回"未知"更有用
    return "發言者未識別"


def clean_subtitle_content(content):
    """
    清理字幕內容，移除語者標籤和時間戳記
    
    這個函數專注於移除實際使用的語者標籤格式，
    確保清理後的內容只包含純粹的對話文字。
    """
    # 需要移除的語者標籤模式（根據實際 SRT 檔案格式優化）
    patterns_to_remove = [
        # 最常見的格式，帶冒號的語者標籤
        r'\[發言者\d+\]:\s*',      # [發言者00]: , [發言者01]:  等
        r'\[語者\d+\]:\s*',        # [語者00]: , [語者01]:  等
        r'\[Speaker\d+\]:\s*',     # [Speaker00]: , [Speaker01]:  等
        r'\[SPEAKER_\d+\]:\s*',    # [SPEAKER_00]: , [SPEAKER_01]:  等
        r'SPEAKER\s*\d+:\s*',       # SPEAKER 1:
        r'Speaker\s*\d+:\s*',       # Speaker 1:
        r'SPEAKER_\d+:\s*',          # SPEAKER_1:
        
        # 不帶冒號的語者標籤格式（新格式）
        r'\[發言者\d+\]\s+',       # [發言者00] , [發言者01]  等
        r'\[語者\d+\]\s+',         # [語者00] , [語者01]  等
        r'\[Speaker\d+\]\s+',      # [Speaker00] , [Speaker01]  等
        r'\[SPEAKER_\d+\]\s+',     # [SPEAKER_00] , [SPEAKER_01]  等
        
        # 可能的時間戳記格式
        r'\(\d{2}:\d{2}:\d{2}\s*-\s*\d{2}:\d{2}:\d{2}\)\s*',  # (00:01:23 - 00:02:45)
        r'\[\d{2}:\d{2}:\d{2}\]\s*',  # [00:01:23]
    ]
    
    cleaned = content
    for pattern in patterns_to_remove:
        cleaned = re.sub(pattern, '', cleaned)
    
    # 移除多餘的空白和換行
    cleaned = re.sub(r'\n+', '\n', cleaned)  # 合併多個換行
    cleaned = re.sub(r'\s+', ' ', cleaned)   # 合併多個空格
    
    return cleaned.strip()


def format_time(seconds):
    """將秒數格式化為 HH:MM:SS 或 MM:SS"""
    if not isinstance(seconds, (int, float)):
        return "00:00"
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def apply_speaker_names_to_srt(srt_content, speaker_name_mapping=None):
    """
    將發言者名稱映射應用到SRT內容中
    
    Args:
        srt_content (str): 原始SRT內容
        speaker_name_mapping (dict): 發言者名稱映射，例如 {"發言者00": "Kevin", "發言者01": "Elvis"}
    
    Returns:
        str: 應用發言者名稱映射後的SRT內容
    """
    if not speaker_name_mapping:
        return srt_content
    
    try:
        # 使用 srt 函式庫解析內容
        subtitles = list(srt.parse(srt_content))
        
        for subtitle in subtitles:
            # 提取原始發言者資訊
            original_speaker = extract_speaker_from_content(subtitle.content)
            
            # 如果有對應的自定義名稱，則替換
            if original_speaker in speaker_name_mapping:
                custom_name = speaker_name_mapping[original_speaker]
                
                # 替換發言者標籤
                # 處理各種可能的格式
                patterns_to_replace = [
                    (rf'\[{re.escape(original_speaker)}\]:', f'[{custom_name}]:'),
                    (rf'\[{re.escape(original_speaker)}\]\s+', f'[{custom_name}] '),
                    (rf'{re.escape(original_speaker)}:', f'{custom_name}:'),
                    (r'SPEAKER\s*\d+:', f'{custom_name}:'),
                    (r'SPEAKER_\d+:', f'{custom_name}:'),
                    (r'Speaker\s*\d+:', f'{custom_name}:'),
                ]
                
                for pattern, replacement in patterns_to_replace:
                    subtitle.content = re.sub(pattern, replacement, subtitle.content)
        
        # 重新組合成SRT格式
        return srt.compose(subtitles)
    
    except Exception as e:
        print(f"[ERROR] 應用發言者名稱映射時發生錯誤: {e}")
        return srt_content  # 如果出錯，返回原始內容
