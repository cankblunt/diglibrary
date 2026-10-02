"""The Quality Engine: what a file actually is, as opposed to what it claims."""

from diglibrary.quality.analysis import (
    ANALYSIS_SECONDS,
    PROBE_FREQUENCIES,
    CommandRunner,
    FfmpegQualityAnalyzer,
    SubprocessCommandRunner,
)
from diglibrary.quality.models import (
    BandEnergy,
    Encoding,
    SpectralProfile,
    TimeDomainStats,
    TrackAnalysis,
)
from diglibrary.quality.verdict import (
    AlbumVerdict,
    Finding,
    TrackVerdict,
    judge,
    judge_album,
)

__all__ = [
    "ANALYSIS_SECONDS",
    "PROBE_FREQUENCIES",
    "AlbumVerdict",
    "BandEnergy",
    "CommandRunner",
    "Encoding",
    "FfmpegQualityAnalyzer",
    "Finding",
    "SpectralProfile",
    "SubprocessCommandRunner",
    "TimeDomainStats",
    "TrackAnalysis",
    "TrackVerdict",
    "judge",
    "judge_album",
]
