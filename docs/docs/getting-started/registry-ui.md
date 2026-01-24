# Using the Stardag Registry - API & UI

So far we have mostly leveraged stardag to store and retrive intemediate task outputs (persistent caching) and materialized them with bottom up, make-style, execution. Next we will leverage the Stardag **Registry** to, among other things, get some observability of our DAGs execution.

## Get setup

=== "stardag.com (Managed)"

    Go to [app.stardag.com](https://app.stardag.com) and click [Sign In] or [Get Started] and complete the signup.

    When signing up, a new personal **Workspace** with one **Environment** (`main`, unless you picked another name) will have been created for you.

    ### Setup your profile and Authenticate

    Active the virtual environment where you have `stardag` installed (or prefix commands with `uv run`):

    === "Activated venv"

        ```sh
        stardag config registry add central --url https://api.stardag.com/
        stardag auth login --registry central
        ```

    === "uv run ..."

        ```sh
        uv run stardag config registry add central --url https://api.stardag.com/
        uv run stardag auth login --registry central
        ```

    You should be redirected to your browser to complete the log in.

    Then follow the steps to get your current profile setup. Then list your profiles to verify the setup.

    === "Activated venv"

        ```sh
        stardag config profile list
        ```

    === "uv run ..."

        ```sh
        uv run stardag config profile list
        ```

    ```
    Profiles:

    central-<username>-<username>-local *
        registry: central
        user: <username>@example.com
        workspace: <username>
        environment: local


    * active profile (via [default] in /Users/<user>/.stardag/config.toml)
    ```

=== "Local `docker compose` (Self hosted)"

    ### Prerequisites

    - [Git](https://git-scm.com/)
    - [Docker Compose](https://docs.docker.com/compose/)

    ### Clone the repo

    === "HTTPS"

        ```
        git clone https://github.com/stardag-dev/stardag.git
        ```

        Clone using the web URL.

    === "SSH"

        ```
        git clone git@github.com:stardag-dev/stardag.git
        ```

        Use a password-protected SSH key.

    === "GitHub CLI"

        ```
        gh repo clone stardag-dev/stardag
        ```

        Use the GitHub official CLI. [Learn more](https://cli.github.com/)

    ### Start services locally

    ```
    cd stardag
    docker compose up --build -d
    ```

    In your browser, navigate to [localhost:3000](http://localhost:3000/), click and [Sign In] or [Get Started]. Click [Register] and sign up with a new user. You can use any email and password, e.g. `me@localhost` and `mypass`.

    When signing up, a new personal **Workspace** with one **Environment** (`main`, unless you picked another name) will have been created for you.

    ### Setup your profile and Authenticate

    Active the virtual environment where you have `stardag` installed (or prefix commands with `uv run`):

    === "Activated venv"

        ```sh
        stardag config registry add local --url http://localhost:8000
        stardag auth login --registry local
        ```

    === "uv run ..."

        ```sh
        uv run stardag config registry add local --url http://localhost:8000
        uv run stardag auth login --registry local
        ```

    You should be redirected to your browser to complete the log in.

    Then follow the steps to get your current profile setup. Then list your profiles to verify the setup.

    === "Activated venv"

        ```sh
        stardag config profile list
        ```

    === "uv run ..."

        ```sh
        uv run stardag config profile list
        ```

    ```
    Profiles:

    local-<username>-<username>-local *
        registry: local
        user: <username>@localhost
        workspace: <username>
        environment: local


    * active profile (via [default] in /Users/<user>/.stardag/config.toml)
    ```

## Build your DAG

Now let's build the DAG from the previous example again, but make sure to set `get_range`'s `limit` argument to something new, so that we need to actually build something (otherwise all tasks will be materialized already from previous runs).

```python
import stardag as sd

@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

# Compose the DAG
task = get_sum(integers=get_range(limit=4))  # <-- SET SOME OTHER NUMBER

# Build executes both tasks in the correct order
sd.build(task)

print(task.output().load())  # 10
```

Now refresh the home page and click the started build.
