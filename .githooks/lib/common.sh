# shellcheck shell=sh
# ql-git-hooks shared helpers. POSIX sh; sourced by hooks, checks and bin/ql-hooks.

QL_HOOKS_NAME="ql-git-hooks"

# ql_refuse ACTION RULE WHY FIX [DETAIL ...]
# Prints the reason and the fix for a refusal to stderr. Never prints secret values.
ql_refuse() {
  _action=$1 _rule=$2 _why=$3 _fix=$4
  shift 4
  {
    printf '\n%s: %s refused by rule "%s"\n' "$QL_HOOKS_NAME" "$_action" "$_rule"
    printf '  Why: %s\n' "$_why"
    for _d in "$@"; do
      printf '    - %s\n' "$_d"
    done
    printf '  Fix: %s\n\n' "$_fix"
  } >&2
}

ql_note() {
  printf '%s: %s\n' "$QL_HOOKS_NAME" "$*" >&2
}

# ql_config_get KEY [DEFAULT]
# Reads KEY=value from the per-repo config (QL_CONFIG_FILE, else $QL_HOOKS_DIR/config).
# The file is parsed, never sourced. Last assignment wins; surrounding quotes are removed.
ql_config_get() {
  _key=$1 _default=${2-}
  _file=${QL_CONFIG_FILE:-${QL_HOOKS_DIR:-.}/config}
  _val=
  if [ -f "$_file" ]; then
    _val=$(sed -n "s/^[[:space:]]*${_key}[[:space:]]*=[[:space:]]*//p" "$_file" | tail -n 1 |
      sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")
  fi
  if [ -n "$_val" ]; then
    printf '%s' "$_val"
  else
    printf '%s' "$_default"
  fi
}

# ql_is_true VALUE -> exit 0 for true/yes/on/1 (any case)
ql_is_true() {
  case $(printf '%s' "$1" | tr '[:upper:]' '[:lower:]') in
    true | yes | on | 1) return 0 ;;
    *) return 1 ;;
  esac
}

# ql_addons -> addon names from ADDONS, one per line (commas or spaces as separators)
ql_addons() {
  ql_config_get ADDONS "" | tr ', ' '\n\n' | sed '/^$/d'
}

# ql_glob_match PATH "GLOB GLOB ..." -> exit 0 if PATH matches any glob
ql_glob_match() {
  _path=$1
  _globs=$2
  set -f
  for _g in $_globs; do
    # shellcheck disable=SC2254
    case $_path in
      $_g)
        set +f
        return 0
        ;;
    esac
  done
  set +f
  return 1
}

# ql_sha256 FILE -> hex digest
ql_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# ql_version_cmp A B -> prints -1, 0 or 1 (numeric dotted versions)
ql_version_cmp() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    na = split(a, x, "."); nb = split(b, y, ".")
    n = (na > nb) ? na : nb
    for (i = 1; i <= n; i++) {
      xi = (i <= na) ? x[i] + 0 : 0; yi = (i <= nb) ? y[i] + 0 : 0
      if (xi < yi) { print -1; exit }
      if (xi > yi) { print 1; exit }
    }
    print 0
  }'
}
