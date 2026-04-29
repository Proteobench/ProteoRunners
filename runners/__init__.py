from .alphadia import AlphaDIARunner
from .diann import DIANNRunner
from .fragpipe import FragPipeRunner
from .maxquant import MaxQuantRunner
from .metamorpheus import MetaMorpheusRunner
from .sage import SageRunner

RUNNER_MAP = {
    "diann": DIANNRunner,
    "alphadia": AlphaDIARunner,
    "sage": SageRunner,
    "fragpipe": FragPipeRunner,
    "maxquant": MaxQuantRunner,
    "metamorpheus": MetaMorpheusRunner,
}
