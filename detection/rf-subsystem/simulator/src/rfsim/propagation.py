"""Simple radio propagation and detection model.

Log-distance path loss anchored to free-space loss at 1 m, plus log-normal
shadowing. Detection probability falls off smoothly near receiver sensitivity
and includes a baseline packet-loss rate. This is deliberately simple: the
goal is realistic *noise characteristics* for analytics, not RF accuracy.
"""

from __future__ import annotations

import math
import random


def path_loss_db(distance_m: float, frequency_mhz: float, exponent: float) -> float:
    d = max(distance_m, 1.0)
    fspl_1m = 20.0 * math.log10(frequency_mhz) - 27.55
    return fspl_1m + 10.0 * exponent * math.log10(d)


def received_power_dbm(
    rng: random.Random,
    tx_power_dbm: float,
    distance_m: float,
    frequency_mhz: float,
    exponent: float,
    shadow_sigma_db: float,
) -> float:
    return tx_power_dbm - path_loss_db(distance_m, frequency_mhz, exponent) + rng.gauss(0.0, shadow_sigma_db)


def detection_probability(rssi_dbm: float, sensitivity_dbm: float, base_loss: float) -> float:
    """Logistic roll-off around sensitivity, capped by the baseline loss rate."""
    x = (rssi_dbm - sensitivity_dbm) / 2.0
    # Guard against overflow for very weak signals.
    if x < -50:
        return 0.0
    return (1.0 - base_loss) / (1.0 + math.exp(-x))
