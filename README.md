# AutoSpec

ローカル LLM（Ollama + Qwen3.8）と Aider で「仕様書 → 要件抽出 → テスト生成 → 実装 → 監査」を自動で回す開発環境。
仕様は [docs/AUTOSPEC_SPEC.md](docs/AUTOSPEC_SPEC.md)（v0.2、§21 に実装時の追記あり）。

## 現在の到達点

| マイルストーン | 状態 |
| :-- | :-- |
| M1 コア分解・旧スクリプト改善（CLI 互換） | 受入完了（2026-11-16、Ubuntu の Qwen3.8 で `pytest -m ollama` 通過） |
| M2 サーバー・ワーカー・最小 Web UI | 実装済み。**いまは Ubuntu 上で完結して使う前提（127.0.0.1 のみ・認証なし）** |
| M3〜M9（worktree 分離・差分・認証・analyze/design/modify・実機検証） | 未着手 |

Mac での実 LLM スモーク（2026-11-13、Qwen3.8 の代わりに qwen2.5-coder:14b・num_ctx 16384）：C の飽和演算サンプルが 3分53秒で一周し、aider ラウンド 1 でゲート PASS、監査 PASS（スコア 100）。

## 役割分担

- **Mac（この開発環境）**：コードの生成とテストだけを行う。LLM は FakeLLM、aider は偽の実行ファイル、C は実コンパイラでテストする。
- **Ubuntu（実際の開発環境）**：Qwen3.8 と実 aider で動かす。成果物は GitHub（histonoby/autospec）経由で移す。

## Ubuntu へのセットアップ

```bash
# private リポジトリのため、初回は認証（sudo apt install gh && gh auth login、または SSH 鍵）
gh repo clone histonoby/autospec && cd autospec
ollama pull <Qwen3.8 のタグ>            # まだなら
scripts/setup_ubuntu.sh <Qwen3.8 のタグ> [別名=local-coder-64k]  # venv・aider・config.yaml・別名作成・自己テスト
```

`setup_ubuntu.sh` は sudo を実行しません。足りないパッケージ（python3.11+、venv、build-essential など）は表示されるので、それを入れてから再実行してください。aider は依存の固定が多いため `.venv-aider` に分けて入れます。

ゲートコマンドは `profiles/*.yaml` が正です（旧スクリプト・旧 `verify_gate.sh` は使いません）。プロジェクトごとの差し替えは `<workspace>/.autospec/workspace.yaml` の `gates` で行います。

## Ubuntu での受入確認（M1）

```bash
.venv/bin/pytest -q                                   # Ollama なしで全件 PASS すること
AUTOSPEC_CONFIG=$PWD/config.yaml .venv/bin/pytest -m ollama -s   # 実 Qwen3.8 + aider で C サンプル一周
```

手元の C プロジェクトでも確認できます。

```bash
cd <サンプルのディレクトリ>
AUTOSPEC_CONFIG=~/autospec/config.yaml ~/autospec/.venv/bin/autospec run implement --lang c \
  --spec spec.md --interface foo.h --impl foo.c --test test_foo.c
```

成果物は `~/autospec/data/jobs/<job>/artifacts/` に出ます（requirements.json、coverage.json、audit_result.json / audit_report.md、rounds.json、diff.patch、llm/NNNN.json）。ログは同じジョブディレクトリの `events.log` です。

終了コード: 0 = ゲート PASS かつ監査 PASS、2 = ゲート PASS かつ監査 WARN、1 = 失敗、4 = 承認待ち。

## Web UI（M2）

Ubuntu 上で起動し、同じマシンのブラウザで開きます。

```bash
cd ~/autospec
AUTOSPEC_CONFIG=$PWD/config.yaml .venv/bin/autospec serve     # → http://127.0.0.1:8765/
```

- トップの「すぐ試す」で C サンプルを取り込み、implement ジョブがそのまま始まります。
- ジョブ画面：ステップの進み具合（aider のラウンド数つき）、ライブログ（ステップ・ゲート・aider の出力・LLM 呼び出しを種類で絞り込み）、承認パネル、成果物（監査レポート・要件・差分など）。
- ブラウザを再読み込みしてもログは最初から出直し、その後も続きが流れます。キャンセルで aider ごと停止します。
- サーバーを止めると実行中のジョブは「中断」になり、画面の「再開」で続きから回せます。
- ジョブは 1 本ずつ順に実行します（GPU 1 枚・Ollama 1 つ前提）。データは `data_dir` の `autospec.db`（SQLite）とジョブディレクトリに残ります。
- 画面右上に、設定中のモデル名と元モデルの情報（系統・サイズ・量子化・ctx）が出ます。緑なら Ollama・モデル・aider がそろっています。

M2 は認証がないため、`127.0.0.1` 以外では起動しません（他のマシンから見る・外部公開は M3 のトークン認証から）。UI のビルド済みファイル（`web/dist`）を同梱しているので、Ubuntu に Node は要りません。UI を直すときは Mac で `cd web && npm ci && npm run build` してからコミットします。

## CLI

| コマンド | 内容 |
| :-- | :-- |
| `autospec run implement --lang c --spec S --interface I --impl X --test T` | 実装パイプライン。`--worktree`、`--max-rounds`、`--reuse-tests`、`--model` など |
| `autospec resume <job> [--from-step N]` | 中断・失敗したジョブの再開 |
| `autospec approve <job> <target> [--reject --comment ...]` | 承認の記録（M5 以降で使用） |
| `autospec gate full --job <job>` | ゲートの単体実行（aider の `--test-cmd` から呼ばれる） |
| `autospec gate full --lang c --interface I --impl X --test T` | ジョブ無しでゲートだけ実行 |
| `autospec profiles` / `autospec health` | プロファイル一覧／Ollama・モデル・aider の確認 |
| `autospec serve [--port 8765]` | Web UI と API（127.0.0.1 のみ） |

## セキュリティ

AutoSpec は LLM が書いたコードをビルド・実行するため、**任意コード実行と同等の権限**を持ちます。

- すべてのコマンドにタイムアウトがあり、プロセスグループごと停止します。`sandbox.mode: docker` でネットワーク無しのコンテナ実行にできます。
- aider には `--no-suggest-shell-commands` を付けています。テストファイルはロック後に SHA-256 で改ざんを検出します。
- サーバー（M2 以降）を外部公開する場合は、トークン認証に加えて Cloudflare Access などの認証を前段に置いてください。
- 実機検証（第2部）のソフトウェア安全機構は、ハードウェアのインターロックの代わりにはなりません。

## 開発（Mac）

```bash
.venv/bin/pytest -q          # FakeLLM + 偽 aider + 実 cc（サーバーの受入シナリオ含む）
.venv/bin/ruff check . && .venv/bin/mypy autospec tests
cd web && npm run lint && npm run build   # UI（ビルド結果 web/dist もコミットする）
```
