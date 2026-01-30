from stardag import BaseTask


class _DoNothing(BaseTask):
    def complete(self) -> bool:
        return True

    def run(self):
        pass


class UnspecifiedNamespace(_DoNothing):
    pass


class OverrideNamespaceByDUnder(_DoNothing):
    __namespace__ = "override_namespace"


class ClearNamespaceByDunder(_DoNothing):
    __namespace__ = ""


class OverrideNamespaceByDUnderChild(OverrideNamespaceByDUnder):
    pass


class OverrideNamespaceByArg(_DoNothing, namespace_override="override_namespace"):
    pass


class ClearNamespaceByArg(_DoNothing, namespace_override=""):
    pass


class OverrideNamespaceByArgChild(OverrideNamespaceByArg):
    pass


class CustomNameByArgFromIntermediate(_DoNothing, name_override="custom_name"):
    """Uses name override with intermediate task implementation inheritance."""

    pass


class CustomNameByArgFromTask(BaseTask, name_override="custom_name_2"):
    """Uses name override with base task."""

    def complete(self) -> bool:
        return True

    def run(self):
        pass


class CustomNameByArgFromIntermediateChild(CustomNameByArgFromIntermediate):
    """Should not inherit name override."""

    pass


class CustomNameByArgFromTaskChild(CustomNameByArgFromTask):
    """Should not inherit name override."""

    pass
