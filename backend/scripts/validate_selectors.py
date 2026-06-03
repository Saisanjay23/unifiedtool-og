"""
Selector Validation CLI Utility.
Executes the live scraper selector validation checks and outputs a formatted console report.
"""

import argparse
import asyncio
import os
import sys

# Add project root to sys.path so backend modules can be imported
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from backend.core.runtime import configure_runtime
configure_runtime()

from backend.core.selector_validator import SelectorValidator

async def main():
    parser = argparse.ArgumentParser(
        description="OSINT Scraper Selector Validation Tool",
    )
    parser.add_argument(
        "--platform",
        type=str,
        choices=["facebook", "instagram", "twitter", "all"],
        default="all",
        help="Validate selectors for a specific platform or all platforms (default: all)",
    )
    args = parser.parse_args()

    validator = SelectorValidator()
    
    if args.platform == "all":
        platforms = ["facebook", "instagram", "twitter"]
    else:
        platforms = [args.platform]

    print("\n" + "=" * 60)
    print("      SCRAPER SELECTOR INTEGRITY VALIDATION RUN")
    print("=" * 60)
    print(f"Target Platforms: {', '.join(p.upper() for p in platforms)}")
    print("Starting validation scrapers (this may take up to 20s per platform)...")
    print("=" * 60 + "\n")

    for platform in platforms:
        print(f"[{platform.upper()}] Starting validation run...")
        result = await validator.validate_platform(platform)
        print("-" * 60)
        
        if result.get("error"):
            print(f"[CRASH] [{platform.upper()}] Validation CRASHED:")
            print(f"   Error: {result['error']}")
            print("-" * 60 + "\n")
            continue

        success = result["success"]
        status_symbol = "[OK]" if success else "[WARN]"
        status_text = "PASS" if success else "DEGRADED"
        
        print(f"{status_symbol} [{platform.upper()}] Status: {status_text}")
        print(f"   Target URL: {result['url']}")
        print("   Fields:")
        
        metrics = result.get("metrics", {})
        for field, metrics_data in metrics.items():
            field_symbol = "   [PASS]" if metrics_data["status"] == "pass" else "   [FAIL]" if metrics_data["status"] == "fail" else "   [WARN]"
            print(f"{field_symbol} {field:<16}: {str(metrics_data['value'])[:60]} ({metrics_data['details']})")
        
        print("-" * 60 + "\n")

    print("Selector validation completed.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nValidation aborted by user.")
        sys.exit(1)
