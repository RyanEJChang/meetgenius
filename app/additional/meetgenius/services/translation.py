"""
翻譯服務模組 (使用標準 OpenAI API)

提供使用 OpenAI 的翻譯服務。
"""
import os
import time
from typing import List, Dict
from openai import OpenAI

from ..utils.chinese import to_traditional, convert_deep

class TranslationError(Exception):
    """翻譯相關錯誤"""
    pass

class OpenAITranslator:
    """使用 OpenAI 服務的翻譯器"""

    def __init__(self, api_key: str, model: str):
        if not all([api_key, model]):
            raise ValueError("缺少 OpenAI 的必要設定 (api_key, model)")

        self.client = OpenAI(api_key=api_key)
        self.deployment = model

    def translate_text(self, text: str, target_language: str, source_language: str = 'auto') -> str:
        """使用 OpenAI GPT 翻譯文字"""
        if not text or not text.strip():
            return text

        # 語言代碼映射到語言名稱，讓 LLM 更易理解
        language_names = {
            'en': 'English',
            'zh-tw': 'Traditional Chinese',
            'ja': 'Japanese',
            'ko': 'Korean',
            'es': 'Spanish',
            'fr': 'French',
            'de': 'German',
            'ru': 'Russian'
        }
        target_lang_name = language_names.get(target_language.lower(), target_language)
        source_lang_name = language_names.get(str(source_language).lower(), source_language)
        source_lang_info = f"from {source_lang_name}" if source_language != 'auto' else ""

        prompt = (f"Please translate the following text {source_lang_info} to {target_lang_name}. "
                  "You must only provide the translation itself, without any additional explanations, "
                  "commentary, or introductory text. Just the translated text.")
        
        try:
            response = self.client.chat.completions.create(
                model=self.deployment,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0.3
            )
            
            # 檢查回應是否有效
            if not response or not response.choices or len(response.choices) == 0:
                raise TranslationError("OpenAI API 回應無效：沒有回應選項")
            
            message_content = response.choices[0].message.content
            if message_content is None:
                print(f"[WARNING] OpenAI 回應內容為空，原文：{text[:50]}...")
                return text  # 回傳原文
            
            translated_text = message_content.strip()
            if not translated_text:
                print(f"[WARNING] OpenAI 翻譯結果為空，原文：{text[:50]}...")
                return text  # 回傳原文

            # 目標語言為繁體中文時，統一過一次簡轉繁（LLM 偶爾會回簡體或中國慣用詞）
            if target_language.lower() in ('zh-tw', 'zh-hant'):
                translated_text = to_traditional(translated_text)

            return translated_text
            
        except TranslationError:
            # 重新拋出 TranslationError
            raise
        except Exception as e:
            # 捕獲來自 openai 套件的錯誤或其他請求錯誤
            print(f"[ERROR] OpenAI API 請求異常：{e}")
            print(f"[ERROR] 原始文字：{text[:100]}...")
            raise TranslationError(f"OpenAI API 請求失敗: {e}")

class TranslationService:
    """翻譯服務統一管理器 (僅使用 OpenAI)"""
    
    def __init__(self):
        self.translator: OpenAITranslator = None
        self._setup_translator()
    
    def _setup_translator(self):
        """設定 OpenAI 翻譯器"""
        try:
            api_key = os.environ["OPENAI_API_KEY"]
            model = os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4o-mini")

            if all([api_key, model]):
                self.translator = OpenAITranslator(
                    api_key=api_key,
                    model=model
                )
                # print(f"[INFO] OpenAI 翻譯器已啟用 (模型: {model})")
            else:
                print("[WARNING] OpenAI 設定不完整，翻譯功能將無法使用。")
        except Exception as e:
            print(f"[ERROR] 初始化 OpenAI 翻譯器失敗: {e}")

    def is_available(self) -> bool:
        """檢查翻譯服務是否可用"""
        return self.translator is not None

    def test_connection(self) -> bool:
        """測試翻譯服務連接是否正常"""
        if not self.is_available():
            return False
        
        try:
            # 使用簡單的測試文字（固定測試繁體中文）
            test_result = self.translate_text("Hello", "zh-tw")
            return test_result is not None and len(test_result.strip()) > 0
        except Exception as e:
            print(f"[ERROR] 翻譯服務連接測試失敗: {e}")
            return False

    def translate_text(self, text: str, target_language: str, source_language: str = 'auto') -> str:
        """翻譯單一文字"""
        if not self.is_available():
            raise TranslationError("翻譯服務尚未設定或初始化失敗。")
        return self.translator.translate_text(text, target_language, source_language)

    def translate_meeting_summary(self, summary_data: Dict, target_language: str, progress_callback=None) -> Dict:
        """翻譯會議摘要"""
        if not self.is_available():
            raise TranslationError("翻譯服務尚未設定。")

        translated_summary = {}

        # 翻譯主要摘要
        if summary_data.get('summary'):
            try:
                translated_summary['summary'] = self.translate_text(
                    summary_data['summary'], target_language
                )
                if progress_callback:
                    progress_callback()
                print("[INFO] 摘要翻譯完成")
            except Exception as e:
                print(f"[WARNING] 摘要翻譯失敗，保留原文: {str(e)}")
                translated_summary['summary'] = summary_data['summary']
            time.sleep(0.1) # 避免過於頻繁的請求

        # 翻譯待辦事項
        if summary_data.get('action_items'):
            try:
                translated_items = []
                for i, item_obj in enumerate(summary_data['action_items']):
                    # 確保我們處理的是字典中的 'item' 鍵
                    text_to_translate = ""
                    if isinstance(item_obj, dict) and 'item' in item_obj:
                        text_to_translate = item_obj['item']
                    elif isinstance(item_obj, str):
                        text_to_translate = item_obj

                    try:
                        if text_to_translate:
                            translated_text = self.translate_text(text_to_translate, target_language)
                            if isinstance(item_obj, dict):
                                # 維持原始結構，只替換文字
                                new_item_obj = item_obj.copy()
                                new_item_obj['item'] = translated_text
                                translated_items.append(new_item_obj)
                            else:
                                translated_items.append(translated_text)
                        else:
                            # 如果沒有可翻譯的文字，保留原始物件
                            translated_items.append(item_obj)
                        
                        if progress_callback:
                            progress_callback()
                    except Exception as e:
                        print(f"[WARNING] 待辦事項 {i+1} 翻譯失敗，保留原文: {str(e)}")
                        translated_items.append(item_obj)

                translated_summary['action_items'] = translated_items
                print(f"[INFO] 待辦事項翻譯完成，共 {len(translated_items)} 項")
            except Exception as e:
                print(f"[WARNING] 待辦事項翻譯整體失敗，保留原文: {str(e)}")
                translated_summary['action_items'] = summary_data['action_items']
            time.sleep(0.1)

        # 翻譯關鍵字
        if summary_data.get('keywords'):
            try:
                translated_keywords = []
                for i, keyword in enumerate(summary_data.get('keywords', [])):
                    try:
                        translated_keyword = self.translate_text(keyword, target_language)
                        translated_keywords.append(translated_keyword)
                        if progress_callback:
                            progress_callback()
                    except Exception as e:
                        print(f"[WARNING] 關鍵字 {i+1} 翻譯失敗，保留原文: {str(e)}")
                        translated_keywords.append(keyword)
                translated_summary['keywords'] = translated_keywords
                print(f"[INFO] 關鍵字翻譯完成，共 {len(translated_keywords)} 個")
            except Exception as e:
                print(f"[WARNING] 關鍵字翻譯整體失敗，保留原文: {str(e)}")
                translated_summary['keywords'] = summary_data['keywords']
            
        return translated_summary

    def translate_transcript_segments(self, segments: List[Dict], target_language: str, progress_callback=None) -> List[Dict]:
        """翻譯逐字稿段落"""
        if not self.is_available():
            raise TranslationError("翻譯服務尚未設定。")
            
        translated_segments = []
        failed_count = 0
        total_count = len(segments)
        
        for i, segment in enumerate(segments):
            translated_segment = segment.copy()
            if segment.get('content'):
                try:
                    translated_content = self.translate_text(segment['content'], target_language)
                    translated_segment['translated_content'] = translated_content
                    print(f"[INFO] 翻譯進度: {i+1}/{total_count}")
                except Exception as e:
                    failed_count += 1
                    print(f"[WARNING] 段落 {i+1} 翻譯失敗，保留原文: {str(e)}")
                    # 翻譯失敗時保留原文
                    translated_segment['translated_content'] = segment['content']
            else:
                # 沒有內容的段落
                translated_segment['translated_content'] = ""
            
            if progress_callback:
                progress_callback()

            translated_segments.append(translated_segment)
            time.sleep(0.1) # 每個段落之間稍微延遲
        
        if failed_count > 0:
            print(f"[WARNING] 逐字稿翻譯完成，共 {total_count} 段，{failed_count} 段翻譯失敗")
        else:
            print(f"[INFO] 逐字稿翻譯完成，共 {total_count} 段全部成功")
        
        return translated_segments

    def extract_speaker_highlights(self, transcript_segments: List[Dict], progress_callback=None) -> Dict:
        """從逐字稿中提取每個發言人的重點"""
        if not self.is_available():
            raise TranslationError("翻譯服務尚未設定或初始化失敗。")
        
        if not transcript_segments:
            return {}
        
        # 按發言人分組逐字稿內容
        speaker_content = {}
        for segment in transcript_segments:
            speaker = segment.get('speaker', '未知發言人')
            content = segment.get('content', '').strip()
            if content:
                if speaker not in speaker_content:
                    speaker_content[speaker] = []
                speaker_content[speaker].append(content)
        
        if not speaker_content:
            return {}
        
        speaker_highlights = {}
        total_speakers = len(speaker_content)
        
        for i, (speaker, contents) in enumerate(speaker_content.items()):
            try:
                # 更新進度
                if progress_callback:
                    progress = int((i / total_speakers) * 100)
                    progress_callback(f"分析發言人 {speaker} 的重點 ({i+1}/{total_speakers})", progress)
                
                # 合併該發言人的所有內容
                combined_content = " ".join(contents)
                
                # 如果內容太短，跳過分析
                if len(combined_content.strip()) < 50:
                    speaker_highlights[speaker] = {
                        'summary': f'{speaker} 的發言內容較少，無法提取重點。',
                        'key_points': [],
                        'word_count': len(combined_content)
                    }
                    continue
                
                # 使用 OpenAI 提取重點
                prompt = f"""
請分析以下發言人的會議發言內容，提取重點摘要：

發言人：{speaker}
發言內容：
{combined_content}

請以繁體中文回應，格式如下的 JSON：
{{
    "summary": "該發言人的主要觀點和貢獻的簡要摘要（2-3句話）",
    "key_points": [
        "重點1：具體的觀點或建議",
        "重點2：提出的問題或解決方案",
        "重點3：重要的決策或承諾"
    ]
}}

注意：
1. 摘要要客觀中性，突出該發言人的主要貢獻
2. 重點要具體明確，避免空泛的描述
3. 如果發言人提出問題、建議或承諾，要特別標註
4. 最多提取5個重點，按重要性排序
"""

                response = self.translator.client.chat.completions.create(
                    model=self.translator.deployment,
                    messages=[
                        {"role": "system", "content": "你是專業的會議分析師，擅長從發言內容中提取重點和洞察。"},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.3,
                    max_tokens=1000
                )
                
                response_content = response.choices[0].message.content.strip()
                
                # 嘗試解析 JSON 回應
                try:
                    import json
                    # 提取 JSON 部分
                    json_start = response_content.find('{')
                    json_end = response_content.rfind('}') + 1
                    if json_start >= 0 and json_end > json_start:
                        json_content = response_content[json_start:json_end]
                        parsed_response = json.loads(json_content)
                        
                        speaker_highlights[speaker] = {
                            'summary': to_traditional(parsed_response.get('summary', '無法提取摘要')),
                            'key_points': convert_deep(parsed_response.get('key_points', [])),
                            'word_count': len(combined_content)
                        }
                    else:
                        raise ValueError("無法找到有效的 JSON 格式")
                        
                except (json.JSONDecodeError, ValueError) as e:
                    print(f"[WARNING] 解析發言人 {speaker} 的回應失敗: {e}")
                    # 回退到簡單格式
                    speaker_highlights[speaker] = {
                        'summary': f'{speaker} 在會議中有重要發言，但無法自動提取結構化重點。',
                        'key_points': [response_content[:200] + '...' if len(response_content) > 200 else response_content],
                        'word_count': len(combined_content)
                    }
                    
            except Exception as e:
                print(f"[ERROR] 分析發言人 {speaker} 重點失敗: {e}")
                speaker_highlights[speaker] = {
                    'summary': f'分析 {speaker} 的發言時發生錯誤。',
                    'key_points': [],
                    'word_count': len(" ".join(contents)) if contents else 0
                }
        
        return speaker_highlights

# 全域翻譯服務實例
translation_service = TranslationService()

def get_translation_service() -> TranslationService:
    """獲取翻譯服務實例"""
    global translation_service
    if translation_service is None:
        translation_service = TranslationService()
    return translation_service 