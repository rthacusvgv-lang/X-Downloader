#!/bin/bash
# Builds bundled-runtime/linux-<arch>.tar.gz: the Python and Tk that let the project run without installing anything.
#
#   tools/build-runtime.sh x86_64        (or aarch64; needs podman or docker)
#
# The bundle is made of Debian stable packages, which are only unpacked, never run, so one machine can build
# for every CPU. It also carries glibc and its dynamic loader: bin/python starts the interpreter through that
# loader, so the bundle needs nothing from the host's userland (it even runs on musl systems and on NixOS).
#
# python-build-standalone is not an option: its Tk has no Xft, so Chinese text and CustomTkinter's rounded
# corners cannot be drawn on a normal desktop.
set -eu

arch=${1:?usage: tools/build-runtime.sh x86_64|aarch64}
case "$arch" in
    x86_64)  debian_arch=amd64 loader=ld-linux-x86-64.so.2 ;;
    aarch64) debian_arch=arm64 loader=ld-linux-aarch64.so.1 ;;
    *) echo "unsupported CPU: $arch" >&2; exit 1 ;;
esac

if [ "${IN_BUILD_CONTAINER:-}" != yes ]; then
    project=$(cd "$(dirname "$0")/.." && pwd)
    engine=$(command -v podman || command -v docker)
    # Pinned by digest (the multi-platform index of debian:13 on 2026-11-04), so every rebuild starts from the same
    # image; the packages themselves come from the Debian archive and are listed in PACKAGES.txt. To move on, take the
    # current digest of the tag ("podman image inspect --format '{{.Digest}}' debian:13" after a pull).
    exec "$engine" run --rm -e IN_BUILD_CONTAINER=yes -v "$project:/project" \
        docker.io/library/debian:13@sha256:9c6e381421d0f48d1b8133e573f33c185ee16cbf505eda9c4a8a4791b25ea8a6 \
        bash /project/tools/build-runtime.sh "$arch"
fi

sysroot=/tmp/sysroot
out=/tmp/runtime
lists=/tmp/lists        # which package each file of the bundle comes from (see "License texts" below)
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends mmdebstrap python3-pip ca-certificates binutils >/dev/null

# The closing clean-up of mmdebstrap needs mount(), which a rootless container may refuse; everything is unpacked by then.
rm -rf "$sysroot" "$out" "$lists"
mmdebstrap --quiet --mode=root --variant=extract --skip=cleanup/apt/cache --architectures="$debian_arch" \
    --include=python3,python3-tk,libgcc-s1 trixie "$sysroot" || true
[ -x "$sysroot/usr/bin/python3" ] || { echo "the packages were not unpacked" >&2; exit 1; }

python_version=$(basename "$(readlink -f "$sysroot/usr/bin/python3")" | sed 's/^python//')
stdlib=$out/lib/python$python_version
mkdir -p "$out/bin" "$out/lib" "$out/etc" "$lists"

cp -L "$sysroot/usr/bin/python$python_version" "$out/bin/python$python_version.bin"
cp -a "$sysroot/usr/lib/python$python_version" "$stdlib"
cp -a "$sysroot/usr/share/tcltk/tcl8.6" "$sysroot/usr/share/tcltk/tk8.6" "$out/lib/"

# What the program can never use. The readline and dbm modules would also bring in libreadline (GPL) and Berkeley DB
# (Sleepycat license).
(
    cd "$stdlib"
    rm -rf test idlelib ensurepip lib2to3 turtledemo pydoc_data __phello__ tkinter/test turtle.py dist-packages config-*
    rm -f lib-dynload/readline.* lib-dynload/_curses* lib-dynload/_dbm.* lib-dynload/_gdbm.* lib-dynload/_test* \
          lib-dynload/_ctypes_test.* lib-dynload/xx*
    find . -name __pycache__ -type d -prune -exec rm -rf {} +
    # A link into /etc: on a host with this Python version, the host's configuration would run inside ours.
    rm -f sitecustomize.py
)

needed() {
    readelf -d "$1" | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p'
}

collect() {     # copies one library and, recursively, everything it needs
    [ -e "$out/lib/$1" ] && return 0
    file=$(find "$sysroot/usr/lib" "$sysroot/usr/lib64" -name "$1" 2>/dev/null | head -n 1)
    [ -n "$file" ] || { echo "library not found: $1" >&2; exit 1; }
    cp -L "$file" "$out/lib/$1"
    echo "${file#"$sysroot"/}" >> "$lists/shipped"
    for dep in $(needed "$out/lib/$1"); do collect "$dep"; done
}

for object in "$out/bin/python$python_version.bin" "$stdlib"/lib-dynload/*.so; do
    for dep in $(needed "$object"); do collect "$dep"; done
done
# The loader itself, and what yt-dlp and ffmpeg need where they can only run through it (see env.py).
for name in "$loader" libgcc_s.so.1 libmvec.so.1 libdl.so.2 libpthread.so.0 librt.so.1 libutil.so.1; do
    collect "$name"
done

# License texts. Most licenses of what the bundle holds (glibc's LGPL, the GPL of libgcc_s, OpenSSL's Apache license
# ...) allow handing it on in binary form only together with them. So every package that has a file in the bundle
# brings its copyright file, to share/doc/<package>/ as on Debian. Which package a file comes from, the .deb files
# say; a file that no package has stops the build, since nobody could tell its license.
for deb in "$sysroot"/var/cache/apt/archives/*.deb; do
    id=$(basename "$deb" .deb | sed 's/%3a/:/')         # <package>_<version>_<arch>, as in PACKAGES.txt
    dpkg-deb --fsys-tarfile "$deb" | tar -t | awk -v id="$id" '
        /\/$/ { next }                                          # a folder
        { sub(/^\.\//, ""); sub(/^(s?bin|lib[^\/]*)\//, "usr/&"); print $0 "\t" id }'
done > "$lists/contents"
{
    echo "usr/bin/python$python_version"
    (cd "$stdlib" && find . ! -type d) | sed "s|^\./|usr/lib/python$python_version/|"
    (cd "$out/lib" && find tcl8.6 tk8.6 ! -type d) | sed 's|^|usr/share/tcltk/|'
} >> "$lists/shipped"
awk -F '\t' 'NR == FNR { package[$1] = $2; next }
             $0 in package { print package[$0]; next }
             { print "no package has " $0 > "/dev/stderr"; unknown = 1 }
             END { exit unknown }' "$lists/contents" "$lists/shipped" > "$lists/owners"
sort -u "$lists/owners" > "$lists/packages"
while read -r id; do
    package=${id%%_*}
    mkdir -p "$out/share/doc/$package"
    cp -L "$sysroot/usr/share/doc/$package/copyright" "$out/share/doc/$package/"
done < "$lists/packages"

pip3 install --quiet --no-compile --no-cache-dir --only-binary=:all: --require-hashes --target "$stdlib" \
    -r /project/requirements.txt

cp -L /etc/ssl/certs/ca-certificates.crt "$out/etc/cacert.pem"
mkdir -p "$out/share/doc/ca-certificates"
cp -L /usr/share/doc/ca-certificates/copyright "$out/share/doc/ca-certificates/"     # the CA list is this container's

# Most copyright files point to the full text of a common license instead of repeating it ("/usr/share/common-licenses/
# GPL-2", libffi8 also ".../{MPL-1.1,GPL-2,LGPL-2.1}"): those texts go to the same place in the bundle.
mkdir -p "$out/share/common-licenses"
grep -ho -E '/usr/share/common-licenses/(\{[A-Za-z0-9.,-]+\}|[A-Za-z0-9.-]*[A-Za-z0-9])' "$out"/share/doc/*/copyright |
    sed 's|^/usr/share/common-licenses/||' | tr -d '{}' | tr ',' '\n' | sort -u |
    while read -r name; do cp -L "/usr/share/common-licenses/$name" "$out/share/common-licenses/"; done

cat > "$out/etc/fonts.conf" <<'EOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir>/usr/share/fonts</dir>
  <dir>/usr/local/share/fonts</dir>
  <dir prefix="xdg">fonts</dir>
  <dir>~/.fonts</dir>
  <cachedir prefix="xdg">fontconfig</cachedir>
</fontconfig>
EOF

# The variables exported here are for our interpreter only: system.host_env() takes exactly these values out of the
# environment of the host programs the app starts. Change both together.
cat > "$out/bin/python" <<EOF
#!/bin/sh
# Starts the bundled Python through the bundled dynamic loader, so the host's C library is never involved.
# LD_LIBRARY_PATH stays unset on purpose: yt-dlp, ffmpeg, zenity and xdg-open are host programs.
# -E -s: the host's PYTHON* variables and ~/.local packages are not meant for this interpreter.
rt=\$(cd "\$(dirname "\$0")/.." && pwd -P)
export TCL_LIBRARY="\$rt/lib/tcl8.6" TK_LIBRARY="\$rt/lib/tk8.6"
export SSL_CERT_FILE="\${SSL_CERT_FILE:-\$rt/etc/cacert.pem}"
[ -e /etc/fonts/fonts.conf ] || export FONTCONFIG_FILE="\$rt/etc/fonts.conf"
exec "\$rt/lib/$loader" --argv0 "\$rt/bin/python" --library-path "\$rt/lib" \\
    "\$rt/bin/python$python_version.bin" -E -s "\$@"
EOF
chmod 755 "$out/bin/python"

# What the bundle is made of, with exact versions, to find the sources again (glibc is LGPL): the packages that have a
# file in it, the same ones as in share/doc/ (the other unpacked ones, readline or dpkg, were dependencies only), the
# CA list and the Python packages. bundled-runtime/README.md compares these versions with Debian's.
{
    cat "$lists/packages"
    dpkg-query --show --showformat="\${Package}_\${Version}_\${Architecture}\n" ca-certificates
} | sort > "$out/PACKAGES.txt"
pip3 list --path "$stdlib" --format=freeze 2>/dev/null >> "$out/PACKAGES.txt"

tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 -C "$out" -c . \
    | gzip -9n > "/project/bundled-runtime/linux-$arch.tar.gz"
du -sh "$out" "/project/bundled-runtime/linux-$arch.tar.gz"
