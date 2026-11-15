#!/bin/sh
# Refuses anything that looks like a login. Reads a unified diff on stdin ("git diff", "git log -p") and exits 1
# when a file that is added or changed has a name that must never be committed, is binary (only the runtime bundles
# and the README screenshots may be), or an added line looks like a cookie export, an auth_token / ct0 value or a
# private key. Prints "path: reason", never the value.
#
#   git diff --cached -U0 | sh tools/check-secrets.sh        (tools/githooks/pre-commit)
#   git log -p <range>    | sh tools/check-secrets.sh        (tools/githooks/pre-push)
#
# It looks at the CONTENT, so it does not matter what a file is called (x.com_cookies.txt, notes.txt ...).
# Plain sh, awk and grep: no Python and no other tool, like the launch script.
set -u
LC_ALL=C; export LC_ALL
T=$(printf '\t')
rec=$(mktemp "${TMPDIR:-/tmp}/check-secrets.XXXXXX") || exit 2
trap 'rm -f "$rec"' EXIT

# diff -> records:  F<TAB>path  for every file that is added or changed (a deletion is the fix, not the
# problem, so it is skipped),  B<TAB>path  if that file is binary,  L<TAB>path<TAB>text  for every added line.
awk -v T="$T" '
  function flush() { if (path != "" && !deleted && !emitted) { print "F" T path; emitted = 1 } }
  /^diff --git / { flush(); p = $0; sub(/^diff --git a\/.* b\//, "", p); path = p; hdr = 1; deleted = 0; emitted = 0; next }
  hdr && /^deleted file mode/ { deleted = 1; next }
  hdr && (/^Binary files / || /^GIT binary patch/) { if (!deleted) print "B" T path; flush(); next }
  /^@@/          { hdr = 0; flush(); next }
  hdr            { next }
  /^\+/          { print "L" T path T substr($0, 2) }
  END            { flush() }
' > "$rec"

say()   { sort -u | while IFS= read -r p; do printf '  %s: %s\n' "$p" "$1"; done; }
paths() { grep "^F$T" "$rec" | cut -f2-; }
lines() { grep -i -E "^L$T[^$T]*$T$1" "$rec" | cut -f2; }

out=$(
  # Names that must never be committed: cookie files, the program's own data, logs, downloads.
  paths | grep -i -E '(^|/)settings\.json$|(^|/)(failed_)?list\.txt$|\.log(\.[0-9]+)?$|cookie[^/]*\.(txt|json|sqlite|db|jar)$|\.(mp4|mkv|webm|m4a|part|ytdl)$' \
        | say 'a file that holds login data, personal data or a download'
  # Binary content never reaches the line checks below (a browser's cookie database, whatever it is called). The
  # exceptions: the runtime bundles, and the PNG screenshots of the README directly in docs/images/ (as .gitignore).
  grep "^B$T" "$rec" | cut -f2- | grep -v -E '^(bundled-runtime/linux-[^/]+\.tar\.gz|docs/images/[^/]+\.png)$' \
        | say 'a binary file (a browser database? a download?); only bundled-runtime/linux-*.tar.gz and docs/images/*.png may be binary'
  # A Netscape cookie file (curl and yt-dlp mark HttpOnly cookies with a "#HttpOnly_" prefix).
  lines "(#HttpOnly_)?[A-Za-z0-9._*-]+$T(TRUE|FALSE)$T[^$T]*$T(TRUE|FALSE)$T[0-9]+$T[^$T]+($T|\$)" \
        | say 'a cookie-file line (Netscape format)'
  # The values themselves, in the other shapes an export or a pasted header takes.
  lines ".*(auth_token|ct0)[^A-Za-z0-9]{1,8}[0-9a-f]{32,}" | say 'an auth_token / ct0 value'
  lines ".*\"value\"[^A-Za-z0-9]{1,8}[0-9a-f]{32,}"         | say 'a JSON cookie export (long hex "value")'
  lines ".*twid[^A-Za-z0-9]{1,8}u(%3D|=)[0-9]{6,}"          | say 'a twid cookie (your numeric X user id)'
  lines ".*BEGIN [A-Z ]*PRIVATE KEY"                        | say 'a private key'
)

if [ -n "$out" ]; then
  {
    echo "check-secrets: refusing. These look like login data or personal data:"
    printf '%s\n' "$out"
    echo "  (the values are not shown. Unstage a file: git restore --staged <path>. Skipping this check on purpose: --no-verify)"
  } >&2
  exit 1
fi
