from typing import Any

import luigi
from pydantic import TypeAdapter

import stardag as sd  # noqa

task_type_adapter = TypeAdapter(sd.TaskParam[Any])


class StardagTaskParameter(luigi.Parameter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def parse(self, x) -> sd.Task:
        if isinstance(x, str):
            return task_type_adapter.validate_json(x)

        if isinstance(x, dict):
            return task_type_adapter.validate_python(x)

        assert isinstance(x, sd.Task)
        return x

    def normalize(self, x) -> sd.Task:
        return self.parse(x)

    def serialize(self, x) -> str:
        return task_type_adapter.dump_json(x).decode()


class StardagTask(luigi.Task):
    wrapped: sd.Task = StardagTaskParameter()  # type: ignore

    def run(self):  # type: ignore
        # TODO dynamic deps
        return self.wrapped.run()

    def requires(self):  # type: ignore
        reqs = self.wrapped.requires()
        if isinstance(reqs, sd.Task):
            return StardagTask(wrapped=reqs)
        if isinstance(reqs, list):
            return [StardagTask(wrapped=req) for req in reqs]
        if isinstance(reqs, dict):
            return {k: StardagTask(wrapped=req) for k, req in reqs.items()}

    def output(self):
        # Stardag Target is compatible with luigi Target
        return self.wrapped.output()
