# onvif-camera-work

このリポジトリは、ONVIF対応カメラを端末から操作して、静止画と動画を保存するためのサンプルコードです。まず接続を確認してから、パンとチルトの操作、撮影と録画へ進む流れにしてあります。

## 想定機種

このコードは、`TP-Link Tapo C210`を想定して調整しています。ほかのONVIF対応機種でも動く場合がありますが、スナップショット取得やパンとチルトの挙動、録画まわりの動きは差が出ることがあります。

## 何ができるか

- `deviceinfo.py`: 接続と機種情報の確認
- `ptz.py`: パンとチルトの単体確認（ズームは未対応）
- `capture_pic.py`: パンとチルト操作と静止画保存
- `capture_mov.py`: パンとチルト操作と静止画と動画保存

## 実行に必要なもの

このコードを動かすには、次が必要です。

- Python 3.12以上
- `uv`
- `ffmpeg`
- ONVIFに対応したカメラ（このREADMEは`TP-Link Tapo C210`を前提にしています）
- カメラへ到達できるネットワーク環境

## 実行手順

最初は次の順で進めます。

1. リポジトリをクローンする

```bash
git clone <レポジトリURL>
```

2. ディレクトリへ移動する

```bash
cd <クローンしたディレクトリ名>
```

3. 依存関係を入れる

```bash
uv sync
```

4. `.env`を作る

```bash
cp .env.sample .env
```

5. スクリプトを実行する

```bash
uv run deviceinfo.py
uv run ptz.py
uv run capture_pic.py
uv run capture_mov.py
```

`capture_pic.py`と`capture_mov.py`は、取得に失敗したときに`ffmpeg`を使う経路があります。事前に`ffmpeg`をインストールしてください。

## 設定

環境変数は`.env`で読み込みます。まず、sampleをコピーして`.env`を作ります。

```bash
cp .env.sample .env
```

設定項目は次のとおりです。

| 項目 | 必須 | 説明 | 既定値 |
| --- | --- | --- | --- |
| `ONVIF_HOST` | はい | カメラのホスト名またはIPアドレス | なし |
| `ONVIF_USER` | はい | ONVIFのユーザー名 | なし |
| `ONVIF_PASSWORD` | はい | ONVIFのパスワード | なし |
| `ONVIF_PORT` | いいえ | ONVIFのポート番号 | `80` |
| `MOUNT_MODE` | いいえ | 設置向き。`desk`または`ceiling` | `desk` |
| `PTZ_STEP` | いいえ | 1回の移動量 | `0.10` |
| `PTZ_MARGIN` | いいえ | パンとチルトの端で止める余白 | `0.02` |
| `PTZ_SETTLE_SEC` | いいえ | 移動後に待つ秒数 | `0.12` |
| `PTZ_PROBE` | いいえ | 起動時に上下方向を判定するときの試行量 | `0.12` |
| `STREAM_URL` | いいえ | RTSP URLを手動指定するときに使う。空なら自動生成URLを使う | 空 |
| `CAPTURE_DIR` | いいえ | 静止画の保存先 | `./captures` |
| `VIDEO_DIR` | いいえ | 動画の保存先 | `./captures` |
| `VIDEO_SECONDS` | いいえ | `V`キーで固定録画するときの秒数 | `10` |

最低限必要な設定例です。

```dotenv
ONVIF_HOST=192.168.1.20
ONVIF_PORT=80
ONVIF_USER=admin
ONVIF_PASSWORD=your_password
```

## キー操作

共通の基本操作は次のとおりです。

- 矢印キー または `WASD`: 移動
- `h`: ホーム移動
- `i`: 上下反転
- `q`: 終了

`capture_pic.py`と`capture_mov.py`では、次の操作を使えます。

- `p`: 静止画を保存

`capture_mov.py`では、さらに次の操作を使えます。

- `v`: 録画の開始と停止
- `V`: 固定秒数の録画（`VIDEO_SECONDS`で設定、既定値は10秒）

## 参考
- https://docs.astral.sh/uv/reference/cli/
- https://github.com/openvideolibs/python-onvif-zeep-async
- https://zenn.dev/collabostyle/articles/84e49b19e4508e
- https://www.tp-link.com/jp/support/faq/4465/
- https://www.tapo.com/jp/faq/34/
- https://github.com/kmizu/embodied-claude
