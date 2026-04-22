"""
Code Execution Service — runs candidate code in a sandboxed environment.

Uses the Piston API (https://github.com/engineer-man/piston) for safe,
multi-language code execution. No API key required.

Supported languages: all mainstream Piston runtimes (40+ languages).
"""

import asyncio
import base64
import logging
import os
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

# Piston's free public API (https://emkc.org/api/v2/piston) went
# whitelist-only on 2026-02-15, so the default is no longer viable for
# production. Override with your own instance by setting PISTON_API_URL.
#
# Self-host in ~30 seconds with Docker:
#   docker run -d --name piston -p 2000:2000 ghcr.io/engineer-man/piston
#   # then install the runtimes you need:
#   docker exec piston cli/index.js ppman install python=3.10.0 node=18.15.0
# and set:   PISTON_API_URL=http://localhost:2000/api/v2
PISTON_API_URL = os.environ.get(
    "PISTON_API_URL", "https://emkc.org/api/v2/piston"
).rstrip("/")
# Optional bearer token for whitelisted public-API access.
PISTON_API_KEY = os.environ.get("PISTON_API_KEY", "").strip()

# ── Judge0 CE fallback ───────────────────────────────────────────────────────
# When Piston is unreachable (the public emkc.org instance has been
# whitelist-only since 2026-02-15), we fall back to Judge0 CE on RapidAPI.
# Free tier covers ~50 execs/day — enough for occasional interviews.
JUDGE0_RAPIDAPI_KEY = os.environ.get("JUDGE0_RAPIDAPI_KEY", "").strip()
JUDGE0_RAPIDAPI_HOST = os.environ.get(
    "JUDGE0_RAPIDAPI_HOST", "judge0-ce.p.rapidapi.com"
).strip()

# Judge0 uses numeric language IDs. Map our internal language keys to the
# closest Judge0 CE language. Versions don't have to match Piston's exactly —
# Judge0's catalogue is fixed. See: https://ce.judge0.com/languages/
JUDGE0_LANGUAGE_IDS = {
    "python": 71,        # Python 3.8.1
    "python2": 70,       # Python 2.7.17
    "javascript": 63,    # Node.js 12.14.0
    "typescript": 74,    # TypeScript 3.7.4
    "java": 62,          # OpenJDK 13.0.1
    "kotlin": 78,        # Kotlin 1.3.70
    "scala": 81,         # Scala 2.13.2
    "cpp": 54,           # C++ (GCC 9.2.0)
    "c": 50,             # C (GCC 9.2.0)
    "csharp": 51,        # C# Mono 6.6.0.161
    "fsharp": 87,        # F# .NET Core SDK 3.1.202
    "vbnet": 84,         # VB.NET .NET Core SDK 3.1.202
    "go": 60,            # Go 1.13.5
    "rust": 73,          # Rust 1.40.0
    "ruby": 72,          # Ruby 2.7.0
    "php": 68,           # PHP 7.4.1
    "swift": 83,         # Swift 5.2.3
    "perl": 85,          # Perl 5.28.1
    "lua": 64,           # Lua 5.3.5
    "bash": 46,          # Bash 5.0.0
    "r": 80,             # R 4.0.0
    "haskell": 61,       # Haskell GHC 8.8.1
    "ocaml": 65,         # OCaml 4.09.0
    "pascal": 67,        # Pascal FPC 3.0.4
    "fortran": 59,       # Fortran GFortran 9.2.0
    "cobol": 77,         # COBOL GnuCOBOL 2.2
    "prolog": 69,        # Prolog GNU Prolog 1.4.5
    "sqlite3": 82,       # SQL (SQLite 3.27.2)
}

# Language config: runtime name + version for Piston API
LANGUAGE_CONFIG = {
    "python": {"language": "python", "version": "3.10.0", "extension": "py"},
    "python2": {"language": "python2", "version": "2.7.18", "extension": "py"},
    "javascript": {"language": "javascript", "version": "18.15.0", "extension": "js"},
    "typescript": {"language": "typescript", "version": "5.0.3", "extension": "ts"},
    "java": {"language": "java", "version": "15.0.2", "extension": "java"},
    "kotlin": {"language": "kotlin", "version": "1.8.20", "extension": "kt"},
    "scala": {"language": "scala", "version": "3.2.2", "extension": "scala"},
    "groovy": {"language": "groovy", "version": "3.0.7", "extension": "groovy"},
    "cpp": {"language": "c++", "version": "10.2.0", "extension": "cpp"},
    "c": {"language": "gcc", "version": "10.2.0", "extension": "c"},
    "csharp": {"language": "csharp.net", "version": "5.0.201", "extension": "cs"},
    "fsharp": {"language": "fsharp.net", "version": "5.0.201", "extension": "fs"},
    "vbnet": {"language": "basic.net", "version": "5.0.201", "extension": "vb"},
    "go": {"language": "go", "version": "1.16.2", "extension": "go"},
    "rust": {"language": "rust", "version": "1.68.2", "extension": "rs"},
    "ruby": {"language": "ruby", "version": "3.0.1", "extension": "rb"},
    "php": {"language": "php", "version": "8.2.3", "extension": "php"},
    "swift": {"language": "swift", "version": "5.3.3", "extension": "swift"},
    "dart": {"language": "dart", "version": "2.19.6", "extension": "dart"},
    "perl": {"language": "perl", "version": "5.36.0", "extension": "pl"},
    "lua": {"language": "lua", "version": "5.4.4", "extension": "lua"},
    "bash": {"language": "bash", "version": "5.2.0", "extension": "sh"},
    "powershell": {"language": "pwsh", "version": "7.1.4", "extension": "ps1"},
    "r": {"language": "rscript", "version": "4.1.1", "extension": "r"},
    "julia": {"language": "julia", "version": "1.8.5", "extension": "jl"},
    "haskell": {"language": "haskell", "version": "9.0.1", "extension": "hs"},
    "elixir": {"language": "elixir", "version": "1.11.3", "extension": "ex"},
    "erlang": {"language": "erlang", "version": "23.0.0", "extension": "erl"},
    "ocaml": {"language": "ocaml", "version": "4.12.0", "extension": "ml"},
    "clojure": {"language": "clojure", "version": "1.10.3", "extension": "clj"},
    "pascal": {"language": "pascal", "version": "3.0.4", "extension": "pas"},
    "fortran": {"language": "fortran", "version": "10.2.0", "extension": "f90"},
    "cobol": {"language": "cobol", "version": "10.2.0", "extension": "cob"},
    "nim": {"language": "nim", "version": "1.6.2", "extension": "nim"},
    "crystal": {"language": "crystal", "version": "0.36.1", "extension": "cr"},
    "zig": {"language": "zig", "version": "0.10.1", "extension": "zig"},
    "v": {"language": "vlang", "version": "0.3.3", "extension": "v"},
    "d": {"language": "d", "version": "10.2.0", "extension": "d"},
    "racket": {"language": "racket", "version": "8.3.0", "extension": "rkt"},
    "lisp": {"language": "lisp", "version": "2.1.2", "extension": "lisp"},
    "prolog": {"language": "prolog", "version": "1.4.5", "extension": "pl"},
    "sqlite3": {"language": "sqlite3", "version": "3.36.0", "extension": "sql"},
}

# Starter code templates for each language
STARTER_CODE = {
    "python": '# Write your solution here\ndef solution():\n    pass\n\n# Read input\n',
    "python2": '# Write your solution here (Python 2)\n',
    "javascript": '// Write your solution here\nfunction solution() {\n  \n}\n\n// Read input from stdin\n',
    "typescript": '// Write your solution here\nfunction solution(): void {\n  \n}\n',
    "java": 'import java.util.*;\n\npublic class Main {\n    public static void main(String[] args) {\n        Scanner sc = new Scanner(System.in);\n        // Write your solution here\n    }\n}\n',
    "kotlin": 'fun main() {\n    // Write your solution here\n}\n',
    "scala": '@main def run(): Unit =\n    // Write your solution here\n    println("")\n',
    "groovy": '// Write your solution here\n',
    "cpp": '#include <iostream>\n#include <vector>\n#include <string>\nusing namespace std;\n\nint main() {\n    // Write your solution here\n    \n    return 0;\n}\n',
    "c": '#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n\nint main() {\n    // Write your solution here\n    \n    return 0;\n}\n',
    "csharp": 'using System;\n\nclass Program {\n    static void Main(string[] args) {\n        // Write your solution here\n    }\n}\n',
    "fsharp": '[<EntryPoint>]\nlet main argv =\n    // Write your solution here\n    0\n',
    "vbnet": 'Module Program\n    Sub Main()\n        \' Write your solution here\n    End Sub\nEnd Module\n',
    "go": 'package main\n\nimport (\n    "fmt"\n)\n\nfunc main() {\n    // Write your solution here\n    fmt.Println()\n}\n',
    "rust": 'fn main() {\n    // Write your solution here\n}\n',
    "ruby": '# Write your solution here\n',
    "php": '<?php\n// Write your solution here\n',
    "swift": 'import Foundation\n\n// Write your solution here\n',
    "dart": 'void main() {\n  // Write your solution here\n}\n',
    "perl": '#!/usr/bin/perl\nuse strict;\nuse warnings;\n\n# Write your solution here\n',
    "lua": '-- Write your solution here\n',
    "bash": '#!/bin/bash\n# Write your solution here\n',
    "powershell": '# Write your solution here\n',
    "r": '# Write your solution here\n',
    "julia": '# Write your solution here\n',
    "haskell": 'module Main where\n\nmain :: IO ()\nmain = do\n    -- Write your solution here\n    putStrLn ""\n',
    "elixir": 'defmodule Main do\n  def run do\n    # Write your solution here\n  end\nend\n\nMain.run()\n',
    "erlang": '-module(main).\n-export([main/1]).\n\nmain(_) ->\n    %% Write your solution here\n    ok.\n',
    "ocaml": '(* Write your solution here *)\nlet () = print_endline ""\n',
    "clojure": ';; Write your solution here\n(defn -main []\n  (println ""))\n\n(-main)\n',
    "pascal": 'program Main;\nbegin\n    { Write your solution here }\nend.\n',
    "fortran": 'program main\n    implicit none\n    ! Write your solution here\nend program main\n',
    "cobol": 'IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN.\nPROCEDURE DIVISION.\n    *> Write your solution here\n    STOP RUN.\n',
    "nim": '# Write your solution here\n',
    "crystal": '# Write your solution here\n',
    "zig": 'const std = @import("std");\n\npub fn main() !void {\n    // Write your solution here\n}\n',
    "v": 'fn main() {\n    // Write your solution here\n}\n',
    "d": 'import std.stdio;\n\nvoid main() {\n    // Write your solution here\n}\n',
    "racket": '#lang racket\n;; Write your solution here\n',
    "lisp": ';; Write your solution here\n',
    "prolog": '% Write your solution here\n:- initialization(main).\nmain :- halt.\n',
    "sqlite3": '-- Write your SQL here\n',
}


async def get_available_runtimes() -> list[dict]:
    """Fetch available runtimes from Piston API."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{PISTON_API_URL}/runtimes")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Failed to fetch Piston runtimes: %s", e)
        return []


class _SandboxUnavailable(Exception):
    """Raised when the sandbox provider itself can't run the code (network,
    auth, rate-limit, 5xx). Distinct from candidate-code failures (compile
    errors, runtime errors, wrong output) which are returned as normal results.
    A `_SandboxUnavailable` is what triggers a fallback to the next provider.
    """
    def __init__(self, message: str, body_preview: str = ""):
        super().__init__(message)
        self.message = message
        self.body_preview = body_preview


async def _execute_piston(
    language: str, code: str, stdin: str, timeout: int, lang_config: dict,
) -> dict:
    """Run code on Piston. Raises _SandboxUnavailable on infra failure."""
    # Cap run_timeout at the Piston instance's configured ceiling. The public
    # emkc.org instance enforces 3000ms; sending more triggers HTTP 400
    # "run_timeout cannot exceed the configured limit of 3000". Override via
    # PISTON_RUN_TIMEOUT_MS when self-hosting with a higher PISTON_RUN_TIMEOUT.
    max_run_timeout_ms = int(os.environ.get("PISTON_RUN_TIMEOUT_MS", "3000"))
    run_timeout_ms = min(timeout * 1000, max_run_timeout_ms)

    payload = {
        "language": lang_config["language"],
        "version": lang_config["version"],
        "files": [{"name": f"main.{lang_config['extension']}", "content": code}],
        "stdin": stdin,
        "run_timeout": run_timeout_ms,
        # Kotlin/Scala compilers routinely take 15-20s on first-run JVM warmup.
        # Keep this generous; candidates shouldn't get "Time limit exceeded"
        # from compiler startup rather than their code.
        "compile_timeout": int(os.environ.get("PISTON_COMPILE_TIMEOUT_MS", "60000")),
    }

    headers: dict[str, str] = {}
    if PISTON_API_KEY:
        headers["Authorization"] = f"Bearer {PISTON_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PISTON_API_URL}/execute", json=payload, headers=headers,
            )
            if resp.status_code >= 400:
                body_preview = resp.text[:500] if resp.text else "(empty body)"
                logger.error(
                    "Piston HTTP %s for lang=%s url=%s: %s",
                    resp.status_code, language, PISTON_API_URL, body_preview,
                )
                raise _SandboxUnavailable(
                    f"Piston HTTP {resp.status_code}", body_preview,
                )
            data = resp.json()
    except httpx.TimeoutException:
        logger.error("Piston timed out for language=%s after %ss", language, timeout)
        raise _SandboxUnavailable("Piston request timed out")
    except httpx.ConnectError as e:
        logger.error("Piston unreachable (connect error): %s", e)
        raise _SandboxUnavailable(f"Piston unreachable: {e}")

    run = data.get("run", {})
    compile_result = data.get("compile", {})

    # Compile or run status "TO" = timed-out (SIGKILL, code=null, empty output).
    # Surface an explicit error so the candidate doesn't see a silent blank.
    for phase, res in (("compile", compile_result), ("run", run)):
        if res and res.get("status") == "TO":
            return {
                "success": False,
                "stdout": "",
                "stderr": res.get("stderr", ""),
                "exit_code": -1,
                "signal": res.get("signal"),
                "error": f"{phase.capitalize()} time limit exceeded",
            }

    # Compilation errors are a CANDIDATE failure, not a sandbox failure —
    # return normally so the candidate sees the compile output.
    if compile_result and compile_result.get("code") != 0 and compile_result.get("stderr"):
        return {
            "success": False,
            "stdout": "",
            "stderr": compile_result.get("stderr", ""),
            "exit_code": compile_result.get("code", 1),
            "signal": compile_result.get("signal"),
            "error": "Compilation failed",
        }

    return {
        "success": run.get("code") == 0,
        "stdout": (run.get("stdout") or "").strip(),
        "stderr": (run.get("stderr") or "").strip(),
        "exit_code": run.get("code") if run.get("code") is not None else -1,
        "signal": run.get("signal"),
        "error": None,
    }


async def _execute_judge0(
    language: str, code: str, stdin: str, timeout: int,
) -> dict:
    """Run code on Judge0 CE (RapidAPI). Raises _SandboxUnavailable on infra
    failure. Caller must check JUDGE0_RAPIDAPI_KEY before invoking.
    """
    lang_id = JUDGE0_LANGUAGE_IDS.get(language)
    if lang_id is None:
        raise _SandboxUnavailable(
            f"Judge0 has no runtime mapping for language '{language}'"
        )

    # Judge0 expects base64 to safely transport unicode/binary.
    def b64(s: str) -> str:
        return base64.b64encode(s.encode("utf-8")).decode("ascii")

    payload = {
        "language_id": lang_id,
        "source_code": b64(code),
        "stdin": b64(stdin or ""),
        # Judge0 CE caps cpu_time_limit at 15s; clamp our request to be safe.
        "cpu_time_limit": min(max(timeout, 1), 15),
        "memory_limit": 128000,
    }
    headers = {
        "content-type": "application/json",
        "X-RapidAPI-Key": JUDGE0_RAPIDAPI_KEY,
        "X-RapidAPI-Host": JUDGE0_RAPIDAPI_HOST,
    }
    url = f"https://{JUDGE0_RAPIDAPI_HOST}/submissions?base64_encoded=true&wait=true"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                body_preview = resp.text[:500] if resp.text else "(empty body)"
                logger.error(
                    "Judge0 HTTP %s for lang=%s: %s",
                    resp.status_code, language, body_preview,
                )
                raise _SandboxUnavailable(
                    f"Judge0 HTTP {resp.status_code}", body_preview,
                )
            data = resp.json()
    except httpx.TimeoutException:
        raise _SandboxUnavailable("Judge0 request timed out")
    except httpx.ConnectError as e:
        raise _SandboxUnavailable(f"Judge0 unreachable: {e}")

    def b64dec(s) -> str:
        if not s:
            return ""
        try:
            return base64.b64decode(s).decode("utf-8", errors="replace")
        except Exception:
            return ""

    stdout = b64dec(data.get("stdout"))
    stderr = b64dec(data.get("stderr"))
    compile_output = b64dec(data.get("compile_output"))
    status = data.get("status") or {}
    status_id = status.get("id", 0)
    status_desc = status.get("description", "")

    # Judge0 status IDs: 3=Accepted, 5=TLE, 6=Compilation Error, 7-12=Runtime
    if status_id == 6 and compile_output:
        return {
            "success": False,
            "stdout": "",
            "stderr": compile_output.strip(),
            "exit_code": 1,
            "signal": None,
            "error": "Compilation failed",
        }

    return {
        "success": status_id == 3,
        "stdout": stdout.strip(),
        "stderr": (stderr or compile_output).strip(),
        "exit_code": 0 if status_id == 3 else 1,
        "signal": status_desc if status_id != 3 else None,
        "error": None if status_id == 3 else status_desc,
    }


async def execute_code(
    language: str,
    code: str,
    stdin: str = "",
    timeout: int = 10,
) -> dict:
    """
    Execute code using Piston (primary) with Judge0 CE as fallback when
    JUDGE0_RAPIDAPI_KEY is set. Candidate-code errors (compile/runtime) are
    returned normally; only sandbox-infra failures trigger the fallback.

    Returns:
        {
            "success": bool,
            "stdout": str,
            "stderr": str,
            "exit_code": int,
            "signal": str | None,
            "error": str | None,
        }
    """
    lang_config = LANGUAGE_CONFIG.get(language)
    if not lang_config:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "signal": None,
            "error": f"Unsupported language: {language}. Supported: {', '.join(LANGUAGE_CONFIG.keys())}",
        }

    piston_error: Optional[_SandboxUnavailable] = None
    try:
        return await _execute_piston(language, code, stdin, timeout, lang_config)
    except _SandboxUnavailable as e:
        piston_error = e
        logger.warning("Piston unavailable, attempting fallback: %s", e.message)
    except Exception as e:
        logger.error("Piston unexpected failure: %s: %s", type(e).__name__, e)
        piston_error = _SandboxUnavailable(f"{type(e).__name__}: {e}")

    if JUDGE0_RAPIDAPI_KEY:
        try:
            result = await _execute_judge0(language, code, stdin, timeout)
            logger.info("Fell back to Judge0 for lang=%s (Piston: %s)",
                        language, piston_error.message)
            return result
        except _SandboxUnavailable as e:
            logger.error("Judge0 fallback also failed: %s", e.message)
            return {
                "success": False,
                "stdout": "",
                "stderr": e.body_preview or e.message,
                "exit_code": -1,
                "signal": None,
                "error": (
                    f"Both sandboxes failed. Piston: {piston_error.message}. "
                    f"Judge0: {e.message}."
                ),
            }
        except Exception as e:
            logger.error("Judge0 unexpected failure: %s: %s", type(e).__name__, e)

    # No fallback configured (or fallback raised non-_SandboxUnavailable) —
    # return Piston's error with the original friendly message.
    if "401" in piston_error.message or "whitelist" in (piston_error.body_preview or "").lower():
        friendly = (
            "Code sandbox is unreachable. The public Piston API is "
            "whitelist-only as of 2026-02-15. Self-host Piston "
            "(docker compose up -d) or set JUDGE0_RAPIDAPI_KEY in .env "
            "to enable the Judge0 fallback."
        )
    else:
        friendly = (
            f"Code sandbox is unreachable: {piston_error.message}. "
            f"Set JUDGE0_RAPIDAPI_KEY in .env to enable the Judge0 fallback."
        )
    return {
        "success": False,
        "stdout": "",
        "stderr": piston_error.body_preview or piston_error.message,
        "exit_code": -1,
        "signal": None,
        "error": friendly,
    }


async def run_test_cases(
    language: str,
    code: str,
    test_cases: list[dict],
    timeout: int = 10,
) -> dict:
    """
    Run code against multiple test cases.

    Args:
        language: Programming language
        code: Source code to execute
        test_cases: List of {"input": str, "expected_output": str, "description": str}
        timeout: Per-test timeout in seconds

    Returns:
        {
            "total": int,
            "passed": int,
            "failed": int,
            "results": [
                {
                    "test_index": int,
                    "description": str,
                    "input": str,
                    "expected_output": str,
                    "actual_output": str,
                    "passed": bool,
                    "stderr": str,
                    "error": str | None,
                }
            ]
        }
    """
    results = []
    passed = 0

    for i, tc in enumerate(test_cases):
        stdin = tc.get("input", "")
        expected = tc.get("expected_output", "").strip()
        description = tc.get("description", f"Test case {i + 1}")

        exec_result = await execute_code(language, code, stdin=stdin, timeout=timeout)

        actual = exec_result["stdout"].strip()
        is_passed = actual == expected and exec_result["success"]

        if is_passed:
            passed += 1

        results.append({
            "test_index": i,
            "description": description,
            "input": stdin,
            "expected_output": expected,
            "actual_output": actual,
            "passed": is_passed,
            "stderr": exec_result["stderr"],
            "error": exec_result.get("error"),
        })

    return {
        "total": len(test_cases),
        "passed": passed,
        "failed": len(test_cases) - passed,
        "results": results,
    }


DISPLAY_NAMES = {
    "python": "Python",
    "python2": "Python 2",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "java": "Java",
    "kotlin": "Kotlin",
    "scala": "Scala",
    "groovy": "Groovy",
    "cpp": "C++",
    "c": "C",
    "csharp": "C#",
    "fsharp": "F#",
    "vbnet": "VB.NET",
    "go": "Go",
    "rust": "Rust",
    "ruby": "Ruby",
    "php": "PHP",
    "swift": "Swift",
    "dart": "Dart",
    "perl": "Perl",
    "lua": "Lua",
    "bash": "Bash",
    "powershell": "PowerShell",
    "r": "R",
    "julia": "Julia",
    "haskell": "Haskell",
    "elixir": "Elixir",
    "erlang": "Erlang",
    "ocaml": "OCaml",
    "clojure": "Clojure",
    "pascal": "Pascal",
    "fortran": "Fortran",
    "cobol": "COBOL",
    "nim": "Nim",
    "crystal": "Crystal",
    "zig": "Zig",
    "v": "V",
    "d": "D",
    "racket": "Racket",
    "lisp": "Common Lisp",
    "prolog": "Prolog",
    "sqlite3": "SQLite",
}


def get_supported_languages() -> list[dict]:
    """Return list of supported languages with their metadata."""
    return [
        {
            "id": key,
            "name": DISPLAY_NAMES.get(key, key.title()),
            "extension": cfg["extension"],
        }
        for key, cfg in LANGUAGE_CONFIG.items()
    ]


def get_starter_code(language: str) -> str:
    """Return starter code template for a language."""
    return STARTER_CODE.get(language, f"// Write your {language} solution here\n")
