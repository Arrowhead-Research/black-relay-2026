"""The rendered Frigate config, pinned to Frigate 0.18.2 and its go2rtc 1.9.14."""

import os
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

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
        "mediamtx_address": "10.0.0.2:8554",
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
    assert cam["ffmpeg"]["input_args"] == "preset-rtsp-restream"
    for item in cam["ffmpeg"]["inputs"]:
        assert item["input_args"] == "-rtsp_transport tcp"
        assert item["path"].startswith("rtsp://admin:")
        assert "@10.77.0.10:554/" in item["path"]


def test_detect_uses_sub_stream_and_record_uses_main():
    roles = inputs_by_role(camera())
    assert set(roles) == {"detect", "record"}
    assert roles["detect"]["path"].endswith("/h264Preview_01_sub")
    assert roles["record"]["path"].endswith("/h264Preview_01_main")


def test_detect_is_explicitly_enabled_because_frigate_defaults_it_off():
    assert camera()["detect"] == {"enabled": True, "width": 640, "height": 480, "fps": 5}


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
    # FrigateConfig forbids unknown keys, so a stray section stops Frigate from starting.
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
    out = text()
    for secret in (CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET):
        assert secret not in out


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
    # create_config.py leaves go2rtc.publish alone, and go2rtc's own loader expands ${VAR}.
    for destination in cfg["go2rtc"]["publish"]["cam-8da040"]:
        assert "${" + RELAY_PASSWORD_ENV + "}" in destination
        assert "{" + RELAY_PASSWORD_ENV + "}" not in destination.replace("${", "")


def test_stream_sources_survive_frigates_str_format_pass():
    # A stray brace here makes create_config.py exit, and then go2rtc never starts.
    env = {CAMERA_PASSWORD_ENV: "x%21y", RELAY_PASSWORD_ENV: "r", PROXY_SECRET_ENV: "p"}
    for source in config()["go2rtc"]["streams"]["cam-8da040"]:
        assert "x%21y@10.77.0.10" in source.format(**env)


# Restream. Unverified on hardware: see the module docstring.


def test_restream_source_is_the_sub_stream_under_the_camera_name():
    assert config()["go2rtc"]["streams"] == {
        "cam-8da040": [
            "rtsp://admin:{"
            + CAMERA_PASSWORD_ENV
            + "}@10.77.0.10:554/h264Preview_01_sub"
        ]
    }


def test_restream_publishes_over_rtmp_because_go2rtc_has_no_rtsp_consumer():
    assert config()["go2rtc"]["publish"] == {
        "cam-8da040": [
            "rtmp://10.0.0.2:1935/cam-8da040?user=kit1&pass=${"
            + RELAY_PASSWORD_ENV
            + "}"
        ]
    }


@pytest.mark.parametrize(
    ("address", "host"),
    [
        ("10.0.0.2:8554", "10.0.0.2:1935"),
        ("10.0.0.2", "10.0.0.2:1935"),
        ("relay.local:8554", "relay.local:1935"),
        ("[fd00::2]:8554", "[fd00::2]:1935"),
        ("[fd00::2]", "[fd00::2]:1935"),
    ],
)
def test_publish_host_comes_from_the_mediamtx_address(address, host):
    url = config(mediamtx_address=address)["go2rtc"]["publish"]["cam-8da040"][0]
    assert urlsplit(url).netloc == host


def test_relay_user_is_encoded_so_it_cannot_change_the_url():
    url = config(relay_user="a&pass=b c/{d}$e")["go2rtc"]["publish"]["cam-8da040"][0]
    query = parse_qs(urlsplit(url).query)
    assert query["user"] == ["a&pass=b c/{d}$e"]
    assert list(query) == ["user", "pass"]


def test_anonymous_broker_publishes_with_no_credentials():
    # Provisioning allows an anonymous broker. A relay that refuses it costs the live view
    # only, and raising here would cost local detection and recording as well.
    url = config(relay_user="")["go2rtc"]["publish"]["cam-8da040"][0]
    assert url == "rtmp://10.0.0.2:1935/cam-8da040"


def test_go2rtc_opens_no_new_port():
    # The firewall keeps 1984, 8554 and 8555 closed. Publishing is outbound only, so the
    # config has no business naming listeners, and must not add an rtmp one.
    assert set(config()["go2rtc"]) == {"streams", "publish"}


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


# Other inputs


@pytest.mark.parametrize("node_id", ["", "a b", "a.b", "a/b", "a\n", "a: b", "{x}", None])
def test_invalid_node_id_raises(node_id):
    with pytest.raises(FrigateConfigError, match="node_id"):
        text(node_id=node_id)


@pytest.mark.parametrize(
    "address", ["", "rtsp://10.0.0.2:8554", "10.0.0.2:8554/x", "a b:8554", "fd00::2", "10.0.0.2:", "h\n", None]
)
def test_invalid_mediamtx_address_raises(address):
    with pytest.raises(FrigateConfigError, match="mediamtx_address"):
        text(mediamtx_address=address)


def test_relay_user_must_be_text():
    with pytest.raises(FrigateConfigError, match="relay_user"):
        text(relay_user=None)


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


def test_every_name_frigate_substitutes_is_prefixed_frigate():
    # Frigate and create_config.py only pick up FRIGATE_* variables.
    for name in (CAMERA_PASSWORD_ENV, RELAY_PASSWORD_ENV, PROXY_SECRET_ENV):
        assert name.startswith("FRIGATE_")


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


@pytest.mark.parametrize("password", ["p@ss:w/rd?#", "with space", "100%", "a+b&c=d", "{x}${y}", "é", 'q"\'\\'])
def test_url_passwords_are_percent_encoded_and_decode_back(tmp_path, password):
    # Both values only ever land inside a URL, unquoted, by plain substitution.
    path = tmp_path / "frigate.env"
    write_env(path, password, password, PROXY_SECRET)
    env = env_lines(path)
    for name in (CAMERA_PASSWORD_ENV, RELAY_PASSWORD_ENV):
        assert re.fullmatch(r"[A-Za-z0-9._~%-]+", env[name])
        assert unquote(env[name]) == password


def test_a_newline_in_a_password_cannot_add_an_env_line(tmp_path):
    path = tmp_path / "frigate.env"
    write_env(path, "a\nFRIGATE_EVIL=1", "b", PROXY_SECRET)
    assert "FRIGATE_EVIL" not in env_lines(path)


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


def test_write_env_failure_names_no_secret_and_keeps_the_old_file(tmp_path):
    missing = tmp_path / "missing" / "frigate.env"
    with pytest.raises(FrigateConfigError) as caught:
        write_env(missing, CAMERA_PASSWORD, RELAY_PASSWORD, PROXY_SECRET)
    message = str(caught.value)
    assert CAMERA_PASSWORD not in message
    assert RELAY_PASSWORD not in message
    assert PROXY_SECRET not in message
