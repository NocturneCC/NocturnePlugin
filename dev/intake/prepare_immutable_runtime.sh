#!/bin/bash
set -euo pipefail
set -E

phase=initialization
diagnostic_path=-
diagnostic_expected="successful immutable-runtime wrapper initialization"
diagnostic_action=stop

diagnostic() {
    local classification=$1 observed=$2
    printf 'status=%s\n' "$classification" >&2
    printf 'phase=%s\n' "$phase" >&2
    printf 'path=%s\n' "$diagnostic_path" >&2
    printf 'expected=%s\n' "$diagnostic_expected" >&2
    printf 'observed=%s\n' "$observed" >&2
    printf 'operator_action=%s\n' "$diagnostic_action" >&2
}

unexpected_error() {
    local status=$?
    trap - ERR
    diagnostic unsafe_blocking "unexpected_command_failure exit_status=$status"
    exit "$status"
}
trap unexpected_error ERR

if effective_uid=$(id -u 2>&1); then :; else
    phase=privilege
    diagnostic_path=/
    diagnostic_expected="numeric effective uid"
    diagnostic_action=stop
    diagnostic unsafe_blocking "effective_uid_query_failed exit_status=$?"
    exit 1
fi
if [[ ! "$effective_uid" =~ ^[0-9]+$ ]]; then
    phase=privilege
    diagnostic_path=/
    diagnostic_expected="numeric effective uid"
    diagnostic_action=stop
    diagnostic unsafe_blocking "invalid_effective_uid=$effective_uid"
    exit 1
fi
if test "$effective_uid" -ne 0; then
    phase=privilege
    diagnostic_path=/
    diagnostic_expected="effective uid 0"
    diagnostic_action=stop
    diagnostic unsafe_blocking "effective_uid=$effective_uid"
    exit 1
fi

if test "$#" -ne 2 || { test "$1" != "--check" && test "$1" != "--prepare"; }; then
    phase=arguments
    diagnostic_path=$0
    diagnostic_expected="--check|--prepare followed by one exact full commit SHA"
    diagnostic_action=stop
    diagnostic unsafe_blocking "invalid_argument_count_or_mode count=$#"
    echo "Usage: $0 --check|--prepare <full-commit-sha>" >&2
    exit 2
fi

mode=$1
sha=$2
repo=/srv/projects/nocturne-plugin-intake
root=/srv/nocturne-plugin
tool="$repo/dev/intake/immutable_runtime_release.py"
emoji_runtime="$repo/dev/intake/emoji_runtime_release.py"
readiness="$repo/dev/intake/immutable_runtime_check.py"
wheel="$root/wheelhouse/python3.14-gunicorn-26.2.0/gunicorn-26.2.0-py3-none-any.whl"
emoji_wheel="$root/wheelhouse/emoji-python3.14-pillow-12.3.0/pillow-12.3.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
release="$root/releases/$sha"
lock="$release/dev/intake/runtime-requirements.lock"
staged="$root/staged-units/$sha"
staged_nginx="$root/staged-nginx/$sha"
target="$root/venvs/python3.14-gunicorn-26.2.0"
emoji_target="$root/venvs/emoji-python3.14-pillow-12.3.0"

if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
    phase=git_commit
    diagnostic_path=$repo
    diagnostic_expected="exact lowercase 40-character Git SHA"
    diagnostic_action=stop
    diagnostic unsafe_blocking "invalid_commit_argument=$sha"
    exit 2
fi

phase=git_checkout
diagnostic_path=$repo
diagnostic_expected="clean checkout with HEAD exactly $sha"
diagnostic_action=stop
if head=$(GIT_OPTIONAL_LOCKS=0 git -C "$repo" rev-parse HEAD 2>&1); then :; else
    diagnostic unsafe_blocking "git_head_query_failed exit_status=$?"
    exit 1
fi
if test "$head" != "$sha"; then
    diagnostic unsafe_blocking "head=$head"
    exit 1
fi
if resolved=$(GIT_OPTIONAL_LOCKS=0 git -C "$repo" rev-parse --verify "$sha^{commit}" 2>&1); then :; else
    diagnostic unsafe_blocking "commit_resolution_failed exit_status=$?"
    exit 1
fi
if test "$resolved" != "$sha"; then
    diagnostic unsafe_blocking "resolved_commit=$resolved"
    exit 1
fi
if dirty=$(GIT_OPTIONAL_LOCKS=0 git -C "$repo" status --porcelain=v1 --untracked-files=all 2>&1); then :; else
    diagnostic unsafe_blocking "git_status_failed exit_status=$?"
    exit 1
fi
if test -n "$dirty"; then
    diagnostic unsafe_blocking "worktree=dirty"
    exit 1
fi

if test "$mode" = --check; then
    phase=readiness_check
    diagnostic_path=$root
    diagnostic_expected="structured read-only immutable-runtime readiness result"
    diagnostic_action=stop
    if /usr/bin/python3.14 -B "$readiness" --repo "$repo" --runtime-root "$root" \
            --commit "$sha" --python /usr/bin/python3.14; then
        exit 0
    else
        status=$?
        if test "$status" -eq 1 || test "$status" -eq 3 || test "$status" -eq 4; then
            exit "$status"
        fi
        diagnostic unsafe_blocking "readiness_checker_failed exit_status=$status"
        exit 1
    fi
fi

require_regular_prerequisite() {
    local required_phase=$1 required_path=$2 required_expected=$3
    phase=$required_phase
    diagnostic_path=$required_path
    diagnostic_expected=$required_expected
    diagnostic_action=prepare
    if test ! -e "$required_path" && test ! -L "$required_path"; then
        diagnostic not_prepared absent
        exit 3
    fi
    if test -L "$required_path" || test ! -f "$required_path"; then
        diagnostic_action=stop
        diagnostic unsafe_blocking "present_but_not_a_regular_non_symlink_file"
        exit 1
    fi
}

require_regular_prerequisite gunicorn_wheel "$wheel" \
    "verified root-owned hash-locked Gunicorn 26.2.0 wheel"
require_regular_prerequisite pillow_wheel "$emoji_wheel" \
    "verified root-owned hash-locked Pillow 12.3.0 wheel"

phase=runtime_root
diagnostic_path=$root
diagnostic_expected="non-symlink runtime-root directory"
diagnostic_action=stop
if test ! -e "$root" && test ! -L "$root"; then
    diagnostic unsafe_blocking absent
    exit 1
fi
if test -L "$root" || test ! -d "$root"; then
    diagnostic unsafe_blocking "present_but_not_a_directory"
    exit 1
fi

# Fail the exact CPython/ABI/architecture gate before creating a release or
# invoking either dependency installer.
phase=host_runtime
diagnostic_path=/usr/bin/python3.14
diagnostic_expected="CPython 3.14 x86-64 ABI and glibc compatible with the pinned Pillow wheel"
diagnostic_action=stop
/usr/bin/python3.14 -B "$emoji_runtime" --repo "$repo" --commit "$sha" \
    --runtime-root "$root" --python /usr/bin/python3.14 \
    --requirements "$repo/dev/intake/emoji-sync-requirements.txt" \
    --wheel "$emoji_wheel" --host-preflight

phase=runtime_root_metadata
diagnostic_path=$root
diagnostic_expected="uid=0 gid=0 mode=0755 basic ACL"
diagnostic_action=stop
metadata=$(stat -c %u:%g:%a "$root")
if test "$metadata" != 0:0:755; then
    diagnostic unsafe_blocking "metadata=$metadata"
    exit 1
fi
if acl=$(getfacl -cp "$root" 2>&1); then :; else
    diagnostic unsafe_blocking "acl_read_failed exit_status=$?"
    exit 1
fi
if printf '%s\n' "$acl" | grep -Eq '^(default:|user:[^:]|group:[^:])'; then
    diagnostic unsafe_blocking "acl=extended"
    exit 1
else
    grep_status=$?
    if test "$grep_status" -ne 1; then
        diagnostic unsafe_blocking "acl_parse_failed exit_status=$grep_status"
        exit 1
    fi
fi
for stage_parent in "$root/staged-units" "$root/staged-nginx"; do
    phase=deployment_staging
    diagnostic_path=$stage_parent
    diagnostic_expected="directory with no .stage-* interrupted staging entry"
    diagnostic_action="stop and inspect"
    if test -e "$stage_parent" || test -L "$stage_parent"; then
        if test -L "$stage_parent" || test ! -d "$stage_parent"; then
            diagnostic unsafe_blocking "present_but_not_a_directory"
            exit 1
        fi
        if incomplete=$(find "$stage_parent" -mindepth 1 -maxdepth 1 \
                -name '.stage-*' -print -quit 2>&1); then :; else
            diagnostic unsafe_blocking "staging_scan_failed exit_status=$?"
            exit 1
        fi
    else
        incomplete=
    fi
    if test -n "$incomplete"; then
        phase=deployment_staging
        diagnostic_path=$stage_parent
        diagnostic_expected="no .stage-* interrupted staging entry"
        diagnostic_action="stop and inspect"
        diagnostic unsafe_blocking "incomplete_staging_present"
        exit 1
    fi
done
phase=release_staging
diagnostic_path="$root/releases"
diagnostic_expected="directory with no .release-* interrupted staging entry"
diagnostic_action="stop and inspect"
if test -e "$root/releases" || test -L "$root/releases"; then
    if test -L "$root/releases" || test ! -d "$root/releases"; then
        diagnostic unsafe_blocking "present_but_not_a_directory"
        exit 1
    fi
    if incomplete=$(find "$root/releases" -mindepth 1 -maxdepth 1 \
            -name '.release-*' -print -quit 2>&1); then :; else
        diagnostic unsafe_blocking "staging_scan_failed exit_status=$?"
        exit 1
    fi
else
    incomplete=
fi
if test -n "$incomplete"; then
    phase=release_staging
    diagnostic_path="$root/releases"
    diagnostic_expected="no .release-* interrupted staging entry"
    diagnostic_action="stop and inspect"
    diagnostic unsafe_blocking "incomplete_staging_present"
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

phase=active_state_snapshot
diagnostic_path=$root
diagnostic_expected="readable current selector and active unit/route metadata"
diagnostic_action=stop
active_before=$(snapshot_active)

phase=release_preparation
diagnostic_path=$release
diagnostic_expected="verified immutable release for $sha"
diagnostic_action=recover
/usr/bin/python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" --prepare
phase=gunicorn_runtime_preparation
diagnostic_path=$target
diagnostic_expected="completed versioned Gunicorn virtual environment"
diagnostic_action=recover
/usr/bin/python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" \
    --prepare-venv --requirements-lock "$lock" --wheel "$wheel"
phase=emoji_runtime_preparation
diagnostic_path=$emoji_target
diagnostic_expected="completed versioned Pillow virtual environment"
diagnostic_action=recover
/usr/bin/python3.14 -B "$emoji_runtime" --repo "$repo" --commit "$sha" \
    --runtime-root "$root" --python /usr/bin/python3.14 \
    --requirements "$release/dev/intake/emoji-sync-requirements.txt" --wheel "$emoji_wheel" --prepare
phase=deployment_staging
diagnostic_path="$staged and $staged_nginx"
diagnostic_expected="verified commit-scoped unit and Nginx staging sets"
diagnostic_action=recover
/usr/bin/python3.14 -B "$tool" --repo "$repo" --runtime-root "$root" --commit "$sha" --stage-deployment
phase=systemd_unit_validation
diagnostic_path=$staged
diagnostic_expected="all four staged units pass systemd-analyze verify"
diagnostic_action=stop
systemd-analyze verify "$staged/nocturne-plugin-writer.service" \
    "$staged/nocturne-plugin-dev.service" "$staged/nocturne-plugin-emoji-sync.service" \
    "$staged/nocturne-plugin-emoji-sync.timer"

phase=active_state_snapshot
diagnostic_path=$root
diagnostic_expected="readable current selector and active unit/route metadata"
diagnostic_action=stop
active_after=$(snapshot_active)
phase=active_state_verification
diagnostic_path=$root
diagnostic_expected="current selector and active unit/route files unchanged by preparation"
diagnostic_action=stop
if test "$active_after" != "$active_before"; then
    diagnostic unsafe_blocking "active_state_changed"
    exit 1
fi
for marker in "$target/PREPARATION_INCOMPLETE" "$emoji_target/PREPARATION_INCOMPLETE"; do
    phase=runtime_completion
    diagnostic_path=$marker
    diagnostic_expected="incomplete marker absent after successful validation"
    diagnostic_action=recover
    if test -e "$marker" || test -L "$marker"; then
        diagnostic recoverable_incomplete "incomplete_marker_present"
        exit 4
    fi
done

echo "PREPARATION COMPLETE"
echo "release=$release"
echo "runtime_venv=$target"
echo "emoji_runtime_venv=$emoji_target"
echo "staged_units=$staged"
echo "staged_nginx=$staged_nginx"
echo "active_state_unchanged=yes"
