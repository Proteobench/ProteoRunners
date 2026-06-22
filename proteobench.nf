#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

// ─── Parameters ──────────────────────────────────────────────────────────────
// All parameters mirror run_proteobench.py CLI flags.
params.config          = "${projectDir}/config.yaml"
params.tool            = null     // restrict to one tool (e.g. --tool diann)
params.dataset         = null     // restrict to one dataset
params.no_preflight    = false    // skip preflight checks
params.max_parallel_jobs = 6      // overridden from config.global.max_parallel_jobs at startup

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
    // Publish the TSV alongside the rest of the results.
    publishDir params.publish_dir, mode: 'copy', overwrite: true

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

    // Read config so we can pick up max_parallel_jobs and output_dir.
    def configPath = new File(params.config as String).absolutePath
    def cfg = new org.yaml.snakeyaml.Yaml().load(new File(configPath).text)

    def maxParallel = (cfg?.global?.max_parallel_jobs ?: params.max_parallel_jobs) as Integer
    def outputDir   = cfg?.global?.output_dir ?: "${projectDir}/results"

    // Propagate back to params so process directives pick them up.
    params.max_parallel_jobs = maxParallel
    params.publish_dir       = outputDir

    // ── Enumerate enabled jobs via Python ────────────────────────────────────
    def enumCmd = ["python3",
                   "${projectDir}/nextflow/enumerate_jobs.py",
                   "--config", configPath]
    if (params.tool)    enumCmd += ["--tool",    params.tool as String]
    if (params.dataset) enumCmd += ["--dataset", params.dataset as String]

    def enumProc = enumCmd.execute()
    def enumOut  = enumProc.text
    def enumErr  = enumProc.err.text
    enumProc.waitFor()

    if (enumProc.exitValue() != 0) {
        error "enumerate_jobs.py failed (exit ${enumProc.exitValue()}):\n${enumErr}"
    }

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

    log.info "Found ${jobs.size()} job(s) to run (max_parallel_jobs=${maxParallel}):"
    jobs.each { t, v, d ->
        log.info "  ${t.padRight(15)} v${v.padRight(8)}  ${d}"
    }

    // ── Dispatch and collect ─────────────────────────────────────────────────
    Channel.from(jobs)
        | RUN_JOB
        | collect
        | WRITE_SUMMARY
}
