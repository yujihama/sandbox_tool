# 出力ゲート実装 改修事項一覧

> 改訂メモ（v1.1）: 初版に対し、レビューで挙がった次の補強を各 Phase に反映した。
> (A) output-gate container 自身の分離強化（パーサが攻撃面）、(B) sandbox-controller API を信頼境界として扱う検証・認証、(C) HTML の SVG / `data:` URI の無害化、(D) md reject 理由の具体化と repair loop 整合、(E) csv エスケープ方式の副作用への配慮。加えて xlsx の zip bomb 耐性、multi_deep の merge 捕捉前提、関連テスト fixture を追加。

## 目的

Deep Agent が sandbox 内で自由に生成した成果物を、ホスト・親 agent・人間レビュー・下流システムへ渡す前に、出力 allowlist ゲートで検証・無害化・隔離する。

本改修の主目的は次の3点。

- sandbox 内の自由度を維持する。
- raw `/outputs` を信頼境界外へ直接出さない。
- 親 agent が確認する成果物を、gate 通過済みの clean export に限定する。

## 前提方針

### 採用する方針

- Deep Agent の作業中はノーゲート。
- Deep Agent は raw output 領域へ自由に書く。
- `request_parent_review(artifacts=[...])` の `artifacts` を明示的な export 宣言として扱う。
- 出口ゲートは export 宣言されたファイルのみ処理する。
- 親 agent は raw output を読まず、clean export 領域のみ読む。
- 不合格・判定不能ファイルは quarantine に隔離し、fail-closed とする。
- `xlsx` は数式を一律値化しない。通常数式は保持し、危険関数・外部参照・能動要素だけを拒否または無効化する。
- RedHat + Docker Compose 環境では、Docker-in-Docker ではなく sandbox-controller sidecar 方式を基本設計とする。
- **output gate container は sandbox container と同一強度で分離する。** gate は未信頼ファイルをパースするため、パーサ自体が攻撃面となる。`network none` だけでなく、cap-drop・no-new-privileges・read-only rootfs・非 root・リソース制限を sandbox container と同等に適用する。
- **sandbox-controller の HTTP API を新たな信頼境界として扱う。** socket を agent-app から剥がした代わりに controller API が攻撃面になるため、ネットワーク分離・入力検証・認証を多層で施す。

### 採用しない方針

- 親 agent が raw `/outputs` を直接読む本番運用。
- Docker-in-Docker を標準構成にすること。
- `xlsx` の全数式を値化する軽量 CDR。
- LLM にセキュリティ判定の執行を任せること。

## 目標アーキテクチャ

```text
docker compose
  agent-app
    parent agent / deepagent tool
    no docker.sock
    |
    | narrow API
    v
  sandbox-controller
    docker/podman socket holder
    starts one-shot sandbox containers
    starts one-shot output-gate containers

host runtime
  /srv/sandbox-tool/runs/{run_id}/
    input/
    raw_outputs/
    clean_exports/
    quarantine/
    gate_logs/
    runner_logs/
```

Sandbox container mount:

```text
/input    -> run/input        read-only
/outputs  -> run/raw_outputs  read-write
network   -> none
```

Output gate container mount:

```text
/raw_outputs    -> run/raw_outputs    read-only
/clean_exports  -> run/clean_exports  read-write
/quarantine     -> run/quarantine     read-write
/gate_logs      -> run/gate_logs      read-write
network         -> none
```

Parent review:

```text
parent agent
  reads only /clean_exports through read_exported_file
  reads gate_logs through inspect_gate_manifest
  never reads raw_outputs in production mode
```

## 改修フェーズ

## Phase 1: Run Directory 分離

### 変更内容

- 現在の `output_dir` を単一領域として使う設計から、run root 配下の複数領域に分離する。

```text
run_root/
  input/
  raw_outputs/
  clean_exports/
  quarantine/
  gate_logs/
  runner_logs/
```

### 実装タスク

- `RunnerConfig` に以下を追加する。
  - `run_root`
  - `raw_output_dir`
  - `clean_export_dir`
  - `quarantine_dir`
  - `gate_log_dir`
  - `runner_log_dir`
- 既存の `output_dir` の用途を整理する。
  - sandbox mount 先は `raw_output_dir`
  - parent trace / cleanup / evaluation は `runner_log_dir`
  - parent が読む成果物は `clean_export_dir`
- `require_safe_output_dir` を `require_safe_run_root` に拡張する。
- `--output-dir` は run root を指す引数として維持する。

### 受け入れ条件

- sandbox container から見える `/outputs` は `raw_outputs/` のみ。
- runner trace や parent report が sandbox から書き換えられない。
- run root 外へ path traversal できない。

## Phase 2: Output Gate Core

### 変更内容

出力 allowlist ゲートを実装する。ゲートは deterministic code として実行し、LLM は判定執行に使わない。

### 対象形式

初期 allowlist:

- `.md`
- `.csv`
- `.json`
- `.yaml`
- `.yml`
- `.xlsx`
- `.html`

その他形式:

- fail-closed
- quarantine へ隔離
- gate manifest に reject reason を記録

### 実装タスク

- `outputs/output_gate.py` または package 化した `sandbox_tool/output_gate.py` を作成する。
- CLI を用意する。

```powershell
python -m sandbox_tool.output_gate `
  --raw-root /raw_outputs `
  --clean-root /clean_exports `
  --quarantine-root /quarantine `
  --log-root /gate_logs `
  --artifact /outputs/report.md `
  --artifact /outputs/result.xlsx
```

- manifest を出力する。

```json
{
  "policy_version": "output-gate-v1",
  "run_id": "...",
  "overall_status": "pass|fail|partial",
  "artifacts": [
    {
      "requested_path": "/outputs/report.md",
      "raw_path": "raw_outputs/report.md",
      "clean_path": "clean_exports/report.md",
      "quarantine_path": null,
      "status": "pass|sanitized|rejected|error",
      "kind": "md",
      "raw_sha256": "...",
      "clean_sha256": "...",
      "bytes_raw": 123,
      "bytes_clean": 120,
      "findings": [],
      "actions": []
    }
  ]
}
```

### 受け入れ条件

- allowlist 外拡張子は clean export に出ない。
- 拡張子と実体が一致しない場合は reject。
- 判定不能時は reject。
- gate manifest に hash、判定、変更内容、エラーが残る。
- **gate container は sandbox container と同一強度で分離されている**（`network none`・cap-drop・no-new-privileges・read-only rootfs・非 root・timeout/memory/CPU 制限）。gate の処理対象は敵対的入力である前提で固める。
- **gate manifest（`gate_logs`）は gate container が書くが、sandbox からは不可視、parent からは read-only であり、後から改ざんされない配置になっている**（Phase 1 の run directory 分離で担保）。

## Phase 3: Parent Review Flow の変更

### 変更内容

親 agent が raw output を読める tool を本番モードから外し、gate 通過済みファイルだけを読む tool に置換する。

### 実装タスク

- 新 tool を追加する。
  - `run_output_gate(export_artifacts: list[str])`
  - `inspect_gate_manifest()`
  - `list_exported_files(root: str = "/exports")`
  - `read_exported_file(path: str)`
  - `inspect_exported_artifacts()`
  - `list_quarantine_metadata()`
- 既存 parent tools の扱いを分ける。
  - `read_sandbox_file`: Deep Agent 用には残す。
  - `inspect_sandbox_file`: Deep Agent 用または dev mode 専用。
  - `inspect_expected_artifacts`: clean export を見る実装へ変更。
- parent system prompt を変更する。
  - Deep Agent 実行後に必ず `run_output_gate` を呼ぶ。
  - gate pass / sanitized の成果物だけ読む。
  - gate fail の場合は gate finding を Deep Agent に返して修正させる。
  - raw output を読むことは禁止。
- dev/debug 用に `--allow-raw-parent-inspection` を追加する。
  - default は false。
  - true の場合のみ raw inspection tools を parent に渡す。

### 受け入れ条件

- production mode では parent tools から raw `/outputs` を読めない。
- parent final response は clean export path と gate manifest に基づく。
- gate fail 時に Deep Agent へ修正指示が返る。

## Phase 4: 明示的 Export 宣言

### 変更内容

`request_parent_review` を export 宣言として扱い、宣言されていない raw file は外へ出さない。

### 実装タスク

- `request_parent_review` の validation を強化する。
  - `artifacts` は `/outputs/...` のみ。
  - path traversal を拒否。
  - duplicate を正規化。
  - `expected_artifacts` との関係を記録。
- export 対象を決めるルールを実装する。

推奨ルール:

```text
export_candidates = request_parent_review.artifacts
required_exports = expected_artifacts

if required_exports not subset of export_candidates:
    protocol issue

gate_targets = export_candidates
```

- self-check script は export 対象にしない。
  - `/outputs/self_check.py` は code なので allowlist 外。
  - 親が読む必要があるのは `/outputs/self_check_report.md`。
  - 必要なら self-check script の hash と実行ログだけ runner log に記録する。

### 受け入れ条件

- request に含まれない中間生成物は clean export に出ない。
- self-check code は clean export に出ない。
- self-check report は md として gate を通る。

## Phase 5: xlsx Gate

### 方針

`xlsx` は通常数式を保持する。数式全体を値化しない。

危険関数・外部参照・能動要素だけを拒否または無効化する。

### 初期 denylist

- `WEBSERVICE`
- `FILTERXML`
- `RTD`
- `CALL`
- `REGISTER.ID`
- `IMAGE`
- `HYPERLINK` with external URL
- external workbook references
- DDE-like patterns

### 通常許可する数式例

- `SUM`
- `AVERAGE`
- `COUNT`
- `IF`
- `IFS`
- `AND`
- `OR`
- `XLOOKUP`
- `VLOOKUP`
- `INDEX`
- `MATCH`
- `LET`
- `TEXTJOIN`
- arithmetic formulas

### 実装タスク

- `.xlsm` を reject。
- zip / OOXML 構造を検査する。
- **zip bomb 耐性を持たせる**（展開後の総サイズ上限、エントリ数上限、ネスト深さ制限）。xlsx は実体が zip のため、エージェントが悪意ある xlsx を生成する経路に対して出口側でも防御する。
- `xl/vbaProject.bin` があれば reject。
- `xl/externalLinks/*` があれば reject または除去。
- workbook connections を reject または除去。
- OLE / ActiveX / embedded objects を reject または除去。
- formula cell を scan する。
  - dangerous function があれば default reject。
  - `--xlsx-dangerous-formula-action=reject|stringify` を追加する。
  - `stringify` の場合は危険セルだけ先頭 `'` を付けて文字列化する。
- clean workbook を再保存する。
  - 通常数式は保持。
  - workbook metadata と gate action を manifest に記録。

### 受け入れ条件

- 通常数式は clean xlsx に残る。
- `=WEBSERVICE(...)` は pass しない。
- `=HYPERLINK("https://...")` は pass しない。
- 内部リンクだけの `HYPERLINK("#Sheet1!A1", "link")` は policy で許可可能。
- manifest に危険セルの sheet/cell/formula/action が残る。

## Phase 6: csv / md / html Gate

### csv

実装タスク:

- UTF-8 decode。
- dialect parse。
- cell 先頭が以下のいずれかなら数式 injection として escape。
  - `=`
  - `+`
  - `-`
  - `@`
  - tab
  - carriage return
- clean CSV を UTF-8 で再出力する。
- 変更セル数を manifest に記録する。

エスケープ方式の注意:

- 先頭 `'` 付与は一般的だが、CSV では `'` がそのままデータとして残り（例: `'=SUM...`）、**監査データの数値・文字列の意味を変える副作用**がある。
- エスケープ対象は「先頭が危険文字 **かつ** 表計算ソフトで数式と誤解釈されうるセル」に絞り、正常な負数（`-100` 等）や正常なテキストを過剰にエスケープしないこと。
- 採用するエスケープ方式（先頭 `'` / 先頭タブ / セルを `"` で囲む等）を policy として固定し、manifest に記録する。受け入れ条件「正常 CSV は内容を大きく変えずに通る」と整合させる。

受け入れ条件:

- `=cmd|' /C calc'!A0` のようなセルがそのまま出ない。
- 正常 CSV は内容を大きく変えずに通る。

### md

実装タスク:

- UTF-8 decode。
- size limit。
- raw HTML の扱いを policy 化する。
  - MVP: raw HTML を escape または reject。
  - 後続: markdown renderer 側で sanitize。
- HTML passthrough を無効化した markdown render path を文書化する。

受け入れ条件:

- `<script>` を含む markdown がそのまま clean export に出ない。
- external image / iframe / object を含む場合は sanitize または reject。
- **reject 時は findings に具体的な理由（例: `raw HTML <script> detected at line N`）を記録し、Phase 3 の repair loop で Deep Agent が修正できるようにする**。正当な表組み等で頻繁に reject されて修復ループが空回りしないよう、理由を具体化する。

### html

実装タスク:

- HTML parser で parse する。
- `<script>` を除去。
- `on*` event handler を除去。
- `javascript:` URL を除去。
- external `src` / `href` / `srcset` を除去または reject。
- `iframe`, `object`, `embed`, `base`, `meta refresh` を除去。
- **SVG を無害化する**（インライン `<svg>` 内の `<script>`・`foreignObject`・イベントハンドラを除去）。`data:` URI 経由の SVG（`<img src="data:image/svg+xml,...">`）にスクリプトが埋め込まれる既知の手法に対処する。
- **`data:` URI の許可範囲を限定する**。CSP で `img-src` に `data:` を許可する場合でも、SVG を含む `data:` はサニタイズ後のみ許可、または `data:` を画像バイナリ形式に限定する。
- 自前パーサではなく **DOMPurify 等の実績あるサニタイザを使う**ことを推奨（SVG・`data:`・mutation XSS にデフォルトで対処されるため）。
- CSP meta を注入する。

初期 CSP:

```html
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self' data:; base-uri 'none'; form-action 'none'">
```

受け入れ条件:

- `fetch`, beacon, external image, event handler が clean HTML に残らない。
- CSS は保持できる。

## Phase 7: Docker Compose / RedHat 対応

### 方針

Docker-in-Docker ではなく、sandbox-controller sidecar 方式を採用する。

### コンポーネント

```text
agent-app
  - parent agent
  - deepagent tool
  - no docker.sock

sandbox-controller
  - docker/podman socket holder
  - narrow API
  - starts sandbox containers
  - starts output-gate containers
  - enforces mount/network/image policy
```

### 実装タスク

- `sandbox-controller` を追加する。
  - FastAPI などの小さい HTTP API。
  - API は内部 compose network のみ。
  - agent-app からしか呼べない構成にする。
  - **API は新たな信頼境界。ネットワーク分離だけに依存せず、多層で防御する。**
- endpoint を定義する。
  - `POST /runs/{run_id}/execute`
  - `POST /runs/{run_id}/gate`
  - `POST /runs/{run_id}/cleanup`
  - `GET /runs/{run_id}/status`
- **controller API の入力検証を強化する。**
  - `run_id` を厳格に検証する（許可文字種を限定し、`../` 等の path traversal を拒否）。controller は `run_id` からホストパスを構築するため、ここがディレクトリ脱出経路になりうる。
  - **gate target / artifact パスを controller 側でも再検証する**（`/outputs` 配下のみ、traversal 拒否）。Phase 4 の validation は agent-app 側だが、controller は別の信頼境界であり二重検証する。
  - **API 認証を持たせる**（内部トークン等）。ネットワーク分離と認証の両方で多層防御する。
- controller 側で policy を強制する。
  - image allowlist
  - `--network none`
  - fixed bind mounts only
  - no arbitrary host path mounts
  - timeout / memory / CPU limits
  - cleanup always
  - **上記 policy を sandbox container だけでなく output-gate container にも同一適用する**（gate も敵対的入力を処理するため）。
- runtime abstraction を追加する。
  - `SANDBOX_RUNTIME=docker|podman`
  - `SANDBOX_SOCKET=/var/run/docker.sock`
  - `SANDBOX_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock`
- run root を固定ホストパスにする。
  - default: `/srv/sandbox-tool/runs`
  - compose で agent-app と sandbox-controller の両方に mount。

### docker compose 概略

```yaml
services:
  agent-app:
    build: .
    environment:
      SANDBOX_CONTROLLER_URL: http://sandbox-controller:8080
      RUNS_ROOT: /srv/sandbox-tool/runs
    volumes:
      - /srv/sandbox-tool/runs:/srv/sandbox-tool/runs
    depends_on:
      - sandbox-controller

  sandbox-controller:
    build:
      context: .
      dockerfile: docker/sandbox-controller.Dockerfile
    environment:
      SANDBOX_RUNTIME: docker
      SANDBOX_SOCKET: /var/run/docker.sock
      RUNS_ROOT: /srv/sandbox-tool/runs
    volumes:
      - /srv/sandbox-tool/runs:/srv/sandbox-tool/runs
      - /var/run/docker.sock:/var/run/docker.sock
```

### RedHat / Podman 変種

```yaml
services:
  sandbox-controller:
    environment:
      SANDBOX_RUNTIME: podman
      SANDBOX_SOCKET: /run/user/1000/podman/podman.sock
    volumes:
      - /srv/sandbox-tool/runs:/srv/sandbox-tool/runs
      - /run/user/1000/podman/podman.sock:/run/user/1000/podman/podman.sock
```

### 受け入れ条件

- agent-app に docker.sock / podman.sock が mount されていない。
- sandbox-controller だけが container runtime API にアクセスできる。
- sandbox container は sibling container として起動される。
- sandbox container は `--network none`。
- controller は任意 host path mount を受け付けない。
- **output-gate container も sandbox container と同一の分離 policy で起動される。**
- **controller API は `run_id` と artifact パスを検証し、path traversal を拒否する。**
- **controller API はネットワーク分離に加えて認証を要求する。**

## Phase 8: Backend 抽象化

### 変更内容

現在の local Podman backend と remote controller backend を同じ interface で扱う。

### 実装タスク

- `SandboxBackendProtocol` を明確化する。
- backend implementations を分ける。
  - `PodmanSandboxBackend`
  - `DockerSandboxBackend`
  - `RemoteSandboxControllerBackend`
- runner option を追加する。

```text
--sandbox-backend local-podman|local-docker|controller
--sandbox-controller-url http://sandbox-controller:8080
--runs-root /srv/sandbox-tool/runs
```

### 受け入れ条件

- Windows local Podman 実験を壊さない。
- RedHat Docker Compose では controller backend を使える。
- sandbox cleanup の結果が同じ形式で記録される。

## Phase 9: 入口ゲート

### 変更内容

入力 stage 前に最低限の入口ゲートを追加する。

### 実装タスク

- input staging 前に file metadata を検査する。
  - path traversal
  - file size
  - extension / magic byte
  - zip nesting / zip bomb risk
- 入力ごとに provenance を付与する。
  - original host path
  - staged path
  - sha256
  - detected kind
  - trust label: untrusted_external
- 危険・判定不能の場合は stage せず reject。
- 後続で ClamAV / pikepdf / OOXML scan などを追加できる interface にする。

### 受け入れ条件

- zip bomb 風の異常 archive を stage しない。
- extension mismatch を記録または reject する。
- input manifest に hash と trust label が残る。

## Phase 10: Tests / Evaluation

### Unit Tests

- path normalization / traversal rejection。
- magic byte / extension 判定。
- CSV formula injection escape。
- HTML sanitizer。
- Markdown raw HTML handling。
- xlsx dangerous formula detection。
- xlsx normal formula preservation。
- manifest generation。

### Integration Tests

- Deep Agent が raw output を作る。
- output gate が clean export を作る。
- parent が clean export だけ読む。
- gate fail 時に parent が Deep Agent に修正指示を返す。
- controller backend が sandbox container を起動し cleanup する。
- **gate container が sandbox container と同一の分離 policy で起動されることを検証する。**
- **controller API が不正な `run_id`（traversal を含む）と `/outputs` 外の artifact パスを拒否することを検証する。**
- **`evil.xlsx`（zip bomb 風の展開比）を gate が安全に拒否することを検証する。**

### Security Regression Fixtures

- `evil.csv`
  - `=WEBSERVICE("https://example.com")`
  - `+cmd|`
  - `@SUM(1,1)`
- `evil.html`
  - `<script>`
  - `onload=`
  - `fetch(...)`
  - external image
  - `javascript:`
  - **`data:image/svg+xml` 内に埋め込まれた script**
  - **インライン `<svg>` 内の `<script>` / `foreignObject`**
- `evil.md`
  - raw `<script>`
  - raw iframe
- `evil.json`
  - malformed JSON
  - `NaN` / `Infinity`
- `evil.yaml`
  - anchors / aliases
  - duplicate keys
  - non-string keys
  - tags outside the JSON-compatible safe subset
- `evil.xlsx`
  - `WEBSERVICE`
  - external links
  - `vbaProject.bin`
  - data connections
- `good.json`
  - parsed and canonicalized.
- `good.yaml`
  - parsed through the safe subset and canonicalized.
- `good.xlsx`
  - normal formulas preserved after gate.

### 受け入れ条件

- malicious fixtures は clean export に出ない。
- normal fixtures は pass する。
- every gate action has manifest evidence。

## Phase 11: Documentation / Operation

### 実装タスク

- README 更新。
  - local Windows / WSL Podman mode
  - RedHat Docker Compose controller mode
  - output gate policy
  - xlsx formula policy
- 運用手順書を追加。
  - run directory retention
  - quarantine review
  - cleanup
  - policy version update
- `.env.example` を追加または更新。
  - `OPENAI_API_KEY`
  - `SANDBOX_BACKEND`
  - `SANDBOX_CONTROLLER_URL`
  - `RUNS_ROOT`
  - `SANDBOX_RUNTIME`
  - `SANDBOX_SOCKET`

### 受け入れ条件

- RedHat Docker Compose 環境で必要な env と volume が明確。
- Docker-in-Docker を使わない理由が明記されている。
- raw output と clean export の責務差が明記されている。

## 実装順序の推奨

1. Run directory 分離。
2. Output gate MVP。
3. Parent review を clean export reader に変更。
4. xlsx dangerous formula policy。
5. csv/md/html policy。
6. Gate fail repair loop。
7. Sandbox-controller backend。
8. Docker Compose / RedHat deployment。
9. 入口ゲート。
10. Security fixtures と regression tests。

## 既存コードへの主な影響

### `outputs/generic_parent_runner.py`

- `RunnerConfig` の directory model を変更。
- parent tools を production/dev で分離。
- `request_parent_review` を export declaration として validation。
- `inspect_expected_artifacts` を clean export ベースに変更。
- `run_output_gate` tool を追加。
- runner logs の保存先を `runner_logs` に移動。

### `outputs/multi_deep_parent_runner.py`

- subtask expected artifacts も gate 対象にする。
- intermediate artifacts は原則 raw のまま。
- final synthesis artifact だけ clean export へ出すか、subtask ごとに gate するかを option 化。
- **subtask 生成物が merge されて final になる場合、merge 後の final を必ず gate に通すことを保証する**（subtask 段階で gate を省略しても、危険要素が final gate で確実に捕捉される構造にする）。MVP で final のみ gate とするなら、この捕捉前提が成り立つことを受け入れ条件とする。

### `outputs/podman_sandbox_backend.py`

- local backend は維持。
- mount 対象を `raw_outputs` に変更。
- Docker backend または controller backend を追加する場合は protocol 化を進める。

### 新規追加候補

- `sandbox_tool/output_gate.py`
- `sandbox_tool/input_gate.py`
- `sandbox_tool/export_readers.py`
- `sandbox_tool/sandbox_controller.py`
- `docker-compose.yml`
- `docker/sandbox-controller.Dockerfile`
- `tests/security_fixtures/`
- `tests/test_output_gate.py`

## Open Decisions

1. `xlsx` 危険関数を見つけた場合の default action。
   - 推奨: `reject`
   - option: `stringify`
   - 所見: `stringify` は数式を壊すため、`reject` で Deep Agent に修復させる方がクリーンな成果物に収束する。

2. `html` の外部リンク。
   - 推奨: external resource は reject または strip。
   - 単なるテキストリンクは policy 次第。
   - 所見: `<a href>` の外部 URL を残す場合、クリック遷移＝端末からの通信となるため、消費時（④）の egress 遮断が前提条件になる。

3. `md` の raw HTML。
   - 推奨 MVP: raw HTML を reject。
   - 後続: sanitizer で許可 tag のみ通す。
   - 所見: reject 理由を findings に具体化し、repair loop が空回りしないようにする（Phase 6 md 受け入れ条件参照）。

4. subtask artifacts の gate timing。
   - 推奨 MVP: final expected artifacts のみ。
   - 後続: subtask artifact も gate できる option。
   - 所見: final のみとする場合、merge 後の final が必ず gate を通り、危険要素を確実に捕捉できることが前提（multi_deep 影響参照）。

5. quarantine の保管期間。
   - 監査証跡として保持するか、短期削除するかを運用方針で決める。
   - 所見: fail-closed で隔離したものの「なぜ拒否したか」は監査上の説明責任になるため、短期削除より run retention に合わせた一定期間保持を推奨。

## Done Definition

- Deep Agent は raw `/outputs` に自由に成果物を作れる。
- 親 agent は production mode で raw `/outputs` を読めない。
- export 宣言された成果物だけ output gate を通る。
- clean export だけが親レビューと人間レビューの対象になる。
- `md/csv/xlsx/html` の allowlist が deterministic に実行される。
- `xlsx` の通常数式は保持され、危険関数だけ reject または文字列化される。
- Docker Compose / RedHat 構成で agent-app に docker.sock を渡さない。
- sandbox-controller が sibling sandbox container を起動し、`--network none` と cleanup を強制する。
- **output-gate container が sandbox container と同一強度で分離されている。**
- **sandbox-controller API が `run_id`・artifact パスを検証し、認証を要求する。**
- gate manifest が監査証跡として残る。
