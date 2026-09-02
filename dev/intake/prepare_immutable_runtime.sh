#!/bin/bash
set -euo pipefail

if test "$(id -u)" -ne 0; then
    echo "This immutable-runtime preparation must run as root." >&2
    exit 1
fi

if test "$#" -ne 2 || { test "$1" != "--check" && test "$1" != "--prepare"; }; then
    echo "Usage: $0 --check|--prepare <full-commit-sha>" >&2
    exit 2
fi

mode=$1
sha=$2
repo=/srv/projects/nocturne-plugin-intake
root=/srv/nocturne-plugin
tool="$repo/dev/intake/immutable_runtime_release.py"
wheel="$root/wheelhouse/gunicorn-26.2.0-py3-none-any.whl"
lock="$root/wheelhouse/runtime-requirements.lock"
release="$root/releases/$sha"
staged="$root/staged-units/$sha"
target="$root/venvs/python3.14-gunicorn-26.2.0"

test "$(git -C "$repo" rev-parse --verify "$sha^{commit}")" = "$sha"
test -f "$wheel"; test ! -L "$wheel"
test -f "$lock"; test ! -L "$lock"
test -d "$root"; test ! -L "$root"; test "$(stat -c %u:%g:%a "$root")" = 0:0:755
if getfacl -cp "$root" | grep -Eq '^(default:|user:[^:]|group:[^:])'; then
    echo "Unsafe runtime-root ACL." >&2
    exit 1
fi
if test -d "$root/staged-units" && \
   find "$root/staged-units" -mindepth 1 -maxdepth 1 -name '.units-*' -print -quit | grep -q .; then
    echo "Incomplete unit staging exists; inspect it before retrying." >&2
    exit 1
fi

if test "$mode" = --check; then
    python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha"
    if test -d "$release" && test ! -L "$release"; then
        python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" \
            --check-venv --requirements-lock "$lock" --wheel "$wheel"
    else
        echo "Release is not prepared; venv validation is deferred." >&2
    fi
    exit 0
fi

current_before=ABSENT
if test -L "$root/current"; then current_before="LINK:$(readlink "$root/current")";
elif test -e "$root/current"; then echo "Unsafe non-symlink current selector." >&2; exit 1; fi
units_before=$(sha256sum /etc/systemd/system/nocturne-plugin-writer.service \
    /etc/systemd/system/nocturne-plugin-dev.service)

python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" --prepare
python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" \
    --prepare-venv --requirements-lock "$lock" --wheel "$wheel"

if test -e "$root/staged-units" || test -L "$root/staged-units"; then
    test -d "$root/staged-units"; test ! -L "$root/staged-units"
    test "$(stat -c %u:%g:%a "$root/staged-units")" = 0:0:755
else
    install -d -o root -g root -m 0755 "$root/staged-units"
fi

if test -e "$staged" || test -L "$staged"; then
    test -d "$staged"; test ! -L "$staged"
    cmp "$staged/nocturne-plugin-writer.service" "$release/deployment-units/nocturne-plugin-writer.service"
    cmp "$staged/nocturne-plugin-dev.service" "$release/deployment-units/nocturne-plugin-dev.service"
else
    staged_tmp="$root/staged-units/.units-$sha.$$"
    test ! -e "$staged_tmp"
    install -d -o root -g root -m 0755 "$staged_tmp"
    install -o root -g root -m 0644 "$release/deployment-units/nocturne-plugin-writer.service" \
        "$staged_tmp/nocturne-plugin-writer.service"
    install -o root -g root -m 0644 "$release/deployment-units/nocturne-plugin-dev.service" \
        "$staged_tmp/nocturne-plugin-dev.service"
    mv -T "$staged_tmp" "$staged"
fi
systemd-analyze verify "$staged/nocturne-plugin-writer.service" "$staged/nocturne-plugin-dev.service"

current_after=ABSENT
if test -L "$root/current"; then current_after="LINK:$(readlink "$root/current")";
elif test -e "$root/current"; then current_after=NONSYMLINK; fi
test "$current_after" = "$current_before"
test "$(sha256sum /etc/systemd/system/nocturne-plugin-writer.service \
    /etc/systemd/system/nocturne-plugin-dev.service)" = "$units_before"
test ! -e "$target/PREPARATION_INCOMPLETE"; test ! -L "$target/PREPARATION_INCOMPLETE"

echo "PREPARATION COMPLETE"
echo "release=$release"
echo "runtime_venv=$target"
echo "staged_units=$staged"
echo "current_unchanged=$current_after"
