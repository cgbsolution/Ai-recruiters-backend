"""
Code Execution Service — runs candidate code in a sandboxed environment.

Uses the Piston API (https://github.com/engineer-man/piston) for safe,
multi-language code execution. No API key required.

Supported languages: Python, JavaScript, Java, C++, TypeScript, Go, Ruby, C#
"""

import asyncio
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

PISTON_API_URL = "https://emkc.org/api/v2/piston"

# Language config: runtime name + version for Piston API
LANGUAGE_CONFIG = {
    "python": {"language": "python", "version": "3.10.0", "extension": "py"},
    "javascript": {"language": "javascript", "version": "18.15.0", "extension": "js"},
    "typescript": {"language": "typescript", "version": "5.0.3", "extension": "ts"},
    "java": {"language": "java", "version": "15.0.2", "extension": "java"},
    "cpp": {"language": "c++", "version": "10.2.0", "extension": "cpp"},
    "c": {"language": "c", "version": "10.2.0", "extension": "c"},
    "go": {"language": "go", "version": "1.16.2", "extension": "go"},
    "ruby": {"language": "ruby", "version": "3.0.1", "extension": "rb"},
    "csharp": {"language": "csharp", "version": "6.12.0", "extension": "cs"},
}

# Starter code templates for each language
STARTER_CODE = {
    "python": '# Write your solution here\ndef solution():\n    pass\n\n# Read input\n',
    "javascript": '// Write your solution here\nfunction solution() {\n  \n}\n\n// Read input from stdin\n',
    "typescript": '// Write your solution here\nfunction solution(): void {\n  \n}\n',
    "java": 'import java.util.*;\n\npublic class Main {\n    public static void main(String[] args) {\n        Scanner sc = new Scanner(System.in);\n        // Write your solution here\n    }\n}\n',
    "cpp": '#include <iostream>\n#include <vector>\n#include <string>\nusing namespace std;\n\nint main() {\n    // Write your solution here\n    \n    return 0;\n}\n',
    "c": '#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n\nint main() {\n    // Write your solution here\n    \n    return 0;\n}\n',
    "go": 'package main\n\nimport (\n    "fmt"\n)\n\nfunc main() {\n    // Write your solution here\n    fmt.Println()\n}\n',
    "ruby": '# Write your solution here\n',
    "csharp": 'using System;\n\nclass Program {\n    static void Main(string[] args) {\n        // Write your solution here\n    }\n}\n',
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

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{PISTON_API_URL}/execute", json=payload)
            resp.raise_for_status()
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
        return {
            "success": False,
            "stdout": "",
            "stderr": "Code execution timed out",
            "exit_code": -1,
            "signal": "SIGTERM",
            "error": "Execution timed out",
        }
    except Exception as e:
        logger.error("Code execution failed: %s", e)
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "signal": None,
            "error": f"Execution service error: {str(e)}",
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


def get_supported_languages() -> list[dict]:
    """Return list of supported languages with their metadata."""
    return [
        {"id": key, "name": key.title() if key != "cpp" else "C++", "extension": cfg["extension"]}
        for key, cfg in LANGUAGE_CONFIG.items()
    ]


def get_starter_code(language: str) -> str:
    """Return starter code template for a language."""
    return STARTER_CODE.get(language, f"// Write your {language} solution here\n")
