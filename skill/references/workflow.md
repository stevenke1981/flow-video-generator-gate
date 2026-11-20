# Flow 工作狀態與本機驗收

Windows 以 Python 3.11 以上執行。工具使用標準函式庫，影片驗收需要 ffprobe 與 FFmpeg。預設搜尋環境變數 `FFPROBE`、`FFMPEG`、PATH 與專案既有的 `C:/tools/ffmpeg/bin`。

每個命令成功回傳 JSON，拒絕時回傳錯誤並以非零結束。用 `python "…/scripts/flow_tool.py" --help` 查參數。Windows 工作資料存在 `%LOCALAPPDATA%/flow-video-generator/flow-video-state.sqlite3`，透過工具讀寫。輸入 JSON、截圖及檔案可能含工作素材或帳號資訊，留在授權的本機工作位置。帳號識別在狀態庫中以雜湊保存。

## 1. prepare 鎖定本鏡規格

```json
{
  "job": "06_16",
  "action": "video",
  "project": "https://flow.google.com/project/00000000-0000-4000-8000-000000000001",
  "account": "example@example.invalid",
  "profile": "替換為 Chrome 設定檔名稱",
  "model": "Veo 3.1 Lite",
  "mode": "ingredients",
  "resolution": "720p",
  "aspect": "16:9",
  "seconds": 8,
  "outputs": 1,
  "refs": [
    {"kind": "character", "name": "Mimi"},
    {"kind": "image", "name": "06_16_B1"},
    {"kind": "image", "name": "06_16_B3"}
  ],
  "prompt": "實際要提交的逐字提示詞",
  "characters": ["Mimi"],
  "points_limit": 12,
  "needs_audio": true
}
```

`python flow_tool.py prepare plan.json` 回傳工作憑證 `receipt`。同一專案、同一工作要沿用原憑證。憑證在 `prepared` 或 `failed` 時，以修正後的計畫重跑 `prepare` 會更新同一張憑證（回傳 `plan_updated`），次數保留；其他狀態不能改計畫。同一 job 在所有專案合計最多 3 次 arm，換帳號或專案不會歸零。job 使用系列與鏡號可識別的穩定名稱，輪值到另一個專案時仍沿用相同 job。原專案已完成或結果未決時，另一個專案也不能登記重送。預算欄填已授權的單支上限，每筆計畫均須明填既有授權上限。以當集核准的 storyboard、`paste_list.py` 或其他既有逐鏡來源填計畫。有角色時列出全部入鏡角色並選實際 Flow 角色元件。若 Frames 不支援必要元件，在已獲主人同意的配置上填 `character_exception` 說明。工具接受 `text`、`ingredients`、`frames`。Frames 第一項是首圖，第二項可選填尾圖，其後只能是角色或語音元件，以實際模型支援為準。專案網址可帶語系、查詢參數或尾段，接受 `flow.google.com` 與 `labs.google` 的 `/project/<uuid>`。解析度 `4K` 會視為 `2160p`。同鏡 3 次用完後 `arm` 會拒絕，在交接記為 `split_required` 並拆鏡。圖片用 `action=image`、`mode=image`、`seconds=0`、`needs_audio=false`，解析度可填當次 UI 標示或精確寬高，如 `1376x768`。編輯、提升、既有影片下載分別用 `edit`、`upscale`、`download` 作 action 和 mode，填 `source_asset` 為原資產名稱或 URL。單純下載設定 `points_limit=0`。

## 2. arm 核對可見 UI 並登記一次操作

在受支援的 Chrome 介面確認實際登入帳號、專案與規格。從頁面讀回提示詞、按順序列出選中的參考，截圖記錄當次設定。以不同於計畫的檔案 `observed.json` 記錄畫面實際讀到的欄位，使用相同欄名並加上 `cost`、`credits` 及 `evidence`（截圖絕對路徑）。上傳及原生 Agent 規劃完成後，在最後確認生成前執行：

```text
python flow_tool.py arm RECEIPT observed.json
```

腳本逐欄比較，提示詞只正規化空白，逐字與標點仍須一致。已有未決項、異常活動停件、同鏡滿三次、點數超過上限、帳號或素材順序不符時拒絕。通過後回傳 `attempt`，並先記為 `armed`。只有拿到本次新回傳的 `attempt` 才在 Chrome 按一次對應的送出或核准。既有影片下載直接進可驗收狀態。

## 3. record 登記可見結果

```text
python flow_tool.py record RECEIPT ATTEMPT pending pending.png
python flow_tool.py record RECEIPT ATTEMPT succeeded result.png --asset "Flow 內實際的資產名稱"
```

實際終局可記 `succeeded`、`failed`、`unusual_activity` 或 `policy_refusal`。結果不明維持 `armed` 或 `pending`，從原帳號和專案查實後再記錄。中斷或畫面不明時用 `handoff RECEIPT "原因與待查事項"` 保存進度，憑證仍保留其狀態。異常活動停止自動提交。若主人自行完成同鏡且結果已在原專案可見，以 `record ... succeeded ... --asset "資產名稱" --manual` 登記手動成果；憑證仍在 `prepared` 或 `failed`（沒有 attempt）時，attempt 欄填 `manual`。主人處理完異常活動但沒有手動完成該鏡時，依主人指示執行 `clear RECEIPT "主人如何處理"`，憑證轉為 `failed` 並解除專案鎖定。

`succeeded` 後下載驗收不合格（畫面、語音或 `finish` 的檔案檢查不過）時，執行 `record RECEIPT ATTEMPT failed 截圖` 退回，之後可修正計畫再 arm。

## 4. finish 正式驗收

先以 Flow 對應資產下載，目視對照原分鏡。需原生語音時，用 OpenAI 線上 API 的 `gpt-transcribe` MCP 核對實際台詞：

1. 抽出單聲道 PCM WAV（上限 240 秒、25 MB）：`ffmpeg -v error -y -i 影片.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le 絕對路徑.wav`
2. 呼叫 MCP 工具 `transcribe_audio`，參數 `{"audio_path": "絕對路徑.wav", "language": "en"}`（中文台詞用 `zh`）。每次呼叫會上傳音訊並計費；只轉錄驗收需要的片段，不重複呼叫同一檔案。
3. 把回傳的 `model`、`text`、`request_id` 原樣填進 review 的 `asr`，與原稿台詞逐句對照。

目前模型不接受音訊輸入，不得把 WAV／MP3 或 base64 音訊放進工具輸出或模型輸入，否則該對話之後每輪都會回報 `Audio input is not available.`，只能另開新對話。`gpt-transcribe` 不提供字詞時間戳，也不驗證說話者、聲線、口型或環境音；說話時機與字幕對時另依實際音訊校對，無法確認的項目在 `notes` 寫明並請主人試聽。MCP 未載入或缺少 `OPENAI_API_KEY` 時，不改用其他方式聽音訊，保留 `handoff`。用本機 JSON 記下實際驗收，例如：

```json
{"visual":"PASS","audio":"PASS","notes":"抽格確認角色、動作和連戲。轉錄台詞與原稿一致。",
 "asr":{"model":"gpt-transcribe","text":"Got it!","request_id":"req_..."}}
```

`audio` 為 `PASS` 時必須附 `asr`（`model` 須為 `gpt-transcribe`，`text`、`request_id` 不可空）。同一個媒體檔（SHA-256 相同）不能讓兩個工作都通過驗收。

`python flow_tool.py finish RECEIPT VIDEO REVIEW.json` 核對檔案、解析度、畫幅、影片時長（±0.25 秒）、需要的音訊串流與完整解碼，並回傳檔案 SHA-256。圖片輸入圖片路徑，`audio` 可為 `NOT_RUN`。每鏡取得 `verified` 再寫回既有交接／storyboard 運行狀態。

## Codex 生命週期 Hook

米米專案的 `.codex/hooks.json` 已註冊專案級 `PreToolUse`、`PostToolUse` 和 `Stop`。`arm`、`record`、`finish`、`handoff`、`clear` 從 shell 以個別命令執行，憑證寫成 32 位字面值（不用變數），讓 Hook 可讀取命令和憑證；抓不到字面憑證時前置檢查會拒絕該命令。前置檢查本工具的憑證和狀態，擋住受支援工具中直接引用狀態庫檔名的呼叫，後置回報失敗和停件，結束只核對本輪碰過的憑證。未驗收或未交接會請 Codex 繼續一次。既有專案的其他工作不受檢查。從米米專案目錄開啟 Codex，宿主才會載入這一層專案設定。Windows 設定以本機專案絕對路徑找腳本，若搬動專案，更新該路徑。

Codex 對新增專案 Hook 要求用 `/hooks` 檢視並信任。技能安裝與 Hook 信任分開。腳本本身會執行同一組狀態和媒體檢查。Chrome 按鈕、畫面證據和肉眼／聽覺驗收由使用者或受支援瀏覽器工具操作與記錄，Hook 不接管瀏覽器或作業系統檔案權限。[Codex Hooks](https://developers.openai.com/codex/hooks)
