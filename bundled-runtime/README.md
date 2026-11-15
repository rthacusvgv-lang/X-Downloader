# Bundled runtime

`linux-x86_64.tar.gz` and `linux-aarch64.tar.gz` hold the Python and Tk that X-Downloader runs on, so that a
project folder freshly downloaded from GitHub works with nothing installed. `start_gui.sh` unpacks the one
for this CPU into `.runtime/` on the first launch.

Each bundle carries Python, Tcl/Tk with Xft, CustomTkinter, the CA certificates, and all shared libraries down to
glibc and its dynamic loader. `bin/python` starts the interpreter through that loader, so the bundle uses nothing
from the host's userland: it also runs on systems with another C library (Alpine) or without the usual folders
(NixOS). It only needs a Linux kernel and an X server (X11 or XWayland).

## Rebuilding

    tools/build-runtime.sh x86_64
    tools/build-runtime.sh aarch64

Needs podman or docker. Every rebuild adds the size of the bundles (about 33 MB) to the Git history, so rebuild only
for one of the reasons below. `tests/test_runtime.py` checks the bundle of the machine it runs on.

### When to rebuild

The bundles never update themselves. Python, OpenSSL, SQLite (it reads the browsers' cookie databases), the X and
font libraries and the CA certificate list stay as they were on the day of the build, while the host's copies get
security updates. Rebuild when:

- Debian has published a newer version of a package the bundle contains (a security update or a point release), or a
  new CA certificate list. Run this from the project folder now and then: it prints every package with a newer version
  and whether the CA list changed, and nothing while the bundle is current.

  ```sh
  podman run --rm -v "$PWD/bundled-runtime:/runtime:ro" docker.io/library/debian:13 sh -c '
    apt-get update -qq && apt-get install -y -qq ca-certificates >/dev/null 2>&1 &&
    tar -xzOf /runtime/linux-x86_64.tar.gz ./PACKAGES.txt | grep _ |
    while IFS=_ read -r pkg ver arch; do
      [ "$arch" = all ] && name=$pkg || name=$pkg:$arch
      new=$(apt-cache policy "$name" | sed -n "s/ *Candidate: //p")
      [ "$ver" = "$new" ] || echo "$pkg $ver -> $new"
    done
    tar -xzOf /runtime/linux-x86_64.tar.gz ./etc/cacert.pem | cmp -s - /etc/ssl/certs/ca-certificates.crt ||
      echo "etc/cacert.pem differs from the current CA list"'
  ```

  Rebuild both bundles together: then they hold the same package versions and checking the x86_64 one covers both.
- `requirements.txt` changed.

## Licenses

The bundles redistribute Debian packages. `PACKAGES.txt` inside each one lists, with their exact versions, the
packages that have a file in the bundle, the CA certificate list (`ca-certificates`) and the Python packages. Their
license texts are in the bundle as well, and in `.runtime/<CPU>/` after the first start:

- `share/doc/<package>/copyright`: copyright and license of each Debian package, as Debian ships them.
- `share/common-licenses/`: the full texts those files point to (they say "see /usr/share/common-licenses/GPL-2").
- `lib/python3.13/LICENSE.txt` and `lib/python3.13/*.dist-info/`: Python's license and those of the Python packages.
  The Roboto font that CustomTkinter brings states in its own metadata that it is under the Apache License 2.0, whose
  text is in `share/common-licenses/`.

The FreeType License, under which FreeType (it draws the text in the window, through Xft) comes, asks the
documentation of every binary distribution to carry this credit:

> Portions of this software are copyright © 2024 The FreeType Project (www.freetype.org). All rights reserved.

glibc is under the LGPL, libgcc_s under the GPL with the GCC Runtime Library Exception. readline (GPL) and Berkeley
DB (Sleepycat license) are left out: the program does not use them.

The libraries in the bundles are the unmodified files of the Debian packages, so the source of those packages is their
source. It is not part of this repository: snapshot.debian.org keeps every version Debian has published, including
each version `PACKAGES.txt` lists. Open `https://snapshot.debian.org/binary/<package>/`, pick the version, and its
entry links to the source package, which lists the source files (`.dsc`, `.orig.tar.*`, `.debian.tar.*`). The source
package can have another name than the binary one (`libc6` is built from `glibc`, `libgcc-s1` from `gcc-14`,
`libssl3t64` from `openssl`), and a version that ends in `+b1`, `+b3` ... is a rebuild of the source version without
that ending.
