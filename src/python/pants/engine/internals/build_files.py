# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import builtins
import itertools
import os.path
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, cast

from pants.build_graph.address import (
    Address,
    AddressInput,
    BuildFileAddress,
    BuildFileAddressRequest,
    MaybeAddress,
    ResolveError,
)
from pants.engine.engine_aware import EngineAwareParameter
from pants.engine.fs import DigestContents, GlobMatchErrorBehavior, PathGlobs, Paths
from pants.engine.internals.defaults import BuildFileDefaults, BuildFileDefaultsParserState
from pants.engine.internals.dep_rules import (
    BuildFileDependencyRules,
    DependencyRuleAction,
    MaybeBuildFileDependencyRulesImplementation,
)
from pants.engine.internals.mapper import AddressFamily, AddressMap
from pants.engine.internals.parser import BuildFilePreludeSymbols, Parser, error_on_imports
from pants.engine.internals.synthetic_targets import (
    SyntheticAddressMaps,
    SyntheticAddressMapsRequest,
)
from pants.engine.internals.target_adaptor import TargetAdaptor, TargetAdaptorRequest
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import (
    DependenciesRuleAction,
    DependenciesRuleActionRequest,
    RegisteredTargetTypes,
)
from pants.engine.unions import UnionMembership
from pants.option.global_options import GlobalOptions
from pants.util.frozendict import FrozenDict
from pants.util.strutil import softwrap


@dataclass(frozen=True)
class BuildFileOptions:
    patterns: tuple[str, ...]
    ignores: tuple[str, ...] = ()
    prelude_globs: tuple[str, ...] = ()


@rule
def extract_build_file_options(global_options: GlobalOptions) -> BuildFileOptions:
    return BuildFileOptions(
        patterns=global_options.build_patterns,
        ignores=global_options.build_ignore,
        prelude_globs=global_options.build_file_prelude_globs,
    )


@rule(desc="Expand macros")
async def evaluate_preludes(
    build_file_options: BuildFileOptions,
    parser: Parser,
) -> BuildFilePreludeSymbols:
    prelude_digest_contents = await Get(
        DigestContents,
        PathGlobs(
            build_file_options.prelude_globs,
            glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
        ),
    )
    globals: dict[str, Any] = {
        **{name: getattr(builtins, name) for name in dir(builtins) if name.endswith("Error")},
        # Ensure the globals for each prelude includes the builtin symbols (E.g. `python_sources`)
        **parser.builtin_symbols,
    }
    locals: dict[str, Any] = {}
    for file_content in prelude_digest_contents:
        try:
            file_content_str = file_content.content.decode()
            content = compile(file_content_str, file_content.path, "exec")
            exec(content, globals, locals)
        except Exception as e:
            raise Exception(f"Error parsing prelude file {file_content.path}: {e}")
        error_on_imports(file_content_str, file_content.path)
    # __builtins__ is a dict, so isn't hashable, and can't be put in a FrozenDict.
    # Fortunately, we don't care about it - preludes should not be able to override builtins, so we just pop it out.
    # TODO: Give a nice error message if a prelude tries to set a expose a non-hashable value.
    locals.pop("__builtins__", None)
    # Ensure preludes can reference each other by populating the shared globals object with references
    # to the other symbols
    globals.update(locals)
    return BuildFilePreludeSymbols(FrozenDict(locals))


@rule
async def maybe_resolve_address(address_input: AddressInput) -> MaybeAddress:
    # Determine the type of the path_component of the input.
    if address_input.path_component:
        paths = await Get(Paths, PathGlobs(globs=(address_input.path_component,)))
        is_file, is_dir = bool(paths.files), bool(paths.dirs)
    else:
        # It is an address in the root directory.
        is_file, is_dir = False, True

    if is_file:
        return MaybeAddress(address_input.file_to_address())
    if is_dir:
        return MaybeAddress(address_input.dir_to_address())
    spec = address_input.path_component
    if address_input.target_component:
        spec += f":{address_input.target_component}"
    return MaybeAddress(
        ResolveError(
            softwrap(
                f"""
                The file or directory '{address_input.path_component}' does not exist on disk in
                the workspace, so the address '{spec}' from {address_input.description_of_origin}
                cannot be resolved.
                """
            )
        )
    )


@rule
async def resolve_address(maybe_address: MaybeAddress) -> Address:
    if isinstance(maybe_address.val, ResolveError):
        raise maybe_address.val
    return maybe_address.val


@dataclass(frozen=True)
class AddressFamilyDir(EngineAwareParameter):
    """The directory to find addresses for.

    This does _not_ recurse into subdirectories.
    """

    path: str

    def debug_hint(self) -> str:
        return self.path


@dataclass(frozen=True)
class OptionalAddressFamily:
    path: str
    address_family: AddressFamily | None = None


@rule
async def ensure_address_family(request: OptionalAddressFamily) -> AddressFamily:
    if request.address_family is None:
        raise ResolveError(f"Directory '{request.path}' does not contain any BUILD files.")
    return request.address_family


@rule(desc="Search for addresses in BUILD files")
async def parse_address_family(
    parser: Parser,
    build_file_options: BuildFileOptions,
    prelude_symbols: BuildFilePreludeSymbols,
    directory: AddressFamilyDir,
    registered_target_types: RegisteredTargetTypes,
    union_membership: UnionMembership,
    maybe_build_file_dependency_rules_implementation: MaybeBuildFileDependencyRulesImplementation,
) -> OptionalAddressFamily:
    """Given an AddressMapper and a directory, return an AddressFamily.

    The AddressFamily may be empty, but it will not be None.
    """
    digest_contents, all_synthetic_address_maps = await MultiGet(
        Get(
            DigestContents,
            PathGlobs(
                globs=(
                    *(os.path.join(directory.path, p) for p in build_file_options.patterns),
                    *(f"!{p}" for p in build_file_options.ignores),
                )
            ),
        ),
        Get(SyntheticAddressMaps, SyntheticAddressMapsRequest(directory.path)),
    )
    synthetic_address_maps = tuple(itertools.chain(all_synthetic_address_maps))
    if not digest_contents and not synthetic_address_maps:
        return OptionalAddressFamily(directory.path)

    defaults = BuildFileDefaults({})
    dependents_rules: BuildFileDependencyRules | None = None
    dependencies_rules: BuildFileDependencyRules | None = None
    parent_dirs = tuple(PurePath(directory.path).parents)
    if parent_dirs:
        maybe_parents = await MultiGet(
            Get(OptionalAddressFamily, AddressFamilyDir(str(parent_dir)))
            for parent_dir in parent_dirs
        )
        for maybe_parent in maybe_parents:
            if maybe_parent.address_family is not None:
                family = maybe_parent.address_family
                defaults = family.defaults
                dependents_rules = family.dependents_rules
                dependencies_rules = family.dependencies_rules
                break

    defaults_parser_state = BuildFileDefaultsParserState.create(
        directory.path, defaults, registered_target_types, union_membership
    )
    build_file_dependency_rules_class = (
        maybe_build_file_dependency_rules_implementation.build_file_dependency_rules_class
    )
    if build_file_dependency_rules_class is not None:
        dependents_rules_parser_state = build_file_dependency_rules_class.create_parser_state(
            directory.path,
            dependents_rules,
        )
        dependencies_rules_parser_state = build_file_dependency_rules_class.create_parser_state(
            directory.path,
            dependencies_rules,
        )
    else:
        dependents_rules_parser_state = None
        dependencies_rules_parser_state = None

    address_maps = [
        AddressMap.parse(
            fc.path,
            fc.content.decode(),
            parser,
            prelude_symbols,
            defaults_parser_state,
            dependents_rules_parser_state,
            dependencies_rules_parser_state,
        )
        for fc in digest_contents
    ]

    # Freeze defaults and dependency rules
    frozen_defaults = defaults_parser_state.get_frozen_defaults()
    frozen_dependents_rules = cast(
        "BuildFileDependencyRules | None",
        dependents_rules_parser_state
        and dependents_rules_parser_state.get_frozen_dependency_rules(),
    )
    frozen_dependencies_rules = cast(
        "BuildFileDependencyRules | None",
        dependencies_rules_parser_state
        and dependencies_rules_parser_state.get_frozen_dependency_rules(),
    )

    # Process synthetic targets.
    for address_map in address_maps:
        for synthetic in synthetic_address_maps:
            synthetic.process_declared_targets(address_map)
            synthetic.apply_defaults(frozen_defaults)

    return OptionalAddressFamily(
        directory.path,
        AddressFamily.create(
            spec_path=directory.path,
            address_maps=(*address_maps, *synthetic_address_maps),
            defaults=frozen_defaults,
            dependents_rules=frozen_dependents_rules,
            dependencies_rules=frozen_dependencies_rules,
        ),
    )


@rule
async def find_build_file(request: BuildFileAddressRequest) -> BuildFileAddress:
    address = request.address
    address_family = await Get(AddressFamily, AddressFamilyDir(address.spec_path))
    owning_address = address.maybe_convert_to_target_generator()
    if address_family.get_target_adaptor(owning_address) is None:
        raise ResolveError.did_you_mean(
            owning_address,
            description_of_origin=request.description_of_origin,
            known_names=address_family.target_names,
            namespace=address_family.namespace,
        )
    bfa = next(
        build_file_address
        for build_file_address in address_family.build_file_addresses
        if build_file_address.address == owning_address
    )
    return BuildFileAddress(address, bfa.rel_path) if address.is_generated_target else bfa


def _get_target_adaptor(
    address: Address, address_family: AddressFamily, description_of_origin: str
) -> TargetAdaptor:
    target_adaptor = address_family.get_target_adaptor(address)
    if target_adaptor is None:
        raise ResolveError.did_you_mean(
            address,
            description_of_origin=description_of_origin,
            known_names=address_family.target_names,
            namespace=address_family.namespace,
        )
    return target_adaptor


@rule
async def find_target_adaptor(request: TargetAdaptorRequest) -> TargetAdaptor:
    """Hydrate a TargetAdaptor so that it may be converted into the Target API."""
    address = request.address
    if address.is_generated_target:
        raise AssertionError(
            "Generated targets are not defined in BUILD files, and so do not have "
            f"TargetAdaptors: {request}"
        )
    address_family = await Get(AddressFamily, AddressFamilyDir(address.spec_path))
    target_adaptor = _get_target_adaptor(address, address_family, request.description_of_origin)
    return target_adaptor


@rule
async def get_dependencies_rule_action(
    request: DependenciesRuleActionRequest,
    maybe_build_file_rules_implementation: MaybeBuildFileDependencyRulesImplementation,
) -> DependenciesRuleAction:
    build_file_dependency_rules_class = (
        maybe_build_file_rules_implementation.build_file_dependency_rules_class
    )
    if build_file_dependency_rules_class is None:
        return DependenciesRuleAction.allow_all()

    spec_paths = {address.spec_path for address in (request.address, *request.dependencies)}
    address_families = await MultiGet(
        Get(AddressFamily, AddressFamilyDir(spec_path)) for spec_path in spec_paths
    )
    families = {address_family.namespace: address_family for address_family in address_families}
    origin_family = families[request.address.spec_path]
    origin_target = _get_target_adaptor(
        request.address.maybe_convert_to_target_generator(),
        origin_family,
        request.description_of_origin,
    )
    dependencies_rule: dict[Address, DependencyRuleAction] = {}
    for dependency_address in request.dependencies:
        dependency_family = families[dependency_address.spec_path]
        dependency_target = _get_target_adaptor(
            dependency_address.maybe_convert_to_target_generator(),
            dependency_family,
            f"{request.description_of_origin} on {dependency_address}",
        )
        dependencies_rule[
            dependency_address
        ] = build_file_dependency_rules_class.check_dependency_rules(
            source_adaptor=origin_target,
            source_path=origin_family.namespace,
            dependencies_rules=origin_family.dependencies_rules,
            target_adaptor=dependency_target,
            target_path=dependency_family.namespace,
            dependents_rules=dependency_family.dependents_rules,
        )
    return DependenciesRuleAction(request.address, FrozenDict(dependencies_rule))


def rules():
    return collect_rules()
