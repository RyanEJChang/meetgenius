"""OpenAI / Azure OpenAI client 的統一建立點。

本專案原本只打原生 OpenAI API。改用 Azure AI Services 上的 Azure OpenAI 部署後，
client 的建立方式集中在這裡：

- 有設定 AZURE_OPENAI_ENDPOINT → 建立 AzureOpenAI client。此時 OPENAI_MODEL 與
  OPENAI_TRANSLATION_MODEL 的值是 **deployment 名稱**，不是 OpenAI 的模型名稱。
- 沒設定 → 退回原生 OpenAI client，行為與改動前完全相同。

兩種模式的金鑰都讀 OPENAI_API_KEY（Azure 模式下放的是 Azure 資源金鑰），
所以 create_app() 的必要環境變數檢查不需要跟著改。
"""
import os


def create_openai_client(api_key: str | None = None):
    """建立 OpenAI 或 AzureOpenAI client。

    Args:
        api_key (str, optional): 明確指定的金鑰；省略時讀 OPENAI_API_KEY 環境變數。

    Returns:
        openai.OpenAI 或 openai.AzureOpenAI 實例。兩者的 chat.completions 介面一致，
        呼叫端不需要區分。
    """
    key = api_key or os.environ["OPENAI_API_KEY"]
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()

    if endpoint:
        from openai import AzureOpenAI

        return AzureOpenAI(
            api_key=key,
            azure_endpoint=endpoint,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )

    from openai import OpenAI

    return OpenAI(api_key=key)
