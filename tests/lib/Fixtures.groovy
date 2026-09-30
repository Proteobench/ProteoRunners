// Helpers for the pipeline tests: every test gets its own folder with a
// config.yaml and a dataset catalog whose file:// URLs point at the tiny
// archives in tests/fixtures/datasets, so no network or web server is needed.
class Fixtures {

    // Copy tests/fixtures/<configName> to <dir>/config.yaml (skipped when
    // configName is null, i.e. a first run) and write <dir>/catalog.yaml.
    static File prepare(String baseDir, String dir, String configName) {
        def fixtures = new File(baseDir, 'tests/fixtures').absolutePath
        def root = new File(dir)
        root.mkdirs()
        new File(root, 'catalog.yaml').text = new File(fixtures, 'catalog.yaml').text.replace('@FIXTURES@', fixtures)
        if (configName) new File(root, 'config.yaml').text = new File(fixtures, configName).text
        return root
    }

    // A dataset folder as setup leaves it after a finished download.
    static void extractedDataset(String dir, String name) {
        def d = new File(dir, "data/${name}")
        d.mkdirs()
        new File(d, 'run_A.raw').text = 'raw-A'
        new File(d, 'HYE.fasta').text = '>sp|P1|X\nPEPTIDEK\n'
    }

    static Map yaml(File f) { return new org.yaml.snakeyaml.Yaml().load(f.text) as Map }
}
