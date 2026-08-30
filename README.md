# MeetGenius

會議錄音自動轉錄與分析服務。上傳音檔後自動完成語音轉文字、語者辨識，並整理出摘要、決議、各方立場、待辦事項與常見問答，所有結果都可點擊跳回音檔對應時間點。

---

## 運作流程

```
音檔上傳
  → ffmpeg 轉檔正規化
  → Azure Speech Fast Transcription（STT + 語者分離 + 詞級時間戳）
  → 依標點切分逐字稿，標註 SPEAKER N
  → OpenCC 簡轉繁（s2twp）
  → OpenAI Chat Completions 結構化抽取
  → 寫入 PostgreSQL
```

摘要階段回傳固定 schema 的 JSON：`summary`、`decisions`、`positions`、`action_items`、`keywords`、`faq`。每筆結果都必須錨定到逐字稿的 segment ID，後處理再把 ID 換算成音檔時間，因此前端每一條待辦或決議都能跳回它的出處。這個設計同時也是防幻覺的機制——錨不到逐字稿的內容不該存在。

---

## 快速開始

需要 Docker 與 Docker Compose。

```bash
git clone https://github.com/RyanEJChang/meetgenius.git
cd meetgenius
cp .env.example .env
```

編輯 `.env` 填入金鑰（見下節），然後：

```bash
docker compose up -d --build
```

服務啟動於 <http://localhost:8080>。

### 建立登入帳號

資料庫沒有對主機開放 port，因此**必須在容器內執行**：

```bash
docker compose exec app python scripts/create_user.py <username> <email> <password>
```

---

## 環境變數

| 變數 | 說明 |
|---|---|
| `SECRET_KEY` | Flask session 簽章金鑰，請改成隨機字串 |
| `MEETGENIUS_DB_NAME` / `_USER` / `_PASSWORD` | Postgres 連線設定，`docker compose` 會據此建立資料庫 |
| `MEETGENIUS_DB_HOST` / `_PORT` | 本機開發用；容器內會被 compose 覆寫為 `db:5432` |
| `OPENAI_API_KEY` | 摘要、翻譯所需 |
| `OPENAI_MODEL` | 摘要模型，預設 `gpt-4o` |
| `OPENAI_TRANSLATION_MODEL` | 翻譯模型，預設 `gpt-4o-mini` |
| `AZURE_SPEECH_KEY` | Azure Speech 資源金鑰 |
| `AZURE_SPEECH_REGION` | 資源所在區域，例如 `japanwest` |
| `AZURE_SPEECH_API_VERSION` | Fast Transcription API 版本，預設 `2024-11-15` |

`OPENAI_API_KEY` 與 `AZURE_SPEECH_KEY` 未設定時服務會直接結束並提示，不會半殘啟動。

---

## 支援格式

**會議音檔**（單檔上限 500 MB）
`.m4a` `.mp3` `.wav` `.flac` `.webm` `.mp4`

**參考文件**（選填）
`.pdf` `.docx` `.pptx` `.md` `.txt` `.csv`

### 關於參考文件

上傳議程或簡報可以顯著改善輸出品質。語音轉文字經常聽錯專有名詞——人名、公司名、產品代號、系統名稱——而參考文件提供了正確寫法。

這份文件**只用於校正名稱與理解術語**。prompt 明確禁止從文件衍生任何決議、立場、待辦或問答，因為議程列了某個主題不代表會議真的討論過。實測中刻意放入一項會議未討論的議程項目，未出現在任何輸出裡。

文件內容上限 6000 字元，超過會截斷，避免比重壓過逐字稿。解析失敗、格式不支援或檔案不存在都只記錄警告並略過，不會中斷整場會議的處理。

---

## 專案結構

```
app/
  __init__.py                    application factory、環境變數檢查
  auth/                          flask-login + bcrypt 登入
  additional/
    meetgenius/
      db.py                      PostgreSQL 資料層與 schema
      main/routes.py             頁面路由、上傳處理
      api/routes.py              進度輪詢、翻譯、匯出等 API
      services/
        processing.py            主流程：轉檔、Azure STT、簡繁轉換
        translation.py           逐字稿翻譯
      utils/
        transcription_processor.py   摘要 prompt 與後處理
        document_parser.py           參考文件解析
        chinese.py                   OpenCC 簡轉繁
    static/meetgenius/           前端樣式與腳本
    templates/meetgenius/        Jinja2 樣板
scripts/create_user.py           建立登入帳號
```

---

## 實作說明

**gunicorn 必須以 `workers=1` 執行。** 轉錄進度追蹤（`app.progress_latest`）與背景執行緒都存在單一 process 的記憶體中，多 worker 會導致前端輪詢不到進度。並行由 `gthread` 的 8 個執行緒處理。

**compose 專案名稱寫死為 `meetgenius`。** 若不指定，Docker Compose 會以資料夾名稱推導專案名，資料夾一改名就會建立全新的空 volume，既有資料庫與音檔會變成孤兒。

**輸出統一走 OpenCC `s2twp`。** Azure Fast Transcription 不輸出 `zh-TW`，中文一律回 `zh-CN`，因此需要簡轉繁；LLM 輸出也再過一次，避免模型自行產生簡體字。

---

## 已知限制

- **長會議未分段。** 整份逐字稿以單次請求送給模型，超長會議中段的待辦事項可能被遺漏。
- **`/additional/api/process-supplement` 為孤兒端點。** 它會為參考文件單獨產生一份摘要，但前端沒有任何地方呼叫。參考文件在主流程中已另有用途（見上文），此端點需以 API 手動觸發。
- **Azure Speech F0 免費層每月 5 小時音訊。** 超出需升級至 S0，Fast Transcription 費率約 USD 0.36 / 音訊小時，S0 無免費額度。

---

## 授權

尚未指定。
