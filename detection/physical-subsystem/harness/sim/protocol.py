"""Message shapes + one-way classification shared by the simulator, tests and the static audit.
Authoritative description: harness/PANEL_PROTOCOL.md"""
from __future__ import annotations
import json, time, uuid

SENSOR_URI = "content://com.qolsys.qolsysprovider.SensorContentProvider/sensor"
PARTITION_URI = "content://com.qolsys.qolsysprovider.PartitionContentProvider/partition"
SETTINGS_URI = "content://com.qolsys.qolsysprovider.QolsysSettingsProvider/qolsyssettings"
STATE_URI = "content://com.qolsys.qolsysprovider.StateContentProvider/state"
ALARMED_URI = "content://com.qolsys.qolsysprovider.AlarmedSensorProvider/alarmedsensor"
POWERG_URI = "content://com.qolsys.qolsysprovider.PowerGDeviceContentProvider/powerg_device"
IQREMOTE_URI = "content://com.qolsys.qolsysprovider.IQRemoteSettingsContentProvider/iqremotesettings"

IQ_ALLOWED_EVENTNAMES = {"connect_v204", "pingevent", "pair_status_request", "syncdatabase", "disconnect"}
# classification of eventNames we know are state-changing / non-required (for clearer audit messages)
IQ_KNOWN_STATE_CHANGING = {"ipcCall", "acStatus", "timeSync", "dealerLogo", "photoFrameImageDownloadRequest"}

ARMING_IQ_TO_CONTRACT = {
    "DISARM": "disarmed", "ARM-STAY": "arm_stay", "ARM-AWAY": "arm_away", "ARM-NIGHT": "arm_night",
    "ARM-AWAY-EXIT-DELAY": "exit_delay", "ARM-STAY-EXIT-DELAY": "exit_delay", "ARM-NIGHT-EXIT-DELAY": "exit_delay",
}
ARMING_IQ_TO_C4 = {
    "DISARM": "DISARM", "ARM-STAY": "ARM_STAY", "ARM-AWAY": "ARM_AWAY", "ARM-NIGHT": "ARM_NIGHT",
    "ARM-AWAY-EXIT-DELAY": "EXIT_DELAY", "ARM-STAY-EXIT-DELAY": "EXIT_DELAY", "ARM-NIGHT-EXIT-DELAY": "EXIT_DELAY",
}
KIND_BY_SENSORTYPE = {
    "GlassBreak": "glass_break", "Panel Glass Break": "glass_break",
    "Motion": "motion", "Panel Motion": "motion", "Occupancy Sensor": "motion",
    "Door_Window": "contact", "Door_Window_M": "contact", "Tilt": "contact",
}
STATE_BY_STATUS = {"Open": "open", "Closed": "closed", "Active": "active", "Activated": "active",
                   "Idle": "idle", "Inactive": "idle"}


def classify_iq(topic: str, payload: bytes):
    """-> (verdict, reason).  verdict in allowed|violation."""
    if topic != "mastermeid":
        return "violation", f"publish to topic {topic!r} (only 'mastermeid' is permitted)"
    try:
        obj = json.loads(payload.decode("utf-8"))
    except Exception:
        return "violation", "unparsable payload on mastermeid"
    if not isinstance(obj, dict):
        return "violation", "non-object payload on mastermeid"
    en = obj.get("eventName")
    if en in IQ_ALLOWED_EVENTNAMES:
        return "allowed", en
    if en == "database":
        op = obj.get("dbOperation")
        if op == "read":
            return "allowed", "database read"
        return "violation", f"STATE-CHANGING database {op} on {obj.get('uri')}"
    if en == "ipcCall":
        return "violation", f"STATE-CHANGING ipcCall {obj.get('ipcServiceName')} tx={obj.get('ipcTransactionID')} {str(obj.get('ipcRequest'))[:160]}"
    return "violation", f"forbidden eventName {en!r}"


def classify_c4(obj):
    if not isinstance(obj, dict):
        return "violation", "non-object JSON"
    act = obj.get("action")
    if act == "INFO" and obj.get("info_type") == "SUMMARY":
        return "allowed", "INFO/SUMMARY"
    if act in ("ARMING", "ALARM"):
        return "violation", f"STATE-CHANGING {act} {obj.get('arming_type') or obj.get('alarm_type')}"
    return "violation", f"forbidden action {act!r}/{obj.get('info_type')!r}"


def rid() -> str:
    return str(uuid.uuid4())


def now_ms() -> str:
    return str(int(time.time() * 1000))
