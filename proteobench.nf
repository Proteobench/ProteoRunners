#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

include { SETUP } from './setup.nf'

// ─── Parameters ──────────────────────────────────────────────────────────────
// All parameters mirror run_proteobench.py CLI flags.
params.config          = "${projectDir}/config.yaml"
params.tool            = null     // restrict to one tool (e.g. --tool diann)
params.dataset         = null     // restrict to one dataset
params.no_preflight    = false    // skip preflight checks
params.max_parallel_jobs = 6      // default; nextflow.config overrides from config.global.max_parallel_jobs

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
        --config   "${params.config}" \
        --tool     "${tool}"          \
        --version  "${version}"       \
        --dataset  "${dataset}"       \
        ${noPreflightFlag}            \
        > "${tool}_v${version}_${dataset}.json"
    """
}

process WRITE_SUMMARY {
    // publish_dir is set by nextflow.config from config.global.output_dir.
    publishDir params.publish_dir ?: "${projectDir}/results", mode: 'copy', overwrite: true

    input:
    path result_jsons   // collected list of per-job JSON files

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

    def configFile = new File(params.config as String)

    // ── Setup gate: run the docker setup wizard on a first run (no config.yaml
    // yet), or whenever an *enabled* tool's docker setup looks incomplete
    // (missing image, missing FragPipe JARs, ...). Otherwise skip it entirely.
    def setupOk = false
    if (configFile.exists()) {
        def checkProc = ["python3", "${projectDir}/config_validator.py",
                          "--config", configFile.absolutePath, "--check-docker-setup"].execute()
        def checkOut = new StringBuilder(), checkErr = new StringBuilder()
        checkProc.consumeProcessOutput(checkOut, checkErr)
        checkProc.waitFor()
        setupOk = (checkProc.exitValue() == 0)
        if (!setupOk) {
            log.warn "Docker setup looks incomplete for one or more enabled tools:"
            checkOut.toString().readLines().each { log.warn "  - ${it}" }
        }
    }

    if (!setupOk) {
        log.info configFile.exists() ?
            "Re-running setup for the missing pieces above ..." :
            "No ${configFile} yet — running first-time docker setup ..."
        SETUP()
        if (!configFile.exists()) {
            error "Setup did not produce ${configFile}. See the messages above, then re-run: nextflow run proteobench.nf"
        }
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
                      .findAll  { it.trim() }
                      .collect  { line ->
                          def j = new groovy.json.JsonSlurper().parseText(line)
                          tuple(j.tool as String, j.version as String, j.dataset as String)
                      }

    if (!jobs) {
        log.warn "No enabled jobs found. Check config.yaml enabled flags and filters."
        log.warn "Run: python3 run_proteobench.py --list-tools   to see what is enabled."
        return
    }

    log.info "Found ${jobs.size()} job(s) to run (max_parallel_jobs=${params.max_parallel_jobs}):"
    jobs.each { t, v, d ->
        log.info "  ${t.padRight(15)} v${v.padRight(8)}  ${d}"
    }

    // ── Dispatch and collect ─────────────────────────────────────────────────
    Channel.from(jobs)
        | RUN_JOB
        | collect
        | WRITE_SUMMARY
}
