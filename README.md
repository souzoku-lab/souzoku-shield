# 相続税 書面添付エージェント M1

Findy DevOps x AI Agent Hackathon 向けの公開用M1デモです。小規模宅地等の特例について、取得者区分ごとに要件確認と資料収集を分岐し、書面添付の下書きを「確認中」として育てます。

## M1で動くこと

- 相続人ごとに同居有無を登録し、自宅取得者をクリックして選ぶと、配偶者・同居親族・家なき子の分岐が決まる
- 相続人は被相続人から見た関係性（配偶者・長男・長女・次男・次女・三男・三女）と「同居・非同居」をプルダウンで選び、一人ずつカード追加できる
- 相談文より先に相続人カードを表示し、カード未登録時は相談文から相続人候補と自宅取得者を起票する
- 同居親族がいる状態で別居親族が自宅を取得する場合は、小規模宅地等の特例の適用不可リスクを赤アラートで出す
- Reviewでは、適用不可アラート時に否認される評価額（公開デモ値: 5,600万円）を大きく表示する
- 相談文を自然語で入力すると、回答文ではなくIntake→Router→Evidence→Draft→ReviewのACTIONタイムラインが起動する
- 配偶者取得では、Reviewで「二次相続の検討はされましたか？」という専門家確認の問いを出す
- 収集カンバンの資料ステータスを進めると、提示書類欄と土地欄の候補文、達成度メーターが更新される
- 総合所見はAIが生成せず、税理士が画面上で手入力した内容だけWordに反映する
- ACTIONタイムラインがReviewに到達し、HITL承認を受けた後だけ、書面添付「33の2①（資）」の表組みに寄せたWord（`.docx`）を出力できる
- 否認インパクトハーネスが、分岐ミス・断定表現・総合所見の自動入力をpytestで落とす
- 金額表示は本税ではなく、否認で失われる評価減（公開デモ値: 5,600万円）に限定する
- Gemini APIキーなしで最後まで動く

## 起動

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8088
```

ブラウザで開く:

```text
http://127.0.0.1:8088
```

## 検証

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\verify_no_secrets.py
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | ヘルスチェック |
| POST | `/api/demo/seed` | デモ状態に戻す |
| POST | `/api/demo/clear-heirs` | 相続人カード未登録のデモ状態に戻す |
| GET | `/api/case` | 案件、ドラフト、反実仮想、ハーネスを取得 |
| PATCH | `/api/case` | 自宅取得者、取得者区分、分割協議進捗を変更 |
| POST | `/api/run` | 相談文からACTIONタイムラインを起動 |
| POST | `/api/heirs` | 関係性と同居有無から相続人カードを追加 |
| PATCH | `/api/heirs/{heir_id}` | 相続人名、続柄、同居有無を更新 |
| PATCH | `/api/manual/overall-opinion` | 税理士の手入力による総合所見を保存 |
| POST | `/api/approve` | Review到達後のHITL承認を記録しWord出力を許可 |
| PATCH | `/api/documents/{document_id}` | 資料ステータスを変更 |
| GET | `/api/counterfactuals` | 取得者切替の分岐差分 |
| GET | `/api/harness` | 否認インパクトハーネス結果 |
| GET | `/api/export/word` | 書面添付の確認中ドラフトをWordで出力 |

## 公開境界

このリポジトリは公開ハッカソン専用の架空デモです。実顧客データ、会話履歴、秘密鍵、非公開検索基盤、非公開DB構造は含めません。AIは税務判断を断定せず、資料収集と下書き整理に留めます。
