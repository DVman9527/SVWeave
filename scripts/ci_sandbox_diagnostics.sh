#!/usr/bin/env bash
# Failure-only evidence for the mandatory probe (plan 6.4, 23.1 / SEC-05).
# Read-only commands only: no policy changes, extra namespace probes, or environment dump.
set -uo pipefail
probe_started_at=${1:?mandatory probe start time required}
probe_finished_at=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

read_diagnostic() {
  printf '\n--- %s ---\n' "$1"
  shift
  # Bound each read and its output. Missing commands/permissions are evidence, not gate success.
  timeout 5s "$@" 2>&1 | head -c 16384 || true
  printf '\n'
}

printf 'Sandbox failure diagnostics: %s through %s\n' "$probe_started_at" "$probe_finished_at"
read_diagnostic 'Kernel release' uname -srvm
read_diagnostic 'OS release' cat /etc/os-release
read_diagnostic 'Namespace/AppArmor sysctls (read only)' sysctl \
  kernel.unprivileged_userns_clone user.max_user_namespaces \
  kernel.apparmor_restrict_unprivileged_userns \
  kernel.apparmor_restrict_unprivileged_unconfined \
  kernel.apparmor_restrict_unprivileged_unconfined_restricted
read_diagnostic 'AppArmor enabled' cat /sys/module/apparmor/parameters/enabled
read_diagnostic 'Diagnostic shell security status (same job as probe)' \
  grep -E '^(Cap(Inh|Prm|Eff|Bnd|Amb)|NoNewPrivs|Seccomp|Seccomp_filters):' "/proc/$$/status"
read_diagnostic 'Diagnostic shell AppArmor label' cat "/proc/$$/attr/current"
read_diagnostic 'bwrap resolved executable' readlink -f /usr/bin/bwrap
read_diagnostic 'bwrap executable metadata' stat -c '%n %A %a %U:%G' /usr/bin/bwrap
read_diagnostic 'bwrap version (not a namespace probe)' /usr/bin/bwrap --version
read_diagnostic 'bubblewrap package' dpkg-query -W '-f=${Package} ${Version} ${Status}\n' bubblewrap
read_diagnostic 'bwrap package owner' dpkg-query -S /usr/bin/bwrap
read_diagnostic 'Loaded bwrap AppArmor profiles' \
  sudo -n grep -Ei 'bwrap|bubblewrap' /sys/kernel/security/apparmor/profiles
read_diagnostic 'Packaged bwrap AppArmor policy (not necessarily loaded)' \
  cat /etc/apparmor.d/bwrap
read_diagnostic 'Kernel audit/security entries during the mandatory probe (last 100 matches)' \
  sudo -n journalctl --kernel --no-pager --output=short-iso \
  --since "$probe_started_at" --until "$probe_finished_at" --lines=100 \
  --grep='apparmor|audit|DENIED|bwrap|bubblewrap|seccomp'
