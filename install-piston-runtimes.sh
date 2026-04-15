#!/usr/bin/env bash
# Install the language runtimes supported by the code editor into the
# Piston container. Safe to re-run — ppman skips already-installed versions.
#
# Usage (from ai-recruiters-backend/):
#   docker compose up -d
#   ./install-piston-runtimes.sh
#
# These versions match LANGUAGE_CONFIG in
#   app/services/code_execution_service.py
# If you add a language there, add the matching line here.

set -e

CONTAINER="${PISTON_CONTAINER:-piston}"

run() {
  echo "▶ Installing $1"
  docker exec "$CONTAINER" cli/index.js ppman install "$1" || {
    echo "   … already installed or unavailable, continuing."
  }
}

# Wait for Piston to be healthy
for i in {1..30}; do
  if docker exec "$CONTAINER" wget -q -O- http://localhost:2000/api/v2/runtimes >/dev/null 2>&1; then
    break
  fi
  echo "Waiting for Piston to start... ($i/30)"
  sleep 2
done

# Core languages — install first so a quick demo works immediately.
run "python=3.10.0"
run "node=18.15.0"
run "typescript=5.0.3"
run "java=15.0.2"
run "c++=10.2.0"
run "c=10.2.0"
run "csharp=6.12.0"
run "go=1.16.2"

# Extended set — matches the full editor dropdown. Comment out any you
# don't need to save disk space (each runtime is ~100-500 MB).
run "python2=2.7.18"
run "kotlin=1.8.20"
run "scala=3.2.2"
run "groovy=3.0.7"
run "rust=1.68.2"
run "ruby=3.0.1"
run "php=8.2.3"
run "swift=5.3.3"
run "dart=2.19.6"
run "perl=5.36.0"
run "lua=5.4.4"
run "bash=5.2.0"
run "pwsh=7.1.4"
run "rscript=4.1.1"
run "julia=1.8.5"
run "haskell=9.0.1"
run "elixir=1.11.3"
run "erlang=23.0.0"
run "ocaml=4.12.0"
run "clojure=1.10.3"
run "pascal=3.0.4"
run "fortran=10.2.0"
run "cobol=10.2.0"
run "nim=1.6.2"
run "crystal=0.36.1"
run "zig=0.10.1"
run "vlang=0.3.3"
run "d=10.2.0"
run "racket=8.3.0"
run "lisp=2.1.2"
run "prolog=1.4.5"
run "sqlite3=3.36.0"
run "fsharp.net=5.0.201"
run "basic.net=5.0.201"

echo
echo "✅ Done. Installed runtimes:"
docker exec "$CONTAINER" cli/index.js ppman list | grep -i "installed" || true
