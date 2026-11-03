"""Multi-node RF observation simulator for the Black Relay RF subsystem.

Generates the normalized observations that rf-analytics ingests (see
``schemas/observation.v0.schema.json``) from scripted scenarios, together with
ground truth for scoring analytics output.
"""

from rfsim.engine import SimulationResult, simulate
from rfsim.model import Observation
from rfsim.scenario import Scenario, load_scenario
from rfsim.site import Site, load_site

__all__ = [
    "Observation",
    "Scenario",
    "SimulationResult",
    "Site",
    "load_scenario",
    "load_site",
    "simulate",
]
