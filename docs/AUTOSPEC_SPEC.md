# AutoSpec 実装仕様書 v0.2
ローカルLLM（Ollama + Qwen3.8）と Aider による自律開発環境の汎用化・Webアプリ化・実機検証自動化

| 版 | 変更内容 |
| :-- | :-- |
| v0.1 | 初版 |
| v0.2 | 旧スクリプトのレビュー結果を反映（§6.4, §6.6〜6.9, §7.3〜7.4, §8, §9, §18, §19）。対象言語を c / cpp / python / web / verilog に確定。第2部「実機検証（HIL）拡張」を追加 |

---

## 0. Claude Code への作業指示

- 本書の **§19 マイルストーン** の順に実装すること。各マイルストーンの受入基準を満たしてから次へ進む。
- 既存スクリプト（以下「旧スクリプト」）は `legacy/autospec_cli.py` として**無改変で保存**し、挙動比較の基準とする。
- 既存の `harness/verify_gate.sh` を**最初に読み**、言語ごとの分岐内容を §7 のプロファイルYAMLへ移植すること。中身が本書の想定と異なる場合は、移植前にユーザーへ確認する。
- システム自体のテストは **Ollama なしで実行できること**（§20 の FakeLLM を使う）。
- 不明点は §21「未決事項」に追記し、推測で大きな設計判断をしない。
- 依存ライブラリは本書に記載のものを基本とし、追加する場合は理由をコミットメッセージに書く。
- **この開発環境（Claude Code の実行環境）には Qwen3.8 も実機も無い前提**で進めること。LLM が関わる部分は FakeLLM、実機が関わる部分は mock ターゲット（第2部 H3.4）でテストする。Qwen3.8 での品質確認と実機での確認はユーザーが行う。
- 第2部（実機検証）の実装は M7 以降。ただし M1 の時点で §6.9 の拡張ポイントを用意し、後から追加しても core を作り直さずに済むようにする。

---

## 1. 背景と目的

旧スクリプトは「仕様書＋インターフェース定義 → テスト生成 → Aider実装 → 仕様監査」を1ファイルで実行するCLIで、C言語では実用水準で動作した。これを次の方向に拡張する。

1. **汎用化**：「新規設計」「既存プロジェクト解析」「既存プロジェクト改修」を同じエンジンで扱う
2. **言語追加の容易化**：言語依存部分を設定ファイル（プロファイル）に分離する
3. **Webアプリ化**：ブラウザ（スマホ含む）からジョブの作成・監視・承認・差分確認ができる
4. **安全性**：LLM生成コードの実行隔離、テスト改ざん検知、Web経由の不正実行防止
5. **実機検証の自動化**：ビルド → 書き込み → 実機テスト → 合否判定 → 修正までを自動で回す。実機ごとの操作スクリプトは後から追加できるようにする（第2部）

## 2. スコープ

### 対象
- パイプライン：`implement` / `design` / `analyze` / `modify`（§8）
- 言語プロファイル：`c` / `cpp`（C系）、`python`、`web`（TypeScript）、`verilog`（SystemVerilog）
- 実機検証（HIL）の拡張機構：実機ごとのスクリプトを後から追加し、パイプラインと AI から安全に呼べるようにする（第2部）
- FastAPI バックエンド、単一ワーカーのジョブキュー、SSE によるログ配信
- React (Vite + TypeScript) のWeb UI
- git worktree によるジョブ単位の作業分離、diff 表示、マージ／破棄
- Docker によるゲートコマンドの隔離実行（オプション）

### 対象外（v0.1）
- 複数GPU・複数ワーカーの並列実行
- 複数ユーザー・権限管理（単一ユーザー＋トークン認証のみ）
- クラウドLLMとの切替（LLMクライアントは差し替え可能な設計にだけしておく）
- IDE プラグイン

## 3. 前提環境

| 項目 | 内容 |
| :-- | :-- |
| OS | Linux または WSL2（`verify_gate.sh` が bash 前提のため） |
| Python | 3.11 以上 |
| Node.js | 20 以上（Web UI のビルド用、および `web` プロファイルのテスト実行用） |
| Ollama | `http://127.0.0.1:11434`、既定モデル名 `local-coder-64k`（Qwen3.8、num_ctx 65536） |
| Aider | `aider` コマンドが PATH 上にあること |
| Docker | 任意。`sandbox.mode: docker` のときのみ必要 |
| C系ツールチェーン | gcc / g++（ホスト）。組込み向けは任意で arm-none-eabi-gcc、openocd / probe-rs 等 |
| Python系 | pytest（必須）、ruff（任意） |
| Web系 | TypeScript、tsx（node:test 実行用）、Playwright（任意・E2E 用） |
| Verilog系 | iverilog または Verilator（シミュレーション）。Vivado は任意（合成・タイミング確認・実機書き込み） |
| 実機関連 | pyserial、pyvisa 等はターゲットのアクションスクリプト側の依存として扱う（core の依存にしない） |
| git | 2.30 以上（worktree 使用） |
| ctags | 任意。universal-ctags があれば解析モードで使用 |

## 4. 全体アーキテクチャ

```
┌──────────── Browser (React) ────────────┐
│ ワークスペース / ジョブ作成 / ジョブ詳細  │
└───────┬──────────────────────▲───────────┘
        │ REST                 │ SSE (events)
┌───────▼──────────────────────┴───────────┐
│ FastAPI (server/api.py)                  │
│   ├─ DB (SQLite)                         │
│   └─ JobQueue ──► Worker (1本, thread)   │
│                     │                    │
│        Pipeline Runner (core/runner.py)  │
│          Step → Step → Step ...          │
│           │        │         │           │
│        LLMClient  Aider    Sandbox       │
│        (Ollama)  (subproc) (local/docker)│
└──────────────────────────────────────────┘
        │ git worktree
┌───────▼──────────────────────────────────┐
│ WORKSPACES_ROOT/<ws>/  (gitリポジトリ)    │
│ DATA_DIR/worktrees/job-<id>/             │
│ DATA_DIR/jobs/<id>/artifacts/            │
└──────────────────────────────────────────┘
```

CLI（`autospec run ...`）は Web を経由せず Pipeline Runner を直接呼ぶ。Web とCLIは同じコアを共有する。

## 5. ディレクトリ構成

```
autospec/
  pyproject.toml
  config.example.yaml
  legacy/
    autospec_cli.py          # 旧スクリプト（無改変）
  harness/
    verify_gate.sh           # 既存（移行期間中は残す）
  autospec/
    __init__.py
    cli.py                   # `autospec` コマンド（run / gate / serve）
    config.py                # 設定ロード（YAML + 環境変数）
    core/
      llm.py                 # LLMClient（Ollama実装 / Fake実装）
      events.py              # EventEmitter, イベント型
      context.py             # JobContext
      runner.py              # PipelineRunner（ステップ実行・再開・キャンセル）
      step.py                # Step 基底クラス, StepResult
      workspace.py           # ワークスペース・worktree・git操作
      sandbox.py             # コマンド実行（local / docker）
      profiles.py            # プロファイルのロードとテンプレート展開
      textutil.py            # strip_thinking, extract_code_block, トークン概算
      schemas.py             # Structured Outputs 用 pydantic モデル
      registry.py            # ステップ・パイプライン・プロファイル・ターゲットの登録（§6.9）
    steps/
      prepare.py             # prepare_workspace, finalize
      requirements.py        # requirements_extract
      interface.py           # interface_design
      approval.py            # approval
      testgen.py             # test_gen, red_check, lock_tests
      implement.py           # implement（Aider）
      integrity.py           # integrity_check
      audit.py               # audit
      analyze.py             # repo_scan, repo_map, file_summaries, relevance_select, deep_read, report
      modify.py              # change_plan, baseline_tests, regression
      hw.py                  # 第2部：hw_plan, hw_build, hw_acquire, hw_deploy, hw_test, hw_diagnose, hw_release, hw_route, hw_report
    pipelines/
      __init__.py            # パイプライン定義（ステップの並び）
    server/
      api.py                 # FastAPI ルーティング
      db.py                  # SQLite アクセス層
      worker.py              # JobQueue, Worker
      auth.py                # トークン認証
    hw/                      # 第2部
      target.py              # ターゲット定義のロード・検証
      runner.py              # アクション実行（local / remote agent / mock）
      evaluator.py           # HWテスト計画の決定的な合否判定
      locks.py               # 実機リソースの排他制御
      sdk.py                 # アクションスクリプト用ヘルパー（標準ライブラリのみ）
  profiles/
    c.yaml
    cpp.yaml
    python.yaml
    web.yaml
    verilog.yaml
  test_support/              # テスト用の共通ファイル（§7.3）
    autospec_test.h          # C/C++ 用（失敗を数えて全件表示）
    autospec_tb.svh          # SystemVerilog 用（チェック・ウォッチドッグ）
  targets/                   # 第2部：実機ターゲット定義（後から追加していく）
    mock-mcu/                # 模擬ターゲット（H3.4）
    mock-faulty/             # 異常注入用の模擬ターゲット
  agent/                     # 第2部：リモート実行エージェント（M9）
  web/                       # React + Vite + TypeScript
  tests/
    fixtures/                # サンプル仕様・IF・小規模リポジトリ
    test_*.py
```

## 6. コア設計

### 6.1 用語

| 用語 | 意味 |
| :-- | :-- |
| Workspace | `WORKSPACES_ROOT/<name>/` にある git リポジトリ。1プロジェクト＝1ワークスペース |
| Job | 1回のパイプライン実行。ワークスペース・パイプライン名・入力・オプションを持つ |
| Pipeline | ステップの並び。名前で登録（§8） |
| Step | 処理の最小単位。`run(ctx)` を持つ |
| Profile | 言語ごとの設定（テスト指示、雛形、ゲートコマンド等） |
| Gate | 機械的な合否判定コマンド。終了コード 0 = PASS |
| Approval | 人の承認待ちでジョブを一時停止するステップ |
| Artifact | ジョブが生成したファイル（要件JSON、レポート、LLMログ等） |

### 6.2 Step インターフェース

```python
class StepStatus(str, Enum):
    DONE = "done"
    FAILED = "failed"
    WAITING_APPROVAL = "waiting_approval"
    SKIPPED = "skipped"

@dataclass
class StepResult:
    status: StepStatus
    message: str = ""
    data: dict = field(default_factory=dict)
    jump_to: str | None = None      # 指定時、そのステップ名へ戻って再実行（実機NG→実装へ戻る等）

class Step(ABC):
    name: str                       # 例: "test_gen"
    always_run: bool = False        # True なら前のステップの失敗・キャンセル後も実行（後始末用）
    def __init__(self, **params): self.params = params
    @abstractmethod
    def run(self, ctx: "JobContext") -> StepResult: ...
    def should_skip(self, ctx) -> bool: return False
```

- ステップは**同期関数**として実装し、ワーカースレッド上で実行する。
- ステップ間で受け渡す値は `ctx.state`（JSONシリアライズ可能な dict）に入れる。Runner はステップ終了ごとに `ctx.state` を DB に保存する。これにより承認待ち・サーバー再起動後の**再開**が可能になる。
- ファイルとして残すべき成果物は `ctx.artifacts.write(name, content)` を使う（`DATA_DIR/jobs/<id>/artifacts/` に保存し、`artifact.created` イベントを発行）。保存先ディレクトリは書き込み前に `mkdir(parents=True)` する。
- `jump_to` によるループは、ジョブごとに `max_jumps`（既定10）で上限を設け、無限ループを防ぐ。
- `always_run = True` のステップは、前のステップが失敗・キャンセルされても必ず実行される（第2部の `hw_release` が使う）。

### 6.3 JobContext

```python
@dataclass
class JobContext:
    job_id: str
    workspace: Workspace            # 名前, ルートパス
    workdir: Path                   # 実際に作業するパス（worktree または読み取り専用チェックアウト）
    profile: Profile | None         # analyze ではNone可
    inputs: dict                    # spec_text, request_text, interface_path, impl_path, test_path 等
    options: dict                   # max_rounds 等の上書き
    state: dict                     # ステップ間共有（永続化対象）
    llm: LLMClient
    events: EventEmitter
    sandbox: Sandbox
    artifacts: ArtifactStore
    cancel: CancelToken
```

### 6.4 LLMClient

```python
class LLMClient(Protocol):
    def chat(self, messages: list[dict], *, think: bool = False,
             schema: type[BaseModel] | None = None,
             temperature: float | None = None) -> str | BaseModel: ...
```

- `OllamaClient`：旧スクリプトの `call_ollama_chat` を移植。
  - `think` パラメータを payload に明示する（旧スクリプト同様）。`think=False` のときはユーザーメッセージ末尾に `/no_think` を自動付与する（各ステップで付け忘れないよう、クライアント側で一元化）。
  - レスポンスは `strip_thinking` を通す。
  - `schema` 指定時は `format` に `schema.model_json_schema()` を渡し、`model_validate_json` で検証する。**検証失敗時はエラー内容を会話に追加して最大2回再試行**する。
  - タイムアウト（既定600秒）、接続エラー時は指数バックオフで3回まで再試行。
  - 呼び出しごとに prompt / response 全文を `artifacts/llm/NNNN.json` に保存し、イベントには先頭500文字のみ載せる。
  - 各呼び出し前に `ctx.cancel` を確認する。
  - **リトライ時の会話は直近1回分だけ残す**：`[system, 最初のuser, 直前のassistant, エラーを伝えるuser]` の4件に圧縮する。全履歴を積み上げると num_ctx を超え、Ollama が古いメッセージ（最初に渡した仕様書を含む）から切り捨てるため。
  - **リトライごとに温度を上げる**：`temperature = base + 0.15 × リトライ回数`（上限0.7）。同じ誤りの繰り返しを避ける。
  - 送信前にメッセージ全体のトークン概算（§8.7）が予算を超えていないか確認し、超える場合は警告イベントを出して例外を返す（黙って送らない）。
  - ツール呼び出し（第2部 H7）に備え、`tools` 引数を受け付けられる形にしておく（M1 では未使用でよい）。
- `FakeLLMClient`：テスト用。事前登録した応答をキュー順に返す（§20）。

### 6.5 イベント

全イベントは次の形で DB に保存し、SSE で配信する。

```json
{
  "id": 123,
  "job_id": "j_20261113_0001",
  "ts": "2026-11-13T18:00:00+09:00",
  "type": "gate.result",
  "step": "implement",
  "level": "info",
  "message": "full gate FAILED (round 2/5)",
  "data": { "gate": "full", "returncode": 1, "output_tail": "..." }
}
```

| type | 用途 |
| :-- | :-- |
| `job.status` | ジョブ状態の変化（§12） |
| `step.start` / `step.end` | ステップ開始・終了（`data.status`） |
| `log` | 一般ログ（旧スクリプトの print を置き換え） |
| `llm.request` / `llm.response` | LLM呼び出し（要約のみ、全文は artifact） |
| `gate.result` | ゲート実行結果 |
| `aider.output` | Aider の標準出力（1行ずつ） |
| `approval.required` / `approval.resolved` | 承認の要求・結果 |
| `artifact.created` | 成果物の作成 |

CLI 実行時は EventEmitter のコンソール実装が人間向けに整形して表示する（旧スクリプトの絵文字付き表示を踏襲してよい）。

### 6.6 Sandbox

```python
class Sandbox(Protocol):
    def run(self, cmd: str, *, cwd: Path, timeout: int,
            on_line: Callable[[str], None] | None = None) -> CompletedRun: ...
    # CompletedRun: returncode, stdout, stderr, timed_out
```

- `LocalSandbox`：`subprocess.Popen(..., start_new_session=True)`。キャンセル・タイムアウト時は `os.killpg` でプロセスグループごと停止。
- `DockerSandbox`：`docker run --rm --network none -v <workdir>:/work -w /work <image> bash -lc "<cmd>"`。イメージはプロファイルの `sandbox_image`。
- ゲートコマンドは**必ず Sandbox 経由**で実行する。
- **すべてのコマンドにタイムアウトを必須とする**（プロファイルの `timeouts`、既定300秒）。LLM が書いたテストやシミュレーションが無限ループしても全体が止まらないようにする。タイムアウトは `timed_out=True` の失敗として扱い、タイムアウトしたことをエラー要約（§6.8）で LLM に伝える。
- コマンドは原則として引数リストで組み立てる。テンプレート展開で文字列にする場合は、埋め込む値（パス等）をすべて `shlex.quote` する。

### 6.7 コードブロック抽出（`core/textutil.py`）

旧スクリプトの `extract_code_block` は「最後のブロックを取る」実装で、次の不具合を実際に再現確認済み。

| 入力 | 旧実装の結果 |
| :-- | :-- |
| テスト本体の後に `bash` のビルド例ブロック | ビルドコマンド1行がテストファイルになる |
| フェンスのタグが `c++` | 正規表現に一致せず、フェンス記号ごとファイルに書かれる |
| フェンスのタグ `c` の後ろに空白 | 同上 |
| コードブロック無し | 説明文がそのままファイルに書かれる |
| `<think>` が閉じていない | 思考途中の断片コードを採用する |

新実装の規則：

1. 閉じた `<think>...</think>` は除去する。閉じていない `<think>` が残っていたら応答が途中で切れたとみなし、`ExtractionError` とする。
2. フェンスは「3連バッククォート＋任意の空白＋タグ（`[\w+#.-]*`）＋行末までの任意文字＋改行 … 3連バッククォート」として解析する（`c++`、末尾空白、CRLF に対応）。
3. 選択順：(a) タグがプロファイルの `code_fence_tags` に含まれるブロックのうち最長のもの → (b) タグ無しブロックのうち最長のもの → (c) 見つからなければ `ExtractionError`。
4. プロファイルの `sanity_patterns`（例：C のテストなら `\bmain\s*\(`）に一致しなければ `ExtractionError`。
5. `ExtractionError` のときはゲートに回さず、「`<tag>` のコードブロック1つで全文を出力してください」と伝えてリトライする（リトライ回数には数える）。

上の5ケースは回帰テストとして `tests/test_textutil.py` に入れること。

### 6.8 エラーログの要約（`extract_error_digest`）

旧スクリプトはゲート出力の**末尾**だけを LLM に渡していた。gcc などは**最初のエラーが原因で、後ろは連鎖したエラー**であることが多く、肝心な部分が切れる。

- プロファイルの `error_patterns`（例：`error:`, `FAILED`, `AssertionError`, `FATAL`, `FAIL `）に一致する行を、前後2行の文脈付きで**出現順に**集める。
- 最初に一致した箇所は必ず含める。
- 残りの枠に出力の末尾（テストランナーの集計行など）を入れる。
- 合計は文字数上限（既定2500）に収め、省略した場合は `...(N行省略)...` と明示する。
- タイムアウト時は先頭に `[TIMEOUT] <秒数>秒で打ち切りました` を付ける。

### 6.9 拡張ポイント（M1 から用意する）

第2部の実機検証や今後の言語追加を、core を作り直さずに行えるよう、M1 の時点で次を用意する。

- `core/registry.py`：ステップ・パイプライン・プロファイル・ターゲットを名前で登録・取得する仕組み。パイプラインは「ステップ名のリスト＋パラメータ」で定義でき、`pipelines/*.yaml` からも組み立てられるようにする。
- `StepResult.jump_to` と `Step.always_run`（§6.2）。
- リソースロックの抽象（`acquire(resource_ids, timeout)` / `release()`）。M1 では LLM（GPU）用の1リソースだけでよく、第2部で実機リソースを追加する。
- ジョブ状態 `waiting_device`（実機の空き待ち）を予約しておく。

## 7. 言語プロファイル

### 7.1 スキーマ

```yaml
name: c
display_name: "C (C11)"
extensions: [".c", ".h"]
sandbox_image: "gcc:13"

# テスト生成プロンプトに埋め込む言語別要件。{if_name} {impl_name} {impl_stem} を展開
test_instructions: |
  C言語（C11）の単体テストコード（main関数を含む自己完結ファイル）を作成してください。
  - 必ず `#include "{if_name}"`, `<math.h>`, `<assert.h>`, `<stdio.h>` をインクルードすること。
  - float型の値比較には `==` を使わず、`fabsf(actual - expected) < 1e-4f` を用いること。
  - テスト対象の関数本体はこのファイル内に定義しないこと。

# 実装ファイルが無い/空のときの雛形。{if_name} {if_content} を展開
scaffold: |
  #include "{if_name}"

  // TODO: Implement functions declared in {if_name}

# スタブ（全関数がゼロ値/空を返す最小実装）の生成指示。red_check で使用
stub_instructions: |
  {if_name} で宣言された全関数について、何もせずゼロ値を返す最小実装を書いてください。

# ゲート。{interface} {impl} {test} {workdir} を展開。終了コード0でPASS
gates:
  syntax: "gcc -std=c11 -fsyntax-only {test}"
  full: "gcc -std=c11 -Wall -Werror -o /tmp/t {impl} {test} -lm && /tmp/t"
  interface: "gcc -std=c11 -fsyntax-only -x c {interface}"   # 任意
  project_test: "make test"                                  # modify の回帰確認用（任意）

red_check: true

# 解析モードでのシグネチャ抽出（ctags が無い場合のフォールバック）
signature_patterns:
  - '^\s*[\w\*\s]+\s+\**(\w+)\s*\([^;]*\)\s*\{'
```

- 上記コマンド例はあくまで仮。**実際の値は既存 `harness/verify_gate.sh` から移植すること**。移植が難しい言語は暫定的に `full: "bash {harness}/verify_gate.sh full c {interface} {impl} {test}"` のようにスクリプト呼び出しのままでよい（`{harness}` はハーネスディレクトリの**絶対パス**）。
- ワークスペース側で上書きできる：`<workspace>/.autospec/workspace.yaml` の `profile`, `gates.*`, `ignore` を優先する。

### 7.2 プロファイルの検証
起動時に全プロファイルを pydantic で検証し、必須キー欠落はエラーで起動を止める。`GET /api/profiles` で一覧を返す。

### 7.3 v0.2 で追加するキー

```yaml
code_fence_tags: [c, h]                       # §6.7 のブロック選択に使う
sanity_patterns: ['\bmain\s*\(']              # テストコードとして最低限満たすべきパターン
error_patterns: ['error:', 'FAIL ', 'FATAL']  # §6.8
failure_count_pattern: 'FAILED: (\d+)'        # ゲート出力から失敗件数を取る（§9 の最良版保持に使う）
test_name_pattern: 'test_{req_id_compact}_\w+'  # テスト名に要件IDを入れる規約（§8.5 coverage_check）
support_files: [autospec_test.h]              # test_support/ から作業ディレクトリへ読み取り専用でコピー
timeouts: { syntax: 60, full: 300 }           # ゲートごとのタイムアウト秒
post_gates: []                                # full 通過後に一度だけ実行する重いゲート（Verilog の合成など）
```

- `{req_id_compact}` は `REQ-001` を `REQ001` に変換した値。
- テスト生成プロンプト（`test_instructions`）には、要件IDをテスト名に含めること、`support_files` を使うことを必ず書く。
- 特定の仕様の名残（旧スクリプトの `pid_init` のような関数名）をプロファイルやプロンプトに直書きしない。

#### `test_support/autospec_test.h`（C/C++ 用）
- `TEST_BEGIN("REQ001_xxx")`, `CHECK(cond)`, `CHECK_EQ_INT(a, b)`, `CHECK_NEAR(a, b, eps)` を提供する。
- 失敗しても止まらずに数え、`FAIL <テスト名> <file>:<line> <式>` を出力する。最後に `AUTOSPEC_SUMMARY()` が `PASSED: n FAILED: m` を出し、失敗があれば非ゼロを返す。
- `assert.h` は使わない。最初の失敗で止まるため1ラウンドに1件しか直せないことと、`-DNDEBUG` 付きビルドで全テストが素通りする罠があるため。

#### `test_support/autospec_tb.svh`（SystemVerilog 用）
- `CHECK_EQ(actual, expected, name)` マクロで失敗を数える。
- テストベンチに**ウォッチドッグ**（一定時間で `$display("FATAL: timeout"); $finish;`）を必ず入れさせる。
- 終了時に `PASSED: n FAILED: m` を出し、全通過なら `PASS`、そうでなければ `FATAL: mismatch` を出す（旧スクリプトの判定規約と互換）。

### 7.4 言語ごとの方針

| プロファイル | syntax ゲート | full ゲート | post_gates（任意） | 雛形・注意点 |
| :-- | :-- | :-- | :-- | :-- |
| `c` | `gcc -std=c11 -fsyntax-only` | ビルド＋実行（`-Wall -Werror`、`-DNDEBUG` は付けない） | — | 雛形は `#include` ＋ `// TODO` |
| `cpp` | `g++ -std=c++17 -fsyntax-only` | 同上 | — | `autospec_test.h` を共用 |
| `python` | `python -m py_compile`（任意で ruff） | `pytest -q`（`PYTHONPATH` に作業ディレクトリ） | — | 雛形は `# TODO` |
| `web` | `tsc --noEmit` | `tsx --test` または `node --test` | Playwright による E2E | **雛形は `// TODO`**（旧スクリプトは `#` を書いており TS として不正）。import の拡張子の扱いを tsconfig と揃える |
| `verilog` | `verilator --lint-only` または `iverilog -g2012` | シミュレーション実行 | Vivado バッチで合成・配置配線し、**WNS ≥ 0 をタイミングゲート**にする | 合成は時間がかかるため `timeouts` を長くする（例：7200秒） |

コマンドは仮の例。実際の値は既存の `verify_gate.sh` から移植する。

## 8. パイプライン

パイプラインは `pipelines/__init__.py` でステップのリストとして登録する。

```python
PIPELINES = {
  "implement": [PrepareWorkspace(), RequirementsExtract(), TestGen(), CoverageCheck(),
                RedCheck(), LockTests(), Implement(), IntegrityCheck(), Audit(), Finalize()],
  "design":    [PrepareWorkspace(), RequirementsExtract(), Approval(target="requirements"),
                InterfaceDesign(), Approval(target="interface"),
                TestGen(), CoverageCheck(), RedCheck(), LockTests(),
                Implement(), IntegrityCheck(), Audit(), Finalize()],
  "analyze":   [RepoScan(), RepoMap(), FileSummaries(), RelevanceSelect(),
                DeepRead(), Report()],
  "modify":    [PrepareWorkspace(), RepoScan(), RepoMap(), FileSummaries(),
                RelevanceSelect(), DeepRead(), ChangePlan(), Approval(target="plan"),
                BaselineTests(), TestGen(mode="modify"), CoverageCheck(), RedCheck(), LockTests(),
                Implement(mode="modify"), IntegrityCheck(), Regression(),
                Audit(), Finalize()],
  # 第2部（M8）：既存コードを変更せずに実機で検証する
  "hw_verify": [HwPlan(), Approval(target="hw_plan", only_if_new=True), HwBuild(),
                HwAcquire(), HwDeploy(), HwTest(), HwDiagnose(), HwRelease(), HwReport()],
}
```

- `implement` にも `RequirementsExtract` を入れる（承認は省略、結果は artifact に残す）。仕様書に要件IDが無くても、テスト命名・網羅確認・監査を要件IDを軸に動かすため。
- `implement` / `design` / `modify` は、ジョブオプション `hw_target` を指定すると `Audit` の後に第2部 H6 の実機ステップ列を追加する。

### 8.1 `implement`（旧スクリプト互換）
入力：`spec_path`, `interface_path`, `impl_path`, `test_path`, `profile`
旧スクリプトの Phase1→2→3 と同等の結果になること（M1 の受入基準）。

### 8.2 `design`（新規設計）
入力：`spec_text`（または `spec_path`）、`profile`、出力先パス（省略時はプロファイル既定：例 `src/<module>.h`, `src/<module>.c`, `tests/test_<module>.c`）
仕様書だけから要件抽出・インターフェース設計を行い、人の承認を経て `implement` と同じ後半に進む。

### 8.3 `analyze`（既存プロジェクト解析・読み取り専用）
入力：`request_text`（例：「エラー処理の流れを説明して」「○○機能を追加するなら影響範囲は？」）
ワークスペースに**一切書き込まない**。出力は artifact（`analysis.md`, `analysis.json`）のみ。

### 8.4 `modify`（既存プロジェクト改修）
入力：`request_text`、`profile`（省略時はワークスペース設定または拡張子から推定）
解析 → 変更計画 → 承認 → 現状テスト → 新規テスト → 実装 → 回帰確認 → 監査。

### 8.5 ステップ詳細

| ステップ | 入力 | 処理 | 出力（state / artifact） |
| :-- | :-- | :-- | :-- |
| `prepare_workspace` | ws, job | ジョブ用 worktree とブランチ `autospec/job-<id>` を作成。impl が無い/空ならプロファイルの `scaffold` で作成 | `state.workdir` |
| `requirements_extract` | spec | LLM（think=on）で要件一覧を `RequirementList` スキーマで抽出 | `requirements.json` |
| `approval` | target | `approval.required` を発行し `WAITING_APPROVAL` を返す。承認時に編集済み内容があれば対象を差し替え。却下＋コメント時は直前ステップをコメント付きで再実行（最大3回） | `state.approvals[target]` |
| `interface_design` | spec, requirements | LLM でインターフェース定義ファイルを生成→`gates.interface` があれば実行、失敗時は再生成（最大4回） | interface ファイル |
| `test_gen` | spec, interface, requirements | 旧 Phase1。生成 → §6.7 で抽出 → `gates.syntax` → 失敗時は §6.8 の要約を渡して再生成（`max_retries` 既定4、会話は §6.4 の通り圧縮）。テスト名に要件IDを入れさせ、`support_files` をコピーする。`options.reuse_tests=true` でテストが既にあれば生成を省略。`mode=modify` では既存テストを変更せず新規ファイルにのみ書く | テストファイル |
| `coverage_check` | requirements, test | テスト名を `test_name_pattern` で検索し、要件IDごとにテストがあるかを**機械的に**確認する。`must` の要件にテストが無ければ、不足しているIDを伝えて `test_gen` へ戻る（`jump_to`、最大2回） | `coverage.json` |
| `red_check` | test, interface | プロファイルの `red_check: true` のとき実行。LLMで**スタブ実装**を一時ファイルに生成し、`syntax` PASS かつ `full` FAIL を確認。`full` が PASS したらテストが何も検査していないとみなし、その旨を添えて `test_gen` をやり直す（最大2回） | `state.red_check` |
| `lock_tests` | test | テストファイルを読み取り専用にし、SHA-256 を記録 | `state.test_hashes` |
| `implement` | spec, interface, test | 旧 Phase2。Aider ループ（§9）。各ラウンド後にハッシュ確認、ラウンドごとのコミット、最良版の保持、テスト疑義の判定を行う | 実装ファイル, `state.gate_output`, `rounds.json` |
| `integrity_check` | test_hashes | テストファイルのハッシュが一致しなければジョブを FAILED にする | — |
| `regression` | baseline | `gates.project_test` を再実行し、`baseline_tests` で通っていたテストが新たに失敗していないか比較 | `regression.json` |
| `audit` | spec, requirements, impl, test, gate_output, coverage | 旧 Phase3。ゲート出力をプロンプトに含める。LLM には要件ごとの判定だけを出させ、**総合判定とスコアは機械的に計算**する（§8.6）。`implemented_in` / `verified_by` に挙げられた名前が実際にファイル内にあるか照合し、無ければ `UNVERIFIED` とする。Markdown の表ではパイプ記号と改行をエスケープする | `audit_result.json`, `audit_report.md` |
| `finalize` | — | worktree 上で変更をコミット（作成者 `AutoSpecBot`）、`git diff <base>...HEAD` を artifact に保存 | `diff.patch` |
| `repo_scan` | ws | `git ls-files --cached --others --exclude-standard` で対象列挙。バイナリ・サイズ上限超過（既定200KB）・`ignore` 指定を除外 | `state.files` |
| `repo_map` | files | ctags があれば関数・クラス一覧を抽出、無ければプロファイルの `signature_patterns` で抽出。ディレクトリツリーと合わせて1テキストにまとめる | `repo_map.txt` |
| `file_summaries` | files | 各ファイルを `FileSummary` スキーマで要約。**内容SHA-256＋モデル名＋プロンプト版**をキーにキャッシュ。コンテキスト予算超過ファイルは分割要約→統合 | `summaries.json` |
| `relevance_select` | request, repo_map, summaries | `RelevanceSelection` スキーマで関連ファイルを最大N件（既定8）選択 | `state.relevant_files` |
| `deep_read` | relevant_files | 予算内で本文を読み込み、要求に対する所見を抽出（think=on） | `state.findings` |
| `report` | findings | `AnalysisReport` スキーマで出力し、Markdown にも整形 | `analysis.json`, `analysis.md` |
| `change_plan` | request, findings | `ChangePlan` スキーマで変更計画（変更ファイル・新規テスト・リスク） | `plan.json` |
| `baseline_tests` | — | `gates.project_test` を実行し、結果を記録（失敗していても続行、記録のみ） | `baseline.json` |
| `test_review` | spec, test, 失敗内容 | §9 から呼ばれる。同じテストが連続して落ちるとき、テスト側が仕様とずれていないかを `TestVerdict` スキーマで判定する | `test_review_<n>.json` |

### 8.6 Structured Outputs スキーマ（`core/schemas.py`）

```python
class Requirement(BaseModel):
    req_id: str                 # "REQ-001"
    title: str
    description: str
    acceptance_criteria: list[str]
    priority: Literal["must", "should", "may"]

class RequirementList(BaseModel):
    requirements: list[Requirement]
    assumptions: list[str]      # 仕様の曖昧さに対して置いた前提
    open_questions: list[str]

class FileSummary(BaseModel):
    path: str
    purpose: str
    key_symbols: list[str]
    dependencies: list[str]

class RelevantFile(BaseModel):
    path: str
    reason: str
    score: int                  # 0-100

class RelevanceSelection(BaseModel):
    files: list[RelevantFile]

class Finding(BaseModel):
    title: str
    detail: str
    files: list[str]
    severity: Literal["info", "low", "medium", "high"]

class AnalysisReport(BaseModel):
    summary: str
    answer: str                 # 要求への直接回答
    relevant_files: list[RelevantFile]
    findings: list[Finding]
    open_questions: list[str]

class PlannedChange(BaseModel):
    path: str
    action: Literal["create", "modify", "delete"]
    description: str

class ChangePlan(BaseModel):
    goal: str
    changes: list[PlannedChange]
    new_tests: list[str]
    risks: list[str]

class RequirementAuditItem(BaseModel):
    req_id: str
    description: str
    status: Literal["PASS", "FAIL", "PARTIAL"]
    implemented_in: list[str]   # 関数名・モジュール名（実在確認に使う）
    verified_by: list[str]      # テスト名（実在確認に使う）
    note: str

class AuditLLMOutput(BaseModel):          # LLM に出させるのはここまで
    requirements: list[RequirementAuditItem]
    potential_risks: list[str]
    summary: str

class TestVerdict(BaseModel):
    test_name: str
    suspect: bool               # True = テスト側が仕様とずれている疑い
    req_id: str
    reason: str
```

最終レポート（`AuditReport`）は `AuditLLMOutput` に次を機械的に付け足して作る。LLM には総合判定もスコアも付けさせない。

- 名前の実在確認の結果（見つからない名前は `UNVERIFIED` と表示）
- `overall_status`：ゲート PASS、かつ `must` 要件がすべて PASS、かつ `UNVERIFIED` なし → `PASS`。それ以外は `WARN`
- `score`：PASS=1、PARTIAL=0.5、FAIL=0 の平均 × 100
- 網羅確認（`coverage.json`）の結果とゲート出力の要約

### 8.7 コンテキスト予算
- 予算 = `num_ctx`（既定65536）− 出力予約（既定8192）− システムプロンプト分。
- トークン数は `len(text) / 3` で概算する（日本語・コード混在を想定した保守的な値。設定で変更可）。
- 予算超過時の方針：要約 → 関連箇所のみ抜粋 → それでも超える場合はファイル単位で分割処理。**黙って切り詰めない**（切り詰めた場合は `log` イベントで警告する）。

## 9. Aider 連携

`implement` ステップは旧 Phase2 を移植し、以下を変更する。

```
aider
  --model ollama_chat/<model>
  --edit-format whole
  --yes-always
  --no-auto-commits
  --no-show-model-warnings
  --no-suggest-shell-commands        # 追加：提案シェルコマンドの自動実行を防ぐ
  --read <spec> --read <interface> --read <test...>
  --test-cmd "autospec gate --job <id> full"   # Sandbox 経由で実行するため自前CLIを挟む
  --auto-test
  --message "<prompt>"
  <editable files...>
```

- `cwd` は必ず `ctx.workdir`（worktree）。旧スクリプトのようにカレントディレクトリへ依存しない。
- 環境変数 `OLLAMA_API_BASE` を設定から渡す。
- 標準出力は1行ずつ `aider.output` イベントとして配信する。
- 1回の Aider 実行にタイムアウト（既定1800秒）を設ける。
- ラウンド数 `max_rounds`（既定5）はジョブオプションで上書き可能。Aider 内部の auto-test による修正試行も含めると LLM 呼び出しが多くなるため、ジョブ詳細画面にラウンド数を表示する。
- `mode=modify` のときは `ChangePlan.changes` の `create/modify` 対象を編集可能ファイル、既存テストと新規テストを `--read` に指定する。
- プロンプト末尾の `/no_think` は旧スクリプト同様に付ける（Aider は LLMClient を通らないため）。
- `autospec gate --job <id> <gate>`：ジョブの state からプロファイル・パスを解決し、Sandbox でゲートを実行して終了コードを返す CLI サブコマンド。
- **コマンドは引数リストで組み立てる**。文字列が必要な `--test-cmd` は `shlex.join` で作る（パスに空白があると壊れるため）。
- **aider の終了コードを確認する**。非ゼロで、出力にモデル未検出・接続エラー等が含まれる場合は、ラウンドを消費せずにジョブを FAILED（`error=infra_error`）にする。
- **ラウンドごとにコミットし、最良版を保持する**：各ラウンド後に full ゲートを実行して失敗件数（`failure_count_pattern`、取れなければ `error_patterns` の一致数）を記録し、job ブランチにコミットする。全ラウンドで通らなかった場合は、失敗件数が最も少なかったラウンドの状態に戻してから終了する。
- **テスト疑義のエスカレーション**：同じテスト名が `suspect_after_rounds`（既定3）ラウンド連続で失敗したら `test_review` を実行する。`suspect=true` なら承認ステップ（target=`test_dispute`）で人に判断を求める。選択肢は「該当テストだけロックを外して再生成し続行」「テストはそのままで続行」「中止」。CLI 実行時は中止して理由を表示する。
- 2ラウンド目以降のプロンプトに渡すエラーは §6.8 の要約を使う。

## 10. ワークスペースと git

- `WORKSPACES_ROOT`（既定 `~/autospec/workspaces`）配下の各ディレクトリがワークスペース。
- 作成方法：
  - `empty`：空ディレクトリ作成＋`git init`＋初期コミット
  - `import`：既存ローカルパスを**コピー**して取り込む（元は触らない）。git リポジトリならそのまま clone する
- ワークスペース名は `^[a-zA-Z0-9_-]{1,64}$` に制限。API で受け取る全ファイルパスは `resolve()` 後にワークスペース配下であることを検証する（パストラバーサル対策）。
- 書き込みのあるジョブ（implement / design / modify）は `DATA_DIR/worktrees/job-<id>/` に `git worktree add -b autospec/job-<id>` で作業する。
- `analyze` は worktree を作らず、HEAD 時点を読み取り専用で参照する。
- マージ：`POST /api/jobs/{id}/merge` でワークスペースの既定ブランチへ `--no-ff` マージ。コンフリクト時は中止してエラーを返す（自動解決はしない）。
- 破棄：`POST /api/jobs/{id}/discard` で worktree とブランチを削除。
- git の user.name / email はリポジトリローカル設定で `AutoSpecBot` / `bot@local.dev`（旧スクリプト踏襲）。

## 11. 永続化（SQLite）

`DATA_DIR/autospec.db`。標準ライブラリ `sqlite3` ＋薄いアクセス層で実装する（ORM 不要）。

```sql
CREATE TABLE workspaces (
  name TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL REFERENCES workspaces(name),
  pipeline TEXT NOT NULL,
  profile TEXT,
  inputs_json TEXT NOT NULL,
  options_json TEXT NOT NULL,
  state_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  current_step INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE steps (
  job_id TEXT NOT NULL REFERENCES jobs(id),
  idx INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT, ended_at TEXT,
  message TEXT,
  PRIMARY KEY (job_id, idx)
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  step TEXT,
  level TEXT NOT NULL,
  message TEXT,
  data_json TEXT
);
CREATE INDEX idx_events_job ON events(job_id, id);
CREATE TABLE approvals (
  job_id TEXT NOT NULL,
  target TEXT NOT NULL,
  decision TEXT,             -- approve / reject / NULL(待ち)
  comment TEXT,
  edited_content TEXT,
  requested_at TEXT NOT NULL,
  resolved_at TEXT,
  PRIMARY KEY (job_id, target)
);
```

## 12. ジョブキューとワーカー

- ワーカーは**1本**（GPU 1枚・Ollama 1インスタンス前提）。`threading.Thread` ＋ DB をキューとして使う（`status='queued'` を作成順に取得）。
- 状態遷移：

```
queued → running → succeeded
                 → failed
                 → cancelled
                 → waiting_approval → (approve/reject) → queued → running ...
running → interrupted（サーバー再起動時）→ (resume) → queued
running → waiting_device（実機の空き待ち。第2部）→ (空き) → queued
```

- `waiting_approval` 中はワーカーを解放し、次の queued ジョブを処理してよい。
- 承認・再開時は `current_step` から実行を再開する（`ctx.state` は DB から復元）。
- キャンセル：`CancelToken` をセットし、実行中の Sandbox / Aider プロセスグループを kill。ステップ境界と LLM 呼び出し前でも確認する。
- サーバー起動時、`running` のジョブは `interrupted` に変更する。

## 13. REST API / SSE

ベースパス `/api`。リクエスト・レスポンスは JSON（pydantic モデルで定義）。

| メソッド | パス | 内容 |
| :-- | :-- | :-- |
| GET | `/health` | Ollama 接続確認・モデル有無・aider/docker の有無を返す |
| POST | `/session` | `{token}` を受け取り、正しければ HttpOnly Cookie を発行 |
| GET | `/profiles` | プロファイル一覧 |
| GET | `/workspaces` | 一覧 |
| POST | `/workspaces` | `{name, mode: "empty"\|"import", source_path?}` |
| GET | `/workspaces/{ws}` | 詳細（既定ブランチ、最新コミット、workspace.yaml） |
| GET | `/workspaces/{ws}/tree?path=` | ディレクトリ一覧 |
| GET | `/workspaces/{ws}/file?path=` | ファイル内容（テキストのみ、上限1MB） |
| POST | `/jobs` | `{workspace, pipeline, profile?, inputs, options?}` → `{id}` |
| GET | `/jobs?workspace=&status=` | 一覧 |
| GET | `/jobs/{id}` | 詳細（steps 含む） |
| GET | `/jobs/{id}/events` | **SSE**。`Last-Event-ID` ヘッダまたは `?after=` で途中から再送 |
| POST | `/jobs/{id}/approve` | `{target, decision: "approve"\|"reject", comment?, edited_content?}` |
| POST | `/jobs/{id}/cancel` | キャンセル |
| POST | `/jobs/{id}/resume` | interrupted / failed からの再開（`from_step?` 指定可） |
| GET | `/jobs/{id}/diff` | unified diff テキスト |
| POST | `/jobs/{id}/merge` | 既定ブランチへマージ |
| POST | `/jobs/{id}/discard` | worktree とブランチを破棄 |
| GET | `/jobs/{id}/artifacts` | 一覧 |
| GET | `/jobs/{id}/artifacts/{name}` | 取得 |

- 仕様書の入力は `inputs.spec_text`（テキスト直接）または `inputs.spec_path`（ワークスペース内パス）のどちらか。
- SSE は `sse-starlette` を使用。15秒ごとに keep-alive コメントを送る。

## 14. Web UI

React + Vite + TypeScript。UI ライブラリは使わず、最小限の CSS で構成する。本番は `web/dist` を FastAPI から静的配信する。

**スマホ幅（375px）で全操作ができること**（特に承認・ログ確認・マージ）。

### 画面

1. **ログイン**：トークン入力（認証有効時のみ）
2. **ワークスペース一覧**：名前、最新ジョブの状態、新規作成ボタン
3. **ワークスペース詳細**：ファイルツリー（閲覧のみ）、ジョブ履歴、「新規ジョブ」ボタン
4. **ジョブ作成**
   - パイプライン選択（implement / design / analyze / modify）
   - 選択に応じて入力欄を切替（仕様テキスト、要求テキスト、パス指定、プロファイル）
   - 詳細オプション（max_rounds、think の有無など）は折りたたみ
5. **ジョブ詳細**
   - ステップ進捗（縦のステッパー、現在ステップ強調、Aider ラウンド数表示）
   - ライブログ（SSE、type フィルタ、自動スクロール切替）
   - 承認パネル（`waiting_approval` 時のみ表示）：対象内容をテキストエリアで表示・編集 → 承認 / コメント付き却下
   - 成果物タブ：監査レポート・解析レポートは Markdown 表示、JSON は整形表示
   - 差分タブ：`diff2html` で表示
   - 操作：キャンセル / 再開 / マージ / 破棄

## 15. セキュリティ

本アプリは**任意コード実行と同等の権限を持つ**ため、以下を必須とする。

- 既定のバインドは `127.0.0.1`。`0.0.0.0` にする場合は `auth.token` の設定を必須とし、未設定なら起動を拒否する。
- 認証：`Authorization: Bearer <token>` または `/session` で発行した Cookie。EventSource 用に Cookie 方式を用意する。
- 外部公開（Cloudflare Tunnel 等）時は前段に Cloudflare Access 等の認証を置くことを README に明記する。
- ゲートコマンドは Sandbox 経由で実行し、docker モードではネットワーク無効。
- Aider に `--no-suggest-shell-commands` を付ける。
- テストファイルのハッシュ検証（`lock_tests` / `integrity_check`）。
- パス検証（§10）、アップロードはテキストのみ・サイズ上限あり。

## 16. 設定

`config.yaml`（`AUTOSPEC_CONFIG` で場所指定可）。各キーは環境変数 `AUTOSPEC_<SECTION>_<KEY>` で上書き可能。

```yaml
ollama:
  url: "http://127.0.0.1:11434"
  model: "local-coder-64k"
  num_ctx: 65536
  temperature: 0.2
  timeout_sec: 600
paths:
  data_dir: "~/autospec/data"
  workspaces_root: "~/autospec/workspaces"
  harness_dir: "./harness"        # 起動時に絶対パス化
server:
  host: "127.0.0.1"
  port: 8765
auth:
  token: ""                       # 空なら認証なし（127.0.0.1 時のみ許可）
sandbox:
  mode: "local"                   # local | docker
  gate_timeout_sec: 300
aider:
  max_rounds: 5
  timeout_sec: 1800
  suspect_after_rounds: 3
pipeline:
  test_gen_max_retries: 4
  approval_max_rejects: 3
  analyze_max_relevant_files: 8
  max_file_bytes: 200000
  output_reserve_tokens: 8192
  chars_per_token: 3
  retry_temperature_step: 0.15
  retry_temperature_max: 0.7
  error_digest_chars: 2500
  max_jumps: 10
hw:                               # 第2部
  targets_dir: "./targets"
  default_mode: mock              # mock | real（real はジョブ作成時に明示したときのみ）
  llm_tool_mode: native           # native（Ollama の tools）| json（Structured Outputs で代用）
  max_llm_tool_calls: 10
  max_hw_rounds: 3                # 実機NG → 実装修正 の最大往復
  lock_wait_timeout_sec: 1800
  agents: {}                      # 例: { lab-pc: { url: "http://192.168.0.20:8770", token: "..." } }
```

## 17. 旧スクリプトからの移行対応表

| 旧スクリプト | 移行先 |
| :-- | :-- |
| `RequirementAuditItem`, `AuditReport` | `core/schemas.py` |
| `ensure_git_repo` | `core/workspace.py`（ワークスペース単位で実行） |
| `set_readonly` | `steps/testgen.py` の `LockTests` |
| `strip_thinking`, `extract_code_block` | `core/textutil.py` |
| `call_ollama_chat` | `core/llm.py` の `OllamaClient` |
| `lang_test_instructions` | `profiles/*.yaml` の `test_instructions` |
| `run_phase1_test_generation` | `steps/testgen.py` の `TestGen` |
| 実装ファイルの雛形作成（Phase2冒頭） | `steps/prepare.py` ＋ `profiles/*.yaml` の `scaffold` |
| `run_phase2_aider_implementation` | `steps/implement.py` の `Implement` |
| `run_phase3_spec_audit` | `steps/audit.py` の `Audit` |
| `main` / argparse | `cli.py`（`autospec run implement --lang c --spec ... ` で旧と同じ引数を受け付ける） |
| `print` 出力 | `EventEmitter` |

## 18. 旧スクリプトの不具合と改善点（M1 で対応）

旧スクリプトのレビュー結果。番号は §19 の受入基準から参照する。

### 優先度高（実際に壊れる・止まる）

| # | 内容 | 対応箇所 |
| :-- | :-- | :-- |
| B1 | コードブロック抽出が別のブロック・フェンス記号・説明文を拾う（再現確認済み） | §6.7 |
| B2 | ゲート・aider にタイムアウトが無く、無限ループで全体が止まる | §6.6, §7.3 `timeouts`, §9 |
| B3 | エラーログを末尾だけ渡しており、最初のエラーが切れる | §6.8 |
| B4 | web の雛形が `# TODO` で TypeScript として不正 | §7.4 |
| B5 | aider の終了コードを見ておらず、即座に落ちてもラウンドを空回りする | §9 |
| B6 | `reports/` を作成しておらず、初回の Phase3 で `FileNotFoundError` | §6.2 ArtifactStore |
| B7 | Phase3 の `gate_output` 引数が未使用 | §8.5 `audit` |
| B8 | `verify_gate.sh` の場所と git 初期化がカレントディレクトリ依存 | §10, §16 `harness_dir` |
| B9 | aider が提案したシェルコマンドを `--yes-always` で実行し得る | §9 `--no-suggest-shell-commands` |

### 優先度中（ループの効率・判定の精度）

| # | 内容 | 対応箇所 |
| :-- | :-- | :-- |
| I1 | C のテストが `assert.h` で、最初の失敗で止まる／`-DNDEBUG` で素通りする | §7.3 `autospec_test.h` |
| I2 | テストと要件の対応が取れない | テスト名に要件ID、§8.5 `coverage_check` |
| I3 | 総合判定とスコアを LLM が決めており、FAIL があっても PASS になり得る | §8.6 |
| I4 | 監査で挙げた関数名・テスト名が実在するか確認していない | §8.5 `audit` |
| I5 | リトライで会話が積み上がり、仕様書が切り捨てられ得る | §6.4 |
| I6 | リトライでも温度が固定で、同じ誤りを繰り返しやすい | §6.4 |
| I7 | ラウンドで悪化しても元に戻せない | §9 最良版の保持 |
| I8 | テスト側の誤りを疑う仕組みが無い | §9 テスト疑義、§8.5 `test_review` |
| I9 | テストが構文チェックのみで、何も検査しないテストを検出できない | §8.5 `red_check` |
| I10 | テストの保護が chmod のみ | §8.5 `lock_tests` / `integrity_check` |

### 優先度低

| # | 内容 | 対応 |
| :-- | :-- | :-- |
| L1 | コマンドを文字列連結しており、パスに空白があると壊れる | §6.6, §9 |
| L2 | C の指示に `pid_init` が直書きされている | §7.3 |
| L3 | Markdown の表でパイプ記号・改行をエスケープしていない | §8.5 `audit` |
| L4 | 失敗時に終了コードを返さず、ログもファイルに残らない | CLI は失敗時に非ゼロ終了。イベントを `DATA_DIR/jobs/<id>/events.log` にも書く |
| L5 | 既存のテストを再利用できない | `options.reuse_tests` |
| L6 | 出力先ディレクトリが無いと書き込みに失敗する | 書き込み前に `mkdir(parents=True)` |

## 19. マイルストーンと受入基準

### M1：コア分解と旧スクリプトの改善（CLI互換）
- §5 の構成で core / steps / profiles / pipelines を実装し、`implement` パイプラインを CLI から実行できる。
- 5つのプロファイル（c / cpp / python / web / verilog）を作成する。`verify_gate.sh` から移植し、難しいものは暫定的にスクリプト呼び出しのままでよい。
- §18 の B1〜B9、I1〜I10、L1〜L6 をすべて対応する。
- §6.9 の拡張ポイントを用意する。
- **受入基準**
  - `autospec run implement --lang c --spec ... --interface ... --impl ... --test ...` が、旧スクリプトで成功した C のサンプルで同等以上に成功する（ユーザー環境の Qwen3.8 で確認）。
  - `pytest` が Ollama なしで全件通る（FakeLLM 使用）。少なくとも次のシナリオを含む。
    - §6.7 の5ケース（コード抽出）
    - 無限ループするテストがタイムアウトで打ち切られ、エラー要約に `[TIMEOUT]` が入る
    - gcc のエラー出力で、最初のエラーが要約に必ず含まれる
    - 要件IDのテストが欠けていると `coverage_check` が `test_gen` へ戻す
    - 常に通るテストを `red_check` が検出する
    - ラウンド2が最良でラウンド3で悪化した場合、ラウンド2の状態で終わる
    - 偽の aider が異常終了したとき、ラウンドを消費せず `infra_error` になる
    - LLM の監査が全要件 PASS と答えても、存在しない関数名を挙げていれば `WARN` になる
  - 成果物（要件、網羅確認、監査 JSON/MD、LLM ログ、ラウンド履歴）がジョブディレクトリに保存される。

### M2：サーバー・ワーカー・最小UI
- SQLite、ジョブキュー、ワーカー、REST API（ワークスペース・ジョブ作成・取得・キャンセル）、SSE。
- Web UI：ワークスペース一覧、ジョブ作成（implement のみ）、ジョブ詳細（ステップ進捗＋ライブログ）。
- **受入基準**
  - ブラウザから implement ジョブを作成し、ログがリアルタイムに流れる。
  - ブラウザを再読み込みしてもログが途中から再表示される。
  - キャンセルで Aider プロセスまで停止する。
  - サーバー再起動後、実行中だったジョブが interrupted になり、resume で続行できる。

### M3：ワークスペース分離・差分・サンドボックス
- worktree 作成、finalize でのコミット、diff 表示、マージ／破棄。
- DockerSandbox と `autospec gate` サブコマンド。
- 認証（トークン＋Cookie）、パス検証。
- **受入基準**
  - ジョブの変更がワークスペース本体に影響せず、マージ操作で初めて反映される。
  - `sandbox.mode: docker` で C サンプルが通る。
  - `0.0.0.0` かつトークン未設定では起動しない。

### M4：analyze パイプライン
- repo_scan / repo_map / file_summaries（キャッシュ付き）/ relevance_select / deep_read / report。
- UI：analyze のジョブ作成、解析レポート表示。
- **受入基準**
  - `tests/fixtures` の小規模リポジトリ（20ファイル程度）に対して、要求に関連するファイルを選び、レポートを出力する。
  - 2回目の実行で、変更のないファイルの要約がキャッシュから読まれる（ログで確認できる）。
  - ワークスペースに一切書き込みがない（git status が変化しない）。

### M5：design パイプライン
- requirements_extract / interface_design / approval（編集・却下再実行あり）/ red_check。
- UI：承認パネル。
- **受入基準**
  - 仕様テキストのみから C モジュールを生成し、要件承認とインターフェース承認を経てゲートを通過する。
  - 承認時に編集したインターフェースが後続ステップで使われる。
  - 空振りテスト（常に通るテスト）を意図的に返す FakeLLM シナリオで、red_check が検出し再生成する。

### M6：modify パイプライン
- change_plan / baseline_tests / regression、複数ファイル編集の Aider 実行。
- **受入基準**
  - fixtures のリポジトリに対して小さな機能追加要求を実行し、既存テストを壊さずに新規テストを通過する。
  - 既存テストを壊す変更をした場合、regression で失敗として検出される。

### M7：実機ターゲット機構の基礎（第2部 H2〜H4, H8, H9, H11）
- `target.yaml` のロードと検証、アクション実行（local）、結果 JSON の契約、mock 実行、監査ログ、`safe_state`、非常停止、実機リソースのロック。
- 模擬ターゲット `targets/mock-mcu` と `targets/mock-faulty`（H3.4）。
- API と UI：ターゲット一覧・詳細・手動実行・実行履歴・非常停止ボタン。
- **受入基準**（すべて mock で確認できること）
  - `autospec target validate mock-mcu` が通り、不正な `target.yaml`（未定義の `safe_state`、mock の無いアクション、範囲指定の無い `actuate` 引数など）はエラーになる。
  - UI からアクションを手動実行でき、引数が JSON Schema の範囲外なら実行前に拒否される。
  - アクション実行中にジョブをキャンセルすると、プロセスが止まり、`safe_state` が実行され、ロックが解放される。
  - `hazardous` のアクションは確認なしでは実行できない。

### M8：実機検証パイプライン（第2部 H5〜H7）
- `hw_verify` パイプライン、HWテスト計画の決定的な判定、`implement` / `design` / `modify` への `hw_target` の後付け、`hw_diagnose` → `hw_route` による実装への差し戻し、LLM からの読み取り系ツール呼び出し。
- **受入基準**（mock で確認）
  - mock-mcu 向けに不具合を仕込んだ C コードで、HWテスト失敗 → `hw_diagnose` が `code` と分類 → 実装修正 → 再書き込み → PASS まで自動で進む。
  - アクションが終了コード3（機器に到達できない）を返したら、コード修正に進まず停止し、人に知らせる。
  - LLM が `llm_tools` 以外のアクションや範囲外の引数を要求しても実行されず、拒否が監査ログに残る。

### M9：リモートエージェントと実機1台目（第2部 H4.4, H10）
- `agent/`（Windows でも動く Python 実装）と `RemoteTargetRunner`。
- AI によるアクションスクリプト雛形の生成（`enabled: false` で作成 → 人が確認して有効化）。
- **受入基準**
  - Claude Code 環境では、ローカルに立てたエージェント＋mock-mcu で M8 と同じ流れが通る。
  - ユーザー環境の実機1台で、ビルド → 書き込み → HWテスト → レポートまでが自動で通る（ユーザーが確認）。

## 20. システム自体のテスト方針

- `FakeLLMClient`：`[(期待するステップ名, 応答文字列 or pydanticモデル), ...]` のキューを持ち、順に返す。想定外の呼び出しはテスト失敗にする。
- Aider は `AIDER_BIN` 設定で差し替え可能にし、テストでは「指定ファイルに固定内容を書くだけのスクリプト」を使う。
- 単体テスト：textutil、profiles のテンプレート展開、workspace（一時ディレクトリで git 操作）、パス検証、runner の再開・キャンセル。
- 結合テスト：FakeLLM ＋ 偽 Aider ＋ 実 gcc で `implement` パイプラインを一周。
- 実LLMテスト（手動、`-m ollama` マーカー）：実 Ollama で C サンプルを一周。
- 実機関連は `targets/mock-mcu` と、終了コード・遅延・異常値を注入できる `targets/mock-faulty` でテストする。実機を前提にしたテストには `-m hardware` マーカーを付け、既定では実行しない。

## 21. 未決事項

実装中に判明したことはここに追記すること。

- `harness/verify_gate.sh` の各言語の実際の処理内容（M1 着手時に確認）
- web / verilog プロファイルで使うテストランナー・シミュレータ（例：tsx + node:test、iverilog / verilator）の確定
- Docker イメージを言語ごとに用意するか、共通イメージにするか
- design パイプラインでの出力ファイル名・配置規約の既定値
- 承認待ちになったときの通知手段（将来：スマホへのプッシュ通知など）
- 「C系」の範囲：v0.2 では C と C++（`cpp`）を含めた。組込み向けクロスコンパイルを別プロファイルにするか、ターゲット側の `artifact.build` で吸収するか（第2部では後者を想定）
- 最初に対象とする実機（MCU ボード、FPGA ボード、計測器を制御する PC など）と接続方法（シリアル、Telnet、SSH、JTAG、VISA など）
- 実機が GPU マシンと同じ PC につながるか、別の PC（Vivado や計測器ドライバが入った Windows 機など）か
- `hazardous` に分類する操作の具体的な一覧（ユーザーが決める）

### M1 実装時の追記（2026-11-13）

- **旧スクリプトと `harness/verify_gate.sh` が開発環境（Mac）に無い。** M1 は本書 §7.4 の仮コマンドでプロファイルを作成した。Ubuntu 側の2ファイルを `legacy/` `harness/` に置いたら、ゲートの中身を比較してプロファイルへ移植する（§0 の「移植前にユーザーへ確認」はこの時点で行う）。
- **テスト名の規約の食い違い。** §7.3 の `test_name_pattern` 例は `test_{req_id_compact}_\w+`、`autospec_test.h` の例は `TEST_BEGIN("REQ001_xxx")`。M1 は前者に統一し、C/C++ は `static void test_REQ001_xxx(void)` ＋ `TEST_BEGIN("test_REQ001_xxx")` とした。
- **プロファイルにキーを追加した。** `failed_test_pattern`（失敗したテスト名の抽出。§9 のテスト疑義に必要）、`interface_instructions`（design 用・M5）、`infra_error_patterns`（予約）。ゲートのプレースホルダに `{build_dir}`（ジョブごとのビルド出力先。旧 `/tmp/t` の衝突回避）、`{interface_dir}`（`-I` 用）、`{python}`（AutoSpec の venv の Python）を追加した。
- **CLI の作業場所。** `autospec run` の既定は旧スクリプト互換の「その場で作業」（inplace：入力ファイルと各ラウンドを現在のブランチにコミット）。`--worktree` で `DATA_DIR/worktrees/job-<id>` に分離する。Web（M2〜）は worktree を既定にする想定。
- **inplace では git の作者を `-c user.name=AutoSpecBot` で毎回指定**し、ユーザーのリポジトリ設定は書き換えない（AutoSpec が作るワークスペースだけ §10 どおりローカル設定する）。
- **ゲート未通過でも implement ステップは DONE** にして監査まで進め、結果を `gate_passed=false` と監査 WARN で示す。CLI の終了コードは 0=ゲート PASS かつ監査 PASS、2=ゲート PASS かつ監査 WARN、1=失敗、4=承認待ち。
- **最良版の同点は後のラウンドを採用。** 比較は「(コンパイル・実行できたか, 失敗件数)」の順で、コンパイルできない版は失敗件数にかかわらずテストが走った版より悪いとみなす。
- **DockerSandbox は `-v <path>:<path>` で同じパスにマウント**する（§6.6 の `/work` ではない）。ゲートコマンドが `{build_dir}` や `{harness}` の絶対パスを含むため。
- **Ollama の `think` フィールドを受け付けないモデル**（qwen2.5-coder など）では 400 が返る。クライアントは自動で `think` を外して再送する（`ollama.send_think`）。Qwen3.8 で `/no_think` と `think` がどう効くかはユーザー環境で確認する。
- **aider に追加したオプション**：`--no-check-update --no-show-release-notes --analytics-disable --no-gitignore --no-pretty --no-fancy-input` と、履歴ファイルをジョブディレクトリへ置く指定。対話プロンプトと作業ツリーへの書き込みを防ぐため。
- **テスト疑義（test_dispute）の Web 承認と「該当テストだけロック解除して再生成」**は M2/M5（承認 UI）で実装する。M1 の CLI では §9 どおり理由を表示して中止する。

---

# 第2部 実機検証（HIL）拡張

## H1. 目的と基本方針

ビルド → 書き込み → 実機テスト → 合否判定 → 修正のループを自動で回す。実機ごとの操作は「ターゲット」としてスクリプトを**後から追加**していく。

1. **合否は LLM に判定させない。** 期待値・許容範囲は HWテスト計画に書き、決定的に判定する。
2. **LLM が触れるのは許可リストのアクションだけ。** 任意のシェルコマンドは実行させない。引数は JSON Schema で検証する。
3. **物理的に動かす操作ほど人の承認を強くする**（H8）。
4. **失敗・中断時は必ず安全状態に戻す**（`safe_state`）。
5. **すべてのターゲットに mock を用意する。** Claude Code 環境や CI では、実機なしで全フローを試せるようにする。
6. ソフトウェアの安全機構は、ハードウェアのインターロックの代わりにはならない。危険を伴う機器はハードウェア側で保護されている前提とし、README にも明記する。

## H2. 用語

| 用語 | 意味 |
| :-- | :-- |
| ターゲット | 実機1台、または実機＋治具・計測器の組。`targets/<name>/` に定義する |
| アクション | ターゲットへの1操作（書き込み、リセット、シリアル読み取り、測定など）。実体はスクリプト |
| リソース | 排他制御の単位（シリアルポート、計測器、JTAG など） |
| HWテスト計画 | アクションの実行順と期待値を書いた YAML |
| 安全状態 | 失敗・中断時に戻す状態。`safe_state` に指定したアクションで実現する |
| ランナー | アクションを実行する仕組み。ローカル実行、リモートエージェント経由、mock がある |

プロファイル（言語・ホストでの単体テスト）とターゲット（実機）は別物として扱う。同じ C コードでもボードによってビルド方法が違うため、実機向けのビルドはターゲット側（`artifact.build`）に書く。

## H3. ターゲット定義

### H3.1 ディレクトリ

```
targets/
  <name>/
    target.yaml        # 定義（必須）
    actions/           # アクションスクリプト（言語は自由）
    mock/              # 模擬実装（必須）
    checks.py          # 任意：独自の判定関数（H5.2）
    README.md          # 配線・前提条件・注意事項
```

ワークスペース固有のターゲットは `<workspace>/.autospec/targets/` にも置ける（同名ならワークスペース側を優先）。

### H3.2 `target.yaml`

```yaml
name: board-a
description: "評価ボードA（UART1 にログ出力、DMM で VOUT を測定）"
kind: mcu                     # mcu | fpga | instrument-host | linux-board | web-device
host: local                   # local | agent:<agent名>（H10）
compatible_profiles: [c, cpp]
artifact:                     # 実機に載せる成果物と、そのビルド方法
  type: elf
  build: "make -C {workdir} BOARD=board-a all"
  path: "build/app.elf"
  timeout_sec: 300
resources: ["uart:/dev/ttyUSB0", "dmm:lab-dmm-1"]   # 排他ロックの単位
safe_state: power_off         # 失敗・キャンセル・非常停止時に必ず実行するアクション
actions:
  - name: flash
    description: "成果物を書き込んでリセットする"
    level: write              # read | write | actuate | hazardous（H8）
    command: "bash actions/flash.sh"
    args: { type: object, properties: {} }
    timeout_sec: 120
    mock: "python mock/mock.py flash"
  - name: read_serial
    description: "UART 出力を指定秒数読み取る"
    level: read
    command: "python actions/read_serial.py"
    args:
      type: object
      properties:
        seconds: { type: integer, minimum: 1, maximum: 60 }
      required: [seconds]
    timeout_sec: 90
    mock: "python mock/mock.py read_serial"
  - name: power_off
    description: "電源を切る"
    level: actuate
    command: "python actions/power.py off"
    args: { type: object, properties: {} }
    timeout_sec: 30
    mock: "python mock/mock.py power_off"
llm_tools: [read_serial]      # LLM が診断中に自分で呼べるアクション（H7）
llm_allow_write: false
limits:
  max_actions_per_job: 200
  max_write_per_job: 20
  max_occupancy_sec: 3600
```

### H3.3 検証規則（`autospec target validate`）
- `safe_state` が存在するアクションを指していること。
- すべてのアクションに `mock` があること。
- `args` が正しい JSON Schema であること。`actuate` / `hazardous` の数値引数には `minimum` / `maximum` が必須。
- `llm_tools` に入れられるのは `read`（`llm_allow_write: true` のときは `write` も）だけ。`actuate` / `hazardous` は不可。
- `enabled: false` のアクションは実行できない（H4.4）。

### H3.4 模擬ターゲット
- **`mock-mcu`**：ホスト向けにビルドした実行ファイルを「ファームウェア」として扱う。
  - `flash`：成果物を mock の状態ディレクトリにコピーする
  - `read_serial`：そのファイルを実行し、標準出力をシリアル出力として返す
  - `measure_vout`：プログラムが出力した `VOUT=3.30` のような行から値を返す
  - `power_off`：状態ファイルを更新する
  - C のコードを書き換えると実機テストの結果も変わる、という流れを実機なしで再現できる。
- **`mock-faulty`**：設定に応じて、終了コード3（到達不能）、タイムアウト、異常値、壊れた結果 JSON を返す。

### H3.5 実行モード
- `mock` / `real` の2つ。既定は `hw.default_mode: mock`。
- `real` はジョブ作成時に明示したときだけ使う（UI ではターゲット名を確認入力させる）。

## H4. アクションスクリプトの契約

アクションは言語を問わない。次の契約さえ守れば、後から自由に追加できる。

### H4.1 呼び出し
- カレントディレクトリはターゲットのディレクトリ。
- 引数は JSON を**標準入力**で渡す。環境変数 `AUTOSPEC_ARGS` にも同じ JSON を入れる。
- 環境変数：
  - `AUTOSPEC_ARTIFACT`：成果物の絶対パス
  - `AUTOSPEC_OUT_DIR`：ログ・波形・CSV などを置くディレクトリ
  - `AUTOSPEC_JOB_ID`
  - `AUTOSPEC_MODE`：`mock` / `real`

### H4.2 結果
標準出力の**最終行**、または `$AUTOSPEC_OUT_DIR/result.json` に次の JSON を出す。

```json
{
  "ok": true,
  "summary": "VOUT=3.29V",
  "measurements": { "vout": { "value": 3.29, "unit": "V" } },
  "data": { "text": "FW v1.2 boot ok" },
  "files": ["uart.log"]
}
```

| 終了コード | 意味 | 扱い |
| :-- | :-- | :-- |
| 0 | 成功 | — |
| 1 | 実行できたが機器の応答が異常 | コード起因の可能性あり（診断へ） |
| 2 | 引数不正 | アクション側の不具合として失敗 |
| 3 | 機器に到達できない・環境の問題 | **コード起因ではない**。修正ループに入れず停止して人に知らせる |
| 124 | タイムアウト（ランナーが付与） | 失敗。`safe_state` を実行する |

結果 JSON が無い・壊れている場合は、終了コードにかかわらず失敗とする。

### H4.3 ヘルパーと CLI
- `autospec/hw/sdk.py`：標準ライブラリだけで書いた単体ファイル。`read_args()`, `emit_result(...)`, `fail(code, message)` を提供する。外部 PC のエージェントにもそのままコピーできるよう、依存を持たない。
- CLI
  - `autospec target new <name> --kind mcu|fpga|instrument-host|linux-board|web-device`：雛形（target.yaml・mock・README）を生成
  - `autospec target validate <name>`
  - `autospec target run <name> <action> --args '{...}' [--mode mock|real]`
  - `autospec target estop <name>`

### H4.4 AI によるアクション作成支援
- `target_scaffold` パイプライン：機器のマニュアル、既存の制御スクリプト（Telnet／シリアル制御ツールなど）、「やりたい操作」を入力すると、LLM がアクションスクリプト・`args` スキーマ・mock を生成する。
- 生成したアクションは **`enabled: false`** で追加する。人が UI でコードと `level` を確認し、mock での試行を通してから有効化する。
- `level` の初期値は LLM の提案に関係なく `actuate` とし、人が必要に応じて下げる（安全側に倒す）。

## H5. HWテスト計画

### H5.1 形式（`<workspace>/.autospec/hw_tests/<target>.yaml`）

```yaml
target: board-a
setup:                          # テスト前に1回実行
  - action: flash
teardown:                       # 成功時の後始末（失敗時は safe_state）
  - action: power_off
tests:
  - id: HW-001
    req: REQ-003
    title: "起動ログにバージョンが出る"
    steps:
      - action: read_serial
        args: { seconds: 5 }
        expect:
          - { type: regex, field: data.text, pattern: "FW v\\d+\\.\\d+" }
  - id: HW-002
    req: REQ-005
    title: "出力電圧が範囲内"
    repeat: 3                   # 3回すべて合格で PASS
    retry_on_infra: 1           # 終了コード3のときだけ1回再試行
    steps:
      - action: measure_vout
        expect:
          - { type: range, field: measurements.vout.value, min: 3.2, max: 3.4 }
```

### H5.2 判定（`autospec/hw/evaluator.py`、LLM は使わない）
- `expect` の種類：`equals`, `range`（両端を含む）, `regex`, `contains`, `not_contains`, `custom`（ターゲットの `checks.py` の関数名。`(bool, message)` を返す）。
- `field` はドット区切りで結果 JSON をたどる。
- テストごとの結果は `PASS` / `FAIL` / `ERROR`（アクション自体の失敗）/ `INFRA`（終了コード3）。測定値・期待値・証拠ファイルを記録する。

### H5.3 計画の作成と承認
- 既存の計画があればそれを使う。
- 無ければ LLM が仕様・要件・`target.yaml` のアクション一覧から `HwTestPlan` スキーマ（YAML と同じ構造の pydantic モデル）で下書きする。実機を動かすため、**必ず人の承認を経る**。承認した計画はワークスペースに保存し（job ブランチにコミット）、以後は再利用する。
- 承認済み計画の中にある `actuate` は自動で実行してよい。`hazardous` は計画の中にあっても、実行のたびに承認を求める。

## H6. パイプラインへの組み込み

### H6.1 ステップ

| ステップ | 処理 |
| :-- | :-- |
| `hw_plan` | 計画を読み込む。無ければ下書きを生成し、次の承認ステップへ |
| `hw_build` | `artifact.build` を Sandbox で実行（クロスコンパイル、Vivado バッチなど）。失敗はコード起因として §6.8 の要約を残す |
| `hw_acquire` | ターゲットのリソースをまとめてロックする。取れなければ `waiting_device` で待つ |
| `hw_deploy` | 計画の `setup` を実行する |
| `hw_test` | 計画の各テストを実行し、H5.2 で判定する |
| `hw_diagnose` | 失敗時のみ。ロックを保持したまま、LLM が読み取り系ツール（H7）で情報を集め、`HwDiagnosis` を出す。終了コード3があれば LLM を使わず `infra` とする |
| `hw_release` | `always_run`。成功時は `teardown`、失敗・キャンセル時は `safe_state` を実行し、ロックを解放する |
| `hw_route` | 診断結果で分岐する（H6.3） |
| `hw_report` | `hw_report.json` / `hw_report.md` を出力し、要件ごとの実機結果を監査レポートに統合する |

### H6.2 `hw_target` 指定時の並び

```
... Audit → HwPlan → Approval(hw_plan, only_if_new) → HwBuild → HwAcquire → HwDeploy
    → HwTest → HwDiagnose → HwRelease → HwRoute → HwReport → Finalize
```

### H6.3 `hw_route` の分岐

```python
class HwDiagnosis(BaseModel):
    category: Literal["code", "test_plan", "hardware", "infra", "unknown"]
    failed_tests: list[str]
    evidence: list[str]        # 根拠（ログの行、測定値など）
    hypothesis: str
    proposed_fix: str          # category=code のとき、実装への指示
```

| category | 動作 |
| :-- | :-- |
| `code` | `hw_round < max_hw_rounds`（既定3）なら `jump_to="implement"`。実機の失敗内容と診断を aider へのエラーとして渡す。ホストでの単体テストも引き続き通す必要がある |
| `test_plan` | 承認ステップ（target=`hw_plan_dispute`）で人に判断を求める |
| `hardware` / `infra` | 停止して人に知らせる（コードは触らない） |
| `unknown` | 承認ステップで人に判断を求める |

## H7. LLM からのツール呼び出し

- `hw_diagnose` の間だけ使える。対象は `llm_tools` に列挙したアクションのみ（既定は `read` だけ）。
- `hw.llm_tool_mode`
  - `native`：Ollama の `/api/chat` の `tools` に、アクションの `args` スキーマから作った定義を渡す。
  - `json`：Structured Outputs で `{action, args, done}` を出させる代替方式。
  - Qwen3.8 のツール呼び出しがどの程度安定するかはユーザー環境で確認し、設定で切り替える。
- 呼び出しのたびに「許可リスト確認 → スキーマ検証 → 回数上限（`max_llm_tool_calls`）→ 実行 → 監査ログ」を行う。結果は要約して LLM に返す（ファイルは名前だけ、テキストは文字数上限付き）。
- 拒否した要求も監査ログに残し、LLM には拒否理由を返す。

## H8. 安全設計

| level | 例 | パイプラインからの実行 | LLM からの直接実行 |
| :-- | :-- | :-- | :-- |
| `read` | シリアル読み取り、レジスタ読み出し、測定 | 自動 | 可（`llm_tools` に入れた場合） |
| `write` | 書き込み、リセット、設定変更 | 自動（回数上限あり） | `llm_allow_write: true` のときのみ |
| `actuate` | 電源操作、リレー、モーター駆動 | 承認済み HWテスト計画の中でのみ自動 | 不可 |
| `hazardous` | 高電圧、レーザー出力、可動部など人や機器に危険が及び得るもの | **実行のたびに人の承認** | 不可 |

- **`safe_state`**：失敗・キャンセル・タイムアウト・サーバー停止（SIGTERM / atexit）・非常停止のときに実行する。ロックの保持者でなくても実行でき、何度実行しても同じ結果になる（冪等）こと。`safe_state` 自体が失敗したら最上位の警告イベントを出し、ターゲットを `fault` 状態にして以降の自動実行を止める（人が UI で解除する）。
- **非常停止**：API・UI・CLI から実行できる。実行中のアクションを止め、`safe_state` を実行し、ターゲットを `fault` にする。UI ではターゲット使用中、どの画面にも非常停止ボタンを表示する。
- **上限**：ジョブあたりのアクション回数、`write` 回数、実機の占有時間。
- **監査ログ**：すべてのアクション実行（起動元 `plan` / `llm` / `human` / `estop`、引数、結果、所要時間、拒否理由）を削除不可で保存する。
- `real` モードは明示指定したときのみ（H3.5）。

## H9. 排他制御とスケジューリング

- 実機リソースは LLM（GPU）とは別のリソースとしてロックする（§6.9 の抽象を使う）。ターゲットのリソースは名前順にまとめて取得し、デッドロックを避ける。
- 取得できないときはジョブを `waiting_device` にしてワーカーを解放し、ロックが空いたら再キューする。
- UI からの手動実行もロックを取る。
- サーバー起動時に残っているロックは、`safe_state` を実行してから解放する。
- 将来、実機ステップ専用のワーカーを分けて、LLM ジョブと並行できるようにする（v0.2 では単一ワーカー）。

## H10. リモート実行（Target Agent）

- 実機が別の PC（例：Vivado や計測器ドライバが入った Windows 機）につながっている場合に使う。
- `agent/`：Python で実装し、Windows / Linux の両方で動くこと。アクションは `.py` / `.bat` / `.ps1` / `.sh` を許可する。
- API

| メソッド | パス | 内容 |
| :-- | :-- | :-- |
| GET | `/health` | 稼働確認 |
| GET | `/targets` | エージェント上のターゲット一覧 |
| POST | `/targets/{t}/actions/{a}` | アクション実行（引数 JSON ＋ 成果物ファイルを multipart で送る） |
| POST | `/targets/{t}/safe_state` | 安全状態へ |
| GET | `/runs/{id}/files/{name}` | 証拠ファイルの取得 |

- トークン認証は必須。LAN 内での利用を基本とし、外部から使う場合は Cloudflare Access 等を前段に置く。
- エージェント側でも引数スキーマと `level` の制限を検証する（サーバーを信用しきらない二重チェック）。
- サーバーからのハートビートが途絶えたら、エージェントが自分で `safe_state` を実行する機能を持つ（設定で有効化）。
- `target.yaml` の `host: agent:<名前>` で切り替え、接続先は設定の `hw.agents` に書く。

## H11. DB・API・UI の追加

### DB

```sql
CREATE TABLE targets (
  name TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'idle',   -- idle / busy / fault
  definition_hash TEXT,
  updated_at TEXT
);
CREATE TABLE device_locks (
  resource TEXT PRIMARY KEY,
  job_id TEXT,
  holder TEXT,                          -- job / manual
  acquired_at TEXT
);
CREATE TABLE hw_invocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target TEXT, action TEXT, level TEXT,
  origin TEXT,                          -- plan / llm / human / estop
  job_id TEXT, mode TEXT,
  args_json TEXT, exit_code INTEGER, result_json TEXT,
  started_at TEXT, ended_at TEXT,
  rejected_reason TEXT
);
CREATE TABLE hw_test_results (
  job_id TEXT, hw_round INTEGER, test_id TEXT, req_id TEXT,
  status TEXT, detail_json TEXT,
  PRIMARY KEY (job_id, hw_round, test_id)
);
```

### API

| メソッド | パス | 内容 |
| :-- | :-- | :-- |
| GET | `/targets` | 一覧（状態・ロック保持者） |
| POST | `/targets/reload` | `targets/` の再読み込み |
| GET | `/targets/{t}` | 定義・アクション一覧 |
| POST | `/targets/{t}/validate` | 検証 |
| POST | `/targets/{t}/actions/{a}/run` | 手動実行 `{args, mode, confirm?}`。`hazardous` は `confirm` 必須 |
| POST | `/targets/{t}/actions/{a}/enable` | AI 生成アクションの有効化 |
| POST | `/targets/{t}/estop` | 非常停止 |
| POST | `/targets/{t}/clear_fault` | `fault` 状態の解除 |
| GET | `/targets/{t}/invocations` | 実行履歴（監査ログ） |
| GET | `/jobs/{id}/hw_results` | HWテスト結果 |

### イベント
`hw.lock`, `hw.action.start`, `hw.action.end`, `hw.test.result`, `hw.safe_state`, `hw.fault`

### UI
- **ターゲット一覧**：状態（idle / busy / fault）、ロック中のジョブ、最終実行。
- **ターゲット詳細**：アクション一覧（level で色分け）、`args` の JSON Schema から自動生成した手動実行フォーム、実行履歴、未承認（`enabled: false`）アクションの確認・有効化。
- **ジョブ詳細への追加**：HWテスト結果の表（HW-ID、要件ID、判定、測定値と許容範囲、証拠ファイル）、実機ラウンド数。
- **非常停止ボタン**：ターゲット使用中は全画面に表示。

## H12. 言語ごとの実機検証の想定

| プロファイル | 成果物 | 書き込み・配置の例 | 実機テストの例 |
| :-- | :-- | :-- | :-- |
| `c` / `cpp`（組込み） | ELF / HEX | openocd、probe-rs、ベンダーの書き込みツール | UART ログ、GPIO、計測器による測定 |
| `c` / `cpp`（Linux ボード） | 実行ファイル | scp ＋ ssh で実行 | 標準出力、デバイスファイル、測定 |
| `python`（計測・制御ツール） | パッケージ／スクリプト | 実機につながった PC の venv へ配置 | ツール自体を実機に対して実行し、応答と測定値を判定 |
| `web` | ビルド成果物 | 実機の Web サーバーや配信先へ配置 | Playwright の E2E、API の応答確認 |
| `verilog` | ビットストリーム | Vivado バッチ（合成・配置配線・**WNS ≥ 0 の確認**）→ hw_server 経由で書き込み | UART やレジスタ読み出し（JTAG-to-AXI など）、必要なら ILA の取得 |

これらは例であり、個々の書き込み・測定の手順はターゲットのアクションとして実装する。core は手順の中身を知らない。
