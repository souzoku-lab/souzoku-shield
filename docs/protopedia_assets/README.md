# ProtoPedia差し替え画像

すべてProtoPediaの表示に合わせた1024×576です。

## 作品画像の順番

1. `01_hero.png` — 扉画像
2. `02_agent_safe_stop.png` — 問い返し・状態不変停止・再開
3. `03_responsibility_model.png` — AI / CODE / HUMANの責任分界
4. 最新本番UIのスクリーンショット — デプロイ後に撮影して追加

## システム構成

- `04_system_architecture.png`をProtoPediaの「システム構成」画像へ登録する。
- `souzoku_shield_protopedia_visuals.pptx`は4画像の編集可能な元データ。

## 公開前の必須確認

- 画像内のテスト件数が最終CIと一致している。
- `APP_VERSION`と公開mainの40文字SHAが一致している。
- 実Geminiで`request_clarification → select_taker_branch`がfallbackなしで成功している。
- 4枚目に実名、住所、APIキー、通知、ブックマークバーが映っていない。
- 5,600万円を税額ではなく「課税価格への影響（架空ケース）」と表示している。
