# ProteoBench Runners Pipeline

This pipeline runs multiple proteomics search engines on ProteoBench benchmark datasets and collects output files for downstream submission to [ProteoBench](https://proteobench.cubimed.rub.de/). It supports DIA-NN, AlphaDIA, Sage, FragPipe, MaxQuant, and MetaMorpheus across DDA and DIA acquisition modes.

Everything runs through Nextflow (`proteobench.nf`), on a local machine or on a cluster (SLURM, …): it checks its own docker setup and runs the setup wizard itself when needed. Pull a tagged release directly from GitHub — no `git clone` needed — and `nextflow run ProteoBench/ProteoRunners -r v1.0.4` is the only command most users ever have to type. Add `--setup` to that command to add or re-enable a tool later.

---

## Prerequisites

**Docker is required.** Every search engine (DIA-NN, AlphaDIA, Sage, FragPipe, MaxQuant, MetaMorpheus) now runs from a docker image, there is nothing left to compile or install natively for any tool.

| Dependency | Required for | Install |
|------------|-------------|---------|
| **Docker** | **every tool — mandatory** | [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) |
| Python 3.11+ with `pyyaml`, available as `python3` | Config checks and job enumeration used internally by the pipeline (`rich` is only needed for the standalone `run_proteobench.py`) | `conda env create -f environment.yml` then `conda activate proteobench-pipeline`, or `pip install -r requirements.txt` in a virtual environment |
| Nextflow 23.10+ | Running the pipeline and setup wizard | `curl -s https://get.nextflow.io \| bash` then move to a directory on `$PATH` |
| `curl`, `tar`, `unzip` | Downloading benchmark datasets | Usually already installed (`sudo apt install curl tar unzip`) |
| `git` | Building the DIA-NN 2.x image only | Usually already installed; see [git-scm.com](https://git-scm.com/downloads) |

Check that each dependency is available:
```bash
docker info            # should print server info, not a connection error
python3 -c "import yaml; print('pyyaml ok')"   # run this in the environment you start Nextflow from
nextflow -version      # needed for setup.nf and the Nextflow runner
```

Your user must be able to run `docker` without `sudo` (on Linux: `sudo usermod -aG docker $USER`, then log out/in). The pipeline checks Docker and Python before it does anything else and says what to fix if one of them is missing.

---

## Quick start

Make a folder for your benchmark, go into it, and run the latest release directly from GitHub:

```bash
mkdir my_benchmark && cd my_benchmark
nextflow run ProteoBench/ProteoRunners -r v1.0.4
```

Everything you own is kept in the folder you run the command from:

| What | Default location | Change with |
|------|------------------|-------------|
| `config.yaml` | `./config.yaml` | `--config` |
| Downloaded datasets | `data/` next to `config.yaml` | `--data_dir` |
| FragPipe licensed JARs | `tools/fragpipe_jars/` next to `config.yaml` | (asked by the wizard) |
| Results and `run_summary_nf.tsv` | `results/` next to `config.yaml` (asked by the wizard, stored as `global.output_dir`) | `global.output_dir`, `--publish_dir` |

Nextflow caches the pipeline code itself under `~/.nextflow/assets/ProteoBench/ProteoRunners`; nothing of yours is written there. (Older versions defaulted `config.yaml` to that cache. If there is no `config.yaml` in your folder but there is one next to the pipeline code, that one is still used.) Relative paths in `config.yaml` are relative to the folder that holds `config.yaml`.

Developing the pipeline itself? Clone the repo and run it from inside your checkout instead — then these defaults resolve to the repo root, matching every other example in this README:

```bash
git clone https://github.com/ProteoBench/ProteoRunners.git
cd ProteoRunners
nextflow run proteobench.nf
```

That's it — the pipeline sets itself up on the way in:

- **No `config.yaml` yet?** It runs the interactive setup wizard first: for each tool, a yes/no prompt to pull its image, then the dataset download, then where the results should go. It writes `config.yaml` and asks whether to start the runs now. Press Enter to accept the default answer (the capital letter in `[Y/n]`).
- **`config.yaml` exists and looks complete** (every enabled tool's docker image — and, for FragPipe, its licensed JARs — is present, and every dataset an enabled tool uses is on disk)? Setup is skipped entirely.
- **`config.yaml` exists but something's missing** (an image was removed, a FragPipe JAR went missing, or a dataset that an enabled tool uses is not on disk)? Setup runs again, but only for those problems:
  - A missing image is pulled again with the exact tag in `config.yaml`, so a pinned version stays pinned. Nothing else in that tool's entry changes.
  - If you decline, or the pull fails, setup offers to set that version to `enabled: false`, so the pipeline stops asking. Only versions with `enabled: true` are checked.
  - A missing dataset is offered for download. If you do not download it, setup offers to comment it out of the tools' `datasets:` lists, so the pipeline stops asking.
  - Tools that are complete, disabled, or never set up are left alone.
- **Want to add a tool you skipped, re-enable a disabled one, or add FragPipe's JARs later?** Add `--setup` to the same command. Setup then also offers every tool that is not set up (default answer: no). FragPipe without its JARs is offered with default yes.
- Before any job starts, the whole `config.yaml` is checked once (paths, FASTA files, `output_dir`, images). If something is wrong, you get one numbered list of problems instead of failing jobs.
- `config.yaml` is edited as text, so your comments and layout are kept. It is only written when something changed, and the previous version is kept as `config.yaml.bak`.

Setup-wizard details per tool:

- **MaxQuant, Sage, MetaMorpheus, AlphaDIA** — a yes/no prompt each; a plain `docker pull` if you say yes.
- **FragPipe** — the `fcyucn/fragpipe` image does **not** include MSFragger, IonQuant, or diaTracer (Nesvilab Academic License, separate from FragPipe's own license). For each of the three, the wizard asks whether you already have it downloaded as a `.zip` or extracted folder; if not, it prints the download URL and lets you skip — FragPipe is written to the config but stays `enabled: false` until all three are present. Run the pipeline with `--setup` to add them later. When you point the wizard at an MSFragger folder, it also copies the `ext/` folder shipped next to the jar (the Thermo `.raw` and Bruker `.d` native readers, run under the mono runtime already in the image) and mounts it at run time, so FragPipe reads `.raw`/`.d` directly; if `ext/` is missing, FragPipe will need mzML input instead. FragPipe also needs decoys already appended to the FASTA (unlike the other tools); if `fasta_decoy:` isn't set for a dataset, one is generated automatically the first time that dataset is searched, via the Philosopher CLI already bundled in the image (the same command the FragPipe GUI's "Add decoys" button runs), and cached next to the source FASTA for reuse.
- **DIA-NN** — always pulls the free `biocontainers/diann:v1.8.1_cv1` image. It also asks whether to build DIA-NN 2.x images (needed for DDA support and native Thermo `.raw` reading on Linux); if you say yes, it `git clone`s [bigbio/quantms-containers](https://github.com/bigbio/quantms-containers), lists the recipes it ships (currently 1.8.1, 1.9.2, 2.0.2, 2.1.0, 2.2.0, 2.3.2, 2.5.0, 2.5.1) and lets you pick **one or several** as a comma-separated list. Each one is built with `docker build` locally and written to the config as its own version entry (`diann:<version>`), so several DIA-NN versions can be benchmarked side by side on the same datasets. DIA-NN itself is downloaded from the public [vdemichev/DiaNN](https://github.com/vdemichev/DiaNN) releases during the build, so no registry account or token is needed (requires `git`; a few minutes per version). `supports_dda` is set automatically: `true` from 2.1.0 onward. If you decline, only 1.8.1 is configured. Already-built images are detected and reused instead of rebuilt, so re-running the wizard to add another version is cheap. On a later run, the wizard lists which configured versions are enabled and present, which are disabled, and which are `enabled: true` but have no local image. For each of the last group it offers to pull or rebuild the image, or to set that version to `enabled: false`. Disabled versions are never pulled or rebuilt.
- **Datasets** — after the tools above are set up, the wizard offers to download benchmark datasets from `nextflow/datasets_catalog.yaml`, scoped to the datasets relevant to your enabled tools (by DDA/DIA acquisition). They are stored in `data/` next to `config.yaml` (change with `--data_dir`). It lists the relevant datasets with their size and the free disk space, and lets you pick `all`, `none`, or specific ones by number. Each dataset is downloaded as an archive (`.tar.gz` or `.zip`) and extracted. An interrupted download resumes on the next setup run where the server allows it, and a half-extracted dataset is detected and extracted again. After extraction the wizard checks that the MS files of the dataset's format are really there. Datasets downloaded during a `--setup` run are also added to the `datasets:` lists of the enabled tools that can use them. The ProteoBench archives unpack as `raws/<acquisition>/` plus `fasta/<name>.zip`; the wizard moves the MS files up into the dataset folder and extracts the FASTA next to them, which is the flat layout the runners expect (datasets downloaded earlier are fixed the same way on the next setup run). The FASTA (and decoy, if present) are detected automatically, and an `mzml/` subfolder is moved to a sibling `<name>_mzml` directory — not a separate dataset entry, but a fallback location tools automatically use whenever they can't read the dataset's native format and need mzML instead (e.g. Sage always; DIA-NN < 2.0 for Thermo `.raw`). Real, resolved paths are written into `datasets:`, and each configured tool's `datasets:` list is filled in automatically — no more `CHANGE_ME` for anything that was downloaded. Datasets already present on disk from an earlier run are reused, not re-downloaded. See "Adding a downloadable dataset" below.

Non-interactive / CI use (skips all prompts, uses these flags instead):
```bash
nextflow run proteobench.nf --non_interactive --skip_fragpipe \
    --build_diann_v2 --diann_version 2.1.0,2.5.0 \
    --download_datasets all --data_dir /path/to/data
```
`--setup_tools sage,diann` sets up exactly those tools without asking (`none` for no tools); without it, non-interactive mode sets up every tool. `--skip_datasets` skips the dataset step entirely; `--download_datasets` also accepts a comma-separated list of dataset names instead of `all`. With no `--download_datasets` given, non-interactive mode downloads nothing (safe default for CI).

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

To run the wizard again regardless of completeness (e.g. to add a tool you skipped), add `--setup`:
```bash
nextflow run ProteoBench/ProteoRunners -r v1.0.4 --setup    # pulled release
nextflow run proteobench.nf --setup                          # inside a clone
```
In a clone, `nextflow run setup.nf` does the same without starting any runs afterwards.

The pipeline reads `config.yaml` from the folder you run it from by default. Each job writes its actual result files under `global.output_dir` from that file. Relative paths in `config.yaml` (`output_dir`, and `path:`, `fasta:` and `fasta_decoy:` under `datasets:`) are resolved relative to the directory that holds `config.yaml`. The repo's `nextflow.config` also reads `config.yaml`: Nextflow's concurrency (`maxForks`) defaults to `global.max_parallel_jobs` (or 6 if unset), and `run_summary_nf.tsv` is published to `global.output_dir` (or `results/` next to `config.yaml` while it is unset or still `CHANGE_ME`). Override either with `--max_parallel_jobs` / `--publish_dir`. `nextflow.config` also lists the defaults of every `--flag` of `proteobench.nf` and `setup.nf`.

The scripts use Nextflow's strict syntax, which Nextflow 26.04+ requires by default, and they still run with the legacy parser (`NXF_SYNTAX_PARSER=v1`). See [Tests](#tests) for how to check changes.

### Common options

| Flag | Description |
|------|-------------|
| `--config /path/to/config.yaml` | Path to config file (default: `config.yaml` in the folder you run from) |
| `--setup` | Run the setup wizard first and offer every tool that is not set up yet |
| `--data_dir /path` | Where setup downloads datasets (default: `data/` next to `config.yaml`) |
| `--tool diann` | Restrict run to one tool |
| `--dataset Entrapment_DIA` | Restrict run to one dataset |
| `--no_preflight` | Skip preflight checks before each job |
| `--max_parallel_jobs 4` | Override Nextflow concurrency (default: `global.max_parallel_jobs`, or 6) |
| `--publish_dir /path` | Where `run_summary_nf.tsv` is published (default: `global.output_dir`, or `./results`) |

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

Each job's actual result files are written to `global.output_dir` (from `config.yaml`). The summary file, `run_summary_nf.tsv`, is published to `global.output_dir` as well, or to `./results` while `output_dir` is unset or still `CHANGE_ME` (override with `--publish_dir`). Columns: `tool`, `version`, `dataset`, `success`, `skipped`, `runtime_s`, `output_dir`, `error_msg`.

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

All six images are pulled by the setup wizard (run automatically by `nextflow run proteobench.nf`, or with `--setup`) — see [Quick start](#quick-start) above. There is no native/manual install path any more; every tool runs from its image.

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

To let setup download a dataset automatically instead of a user pointing `path:`/`fasta:` at existing files by hand, add an entry to `nextflow/datasets_catalog.yaml` with a real download URL:

```yaml
My_New_Dataset:
  url: https://example.org/path/to/My_New_Dataset.zip
  acquisition: DIA          # DDA or DIA
  format: raw                # raw | mzml | d | wiff | mgf
  instrument: Orbitrap       # Orbitrap | Astral | timstof | ZenoTOF
```

The archive may use the ProteoBench layout (`raws/<acquisition>/` + `fasta/<name>.zip`) or contain, at its top level: the MS files, one `*.fasta` (+ optionally one decoy fasta with `decoy` in the name), and optionally an `mzml/` subfolder. Run the pipeline with `--setup` — it offers the new dataset (scoped to tools it's relevant to), downloads and unzips it, and writes the resolved `path:`/`fasta:` into `config.yaml` automatically.

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

## Tests

There are two test suites. Run both before a release or after changing the setup logic.

**Nextflow ([nf-test](https://www.nf-test.com))** tests the helpers in `setup.nf` and the setup gate of `proteobench.nf` end to end:

```bash
curl -fsSL https://get.nf-test.com | bash   # once; puts ./nf-test here, move it onto your PATH
nf-test test tests/nextflow                 # about 2 minutes
```

- `tests/nextflow/setup_functions.nf.test`: how setup decides what to do per tool, and how it edits `config.yaml` as text.
- `tests/nextflow/pipeline.nf.test`: non-interactive runs from a fresh folder (first run, missing image, missing dataset, broken downloads, nothing to do). Datasets come from tiny archives in `tests/fixtures/datasets` via `file://` URLs, so no network is needed for them. These tests need Docker; they use fake image tags, the tiny `hello-world` image, and the Sage image for the first-run test.

**Python ([pytest](https://pytest.org))** tests `config_validator.py` and the path handling of `run_single_job.py`, with a fake Docker:

```bash
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements-dev.txt   # once
.venv/bin/pytest
```

The interactive prompts are not covered by the automated tests: nf-test cannot type answers. Check those by hand after changing a prompt.

---

## Troubleshooting

| Error message | Likely cause | Fix |
|---------------|-------------|-----|
| `still contains 'CHANGE_ME'` | Path not updated in config | Edit `config.yaml` and replace CHANGE_ME |
| `This computer is not ready yet` | Docker is missing, not running, or not usable without `sudo`; or `python3` has no `pyyaml` | Follow the fix printed under the message |
| `docker image not pulled locally` | Image not pulled yet | Run the pipeline again; setup offers to pull it |
| `No 'raw' files found` | Wrong `format:` or wrong `path:` | Verify files exist and `format:` matches |
| `MSFragger/IonQuant/diaTracer JAR not found in jars_dir` | Licensed FragPipe JAR missing | Run the pipeline with `--setup` and provide the downloaded zip/folder when asked |
| `'ext/thermo' folder was not found next to MSFragger jar` | MSFragger's native readers weren't copied (pointed the wizard at a bare `.jar`, not its folder) | Delete the MSFragger jar in `jars_dir`, run the pipeline with `--setup` and point at the MSFragger *folder* or zip (which has `ext/`), or switch that dataset to mzML input |
| `build_command failed: Philosopher decoy generation failed` | FASTA's directory isn't writable, or the FASTA is malformed | Check permissions on the FASTA's directory; or set `fasta_decoy:` explicitly to a pre-built one |
| `no download URL set in the dataset catalog` | Catalog entry still has `url: CHANGE_ME` | Add the real URL to `nextflow/datasets_catalog.yaml`, then run the pipeline with `--setup` |
| `not ready to run yet (N problem(s) above)` | `config.yaml` has problems that would make jobs fail | Fix the numbered problems, or run the pipeline with `--setup` |
| `is not valid YAML` | A typo in a hand edit of `config.yaml` | Fix the line shown in the message, or restore `config.yaml.bak` |
| Setup keeps asking about a dataset | An enabled tool lists a dataset that is not on disk | Download it, or let setup comment it out of the tools' `datasets:` lists |
| Dataset download skipped in CI | Non-interactive mode with no `--download_datasets` (safe default) | Pass `--download_datasets all` or a comma-separated list |
| `exit code 1 — check log: ...` | Tool crashed during search | Open the `stderr.log` file shown in the error |
| Job is skipped unexpectedly | `.done` marker exists | Delete the `.done` file in the output directory, or set `overwrite: true` |
| `No jobs to run` | All versions have `enabled: false`, or the enabled tools list no dataset | Set `enabled: true` for at least one version, and list dataset names from `datasets:` under that tool's `datasets:` |
| Setup keeps asking about a DIA-NN version | That version is `enabled: true` but its image is not present locally | Let setup pull or build it, or set `enabled: false` for it (setup offers this) |
| `Groovy import declarations are not supported` or `for loops are no longer supported` | An old checkout with Nextflow 26.04+ (strict parser) | Update to the current version, or run with `NXF_SYNTAX_PARSER=v1` |
| `enumerate_jobs.py failed` (Nextflow) | Python or YAML not on PATH | Run Nextflow from the activated conda/pip env; check `python3 -c "import yaml"` |
| `No module named 'yaml'` (Nextflow) | pyyaml not installed | `pip install pyyaml` |
| Nextflow process hangs | `max_parallel_jobs` too high for available cores/RAM | Lower `global.max_parallel_jobs` in `config.yaml` or pass `--max_parallel_jobs N` |
