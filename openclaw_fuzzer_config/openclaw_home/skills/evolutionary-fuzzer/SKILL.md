---
name: evolutionary-fuzzer
description: Run bounded evolutionary mutation planning for a local mocked target, score structural coverage feedback, and adapt operator weights from a localized history matrix. Use only for approved loopback sandbox campaigns.
requires:
  bins: [python3]
  config: [fuzzer_metrics_port]
---

# Evolutionary Fuzzer

Operate only on a human-approved blueprint and the loopback mock target. Keep
all corpus, metrics, and history beneath `WORKSPACE_DIR`; never synthesize shell
commands or use payload bytes as command arguments. This skill defines mutation
and scoring policy, not executable runtime code.

## State model

Represent candidate (x_i) by payload class, length bucket, path signature,
parent ID, operator ID, generation, and fitness history. Persist aggregate—not
secret or externally sourced—statistics in `shared_data/evolution_matrix.json`.

Use the normalized fitness:

\[
F(x_i)=100\,\mathrm{clip}(0.50C_i+0.25N_i+0.15D_i+0.10S_i,0,1)
\]

where (C_i) is branch coverage ratio, (N_i) is path novelty, (D_i) is
normalized execution depth, and (S_i) is the explicit simulated-failure
signal. A process exit code alone must not set (S_i=1).

The fitness delta for operator (o) is

\[
\Delta F_o = F(\mathrm{child})-F(\mathrm{parent}).
\]

## Mutation operators

- **Byte flipping:** choose bounded byte offsets and XOR one bit or a small
  deterministic mask. Track offset bucket and mask width.
- **Block shuffling:** select two non-overlapping, length-bounded blocks and
  transpose them without exceeding the blueprint maximum length.
- **Arithmetic adjustment:** interpret an approved 8-, 16-, or 32-bit field with
  declared endianness and add a bounded delta from
  \(\{-16,-8,-1,+1,+8,+16\}\), wrapping only when the blueprint permits it.
- **Havoc:** apply a capped stack of approved operations. Record the ordered
  operator composition so credit assignment remains reproducible.
- **Character truncation:** delete only at a declared token or field boundary and
  never below the minimum input length.

Reject candidates that violate encoding, framing, length, checksum, generation,
or execution-budget constraints before target evaluation.

## Adaptive learning matrix

Maintain this declarative shape atomically:

```json
{
  "schema_version": 1,
  "cycle_id": "example",
  "generation": 0,
  "path_signatures": {},
  "operators": {
    "byte_flip": {"trials": 0, "positive_deltas": 0, "ema_delta": 0.0, "weight": 0.25},
    "block_shuffle": {"trials": 0, "positive_deltas": 0, "ema_delta": 0.0, "weight": 0.20},
    "arithmetic": {"trials": 0, "positive_deltas": 0, "ema_delta": 0.0, "weight": 0.20},
    "havoc": {"trials": 0, "positive_deltas": 0, "ema_delta": 0.0, "weight": 0.25},
    "character_truncation": {"trials": 0, "positive_deltas": 0, "ema_delta": 0.0, "weight": 0.10}
  }
}
```

After each evaluated child, update the exponential moving average

\[
m_o \leftarrow \alpha\Delta F_o+(1-\alpha)m_o,\quad \alpha=0.20.
\]

Adjust weights only after at least eight trials per eligible operator. Compute

\[
w_o'=\max(0.05,w_o\exp(\eta m_o/100)),\quad \eta=0.5,
\qquad w_o\leftarrow\frac{w_o'}{\sum_jw_j'}.
\]

Thus, if character truncation repeatedly produces positive deltas, its weight
increases while every operator retains a 0.05 exploration floor. Cap any weight
at 0.60, renormalize, and freeze adaptation when feedback is missing, malformed,
or derived from fewer than eight trials. Record old/new weights and evidence in
the workflow ledger.

## Selection and stopping

Use elitism to retain the highest-fitness representative of each unique path,
then weighted tournament selection with the current operator matrix. Seed every
choice from the approved cycle seed. Stop at fitness 100, the generation or
execution budget, timeout, operator cancellation, or policy violation. Publish
only normalized feedback, payload hex plus SHA-256 for simulated findings, and
defensive reproduction metadata.

