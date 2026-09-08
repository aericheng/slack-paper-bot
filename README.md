# Slack Paper Summarizer Bot

基於 **GitHub Actions cron + Google Gemini LLM** 的 Slack 論文週報 bot — 定時抓取指定頻道近 N 天的論文連結與 thread 討論，呼叫 LLM 生成結構化 digest，再透過 Slack DM 推送給指定接收者。原本為實驗室論文分享頻道設計，可調整 prompt 與排程套用到其他用途（例如 release notes、討論摘要等）。

## 技術棧

- **Language**：Python 3.11+
- **Slack 整合**：[`slack_bolt`](https://github.com/slackapi/bolt-python) — Socket Mode（本機開發）/ Web API（生產）
- **LLM**：Google Gemini 2.5 Flash via [`google-genai`](https://github.com/googleapis/python-genai) SDK，啟用 URL Context 自動抓取論文頁面
- **URL 預抓**：arxiv 官方 API（abstract）+ [fxtwitter](https://github.com/FxEmbed/FxEmbed) 公開 API（X/Twitter 內文）
- **排程**：GitHub Actions cron（生產） / [`schedule`](https://github.com/dbader/schedule)（本機備援）
- **部署**：無伺服器，全部跑在 GitHub Actions runner（免費額度足夠）

## 功能

- **自動排程**：透過 GitHub Actions cron，每週三、五、日 17:00（台北時間）自動觸發
- **手動觸發**：本機開發時可在 Slack 用 `@bot summarize` 觸發；部署後可從 GitHub Actions UI 點 *Run workflow*
- **時間窗口去重**：每次只抓「上次跑到現在」的訊息，同一篇論文不會被重複總結
- **Thread 回覆抓取**：論文 thread 內同學的補充討論也會納入
- **arxiv abstract 預抓**：偵測到 `arxiv.org/abs/XXX` 自動透過 arxiv API 取 title + abstract，餵給 LLM
- **X/Twitter 預抓**：偵測到 `x.com/.../status/XXX` 透過 fxtwitter 公開 API 取 tweet 內文（X 擋一般爬蟲）
- **Gemini URL Context**：對 arxiv / project page，Gemini 會自動 fetch 內容，輸出能寫到具體技術細節
- **重試保護**：遇到 503（伺服器忙）或 429（rate limit）自動退避重試（10s → 30s → 60s → 120s）

## 架構

```
GitHub Actions (cron)
        │
        ├── 週三/五/日 17:00 台北 觸發
        ├── 用 secrets 注入 token
        │
        ▼
   run_once.py
        │
        ├── 抓 Slack 訊息（含 thread reply、過濾 bot 自己訊息）
        ├── 預抓 arxiv abstract / X tweet 內文
        ├── 丟給 Gemini 2.5 Flash + URL Context
        ├── 取得 Markdown 結構化總結
        │
        ▼
   Slack DM 給指定使用者
```

## 環境變數

| 名稱 | 必要 | 說明 |
|------|------|------|
| `SLACK_BOT_TOKEN` | ✅ | Bot User OAuth Token，`xoxb-` 開頭。從 Slack App OAuth & Permissions 頁面取得 |
| `SLACK_APP_TOKEN` | 僅 Socket Mode | App-Level Token，`xapp-` 開頭。**只有本機 mention 觸發模式需要**，GH Actions 用不到 |
| `GEMINI_API_KEY` | ✅ | Google AI Studio 的 API key，`AIza` 開頭。從 https://aistudio.google.com/apikey 取得 |
| `GEMINI_MODEL` | ❌ | 預設 `gemini-2.5-flash`。可改 `gemini-2.5-pro` / `gemini-2.0-flash` |
| `TARGET_CHANNEL_ID` | ✅ | 要被總結的私密頻道 ID，`C` 開頭。在 Slack 對頻道按右鍵複製連結，網址尾段就是 |
| `DM_USER_ID` | ✅ | 接收總結的使用者 ID，`U` 開頭。Slack 個人檔案的「複製成員 ID」 |
| `HISTORY_LIMIT` | ❌ | 抓取訊息上限，預設 150 |
| `HISTORY_LOOKBACK_HOURS` | ❌ | 時間窗口（小時）；`0` = 不限。GH Actions workflow 會根據觸發日動態設定（72 / 48） |
| `TEST_RUN_ON_START` | ❌ | `true` 時，本機啟動後立即跑一次（開發測試用），預設 `false` |

## 部署

兩種模式擇一。**GH Actions 推薦給生產用**（免費、不需要常駐機器）；**本機 Socket Mode** 適合開發迭代 prompt。

### 模式 A：GitHub Actions（生產用）

#### 1. 推到 GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/aericheng/slack-paper-bot.git  # fork 者請改成你自己帳號下的 repo URL
git push -u origin main
```

⚠️ `.gitignore` 已設定排除 `.env`，請勿手動把 `.env` 加進 commit。

#### 2. 設定 GitHub Secrets

在 repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，新增以下 4 個 **必填** secret（值的格式與取得方式見上方[環境變數](#環境變數)表）：

| Secret 名稱 | 範例格式 | 取得來源 |
|------------|---------|---------|
| `SLACK_BOT_TOKEN` | `xoxb-...` | Slack App → OAuth & Permissions |
| `GEMINI_API_KEY` | `AIza...` | https://aistudio.google.com/apikey |
| `TARGET_CHANNEL_ID` | `C0XXXXXXX` | Slack 頻道右鍵 → 複製連結，網址尾段 |
| `DM_USER_ID` | `U0XXXXXXX` | Slack 個人檔案 → 複製成員 ID |

> `SLACK_APP_TOKEN`（`xapp-` 開頭）只有本機 Socket Mode 需要，**GitHub Actions 不需要設定**。

#### 3. 確認 workflow

`.github/workflows/digest.yml` 已配置：

- cron: `0 9 * * 3,5,0`（UTC 09:00 = 台北 17:00；週三/五/日）
- 根據觸發當天自動設定 `HISTORY_LOOKBACK_HOURS`：週三 72h、週五 48h、週日 48h
- 支援 `workflow_dispatch` 手動觸發

#### 4. 手動觸發測試

repo → **Actions** → **Paper Digest** → **Run workflow** → **Run workflow**

成功後 DM 應該收到一份總結。

### 模式 B：本機 Socket Mode（開發 / 迭代 prompt）

#### 1. 環境

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows PowerShell
# source .venv/bin/activate    # Linux / macOS
pip install -r requirements.txt
```

> Windows 若 Activate.ps1 被擋：`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

#### 2. 設定 `.env`

```bash
cp .env.example .env
# 編輯 .env 填入真實 token / ID
```

`SLACK_APP_TOKEN` 僅本機 Socket Mode 需要。設定 `TEST_RUN_ON_START=true` 可在啟動時立即跑一次。

#### 3. 啟動

```bash
python main.py
```

看到 `🤖 Slack Bot 啟動，連線 Socket Mode…` + `Bolt app is running!` 即正常。

#### 4. 觸發

在已邀請 bot 的頻道 `@Paper Summarizer Bot summarize` 或 `@Paper Summarizer Bot 總結`。

## Slack App 初始設定

第一次部署前需要先建好 Slack App：

1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. **Socket Mode** 開啟，建 App-Level Token（scope: `connections:write`）→ 即 `SLACK_APP_TOKEN`
3. **OAuth & Permissions** → Bot Token Scopes 加入：
   - `channels:history`
   - `groups:history`（私密頻道用）
   - `chat:write`
   - `im:write`
   - `app_mentions:read`
4. **Event Subscriptions** → On → 訂閱 `app_mention`
5. **Install App** → 取得 `SLACK_BOT_TOKEN`（`xoxb-` 開頭）
6. 在目標頻道輸入 `/invite @你的Bot名字` 邀請 bot

## Cron 排程說明

`.github/workflows/digest.yml` 設定：

```yaml
- cron: '0 9 * * 3,5,0'
```

| Cron 欄位 | 值 | 意義 |
|----------|----|----|
| 分 | `0` | 整點 |
| 時 | `9` | UTC 09:00 = 台北 17:00 |
| 日 | `*` | 每天 |
| 月 | `*` | 每月 |
| 週幾 | `3,5,0` | 週三、週五、週日 |

> ⚠️ GitHub Actions cron **不保證精準**，尖峰時段可能延遲 5~15 分鐘觸發。

## 修改觸發頻率

改 `.github/workflows/digest.yml` 的 `cron` 行即可：

| 想要 | cron 表達式 |
|------|-----------|
| 每週五 17:00 台北 | `0 9 * * 5` |
| 每週一三五 17:00 台北 | `0 9 * * 1,3,5` |
| 每天 17:00 台北 | `0 9 * * *` |
| 每天早晚兩次（9 點、17 點台北） | `0 1,9 * * *` |

同時調整 `HISTORY_LOOKBACK_HOURS` 的 case 邏輯，讓時間窗口剛好接續。

## 故障排除

### `not_authed` / `invalid_auth`
`SLACK_BOT_TOKEN` 沒設或值錯。確認貼的是 `xoxb-` 開頭（不是 `xapp-`）。

### `not_in_channel`
Bot 沒被邀請進目標頻道。在頻道輸入 `/invite @bot名字`。

### `missing_scope`
Slack App 漏 OAuth scope。補完後到 **Install App** 頁面 **Reinstall to Workspace**。

### `429 RESOURCE_EXHAUSTED` / `limit: 0`
Gemini 該模型對你的 project 沒免費額度。改 `GEMINI_MODEL` 為 `gemini-2.5-flash` 或 `gemini-2.0-flash`。

### `503 UNAVAILABLE`
Gemini 伺服器暫時忙碌。程式會自動重試 4 次（最多 ~3.7 分鐘）。

### Slack DM 收到的內容是 markdown 字面（看到 `**` 跟 `###`）
Gemini 沒按 prompt 出 Slack mrkdwn。檢查 `summarize_with_llm` 內 prompt 是否被改壞。

### GH Actions 一直失敗，但本機 OK
99% 是 secrets 沒設好。Settings → Secrets and variables → Actions 全部重貼一次。

## 開發迭代

調整 prompt（`main.py` 的 `summarize_with_llm`）：

1. 本機 `python main.py`（`TEST_RUN_ON_START=true` 或 mention 觸發）
2. 看 DM，不滿意就改 prompt → Ctrl+C → 重啟 → 再 mention
3. 滿意後 commit & push，下次 GH Actions cron 自動用新版

## 檔案結構

```
slack_bot/
├── main.py                       核心邏輯（Slack 抓取、LLM 呼叫、Socket Mode）
├── run_once.py                   GH Actions 用的入口（跑一次後 exit）
├── requirements.txt
├── .env.example                  環境變數樣板
├── .gitignore                    排除 .env / .venv
├── Dockerfile                    （備用，自架伺服器才用得到）
├── .github/workflows/
│   └── digest.yml                GH Actions cron + 動態 lookback
└── README.md
```
