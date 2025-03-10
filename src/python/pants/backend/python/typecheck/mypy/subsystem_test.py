# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from textwrap import dedent

import pytest

from pants.backend.python import target_types_rules
from pants.backend.python.goals.lockfile import GeneratePythonLockfile
from pants.backend.python.target_types import PythonRequirementTarget, PythonSourcesGeneratorTarget
from pants.backend.python.typecheck.mypy import skip_field, subsystem
from pants.backend.python.typecheck.mypy.subsystem import (
    MyPy,
    MyPyConfigFile,
    MyPyExtraTypeStubsLockfileSentinel,
    MyPyFirstPartyPlugins,
    MyPyLockfileSentinel,
)
from pants.backend.python.util_rules import python_sources
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.core.target_types import GenericTarget
from pants.core.util_rules import config_files
from pants.engine.fs import EMPTY_DIGEST
from pants.testutil.python_interpreter_selection import skip_unless_python39_present
from pants.testutil.rule_runner import QueryRule, RuleRunner
from pants.util.ordered_set import FrozenOrderedSet
from pants.util.strutil import softwrap


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        rules=[
            *subsystem.rules(),
            *skip_field.rules(),
            *config_files.rules(),
            *python_sources.rules(),
            *target_types_rules.rules(),
            QueryRule(MyPyConfigFile, []),
            QueryRule(MyPyFirstPartyPlugins, []),
            QueryRule(GeneratePythonLockfile, [MyPyLockfileSentinel]),
            QueryRule(GeneratePythonLockfile, [MyPyExtraTypeStubsLockfileSentinel]),
        ],
        target_types=[PythonSourcesGeneratorTarget, PythonRequirementTarget, GenericTarget],
    )


def test_warn_if_python_version_configured(rule_runner: RuleRunner, caplog) -> None:
    config = {"mypy.ini": "[mypy]\npython_version = 3.6"}
    rule_runner.write_files(config)  # type: ignore[arg-type]
    config_digest = rule_runner.make_snapshot(config).digest

    def maybe_assert_configured(*, has_config: bool, args: list[str], warning: str = "") -> None:
        rule_runner.set_options(
            [f"--mypy-args={repr(args)}", f"--mypy-config-discovery={has_config}"],
            env_inherit={"PATH", "PYENV_ROOT", "HOME"},
        )
        result = rule_runner.request(MyPyConfigFile, [])

        assert result.digest == (config_digest if has_config else EMPTY_DIGEST)
        should_be_configured = has_config or bool(args)
        assert result._python_version_configured == should_be_configured

        autoset_python_version = result.python_version_to_autoset(
            InterpreterConstraints([">=3.6"]), ["2.7", "3.6", "3.7", "3.8"]
        )
        if should_be_configured:
            assert autoset_python_version is None
        else:
            assert autoset_python_version == "3.6"

        if should_be_configured:
            assert caplog.records
            assert warning in caplog.text
            caplog.clear()
        else:
            assert not caplog.records

    maybe_assert_configured(
        has_config=True, args=[], warning="You set `python_version` in mypy.ini"
    )
    maybe_assert_configured(
        has_config=False, args=["--py2"], warning="You set `--py2` in the `--mypy-args` option"
    )
    maybe_assert_configured(
        has_config=False,
        args=["--python-version=3.6"],
        warning="You set `--python-version` in the `--mypy-args` option",
    )
    maybe_assert_configured(
        has_config=True,
        args=["--py2", "--python-version=3.6"],
        warning=softwrap(
            """
            You set `python_version` in mypy.ini (which is used because of either config
            discovery or the `[mypy].config` option) and you set `--py2` in the `--mypy-args`
            option and you set `--python-version` in the `--mypy-args` option.
            """
        ),
    )
    maybe_assert_configured(has_config=False, args=[])


def test_first_party_plugins(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "BUILD": dedent(
                """\
                python_requirement(name='mypy', requirements=['mypy==0.81'])
                python_requirement(name='colors', requirements=['ansicolors'])
                """
            ),
            "mypy-plugins/subdir1/util.py": "",
            "mypy-plugins/subdir1/BUILD": "python_sources(dependencies=['mypy-plugins/subdir2'])",
            "mypy-plugins/subdir2/another_util.py": "",
            "mypy-plugins/subdir2/BUILD": "python_sources()",
            "mypy-plugins/plugin.py": "",
            "mypy-plugins/BUILD": dedent(
                """\
                python_sources(
                    dependencies=['//:mypy', '//:colors', "mypy-plugins/subdir1"]
                )
                """
            ),
        }
    )
    rule_runner.set_options(
        [
            "--source-root-patterns=mypy-plugins",
            "--mypy-source-plugins=mypy-plugins/plugin.py",
        ],
        env_inherit={"PATH", "PYENV_ROOT", "HOME"},
    )
    first_party_plugins = rule_runner.request(MyPyFirstPartyPlugins, [])
    assert first_party_plugins.requirement_strings == FrozenOrderedSet(["ansicolors", "mypy==0.81"])
    assert (
        first_party_plugins.sources_digest
        == rule_runner.make_snapshot(
            {
                "mypy-plugins/plugin.py": "",
                "mypy-plugins/subdir1/util.py": "",
                "mypy-plugins/subdir2/another_util.py": "",
            }
        ).digest
    )
    assert first_party_plugins.source_roots == ("mypy-plugins",)


@skip_unless_python39_present
def test_setup_lockfile_interpreter_constraints(rule_runner: RuleRunner) -> None:
    global_constraint = "==3.9.*"

    def assert_lockfile_request(
        build_file: str,
        expected_ics: list[str],
        *,
        extra_expected_requirements: list[str] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        rule_runner.write_files({"project/BUILD": build_file, "project/f.py": ""})
        rule_runner.set_options(
            ["--mypy-lockfile=lockfile.txt", *(extra_args or [])],
            env={"PANTS_PYTHON_INTERPRETER_CONSTRAINTS": f"['{global_constraint}']"},
            env_inherit={"PATH", "PYENV_ROOT", "HOME"},
        )
        lockfile_request = rule_runner.request(GeneratePythonLockfile, [MyPyLockfileSentinel()])
        assert lockfile_request.interpreter_constraints == InterpreterConstraints(expected_ics)
        assert lockfile_request.requirements == FrozenOrderedSet(
            [
                MyPy.default_version,
                *MyPy.default_extra_requirements,
                *(extra_expected_requirements or ()),
            ]
        )

    # If all code is Py38+, use those constraints. Otherwise, use subsystem constraints.
    assert_lockfile_request("python_sources()", [global_constraint])
    assert_lockfile_request("python_sources(interpreter_constraints=['==3.9.*'])", ["==3.9.*"])
    assert_lockfile_request(
        "python_sources(interpreter_constraints=['==3.8.*', '==3.9.*'])", ["==3.8.*", "==3.9.*"]
    )

    assert_lockfile_request(
        "python_sources(interpreter_constraints=['>=3.6'])",
        MyPy.default_interpreter_constraints,
    )
    assert_lockfile_request(
        dedent(
            """\
            python_sources(name='t1', interpreter_constraints=['>=3.6'])
            python_sources(name='t2', interpreter_constraints=['==3.8.*'])
            """
        ),
        MyPy.default_interpreter_constraints,
    )

    # If no Python targets in repo, fall back to global Python constraint.
    assert_lockfile_request("target()", [global_constraint])

    # Ignore targets that are skipped.
    assert_lockfile_request(
        dedent(
            """\
            python_sources(name='a', interpreter_constraints=['==3.8.*'])
            python_sources(name='b', interpreter_constraints=['>=3.6.*'], skip_mypy=True)
            """
        ),
        ["==3.8.*"],
    )

    # Also consider transitive deps. They should be ANDed within each python_tests's transitive
    # closure like normal, but then ORed across each python_tests closure.
    assert_lockfile_request(
        dedent(
            """\
            python_sources(name='lib1', interpreter_constraints=['>=3.8'], skip_mypy=True)
            python_sources(name='lib2', dependencies=[":lib1"], interpreter_constraints=['==3.9.*'])
            """
        ),
        ["==3.9.*"],
    )
    assert_lockfile_request(
        dedent(
            """\
            python_sources(name='lib1', interpreter_constraints=['==2.7.*', '>=3.6'], skip_mypy=True)
            python_sources(name='lib2', dependencies=[":lib1"], interpreter_constraints=['==2.7.*'])

            python_sources(name='lib3', interpreter_constraints=['>=3.8'], skip_mypy=True)
            python_sources(name='lib4', dependencies=[":lib3"], interpreter_constraints=['==3.9.*'])
            """
        ),
        MyPy.default_interpreter_constraints,
    )

    # Check that source_plugins are included, even if they aren't checked directly.
    assert_lockfile_request(
        dedent(
            """\
            python_sources(
                dependencies=[":thirdparty"],
                skip_mypy=True,
            )
            python_requirement(name="thirdparty", requirements=["ansicolors"])
            """
        ),
        [global_constraint],
        extra_args=["--mypy-source-plugins=project"],
        extra_expected_requirements=["ansicolors"],
    )


def test_setup_extra_type_stubs_lockfile_interpreter_constraints(rule_runner: RuleRunner) -> None:
    global_constraint = "==3.9.*"

    def assert_lockfile_request(build_file: str, expected_ics: list[str]) -> None:
        rule_runner.write_files({"project/BUILD": build_file, "project/f.py": ""})
        rule_runner.set_options(
            ["--mypy-extra-type-stubs-lockfile=lockfile.txt"],
            env={"PANTS_PYTHON_INTERPRETER_CONSTRAINTS": f"['{global_constraint}']"},
            env_inherit={"PATH", "PYENV_ROOT", "HOME"},
        )
        lockfile_request = rule_runner.request(
            GeneratePythonLockfile, [MyPyExtraTypeStubsLockfileSentinel()]
        )
        assert lockfile_request.interpreter_constraints == InterpreterConstraints(expected_ics)

    assert_lockfile_request("python_sources()", [global_constraint])
    assert_lockfile_request("python_sources(interpreter_constraints=['==2.7.*'])", ["==2.7.*"])
    assert_lockfile_request(
        "python_sources(interpreter_constraints=['==2.7.*', '==3.8.*'])", ["==2.7.*", "==3.8.*"]
    )

    # If no Python targets in repo, fall back to global [python] constraints.
    assert_lockfile_request("target()", [global_constraint])

    # Ignore targets that are skipped.
    assert_lockfile_request(
        dedent(
            """\
            python_sources(name='a', interpreter_constraints=['==2.7.*'])
            python_sources(name='b', interpreter_constraints=['==3.8.*'], skip_mypy=True)
            """
        ),
        ["==2.7.*"],
    )

    # If there are multiple distinct ICs in the repo, we OR them because the lockfile needs to be
    # compatible with every target.
    assert_lockfile_request(
        dedent(
            """\
            python_sources(name='a', interpreter_constraints=['==2.7.*'])
            python_sources(name='b', interpreter_constraints=['==3.8.*'])
            """
        ),
        ["==2.7.*", "==3.8.*"],
    )
    assert_lockfile_request(
        dedent(
            """\
            python_sources(name='a', interpreter_constraints=['==2.7.*', '==3.8.*'])
            python_sources(name='b', interpreter_constraints=['>=3.8'])
            """
        ),
        ["==2.7.*", "==3.8.*", ">=3.8"],
    )
    assert_lockfile_request(
        dedent(
            """\
            python_sources(name='a')
            python_sources(name='b', interpreter_constraints=['==2.7.*'])
            python_sources(name='c', interpreter_constraints=['>=3.8'])
            """
        ),
        ["==2.7.*", global_constraint, ">=3.8"],
    )
