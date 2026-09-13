#!/usr/bin/env python3
"""
Python Skill Compliance Validator

This script analyzes a Python file to ensure it complies with the ithqbot
Advanced Python Skill Interface standards defined in SKILL_SPEC.md.

Usage:
  python3 validate_skill_compliance.py <path_to_skill.py>
"""

import ast
import sys
from pathlib import Path


def _base_name(base: ast.expr) -> str:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return ""


def _is_valid_execute_signature(node: ast.AsyncFunctionDef, base_names: set[str]) -> bool:
    arg_names = [arg.arg for arg in node.args.args]
    if not arg_names or arg_names[0] != "self":
        return False
    if "BasePythonSkill" in base_names:
        return "context" in arg_names and "payload" in arg_names
    if "Tool" in base_names:
        return True
    return False


def check_skill_compliance(file_path: Path) -> list[str]:
    errors = []

    if not file_path.exists():
        return [f"File not found: {file_path}"]

    source = file_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"Syntax error trying to parse {file_path.name}: {e}"]

    has_execute = False
    imports_ml_provider = False
    uses_tempfile = False
    violates_workspace = False

    for node in ast.walk(tree):
        # Check imports for hardcoded LLM providers
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("openai", "anthropic", "google.generativeai", "dashscope"):
                    imports_ml_provider = True
                if alias.name == "tempfile":
                    uses_tempfile = True
        elif isinstance(node, ast.ImportFrom):
            if node.module in ("openai", "anthropic", "google.generativeai", "dashscope"):
                imports_ml_provider = True
            if node.module == "tempfile":
                uses_tempfile = True

        # Look for suspicious strings relating to 'workspace'
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "workspace/" in node.value or "/workspace" in node.value:
                violates_workspace = True

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {_base_name(base) for base in node.bases}
        if "Tool" not in base_names and "BasePythonSkill" not in base_names:
            continue
        for child in node.body:
            if isinstance(child, ast.AsyncFunctionDef) and child.name == "execute":
                if _is_valid_execute_signature(child, base_names):
                    has_execute = True
                    break
        if has_execute:
            break

    # Rule Validations
    if not has_execute:
        errors.append("❌ Missing compliant `async def execute(...)` method signature for Tool or BasePythonSkill.")
    else:
        print("✅ Found compliant `execute` method signature.")

    if imports_ml_provider:
        errors.append("❌ Hardcoded LLM SDK imports detected! (e.g., openai, anthropic). Use `context.call_llm()` instead.")
    else:
        print("✅ No hardcoded LLM provider SDKs found.")

    if violates_workspace:
        errors.append("❌ Suspicious usage of static workspace paths found. Skills must be stateless; use `tempfile`.")
    else:
        print("✅ No static workspace dependencies found.")

    if not uses_tempfile:
        print("⚠️ Warning: `tempfile` is not imported. Ensure you aren't saving files permanently on disk.")
    else:
        print("✅ Detected `tempfile` import for stateless data processing.")

    return errors


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 validate_skill_compliance.py <path_to_skill.py>")
        sys.exit(1)

    target = Path(sys.argv[1])
    print(f"🔍 Analyzing: {target.name} ...\n")

    validation_errors = check_skill_compliance(target)
    print("")

    if validation_errors:
        print("🚨 Skill Validation FAILED with the following violations:")
        for err in validation_errors:
            print("  " + err)
        sys.exit(1)
    else:
        print("🎉 Skill Validation PASSED! The skill complies with SKILL_SPEC.md")
        sys.exit(0)
