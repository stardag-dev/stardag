"""Artifact models for tasks to expose rich outputs."""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

# JSON-serializable dict type
# We use dict[str, Any] because Pydantic's schema generation doesn't support
# recursive type definitions like dict[str, JSONValue] where JSONValue references itself.
# At runtime, values should be JSON-serializable (str, int, float, bool, None, list, dict).
JSONAbleDict = dict[str, Any]


class MarkdownArtifact(BaseModel):
    """A markdown artifact for rich text reports and documentation."""

    type: Literal["markdown"] = "markdown"
    name: str = Field(..., description="Artifact name/slug for identification")
    body: str = Field(..., description="Markdown content")


class JSONArtifact(BaseModel):
    """A JSON artifact for structured data."""

    type: Literal["json"] = "json"
    name: str = Field(..., description="Artifact name/slug for identification")
    body: JSONAbleDict = Field(..., description="JSON-serializable dict data")


# Discriminated union for all artifact types
Artifact = Annotated[
    Union[MarkdownArtifact, JSONArtifact],
    Field(discriminator="type"),
]
