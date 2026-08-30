import os
import re
from openai import OpenAI
import json

from .chinese import convert_deep

def _srt_time_to_seconds(time_str: str) -> float:
    """將 SRT 時間格式 'HH:MM:SS,ms' 轉換為總秒數。"""
    h, m, s_ms = time_str.replace(',', '.').split(':')
    return int(h) * 3600 + int(m) * 60 + float(s_ms)

def parse_srt(srt_content: str) -> list[dict]:
    """將 SRT 檔案內容解析為結構化列表。"""
    segments = []
    if not srt_content:
        return segments
    
    # 使用正確的正規表示式來分割 SRT 段落（段落間用空行分隔）
    blocks = re.split(r'\n\s*\n', srt_content.strip())
    
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) >= 3:  # SRT 格式：ID行 + 時間行 + 至少一行文字
            try:
                segment_id = lines[0].strip()
                time_line = lines[1].strip()
                text_lines = lines[2:]
                
                # 檢查時間行格式
                if '-->' not in time_line:
                    continue
                
                start_str, end_str = [t.strip() for t in time_line.split('-->')]
                start_time = _srt_time_to_seconds(start_str)
                end_time = _srt_time_to_seconds(end_str)
                
                text = ' '.join(line.strip() for line in text_lines if line.strip())

                segments.append({
                    "id": segment_id,
                    "start": start_time,
                    "end": end_time,
                    "text": text,
                })
                
            except (ValueError, IndexError):
                continue
    
    print(f"[DEBUG] 成功解析 {len(segments)} 個 SRT 段落")
    return segments

def get_summary_from_transcript(
    speaker_srt_content: str,
    model: str = None,
    reference_text: str = None
) -> dict:
    """
    從會議逐字稿生成摘要、決議、立場、待辦事項、關鍵字與常見問答。

    Args:
        speaker_srt_content (str): 帶有發言者標籤的 SRT 內容，用於時間戳和內容。
        model (str, optional): 要使用的 OpenAI 模型名稱，預設讀取 OPENAI_MODEL 環境變數。
        reference_text (str, optional): 使用者上傳的參考文件（議程、簡報等）文字內容。
            僅用於校正專有名詞與補充背景，不會被當成會議中說過的話。

    Returns:
        dict: 包含 summary / decisions / positions / action_items / keywords / faq，或錯誤訊息。
    """
    
    # 步驟 1 & 2: 解析 _speaker.srt 並建立 ID 到開始時間的映射
    segments = parse_srt(speaker_srt_content)
    if not segments:
        return {"error": "SRT content is empty or could not be parsed."}
        
    id_to_start_time = {seg["id"]: seg["start"] for seg in segments}

    # 步驟 3: 準備帶有 ID 的 prompt
    # 注意：這裡必須是真正的換行。先前誤用 "\\n"（字面反斜線 + n），
    # 導致整份逐字稿擠在同一行送給模型，嚴重影響話輪判讀。
    prompt_transcript = [f"<{seg['id']}> {seg['text']}" for seg in segments]
    formatted_transcript = "\n".join(prompt_transcript)

    # 步驟 4: 更新系統提示，要求提供 source ID
    system_prompt = """
You are a meeting analyst. You read a transcript and produce a structured record that someone can act on without listening to the recording. Accuracy of attribution matters more than completeness: a record that assigns work to the wrong party is worse than one that says "unassigned".

The transcript is given one utterance per line, each prefixed with a segment ID like `<12>`. Speakers appear as labels such as `SPEAKER 1` or a real name if one was set.

Return a single valid JSON object with exactly these keys: "summary", "decisions", "positions", "action_items", "keywords", "faq".

===============================================================
GROUND RULES (these override everything below)
===============================================================

1. NEVER invent an owner. Assign work only to the party the transcript actually names. If a speaker says "本地智慧要改成…" the owner is the external company 本地智慧 — NOT an internal department. If nobody is named, use owner_type "unassigned" and leave owner empty.
2. NEVER promote a hypothesis to a conclusion. If the group ruled something out, that belongs in "decisions" as a ruled-out item, not in the summary as a fact.
3. Distinguish "we/我們/我" (the speakers' own organisation or the speaker personally) from any third party named in the transcript. Two organisations sharing blame must both be named.
4. Quote nothing verbatim in "summary". Never write a segment ID (e.g. <3>) anywhere except in a "source" array.
5. If the transcript does not support a field, return an empty array rather than filling it with plausible content.
6. A REFERENCE DOCUMENT may be supplied after the transcript. It is context only — see the rules below. Everything you output must be traceable to the TRANSCRIPT.

===============================================================
REFERENCE DOCUMENT (only when one is provided)
===============================================================

The user may attach an agenda, deck, or notes. Speech-to-text mangles unfamiliar names, so the document exists to help you spell things correctly and understand context.

USE it to:
  - Correct proper nouns the transcript garbled — people, companies, products, systems, project codes. If the transcript says "本地智慧" and the document says "本地智匯", prefer the document's spelling.
  - Understand acronyms and jargon that speakers use without explaining.
  - Recognise which organisation the speakers belong to, which sharpens the "team" vs "external" call.

NEVER use it to:
  - Add a decision, position, action item, or FAQ that nobody actually discussed. An agenda listing a topic is NOT evidence the topic was discussed.
  - Fill in an owner or deadline that the transcript did not state, even if the document assigns one.
  - Write summary bullets about material that only appears in the document.

Every "source" segment ID must point at the transcript. The document has no segment IDs, so anything you cannot anchor to a transcript segment does not belong in the output.

===============================================================
FIELDS
===============================================================

1. "summary" — string.
Markdown bullet points covering what was discussed and what it means. Cover the problem, its root cause (including every party responsible), and where things landed. If responsibility is shared between parties, say so explicitly and name each party. 150-300 characters per bullet, 4-7 bullets.

2. "decisions" — array of objects. Conclusions about WHAT IS TRUE or WHICH APPROACH WAS CHOSEN.

CRITICAL BOUNDARY — decisions vs action_items:
  - A decision records a CONCLUSION: a diagnosis the group settled on, an explanation they accepted, an option they picked over another, or a theory they threw out.
  - Work that still has to be performed is an ACTION ITEM, never a decision — even when the group agreed to do it in this meeting. "本地智慧要改成產生不重複編號" is work → action_items (owner 本地智慧, owner_type external). Do NOT file it as a decision.
  - If in doubt, ask: "is there something left to do?" If yes → action_items.

Each object:
  - "decision": what was concluded, in one sentence.
  - "ruled_out": array of strings — explanations, causes, or options the group explicitly rejected.
  - "source": array of 1-3 segment IDs.

Pay special attention to negations. When a speaker says "不是A，也不是B，是C" / "it's not A, and not simply B — it's C", that is one decision: decision = C, ruled_out = [A, B]. Rejected explanations are among the most valuable content in a meeting because they stop other people re-investigating them. Never discard them.

Return an empty array only if the group genuinely settled nothing.

3. "positions" — array of objects. Moments where a speaker pushed, doubted, demanded, or resisted. This is about the human dynamic, NOT the technical facts.
Each object:
  - "topic": the point at issue, one short phrase.
  - "stances": array of {"speaker": <speaker label as it appears>, "position": one sentence describing what they argued, asked for, or resisted}.
  - "outcome": one of "consensus" (they agreed), "unresolved" (still open), "deferred" (parked for later), "clarified" (one side explained until the other understood).
  - "source": array of 1-3 segment IDs.

Include a position whenever any of these appear — each is a separate entry, do not merge them:
  - someone challenges a claim or asks why something is the case
  - someone says they do not understand, or asks for a different explanation ("你可以不要用工程師的說法嗎？")
  - someone demands a plan for a specific situation ("修好以前要怎麼處理？", "修好以後怎麼確認可以上？")
  - someone disagrees, hesitates, or expresses concern about a proposal
  - someone assigns responsibility or pushes it elsewhere

A meeting with several speakers almost always contains more than one such moment. Look for them deliberately before concluding there are none.

4. "action_items" — array of objects. Concrete work someone is expected to do after the meeting.
Each object:
  - "item": the task in one sentence. Include the deadline if one was stated.
  - "owner": the responsible party exactly as the transcript names it (a person, a team, or an external company). Empty string ONLY if the transcript truly names nobody — if the task sentence itself names a team ("支援團隊需…"), that team is the owner.
  - "owner_type": one of
        "person"      — a named individual, or a speaker who committed in the first person
        "team"        — a department or team inside the speakers' own organisation
        "external"    — a company or party OUTSIDE the speakers' organisation
        "unassigned"  — the transcript names no owner

  How to tell "team" from "external": an organisation is EXTERNAL only when the speakers talk about it in the third person as a separate company ("他們", "本地智慧那邊要改"). If the speakers ever refer to that organisation's system or work as "我們" / "我們這邊" / "our side", it is INTERNAL — use "team" even though it has a company name. Read the whole transcript before deciding; the two organisations in a vendor discussion are rarely both external.
  - "precondition": what must happen first, if the transcript states a dependency ("等兩邊修完之後…", "上線前…"). Empty string when the task can start immediately.
  - "source": array of 1-3 segment IDs.

Rules for action_items:
  - Capture EVERY piece of outstanding work, including:
      * a first-person future commitment ("我會整理…", "我來處理…") — owner = that speaker, owner_type "person". These are the most reliable action items in any transcript; never drop one.
      * a fix a party is expected to make to their own system ("本地智慧要讓每筆付款有不重複編號", "我們要改成…")
      * interim handling instructions for the period before a fix ships ("修好以前，支援就先查…不退款也不用請客人重刷")
      * verification or rollout steps ("在測試環境讓不同分店同時送出多筆付款…才安排更新")
  - Work described as belonging to a third-party company gets owner_type "external". Do not translate it into an internal department.
  - When the transcript says "我們" (the speakers' own organisation) and that organisation is named elsewhere, use that name as owner with owner_type "team".
  - Do not turn a question, a diagnosis, or a statement of fact into a task.
  - One item = one owner and one precondition. NEVER combine two pieces of work into a single item when they have different owners, or when one can start now and the other must wait ("整理受影響的交易" can start immediately; "在測試環境驗證" waits for both fixes — these are two items, not one).
  - Conversely, do not split a single piece of work into artificial sub-steps.
  - A meeting that identified a problem and agreed on fixes normally yields 3-6 action items. If you produced fewer than 3, re-read the transcript for work you skipped.

5. "keywords" — array of 5-10 strings. Domain-specific terms, systems, and parties that actually appeared. No generic words like "會議" or "討論".

6. "faq" — array of 3-6 objects, each {"question", "answer", "source"}.
Questions someone who ATTENDED would ask afterwards to recall reasoning — "why did we conclude X", "what happens between now and the fix". Not fact-checking, not restated tasks. "source" is an array of 1-3 segment IDs.

===============================================================
OUTPUT
===============================================================

Respond in Traditional Chinese (Taiwan) for all human-readable text. Keep "owner_type" and "outcome" as the exact English enum values listed above. Speaker labels stay exactly as they appear in the transcript.

Return only the JSON object.
"""
    user_prompt = f"""Analyse the following meeting transcript. Each line is one utterance prefixed with its segment ID.

TRANSCRIPT
```
{formatted_transcript}
```"""

    if reference_text and reference_text.strip():
        user_prompt += f"""

REFERENCE DOCUMENT — context only. Use it to spell names correctly and understand jargon.
Do NOT derive any decision, position, action item, or FAQ from it; nothing here was necessarily said in the meeting.
```
{reference_text.strip()}
```"""

    try:
        subscription_key = os.environ["OPENAI_API_KEY"]
        deployment = model or os.getenv("OPENAI_MODEL", "gpt-4o")

        client = OpenAI(api_key=subscription_key)

        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            # 這是結構化抽取而非創作；溫度高會鼓勵模型改寫與腦補，
            # 先前 0.7 是造成負責人張冠李戴的可能原因之一。
            temperature=0.2,
            response_format={"type": "json_object"},
            model=deployment
        )
        
        raw_response_content = response.choices[0].message.content
        
        raw_response_data = json.loads(raw_response_content)

        # 步驟 5: 後處理，將 source ID 轉換為 jump 時間
        processed_data = raw_response_data.copy()

        def add_jump_time(items):
            """把 source 的 segment ID 換算成音檔跳轉時間（取最早的那段）。"""
            if not isinstance(items, list):
                return

            for item in items:
                if not isinstance(item, dict):
                    continue
                source_ids = item.pop("source", None) or []
                if isinstance(source_ids, (str, int)):
                    source_ids = [source_ids]
                start_times = [
                    id_to_start_time[str(sid)]
                    for sid in source_ids
                    if str(sid) in id_to_start_time
                ]
                if start_times:
                    item["jump"] = min(start_times)

        for key in ("action_items", "faq", "decisions", "positions"):
            add_jump_time(processed_data.get(key))

        # 確保新欄位一定存在，前端與匯出不需再做防禦性判斷
        for key in ("decisions", "positions", "action_items", "keywords", "faq"):
            processed_data.setdefault(key, [])

        # 步驟 6: 保險起見統一轉繁體（LLM 偶爾仍會夾雜簡體字或中國慣用詞）
        processed_data = convert_deep(processed_data)

        return processed_data

    except json.JSONDecodeError as e:
        print(f"[ERROR] 無法解析來自 LLM 的 JSON 回應: {e}")
        return {
            "error": "Failed to parse JSON response from LLM.",
            "raw_response": response.choices[0].message.content if 'response' in locals() else "No response"
        }
    except Exception as e:
        print(f"[ERROR] 與 OpenAI 互動時發生錯誤: {e}")
        return {
            "error": f"An error occurred while communicating with OpenAI: {e}"
        }

