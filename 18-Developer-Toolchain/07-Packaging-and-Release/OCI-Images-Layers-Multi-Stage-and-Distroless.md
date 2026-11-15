---
type: concept
track: [sde, distinguished, ai-eng]
level:
status: solid
last_reviewed:
sources:
  - "Open Container Initiative, OCI Image Format Specification v1.1.1, Open Containers Initiative, 2025, https://github.com/opencontainers/image-spec/blob/v1.1.1/spec.md"
  - "Open Container Initiative, OCI Image Manifest Specification v1.1.1, Open Containers Initiative, 2025, https://github.com/opencontainers/image-spec/blob/v1.1.1/manifest.md"
  - "Open Container Initiative, OCI Image Configuration Specification v1.1.1, Open Containers Initiative, 2025, https://github.com/opencontainers/image-spec/blob/v1.1.1/config.md"
  - "Open Container Initiative, OCI Image Layer Filesystem Changeset Specification v1.1.1, Open Containers Initiative, 2025, https://github.com/opencontainers/image-spec/blob/v1.1.1/layer.md"
  - "Open Container Initiative, OCI Image Index Specification v1.1.1, Open Containers Initiative, 2025, https://github.com/opencontainers/image-spec/blob/v1.1.1/image-index.md"
  - "Open Container Initiative, OCI Image Layout Specification v1.1.1, Open Containers Initiative, 2025, https://github.com/opencontainers/image-spec/blob/v1.1.1/image-layout.md"
  - "Open Container Initiative, OCI Pre-Defined Annotation Keys v1.1.1, Open Containers Initiative, 2025, https://github.com/opencontainers/image-spec/blob/v1.1.1/annotations.md"
  - "Open Container Initiative, OCI Media Types Specification v1.1.1, Open Containers Initiative, 2025, https://github.com/opencontainers/image-spec/blob/v1.1.1/media-types.md"
  - "Linux Kernel Organization, Overlay Filesystem (Documentation/filesystems/overlayfs.rst), Linux Kernel 6.12+, 2026, https://docs.kernel.org/filesystems/overlayfs.html"
  - "Moby Project, BuildKit LLB operation definitions (solver/pb/ops.proto), BuildKit Contributors, 2026, https://github.com/moby/buildkit/blob/master/solver/pb/ops.proto"
  - "Google Container Tools, 'Distroless' Container Images, Google LLC, 2026, https://github.com/GoogleContainerTools/distroless"
  - "Reproducible Builds Project, SOURCE_DATE_EPOCH Specification, Reproducible-Builds.org, 2026, https://reproducible-builds.org/docs/source-date-epoch/"
  - "IETF, RFC 8878 - Zstandard Compression and The 'application/zstd' Media Type, Internet Engineering Task Force, 2021, https://www.rfc-editor.org/rfc/rfc8878.html"
  - "Containerd Project, Stargz Snapshotter: Lazy Image Pulling with eStargz and zstd:chunked, CNCF Containerd, 2026, https://github.com/containerd/stargz-snapshotter"
  - "Chainguard Inc., Chainguard Images: Minimal, Secure, Distroless Container Base Images, Chainguard Inc., 2026, https://images.chainguard.dev/"
  - "Open Container Initiative, OCI Image Media Types v1.0.2 (no zstd entry), Open Containers Initiative, 2021, https://github.com/opencontainers/image-spec/blob/v1.0.2/media-types.md"
  - "Moby Project, Dockerfile frontend reference (COPY --chown, RUN --mount, ARG warning), BuildKit Contributors, 2026, https://github.com/moby/buildkit/blob/master/frontend/dockerfile/docs/reference.md"
  - "Moby Project, BuildKit README (cache export mode=min|max), BuildKit Contributors, 2026, https://github.com/moby/buildkit/blob/master/README.md"
  - "Moby Project, Build reproducibility (SOURCE_DATE_EPOCH, rewrite-timestamp), BuildKit Contributors, 2026, https://github.com/moby/buildkit/blob/master/docs/build-repro.md"
  - "Moby Project, dockerfile2llb convert.go (dispatchWorkdir uses FileOp Mkdir), BuildKit Contributors, 2026, https://github.com/moby/buildkit/blob/master/frontend/dockerfile/dockerfile2llb/convert.go"
  - "Docker Inc., Docker Engine 23.0 release notes, Docker Docs, 2023, https://docs.docker.com/engine/release-notes/23.0/"
  - "Linux kernel, fs/namei.c vfs_mknod (whiteout exemption from CAP_MKNOD, present since v5.8), kernel.org, 2026, https://github.com/torvalds/linux/blob/master/fs/namei.c"
  - "Miklos Szeredi, vfs: allow unprivileged whiteout creation (commit a3c751a50fe6), Linux kernel, 2020, https://lore.kernel.org/linux-fsdevel/20200501041444.GJ23230@ZenIV.linux.org.uk/T/"
  - "Google Container Tools, distroless base and cc README, Google LLC, 2026, https://github.com/GoogleContainerTools/distroless/blob/main/base/README.md"
  - "Google Container Tools, distroless PACKAGE_METADATA.md (/var/lib/dpkg/status.d), Google LLC, 2026, https://github.com/GoogleContainerTools/distroless/blob/main/PACKAGE_METADATA.md"
  - "Meta, Zstandard README benchmark table, facebook/zstd, 2026, https://github.com/facebook/zstd/blob/dev/README.md"
  - "T. Harter et al., Slacker: Fast Distribution with Lazy Docker Containers, USENIX FAST, 2016, https://research.cs.wisc.edu/wind/Publications/slacker-fast16.pdf"
  - "Go Team, Go 1.20 Release Notes (cgo disabled without a C toolchain), go.dev, 2023, https://go.dev/doc/go1.20"
  - "NIST, CVE-2019-5736 Detail, National Vulnerability Database, 2019, https://nvd.nist.gov/vuln/detail/CVE-2019-5736"
  - "containers/storage contributors, zstd:chunked format (docs/containers-storage-zstd-chunked.md), Red Hat and containers project, 2026, https://github.com/containers/storage/blob/main/docs/containers-storage-zstd-chunked.md"
  - "containers/storage contributors, zstd:chunked TOC, tar-split, and footer writer (pkg/chunked/internal/minimal/compression.go), Red Hat and containers project, 2026, https://github.com/containers/storage/blob/main/pkg/chunked/internal/minimal/compression.go"
  - "Google Container Tools, distroless cc/config.bzl (CC_PACKAGES) and common/variables.bzl (OS_RELEASE), Google LLC, 2026, https://github.com/GoogleContainerTools/distroless/blob/main/cc/config.bzl"
  - "Uber Engineering, Kraken README (P2P Docker registry benchmark), Uber Technologies, 2026, https://github.com/uber/kraken"
---

# OCI Images: Layers, Multi-Stage Builds, and Distroless Roots

> [!summary] Claim:
> An OCI container image is an immutable Merkle directed acyclic graph of content-addressable blobs tied together by descriptors, manifests, configurations, and ordered tar filesystem diff layers.
> Layer modifications and deletions serialize through POSIX tar whiteout markers (`.wh.<basename>` and `.wh..wh..opq`), which container runtimes translate into Linux kernel OverlayFS character devices (`makedev(0, 0)`) and opaque directory xattrs (`trusted.overlay.opaque="y"`).
> Multi-stage builds compile applications inside ephemeral toolchain stages and copy isolated binaries into distroless or scratch roots, dropping hundreds of megabytes of compilers, package managers, and shell LOLBins to minimize attack surface and vulnerability counts.
> Bit-for-bit reproducible image distribution requires clamping filesystem timestamps to `SOURCE_DATE_EPOCH`, sorting tar headers alphabetically, zeroing ownership metadata, and isolating builds from host non-determinism.

```mermaid
flowchart TD
    subgraph INDEX["OCI Image Index (Multi-Arch Manifest List)"]
        IDX["application/vnd.oci.image.index.v1+json<br/>schemaVersion: 2<br/>manifests: [ Descriptor(linux/amd64), Descriptor(linux/arm64) ]"]
    end

    subgraph MANIFEST["OCI Image Manifest (linux/arm64)"]
        MF["application/vnd.oci.image.manifest.v1+json<br/>schemaVersion: 2<br/>config: Descriptor(config.json)<br/>layers: [ Descriptor(L1), Descriptor(L2) ]"]
    end

    subgraph CONFIG["OCI Image Configuration Blob"]
        CFG["application/vnd.oci.image.config.v1+json<br/>architecture: arm64 | os: linux<br/>config: { User: '65532:65532', Entrypoint: ['/app/server'] }<br/>rootfs: { type: 'layers', diff_ids: [ sha256(L1_tar), sha256(L2_tar) ] }<br/>history: [ created_by: 'COPY binary', empty_layer: false ]"]
    end

    subgraph LAYERS["Content-Addressable Blobs (blobs/sha256/...)"]
        L1["Layer 1: Base Runtime (compressed .tar.gz)<br/>Digest: sha256(compressed_L1)<br/>DiffID: sha256(uncompressed_tar)<br/>Entries: /lib/ld-musl.so, /etc/passwd"]
        L2["Layer 2: App Diff & Whiteouts (.tar.gz)<br/>Digest: sha256(compressed_L2)<br/>DiffID: sha256(uncompressed_tar)<br/>Entries: /app/server, /etc/.wh.old_cfg, /tmp/.wh..wh..opq"]
    end

    subgraph KERNEL["Linux Kernel OverlayFS Mount"]
        LOW1["lowerdir layer 1 (read-only): /var/lib/containerd/.../l1/diff"]
        LOW2["lowerdir layer 2 (read-only): /var/lib/containerd/.../l2/diff<br/>Translates .wh.old_cfg -> c 0 0 device<br/>Translates .wh..wh..opq -> xattr trusted.overlay.opaque='y'"]
        UP["upperdir (read-write): container runtime diff"]
        MERGED["merged rootfs: /run/containerd/.../rootfs"]
    end

    IDX -->|"selects platform"| MF
    MF -->|"config descriptor"| CFG
    MF -->|"layers[0] descriptor"| L1
    MF -->|"layers[1] descriptor"| L2
    CFG -.->|"rootfs.diff_ids[0]"| L1
    CFG -.->|"rootfs.diff_ids[1]"| L2
    L1 -->|"unpacked into"| LOW1
    L2 -->|"unpacked into"| LOW2
    LOW1 --> MERGED
    LOW2 --> MERGED
    UP --> MERGED
```

## The Mechanism

Container images are not block-level disk images, virtual hard drives, or machine snapshots.
An image is a cryptographically authenticated directed acyclic graph (DAG) of content-addressable blobs defined by the Open Container Initiative (OCI) Image Format Specification v1.1.1.
When a container engine boots an image, it mounts these stacked layers using kernel union filesystems, primarily Linux OverlayFS.

### 1. Content Addressable Storage and the OCI Merkle DAG

Every artifact within an OCI container image repository resides in a Content Addressable Storage (CAS) layout.
A blob is identified exclusively by the cryptographic hash of its payload, formatted as `<algorithm>:<hex-encoded-hash>`, almost universally using `sha256`.
The OCI specification defines four fundamental document structures that form a Merkle DAG: Descriptors, Manifests, Configurations, and Indexes.

#### The Content Descriptor

A Descriptor is the universal pointer in the OCI universe.
It links one content-addressed blob to another while specifying the expected payload size and MIME media type:

```json
{
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "digest": "sha256:725e6abf8167ffb9017525659e2d3b0a09e80a8c123be475b1e47d77f355c43f",
  "size": 664,
  "annotations": {
    "org.opencontainers.image.ref.name": "v1.0.0"
  }
}
```

The `mediaType` field informs the consumer how to decode the payload.
The `digest` guarantees bit-level data integrity; if a single byte changes, the digest mismatches and the client aborts.
The `size` field allows downloaders to pre-allocate buffers and verify transfer completeness without inspecting the byte stream.
The optional `urls` array allows out-of-band blob storage (such as external HTTP CDNs for massive model weights).
The optional `artifactType` field (introduced in OCI v1.1.0) specifies the artifact category when the descriptor references non-traditional container payloads.

#### The Image Manifest

The Image Manifest (`application/vnd.oci.image.manifest.v1+json`) bundles the execution configuration with an ordered array of filesystem layers for a single concrete platform:

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "config": {
    "mediaType": "application/vnd.oci.image.config.v1+json",
    "digest": "sha256:db25254fc33b1658591a6ac9e901e42180b5129c37ee3773367481cffd0a32f2",
    "size": 765
  },
  "layers": [
    {
      "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:5ce89552d2bc8f22253a1e75900e16387732cd0ddc0d4ea80756abc50274dd53",
      "size": 175
    },
    {
      "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
      "digest": "sha256:1f656bbd158e2172197a4cd4b6258cc58cfcdd5d87e62bae18143e6a5eed60e7",
      "size": 142
    }
  ]
}
```

This is byte for byte the 664-byte manifest blob that the Worked Example produces; an optional top-level `annotations` map (for example `org.opencontainers.image.created`) would change its digest.

The layers array is strictly ordered from base (index 0) to top (index $N-1$).
During root filesystem assembly, index 0 is applied first, and each subsequent layer diff applies over the accumulated tree.
OCI v1.1.0 introduced the optional `subject` descriptor field.
The `subject` field points to another manifest digest, enabling registries to track reverse links (referrers).
This allows supply-chain security tooling to attach digital signatures, in-toto attestations, and Software Bills of Materials (SBOMs) to an existing image without mutating the original image manifest digest.

#### The Image Configuration

The Image Configuration blob (`application/vnd.oci.image.config.v1+json`) contains execution metadata and container environment variables.
The container runtime transforms this JSON into the runtime spec (`config.json`) passed to `runc` or `crun`:

- `architecture` and `os`: Target CPU architecture (`amd64`, `arm64`, `riscv64`) and operating system (`linux`, `windows`).
- `config`: Runtime parameters including `User`, `Env` arrays, `Entrypoint`, `Cmd`, `WorkingDir`, and `StopSignal`.
- `rootfs`: Contains `type: "layers"` and the `diff_ids` array.
- `history`: An array recording the Dockerfile command string (`created_by`), timestamp, and an `empty_layer` boolean flag for commands (like `ENV` or `LABEL`) that alter metadata without producing filesystem tar diffs.

#### The Image Index

An Image Index (`application/vnd.oci.image.index.v1+json`), formerly known as a multi-arch manifest list, enables a single image tag to support multiple architectures and operating systems.
The index contains a `manifests` array of descriptors.
Each descriptor specifies a `platform` object containing `architecture`, `os`, and optional sub-architecture selectors:
- `variant`: CPU pipeline variant, such as `v7` for 32-bit ARM or `v8` for 64-bit ARM.
- `os.version`: OPTIONAL in the spec; implementations MAY refuse manifests whose `os.version` is not known to work with the host, which Windows hosts use to match the host build (spec example `10.0.14393.1066`).
- `os.features`: OPTIONAL list of mandatory OS features; for Windows the spec defines `win32k`.
- `features`: RESERVED for future versions of the specification.

When a client runs `docker pull myapp:v1` on an Apple Silicon or AWS Graviton machine, the container engine fetches the image index from the registry manifest endpoint (`index.json` is only the on-disk layout root), searches the `manifests` array for `os == "linux"` and `architecture == "arm64"`, and downloads the target manifest.

#### The OCI Image Layout on Disk

When exported to a directory or a flat tar archive via `docker save` or `skopeo copy`, the OCI specification mandates a standardized directory structure:

```text
my-image-layout/
|-- oci-layout
|-- index.json
\-- blobs/
    \-- sha256/
        |-- 1f656bbd158e2172197a4cd4b6258cc58cfcdd5d87e62bae18143e6a5eed60e7
        |-- 5ce89552d2bc8f22253a1e75900e16387732cd0ddc0d4ea80756abc50274dd53
        |-- 725e6abf8167ffb9017525659e2d3b0a09e80a8c123be475b1e47d77f355c43f
        \-- db25254fc33b1658591a6ac9e901e42180b5129c37ee3773367481cffd0a32f2
```

The file `oci-layout` contains exactly `{"imageLayoutVersion": "1.0.0"}`.
The file `index.json` acts as the root entry point pointing to manifests or sub-indexes within `blobs/sha256/`.
All configuration documents, manifests, and filesystem layers reside inside the content-addressable `blobs/` tree, named strictly by their hash.

---

### 2. Filesystem Diff Layers, Compression, and Digest Duality

Each layer in an OCI image represents a filesystem delta: files added, modified, or deleted relative to the preceding layer.
The OCI specification serializes these deltas as standard POSIX tar archives.

#### The DiffID versus Layer Digest Duality

Every layer has two distinct cryptographic hashes that engineers must never confuse:
1. **Layer Digest (`manifest.layers[i].digest`):** The SHA-256 hash of the *compressed* archive as stored in the registry CAS and transferred across the network.
2. **Layer DiffID (`config.rootfs.diff_ids[i]`):** The SHA-256 hash of the *uncompressed* tar stream.

This dual-hash architecture serves two distinct purposes:
- The Layer Digest guarantees wire transfer integrity and enables HTTP CDN caching of compressed payloads.
- The DiffID provides a canonical, compression-independent identifier for the layer's uncompressed filesystem state.

If a mirroring tool or proxy recompresses a layer (and rewrites the manifest) from gzip to zstd to accelerate decompression, or adjusts gzip compression from level 6 to level 9, the compressed Layer Digest changes.
However, because the unpacked tar contents remain byte-for-byte identical, the uncompressed `DiffID` remains completely unchanged.
Consequently, the rootfs calculation and the resulting configuration hash (`ImageID`) remain invariant across compression migrations.

```text
Tarball Generation -> sha256(raw_tar)      ====> Stored in config.rootfs.diff_ids (DiffID)
         |
    gzip / zstd
         |
         v
Compressed Stream  -> sha256(compressed)   ====> Stored in manifest.layers.digest (Digest)
```

#### Layer Compression: Gzip versus Zstandard

Historically, container layers relied exclusively on gzip (`application/vnd.oci.image.layer.v1.tar+gzip`, RFC 1952).
Gzip decompression is single-threaded and slow: the zstd project benchmark (Core i7-9700K, Silesia corpus) measures zlib 1.3.1 level 1 at 390 MB/s decompression per core (as of 2026-11).
For large images (such as machine learning training containers or monolithic enterprise runtimes spanning 10 to 30 GB), gzip decompression dominates container startup latency.

OCI Image Specification v1.1.0 standardized Zstandard compression (the v1.0.2 media types list has no zstd entry) (`application/vnd.oci.image.layer.v1.tar+zstd`, RFC 8878).
In the same benchmark zstd 1.5.7 level 1 reaches ratio 2.896 against zlib's 2.743 while decompressing at 1550 MB/s per core (as of 2026-11).
Zstd also supports multi-threaded compression (decompression of a single frame stays single-threaded) and custom dictionary training.

#### Lazy Pulling with eStargz and Chunked Zstandard

Standard container runtimes download and decompress every layer completely before `execve()` can launch the entrypoint process.
The Slacker study (Harter et al., FAST 2016) measured that pulling accounts for 76% of container start time while only 6.4% of the pulled data is read.
To eliminate pull latency, the containerd and containers/storage ecosystems define lazy pulling formats; they are not part of the OCI image spec but remain valid OCI gzip or zstd layers that ordinary runtimes can pull in full:
- **eStargz (Extended Stargz):** Formats the gzip stream into independently compressed file chunks with a Table of Contents (TOC) tar entry appended to the archive tail.
- **`zstd:chunked`:** Formats the Zstandard stream into independent frames with the TOC (a CRFS-compatible manifest, manifest type 1) stored in a Zstandard skippable frame (magic `0x184D2A50`) near the end of the blob, followed by a tar-split frame and a 64-byte footer whose last 8 bytes are the magic `GNUlInUx`; the footer makes the blob self-describing, but the TOC digest carried in a layer annotation is what a partial pull trusts (containers/storage `docs/containers-storage-zstd-chunked.md` and `pkg/chunked/internal/minimal/compression.go`, as of 2026-11).

A lazy-pull snapshotter (such as containerd's `stargz-snapshotter` or `nydus`) fetches only the TOC metadata (a few kilobytes via HTTP Range requests).
The runtime creates the mount immediately, allowing the container to boot in sub-second time.
When the application attempts to `open()` an unread binary or library, the snapshotter's userspace filesystem (FUSE for `stargz-snapshotter`; kernel OverlayFS itself never fetches remote data) intercepts the request, issues an on-demand HTTP Range request to the registry, and populates the file on the fly.

---

### 3. Whiteout Filesystem Mechanics: Tar Serialization vs Kernel OverlayFS

Standard tar archives are append-only formats.
A tar file can create a file, update a file, or create a directory, but has no native header record to declare "delete file `/etc/secret.key` that was created in an earlier archive".
To express deletions and directory masking within a stack of tar archives, the OCI specification adopted the whiteout convention originated by AUFS.

#### Tar Whiteout Representation

The OCI Layer Specification defines two whiteout types:

1. **Explicit File Whiteout (`.wh.<basename>`):**
To delete a file named `config.json` inside `/etc`, the diff layer tarball includes an empty, regular file named `/etc/.wh.config.json`.
When the unpacking engine extracts this layer over a base layer containing `/etc/config.json`, it deletes or hides `/etc/config.json` and drops the `.wh.config.json` marker.
The marker must never appear in the running container's root filesystem.

2. **Opaque Directory Whiteout (`.wh..wh..opq`):**
When a build step replaces or clears an entire directory structure, emitting individual `.wh.*` files for thousands of subfiles is inefficient.
Instead, the builder emits a special marker named `.wh..wh..opq` directly inside the target directory.
This instructs the unpacker that the directory is opaque: all children and descendants of this directory inherited from lower layers must be masked and rendered invisible.
Only files explicitly defined inside the new layer's directory will be visible.

```text
Layer 0 (Base):
  \-- /app
      |-- server.py
      \-- config.json

Layer 1 (Diff):
  \-- /app
      |-- .wh.config.json    <-- Masks /app/config.json from Layer 0
      \-- server.py          <-- Overwrites /app/server.py
```

#### Translation to Linux Kernel OverlayFS

The Linux kernel does not parse OCI tar archives.
The container runtime (such as containerd or CRI-O) unpacks each layer tarball into an isolated host directory under `/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/`.
When preparing the container rootfs, the runtime mounts these directories using the OverlayFS kernel driver:

```sh
mount -t overlay overlay -o lowerdir=/snapshots/3/diff:/snapshots/2/diff:/snapshots/1/diff,upperdir=/snapshots/4/diff,workdir=/snapshots/4/work /run/containerd/rootfs
```

During layer extraction onto disk, the container engine translates OCI whiteout markers into kernel-native OverlayFS structures:
- **Standard Whiteouts:** When the extractor encounters `.wh.<basename>`, it executes the `mknod()` system call to create a character device with major device number 0 and minor device number 0 (`makedev(0, 0)`):

```c
mknod("/snapshots/2/diff/etc/config.json", S_IFCHR | 0666, makedev(0, 0));
```

When the Linux kernel OverlayFS driver traverses the stacked directories and encounters a character device with device number 0/0 in an upper layer, any matching name in the lower layers is ignored and the whiteout itself is hidden, so lookup returns `-ENOENT`.
The kernel also accepts an alternative whiteout form: a zero-size regular file carrying the xattr `trusted.overlay.whiteout` (Documentation/filesystems/overlayfs.rst).
The file in the lower layer is hidden from userspace.

- **Opaque Directories:** When the extractor encounters `.wh..wh..opq`, it deletes the marker file and sets a trusted extended attribute on the directory:

```c
setxattr("/snapshots/2/diff/var/log", "trusted.overlay.opaque", "y", 1, 0);
```

When the kernel traverses `/var/log` across the union mount, `trusted.overlay.opaque="y"` makes OverlayFS ignore any lower directory with the same name, so neither lookup nor readdir merges entries from lower layers (Documentation/filesystems/overlayfs.rst).

```text
OCI Tarball Layer                       Unpacked on Host Filesystem (OverlayFS)
-----------------                       ---------------------------------------
etc/.wh.config.json     == unpack ==>   mknod("etc/config.json", S_IFCHR, makedev(0, 0))
var/log/.wh..wh..opq    == unpack ==>   setxattr("var/log", "trusted.overlay.opaque", "y")
```

#### Rootless Containers and User Namespaces

The standard OverlayFS whiteout mechanisms present a severe challenge for rootless container runtimes (such as rootless Podman, Docker rootless, or unprivileged Kubernetes user namespaces).
Inside an unprivileged user namespace, unprivileged users lack `CAP_MKNOD` in the initial user namespace.
Before Linux 5.8, calling `mknod()` to create a `0/0` character device without `CAP_MKNOD` failed with `EPERM`; since 5.8 (commit a3c751a50fe6, "vfs: allow unprivileged whiteout creation"), `vfs_mknod()` exempts the 0/0 whiteout device from the `CAP_MKNOD` check.
Unprivileged processes still cannot write to the `trusted.*` extended attribute namespace; `setxattr()` fails with `EPERM`.

To resolve this limitation, Linux kernel 5.11 introduced unprivileged OverlayFS mounts in user namespaces together with the `userxattr` mount option.
With `-o userxattr` the driver uses the `user.overlay.` xattr namespace, so opaque directories are marked with `user.overlay.opaque="y"`.
On older kernels, rootless runtimes fall back to FUSE-based union filesystems like `fuse-overlayfs`.
The `fuse-overlayfs` driver intercepts filesystem calls in userspace and simulates character device whiteouts and xattrs entirely within standard unprivileged user permissions.

#### The Deletion Inefficiency Trap

Because lower layers are strictly immutable read-only snapshots, deleting a file in a higher layer never reclaims disk space or network bandwidth from the published image.
If Layer 1 downloads a 1 GB database dump `/tmp/data.dump`, and Layer 2 runs `rm /tmp/data.dump`, Layer 2 simply records a 0-byte whiteout marker `.wh.data.dump`.
The final published container image still contains the full 1 GB blob in Layer 1.
Every registry pull still downloads that 1 GB across the wire, and every node still stores that 1 GB on local disk.

---

### 4. Multi-Stage Compilation and BuildKit LLB Execution

Single-stage container builds frequently produce bloated, insecure images.
Building a compiled binary directly inside a single container image forces the inclusion of compilers (GCC, Clang, Rustc), header packages (`glibc-devel`, `kernel-headers`), package manager databases, and intermediate object files.
This practice increases image size from tens of megabytes to gigabytes and leaves thousands of unneeded system packages vulnerable to CVE scanners.

#### Multi-Stage Architecture

Multi-stage builds resolve this problem by creating isolated execution environments in a single `Dockerfile`.
Artifacts are selectively copied between stages using `COPY --from=<stage>`:

```dockerfile
# Stage 1: Dedicated compiler stage
FROM golang:1.24-bookworm AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /bin/server .

# Stage 2: Minimal runtime stage
FROM gcr.io/distroless/static-debian13:nonroot
COPY --from=builder /bin/server /bin/server
USER nonroot:nonroot
ENTRYPOINT ["/bin/server"]
```

The resulting runtime image contains only the base distroless layers plus one layer holding the compiled `/bin/server` binary.
The entire Go toolchain, intermediate object files, source tree, and package caches evaporate when the build finishes.

#### BuildKit Low-Level Builder (LLB) Engine

Modern container builds are driven by Docker BuildKit.
BuildKit discards the legacy Docker engine model of executing lines sequentially inside temporary container commits.
Instead, BuildKit compiles the Dockerfile into a directed acyclic graph of Low-Level Builder (LLB) operations.
LLB operations are defined as Protocol Buffer messages representing a dataflow graph:
- `SourceOp`: Resolves external inputs (base images, Git repositories, local build context).
- `ExecOp`: Executes a process within a mount namespace.
- `FileOp`: Performs atomic filesystem manipulations (copy, move, delete, mkdir).
- `BuildOp`: Triggers sub-builds.
- `MergeOp` and `DiffOp`: Stack independent snapshots and compute the difference between two snapshots (`solver/pb/ops.proto`).

```mermaid
flowchart LR
    CTX["Local Build Context"] --> S2["FileOp: COPY go.mod go.sum"]
    BASE["SourceOp: golang:1.24"] --> S1["FileOp: mkdir /src (WORKDIR)"]
    S1 --> S2
    S2 --> S3["ExecOp: go mod download"]
    CTX --> S4["FileOp: COPY . ."]
    S3 --> S4
    S4 --> S5["ExecOp: go build"]
    DISTRO["SourceOp: distroless/static"] --> S6["FileOp: COPY --from=builder /bin/server"]
    S5 -->|"provides /bin/server"| S6
    S6 --> EXP["Export: OCI Image Manifest"]
```

Because LLB operates as a dependency graph rather than a sequential script:
1. **Parallel Stage Execution:** Independent stages execute concurrently across available CPU cores.
If a Dockerfile defines a frontend Node.js asset build stage and a backend C++ compilation stage, BuildKit evaluates both stages in parallel.
2. **Automatic Stage Pruning:** If a stage is defined in a Dockerfile but is not referenced by the targeted final stage (or by `--target`), BuildKit prunes that stage from the LLB graph.
The unreferenced stage is never pulled, scheduled, or built.
3. **Target Stage Selection:** The `--target` flag allows CI pipelines to run linting, unit tests, and production image compilation from a single Dockerfile while evaluating only the exact DAG path required for that specific target.

#### BuildKit Advanced Mount Topologies

BuildKit introduces specialized `RUN --mount` primitives that execute builds without leaking state into output layers:

- **Cache Mounts (`--mount=type=cache`):**
Caches package manager downloads, compiler intermediate objects, and dependencies across successive builds without committing those directories to image layers:

```dockerfile
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    go build -o /bin/server .
```

Cache mounts persist in BuildKit's local storage database outside the layer hierarchy.
Cache mounts support three sharing modes, defaulting to `shared`: `sharing=shared` (multiple concurrent builds write to the same cache), `sharing=private` (a new mount is created when there are multiple writers), and `sharing=locked` (concurrent builds block until the lock releases).

- **Secret Mounts (`--mount=type=secret`):**
Mounts sensitive credentials (such as GitHub personal access tokens, AWS keys, or private SSH deploy keys) into an in-memory `tmpfs` during command execution, at `/run/secrets/<id>` unless `target` or `env` is set:

```dockerfile
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc \
    npm ci --production
```

The secret is mounted exclusively into the transient container process namespace.
It is never written to disk, never serialized into an OCI tar layer, and never leaks into the image configuration history.

- **Bind Mounts (`--mount=type=bind`):**
Mounts a directory directly from the host build context or an earlier stage without executing a filesystem `COPY`.
For example, a compiler can read source code directly from a bind mount, compile the binary into a separate directory, and exit without ever generating a layer that contains the raw source code.

---

### 5. Distroless Roots and Minimal Attack Surfaces

Production container security requires minimizing the amount of executable software present inside the runtime rootfs.
Engineers categorize container root filesystems across a density and security spectrum.

#### The Root Filesystem Spectrum

| Base Image Type | Example Images | Size Cited by the Distroless README (as of 2026-11) | Shell Included | Package Manager | Standard C Library |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Full Distribution** | `ubuntu:24.04`, `debian:12` | `debian`: 124 MiB; `ubuntu` not listed | Yes (`/bin/bash`, `/bin/sh`) | Yes (`apt`, `dpkg`) | GNU C Library (`glibc`) |
| **Slim Distribution** | `debian:12-slim` | not listed | Yes (`/bin/dash`, `/bin/bash`) | Yes (`apt`, `dpkg`) | GNU C Library (`glibc`) |
| **Lightweight OS** | `alpine:3.20` | `alpine`: about 5 MiB | Yes (`/bin/sh` BusyBox) | Yes (`apk`) | `musl` libc |
| **Distroless Base** | `gcr.io/distroless/base-debian13` | not listed | None | None | GNU C Library (`glibc`) |
| **Distroless CC** | `gcr.io/distroless/cc-debian13` | not listed | None | None | `glibc`, plus `libstdc++6`, `libgcc-s1`, `libgomp1`, `gcc-14-base` |
| **Distroless Static**| `gcr.io/distroless/static-debian13`| about 2 MiB | None | None | None (Pure static only) |
| **Scratch** | `scratch` | 0 B | None | None | None |

The sizes are the ones the distroless README states for its comparison of `static`, `alpine`, and `debian`; this note does not quote sizes for images the README does not list, because they change with every rebuild.
Measure your own with `docker image inspect --format '{{.Size}}' <image>` after a pull.
The README lists only `-debian13` (Debian 13 trixie) images as of 2026-11, so this note uses those tags.

#### Anatomy of a Distroless Image

Pioneered by Google and expanded by Chainguard, "Distroless" images contain strictly the application binary and its direct runtime dependencies.
A distroless base image strips everything else: no interactive shell (`/bin/sh`, `/bin/bash`), no package managers (`apt`, `apk`, `rpm`), and no standard coreutils (`ls`, `cat`, `rm`, `chmod`, `curl`, `nc`).

A minimal distroless image (such as `gcr.io/distroless/static-debian13`) contains only a few foundational components:
1. `/etc/passwd` and `/etc/group`: Contains entries for `root` (UID 0), `nonroot` (UID 65532), and `nobody` (UID 65534).
2. `/etc/ssl/certs/ca-certificates.crt`: Standard trusted Root Certificate Authorities required to establish outbound HTTPS and TLS connections.
3. `/usr/share/zoneinfo`: Timezone database files required for date and time parsing across global regions.
4. `/tmp`: A directory configured with the POSIX sticky bit (`chmod 1777`).
5. `/etc/os-release`: A generated file that identifies the image as `PRETTY_NAME="Distroless"`, `NAME="Debian GNU/Linux"`, `ID=debian`, with the Debian `VERSION_ID` and `VERSION` (distroless `common/variables.bzl` `OS_RELEASE`, with a `static_release` test in `static/BUILD`; the README does not list it).
Wolfi is the base of Chainguard images, not of `gcr.io/distroless`.

For dynamically linked applications, `distroless/base-debian13` adds `glibc` and `libssl` (on Debian 13 also the `libssl` dependencies `zlib` and `libzstd`), per `base/README.md`.
For C++ binaries, `distroless/cc-debian13` adds the runtime libraries of the GCC toolchain: its `cc/config.bzl` `CC_PACKAGES` lists `libgomp1`, `libstdc++6`, `libgcc-s1`, and `gcc-14-base` for Debian 13 (as of 2026-11), so `libstdc++.so.6` is present although `cc/README.md` mentions only `libgcc1`.

#### Attack Surface Reduction and Elimination of LOLBins

Distroless runtimes fundamentally reshape container security:

1. **Elimination of Shell Injections:**
In web applications, command injection bugs frequently occur when user-supplied input reaches functions like Python's `os.system()` or Node.js's `child_process.exec()`.
These functions implicitly spawn `/bin/sh -c <command>`.
Inside a distroless container, `/bin/sh` does not exist.
The underlying `execve("/bin/sh", ...)` system call fails immediately with `-ENOENT`, completely defanging the injection exploit.

2. **Denial of Living-off-the-Land Binaries (LOLBins):**
When an attacker gains arbitrary remote code execution via memory corruption or application deserialization, their first step is running LOLBins to explore the environment, fetch payloads, and establish persistence.
They invoke `which curl`, `wget`, `nc -e /bin/sh`, `python -c ...`, or `bash -i >& /dev/tcp/...`.
In a distroless container, none of these binaries exist on disk.
The attacker cannot download second-stage malware or spawn an interactive reverse shell.

3. **Enforcement of Non-Root Execution:**
Distroless images configure `USER 65532:65532` (`nonroot`).
Running as a non-root user prevents container breakout vulnerabilities.
For instance, the runc breakout vulnerability (CVE-2019-5736, runc through 1.0-rc6) let a malicious container overwrite the host runc binary through `/proc/self/exe`, either from an attacker-controlled image or when an administrator attached via `docker exec`.
NVD states the attack required the ability to execute a command as root inside the container.
Running as nonroot blocks this exploit class, although it does not prevent every breakout.

4. **Near-Zero CVE Scanning Noise:**
Traditional enterprise scanners (like Trivy or Grype) inspect `/var/lib/dpkg/status` or `/lib/apk/db/installed`.
An Ubuntu or Debian base image records its whole installed userland in that dpkg database, so scanners report CVEs against dormant utilities like `sed`, `grep`, `coreutils`, or `systemd-sysv` whether or not the application ever executes them.
Security teams waste thousands of engineering hours triaging CVEs in packages that are never executed.
A distroless static image still carries a handful of Debian packages (such as `ca-certificates` and `tzdata`), recorded per package under `/var/lib/dpkg/status.d/` so scanners can see them (`PACKAGE_METADATA.md`), which the README describes as improving scanner signal to noise rather than eliminating alerts.

#### Operational Trade-offs: Debugging Distroless Containers

The primary operational critique of distroless roots is the loss of interactive developer ergonomics.
Running `docker exec -it <container> /bin/sh` fails with:

```text
OCI runtime exec failed: exec failed: unable to start container process: exec: "/bin/sh": stat /bin/sh: no such file or directory: unknown
```

In mature production fleets, engineers solve this without re-introducing shells into the base image:

- **Kubernetes Ephemeral Debug Containers:**
Kubernetes supports launching an ephemeral debug container directly inside a running pod's namespace:

```sh
kubectl debug -it <pod-name> --image=busybox:musl --target=<container-name>
```

The debug container shares the target container's process namespace (`PID namespace`) and network namespace.
The engineer runs `ps aux`, inspects `/proc/<pid>/root/`, checks open network sockets, and traces processes while the target container continues running in its hardened distroless state.

- **Host Namespace Entry (`nsenter`):**
Engineers with node access can enter the container's namespaces from the host using `nsenter`:

```sh
PID=$(crictl inspect --output go-template --template '{{.info.pid}}' <container-id>)
sudo nsenter -t "$PID" -u -i -n -p /bin/sh
```

Omit `-m`: entering the container's mount namespace would resolve `/bin/sh` inside the distroless rootfs, where it does not exist; the host shell then reaches the container filesystem through `/proc/$PID/root/`.

- **Debug Sidecar Volumes:**
In staging environments, teams mount an ephemeral volume containing statically compiled debugging tools (such as static `busybox`, `curl`, and `strace`) into `/debug`.

---

### 6. Bit-for-Bit Deterministic and Reproducible Container Builds

A build is reproducible if given identical source code and build instructions, it produces byte-for-byte identical output images regardless of build timestamp, build machine, host operating system, or directory path.
Bit-for-bit reproducibility allows cryptographic verification of binaries, eliminates spurious deployment rollouts caused by meaningless hash drift, and validates software supply chain provenance.

#### Root Causes of Tar Non-Determinism

Standard `docker build` commands are inherently non-deterministic.
Building the exact same `Dockerfile` twice five minutes apart yields different Layer Digests and different ImageIDs due to five hidden sources of entropy:

1. **File Modification Timestamps (`mtime`):**
Every file copied into an image keeps the `mtime` it has in the build context, which is the checkout or edit time on that machine.
Tar headers record `mtime` as an integer timestamp or a fractional second PAX record.
Two builds executed seconds apart record different timestamps, changing the SHA-256 digest of the tar layer.

2. **File Ordering in Directory Traversal:**
The POSIX `readdir()` system call returns directory entries in the order they appear on the host filesystem's underlying inode b-tree or directory hash table (such as `htree` in ext4).
Copying a directory on ext4 yields a different tar entry sequence than copying the exact same directory on XFS, APFS, or another ext4 filesystem formatted with different hash seeds.
Changing the sequence of files changes the tar stream digest.

3. **User and Group Metadata (UID / GID):**
Dockerfile `COPY` from the build context does not leak host ownership: without `--chown`, files are created with UID and GID 0 (Dockerfile reference).
Ownership drift comes from hand-rolled layer tarballs and other image tools: if Developer A tars the rootfs with UID 1000 and Developer B with UID 501, the tar headers record different owner IDs, producing different layer hashes.

4. **File Permissions and Umask:**
Files created or checked out from Git inherit the host machine's `umask` (e.g., `0022` versus `0002`), altering file mode bits in the tar header.

5. **Gzip Header Timestamps and Flags:**
Standard gzip compression (RFC 1952) embeds a 32-bit modification timestamp in bytes 4 through 7 of its 10-byte header, along with an OS flag (byte 9).
Even if the underlying tar archive were byte-identical, compressing it with standard `gzip` produces a unique compressed hash on every run.

#### The `SOURCE_DATE_EPOCH` Normalization Standard

To achieve bit-for-bit reproducibility, the open-source community established the `SOURCE_DATE_EPOCH` specification.
`SOURCE_DATE_EPOCH` defines an environment variable containing an integer Unix epoch timestamp (typically the commit timestamp of the Git commit being built).

When generating reproducible tar layers:
- Every file `mtime` is set to `SOURCE_DATE_EPOCH` (`--mtime=@...`); add GNU tar `--clamp-mtime` to change only files newer than it.
- Tar entries are sorted alphabetically by path (`--sort=name`, a GNU tar option; macOS `bsdtar` lacks it, so run the pipeline below on Linux).
- Owner and group are forced to root or fixed IDs (`--owner=0 --group=0 --numeric-owner`).
- Non-deterministic extended attributes and nanosecond timestamps are stripped (`--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime`).
- Gzip timestamps are suppressed using `gzip -n`.

BuildKit consumes `SOURCE_DATE_EPOCH` as a special build arg since v0.11 (image config and history timestamps), and the `rewrite-timestamp=true` image exporter option, available since v0.13, also clamps file timestamps inside layers (`docs/build-repro.md`, as of 2026-11).

A reproducible tar layer creation pipeline takes the following form:

```sh
export SOURCE_DATE_EPOCH=$(git log -1 --pretty=%ct)

tar --sort=name \
    --mtime="@${SOURCE_DATE_EPOCH}" \
    --owner=0 --group=0 --numeric-owner \
    --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
    -cf layer.tar -C /src/rootfs .

gzip -n -9 < layer.tar > layer.tar.gz
```

#### Daemonless Image Builders

Modern hermetic build systems bypass the Docker daemon entirely to guarantee bit-for-bit reproducible OCI images:
- **`apko`:** A declarative, daemonless tool developed by Chainguard that builds minimal distroless OCI images directly from Alpine and Wolfi APK repositories.
It constructs tar layers with strictly pinned timestamps, sorted entries, and deterministic package hashing.
- **`ko`:** A specialized builder for Go applications that compiles binaries, generates OCI manifests, and pushes images directly to registries without a Dockerfile or Docker daemon, enforcing `mtime = 0` (January 1, 1970).
- **`rules_oci` (Bazel):** The Bazel build system models each OCI layer as a deterministic hermetic build action.
Bazel guarantees that if the source code and dependencies are identical, the generated layer tarballs and manifest digests match bit-for-bit across any build worker.

---

### 7. Layer Caching Invariants and Cache Topologies

Build execution time in continuous integration pipelines is heavily governed by layer cache efficiency.
Understanding how container builders compute cache keys allows engineers to design Dockerfiles that maximize cache hit rates.

#### Cache Key Derivation Rules

BuildKit derives cache keys by walking the LLB dependency graph from the root:
1. **Instruction String:** The exact command string (e.g., `RUN apt-get update`).
Modifying whitespace or flags invalidates the cache for that instruction and all downstream children.
2. **Parent Layer State:** The cache key of the preceding instruction.
If a parent instruction misses cache, all subsequent instructions miss cache regardless of whether their command text changed.
3. **File Checksums for `COPY` and `ADD`:** For instructions that inject files from the build context, BuildKit computes a checksum of the file contents and file metadata (mode, permissions).
If the checksum matches the cached record, the cache hits.
If any file in the copied set has modified bytes, the cache busts.

#### Invariant Ordering in Dockerfiles

To maximize caching, instructions must be organized in order of ascending change frequency:

```text
Least Frequent Changes   ^  1. Base Image Specification (FROM ...)
                         |  2. System Package Installation (RUN apt-get ...)
                         |  3. Toolchain & Build Tool Setup
                         |  4. Dependency Manifest Copying (package.json, Cargo.lock)
                         |  5. Dependency Installation & Fetching (npm ci, cargo fetch)
                         |  6. Source Code Injection (COPY src/ ...)
Most Frequent Changes    v  7. Compilation & Application Entrypoint
```

#### Cache Busting Anti-Patterns

A widespread developer mistake is copying the entire project workspace before installing dependencies:

```dockerfile
# ANTI-PATTERN: Busts dependency cache on every single code change!
COPY . /app
RUN npm install
RUN npm run build
```

Whenever an engineer edits a frontend CSS file or updates a README, the checksum of `/app` changes.
This invalidates line 1, forcing `npm install` to re-download hundreds of megabytes of dependencies on every commit.

The correct, cache-friendly pattern separates the dependency manifest from the application source code:

```dockerfile
# CORRECT PATTERN: Only busts when package-lock.json changes!
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY src/ ./src
RUN npm run build
```

#### Remote Cache Topologies

In distributed CI environments (such as GitHub Actions or Kubernetes runners), local daemon caches are ephemeral and disappear when the CI runner terminates.
BuildKit supports exporting and importing cache manifests to remote registries:

```sh
docker buildx build \
    --cache-to type=registry,ref=registry.example.com/myapp:buildcache,mode=max \
    --cache-from type=registry,ref=registry.example.com/myapp:buildcache \
    --tag registry.example.com/myapp:v1.0.0 \
    --push .
```

The `mode` parameter defines the caching scope:
- `mode=min`: Exports layer cache entries only for the stages that appear in the final output image.
- `mode=max`: Exports layer cache entries for all intermediate steps in the Dockerfile, including multi-stage build toolchains; `--mount=type=cache` contents are not exported.
The default is `mode=min` (BuildKit README).
In complex multi-stage pipelines, `mode=max` is essential to prevent CI workers from rebuilding intermediate compiler stages.

---

## Commands and Numbers to Know

Every parameter, flag default, and artifact format below reflects the production toolchain standards (as of 2026-11).

| Parameter / Command / Artifact | Type / Default Value | Target Platform | Significance and Mechanism |
| :--- | :--- | :--- | :--- |
| `application/vnd.oci.image.manifest.v1+json` | Media Type | Both | Standard OCI Image Manifest MIME type declaring image config and layer descriptors (as of 2026-11). |
| `application/vnd.oci.image.index.v1+json` | Media Type | Both | Standard OCI Image Index MIME type defining multi-architecture manifest collections (as of 2026-11). |
| `application/vnd.oci.image.config.v1+json` | Media Type | Both | OCI Image Configuration JSON MIME type storing `diff_ids` and runtime execution parameters (as of 2026-11). |
| `application/vnd.oci.image.layer.v1.tar+gzip` | Media Type | Both | POSIX tar layer compressed with gzip (RFC 1952); legacy baseline container layer format (as of 2026-11). |
| `application/vnd.oci.image.layer.v1.tar+zstd` | Media Type | Both | POSIX tar layer compressed with Zstandard (RFC 8878); high-throughput decompression format (as of 2026-11). |
| `application/vnd.oci.empty.v1+json` | Media Type | Both | Empty JSON descriptor `{}` (size 2 B, sha256 `44136fa355...`) used as config for non-container artifacts (as of 2026-11). |
| `.wh.<basename>` | File Marker | Linux | 0-byte regular file in tar diff indicating explicit deletion of `<basename>` from parent layers (as of 2026-11). |
| `.wh..wh..opq` | File Marker | Linux | 0-byte regular file in tar diff masking all inherited directory entries from lower layers (as of 2026-11). |
| `makedev(0, 0)` | Character Device | Linux | Kernel OverlayFS representation of file whiteouts in `upperdir` with major 0, minor 0 (as of 2026-11). |
| `trusted.overlay.opaque` | Extended Attribute | Linux | Kernel OverlayFS xattr set to `"y"` on directory to suppress lower layer directory merging (as of 2026-11). |
| `user.overlay.opaque` | Extended Attribute | Linux | Kernel OverlayFS xattr used in unprivileged user namespaces on Linux kernel 5.11+ (as of 2026-11). |
| `UID 65532 / GID 65532` | Identity | Linux | Standard `nonroot:nonroot` user and group ID in distroless and Chainguard base images (as of 2026-11). |
| `SOURCE_DATE_EPOCH` | Environment Variable | Both | Standardized integer Unix epoch timestamp used to clamp file mtimes for reproducible tar generation (as of 2026-11). |
| `DOCKER_BUILDKIT=1` | Environment Variable | Both | Activates BuildKit DAG compilation engine; default builder on Linux since Docker Engine 23.0 (as of 2026-11). |
| `--mount=type=cache` | Dockerfile Flag | Linux | Mounts persistent compiler and package manager caches outside layer history (as of 2026-11). |
| `--mount=type=secret` | Dockerfile Flag | Linux | Mounts host credentials into transient `tmpfs` avoiding image layer secret leaks (as of 2026-11). |
| `--cache-to type=registry,mode=max` | CLI Flag | Both | BuildKit flag exporting cache artifacts for all intermediate multi-stage layers (as of 2026-11). |

---

## Multi-Language Comparison

Packaging native binaries, bytecode runtimes, and dynamic languages into container layers requires different toolchain strategies.

| Packaging Concern | C++ (Clang / GCC / CMake) | Python (uv / pip / wheels) | Rust (Cargo / rustc) |
| :--- | :--- | :--- | :--- |
| **Compilation Strategy** | Multi-stage build with full toolchain (`gcc`, `clang`, `cmake`, `ninja`) in builder; binary stripped and copied to runtime stage. | Multi-stage build installing wheels into an isolated virtual environment (`/opt/venv`); copied to runtime stage. | Multi-stage build utilizing `cargo build --release`; compiled executable copied to runtime stage. |
| **Runtime Base Image** | `distroless/cc-debian13` (for `libgcc`), `distroless/base-debian13` (glibc), or `scratch` (if statically linked with musl). | `distroless/python3-debian13` or `debian:12-slim` with virtualenv. | `scratch` (with `x86_64-unknown-linux-musl` target) or `distroless/base-debian13` (glibc). |
| **Dependency Layer Caching** | Cache FetchContent / vcpkg build artifacts via `--mount=type=cache,target=/root/.cache/vcpkg`. | Copy `pyproject.toml` and `uv.lock` first; execute `uv sync` with `--mount=type=cache,target=/root/.cache/uv`. | Copy `Cargo.toml` and `Cargo.lock` first; build dummy target or use `cargo chef` to cache compiled crate dependencies. |
| **C Library Dependencies** | Dynamic linking requires `libc.so.6` and `ld-linux-*.so.2` matching the compile-time sysroot; static linking eliminates all shared objects. | Requires CPython interpreter binary and shared objects for native extensions (verified with `[[Python-Wheels-Platform-Tags-Manylinux-and-ABI3\|auditwheel]]`). | Compiling to target `*-unknown-linux-musl` produces a pure static binary with zero runtime libc dependencies. |
| **Attack Surface Elimination** | Zero shell, zero compiler; debug symbols stripped via `objcopy --only-keep-debug` as covered in `[[Cpp-Hardened-Release-Builds-and-Separate-Debug-Symbols\|Hardened Builds]]`. | Strips `.pyc` caches, tests, and documentation; runs strictly within dedicated unprivileged user account. | Single static executable placed in `scratch`; zero operating system files, zero libraries, zero LOLBins. |
| **Typical Runtime Size** | Binary size plus nothing on `scratch`; plus the base image (about 2 MiB for `static`, per the README) on distroless. | Dominated by the CPython runtime and site-packages; measure with `docker image inspect`. | Binary size plus nothing on `scratch`; Rust release binaries are usually the whole image. |

---

## Worked Example

This note has no lab in `code/`.
Transcript 1 below was executed on macOS (darwin arm64) on 2026-11-15 with Python 3.15.2.
Example 2 is a Dockerfile shown without captured output.
Transcript 1 was re-run independently on 2026-11-15 with the reporting block below appended; every size and digest reproduced byte for byte across two runs.

### Transcript 1: Constructing and Verifying a Valid OCI Image Layout

The script below generates a fully compliant OCI Image Format Specification v1.1.1 on-disk layout from scratch using Python.
It creates two layers: a base layer and a diff layer containing both a standard whiteout (`.wh.config.json`) and an opaque whiteout (`.wh..wh..opq`).
It computes the uncompressed `DiffID` and compressed `Layer Digest` for each layer, writes the Configuration JSON, writes the Manifest JSON, and initializes `index.json` and `oci-layout`.

```python
import os, json, hashlib, gzip, tarfile, io, shutil

workdir = "/tmp/oci-worked-example"
if os.path.exists(workdir):
    shutil.rmtree(workdir)
os.makedirs(f"{workdir}/blobs/sha256", exist_ok=True)

# 1. oci-layout
with open(f"{workdir}/oci-layout", "w") as f:
    json.dump({"imageLayoutVersion": "1.0.0"}, f)

def save_blob(data: bytes):
    digest = hashlib.sha256(data).hexdigest()
    with open(f"{workdir}/blobs/sha256/{digest}", "wb") as f:
        f.write(data)
    return f"sha256:{digest}", len(data)

# 2. Layer 1: Base layer containing /etc/hosts and /etc/config.json
buf1 = io.BytesIO()
with tarfile.open(fileobj=buf1, mode="w", format=tarfile.PAX_FORMAT) as tar:
    hosts_data = b"127.0.0.1 localhost\n"
    ti1 = tarfile.TarInfo(name="etc/hosts")
    ti1.size = len(hosts_data)
    ti1.mtime = 1700000000
    tar.addfile(ti1, io.BytesIO(hosts_data))

    cfg_data = b'{"debug": true}\n'
    ti2 = tarfile.TarInfo(name="etc/config.json")
    ti2.size = len(cfg_data)
    ti2.mtime = 1700000000
    tar.addfile(ti2, io.BytesIO(cfg_data))

layer1_tar = buf1.getvalue()
diff_id_1 = f"sha256:{hashlib.sha256(layer1_tar).hexdigest()}"
layer1_gz = gzip.compress(layer1_tar, mtime=0)
layer1_digest, layer1_size = save_blob(layer1_gz)

# 3. Layer 2: Diff layer with file whiteout and opaque directory whiteout
buf2 = io.BytesIO()
with tarfile.open(fileobj=buf2, mode="w", format=tarfile.PAX_FORMAT) as tar:
    wh1 = tarfile.TarInfo(name="etc/.wh.config.json")
    wh1.size = 0
    wh1.mtime = 1700000000
    tar.addfile(wh1, io.BytesIO(b""))

    opq = tarfile.TarInfo(name="var/log/.wh..wh..opq")
    opq.size = 0
    opq.mtime = 1700000000
    tar.addfile(opq, io.BytesIO(b""))

layer2_tar = buf2.getvalue()
diff_id_2 = f"sha256:{hashlib.sha256(layer2_tar).hexdigest()}"
layer2_gz = gzip.compress(layer2_tar, mtime=0)
layer2_digest, layer2_size = save_blob(layer2_gz)

# 4. Image Configuration JSON
config = {
    "created": "2026-11-15T00:00:00Z",
    "architecture": "arm64",
    "os": "linux",
    "config": {
        "User": "65532:65532",
        "Env": ["PATH=/usr/local/bin:/usr/bin:/bin"],
        "Entrypoint": ["/app/server.py"],
        "WorkingDir": "/app"
    },
    "rootfs": {
        "type": "layers",
        "diff_ids": [diff_id_1, diff_id_2]
    },
    "history": [
        {"created": "2026-11-15T00:00:00Z", "created_by": "ADD base files", "comment": "base"},
        {"created": "2026-11-15T00:00:00Z", "created_by": "DELETE config.json and clear logs", "comment": "cleanup"}
    ]
}
config_bytes = json.dumps(config, indent=2).encode("utf-8")
config_digest, config_size = save_blob(config_bytes)

# 5. Image Manifest JSON
manifest = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": config_digest,
        "size": config_size
    },
    "layers": [
        {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": layer1_digest,
            "size": layer1_size
        },
        {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": layer2_digest,
            "size": layer2_size
        }
    ]
}
manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
manifest_digest, manifest_size = save_blob(manifest_bytes)

# 6. Root index.json
index = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": manifest_digest,
            "size": manifest_size,
            "platform": {
                "architecture": "arm64",
                "os": "linux"
            },
            "annotations": {
                "org.opencontainers.image.ref.name": "v1.0.0"
            }
        }
    ]
}
with open(f"{workdir}/index.json", "w") as f:
    json.dump(index, f, indent=2)

# 7. Report sizes and digests
print("=== OCI LAYOUT STRUCTURE ===")
for name in ["index.json", "oci-layout"]:
    print(f"{name:<40} {os.path.getsize(f'{workdir}/{name}'):>6} B")
for b in sorted(os.listdir(f"{workdir}/blobs/sha256")):
    print(f"blobs/sha256/{b} {os.path.getsize(f'{workdir}/blobs/sha256/{b}'):>5} B")
print()
print("=== DIFF_IDS VS LAYER DIGESTS ===")
print("Layer 1 DiffID (uncompressed tar) :", diff_id_1)
print("Layer 1 Digest (compressed blob)  :", layer1_digest)
print("Layer 2 DiffID (uncompressed tar) :", diff_id_2)
print("Layer 2 Digest (compressed blob)  :", layer2_digest)
print("Config Digest (ImageID)           :", config_digest)
print("Manifest Digest                   :", manifest_digest)
```

Execution Output (darwin-arm64, Python 3.15.2, as of 2026-11):

```text
=== OCI LAYOUT STRUCTURE ===
index.json                                  460 B
oci-layout                                   31 B
blobs/sha256/1f656bbd158e2172197a4cd4b6258cc58cfcdd5d87e62bae18143e6a5eed60e7   142 B
blobs/sha256/5ce89552d2bc8f22253a1e75900e16387732cd0ddc0d4ea80756abc50274dd53   175 B
blobs/sha256/725e6abf8167ffb9017525659e2d3b0a09e80a8c123be475b1e47d77f355c43f   664 B
blobs/sha256/db25254fc33b1658591a6ac9e901e42180b5129c37ee3773367481cffd0a32f2   765 B

=== DIFF_IDS VS LAYER DIGESTS ===
Layer 1 DiffID (uncompressed tar) : sha256:f7312b774f8fabf5d26ecfcc5bb955318043f42b420bd641b21ae4bd032308cf
Layer 1 Digest (compressed blob)  : sha256:5ce89552d2bc8f22253a1e75900e16387732cd0ddc0d4ea80756abc50274dd53
Layer 2 DiffID (uncompressed tar) : sha256:6a5761e625c2459724fc2ceb7eb94c2d873b78bd330164b87d4a42fae1f9fe85
Layer 2 Digest (compressed blob)  : sha256:1f656bbd158e2172197a4cd4b6258cc58cfcdd5d87e62bae18143e6a5eed60e7
Config Digest (ImageID)           : sha256:db25254fc33b1658591a6ac9e901e42180b5129c37ee3773367481cffd0a32f2
Manifest Digest                   : sha256:725e6abf8167ffb9017525659e2d3b0a09e80a8c123be475b1e47d77f355c43f
```

---

### Example 2: Multi-Stage Build into a `scratch` Rootfs

This example is not a captured run: container checks were paused when this note was finished, so no output is shown for it and none is claimed.
This scenario compiles a static C binary inside a multi-stage Dockerfile using Alpine and GCC, then transfers exclusively the compiled binary into a `scratch` rootfs.

`main.c`:
```c
#include <stdio.h>
int main(void) {
    printf("Distroless C binary running\n");
    return 0;
}
```

`Dockerfile`:
```dockerfile
# Stage 1: Compiler stage with Alpine build dependencies
FROM alpine:3.20 AS builder
RUN apk add --no-cache build-base
WORKDIR /src
COPY main.c .
RUN gcc -static -O3 main.c -o myapp

# Stage 2: Distroless scratch runtime
FROM scratch
COPY --from=builder /src/myapp /bin/myapp
ENTRYPOINT ["/bin/myapp"]
```

To try it, run:

```sh
docker build -t test-distroless:v1 .
docker image inspect --format '{{.Size}}' test-distroless:v1
docker run --rm test-distroless:v1
```

The expected behavior follows from the mechanism, not from a recorded run: the `builder` stage holds the `build-base` toolchain (`gcc`, `musl-dev`, `binutils`, `make`), but only the `COPY --from=builder` layer lands in the final image, so the image contains one layer with one static binary, no shell, and no package database.
The container prints `Distroless C binary running`.

---

## Pitfalls

> [!warning] Pitfalls:
> 1. **Chained Run Deletion Anti-Pattern:**
> Running `RUN apt-get update && apt-get install -y gcc` followed by `RUN rm -rf /var/lib/apt/lists/*` in a subsequent Dockerfile line does not reduce image size.
> The package cache is permanently committed to the first layer blob.
> The second layer only stores `.wh.*` whiteouts.
> Always combine download and cleanup operations in a single `RUN` command, or use BuildKit cache mounts (`--mount=type=cache,target=/var/lib/apt/lists`).
>
> 2. **Leaking Credentials via Build Arguments:**
> Supplying secrets via `ARG GITHUB_TOKEN` or `docker build --build-arg TOKEN=...` exposes the secret to anyone who pulls the image.
> The Dockerfile reference warns that build arguments are visible in `docker history` and in `max` mode provenance attestations; values used by `RUN` land in the Image Configuration `history`, and in `config.Env` if copied into an `ENV`.
> Inspecting the image with `docker history --no-trunc` or reading `config.json` immediately exposes the secret.
> Always use `--mount=type=secret,id=token` to mount credentials into transient in-memory tmpfs mounts.
>
> 3. **Layer Cache Busting via Premature Source Copy:**
> Executing `COPY . .` before downloading language dependencies causes every minor file modification (such as editing documentation or local tests) to bust the cache for `RUN npm ci` or `RUN cargo build`.
> Always copy dependency manifests (`package.json`, `Cargo.lock`, `uv.lock`) independently and fetch dependencies in an isolated layer before copying application source code.
>
> 4. **Host User ID and Group ID Contamination:**
> Dockerfile `COPY` from the build context writes UID and GID 0 unless `--chown` is given, but hand-rolled layer tarballs, `COPY --from` of files a builder stage created as another user, and non-Dockerfile image tools can bake the local developer's host UID and GID (e.g. UID 501 on macOS or UID 1000 on Linux) into the layer tar headers.
> When run on production Kubernetes nodes under strict security policies, permission mismatches cause file access crashes (`EACCES`).
> Standardize ownership by setting `--chown=65532:65532` on `COPY` commands or enforcing `--owner=0 --group=0` during tarball generation.
>
> 5. **Missing Root Certificates and Timezone Files in `scratch`:**
> Copying a statically compiled binary into `FROM scratch` without copying Root CA certificates causes outbound HTTPS requests to crash with `x509: certificate signed by unknown authority`.
> Failing to copy `/usr/share/zoneinfo` causes date formatting calls to crash or fall back silently to UTC.
> Always copy `/etc/ssl/certs/ca-certificates.crt` and `/usr/share/zoneinfo` from a builder stage, or use `gcr.io/distroless/static-debian13`.
>
> 6. **Dynamic Linking Failures in Minimal Runtimes:**
> Compiling a binary on a Debian builder and copying it to Alpine or `scratch` results in a confusing startup error: `exec user process caused: no such file or directory`.
> This error does not mean the application binary is missing.
> It indicates that the Linux kernel's ELF program interpreter declared in the binary's `.interp` section (such as `/lib64/ld-linux-x86-64.so.2`) is absent in the target rootfs.
> Verify dynamic library dependencies with `ldd` or build statically using `-static` with `musl`.

---

## At Scale

Operational decisions regarding container layers, caching, and runtime base images shift drastically as an engineering organization scales.

```text
Team Scale        Architecture & Governance Model
----------        -------------------------------
1 - 10 Devs       Local Docker daemon, single-stage or basic multi-stage builds.
                  Standard base images (Ubuntu, Alpine); local Docker layer cache.

10 - 200 Devs     Standardized multi-stage Dockerfiles across team microservices.
                  Remote registry layer caching (BuildKit --cache-to type=registry,mode=max).
                  Automated CI vulnerability scanning with Trivy or Grype gating PRs.

200 - 5,000 Devs  Golden Base Image catalog maintained by dedicated platform security team.
                  Mandatory distroless runtime roots (`distroless/static`, `distroless/base`).
                  Ephemeral debug container workflows (`kubectl debug`) standardized.
                  Daemonless build pipelines (apko, ko) integrated into centralized CI runners.

5,000 - 50,000+   Hermetic, bit-for-bit reproducible container generation with Bazel rules_oci.
                  Seekable lazy-pulling container images (zstd:chunked / eStargz) across clusters.
                  Peer-to-peer (P2P) image distribution networks (Kraken, Dragonfly) to eliminate
                  bandwidth bottlenecks during simultaneous fleet-wide rollouts.
                  Cryptographic supply chain admission controllers validating Cosign signatures
                  and SLSA provenance attestations before pod scheduling.
```

### The P2P Distribution Mandate

At large scale (5,000 to 50,000+ nodes), triggering a production release across thousands of machines simultaneously generates a massive network traffic spike against central artifact registries.
If an engineering team deploys a 500 MB container image across a 10,000-node cluster, the central registry must serve 5 Terabytes of data within seconds.
Central registries saturate network interface cards (NICs), throttle requests, and cause cascading deployment timeouts.
Organizations solve this at the architectural level:
- Implementing peer-to-peer (P2P) distribution layers like Uber's Kraken or CNCF Dragonfly.
When nodes download layer blobs, they act as seeders for neighboring nodes in the same top-of-rack (ToR) switch, reducing the load on the central registry; Kraken's README reports distributing a 3 GB image to 2,600 hosts with p50 of 10 s and p99 of 18 s under a 300 MB/s per-agent limit (uber/kraken README, as of 2026-11).
- Enforcing distroless and scratch roots to shrink total image transfer size from 500 MB to under 20 MB per service.
- Transitioning to seekable chunked compression formats (`zstd:chunked`), allowing pods to boot after downloading only the bytes they read (Slacker measured 6.4% of pulled data read at startup).

---

## Practice Drill

> [!tip] Practice Drill:
> Execute the following four-step exercise on a development machine or Linux VM to master OCI layer mechanics:
>
> 1. **Inspect Raw Layer Blobs:**
> Pull an existing small image using `docker pull alpine:3.20`.
> Save it to an archive using `docker save alpine:3.20 -o alpine.tar`.
> Extract `alpine.tar` into a directory and locate the layer tarball and `manifest.json`.
> Use `tar -tvf` to inspect the contents of the layer and identify the file permissions and ownership.
>
> 2. **Construct a Manual Whiteout Layer:**
> Create a scratch directory `/tmp/test-layer`.
> Inside it, create an empty file named `.wh.etc` and a file named `app/.wh..wh..opq`.
> Package the directory with `tar -cvf layer.tar -C /tmp/test-layer .`.
> Compute the SHA-256 hash using `sha256sum layer.tar` (this is the `DiffID`).
> Compress it with `gzip -n -9 < layer.tar > layer.tar.gz` and compute `sha256sum layer.tar.gz` (this is the `Layer Digest`).
>
> 3. **Implement a Minimal Multi-Stage Build:**
> Write a C application that makes an outbound HTTPS request using `libcurl`.
> Create a multi-stage Dockerfile that compiles the binary in `debian:13-slim` and transfers it to `gcr.io/distroless/base-debian13:nonroot`, so the builder and runtime glibc match.
> Ensure all required shared library `.so` files are located with `ldd` and copied into `/usr/lib/`.
> Run the resulting image and verify that HTTPS succeeds and that attempting to run `docker exec ... /bin/sh` fails with `stat /bin/sh: no such file or directory`.
>
> 4. **Enforce Deterministic Tar Output:**
> Export `SOURCE_DATE_EPOCH=1700000000`.
> Package a directory twice with GNU tar on Linux using the reproducible tar flags from the `SOURCE_DATE_EPOCH` Normalization Standard subsection of this note.
> Verify that `sha256sum` produces identical hashes on both output archives.

---

## Interview Questions

### Question 1 (L5): OCI Layer Digest versus DiffID

What is the technical distinction between a `Layer Digest` and a `DiffID` in the OCI Image Specification, and why does the specification maintain both?

> [!success]- Answer
> A `Layer Digest` is the cryptographic SHA-256 hash of the *compressed* layer blob (e.g., `tar.gz` or `tar.zst`).
> It is stored in the Image Manifest's `layers` array (`manifest.layers[i].digest`).
> The Layer Digest serves as the content-addressable key for transferring layers over the network and storing blobs in remote container registries.
>
> A `DiffID` is the cryptographic SHA-256 hash of the *uncompressed* filesystem tar archive.
> It is stored in the Image Configuration blob (`config.rootfs.diff_ids[i]`).
> The DiffID identifies the exact, uncompressed filesystem delta that the container runtime applies when assembling the container rootfs.
>
> The specification maintains both to decouple wire transport optimizations from image identity.
> If a registry or proxy recompresses an image from gzip to zstd to accelerate decompression, or adjusts gzip compression levels, the compressed `Layer Digest` changes.
> However, because the uncompressed filesystem contents remain byte-for-byte identical, the `DiffID` remains unchanged.
> Since the configuration hash (`ImageID`) is calculated from the configuration JSON containing `diff_ids`, the image identity remains invariant across compression migrations.

---

### Question 2 (L5): BuildKit Stage Evaluation

How does Docker BuildKit determine which stages to execute in a multi-stage Dockerfile, and how does this differ from the legacy Docker engine builder?

> [!success]- Answer
> The legacy Docker builder executed Dockerfiles strictly sequentially, line by line, from the first instruction to the last.
> Every stage in a multi-stage Dockerfile was built in linear order, regardless of whether downstream stages actually consumed its outputs.
>
> BuildKit compiles the Dockerfile into a directed acyclic graph (DAG) of Low-Level Builder (LLB) operations.
> BuildKit begins evaluation at the target stage (specified by `--target` or defaulting to the final stage in the Dockerfile) and walks backward through the dependency graph.
>
> As a result:
> 1. **Dead Stage Pruning:** Any stage whose outputs are not transitively referenced by the target stage is pruned from the graph and never pulled, scheduled, or executed.
> 2. **Parallel Stage Execution:** Independent stages whose dependency branches do not depend on each other execute concurrently across multiple CPU cores, drastically accelerating build pipelines.

---

### Question 3 (L6 - Predict the Output): Chained Layer Deletions

Predict the layer contents, resulting manifest structure, and relative size when building the following two Dockerfiles:

```dockerfile
# Dockerfile A
FROM alpine:3.20
RUN dd if=/dev/urandom of=/bigfile bs=1M count=50
RUN rm -f /bigfile
```

```dockerfile
# Dockerfile B
FROM alpine:3.20 AS temp
RUN dd if=/dev/urandom of=/bigfile bs=1M count=50

FROM alpine:3.20
COPY --from=temp /etc/alpine-release /etc/alpine-release
```

> [!success]- Answer
> **Dockerfile A Output:**
> The resulting image has 3 layers (1 base Alpine layer + 2 diff layers).
> - Layer 1: Base Alpine rootfs (~3.5 MB compressed).
> - Layer 2: Contains the 50 MB `/bigfile` payload.
> - Layer 3: Contains a 0-byte whiteout marker `.wh.bigfile`.
> Total image size: ~53.5 MB.
> The `/bigfile` payload is permanently retained in Layer 2.
> When the container runs, OverlayFS uses `.wh.bigfile` to hide the file from userspace, but the 50 MB is still downloaded over the network and stored on disk.
>
> **Dockerfile B Output:**
> The resulting image has 2 layers (1 base Alpine layer + 1 diff layer).
> - Layer 1: Base Alpine rootfs (~3.5 MB compressed).
> - Layer 2: Contains only `/etc/alpine-release` (a few bytes).
> Total image size: ~3.5 MB.
> BuildKit builds the `temp` stage, creates `/bigfile`, copies `/etc/alpine-release`, and completely discards the `temp` stage's rootfs.
> The 50 MB `/bigfile` is never serialized into any layer of the final image.

---

### Question 4 (L6 - Debug This): The Missing Dynamic Interpreter

A Go microservice compiles and runs properly inside an `alpine:3.20` container.
To harden the image, the engineer switches to `FROM scratch` in a multi-stage build:

```dockerfile
FROM golang:1.24-alpine AS builder
RUN apk add --no-cache build-base
WORKDIR /app
COPY . .
RUN go build -o server .

FROM scratch
COPY --from=builder /app/server /server
ENTRYPOINT ["/server"]
```

The container builds successfully.
However, when launched with `docker run --rm server:latest`, the container immediately crashes with:

```text
standard_init_linux.go:228: exec user process caused: no such file or directory
```

Explain the root cause of this failure and provide the exact fix.

> [!success]- Answer
> **Root Cause:**
> The error message `exec user process caused: no such file or directory` is emitted by the container runtime (`runc`, from `standard_init_linux.go`); newer runtime releases print the path instead, as `exec /server: no such file or directory`, and this note does not date that wording change.
> It does *not* mean that the `/server` binary is missing.
> It indicates that the Linux kernel failed to locate the **ELF dynamic program interpreter** declared in the binary's `.interp` section.
>
> Since Go 1.20, cgo is enabled by default only when a C compiler is on the path; the `build-base` line installs `gcc`, so cgo is on, and a service importing `net` links the cgo resolver dynamically against `musl libc`, generating an ELF binary with an interpreter path pointing to `/lib/ld-musl-x86_64.so.1` (or `aarch64`).
> Without `build-base`, the stock `golang:alpine` image has no C toolchain and the same `go build` would produce a static binary that runs on `scratch`.
> When the binary is copied to `FROM scratch`, the root filesystem is completely empty.
> There is no `/lib` directory and no `ld-musl.so` dynamic loader.
> When the kernel issues the `execve()` system call, it reads the ELF header, attempts to load `/lib/ld-musl-x86_64.so.1`, finds that the loader does not exist, and returns `-ENOENT`.
>
> **Fix:**
> Compile the Go binary as a completely static executable by disabling cgo and passing static linker flags:
>
> ```dockerfile
> RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o server .
> ```
>
> With `CGO_ENABLED=0`, Go produces a pure static ELF binary with no `.interp` header and zero shared library dependencies, allowing it to execute successfully on an empty `scratch` filesystem.
> Alternatively, if dynamic cgo is strictly required, the runtime stage must use a base image containing the corresponding dynamic loader: `alpine:3.20` for a musl build, or `gcr.io/distroless/base-debian13` only for a glibc build from a Debian builder.

---

### Question 5 (L6): Kernel OverlayFS Deletion Mechanics

How does the Linux kernel's OverlayFS driver represent file deletions and directory replacements across stacked layers, and how does a container runtime bridge this with OCI tar archives?

> [!success]- Answer
> OverlayFS operates on a hierarchy of host directories: one or more read-only `lowerdir` paths, one read-write `upperdir` path, and a unified `merged` mount.
>
> 1. **File Deletions:**
> In OverlayFS, deleting a file that exists in a lower layer creates a special character device in the `upperdir` with major number 0 and minor number 0 (`makedev(0, 0)`).
> When userspace attempts to look up or open that filename, the OverlayFS kernel driver checks the upper directory, encounters the 0/0 character device, terminates the search, and returns `-ENOENT`, masking the lower-layer file.
>
> 2. **Directory Replacements (Opaque Directories):**
> When a directory in an upper layer must hide all inherited contents from lower layers, OverlayFS sets an extended attribute on the upper directory: `trusted.overlay.opaque="y"` (or `user.overlay.opaque="y"` in unprivileged user namespaces).
> During directory reading (`ovl_iterate()`), the kernel detects this attribute and ignores matching directories in lower layers.
>
> 3. **OCI Tar Translation:**
> Container archives cannot store live kernel mounts.
> The OCI specification defines serialization conventions:
> - A file whiteout is stored in the tar archive as a 0-byte regular file named `.wh.<basename>`.
> - A directory opacity marker is stored as a 0-byte regular file named `.wh..wh..opq` inside the directory.
>
> When `containerd` or `CRI-O` unpacks an OCI layer onto the host disk:
> - It converts `.wh.<basename>` into `mknod(<basename>, S_IFCHR, makedev(0, 0))`.
> - It converts `.wh..wh..opq` into `setxattr(dir, "trusted.overlay.opaque", "y")`.
> Conversely, when `docker commit` or BuildKit diffs an upper layer into an OCI tarball, it scans for 0/0 character devices and opaque xattrs and serializes them back into `.wh.*` files.

---

### Question 6 (L7): Secrets Exfiltration via Build Arguments

A developer passes a GitHub personal access token to a Docker build using `docker build --build-arg GITHUB_TOKEN=ghp_EXAMPLE...`.
Demonstrate how an attacker with read-only pull access to the resulting published container image can extract this token, and write the secure BuildKit alternative.

> [!success]- Answer
> **Exfiltration Mechanism:**
> `ARG` values are not secrets.
> When a Dockerfile declares `ARG GITHUB_TOKEN`, BuildKit embeds the evaluated build arguments into the image's Configuration JSON metadata.
> Specifically:
> 1. The full command string, including the expanded argument, is written to the `history.created_by` array in the Image Configuration JSON.
> 2. If the variable is referenced in an `ENV` instruction or inherited by children, it persists in the `config.Env` string array.
>
> An attacker with pull access executes:
>
> ```sh
> docker history --no-trunc my-image:v1 | grep GITHUB_TOKEN
> ```
>
> Or pulls the configuration blob directly:
>
> ```sh
> crane config my-image:v1 | jq '.history[] | .created_by'
> ```
>
> The token is visible in plaintext in the JSON metadata, even if the developer executed `RUN unset GITHUB_TOKEN` in the very next line.
>
> **Secure Alternative:**
> Use BuildKit secret mounts.
> Secrets are passed via the CLI using `--secret` and mounted exclusively into a transient in-memory tmpfs mount:
>
> ```dockerfile
> # Dockerfile
> RUN --mount=type=secret,id=gh_token \
>     TOKEN=$(cat /run/secrets/gh_token) && \
>     git clone https://x-access-token:${TOKEN}@github.com/org/privaterepo.git /src
> ```
>
> Build invocation:
>
> ```sh
> docker buildx build --secret id=gh_token,src=./token.txt -t my-image:v1 .
> ```
>
> The secret exists exclusively in memory during the execution of that specific process.
> It is never written to disk, never committed to a tar layer, and never recorded in the Image Configuration JSON history.

---

### Question 7 (L7): Deterministic OCI Image Generation

Explain the technical factors that cause `docker build` to produce non-deterministic image digests across different build machines, and explain how an enterprise build toolchain enforces bit-for-bit reproducibility.

> [!success]- Answer
> Non-deterministic image digests stem from five distinct entropy sources:
>
> 1. **Timestamp Drift:** Files copied from the host filesystem retain modification timestamps (`mtime`).
> Standard tar archives encode mtimes in their 512-byte headers.
> Two identical builds executed at different seconds produce different tar bytes, altering the `DiffID` and the `Layer Digest`.
>
> 2. **Directory Inode Traversal Order:** The `readdir()` system call iterates files according to the underlying filesystem's directory indexing structure (e.g. ext4 directory hash trees or XFS B-trees).
> Copying a directory on different machines or filesystem types yields different tar entry sequences, altering the archive hash.
>
> 3. **User and Group ID Contamination:** Dockerfile `COPY` from the build context writes UID and GID 0, but hand-rolled tar layers and other image tools record host file ownership.
> If Worker A runs with UID 1000 and Worker B runs with UID 501, different owner numbers are written into the tar headers.
>
> 4. **Compression Headers:** The gzip header (RFC 1952) embeds a 32-bit timestamp in bytes 4 through 7.
> Different builds produce different compressed digests even if the underlying tar stream is identical.
>
> 5. **Unpinned Upstream Dependencies:** Commands like `apt-get install` or `pip install` without cryptographic hash pinning fetch rolling updates from remote mirrors.
>
> **Enterprise Enforcement:**
> 1. Set `SOURCE_DATE_EPOCH` to a fixed timestamp (such as the Git commit timestamp `git log -1 --pretty=%ct`).
> 2. Force tar creation to sort entries alphabetically (`--sort=name`), clamp mtimes to `SOURCE_DATE_EPOCH`, and normalize ownership (`--owner=0 --group=0 --numeric-owner`).
> 3. Strip variable PAX headers using `--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime`.
> 4. Compress with `gzip -n` to omit timestamps and filenames from gzip headers.
> 5. Utilize daemonless, hermetic OCI generators (such as Bazel's `rules_oci`, `apko`, or `ko`) that construct OCI layer tarballs directly from pinned dependency graphs with zero host state contamination.

---

### Question 8 (L8+): AI/ML Image Cold-Start Architecture at Scale

You are the Principal Infrastructure Architect for an AI inference platform operating 20,000 GPU nodes.
Large model serving images (30 GB to 60 GB, bundling PyTorch, CUDA libraries, and weights) cause 15-minute pod cold-start latencies and saturate top-of-rack switches during auto-scaling events.
Design an end-to-end container packaging, registry, and runtime architecture to achieve sub-10-second pod cold-start times.

> [!success]- Answer
> An expert architecture decouples model weight storage from base code runtimes, transitions layer serialization to seekable formats, and implements peer-to-peer distribution.
>
> 1. **Decouple Weights from Container Images:**
> Large model weights (tensors) must not be packaged inside OCI tar layers.
> Storing weights in tar archives forces full sequential download and extraction, and stores them twice on each node (compressed blob plus unpacked snapshot).
> Mount model weights out-of-band via high-throughput read-only volume mounts (e.g., distributed caching filesystems like JuiceFS or local NVMe cache daemons backed by S3/GCS) or OCI Artifact descriptors utilizing the `urls` property pointing to external object stores.
>
> 2. **Adopt Seekable, Chunked Layer Compression (`zstd:chunked` or eStargz):**
> Package the base container (CUDA, Python, PyTorch) using `zstd:chunked` layers compliant with OCI v1.1.0.
> The `zstd:chunked` format embeds a Table of Contents (TOC) inside Zstandard skippable frames, mapping file paths to byte ranges.
>
> 3. **Deploy Lazy-Pull Snapshotters on Cluster Nodes:**
> Configure Kubernetes worker nodes with containerd's `stargz-snapshotter` or `nydus-snapshotter`.
> When a pod is scheduled, the snapshotter downloads only the TOC metadata (several kilobytes) via HTTP Range requests.
> It creates an OverlayFS mount immediately, allowing the container to execute `entrypoint` in under 3 seconds.
> The runtime reads CUDA `.so` files and Python modules on-demand over the network as the process references them.
>
> 4. **Implement Peer-to-Peer (P2P) Distribution:**
> Deploy an enterprise P2P layer caching topology (such as CNCF Dragonfly or Uber Kraken) across cluster racks.
> When hundreds of GPU nodes in the same datacenter pull identical chunks concurrently, nodes stream chunks directly from peer nodes on the same local switch, eliminating central registry egress saturation.
>
> 5. **Base Image Rationalization:**
> Build the core runtime using a minimal distroless base image (`distroless/cc-debian13`) stripped of compiler tools, man pages, and test suites, minimizing the required initial footprint.

---

## Further Reading

- Open Container Initiative, *OCI Image Format Specification v1.1.1*, 2025: The authoritative specification defining image manifests, configuration schemas, layer tar formats, and content descriptors.
- Open Container Initiative, *OCI Image Layer Filesystem Changeset Specification v1.1.1*, 2025: The formal specification detailing tar formatting, standard whiteout files (`.wh.*`), and opaque directory whiteouts (`.wh..wh..opq`).
- Linux Kernel Organization, *Overlay Filesystem Documentation (overlayfs.rst)*, Linux Kernel 6.12+, 2026: The kernel documentation detailing `lowerdir`, `upperdir`, character device 0/0 whiteouts, and `trusted.overlay.opaque` extended attributes.
- Moby Project, *BuildKit LLB operation definitions (solver/pb/ops.proto)*, 2026: The Protocol Buffer definitions of the LLB operations (`ExecOp`, `SourceOp`, `FileOp`, `BuildOp`, `MergeOp`, `DiffOp`) that Dockerfiles compile into.
- Google Container Tools, *Distroless Container Images*, 2026: The foundational open-source project defining minimalist container runtimes stripped of shells and package managers.
- Reproducible Builds Project, *SOURCE_DATE_EPOCH Specification*, 2026: The universal standard for eliminating timestamp entropy to achieve bit-for-bit reproducible binary archives.
- IETF, *RFC 8878 - Zstandard Compression and The 'application/zstd' Media Type*, 2021: The technical standard governing Zstandard compression, framing, and dictionary mechanisms.
- Containerd Project, *Stargz Snapshotter: Lazy Image Pulling with eStargz and zstd:chunked*, 2026: High-performance container snapshotter architecture enabling sub-second pod cold starts via HTTP Range requests.

---

## Related

- Prerequisites: [[Lockfiles-Vendoring-and-Offline-Mirrors]]
- Next: [[Progressive-Delivery-Rollbacks-and-Feature-Flags]]
- See also: [[04-System-Design/00-Concepts/Virtual-Machines-vs-Containers|Virtual Machines vs Containers]], [[04-System-Design/07-Infrastructure-and-Orchestration/Docker-and-Container-Runtimes|Docker and Container Runtimes]]
- Sibling notes in Packaging and Release:
  - Artifact lifecycle and registry immutability: [[Release-Trains-Backports-and-Artifact-Registries]]
  - Packaging strategy trade-offs: [[Packaging-Decision-Records]]
  - Binary stripping and hardened compilation: [[Cpp-Hardened-Release-Builds-and-Separate-Debug-Symbols]]
  - Static compilation and cross-compilation: [[Rust-Binary-Distribution-with-dist-and-Cross-Compilation]]
  - Python application packaging alternatives: [[Python-Shipping-Applications-Zipapp-PEX-and-Bundlers]]
