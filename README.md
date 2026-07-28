# ProteoBench Pipeline

This pipeline runs multiple proteomics search engines on ProteoBench benchmark datasets and collects output files for downstream submission to [ProteoBench](https://proteobench.cubimed.rub.de/). It supports DIA-NN, AlphaDIA, Sage, FragPipe, MaxQuant, and MetaMorpheus across DDA and DIA acquisition modes.

Two equivalent runners are provided:

| Runner | Entry point | Parallelism | Best for |
|--------|------------|-------------|---------|
| Nextflow | `proteobench.nf` | Nextflow executor (local, SLURM, …) | **Recommended** — cluster runs, reproducibility, resumable, sets itself up automatically |
| Python | `run_proteobench.py` | `ThreadPoolExecutor` on one machine | Interactive use, dry-run preview |

Both runners use the same `config.yaml`, the same per-tool parameter logic, and produce the same output directory layout and summary TSV. `proteobench.nf` is the simpler of the two to get started with: it checks its own docker setup and runs the setup wizard itself when needed, so `nextflow run proteobench.nf` is the only command most users ever need to type. The Python runner is a thinner, non-Nextflow alternative for local/interactive use; it does not run the setup wizard for you, so run `nextflow run setup.nf` once yourself before using it.

---

## Prerequisites

**Docker is required.** Every search engine (DIA-NN, AlphaDIA, Sage, FragPipe, MaxQuant, MetaMorpheus) now runs from a docker image — there is nothing left to compile or install natively for any tool. Install Docker before doing anything else:

| Dependency | Required for | Install |
|------------|-------------|---------|
| **Docker** | **every tool — mandatory** | [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) |
| Python 3.11+ | Both runners | `conda install python=3.11` or [python.org](https://www.python.org/downloads/) |
| Nextflow 23.10+ | Nextflow runner + docker setup wizard | `curl -s https://get.nextflow.io \| bash` then move to a directory on `$PATH` |

Check that each dependency is available:
```bash
docker info            # should print server info, not a connection error
python --version       # should print 3.11 or higher
nextflow -version      # needed for setup.nf and the Nextflow runner
```

Your user must be able to run `docker` without `sudo` (on Linux: `sudo usermod -aG docker $USER`, then log out/in).

---

## Quick start (Nextflow runner — recommended)

```bash
nextflow run proteobench.nf
```

That's it — the pipeline sets itself up on the way in:

- **No `config.yaml` yet?** It runs the interactive docker setup wizard first: for each tool, a yes/no prompt to pull its image, then writes straight to `config.yaml` and tells you which `CHANGE_ME` dataset paths are left to fill in. Re-run the same command once you've edited those.
- **`config.yaml` exists and looks complete** (every enabled tool's docker image — and, for FragPipe, its licensed JARs — is actually present)? Setup is skipped entirely; it goes straight to running jobs.
- **`config.yaml` exists but something's missing** (e.g. an image got removed, or a FragPipe JAR went missing)? Setup runs again but only for the tools that are actually incomplete — already-complete tools are kept untouched and tools you never configured are left alone, so you are only prompted for the parts that need redoing. It then updates `config.yaml` in place (keeping a `config.yaml.bak` copy), preserving your `global`/`search_params`/`datasets` and every complete tool verbatim.

Setup-wizard details per tool:

- **MaxQuant, Sage, MetaMorpheus, AlphaDIA** — a yes/no prompt each; a plain `docker pull` if you say yes.
- **FragPipe** — the `fcyucn/fragpipe` image does **not** include MSFragger, IonQuant, or diaTracer (Nesvilab Academic License, separate from FragPipe's own license). For each of the three, the wizard asks whether you already have it downloaded as a `.zip` or extracted folder; if not, it prints the download URL and lets you skip — FragPipe is written to the config but stays `enabled: false` until all three are present. Re-run to add them later. When you point the wizard at an MSFragger folder, it also copies the `ext/` folder shipped next to the jar (the Thermo `.raw` and Bruker `.d` native readers, run under the mono runtime already in the image) and mounts it at run time, so FragPipe reads `.raw`/`.d` directly; if `ext/` is missing, FragPipe will need mzML input instead. FragPipe also needs decoys already appended to the FASTA (unlike the other tools); if `fasta_decoy:` isn't set for a dataset, one is generated automatically the first time that dataset is searched, via the Philosopher CLI already bundled in the image (the same command the FragPipe GUI's "Add decoys" button runs), and cached next to the source FASTA for reuse.
- **DIA-NN** — always pulls the free `biocontainers/diann:v1.8.1_cv1` image. It also asks if you have a GitHub account to pull the newer 2.x images from `ghcr.io/bigbio/diann` (see [quantms containers](https://quantmsdiann.quantms.org/containers/)), which are private and need a **GitHub personal access token** with the `read:packages` scope (create one at [github.com/settings/tokens](https://github.com/settings/tokens)). If you decline, only 1.8.1 is configured. Your username/token are saved to a git-ignored `.env` file, not written into any config.
- **Datasets** — after the tools above are set up, the wizard offers to download benchmark datasets from `nextflow/datasets_catalog.yaml`, scoped to only the datasets relevant to the tools you just enabled (by DDA/DIA acquisition). It asks where to store them (default: `data/`, git-ignored), lists the relevant datasets, and lets you pick `all`, `none`, or specific ones by number. Each dataset is downloaded as a zip and unzipped; the FASTA (and decoy, if present) inside it are detected automatically, and an `mzml/` subfolder is split out into a sibling `<name>_mzml` dataset entry. Real, resolved paths are written into `datasets:`, and each configured tool's `datasets:` list is filled in automatically — no more `CHANGE_ME` for anything that was downloaded. Datasets already present on disk from an earlier run are reused, not re-downloaded. See "Adding a downloadable dataset" below.

Non-interactive / CI use (skips all prompts, uses these flags instead):
```bash
nextflow run proteobench.nf --non_interactive --skip_fragpipe \
    --diann_user YOUR_GH_USER --diann_token YOUR_GH_TOKEN \
    --download_datasets all --data_dir /path/to/data
```
`--skip_datasets` skips the dataset step entirely; `--download_datasets` also accepts a comma-separated list of dataset names instead of `all`. With no `--download_datasets` given, non-interactive mode downloads nothing (safe default for CI).

To force the wizard to run again regardless of completeness (e.g. to add a tool you skipped), run it directly instead of through `proteobench.nf`:
```bash
nextflow run setup.nf
```

The pipeline reads `config.yaml` from the project root by default. It automatically picks up `global.output_dir` and `global.max_parallel_jobs` from that file.

### Common options

| Flag | Equivalent Python flag | Description |
|------|----------------------|-------------|
| `--config /path/to/config.yaml` | `--config` | Path to config file (default: `config.yaml` next to `proteobench.nf`) |
| `--tool diann` | `--tool` | Restrict run to one tool |
| `--dataset Entrapment_DIA` | `--dataset` | Restrict run to one dataset |
| `--no_preflight` | `--no-preflight` | Skip preflight checks before each job |
| `--max_parallel_jobs 4` | `--max-parallel-jobs` | Override concurrency (default comes from `config.yaml`) |

Example — run only DIA-NN jobs, skip preflight:

```bash
nextflow run proteobench.nf --tool diann --no_preflight
```

Example — run against a custom config and limit concurrency:

```bash
nextflow run proteobench.nf --config /data/my_config.yaml --max_parallel_jobs 2
```

### Resuming interrupted runs

Nextflow caches each completed job in the `work/` directory. If a run is interrupted, resume it without re-running successful jobs:

```bash
nextflow run proteobench.nf -resume
```

Jobs that already have a `.done` marker in the output directory are also skipped by the runner logic itself, so both layers protect against redundant work.

### Output

The Nextflow runner writes results to the same `global.output_dir` as the Python runner. The summary file is named `run_summary_nf.tsv` (vs `run_summary_<timestamp>.tsv` for the Python runner). Both files use identical columns: `tool`, `version`, `dataset`, `success`, `skipped`, `runtime_s`, `output_dir`, `error_msg`.

Nextflow task working directories are placed under `<output_dir>/nf_work/`. To delete them after a successful run:

```bash
nextflow clean -f
```

To also redirect the Nextflow log into the results directory, pass `-log` on the command line:

```bash
nextflow run proteobench.nf -log /path/to/results/.nextflow.log
```

### Cluster / HPC execution

To run on SLURM (or another executor), extend `nextflow.config` (which already exists in the project root) with executor settings:

```groovy
// append to nextflow.config
process.executor      = 'slurm'
process.queue         = 'gpu'
process.clusterOptions = '--mem=64G --time=04:00:00'
```

See the [Nextflow executor documentation](https://www.nextflow.io/docs/latest/executor.html) for other executors (PBS, LSF, Kubernetes, etc.). No changes to `proteobench.nf` itself are needed.

---

## Quick start (Python runner)

The Python runner does not run the setup wizard itself — run it once via Nextflow first:

```bash
# Option A: conda
conda env create -f environment.yml
conda activate proteobench-pipeline
# Option B: pip
pip install -r requirements.txt

nextflow run setup.nf       # interactive; writes config.yaml directly on a first run
```

If `config.yaml` didn't exist yet, `setup.nf` writes it directly and tells you which dataset `CHANGE_ME` paths to fill in. If it already existed, `setup.nf` only redoes the tools whose docker setup is incomplete and updates `config.yaml` in place, preserving everything else and saving the previous version as `config.yaml.bak`.

Then:

```bash
# See which tools and datasets are configured
python run_proteobench.py --list-tools
python run_proteobench.py --list-datasets

# Preview all planned jobs and their full CLI commands (nothing is executed)
python run_proteobench.py --dry-run

# Execute all enabled jobs
python run_proteobench.py
```

---

## Tool overview

| Tool | Acquisition | Input format | Docker image |
|------|-------------|-------------|----------------|
| DIA-NN | DDA (v2.1+), DIA | raw, mzML | `biocontainers/diann:v1.8.1_cv1` (public) or `ghcr.io/bigbio/diann:2.x` (GitHub token) |
| AlphaDIA | DIA | raw, mzML, .d | `mannlabs/alphadia:latest` |
| Sage | DDA | mzML, MGF | `ghcr.io/lazear/sage:latest` |
| FragPipe | DDA, DIA | raw, mzML, .d | `fcyucn/fragpipe:latest` + licensed MSFragger/IonQuant/diaTracer JARs |
| MaxQuant | DDA, DIA | raw | `quay.io/medbioinf/maxquant:2.6.3.0`, `quay.io/medbioinf/maxquant:2.8.1.0` |
| MetaMorpheus | DDA | raw, mzML | `smithchemwisc/metamorpheus:latest` |

All six images are pulled by the setup wizard (run automatically by `nextflow run proteobench.nf`, or directly via `nextflow run setup.nf`) — see [Quick start](#quick-start-nextflow-runner--recommended) above. There is no native/manual install path any more; every tool runs from its image.

---

## Adding a docker image tag setup.nf didn't pull

`setup.nf` always pulls `:latest` (or, for DIA-NN, whichever versions you chose). To pin or add a different tag, edit `config.yaml` directly — add a new entry under that tool's `versions:` list with the new `image:` tag and `enabled: true`. Any `*_bin`/`*_dir`/`fragpipe_root` path may need updating too if the new image version changes its internal layout; `docker run --rm --entrypoint find <image> / -maxdepth 4 -iname <binary-name>` (as `setup.nf` does internally) will locate it.

---

## Enabling and disabling tools

Each tool version has an `enabled` flag in `config.yaml`. Setting it to `false` skips that version without removing its configuration:

```yaml
tools:
  diann:
    versions:
      - id: "2.5.0"
        image: ghcr.io/bigbio/diann:2.5.0
        diann_bin: /usr/diann/2.5.0/diann
        enabled: true    # ← run this version
      - id: "1.8.1"
        image: biocontainers/diann:v1.8.1_cv1
        diann_bin: /usr/diann/1.8.1/diann
        enabled: false   # ← skip this version
```

---

## Adding a new dataset

Add a block under `datasets:` in `config.yaml`, then add the dataset name to the `datasets:` list of each tool that should run on it:

```yaml
datasets:
  My_New_Dataset:
    path: /data/my_experiment          # directory containing the MS files
    acquisition: DIA                   # DDA or DIA
    format: raw                        # raw | mzml | d | wiff | mgf
    instrument: Orbitrap               # Orbitrap | Astral | timstof | ZenoTOF
    fasta: /data/fastas/human.fasta
    fasta_decoy: /data/fastas/human_decoy.fasta   # optional; FragPipe generates one automatically if omitted

tools:
  diann:
    datasets:
      - Entrapment_DIA
      - My_New_Dataset    # ← add here
```

---

## Adding a downloadable dataset

To let `nextflow run setup.nf` download a dataset automatically instead of a user pointing `path:`/`fasta:` at existing files by hand, add an entry to `nextflow/datasets_catalog.yaml` with a real download URL:

```yaml
My_New_Dataset:
  url: https://example.org/path/to/My_New_Dataset.zip
  acquisition: DIA          # DDA or DIA
  format: raw                # raw | mzml | d | wiff | mgf
  instrument: Orbitrap       # Orbitrap | Astral | timstof | ZenoTOF
```

The zip is expected to contain, at its top level: the MS files, exactly one `*.fasta` (+ optionally one decoy fasta with `decoy` in the name), and optionally an `mzml/` subfolder. Re-run `nextflow run setup.nf` — it offers the new dataset (scoped to tools it's relevant to), downloads and unzips it, and writes the resolved `path:`/`fasta:` into `config.yaml` automatically.

---

## Adding a new tool version

Copy an existing version block, update `id` and the binary path, and set `enabled: true`:

```yaml
tools:
  diann:
    versions:
      - id: "2.6.0"                              # new version
        binary: /opt/diann-2.6.0/diann-linux     # path to the binary
        supports_dda: true
        enabled: true
```

---

## Output structure

Each tool run creates a subdirectory under `output_dir`:

```
results/
└── Entrapment_DIA/
    ├── diann_v2.5.0/
    │   ├── report.tsv        ← DIA-NN result file
    │   ├── stdout.log
    │   ├── stderr.log
    │   └── .done             ← marker: job succeeded; delete to re-run
    ├── alphadia_v2.1.1/
    │   └── ...
    └── run_summary_20260603_120000.tsv   ← summary of all runs
```

The `.done` marker causes the pipeline to skip that job on the next run (useful for resuming after interruption). Set `overwrite: true` in `config.yaml` to force all jobs to re-run.

---

## Troubleshooting

| Error message | Likely cause | Fix |
|---------------|-------------|-----|
| `Config file not found` (Python runner only; `proteobench.nf` runs setup itself) | `config.yaml` does not exist | `nextflow run setup.nf` |
| `still contains 'CHANGE_ME'` | Path not updated in config | Edit `config.yaml` and replace CHANGE_ME |
| `docker: command not found` / `Cannot connect to the Docker daemon` | Docker not installed, not running, or user lacks permission | Install Docker, start the daemon, add your user to the `docker` group |
| `docker image not pulled locally` | Image not pulled yet | `nextflow run setup.nf` |
| `No 'raw' files found` | Wrong `format:` or wrong `path:` | Verify files exist and `format:` matches |
| `MSFragger/IonQuant/diaTracer JAR not found in jars_dir` | Licensed FragPipe JAR missing | `nextflow run setup.nf` and provide the downloaded zip/folder when asked |
| `'ext/thermo' folder was not found next to MSFragger jar` | MSFragger's native readers weren't copied (pointed the wizard at a bare `.jar`, not its folder) | Re-run `nextflow run setup.nf` and point at the MSFragger *folder* (which has `ext/`), or switch that dataset to mzML input |
| `build_command failed: Philosopher decoy generation failed` | FASTA's directory isn't writable, or the FASTA is malformed | Check permissions on the FASTA's directory; or set `fasta_decoy:` explicitly to a pre-built one |
| `no download URL set in nextflow/datasets_catalog.yaml` | Catalog entry still has `url: CHANGE_ME` | Add the real URL to `nextflow/datasets_catalog.yaml`, then re-run `nextflow run setup.nf` |
| Dataset download skipped in CI | Non-interactive mode with no `--download_datasets` (safe default) | Pass `--download_datasets all` or a comma-separated list |
| `exit code 1 — check log: ...` | Tool crashed during search | Open the `stderr.log` file shown in the error |
| Job is skipped unexpectedly | `.done` marker exists | Delete the `.done` file in the output directory, or set `overwrite: true` |
| `No enabled jobs found` | All versions have `enabled: false` | Set `enabled: true` for at least one version in `config.yaml` |
| `enumerate_jobs.py failed` (Nextflow) | Python or YAML not on PATH | Run Nextflow from the activated conda/pip env; check `python3 --version` |
| `No module named 'yaml'` (Nextflow) | pyyaml not installed | `pip install pyyaml` |
| Nextflow process hangs | `max_parallel_jobs` too high for available cores/RAM | Lower `global.max_parallel_jobs` in `config.yaml` or pass `--max_parallel_jobs N` |
