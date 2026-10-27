"""Parameter sweeps: run every enabled version of a tool once per parameter set.

A tool block may hold a `sweep:` section. Its keys are search_params keys,
plus `extra_args` for flags the pipeline does not model. As a mapping, every
combination of the value lists is run (a grid); as a list, each entry is one
hand-picked parameter set:

    tools:
      diann:
        sweep:                                  # grid: 2 x 2 = 4 sets
          fragment_mass_tolerance_ppm: [10, 15]
          extra_args: ["", "--window 8"]

        sweep:                                  # or: explicit sets
          - {fragment_mass_tolerance_ppm: 10}
          - {fragment_mass_tolerance_ppm: 15, match_between_runs: false}

Each set becomes its own version entry with id "<version>-<hash>", so it gets
its own output folder (<tool>_v<version>-<hash>) and its own summary row. The
hash depends only on the parameter set: the same set has the same hash for
every version and on every re-run. sweep.json in the output folder records it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shlex


def sweep_sets(tool_cfg: dict) -> list[dict]:
    """The parameter sets of a tool's `sweep:` section ([] when there is none)."""
    sweep = tool_cfg.get("sweep")
    if not sweep:
        return []
    if isinstance(sweep, list):
        return [dict(s) for s in sweep]
    keys = list(sweep)
    values = [v if isinstance(v, list) else [v] for v in sweep.values()]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def sweep_id(params: dict) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:8]


def _args(raw) -> list[str]:
    if not raw:
        return []
    return [str(a) for a in raw] if isinstance(raw, (list, tuple)) else shlex.split(str(raw))


def expand_versions(tool_cfg: dict) -> list[dict]:
    """The tool's version entries, with one entry per sweep set when it has a sweep."""
    versions = tool_cfg.get("versions") or []
    sets = sweep_sets(tool_cfg)
    if not sets:
        return versions

    expanded = []
    for ver in versions:
        for params in sets:
            overrides = {k: v for k, v in params.items() if k != "extra_args"}
            expanded.append({
                **ver,
                "id": f"{ver.get('id')}-{sweep_id(params)}",
                "search_params": {**(ver.get("search_params") or {}), **overrides},
                "extra_args": _args(ver.get("extra_args")) + _args(params.get("extra_args")),
                "sweep": params,
            })
    return expanded
