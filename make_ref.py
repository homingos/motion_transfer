"""Quick Modal job to create 4-second looped reference video."""

import modal
import subprocess
from pathlib import Path

image = modal.Image.debian_slim().apt_install("ffmpeg")
app = modal.App("create-reference-video", image=image)

@app.function()
def create_4sec_loop():
    """Create idle_avatar_4sec_loop.mp4 in /tmp."""

    input_file = "/tmp/idle_avatar_15_reverse.mp4"
    output_file = "/tmp/idle_avatar_4sec_loop.mp4"

    # Step 1: Extract first 2s
    clip1 = "/tmp/clip_forward.mp4"
    cmd1 = f"ffmpeg -i {input_file} -t 2 -c copy {clip1} -y"
    print(f"Extracting first 2s...")
    subprocess.run(cmd1, shell=True, check=True)

    # Step 2: Reverse it
    clip2 = "/tmp/clip_reverse.mp4"
    cmd2 = f"ffmpeg -i {clip1} -vf reverse -af areverse {clip2} -y"
    print(f"Creating reverse...")
    subprocess.run(cmd2, shell=True, check=True)

    # Step 3: Concatenate
    concat_file = "/tmp/concat.txt"
    with open(concat_file, "w") as f:
        f.write(f"file '{clip1}'\n")
        f.write(f"file '{clip2}'\n")

    cmd3 = f"ffmpeg -f concat -safe 0 -i {concat_file} -c copy {output_file} -y"
    print(f"Concatenating...")
    subprocess.run(cmd3, shell=True, check=True)

    print(f"✅ Created {output_file}")
    return output_file

if __name__ == "__main__":
    with app.run():
        result = create_4sec_loop.remote()
        print(f"Result: {result}")
