"""簡體轉繁體（台灣用語）共用工具。

LLM 即使被要求以繁體中文回應，仍可能夾雜簡體字或中國慣用詞，
因此在資料落地前統一過一次 OpenCC `s2twp`（簡體 → 繁體台灣正體，含慣用詞轉換）。
"""

try:
    from opencc import OpenCC
    _converter = OpenCC('s2twp')
except Exception as e:  # pragma: no cover - 僅在缺少 opencc 套件時發生
    print(f"[WARNING] OpenCC 不可用，將略過簡轉繁：{e}")
    _converter = None


def to_traditional(text):
    """把字串轉為繁體中文（台灣用語）。非字串或轉換失敗時原樣回傳。"""
    if not _converter or not isinstance(text, str) or not text:
        return text
    try:
        return _converter.convert(text)
    except Exception as e:
        print(f"[WARNING] 簡轉繁失敗，保留原文：{e}")
        return text


def convert_deep(data):
    """遞迴轉換巢狀結構（dict / list / str）中的所有字串值。

    dict 的 key 與非字串型別（數字、None、bool）維持不變。
    """
    if isinstance(data, str):
        return to_traditional(data)
    if isinstance(data, dict):
        return {key: convert_deep(value) for key, value in data.items()}
    if isinstance(data, list):
        return [convert_deep(item) for item in data]
    return data
