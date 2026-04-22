"""End-to-end smoke test for code_execution_service language mappings."""
import asyncio
import sys
from app.services.code_execution_service import execute_code, LANGUAGE_CONFIG

CASES = {
    "python":     'print("hi py")',
    "javascript": 'console.log("hi js");',
    "typescript": 'const m: string = "hi ts"; console.log(m);',
    "java":       'public class Main { public static void main(String[] a){ System.out.println("hi java"); } }',
    "go":         'package main\nimport "fmt"\nfunc main(){ fmt.Println("hi go") }',
    "rust":       'fn main(){ println!("hi rust"); }',
    "c":          '#include <stdio.h>\nint main(void){ printf("hi c\\n"); return 0; }',
    "cpp":        '#include <iostream>\nint main(){ std::cout << "hi cpp\\n"; return 0; }',
    "csharp":     'using System; class P { static void Main(){ Console.WriteLine("hi cs"); } }',
    "ruby":       'puts "hi ruby"',
    "php":        '<?php echo "hi php\\n";',
    "bash":       'echo "hi bash"',
    "kotlin":     'fun main(){ println("hi kotlin") }',
    "scala":      '@main def run(): Unit = println("hi scala")',
    "swift":      'print("hi swift")',
}

async def main():
    results = []
    for lang, code in CASES.items():
        if lang not in LANGUAGE_CONFIG:
            results.append((lang, "SKIP (not in LANGUAGE_CONFIG)"))
            continue
        try:
            r = await execute_code(lang, code, timeout=30)
            ok = r["success"] and "hi" in r["stdout"]
            status = "PASS" if ok else f"FAIL code={r['exit_code']} stderr={(r['stderr'] or r.get('error',''))[:120]}"
            results.append((lang, f"{status} stdout={r['stdout']!r}"))
        except Exception as e:
            results.append((lang, f"EXCEPTION {type(e).__name__}: {e}"))
    passed = sum(1 for _, s in results if s.startswith("PASS"))
    for lang, s in results:
        print(f"  {lang:12s} {s}")
    print(f"\n{passed}/{len(results)} languages passed")
    return 0 if passed == len(results) else 1

sys.exit(asyncio.run(main()))
