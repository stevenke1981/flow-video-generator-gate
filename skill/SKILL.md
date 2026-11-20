---
name: flow-video-generator
description: Plan, generate, download, and verify Google Flow or Veo videos and related assets in the supported Chrome UI. Uses a local workflow tool with submission receipts, lifecycle checks, and media validation.
---

# Google Flow 影片生成

用 `scripts/flow_tool.py` 執行有狀態的製作流程。工具產生可核對的工作憑證，鎖定畫幅、片長、解析度、素材順序、帳號、專案和預算。規格、證據欄位和命令見 [本機驗收工作流程](references/workflow.md)。

1. **規劃**。先讀當集交接及核准的逐鏡資料。使用核准角色圖在 Flow 建立、核對及選用「角色」元件。用 `prepare plan.json` 建立一鏡一份憑證。同一專案、鏡號沿用憑證。
2. **核對與登記**。到已連線的原 Chrome 專案讀取實際帳號和 UI 設定。Flow 預設 Veo 3.1 Lite、720p、16:9、8 秒、x1、三項 Ingredients，任務有其他指定時照該次計畫。長提示詞放慢並逐段輸入，上傳後等素材顯示。讀回全文及時間碼。使用 Flow Agent 時，保留生成前確認並核對實際提案。保存截圖和觀察值，以 `arm RECEIPT observed.json` 檢查及登記。收到新 `attempt` 才能按一次對應的提交／核准。
3. **觀察與記錄**。只在受支援的 Chrome UI 操作。用 `record` 和本次 attempt 憑證登記結果。只有已確認的普通失敗可考慮再次登記，同鏡最多三次。異常活動立即停止自動提交。若畫面未定，先從原帳號與專案查證或 `handoff`，保留未決狀態。
4. **下載與驗收**。確認結果卡後立即下載。先對照畫面，需要語音時用 ffmpeg 抽出單聲道 WAV，呼叫 `gpt-transcribe` MCP 的 `transcribe_audio`（OpenAI 線上 API，會計費）核對台詞；不得把音訊檔或 base64 音訊放進模型輸入，否則整個對話會持續回報 `Audio input is not available.`。再用 `finish RECEIPT MEDIA REVIEW.json` 完成媒體驗證。畫面或語音不合格時用 `record RECEIPT ATTEMPT failed 截圖` 退回，計入同鏡次數。回傳 `verified` 才算本機交付。逐鏡寫回既有 storyboard 或交接紀錄。卡住時保存實際狀態，回報未完成的檢查。

## 詳細規格

- [Flow 操作、角色元件和提示詞](references/flow-ui.md)：角色卡、影格與素材、音訊、360p 提升、編輯與協作。先讀本頁的工作工具流程，再按實際任務用相應細節。
- [本機工具與專案 Hook](references/workflow.md)：計畫與證據格式、終局、驗證器、原生 Hook 信任與適用範圍。
- [影片與提示詞範例](references/examples.md)：編寫影片提示詞及加入聲音時參考。
- [本機帳號輪值參考](references/account-rotation.md)：只有點數不足、已有輪值授權且原任務確認沒有未決提交時才讀取。切換後重新確認帳號、剩餘額度、專案與角色元件。所有可用帳號不足時保留待辦，依主人指示處理。
