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
params.selftest          = false  // run the SELFTEST workflow instead of SETUP

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

// Capture a command's stdout+stderr instead of streaming it (used for the
// one-off `find` calls that auto-detect in-container paths after a pull).
def capture(cmd) {
    def pb = new ProcessBuilder(strList(cmd)).redirectErrorStream(true)
    def p = pb.start()
    def text = p.inputStream.text
    p.waitFor()
    return text
}

def firstLine(text) { return text.readLines().find { l -> l.trim() } ?: '' }

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

// The single subdirectory of `d`, or null if `d` holds anything else. Bruker
// '.d' directories are never treated as wrappers (they are data units).
def onlyChildDir(d) {
    def e = d.listFiles()
    return (e != null && e.length == 1 && e[0].isDirectory() && !e[0].name.toLowerCase().endsWith('.d')) ? e[0] : null
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

// Extract a JAR matching `namePart` (case-insensitive) out of a user-supplied
// path — which may be a .zip, an already-extracted folder, or the jar itself.
def locateJar(pathStr, namePart) {
    def src = new File(pathStr.toString())
    if (!src.exists()) {
        println "  Path not found: ${pathStr}"
        return null
    }
    def searchDir = null
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
    def matches = []
    searchDir.eachFileRecurse { f ->
        if (f.isFile() && f.name.toLowerCase().endsWith('.jar') && f.name.toLowerCase().contains(namePart)) {
            matches << f
        }
    }
    return matches ? matches[0] : null
}

// Parse nextflow/datasets_catalog.yaml (a flat name -> {url, acquisition,
// format, instrument} map). Hand-rolled instead of a real YAML parser since
// the shape is fixed and simple — no new dependency needed.
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

// Split a config.yaml's `tools:` section into per-tool block text, keyed by
// tool name. Used on a repair run to preserve already-complete tools verbatim
// (block boundaries are the 2-space-indented `  <tool>:` lines).
def parseToolBlocks(configText) {
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
        if (entries) entries[-1][m.group(2)] = m.group(3).replaceAll(/^"|"$/, '')
    }
    return entries
}

// Offers to download+unzip catalog datasets relevant to the tools just set up
// (per `results`, scoped by acquisition via toolAcquisitions()). Returns a map:
// dataset name -> [path, fasta, fasta_decoy, acquisition, format, instrument].
def downloadDatasets(results) {
    def resolvedDatasets = [:]
    if (params.skip_datasets || results.isEmpty()) {
        warn('Skipping dataset download (--skip_datasets given, or no tools set up).')
        return resolvedDatasets
    }

    def acquisitions = toolAcquisitions()
    def catalogFile = new File("${projectDir}/nextflow/datasets_catalog.yaml")
    def catalog = catalogFile.exists() ? loadCatalog(catalogFile) : [:]
    def enabledAcqs = results.keySet().collect { tool -> acquisitions[tool] ?: ['DDA', 'DIA'] }.flatten().toSet()
    def relevant = catalog.findAll { _name, meta -> meta.acquisition in enabledAcqs }
    if (!relevant) {
        warn('No catalog datasets are relevant to the tools you just set up — skipping.')
        return resolvedDatasets
    }

    def dataDir = new File((params.data_dir ?: "${projectDir}/data") as String)
    def present = relevant.findAll { name, _meta -> new File(dataDir, name).with { d -> d.isDirectory() && d.list() } }
    def missing = relevant.findAll { name, _meta -> !present.containsKey(name) }

    def toDownload = [:]
    if (missing) {
        if (params.download_datasets) {
            def wanted = params.download_datasets.toString() == 'all' ?
                missing.keySet() : params.download_datasets.toString().split(',').collect { w -> w.trim() }
            toDownload = missing.findAll { name, _meta -> name in wanted }
        } else if (!isInteractive()) {
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
                def idx = answer.split(',').collect { a -> a.trim() }.findAll { a -> a.isInteger() }.collect { a -> a.toInteger() - 1 }
                toDownload = missing.findAll { name, _meta -> names.indexOf(name) in idx }
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
            if (runCmd(['curl', '-L', '-o', tmpArchive.path, meta.url]) == 0) {
                destDir.mkdirs()
                def extractCmd = isTarGz ?
                    ['tar', 'xzf', tmpArchive.path, '-C', destDir.path] :
                    ['unzip', '-q', tmpArchive.path, '-d', destDir.path]
                if (runCmd(extractCmd) == 0) {
                    flattenSingleDirs(destDir)
                    normalizeProteoBenchLayout(destDir)
                    // Bruker .d datasets (diaPASEF) ship each run as a nested
                    // <run>.d.zip inside the archive; unpack them into real .d
                    // directories so the runner's format:d glob finds them.
                    destDir.listFiles()?.findAll { f -> f.isFile() && f.name.toLowerCase().endsWith('.d.zip') }?.each { z ->
                        if (runCmd(['unzip', '-q', z.path, '-d', destDir.path]) == 0) z.delete()
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
        normalizeProteoBenchLayout(destDir)   // also fixes datasets downloaded before this step existed

        def fasta = destDir.listFiles()?.find { f ->
            def n = f.name.toLowerCase()
            (n.endsWith('.fasta') || n.endsWith('.fa')) && !n.contains('decoy')
        }
        def fastaDecoy = destDir.listFiles()?.find { f ->
            def n = f.name.toLowerCase()
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

    def interactive = isInteractive()
    def results = [:]   // tool name -> map of facts collected during this run, used to write the config

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
        def o = new StringBuilder()
        def e = new StringBuilder()
        p.consumeProcessOutput(o, e)
        p.waitFor()
        incompleteTools = o.toString().readLines().collect { l -> l.trim() }.findAll { l -> l } as Set
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
            if (runCmd(['docker', 'pull', mqImage]) == 0) {
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
        if (runCmd(['docker', 'pull', image]) == 0) {
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
        if (runCmd(['docker', 'pull', image]) == 0) {
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
        if (runCmd(['docker', 'pull', image]) == 0) {
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
        if (runCmd(['docker', 'pull', image]) == 0) {
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
                def already = jarsDir.listFiles()?.find { f -> f.name.toLowerCase().contains(spec.key) }
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
                        java.nio.file.Files.copy(jar.toPath(), new File(jarsDir, jar.name).toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
                        ok("${spec.label}: copied " + dim(jar.name))
                        // MSFragger ships an ext/ folder (Thermo .raw + Bruker .d native
                        // readers) next to its jar; copy it too, else MSFragger can only
                        // read mzML. Mounted next to the jar at run time by fragpipe.py.
                        if (spec.key == 'msfragger') {
                            def extSrc = new File(jar.parentFile, 'ext')
                            if (extSrc.isDirectory()) {
                                def extDst = new File(jarsDir, 'ext')
                                extDst.deleteDir()
                                runCmd(['cp', '-r', extSrc.path, extDst.path])
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
    } else {
        // Start from what is already configured; versions found below are added
        // to this list. Only versions with `enabled: true` are checked, matching
        // config_validator.py; disabled versions are never pulled or rebuilt.
        def versions = parseVersionEntries(existingToolBlocks.diann)
        def disabledVersions = versions.findAll { v -> v.enabled != 'true' }
        def enabledVersions  = versions.findAll { v -> v.enabled == 'true' }
        def missingEnabled   = enabledVersions.findAll { v -> !imagePresent(v.image) }
        def presentEnabled   = enabledVersions - missingEnabled

        if (presentEnabled)   ok("Enabled and present: " + presentEnabled.collect { v -> v.id }.join(', '))
        if (disabledVersions) info("Disabled in config.yaml (not checked): " + disabledVersions.collect { v -> v.id }.join(', '))
        if (missingEnabled) {
            warn('These DIA-NN versions are ' + bold('enabled: true') + ' in config.yaml, but their docker image is not present locally:')
            missingEnabled.each { v -> println '      - ' + bold(v.id) + ' ' + dim("(${v.image})") }
            info('That is why setup is asking about DIA-NN. Set ' + bold('enabled: false') + ' for a version to stop being asked about it.')
        }

        // With missing enabled versions the per-version prompts below replace the
        // generic question, so the user is asked about each version directly.
        if (missingEnabled || askYesNo('Set up DIA-NN?', true)) {
            def haveVersion = { id -> versions.any { v -> v.id == id } }

            // Repair enabled versions whose image is missing. Locally built images
            // (diann:<id>) are queued for the build step below; others are pulled.
            def toBuild = []
            missingEnabled.each { v ->
                def localBuild = v.image == "diann:${v.id}".toString()
                def action = localBuild ? 'Build' : 'Pull'
                if (askYesNo("${action} DIA-NN ${v.id} now? " + dim("(enabled in config.yaml, image ${v.image} missing)"), true)) {
                    if (localBuild) {
                        toBuild << v
                    } else if (runCmd(['docker', 'pull', v.image]) == 0) {
                        ok("DIA-NN ${v.id} ready " + dim("(${v.image})"))
                    } else {
                        fail("DIA-NN ${v.id} pull failed. It is still enabled, so setup will ask again on the next run.")
                    }
                } else if (askYesNo("Set DIA-NN ${v.id} to enabled: false in config.yaml, so setup stops asking about it?", false)) {
                    v.enabled = 'false'
                    ok("DIA-NN ${v.id} set to " + bold('enabled: false'))
                } else {
                    warn("DIA-NN ${v.id} stays enabled without an image, so setup will ask again on the next run.")
                }
            }

            def baseImage = 'biocontainers/diann:v1.8.1_cv1'
            if (haveVersion.call('1.8.1')) {
                // already in the list from a previous run
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
            def wantsV2 = params.build_diann_v2 || askYesNo(
                'Also build additional DIA-NN 2.x image(s) locally? (needed for DDA support and native Thermo .raw on Linux)', false)

            if (toBuild || wantsV2) {
                info('DIA-NN 2.x is built locally from the bigbio/quantms-containers recipes,')
                info('which download the DIA-NN Academia release during the build.')
                info('By continuing you accept the DIA-NN license: ' + dim('https://github.com/vdemichev/DiaNN'))

                def wanted = params.diann_version.toString().split(',').collect { w -> w.trim() }.findAll { w -> w }
                def buildDir = File.createTempDir()
                if (runCmd(['git', 'clone', '--depth', '1', 'https://github.com/bigbio/quantms-containers.git', buildDir.path]) != 0) {
                    fail('Could not clone bigbio/quantms-containers (need git + network). Skipping DIA-NN 2.x.')
                if (toBuild) warn("Not built: ${toBuild.collect { v -> v.id }.join(', ')}. They stay enabled, so setup will ask again on the next run.")
                } else {
                    // Recipe folders are named diann-<version>; the enterprise variant is
                    // per-user licensed and cannot be built from a plain clone, so hide it.
                    def avail = (buildDir.listFiles() ?: []).findAll { d ->
                        d.isDirectory() && d.name.startsWith('diann-') && !d.name.contains('enterprise')
                    }.collect { d -> d.name.replace('diann-', '') }.toSorted { a, b -> versionOrder(a, b) }

                    // Rebuild the enabled versions whose image was missing (already confirmed above).
                    toBuild.each { v ->
                        if (!avail.contains(v.id)) {
                            fail("No build recipe for DIA-NN ${v.id}. Available: ${avail.join(', ')}. Set enabled: false for it in config.yaml.")
                            return
                        }
                        if (runCmd(['docker', 'build', '-t', v.image, new File(buildDir, "diann-${v.id}").path]) == 0) {
                            ok("DIA-NN ${v.id} ready " + dim("(${v.image}, built locally)"))
                        } else {
                            fail("DIA-NN ${v.id} build failed — see the output above.")
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

            if (versions) results.diann = [versions: versions]
        } else {
            warn('Skipping DIA-NN.')
        }
    }

    // ── Datasets (optional automatic download) ────────────────────────────
    section('Datasets')
    def resolvedDatasets = downloadDatasets(results)

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
    // Match only the top-level key at the start of a line: the comments above it
    // mention `datasets:` too, and cutting there drops the real key.
    def datasetsKey = java.util.regex.Pattern.compile(/(?m)^datasets:/).matcher(staticSection)
    def hasDatasetsKey = datasetsKey.find()
    def beforeDatasets = hasDatasetsKey ? staticSection.substring(0, datasetsKey.start()) : staticSection + '\n'
    def afterDatasetsKeyword = hasDatasetsKey ? staticSection.substring(datasetsKey.end()) : ''

    def entryPattern = java.util.regex.Pattern.compile(/(?m)^  (\S+):\n((?:    .*\n)+)/)
    def existingEntries = [:]
    def entryMatches = entryPattern.matcher(afterDatasetsKeyword).results().toList()
    entryMatches.each { r ->
        existingEntries[r.group(1)] = "  ${r.group(1)}:\n${r.group(2)}".toString()
    }
    def lastEnd = entryMatches ? entryMatches[-1].end() : 0
    // Every rendered entry above already ends with its own blank-line separator,
    // so drop the one leading blank line here to avoid a doubled-up gap.
    def datasetsTrailer = afterDatasetsKeyword.substring(lastEnd).replaceFirst(/^\n/, '')

    def datasetsOut = new StringBuilder('datasets:\n\n')
    existingEntries.each { name, block ->
        def overrideWithResolved = resolvedDatasets.containsKey(name) && block.contains('CHANGE_ME')
        if (!overrideWithResolved) datasetsOut << block << '\n'
    }
    resolvedDatasets.each { name, d ->
        def keptVerbatim = existingEntries.containsKey(name) && !existingEntries[name].contains('CHANGE_ME')
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
    def acquisitions = toolAcquisitions()
    def datasetsFor = { toolName, fallback ->
        def preserved = existingToolBlocks[toolName] ? extractDatasetsSubBlock(existingToolBlocks[toolName]) : null
        if (preserved) return preserved
        def acqs = acquisitions[toolName] ?: ['DDA', 'DIA']
        def names = resolvedDatasets.findAll { _n, d -> d.acquisition in acqs }.keySet()
        if (!names) return fallback
        def out = new StringBuilder('    datasets:\n')
        names.each { n -> out << "      - ${n}\n" }
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
    def emitPreserved = { tool -> sb << existingToolBlocks[tool].replaceAll(/\n+$/, '') + '\n\n' }

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
        sb << datasetsFor.call('diann', "    datasets:\n      - CHANGE_ME\n\n")
        sb << (extractSubBlock(existingToolBlocks.diann ?: '', 'extra') ?: "    extra:\n      library: \"\"\n\n")
    } else if (existingToolBlocks.diann) {
        emitPreserved.call('diann')
    }

    if (results.alphadia) {
        sb << "  alphadia:\n    versions:\n      - id: \"latest\"\n        image: ${results.alphadia.image}\n"
        sb << "        gpu: ${results.alphadia.gpu}\n        enabled: true\n\n"
        sb << datasetsFor.call('alphadia', "    datasets:\n      - CHANGE_ME\n\n")
        sb << "    extra:\n      library: \"\"\n\n"
    } else if (existingToolBlocks.alphadia) {
        emitPreserved.call('alphadia')
    }

    if (results.sage) {
        sb << "  sage:\n    versions:\n      - id: \"latest\"\n        image: ${results.sage.image}\n"
        sb << "        sage_bin: ${results.sage.sage_bin}\n        enabled: true\n\n"
        sb << datasetsFor.call('sage', "    datasets: []   # requires mzML input (auto-detected from a <name>_mzml sibling folder)\n\n")
        sb << "    extra:\n      write_pin: true\n      parquet: false\n\n"
    } else if (existingToolBlocks.sage) {
        emitPreserved.call('sage')
    }

    if (results.fragpipe) {
        sb << "  fragpipe:\n    versions:\n      - id: \"24.0\"\n        image: ${results.fragpipe.image}\n"
        sb << "        fragpipe_root: ${results.fragpipe.fragpipe_root}\n        jars_dir: ${results.fragpipe.jars_dir}\n"
        sb << "        container_python: /usr/bin/python3\n        enabled: ${results.fragpipe.ready}\n\n"
        sb << datasetsFor.call('fragpipe', "    datasets:\n      - CHANGE_ME\n\n")
        sb << "    extra:\n      dda_workflow: LFQ-MBR\n      dia_workflow: DIA_SpecLib_Quant\n      dia_pasef_workflow: DIA_SpecLib_Quant_diaPASEF\n\n"
    } else if (existingToolBlocks.fragpipe) {
        emitPreserved.call('fragpipe')
    }

    if (results.maxquant) {
        sb << "  maxquant:\n    versions:\n"
        results.maxquant.eachWithIndex { v, i ->
            sb << "      - id: \"${v.id}\"\n        image: ${v.image}\n"
            sb << "        maxquant_dll: ${v.maxquant_dll}\n        enabled: ${i == 0}\n"
        }
        sb << "\n"
        sb << datasetsFor.call('maxquant', "    datasets:\n      - CHANGE_ME\n\n")
        sb << "    extra: {}\n\n"
    } else if (existingToolBlocks.maxquant) {
        emitPreserved.call('maxquant')
    }

    if (results.metamorpheus) {
        sb << "  metamorpheus:\n    versions:\n      - id: \"latest\"\n        image: ${results.metamorpheus.image}\n        enabled: true\n\n"
        sb << datasetsFor.call('metamorpheus', "    datasets: []   # DDA-only\n\n")
        sb << "    extra: {}\n\n"
    } else if (existingToolBlocks.metamorpheus) {
        emitPreserved.call('metamorpheus')
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
// The strict parser does not support `-entry`, so --selftest selects SELFTEST.
workflow {
    if (params.selftest) {
        SELFTEST()
    } else {
        SETUP()
    }
}

// Self-check for the version helpers that decide DIA-NN recipe ordering and
// DDA support. Run with: nextflow run setup.nf --selftest
workflow SELFTEST {
    def sorted = ['2.5.0', '1.8.1', '2.0.2', '1.9.2', '2.10.0'].toSorted { a, b -> versionOrder(a, b) }
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
    assert parsed.collect { v -> v.id } == ['1.8.1', '2.5.0'] : "got ${parsed.collect { v -> v.id }}"
    assert parsed[1].image == 'diann:2.5.0' && parsed[1].enabled == 'false' : "got ${parsed[1]}"
    assert extractSubBlock(block, 'extra').contains('library') && extractDatasetsSubBlock(block).contains('Entrapment_DIA')
    println 'SELFTEST ok'
}
