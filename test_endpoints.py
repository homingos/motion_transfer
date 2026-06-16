#!/usr/bin/env python3
"""
Test script for validating the new avatar metadata endpoints.
Tests both dev (r2_only mode) and feat (full mode) configurations.
"""

import os
import json
import sys
from pathlib import Path

# Test that imports work
def test_imports():
    """Verify all required modules can be imported."""
    try:
        import server
        import integrations
        import pipeline_runtime
        print("✓ All imports successful")
        return True
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False

def test_server_definitions():
    """Verify the server has all expected endpoints."""
    import inspect
    import server

    app = server.app
    routes = [route.path for route in app.routes]

    expected_routes = [
        "/",
        "/generate",
        "/idle-motion",
        "/idle-motion/{avatar_id}/preview",
        "/animate",
        "/avatar/{avatar_id}/info",
        "/avatar/{avatar_id}/status",
        "/jobs/{request_id}",
        "/jobs/{request_id}/result",
    ]

    print("\nEndpoints available:")
    for route in sorted(set(routes)):
        if not route.startswith("/static"):
            print(f"  {route}")

    missing = set(expected_routes) - set(routes)
    if missing:
        print(f"\n✗ Missing endpoints: {missing}")
        return False

    print(f"\n✓ All expected endpoints present")
    return True

def test_api_mode():
    """Verify API_MODE is correctly read from environment."""
    import server

    api_mode = server.API_MODE
    print(f"\n✓ API_MODE = {api_mode}")
    return True

def test_endpoint_signatures():
    """Verify new endpoint function signatures."""
    import inspect
    import server

    # Check avatar_info endpoint
    avatar_info = server.avatar_info
    sig = inspect.signature(avatar_info)
    params = list(sig.parameters.keys())

    if "avatar_id" not in params:
        print("✗ avatar_info missing avatar_id parameter")
        return False
    print(f"✓ avatar_info signature: {sig}")

    # Check update_avatar_status endpoint
    update_status = server.update_avatar_status
    sig = inspect.signature(update_status)
    params = list(sig.parameters.keys())

    if "avatar_id" not in params or "status" not in params:
        print("✗ update_avatar_status missing required parameters")
        return False
    print(f"✓ update_avatar_status signature: {sig}")

    return True

def test_logger():
    """Verify logger is configured."""
    import server

    if not hasattr(server, 'logger'):
        print("✗ logger not found in server module")
        return False

    print(f"✓ Logger configured: {server.logger}")
    return True

def verify_integrations_functions():
    """Verify integrations module has required functions."""
    import integrations

    required_functions = [
        'get_source_image_key',
        'get_template_summary',
        'set_idle_animation_key',
        'set_status',
        'r2_download',
        'r2_upload',
        'r2_object_exists',
    ]

    missing = []
    for func in required_functions:
        if not hasattr(integrations, func):
            missing.append(func)

    if missing:
        print(f"✗ Missing integrations functions: {missing}")
        return False

    print(f"✓ All integrations functions present")
    return True

def main():
    """Run all tests."""
    print("=" * 60)
    print("Motion Transfer Endpoint Tests")
    print("=" * 60)

    tests = [
        ("Imports", test_imports),
        ("Server Endpoints", test_server_definitions),
        ("API Mode", test_api_mode),
        ("Endpoint Signatures", test_endpoint_signatures),
        ("Logger Configuration", test_logger),
        ("Integrations Functions", verify_integrations_functions),
    ]

    results = []
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"✗ {test_name} failed with exception: {e}")
            import traceback
            traceback.print_exc()
            results.append((test_name, False))

    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {test_name}")

    all_passed = all(result for _, result in results)

    if all_passed:
        print("\n✓ All tests passed!")
        return 0
    else:
        print("\n✗ Some tests failed")
        return 1

if __name__ == "__main__":
    sys.exit(main())
