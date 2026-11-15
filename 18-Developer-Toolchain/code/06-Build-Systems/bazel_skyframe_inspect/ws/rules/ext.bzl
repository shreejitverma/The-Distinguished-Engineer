"""A module extension that reads tags from the module graph and defines one repo."""

def _facts_repo_impl(rctx):
    rctx.file("BUILD.bazel", 'exports_files(["facts.txt"], visibility = ["//visibility:public"])\n')
    rctx.file("facts.txt", "".join([line + "\n" for line in rctx.attr.lines]))

_facts_repo = repository_rule(
    implementation = _facts_repo_impl,
    attrs = {"lines": attr.string_list()},
)

_fact = tag_class(attrs = {"text": attr.string()})

def _facts_impl(mctx):
    lines = []
    for mod in mctx.modules:
        for tag in mod.tags.fact:
            lines.append("%s: %s" % (mod.name, tag.text))
    _facts_repo(name = "lab_facts", lines = sorted(lines))

facts = module_extension(
    implementation = _facts_impl,
    tag_classes = {"fact": _fact},
)
