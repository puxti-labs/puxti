"""Unit tests for the telemetry module."""
from unittest.mock import MagicMock, patch


# ── is_enabled ────────────────────────────────────────────────────────────────

def test_is_enabled_false_by_default():
    with patch("puxti.telemetry._load_config", return_value={}):
        from puxti.telemetry import is_enabled
        assert not is_enabled()


def test_is_enabled_false_when_explicitly_set():
    with patch("puxti.telemetry._load_config", return_value={"telemetry": {"enabled": False}}):
        from puxti.telemetry import is_enabled
        assert not is_enabled()


def test_is_enabled_true_when_opted_in():
    with patch("puxti.telemetry._load_config", return_value={"telemetry": {"enabled": True}}):
        from puxti.telemetry import is_enabled
        assert is_enabled()


# ── set_enabled ───────────────────────────────────────────────────────────────

def test_set_enabled_true_persists(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        from puxti.telemetry import is_enabled, set_enabled
        set_enabled(True)
        assert is_enabled()


def test_set_enabled_false_persists(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        from puxti.telemetry import is_enabled, set_enabled
        set_enabled(True)
        set_enabled(False)
        assert not is_enabled()


def test_set_enabled_true_creates_install_id(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        from puxti.telemetry import get_install_id, set_enabled
        set_enabled(True)
        install_id = get_install_id()
        assert len(install_id) == 36  # UUID4 format


# ── get_install_id ────────────────────────────────────────────────────────────

def test_get_install_id_creates_uuid(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        from puxti.telemetry import get_install_id
        install_id = get_install_id()
        assert len(install_id) == 36


def test_get_install_id_stable_across_calls(tmp_path):
    config_file = tmp_path / "config.toml"
    with patch("puxti.telemetry._CONFIG_PATH", config_file):
        from puxti.telemetry import get_install_id
        assert get_install_id() == get_install_id()


def test_get_install_id_returns_existing_when_present():
    existing_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with patch("puxti.telemetry._load_config", return_value={"telemetry": {"install_id": existing_id}}):
        from puxti.telemetry import get_install_id
        assert get_install_id() == existing_id


# ── record_event ──────────────────────────────────────────────────────────────

def test_record_event_returns_none_when_disabled():
    with patch("puxti.telemetry.is_enabled", return_value=False):
        from puxti.telemetry import record_event
        assert record_event(command="scan", duration_ms=1000, exit_status=0) is None


def test_record_event_returns_thread_when_enabled():
    mock_posthog = MagicMock()
    with (
        patch("puxti.telemetry.is_enabled", return_value=True),
        patch("puxti.telemetry.get_install_id", return_value="test-uuid"),
        patch.dict("sys.modules", {"posthog": mock_posthog}),
    ):
        from puxti.telemetry import record_event
        thread = record_event(command="scan", duration_ms=1000, exit_status=0)
        assert thread is not None
        thread.join(timeout=5)

    mock_posthog.capture.assert_called_once()
    call_kwargs = mock_posthog.capture.call_args
    assert call_kwargs.kwargs["distinct_id"] == "test-uuid"
    assert call_kwargs.kwargs["event"] == "command_run"
    props = call_kwargs.kwargs["properties"]
    assert props["command"] == "scan"
    assert props["duration_ms"] == 1000
    assert props["exit_status"] == 0
    assert "version" in props
    assert "python_version" in props
    assert "platform" in props


def test_record_event_calls_flush():
    mock_posthog = MagicMock()
    with (
        patch("puxti.telemetry.is_enabled", return_value=True),
        patch("puxti.telemetry.get_install_id", return_value="test-uuid"),
        patch.dict("sys.modules", {"posthog": mock_posthog}),
    ):
        from puxti.telemetry import record_event
        thread = record_event(command="capture", duration_ms=500, exit_status=0)
        thread.join(timeout=5)

    mock_posthog.flush.assert_called_once()


def test_record_event_silently_drops_on_posthog_error():
    mock_posthog = MagicMock()
    mock_posthog.capture.side_effect = RuntimeError("network failure")
    with (
        patch("puxti.telemetry.is_enabled", return_value=True),
        patch("puxti.telemetry.get_install_id", return_value="test-uuid"),
        patch.dict("sys.modules", {"posthog": mock_posthog}),
    ):
        from puxti.telemetry import record_event
        thread = record_event(command="scan", duration_ms=100, exit_status=1)
        assert thread is not None
        thread.join(timeout=5)
    # No exception raised — error swallowed silently
