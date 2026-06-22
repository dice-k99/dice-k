#!/usr/bin/env python3
"""
Slack Task Scheduling Agent
Slack→Notionタスク自動転記・日程調整漏れ検出
"""

import os
import json
import time
from datetime import datetime, timedelta
from typing import Optional
import anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from notion_client import Client as NotionClient
import pytz

JST = pytz.timezone("Asia/Tokyo")

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TASK_DB_ID = os.environ.get("NOTION_TASK_DB_ID", "3604e7f2-0664-80df-a1c1-cf180b420ee8")
SLACK_NOTIFY_CHANNEL = os.environ.get("SLACK_NOTIFY_CHANNEL", "D0ATGMNKJK1")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "7"))
BUSINESS_DAYS_THRESHOLD = int(os.environ.get("BUSINESS_DAYS_THRESHOLD", "3"))


def load_channels() -> list[dict]:
    config_path = os.path.join(os.path.dirname(__file__), "channel_config.json")
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)["channels"]


def count_business_days_since(ts: float) -> int:
    """メッセージ送信日から今日までの営業日数"""
    msg_date = datetime.fromtimestamp(ts, JST).date()
    today = datetime.now(JST).date()
    count = 0
    cur = msg_date + timedelta(days=1)
    while cur <= today:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def fetch_messages(slack: WebClient, channel_id: str, oldest_ts: float) -> list[dict]:
    messages, cursor = [], None
    while True:
        try:
            kwargs = {"channel": channel_id, "oldest": str(oldest_ts), "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = slack.conversations_history(**kwargs)
            messages.extend(resp["messages"])
            if not resp.get("has_more"):
                break
            cursor = resp["response_metadata"]["next_cursor"]
            time.sleep(0.5)
        except SlackApiError as e:
            print(f"[WARN] {channel_id} 取得エラー: {e.response['error']}")
            break
    return messages


def get_reply_count(slack: WebClient, channel_id: str, ts: str) -> int:
    try:
        resp = slack.conversations_replies(channel=channel_id, ts=ts, limit=2)
        return len(resp["messages"]) - 1
    except SlackApiError:
        return 0


def extract_tasks(claude: anthropic.Anthropic, messages: list[dict], channel_name: str) -> list[dict]:
    if not messages:
        return []

    lines = []
    for m in messages[:80]:
        ts = m.get("ts", "")
        try:
            dt = datetime.fromtimestamp(float(ts), JST).strftime("%m/%d %H:%M")
        except Exception:
            dt = ""
        text = m.get("text", "").replace("\n", " ")[:300]
        lines.append(f"[{dt}|ts:{ts}] {text}")

    prompt = f"""チャンネル「{channel_name}」のSlackメッセージから対応が必要なタスクを抽出してJSON配列で返してください。

ルール:
- 依頼・確認・対応を求めるメッセージのみ抽出（完了報告・情報共有は除外）
- 個人への主観的評価は含めない（事実のみ）
- @吉形大介 へのメンションは優先度「高」

出力形式（JSON配列のみ、コードブロック不要）:
[{{"task_name":"内容（30文字以内）","priority":"高|中|低","requester":"依頼者名","date":"YYYY-MM-DD","is_scheduling":false,"message_ts":"timestamp","mentioned_user":"@名前またはnull"}}]

タスクなしの場合は []

メッセージ:
{chr(10).join(lines)}"""

    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if "```" in text:
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else parts[0]
            if text.startswith("json"):
                text = text[4:].strip()
        return json.loads(text)
    except Exception as e:
        print(f"[WARN] タスク抽出失敗 ({channel_name}): {e}")
        return []


def is_duplicate_in_notion(notion: NotionClient, db_id: str, task_name: str) -> bool:
    try:
        res = notion.databases.query(
            database_id=db_id,
            filter={"property": "タスク名", "title": {"contains": task_name[:20]}},
        )
        return len(res["results"]) > 0
    except Exception as e:
        print(f"[WARN] Notion重複確認エラー: {e}")
        return False


def create_notion_page(notion: NotionClient, db_id: str, task: dict, channel_name: str) -> Optional[str]:
    summary = f"#{channel_name} / {task.get('requester','不明')} / {task.get('date','不明')}"
    if task.get("mentioned_user"):
        summary = f"{task['mentioned_user']} 宛メンション / {summary}"

    hiring_keywords = ["採用", "新卒", "オファー", "bizreach", "forstartups", "syngress", "エージェント"]
    kind_name = "採用" if any(k in channel_name for k in hiring_keywords) else "作業"

    properties: dict = {
        "タスク名": {"title": [{"text": {"content": task["task_name"]}}]},
        "Summary": {"rich_text": [{"text": {"content": summary[:2000]}}]},
        "種類": {"multi_select": [{"name": kind_name}]},
    }
    if task.get("priority") == "高":
        properties["プライオリティ"] = {"select": {"name": "⚠️"}}

    try:
        resp = notion.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
        page_id = resp["id"].replace("-", "")
        return f"https://app.notion.com/p/{page_id}"
    except Exception as e:
        print(f"[ERROR] Notionページ作成エラー: {e}")
        return None


def post_slack_report(
    slack: WebClient,
    channel: str,
    new_tasks: list[dict],
    gaps: list[dict],
    run_time: str,
):
    p_icon = {"🔥 高": "🔥", "🔶 中": "🔶", "🔷 低": "🔷"}
    lines = [f"✅ *[{run_time}] Slackタスクエージェント完了*\n"]

    if new_tasks:
        lines.append(f"*📝 新規Notionタスク {len(new_tasks)}件*")
        for t in new_tasks[:15]:
            icon = p_icon.get(t.get("priority_label", ""), "•")
            lines.append(f"{icon} {t['task_name']}")
            if t.get("url"):
                lines.append(f"   ↳ {t['url']}")
    else:
        lines.append("新規タスク: 0件")

    if gaps:
        lines.append(f"\n*⚠️ 日程調整漏れ {len(gaps)}件*")
        for g in gaps[:5]:
            lines.append(f"• {g['summary']} （{g['days']}営業日経過、返信なし）")
    else:
        lines.append("日程調整漏れ: 0件")

    slack.chat_postMessage(channel=channel, text="\n".join(lines))


def main():
    now_jst = datetime.now(JST)
    run_time = now_jst.strftime("%Y-%m-%d %H:%M JST")
    print(f"[INFO] 実行開始: {run_time}")

    slack = WebClient(token=SLACK_BOT_TOKEN)
    notion = NotionClient(auth=NOTION_API_KEY)
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    channels = load_channels()
    oldest_ts = (now_jst - timedelta(days=DAYS_BACK)).timestamp()

    new_tasks: list[dict] = []
    gaps: list[dict] = []

    for ch in channels:
        ch_id, ch_name = ch["id"], ch["name"]
        print(f"[INFO] 処理中: {ch_name}")
        try:
            messages = fetch_messages(slack, ch_id, oldest_ts)
            if not messages:
                continue

            tasks = extract_tasks(claude, messages, ch_name)

            for task in tasks:
                name = task.get("task_name", "").strip()
                if not name:
                    continue

                # 日程調整漏れチェック
                if task.get("is_scheduling") and task.get("message_ts"):
                    try:
                        days = count_business_days_since(float(task["message_ts"]))
                        if days >= BUSINESS_DAYS_THRESHOLD:
                            replies = get_reply_count(slack, ch_id, task["message_ts"])
                            if replies == 0:
                                gaps.append({"summary": f"{ch_name}: {name}", "days": days})
                    except (ValueError, TypeError):
                        pass

                # 重複チェック → 登録
                if not is_duplicate_in_notion(notion, NOTION_TASK_DB_ID, name):
                    url = create_notion_page(notion, NOTION_TASK_DB_ID, task, ch_name)
                    priority_label = {"高": "🔥 高", "中": "🔶 中", "低": "🔷 低"}.get(
                        task.get("priority", "中"), "🔶 中"
                    )
                    new_tasks.append({"task_name": name, "url": url, "priority_label": priority_label})
                    print(f"[INFO] 登録: {name}")
                else:
                    print(f"[INFO] 重複スキップ: {name}")

            time.sleep(1)

        except Exception as e:
            print(f"[ERROR] {ch_name}: {e}")

    post_slack_report(slack, SLACK_NOTIFY_CHANNEL, new_tasks, gaps, run_time)
    print(f"[INFO] 完了 — 新規タスク: {len(new_tasks)}件 / 調整漏れ: {len(gaps)}件")


if __name__ == "__main__":
    main()
