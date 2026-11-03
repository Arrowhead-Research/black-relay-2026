"""RF subsystem analytics (DESIGN.md section 7.3).

Consumes normalized observations (``rf.observation.v0``), removes friendly
devices, builds visits, and estimates each emitter's zone. Emits analytics
events (``rf.analytics-event.v0``) for alerting and downstream consumers.
"""

from rfanalytics.analyzer import Analyzer, AnalyzerConfig
from rfanalytics.events import Event
from rfanalytics.friendly import FriendlyEntry, FriendlyList, load_friendly
from rfanalytics.observation import Observation, ObservationError, parse_observation
from rfanalytics.site import Site, load_site

__all__ = [
    "Analyzer",
    "AnalyzerConfig",
    "Event",
    "FriendlyEntry",
    "FriendlyList",
    "Observation",
    "ObservationError",
    "Site",
    "load_friendly",
    "load_site",
    "parse_observation",
]
