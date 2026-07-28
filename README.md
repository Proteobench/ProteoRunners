# ProteoBench Runners Pipeline

This pipeline runs multiple proteomics search engines on ProteoBench benchmark datasets and collects output files for downstream submission to [ProteoBench](https://proteobench.cubimed.rub.de/). It supports DIA-NN, AlphaDIA, Sage, FragPipe, MaxQuant, and MetaMorpheus across DDA and DIA acquisition modes.

Everything runs through Nextflow (`proteobench.nf`), on a local machine or on a cluster (SLURM, …): it checks its own docker setup and runs the setup wizard itself when needed. Pull a tagged release directly from GitHub — no `git clone` needed — and `nextflow run ProteoBench/ProteoRunners -r v1.0.0` is the only command most users ever have to type.

---

## Prerequisites

**Docker is required.** Every search engine (DIA-NN, AlphaDIA, Sage, FragPipe, MaxQuant, MetaMorpheus) now runs from a docker image, there is nothing left to compile or install natively for any tool.

| Dependency | Required for | Install |
|------------|-------------|---------|
| **Docker** | **every tool — mandatory** | [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) |
| Python 3.11+ | Job enumeration used internally by the pipeline | `conda install python=3.11` or [python.org](https://www.python.org/downloads/) |
| Nextflow 23.10+ | Running the pipeline and setup wizard | `curl -s https://get.nextflow.io \| bash` then move to a directory on `$PATH` |
| `git` | Building the DIA-NN 2.x image only | Usually already installed; see [git-scm.com](https://git-scm.com/downloads) |

Check that each dependency is available:
```bash
docker info            # should print server info, not a connection error
python --version       # should print 3.11 or higher
nextflow -version      # needed for setup.nf and the Nextflow runner
```

Your user must be able to run `docker` without `sudo` (on Linux: `sudo usermod -aG docker $USER`, then log out/in).

---

## Quick start

Run the latest release directly from GitHub:

```bash
nextflow run ProteoBench/ProteoRunners -r v1.0.0 --config ./config.yaml
```

Nextflow caches the pipeline code itself under `~/.nextflow/assets/ProteoBench/ProteoRunners`, not your current directory. `config.yaml`, downloaded datasets, and results all default to living next to the pipeline code — i.e. inside that shared cache, not your project — so when running a pulled release, always pass `--config` (as above) and, once you're producing output, `--publish_dir` and `--data_dir` too, pointing them at paths in your own working directory.

Developing the pipeline itself? Clone the repo and run it from inside your checkout instead — then these defaults resolve to the repo root, matching every other example in this README:

```bash
git clone https://github.com/ProteoBench/ProteoRunners.git
cd ProteoRunners
nextflow run proteobench.nf
```

That's it — the pipeline sets itself up on the way in:

- **No `config.yaml` yet?** It runs the interactive docker setup wizard first: for each tool, a yes/no prompt to pull its image, then writes straight to `config.yaml` and tells you which `CHANGE_ME` dataset paths are left to fill in. Re-run the same command once you've edited those.
- **`config.yaml` exists and looks complete** (every enabled tool's docker image — and, for FragPipe, its licensed JARs — is actually present)? Setup is skipped entirely; it goes straight to running jobs.
- **`config.yaml` exists but something's missing** (e.g. an image got removed, or a FragPipe JAR went missing)? Setup runs again but only for the tools that are actually incomplete. Already-complete tools are kept untouched and tools you never configured are left alone, so you are only prompted for the parts that need redoing. It then updates `config.yaml` in place (keeping a `config.yaml.bak` copy), preserving your `global`/`search_params`/`datasets` and every complete tool verbatim.

Setup-wizard details per tool:

- **MaxQuant, Sage, MetaMorpheus, AlphaDIA** — a yes/no prompt each; a plain `docker pull` if you say yes.
- **FragPipe** — the `fcyucn/fragpipe` image does **not** include MSFragger, IonQuant, or diaTracer (Nesvilab Academic License, separate from FragPipe's own license). For each of the three, the wizard asks whether you already have it downloaded as a `.zip` or extracted folder; if not, it prints the download URL and lets you skip — FragPipe is written to the config but stays `enabled: false` until all three are present. Re-run to add them later. When you point the wizard at an MSFragger folder, it also copies the `ext/` folder shipped next to the jar (the Thermo `.raw` and Bruker `.d` native readers, run under the mono runtime already in the image) and mounts it at run time, so FragPipe reads `.raw`/`.d` directly; if `ext/` is missing, FragPipe will need mzML input instead. FragPipe also needs decoys already appended to the FASTA (unlike the other tools); if `fasta_decoy:` isn't set for a dataset, one is generated automatically the first time that dataset is searched, via the Philosopher CLI already bundled in the image (the same command the FragPipe GUI's "Add decoys" button runs), and cached next to the source FASTA for reuse.
- **DIA-NN** — always pulls the free `biocontainers/diann:v1.8.1_cv1` image. It also asks whether to build a DIA-NN 2.x image (needed for DDA support and native Thermo `.raw` reading on Linux); if you say yes, it `git clone`s [bigbio/quantms-containers](https://github.com/bigbio/quantms-containers) and runs `docker build` locally — DIA-NN itself is downloaded from the public [vdemichev/DiaNN](https://github.com/vdemichev/DiaNN) releases during the build, so no registry account or token is needed (requires `git` and takes a few minutes). If you decline, only 1.8.1 is configured.
- **Datasets** — after the tools above are set up, the wizard offers to download benchmark datasets from `nextflow/datasets_catalog.yaml`, scoped to only the datasets relevant to the tools you just enabled (by DDA/DIA acquisition). It asks where to store them (default: `data/`, git-ignored), lists the relevant datasets, and lets you pick `all`, `none`, or specific ones by number. Each dataset is downloaded as a zip and unzipped; the FASTA (and decoy, if present) inside it are detected automatically, and an `mzml/` subfolder is moved to a sibling `<name>_mzml` directory — not a separate dataset entry, but a fallback location tools automatically use whenever they can't read the dataset's native format and need mzML instead (e.g. Sage always; DIA-NN < 2.0 for Thermo `.raw`). Real, resolved paths are written into `datasets:`, and each configured tool's `datasets:` list is filled in automatically — no more `CHANGE_ME` for anything that was downloaded. Datasets already present on disk from an earlier run are reused, not re-downloaded. See "Adding a downloadable dataset" below.

Non-interactive / CI use (skips all prompts, uses these flags instead):
```bash
nextflow run proteobench.nf --non_interactive --skip_fragpipe \
    --build_diann_v2 --diann_version 2.5.0 \
    --download_datasets all --data_dir /path/to/data
```
`--skip_datasets` skips the dataset step entirely; `--download_datasets` also accepts a comma-separated list of dataset names instead of `all`. With no `--download_datasets` given, non-interactive mode downloads nothing (safe default for CI).

Available dataset names (source of truth: [`nextflow/datasets_catalog.yaml`](nextflow/datasets_catalog.yaml)):

| Name | Acquisition | Format | Instrument |
|------|------|------|------|
| `HYE_DDA_Orbitrap` | DDA | raw | Orbitrap |
| `HYE_DDA_Astral` | DDA | raw | Astral |
| `HYE_Astral` | DIA | raw | Astral |
| `HYE_Astral_Single_Cell` | DIA | raw | Astral |
| `HYE_AIF` | DIA | raw | Orbitrap |
| `HYE_diaPASEF` | DIA | d | timstof |
| `HYE_ZenoSWATH` | DIA | wiff | ZenoTOF |
| `PYE_diaPASEF` | DIA | d | timstof |
| `Entrapment_DIA` | DIA | raw | Orbitrap |

To force the wizard to run again regardless of completeness (e.g. to add a tool you skipped), run it directly instead of through `proteobench.nf`:
```bash
nextflow run setup.nf
```

The pipeline reads `config.yaml` from the project root by default. Each job writes its actual result files under `global.output_dir` from that file. Nextflow's own concurrency (`maxForks`) and the location it publishes `run_summary_nf.tsv` to are separate from that: they default to 6 and `./results` respectively and are not read from `config.yaml` automatically — override them with `--max_parallel_jobs` / `--publish_dir`, or by adding a `nextflow.config` (see [Cluster / HPC execution](#cluster--hpc-execution)).

### Common options

| Flag | Description |
|------|-------------|
| `--config /path/to/config.yaml` | Path to config file (default: `config.yaml` next to `proteobench.nf`) |
| `--tool diann` | Restrict run to one tool |
| `--dataset Entrapment_DIA` | Restrict run to one dataset |
| `--no_preflight` | Skip preflight checks before each job |
| `--max_parallel_jobs 4` | Override Nextflow concurrency (default: 6) |
| `--publish_dir /path` | Where `run_summary_nf.tsv` is published (default: `./results`) |

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

Each job's actual result files are written to `global.output_dir` (from `config.yaml`). The summary file, `run_summary_nf.tsv`, is published separately to `./results` by default (override with `--publish_dir`) — it is not written inside `global.output_dir` unless you point `--publish_dir` there too. Columns: `tool`, `version`, `dataset`, `success`, `skipped`, `runtime_s`, `output_dir`, `error_msg`.

Nextflow task working directories are placed under Nextflow's default `work/` directory in the project root (override with `-w /path/to/dir`). To delete them after a successful run:

```bash
nextflow clean -f
```

To also redirect the Nextflow log into the results directory, pass `-log` on the command line:

```bash
nextflow run proteobench.nf -log /path/to/results/.nextflow.log
```

### Cluster / HPC execution

To run on SLURM (or another executor), add executor settings to a `nextflow.config` in the directory you run `nextflow run` from (Nextflow merges it with the repo's own `nextflow.config` automatically — this works the same whether you're running from a clone or a pulled release):

```groovy
// nextflow.config in your working directory
process.executor      = 'slurm'
process.queue         = 'gpu'
process.clusterOptions = '--mem=64G --time=04:00:00'
```

See the [Nextflow executor documentation](https://www.nextflow.io/docs/latest/executor.html) for other executors (PBS, LSF, Kubernetes, etc.). No changes to `proteobench.nf` itself are needed.

---

## Tool overview

| Tool | Acquisition | Input format | Docker image |
|------|-------------|-------------|----------------|
| DIA-NN | DDA (v2.1+), DIA | raw, mzML | `biocontainers/diann:v1.8.1_cv1` (public) or `diann:2.x` (built locally from [bigbio/quantms-containers](https://github.com/bigbio/quantms-containers)) |
| AlphaDIA | DIA | raw, mzML, .d | `mannlabs/alphadia:latest` |
| Sage | DDA | mzML, MGF | `ghcr.io/lazear/sage:latest` |
| FragPipe | DDA, DIA | raw, mzML, .d | `fcyucn/fragpipe:latest` + licensed MSFragger/IonQuant/diaTracer JARs |
| MaxQuant | DDA, DIA | raw | `quay.io/medbioinf/maxquant:2.6.3.0`, `quay.io/medbioinf/maxquant:2.8.1.0` |
| MetaMorpheus | DDA | raw, mzML | `smithchemwisc/metamorpheus:latest` |

All six images are pulled by the setup wizard (run automatically by `nextflow run proteobench.nf`, or directly via `nextflow run setup.nf`) — see [Quick start](#quick-start-nextflow-runner--recommended) above. There is no native/manual install path any more; every tool runs from its image.

---

## Adding a docker image tag setup.nf didn't pull

`setup.nf` always pulls `:latest` (except DIA-NN, which is described above, and MaxQuant, which pulls two fixed versions: `2.6.3.0` and `2.8.1.0`). To pin or add a different tag, edit `config.yaml` directly — add a new entry under that tool's `versions:` list with the new `image:` tag and `enabled: true`. Any `*_bin`/`*_dir`/`fragpipe_root` path may need updating too if the new image version changes its internal layout; `docker run --rm --entrypoint find <image> / -maxdepth 4 -iname <binary-name>` (as `setup.nf` does internally) will locate it.

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

Copy an existing version block, update `id`, `image`, and the tool's in-container binary path, then set `enabled: true`. Each tool uses its own key for that path — `diann_bin` (DIA-NN), `sage_bin` (Sage), `maxquant_dll` (MaxQuant), `fragpipe_root` (FragPipe); AlphaDIA and MetaMorpheus need no path key:

```yaml
tools:
  diann:
    versions:
      - id: "2.6.0"                              # new version
        image: diann:2.6.0                       # docker image tag
        diann_bin: /usr/diann-2.6.0/diann        # in-container binary path
        supports_dda: true                       # DIA-NN only: whether this build supports DDA
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
