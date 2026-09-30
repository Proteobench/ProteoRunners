#!/usr/bin/env nextflow
// Interactive Docker setup for the ProteoBench pipeline. Every search engine
// runs from a docker image, so this script's only job is to pull the right
// images, collect the FragPipe/DIA-NN license extras, download datasets and
// write config.yaml.
//
// Normally you never run this file directly — proteobench.nf includes it as
// the SETUP workflow and calls it automatically:
//   - on a first run (no config.yaml yet): every tool is offered;
//   - when an enabled tool's docker setup is incomplete, or a dataset used by
//     an enabled tool is missing: only those problems are fixed;
//   - with --setup: the problems are fixed AND every tool that is not set up
//     (or only has disabled versions) is offered again, default "no".
//
// Direct use behaves like `proteobench.nf --setup`, without starting runs:
//   nextflow run setup.nf                      # interactive, guided setup
//   nextflow run setup.nf --non_interactive \
//       --msfragger_path ... --ionquant_path ... --diatracer_path ... \
//       --build_diann_v2 --diann_version 2.1.0,2.5.0   # scripted / CI setup
//   nf-test test tests/nextflow                # tests for the helper functions and the pipeline
//
// A rerun never rewrites a tool that is complete. A tool that is repaired
// keeps its config block; only `enabled:` flags change when you choose to
// disable a version. config.yaml is only written when something changed, and
// the previous version is kept as config.yaml.bak.
//
// Written in Nextflow's strict syntax (no top-level statements, imports, or
// for/while loops), which also parses under the legacy parser
// (NXF_SYNTAX_PARSER=v1). Shared state is therefore passed explicitly instead
// of living in script-level variables.

nextflow.enable.dsl = 2

params.config            = params.config ?: "${launchDir}/config.yaml"
params.non_interactive   = false
params.skip_fragpipe     = false
params.msfragger_path    = null   // path to a MSFragger .zip or extracted folder
params.ionquant_path     = null   // path to an IonQuant .zip or extracted folder
params.diatracer_path    = null   // path to a diaTracer .zip or extracted folder
params.diann_version     = '2.5.0'   // DIA-NN 2.x version(s) to build locally, comma-separated
                                     // (recipe names from bigbio/quantms-containers, e.g. '2.1.0,2.5.0')
params.build_diann_v2    = false     // non-interactive opt-in to build the DIA-NN 2.x image(s)
params.alphadia_gpu      = false
params.skip_datasets     = false
params.data_dir          = null   // where downloaded datasets go; default: data/ next to config.yaml
params.download_datasets = null   // "all", or a comma-separated list of dataset names; CI opt-in
params.datasets_catalog  = null   // default: nextflow/datasets_catalog.yaml in the pipeline
params.output_dir        = null   // global.output_dir when setup has to set it; default: results/ next to config.yaml
params.setup_tools       = null   // comma-separated tools to set up without asking (e.g. "sage,diann"), or "none"

// Approximate tool → acquisition capability, used ONLY to decide which
// catalog datasets are worth offering during setup. NOT a source of truth —
// runners/*.py SUPPORTED_ACQUISITIONS + is_compatible() enforce the real
// compatibility at job-enumeration time.
def toolAcquisitions() {
    return [
        diann:        ['DDA', 'DIA'],
        alphadia:     ['DIA'],
        sage:         ['DDA'],
        fragpipe:     ['DDA', 'DIA'],
        maxquant:     ['DDA', 'DIA'],
        metamorpheus: ['DDA'],
    ]
}

// Display names, in the order tools are written to a new config.
def toolLabels() {
    return [diann: 'DIA-NN', alphadia: 'AlphaDIA', sage: 'Sage', fragpipe: 'FragPipe', maxquant: 'MaxQuant', metamorpheus: 'MetaMorpheus']
}

// Written into a dataset folder while it is downloaded and extracted, removed
// when that finished. Must match config_validator.INCOMPLETE_MARKER.
def incompleteMarker() { return '.proteorunners_incomplete' }

// ── Small interactive-IO helpers ──────────────────────────────────────────
// Nextflow scripts aren't normally interactive, but the workflow{} block below
// runs sequentially on the driver process (this machine's terminal), so plain
// stdin prompts work exactly like a regular CLI wizard when run locally.

def isInteractive() {
    return !params.non_interactive
}

// Read one line from stdin without a buffered reader, so repeated calls never
// lose input that a previous reader had already buffered (e.g. piped answers).
// Recursive because the strict syntax has no while/for loops.
def readStdinBytes(buf) {
    def b = System.in.read()
    if (b < 0 || b == 10) return b
    if (b != 13) buf.write(b)
    return readStdinBytes(buf)
}

def readStdinLine() {
    def buf = new ByteArrayOutputStream()
    def last = readStdinBytes(buf)
    return (last < 0 && buf.size() == 0) ? null : buf.toString('UTF-8')
}

// ── Pretty terminal output ─────────────────────────────────────────────────
// ANSI styling, but only when writing to a real terminal and NO_COLOR is unset,
// so piped / CI logs stay plain text.
def useColor() {
    return System.console() != null && !System.getenv('NO_COLOR')
}

def sgr(code, s)  { return useColor() ? "\u001B[${code}m${s}\u001B[0m" : "${s}" }
def bold(s)       { return sgr('1',  s) }
def dim(s)        { return sgr('2',  s) }
def red(s)        { return sgr('31', s) }
def green(s)      { return sgr('32', s) }
def yellow(s)     { return sgr('33', s) }
def cyan(s)       { return sgr('36', s) }

def rep(ch, n) { return n > 0 ? ch * n : '' }

def center(s, w) {
    def pad = w - s.length()
    def l = pad > 0 ? pad.intdiv(2) : 0
    return rep(' ', l) + s + rep(' ', pad - l)
}

def banner(title, subtitle) {
    def boxW = 60
    println ''
    println cyan('  ╭' + rep('─', boxW) + '╮')
    println cyan('  │') + bold(center(title, boxW)) + cyan('│')
    if (subtitle) println cyan('  │') + dim(center(subtitle, boxW)) + cyan('│')
    println cyan('  ╰' + rep('─', boxW) + '╯')
}

def section(title) {
    println ''
    println bold(cyan('▶ ' + title))
}

def ok(s)   { println '  ' + green('✓') + ' ' + s }
def warn(s) { println '  ' + yellow('•') + ' ' + s }
def fail(s) { println '  ' + red('✗') + ' ' + s }
def info(s) { println '  ' + cyan('→') + ' ' + s }

def ask(prompt) {
    print prompt
    System.out.flush()
    def console = System.console()
    def line = console != null ? console.readLine() : readStdinLine()
    return line?.trim()
}

def askYesNo(prompt, byDefault) {
    if (!isInteractive()) return byDefault
    def suffix = byDefault ? ' [Y/n] ' : ' [y/N] '
    def a = ask('  ' + cyan('?') + ' ' + prompt + dim(suffix))
    if (!a) return byDefault
    return a.toLowerCase().startsWith('y')
}

// ProcessBuilder converts the command list to a String[] internally and
// throws a cryptic arraycopy/ClassCastException if any element is a GString
// (from "${...}" interpolation) rather than a real String — coerce here once
// so no call site has to remember to .toString() its own arguments.
def strList(cmd) { return cmd.collect { arg -> arg.toString() } }

// Stream a command's output live to this terminal (docker pull progress bars, etc).
def runCmd(cmd) {
    def pb = new ProcessBuilder(strList(cmd))
    pb.redirectOutput(ProcessBuilder.Redirect.INHERIT)
    pb.redirectError(ProcessBuilder.Redirect.INHERIT)
    def p = pb.start()
    p.waitFor()
    return p.exitValue()
}

// Capture a command's stdout instead of streaming it (used for the one-off
// `find` calls that auto-detect in-container paths after a pull). stderr is
// dropped, so a "find: Permission denied" line is never taken for a path.
def capture(cmd) {
    def pb = new ProcessBuilder(strList(cmd)).redirectError(ProcessBuilder.Redirect.DISCARD)
    def p = pb.start()
    def text = p.inputStream.text
    p.waitFor()
    return text
}

def firstLine(text) { return text.readLines().find { l -> l.trim() } ?: '' }

def hasCmd(name) {
    def p = new ProcessBuilder(strList(['sh', '-c', "command -v '${name}'"])).redirectErrorStream(true).start()
    p.inputStream.text
    return p.waitFor() == 0
}

// Quote a value for YAML, so paths with spaces, ':' or '#' stay valid.
def yq(s) { return '"' + s.toString().replace('\\', '\\\\').replace('"', '\\"') + '"' }

// ── Locations ─────────────────────────────────────────────────────────────
// Everything the user owns (datasets, FragPipe JARs, results) defaults to a
// folder next to config.yaml, never to the pipeline code: for a pulled
// release that code lives in ~/.nextflow/assets.

def configDirOf(configFile) { return configFile.absoluteFile.parentFile }

def expandHome(s) { return s.toString().replaceFirst(/^~(?=\/|$)/, System.getProperty('user.home')) }

// Relative paths are relative to the folder that holds config.yaml.
def absoluteFrom(configFile, s) {
    def f = new File(expandHome(s))
    return f.isAbsolute() ? f : new File(configDirOf(configFile), f.path)
}

def dataDirFor(configFile) {
    return params.data_dir ? new File(expandHome(params.data_dir)).absoluteFile : new File(configDirOf(configFile), 'data')
}

def catalogFile() {
    return new File((params.datasets_catalog ?: "${projectDir}/nextflow/datasets_catalog.yaml").toString()).absoluteFile
}

// global.output_dir from config.yaml as an absolute path (default: results/
// next to config.yaml while it is unset or still CHANGE_ME).
def resultsDirFor(configFile) {
    def cfg = configFile.exists() ? new org.yaml.snakeyaml.Yaml().load(configFile.text) : null
    def od = (cfg instanceof Map && cfg.global instanceof Map) ? cfg.global.output_dir?.toString() : null
    return (!od || od.contains('CHANGE_ME')) ? new File(configDirOf(configFile), 'results').path : absoluteFrom(configFile, od).path
}

// ── Prerequisites ─────────────────────────────────────────────────────────
// Checked before anything else, so a missing dependency gives one clear
// message instead of a wrong decision further on.

def dockerProblem() {
    if (!hasCmd('docker')) {
        return 'Docker is not installed. Install it (https://docs.docker.com/engine/install/), then run the same command again.'
    }
    def p = new ProcessBuilder(strList(['docker', 'info'])).redirectErrorStream(true).start()
    def out = p.inputStream.text
    if (p.waitFor() == 0) return null
    if (out.toLowerCase().contains('permission denied')) {
        return "Docker is installed, but your user is not allowed to use it. Run: sudo usermod -aG docker ${System.getProperty('user.name')}   then log out, log in again, and run the same command again."
    }
    return 'Docker is installed but not running. Start it (for example: sudo systemctl start docker), then run the same command again.'
}

def pythonProblem() {
    if (!hasCmd('python3')) {
        return 'python3 was not found. Install Python 3.11 or newer, or activate the pipeline environment: conda activate proteobench-pipeline'
    }
    def p = new ProcessBuilder(strList(['python3', '-c', 'import yaml'])).redirectErrorStream(true).start()
    p.inputStream.text
    if (p.waitFor() == 0) return null
    return 'The Python package pyyaml is missing for python3. Activate the pipeline environment (conda activate proteobench-pipeline) or run: python3 -m pip install pyyaml'
}

def checkPrerequisites() {
    def problems = [dockerProblem(), pythonProblem()].findAll { m -> m }
    if (problems) {
        error("This computer is not ready yet:\n" + problems.collect { m -> "  - ${m}" }.join('\n'))
    }
}

// ── config_validator.py ───────────────────────────────────────────────────
// Exit code 2/3 means the check itself failed (unreadable config, no pyyaml):
// stop with its message instead of treating the empty output as "all fine".

def runValidator(configFile, args) {
    def cmd = ['python3', "${projectDir}/config_validator.py", '--config', configFile.absolutePath,
               '--catalog', catalogFile().path] + args
    def p = strList(cmd).execute()
    def o = new StringBuilder()
    def e = new StringBuilder()
    p.consumeProcessOutput(o, e)
    p.waitFor()
    if (p.exitValue() >= 2) {
        error("Could not check ${configFile}:\n${e.toString().trim() ?: o.toString().trim()}")
    }
    return o.toString().readLines().collect { l -> l.trim() }.findAll { l -> l }
}

// Per-tool status + missing datasets, see config_validator.setup_status().
def setupStatus(configFile) {
    return new groovy.json.JsonSlurper().parseText(runValidator(configFile, ['--setup-status']).join('\n'))
}

// Everything that would stop a run (config_validator.validate_config()).
def configErrors(configFile) { return runValidator(configFile, []) }

// ── Deciding what to do per tool ──────────────────────────────────────────
// new    first run, or the tool's config entry is broken: set it up from scratch (default yes)
// repair enabled versions miss an image or JARs: fix those, keep the block
// offer  not configured / all versions disabled, and the user asked for setup (default no)
// skip   not configured / disabled, and setup was started by the gate: leave alone
// keep   complete: leave alone
def toolPlan(tool, ctx) {
    if (ctx.firstRun) return 'new'
    def st = ctx.status.tools[tool]
    if (st == null || st.status == 'disabled') return ctx.fromGate ? 'skip' : 'offer'
    if (st.status == 'complete') return 'keep'
    return st.problems.any { pr -> pr.reason == 'config' } ? 'new' : 'repair'
}

// --setup_tools, as a set of tool names, or null when not given.
def selectedTools() {
    return params.setup_tools == null ? null :
        params.setup_tools.toString().split(',').collect { t -> t.trim().toLowerCase() }.findAll { t -> t && t != 'none' } as Set
}

// Ask whether to set a tool up; only asks for plans 'new' and 'offer'.
// --setup_tools answers the question for every tool.
def wantTool(plan, tool, what, ctx) {
    def label = toolLabels()[tool]
    if (plan in ['new', 'offer'] && selectedTools() != null) return tool in selectedTools()
    if (plan == 'new') return askYesNo("Pull ${label}? " + dim(what), true)
    if (plan != 'offer') return false
    def st = ctx.status.tools[tool]
    if (st == null) return askYesNo("${label} is not set up yet. Set it up now? " + dim(what), false)
    if (tool == 'fragpipe' && st.fragpipe_jars_missing) {
        return askYesNo('FragPipe is disabled because its licensed JARs were missing. Add them now?', true)
    }
    return askYesNo("${label} is in config.yaml, but no version is enabled. Set it up again? " + dim(what), false)
}

def notDoneMessage(plan, tool) {
    def label = toolLabels()[tool]
    if (plan == 'keep') ok("${label} is set up — keeping it as it is.")
    else if (plan == 'skip') info("${label} is not set up " + dim('(to add it: run the pipeline with --setup)'))
    else warn("Skipping ${label}.")
}

// Set `enabled:` to `value` for the given version ids inside one tool block,
// leaving every other line as it was.
def setEnabled(block, ids, value) {
    def cur = null
    def idSet = ids.collect { i -> i.toString() } as Set
    return block.readLines().collect { line ->
        def m = (line =~ /^\s+(-\s+)?id:\s*"?([^"\s#]+)"?/)
        if (m.find()) cur = m.group(2)
        (cur in idSet && line ==~ /^\s+enabled:\s*(true|false)\s*(#.*)?$/) ? line.replaceFirst(/(true|false)/, value.toString()) : line
    }.join('\n')
}

def disableVersions(block, ids) { return setEnabled(block, ids, false) }

// Append rendered version entries at the end of a tool block's versions: list.
def appendVersions(block, entriesText) {
    def m = java.util.regex.Pattern.compile(/(?ms)^    versions:.*?(?=^    \w|\z)/).matcher(block)
    if (!m.find()) return block
    return block.substring(0, m.start()) + m.group(0).replaceAll(/\s+$/, '') + '\n\n' + entriesText + '\n' + block.substring(m.end())
}

def renderDiannVersion(v) {
    return "      - id: \"${v.id}\"\n        image: ${v.image}\n        diann_bin: ${v.diann_bin}\n" +
           "        supports_dda: ${v.supports_dda}\n        enabled: ${v.containsKey('enabled') ? v.enabled : true}\n"
}

// Offer to disable versions that are still not ready, so the pipeline stops
// re-running setup for them. Never done silently in non-interactive mode.
def offerDisable(tool, ids, block) {
    def label = toolLabels()[tool]
    warn("${label} ${ids.join(', ')} is enabled in config.yaml but still not ready.")
    if (isInteractive() && askYesNo("Set it to enabled: false in config.yaml, so the pipeline stops asking about it?", true)) {
        ok("${label} ${ids.join(', ')} set to " + bold('enabled: false') + dim(' (set it back to true to use it again)'))
        return disableVersions(block, ids)
    }
    warn('It stays enabled, so setup will ask again on the next run.')
    return block
}

// Fix the enabled versions of one tool in place: pull the image that is
// configured (so a pinned tag stays pinned) and, for FragPipe, collect the
// licensed JARs. Returns the tool block, changed only in its enabled flags.
def repairTool(tool, st, block) {
    def label = toolLabels()[tool]
    def notReady = []
    st.problems.findAll { pr -> pr.reason == 'image' }.groupBy { pr -> pr.image }.each { image, prs ->
        def ids = prs.collect { pr -> pr.id }
        def pull = askYesNo("Pull ${image} again? " + dim("(${label} ${ids.join(', ')} is enabled in config.yaml, but the image is not on this machine)"), true)
        if (pull && runCmd(['docker', 'pull', image]) == 0) {
            ok("${label} ${ids.join(', ')} ready " + dim("(${image})"))
        } else {
            if (pull) fail("Pulling ${image} failed — see the message above.")
            notReady.addAll(ids)
        }
    }
    def jarProblems = st.problems.findAll { pr -> pr.reason == 'jars' }
    if (jarProblems) {
        def jarsDir = parseVersionEntries(block).find { v -> v.enabled == 'true' }?.jars_dir
        if (!collectFragpipeJars(new File(expandHome(jarsDir)))) notReady.addAll(jarProblems.collect { pr -> pr.id })
    }
    notReady = notReady.unique()
    return notReady ? offerDisable(tool, notReady, block) : block
}

// ── Dataset download helpers ──────────────────────────────────────────────

// The single subdirectory of `d`, or null if `d` holds anything else. Bruker
// '.d' directories are never treated as wrappers (they are data units), and
// the download marker does not count.
def onlyChildDir(d) {
    def e = d.listFiles()?.findAll { f -> f.name != incompleteMarker() }
    return (e != null && e.size() == 1 && e[0].isDirectory() && !e[0].name.toLowerCase().endsWith('.d')) ? e[0] : null
}

// Follow a chain of single-subdirectory wrappers down to the innermost one.
def innermostWrapper(d) {
    def next = onlyChildDir(d)
    return next != null ? innermostWrapper(next) : d
}

// ProteoBench archives extract to a nested wrapper (e.g. raws/<subdir>/...) whose
// directory name doesn't match the dataset. Collapse any chain of single-
// subdirectory wrappers so the dataset files sit directly in destDir, matching
// the flat layout the resolve step and the config paths expect.
def flattenSingleDirs(destDir) {
    def firstWrapper = onlyChildDir(destDir)
    if (firstWrapper == null) return
    def root = innermostWrapper(firstWrapper)
    root.listFiles().each { child -> child.renameTo(new File(destDir, child.name)) }
    firstWrapper.deleteDir()   // recursively removes the now-empty wrapper chain
}

def isFastaName(name) {
    def n = name.toLowerCase()
    return n.endsWith('.fasta') || n.endsWith('.fa') || n.endsWith('.fas')
}

// Move `f` into `destDir` unless a file of that name is already there.
def moveInto(f, destDir) {
    def target = new File(destDir, f.name)
    if (target.exists()) {
        warn("${target} already exists — left ${f} in place.")
        return false
    }
    return f.renameTo(target)
}

def hasFilesLeft(dir) {
    def left = []
    dir.eachFileRecurse { f -> if (f.isFile()) left << f }
    return !left.isEmpty()
}

// Extract the FASTA files from a zip into destDir, skipping macOS metadata.
def extractFastas(zipFile, destDir) {
    def zip = new java.util.zip.ZipFile(zipFile)
    zip.entries().each { entry ->
        def base = new File(entry.name).name
        if (entry.isDirectory() || entry.name.startsWith('__MACOSX') || base.startsWith('._') || !isFastaName(base)) return
        def target = new File(destDir, base)
        if (target.exists()) return
        target.withOutputStream { os -> os << zip.getInputStream(entry) }
    }
    zip.close()
}

// ProteoBench archives unpack as raws/<acquisition>/<MS files> (plus an
// optional mzml/ folder) and fasta/<name>.zip, while the runners and the
// config expect MS files and the FASTA directly in the dataset folder. Move
// the MS files up and extract the FASTA. Safe to run again on a dataset that
// is already flat; raws/ is only removed once no files are left in it.
def normalizeProteoBenchLayout(destDir) {
    def rawsDir = new File(destDir, 'raws')
    if (rawsDir.isDirectory()) {
        def msRoot = innermostWrapper(rawsDir)
        msRoot.listFiles().each { child -> moveInto(child, destDir) }
        if (!hasFilesLeft(rawsDir)) rawsDir.deleteDir()
    }
    def fastaDir = new File(destDir, 'fasta')
    if (fastaDir.isDirectory()) {
        fastaDir.listFiles().each { f ->
            if (isFastaName(f.name)) moveInto(f, destDir)
            else if (f.name.toLowerCase().endsWith('.zip')) extractFastas(f, destDir)
        }
    }
}

def isDatasetPresent(d) {
    return d.isDirectory() && d.list() && !new File(d, incompleteMarker()).exists()
}

// MS files of the dataset's format directly in destDir (after the layout was
// normalised). Checked after extraction: tar exits 0 on some broken archives.
def msFiles(destDir, format) {
    def ext = ".${format ?: ''}".toString().toLowerCase()
    return (destDir.listFiles() ?: []).findAll { f -> f.name.toLowerCase().endsWith(ext) && (ext == '.d' ? f.isDirectory() : f.isFile()) }
}

def gb(bytes) { return bytes >= 1e9 ? String.format('%.1f GB', bytes / 1e9) : String.format('%.0f MB', Math.max((bytes / 1e6) as double, 1d)) }

// Size of a download in bytes from its HTTP headers, or -1 if unknown.
def remoteSize(url) {
    def sizes = capture(['curl', '-sIL', '--fail', '--max-time', '30', url]).readLines()
        .findAll { l -> l.toLowerCase().startsWith('content-length:') }
        .collect { l -> l.split(':', 2)[1].trim() }
        .findAll { s -> s.isLong() }
    return sizes ? sizes[-1].toLong() : -1L
}

// Download and extract one catalog dataset into dataDir/<name>. The archive is
// kept as dataDir/.<name>.download.* until extraction succeeded, so an
// interrupted download resumes on the next run; the folder carries the
// incomplete marker until everything is in place.
def downloadOne(name, meta, dataDir) {
    if (!meta.url || meta.url == 'CHANGE_ME') {
        warn("${name}: no download URL set in the dataset catalog — skipping.")
        return false
    }
    def urlLower = meta.url.toString().toLowerCase()
    def isTarGz  = urlLower.endsWith('.tar.gz') || urlLower.endsWith('.tgz')
    def isZip    = urlLower.endsWith('.zip')
    if (!isTarGz && !isZip) {
        warn("${name}: unrecognized archive type for ${meta.url} (expected .zip or .tar.gz/.tgz) — skipping.")
        return false
    }

    def destDir = new File(dataDir, name)
    def marker  = new File(destDir, incompleteMarker())
    def archive = new File(dataDir, ".${name}.download" + (isTarGz ? '.tar.gz' : '.zip'))
    if (destDir.isDirectory() && marker.exists()) {
        warn("${name}: removing an unfinished earlier extraction.")
        destDir.deleteDir()
    }

    // Archive + extracted files need about twice the archive size.
    def size = remoteSize(meta.url)
    if (size > 0) {
        def need = 2 * size - (archive.exists() ? archive.length() : 0)
        def free = dataDir.usableSpace
        if (free < need) {
            fail("${name}: needs about ${gb(need)} of free disk space, but only ${gb(free)} is free in ${dataDir}.")
            if (!askYesNo('Try anyway?', false) || !isInteractive()) return false
        }
    }

    info("Downloading ${name}" + (size > 0 ? " (${gb(size)})" : '') + (archive.exists() ? ' — resuming the earlier download' : '') + ' …')
    def curl = ['curl', '--fail', '--location', '--retry', '3', '--retry-delay', '5', '-o', archive.path]
    def downloaded = runCmd(curl + (archive.exists() ? ['-C', '-'] : []) + [meta.url]) == 0
    if (!downloaded && archive.exists() && archive.length() > 0) {
        warn('Resuming did not work; starting this download again from the beginning.')
        archive.delete()
        downloaded = runCmd(curl + [meta.url]) == 0
    }
    if (!downloaded) {
        fail("${name}: download failed (see the curl message above). Run setup again to retry; it resumes where it stopped.")
        return false
    }

    destDir.mkdirs()
    marker.text = "Download of ${meta.url} not finished. setup removes this folder and extracts again.\n"
    def extractCmd = isTarGz ?
        ['tar', 'xzf', archive.path, '-C', destDir.path] :
        ['unzip', '-q', archive.path, '-d', destDir.path]
    if (runCmd(extractCmd) != 0) {
        fail("${name}: the archive could not be extracted. It was removed, so the next setup run downloads it again.")
        archive.delete()
        return false
    }
    flattenSingleDirs(destDir)
    normalizeProteoBenchLayout(destDir)
    // Bruker .d datasets (diaPASEF) ship each run as a nested <run>.d.zip
    // inside the archive; unpack them into real .d directories so the
    // runner's format:d glob finds them.
    def badInner = destDir.listFiles()?.findAll { f -> f.isFile() && f.name.toLowerCase().endsWith('.d.zip') }?.findAll { z ->
        def unzipped = runCmd(['unzip', '-q', '-o', z.path, '-d', destDir.path]) == 0
        if (unzipped) z.delete()
        !unzipped
    }
    archive.delete()
    if (badInner) {
        fail("${name}: could not unpack ${badInner.collect { f -> f.name }.join(', ')}. The next setup run downloads it again.")
        return false
    }
    if (!msFiles(destDir, meta.format)) {
        fail("${name}: the archive contained no .${meta.format} files, so the download looks broken. The next setup run downloads it again.")
        return false
    }
    marker.delete()
    ok("${name}: downloaded → " + dim("${destDir}"))
    return true
}

// Extract a JAR matching `namePart` (case-insensitive) out of a user-supplied
// path — which may be a .zip, an already-extracted folder, or the jar itself.
// Returns [jar: File, tmp: temp dir to delete afterwards or null], or null.
def locateJar(pathStr, namePart) {
    def src = new File(expandHome(pathStr))
    if (!src.exists()) {
        fail("Path not found: ${pathStr}")
        return null
    }
    def searchDir = null
    def tmp = null
    if (src.isFile() && src.name.toLowerCase().endsWith('.zip')) {
        tmp = File.createTempDir()
        searchDir = tmp
        def zip = new java.util.zip.ZipFile(src)
        zip.entries().each { entry ->
            def outFile = new File(searchDir, entry.name)
            if (entry.isDirectory()) {
                outFile.mkdirs()
            } else {
                outFile.parentFile.mkdirs()
                outFile.withOutputStream { os -> os << zip.getInputStream(entry) }
            }
        }
        zip.close()
    } else if (src.isDirectory()) {
        searchDir = src
    } else if (src.isFile() && src.name.toLowerCase().endsWith('.jar')) {
        return src.name.toLowerCase().contains(namePart) ? [jar: src, tmp: null] : null
    } else {
        fail("Not a .zip, folder, or .jar: ${pathStr}")
        return null
    }
    def matches = []
    searchDir.eachFileRecurse { f ->
        if (f.isFile() && f.name.toLowerCase().endsWith('.jar') && f.name.toLowerCase().contains(namePart)) {
            matches << f
        }
    }
    if (!matches && tmp) tmp.deleteDir()
    return matches ? [jar: matches[0], tmp: tmp] : null
}

// Make sure MSFragger, IonQuant and diaTracer are in jarsDir, asking for each
// one that is missing. Returns true when all three are present.
def collectFragpipeJars(jarsDir) {
    jarsDir.mkdirs()
    def specs = [
        [key: 'msfragger', label: 'MSFragger', url: 'https://msfragger.nesvilab.org/upgrading_msfragger.html', param: params.msfragger_path],
        [key: 'ionquant',  label: 'IonQuant',   url: 'https://msfragger-upgrader.nesvilab.org/ionquant/',      param: params.ionquant_path],
        [key: 'diatracer', label: 'diaTracer',  url: 'https://msfragger-upgrader.nesvilab.org/diatracer/',     param: params.diatracer_path],
    ]
    def allFound = true
    specs.each { spec ->
        def already = jarsDir.listFiles()?.find { f -> f.name.toLowerCase().endsWith('.jar') && f.name.toLowerCase().contains(spec.key) }
        if (already) {
            ok("${spec.label}: already present " + dim("(${already.name})"))
            return
        }
        def path = spec.param
        def has = path ? true : askYesNo("Do you already have ${spec.label} downloaded (as a .zip or extracted folder)?", false)
        if (!has) {
            info("${spec.label} download (academic license): ${spec.url}")
            warn('Run setup again once it is downloaded (run the pipeline with --setup). FragPipe stays disabled until all three are present.')
            allFound = false
            return
        }
        if (!path) path = ask('  ' + cyan('?') + " Path to the ${spec.label} zip or folder: ")
        def found = path ? locateJar(path, spec.key) : null
        if (!found) {
            fail("${spec.label}: no matching .jar found in ${path} — FragPipe stays disabled for now.")
            allFound = false
            return
        }
        java.nio.file.Files.copy(found.jar.toPath(), new File(jarsDir, found.jar.name).toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
        ok("${spec.label}: copied " + dim(found.jar.name))
        // MSFragger ships an ext/ folder (Thermo .raw + Bruker .d native
        // readers) next to its jar; copy it too, else MSFragger can only
        // read mzML. Mounted next to the jar at run time by fragpipe.py.
        if (spec.key == 'msfragger') {
            def extSrc = new File(found.jar.parentFile, 'ext')
            if (extSrc.isDirectory()) {
                def extDst = new File(jarsDir, 'ext')
                extDst.deleteDir()
                runCmd(['cp', '-r', extSrc.path, extDst.path])
                ok('MSFragger native readers: copied ' + dim('ext/ (Thermo .raw + Bruker .d)'))
            } else {
                warn('No ext/ folder next to MSFragger — .raw/.d will not read; FragPipe will need mzML input.')
            }
        }
        found.tmp?.deleteDir()
    }
    return allFound
}

// Compare dotted version strings numerically ("1.9.2" < "2.0.2" < "2.5.1"), so
// sorting and >= checks don't fall back to string order ("1.9.2" > "1.10.0").
def versionParts(v) { return v.toString().split(/\./).collect { part -> (part =~ /^\d+/)[0] as int } }

def versionOrder(a, b) {
    def pa = versionParts(a)
    def pb = versionParts(b)
    def n = Math.max(pa.size(), pb.size())
    def diffs = (0..<n).collect { i -> (i < pa.size() ? pa[i] : 0) <=> (i < pb.size() ? pb[i] : 0) }
    return diffs.find { c -> c != 0 } ?: 0
}

// DIA-NN gained DDA support in 2.1.0; older releases are DIA-only.
def supportsDda(v) { return versionOrder(v, '2.1.0') >= 0 }

// True if a docker image (local or pulled) exists on this machine.
def imagePresent(image) {
    def p = new ProcessBuilder(strList(['docker', 'image', 'inspect', image])).redirectErrorStream(true).start()
    p.inputStream.text
    return p.waitFor() == 0
}

// Parse the dataset catalog (a flat name -> {url, acquisition, format,
// instrument} map). Hand-rolled instead of a real YAML parser since the
// shape is fixed and simple — no new dependency needed.
def loadCatalog(f) {
    def catalog = [:]
    def current = null
    f.eachLine { line ->
        if (!line.trim() || line.trim().startsWith('#')) return
        if (!line.startsWith(' ')) {
            current = [:]
            catalog[line.tokenize(':')[0].trim()] = current
        } else if (current != null) {
            def parts = line.trim().split(':', 2)
            if (parts.size() == 2) current[parts[0].trim()] = parts[1].trim()
        }
    }
    return catalog
}

// ── Reading and writing config.yaml as text ───────────────────────────────
// The config is edited as text, not re-serialised, so comments and layout the
// user wrote survive every rerun.

// The top-level `tools:` line (also `tools: {}`), or null.
def toolsKeyMatch(configText) {
    def m = java.util.regex.Pattern.compile(/(?m)^tools:[^\n]*(\n|\z)/).matcher(configText)
    return m.find() ? m : null
}

// Split a config's `tools:` section into per-tool block text, keyed by tool
// name in file order (block boundaries are the 2-space-indented `  <tool>:` lines).
def parseToolBlocks(configText) {
    def blocks = [:]
    def key = toolsKeyMatch(configText)
    if (key == null) return blocks
    def curName = null
    def curLines = []
    configText.substring(key.end()).eachLine { line ->
        def m = (line =~ /^  (\w+):\s*$/)
        if (m) {
            if (curName) blocks[curName] = curLines.join('\n')
            curName = m[0][1]
            curLines = [line]
        } else if (curName != null) {
            curLines << line
        }
    }
    if (curName) blocks[curName] = curLines.join('\n')
    return blocks
}

// Grab a tool block's `<key>:` sub-block (so a rewrite preserves e.g. dataset
// assignments instead of resetting them). Returns null if not found.
def extractSubBlock(toolBlock, key) {
    def pattern = java.util.regex.Pattern.compile(
        "(?ms)(^    " + java.util.regex.Pattern.quote(key.toString()) + ":.*?)(?=^    \\w|\\z)"
    )
    def m = pattern.matcher(toolBlock.toString())
    return m.find() ? m.group(1).replaceAll('\\n+$', '') + '\n\n' : null
}

def extractDatasetsSubBlock(toolBlock) { return extractSubBlock(toolBlock, 'datasets') }

// Parse a tool block's `versions:` entries back into maps, so a re-run can ADD
// versions to the existing list rather than replacing it — otherwise adding a
// second DIA-NN image would silently drop any `enabled: false` a user set by
// hand. Values stay strings; they are written back out verbatim.
def parseVersionEntries(toolBlock) {
    def sub = toolBlock ? extractSubBlock(toolBlock, 'versions') : null
    if (!sub) return []
    def entries = []
    sub.eachLine { line ->
        def m = (line =~ /^\s+(-\s+)?([\w]+):\s*(.*?)\s*$/)
        if (!m.matches() || m.group(2) == 'versions') return
        if (m.group(1)) entries << [:]
        if (entries) entries[-1][m.group(2)] = m.group(3).replaceAll(/\s+#.*$/, '').replaceAll(/^"|"$/, '')
    }
    return entries
}

// Replace (or add) `output_dir:` in the global section.
def setOutputDir(staticText, value) {
    def line = "  output_dir: ${yq(value)}"
    def m = java.util.regex.Pattern.compile(/(?m)^  output_dir:[^\n]*$/).matcher(staticText)
    if (m.find()) return staticText.substring(0, m.start()) + line + staticText.substring(m.end())
    return staticText.replaceFirst(/(?m)^global:[^\n]*\n/, java.util.regex.Matcher.quoteReplacement("global:\n${line}\n"))
}

def currentOutputDir(staticText) {
    def m = (staticText =~ /(?m)^  output_dir:[ \t]*([^\n]*)$/)
    return m.find() ? m.group(1).replaceAll(/\s+#.*$/, '').trim().replaceAll(/^"|"$/, '') : null
}

def renderDatasetEntry(name, d) {
    def sb = new StringBuilder("  ${name}:\n")
    sb << "    path: ${yq(d.path)}\n"
    sb << "    acquisition: ${d.acquisition}\n"
    sb << "    format: ${d.format}\n"
    sb << "    instrument: ${d.instrument}\n"
    if (d.fasta) sb << "    fasta: ${yq(d.fasta)}\n"
    if (d.fasta_decoy) sb << "    fasta_decoy: ${yq(d.fasta_decoy)}\n"
    return sb.toString()
}

// Update the `datasets:` section in place: replace the entries named in
// `replace` (with the resolved values) and append resolved datasets that have
// no entry yet. Every other line stays as it was. Returns the new text.
def mergeDatasets(staticText, resolved, replace) {
    def key = java.util.regex.Pattern.compile(/(?m)^datasets:[^\n]*(\n|\z)/).matcher(staticText)
    if (!key.find()) {
        error "No top-level 'datasets:' key found in config.yaml. Restore it (see config.template.yaml), then run setup again."
    }
    def body = staticText.substring(key.end())
    def entries = java.util.regex.Pattern.compile(/(?m)^  (\S+):[ \t]*\n((?:    [^\n]*(?:\n|\z))+)/).matcher(body).results().toList()
    def existing = entries.collect { r -> r.group(1) }
    if (!resolved.keySet().any { n -> !(n in existing) || n in replace }) return staticText
    // `datasets: {}` becomes a plain `datasets:` key once entries are added.
    def head = staticText.substring(0, key.start()) + 'datasets:\n'
    def out = new StringBuilder()
    def pos = 0
    entries.each { r ->
        def name = r.group(1)
        out << body.substring(pos, r.start())
        out << ((name in replace && resolved.containsKey(name)) ? renderDatasetEntry(name, resolved[name]) : r.group(0))
        pos = r.end()
    }
    def added = resolved.findAll { n, _d -> !(n in existing) }.collect { n, d -> '\n' + renderDatasetEntry(n, d) }.join('')
    if (!entries && added) added = added.substring(1)
    out << added
    out << body.substring(pos)
    return head + out.toString()
}

// Comment out `- <name>` items in the tools' datasets: lists, so a dataset
// the user chose not to download stops triggering setup.
def commentOutDatasets(toolsText, names) {
    return toolsText.readLines().collect { line ->
        def m = (line =~ /^(\s+)-\s+"?([^"\s#]+)"?\s*$/)
        (m.matches() && m.group(2) in names) ? "${m.group(1)}# - ${m.group(2)}   # not downloaded; remove the '# ' once it is on disk" : line
    }.join('\n') + (toolsText.endsWith('\n') ? '\n' : '')
}

// A new tool block's datasets: sub-block. Reuse the tool's existing
// assignment verbatim; otherwise fill from the datasets resolved this run.
def datasetsFor(tool, existingBlock, resolvedDatasets) {
    def preserved = existingBlock ? extractDatasetsSubBlock(existingBlock) : null
    if (preserved) return preserved
    def acqs = toolAcquisitions()[tool] ?: ['DDA', 'DIA']
    def names = resolvedDatasets.findAll { _n, d -> d.acquisition in acqs }.keySet()
    if (!names) return "    datasets: []   # add dataset names from the datasets: section above\n\n"
    return '    datasets:\n' + names.collect { n -> "      - ${n}\n" }.join('') + '\n'
}

// Add dataset names to an existing tool block's datasets: list (also turns
// `datasets: []` into a list), leaving the rest of the block as it was.
def addDatasetsToBlock(block, names) {
    def items = names.collect { n -> "      - ${n}".toString() }
    def lines = block.readLines()
    def k = lines.findIndexOf { l -> l ==~ /^    datasets:.*/ }
    if (k < 0) return block.replaceAll(/\n+$/, '') + '\n\n    datasets:\n' + items.join('\n')
    def insertAt = k + 1
    if (lines[k] ==~ /^    datasets:\s*\[\s*\].*/) {
        lines[k] = '    datasets:'
    } else {
        def end = ((k + 1)..<lines.size()).find { i -> lines[i].trim() && !lines[i].startsWith('      ') } ?: lines.size()
        def lastItem = ((k + 1)..<end).findAll { i -> lines[i] ==~ /^\s+-\s+.*/ }.max()
        if (lastItem != null) insertAt = lastItem + 1
    }
    return (lines.take(insertAt) + items + lines.drop(insertAt)).join('\n')
}

def extraFor(existingBlock, fallback) { return extractSubBlock(existingBlock ?: '', 'extra') ?: fallback }

// Render the config block of a tool that was set up from scratch this run.
def renderTool(tool, r, existingBlock, resolvedDatasets) {
    def sb = new StringBuilder("  ${tool}:\n    versions:\n")
    if (tool == 'diann') {
        r.versions.each { v -> sb << renderDiannVersion(v) << '\n' }
        sb << datasetsFor(tool, existingBlock, resolvedDatasets)
        sb << extraFor(existingBlock, "    extra:\n      library: \"\"\n\n")
    } else if (tool == 'alphadia') {
        sb << "      - id: \"latest\"\n        image: ${r.image}\n        gpu: ${r.gpu}\n        enabled: true\n\n"
        sb << datasetsFor(tool, existingBlock, resolvedDatasets)
        sb << extraFor(existingBlock, "    extra:\n      library: \"\"\n\n")
    } else if (tool == 'sage') {
        sb << "      - id: \"latest\"\n        image: ${r.image}\n        sage_bin: ${r.sage_bin}\n        enabled: true\n\n"
        sb << datasetsFor(tool, existingBlock, resolvedDatasets)
        sb << extraFor(existingBlock, "    extra:\n      write_pin: true\n      parquet: false\n\n")
    } else if (tool == 'fragpipe') {
        sb << "      - id: \"${r.id}\"\n        image: ${r.image}\n"
        sb << "        fragpipe_root: ${r.fragpipe_root}\n        jars_dir: ${yq(r.jars_dir)}\n"
        sb << "        container_python: /usr/bin/python3\n        enabled: ${r.ready}\n\n"
        sb << datasetsFor(tool, existingBlock, resolvedDatasets)
        sb << extraFor(existingBlock, "    extra:\n      dda_workflow: LFQ-MBR\n      dia_workflow: DIA_SpecLib_Quant\n      dia_pasef_workflow: DIA_SpecLib_Quant_diaPASEF\n\n")
    } else if (tool == 'maxquant') {
        r.eachWithIndex { v, i ->
            sb << "      - id: \"${v.id}\"\n        image: ${v.image}\n"
            sb << "        maxquant_dll: ${v.maxquant_dll}\n        enabled: ${i == 0}\n"
        }
        sb << "\n"
        sb << datasetsFor(tool, existingBlock, resolvedDatasets)
        sb << extraFor(existingBlock, "    extra: {}\n\n")
    } else if (tool == 'metamorpheus') {
        sb << "      - id: \"latest\"\n        image: ${r.image}\n        enabled: true\n\n"
        sb << datasetsFor(tool, existingBlock, resolvedDatasets)
        sb << extraFor(existingBlock, "    extra: {}\n\n")
    }
    return sb.toString()
}

// ── Datasets step ─────────────────────────────────────────────────────────
// Offers to download+unzip catalog datasets relevant to the tools just set up
// (per `results`, scoped by acquisition via toolAcquisitions()), plus the
// `required` ones: datasets an enabled tool already uses that are not on disk.
// `tools` are the tool names whose datasets are offered. Returns
// [resolved: name -> [path, fasta, fasta_decoy, acquisition, format, instrument],
//  downloaded: names downloaded in this run].
def downloadDatasets(tools, required, dataDir) {
    def resolvedDatasets = [:]
    def downloaded = []
    if (params.skip_datasets || (tools.isEmpty() && !required)) {
        info('No datasets to download' + (params.skip_datasets ? ' (--skip_datasets given).' : ' for this run.'))
        return [resolved: resolvedDatasets, downloaded: downloaded]
    }

    def acquisitions = toolAcquisitions()
    def catalogF = catalogFile()
    def catalog = catalogF.exists() ? loadCatalog(catalogF) : [:]
    def enabledAcqs = tools.collect { tool -> acquisitions[tool] ?: ['DDA', 'DIA'] }.flatten().toSet()
    def relevant = catalog.findAll { name, meta -> meta.acquisition in enabledAcqs || name in required }
    if (!relevant) {
        warn('No catalog datasets are relevant to the tools you just set up — skipping.')
        return [resolved: resolvedDatasets, downloaded: downloaded]
    }

    info('Datasets are stored in ' + bold("${dataDir}") + dim(' (change with --data_dir)'))
    def present = relevant.findAll { name, _meta -> isDatasetPresent(new File(dataDir, name)) }
    def missing = relevant.findAll { name, _meta -> !present.containsKey(name) }
    if (present) ok('Already on disk: ' + present.keySet().join(', '))

    def toDownload = [:]
    if (missing) {
        if (params.download_datasets) {
            def wanted = params.download_datasets.toString() == 'all' ?
                missing.keySet() : params.download_datasets.toString().split(',').collect { w -> w.trim() }
            toDownload = missing.findAll { name, _meta -> name in wanted }
        } else if (!isInteractive()) {
            warn('Skipping dataset download in non-interactive mode (pass --download_datasets all|name1,name2 to opt in).')
        } else {
            info('Datasets you can download:')
            def names = missing.keySet().toList()
            names.eachWithIndex { name, i ->
                def meta = missing[name]
                def size = meta.url ? remoteSize(meta.url) : -1L
                def note = name in required ? yellow('  used by an enabled tool, but missing') : ''
                println '      ' + bold("${i + 1}.") + " ${name}  " + dim("(${meta.acquisition}, ${meta.format}, ${meta.instrument}" + (size > 0 ? ", ${gb(size)}" : '') + ')') + note
            }
            info("Free disk space in ${dataDir.exists() ? dataDir : dataDir.parentFile}: ${gb((dataDir.exists() ? dataDir : dataDir.parentFile).usableSpace)}")
            def answer = ask('  ' + cyan('?') + ' Download which of these? ' + dim('[all/none/1,2,...]') + ' (default: all): ')?.toLowerCase()
            if (!answer || answer == 'all') {
                toDownload = missing
            } else if (answer != 'none') {
                def idx = answer.split(',').collect { a -> a.trim() }.findAll { a -> a.isInteger() }.collect { a -> a.toInteger() - 1 }
                toDownload = missing.findAll { name, _meta -> names.indexOf(name) in idx }
            }
        }
    }

    if (toDownload) {
        def needed = ['curl', 'tar', 'unzip'].findAll { c -> !hasCmd(c) }
        if (needed) {
            fail("Downloading datasets needs ${needed.join(', ')}, which is not installed. Install it (for example: sudo apt install ${needed.join(' ')}) and run setup again.")
            toDownload = [:]
        }
        dataDir.mkdirs()
        toDownload.each { name, meta -> if (downloadOne(name, meta, dataDir)) downloaded << name }
    }

    // Resolve every present dataset (already-there + freshly downloaded this run).
    relevant.each { name, meta ->
        def destDir = new File(dataDir, name)
        if (!isDatasetPresent(destDir)) return   // never (fully) downloaded — leave untouched
        normalizeProteoBenchLayout(destDir)   // also fixes datasets downloaded before this step existed

        def files = (destDir.listFiles() ?: []).toList().sort { f -> f.name }
        def fastas = files.findAll { f -> isFastaName(f.name) && !f.name.toLowerCase().contains('decoy') }
        def fastaDecoy = files.find { f -> isFastaName(f.name) && f.name.toLowerCase().contains('decoy') }
        if (fastas.size() > 1) warn("${name}: several FASTA files found; using ${fastas[0].name}. Change 'fasta:' in config.yaml if that is wrong.")

        resolvedDatasets[name] = [
            path: destDir.absolutePath, fasta: fastas ? fastas[0].absolutePath : null, fasta_decoy: fastaDecoy?.absolutePath,
            acquisition: meta.acquisition, format: meta.format, instrument: meta.instrument,
        ]

        // mzml/ subfolder → move to a sibling "<name>_mzml" dir. This is not a
        // separate dataset entry: runners fall back to it automatically (see
        // BaseRunner.requires_mzml/_mzml_search_dirs) whenever a tool can't
        // read the dataset's native format and needs mzML instead.
        def mzmlSub = new File(destDir, 'mzml')
        if (mzmlSub.isDirectory() && mzmlSub.list()) {
            def mzmlSibling = new File(dataDir, "${name}_mzml")
            if (!mzmlSibling.exists()) mzmlSub.renameTo(mzmlSibling)
        }
    }

    return [resolved: resolvedDatasets, downloaded: downloaded]
}

def printStatus(ctx) {
    def labels = toolLabels()
    def tools = ctx.status.tools
    def names = { m -> m.keySet().collect { n -> labels[n] ?: n }.join(', ') }
    section('What needs doing')
    def complete = tools.findAll { _n, t -> t.status == 'complete' }
    def disabled = tools.findAll { _n, t -> t.status == 'disabled' }
    def absent   = labels.findAll { n, _l -> !tools.containsKey(n) }
    if (complete) ok('Set up and complete: ' + names.call(complete))
    tools.findAll { _n, t -> t.status == 'incomplete' }.each { n, t ->
        def why = t.problems.collect { pr ->
            pr.reason == 'image' ? "docker image ${pr.image} is not on this machine" :
            pr.reason == 'jars'  ? 'licensed MSFragger/IonQuant/diaTracer JARs are missing' :
                                   'its entry in config.yaml is incomplete'
        }.unique()
        warn("${labels[n] ?: n}: " + why.join('; '))
    }
    if (disabled) info('In config.yaml but disabled: ' + names.call(disabled))
    if (absent)   info('Not set up: ' + absent.values().join(', '))
    if (ctx.status.missing_datasets) warn('Datasets used by enabled tools, but not on disk: ' + ctx.status.missing_datasets.join(', '))
    if (ctx.fromGate && (disabled || absent)) {
        info('Only the problems above are fixed now. To add or re-enable a tool, run the pipeline with ' + bold('--setup') + '.')
    } else if (disabled || absent) {
        info('Tools that are not set up are offered below; press Enter to skip them.')
    }
}

workflow SETUP {
    take:
    fromGate   // true when proteobench.nf started setup because something is missing

    main:
    def interactive = isInteractive()
    def results = [:]    // tool name -> facts collected for a tool set up from scratch this run
    def repaired = [:]   // tool name -> its existing block, with only enabled flags changed

    banner('ProteoBench · Docker setup', 'search engines run in Docker — no native installs')
    println ''
    println dim('  Pick the tools you want; this wizard pulls their images and writes')
    println dim('  config.yaml. Press Enter to accept the default answer (the capital letter).')

    section('Checking this computer')
    checkPrerequisites()
    ok('Docker is running and usable')
    ok('python3 with pyyaml is available')

    def configFile = new File(params.config as String).absoluteFile
    def firstRun   = !configFile.exists()
    def baseText   = firstRun ? new File("${projectDir}/config.template.yaml").text : configFile.text
    def existingToolBlocks = firstRun ? [:] : parseToolBlocks(baseText)
    def status = firstRun ? [tools: [:], missing_datasets: []] : setupStatus(configFile)
    def ctx = [firstRun: firstRun, fromGate: fromGate, status: status]
    info((firstRun ? 'Creating ' : 'Using ') + bold("${configFile}") + dim(' (change with --config)'))
    if (!firstRun) printStatus(ctx)

    // ── MaxQuant ──────────────────────────────────────────────────────────
    section('MaxQuant')
    def mqPlan = toolPlan('maxquant', ctx)
    if (mqPlan == 'repair') {
        repaired.maxquant = repairTool('maxquant', status.tools.maxquant, existingToolBlocks.maxquant)
    } else if (wantTool(mqPlan, 'maxquant', '(quay.io/medbioinf/maxquant:2.6.3.0 + :2.8.1.0, Max Planck academic license)', ctx)) {
        results.maxquant = []
        // Pin explicit version tags: :latest drifts (it currently points at 2.8.1.0),
        // which would break the recorded maxquant_dll path for the older version.
        ['quay.io/medbioinf/maxquant:2.6.3.0', 'quay.io/medbioinf/maxquant:2.8.1.0'].each { mqImage ->
            if (runCmd(['docker', 'pull', mqImage]) == 0) {
                def found = firstLine(capture(['docker', 'run', '--rm', '--entrypoint', 'find', mqImage, '/opt', '-maxdepth', '3', '-iname', 'MaxQuantCmd.dll']))
                def ver = mqImage.tokenize(':')[-1]
                def dll = found ?: "/opt/MaxQuant_v${ver}/bin/MaxQuantCmd.dll".toString()
                results.maxquant << [id: ver, image: mqImage, maxquant_dll: dll]
                ok("MaxQuant ready " + dim("(${mqImage})"))
            } else {
                fail("MaxQuant pull failed for ${mqImage} — skipping.")
            }
        }
        if (!results.maxquant) results.remove('maxquant')
    } else {
        notDoneMessage(mqPlan, 'maxquant')
    }

    // ── Sage ──────────────────────────────────────────────────────────────
    section('Sage')
    def sagePlan = toolPlan('sage', ctx)
    if (sagePlan == 'repair') {
        repaired.sage = repairTool('sage', status.tools.sage, existingToolBlocks.sage)
    } else if (wantTool(sagePlan, 'sage', '(ghcr.io/lazear/sage:latest)', ctx)) {
        def image = 'ghcr.io/lazear/sage:latest'
        if (runCmd(['docker', 'pull', image]) == 0) {
            def found = firstLine(capture(['docker', 'run', '--rm', '--entrypoint', 'find', image, '/app', '-maxdepth', '1', '-type', 'f', '-name', 'sage']))
            results.sage = [image: image, sage_bin: found ?: '/app/sage']
            ok("Sage ready " + dim("(${image})"))
        } else {
            fail('Sage pull failed — skipping.')
        }
    } else {
        notDoneMessage(sagePlan, 'sage')
    }

    // ── MetaMorpheus ──────────────────────────────────────────────────────
    section('MetaMorpheus')
    def mmPlan = toolPlan('metamorpheus', ctx)
    if (mmPlan == 'repair') {
        repaired.metamorpheus = repairTool('metamorpheus', status.tools.metamorpheus, existingToolBlocks.metamorpheus)
    } else if (wantTool(mmPlan, 'metamorpheus', '(smithchemwisc/metamorpheus:latest)', ctx)) {
        def image = 'smithchemwisc/metamorpheus:latest'
        if (runCmd(['docker', 'pull', image]) == 0) {
            results.metamorpheus = [image: image]
            ok("MetaMorpheus ready " + dim("(${image})"))
        } else {
            fail('MetaMorpheus pull failed — skipping.')
        }
    } else {
        notDoneMessage(mmPlan, 'metamorpheus')
    }

    // ── AlphaDIA ──────────────────────────────────────────────────────────
    section('AlphaDIA')
    def adPlan = toolPlan('alphadia', ctx)
    if (adPlan == 'repair') {
        repaired.alphadia = repairTool('alphadia', status.tools.alphadia, existingToolBlocks.alphadia)
    } else if (wantTool(adPlan, 'alphadia', '(mannlabs/alphadia:latest)', ctx)) {
        def image = 'mannlabs/alphadia:latest'
        if (runCmd(['docker', 'pull', image]) == 0) {
            def gpu = params.alphadia_gpu ?: askYesNo('Do you have an NVIDIA GPU with the NVIDIA Container Toolkit set up for docker?', false)
            results.alphadia = [image: image, gpu: gpu]
            ok("AlphaDIA ready " + dim("(${image}, gpu=${gpu})"))
        } else {
            fail('AlphaDIA pull failed — skipping.')
        }
    } else {
        notDoneMessage(adPlan, 'alphadia')
    }

    // ── FragPipe (image + separately-licensed MSFragger/IonQuant/diaTracer) ─
    section('FragPipe')
    def fpPlan = toolPlan('fragpipe', ctx)
    if (fpPlan == 'repair') {
        repaired.fragpipe = repairTool('fragpipe', status.tools.fragpipe, existingToolBlocks.fragpipe)
    } else if (!params.skip_fragpipe && wantTool(fpPlan, 'fragpipe', '(MSFragger/IonQuant/diaTracer need a separate Nesvilab academic license)', ctx)) {
        def image = 'fcyucn/fragpipe:latest'
        if (runCmd(['docker', 'pull', image]) == 0) {
            def rootFound = firstLine(capture(['docker', 'run', '--rm', '--entrypoint', 'find', image, '/fragpipe_bin', '-maxdepth', '4', '-type', 'f', '-name', 'fragpipe']))
            def fragpipeRoot = rootFound ? new File(rootFound).parentFile.parentFile.path : '/fragpipe_bin/fragpipe-24.0/fragpipe-24.0'
            def fpVersion = (fragpipeRoot =~ /fragpipe-(\d[\d.]*)/).with { m -> m.find() ? m.group(1) : 'latest' }
            // Reuse the JAR folder of an earlier FragPipe entry, else jars go next to config.yaml.
            def oldJars = parseVersionEntries(existingToolBlocks.fragpipe).collect { v -> v.jars_dir ? new File(expandHome(v.jars_dir)) : null }.find { d -> d?.isDirectory() }
            def jarsDir = oldJars ?: new File(configDirOf(configFile), 'tools/fragpipe_jars')
            info('Licensed JARs are kept in ' + dim("${jarsDir}"))
            def allFound = collectFragpipeJars(jarsDir)
            results.fragpipe = [id: fpVersion, image: image, fragpipe_root: fragpipeRoot, jars_dir: jarsDir.absolutePath, ready: allFound]
            if (allFound) ok('FragPipe ready.')
            else          warn('FragPipe image pulled, but licensed JARs are missing — it is written as disabled until you add them.')
        } else {
            fail('FragPipe pull failed — skipping.')
        }
    } else {
        notDoneMessage(fpPlan, 'fragpipe')
    }

    // ── DIA-NN (1.8.1 public image + optional 2.x built locally) ─────────
    section('DIA-NN')
    // --build_diann_v2 is an explicit "I want these versions" request, so it
    // re-opens the DIA-NN step even when the existing block is already complete
    // (that is how a version is added to a working setup).
    def dnPlan = toolPlan('diann', ctx)
    if (dnPlan == 'keep' && params.build_diann_v2) dnPlan = 'add'
    if (dnPlan in ['keep', 'skip']) {
        notDoneMessage(dnPlan, 'diann')
    } else {
        // Start from what is already configured; versions found below are added
        // to this list. Only versions with `enabled: true` are checked, matching
        // config_validator.py; disabled versions are never pulled or rebuilt.
        def versions = parseVersionEntries(existingToolBlocks.diann)
        def setUp = dnPlan in ['repair', 'add'] ||
            (dnPlan == 'new' && (selectedTools() != null ? 'diann' in selectedTools() :
                askYesNo('Set up DIA-NN? ' + dim('(1.8.1 public image; 2.x can be built locally)'), true))) ||
            (dnPlan == 'offer' && wantTool('offer', 'diann', '(1.8.1 public image; 2.x can be built locally)', ctx))
        if (!setUp) {
            warn('Skipping DIA-NN.')
        } else {
            if (dnPlan == 'offer') {
                versions.findAll { v -> v.enabled != 'true' }.each { v ->
                    if (askYesNo("Enable DIA-NN ${v.id} again? " + dim("(${v.image})"), true)) v.enabled = 'true'
                }
            }
            def disabledVersions = versions.findAll { v -> v.enabled != 'true' }
            def enabledVersions  = versions.findAll { v -> v.enabled == 'true' }
            def missingEnabled   = enabledVersions.findAll { v -> !imagePresent(v.image) }
            def presentEnabled   = enabledVersions - missingEnabled

            if (presentEnabled)   ok("Enabled and present: " + presentEnabled.collect { v -> v.id }.join(', '))
            if (disabledVersions) info("Disabled in config.yaml (not checked): " + disabledVersions.collect { v -> v.id }.join(', '))
            if (missingEnabled) {
                warn('These DIA-NN versions are ' + bold('enabled: true') + ' in config.yaml, but their docker image is not on this machine:')
                missingEnabled.each { v -> println '      - ' + bold(v.id) + ' ' + dim("(${v.image})") }
            }
            def haveVersion = { id -> versions.any { v -> v.id == id } }
            def disableVersion = { v ->
                if (interactive && askYesNo("Set DIA-NN ${v.id} to enabled: false in config.yaml, so setup stops asking about it?", true)) {
                    v.enabled = 'false'
                    ok("DIA-NN ${v.id} set to " + bold('enabled: false'))
                } else {
                    warn("DIA-NN ${v.id} stays enabled without an image, so setup will ask again on the next run.")
                }
            }

            // Repair enabled versions whose image is missing. Locally built images
            // (diann:<id>) are queued for the build step below; others are pulled.
            def toBuild = []
            missingEnabled.each { v ->
                def localBuild = v.image == "diann:${v.id}".toString()
                def action = localBuild ? 'Build' : 'Pull'
                if (!askYesNo("${action} DIA-NN ${v.id} now? " + dim("(enabled in config.yaml, image ${v.image} missing)"), true)) {
                    disableVersion.call(v)
                } else if (localBuild) {
                    toBuild << v
                } else if (runCmd(['docker', 'pull', v.image]) == 0) {
                    ok("DIA-NN ${v.id} ready " + dim("(${v.image})"))
                } else {
                    fail("DIA-NN ${v.id} pull failed — see the message above.")
                    disableVersion.call(v)
                }
            }

            // The public 1.8.1 image is added on a fresh setup; a repair only
            // fixes what is configured.
            def baseImage = 'biocontainers/diann:v1.8.1_cv1'
            if (dnPlan in ['repair', 'add'] || haveVersion.call('1.8.1')) {
                // repaired above if enabled and missing; never re-added on a repair
            } else if (runCmd(['docker', 'pull', baseImage]) == 0) {
                def found = firstLine(capture(['docker', 'run', '--rm', '--entrypoint', 'find', baseImage, '/usr', '-maxdepth', '3', '-iname', 'diann', '-type', 'f']))
                versions << [id: '1.8.1', image: baseImage, diann_bin: found ?: '/usr/diann/1.8.1/diann', supports_dda: false]
                ok("DIA-NN 1.8.1 ready " + dim("(${baseImage})"))
            } else {
                fail('DIA-NN 1.8.1 pull failed.')
            }

            // DIA-NN 2.x images cannot be publicly distributed (DIA-NN license), so
            // they are built locally from the bigbio/quantms-containers Dockerfiles.
            // Those Dockerfiles download DIA-NN itself from the public vdemichev/DiaNN
            // releases — no registry token or account is needed.
            def wantsV2 = params.build_diann_v2 || (dnPlan != 'repair' && askYesNo(
                'Also build additional DIA-NN 2.x image(s) locally? (needed for DDA support and native Thermo .raw on Linux)', false))

            if ((toBuild || wantsV2) && !hasCmd('git')) {
                fail('Building DIA-NN 2.x needs git, which is not installed. Install it (for example: sudo apt install git) and run setup again.')
                toBuild.each { v -> disableVersion.call(v) }
            } else if (toBuild || wantsV2) {
                info('DIA-NN 2.x is built locally from the bigbio/quantms-containers recipes,')
                info('which download the DIA-NN Academia release during the build.')
                info('By continuing you accept the DIA-NN license: ' + dim('https://github.com/vdemichev/DiaNN'))

                def wanted = params.diann_version.toString().split(',').collect { w -> w.trim() }.findAll { w -> w }
                def buildDir = File.createTempDir()
                if (runCmd(['git', 'clone', '--depth', '1', 'https://github.com/bigbio/quantms-containers.git', buildDir.path]) != 0) {
                    fail('Could not clone bigbio/quantms-containers (needs a network connection). Skipping DIA-NN 2.x.')
                    toBuild.each { v -> disableVersion.call(v) }
                } else {
                    // Recipe folders are named diann-<version>; the enterprise variant is
                    // per-user licensed and cannot be built from a plain clone, so hide it.
                    def avail = (buildDir.listFiles() ?: []).findAll { d ->
                        d.isDirectory() && d.name.startsWith('diann-') && !d.name.contains('enterprise')
                    }.collect { d -> d.name.replace('diann-', '') }.toSorted { a, b -> versionOrder(a, b) }

                    // Rebuild the enabled versions whose image was missing (already confirmed above).
                    toBuild.each { v ->
                        if (!avail.contains(v.id)) {
                            fail("No build recipe for DIA-NN ${v.id}. Available: ${avail.join(', ')}.")
                            disableVersion.call(v)
                        } else if (runCmd(['docker', 'build', '-t', v.image, new File(buildDir, "diann-${v.id}").path]) == 0) {
                            ok("DIA-NN ${v.id} ready " + dim("(${v.image}, built locally)"))
                        } else {
                            fail("DIA-NN ${v.id} build failed — see the output above.")
                            disableVersion.call(v)
                        }
                    }

                    if (wantsV2 && interactive) {
                        info('Available recipes: ' + avail.join(', '))
                        def a = ask('  ' + cyan('?') + " Which version(s) to build? " + dim("(comma-separated) [${wanted.join(',')}] "))
                        if (a) wanted = a.split(',').collect { w -> w.trim() }.findAll { w -> w }
                    }

                    (wantsV2 ? wanted : []).each { ver ->
                        if (haveVersion.call(ver)) {
                            ok("DIA-NN ${ver} already configured " + dim('(left as-is)'))
                            return
                        }
                        if (!avail.contains(ver)) {
                            fail("No build recipe for DIA-NN ${ver}. Available: ${avail.join(', ')}.")
                            return
                        }
                        // Path is fixed by every recipe (ln -s .../diann-linux .../diann); no find needed.
                        def entry = [id: ver, image: "diann:${ver}".toString(),
                                     diann_bin: "/usr/diann-${ver}/diann".toString(), supports_dda: supportsDda(ver)]
                        if (imagePresent(entry.image)) {
                            versions << entry
                            ok("DIA-NN ${ver} already built locally " + dim("(${entry.image})"))
                        } else if (askYesNo("Build DIA-NN ${ver} now? " + dim('(downloads a few hundred MB, takes a few minutes)'), true)) {
                            if (runCmd(['docker', 'build', '-t', entry.image, new File(buildDir, "diann-${ver}").path]) == 0) {
                                versions << entry
                                ok("DIA-NN ${ver} ready " + dim("(${entry.image}, built locally)"))
                            } else {
                                fail("DIA-NN ${ver} build failed — see the output above.")
                            }
                        }
                    }
                }
                buildDir.deleteDir()
            } else if (!versions.any { v -> v.id != '1.8.1' }) {
                warn('Only DIA-NN 1.8.1 will be configured.')
            }

            // An existing block is edited in place (enabled flags, appended
            // versions), so per-version keys such as extra_args survive.
            def orig = parseVersionEntries(existingToolBlocks.diann)
            if (existingToolBlocks.diann) {
                def was = orig.collectEntries { v -> [(v.id): v.enabled] }
                def block = setEnabled(existingToolBlocks.diann, versions.findAll { v -> was.containsKey(v.id) && was[v.id] == 'true' && v.enabled == 'false' }.collect { v -> v.id }, false)
                block = setEnabled(block, versions.findAll { v -> was.containsKey(v.id) && was[v.id] != 'true' && v.enabled == 'true' }.collect { v -> v.id }, true)
                def added = versions.findAll { v -> !was.containsKey(v.id) }
                repaired.diann = added ? appendVersions(block, added.collect { v -> renderDiannVersion(v) }.join('\n')) : block
            } else if (versions) {
                results.diann = [versions: versions]
            }
        }
    }

    // ── Datasets (optional automatic download) ────────────────────────────
    section('Datasets')
    def dataDir = dataDirFor(configFile)
    def missingDatasets = status.missing_datasets
    // Datasets are offered for the tools set up now and, unless the gate
    // started setup, for the enabled tools that were already there.
    def enabledBefore = status.tools.findAll { _n, t -> t.status in ['complete', 'incomplete'] }.keySet()
    def dl = downloadDatasets((results.keySet() + (fromGate ? [] : enabledBefore)).unique(), missingDatasets, dataDir)
    def resolvedDatasets = dl.resolved
    def stillMissing = missingDatasets.findAll { n -> !resolvedDatasets.containsKey(n) }
    def dropDatasets = []
    if (stillMissing && interactive) {
        warn('Still not on disk: ' + stillMissing.join(', '))
        if (askYesNo('Remove these from the tools\' dataset lists in config.yaml, so the pipeline stops asking? ' + dim('(they are commented out, not deleted)'), true)) {
            dropDatasets = stillMissing
        }
    }

    // ── Write config.yaml ─────────────────────────────────────────────────
    // The part before `tools:` (global/search_params/datasets) is kept as
    // text; only output_dir and the dataset entries resolved this run change.
    def toolsKey = toolsKeyMatch(baseText)
    def globalKey = java.util.regex.Pattern.compile(/(?m)^global:/).matcher(baseText)
    def staticText = baseText.substring(firstRun && globalKey.find() ? globalKey.start() : 0, toolsKey ? toolsKey.start() : baseText.length())
    if (firstRun) {
        staticText = '# =============================================================================\n' +
            '# Written by the ProteoBench setup wizard. To add a tool later, run the pipeline\n' +
            '# with --setup. Edit anything below by hand; setup keeps your changes.\n' +
            '# =============================================================================\n\n' + staticText
    }

    // Dataset entries are replaced with the resolved (downloaded) paths when
    // they are still defaults: every entry on a first run (template), or a
    // CHANGE_ME / missing entry on a rerun. A working entry is never changed.
    def replace = resolvedDatasets.keySet().findAll { n ->
        firstRun || n in missingDatasets ||
            java.util.regex.Pattern.compile("(?m)^  ${java.util.regex.Pattern.quote(n)}:\\s*\\n(?:    [^\\n]*\\n)*?    (path|fasta):[^\\n]*CHANGE_ME").matcher(staticText).find()
    }
    staticText = mergeDatasets(staticText, resolvedDatasets, replace)

    // global.output_dir: asked on a first run, or while it is still unset.
    def od = currentOutputDir(staticText)
    if (firstRun || !od || od.contains('CHANGE_ME')) {
        section('Results folder')
        def dflt = params.output_dir ?: new File(configDirOf(configFile), 'results').path
        def answer = (interactive && !params.output_dir) ? ask('  ' + cyan('?') + ' Where should the results go? ' + dim("[${dflt}] ")) : null
        def chosen = absoluteFrom(configFile, answer ?: dflt)
        staticText = setOutputDir(staticText, chosen.path)
        ok('Results go to ' + dim("${chosen}"))
    }

    // Tools in file order, then new ones in the standard order. Complete and
    // unknown tools are written back exactly as they were.
    def order = (existingToolBlocks.keySet().toList() + toolLabels().keySet().toList()).unique()
    // Datasets downloaded now are added to the lists of the tools that were
    // already set up and can use them.
    def blockFor = { tool ->
        if (results.containsKey(tool)) return renderTool(tool, results[tool], existingToolBlocks[tool], resolvedDatasets)
        def b = repaired.containsKey(tool) ? repaired[tool] : existingToolBlocks[tool]
        if (b == null || !(tool in enabledBefore)) return b
        def listed = extractDatasetsSubBlock(b) ?: ''
        def add = dl.downloaded.findAll { n ->
            resolvedDatasets.containsKey(n) && resolvedDatasets[n].acquisition in (toolAcquisitions()[tool] ?: ['DDA', 'DIA']) &&
                !listed.readLines().collect { l -> l.trim().replace('"', '') }.any { l -> l.startsWith('-') && l.substring(1).trim() == n }
        }
        if (add) info("Added ${add.join(', ')} to the datasets of ${toolLabels()[tool] ?: tool}.")
        return add ? addDatasetsToBlock(b, add) : b
    }
    def newBlocks = order.collectEntries { tool -> [(tool): blockFor.call(tool)] }.findAll { _t, b -> b != null }
    def blocks = newBlocks.values().collect { b -> b.replaceAll(/\n+$/, '') }
    def toolsChanged = firstRun || !toolsKey || newBlocks.any { tool, b -> b != existingToolBlocks[tool] }
    def toolsText = !toolsChanged ? baseText.substring(toolsKey.start()) :
        blocks ? 'tools:\n\n' + blocks.join('\n\n') + '\n' :
        // An empty mapping value ('tools:' with nothing under it) parses as
        // YAML null, not {} — write the explicit empty-map form.
        'tools: {}\n'
    if (dropDatasets) toolsText = commentOutDatasets(toolsText, dropDatasets)

    def newText = staticText + toolsText
    banner('Setup complete', results ? "set up: ${results.keySet().collect { t -> toolLabels()[t] }.join(', ')}" : (newText == baseText ? 'no changes' : 'config.yaml updated'))
    if (!firstRun && newText == baseText) {
        ok('config.yaml is up to date — nothing changed.')
    } else {
        if (!firstRun) new File("${configFile.path}.bak").text = baseText
        configFile.parentFile.mkdirs()
        configFile.text = newText
        ok('Wrote ' + dim("${configFile}"))
        if (!firstRun) info('The previous version is saved as ' + dim("${configFile.path}.bak"))
    }
}

// Only used when running `nextflow run setup.nf` directly; ignored when this
// file is included as a module (e.g. by proteobench.nf), since only the
// including script's own workflow{} executes in that case.
workflow {
    SETUP(false)
    println ''
    println bold('  Next step:') + ' start the runs with ' + bold('nextflow run proteobench.nf') + dim(' (same --config)')
    println ''
}
