# ProtoPedia 全面差し替え原稿

## タイトル

Souzoku Shield - 相続の盾

## キャッチコピー

曖昧さはGemini、正しさは決定的処理、責任は人へ。相続専門税理士の小規模宅地確認と書面添付資料作成を支援するAIエージェント。

## 概要

相続税の小規模宅地等の特例は、同じ自宅でも「誰が取得するか」「同居していたか」「配偶者や同居親族がいるか」で確認ルートが変わります。取得者区分を取り違えると、このデモの架空値では課税価格に5,600万円の差が出ます。

Souzoku Shieldは、Geminiが未定型の相談文から取得者ルートをFunction Callingで選び、その構造化結果を受けた決定的ワークフローが、不足資料・リスク・確認タスク・書面添付ドラフトを組み立てるデモです。最後は税理士がレビュー完了（承認）してからだけ、書面添付資料をWordで出力します。

## 何がAIエージェントなのか

Geminiの役割は、自然文の曖昧さを解き、取得者区分を選ぶことです。公開しているFunction Callingは`select_taker_branch`の1つだけです。

決定的コアの役割は、税務要件、資料、課税価格への影響、ドラフトを再現可能に導出することです。AIに税務判断や総合所見を書かせません。

税理士の役割は、適用可否、最終記載、署名、提出を判断することです。システムはReviewで停止し、人の承認後だけWord出力を許可します。

## Runtime構成

```mermaid
flowchart LR
    U[税理士の相談文 / 相続人カード] --> G{{Gemini 3.5 Flash / Function Calling}}
    G -->|select_taker_branch| C[決定的税務コア]
    C --> D[不足資料 / リスク / 確認タスク]
    D --> R[Review停止]
    R -->|税理士承認| W[Word出力]
```

## DevOps構成

```mermaid
flowchart LR
    P[push / pull request] --> S[secret scan]
    S --> T[pytest 59件]
    T --> B[docker build]
    B --> H[container /api/health smoke]
    H -. CI成功後に明示デプロイ .-> C[Cloud Run]
```

## デモで確認できること

- 相続人カードを登録し、自宅取得者をクリックで選べる。
- 登録した相続人の関係性・同居区分を修正し、誤登録したカードを削除できる。
- 相談文なしでも相続人カードだけでReview作成できる。
- 相談文を入れた場合は、Geminiが`select_taker_branch`で取得者区分を選ぶ。
- 同居親族や配偶者がいるのに別居親族が自宅を取得する場合、適用不可アラートを出す。
- 否認インパクトは税額ではなく、課税価格への影響`+56,000,000円`として表示する。
- 配偶者取得では二次相続の検討アラートを出す。
- 総合所見はAIが書かず、税理士が画面上で入力する。
- 承認前はWord出力不可、承認後のみWord出力可能。

## 公開デモの安全性

- 公開用の架空デモです。実名、住所、マイナンバー、実案件情報は入力しないでください。
- 相談文はGemini APIへ送信されます。
- 状態は訪問者間でセッション分離されます。インスタンス再起動時には初期化される一時状態です。
- CORSは開放していません。
- 相談文は8〜1200文字、AI実行は2秒cooldown・1セッション20回までです。
- Gemini APIキーはSecret Managerで管理し、GitHubには含めません。

## 未実装のもの

- 登記事項証明書のOCR。
- 登記名義の自動照合。
- 実顧客データの保存。
- 本人認証。
- 永続監査ログ。
- Firestore等の永続DB。
- 税額の最終計算や申告書提出。

## 技術スタック

- FastAPI
- Python 3.13
- Gemini API / `google-genai==2.10.0`
- GitHub Actions CI
- Docker
- Cloud Run
- python-docx

## 審査員向け確認ポイント

1. Cloud Run公開URLを開く。
2. 個人情報警告を確認する。
3. 既定デモ文でAIエージェントを実行する。
4. Gemini実行トレースで`mode=gemini_function_calling`、`tool=select_taker_branch`、`fallback=false`を確認する。
5. 課税価格`+56,000,000円`のアラートを確認する。
6. 総合所見を手入力し、レビュー完了（承認）する。
7. 承認後だけWord出力できることを確認する。
8. GitHub Actions CIでsecret scan、pytest、Docker build、container health smokeが通っていることを確認する。
