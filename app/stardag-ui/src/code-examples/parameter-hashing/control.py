from typing import Annotated
import stardag as sd

# Initial implementation
# class Range(sd.Task[list[int]]):
#     limit: int
#
#     def run(self):
#         self._save(list(range(self.limit)))

# Extended implementation
class Range(sd.Task[list[int]]):
    limit: int
    # Previously implicit defaults can be exposed with backwards 
    # compatability in terms of task ID/hash and deserialization
    start: Annotated[int, sd.StardagField(compat_default=0)]
    step: Annotated[int, sd.StardagField(compat_default=1)]

    # Parameters with no effect on output can be excluded from the hash
    verbose: Annotated[bool, sd.StardagField(hash_exclude=True)] = False

    def run(self):
        if self.verbose:
            print("Generating range")
        self._save(list(range(self.start, self.limit, self.step)))
