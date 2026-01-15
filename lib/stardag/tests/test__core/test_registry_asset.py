"""Tests for registry asset models and task integration."""

import pytest

from stardag._core.registry_asset import (
    JSONRegistryAsset,
    MarkdownRegistryAsset,
    RegistryAsset,
)
from stardag._core.task import BaseTask, auto_namespace

auto_namespace(__name__)  # Avoid collisions in task registry


class MockTask(BaseTask):
    """A simple task for testing."""

    value: int = 0

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class MockTaskWithAssets(BaseTask):
    """A task that produces registry assets."""

    value: int = 0

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass

    def registry_assets(self) -> list[RegistryAsset]:
        return [
            MarkdownRegistryAsset(
                name="report",
                body=f"# Report\n\nValue is {self.value}",
            ),
            JSONRegistryAsset(
                name="metrics",
                body={"value": self.value, "doubled": self.value * 2},
            ),
        ]


class TestMarkdownRegistryAsset:
    """Tests for MarkdownRegistryAsset."""

    def test_create_markdown_asset(self):
        """Test creating a markdown asset."""
        asset = MarkdownRegistryAsset(
            name="test-report",
            body="# Hello\n\nThis is a test.",
        )
        assert asset.type == "markdown"
        assert asset.name == "test-report"
        assert asset.body == "# Hello\n\nThis is a test."

    def test_markdown_asset_serialization(self):
        """Test serializing markdown asset to JSON."""
        asset = MarkdownRegistryAsset(
            name="report",
            body="# Test",
        )
        data = asset.model_dump(mode="json")
        assert data == {
            "type": "markdown",
            "name": "report",
            "body": "# Test",
        }

    def test_markdown_asset_from_dict(self):
        """Test creating markdown asset from dict."""
        data = {"type": "markdown", "name": "doc", "body": "Content"}
        asset = MarkdownRegistryAsset.model_validate(data)
        assert asset.name == "doc"
        assert asset.body == "Content"


class TestJSONRegistryAsset:
    """Tests for JSONRegistryAsset."""

    def test_create_json_asset(self):
        """Test creating a JSON asset."""
        body = {"key": "value", "count": 42}
        asset = JSONRegistryAsset(name="data", body=body)
        assert asset.type == "json"
        assert asset.name == "data"
        assert asset.body == {"key": "value", "count": 42}

    def test_json_asset_with_nested_data(self):
        """Test JSON asset with nested structures."""
        body = {
            "metrics": {
                "accuracy": 0.95,
                "items": [1, 2, 3],
            },
            "labels": ["a", "b", "c"],
        }
        asset = JSONRegistryAsset(name="results", body=body)
        assert asset.body["metrics"]["accuracy"] == 0.95  # type: ignore
        assert asset.body["labels"] == ["a", "b", "c"]

    def test_json_asset_serialization(self):
        """Test serializing JSON asset."""
        asset = JSONRegistryAsset(
            name="stats",
            body={"count": 10, "values": [1, 2, 3]},
        )
        data = asset.model_dump(mode="json")
        assert data == {
            "type": "json",
            "name": "stats",
            "body": {"count": 10, "values": [1, 2, 3]},
        }


class TestRegistryAssetsOnTask:
    """Tests for registry_assets method on BaseTask."""

    def test_default_registry_assets_empty(self):
        """Test that default registry_assets returns empty list."""
        task = MockTask(value=42)
        assets = task.registry_assets()
        assert assets == []

    def test_task_with_registry_assets(self):
        """Test task that produces registry assets."""
        task = MockTaskWithAssets(value=5)
        assets = task.registry_assets()
        assert len(assets) == 2

        # Check markdown asset
        md_asset = assets[0]
        assert isinstance(md_asset, MarkdownRegistryAsset)
        assert md_asset.name == "report"
        assert "Value is 5" in md_asset.body

        # Check JSON asset
        json_asset = assets[1]
        assert isinstance(json_asset, JSONRegistryAsset)
        assert json_asset.name == "metrics"
        assert json_asset.body == {"value": 5, "doubled": 10}

    def test_registry_assets_are_dynamic(self):
        """Test that registry assets can use task state."""
        task1 = MockTaskWithAssets(value=10)
        task2 = MockTaskWithAssets(value=20)

        assets1 = task1.registry_assets()
        assets2 = task2.registry_assets()

        # Assets should reflect the task's value
        json1 = assets1[1]
        json2 = assets2[1]
        assert isinstance(json1, JSONRegistryAsset)
        assert isinstance(json2, JSONRegistryAsset)
        assert json1.body["value"] == 10
        assert json2.body["value"] == 20


class TestRegistryAssetUnion:
    """Tests for the RegistryAsset discriminated union."""

    def test_parse_markdown_from_union(self):
        """Test parsing markdown asset via discriminated union."""
        from pydantic import TypeAdapter

        adapter = TypeAdapter(RegistryAsset)
        data = {"type": "markdown", "name": "doc", "body": "# Title"}
        asset = adapter.validate_python(data)
        assert isinstance(asset, MarkdownRegistryAsset)
        assert asset.name == "doc"

    def test_parse_json_from_union(self):
        """Test parsing JSON asset via discriminated union."""
        from pydantic import TypeAdapter

        adapter = TypeAdapter(RegistryAsset)
        data = {"type": "json", "name": "data", "body": {"key": "val"}}
        asset = adapter.validate_python(data)
        assert isinstance(asset, JSONRegistryAsset)
        assert asset.name == "data"

    def test_invalid_type_raises(self):
        """Test that invalid type raises validation error."""
        from pydantic import TypeAdapter, ValidationError

        adapter = TypeAdapter(RegistryAsset)
        data = {"type": "unknown", "name": "bad", "body": "data"}
        with pytest.raises(ValidationError):
            adapter.validate_python(data)
