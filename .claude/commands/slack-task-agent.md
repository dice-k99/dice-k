# Slack + Gmail タスク整理・日程調整支援エージェント

Slack & Gmail のメッセージを確認し、対応が必要なタスクを抽出・日程調整漏れを検出してSlackに下書きレポートを作成する。

## 禁止事項（厳守）
- Slackメッセージの自動送信（必ず下書き保存のみ）
- 個人の評価・印象に関する主観的コメントをレポートに記載すること（事実のみ）

## 設定値
- 対象期間: 直近 **7日間**
- 日程調整漏れ閾値: **3営業日**以上返信なし
- Slack通知先: `D0ATGMNKJK1`（吉形大介 ↔ Claude DM）
- Gmail対象アドレス: `d.yoshikata@can-ly.com`

## 対象チャンネル（16件）
| channel_id | name |
|---|---|
| C0B6PUACNE6 | 28卒_新卒採用 |
| CQTR94NJ2 | 806_corp_採用 |
| C09L55568RF | 826_corp_人事戦略部メンバー |
| C0ATXD0MNAJ | 吉形大介さん-コーポ確認チャンネル |
| C07QFSMERS4 | 採用チーム_業務チャンネル |
| C033GL9JQQ2 | 新入社員専用 |
| C046Y2BQWCF | canly_bizreach |
| C03S0A7KLRK | canly_forstartups |
| C06LH5EE86R | canly_syngress |
| C07P422UAUS | カンリー_エージェントセブン |
| C07N91VVDEV | 808a_corp_オファー業務委託 |
| C02LUTLSQMP | 808b_corp_オファー3等級以下 |
| C06880ACLVC | 808c_corp_オファー4等級 |
| C06880KTB70 | 808d_corp_オファー5等級 |
| C0B3KLYJBFD | 808e_corp_オファー6等級以上 |
| C08PKTFFW8L | 808f_corp_オファーアルバイト |

## 実行手順

### ステップ1：Slack & Gmail メッセージ取得（並列実行）

**1-A: Slackメッセージ取得**
- `slack_read_channel` を使い、全16チャンネルを **並列** で取得する
- `oldest` パラメーター: 今日の日付から7日前のUNIXタイムスタンプ（JST基準）
- `limit: 50`、`response_format: concise`

**1-B: Gmailメッセージ取得（1-Aと並列実行）**
- `search_threads` を使い、以下の2クエリを **並列** で取得する
  1. `newer_than:7d is:unread` — 未読メール
  2. `newer_than:7d label:inbox -from:no-reply -from:noreply -from:notifications` — 要対応の受信メール
- 重複するスレッドIDは1件にまとめる

### ステップ2：Slackタスク抽出（各チャンネルごと）
以下のルールで対応が必要なタスクを選別する：
- **含める**: 依頼・確認・対応を求めるメッセージ
- **除外**: 完了報告・情報共有・雑談
- **@吉形大介（U0ATY5MS64A）へのメンション** → 優先度「高」
- 個人への評価・印象は一切含めない（事実ベースのみ）

抽出した各タスクに以下を付与：
```
source      : "slack"
task_name   : 内容（30文字以内）
priority    : 高|中|低
requester   : 依頼者名
date        : YYYY-MM-DD
is_scheduling: true/false（日程調整依頼かどうか）
channel_id  : SlackチャンネルID
channel_name: チャンネル名
message_ts  : Slackメッセージのタイムスタンプ
```

### ステップ3：Gmailタスク抽出
各Gmailスレッドに対して `get_thread`（format: MINIMAL）で件名・送信者・日時を取得し、以下のルールで選別する：
- **含める**: 日程調整依頼・作業依頼・確認・承認依頼
- **除外**: 自動通知（no-reply/noreply）・ニュースレター・システムメール・完了報告
- 送信者が `d.yoshikata@can-ly.com` 本人 → 対応不要のため除外
- **件名に「ご確認」「お願い」「日程」「調整」「面接」「オファー」を含む** → 優先度「高」

抽出した各タスクに以下を付与：
```
source      : "gmail"
task_name   : 件名ベースの内容（30文字以内）
priority    : 高|中|低
sender      : 送信者名またはメールアドレス
date        : YYYY-MM-DD
is_scheduling: true/false（日程調整依頼かどうか）
thread_id   : GmailスレッドID
```

### ステップ4：日程調整漏れ検出

**4-A: Slack 日程調整漏れ**
`is_scheduling: true` の Slackタスクに対して：
1. メッセージ送信日から今日までの **営業日数**（月〜金）を計算
2. 営業日数 ≥ 3 の場合、`slack_read_thread` でスレッド返信数を確認
3. 返信が0件 → 「日程調整漏れ」として記録

**4-B: Gmail 日程調整漏れ**
`is_scheduling: true` の Gmailタスクに対して：
1. メール受信日から今日までの **営業日数**（月〜金）を計算
2. 営業日数 ≥ 3 の場合、`get_thread`（format: FULL_CONTENT）で `d.yoshikata@can-ly.com` からの返信メッセージを確認
3. 自分の返信が存在しない → 「日程調整漏れ」として記録

### ステップ5：Slack下書きレポート作成
`slack_send_message_draft` を使い `D0ATGMNKJK1` に下書きを作成する。

レポート形式：
```
✅ *[YYYY-MM-DD HH:MM JST] タスクエージェント完了*

*📝 抽出タスク N件（Slack: N件 / Gmail: N件）*
🔥 タスク名 [Slack/#チャンネル名 / 依頼者:xxx / YYYY-MM-DD]
🔶 タスク名 [Gmail / 送信者:xxx / YYYY-MM-DD]
🔷 タスク名 [Slack/#チャンネル名 / 依頼者:xxx / YYYY-MM-DD]
   ...

抽出タスク: N件

*⚠️ 日程調整漏れ N件（Slack: N件 / Gmail: N件）*
• [Slack] チャンネル名: タスク名（X営業日経過、返信なし）
• [Gmail] 送信者名: タスク名（X営業日経過、未返信）

日程調整漏れ: N件
```
- 抽出タスク0件の場合: 「抽出タスク: 0件」のみ
- 日程調整漏れ0件の場合: 「日程調整漏れ: 0件」のみ
- 下書き作成後、チャンネルリンクをチャットに表示する

## 実行後の報告
チャットに以下を簡潔に出力：
- 処理チャンネル数（Slack: 16件 / Gmail: 取得スレッド数）
- 抽出タスク数（Slack由来・Gmail由来の内訳）
- 日程調整漏れ件数（Slack・Gmail の内訳）
- Slack下書きリンク
