"""
Code Execution Service — runs candidate code in a sandboxed environment.

Uses the Piston API (https://github.com/engineer-man/piston) for safe,
multi-language code execution. No API key required.

Supported languages: all mainstream Piston runtimes (40+ languages).
"""

import asyncio
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
    "c": {"language": "c", "version": "10.2.0", "extension": "c"},
    "csharp": {"language": "csharp", "version": "6.12.0", "extension": "cs"},
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


async def execute_code(
    language: str,
    code: str,
    stdin: str = "",
    timeout: int = 10,
) -> dict:
    """
    Execute code using Piston API.

    Returns:
        {
            "success": bool,
            "stdout": str,
            "stderr": str,
            "exit_code": int,
            "signal": str | None,
            "error": str | None,   # Only if execution itself failed
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

    payload = {
        "language": lang_config["language"],
        "version": lang_config["version"],
        "files": [
            {
                "name": f"main.{lang_config['extension']}",
                "content": code,
            }
        ],
        "stdin": stdin,
        "run_timeout": timeout * 1000,  # ms
        "compile_timeout": 15000,       # 15s for compilation
        "run_memory_limit": 256_000_000,  # 256 MB
    }

    headers: dict[str, str] = {}
    if PISTON_API_KEY:
        headers["Authorization"] = f"Bearer {PISTON_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PISTON_API_URL}/execute", json=payload, headers=headers,
            )
            # Piston can reply 4xx/5xx with a human-readable error body. Surface
            # it loudly instead of silently converting to a generic 0/N failure.
            if resp.status_code >= 400:
                body_preview = resp.text[:500] if resp.text else "(empty body)"
                logger.error(
                    "Piston HTTP %s for lang=%s url=%s: %s",
                    resp.status_code, language, PISTON_API_URL, body_preview,
                )
                if resp.status_code == 401:
                    friendly = (
                        "The public Piston API is now whitelist-only. "
                        "Either self-host Piston (docker run -d -p 2000:2000 "
                        "ghcr.io/engineer-man/piston and set "
                        "PISTON_API_URL=http://localhost:2000/api/v2) "
                        "or set PISTON_API_KEY with a whitelisted bearer token."
                    )
                else:
                    friendly = (
                        f"Piston API returned HTTP {resp.status_code}. "
                        f"The sandbox may be rate-limited or down."
                    )
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": body_preview,
                    "exit_code": -1,
                    "signal": None,
                    "error": friendly,
                }
            data = resp.json()

        run = data.get("run", {})
        compile_result = data.get("compile", {})

        # Check for compilation errors first
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
            "success": run.get("code", 1) == 0,
            "stdout": (run.get("stdout") or "").strip(),
            "stderr": (run.get("stderr") or "").strip(),
            "exit_code": run.get("code", 0),
            "signal": run.get("signal"),
            "error": None,
        }

    except httpx.TimeoutException:
        logger.error("Piston timed out for language=%s after %ss", language, timeout)
        return {
            "success": False,
            "stdout": "",
            "stderr": "Code execution timed out",
            "exit_code": -1,
            "signal": "SIGTERM",
            "error": "Execution timed out",
        }
    except httpx.ConnectError as e:
        logger.error("Piston unreachable (connect error): %s", e)
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "signal": None,
            "error": "Cannot reach Piston API (network unreachable). "
                     "Check your internet connection or firewall.",
        }
    except Exception as e:
        logger.error("Code execution failed: %s: %s", type(e).__name__, e)
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "signal": None,
            "error": f"Execution service error: {type(e).__name__}: {e}",
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
