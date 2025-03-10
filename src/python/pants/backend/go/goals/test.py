# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Sequence

from pants.backend.go.subsystems.gotest import GoTestSubsystem
from pants.backend.go.target_types import (
    GoPackageSourcesField,
    GoTestExtraEnvVarsField,
    GoTestTimeoutField,
    SkipGoTestsField,
)
from pants.backend.go.util_rules.build_opts import GoBuildOptions, GoBuildOptionsFromTargetRequest
from pants.backend.go.util_rules.build_pkg import (
    BuildGoPackageRequest,
    BuiltGoPackage,
    FallibleBuildGoPackageRequest,
    FallibleBuiltGoPackage,
)
from pants.backend.go.util_rules.build_pkg_target import BuildGoPackageTargetRequest
from pants.backend.go.util_rules.coverage import (
    GenerateCoverageSetupCodeRequest,
    GenerateCoverageSetupCodeResult,
    GoCoverageConfig,
    GoCoverageData,
)
from pants.backend.go.util_rules.first_party_pkg import (
    FallibleFirstPartyPkgAnalysis,
    FallibleFirstPartyPkgDigest,
    FirstPartyPkgAnalysisRequest,
    FirstPartyPkgDigestRequest,
)
from pants.backend.go.util_rules.goroot import GoRoot
from pants.backend.go.util_rules.import_analysis import ImportConfig, ImportConfigRequest
from pants.backend.go.util_rules.link import LinkedGoBinary, LinkGoBinaryRequest
from pants.backend.go.util_rules.tests_analysis import GeneratedTestMain, GenerateTestMainRequest
from pants.core.goals.test import (
    TestDebugAdapterRequest,
    TestDebugRequest,
    TestExtraEnv,
    TestFieldSet,
    TestRequest,
    TestResult,
    TestSubsystem,
)
from pants.core.target_types import FileSourceField
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.engine.env_vars import EnvironmentVars, EnvironmentVarsRequest
from pants.engine.fs import EMPTY_FILE_DIGEST, AddPrefix, Digest, MergeDigests
from pants.engine.internals.native_engine import EMPTY_DIGEST
from pants.engine.process import FallibleProcessResult, Process, ProcessCacheScope
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import Dependencies, DependenciesRequest, SourcesField, Target, Targets
from pants.engine.unions import UnionRule
from pants.util.logging import LogLevel
from pants.util.ordered_set import FrozenOrderedSet

# Known options to Go test binaries. Only these options will be transformed by `transform_test_args`.
# The bool value represents whether the option is expected to take a value or not.
# To regenerate this list, run `go run ./gentestflags.go` and copy the output below.
TEST_FLAGS = {
    "bench": True,
    "benchmem": False,
    "benchtime": True,
    "blockprofile": True,
    "blockprofilerate": True,
    "count": True,
    "coverprofile": True,
    "cpu": True,
    "cpuprofile": True,
    "failfast": False,
    "fuzz": True,
    "fuzzminimizetime": True,
    "fuzztime": True,
    "list": True,
    "memprofile": True,
    "memprofilerate": True,
    "mutexprofile": True,
    "mutexprofilefraction": True,
    "outputdir": True,
    "parallel": True,
    "run": True,
    "short": False,
    "shuffle": True,
    "timeout": True,
    "trace": True,
    "v": False,
}


@dataclass(frozen=True)
class GoTestFieldSet(TestFieldSet):
    required_fields = (GoPackageSourcesField,)

    sources: GoPackageSourcesField
    dependencies: Dependencies
    timeout: GoTestTimeoutField
    extra_env_vars: GoTestExtraEnvVarsField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipGoTestsField).value


class GoTestRequest(TestRequest):
    tool_subsystem = GoTestSubsystem
    field_set_type = GoTestFieldSet


def transform_test_args(args: Sequence[str], timeout_field_value: int | None) -> tuple[str, ...]:
    result = []
    i = 0
    next_arg_is_option_value = False
    timeout_is_set = False
    while i < len(args):
        arg = args[i]
        i += 1

        # If this argument is an option value, then append it to the result and continue to next
        # argument.
        if next_arg_is_option_value:
            result.append(arg)
            next_arg_is_option_value = False
            continue

        # Non-arguments stop option processing.
        if arg[0] != "-":
            result.append(arg)
            break

        # Stop processing since "-" is a non-argument and "--" is terminator.
        if arg == "-" or arg == "--":
            result.append(arg)
            break

        start_index = 2 if arg[1] == "-" else 1
        equals_index = arg.find("=", start_index)
        if equals_index != -1:
            arg_name = arg[start_index:equals_index]
            option_value = arg[equals_index:]
        else:
            arg_name = arg[start_index:]
            option_value = ""

        if arg_name in TEST_FLAGS:
            if arg_name == "timeout":
                timeout_is_set = True

            rewritten_arg = f"{arg[0:start_index]}test.{arg_name}{option_value}"
            result.append(rewritten_arg)

            no_opt_provided = TEST_FLAGS[arg_name] and option_value == ""
            if no_opt_provided:
                next_arg_is_option_value = True
        else:
            result.append(arg)

    if not timeout_is_set and timeout_field_value is not None:
        result.append(f"-test.timeout={timeout_field_value}s")

    result.extend(args[i:])
    return tuple(result)


@rule(desc="Test with Go", level=LogLevel.DEBUG)
async def run_go_tests(
    batch: GoTestRequest.Batch[GoTestFieldSet, Any],
    test_subsystem: TestSubsystem,
    go_test_subsystem: GoTestSubsystem,
    test_extra_env: TestExtraEnv,
    goroot: GoRoot,
) -> TestResult:
    field_set = batch.single_element

    build_opts = await Get(GoBuildOptions, GoBuildOptionsFromTargetRequest(field_set.address))

    maybe_pkg_analysis, maybe_pkg_digest, dependencies = await MultiGet(
        Get(
            FallibleFirstPartyPkgAnalysis,
            FirstPartyPkgAnalysisRequest(field_set.address, build_opts=build_opts),
        ),
        Get(
            FallibleFirstPartyPkgDigest,
            FirstPartyPkgDigestRequest(field_set.address, build_opts=build_opts),
        ),
        Get(Targets, DependenciesRequest(field_set.dependencies)),
    )

    def compilation_failure(exit_code: int, stdout: str | None, stderr: str | None) -> TestResult:
        return TestResult(
            exit_code=exit_code,
            stdout=stdout or "",
            stderr=stderr or "",
            stdout_digest=EMPTY_FILE_DIGEST,
            stderr_digest=EMPTY_FILE_DIGEST,
            addresses=(field_set.address,),
            output_setting=test_subsystem.output,
            result_metadata=None,
        )

    if maybe_pkg_analysis.analysis is None:
        assert maybe_pkg_analysis.stderr is not None
        return compilation_failure(maybe_pkg_analysis.exit_code, None, maybe_pkg_analysis.stderr)
    if maybe_pkg_digest.pkg_digest is None:
        assert maybe_pkg_digest.stderr is not None
        return compilation_failure(maybe_pkg_digest.exit_code, None, maybe_pkg_digest.stderr)

    pkg_analysis = maybe_pkg_analysis.analysis
    pkg_digest = maybe_pkg_digest.pkg_digest
    import_path = pkg_analysis.import_path

    testmain = await Get(
        GeneratedTestMain,
        GenerateTestMainRequest(
            digest=pkg_digest.digest,
            test_paths=FrozenOrderedSet(
                os.path.join(".", pkg_analysis.dir_path, name)
                for name in pkg_analysis.test_go_files
            ),
            xtest_paths=FrozenOrderedSet(
                os.path.join(".", pkg_analysis.dir_path, name)
                for name in pkg_analysis.xtest_go_files
            ),
            import_path=import_path,
            register_cover=test_subsystem.use_coverage,
            address=field_set.address,
        ),
    )

    if testmain.failed_exit_code_and_stderr is not None:
        _exit_code, _stderr = testmain.failed_exit_code_and_stderr
        return compilation_failure(_exit_code, None, _stderr)

    if not testmain.has_tests and not testmain.has_xtests:
        return TestResult.skip(field_set.address, output_setting=test_subsystem.output)

    coverage_config: GoCoverageConfig | None = None
    if test_subsystem.use_coverage:
        coverage_config = GoCoverageConfig(cover_mode=go_test_subsystem.coverage_mode)

    # Construct the build request for the package under test.
    maybe_test_pkg_build_request = await Get(
        FallibleBuildGoPackageRequest,
        BuildGoPackageTargetRequest(
            field_set.address,
            for_tests=True,
            coverage_config=coverage_config,
            build_opts=build_opts,
        ),
    )
    if maybe_test_pkg_build_request.request is None:
        assert maybe_test_pkg_build_request.stderr is not None
        return compilation_failure(
            maybe_test_pkg_build_request.exit_code, None, maybe_test_pkg_build_request.stderr
        )
    test_pkg_build_request = maybe_test_pkg_build_request.request

    # TODO: Eventually support adding coverage to non-test packages. Those other packages will need to be
    # added to `main_direct_deps` and to the coverage setup in the testmain.
    main_direct_deps = [test_pkg_build_request]

    if testmain.has_xtests:
        # Build a synthetic package for xtests where the import path is the same as the package under test
        # but with "_test" appended.
        maybe_xtest_pkg_build_request = await Get(
            FallibleBuildGoPackageRequest,
            BuildGoPackageTargetRequest(
                field_set.address,
                for_xtests=True,
                coverage_config=coverage_config,
                build_opts=build_opts,
            ),
        )
        if maybe_xtest_pkg_build_request.request is None:
            assert maybe_xtest_pkg_build_request.stderr is not None
            return compilation_failure(
                maybe_xtest_pkg_build_request.exit_code, None, maybe_xtest_pkg_build_request.stderr
            )
        xtest_pkg_build_request = maybe_xtest_pkg_build_request.request
        main_direct_deps.append(xtest_pkg_build_request)

    # Generate coverage setup code for the test main if coverage is enabled.
    #
    # Note: Go coverage analysis is a form of codegen. It rewrites the Go source code at issue to include explicit
    # references to "coverage variables" which contain the statement counts for coverage analysis. The test main
    # generated for a Go test binary has to explicitly reference the coverage variables generated by this codegen and
    # register them with the coverage runtime.
    coverage_setup_digest = EMPTY_DIGEST
    coverage_setup_files = []
    if coverage_config is not None:
        # Build the `main_direct_deps` when in coverage mode to obtain the "coverage variables" for those packages.
        built_main_direct_deps = await MultiGet(
            Get(BuiltGoPackage, BuildGoPackageRequest, build_req) for build_req in main_direct_deps
        )
        coverage_metadata = [
            pkg.coverage_metadata for pkg in built_main_direct_deps if pkg.coverage_metadata
        ]
        coverage_setup_result = await Get(
            GenerateCoverageSetupCodeResult,
            GenerateCoverageSetupCodeRequest(
                packages=FrozenOrderedSet(coverage_metadata),
                cover_mode=go_test_subsystem.coverage_mode,
            ),
        )
        coverage_setup_digest = coverage_setup_result.digest
        coverage_setup_files = [GenerateCoverageSetupCodeResult.PATH]

    testmain_input_digest = await Get(
        Digest, MergeDigests([testmain.digest, coverage_setup_digest])
    )

    # Generate the synthetic main package which imports the test and/or xtest packages.
    maybe_built_main_pkg = await Get(
        FallibleBuiltGoPackage,
        BuildGoPackageRequest(
            import_path="main",
            pkg_name="main",
            digest=testmain_input_digest,
            dir_path="",
            build_opts=build_opts,
            go_files=(GeneratedTestMain.TEST_MAIN_FILE, *coverage_setup_files),
            s_files=(),
            direct_dependencies=tuple(main_direct_deps),
            minimum_go_version=pkg_analysis.minimum_go_version,
        ),
    )
    if maybe_built_main_pkg.output is None:
        assert maybe_built_main_pkg.stderr is not None
        return compilation_failure(
            maybe_built_main_pkg.exit_code, maybe_built_main_pkg.stdout, maybe_built_main_pkg.stderr
        )
    built_main_pkg = maybe_built_main_pkg.output

    main_pkg_a_file_path = built_main_pkg.import_paths_to_pkg_a_files["main"]
    import_config = await Get(
        ImportConfig, ImportConfigRequest(built_main_pkg.import_paths_to_pkg_a_files)
    )
    linker_input_digest = await Get(
        Digest, MergeDigests([built_main_pkg.digest, import_config.digest])
    )
    binary = await Get(
        LinkedGoBinary,
        LinkGoBinaryRequest(
            input_digest=linker_input_digest,
            archives=(main_pkg_a_file_path,),
            import_config_path=import_config.CONFIG_PATH,
            output_filename="./test_runner",  # TODO: Name test binary the way that `go` does?
            description=f"Link Go test binary for {field_set.address}",
        ),
    )

    # To emulate Go's test runner, we set the working directory to the path of the `go_package`.
    # This allows tests to open dependencies on `file` targets regardless of where they are
    # located. See https://dave.cheney.net/2016/05/10/test-fixtures-in-go.
    working_dir = field_set.address.spec_path
    field_set_extra_env_get = Get(
        EnvironmentVars, EnvironmentVarsRequest(field_set.extra_env_vars.value or ())
    )
    binary_with_prefix, files_sources, field_set_extra_env = await MultiGet(
        Get(Digest, AddPrefix(binary.digest, working_dir)),
        Get(
            SourceFiles,
            SourceFilesRequest(
                (dep.get(SourcesField) for dep in dependencies),
                for_sources_types=(FileSourceField,),
                enable_codegen=True,
            ),
        ),
        field_set_extra_env_get,
    )
    test_input_digest = await Get(
        Digest, MergeDigests((binary_with_prefix, files_sources.snapshot.digest))
    )

    extra_env = {
        **test_extra_env.env,
        # NOTE: field_set_extra_env intentionally after `test_extra_env` to allow overriding within
        # `go_package`.
        **field_set_extra_env,
    }

    # Add $GOROOT/bin to the PATH just as `go test` does.
    # See https://github.com/golang/go/blob/master/src/cmd/go/internal/test/test.go#L1384
    goroot_bin_path = os.path.join(goroot.path, "bin")
    if "PATH" in extra_env:
        extra_env["PATH"] = f"{goroot_bin_path}:{extra_env['PATH']}"
    else:
        extra_env["PATH"] = goroot_bin_path

    cache_scope = (
        ProcessCacheScope.PER_SESSION if test_subsystem.force else ProcessCacheScope.SUCCESSFUL
    )

    maybe_cover_args = []
    maybe_cover_output_file = []
    if test_subsystem.use_coverage:
        maybe_cover_args = ["-test.coverprofile=cover.out"]
        maybe_cover_output_file = ["cover.out"]

    test_run_args = [
        "./test_runner",
        *transform_test_args(
            go_test_subsystem.args,
            field_set.timeout.calculate_from_global_options(test_subsystem),
        ),
        *maybe_cover_args,
    ]

    result = await Get(
        FallibleProcessResult,
        Process(
            argv=test_run_args,
            env=extra_env,
            input_digest=test_input_digest,
            description=f"Run Go tests: {field_set.address}",
            cache_scope=cache_scope,
            working_directory=working_dir,
            output_files=maybe_cover_output_file,
            level=LogLevel.DEBUG,
        ),
    )

    coverage_data: GoCoverageData | None = None
    if test_subsystem.use_coverage:
        coverage_data = GoCoverageData(
            coverage_digest=result.output_digest,
            import_path=import_path,
            sources_digest=pkg_digest.digest,
            sources_dir_path=pkg_analysis.dir_path,
        )

    return TestResult.from_fallible_process_result(
        process_result=result,
        address=field_set.address,
        output_setting=test_subsystem.output,
        coverage_data=coverage_data,
    )


@rule
async def generate_go_tests_debug_request(_: GoTestRequest.Batch) -> TestDebugRequest:
    raise NotImplementedError("This is a stub.")


@rule
async def generate_go_tests_debug_adapter_request(
    _: GoTestRequest.Batch,
) -> TestDebugAdapterRequest:
    raise NotImplementedError("This is a stub.")


def rules():
    return [
        *collect_rules(),
        UnionRule(TestFieldSet, GoTestFieldSet),
        *GoTestRequest.rules(),
    ]
