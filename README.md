# flow-video-generator-gate

Codex skill for producing Google Flow (Veo) video shots with a local **gate**: a small state machine (`scripts/flow_tool.py`) that locks each shot's spec, checks the visible Flow UI before every submission, limits retries, and verifies the downloaded media before a shot counts as delivered. Optional Codex lifecycle hooks stop a turn from ending with unverified work.

用於 Google Flow／Veo 逐鏡生成影片的 Codex 技能，附本機「Gate」工作流程：鎖定每鏡規格、送出前核對畫面設定、限制重試、下載後驗證媒體，並可用 Codex Hook 防止未驗收就結束。

## 內容

```
skill/                     ← 複製到 ~/.codex/skills/flow-video-generator
  SKILL.md
  agents/openai.yaml
  references/workflow.md         工具命令、證據格式、Hook 說明
  references/flow-ui.md          Flow 操作、角色元件、提示詞防錯
  references/account-rotation.md 多帳號輪值規則（帳號已換成佔位符）
  references/examples.md
  scripts/flow_tool.py           Gate 狀態機（Python 3.11+，僅標準函式庫）
examples/hooks.json        ← 專案級 Codex Hook 範例（改成實際路徑）
```

## 狀態機

```
prepare → prepared ──arm──▶ armed ──record──▶ pending / succeeded / failed / unusual_activity / policy_refusal
                      ▲                          │
                      └──── failed ◀── record failed（下載驗收不合格）
succeeded ──finish──▶ verified
unusual_activity ──clear──▶ failed
```

- `prepare plan.json`：一鏡一張憑證；`prepared`／`failed` 時可用修正後的計畫更新。
- `arm RECEIPT observed.json`：逐欄比對畫面讀回的設定、點數上限與餘額，通過才發 attempt。同一 job 跨專案合計最多 3 次。
- `record RECEIPT ATTEMPT STATE evidence.png [--asset NAME] [--manual]`
- `finish RECEIPT media review.json`：ffprobe／FFmpeg 核對解析度、畫幅（含旋轉）、片長、音訊與完整解碼；`audio: PASS` 需附 `gpt-transcribe` MCP 的轉錄結果；同一檔案不能驗收兩個工作。
- `handoff RECEIPT reason`、`clear RECEIPT reason`、`status RECEIPT`

狀態存在 `%LOCALAPPDATA%/flow-video-generator/flow-video-state.sqlite3`。

## 需求

- Python 3.11 以上、ffprobe 與 FFmpeg（`FFPROBE`／`FFMPEG` 環境變數或 PATH）。
- 語音驗收：OpenAI 線上 API 的 `gpt-transcribe` MCP（`transcribe_audio`，會計費）。不得把音訊直接放進模型輸入，否則對話會持續回報 `Audio input is not available.`。
- Hook：把 `examples/hooks.json` 放到專案 `.codex/hooks.json`，改成實際腳本路徑，再於 Codex 用 `/hooks` 信任。

## 注意

- 文件中的專案名稱、角色與集數是作者自己的動畫專案範例，請依自己的專案調整。
- `--manual` 只是旗標，無法真正驗證是人手操作。
- 只在 Flow 支援的瀏覽器介面操作；遇到「偵測到異常活動」就停，不以其他自動化方式繞過。
