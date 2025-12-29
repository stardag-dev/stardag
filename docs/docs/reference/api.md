# API Reference

Auto-generated API documentation from source code.

<!-- prettier-ignore -->
<!-- Note: mkdocstrings generates this content from docstrings -->

## Core Module

::: stardag
    options:
      show_root_heading: false
      show_source: false
      members:
        - task
        - Task
        - AutoTask
        - Depends
        - TaskLoads
        - TaskSet
        - TaskDeps
        - TaskParam
        - build
        - namespace
        - auto_namespace
        - get_target
        - get_directory_target
        - target_factory_provider
        - IDHasher
        - IDHashInclude

## Build Module

::: stardag.build.sequential
    options:
      show_root_heading: true
      members:
        - build

## Target Module

::: stardag.target
    options:
      show_root_heading: true
      members:
        - FileSystemTarget
        - LoadableSaveableFileSystemTarget
        - LocalTarget
        - TargetFactory
        - target_factory_provider

## Configuration

::: stardag.config
    options:
      show_root_heading: true
      members:
        - load_config

<!-- TODO: Expand API reference as docstrings are improved -->
