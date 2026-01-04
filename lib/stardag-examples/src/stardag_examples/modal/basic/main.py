from stardag_examples.modal.basic.app import stardag_app
from stardag_examples.modal.basic.task import get_range, get_sum

if __name__ == "__main__":
    dag = get_sum(integers=get_range(limit=10))
    res = stardag_app.build_spawn(dag)
    print(res)
