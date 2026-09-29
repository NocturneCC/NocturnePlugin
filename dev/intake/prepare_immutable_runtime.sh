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
emoji_runtime="$repo/dev/intake/emoji_runtime_release.py"
ownership="$repo/dev/intake/runtime_ownership.py"
wheel="$root/wheelhouse/python3.14-gunicorn-26.2.0/gunicorn-26.2.0-py3-none-any.whl"
emoji_wheel="$root/wheelhouse/emoji-python3.14-pillow-12.3.0/pillow-12.3.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
release="$root/releases/$sha"
lock="$release/dev/intake/runtime-requirements.lock"
staged="$root/staged-units/$sha"
staged_nginx="$root/staged-nginx/$sha"
target="$root/venvs/python3.14-gunicorn-26.2.0"
emoji_target="$root/venvs/emoji-python3.14-pillow-12.3.0"

if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "An exact lowercase 40-character Git SHA is required." >&2
    exit 2
fi
test "$(git -C "$repo" rev-parse HEAD)" = "$sha"
test "$(git -C "$repo" rev-parse --verify "$sha^{commit}")" = "$sha"
if test -n "$(git -C "$repo" status --porcelain=v1 --untracked-files=all)"; then
    echo "The deployment checkout must be clean." >&2
    exit 1
fi
test -f "$wheel"; test ! -L "$wheel"
test -f "$emoji_wheel"; test ! -L "$emoji_wheel"
test -d "$root"; test ! -L "$root"

# Fail the exact CPython/ABI/architecture gate before creating a release or
# invoking either dependency installer.
python3.14 -B "$emoji_runtime" --repo "$repo" --commit "$sha" \
    --runtime-root "$root" --python /usr/bin/python3.14 \
    --requirements "$repo/dev/intake/emoji-sync-requirements.txt" \
    --wheel "$emoji_wheel" --host-preflight

if test "$mode" = --check; then
    python3.14 -B "$ownership" --repo "$repo" --runtime-root "$root" --commit "$sha"
    if test "$(stat -c %u:%g:%a "$root")" != 0:0:755; then
        echo "Runtime-root ownership/mode requires guarded correction before preparation." >&2
        exit 1
    fi
    if getfacl -cp "$root" | grep -Eq '^(default:|user:[^:]|group:[^:])'; then
        echo "Unsafe runtime-root ACL." >&2
        exit 1
    fi
    python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha"
    if test -d "$release" && test ! -L "$release"; then
        python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" \
            --check-venv --requirements-lock "$lock" --wheel "$wheel"
        python3.14 -B "$emoji_runtime" --repo "$repo" --commit "$sha" \
            --runtime-root "$root" --python /usr/bin/python3.14 \
            --requirements "$release/dev/intake/emoji-sync-requirements.txt" --wheel "$emoji_wheel"
        if test -d "$staged" && test -d "$staged_nginx"; then
            python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" \
                --check-deployment
        fi
    else
        echo "Release is not prepared; venv validation is deferred." >&2
    fi
    exit 0
fi

test "$(stat -c %u:%g:%a "$root")" = 0:0:755
if getfacl -cp "$root" | grep -Eq '^(default:|user:[^:]|group:[^:])'; then
    echo "Unsafe runtime-root ACL." >&2
    exit 1
fi
for stage_parent in "$root/staged-units" "$root/staged-nginx"; do
    if test -d "$stage_parent" && \
       find "$stage_parent" -mindepth 1 -maxdepth 1 -name '.stage-*' -print -quit | grep -q .; then
        echo "Incomplete deployment staging exists; inspect it before retrying." >&2
        exit 1
    fi
done
if test -d "$root/releases" && \
   find "$root/releases" -mindepth 1 -maxdepth 1 -name '.release-*' -print -quit | grep -q .; then
    echo "Incomplete release staging exists; inspect it before retrying." >&2
    exit 1
fi

snapshot_active() {
    for path in "$root/current" "$root/venv" \
        /etc/systemd/system/nocturne-plugin-writer.service \
        /etc/systemd/system/nocturne-plugin-dev.service \
        /etc/systemd/system/nocturne-plugin-emoji-sync.service \
        /etc/systemd/system/nocturne-plugin-emoji-sync.timer \
        /etc/nginx/sites-enabled/nocturne; do
        if test -L "$path"; then
            printf 'L %s %s\n' "$path" "$(readlink "$path")"
        elif test -f "$path"; then
            printf 'F %s %s %s\n' "$path" "$(stat -c %u:%g:%a "$path")" \
                "$(sha256sum "$path" | cut -d' ' -f1)"
        elif test -e "$path"; then
            printf 'X %s\n' "$path"
        else
            printf 'A %s\n' "$path"
        fi
    done
}

active_before=$(snapshot_active)

python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" --prepare
python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" \
    --prepare-venv --requirements-lock "$lock" --wheel "$wheel"
python3.14 -B "$emoji_runtime" --repo "$repo" --commit "$sha" \
    --runtime-root "$root" --python /usr/bin/python3.14 \
    --requirements "$release/dev/intake/emoji-sync-requirements.txt" --wheel "$emoji_wheel" --prepare
python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" --stage-deployment
systemd-analyze verify "$staged/nocturne-plugin-writer.service" \
    "$staged/nocturne-plugin-dev.service" "$staged/nocturne-plugin-emoji-sync.service" \
    "$staged/nocturne-plugin-emoji-sync.timer"

active_after=$(snapshot_active)
test "$active_after" = "$active_before"
test ! -e "$target/PREPARATION_INCOMPLETE"; test ! -L "$target/PREPARATION_INCOMPLETE"
test ! -e "$emoji_target/PREPARATION_INCOMPLETE"; test ! -L "$emoji_target/PREPARATION_INCOMPLETE"

echo "PREPARATION COMPLETE"
echo "release=$release"
echo "runtime_venv=$target"
echo "emoji_runtime_venv=$emoji_target"
echo "staged_units=$staged"
echo "staged_nginx=$staged_nginx"
echo "active_state_unchanged=yes"
