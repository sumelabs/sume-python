"""Make an image, then animate it. Needs SUME_API_KEY. Costs about $0.06."""

import sume

image = sume.run("gpt-image-2", "a ceramic cup on a wooden table, morning light", quality="low")
print("image", image.save("cup.png"), f"${image.usd}")

video = sume.run("grok-imagine-video-1.5", "slow push in, steam rising", image="cup.png", duration=4, resolution="480p")
print("video", video.save("cup.mp4"), f"${video.usd}")
