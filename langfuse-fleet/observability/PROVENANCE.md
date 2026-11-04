# Provenance — F3 scripts imported into the plugin source

The F3 canary scripts lived only in the datastore (`/opt/hermes-agent-data/fleet-canary/`),
outside any git repository. They are copied here verbatim, as the source of truth, before
any F4 work builds on them. The live originals stay untouched: they are the F3 evidence.

| File here | Origin | SHA-256 (copy == origin) |
|---|---|---|
| `f3_canary_driver.py` | `/opt/hermes-agent-data/fleet-canary/canary_driver.py` | `9ab2b18504e15f56c6533902cd18b0d9d432b0ad575336b2290b6711ace99f71` |
| `f3_readback.py` | `/opt/hermes-agent-data/fleet-canary/readback.py` | `0b4c7ed572091672bfc0ebddfd9b0a13d402fb14fd2081aa562e5b60c932a4c0` |

Verification command (hashes must match pairwise):

```
sha256sum observability/f3_canary_driver.py observability/f3_readback.py \
  /opt/hermes-agent-data/fleet-canary/canary_driver.py \
  /opt/hermes-agent-data/fleet-canary/readback.py
```

`f4_observe.py` is a rewrite of `f4_sample.py` (the datastore sampler), not a copy: the
sample schema changed and the verdict became baseline-delta based. `f4_sample.py` stays in
the datastore as the F3-era draft.
