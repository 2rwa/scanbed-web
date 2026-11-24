# Scanbed Web

LAN向けフラットベッドスキャンアプリ。Brother DCP-J940N / SANE を初期対象とし、DVD専用解析から独立する。

- [要求仕様 v1.1（原本）](docs/spec-v1.1.md)
- [実装範囲・移行手順](docs/implementation.md)

## 開発モード（実機不要）
```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python server.py
```
http://127.0.0.1:8766/ を開く。既定は合成画像のモック。実機へのアクセスなし。

```sh
python -m unittest discover -s tests -v
```

**現在の8765番のサービスを置き換えない。** 実機有効化は別作業として、既存サービス停止と全スキャン終了、排他手順の確認後に実施する。
