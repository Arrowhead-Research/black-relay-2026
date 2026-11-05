"""The rendered Frigate config, pinned to Frigate 0.18.2 and its go2rtc 1.9.14."""

import errno
import inspect
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote_plus

import pytest
import yaml

from cameralink.frigate_config import (
    CAMERA_PASSWORD_ENV,
    FULL_FRAME,
    PROXY_SECRET_ENV,
    RELAY_PASSWORD_ENV,
    FrigateConfigError,
    render,
    write_env,
)

CAMERA_PASSWORD = "cam-S3cret-password"
RELAY_PASSWORD = "relay-S3cret-password"
PROXY_SECRET = "proxy-S3cret-token"


def text(**overrides) -> str:
    args = {
        "node_id": "cam-8da040",
        "covered_zones": ["driveway", "gate"],
        "relay_user": "kit1",
        "existing": None,
        "proxy_secret": PROXY_SECRET,
    }
    args.update(overrides)
    return render(**args)


def config(**overrides) -> dict:
    return yaml.safe_load(text(**overrides))


def camera(**overrides) -> dict:
    return config(**overrides)["cameras"]["cam-8da040"]


def inputs_by_role(cam: dict) -> dict:
    return {role: item for item in cam["ffmpeg"]["inputs"] for role in item["roles"]}


def drawn(zones: dict, cam: str = "cam-8da040") -> dict:
    return {
        "cameras": {
            cam: {"zones": {name: {"coordinates": c} for name, c in zones.items()}},
        }
    }


# Camera inputs


def test_camera_is_named_node_id_and_pulls_rtsp_over_tcp():
    cfg = config()
    assert list(cfg["cameras"]) == ["cam-8da040"]
    cam = cfg["cameras"]["cam-8da040"]
    for item in cam["ffmpeg"]["inputs"]:
        assert item["input_args"] == "-rtsp_transport tcp"
        assert item["path"].startswith("rtsp://admin:")
        assert "@10.77.0.10:554/" in item["path"]


@pytest.mark.parametrize("node_id", ["123", "1", "null", "true", "1e3", "0x10", "on", "no"])
def test_a_node_id_yaml_would_read_as_another_type_stays_a_string(node_id):
    # Provisioning allows all of these, and Frigate refuses a camera key that is not a
    # string, so one unquoted key would cost the kit all detection.
    cfg = config(node_id=node_id, covered_zones=["gate"])
    assert list(cfg["cameras"]) == [node_id]
    assert list(cfg["go2rtc"]["streams"]) == [node_id]


def test_detect_uses_sub_stream_and_record_uses_main():
    roles = inputs_by_role(camera())
    assert set(roles) == {"detect", "record"}
    assert roles["detect"]["path"].endswith("/h264Preview_01_sub")
    assert roles["record"]["path"].endswith("/h264Preview_01_main")


def test_detect_is_explicitly_enabled_because_frigate_defaults_it_off():
    # Nothing else is set: max_disappeared, min_initialized and the stationary threshold
    # and interval all default to a multiple of fps in Frigate, so they keep their meaning.
    assert camera()["detect"] == {"enabled": True, "width": 640, "height": 480, "fps": 15}


def test_person_and_car_are_tracked():
    assert config()["objects"] == {"track": ["person", "car"]}


# Detector and broker


def test_detector_is_hailo_with_the_local_model_path():
    cfg = config()
    assert cfg["detectors"] == {"hailo": {"type": "hailo8l", "device": "PCIe"}}
    assert cfg["model"] == {
        "path": "/config/model_cache/yolov6n.hef",
        "width": 320,
        "height": 320,
        "input_tensor": "nhwc",
        "input_pixel_format": "rgb",
        "input_dtype": "int",
        "model_type": "yolo-generic",
        "labelmap_path": "/labelmap/coco-80.txt",
    }


def test_mqtt_is_loopback_1883():
    assert config()["mqtt"] == {"host": "127.0.0.1", "port": 1883}


def test_config_version_is_current_so_frigate_does_not_rewrite_the_file():
    # With no version Frigate migrates from 0.13 and rewrites the file at start, which
    # would make every later render look like a change.
    assert config()["version"] == "0.18-0"


def test_only_sections_frigate_accepts_are_written():
    # Pins the list only. FrigateConfig forbids unknown keys, and this suite cannot run it,
    # so a new section needs checking against Frigate's own model.
    assert set(config()) == {
        "version",
        "mqtt",
        "auth",
        "proxy",
        "tls",
        "detectors",
        "model",
        "objects",
        "go2rtc",
        "cameras",
    }


# Secrets


def test_no_password_appears_in_the_yaml():
    # render is never given a camera or relay password, and only checks the proxy secret.
    assert set(inspect.signature(render).parameters) == {
        "node_id",
        "covered_zones",
        "relay_user",
        "existing",
        "proxy_secret",
    }
    assert PROXY_SECRET not in text()


def test_template_holds_no_literal_camera_credentials():
    template = Path(__file__).parents[1] / "src/cameralink/templates/frigate.yml.tmpl"
    logins = re.findall(r"admin:([^@\s\"]*)@10\.77\.0\.10", template.read_text())
    assert logins
    assert set(logins) == {"{FRIGATE_CAMERA_PASSWORD}"}


def test_each_secret_uses_the_reference_syntax_its_reader_expands():
    cfg = config()
    camera_ref = "{" + CAMERA_PASSWORD_ENV + "}"
    # Frigate's EnvString fields.
    assert cfg["proxy"]["auth_secret"] == "{" + PROXY_SECRET_ENV + "}"
    for item in cfg["cameras"]["cam-8da040"]["ffmpeg"]["inputs"]:
        assert f"admin:{camera_ref}@" in item["path"]
    # Frigate's create_config.py runs str.format over go2rtc.streams.
    for source in cfg["go2rtc"]["streams"]["cam-8da040"]:
        assert f"admin:{camera_ref}@" in source
    # create_config.py runs Frigate's own substitution over go2rtc.rtsp.password.
    assert cfg["go2rtc"]["rtsp"]["password"] == "{" + RELAY_PASSWORD_ENV + "}"


def test_stream_sources_survive_frigates_str_format_pass():
    # A stray brace here makes create_config.py exit, and then go2rtc never starts.
    env = {CAMERA_PASSWORD_ENV: "x-y", RELAY_PASSWORD_ENV: "r", PROXY_SECRET_ENV: "p"}
    for source in config()["go2rtc"]["streams"]["cam-8da040"]:
        assert "x-y@10.77.0.10" in source.format(**env)


# Live view. Unverified on hardware: see the module docstring.


def test_live_view_serves_the_main_stream_under_the_camera_name():
    assert config()["go2rtc"]["streams"] == {
        "cam-8da040": [
            "rtsp://admin:{"
            + CAMERA_PASSWORD_ENV
            + "}@10.77.0.10:554/h264Preview_01_main"
        ]
    }


def test_live_view_requires_the_relay_login():
    assert config()["go2rtc"]["rtsp"] == {
        "username": "kit1",
        "password": "{" + RELAY_PASSWORD_ENV + "}",
    }


def test_nothing_keeps_the_camera_open_or_publishes_video():
    # go2rtc dials a source for its first consumer and drops it after the last. publish
    # and preload would each hold a consumer open, so neither may appear.
    assert set(config()["go2rtc"]) == {"rtsp", "streams"}


def test_no_rtmp_or_relay_address_is_left():
    module = Path(__file__).parents[1] / "src/cameralink/frigate_config.py"
    template = module.parent / "templates/frigate.yml.tmpl"
    for body in (module.read_text(), template.read_text(), text()):
        assert "rtmp" not in body.lower()
        assert "mediamtx_address" not in body
        assert "1935" not in body


def test_anonymous_broker_leaves_rtsp_on_loopback_with_no_login():
    # go2rtc skips the login check when no username is set, and an open video port is
    # worse than no live view. Raising would cost local detection and recording as well.
    cfg = config(relay_user="")
    assert cfg["go2rtc"]["rtsp"] == {"listen": "127.0.0.1:8554"}
    assert "{" + RELAY_PASSWORD_ENV + "}" not in text(relay_user="")


@pytest.mark.parametrize("user", ["kit1", "Kit_1", "a.b-c", "me@site", "a+b~c"])
def test_ordinary_relay_users_are_written_as_they_are(user):
    assert config(relay_user=user)["go2rtc"]["rtsp"]["username"] == user


@pytest.mark.parametrize(
    "user",
    # ":" splits Basic auth. Braces go through Frigate's substitution, and "$" through
    # go2rtc's, before the name is ever compared.
    ["a:b", "{FRIGATE_X}", "a{{b", "a}b", "${HOME}", "a b", "a\nb", "é", "bad\ud800", 'a"b', None, 7],
)
def test_unusable_relay_user_raises(user):
    with pytest.raises(FrigateConfigError, match="relay_user"):
        text(relay_user=user)


# Auth


def test_frigate_login_off_proxy_secret_from_env_tls_on():
    cfg = config()
    assert cfg["auth"] == {"enabled": False}
    assert cfg["proxy"] == {"auth_secret": "{FRIGATE_PROXY_SECRET}"}
    assert cfg["tls"] == {"enabled": True}


@pytest.mark.parametrize("secret", ["", "has space", "new\nline", 'quo"te', "dollar$"])
def test_render_refuses_a_proxy_secret_write_env_would_refuse(secret):
    with pytest.raises(FrigateConfigError, match="proxy secret"):
        text(proxy_secret=secret)


# Recording


def test_recording_retention_values():
    assert camera()["record"] == {
        "enabled": True,
        "continuous": {"days": 0},
        "motion": {"days": 1},
        "alerts": {"retain": {"days": 7}},
        "detections": {"retain": {"days": 7}},
    }


def test_snapshots_are_on_and_kept_as_long_as_the_event_recordings():
    # Off by default in Frigate, and then every finished event's snapshot_url is a 404.
    cam = camera()
    assert cam["snapshots"] == {"enabled": True, "retain": {"default": 7}}
    assert cam["snapshots"]["retain"]["default"] == cam["record"]["alerts"]["retain"]["days"]
    assert cam["snapshots"]["retain"]["default"] == cam["record"]["detections"]["retain"]["days"]


# Zones


def test_new_zone_gets_a_full_frame_polygon():
    zones = camera()["zones"]
    assert zones == {
        "driveway": {"coordinates": FULL_FRAME},
        "gate": {"coordinates": FULL_FRAME},
    }
    assert FULL_FRAME == "0,0,1,0,1,1,0,1"


def test_full_frame_is_read_by_frigate_as_relative():
    # Frigate compares points as strings against "1.0" to spot legacy pixel coordinates.
    assert not any(point > "1.0" for point in FULL_FRAME.split(","))


def test_existing_zone_coordinates_survive_a_re_render():
    zones = camera(existing=drawn({"gate": "0.1,0.2,0.3,0.2,0.3,0.9"}))["zones"]
    assert zones["gate"]["coordinates"] == "0.1,0.2,0.3,0.2,0.3,0.9"
    assert zones["driveway"]["coordinates"] == FULL_FRAME


def test_existing_list_form_coordinates_survive():
    points = ["0.1,0.2", "0.3,0.2", "0.3,0.9"]
    assert camera(existing=drawn({"gate": points}))["zones"]["gate"]["coordinates"] == points


def test_removed_zone_is_dropped():
    existing = drawn({"gate": "0.1,0.2,0.3,0.2,0.3,0.9", "old": "0,0,1,0,1,1"})
    assert set(camera(existing=existing)["zones"]) == {"driveway", "gate"}


def test_drawn_coordinates_survive_a_node_id_change():
    existing = drawn({"gate": "0.1,0.2,0.3,0.2,0.3,0.9"}, cam="cam-old")
    cfg = config(node_id="cam-new", existing=existing)
    assert list(cfg["cameras"]) == ["cam-new"]
    assert cfg["cameras"]["cam-new"]["zones"]["gate"]["coordinates"] == "0.1,0.2,0.3,0.2,0.3,0.9"


def test_current_node_id_wins_over_a_leftover_camera():
    existing = {
        "cameras": {
            "aaa-other": {"zones": {"gate": {"coordinates": "0,0,0.5,0,0.5,0.5"}}},
            "cam-8da040": {"zones": {"gate": {"coordinates": "0.1,0.2,0.3,0.2,0.3,0.9"}}},
        }
    }
    assert camera(existing=existing)["zones"]["gate"]["coordinates"] == "0.1,0.2,0.3,0.2,0.3,0.9"


@pytest.mark.parametrize(
    "existing",
    [{}, {"cameras": None}, {"cameras": {"cam-8da040": None}}, {"cameras": {"cam-8da040": {"zones": []}}}],
)
def test_existing_without_zones_means_full_frame(existing):
    assert camera(existing=existing)["zones"]["gate"]["coordinates"] == FULL_FRAME


@pytest.mark.parametrize(
    "coordinates", ["", [], 7, "0,0,1", "0,0,1,0", "a,b,c,d,e,f", ["0,0", "1,0", "bad"], [1, 2, 3]]
)
def test_unusable_existing_coordinates_raise_and_name_the_zone(coordinates):
    with pytest.raises(FrigateConfigError, match="gate"):
        text(existing=drawn({"gate": coordinates}))


@pytest.mark.parametrize("zone", [None, "0,0,1,0,1,1", {}, {"coordinates": None}, {"inertia": 3}, []])
@pytest.mark.parametrize("cam", ["cam-8da040", "cam-old"])
def test_a_saved_zone_of_unusable_shape_raises_and_never_becomes_full_frame(zone, cam):
    existing = {"cameras": {cam: {"zones": {"gate": zone}}}}
    with pytest.raises(FrigateConfigError, match="gate"):
        text(existing=existing)


def test_a_removed_zone_of_unusable_shape_is_just_dropped():
    existing = {"cameras": {"cam-8da040": {"zones": {"old": None}}}}
    assert set(camera(existing=existing)["zones"]) == {"driveway", "gate"}


@pytest.mark.parametrize("name", ["test_zone", "zone-2", "Yard1", "on", "123", "null"])
def test_valid_zone_names_come_back_as_the_same_string(name):
    # Names such as "on" and "123" must stay strings: the bridge matches them against
    # Frigate's current_zones, and the RF side's vocabulary.
    assert list(camera(covered_zones=[name])["zones"]) == [name]


@pytest.mark.parametrize(
    "name",
    ["Zone 2", "a.b", "a/b", "a+", "a#", "x", "", "zone\n", "zöne", "a:b", 'a"b', "{FRIGATE_X}", None, 7],
)
def test_invalid_zone_name_raises_a_clear_error(name):
    with pytest.raises(FrigateConfigError, match="zone name"):
        text(covered_zones=["driveway", name])


def test_zone_named_like_the_camera_raises():
    # Frigate refuses this at load: "Zones cannot share names with cameras".
    with pytest.raises(FrigateConfigError, match="cam-8da040"):
        text(covered_zones=["cam-8da040"])


def test_a_bare_string_for_covered_zones_raises():
    with pytest.raises(FrigateConfigError, match="covered_zones"):
        text(covered_zones="driveway")


@pytest.mark.parametrize("covered_zones", [None, 7])
def test_covered_zones_that_is_not_a_collection_raises(covered_zones):
    with pytest.raises(FrigateConfigError, match="covered_zones"):
        text(covered_zones=covered_zones)


def test_a_generator_of_zones_keeps_its_zones():
    assert set(camera(covered_zones=(name for name in ["driveway", "gate"]))["zones"]) == {"driveway", "gate"}


# Other inputs


@pytest.mark.parametrize("node_id", ["", "a b", "a.b", "a/b", "a\n", "a: b", "{x}", None])
def test_invalid_node_id_raises(node_id):
    with pytest.raises(FrigateConfigError, match="node_id"):
        text(node_id=node_id)


# Determinism


def test_same_input_gives_byte_identical_output():
    existing = drawn({"gate": "0.1,0.2,0.3,0.2,0.3,0.9"})
    assert text(existing=existing) == text(existing=existing)


def test_zone_order_and_repeats_do_not_change_the_output():
    assert text(covered_zones=["gate", "driveway"]) == text(covered_zones=["driveway", "gate", "gate"])


def test_rendering_its_own_output_changes_nothing():
    first = text(existing=drawn({"gate": "0.1,0.2,0.3,0.2,0.3,0.9"}))
    assert text(existing=yaml.safe_load(first)) == first


def test_existing_is_not_modified():
    existing = drawn({"gate": "0.1,0.2,0.3,0.2,0.3,0.9", "old": "0,0,1,0,1,1"})
    before = yaml.safe_dump(existing)
    text(existing=existing)
    assert yaml.safe_dump(existing) == before


def test_output_loads_through_yaml_safe_load():
    assert isinstance(yaml.safe_load(text()), dict)


# The env file


def env_lines(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def test_write_env_creates_a_0600_file_with_all_three_values(tmp_path):
    path = tmp_path / "frigate.env"
    write_env(path, CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert env_lines(path) == {
        CAMERA_PASSWORD_ENV: CAMERA_PASSWORD,
        RELAY_PASSWORD_ENV: RELAY_PASSWORD,
        PROXY_SECRET_ENV: PROXY_SECRET,
    }


def test_every_reference_in_the_yaml_is_a_name_write_env_writes(tmp_path):
    # A missing name stops Frigate at load, or stops go2rtc's config from being created.
    path = tmp_path / "frigate.env"
    write_env(path, CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    assert set(re.findall(r"\{(FRIGATE_[A-Z_]+)\}", text())) == set(env_lines(path))


def test_secrets_are_never_on_disk_in_a_readable_file(tmp_path, monkeypatch):
    modes = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    previous = os.umask(0)
    try:
        write_env(tmp_path / "frigate.env", CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    finally:
        os.umask(previous)
    # The first sync is of the temporary file, with the secrets already written to it.
    assert modes[0] == 0o600
    assert stat.S_IMODE((tmp_path / "frigate.env").stat().st_mode) == 0o600


def test_write_env_tightens_and_replaces_an_open_file(tmp_path):
    path = tmp_path / "frigate.env"
    path.write_text("OLD=1\n")
    path.chmod(0o644)
    write_env(path, CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "OLD" not in path.read_text()


def test_write_env_leaves_no_temporary_file(tmp_path):
    write_env(tmp_path / "frigate.env", CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    assert os.listdir(tmp_path) == ["frigate.env"]


def test_write_env_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_env(a, CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    write_env(b, CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.parametrize("password", ["p@ss:w/rd?#", "100%", "a+b&c=d", "{x}", "a!*()[],;<>|^"])
def test_relay_password_is_written_literally(tmp_path, password):
    # go2rtc compares it with the client's login as it is, so encoding would change it.
    path = tmp_path / "frigate.env"
    write_env(path, CAMERA_PASSWORD, password, PROXY_SECRET)
    assert env_lines(path)[RELAY_PASSWORD_ENV] == password


@pytest.mark.parametrize(
    "password",
    # "$" could become a go2rtc ${VAR}. The rest do not pass the same way through every
    # env file reader, and a newline would add a line.
    ["with space", "a\nFRIGATE_EVIL=1", "${HOME}", 'q"', "q'", "back\\slash", "tick`", "é", "bad\ud800"],
)
def test_relay_password_an_env_file_cannot_carry_is_refused(tmp_path, password):
    path = tmp_path / "frigate.env"
    with pytest.raises(FrigateConfigError, match="relay password") as caught:
        write_env(path, CAMERA_PASSWORD, password, PROXY_SECRET)
    assert password not in str(caught.value)
    assert caught.value.__context__ is None
    assert not path.exists()


def test_camera_password_is_written_literally(tmp_path):
    path = tmp_path / "frigate.env"
    write_env(path, "Az09._~-", RELAY_PASSWORD, PROXY_SECRET)
    assert env_lines(path)[CAMERA_PASSWORD_ENV] == "Az09._~-"


def test_allowed_camera_password_characters_survive_frigates_quote_plus():
    # Frigate runs quote_plus over ffmpeg input passwords and go2rtc does not, so only a
    # value unchanged by it means the same password to both.
    allowed = "abcxyzABCXYZ0189._~-"
    assert quote_plus(allowed) == allowed


@pytest.mark.parametrize("password", ["p@ss", "a:b", "a/b", "a%40b", "a+b", "with space", "a\nb", "é", "bad\ud800"])
def test_camera_password_needing_encoding_is_refused(tmp_path, password):
    path = tmp_path / "frigate.env"
    with pytest.raises(FrigateConfigError, match="camera password") as caught:
        write_env(path, password, RELAY_PASSWORD, PROXY_SECRET)
    assert password not in str(caught.value)
    assert not path.exists()


@pytest.mark.parametrize("password", [None, 7, b"bytes"])
def test_a_password_that_is_not_text_is_refused(tmp_path, password):
    path = tmp_path / "frigate.env"
    with pytest.raises(FrigateConfigError, match="camera password"):
        write_env(path, password, RELAY_PASSWORD, PROXY_SECRET)
    with pytest.raises(FrigateConfigError, match="relay password"):
        write_env(path, CAMERA_PASSWORD, password, PROXY_SECRET)
    assert not path.exists()


def test_empty_relay_password_is_allowed_for_an_anonymous_broker(tmp_path):
    path = tmp_path / "frigate.env"
    write_env(path, CAMERA_PASSWORD, "", PROXY_SECRET)
    assert env_lines(path)[RELAY_PASSWORD_ENV] == ""


def test_empty_camera_password_is_refused(tmp_path):
    path = tmp_path / "frigate.env"
    with pytest.raises(FrigateConfigError, match="camera password"):
        write_env(path, "", RELAY_PASSWORD, PROXY_SECRET)
    assert not path.exists()


@pytest.mark.parametrize("secret", ["", "has space", "new\nline", 'quo"te', "dollar$", "back\\slash"])
def test_write_env_refuses_a_proxy_secret_it_cannot_carry_literally(tmp_path, secret):
    # Compared byte for byte with a header, so it cannot be encoded the way passwords are.
    path = tmp_path / "frigate.env"
    with pytest.raises(FrigateConfigError, match="proxy secret") as caught:
        write_env(path, CAMERA_PASSWORD, RELAY_PASSWORD, secret)
    assert not path.exists()
    if secret:
        assert secret not in str(caught.value)


def assert_no_secret_in(error: BaseException) -> None:
    for secret in (CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET):
        assert secret not in str(error)


def test_failed_replace_keeps_the_old_file_and_leaves_no_temporary(tmp_path, monkeypatch):
    path = tmp_path / "frigate.env"
    path.write_text("OLD=1\n")

    def refused(_source, _target):
        raise OSError(errno.EACCES, "refused")

    monkeypatch.setattr(os, "replace", refused)
    with pytest.raises(FrigateConfigError, match="refused") as caught:
        write_env(path, CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    assert_no_secret_in(caught.value)
    assert path.read_text() == "OLD=1\n"
    assert os.listdir(tmp_path) == ["frigate.env"]


def test_replace_failing_for_real_leaves_no_temporary(tmp_path):
    # os.replace cannot put a file over a directory.
    path = tmp_path / "frigate.env"
    path.mkdir()
    with pytest.raises(FrigateConfigError) as caught:
        write_env(path, CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    assert_no_secret_in(caught.value)
    assert os.listdir(tmp_path) == ["frigate.env"]
    assert path.is_dir()


def test_missing_directory_raises_with_no_secret(tmp_path):
    with pytest.raises(FrigateConfigError) as caught:
        write_env(tmp_path / "missing" / "frigate.env", CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    assert_no_secret_in(caught.value)
