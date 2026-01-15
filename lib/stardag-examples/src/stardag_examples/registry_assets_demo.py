"""Demo script showing how to use registry assets with stardag.

Registry assets allow tasks to produce rich outputs (markdown reports, JSON data)
that are stored in the registry and viewable in the UI.

To run this demo:
1. Start the services: docker compose up -d
2. Set the API URL: export STARDAG_API_REGISTRY_URL=http://localhost:8000
3. Run this script: python -m stardag_examples.registry_assets_demo
"""

import random
from datetime import datetime
from typing import TypedDict

import stardag as sd
from stardag.config import load_config


class DataResult(TypedDict):
    """Type for data collector result."""

    samples: list[float]
    count: int
    collected_at: str


class DataCollector(sd.AutoTask[DataResult]):
    """A task that collects sample data and produces a JSON asset with the results."""

    sample_size: int = 100

    def run(self) -> None:
        """Generate sample data and save to output."""
        # Simulate collecting data
        data = [random.gauss(0, 1) for _ in range(self.sample_size)]
        result: DataResult = {
            "samples": data,
            "count": len(data),
            "collected_at": datetime.now().isoformat(),
        }
        self.output().save(result)

    def registry_assets(self) -> list[sd.RegistryAsset]:
        """Produce a JSON asset with data statistics."""
        result = self.output().load()
        samples = result["samples"]

        # Calculate basic statistics
        mean = sum(samples) / len(samples)
        variance = sum((x - mean) ** 2 for x in samples) / len(samples)
        std_dev = variance**0.5
        min_val = min(samples)
        max_val = max(samples)

        return [
            sd.JSONRegistryAsset(
                name="raw-data",
                body={
                    "sample_size": result["count"],
                    "collected_at": result["collected_at"],
                    "first_10_samples": samples[:10],
                },
            ),
            sd.JSONRegistryAsset(
                name="statistics",
                body={
                    "mean": round(mean, 4),
                    "std_dev": round(std_dev, 4),
                    "min": round(min_val, 4),
                    "max": round(max_val, 4),
                    "variance": round(variance, 4),
                },
            ),
        ]


class AnalysisReport(sd.AutoTask[str]):
    """A task that generates a markdown analysis report from collected data."""

    data_source: sd.TaskLoads[DataResult]

    def requires(self) -> sd.TaskStruct:
        """Declare dependency on data source."""
        return self.data_source

    def run(self) -> None:
        """Generate analysis report content."""
        data = self.data_source.output().load()
        samples = data["samples"]

        # Calculate statistics
        mean = sum(samples) / len(samples)
        variance = sum((x - mean) ** 2 for x in samples) / len(samples)
        std_dev = variance**0.5

        result = f"Analysis complete. Mean: {mean:.4f}, StdDev: {std_dev:.4f}"
        self.output().save(result)

    def registry_assets(self) -> list[sd.RegistryAsset]:
        """Produce a markdown report asset."""
        data = self.data_source.output().load()
        samples = data["samples"]

        # Calculate statistics
        mean = sum(samples) / len(samples)
        variance = sum((x - mean) ** 2 for x in samples) / len(samples)
        std_dev = variance**0.5
        min_val = min(samples)
        max_val = max(samples)

        # Build markdown report
        report = f"""# Data Analysis Report

## Overview

This report summarizes the analysis of {len(samples)} data samples
collected at {data["collected_at"]}.

## Statistics Summary

| Metric | Value |
|--------|-------|
| Sample Size | {len(samples)} |
| Mean | {mean:.4f} |
| Standard Deviation | {std_dev:.4f} |
| Variance | {variance:.4f} |
| Minimum | {min_val:.4f} |
| Maximum | {max_val:.4f} |

## Distribution Analysis

The data appears to follow a normal distribution with the following
characteristics:

- **Central Tendency**: The mean value of {mean:.4f} suggests the data
  is centered around zero, as expected for a standard normal distribution.
- **Spread**: A standard deviation of {std_dev:.4f} indicates the
  typical spread of values from the mean.
- **Range**: Values range from {min_val:.4f} to {max_val:.4f}.

## Conclusions

The collected data shows characteristics consistent with a normal
distribution (μ ≈ 0, σ ≈ 1).

---
*Report generated automatically by stardag*
"""
        return [
            sd.MarkdownRegistryAsset(
                name="analysis-report",
                body=report,
            ),
        ]


def main() -> None:
    """Run the registry assets demo."""
    # Load configuration
    config = load_config()
    print("Configuration loaded. Running against Registry at:", config.api.url)

    # Create a simple DAG:
    # DataCollector -> AnalysisReport
    collector = DataCollector(sample_size=50)
    report = AnalysisReport(data_source=collector)

    print("Building DAG with registry assets...")
    print("  - DataCollector: produces JSON assets (raw-data, statistics)")
    print("  - AnalysisReport: produces markdown asset (analysis-report)")

    # Build the DAG
    sd.build([report])

    # Read the result
    result = report.output().load()
    print(f"\nResult: {result}")

    ui_url = None
    if config.api.url == "http://localhost:8000":
        ui_url = "http://localhost:3000"
    elif config.api.url == "https://api.stardag.com":
        ui_url = "https://app.stardag.com"

    if ui_url:
        print(f"\nCheck the UI at {ui_url} to see the registered tasks and assets!")
        print("Click on a task to view its assets (JSON data and markdown reports).")


if __name__ == "__main__":
    main()
