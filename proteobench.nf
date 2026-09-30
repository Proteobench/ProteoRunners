#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

include { SETUP; checkPrerequisites; setupStatus; configErrors; askYesNo; resultsDirFor } from './setup.nf'

// ─── Parameters ──────────────────────────────────────────────────────────────
// All parameters mirror run_proteobench.py CLI flags.
params.config          = params.config ?: "${launchDir}/config.yaml"
params.setup           = false    // run the setup wizard first and offer every tool that is not set up
params.tool            = null     // restrict to one tool (e.g. --tool diann)
params.dataset         = null     // restrict to one dataset
params.no_preflight    = false    // skip preflight checks
params.max_parallel_jobs = 6      // default; nextflow.config overrides from config.global.max_parallel_jobs
params.unfiltered_outputs = false // keep full raw tool outputs; default filters down to ProteoBench upload files

// ─── Processes ───────────────────────────────────────────────────────────────

process RUN_JOB {
    tag "${tool} v${version} / ${dataset}"

    // Honour max_parallel_jobs from config. Users can also pass --max_parallel_jobs N.
    maxForks params.max_parallel_jobs as Integer

    input:
    tuple val(tool), val(version), val(dataset)

    // Each job writes its result JSON with a unique name derived from the job
    // identity so files can be collected without name collisions.
    output:
    path "${tool}_v${version}_${dataset}.json", emit: result_json

    script:
    def noPreflightFlag = params.no_preflight ? "--no-preflight" : ""
    """
    python3 "${projectDir}/nextflow/run_single_job.py" \
        --config   "${new File(params.config as String).absolutePath}" \
        --tool     "${tool}"          \
        --version  "${version}"       \
        --dataset  "${dataset}"       \
        ${noPreflightFlag}            \
        > "${tool}_v${version}_${dataset}.json"
    """
}

process FILTER_OUTPUTS {
    tag "${result_json.baseName}"

    // Deletes files from the job's output_dir (an absolute host path recorded
    // inside result_json, outside Nextflow's own staging), so this runs after
    // every job regardless of caching.
    input:
    path result_json

    output:
    path result_json.name, emit: result_json

    script:
    def unfilteredFlag = params.unfiltered_outputs ? "--unfiltered" : ""
    """
    python3 "${projectDir}/nextflow/filter_outputs.py" "${result_json}" ${unfilteredFlag} > "${result_json.name}.out"
    mv "${result_json.name}.out" "${result_json.name}"
    """
}

process WRITE_SUMMARY {
    // Published to --publish_dir, else global.output_dir of config.yaml, read
    // after setup ran (a first-run setup only sets output_dir then).
    publishDir path: { publish_to }, mode: 'copy', overwrite: true

    input:
    path result_jsons   // collected list of per-job JSON files
    val publish_to

    output:
    path "run_summary_nf.tsv", emit: summary

    script:
    """
    python3 "${projectDir}/nextflow/write_summary.py" \
        ${result_jsons} > run_summary_nf.tsv
    cat run_summary_nf.tsv >&2
    """
}

// ─── Workflow ─────────────────────────────────────────────────────────────────

workflow {

    def configFile = new File(params.config as String).absoluteFile

    // ── Setup gate ───────────────────────────────────────────────────────────
    // Setup runs on a first run (no config.yaml yet), with --setup, when an
    // *enabled* tool's docker setup is incomplete (missing image, missing
    // FragPipe JARs, ...), or when a dataset used by an enabled tool is not on
    // disk but downloadable from the catalog. Otherwise it is skipped.
    checkPrerequisites()
    def firstRun = !configFile.exists()
    def runSetup = firstRun || params.setup
    if (!runSetup) {
        def status = setupStatus(configFile)
        def incomplete = status.tools.findAll { _n, t -> t.status == 'incomplete' }
        incomplete.each { _n, t -> t.problems.each { pr -> log.warn pr.message } }
        if (incomplete) {
            log.warn "These versions are checked because they have 'enabled: true' in ${configFile}. Set 'enabled: false' for a version you do not want, and it is no longer checked."
        }
        if (status.missing_datasets) {
            log.warn "Datasets used by enabled tools are not on disk yet: ${status.missing_datasets.join(', ')}"
        }
        if (status.tools.fragpipe?.status == 'disabled' && status.tools.fragpipe.fragpipe_jars_missing) {
            log.info "FragPipe is disabled because its licensed JARs are missing. To add them, run the pipeline again with --setup."
        }
        runSetup = incomplete || status.missing_datasets
    }

    if (runSetup) {
        log.info firstRun ? "No ${configFile} yet — running first-time setup ..." :
                 params.setup ? "Running setup (--setup) ..." : "Running setup for the missing pieces above ..."
        SETUP(!firstRun && !params.setup)
        if (!configFile.exists()) {
            error "Setup did not produce ${configFile}. See the messages above, then run the same command again."
        }
    }

    // ── Is config.yaml ready to run? ─────────────────────────────────────────
    // One clear list up front, instead of every job failing on its own.
    def problems = configErrors(configFile)
    if (problems) {
        problems.eachWithIndex { m, i -> log.error "${i + 1}. ${m}" }
        if (!params.no_preflight) {
            error "${configFile} is not ready to run yet (${problems.size()} problem(s) above). Fix them (or run the pipeline with --setup), then run the same command again."
        }
        log.warn "Continuing anyway because --no_preflight was given."
    }
    if ((firstRun || params.setup) && !askYesNo('Setup is done. Start the benchmark runs now?', true)) {
        log.info "Not starting now. Start the runs later with the same command, without --setup."
        return
    }

    def configPath = configFile.absolutePath

    // ── Enumerate enabled jobs via Python ────────────────────────────────────
    def enumCmd = ["python3",
                   "${projectDir}/nextflow/enumerate_jobs.py",
                   "--config", configPath]
    if (params.tool)    enumCmd += ["--tool",    params.tool as String]
    if (params.dataset) enumCmd += ["--dataset", params.dataset as String]

    // consumeProcessOutput reads stdout and stderr concurrently to avoid the
    // OS pipe-buffer deadlock that occurs when reading them sequentially.
    def enumStdout = new StringBuilder()
    def enumStderr = new StringBuilder()
    def enumProc   = enumCmd.execute()
    enumProc.consumeProcessOutput(enumStdout, enumStderr)
    enumProc.waitFor()

    if (enumProc.exitValue() != 0) {
        error "enumerate_jobs.py failed (exit ${enumProc.exitValue()}):\n${enumStderr}"
    }

    def enumOut = enumStdout.toString()

    def jobs = enumOut.readLines()
                      .findAll  { line -> line.trim() }
                      .collect  { line ->
                          def j = new groovy.json.JsonSlurper().parseText(line)
                          tuple(j.tool as String, j.version as String, j.dataset as String)
                      }

    if (!jobs) {
        log.warn "No jobs to run. A job needs a tool version with 'enabled: true' and a dataset name in that tool's datasets: list in ${configFile}" +
                 (params.tool || params.dataset ? ", matching --tool/--dataset." : ".")
        return
    }

    log.info "Found ${jobs.size()} job(s) to run (max_parallel_jobs=${params.max_parallel_jobs}):"
    jobs.each { t, v, d ->
        log.info "  ${t.padRight(15)} v${v.padRight(8)}  ${d}"
    }

    // ── Dispatch and collect ─────────────────────────────────────────────────
    def publishTo = params.publish_dir ?: resultsDirFor(configFile)
    RUN_JOB(channel.fromList(jobs))
    FILTER_OUTPUTS(RUN_JOB.out.result_json)
    WRITE_SUMMARY(FILTER_OUTPUTS.out.result_json.collect(), publishTo)
}
