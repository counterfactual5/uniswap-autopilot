# Risk Policy Reference

Cross-project rule engine that sits between `PREFLIGHT` and `SIGNED` states
in the trade execution state machine.

## Quick Start

```bash
# Copy the template to the default lookup path (YAML or JSON both work)
cp policy.yaml ~/.stageforge/policy.yaml
# cp policy.yaml ~/.stageforge/policy.json   # optional: convert if you prefer JSON

# Or use a custom location
export POLICY_FILE=/path/to/my-policy.yaml
```

When running under **StageForge**, `bin/stageforge` exports `POLICY_FILE` automatically
if `~/.stageforge/policy.yaml`, `policy.yml`, or `policy.json` exists (same order as
`load_policy()`).

## File Resolution

1. Explicit path argument to `load_policy(path=...)`
2. `POLICY_FILE` environment variable
3. `~/.stageforge/policy.yaml`
4. `~/.stageforge/policy.json`

If no file is found, returns a **permissive default** (no limits, everything
allowed).  This is the safe default — policy files only restrict, never
broaden access.

## Rule Cascade

```
global:
  max_amount: 1000          ← base rules
  allowed_chains: [...]

uniswap-autopilot:
  max_amount: 2000          ← overrides global for this project only
```

Project sections merge on top of `global`.  Non-null values override;
non-overridden fields inherit from global.  Two edge cases worth knowing:

- **`allowed_chains: []`** — empty list disables the chain check (all chains
  pass).  This is different from omitting the field (inherits global list).
- **`blacklist_addresses: []`** — empty list means no addresses are blocked
  (check runs, nothing matches).

In short: an empty list always means "no restriction on this field", never
"block all".

## Shared Rules (all projects)

| Rule | Key | Severity | Description |
|------|-----|----------|-------------|
| Trade amount cap | `max_amount` | **Hard reject** | Trade value > limit → blocked. USD or native units. |
| Chain whitelist | `allowed_chains` | **Hard reject** | Chain not in list → blocked. |
| Blacklist addresses | `blacklist_addresses` | **Hard reject** | Sender/receiver/spender in list → blocked. |
| Whitelist addresses | `whitelist_addresses` | **Soft warn** | Address not in list → warning only. |
| Max slippage | `max_slippage_bps` | **Hard reject** | Slippage in basis points > limit → blocked. |
| Max gas price | `max_gas_price_gwei` | **Soft warn** | Gas price > limit → warning only. |

**Hard reject** = trade is blocked, `STATE_FAILED` recorded, audit event emitted.

**Soft warn** = trade proceeds, warning attached to `CheckResult.warnings`.

## Project-Specific Rules

### hyperliquid-autopilot

Activated via `check_hyperliquid(policy, context)`.

| Rule | Key | Severity | Description |
|------|-----|----------|-------------|
| Max leverage | `max_leverage` | **Hard reject** | Leverage multiplier > limit → blocked. |
| Allowed coins | `allowed_coins` | **Hard reject** | Coin not in list → blocked (empty = all allowed). |

```yaml
hyperliquid-autopilot:
  max_leverage: 10
  allowed_coins:
    - BTC
    - ETH
    - SOL
```

### polymarket-autopilot

Activated via `check_polymarket(policy, context)`.

| Rule | Key | Severity | Description |
|------|-----|----------|-------------|
| Min price | `min_price` | **Hard reject** | Order price < floor → blocked. |
| Max price | `max_price` | **Hard reject** | Order price > ceiling → blocked. |
| Max position | `max_position_value` | **Hard reject** | Per-order notional (`price × size`, in USDC) > cap → blocked. Does not aggregate existing positions. |

```yaml
polymarket-autopilot:
  min_price: 0.01
  max_price: 0.99
  max_position_value: 5000
```

### uniswap-autopilot

Activated via `check_uniswap(policy, context)`.

| Rule | Key | Severity | Description |
|------|-----|----------|-------------|
| Min output | `min_output_amount` | **Hard reject** | Expected output < floor → blocked (0 = disabled). |

```yaml
uniswap-autopilot:
  min_output_amount: 100.0   # require at least 100 USDC worth of output
```

### defi-autopilot

defi-autopilot enforces policy at the **broadcast chokepoint**
(`core/tx.build_and_send_tx`), which every protocol client (Aave, Morpho,
Moonwell, Uniswap, Curve, Compound, Lido, 1inch) funnels through.

| Rule | Key | Severity | Notes |
|------|-----|----------|-------|
| Chain allow-list | `allowed_chains` | **Hard reject** | Enforced for every tx |
| Blacklist | `blacklist_addresses` | **Hard reject** | Checked against the tx destination/spender |
| Native amount cap | `max_amount` | **Hard reject** | **Native value only** (ETH/MATIC). ERC-20 amounts live in calldata and are not visible here |

**Why no state machine here?** A single CLI command (e.g. `defi aave supply`)
can broadcast **two** transactions — an ERC-20 `approve` followed by the actual
action. A per-process `run_id` with strict anti-replay would incorrectly treat
the second broadcast as a replay of the first. defi-autopilot therefore applies
the **stateless** policy + audit layers at the chokepoint; if you need
resumable per-operation state, gate it at the CLI-command layer with a distinct
`run_id` per logical operation.

To cap ERC-20 notionals, pass the amount/token explicitly at the protocol-client
layer (future enhancement) rather than relying on the chokepoint.

## Troubleshooting

### "policy_rejected" in audit log

Look for the `details.violations` array:

```bash
jq 'select(.error_code=="policy_rejected") | .details' audit.jsonl
```

Each violation has a `rule` and `message` field telling you exactly what
failed and why.

### Trade blocked, no policy file

This can't happen.  If no policy file is found, `load_policy()` returns a
permissive `Policy()` with all limits set to `None` — everything passes.

### Trade blocked, expected to pass

1. Check `~/.stageforge/policy.yaml` (or `.json`) — is the right project section present?
2. Validate syntax:
   ```bash
   python3 -c "import yaml; from pathlib import Path; yaml.safe_load(Path.home().joinpath('.stageforge/policy.yaml').read_text())"
   # or for JSON:
   python3 -c "import json; from pathlib import Path; json.loads(Path.home().joinpath('.stageforge/policy.json').read_text())"
   ```
3. Override temporarily: `unset POLICY_FILE` and rename/disable the file under `~/.stageforge/`,
   or point `POLICY_FILE` at a permissive test file.

## Audit Integration

Policy violations emit `log_event(event=EVENT_ERROR, error_code="policy_rejected")`
with `details.violations` containing the list of failed rules:

```json
{
  "event": "error",
  "error_code": "policy_rejected",
  "details": {
    "violations": [
      {"rule": "max_amount", "message": "amount 1500 exceeds limit 1000"}
    ]
  }
}
```

Soft warnings (whitelist miss, high gas price) do **not** block the trade but
are recorded as a `preflight` event so they remain visible:

```json
{
  "event": "preflight",
  "details": {
    "stage": "policy",
    "warnings": [
      {"rule": "max_gas_price_gwei", "message": "gas price 120 gwei exceeds soft limit 100 gwei"}
    ]
  }
}
```

## Anti-Replay & Resume Drill

Run this 4-step drill per project before trusting the state machine in
production.  It verifies that a re-run with the same `run_id` never
double-broadcasts and that policy rejection lands in a terminal state.

```bash
export STAGEFORGE_STATE_DIR=/tmp/sf-drill        # isolate from real state
export AUDIT_LOG_PATH=/tmp/sf-drill/audit.jsonl
export AUDIT_RUN_ID=drill-001                     # pin a fixed run id
```

1. **First execution** — run the normal trade path. Confirm the state file
   reaches `broadcast` (or `confirmed`):

   ```bash
   cat "$STAGEFORGE_STATE_DIR/drill-001.json" | python3 -c "import json,sys; print(json.load(sys.stdin)['current_state'])"
   ```

2. **Replay (same run_id)** — run the exact same command again. The trade must
   return `already_broadcast` / `already_confirmed` and **must not** call the
   broadcast / order API a second time. Confirm no new `broadcast` line was
   appended to the audit log.

3. **Policy rejection** — bump the trade amount above `max_amount` (or set a
   blacklist hit) with a fresh `AUDIT_RUN_ID=drill-002`. Confirm:
   - `error_code="policy_rejected"` appears in the audit log
   - state file `drill-002.json` is in `failed` (terminal)

4. **Correlation** — verify the `run_id` in the state file matches the `run_id`
   on every audit line for that run:

   ```bash
   jq -r 'select(.run_id=="drill-001") | "\(.event)\t\(.run_id)"' "$AUDIT_LOG_PATH"
   ```

Clean up with `rm -rf /tmp/sf-drill`.

## Auditing All Runs

Audit logs are plain JSON Lines (`audit.py` schema).  Each repository is
**self-contained on GitHub** — there is no bundled summary script in the repo.

For local development, a standalone helper may live beside your clones at
`~/github_projects/audit_summary.py` (not shipped in any cloud repo):

```bash
python3 ~/github_projects/audit_summary.py /tmp/sf-drill/audit.jsonl
cat audit.jsonl | python3 ~/github_projects/audit_summary.py -
python3 ~/github_projects/audit_summary.py audit.jsonl --json
```

Without that local tool, `jq` is enough:

```bash
jq -r '.event' audit.jsonl | sort | uniq -c
```

Example output after a drill run:

```
records: 6  (parse errors: 0)

events:
  preflight  2
  sign       1
  broadcast  1
  confirm    1
  error      1

error_codes:
  policy_rejected  1

policy_warnings:
  max_gas_price_gwei  1

runs:
  unique             3
  reached_broadcast  1
  reached_confirm    1
  with_error_event   1
```

## Maintaining Shared Modules

Four trading projects share `audit.py`, `state_machine.py`, and the **shared
portion** of `policy.py`.  **evm-wallet-scanner** is the source of truth.

| File | Auto-sync? | Notes |
|------|------------|-------|
| `audit.py` | Yes | Identical except `_DEFAULT_PROJECT` |
| `state_machine.py` | Yes | Identical except `_DEFAULT_PROJECT` |
| `policy.py` | **No** (check only) | Each repo adds `check_hyperliquid`, `check_polymarket`, `check_uniswap` |

### Workflow (local development only)

These projects share `audit.py`, `state_machine.py`, and the **shared portion**
of `policy.py`.  **evm-wallet-scanner** is the canonical source when you edit
locally.  On GitHub each repo is **fully independent** — no cross-repo CI, no
bundled sync scripts, no git clones of sibling projects.

Local tooling (lives beside your clones, **not** in any cloud repo):

| Script | Location | Purpose |
|--------|----------|---------|
| `sync_shared.py` | `~/github_projects/sync_shared.py` | Copy shared modules across sibling clones |
| `audit_summary.py` | `~/github_projects/audit_summary.py` | Summarize audit JSONL logs |

```bash
# Requires sibling repos under ~/github_projects/ (never clones from GitHub)
python3 ~/github_projects/sync_shared.py

# Dry-run: report drift without writing
python3 ~/github_projects/sync_shared.py --check
```

**Rules:**

1. Edit `audit.py` / `state_machine.py` under `evm-wallet-scanner/src/evm_wallet_scanner/`, then run the local sync script.
2. For `policy.py`, edit shared rules in scanner; edit project-specific functions (`check_hyperliquid`, …) in each repo.
3. Run tests in each repo you touched after a sync.
4. The sync script only updates directories that exist as **sibling folders on disk**.

## StageForge Integration

`stageforge bin/stageforge run` exports environment variables for downstream
autopilot CLIs:

| Variable | When set | Purpose |
|----------|----------|---------|
| `STAGEFORGE_RUN_ID` | Always (after `signal_init_run`) | Same id in signal files, state machine, audit log |
| `POLICY_FILE` | If `~/.stageforge/policy.{yaml,yml,json}` exists | Points trading projects at the live policy file |

Downstream projects resolve `run_id` from `STAGEFORGE_RUN_ID`, `AUDIT_RUN_ID`, or a generated id (in that order).

### StageForge smoke test

```bash
mkdir -p ~/.stageforge
cp /path/to/evm-wallet-scanner/policy.yaml ~/.stageforge/policy.yaml
# YAML policy files need PyYAML: pip install pyyaml  (or use policy.json instead)

export STAGEFORGE_STATE_DIR=/tmp/sf-smoke
export AUDIT_LOG_PATH=/tmp/sf-smoke/audit.jsonl
mkdir -p /tmp/sf-smoke

# Simulate what stageforge exports (replace RUN_ID with your pipeline run id)
export STAGEFORGE_RUN_ID="smoke-$(date +%s)"
export POLICY_FILE="$HOME/.stageforge/policy.yaml"

# 1) Policy dry-check without RPC
evm-scan doctor --policy --chain ethereum --wallet 0x0000000000000000000000000000000000000001 \
  --amount 10 --policy-file "$POLICY_FILE" --exit-code || true

# 2) After any trade attempt, correlate state + audit
python3 ~/github_projects/audit_summary.py "$AUDIT_LOG_PATH" 2>/dev/null \
  || jq -r '.event' "$AUDIT_LOG_PATH" | sort | uniq -c
ls -la "$STAGEFORGE_STATE_DIR/${STAGEFORGE_RUN_ID}.json" 2>/dev/null || echo "(no state file yet)"
jq -r 'select(.run_id=="'"$STAGEFORGE_RUN_ID"'") | .event' "$AUDIT_LOG_PATH" 2>/dev/null
```

Expected: `doctor --policy` prints a `"policy"` block with `"allowed": true/false`;
`audit_summary` shows event counts; state file name matches `STAGEFORGE_RUN_ID` when a broadcast path ran.
