import json

import pytest

from pmcore.config import ConfigError, ConfigStore, default_config, validate_config


def test_defaults_are_valid_and_match_spec_example():
    d = default_config()
    assert validate_config(d) == d
    assert d["version"] == 1
    assert d["feeding"]["schedule"][0] == {"time": "08:00", "grams": 25, "days": [1, 2, 3, 4, 5, 6, 7]}
    assert d["privacy"] == {"stream_led": True, "rear_camera_streaming": True}


def test_missing_sections_filled_from_defaults():
    out = validate_config({"pet": {"name": "Luna"}})
    assert out["pet"] == {"name": "Luna", "species": "cat", "weight_kg": 4.5}
    assert out["audio"]["speaker_volume"] == 70


@pytest.mark.parametrize("patch,path", [
    ({"pet": {"species": "fish"}}, "pet.species"),
    ({"audio": {"speaker_volume": 101}}, "audio.speaker_volume"),
    ({"audio": {"speaker_volume": True}}, "audio.speaker_volume"),
    ({"feeding": {"schedule": [{"time": "24:00", "grams": 10}]}}, "feeding.schedule[0].time"),
    ({"feeding": {"schedule": [{"time": "08:00", "grams": 0}]}}, "feeding.schedule[0].grams"),
    ({"feeding": {"schedule": [{"time": "08:00", "grams": 10, "days": [0]}]}}, "feeding.schedule[0].days[0]"),
    ({"play": {"launch_power": 90}}, "play.launch_power"),
    ({"play": {"games": ["poker"]}}, "play.games[0]"),
    ({"play": {"quiet_hours": {"start": "7am"}}}, "play.quiet_hours.start"),
    ({"timezone": "Mars/Olympus"}, "timezone"),
    ({"version": 2}, "version"),
    ({"privacy": {"stream_led": "yes"}}, "privacy.stream_led"),
])
def test_invalid_values_rejected_with_path(patch, path):
    from pmcore.config import deep_merge

    with pytest.raises(ConfigError) as ei:
        validate_config(deep_merge(default_config(), patch))
    assert ei.value.path == path


def test_portion_and_daily_limits():
    cfg = default_config()
    cfg["feeding"]["portion_max_g"] = 30
    cfg["feeding"]["schedule"] = [{"time": "08:00", "grams": 40, "days": [1]}]
    with pytest.raises(ConfigError, match="portion_max_g"):
        validate_config(cfg)
    cfg["feeding"]["portion_max_g"] = 60
    cfg["feeding"]["daily_max_g"] = 50
    cfg["feeding"]["schedule"] = [
        {"time": "08:00", "grams": 30, "days": [1, 2]},
        {"time": "18:00", "grams": 30, "days": [2]},
    ]
    with pytest.raises(ConfigError, match="weekday 2"):
        validate_config(cfg)


def test_days_normalised_and_unknown_keys_dropped():
    dropped = []
    cfg = default_config()
    cfg["feeding"]["schedule"][0]["days"] = [7, 1, 1, 3]
    cfg["bogus"] = 1
    cfg["pet"]["color"] = "black"
    out = validate_config(cfg, dropped)
    assert out["feeding"]["schedule"][0]["days"] == [1, 3, 7]
    assert "bogus" not in out and "color" not in out["pet"]
    assert set(dropped) == {"bogus", "pet.color"}


def test_store_load_defaults_persist_and_apply(tmp_path):
    p = tmp_path / "var/lib/petmate/config.json"
    st = ConfigStore(p)
    st.load()
    assert json.loads(p.read_text()) == default_config()

    seen = []
    st.on_change(lambda new, old: seen.append((new["audio"]["speaker_volume"], old["audio"]["speaker_volume"])))
    new, changed = st.apply({"audio": {"speaker_volume": 30}})
    assert changed and new["audio"]["speaker_volume"] == 30
    assert seen == [(30, 70)]
    assert json.loads(p.read_text())["audio"]["speaker_volume"] == 30
    # no-op apply does not notify
    _, changed = st.apply({"audio": {"speaker_volume": 30}})
    assert not changed and len(seen) == 1
    # invalid apply leaves state untouched
    with pytest.raises(ConfigError):
        st.apply({"audio": {"speaker_volume": 300}})
    assert st.get("audio.speaker_volume") == 30
    assert not list(p.parent.glob("*.tmp"))  # atomic write leaves no temp files

    st2 = ConfigStore(p)
    assert st2.load()["audio"]["speaker_volume"] == 30


def test_store_corrupt_file_is_backed_up_and_reset(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    st = ConfigStore(p)
    assert st.load() == default_config()
    assert (tmp_path / "config.json.bad").exists()
