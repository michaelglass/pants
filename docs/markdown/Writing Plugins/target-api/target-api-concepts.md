---
title: "Concepts"
slug: "target-api-concepts"
excerpt: "The core concepts of Targets and Fields."
hidden: false
createdAt: "2020-05-07T22:38:43.975Z"
updatedAt: "2021-11-16T02:52:06.072Z"
---
The Target API defines how you interact with targets in your plugin. For example, you would use the Target API to read the `source` / `sources` field of a target to know which files to run on.

The Target API can also be used to add new target types—such as adding support for a new language. Additionally, the Target API can be used to extend existing target types and even declare synthetic targets as if they came from a BUILD file.

Targets and Fields - the core building blocks
---------------------------------------------

### Definition of _target_

As described in [Targets and BUILD files](doc:targets), a _target_ is an _addressable_ set of metadata describing some of your code.

For example, this BUILD file defines a `PythonTestTarget` target with `Address("project", target_name="app_test")`.

```python project/BUILD
python_test(
    name="app_test",
    source="app_test.py",
    timeout=120,
)
```

### Definition of _field_

A _field_ is a single value of metadata belonging to a target, such as `source` and `timeout` above. (`name` is a special thing used to create the `Address`.)

Each field has a Python class that defines its BUILD file alias, data type, and optional settings like default values. For example:

```python example_fields.py
from pants.engine.target import IntField
    
class PythonTestTimeoutField(IntField):
    alias = "timeout"
    default = 60
```

### Target == alias + combination of fields

Alternatively, you can think of a target as simply an alias and a combination of fields:

```python plugin_target_types.py
from pants.engine.target import Dependencies, SingleSourceField, Target, Tags

class CustomTarget(Target):
    alias = "custom_target"
    core_fields = (SingleSourceField, Dependencies, Tags)
```

A target's fields should make sense together. For example, it does not make sense for a `python_source` target to have a `haskell_version` field.

Any unrecognized fields will cause an exception when used in a BUILD file.

### Fields may be reused

Because fields are stand-alone Python classes, the same field definition may be reused across multiple different target types.

For example, many target types have the `source` field.

```python BUILD
resource(
    name="logo",
    source="logo.png",
)

dockerfile(
    name="docker",
    source="Dockerfile",
)
```

This gives you reuse of code ([DRY](https://en.wikipedia.org/wiki/Don't_repeat_yourself)) and is important for your plugin to work with multiple different target types, as explained below.

A Field-Driven API
------------------

Idiomatic Pants plugins do not care about specific target types; they only care that the target type has the right combination of field types that the plugin needs to operate.

For example, the Python formatter Black does not actually care whether you have a `python_source`, `python_test`, or `custom_target` target; all that it cares about is that your target type has the field `PythonSourceField`. 

Targets are only [used by the Rules API](doc:rules-api-and-target-api) to get access to the underlying fields through the methods `.has_field()` and `.get()`:

```python
if target.has_field(PythonSourceField):
    print("My plugin can work on this target.")

timeout_field = target.get(PythonTestTimeoutField)
print(timeout_field.value)
```

This means that when creating new target types, the fields you choose for your target will determine the functionality it has.

Customizing fields through subclassing
--------------------------------------

Often, you may like how a field behaves, but want to make some tweaks. For example, you may want to give a default value to the `SingleSourceField` field.

To modify an existing field, simply subclass it.

```python
from pants.engine.target import SingleSourceField

class DockerSourceField(SingleSourceField):
    default = "Dockerfile"
```

The `Target` methods `.has_field()` and `.get()` understand this subclass relationship, as follows:

```python
>>> docker_tgt.has_field(DockerSourceField)
True
>>> docker_tgt.has_field(SingleSourceField)
True
>>> python_test_tgt.has_field(DockerSourceField)
False
>>> python_test_tgt.has_field(SingleSourceField)
True
```

This subclass mechanism is key to how the Target API behaves:

- You can use subclasses of fields—along with `Target.has_field()`— to filter out irrelevant targets. For example, the Black formatter doesn't work with any plain `SourcesField` field; it needs `PythonSourceField`. The Python test runner is even more specific: it needs `PythonTestSourceField`.
- You can create custom fields and custom target types that still work with pre-existing functionality. For example, you can subclass `PythonSourceField` to create `DjangoSourceField`, and the Black formatter will still be able to operate on your target.


Synthetic Targets API
---------------------

Normally targets are declared in BUILD files to provide meta data about the project's sources and artifacts etc. Occassionally there may be instances of project meta data that is not served well by being declared explicitly in a BUILD file, for instance if the meta data itself is inferred from other sources of information. For these cases, there is a Target API for declaring synthetic targets, that is targets that are not declared in a BUILD file on disk but instead come from a Plugin's rule.

### Example

To declare synthetic targets from a Plugin, first subclass the `SyntheticTargetsRequest` union type and register it as a union member with `UnionRule(SyntheticTargetsRequest, SubclassedType)`. Secondly there needs to be a rule that takes this union member type as input and returns a `SyntheticAddressMaps`.

    from dataclasses import dataclass
    from pants.engine.internals.synthetic_targets import (
        SyntheticAddressMaps,
        SyntheticTargetsRequest,
    )
    from pants.engine.internals.target_adaptor import TargetAdaptor
    from pants.engine.unions import UnionRule
    from pants.engine.rules import collect_rules, rule


    @dataclass(frozen=True)
    class SyntheticExampleTargetsRequest(SyntheticTargetsRequest):
        pass


    @rule
    async def example_synthetic_targets(request: SyntheticExampleTargetsRequest) -> SyntheticAddressMaps:
        return SyntheticAddressMaps.for_targets_request(
            request,
            [
                (
                  "BUILD.synthetic-example",
                  (
                    TargetAdaptor("<target-type>", "<name>", **target_field_values),
                    ...
                  ),
                ),
                ...
            ]
        )


    def rules():
        return (
            *collect_rules(),
            UnionRule(SyntheticTargetsRequest, SyntheticExampleTargetsRequest),
            ...
        )

### Register synthetic targets per directory or globally

Depending on the source information for the synthetic targets, it may make sense to either register them with a request per directory or for all directories at once with a single request.

If the source information is derived from parsing files from the project source tree, then go with the per directory request style (which also is the default mode of operation), where as if the information is known up-front without consulting the project sources or otherwise does not depend on which directory is being parsed for BUILD files, it may be more performant to return all synthetic targets in a single request.

The mode of operation is declared per union member (i.e. on the subclass of the `SyntheticTargetsRequest` class) by providing a default value to the `path` field:

    @dataclass(frozen=True)
    class SyntheticExamplePerDirectoryTargetsRequest(SyntheticTargetsRequest):
        path: str = SyntheticTargetsRequest.REQUEST_TARGETS_PER_DIRECTORY

    @dataclass(frozen=True)
    class SyntheticExampleAllTargetsAtOnceRequest(SyntheticTargetsRequest):
        path: str = SyntheticTargetsRequest.SINGLE_REQUEST_FOR_ALL_TARGETS

Any other default value for `path` should be considered invalid and yield undefined behaviour. (that is it may change without notice in future versions of Pants.)

During rule execution, the `path` field of the `request` instance will hold the value for the path currently being parsed in case of a per directory mode of operation otherwise it will be `SyntheticTargetsRequest.SINGLE_REQUEST_FOR_ALL_TARGETS`.
