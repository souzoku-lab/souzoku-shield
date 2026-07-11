# 最終提出手順書

対象: DevOps x AI Agent Hackathon / Souzoku Shield - 相続の盾

この手順書は、ローカル、公開GitHub、Cloud Run本番を分けて検証するための公開版メモです。最終SHAは作業完了報告で1つだけ記載します。文書内に古いコミットSHAは固定しません。

## 1. ローカル最終HEAD確認

```powershell
git rev-parse HEAD
git status --porcelain
python scripts\verify_no_secrets.py
python -m pytest -q
docker build -t souzoku-shield:local .
docker run --rm -p 8080:8080 souzoku-shield:local
curl http://127.0.0.1:8080/api/health
git ls-files
```

合格条件:

- working treeがクリーン。
- secret scanがOK。
- pytest 56件が成功。
- Docker buildが成功し、コンテナの`/api/health`が`ok=true`を返す。
- `.env`、ログ、検証一時ファイル、APIキーがgit管理対象に含まれない。
- `google-genai==2.10.0`で再現性を固定する。

## 2. 公開GitHub反映

force push、orphan branch、`git add -A`は使わない。公開mainを新規cloneし、通常ブランチで差分を確認する。

```powershell
git clone https://github.com/souzoku-lab/souzoku-shield.git C:\tmp\souzoku-shield-public-checkout
cd C:\tmp\souzoku-shield-public-checkout
git switch -c final-public-submission
```

ローカル最終HEADのgit追跡済みファイルだけを移し、差分を確認する。

push直前に報告する項目:

| 項目 | 内容 |
|---|---|
| FINAL_SHA | `git rev-parse HEAD`の結果 |
| push対象remote | `git remote -v` |
| 公開mainの現在SHA | `git rev-parse origin/main` |
| 変更ファイル一覧 | `git diff --name-status origin/main` |
| secret scan結果 | `python scripts\verify_no_secrets.py` |
| pytest結果 | `python -m pytest -q` |
| Docker build結果 | `docker build -t souzoku-shield:public-check .` |
| rollback先 | 公開mainの現在SHA |

ユーザー確認後に通常pushし、PRまたは通常mergeでmainへ反映する。反映後、公開mainを再cloneして再現確認する。

## 3. Cloud Runデプロイ

Cloud Runは公開GitHubに反映されたmainを新規cloneしたクリーンなディレクトリからdeployする。元の作業ディレクトリから直接deployしない。

deploy前に報告する項目:

| 項目 | 内容 |
|---|---|
| gcloudアカウント | `gcloud auth list` |
| project ID | `gcloud config get-value project` |
| region | `asia-northeast1` |
| service名 | `souzoku-agent` |
| 現在稼働中revision | `gcloud run revisions list ...` |
| rollback先revision | 現在traffic 100%のrevision |
| デプロイ対象SHA | 公開mainの`git rev-parse HEAD` |

コマンドでは必ず`--project`を明示し、`APP_VERSION=<公開main SHA>`を設定する。失敗時は旧revisionへ戻せる状態を維持する。

## 4. Gemini実接続証拠

`gemini_configured=true`は環境変数の有無であり、成功証拠として扱わない。Cloud Run本番で実行トレースを確認する。

5回試験:

- 各回を新規セッションで開始。
- 既定ケースを実行。
- 5回すべてで`mode=gemini_function_calling`、`tool=select_taker_branch`、`result=house_lost`、`fallback=false`、latency記録、Cloud Runログ例外なしを確認。

9ケース評価:

- 期待ルート`spouse`、`co_resident`、`house_lost`を各3言い換え、合計9件。
- 入力文、期待ルート、実際のルート、Function名、fallback有無、latency、合否を記録。

公開E2Eと合わせた機械実行:

```powershell
python scripts\production_evidence.py `
  --base-url https://souzoku-agent-698253423667.asia-northeast1.run.app `
  --output C:\tmp\souzoku-shield-production-evidence.json `
  --word-output C:\tmp\souzoku-shield-production.docx
```

証拠JSONは公開リポジトリへ追加せず、提出時の確認記録として保管する。

## 5. 公開E2E確認

- ログアウト状態で開ける。
- 新ブランドと個人情報警告が表示される。
- Gemini実行トレースが読める。
- 課税価格への影響`+56,000,000円`が表示され、「税額」ではないことが分かる。
- Chrome通常ブラウザとEdgeなど、Cookieを共有しない2環境でセッションが混ざらない。
- Cookieに`HttpOnly; Secure; SameSite=Lax`が付く。
- 任意Originを許すCORS設定が無い。
- 承認前はWord出力不可、承認後のみWord出力可能。
- Microsoft Wordで`.docx`を開け、破損警告・文字化けがない。
- 初期化が別セッションへ影響しない。
- 相談文8〜1200文字、2秒cooldown、1セッション20回上限、同時実行防止が有効。

Gemini APIのクォータ・予算通知はリポジトリ内の機能ではなくGoogle Cloud側の設定であるため、デプロイ前に対象projectのBudget alertsとAPI quotaを別途確認する。

## 6. ユーザーが行う提出作業

Codexは認証が必要なYouTube、ProtoPedia、Google Formを勝手に操作しない。

1. 60秒動画をYouTube限定公開へアップロード。
2. ProtoPedia本文を`docs/protopedia_replacement.md`の内容で全面置換。
3. Cloud Run URL、GitHub URL、YouTube URL、CI結果を確認。
4. 必要に応じてGoogle Formから再応募または追記提出。

## 今日やらないこと

Firestore導入、OCR・登記自動照合、本格認証、永続監査ログ、新しい税務論点、マルチツール化目的の機能追加、未検証の`tool_choice`、Elasticsearch、Vertex、ADK等の後付け、大規模UI刷新は行わない。
