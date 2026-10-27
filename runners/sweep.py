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
A set listed twice runs once, and an empty extra_args counts as none.

MaxQuant cannot be swept: its sweep section is ignored with a warning (see
NO_SWEEP_TOOLS).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shlex

# Tools whose sweep section is ignored. MaxQuant 2.8+ writes its results into
# <raw folder>/combined/, which every MaxQuant job on that dataset shares, and
# only moves them into the job's own folder afterwards (MaxQuantRunner.
# post_run_hook). Sweep sets running side by side would mix their results.
NO_SWEEP_TOOLS = {
    "maxquant": "MaxQuant writes its results to a combined/ folder shared by every "
                "run on a dataset, so sweep sets running in parallel would mix them.",
}


def sweep_sets(tool_cfg: dict) -> list[dict]:
    """The distinct parameter sets of a tool's `sweep:` section ([] when there is
    none). Duplicates would share an output folder, so each set is kept once."""
    sweep = tool_cfg.get("sweep")
    if not sweep:
        return []
    if isinstance(sweep, list):
        sets = [dict(s) for s in sweep]
    else:
        keys = list(sweep)
        values = [v if isinstance(v, list) else [v] for v in sweep.values()]
        sets = [dict(zip(keys, combo)) for combo in itertools.product(*values)]
    unique: dict[str, dict] = {}
    for params in sets:
        if not params.get("extra_args"):
            params.pop("extra_args", None)
        unique.setdefault(sweep_id(params), params)
    return list(unique.values())


def sweep_id(params: dict) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:8]


def _args(raw) -> list[str]:
    if not raw:
        return []
    return [str(a) for a in raw] if isinstance(raw, (list, tuple)) else shlex.split(str(raw))


def unsupported_sweep(tool_name: str, tool_cfg: dict) -> str | None:
    """A warning when the tool has a sweep section that expand_versions ignores."""
    if tool_cfg.get("sweep") and tool_name in NO_SWEEP_TOOLS:
        return (f"tools > {tool_name} > sweep is ignored: sweeping {tool_name} is not supported. "
                f"{NO_SWEEP_TOOLS[tool_name]} Running its versions without the sweep.")
    return None


def expand_versions(tool_cfg: dict, tool_name: str) -> list[dict]:
    """The tool's version entries, with one entry per sweep set when it has a sweep."""
    versions = tool_cfg.get("versions") or []
    sets = [] if tool_name in NO_SWEEP_TOOLS else sweep_sets(tool_cfg)
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
