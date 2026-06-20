#!/usr/bin/env python3
"""Create 4-second looped reference video: first 2s + reverse."""

import subprocess
import sys
from pathlib import Path

def create_4sec_reference():
    """Extract 2s from idle_avatar_15_reverse.mp4, reverse it, concatenate."""

    assets_dir = Path(__file__).parent / "assets"
    input_file = assets_dir / "idle_avatar_15_reverse.mp4"
    output_file = assets_dir / "idle_avatar_4sec_loop.mp4"

    if not input_file.exists():
        print(f"❌ Input not found: {input_file}")
        sys.exit(1)

    # Extract first 2 seconds
    clip1 = "/tmp/clip_forward.mp4"
    cmd1 = f"ffmpeg -i {input_file} -t 2 -c copy {clip1} -y 2>&1"
    print(f"1️⃣ Extracting first 2s: {cmd1}")
    result = subprocess.run(cmd1, shell=True)
    if result.returncode != 0:
        print("❌ Extract failed")
        sys.exit(1)

    # Create reverse clip
    clip2 = "/tmp/clip_reverse.mp4"
    cmd2 = f"ffmpeg -i {clip1} -vf reverse -af areverse {clip2} -y 2>&1"
    print(f"2️⃣ Creating reverse: {cmd2}")
    result = subprocess.run(cmd2, shell=True)
    if result.returncode != 0:
        print("❌ Reverse failed")
        sys.exit(1)

    # Concatenate
    concat_file = "/tmp/concat.txt"
    with open(concat_file, "w") as f:
        f.write(f"file '{clip1}'\n")
        f.write(f"file '{clip2}'\n")

    cmd3 = f"ffmpeg -f concat -safe 0 -i {concat_file} -c copy {output_file} -y 2>&1"
    print(f"3️⃣ Concatenating: {cmd3}")
    result = subprocess.run(cmd3, shell=True)
    if result.returncode != 0:
        print("❌ Concat failed")
        sys.exit(1)

    print(f"✅ Created: {output_file}")
    print(f"   Size: {output_file.stat().st_size / 1024:.1f} KB")

if __name__ == "__main__":
    create_4sec_reference()
