from stardag._task import BaseTask


class _DoNothing(BaseTask):
    def complete(self) -> bool:
        return True

    def run(self):
        pass


class UnspecifiedNamespace(_DoNothing):
    pass


class OverrideNamespaceByDUnder(_DoNothing):
    __type_namespace__ = "override_namespace"


class ClearNamespaceByDunder(_DoNothing):
    __type_namespace__ = ""


class OverrideNamespaceByDUnderChild(OverrideNamespaceByDUnder):
    pass


class OverrideNamespaceByArg(_DoNothing, type_namespace="override_namespace"):
    pass


class ClearNamespaceByArg(_DoNothing, type_namespace=""):
    pass


class OverrideNamespaceByArgChild(OverrideNamespaceByArg):
    pass


class CustomFamilyByArgFromIntermediate(_DoNothing, type_name="custom_family"):
    """Uses family_override with intermediate task implementation inheritance."""

    pass


class CustomFamilyByArgFromTask(BaseTask, type_name="custom_family_2"):
    """Uses family_override with base task."""

    def complete(self) -> bool:
        return True

    def run(self):
        pass


class CustomFamilyByDUnder(_DoNothing):
    """Children would have to override either namespace or family (almost never makes
    sense to use this)"""

    __family__ = "custom_family_3"


class CustomFamilyByArgFromIntermediateChild(CustomFamilyByArgFromIntermediate):
    """Should not inherit family_override."""

    pass


class CustomFamilyByArgFromTaskChild(CustomFamilyByArgFromTask):
    """Should not inherit family_override."""

    pass


try:

    class CustomFamilyByDUnderChild(CustomFamilyByDUnder):  # type: ignore
        """Must override __family__."""

        pass

except ValueError:

    class CustomFamilyByDUnderChild(
        CustomFamilyByDUnder, family_override="custom_family_3_child"
    ):
        """Must override __family__."""

        pass
