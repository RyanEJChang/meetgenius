from pathlib import Path
from datetime import datetime
import json
import time
from typing import List, Optional, Tuple
import subprocess
import shutil
import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pydub import AudioSegment
from flask import current_app

try:
    from opencc import OpenCC
except Exception:
    OpenCC = None

from .. import db
from ..utils.transcription_processor import get_summary_from_transcript
from ..utils.document_parser import read_reference_document
# from .translation import get_translation_service

# =====================
# Azure 轉錄整合工具
# =====================

SENT_END = set("。！？!?.")

# 參考測試腳本的標點與空白處理
PUNCTUATION_TOKENS = set(
    list(",.!?;:—-…()[]{}<>\"'，。！？；：、（）《》「」『』“”’‘")
)
TRAILING_SPACE_AFTER_EN_PUNCT = {",", ".", "!", "?", ";", ":"}


def _resolve_ffmpeg_path() -> str:
    """解析 ffmpeg 實際執行路徑（優先環境變數，再常見固定路徑，最後 PATH）。"""
    candidates = [
        os.getenv("FFMPEG_BINARY", "").strip(),
        "/home/ffmpeg/ffmpeg",
        "/home/site/wwwroot/bin/ffmpeg",
    ]
    for p in candidates:
        if p and Path(p).exists():
            return p
    which_path = shutil.which("ffmpeg")
    return which_path or ""


def _resolve_ffprobe_path() -> str:
    """解析 ffprobe 實際執行路徑（優先環境變數，再常見固定路徑，最後 PATH）。"""
    candidates = [
        os.getenv("FFPROBE_BINARY", "").strip(),
        "/home/ffmpeg/ffprobe",
        "/home/site/wwwroot/bin/ffprobe",
    ]
    for p in candidates:
        if p and Path(p).exists():
            return p
    which_path = shutil.which("ffprobe")
    return which_path or ""


FFMPEG_BINARY = _resolve_ffmpeg_path()
FFPROBE_BINARY = _resolve_ffprobe_path()

# 告知 pydub 應使用的 ffmpeg/ffprobe 路徑（若存在）
try:
    if FFMPEG_BINARY and Path(FFMPEG_BINARY).exists():
        AudioSegment.converter = FFMPEG_BINARY
    if FFPROBE_BINARY and Path(FFPROBE_BINARY).exists():
        AudioSegment.ffprobe = FFPROBE_BINARY
except Exception:
    pass


def _contains_cjk(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if (
            0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        ):
            return True
    return False


def _should_insert_space(prev_token: str, current_token: str) -> bool:
    if not prev_token:
        return False
    if _contains_cjk(prev_token) or _contains_cjk(current_token):
        return False
    if prev_token in TRAILING_SPACE_AFTER_EN_PUNCT and current_token and current_token[0].isalnum():
        return True
    if current_token in PUNCTUATION_TOKENS:
        return False
    return prev_token[-1].isalnum() and current_token and current_token[0].isalnum()


def _ms_to_srt_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    hh = ms // 3_600_000
    mm = (ms % 3_600_000) // 60_000
    ss = (ms % 60_000) // 1000
    mmm = ms % 1000
    return f"{hh:02d}:{mm:02d}:{ss:02d},{mmm:03d}"


def _ensure_pcm_wav_16k_mono(input_path: Path, work_dir: Path) -> Path:
    """
    將任意常見音訊格式轉為 16kHz/mono/16-bit PCM WAV，以符合 Azure STT API 的最佳相容性。
    回傳轉換後的檔案路徑（位於 work_dir）。
    若輸入已經是符合條件的 WAV，仍重新輸出以確保格式正確。
    """
    target_path = work_dir / f"{input_path.stem}_16k_mono.wav"
    ffmpeg_path = FFMPEG_BINARY or shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise Exception("找不到 ffmpeg，可執行檔不存在")
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-vn",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        str(target_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        raise Exception(f"音訊轉換失敗（轉為 PCM WAV）: {e}")

    return target_path


def _remux_mp4_faststart(input_path: Path, work_dir: Path) -> Path:
    """
    若為 MP4/M4A/MOV 容器，嘗試使用 ffmpeg 無重編碼重封裝並將 moov 移到檔頭（-movflags +faststart）。
    若 ffmpeg 不存在或失敗，回傳原路徑以不中斷流程。
    """
    suffix = input_path.suffix.lower()
    if suffix not in {".mp4", ".m4a", ".mov"}:
        return input_path

    ffmpeg_path = FFMPEG_BINARY if FFMPEG_BINARY else shutil.which("ffmpeg")
    if not ffmpeg_path or not Path(str(ffmpeg_path)).exists():
        return input_path

    target_ext = ".m4a" if suffix == ".m4a" else ".mp4"
    target_path = work_dir / f"{input_path.stem}_faststart{target_ext}"
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(target_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return target_path
    except Exception:
        return input_path


def _convert_audio_for_azure(input_path: Path, work_dir: Path) -> Tuple[Path, str]:
    """
    產生一個適合上傳至 Azure STT 的音訊檔案，並回傳 (輸出路徑, content_type)。
    預設輸出 OGG/OPUS（16kHz/mono/低比特率），大幅降低檔案大小，降低網路/代理壓力。
    """
    target_format = "ogg"

    if target_format == "wav":
        path = _ensure_pcm_wav_16k_mono(input_path, work_dir)
        return path, "audio/wav"

    # 預設走 ogg/opus
    target_path = work_dir / f"{input_path.stem}_16k_mono.opus.ogg"
    ffmpeg_path = FFMPEG_BINARY or shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise Exception("找不到 ffmpeg，可執行檔不存在")
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-vn",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libopus",
        "-b:a",
        "24k",
        "-vbr",
        "on",
        str(target_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        raise Exception(f"音訊轉換失敗（轉為 OGG/OPUS）: {e}")

    return target_path, "audio/ogg"


def _export_playback_mp3(input_path: Path, work_dir: Path, meeting_id: str) -> Path:
    """
    產出高相容播放用 MP3 檔案（單聲道/44.1kHz/約 96kbps），回傳輸出路徑。
    若轉檔失敗則拋出例外讓呼叫端決定是否忽略。
    """
    target_path = work_dir / f"{meeting_id}_playback.mp3"
    ffmpeg_path = FFMPEG_BINARY or shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise Exception("找不到 ffmpeg，可執行檔不存在")
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-vn",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "44100",
        "-b:a",
        "96k",
        str(target_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return target_path

def _get_audio_duration_ffprobe(input_path: Path) -> float:
    """使用 ffprobe 取得音訊長度（秒）；若 ffprobe 不可用則回傳 0.0。"""
    ffprobe_path = FFPROBE_BINARY or shutil.which("ffprobe")
    if not ffprobe_path:
        return 0.0
    try:
        result = subprocess.run([
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True, text=True)
        return float((result.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0

def _segment_by_phrase_punct_only(data: dict, include_speaker_label: bool = True) -> str:
    """
    以 phrase 為單位、以標點為邊界 產生 SRT：
    - 只看 words 中的句末標點 token (。！？!?)
    - 保留說話者標籤（以 phrase 的 speaker 為準；缺失則忽略）
    - 若 phrase 末尾沒有句末標點，則以該 phrase 最後一個詞的結束時間作為片段結束
    """
    phrases = data.get("phrases", []) or []
    srt_lines: List[str] = []
    idx = 1

    for ph in phrases:
        words = ph.get("words", []) or []
        if not words:
            continue

        speaker = (
            ph.get("speakerId")
            or ph.get("speaker")
            or ph.get("speakerNumber")
        )

        buf: List[str] = []
        start: Optional[int] = None
        last_end: Optional[int] = None

        def flush(end_time: int):
            nonlocal buf, start, last_end, idx
            if not buf or start is None or end_time is None:
                buf, start, last_end = [], None, None
                return
            text = "".join(buf).strip()
            if text:
                line_text = text
                if include_speaker_label and speaker is not None:
                    line_text = f"SPEAKER {speaker}: " + text
                srt_lines.append(
                    f"{idx}\n{_ms_to_srt_time(start)} --> {_ms_to_srt_time(end_time)}\n{line_text}\n"
                )
                idx += 1
            buf, start, last_end = [], None, None

        for w in words:
            txt = w.get("text", "")
            ws = int(w.get("offsetMilliseconds", 0))
            dur = int(w.get("durationMilliseconds", 0))
            we = ws + max(dur, 0)

            if start is None:
                start = ws
            # 智能插入空白（避免破壞 CJK 與標點，對齊測試腳本行為）
            if buf:
                prev_token = buf[-1]
                if _should_insert_space(prev_token, txt):
                    buf.append(" ")
            buf.append(txt)
            last_end = we

            # 句末標點 => 立即輸出一段
            if txt in SENT_END:
                flush(end_time=we)

        # phrase 結尾若仍有殘留且沒有句末標點，以最後一詞時間結束
        if buf and start is not None and last_end is not None:
            flush(end_time=last_end)

    return "\n".join(srt_lines)


def _azure_fast_transcribe_sync(
    audio_path: Path,
    content_type: str,
    region: str,
    api_version: str,
    subscription_key: str,
    locales: List[str],
    timeout=(30, 300),
    max_speakers: Optional[int] = None,
) -> dict:
    # 依序嘗試新版與舊版網域，避免區域/帳戶配置差異造成 422/404
    candidate_urls = [
        f"https://{region}.stt.speech.microsoft.com/speechtotext/transcriptions:transcribe",
        f"https://{region}.api.cognitive.microsoft.com/speechtotext/transcriptions:transcribe",
    ]
    params = {"api-version": api_version}
    headers = {
        "Ocp-Apim-Subscription-Key": subscription_key,
        "Accept": "application/json",
    }

    # 跨網路不穩/代理情境：為 POST 也啟用重試
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)

    # SSL 驗證固定啟用
    verify_value = True

    # 要求回傳詞級時間戳與語者，支援多語系（直接使用硬編碼的 locales，不做 zh-TW 特殊處理）
    definition = {
        "locales": [lc for lc in (locales or []) if lc and lc.strip()],
        "wordLevelTimestampsEnabled": True,
        "diarization": {"enabled": True},
    }

    # 若有提供預期語者數量，設定於請求定義（啟用 diarization）
    if isinstance(max_speakers, int) and max_speakers > 0:
        definition["diarization"]["maxSpeakers"] = int(max_speakers)

    last_error: Optional[Exception] = None
    with audio_path.open("rb") as fp:
        for url in candidate_urls:
            try:
                # 每次嘗試前重設檔案指標，避免後續重試送出空音檔
                try:
                    fp.seek(0)
                except Exception:
                    pass
                files = {
                    "audio": (audio_path.name, fp, content_type or "application/octet-stream"),
                    "definition": (None, json.dumps(definition), "application/json"),
                }
                try:
                    resp = session.post(
                        url,
                        headers=headers,
                        params=params,
                        files=files,
                        timeout=timeout,
                        verify=verify_value,
                    )
                except requests.exceptions.SSLError as ssl_err:
                    # 某些環境會出現 SSLWantWriteError，稍後重試
                    last_error = ssl_err
                    time.sleep(1.5)
                    continue
                if not resp.ok:
                    detail = (resp.text or "").strip()
                    raise requests.HTTPError(
                        f"HTTP {resp.status_code} calling {url}: {detail[:2000]}", response=resp
                    )
                return resp.json()
            except Exception as e:
                last_error = e
                continue
    # 若所有端點都失敗，拋出最後錯誤
    if last_error:
        raise last_error
    raise RuntimeError("Azure STT 呼叫失敗（未知錯誤）")


def _is_timecode_line(line: str) -> bool:
    """偵測是否為 SRT 的時間碼行。"""
    return "-->" in line


def _contains_chinese(text: str) -> bool:
    """判斷字串是否包含中文（中日韓共享區塊多為漢字，但盡量以中文常用符號/字為主）。"""
    if not text:
        return False
    for ch in text:
        code = ord(ch)
        # 中日韓統一表意文字基本區 (中文漢字主要分佈)
        if 0x4E00 <= code <= 0x9FFF:
            return True
        # 常用中文全形標點
        if ch in "，。！？；：、（）《》「」『』“”’‘—…":
            return True
    return False


def _convert_chinese_lines_to_zh_tw_in_srt(srt_content: str) -> str:
    """
    僅轉換字幕中的簡體中文為繁體中文：
    - 保留索引與時間碼行
    - 保留 "SPEAKER X: " 標籤，只翻譯其後的內容
    - 僅在行內判斷為簡體時才呼叫 LLM
    """
    if not srt_content or not srt_content.strip():
        print("[DEBUG] 簡轉繁：收到空的 SRT 內容，直接返回原文。")
        return srt_content

    oc = None
    try:
        oc = OpenCC('s2twp') if OpenCC else None
    except Exception:
        oc = None
    if not oc:
        print("[DEBUG] 簡轉繁：OpenCC 不可用，跳過簡轉繁處理並返回原文。")
        return srt_content

    lines = srt_content.splitlines()
    print(f"[DEBUG] 簡轉繁：開始處理字幕，共 {len(lines)} 行")
    converted_lines: List[str] = []
    total_empty = 0
    total_time_or_index = 0
    total_no_chinese = 0
    total_translated = 0
    total_errors = 0

    for line in lines:
        raw = line
        stripped = raw.strip()
        if not stripped:
            total_empty += 1
            converted_lines.append(raw)
            continue
        # 索引行（純數字）或時間碼行直接保留
        if stripped.isdigit() or _is_timecode_line(stripped):
            total_time_or_index += 1
            converted_lines.append(raw)
            continue

        # 嘗試處理 SPEAKER 前綴
        prefix = ""
        content = raw
        if stripped.startswith("SPEAKER ") and ":" in stripped:
            # 找到第一個冒號後的內容（保留原始前綴與空白）
            try:
                # 以原始行為準，避免空白變動
                colon_idx = raw.index(":")
                prefix = raw[: colon_idx + 1]  # 含冒號
                # 後面若有空白，保留一個空白做拼接
                rest = raw[colon_idx + 1 :]
                # 去掉一個前導空白，保留其餘原樣
                content = rest.lstrip(" ")
                leading = " " if rest.startswith(" ") else ""
                prefix = prefix + leading
            except Exception:
                prefix = ""
                content = raw

        # 只要行內包含中文，就轉繁體
        if _contains_chinese(content):
            try:
                translated = oc.convert(content)
                converted_lines.append(f"{prefix}{translated}")
                total_translated += 1
                continue
            except Exception as e:
                total_errors += 1
                print(f"[ERROR] 簡轉繁：單行轉換失敗：{e}")
                converted_lines.append(raw)
                continue
        else:
            total_no_chinese += 1
            converted_lines.append(raw)

    print(
        f"[DEBUG] 簡轉繁完成：總行數={len(lines)}, 空白行={total_empty}, 時間碼/索引行={total_time_or_index}, "
        f"無中文={total_no_chinese}, 成功翻譯={total_translated}, 失敗={total_errors}"
    )
    return "\n".join(converted_lines)

def process_meeting(app, meeting_id, file_path_str, progress_queue, num_speakers=None, supplementary_file_path=None):
    """
    處理會議音訊的主要函式 - 完整改為本地處理（含轉錄與語者分離），再產出摘要。
    此函式在獨立的執行緒中運行，需要傳入 app context 和 progress_queue。
    """
    with app.app_context():
        progress_tracker = current_app.progress_tracker

        def report_progress(step, progress, message):
            """輔助函式，用於回報進度"""
            print(f"[INFO] 會議 {meeting_id} 進度: {message} ({progress}%)")
            # 移除 SSE 佇列傳遞，僅更新最新快照
            # 併寫入最新進度供輪詢使用
            try:
                current_app.progress_latest[meeting_id] = {
                    'step': step,
                    'progress': progress,
                    'message': message
                }
            except Exception:
                pass

        try:
            print(f"[INFO] 開始處理會議 {meeting_id}")
            
            # --- 設定配置 ---
            # 假設 app 的根目錄是 AMI/app/
            base_dir = Path(app.root_path).parent 
            processed_folder = base_dir / "app" / "additional" / "meetgenius" / "processed"
            output_folder = base_dir / "app" / "additional" / "meetgenius" / "output"
            
            # 確保資料夾存在
            try:
                processed_folder.mkdir(parents=True, exist_ok=True)
                output_folder.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                raise Exception(f"無法創建必要的資料夾: {str(e)}")

            file_path = Path(file_path_str)
            
            # 驗證輸入檔案
            if not file_path.exists():
                raise Exception(f"輸入檔案不存在: {file_path}")
            if file_path.stat().st_size == 0:
                raise Exception("輸入檔案為空")
            try:
                file_size_mb = file_path.stat().st_size / (1024 * 1024)
                print(f"[INFO] 輸入檔案: {file_path.name}, 大小: {file_size_mb:.2f} MB, 副檔名: {file_path.suffix}")
            except Exception:
                pass
            
            # 檢查檔案格式
            supported_formats = {'.mp3', '.wav', '.mp4', '.m4a', '.webm', '.ogg'}
            if file_path.suffix.lower() not in supported_formats:
                raise Exception(f"不支援的檔案格式: {file_path.suffix}。支援的格式: {', '.join(supported_formats)}")
            print(f"[DEBUG] 檔案格式檢查通過，支援格式: {', '.join(sorted(supported_formats))}")
            
            # 更新狀態為處理中
            db.update_meeting_status(meeting_id, 'processing')
            
            # 針對 MP4/M4A/MOV 先做無重編碼重封裝（-movflags +faststart），避免 moov 在尾端導致解析問題
            source_for_conversion = file_path
            try:
                if file_path.suffix.lower() in {'.m4a', '.mp4', '.mov'}:
                    source_for_conversion = _remux_mp4_faststart(file_path, processed_folder)
                    if source_for_conversion != file_path:
                        print(f"[INFO] 已重封裝（faststart）: {source_for_conversion}")
            except Exception:
                pass

            # 將音訊轉為體積更小的 OGG/OPUS（16kHz/mono），提升上傳穩定性
            converted_audio_path = None
            converted_content_type = None
            try:
                converted_audio_path, converted_content_type = _convert_audio_for_azure(source_for_conversion, processed_folder)
                print(f"[INFO] 轉檔完成: {converted_audio_path} ({converted_content_type})")
            except Exception as e:
                raise Exception(f"前置轉檔失敗: {e}")

            # 額外輸出高相容播放用 MP3 檔案（供前端播放器優先串流）
            try:
                mp3_path = _export_playback_mp3(source_for_conversion, processed_folder, meeting_id)
                try:
                    print(f"[INFO] 已生成播放用 MP3: {mp3_path} 大小: {mp3_path.stat().st_size/1024:.2f} KB")
                except Exception:
                    pass
            except Exception as e:
                print(f"[WARN] 產生播放用 MP3 失敗，將回退至其他格式: {e}")

            # 定義輸出路徑（僅保留含說話者標籤的 SRT）
            srt_path = output_folder / f"{meeting_id}.srt"
            
            # 步驟 1-4: 使用 Azure 快速轉錄 + 語者分離（由 Azure 提供 diarization）
            try:
                report_progress('processing', 20, '使用 Azure 進行轉錄中...')
                print(f"[INFO] 開始 Azure 轉錄，會議: {meeting_id}")

                region = os.getenv("AZURE_SPEECH_REGION", "eastus")
                api_version = os.getenv("AZURE_SPEECH_API_VERSION", "2024-11-15")
                subscription_key = os.getenv("AZURE_SPEECH_KEY")

                # 多語系清單（Fast Transcription 只會輸出這些語系，不會輸出 zh-TW）
                locales = [
                    "en-US", "ja-JP", "es-ES", "fr-FR", "ko-KR", "pt-PT", "ru-RU", "de-DE", "zh-CN"
                ]

                if not subscription_key:
                    raise Exception("缺少 AZURE_SPEECH_KEY，請設定環境變數後重試。")

                # 取得音檔時長（使用 ffprobe，若不可用則回退 0.0）
                duration = 0.0
                try:
                    duration = _get_audio_duration_ffprobe(converted_audio_path)
                except Exception:
                    duration = 0.0

                data = _azure_fast_transcribe_sync(
                    audio_path=converted_audio_path,
                    content_type=converted_content_type,
                    region=region,
                    api_version=api_version,
                    subscription_key=subscription_key,
                    locales=locales,
                    timeout=(30, 300),
                    max_speakers=num_speakers,
                )

                # 產生單一 SRT（含說話者標籤）
                srt_content = _segment_by_phrase_punct_only(data, include_speaker_label=True)

                # 偵測主語系（以 phrases.locale 眾數）
                dominant_locale = None
                try:
                    from collections import Counter
                    phrase_locales = [
                        (ph.get("locale") or "").strip()
                        for ph in (data.get("phrases", []) or [])
                        if ph.get("locale")
                    ]
                    if phrase_locales:
                        dominant_locale = Counter(phrase_locales).most_common(1)[0][0]
                except Exception:
                    dominant_locale = None

                # 僅在偵測為簡體中文 zh-CN 時使用 OpenCC 進行簡轉繁
                performed_opencc = False
                try:
                    norm_locale = (dominant_locale or "").strip().lower()
                    is_simplified_cn = (norm_locale == "zh-cn")
                    if is_simplified_cn:
                        srt_content = _convert_chinese_lines_to_zh_tw_in_srt(srt_content)
                        performed_opencc = True
                except Exception as _e:
                    print(f"[WARN] 簡轉繁處理失敗，保留原始 SRT：{_e}")

                # 實際語者數（以 phrases 的 speaker 欄位估算）
                phrases = data.get("phrases", []) or []
                speakers = set()
                for ph in phrases:
                    sp = ph.get("speakerId") or ph.get("speaker") or ph.get("speakerNumber")
                    if sp is not None:
                        speakers.add(sp)
                actual_num_speakers = max(len(speakers), 1)

                msg_after_transcribe = '轉錄完成，正在儲存逐字稿...'
                if performed_opencc:
                    msg_after_transcribe += '（已簡轉繁）'
                report_progress('processing', 70, msg_after_transcribe)

                # 直接儲存繁體版逐字稿至主要 SRT 路徑
                with open(srt_path, "w", encoding="utf-8") as f:
                    f.write(srt_content)
                try:
                    print(f"[INFO] 已寫入繁體 SRT: {srt_path} 大小: {srt_path.stat().st_size/1024:.2f} KB")
                except Exception:
                    pass

                # 驗證檔案是否成功儲存
                if not srt_path.exists() or srt_path.stat().st_size == 0:
                    raise Exception("SRT 檔案儲存失敗")

                try:
                    print(f"[DEBUG] 儲存完成：SRT 路徑={srt_path}, 檔案存在={srt_path.exists()} 大小={srt_path.stat().st_size if srt_path.exists() else 'N/A'} bytes")
                except Exception:
                    pass

                report_progress('processing', 85, '繁體逐字稿儲存完成')

            except Exception as e:
                raise Exception(f"語音處理過程中發生錯誤: {str(e)}")

            # 步驟 5: 生成會議摘要（本地處理）
            report_progress('summarization', 90, '正在生成會議摘要...')
            summary_id = None
            try:
                # 直接讀取單一 SRT（已含發言人與時間戳）
                with open(srt_path, 'r', encoding='utf-8') as f:
                    speaker_srt_content = f.read()
                
                if speaker_srt_content and speaker_srt_content.strip():
                    # 參考文件（議程／簡報等）：僅用於校正專有名詞與補充背景。
                    # 解析失敗只會回傳空字串，不影響會議本身的處理。
                    reference_text = read_reference_document(supplementary_file_path)
                    if reference_text:
                        report_progress('summarization', 92, '已載入參考文件，正在生成會議摘要...')
                        print(f"[INFO] 會議 {meeting_id} 使用參考文件，長度 {len(reference_text)} 字元")

                    summary_data = get_summary_from_transcript(
                        speaker_srt_content,
                        reference_text=reference_text,
                    )

                    if summary_data and not summary_data.get('error'):
                        summary_id = db.create_global_summary(
                            meeting_id=meeting_id,
                            summary=summary_data.get('summary'),
                            action_items=summary_data.get('action_items', []),
                            keywords=summary_data.get('keywords', []),
                            faq=summary_data.get('faq', []),
                            decisions=summary_data.get('decisions', []),
                            positions=summary_data.get('positions', [])
                        )
                        report_progress('summarization', 98, '會議摘要生成完畢')
                        try:
                            print(f"[INFO] 會議 {meeting_id} 摘要已建立，ID: {summary_id}")
                        except Exception:
                            pass
                    else:
                        print(f"[WARNING] 會議 {meeting_id} 摘要生成失敗: {summary_data.get('error', '未知錯誤')}")
                        report_progress('summarization', 98, '會議摘要生成失敗')
                else:
                    print(f"[WARNING] 會議 {meeting_id}: 逐字稿內容為空，跳過摘要生成")
                    report_progress('summarization', 98, '逐字稿為空，跳過摘要')
            except Exception as e:
                print(f"[ERROR] 會議 {meeting_id} 摘要生成過程中發生錯誤: {e}")
                report_progress('summarization', 98, f'摘要生成失敗: {e}')

            # 更新為完成狀態
            try:
                # 若為 zh-CN（Azure 回傳），儲存為 zh-TW 以利下游翻譯判斷
                persisted_language = 'zh-TW' if ((dominant_locale or '').strip().lower() == 'zh-cn') else dominant_locale

                db.update_meeting_status(
                    meeting_id, 
                    'completed',
                    processed_at=datetime.now().isoformat(),
                    duration=duration,
                    num_speakers=actual_num_speakers,
                    srt_path=str(srt_path.relative_to(base_dir)),
                    summary_id=summary_id,
                    language=persisted_language
                )
                report_progress('completed', 100, '處理完成！')
                print(f"[INFO] 會議 {meeting_id} 處理完成")
                
            except Exception as e:
                print(f"[ERROR] 會議 {meeting_id} 更新完成狀態失敗: {e}")
                # 即使狀態更新失敗，也不應該影響處理結果
                report_progress('completed', 100, '處理完成但狀態更新失敗')
                
        except Exception as e:
            error_message = str(e)
            print(f"[ERROR] 處理會議 {meeting_id} 失敗: {error_message}")
            
            # 更新失敗狀態
            try:
                db.update_meeting_status(meeting_id, 'failed', error_message=error_message)
            except Exception as db_error:
                print(f"[ERROR] 無法更新會議 {meeting_id} 的失敗狀態: {db_error}")
                
            if progress_queue:
                progress_queue.put({'step': 'failed', 'progress': 100, 'message': error_message})
        finally:
            # 發送結束信號並清理
            if meeting_id in progress_tracker:
                del progress_tracker[meeting_id]
                print(f"[INFO] 已清理會議 {meeting_id} 的進度追蹤器。")
            # 清理 latest 緩存
            try:
                # 將最後狀態保留為 completed/failed 的快照
                latest = current_app.progress_latest.get(meeting_id)
                if latest and latest.get('step') in {'completed', 'failed'}:
                    pass
                else:
                    current_app.progress_latest[meeting_id] = {'step': 'completed', 'progress': 100, 'message': '處理完成！'}
            except Exception:
                pass
