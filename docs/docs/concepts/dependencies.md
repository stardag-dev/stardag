# Dependencies

Dependencies define how tasks relate to each other and form the DAG structure.

## Declaring Dependencies

Task dependencies are declared via the method `requires`:

```python
Number = int | float

class AddAB(sd.AutoTask[Number]):

    def requires(self):
        return {
            "a": ATask(),
            "b": BTask(),
        }

    def run(self):
        deps = self.requires()
        a = deps["a"].output().load()
        b = deps["b"].output().load()
        result = a + b
        self output().save(results)
```

This task declares hardcoded dependencies resulting in a _static_ DAG, both in terms of topology and precise nodes.

We can use parameters to forward information to dependencies:

```{.python notest}
ParamType = ...  # Some information to forward to A/BTask

class AddParameterizedAB(sd.AutoTask[Number]):
    a_param: ParamType
    b_param: ParamType

    def requires(self):
        return {
            "a": ATask(a=self.a_param),
            "b": BTask(b=self.b_param),
        }
```

Now we have a DAG with fixed topology, but have parameterized the nodes.

We can extend this pattern with conditional logic to achive parameterized topolgy with a predefined set of alternatives:

```{.python notest}
ParamType = ...  # Some information to forward to A/BTask

class AddParameterizedABAndMaybeC(sd.AutoTask[Number]):
    a_param: ParamType
    b_param: ParamType
    c_param: ParamType | None  # optionally include a third dependency

    def requires(self):
        deps = {
            "a": ATask(a=self.a_param),
            "b": BTask(b=self.b_param),
        }
        if c_param:
            deps["c"] = CTask(self.c_param)

        return deps
```

## Dependency Injection

The pattern above quickly becomes verbose and inflexible, here dependency injection comes to the rescue:

```{.python notest}
class Add(sd.AutoTask[Number]):
    values: list[sd.TaskLoads[Number]]

    def requires(self):
        return self.values

    def run(self):
        result = sum([dep.output().load() for dep in self.values])
        self.output().save(result)
```

Now we have effectively _arbitrarilly_ parameterized the upstream dependencies, the definition of nodes as well as the DAG topology. Each elemet of `values` can be any task type, as long as its `output().load()` returns a `Number`.

We can also accept raw data as parameters _or_ tasks which output loads this data, by:

```{.python notest}
class Add(sd.AutoTask[Number]):
    values: list[sd.TaskLoads[Number] | Number]

    def requires(self):
        return [value for value in self.values if isinstance(value, sd.BaseTask)]

    def run(self):
        result = sum(
            [
                value.output().load() if isinstance(value, sd.BaseTask)
                else value
                for value in self.values
            ]
        )
        self.output().save(result)
```

### Benefits of Dependency Injection:

- **Loose coupling**: Downstream tasks don't know upstream implementation
- **Reusability**: Same task works with different inputs
- **Testability**: Easy to mock dependencies
- **Flexibility**: Compose DAGs dynamically

## Decorator API

When using the decorator API, `requires` is implemented automatically based on parameters annotated with `sd.Depends`:

```python
@sd.task
def add(a: sd.Depends[Number], b: sd.Depends[Number]) -> Number:
    return a + b
```

---

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. It should soon cover:

    - Dynamic dependencies (which requires upstream tasks to be executed before we know which additional dependencies are needed)
    - Common patterns and best practices
    - Context on how *Polymorphism* is handled via `PolymorphicRoot`s class registry.
