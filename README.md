# ProteoBench Pipeline

This pipeline runs multiple proteomics search engines on ProteoBench benchmark datasets and collects output files for downstream submission to [ProteoBench](https://proteobench.cubimed.rub.de/). It supports DIA-NN, AlphaDIA, Sage, FragPipe, MaxQuant, and MetaMorpheus across DDA and DIA acquisition modes.

Two equivalent runners are provided:

| Runner | Entry point | Parallelism | Best for |
|--------|------------|-------------|---------|
| Python | `run_proteobench.py` | `ThreadPoolExecutor` on one machine | Interactive use, dry-run preview |
| Nextflow | `proteobench.nf` | Nextflow executor (local, SLURM, …) | Cluster runs, reproducibility, resumable |

Both runners use the same `config.yaml`, the same per-tool parameter logic, and produce the same output directory layout and summary TSV.

---

## Prerequisites

Install the following system dependencies before running the pipeline.

| Dependency | Required for | Install |
|------------|-------------|---------|
| Python 3.11+ | Both runners | `conda install python=3.11` or [python.org](https://www.python.org/downloads/) |
| .NET 8 runtime | MaxQuant, MetaMorpheus | `apt install dotnet-runtime-8.0` (Linux) or `winget install Microsoft.DotNet.Runtime.8` (Windows) |
| Java 11+ | FragPipe | `apt install default-jre` or `conda install -c conda-forge openjdk` |
| Rust / cargo | Sage (compilation) | `curl https://sh.rustup.rs -sSf \| sh` |
| Nextflow 23.10+ | Nextflow runner only | `curl -s https://get.nextflow.io \| bash` then move to a directory on `$PATH` |

Check that each dependency is available:
```bash
python --version       # should print 3.11 or higher
dotnet --version       # needed for MaxQuant / MetaMorpheus
java -version          # needed for FragPipe
cargo --version        # needed for Sage
nextflow -version      # needed for the Nextflow runner
```

---

## Quick start (Python runner)

### Step 1 — Install Python dependencies

```bash
# Option A: conda 
conda env create -f environment.yml
conda activate proteobench-pipeline

# Option B: pip (recommended)
pip install -r requirements.txt
```

### Step 2 — Configure the pipeline

```bash
cp config.template.yaml config.yaml
```

Open `config.yaml` in a text editor and replace every `CHANGE_ME` with a real path. The template contains inline comments explaining each field. The minimum changes required:

- `global.output_dir` — directory where results will be written
- `global.dotnet` — path to the .NET runtime (or `dotnet` if it is on your PATH)
- Paths to each tool's binary/directory under `tools`
- Enable at least one tool version by setting `enabled: true`

### Step 3 — Download third-party tool binaries

Run the setup script to download MSFragger, IonQuant, DiaTracer, and DIA-NN automatically:

```bash
python setup.py          # interactive, will ask about the MSFragger license
```

For non-interactive / CI use:
```bash
python setup.py --accept-license          # download everything (MSFragger license accepted)
python setup.py --download-diann          # DIA-NN only (no license needed)
python setup.py --sage-only               # compile Sage from source (requires cargo)
python setup.py --diatracer-only          # DiaTracer only
```

Check what is installed:
```bash
python setup.py --check
```

### Step 4 — Preview and run

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

## Quick start (Nextflow runner)

Steps 1–3 above (install deps, configure, download binaries) are identical. Once that is done:

### Run the pipeline

```bash
nextflow run proteobench.nf
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

Nextflow also writes job working directories under `work/` in the project root. These can be deleted with `nextflow clean` once the run is complete.

### Cluster / HPC execution

To run on SLURM (or another executor), add a `nextflow.config` file alongside `proteobench.nf`:

```groovy
process.executor = 'slurm'
process.queue    = 'gpu'
process.clusterOptions = '--mem=64G --time=04:00:00'
```

See the [Nextflow executor documentation](https://www.nextflow.io/docs/latest/executor.html) for other executors (PBS, LSF, Kubernetes, etc.). No changes to `proteobench.nf` itself are needed.

---

## Tool overview

| Tool | Acquisition | Input format | Install method |
|------|-------------|-------------|----------------|
| DIA-NN | DDA (v2.1+), DIA | raw, mzML | `python setup.py --download-diann` |
| AlphaDIA | DIA | raw, mzML, .d | `pip install alphadia` |
| Sage | DDA | mzML, MGF | `python setup.py --sage-only` (needs cargo) |
| FragPipe | DDA, DIA | raw, mzML, .d | GUI installer + `python setup.py --accept-license` |
| MaxQuant | DDA, DIA | raw | Download from maxquant.org |
| MetaMorpheus | DDA | raw, mzML | Download from GitHub |

---

## Manually installing tools not handled by setup.py

### AlphaDIA

```bash
pip install alphadia
which alphadia   # copy this path into config.yaml > tools > alphadia > versions > command
```

### MaxQuant

1. Download the `.zip` from [maxquant.org](https://www.maxquant.org/)
2. Extract to a directory, e.g. `/opt/MaxQuant_v2.8.0.0`
3. Set `dir: /opt/MaxQuant_v2.8.0.0` in `config.yaml` under `tools > maxquant > versions`
4. Set `dotnet:` under `global` to your .NET 8 runtime path

### MetaMorpheus

1. Download the `.zip` from [GitHub releases](https://github.com/smith-chem-wisc/MetaMorpheus/releases)
2. Extract to a directory, e.g. `/opt/MetaMorpheus`
3. Set `dir: /opt/MetaMorpheus` in `config.yaml` under `tools > metamorpheus > versions`

### FragPipe

1. Download from [FragPipe GitHub releases](https://github.com/Nesvilab/FragPipe/releases)
2. Extract to e.g. `/opt/fragpipe-24.0`
3. Set `dir: /opt/fragpipe-24.0` in `config.yaml`
4. Run `python setup.py --accept-license` to download MSFragger, IonQuant, and DiaTracer

---

## Enabling and disabling tools

Each tool version has an `enabled` flag in `config.yaml`. Setting it to `false` skips that version without removing its configuration:

```yaml
tools:
  diann:
    versions:
      - id: "2.5.0"
        binary: /opt/diann-2.5.0/diann-linux
        enabled: true    # ← run this version
      - id: "1.9.2"
        binary: /opt/diann-1.9.2/diann-linux
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
    fasta_decoy: /data/fastas/human_decoy.fasta   # only needed for FragPipe

tools:
  diann:
    datasets:
      - Entrapment_DIA
      - My_New_Dataset    # ← add here
```

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
| `Config file not found` | `config.yaml` does not exist | `cp config.template.yaml config.yaml` |
| `still contains 'CHANGE_ME'` | Path not updated in config | Edit `config.yaml` and replace CHANGE_ME |
| `binary not found: ...` | Tool not installed or wrong path | Check the path; run `python setup.py --check` |
| `No 'raw' files found` | Wrong `format:` or wrong `path:` | Verify files exist and `format:` matches |
| `MSFragger JAR not found` | MSFragger not downloaded | `python setup.py --accept-license --msfragger-only` |
| `exit code 1 — check log: ...` | Tool crashed during search | Open the `stderr.log` file shown in the error |
| Job is skipped unexpectedly | `.done` marker exists | Delete the `.done` file in the output directory, or set `overwrite: true` |
| `No enabled jobs found` | All versions have `enabled: false` | Set `enabled: true` for at least one version in `config.yaml` |
| `enumerate_jobs.py failed` (Nextflow) | Python or YAML not on PATH | Run Nextflow from the activated conda/pip env; check `python3 --version` |
| `No module named 'yaml'` (Nextflow) | pyyaml not installed | `pip install pyyaml` |
| Nextflow process hangs | `max_parallel_jobs` too high for available cores/RAM | Lower `global.max_parallel_jobs` in `config.yaml` or pass `--max_parallel_jobs N` |
