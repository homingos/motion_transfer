#!/usr/bin/env python3
"""
Syntax and structure validation for server.py endpoints.
"""

import ast
import sys
from pathlib import Path

def validate_syntax(file_path):
    """Check if Python file has valid syntax."""
    try:
        with open(file_path) as f:
            ast.parse(f.read())
        return True, None
    except SyntaxError as e:
        return False, str(e)

def find_function_definitions(file_path):
    """Extract all function definitions from a Python file."""
    with open(file_path) as f:
        tree = ast.parse(f.read())

    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            params = [arg.arg for arg in node.args.args]
            functions[node.name] = {
                'params': params,
                'lineno': node.lineno,
                'decorators': [d.id if isinstance(d, ast.Name) else
                              (d.attr if isinstance(d, ast.Attribute) else str(d))
                              for d in node.decorator_list]
            }
    return functions

def validate_endpoints():
    """Validate that required endpoints are defined."""
    server_path = Path("/teamspace/studios/this_studio/motion_transfer/server.py")

    # Check syntax
    valid, error = validate_syntax(server_path)
    if not valid:
        print(f"✗ Syntax error in server.py: {error}")
        return False

    print("✓ server.py has valid Python syntax")

    # Extract functions
    functions = find_function_definitions(server_path)

    # Check for required endpoints
    required_endpoints = {
        'avatar_info': {
            'params': ['avatar_id'],
            'decorators': ['get']
        },
        'update_avatar_status': {
            'params': ['avatar_id', 'status'],
            'decorators': ['post']
        },
        'idle_motion_preview': {
            'params': ['avatar_id'],
            'decorators': ['get']
        },
        'animate': {
            'params': ['image', 'avatar_id', 'prompt'],
            'decorators': ['post']
        },
        'get_job': {
            'params': ['request_id'],
            'decorators': ['get']
        },
    }

    print("\n✓ Found functions:")
    missing = []
    for func_name, expected_info in required_endpoints.items():
        if func_name in functions:
            info = functions[func_name]
            print(f"  ✓ {func_name} (line {info['lineno']}, params: {info['params']})")
        else:
            print(f"  ✗ {func_name} NOT FOUND")
            missing.append(func_name)

    if missing:
        print(f"\n✗ Missing endpoints: {missing}")
        return False

    return True

def check_api_mode_logic():
    """Check that API_MODE logic is in place."""
    server_path = Path("/teamspace/studios/this_studio/motion_transfer/server.py")

    with open(server_path) as f:
        content = f.read()

    # Check for API_MODE usage
    checks = [
        ('API_MODE definition', 'API_MODE = os.environ.get("API_MODE"'),
        ('API_MODE r2_only check', 'if API_MODE == "r2_only"'),
        ('Animate endpoint', '@app.post("/animate")'),
        ('Avatar info endpoint', '@app.get("/avatar/{avatar_id}/info")'),
        ('Avatar status endpoint', '@app.post("/avatar/{avatar_id}/status")'),
    ]

    print("\n✓ Code checks:")
    all_found = True
    for check_name, check_str in checks:
        if check_str in content:
            print(f"  ✓ {check_name}")
        else:
            print(f"  ✗ {check_name} NOT FOUND")
            all_found = False

    return all_found

def main():
    """Run all validation tests."""
    print("=" * 60)
    print("Motion Transfer Syntax & Structure Validation")
    print("=" * 60)

    tests = [
        ("Endpoint Definitions", validate_endpoints),
        ("API Mode Logic", check_api_mode_logic),
    ]

    results = []
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"\n✗ {test_name} failed: {e}")
            import traceback
            traceback.print_exc()
            results.append((test_name, False))

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)

    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {test_name}")

    all_passed = all(result for _, result in results)
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())
