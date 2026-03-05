"""Since tasks and serialization are decoupled, we can reference and load tasks
from the registry, even when the task source code is not available."""
from uuid import UUID

import stardag as sd
import pandas as pd

@sd.task
def get_num_rows(df: sd.Depends[pd.DataFrame]) -> int:
    return len(df)

# Get task id from registry UI
df_task_id = UUID("359d8374-de0e-521b-84c1-1e9fd3b8b112")

# We must (only) know data type used for serialization
df_task = sd.AliasTask[pd.DataFrame].from_registry(id=df_task_id)

# AliasTask maintains the original task's ID, and can be composed as usual
num_rows_task = get_num_rows(df=df_task)

sd.build(num_rows_task)
