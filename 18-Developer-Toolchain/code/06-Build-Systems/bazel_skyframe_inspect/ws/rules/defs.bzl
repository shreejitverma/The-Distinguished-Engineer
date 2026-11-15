"""Minimal rules for the Skyframe lab: a provider, a depset, a worker action, an aspect."""

SourcesInfo = provider(
    doc = "Normalized files produced by a target and everything it depends on.",
    fields = {
        "transitive": "depset of normalized Files, postorder",
    },
)

FlavorInfo = provider(fields = {"value": "the flag value"})

def _flavor_flag_impl(ctx):
    return [FlavorInfo(value = ctx.build_setting_value)]

flavor_flag = rule(
    implementation = _flavor_flag_impl,
    build_setting = config.string(flag = True),
)

def _normalized_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".norm")

    # Per-request arguments go in a params file; a worker action must end with @flagfile.
    args = ctx.actions.args()
    args.add("--src", ctx.file.src)
    args.add("--out", out)
    args.use_param_file("@%s", use_always = True)
    args.set_param_file_format("multiline")

    ctx.actions.run(
        mnemonic = "Normalize",
        progress_message = "Normalizing %{input}",
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = [ctx.file.src],
        outputs = [out],
        execution_requirements = {
            "supports-workers": "1",
            "requires-worker-protocol": "json",
        },
    )
    transitive = depset(
        direct = [out],
        transitive = [d[SourcesInfo].transitive for d in ctx.attr.deps],
        order = "postorder",
    )
    return [
        DefaultInfo(files = depset([out])),
        SourcesInfo(transitive = transitive),
    ]

normalized = rule(
    implementation = _normalized_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "deps": attr.label_list(providers = [SourcesInfo]),
        "_tool": attr.label(
            default = "//rules:normalize.py",
            executable = True,
            allow_single_file = True,
            cfg = "exec",
        ),
    },
)

def _bundle_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    srcs = depset(
        transitive = [d[SourcesInfo].transitive for d in ctx.attr.deps],
        order = "postorder",
    )
    data = depset(ctx.files.data)
    args = ctx.actions.args()
    args.add(out)

    # add_all on a depset is expanded lazily, at execution time, not during analysis.
    args.add_all(srcs)
    args.add_all(data)
    ctx.actions.run_shell(
        mnemonic = "Bundle",
        progress_message = "Bundling %{label}",
        inputs = depset(transitive = [srcs, data]),
        outputs = [out],
        arguments = [args],
        command = 'out="$1"; shift; for f in "$@"; do printf "== %s\\n" "${f##*/}"; cat "$f"; done > "$out"',
    )
    return [DefaultInfo(files = depset([out]))]

bundle = rule(
    implementation = _bundle_impl,
    attrs = {
        "deps": attr.label_list(providers = [SourcesInfo]),
        "data": attr.label_list(allow_files = True),
    },
)

InventoryInfo = provider(fields = {"count": "number of targets in the shadow graph below and at this node"})

def _inventory_aspect_impl(target, ctx):
    count = 1
    for dep in getattr(ctx.rule.attr, "deps", []):
        if InventoryInfo in dep:
            count += dep[InventoryInfo].count
    report = ctx.actions.declare_file(target.label.name + ".inventory")
    ctx.actions.write(report, "%s kind=%s shadow_nodes=%d\n" % (target.label.name, ctx.rule.kind, count))
    return [
        InventoryInfo(count = count),
        OutputGroupInfo(inventory = depset([report])),
    ]

# Propagates along "deps" only, so it builds a shadow graph of the deps edges.
inventory_aspect = aspect(
    implementation = _inventory_aspect_impl,
    attr_aspects = ["deps"],
)
