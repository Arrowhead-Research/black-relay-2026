"""RF subsystem analytics (DESIGN.md section 7.3).

Consumes normalized observations (``rf.observation.v0``), removes friendly
devices, builds visits, and estimates each emitter's zone. Emits analytics
events (``rf.analytics-event.v0``) for alerting and downstream consumers.
"""

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

_LAZY = {
    "Analyzer": ("rfanalytics.analyzer", "Analyzer"),
    "AnalyzerConfig": ("rfanalytics.analyzer", "AnalyzerConfig"),
    "Event": ("rfanalytics.events", "Event"),
    "FriendlyEntry": ("rfanalytics.friendly", "FriendlyEntry"),
    "FriendlyList": ("rfanalytics.friendly", "FriendlyList"),
    "load_friendly": ("rfanalytics.friendly", "load_friendly"),
    "Observation": ("rfanalytics.observation", "Observation"),
    "ObservationError": ("rfanalytics.observation", "ObservationError"),
    "parse_observation": ("rfanalytics.observation", "parse_observation"),
    "Site": ("rfanalytics.site", "Site"),
    "load_site": ("rfanalytics.site", "load_site"),
}


def __getattr__(name: str):
    if name in _LAZY:
        mod_name, attr = _LAZY[name]
        import importlib

        mod = importlib.import_module(mod_name)
        value = getattr(mod, attr)
        globals()[name] = value
        return value
    raise AttributeError(name)
