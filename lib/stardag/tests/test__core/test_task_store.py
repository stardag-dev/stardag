"""The committed task store: the normalized format and its lossless round
trip, the writers, the eager reader, and references served from it.

No test reaches a registry other than an ``InMemoryRegistry``: the root
conftest keeps the configured registry off and the project config empty,
and every store is a per-test directory.
"""

from __future__ import annotations

import json
import random
import typing
import warnings
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import pytest

import stardag as sd
from stardag._core.task_id import canonical_body_json
from stardag._core.task_store import TaskStore, TaskStoreNotConfiguredError
from stardag._core.task_store_format import (
    POINTER_KEY,
    Envelope,
    denormalize,
    hash_of,
)
from stardag._core.task_store_write import write_from_registry
from stardag.config.project import ProjectConfig, project_config_override
from stardag.testing import InMemoryRegistry
from stardag.testing import test_harness as harness

sd.auto_namespace(__name__)


class Dataset(sd.Task[list[int]]):
    size: int
    note: Annotated[str, sd.StardagField(significant=False)] = ""

    def run(self):
        self._save(list(range(self.size)))


class TrainedModel(sd.Task[str]):
    data: sd.TaskLoads[list[int]]
    learning_rate: float = 0.1

    def run(self):
        self._save("model")


class Ensemble(sd.Task[str]):
    members: list[sd.TaskLoads[str]] = []
    by_name: dict[str, sd.TaskLoads[list[int]]] = {}
    pair: tuple[sd.TaskLoads[list[int]], ...] = ()

    def run(self):
        self._save("ensemble")


with warnings.catch_warnings():
    # Pydantic warns that the field shadows the classmethod: the point.
    warnings.simplefilter("ignore", UserWarning)

    class Shadowing(sd.Task[int]):
        from_store: int = 0  # pyright: ignore[reportIncompatibleMethodOverride]

        def run(self):
            self._save(self.from_store)


def _store_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.json"))


def _read_back(root: Path, envelope_path: Path) -> dict[str, typing.Any]:
    store = TaskStore(root)
    index = store.index()
    return denormalize(
        store.read(envelope_path),
        lambda tid, h, _: store.read(index[(tid, h)]),
    )


# -----------------------------------------------------------------------------
# The round trip, as a property
# -----------------------------------------------------------------------------


def _random_dag(rng: random.Random) -> sd.BaseTask:
    """A random tree of tasks with shared upstreams and one completion
    embedded under different non-significant fields."""
    datasets = [
        Dataset(size=rng.randint(1, 4), note=rng.choice(["", "a", "b"]))
        for _ in range(rng.randint(1, 4))
    ]
    models = [
        TrainedModel(data=rng.choice(datasets), learning_rate=rng.choice([0.1, 0.2]))
        for _ in range(rng.randint(1, 4))
    ]
    return Ensemble(
        members=[rng.choice(models) for _ in range(rng.randint(0, 4))],
        by_name={str(i): rng.choice(datasets) for i in range(rng.randint(0, 3))},
        pair=tuple(rng.choice(datasets) for _ in range(rng.randint(0, 2))),
    )


@pytest.mark.parametrize("seed", range(40))
def test_normalize_then_denormalize_is_byte_identical(seed, tmp_path):
    task = _random_dag(random.Random(seed))
    (stored,) = sd.write_to_store(task, store=tmp_path)
    body = _read_back(tmp_path, stored.path)
    assert canonical_body_json(body) == task._instance_body_json
    assert UUID(hash_of(body)) == task.instance_hash == stored.instance_hash
    assert sd.task_from_store(Ensemble, task.id, store=tmp_path) == task


def test_two_embeddings_of_one_task_id_are_two_files(tmp_path):
    plain, annotated = Dataset(size=3), Dataset(size=3, note="kept")
    assert plain.id == annotated.id
    task = Ensemble(pair=(plain, annotated))
    sd.write_to_store(task, store=tmp_path)
    files = list((tmp_path / f"{__name__}.Dataset" / str(plain.id)).iterdir())
    assert sorted(f.stem for f in files) == sorted(
        [str(plain.instance_hash), str(annotated.instance_hash)]
    )
    loaded = Ensemble.from_store(task.id, store=tmp_path)
    assert [d.note for d in loaded.pair] == ["", "kept"]  # type: ignore[attr-defined]


def test_a_shared_upstream_is_stored_once(tmp_path):
    shared = Dataset(size=2)
    task = Ensemble(
        members=[
            TrainedModel(data=shared),
            TrainedModel(data=shared, learning_rate=0.3),
        ],
        by_name={"x": shared},
    )
    sd.write_to_store(task, store=tmp_path)
    # Ensemble, two models, one dataset.
    assert len(_store_files(tmp_path)) == 4
    root = json.loads(
        (tmp_path / f"{__name__}.Ensemble" / str(task.id))
        .glob("*.json")
        .__next__()
        .read_text()
    )
    assert root["body"]["by_name"]["x"] == {
        POINTER_KEY: {
            "instance_hash": str(shared.instance_hash),
            "task": f"{__name__}.Dataset",
            "task_id": str(shared.id),
        }
    }


# -----------------------------------------------------------------------------
# The files
# -----------------------------------------------------------------------------


def test_the_file_layout_and_envelope(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    (stored,) = sd.write_to_store(model, store=tmp_path)
    assert stored.path == (
        tmp_path
        / f"{__name__}.TrainedModel"
        / str(model.id)
        / f"{model.instance_hash}.json"
    )
    data = json.loads(stored.path.read_text())
    assert list(data) == [
        "stardag_task_store",
        "task_id",
        "instance_hash",
        "namespace",
        "name",
        "module",
        "sources",
        "body",
    ]
    assert data["module"] == __name__
    assert data["namespace"] == __name__
    assert data["name"] == "TrainedModel"
    (source,) = data["sources"]
    assert source["kind"] == "local"
    assert {"written_at", "stardag_version"} <= set(source)
    assert list(data["body"]) == sorted(data["body"])
    assert stored.path.read_text().endswith("}\n")


def test_rewriting_the_same_content_is_a_no_op(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    (first,) = sd.write_to_store(model, store=tmp_path)
    before = {p: p.read_bytes() for p in _store_files(tmp_path)}
    (second,) = sd.write_to_store(model, store=tmp_path)
    assert len(first.written) == 2
    assert second.written == ()
    assert {p: p.read_bytes() for p in _store_files(tmp_path)} == before


def test_a_different_body_at_an_existing_address_is_refused(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    (stored,) = sd.write_to_store(model, store=tmp_path)
    data = json.loads(stored.path.read_text())
    data["body"]["learning_rate"] = 0.5
    stored.path.write_text(json.dumps(data))
    with pytest.raises(sd.TaskStoreError, match="different body"):
        sd.write_to_store(model, store=tmp_path)


def test_a_hand_edited_file_is_refused_on_read(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    sd.write_to_store(model, store=tmp_path)
    dataset_file = next((tmp_path / f"{__name__}.Dataset").rglob("*.json"))
    data = json.loads(dataset_file.read_text())
    data["body"]["note"] = "edited"
    dataset_file.write_text(json.dumps(data))
    with pytest.raises(sd.TaskStoreError, match="was edited"):
        TrainedModel.from_store(model.id, store=tmp_path)


def test_a_missing_nested_file_names_the_pointer(tmp_path):
    dataset = Dataset(size=3)
    model = TrainedModel(data=dataset)
    sd.write_to_store(model, store=tmp_path)
    next((tmp_path / f"{__name__}.Dataset").rglob("*.json")).unlink()
    with pytest.raises(sd.TaskStoreError) as e:
        TrainedModel.from_store(model.id, store=tmp_path)
    assert str(dataset.id) in str(e.value)
    assert "--write" in str(e.value)


def test_the_reserved_pointer_key_is_refused_in_a_body(tmp_path):
    class WithDict(sd.Task[int]):
        payload: dict[str, int] = {}

        def run(self):
            self._save(0)

    with pytest.raises(sd.TaskStoreError, match="reserved key"):
        sd.write_to_store(WithDict(payload={POINTER_KEY: 1}), store=tmp_path)


def test_files_outside_the_layout_are_ignored(tmp_path):
    (tmp_path / "README.md").write_text("# pinned tasks\n")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "x.json").write_text("{}")
    model = TrainedModel(data=Dataset(size=3))
    sd.write_to_store(model, store=tmp_path)
    assert TrainedModel.from_store(model.id, store=tmp_path) == model


def test_an_envelope_of_another_format_version_is_refused():
    with pytest.raises(sd.TaskStoreError, match="format 2"):
        Envelope.from_json(json.dumps({"stardag_task_store": 2}), "x.json")


# -----------------------------------------------------------------------------
# The reader
# -----------------------------------------------------------------------------


def test_from_store_returns_the_real_task(tmp_path):
    model = TrainedModel(data=Dataset(size=3), learning_rate=0.2)
    sd.write_to_store(model, store=tmp_path)
    loaded = TrainedModel.from_store(str(model.id), store=tmp_path)
    assert type(loaded) is TrainedModel
    assert loaded.learning_rate == 0.2
    derived = loaded.model_copy(update={"learning_rate": 0.3})
    assert derived.id == TrainedModel(data=Dataset(size=3), learning_rate=0.3).id


def test_a_base_class_reads_the_concrete_subclass(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    sd.write_to_store(model, store=tmp_path)
    assert type(sd.Task.from_store(model.id, store=tmp_path)) is TrainedModel


def test_another_class_is_refused(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    sd.write_to_store(model, store=tmp_path)
    with pytest.raises(sd.TaskRehydrationError, match="not a"):
        Dataset.from_store(model.id, store=tmp_path)


def test_an_unknown_task_says_how_to_write_it(tmp_path):
    missing = uuid4()
    with pytest.raises(sd.TaskStoreError) as e:
        Dataset.from_store(missing, store=tmp_path)
    assert f"stardag tasks ref {missing} --write" in str(e.value)


def test_several_bodies_need_a_pick(tmp_path):
    plain, annotated = Dataset(size=3), Dataset(size=3, note="kept")
    sd.write_to_store([plain, annotated], store=tmp_path)
    with pytest.raises(sd.TaskStoreError) as e:
        Dataset.from_store(plain.id, store=tmp_path)
    assert str(plain.instance_hash) in str(e.value)
    assert str(annotated.instance_hash) in str(e.value)
    picked = Dataset.from_store(
        plain.id, instance_hash=annotated.instance_hash, store=tmp_path
    )
    assert picked.note == "kept"


def test_no_store_configured_is_a_clear_error():
    with pytest.raises(TaskStoreNotConfiguredError, match=r"\[tool.stardag\]"):
        Dataset.from_store(uuid4())
    with pytest.raises(TaskStoreNotConfiguredError):
        sd.write_to_store(Dataset(size=1))


def test_the_configured_store_is_used(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    with project_config_override(ProjectConfig(task_store=tmp_path)):
        (stored,) = sd.write_to_store(model)
        assert TrainedModel.from_store(model.id) == model
    assert stored.path.is_relative_to(tmp_path)


def test_the_function_form_reads_a_class_shadowing_the_classmethod(tmp_path):
    task = Shadowing(from_store=3)
    (stored,) = sd.write_to_store(task, store=tmp_path)
    assert sd.task_from_store(Shadowing, task.id, store=tmp_path) == task
    assert stored.call.startswith("sd.task_from_store(Shadowing, ")


def test_the_printed_lines(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    with project_config_override(ProjectConfig(task_store=tmp_path)):
        (stored,) = sd.write_to_store(model, store=tmp_path)
    assert str(stored).splitlines() == [
        f"from {__name__} import TrainedModel",
        f'TrainedModel.from_store("{model.id}", instance_hash="{model.instance_hash}")',
    ]


def test_an_explicit_store_other_than_the_configured_one_is_printed(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    model = TrainedModel(data=Dataset(size=3))
    (stored,) = sd.write_to_store(model, store="reviewed")
    assert stored.call.endswith(', store="reviewed")')
    assert TrainedModel.from_store(model.id, store="reviewed") == model


def test_concurrent_writers_keep_every_instance_id(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    model = TrainedModel(data=Dataset(size=3))
    registry = InMemoryRegistry()
    ids = [uuid4() for _ in range(8)]
    for iid in ids:
        registry.add_instance(
            body=model.instance_body(), task_id=model.id, instance_id=iid
        )
    with ThreadPoolExecutor(8) as pool:
        list(
            pool.map(
                lambda iid: write_from_registry(
                    model.id, instance_id=iid, registry=registry, store=tmp_path
                ),
                ids,
            )
        )
    root = json.loads(
        next((tmp_path / f"{__name__}.TrainedModel").rglob("*.json")).read_text()
    )
    assert sorted(s["instance_id"] for s in root["sources"]) == sorted(map(str, ids))


def test_an_unhydrated_ref_is_not_written(tmp_path):
    model = TrainedModel(data=Dataset.ref(uuid4()))
    with pytest.raises(sd.TaskRefNotHydratedError):
        sd.write_to_store(model, store=tmp_path)


# -----------------------------------------------------------------------------
# From the registry
# -----------------------------------------------------------------------------


def _seeded(task: sd.BaseTask) -> tuple[InMemoryRegistry, UUID]:
    registry = InMemoryRegistry()
    instance_id = uuid4()
    registry.add_instance(
        body=task.instance_body(), task_id=task.id, instance_id=instance_id
    )
    return registry, instance_id


def test_write_from_registry_records_the_instance(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    registry, instance_id = _seeded(model)
    with project_config_override(ProjectConfig(task_store=tmp_path)):
        stored = write_from_registry(model.id, registry=registry)
    assert stored.instance_id == instance_id
    assert stored.call == (
        f'TrainedModel.from_store("{model.id}", instance_id="{instance_id}", '
        f'instance_hash="{model.instance_hash}")'
    )
    root = json.loads(stored.path.read_text())
    (source,) = root["sources"]
    assert source["kind"] == "registry"
    assert source["instance_id"] == str(instance_id)
    nested = json.loads(
        next((tmp_path / f"{__name__}.Dataset").rglob("*.json")).read_text()
    )
    assert "instance_id" not in nested["sources"][0]
    loaded = TrainedModel.from_store(model.id, instance_id=instance_id, store=tmp_path)
    assert loaded == model


def test_a_second_instance_of_the_same_body_adds_a_source(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    registry, first = _seeded(model)
    second = uuid4()
    registry.add_instance(
        body=model.instance_body(), task_id=model.id, instance_id=second
    )
    write_from_registry(model.id, instance_id=first, registry=registry, store=tmp_path)
    write_from_registry(model.id, instance_id=second, registry=registry, store=tmp_path)
    root = json.loads(
        next((tmp_path / f"{__name__}.TrainedModel").rglob("*.json")).read_text()
    )
    assert [s["instance_id"] for s in root["sources"]] == [str(first), str(second)]
    for iid in (first, second):
        assert (
            TrainedModel.from_store(model.id, instance_id=iid, store=tmp_path) == model
        )


def test_a_body_that_does_not_rehydrate_is_not_written(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    registry = InMemoryRegistry()
    body = model.instance_body()
    body["learning_rate"] = 0.9  # recomputes to another task id
    registry.add_instance(body=body, task_id=model.id, instance_id=uuid4())
    with pytest.raises(sd.TaskStoreError, match="does not rehydrate"):
        write_from_registry(model.id, registry=registry, store=tmp_path)
    assert _store_files(tmp_path) == []


# -----------------------------------------------------------------------------
# References served from the store
# -----------------------------------------------------------------------------


@pytest.fixture
def configured_store(tmp_path) -> typing.Iterator[Path]:
    with project_config_override(ProjectConfig(task_store=tmp_path)):
        yield tmp_path


def test_an_unpinned_ref_hydrates_from_the_only_stored_body(configured_store):
    model = TrainedModel(data=Dataset(size=3))
    sd.write_to_store(model)
    # No registry is configured: the store alone serves it.
    assert TrainedModel.ref(model.id).hydrate() == model


def test_a_pinned_ref_hydrates_from_the_body_recorded_under_its_instance(
    configured_store,
):
    model = TrainedModel(data=Dataset(size=3))
    registry, instance_id = _seeded(model)
    write_from_registry(model.id, registry=registry)
    ref = TrainedModel.ref(model.id, instance_id=instance_id)
    assert ref.hydrate() == model
    with harness():
        built = Ensemble(members=[ref])
        assert sd.resolve_refs(built).members[0] == model


def test_a_pinned_ref_not_in_the_store_falls_back_to_the_registry(configured_store):
    model = TrainedModel(data=Dataset(size=3))
    sd.write_to_store(model)  # local: recorded under no instance id
    registry, instance_id = _seeded(model)
    ref = TrainedModel.ref(model.id, instance_id=instance_id)
    assert ref.hydrate(registry) == model


def test_an_unpinned_ref_with_several_stored_bodies_reads_the_registry(
    configured_store,
):
    plain, annotated = Dataset(size=3), Dataset(size=3, note="kept")
    sd.write_to_store([plain, annotated])
    with pytest.raises(sd.TaskRefHydrationError, match="no registry"):
        Dataset.ref(plain.id).hydrate()


def test_a_corrupt_stored_body_fails_the_ref_loudly(configured_store):
    model = TrainedModel(data=Dataset(size=3))
    (stored,) = sd.write_to_store(model)
    stored.path.write_text("{not json")
    with pytest.raises(sd.TaskRefHydrationError, match="not valid JSON"):
        TrainedModel.ref(model.id).hydrate()


def test_provenance_is_the_clients_own_scope_not_the_ambient_profile():
    from types import SimpleNamespace

    from stardag._core.task_store_write import registry_origin

    client = SimpleNamespace(
        api_url="https://registry.example.com",
        environment_id="env-id",
        _cache_workspace_id="ws-id",
    )
    # The hermetic config has no registry: nothing to name it by.
    assert registry_origin(client) == {
        "kind": "registry",
        "registry_url": "https://registry.example.com",
        "workspace": "ws-id",
        "environment": "env-id",
    }


# -----------------------------------------------------------------------------
# Verified vs metadata, paths, modes, labels
# -----------------------------------------------------------------------------


def test_swapped_sources_are_caught_by_the_printed_hash(tmp_path):
    """``sources`` is unverified metadata: swapping the instance ids of two
    bodies (a bad merge) misleads a lookup by instance id alone, and the
    printed line, which also passes the verified hash, refuses it."""
    plain, annotated = Dataset(size=3), Dataset(size=3, note="kept")
    registry = InMemoryRegistry()
    a, b = uuid4(), uuid4()
    registry.add_instance(body=plain.instance_body(), task_id=plain.id, instance_id=a)
    registry.add_instance(
        body=annotated.instance_body(), task_id=annotated.id, instance_id=b
    )
    stored_a = write_from_registry(
        plain.id, instance_id=a, registry=registry, store=tmp_path
    )
    stored_b = write_from_registry(
        plain.id, instance_id=b, registry=registry, store=tmp_path
    )
    for stored, other in ((stored_a, b), (stored_b, a)):
        data = json.loads(stored.path.read_text())
        data["sources"][0]["instance_id"] = str(other)
        stored.path.write_text(json.dumps(data))
    # By id alone: the unverified record decides (documented).
    assert Dataset.from_store(plain.id, instance_id=a, store=tmp_path).note == "kept"
    # By id and hash, as printed: refused, not the other body.
    with pytest.raises(sd.TaskStoreError, match="holds no body"):
        Dataset.from_store(
            plain.id,
            instance_id=a,
            instance_hash=plain.instance_hash,
            store=tmp_path,
        )


def test_a_pointer_naming_another_class_is_refused(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    (stored,) = sd.write_to_store(model, store=tmp_path)
    data = json.loads(stored.path.read_text())
    data["body"]["data"][POINTER_KEY]["task"] = "something.Else"
    stored.path.write_text(json.dumps(data))
    with pytest.raises(sd.TaskStoreError, match="holds a"):
        TrainedModel.from_store(model.id, store=tmp_path)


def test_a_relative_store_resolves_against_the_package_root(tmp_path, monkeypatch):
    package = tmp_path / "pkg"
    package.mkdir()
    monkeypatch.chdir(tmp_path)  # not the package root
    model = TrainedModel(data=Dataset(size=3))
    with project_config_override(ProjectConfig(root=package)):
        (stored,) = sd.write_to_store(model, store="pins")
        assert stored.path.is_relative_to(package / "pins")
        assert stored.call.endswith(', store="pins")')
        assert TrainedModel.from_store(model.id, store="pins") == model


def test_the_configured_store_is_not_printed(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    with project_config_override(
        ProjectConfig(root=tmp_path, task_store=tmp_path / "s")
    ):
        (stored,) = sd.write_to_store(model, store="s")
    assert "store=" not in stored.call


def test_an_absolute_store_outside_the_package_warns(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    model = TrainedModel(data=Dataset(size=3))
    with project_config_override(ProjectConfig(root=package)):
        with pytest.warns(UserWarning, match="absolute path outside the package"):
            sd.write_to_store(model, store=tmp_path / "elsewhere")


def test_files_get_an_ordinary_mode(tmp_path):
    import os

    mask = os.umask(0o022)
    os.umask(mask)
    sd.write_to_store(TrainedModel(data=Dataset(size=3)), store=tmp_path)
    for path in _store_files(tmp_path):
        assert path.stat().st_mode & 0o777 == 0o666 & ~mask


@pytest.mark.parametrize(
    "namespace,name,expected",
    [
        ("", ".", "_."),
        ("", "..", "_.."),
        ("", ".hidden", "_.hidden"),
        ("", "CON", "_CON"),
        ("", "nul", "_nul"),
        ("", "Com1", "_Com1"),
        ("lpt9", "Model", "_lpt9.Model"),
        ("", "Aux.txt", "_Aux.txt"),
        ("", "Console", "Console"),
        ("a b", "Model", "a_b.Model"),
    ],
)
def test_labels_are_safe_directory_names(namespace, name, expected):
    from stardag._core.task_store_format import label

    assert label(namespace, name) == expected


def test_a_known_module_hint_is_never_replaced_by_none(tmp_path):
    model = TrainedModel(data=Dataset(size=3))
    (stored,) = sd.write_to_store(model, store=tmp_path)
    store = TaskStore(tmp_path)
    envelope = store.read(stored.path)
    envelope.module = None
    assert store.write(envelope, {"kind": "local"}) is None
    assert json.loads(stored.path.read_text())["module"] == __name__


def test_files_added_on_disk_are_found_after_a_cached_index(tmp_path):
    import shutil

    first, second = tmp_path / "first", tmp_path / "second"
    model, other = TrainedModel(data=Dataset(size=3)), Dataset(size=9)
    sd.write_to_store(model, store=first)
    sd.write_to_store(other, store=second)
    assert TrainedModel.from_store(model.id, store=first) == model  # index cached
    shutil.copytree(second, first, dirs_exist_ok=True)  # e.g. a git pull
    assert Dataset.from_store(other.id, store=first) == other


def test_concurrent_processes_keep_every_instance_id(tmp_path):
    """Two processes adding sources to one body serialize on the store's
    lock: no addition is lost."""
    import os
    import subprocess
    import sys
    import textwrap

    script = tmp_path / "writer.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys, uuid
            sys.path.insert(0, {str(Path(__file__).parent)!r})
            import stardag as sd
            from stardag._core.task_store_write import write_from_registry
            from stardag.testing import InMemoryRegistry
            from test_task_store import Dataset

            task = Dataset(size=3)
            registry = InMemoryRegistry()
            for _ in range(15):
                iid = uuid.uuid4()
                registry.add_instance(body=task.instance_body(), task_id=task.id, instance_id=iid)
                write_from_registry(task.id, instance_id=iid, registry=registry, store=sys.argv[1])
            """
        )
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("STARDAG_")}
    env.update(STARDAG_NO_REGISTRY="1", HOME=str(tmp_path))
    store = tmp_path / "store"
    procs = [
        subprocess.Popen([sys.executable, str(script), str(store)], env=env)
        for _ in range(2)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    (path,) = list(store.rglob("*.json"))
    assert len(json.loads(path.read_text())["sources"]) == 30


# -----------------------------------------------------------------------------
# References: which source serves them
# -----------------------------------------------------------------------------


def test_an_explicit_registry_skips_the_store(configured_store):
    stored_body = Dataset(size=3, note="from the store")
    sd.write_to_store(stored_body)
    registry, instance_id = _seeded(Dataset(size=3, note="from the registry"))
    ref = Dataset.ref(stored_body.id)
    assert ref.hydrate(registry).note == "from the registry"
    sd.clear_hydration_memo()
    assert Dataset.ref(stored_body.id).hydrate().note == "from the store"


def test_a_failed_hydration_says_which_store_was_checked(configured_store):
    with pytest.raises(sd.TaskRefHydrationError) as e:
        Dataset.ref(uuid4()).hydrate()
    assert f"The task store at {configured_store} holds no body" in str(e.value)


def test_a_failed_hydration_says_no_store_is_configured():
    with pytest.raises(sd.TaskRefHydrationError, match="No task store is configured"):
        Dataset.ref(uuid4()).hydrate()


def test_a_ref_keeps_its_anchor_on_copies_but_not_in_pickles():
    import copy
    import pickle

    from stardag._core.task_store_read import REF_ANCHOR_KEY

    ref = Dataset.ref(uuid4())
    assert vars(ref)[REF_ANCHOR_KEY] == Path(__file__).absolute()
    assert vars(copy.deepcopy(ref))[REF_ANCHOR_KEY] == Path(__file__).absolute()
    assert vars(ref.model_copy())[REF_ANCHOR_KEY] == Path(__file__).absolute()
    assert REF_ANCHOR_KEY not in vars(pickle.loads(pickle.dumps(ref)))
    other = Dataset.ref(ref.task_id)
    vars(other)[REF_ANCHOR_KEY] = Path("/elsewhere.py")
    assert other == ref  # not compared


class Annotated_(sd.Task[int]):
    """A small tree for the exact file text."""

    label: str
    options: dict[str, list[int]] = {}
    inputs: list[sd.TaskLoads[list[int]]] = []

    def run(self):
        self._save(0)


def test_the_exact_file_text(tmp_path):
    dataset = Dataset(size=2)
    task = Annotated_(
        label="Ångström", options={"b": [2, 1], "a": []}, inputs=[dataset]
    )
    (stored,) = sd.write_to_store(task, store=tmp_path)
    text = stored.path.read_text(encoding="utf-8")
    source = json.loads(text)["sources"][0]
    expected = f"""{{
  "stardag_task_store": 1,
  "task_id": "{task.id}",
  "instance_hash": "{task.instance_hash}",
  "namespace": "{__name__}",
  "name": "Annotated_",
  "module": "{__name__}",
  "sources": [
    {{
      "kind": "local",
      "stardag_version": "{source["stardag_version"]}",
      "written_at": "{source["written_at"]}"
    }}
  ],
  "body": {{
    "__name": "Annotated_",
    "__namespace": "{__name__}",
    "inputs": [
      {{
        "__stardag_task": {{
          "instance_hash": "{dataset.instance_hash}",
          "task": "{__name__}.Dataset",
          "task_id": "{dataset.id}"
        }}
      }}
    ],
    "label": "Ångström",
    "options": {{
      "a": [],
      "b": [
        2,
        1
      ]
    }},
    "version": ""
  }}
}}
"""
    assert text == expected


def test_files_follow_the_umask_in_effect_when_written(tmp_path):
    import os

    previous = os.umask(0o077)
    try:
        sd.write_to_store(Dataset(size=3), store=tmp_path)
    finally:
        os.umask(previous)
    for path in _store_files(tmp_path):
        assert path.stat().st_mode & 0o777 == 0o600


def test_a_body_added_on_disk_for_a_known_task_is_seen(tmp_path):
    import shutil

    plain, annotated = Dataset(size=3), Dataset(size=3, note="kept")
    sd.write_to_store(plain, store=tmp_path / "a")
    sd.write_to_store(annotated, store=tmp_path / "b")
    assert Dataset.from_store(plain.id, store=tmp_path / "a") == plain  # cached
    shutil.copytree(tmp_path / "b", tmp_path / "a", dirs_exist_ok=True)
    picked = Dataset.from_store(
        plain.id, instance_hash=annotated.instance_hash, store=tmp_path / "a"
    )
    assert picked.note == "kept"
    with pytest.raises(sd.TaskStoreError, match="2 bodies"):
        Dataset.from_store(plain.id, store=tmp_path / "a")


def test_the_marker_records_the_packages_task_modules(tmp_path):
    with project_config_override(
        ProjectConfig(root=tmp_path, task_store=tmp_path / "s", task_modules=("a.*",))
    ):
        sd.write_to_store(Dataset(size=3))
    marker = (tmp_path / "s" / "stardag_task_store.toml").read_text()
    assert 'task_modules = ["a.*"]' in marker
