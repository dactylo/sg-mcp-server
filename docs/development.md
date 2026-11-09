# Development

Use a development checkout to change the server or run its checks. For an installed command, follow
the [installation instructions](../README.md#installation).

## Local environment

Install these tools before setting up the checkout:

| Tool    | Required version | Purpose                                                |
| ------- | ---------------- | ------------------------------------------------------ |
| Python  | 3.11 or newer    | Runs the server and development tools.                 |
| uv      | 0.13.6           | Creates the environment and resolves dependency locks. |
| Node.js | 26.12.0          | Runs Pyright.                                          |
| Taplo   | 0.10.0           | Formats TOML code blocks in Markdown.                  |
| shfmt   | 3.14.1           | Formats shell code blocks in Markdown.                 |

From the repository root, run:

```sh
make setup
make check
```

Setup uses `python` on `PATH`. To select another interpreter, run
`make setup PYTHON=/path/to/python`; setup never downloads Python. Setup and the lock targets
require the listed uv version.

`make setup` creates `.venv` if needed, installs the exact hashed development pins, and installs
the project in editable mode. Run it again after changing `pyproject.toml` or the lock files,
including when `make format` rewrites `pyproject.toml`. Commands that use `.venv` refuse a missing
or outdated environment. Checks do not install or update packages.

## Run checks

Run commands from the repository root. Some test fixtures resolve data paths from that directory.

| Command                 | Use it to...                                                                  |
| ----------------------- | ----------------------------------------------------------------------------- |
| `make format`           | Apply formatters, then inspect the changes before committing.                 |
| `make format-check`     | Check formatting and lint without editing files.                              |
| `make whitespace-check` | Check for trailing whitespace.                                                |
| `make quality`          | Run formatting, lint, dependency-lock, type, whitespace, and workflow checks. |
| `make test`             | Run the test suite serially.                                                  |
| `make check`            | Run quality checks and the test suite.                                        |

Quality checks include Pyright for Linux, macOS, and Windows and an offline zizmor audit of GitHub
Actions workflows. Watch formatter warnings: if Taplo or shfmt is missing, mdformat can succeed
without formatting the affected code blocks.

For parallel tests with the same scheduling as CI:

```sh
make test TEST_FLAGS='-n auto --dist worksteal --durations=15'
```

Set `PYTEST_XDIST_AUTO_NUM_WORKERS` when the machine's visible CPU count exceeds its CPU quota.

To install the command from the checkout, run `make install`; use `make reinstall` to replace an
existing installation. Both use `uv tool` and include the official SG client.

## Continuous integration

The [CI workflow](../.github/workflows/ci.yml) runs `quality`, then parallel `test` jobs, then
`package`. The `hosted-validation` job reports the overall outcome, including failures. Public
repository runs use GitHub-hosted runners and include the full test suite. Push runs can be
disabled with the repository variable `CI_RUN_ON_PUSH=false`.

| Test jobs | Python versions   |
| --------- | ----------------- |
| Linux     | 3.11 through 3.15 |
| macOS     | 3.13              |
| Windows   | 3.13              |

Each test job has a one-hour hang guard.

## Update dependencies

Use the uv version in [Local environment](#local-environment). After editing dependencies, update
both locks:

```sh
make lock-update
```

For an intentional upgrade of the pinned packages:

```sh
make lock-update LOCK_FLAGS=--upgrade
```

Review both pin diffs, run `make setup`, then run `make lock-check` to verify the locks.
`make check` also includes that check.

## Dependency lock details

`requirements-ci.txt` pins the development tools, the `official` provider extra, the `ci-build`
group, and the build backend. `requirements-test.txt` selects the smaller set used by test jobs,
constrained by the same pins. Neither file ships with the package; published dependency ranges stay
in `pyproject.toml`. The `ci-build` group supplies third-party source-build tools for the CI
interpreters.

CI downloads checksum-verified Linux builds of Taplo and shfmt. The lock omits the Python Taplo
package otherwise pulled in by `mdformat-config`, because it lacks some ARM wheels; install the
native binary listed under [Local environment](#local-environment).
