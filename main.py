import os
import re
import json
import time
import threading
import logging
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
import schedule
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("paper-bot")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")  # 只有 Socket Mode 模式需要
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TARGET_CHANNEL_ID = os.environ["TARGET_CHANNEL_ID"]
DM_USER_ID = os.environ["DM_USER_ID"]
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "150"))
TEST_RUN_ON_START = os.environ.get("TEST_RUN_ON_START", "false").lower() == "true"

app = App(token=SLACK_BOT_TOKEN)
gemini = genai.Client(api_key=GEMINI_API_KEY)

BOT_USER_ID = app.client.auth_test()["user_id"]

X_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:twitter|x)\.com/[^/\s<>]+/status(?:es)?/(\d+)"
)
ARXIV_URL_PATTERN = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?"
)


def _fetch_tweet_text(tweet_id: str, timeout: float = 5.0) -> str | None:
    try:
        req = Request(
            f"https://api.fxtwitter.com/status/{tweet_id}",
            headers={"User-Agent": "PaperSummarizerBot/1.0"},
        )
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        tweet = data.get("tweet", {})
        author = tweet.get("author", {}).get("screen_name", "?")
        text = (tweet.get("text") or "").strip()
        return f"@{author}: {text}" if text else None
    except Exception as e:
        log.warning("fxtwitter 取 tweet 失敗 id=%s: %s", tweet_id, e)
        return None


def _fetch_arxiv_meta(arxiv_id: str, timeout: float = 8.0) -> str | None:
    """用 arxiv 官方 API 抓 title + abstract，讓 Gemini 有具體技術內容可寫。"""
    try:
        url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
        req = Request(url, headers={"User-Agent": "PaperSummarizerBot/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            xml_text = resp.read().decode("utf-8")
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        entry = root.find("atom:entry", ns)
        if entry is None:
            return None
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
        if not summary:
            return None
        # 換行壓平避免破壞 prompt 結構
        summary = " ".join(summary.split())
        return f"標題: {title} | Abstract: {summary}"
    except Exception as e:
        log.warning("arxiv API 取 abstract 失敗 id=%s: %s", arxiv_id, e)
        return None


def _enrich_links(text: str) -> str:
    """X 跟 arxiv Gemini 不一定讀得到（X 擋爬蟲、arxiv PDF 大），預先抓好塞進 prompt。"""
    enrichments = []
    seen_x = set()
    seen_arxiv = set()
    for m in X_URL_PATTERN.finditer(text):
        if (tid := m.group(1)) not in seen_x:
            seen_x.add(tid)
            if (t := _fetch_tweet_text(tid)):
                enrichments.append(f"[Tweet] {t}")
    for m in ARXIV_URL_PATTERN.finditer(text):
        if (aid := m.group(1)) not in seen_arxiv:
            seen_arxiv.add(aid)
            if (a := _fetch_arxiv_meta(aid)):
                enrichments.append(f"[arxiv {aid}] {a}")
    return f"{text}\n      [預抓] {' ‖ '.join(enrichments)}" if enrichments else text


def _is_human_message(msg: dict) -> bool:
    if msg.get("subtype") is not None:
        return False
    if msg.get("bot_id") or msg.get("user") == BOT_USER_ID:
        return False
    return bool(msg.get("text", "").strip())


def fetch_channel_text(channel_id: str, limit: int) -> str:
    """抓主訊息 + 各 thread 的 replies，組成單一文字。bot 自己貼的訊息會被過濾。"""
    result = app.client.conversations_history(channel=channel_id, limit=limit)
    lines: list[str] = []
    for msg in result.get("messages", []):
        if not _is_human_message(msg):
            continue
        lines.append(f"- {_enrich_links(msg['text'].strip())}")

        thread_ts = msg.get("thread_ts")
        if thread_ts and msg.get("reply_count", 0) > 0:
            try:
                replies = app.client.conversations_replies(channel=channel_id, ts=thread_ts)
                for reply in replies.get("messages", []):
                    if reply.get("ts") == thread_ts:
                        continue
                    if not _is_human_message(reply):
                        continue
                    lines.append(f"  ↳ {_enrich_links(reply['text'].strip())}")
            except Exception:
                log.exception("抓取 thread replies 失敗 ts=%s", thread_ts)
    return "\n".join(lines)


def summarize_with_llm(chat_text: str) -> str:
    prompt = f"""你是計算機視覺與機器學習領域的研究員，要為實驗室同學產生一份在 *Slack* 上閱讀的論文 digest。

==== 輸入格式 ====
- 主訊息以 `- ` 開頭，內含論文連結（arxiv / project page / X tweet），可能附帶中文評論
- `↳ ` 開頭是該訊息 thread 內的回覆，含同學的補充觀點
- `[預抓] [Tweet] @user: ...` 或 `[預抓] [arxiv ID] 標題: ... | Abstract: ...` 是已預先抓好的內容，請當作該連結的真實內容看待，不要忽略

請額外用 url_context 工具實際讀取 project page（arxiv abstract / X 已預抓不用再 fetch；project page 通常有更具體的方法描述跟比較表）。

==== 每篇論文輸出格式（嚴格遵守，這是 Slack 訊息不是 GitHub markdown）====

📄 *論文標題*
🔗 https://...
💡 *TL;DR*：一句話精準摘要，最多 35 字

• *做什麼*：解決的問題、應用情境（不要重複 TL;DR）
• *怎麼做*：具體的方法 — 用什麼模型架構、什麼 loss / training trick、什麼 input/output、key insight 是什麼。**這欄要有真正的技術內容**，不是「使用先進的方法處理 X」這種空話。如果 abstract / project page 提到 specific 元件名稱（例如 "selective state space model"、"diffusion transformer"、"two-stage training"），請保留
• *進步在哪*：跟既有方法（baseline 名稱要寫出來）相比，具體贏在哪 — 速度 / 品質 / 規模 / 場景適用性等。例：「比 LVSM 能外插到更遠視角，因為 diffusion-based 而非 regression」、「training 比 official 4x 快但 PSNR 持平」。沒有具體 baseline 對比就省略整欄，不要寫「暗示了顯著進步」這種廢話
• *實驗室觀點*：原訊息或 thread 同學評論的大意（可引述具體句子）。沒有就省略整欄，不要硬編

——

==== 內容深度規則（這次重點）====
- 預抓內容若有 abstract（`[arxiv ...]` 或 project page 詳細內容），*怎麼做* 跟 *進步在哪* 應該寫到具體技術點 — 拒絕「該方法採用先進的技術從輸入生成輸出」這種空殼句
- 如果只有 tweet 一句宣傳文字、沒有 abstract、沒有 thread 討論，材料就是不夠寫深，這時候欄位寫短一點甚至省略，**不要 padding 補字**
- 三個欄位嚴禁講同一件事用不同字。如果發現自己在改寫同一句話，刪掉重複的欄

==== 格式硬規則 ====
1. Slack mrkdwn：`*單星號 bold*`，不用 `**雙星號**`，不用 `#`/`###` 標題
2. 論文間用 `——` 分隔，分隔線前後各留一空行
3. `📄 *標題*` 當 header
4. bullet 用 `• ` 開頭（中黑點 + 半形空格）

==== 處理規則 ====
- 跳過：純閒聊、無連結也無預抓內容的純文字、Slack URL preview 殘留（例如「X (formerly Twitter)」、整段重複 unfurl）
- 同一論文（同 URL / 同 arxiv ID）只寫一次，合併所有訊息與 thread 評論
- 排序：討論度 / 重要性高的放前面

==== 輸入訊息 ====
{chat_text}
"""
    delays = [10, 30, 60, 120]
    config = genai_types.GenerateContentConfig(
        tools=[genai_types.Tool(url_context=genai_types.UrlContext())],
    )
    for attempt in range(len(delays) + 1):
        try:
            response = gemini.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
            return response.text
        except (genai_errors.ServerError, genai_errors.ClientError) as e:
            is_retryable = isinstance(e, genai_errors.ServerError) or getattr(e, "code", None) == 429
            if not is_retryable or attempt == len(delays):
                raise
            wait = delays[attempt]
            log.warning("Gemini 暫時失敗 (code=%s)，%d 秒後重試 %d/%d",
                        getattr(e, "code", "?"), wait, attempt + 1, len(delays))
            time.sleep(wait)


def run_summary(notify_channel: str | None = None) -> None:
    """跑一次完整流程：抓訊息 → 總結 → DM。notify_channel 用於 mention 觸發時回覆狀態。"""
    log.info("開始執行論文總結任務 channel=%s", TARGET_CHANNEL_ID)
    try:
        chat_text = fetch_channel_text(TARGET_CHANNEL_ID, HISTORY_LIMIT)
        if not chat_text:
            log.info("頻道無可總結訊息")
            if notify_channel:
                app.client.chat_postMessage(channel=notify_channel, text="頻道近期沒有可總結的內容。")
            return

        summary = summarize_with_llm(chat_text)

        app.client.chat_postMessage(
            channel=DM_USER_ID,
            text=f"📊 *本週實驗室論文與討論總結：*\n\n{summary}",
        )
        log.info("總結已 DM 發送給 %s", DM_USER_ID)

        if notify_channel:
            app.client.chat_postMessage(channel=notify_channel, text="✅ 總結完成，已發送私訊。")
    except Exception as e:
        log.exception("總結任務失敗")
        if notify_channel:
            app.client.chat_postMessage(channel=notify_channel, text=f"❌ 任務失敗：{e}")


@app.event("app_mention")
def handle_mention(event, say):
    """在頻道 @bot summarize 即可手動觸發，方便測試。"""
    text = event.get("text", "").lower()
    if "summarize" in text or "總結" in text:
        say("收到，開始抓訊息並總結中…")
        threading.Thread(target=run_summary, args=(event["channel"],), daemon=True).start()
    else:
        say("用法：`@bot summarize` 或 `@bot 總結` 來手動觸發一次論文總結。")


def run_scheduler() -> None:
    schedule.every().friday.at("17:00").do(run_summary)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    if TEST_RUN_ON_START:
        log.info("TEST_RUN_ON_START=true → 啟動時先跑一次總結")
        threading.Thread(target=run_summary, daemon=True).start()

    threading.Thread(target=run_scheduler, daemon=True).start()

    if not SLACK_APP_TOKEN:
        raise SystemExit("缺少 SLACK_APP_TOKEN — Socket Mode 模式需要。如果想跑一次就退出，請改用 run_once.py")
    log.info("🤖 Slack Bot 啟動，連線 Socket Mode…")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
