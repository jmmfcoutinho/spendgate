"""YAML reading: C loader when available, duplicate mapping keys refused.

PyYAML keeps the last of two equal keys without a word, which in a spend-control file turns a
copy-paste slip into a silently different budget. The loader below raises instead.
"""

import yaml
from yaml.constructor import ConstructorError

_BaseLoader: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class StrictLoader(_BaseLoader):  # type: ignore[misc,valid-type]  # base picked at import time
    """SafeLoader (C-accelerated when libyaml is present) that rejects duplicate keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:  # unhashable key: the parent constructor reports it
                continue
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )  # fmt: skip
        return super().construct_mapping(node, deep)  # type: ignore[no-any-return]


def load_yaml_bytes(data: bytes) -> object:
    """Parse one document. Raises yaml.YAMLError; the caller words the message (no snippets)."""
    return yaml.load(data, Loader=StrictLoader)  # noqa: S506 - SafeLoader subclass
