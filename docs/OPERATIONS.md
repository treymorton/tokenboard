# TokenBoard — field operations notes

Machine-specific operating notes from the original deployment (GB10 "gb10-1").
For the generic, new-user documentation see the main README.

## This box

- Box name: `gb10-1` (config.json), role box, port 8100
- Hub: same machine, `config-hub.json`, port 8101
- Model backend **since 2026-10-21**: llama.cpp (`llama-server`, Hermes-
  spawned, ephemeral port) serving Qwen3.8-Flash-Next IQ4_XS + MTP draft.
  TokenBoard counts it via llama auto-discovery — no config needed.
- Model backend until 2026-10-15: vLLM Qwen3.8-27B (NVFP4) in Docker,
  `--solo` mode, port 8000 (see startup below; kept for reference)
- Deployed 2026-10-12; running as user systemd services since 2026-10-13

## vLLM startup (manual — not a service)

```bash
cd ~/spark-vllm-docker
./launch-cluster.sh stop
./launch-cluster.sh --solo -d exec vllm serve unsloth/Qwen3.8-27B-NVFP4 \
  --served-model-name qwen3.8-27b --host 0.0.0.0 --port 8000 \
  --tensor-parallel-size 1 --gpu-memory-utilization 0.45 --max-model-len 262144 \
  --max-num-seqs 4 --max-num-batched-tokens 8192 --enable-chunked-prefill \
  --enable-prefix-caching --reasoning-parser qwen3 --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder --distributed-executor-backend mp \
  --speculative-config '{"method":"mtp","num_speculative_tokens":5}'
# ~4 min → "Application startup complete", then:
curl -s http://localhost:8000/v1/models
```

TokenBoard needs no intervention across reboots: its systemd user services
come up on their own (account has `Linger=yes`), tolerate vLLM being down,
and re-baseline the counters when vLLM returns.

## systemd services (this box)

```bash
systemctl --user status tokenboard-box tokenboard-hub --no-pager
systemctl --user restart tokenboard-box tokenboard-hub
journalctl --user -u tokenboard-box -n 50 --no-pager
```

Installed 2026-10-13. The generic templates live in `deploy/`; `install.sh`
installs them for any machine.

## Incident log

### 2026-10-14 — reboot exposed a silent commit bug

Full machine reboot at ~17:22 (the "does it survive a reboot" test). vLLM
down 17:22–22:05, then restarted with counters at 0. The dashboard then
showed "today = 0" and "last 7 days ≈ 568k" instead of the true ~2.6M and
~29M.

**Root cause:** the sampler's reset-detection state (offsets + last-seen raw
counters) is persisted in the SQLite `meta` table, but the writes were made
on a connection that was never `commit()`-ed — SQLite rolled them back on
close. Reset detection therefore always saw `last_raw_prompt = None` and
never re-baselined; the cumulative series dropped 27,142,367 → 0 at the
vLLM restart. Windows whose baseline predated the reset (e.g. "yesterday" =
23,021,173) were unaffected, which is why the daily bars looked right while
the rolling numbers were wrong.

**Fix:** commit the meta writes (vLLM + ollama branches in `Sampler.sample`),
plus rollback-on-error. The affected counter was back-filled manually:
`offset_prompt = 27142367 / offset_gen = 292835 / offset_req = 282` (last
pre-reset sample, 17:22:06), `last_raw_*` set to the live raw counters.
Corrected readouts verified: today ≈ 3.2M, yesterday 23,021,173 (unchanged),
7d/30d/all ≈ 29M, cumulative 29.2M — all consistent with the sample history.

**Lesson:** every SQLite write in this project must be followed by an
explicit `conn.commit()` (WAL mode does not auto-commit).

**Outcome:** this was also a successful live validation of the reboot story —
systemd services came up on their own, the 4.7 h vLLM gap shows in the GPU
chart, and counters continued from 27.1M instead of 0.

### 2026-10-21 — llama.cpp token source added; vLLM→llama switch re-seed

The box's model moved from vLLM (dead since 10-15 22:11, cumulative
37,474,052 prompt / 425,572 gen / 413 req) to Hermes-served llama.cpp
(Qwen3.8-Flash-Next, `llama-server` on an ephemeral port). The vLLM-only
sampler counted nothing after the switch; llama support with /proc port
auto-discovery and source-aware re-baselining was added (see README).

Re-seed used after the switch (additive semantics — correct only because
the two engines never overlapped and the llama server started after
midnight, so its whole raw counter belongs to "today"):

```python
import sqlite3
c = sqlite3.connect('tokenboard.db')   # STOP the service first
for k, v in [('offset_prompt', 37474052), ('offset_gen', 425572),
             ('offset_req', 413)]:     # vLLM-era true totals, fake-test bleed excluded
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
c.commit()
# systemctl --user restart tokenboard-box  (reloads offsets)
# verify: today == llama raw (prompt_tokens_total + prompt_tokens_cached_total
#         + tokens_predicted_total from /metrics); all == vLLM-era + llama raw
```

**Test-bleed warning:** during the same window a test fixture served a fake
vLLM `/metrics` on :8000 for ~40 s; the then-running vLLM-only sampler
ingested it (~5k phantom tokens) before the re-seed wiped it out. Test
backends must never bind ports the live sampler polls — stop
`tokenboard-box` first, or use a port nothing probes.

## Tradeshow checklist

- [ ] Box timezone set to the venue's local zone (daily bars follow it)
- [ ] vLLM started before the day (TokenBoard can come up first, but a fresh
      box mid-stream attributes vLLM's existing counter to day one)
- [ ] `python3 scripts/refresh_prices.py` run at the venue (or trust the
      snapshot date shown in the dashboard's price table)
- [ ] All boxes pointed at the hub (`--hub-url`), hub on the laptop
- [ ] Verify: `curl -s localhost:8100/api/state` (box) and
      `curl -s localhost:8101/api/state` (hub, `boxes_online`)
- [ ] Prices re-verified against vendor pages before publishing dollar figures
