"""General helper utilities for Bermuda"""

from __future__ import annotations


def rssi_to_metres(rssi, ref_power=None, attenuation=None):
    """Convert instant rssi value to a distance in metres

    Based on the information from
    https://mdpi-res.com/d_attachment/applsci/applsci-10-02003/article_deploy/applsci-10-02003.pdf?version=1584265508

    attenuation:    a factor representing environmental attenuation
                    along the path. Will vary by humidity, terrain etc.
    ref_power:      db. measured rssi when at 1m distance from rx. The will
                    be affected by both receiver sensitivity and transmitter
                    calibration, antenna design and orientation etc.

    TODO: the ref_power and attenuation figures can/should probably be mapped
        against each receiver and transmitter for variances. We could also fine-
        tune the attenuation in real time based on changing values coming from
        known-fixed beacons (eg thermometers, window sensors etc)
    """
    if ref_power is None:
        return False
        # ref_power = self.ref_power
    if attenuation is None:
        return False
        # attenuation= self.attenuation

    distance = 10 ** ((ref_power - rssi) / (10 * attenuation))
    return distance
