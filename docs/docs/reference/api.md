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
        - BaseTask
        - Depends
        - TaskLoads
        - TaskRef
        - TaskStruct
        - build
        - build_aio
        - build_sequential
        - build_sequential_aio
        - namespace
        - auto_namespace
        - get_target
        - get_directory_target
        - target_factory_provider

## Build Module

::: stardag.build
    options:
      show_root_heading: true
      members:
        - build
        - build_aio
        - build_sequential
        - build_sequential_aio
        - BuildSummary
        - BuildExitStatus
        - FailMode
        - HybridConcurrentTaskExecutor
        - TaskExecutorABC

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

## Registry Module

::: stardag.registry
    options:
      show_root_heading: true
      members:
        - APIRegistry
        - RegistryABC
        - NoOpRegistry
        - registry_provider

## Configuration

::: stardag.config
    options:
      show_root_heading: true
      members:
        - load_config

<!-- TODO: Expand API reference as docstrings are improved -->
