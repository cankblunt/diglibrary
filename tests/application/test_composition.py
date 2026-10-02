"""Tests for the application composition root."""

from pathlib import Path

from diglibrary.application.composition import create_application


def test_create_application_initializes_configured_database(tmp_path: Path) -> None:
    """The composition root initializes all Phase 1 infrastructure."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[database]
path = "state/library.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = ["youtube"]
priority = ["youtube"]
""",
        encoding="utf-8",
    )

    application = create_application(config_path)

    assert application.database.path.is_file()
    assert application.config.logging.path.is_file()


def test_the_soulseek_provider_is_registered_and_follows_the_configuration(
    tmp_path: Path,
) -> None:
    """It exists whether or not it is wanted, so the window can say why it is quiet.

    Registering it always and letting configuration decide enablement is what
    lets a disabled provider be shown as disabled instead of missing.
    """
    from diglibrary.providers.models import ProviderStatus
    from diglibrary.providers.soulseek import SOULSEEK_PROVIDER_ID

    wanted = create_application(_config(tmp_path / "on", enabled=True))
    unwanted = create_application(_config(tmp_path / "off", enabled=False))

    assert wanted.providers.state(SOULSEEK_PROVIDER_ID).metadata.enabled
    assert not unwanted.providers.state(SOULSEEK_PROVIDER_ID).metadata.enabled
    assert unwanted.providers.state(SOULSEEK_PROVIDER_ID).status is ProviderStatus.DISABLED


def test_a_disabled_soulseek_provider_never_reaches_the_network(tmp_path: Path) -> None:
    """A provider that is not enabled must not contact anything on start-up."""
    from diglibrary.providers.models import ProviderStatus
    from diglibrary.providers.soulseek import SOULSEEK_PROVIDER_ID

    application = create_application(_config(tmp_path / "quiet", enabled=False))

    states = application.providers.initialize()

    assert [state.status for state in states] == [ProviderStatus.DISABLED]
    assert application.providers.state(SOULSEEK_PROVIDER_ID).health is None


def _config(directory: Path, enabled: bool) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.toml"
    listed = '["soulseek"]' if enabled else "[]"
    path.write_text(
        f"""[database]
path = "state/library.sqlite3"

[logging]
level = "INFO"
directory = "logs"
filename = "app.jsonl"

[providers]
enabled = {listed}
priority = {listed}

[connectors.slskd]
enabled = false
""",
        encoding="utf-8",
    )
    return path


def test_whether_audio_can_be_measured_is_one_answer_for_every_screen(
    tmp_path: Path, monkeypatch
) -> None:
    """ffmpeg is looked for once, and every screen reads that one answer.

    If each screen asked the object it happens to hold, Quality and Mixing
    could disagree about whether audio can be measured. The fact comes from
    one look at the disk, taken beside the objects built from it, so the
    window cannot be told one thing while the work is unable to happen.
    """
    from diglibrary.application import composition
    from diglibrary.application.composition import create_api

    looked: list[int] = []

    def once() -> str | None:
        looked.append(1)
        return None

    monkeypatch.setattr(composition, "find_ffmpeg", once)
    api = create_api(create_application(_config(tmp_path / "no-ffmpeg", enabled=False)))

    assert looked == [1], "ffmpeg is looked for more than once, so the answers can differ"
    assert api.state()["can_measure"] is False
    assert api.bench_state()["can_measure"] is False
    # And the gesture refuses rather than running and reporting nothing measured.
    refusal = api.measure_harmonics([1])
    assert refusal["ok"] is False and "ffmpeg" in str(refusal["error"])


def test_an_ffmpeg_that_is_here_is_reported_to_both_screens(tmp_path: Path, monkeypatch) -> None:
    """The other half of the claim: the fact follows the binary, not a constant."""
    from diglibrary.application import composition
    from diglibrary.application.composition import create_api

    monkeypatch.setattr(composition, "find_ffmpeg", lambda: "/opt/homebrew/bin/ffmpeg")
    api = create_api(create_application(_config(tmp_path / "with-ffmpeg", enabled=False)))

    assert api.state()["can_measure"] is True
    assert api.bench_state()["can_measure"] is True
