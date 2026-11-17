# Qolsys IQ Panel 5 — client protocols (as implemented by the harness simulator)

Sources: installed `qolsys_controller` library (controller.py, mqtt_command*.py, commands/panel.py, panel.py,
database/*, pki.py, zone.py, enum_qolsys.py) for IQ Remote; Control4 per the XaF/qolsysgw
`qolsys-panel-interactions.md` doc (which gives message shapes but NOT the port, TLS details or keepalive;
those parts below are marked **[sim convention]** and follow CONTRACT.md).

---------------------------------------------------------------------------------------------------
## Part 1. IQ Remote ("plugin") session — primary adapter

### 1.1 Transport / TLS (after pairing)
* TCP **8883** on the panel IP (harness: `iq_remote.panel_port`). MQTT 3.1.1 over TLS. The panel runs a Mosquitto-like broker.
* Client identity = pairing PKI directory `<pki_dir>/<id>/` where `<id>` = lowercase 12-hex MAC without colons
  (the "random_mac", e.g. `f2163e1a2b3c`, shown to panel as `f2:16:3e:1a:2b:3c`). Files:
  `<id>.key` (client private key, PKCS8 PEM), `<id>.secure` (client cert signed by the panel CA, PEM),
  `<id>.qolsys` (panel CA cert, PEM = trust anchor), `.cer`/`.csr` (pairing-time only).
  The library auto-discovers the id when the pki dir has exactly one subdirectory (`auto_discover_pki`).
* Client TLS context (library): `SERVER_AUTH`, `cafile=<id>.qolsys`, `minimum_version=TLS1.2`, ciphers `DEFAULT:@SECLEVEL=0`,
  `verify_mode=CERT_REQUIRED`, **hostname check OFF** (connect by IP, panel cert has no SAN), `VERIFY_X509_STRICT` cleared
  (panel CA lacks critical BasicConstraints), client cert chain loaded from `.secure` + `.key`.
  => A bridge MUST verify the server cert chain against the pinned panel CA (`<id>.qolsys`, or harness `iq_remote.ca_file`)
  and MUST refuse a panel presenting a cert from any other CA. The simulator broker REQUIRES a client cert signed by its CA.
* MQTT: clean_session=True, keepalive=30 s (`mqtt_timeout`), QoS 0 (`mqtt_qos`), client id `qolsys-controller-<12 hex>` (`remoteClientID`).

### 1.2 Topics
| Dir | Topic | Meaning |
|---|---|---|
| client SUB | `iq2meid` | panel -> remote: database change events (and chime/doorbell etc.) |
| client SUB | `response_<random_mac>` | panel -> remote: responses to commands (`<random_mac>` = `f2:16:3e:xx:xx:xx` WITH colons, exactly the `macAddress` given in `connect`) |
| client SUB | `ZWAVE_RESPONSE` | Z-Wave replies (not needed for PowerG sensors; may be skipped) |
| client SUB | `mastermeid` | optional debug mirror; not needed |
| client PUB | `mastermeid` | the ONLY topic the client ever publishes to: command envelope below |

### 1.3 Command envelope (client -> `mastermeid`, JSON, QoS 0)
```json
{"requestID":"<uuid4>","responseTopic":"response_<mac>","eventName":"<name>","remoteMacAddress":"<mac>", ...extra}
```
Panel replies on `responseTopic` with a JSON object that contains the same `"requestID"` (library correlates solely on it;
timeout 30 s). Extra fields by eventName:

| eventName | extra fields | panel response (fields used by clients) | classification |
|---|---|---|---|
| `connect_v204` | `ipAddress, pairing_request:true, macAddress, remoteClientID, softwareVersion:"4.4.1", productType:"tab07_rk68", bssid, lastUpdateChecksum, dealerIconsCheckSum, remote_feature_support_version:"1", current_battery_status:"Normal", remote_panel_battery_* , dhcpInfo(JSON string)` | `master_imei`, `primary_product_type` (+ misc). Must be the first command of a session. | **READ-ONLY-REQUIRED** (session handshake; panel only records the remote as connected) |
| `pingevent` | `remote_panel_status:"Active", macAddress, ipAddress, current_battery_status, remote_panel_battery_*` | ack (any JSON with requestID). Sent once after connect, then every `mqtt_ping`=300 s. | **READ-ONLY-REQUIRED** (keepalive; also usable as the liveness probe) |
| `pair_status_request` | none | ack | **READ-ONLY-REQUIRED** (library sends it in handshake) |
| `syncdatabase` | none | `{"requestID":..., "fulldbdata":[ {"uri":"content://...","resultSet":[{row},...]}, ...]}` | **READ-ONLY-REQUIRED** (full snapshot) |
| `database` + `dbOperation:"read"` | `uri, projection, selection, selectionArgs, sortOrder` | `{"responseStatus":"success","resultSet":[...]}` | read-only, not required (allowed) |
| `disconnect` | none | ack | allowed (polite teardown), not required |
| `database` + `dbOperation:"insert"/"update"/"delete"` | `uri, contentValues, selection...` | `responseStatus`, `longValue` | **STATE-CHANGING** (writes panel DB, e.g. creates camera request) |
| `ipcCall` (any `ipcServiceName`) | `ipcServiceName, ipcInterfaceName, ipcTransactionID, ipcRequest:[{dataType,dataValue}]` | result | **STATE-CHANGING**. Includes: `qinternalservice` tx 7 with JSON `operation_name` in {`ui_armstay`,`ui_armaway`,`ui_armnight` (ARM), `disarm_from_emergency`,`disarm_from_openlearn_sensor`,`disarm_the_panel_from_entry_delay` (DISARM), `ui_delay`, `generate_emergency` (police/fire/aux TRIGGER), `execute_scene`, `quick_exit_state`, `speak_text`, `changeMasterVolumeLevel`, `changeDoorBellVolumeLevel`, `send_virtual_device_description` (ADC)}; also bypass (`bypass_zoneid_set` inside arm), `qzwaveservice` tx 47 and `zwaveservice` tx 28 (Z-Wave SET/GET raw), `qautomationservice` tx 1 (lock/unlock, light on/off), `qcamservice` (capture/delete photo), user-code ops (arm/disarm carry `userID`) |
| `acStatus` (`acStatus:"Connected"`), `timeSync` (`startTimestamp`), `dealerLogo`, `photoFrameImageDownloadRequest`, `changeMasterVolumeLevel` etc. | | | acStatus/timeSync/dealerLogo/photo*: **NOT REQUIRED -> treated as violations by the harness** (timeSync mutates panel clock sync state; photo* touches panel camera dir) |

**Harness rule (IQ side)**: an inbound publish is ALLOWED iff topic == `mastermeid` AND eventName in
`{connect_v204, pingevent, pair_status_request, syncdatabase, disconnect}` OR (eventName == `database` AND dbOperation == `read`).
Anything else — other topic, `ipcCall`, db insert/update/delete, acStatus, timeSync, … — is a **ONE-WAY VIOLATION**.
(MQTT-level SUBSCRIBE/PING/CONNECT/DISCONNECT are protocol plumbing and are not violations.)

### 1.4 Database snapshot (`fulldbdata`) — what a bridge needs
`fulldbdata` is a list of `{ "uri": <content URI>, "resultSet": [ {col: "string value", ...}, ... ] }`; ALL values are strings.
Tables a PowerG-sensor bridge uses:

* **Sensors** `uri = content://com.qolsys.qolsysprovider.SensorContentProvider/sensor` — one row per zone. Columns (subset used):
  `_id, zoneid (key; "1","12"…), sensorname, sensortype, sensorgroup, sensorstatus, sensorstate, partition_id, battery_status, time (epoch ms string),
  current_capability ("POWERG" for PowerG), shortID (PowerG short id), latestdBm, averagedBm, zone_type, zone_physical_type, zone_alarm_type,
  zone_supervised, signal_source, extras (JSON string), updated_date, ...`
  * `sensortype` values (ZoneSensorType): `Door_Window`, `Door_Window_M`, `Motion`, `Panel Motion`, `Occupancy Sensor`, `GlassBreak`,
    `Panel Glass Break`, `Shock`, `Tilt`, `SmokeDetector`, `Smoke_M`, `CODetector`, `Water`, `Freeze`, `Heat`, `Temperature`, `Keyfob`/`KeyFob`, `Keypad`, `Panic`, `Auxiliary Pendant`, `Tamper Sensor`, `Siren`, `PowerGSiren`, `Unknown`, ...
    **Recommended kind map**: GlassBreak, Panel Glass Break -> `glass_break`; Motion, Panel Motion, Occupancy Sensor -> `motion`;
    Door_Window, Door_Window_M, Tilt, Shock(optional) -> `contact`; everything else -> `other`.
  * `sensorstatus` values (ZoneStatus): `Open`, `Closed`, `Active`, `Activated`, `Idle`, `Inactive`, `Normal`, `Tampered`, `Alarmed`, `Failure`,
    `Unreachable`, `Not Networked`, `Synchronizing`, `Trouble`, `disconnected`, `connected`, `Unknown`, `Vacant`, `Occupied`, … . Doors: Open/Closed;
    motion & glass-break: Active/Idle. Recommended: Open->open, Closed->closed, Active|Activated|Alarmed->active, Idle|Inactive|Normal|Vacant->idle (closed for contact kind),
    `Tampered` => tamper=true; `Unreachable|Failure|Not Networked|disconnected` => online=false; unseen => unknown.
  * `battery_status`: `Normal` | `Low` | ... (library only tests non-empty). Recommended battery_low = value not in {"", "Normal"}.
* **Partitions** `uri = content://com.qolsys.qolsysprovider.PartitionContentProvider/partition` rows: `_id, partition_id ("0","1".. string), name, devices`.
* **Settings** `uri = content://com.qolsys.qolsysprovider.QolsysSettingsProvider/qolsyssettings` rows: `_id, partition_id, name, value`.
  Per partition: `name="SYSTEM_STATUS"` value in {`DISARM`,`ARM-STAY`,`ARM-AWAY`,`ARM-NIGHT`,`ARM-AWAY-EXIT-DELAY`,`ARM-STAY-EXIT-DELAY`,`ARM-NIGHT-EXIT-DELAY`}; `SYSTEM_STATUS_CHANGED_TIME`; `ENTRY_DELAYS`; `EXIT_SOUNDS`.
  Panel-wide settings use partition_id "0" (`AC_STATUS`, `PANEL_TAMPER_STATE`, `BATTERY_STATUS`, …).
  (URIs above are verbatim from the library's `database/table_*.py`.)
* **State** `uri = content://com.qolsys.qolsysprovider.StateContentProvider/state` rows `_id, partition_id, name, value, extraparams`; `name="ALARM_STATE"` value `None|Delay|Alarm`; `QUICK_EXIT_STATE`.
* **Alarmed sensors** `uri = content://com.qolsys.qolsysprovider.AlarmedSensorProvider/alarmedsensor` rows `partition_id, zone_id, sgroup, action, silenced, timed_out, type, alarm_time`; `sgroup` = alarm type (`glassbreak`, `entryexitdelay`, `Police Emergency`, `Fire Emergency`, ...). Present while alarmed.
* **PowerG** `uri = content://com.qolsys.qolsysprovider.PowerGDeviceContentProvider/powerg_device` rows keyed by `shortID`: `longID, link_quality, avg_link_quality, link_status, battery_voltage, status_data, temperature, light, notification_period, ...` (joined to a zone via `shortID`).
Other tables exist (zwave, thermostats, scenes, users...) and can be ignored; an empty/absent table is fine.

### 1.5 Live updates (panel -> `iq2meid`, JSON)
All have `"eventName":"dbChanged"`, `"dbOperation": "insert"|"update"|"delete"`, `"uri": <table uri>`.
* update: `"contentValues": {col:val,...}`, `"selection":"zoneid=?"` , `"selectionArgs": ["12"]` (firmware 4.4.1 sends `"[12]"` as a STRING; 4.6.1 a list — accept both). Sensor update example:
  `{"eventName":"dbChanged","dbOperation":"update","uri":".../SensorContentProvider/sensor","contentValues":{"zoneid":"12","sensorstatus":"Open","time":"1731600000000", "sensorname":"Front door", "battery_status":"Normal", "partition_id":"0"},"selection":"zoneid=?","selectionArgs":["12"]}`.
  The library finds the zone by `contentValues.zoneid`; a bridge MUST do likewise (selection is SQL text — do not evaluate; fall back to `selectionArgs[0]` if `zoneid` absent).
* partition arming: update on settings uri `contentValues {"name":"SYSTEM_STATUS","value":"ARM-AWAY","partition_id":"0"}` (+ `SYSTEM_STATUS_CHANGED_TIME`);
  alarm: update/insert on state uri `{"name":"ALARM_STATE","value":"Alarm","partition_id":"0"}`; alarm types: insert/delete on alarmedsensor uri.
* zone add: `insert` on sensor uri with `contentValues` = full row (library then re-reads the DB: treat as add/replace); zone remove: `delete` on sensor uri with `selection:"zoneid=?"`, `selectionArgs:["12"]`.
* powerg update on powerg uri keyed by `shortID`.
* other events seen: `primaryDisconnect`, `chime`, `eventNameDoorBell`, `stopScreenCapture` (ignore).
A client that missed events (disconnect) MUST re-run connect -> syncdatabase and diff (events are not replayed; clean_session).

### 1.6 Session order the library uses (a bridge should mirror exactly, nothing more)
1. TLS+MQTT connect; SUB `iq2meid`, `response_<mac>` (and `ZWAVE_RESPONSE` optional); 2. sleep ~2 s (library) — not needed;
3. `connect_v204` -> 4. `pingevent` -> 5. `pair_status_request` -> 6. `syncdatabase` -> load `fulldbdata`; 7. `pingevent` every 300 s (config `silence_probe_s` probe may use `pingevent`).
Reconnect delay in the library: 1,2,4,…,300 s.

---------------------------------------------------------------------------------------------------
## Part 2. Control4 interface — fallback adapter

Panel option "Control4" must be enabled with a token. **[sim convention]** TLS server on TCP **12345** (`control4.port`); self-signed panel cert, bridge pins `control4.cert_sha256` = SHA-256 of the **DER leaf certificate** (hex, lowercase, no colons; accept colons/uppercase in config). Hostname/CA validation off; fingerprint mismatch => refuse (abort handshake, no bytes sent).
Framing **[sim convention]**: a stream of UTF-8 JSON objects. The simulator emits each object followed by `\n`; it accepts input as concatenated JSON objects with optional whitespace. Clients MUST use an incremental decoder (`json.JSONDecoder.raw_decode` / `json.Decoder` / `serde_json::StreamDeserializer`), not assume one object per TCP read.

### 2.1 Client -> panel ("actions")
Common fields: `"token":"<token>"`, `"nonce":"qolsys"`, `"source":"C4"`, `"version":0`. Token comes from `control4.token_file` (strip whitespace).
```json
{"action":"INFO","info_type":"SUMMARY","token":"T","nonce":"qolsys","source":"C4","version":0}
```
| action | fields | classification |
|---|---|---|
| `INFO` / `info_type:"SUMMARY"` | token | **READ-ONLY-REQUIRED** (full snapshot; also the keepalive/liveness probe) |
| `INFO` / `info_type:"SECURE_ARM"` | token | read-only but not needed -> harness treats as violation |
| `ARMING` / `arming_type` ARM_STAY, ARM_AWAY (`delay`,`bypass`), DISARM (`usercode`) | `partition_id`, token | **STATE-CHANGING** |
| `ALARM` / `alarm_type` POLICE, FIRE, AUXILIARY | `partition_id`, token | **STATE-CHANGING** (triggers alarm) |
**Harness rule (C4 side)**: ALLOWED iff `action=="INFO"` and `info_type=="SUMMARY"` (and token valid). Anything else (incl. unparsable JSON containing `"action"` other than INFO/SUMMARY) => **ONE-WAY VIOLATION**. A wrong token: simulator replies ERROR `INVALID_TOKEN` and closes (counted as `bad_token`, not a violation).
Keepalive **[sim convention]**: the panel answers every action with an ACK event `{"event":"ACK","requestID":"<uuid>","version":0}` followed (for INFO/SUMMARY) by the INFO SUMMARY event. A bridge uses periodic INFO/SUMMARY as keepalive/probe. The sim pushes ZONE_EVENT/ARMING/ALARM only on change.

### 2.2 Panel -> client ("events"); each has `"requestID":"<uuid>"`, `"version":0`
* **INFO SUMMARY** (response to the request; also what a diff must use as full snapshot):
```json
{"event":"INFO","info_type":"SUMMARY","partition_list":[
  {"partition_id":0,"name":"Main","status":"DISARM","secure_arm":false,
   "zone_list":[{"id":"12","type":"Door_Window","name":"Front door","group":"entryexitdelay","status":"Closed","state":"0",
                 "zone_id":12,"zone_physical_type":1,"zone_alarm_type":1,"zone_type":1,"partition_id":0}]}],
 "nonce":"qolsys","requestID":"..."}
```
  `status` partition values: `DISARM`, `ARM_STAY`, `ARM_AWAY` (and exit-delay forms; sim uses the IQ strings `ARM-STAY`… -> **[sim convention]** the sim emits `DISARM|ARM_STAY|ARM_AWAY|ARM_NIGHT|ENTRY_DELAY|EXIT_DELAY|ALARM`; bridges SHOULD also accept the hyphenated IQ spellings). Zone `type` uses the same sensortype strings as IQ; `status` the same ZoneStatus strings.
  `zone_id` may be int or str; `id` is a string — accept both.
* **ZONE_EVENT**: `{"event":"ZONE_EVENT","zone_event_type":"ZONE_ACTIVE","zone":{"zone_id":12,"status":"Open"},...}` (`ZONE_ACTIVE` also signals tamper: status `Tampered`); `ZONE_UPDATE` carries the full zone object (same keys as summary zone: id,type,name,group,status,state,zone_id,partition_id...); `ZONE_ADD` same shape (the real panel's `zone_event_type` has a leading space `" ZONE_ADD"` — strip it; the simulator reproduces the quirk). **There is no ZONE_REMOVE event**: removal is only visible by diffing the next SUMMARY.
* **ARMING**: `{"event":"ARMING","arming_type":"DISARM","partition_id":0,...}` (`ARM_STAY|ARM_AWAY|DISARM|EXIT_DELAY|ENTRY_DELAY` **[sim]**).
* **ALARM**: `{"event":"ALARM","alarm_type":"","partition_id":0,...}` (`alarm_type` may be empty; `POLICE|FIRE|AUXILIARY`).
* **ERROR**: `{"event":"ERROR","partition_id":0,"error_type":"DISARM_FAILED","description":"Invalid usercode"}` (only after commands; never expected for a one-way bridge).
* **ACK** as above.
There is no per-event push for battery / partition arming beyond ARMING; the bridge should re-request SUMMARY periodically if it wants battery, and must after every reconnect.

---------------------------------------------------------------------------------------------------
## Part 3. How the simulator realises this
* IQ side: a real Mosquitto per simulated panel on `iq_port` (TLS, `require_certificate true`, CA = sim test CA, server cert signed by the same CA unless `rotate_cert`/`untrusted` is used) + an internal loopback listener (127.0.0.1, sim-only). The sim "panel logic" is an MQTT client on the loopback listener: subscribes `#`, auditing everything published by anyone (only the bridge publishes besides the sim itself), answers `mastermeid` commands on `responseTopic`, and publishes `iq2meid` events. Outage = SIGKILL mosquitto (connection refused / RST); stall = SIGSTOP; drop = kill+restart; cert rotate = restart with new server cert.
* Control4 side: asyncio TLS server (see `sim/panel_sim.py`).
