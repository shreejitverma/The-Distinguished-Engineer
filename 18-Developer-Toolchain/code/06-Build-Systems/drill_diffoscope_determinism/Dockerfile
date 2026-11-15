# Image for the diffoscope drill: GCC, rustc, and diffoscope from Ubuntu 24.04.
# The base is the same multi-arch index digest as code/_harness/linux-labs.Dockerfile.
# diffoscope-minimal is diffoscope without its large optional dependency set; the ELF,
# ar, tar, and gzip comparators the drill needs come from binutils, file, and libarchive.
# The image tag is a hash of this file, so editing it forces a rebuild.
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
  && apt-get install -y -qq --no-install-recommends binutils diffoscope-minimal file g++ gcc python3 rustc xz-utils >/dev/null \
  && rm -rf /var/lib/apt/lists/* \
  && dpkg-query -W -f='${Package} ${Version}\n' binutils diffoscope-minimal g++ rustc tar gzip | sort > /etc/drill-versions
RUN useradd --create-home lab && mkdir /work && chown lab:lab /work
USER lab
WORKDIR /home/lab
