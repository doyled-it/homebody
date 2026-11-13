"""Tests for the homebody CLI (src/homebody/cli.py).

The CLI's Matter commands are driven against the in-memory FakeController by
injecting a connect factory, so no controller process or network is needed. The
tests pin the command wiring (the right attribute writes reach the controller)
and the printed output a person reads during the live proof.
"""

from __future__ import annotations

from fakes import FakeConnectionContext, FakeController
from homebody import cli, matter


def _connect_to(controller: FakeController):
    def factory(url: str) -> FakeConnectionContext:
        return FakeConnectionContext(controller)

    return factory


def test_no_command_prints_version(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert matter.__name__  # module importable
    assert "homebody" in out
    assert "0.1.0" in out


def test_status_prints_mode_temperature_and_setpoint(capsys):
    controller = FakeController()
    controller.add_thermostat(
        node_id=1,
        endpoint=1,
        system_mode=int(matter.SystemMode.COOL),
        local_c100=2345,
        cooling_c100=2222,
    )

    rc = cli.main(["matter", "status"], connect=_connect_to(controller))

    out = capsys.readouterr().out
    assert rc == 0
    assert "cool" in out
    assert "74.2" in out  # local temperature in Fahrenheit
    assert "72.0" in out  # cooling setpoint in Fahrenheit


def test_status_without_a_thermostat_reports_and_fails(capsys):
    controller = FakeController()  # no nodes commissioned

    rc = cli.main(["matter", "status"], connect=_connect_to(controller))

    captured = capsys.readouterr()
    assert rc == 1
    assert "no thermostat" in (captured.out + captured.err).lower()


def test_commission_prints_the_new_node_id(capsys):
    controller = FakeController()

    rc = cli.main(
        ["matter", "commission", "1234-567-8901"], connect=_connect_to(controller)
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "1" in out
    assert controller.calls[0][0] == "commission_with_code"
    assert controller.calls[0][1]["network_only"] is True


def test_set_setpoint_writes_the_pinned_matter_value(capsys):
    controller = FakeController()
    controller.add_thermostat(node_id=1, endpoint=1, cooling_c100=2222)

    rc = cli.main(
        ["matter", "set-setpoint", "82"], connect=_connect_to(controller)
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "82" in out
    path = f"1/{matter.THERMOSTAT_CLUSTER_ID}/{matter.ATTR_OCCUPIED_COOLING_SETPOINT}"
    assert controller.nodes[1]["attributes"][path] == 2778


def test_set_mode_writes_the_mode(capsys):
    controller = FakeController()
    controller.add_thermostat(node_id=1, endpoint=1)

    rc = cli.main(["matter", "set-mode", "cool"], connect=_connect_to(controller))

    out = capsys.readouterr().out
    assert rc == 0
    assert "cool" in out
    path = f"1/{matter.THERMOSTAT_CLUSTER_ID}/{matter.ATTR_SYSTEM_MODE}"
    assert controller.nodes[1]["attributes"][path] == int(matter.SystemMode.COOL)


def test_matter_url_defaults_to_loopback_controller():
    # The default URL is the loopback controller the snaphouse unit binds to.
    assert cli._matter_url() == matter.DEFAULT_MATTER_URL


def test_matter_url_honours_environment_override(monkeypatch):
    monkeypatch.setenv("HOMEBODY_MATTER_URL", "ws://127.0.0.1:9999/ws")
    assert cli._matter_url() == "ws://127.0.0.1:9999/ws"


def test_run_loads_the_config_and_starts_the_daemon(tmp_path, monkeypatch):
    path = tmp_path / "local.toml"
    path.write_text('[daemon]\nstate_dir = "/tmp/hb-cli-test"\n')
    started = []
    monkeypatch.delenv("HOMEBODY_MATTER_URL", raising=False)

    rc = cli.main(["run", "--config", str(path)], run_daemon=started.append)

    assert rc == 0
    assert len(started) == 1
    assert str(started[0].state_dir) == "/tmp/hb-cli-test"


def test_run_reports_a_missing_config(tmp_path, capsys):
    rc = cli.main(
        ["run", "--config", str(tmp_path / "missing.toml")],
        run_daemon=lambda config: None,
    )
    assert rc == 1
    assert "cannot read config" in capsys.readouterr().err
