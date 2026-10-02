"""Library Engine: what is on disk, and what it should become."""

from diglibrary.library.audio import (
    AUDIO_EXTENSIONS,
    AudioProbe,
    AudioProperties,
    MutagenAudioProbe,
)
from diglibrary.library.executor import ChangeExecutor, ExecutionResult, ExecutionState
from diglibrary.library.matching import (
    AlbumMatcher,
    MatchCandidate,
    TrackAlignment,
    align_tracks,
    reconcile,
)
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import ChangeOperation, ChangePlan, ChangePlanner, OperationKind
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.signature import content_signature, unit_signature
from diglibrary.library.tags import MutagenTagStore, TagStore, desired_tags

__all__ = [
    "AUDIO_EXTENSIONS",
    "AlbumMatcher",
    "AlbumUnit",
    "AudioFileFacts",
    "AudioProbe",
    "AudioProperties",
    "ChangeExecutor",
    "ChangeOperation",
    "ChangePlan",
    "ChangePlanner",
    "ExecutionResult",
    "ExecutionState",
    "LibraryScanner",
    "MatchCandidate",
    "MutagenAudioProbe",
    "MutagenTagStore",
    "NamingPolicy",
    "OperationKind",
    "TagStore",
    "TrackAlignment",
    "align_tracks",
    "content_signature",
    "desired_tags",
    "reconcile",
    "unit_signature",
]
