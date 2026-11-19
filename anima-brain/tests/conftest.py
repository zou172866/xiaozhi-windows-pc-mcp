import pytest

from companion_engine import CharacterSpec, Engine


@pytest.fixture(autouse=True)
def private_test_provider_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANION_SECRET_STORE", "file")
    monkeypatch.setenv("COMPANION_SETTINGS_FILE", str(tmp_path / "providers.json"))


@pytest.fixture
def engine(tmp_path):
    with Engine(tmp_path / "brain.sqlite3", owner="alice") as value:
        value.create_character(
            CharacterSpec(
                id="nova",
                name="Nova",
                persona="Warm, concise, curious.",
                memory_policy="review",
                relationship_enabled=False,
            )
        )
        yield value
