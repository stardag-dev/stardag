"""Tests for artifact models and task integration."""

import pytest

from stardag import BaseTask, auto_namespace
from stardag.artifact import (
    Artifact,
    JSONArtifact,
    MarkdownArtifact,
)

auto_namespace(__name__)  # Avoid collisions in task registry


class MockTask(BaseTask):
    """A simple task for testing."""

    value: int = 0

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class MockTaskWithArtifacts(BaseTask):
    """A task that produces artifacts."""

    value: int = 0

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass

    def artifacts(self) -> list[Artifact]:
        return [
            MarkdownArtifact(
                name="report",
                body=f"# Report\n\nValue is {self.value}",
            ),
            JSONArtifact(
                name="metrics",
                body={"value": self.value, "doubled": self.value * 2},
            ),
        ]


class TestMarkdownArtifact:
    """Tests for MarkdownArtifact."""

    def test_create_markdown_artifact(self):
        """Test creating a markdown artifact."""
        artifact = MarkdownArtifact(
            name="test-report",
            body="# Hello\n\nThis is a test.",
        )
        assert artifact.type == "markdown"
        assert artifact.name == "test-report"
        assert artifact.body == "# Hello\n\nThis is a test."

    def test_markdown_artifact_serialization(self):
        """Test serializing markdown artifact to JSON."""
        artifact = MarkdownArtifact(
            name="report",
            body="# Test",
        )
        data = artifact.model_dump(mode="json")
        assert data == {
            "type": "markdown",
            "name": "report",
            "body": "# Test",
        }

    def test_markdown_artifact_from_dict(self):
        """Test creating markdown artifact from dict."""
        data = {"type": "markdown", "name": "doc", "body": "Content"}
        artifact = MarkdownArtifact.model_validate(data)
        assert artifact.name == "doc"
        assert artifact.body == "Content"


class TestJSONArtifact:
    """Tests for JSONArtifact."""

    def test_create_json_artifact(self):
        """Test creating a JSON artifact."""
        body = {"key": "value", "count": 42}
        artifact = JSONArtifact(name="data", body=body)
        assert artifact.type == "json"
        assert artifact.name == "data"
        assert artifact.body == {"key": "value", "count": 42}

    def test_json_artifact_with_nested_data(self):
        """Test JSON artifact with nested structures."""
        body = {
            "metrics": {
                "accuracy": 0.95,
                "items": [1, 2, 3],
            },
            "labels": ["a", "b", "c"],
        }
        artifact = JSONArtifact(name="results", body=body)
        assert artifact.body["metrics"]["accuracy"] == 0.95  # type: ignore
        assert artifact.body["labels"] == ["a", "b", "c"]

    def test_json_artifact_serialization(self):
        """Test serializing JSON artifact."""
        artifact = JSONArtifact(
            name="stats",
            body={"count": 10, "values": [1, 2, 3]},
        )
        data = artifact.model_dump(mode="json")
        assert data == {
            "type": "json",
            "name": "stats",
            "body": {"count": 10, "values": [1, 2, 3]},
        }


class TestArtifactsOnTask:
    """Tests for artifacts method on BaseTask."""

    def test_default_artifacts_empty(self):
        """Test that default artifacts returns empty list."""
        task = MockTask(value=42)
        artifacts = task.artifacts()
        assert artifacts == []

    def test_task_with_artifacts(self):
        """Test task that produces artifacts."""
        task = MockTaskWithArtifacts(value=5)
        artifacts = task.artifacts()
        assert len(artifacts) == 2

        # Check markdown artifact
        md_artifact = artifacts[0]
        assert isinstance(md_artifact, MarkdownArtifact)
        assert md_artifact.name == "report"
        assert "Value is 5" in md_artifact.body

        # Check JSON artifact
        json_artifact = artifacts[1]
        assert isinstance(json_artifact, JSONArtifact)
        assert json_artifact.name == "metrics"
        assert json_artifact.body == {"value": 5, "doubled": 10}

    def test_artifacts_are_dynamic(self):
        """Test that artifacts can use task state."""
        task1 = MockTaskWithArtifacts(value=10)
        task2 = MockTaskWithArtifacts(value=20)

        artifacts1 = task1.artifacts()
        artifacts2 = task2.artifacts()

        # Artifacts should reflect the task's value
        json1 = artifacts1[1]
        json2 = artifacts2[1]
        assert isinstance(json1, JSONArtifact)
        assert isinstance(json2, JSONArtifact)
        assert json1.body["value"] == 10
        assert json2.body["value"] == 20


class TestArtifactUnion:
    """Tests for the Artifact discriminated union."""

    def test_parse_markdown_from_union(self):
        """Test parsing markdown artifact via discriminated union."""
        from pydantic import TypeAdapter

        adapter = TypeAdapter(Artifact)
        data = {"type": "markdown", "name": "doc", "body": "# Title"}
        artifact = adapter.validate_python(data)
        assert isinstance(artifact, MarkdownArtifact)
        assert artifact.name == "doc"

    def test_parse_json_from_union(self):
        """Test parsing JSON artifact via discriminated union."""
        from pydantic import TypeAdapter

        adapter = TypeAdapter(Artifact)
        data = {"type": "json", "name": "data", "body": {"key": "val"}}
        artifact = adapter.validate_python(data)
        assert isinstance(artifact, JSONArtifact)
        assert artifact.name == "data"

    def test_invalid_type_raises(self):
        """Test that invalid type raises validation error."""
        from pydantic import TypeAdapter, ValidationError

        adapter = TypeAdapter(Artifact)
        data = {"type": "unknown", "name": "bad", "body": "data"}
        with pytest.raises(ValidationError):
            adapter.validate_python(data)
