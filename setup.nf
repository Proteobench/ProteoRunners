#!/usr/bin/env nextflow
// Interactive Docker setup for the ProteoBench pipeline. Replaces the old
// setup.py: every search engine now runs from a docker image instead of a
// natively compiled/installed binary, so this script's only job is to pull
// the right images and collect the FragPipe/DIA-NN license extras.
//
// Normally you never run this file directly — proteobench.nf includes it as
// the SETUP workflow and calls it automatically the first time config.yaml
// doesn't exist yet, or whenever an enabled tool's docker setup looks
// incomplete (see the completeness check at the top of proteobench.nf).
//
// Direct/manual use (e.g. to force a full re-check outside of a real run):
//   nextflow run setup.nf                      # interactive, guided setup
//   nextflow run setup.nf --non_interactive \
//       --msfragger_path ... --ionquant_path ... --diatracer_path ... \
//       --build_diann_v2 --diann_version 2.1.0,2.5.0   # scripted / CI setup
//
// On a first run it writes config.yaml. On a later run it only re-prompts and
// redoes the tools whose docker setup is incomplete, preserves already-complete
// tools and the global/search_params/datasets sections, and updates config.yaml
// in place (keeping a .bak copy).
//
// Docker must already be installed and running — see README.md.

nextflow.enable.dsl = 2

import java.nio.file.Files
import java.nio.file.StandardCopyOption

params.config           = params.config ?: "${projectDir}/config.yaml"
params.non_interactive = false
params.skip_fragpipe    = false
params.msfragger_path   = null   // path to a MSFragger .zip or extracted folder
params.ionquant_path    = null   // path to an IonQuant .zip or extracted folder
params.diatracer_path   = null   // path to a diaTracer .zip or extracted folder
params.diann_version    = '2.5.0'   // DIA-NN 2.x version(s) to build locally, comma-separated
                                    // (recipe names from bigbio/quantms-containers, e.g. '2.1.0,2.5.0')
params.build_diann_v2   = false     // non-interactive opt-in to build the DIA-NN 2.x image(s)
params.alphadia_gpu     = false
params.skip_datasets     = false
params.data_dir          = null   // where downloaded datasets go; default: ${projectDir}/data
params.download_datasets = null   // "all", or a comma-separated list of dataset names; CI opt-in

// Approximate tool → acquisition capability, used ONLY to decide which
// catalog datasets are worth offering during setup. NOT a source of truth —
// runners/*.py SUPPORTED_ACQUISITIONS + is_compatible() enforce the real
// compatibility at job-enumeration time.
def TOOL_ACQUISITIONS = [
    diann:        ['DDA', 'DIA'],
    alphadia:     ['DIA'],
    sage:         ['DDA'],
    fragpipe:     ['DDA', 'DIA'],
    maxquant:     ['DDA', 'DIA'],
    metamorpheus: ['DDA'],
]

// ── Small interactive-IO helpers ──────────────────────────────────────────
// Nextflow scripts aren't normally interactive, but the workflow{} block below
// runs sequentially on the driver process (this machine's terminal), so plain
// stdin prompts work exactly like a regular CLI wizard when run locally.

def interactive  = !params.non_interactive
def console      = System.console()
def stdinReader  = new BufferedReader(new InputStreamReader(System.in))

// ── Pretty terminal output ─────────────────────────────────────────────────
// ANSI styling, but only when writing to a real terminal and NO_COLOR is unset,
// so piped / CI logs stay plain text.
def useColor = console != null && !System.getenv('NO_COLOR')
def sgr    = { String code, s -> useColor ? "[${code}m${s}[0m" : "${s}" }
def bold   = { s -> sgr('1',  s) }
def dim    = { s -> sgr('2',  s) }
def red    = { s -> sgr('31', s) }
def green  = { s -> sgr('32', s) }
def yellow = { s -> sgr('33', s) }
def cyan   = { s -> sgr('36', s) }

int BOX_W = 60
def rep    = { String ch, int n -> n > 0 ? ch * n : '' }
def center = { String s, int w -> def pad = w - s.length(); def l = pad > 0 ? pad.intdiv(2) : 0; rep(' ', l) + s + rep(' ', pad - l) }

def banner = { String title, String subtitle ->
    println ''
    println cyan('  ╭' + rep('─', BOX_W) + '╮')
    println cyan('  │') + bold(center(title, BOX_W)) + cyan('│')
    if (subtitle) println cyan('  │') + dim(center(subtitle, BOX_W)) + cyan('│')
    println cyan('  ╰' + rep('─', BOX_W) + '╯')
}

def section = { String title -> println ''; println bold(cyan('▶ ' + title)) }
def ok      = { s -> println '  ' + green('✓') + ' ' + s }
def warn    = { s -> println '  ' + yellow('•') + ' ' + s }
def fail    = { s -> println '  ' + red('✗') + ' ' + s }
def info    = { s -> println '  ' + cyan('→') + ' ' + s }

def ask = { String prompt ->
    print prompt
    System.out.flush()
    def line = console != null ? console.readLine() : stdinReader.readLine()
    return line?.trim()
}

def askYesNo = { String prompt, boolean byDefault ->
    if (!interactive) return byDefault
    def suffix = byDefault ? ' [Y/n] ' : ' [y/N] '
    def a = ask('  ' + cyan('?') + ' ' + prompt + dim(suffix))
    if (!a) return byDefault
    return a.toLowerCase().startsWith('y')
}

// ProcessBuilder converts the command list to a String[] internally and
// throws a cryptic arraycopy/ClassCastException if any element is a GString
// (from "${...}" interpolation) rather than a real String — coerce here once
// so no call site has to remember to .toString() its own arguments.
def strList = { List cmd -> cmd.collect { it.toString() } }

// Stream a command's output live to this terminal (docker pull progress bars, etc).
def run = { List cmd ->
    def pb = new ProcessBuilder(strList(cmd))
    pb.redirectOutput(ProcessBuilder.Redirect.INHERIT)
    pb.redirectError(ProcessBuilder.Redirect.INHERIT)
    def p = pb.start()
    p.waitFor()
    return p.exitValue()
}

// Capture a command's stdout+stderr instead of streaming it (used for the
// one-off `find` calls that auto-detect in-container paths after a pull).
def capture = { List cmd ->
    def pb = new ProcessBuilder(strList(cmd)).redirectErrorStream(true)
    def p = pb.start()
    def text = p.inputStream.text
    p.waitFor()
    return text
}

def firstLine = { String text -> text.readLines().find { it.trim() } ?: '' }

// Compare dotted version strings numerically ("1.9.2" < "2.0.2" < "2.5.1"), so
// sorting and >= checks don't fall back to string order ("1.9.2" > "1.10.0").
def versionParts = { String v -> v.split(/\./).collect { (it =~ /^\d+/)[0] as int } }
def versionOrder = { String a, String b ->
    def (pa, pb) = [versionParts(a), versionParts(b)]
    for (int i = 0; i < Math.max(pa.size(), pb.size()); i++) {
        def c = (i < pa.size() ? pa[i] : 0) <=> (i < pb.size() ? pb[i] : 0)
        if (c != 0) return c
    }
    return 0
}

// DIA-NN gained DDA support in 2.1.0; older releases are DIA-only.
def supportsDda = { String v -> versionOrder(v, '2.1.0') >= 0 }

// True if a docker image (local or pulled) exists on this machine.
def imagePresent = { String image ->
    def p = new ProcessBuilder(['docker', 'image', 'inspect', image]).redirectErrorStream(true).start()
    p.inputStream.text
    return p.waitFor() == 0
}

// ProteoBench archives extract to a nested wrapper (e.g. raws/<subdir>/...) whose
// directory name doesn't match the dataset. Collapse any chain of single-
// subdirectory wrappers so the dataset files sit directly in destDir, matching
// the flat layout the resolve step and the config paths expect. Bruker '.d'
// directories are never descended into (they are data units, not wrappers).
def flattenSingleDirs = { File destDir ->
    def onlyChildDir = { File d ->
        def e = d.listFiles()
        (e != null && e.length == 1 && e[0].isDirectory() && !e[0].name.toLowerCase().endsWith('.d')) ? e[0] : null
    }
    def firstWrapper = onlyChildDir(destDir)
    if (firstWrapper == null) return
    def root = firstWrapper
    def next = onlyChildDir(root)
    while (next != null) { root = next; next = onlyChildDir(root) }
    root.listFiles().each { child -> child.renameTo(new File(destDir, child.name)) }
    firstWrapper.deleteDir()   // recursively removes the now-empty wrapper chain
}

// Extract a JAR matching `namePart` (case-insensitive) out of a user-supplied
// path — which may be a .zip, an already-extracted folder, or the jar itself.
def locateJar = { String pathStr, String namePart ->
    def src = new File(pathStr)
    if (!src.exists()) {
        println "  Path not found: ${pathStr}"
        return null
    }
    File searchDir
    if (src.isFile() && src.name.toLowerCase().endsWith('.zip')) {
        searchDir = File.createTempDir()
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
        return src.name.toLowerCase().contains(namePart) ? src : null
    } else {
        println "  Not a .zip, folder, or .jar: ${pathStr}"
        return null
    }
    File match = null
    searchDir.eachFileRecurse { f ->
        if (match == null && f.isFile() && f.name.toLowerCase().endsWith('.jar') && f.name.toLowerCase().contains(namePart)) {
            match = f
        }
    }
    return match
}

// Parse nextflow/datasets_catalog.yaml (a flat name -> {url, acquisition,
// format, instrument} map). Hand-rolled instead of a real YAML parser since
// the shape is fixed and simple — no new dependency needed.
def loadCatalog = { File f ->
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

// Split a config.yaml's `tools:` section into per-tool block text, keyed by
// tool name. Used on a repair run to preserve already-complete tools verbatim
// (block boundaries are the 2-space-indented `  <tool>:` lines).
def parseToolBlocks = { String configText ->
    def blocks = [:]
    def idx = configText.indexOf('\ntools:\n')
    if (idx < 0) return blocks
    def toolsText = configText.substring(idx + '\ntools:\n'.length())
    def curName = null
    def curLines = []
    toolsText.eachLine { line ->
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

// Grab a tool block's `datasets:` sub-block (so a repair preserves dataset
// assignments instead of resetting them). Returns null if not found.
def extractSubBlock = { String toolBlock, String key ->
    def m = (toolBlock =~ /(?ms)(^    ${key}:.*?)(?=^    \w|\z)/)
    return m.find() ? m.group(1).replaceAll(/\n+$/, '') + '\n\n' : null
}

def extractDatasetsSubBlock = { String toolBlock -> extractSubBlock(toolBlock, 'datasets') }

// Parse a tool block's `versions:` entries back into maps, so a re-run can ADD
// versions to the existing list rather than replacing it — otherwise adding a
// second DIA-NN image would silently drop any `enabled: false` a user set by
// hand. Values stay strings; they are written back out verbatim.
def parseVersionEntries = { String toolBlock ->
    def sub = toolBlock ? extractSubBlock(toolBlock, 'versions') : null
    if (!sub) return []
    def entries = []
    sub.eachLine { line ->
        def m = (line =~ /^\s+(-\s+)?([\w]+):\s*(.*?)\s*$/)
        if (!m.matches() || m.group(2) == 'versions') return
        if (m.group(1)) entries << [:]
        if (entries) entries[-1][m.group(2)] = m.group(3).replaceAll(/^"|"$/, '')
    }
    return entries
}

def results = [:]   // tool name -> map of facts collected during this run, used to write the config

// Offers to download+unzip catalog datasets relevant to the tools just set up
// (per `results`, scoped by acquisition via TOOL_ACQUISITIONS). Returns a map:
// dataset name -> [path, fasta, fasta_decoy, acquisition, format, instrument].
def downloadDatasets = {
    def resolvedDatasets = [:]
    if (params.skip_datasets || results.isEmpty()) {
        warn('Skipping dataset download (--skip_datasets given, or no tools set up).')
        return resolvedDatasets
    }

    def catalogFile = new File("${projectDir}/nextflow/datasets_catalog.yaml")
    def catalog = catalogFile.exists() ? loadCatalog(catalogFile) : [:]
    def enabledAcqs = results.keySet().collect { TOOL_ACQUISITIONS[it] ?: ['DDA', 'DIA'] }.flatten().toSet()
    def relevant = catalog.findAll { name, meta -> meta.acquisition in enabledAcqs }
    if (!relevant) {
        warn('No catalog datasets are relevant to the tools you just set up — skipping.')
        return resolvedDatasets
    }

    def dataDir = new File((params.data_dir ?: "${projectDir}/data") as String)
    def present = relevant.findAll { name, meta -> new File(dataDir, name).with { it.isDirectory() && it.list() } }
    def missing = relevant.findAll { name, meta -> !present.containsKey(name) }

    def toDownload = [:]
    if (missing) {
        if (params.download_datasets) {
            def wanted = params.download_datasets.toString() == 'all' ?
                missing.keySet() : params.download_datasets.toString().split(',').collect { it.trim() }
            toDownload = missing.findAll { name, meta -> name in wanted }
        } else if (!interactive) {
            warn('Skipping dataset download in non-interactive mode (pass --download_datasets all|name1,name2 to opt in).')
        } else {
            info('Datasets relevant to the tools you just set up:')
            def names = missing.keySet().toList()
            names.eachWithIndex { name, i ->
                def meta = missing[name]
                println '      ' + bold("${i + 1}.") + " ${name}  " + dim("(${meta.acquisition}, ${meta.format}, ${meta.instrument})")
            }
            def answer = ask('  ' + cyan('?') + ' Download which of these? ' + dim('[all/none/1,2,...]') + ' (default: all): ')?.toLowerCase()
            if (!answer || answer == 'all') {
                toDownload = missing
            } else if (answer != 'none') {
                def idx = answer.split(',').collect { it.trim() }.findAll { it.isInteger() }.collect { it.toInteger() - 1 }
                toDownload = missing.findAll { name, meta -> names.indexOf(name) in idx }
            }
        }
    }

    if (toDownload) {
        dataDir.mkdirs()
        toDownload.each { name, meta ->
            if (!meta.url || meta.url == 'CHANGE_ME') {
                warn("${name}: no download URL set in nextflow/datasets_catalog.yaml — skipping.")
                return
            }
            def urlLower = meta.url.toString().toLowerCase()
            def isTarGz  = urlLower.endsWith('.tar.gz') || urlLower.endsWith('.tgz')
            def isZip    = urlLower.endsWith('.zip')
            if (!isTarGz && !isZip) {
                warn("${name}: unrecognized archive type for ${meta.url} (expected .zip or .tar.gz/.tgz) — skipping.")
                return
            }

            info("Downloading ${name} …")
            def tmpArchive = File.createTempFile('proteobench_ds_', isTarGz ? '.tar.gz' : '.zip')
            def destDir = new File(dataDir, name)
            if (run(['curl', '-L', '-o', tmpArchive.path, meta.url]) == 0) {
                destDir.mkdirs()
                def extractCmd = isTarGz ?
                    ['tar', 'xzf', tmpArchive.path, '-C', destDir.path] :
                    ['unzip', '-q', tmpArchive.path, '-d', destDir.path]
                if (run(extractCmd) == 0) {
                    flattenSingleDirs(destDir)
                    // Bruker .d datasets (diaPASEF) ship each run as a nested
                    // <run>.d.zip inside the archive; unpack them into real .d
                    // directories so the runner's format:d glob finds them.
                    destDir.listFiles()?.findAll { it.isFile() && it.name.toLowerCase().endsWith('.d.zip') }?.each { z ->
                        if (run(['unzip', '-q', z.path, '-d', destDir.path]) == 0) z.delete()
                    }
                    ok("${name}: downloaded → " + dim("${destDir}"))
                } else {
                    fail("${name}: failed to extract archive — skipping.")
                }
            } else {
                fail("${name}: download failed — skipping.")
            }
            tmpArchive.delete()
        }
    }

    // Resolve every present dataset (already-there + freshly downloaded this run).
    relevant.each { name, meta ->
        def destDir = new File(dataDir, name)
        if (!destDir.isDirectory() || !destDir.list()) return   // never downloaded — leave untouched

        def fasta = destDir.listFiles()?.find {
            def n = it.name.toLowerCase()
            (n.endsWith('.fasta') || n.endsWith('.fa')) && !n.contains('decoy')
        }
        def fastaDecoy = destDir.listFiles()?.find {
            def n = it.name.toLowerCase()
            n.contains('decoy') && (n.endsWith('.fasta') || n.endsWith('.fa') || n.endsWith('.fas'))
        }

        resolvedDatasets[name] = [
            path: destDir.absolutePath, fasta: fasta?.absolutePath, fasta_decoy: fastaDecoy?.absolutePath,
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

    return resolvedDatasets
}

workflow SETUP {

    banner('ProteoBench · Docker setup', 'search engines run in Docker — no native installs')
    println ''
    println dim('  Pick the tools you want; this wizard pulls their images and writes')
    println dim('  a config listing only the tools that were set up successfully.')

    section('Docker')
    def dockerInfo = new ProcessBuilder(['docker', 'info']).redirectErrorStream(true).start()
    dockerInfo.inputStream.text
    if (dockerInfo.waitFor() != 0) {
        error "Docker is not installed or the daemon is not running. Install/start Docker, then re-run: nextflow run setup.nf"
    }
    ok('Docker daemon is running')

    // First run (no config.yaml): walk through every tool. Repair run (config
    // exists): only prompt for / redo the tools the validator flags as
    // incomplete — already-complete tools are preserved verbatim and tools that
    // were never configured are left alone (add them via a fresh setup).
    def configFile         = new File(params.config as String)
    def firstRun           = !configFile.exists()
    def existingToolBlocks = firstRun ? [:] : parseToolBlocks(configFile.text)
    def incompleteTools    = [] as Set
    if (!firstRun) {
        def p = ['python3', "${projectDir}/config_validator.py".toString(),
                 '--config', configFile.absolutePath, '--list-incomplete-tools'].execute()
        def o = new StringBuilder(), e = new StringBuilder()
        p.consumeProcessOutput(o, e)
        p.waitFor()
        incompleteTools = o.toString().readLines()*.trim().findAll { it } as Set
        def complete = (existingToolBlocks.keySet() - incompleteTools)
        section('What needs doing')
        if (incompleteTools) info("Re-doing (incomplete): " + incompleteTools.sort().join(', '))
        if (complete)        ok("Keeping (already complete): " + complete.sort().join(', '))
    }

    // ── MaxQuant ──────────────────────────────────────────────────────────
    section('MaxQuant')
    if (!firstRun && !incompleteTools.contains('maxquant')) {
        if (existingToolBlocks.maxquant) ok('MaxQuant already set up — skipping.')
        else warn('MaxQuant not configured — skipping (add it via a fresh setup).')
    } else if (askYesNo('Pull MaxQuant? ' + dim('(quay.io/medbioinf/maxquant:2.6.3.0 + :2.8.1.0, Max Planck academic license)'), true)) {
        results.maxquant = []
        // Pin explicit version tags: :latest drifts (it currently points at 2.8.1.0),
        // which would break the recorded maxquant_dll path for the older version.
        ['quay.io/medbioinf/maxquant:2.6.3.0', 'quay.io/medbioinf/maxquant:2.8.1.0'].each { mqImage ->
            if (run(['docker', 'pull', mqImage]) == 0) {
                def found = firstLine(capture(['docker', 'run', '--rm', '--entrypoint', 'find', mqImage, '/opt', '-maxdepth', '3', '-iname', 'MaxQuantCmd.dll']))
                def dll = found ?: '/opt/MaxQuant/bin/MaxQuantCmd.dll'
                def m = (dll =~ /MaxQuant_v([\d.]+)/)
                def ver = m.find() ? m.group(1) : 'latest'
                results.maxquant << [id: ver, image: mqImage, maxquant_dll: dll]
                ok("MaxQuant ready " + dim("(${mqImage})"))
            } else {
                fail("MaxQuant pull failed for ${mqImage} — skipping.")
            }
        }
        if (!results.maxquant) results.remove('maxquant')
    } else {
        warn('Skipping MaxQuant.')
    }

    // ── Sage ──────────────────────────────────────────────────────────────
    section('Sage')
    if (!firstRun && !incompleteTools.contains('sage')) {
        if (existingToolBlocks.sage) ok('Sage already set up — skipping.')
        else warn('Sage not configured — skipping (add it via a fresh setup).')
    } else if (askYesNo('Pull Sage? ' + dim('(ghcr.io/lazear/sage:latest)'), true)) {
        def image = 'ghcr.io/lazear/sage:latest'
        if (run(['docker', 'pull', image]) == 0) {
            def found = firstLine(capture(['docker', 'run', '--rm', '--entrypoint', 'find', image, '/app', '-maxdepth', '1', '-type', 'f', '-executable']))
            def bin = found ?: '/app/sage'
            results.sage = [image: image, sage_bin: bin]
            ok("Sage ready " + dim("(${image})"))
        } else {
            fail('Sage pull failed — skipping.')
        }
    } else {
        warn('Skipping Sage.')
    }

    // ── MetaMorpheus ──────────────────────────────────────────────────────
    section('MetaMorpheus')
    if (!firstRun && !incompleteTools.contains('metamorpheus')) {
        if (existingToolBlocks.metamorpheus) ok('MetaMorpheus already set up — skipping.')
        else warn('MetaMorpheus not configured — skipping (add it via a fresh setup).')
    } else if (askYesNo('Pull MetaMorpheus? ' + dim('(smithchemwisc/metamorpheus:latest)'), true)) {
        def image = 'smithchemwisc/metamorpheus:latest'
        if (run(['docker', 'pull', image]) == 0) {
            results.metamorpheus = [image: image]
            ok("MetaMorpheus ready " + dim("(${image})"))
        } else {
            fail('MetaMorpheus pull failed — skipping.')
        }
    } else {
        warn('Skipping MetaMorpheus.')
    }

    // ── AlphaDIA ──────────────────────────────────────────────────────────
    section('AlphaDIA')
    if (!firstRun && !incompleteTools.contains('alphadia')) {
        if (existingToolBlocks.alphadia) ok('AlphaDIA already set up — skipping.')
        else warn('AlphaDIA not configured — skipping (add it via a fresh setup).')
    } else if (askYesNo('Pull AlphaDIA? ' + dim('(mannlabs/alphadia:latest)'), true)) {
        def image = 'mannlabs/alphadia:latest'
        if (run(['docker', 'pull', image]) == 0) {
            def gpu = params.alphadia_gpu ?: askYesNo('Do you have an NVIDIA GPU with the NVIDIA Container Toolkit set up for docker?', false)
            results.alphadia = [image: image, gpu: gpu]
            ok("AlphaDIA ready " + dim("(${image}, gpu=${gpu})"))
        } else {
            fail('AlphaDIA pull failed — skipping.')
        }
    } else {
        warn('Skipping AlphaDIA.')
    }

    // ── FragPipe (image + separately-licensed MSFragger/IonQuant/diaTracer) ─
    section('FragPipe')
    def skipFragpipe = !firstRun && !incompleteTools.contains('fragpipe')
    def doFragpipe = !skipFragpipe && !params.skip_fragpipe &&
        askYesNo('Set up FragPipe? ' + dim('(MSFragger/IonQuant/diaTracer need a separate Nesvilab academic license)'), true)

    if (skipFragpipe) {
        if (existingToolBlocks.fragpipe) ok('FragPipe already set up — skipping.')
        else warn('FragPipe not configured — skipping (add it via a fresh setup).')
    } else if (doFragpipe) {
        def image = 'fcyucn/fragpipe:latest'
        if (run(['docker', 'pull', image]) == 0) {
            def rootFound = firstLine(capture(['docker', 'run', '--rm', '--entrypoint', 'find', image, '/fragpipe_bin', '-maxdepth', '4', '-type', 'f', '-name', 'fragpipe']))
            def fragpipeRoot = rootFound ? new File(rootFound).parentFile.parentFile.path : '/fragpipe_bin/fragpipe-24.0/fragpipe-24.0'

            def jarsDir = new File("${projectDir}/tools/fragpipe_jars")
            jarsDir.mkdirs()

            def specs = [
                [key: 'msfragger', label: 'MSFragger', url: 'https://msfragger.nesvilab.org/upgrading_msfragger.html', param: params.msfragger_path],
                [key: 'ionquant',  label: 'IonQuant',   url: 'https://msfragger-upgrader.nesvilab.org/ionquant/',      param: params.ionquant_path],
                [key: 'diatracer', label: 'diaTracer',  url: 'https://msfragger-upgrader.nesvilab.org/diatracer/',     param: params.diatracer_path],
            ]

            def allFound = true
            specs.each { spec ->
                def already = jarsDir.listFiles()?.find { it.name.toLowerCase().contains(spec.key) }
                if (already) {
                    ok("${spec.label}: already present " + dim("(${already.name})"))
                    return
                }
                def path = spec.param
                def has = path ? true : askYesNo("Do you already have ${spec.label} downloaded (as a .zip or extracted folder)?", false)
                if (has) {
                    if (!path) path = ask('  ' + cyan('?') + " Path to the ${spec.label} zip or folder: ")
                    def jar = path ? locateJar(path, spec.key) : null
                    if (jar) {
                        Files.copy(jar.toPath(), new File(jarsDir, jar.name).toPath(), StandardCopyOption.REPLACE_EXISTING)
                        ok("${spec.label}: copied " + dim(jar.name))
                        // MSFragger ships an ext/ folder (Thermo .raw + Bruker .d native
                        // readers) next to its jar; copy it too, else MSFragger can only
                        // read mzML. Mounted next to the jar at run time by fragpipe.py.
                        if (spec.key == 'msfragger') {
                            def extSrc = new File(jar.parentFile, 'ext')
                            if (extSrc.isDirectory()) {
                                def extDst = new File(jarsDir, 'ext')
                                extDst.deleteDir()
                                run(['cp', '-r', extSrc.path, extDst.path])
                                ok('MSFragger native readers: copied ' + dim('ext/ (Thermo .raw + Bruker .d)'))
                            } else {
                                warn('No ext/ folder next to MSFragger — .raw/.d will not read; FragPipe will need mzML input.')
                            }
                        }
                    } else {
                        fail("${spec.label}: no matching .jar found in ${path} — skipping this tool for now.")
                        allFound = false
                    }
                } else {
                    info("${spec.label} download (academic license): ${spec.url}")
                    warn('Re-run setup once downloaded — FragPipe stays disabled until all three are present.')
                    allFound = false
                }
            }

            results.fragpipe = [image: image, fragpipe_root: fragpipeRoot, jars_dir: jarsDir.absolutePath, ready: allFound]
            if (allFound) ok('FragPipe ready.')
            else          warn('FragPipe image pulled, but licensed JARs are missing — will be written as disabled.')
        } else {
            fail('FragPipe pull failed — skipping.')
        }
    } else {
        warn('Skipping FragPipe.')
    }

    // ── DIA-NN (1.8.1 public image + optional 2.x built locally) ─────────
    section('DIA-NN')
    // --build_diann_v2 is an explicit "I want these versions" request, so it
    // re-opens the DIA-NN step even when the existing block is already complete
    // (that is how a version is added to a working setup).
    if (!firstRun && !incompleteTools.contains('diann') && !params.build_diann_v2) {
        if (existingToolBlocks.diann) ok('DIA-NN already set up — skipping ' + dim('(pass --build_diann_v2 to add versions).'))
        else warn('DIA-NN not configured — skipping (add it via a fresh setup).')
    } else if (askYesNo('Set up DIA-NN?', true)) {
        // Start from what is already configured; versions found below are added
        // to this list, and existing ids are never rebuilt or reset.
        def versions = parseVersionEntries(existingToolBlocks.diann)
        if (versions) ok("Keeping configured version(s): " + versions*.id.join(', '))
        def haveVersion = { String id -> versions.any { it.id == id } }

        def baseImage = 'biocontainers/diann:v1.8.1_cv1'
        if (haveVersion('1.8.1')) {
            // already in the list from a previous run
        } else if (run(['docker', 'pull', baseImage]) == 0) {
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
        def wantsV2 = params.build_diann_v2 || askYesNo(
            'Also build DIA-NN 2.x image(s) locally? (needed for DDA support and native Thermo .raw on Linux)', false)

        if (wantsV2) {
            info('DIA-NN 2.x is built locally from the bigbio/quantms-containers recipes,')
            info('which download the DIA-NN Academia release during the build.')
            info('By continuing you accept the DIA-NN license: ' + dim('https://github.com/vdemichev/DiaNN'))

            def wanted = params.diann_version.split(',').collect { it.trim() }.findAll { it }
            def buildDir = File.createTempDir()
            if (run(['git', 'clone', '--depth', '1', 'https://github.com/bigbio/quantms-containers.git', buildDir.path]) != 0) {
                fail('Could not clone bigbio/quantms-containers (need git + network). Skipping DIA-NN 2.x.')
            } else {
                // Recipe folders are named diann-<version>; the enterprise variant is
                // per-user licensed and cannot be built from a plain clone, so hide it.
                def avail = (buildDir.listFiles() ?: []).findAll {
                    it.isDirectory() && it.name.startsWith('diann-') && !it.name.contains('enterprise')
                }.collect { it.name.replace('diann-', '') }.toSorted(versionOrder)

                if (interactive) {
                    info('Available recipes: ' + avail.join(', '))
                    def a = ask('  ' + cyan('?') + " Which version(s) to build? " + dim("(comma-separated) [${wanted.join(',')}] "))
                    if (a) wanted = a.split(',').collect { it.trim() }.findAll { it }
                }

                wanted.each { ver ->
                    if (haveVersion(ver)) {
                        ok("DIA-NN ${ver} already configured " + dim('(left as-is)'))
                        return
                    }
                    if (!avail.contains(ver)) {
                        fail("No build recipe for DIA-NN ${ver}. Available: ${avail.join(', ')}.")
                        return
                    }
                    // Path is fixed by every recipe (ln -s .../diann-linux .../diann); no find needed.
                    def entry = [id: ver, image: "diann:${ver}",
                                 diann_bin: "/usr/diann-${ver}/diann", supports_dda: supportsDda(ver)]
                    if (imagePresent(entry.image)) {
                        versions << entry
                        ok("DIA-NN ${ver} already built locally " + dim("(${entry.image})"))
                    } else if (askYesNo("Build DIA-NN ${ver} now? " + dim('(downloads a few hundred MB, takes a few minutes)'), true)) {
                        if (run(['docker', 'build', '-t', entry.image, new File(buildDir, "diann-${ver}").path]) == 0) {
                            versions << entry
                            ok("DIA-NN ${ver} ready " + dim("(${entry.image}, built locally)"))
                        } else {
                            fail("DIA-NN ${ver} build failed — see the output above.")
                        }
                    }
                }
            }
            buildDir.deleteDir()
        } else {
            warn('Only DIA-NN 1.8.1 will be configured.')
        }

        if (versions) results.diann = [versions: versions]
    } else {
        warn('Skipping DIA-NN.')
    }

    // ── Datasets (optional automatic download) ────────────────────────────
    section('Datasets')
    def resolvedDatasets = downloadDatasets()

    // ── Write config.yaml ─────────────────────────────────────────────────
    // Base the global/search_params/datasets sections on the existing config
    // when there is one (so a repair preserves the user's edits), otherwise on
    // the template (first run).
    def baseText = configFile.exists() ? configFile.text : new File("${projectDir}/config.template.yaml").text
    def staticStart = baseText.indexOf('global:')
    def staticEnd   = baseText.indexOf('\ntools:\n')
    def staticSection = baseText.substring(staticStart, staticEnd >= 0 ? staticEnd : baseText.length())
    // Slicing at '\ntools:\n' consumes the newline terminating the last static
    // line; restore it so the entry regex below still sees a complete last line
    // (e.g. a config whose final dataset line is immediately followed by tools:).
    if (!staticSection.endsWith('\n')) staticSection += '\n'

    // Rebuild the datasets: block. Keep every existing entry verbatim; only
    // replace an entry with a freshly-resolved one when the existing entry is
    // still a CHANGE_ME placeholder (template first run), and append any newly
    // downloaded datasets not already present. This never clobbers real paths.
    def datasetsIdx = staticSection.indexOf('datasets:')
    def beforeDatasets = staticSection.substring(0, datasetsIdx)
    def afterDatasetsKeyword = staticSection.substring(datasetsIdx + 'datasets:'.length())

    def entryPattern = ~/(?m)^  (\S+):\n((?:    .*\n)+)/
    def existingEntries = [:]
    def matcher = entryPattern.matcher(afterDatasetsKeyword)
    def lastEnd = 0
    while (matcher.find()) {
        existingEntries[matcher.group(1)] = "  ${matcher.group(1)}:\n${matcher.group(2)}"
        lastEnd = matcher.end()
    }
    // Every rendered entry above already ends with its own blank-line separator,
    // so drop the one leading blank line here to avoid a doubled-up gap.
    def datasetsTrailer = afterDatasetsKeyword.substring(lastEnd).replaceFirst(/^\n/, '')

    def datasetsOut = new StringBuilder('datasets:\n\n')
    existingEntries.each { name, block ->
        boolean overrideWithResolved = resolvedDatasets.containsKey(name) && block.contains('CHANGE_ME')
        if (!overrideWithResolved) datasetsOut << block << '\n'
    }
    resolvedDatasets.each { name, d ->
        boolean keptVerbatim = existingEntries.containsKey(name) && !existingEntries[name].contains('CHANGE_ME')
        if (keptVerbatim) return
        datasetsOut << "  ${name}:\n"
        datasetsOut << "    path: ${d.path}\n"
        datasetsOut << "    acquisition: ${d.acquisition}\n"
        datasetsOut << "    format: ${d.format}\n"
        datasetsOut << "    instrument: ${d.instrument}\n"
        if (d.fasta) datasetsOut << "    fasta: ${d.fasta}\n"
        if (d.fasta_decoy) datasetsOut << "    fasta_decoy: ${d.fasta_decoy}\n"
        datasetsOut << '\n'
    }

    // A tool's datasets: sub-block. On a repair, reuse the tool's existing
    // assignment verbatim (redoing an image shouldn't reset its datasets);
    // otherwise fill from the datasets resolved this run, or the fallback.
    def datasetsFor = { String toolName, String fallback ->
        def preserved = existingToolBlocks[toolName] ? extractDatasetsSubBlock(existingToolBlocks[toolName]) : null
        if (preserved) return preserved
        def acqs = TOOL_ACQUISITIONS[toolName] ?: ['DDA', 'DIA']
        def names = resolvedDatasets.findAll { n, d -> d.acquisition in acqs }.keySet()
        if (!names) return fallback
        def out = new StringBuilder('    datasets:\n')
        names.each { out << "      - ${it}\n" }
        out << '\n'
        return out.toString()
    }

    def sb = new StringBuilder()
    if (firstRun) {
        sb << '# =============================================================================\n'
        sb << '# Auto-generated by `nextflow run setup.nf` — only tools that were successfully\n'
        sb << '# set up appear below. Re-run setup.nf any time to add more.\n'
        sb << '# =============================================================================\n\n'
    } else {
        // Repair run: keep whatever header the existing config had, verbatim.
        sb << baseText.substring(0, staticStart)
    }
    sb << beforeDatasets
    sb << datasetsOut.toString()
    sb << datasetsTrailer
    // An empty mapping value ('tools:' with nothing indented under it) parses
    // as YAML null, not {} — write the explicit empty-map form so downstream
    // Python (cfg.get("tools", {})) doesn't choke on a None.
    sb << ((results || existingToolBlocks) ? 'tools:\n\n' : 'tools: {}\n')

    // Emit an already-complete tool's existing block verbatim (normalised to a
    // single trailing blank line).
    def emitPreserved = { String tool -> sb << existingToolBlocks[tool].replaceAll(/\n+$/, '') + '\n\n' }

    if (results.diann) {
        sb << '  diann:\n    versions:\n'
        results.diann.versions.each { v ->
            sb << "      - id: \"${v.id}\"\n"
            sb << "        image: ${v.image}\n"
            sb << "        diann_bin: ${v.diann_bin}\n"
            sb << "        supports_dda: ${v.supports_dda}\n"
            // Newly discovered versions default to enabled; ones carried over from
            // the existing config keep whatever the user set.
            sb << "        enabled: ${v.containsKey('enabled') ? v.enabled : true}\n\n"
        }
        sb << datasetsFor('diann', "    datasets:\n      - CHANGE_ME\n\n")
        sb << (extractSubBlock(existingToolBlocks.diann ?: '', 'extra') ?: "    extra:\n      library: \"\"\n\n")
    } else if (existingToolBlocks.diann) {
        emitPreserved('diann')
    }

    if (results.alphadia) {
        sb << "  alphadia:\n    versions:\n      - id: \"latest\"\n        image: ${results.alphadia.image}\n"
        sb << "        gpu: ${results.alphadia.gpu}\n        enabled: true\n\n"
        sb << datasetsFor('alphadia', "    datasets:\n      - CHANGE_ME\n\n")
        sb << "    extra:\n      library: \"\"\n\n"
    } else if (existingToolBlocks.alphadia) {
        emitPreserved('alphadia')
    }

    if (results.sage) {
        sb << "  sage:\n    versions:\n      - id: \"latest\"\n        image: ${results.sage.image}\n"
        sb << "        sage_bin: ${results.sage.sage_bin}\n        enabled: true\n\n"
        sb << datasetsFor('sage', "    datasets: []   # requires mzML input (auto-detected from a <name>_mzml sibling folder)\n\n")
        sb << "    extra:\n      write_pin: true\n      parquet: false\n\n"
    } else if (existingToolBlocks.sage) {
        emitPreserved('sage')
    }

    if (results.fragpipe) {
        sb << "  fragpipe:\n    versions:\n      - id: \"24.0\"\n        image: ${results.fragpipe.image}\n"
        sb << "        fragpipe_root: ${results.fragpipe.fragpipe_root}\n        jars_dir: ${results.fragpipe.jars_dir}\n"
        sb << "        container_python: /usr/bin/python3\n        enabled: ${results.fragpipe.ready}\n\n"
        sb << datasetsFor('fragpipe', "    datasets:\n      - CHANGE_ME\n\n")
        sb << "    extra:\n      dda_workflow: LFQ-MBR\n      dia_workflow: DIA_SpecLib_Quant\n      dia_pasef_workflow: DIA_SpecLib_Quant_diaPASEF\n\n"
    } else if (existingToolBlocks.fragpipe) {
        emitPreserved('fragpipe')
    }

    if (results.maxquant) {
        sb << "  maxquant:\n    versions:\n"
        results.maxquant.eachWithIndex { v, i ->
            sb << "      - id: \"${v.id}\"\n        image: ${v.image}\n"
            sb << "        maxquant_dll: ${v.maxquant_dll}\n        enabled: ${i == 0}\n"
        }
        sb << "\n"
        sb << datasetsFor('maxquant', "    datasets:\n      - CHANGE_ME\n\n")
        sb << "    extra: {}\n\n"
    } else if (existingToolBlocks.maxquant) {
        emitPreserved('maxquant')
    }

    if (results.metamorpheus) {
        sb << "  metamorpheus:\n    versions:\n      - id: \"latest\"\n        image: ${results.metamorpheus.image}\n        enabled: true\n\n"
        sb << datasetsFor('metamorpheus', "    datasets: []   # DDA-only\n\n")
        sb << "    extra: {}\n\n"
    } else if (existingToolBlocks.metamorpheus) {
        emitPreserved('metamorpheus')
    }

    // On a first run we create config.yaml. On a repair the merged output
    // preserves the existing global/search_params/datasets and every
    // already-complete tool verbatim (only the tools redone this run change),
    // so it is safe to update config.yaml in place — a .bak copy is kept as a
    // safety net. (firstRun was captured at the top, before this write.)
    if (!firstRun) {
        new File("${configFile.path}.bak").text = configFile.text
    }
    configFile.text = sb.toString()

    banner('Setup complete', results ? "configured: ${results.keySet().sort().join(', ')}" : 'no changes')
    ok('Wrote ' + dim("${configFile}"))
    if (firstRun) {
        println ''
        println bold('  Next steps:')
        println '    ' + cyan('1.') + ' Edit ' + dim("${configFile}") + ': set global.output_dir and any remaining CHANGE_ME paths.'
        println '    ' + cyan('2.') + ' Run ' + bold('nextflow run proteobench.nf') + dim('  (runs straight from here on)')
        println ''
    } else {
        println ''
        info("Updated in place; previous version saved to " + dim("${configFile.path}.bak"))
        println '    ' + bold('nextflow run proteobench.nf')
        println ''
    }
}

// Only used when running `nextflow run setup.nf` directly; ignored when this
// file is included as a module (e.g. by proteobench.nf), since only the
// including script's own workflow{} executes in that case.
workflow {
    SETUP()
}

// Self-check for the version helpers that decide DIA-NN recipe ordering and
// DDA support. Run with: nextflow run setup.nf -entry SELFTEST
workflow SELFTEST {
    def sorted = ['2.5.0', '1.8.1', '2.0.2', '1.9.2', '2.10.0'].toSorted(versionOrder)
    assert sorted == ['1.8.1', '1.9.2', '2.0.2', '2.5.0', '2.10.0'] : "got ${sorted}"
    assert [!supportsDda('1.8.1'), !supportsDda('2.0.2'), supportsDda('2.1.0'), supportsDda('2.5.1')].every()

    // A re-run must read back every configured version, including enabled: false,
    // or adding a version would silently drop the others.
    def block = '''  diann:
    versions:
      - id: "1.8.1"
        image: biocontainers/diann:v1.8.1_cv1
        diann_bin: /usr/diann/1.8.1/diann
        supports_dda: false
        enabled: true

      - id: "2.5.0"
        image: diann:2.5.0
        diann_bin: /usr/diann-2.5.0/diann
        supports_dda: true
        enabled: false

    datasets:
      - Entrapment_DIA

    extra:
      library: ""
'''
    def parsed = parseVersionEntries(block)
    assert parsed*.id == ['1.8.1', '2.5.0'] : "got ${parsed*.id}"
    assert parsed[1].image == 'diann:2.5.0' && parsed[1].enabled == 'false' : "got ${parsed[1]}"
    assert extractSubBlock(block, 'extra').contains('library') && extractDatasetsSubBlock(block).contains('Entrapment_DIA')
    println 'SELFTEST ok'
}
