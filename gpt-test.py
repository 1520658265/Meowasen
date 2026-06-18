# -*- coding: utf-8 -*-
import httpx
import base64
import json
import time

BASE_URL = "https://bobdong.cn/v1"
API_KEY = ""

prompt = """
pixel art style, top-down tile reference texture, game terrain material, simple readable pattern, limited color palette, no perspective, no characters, seamless-ready reference, 64x64 pixel game asset 16-tile minimal autotile terrain set arranged in a strict 4x4 logical grid. This tileset must be usable by code to assemble arbitrary terrain shapes on a base map. Do not draw visible grid lines, labels, tile numbers, UI, or borders between cells. Keep the same terrain material, edge style, palette, lighting, and pixel scale across all cells. The overlay terrain should transition cleanly into a neutral base terrain around its edges. volcanic terrain autotile for a top-down RPG map: molten lava overlay transitioning into dark cracked basalt and volcanic ash ground, glowing orange lava center, scorched black rock edges, readable pixel art, seamless repeatable terrain, clean edges for arbitrary lava pool shapes cell 1 center fill, cell 2 top edge, cell 3 bottom edge, cell 4 left edge cell 5 right edge, cell 6 outer top-left corner, cell 7 outer top-right corner, cell 8 outer bottom-left corner cell 9 outer bottom-right corner, cell 10 inner top-left corner, cell 11 inner top-right corner, cell 12 inner bottom-left corner cell 13 inner bottom-right corner, cell 14 isolated island, cell 15 horizontal strip, cell 16 vertical strip Create one image only. Target canvas: 1024x1024. Frame layout: terrain_autotile16, grid=4x4. Strictly follow the grid. Return only the image. avoid: realistic, photorealistic, blurry, noisy, characters, props, text, watermark, perspective view, complex scene, visible grid lines, labels, tile numbers, characters, props, roads, buildings, scene composition, perspective, shadows, inconsistent material, mismatched edges, random unrelated texture cells, thick outlines around cells
"""

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

body = {
    "model": "gpt-image-2",
    "prompt": prompt.strip(),
    "size": "1024x1024",
    "n": 1,
    "stream": True
}

print("正在生成图片，请稍候...")

# 4K 分辨率生成时间较长，使用明确的 Timeout 对象配置
# 连接超时 30 秒，读取超时 600 秒（10 分钟）
timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)

with httpx.Client(timeout=timeout) as client:
    print("发送流式请求，预计需要几分钟，请耐心等待...")

    with client.stream("POST", f"{BASE_URL}/images/generations", headers=headers, json=body) as r:
        print(f"HTTP 状态码: {r.status_code}")

        if r.status_code != 200:
            print(f"请求失败: {r.status_code}")
            print(r.read().decode())
            exit(1)

        # 收集流式响应（中转站直接返回原始 JSON，非 SSE 格式）
        chunks = []
        received = 0
        total = int(r.headers.get("content-length", 0))
        if total == 0:
            # 中转站无 Content-Length，按 1024x1024 经验值估算（约 2.5MB）
            total = 2500 * 1024
            estimated = True
        else:
            estimated = False
        for chunk in r.iter_bytes():
            chunks.append(chunk)
            received += len(chunk)
            pct = min(int(received / total * 100), 99) if estimated else int(received / total * 100)
            print(f"\r接收进度: {pct}% ({received // 1024}KB / {total // 1024}KB)", end="", flush=True)

    print()

    try:
        data = json.loads(b"".join(chunks))
    except json.JSONDecodeError:
        print("解析 JSON 失败，响应内容:", b"".join(chunks)[:500].decode(errors="replace"))
        exit(1)

    if data.get("data"):
        img = data["data"][0]
        if img.get("url"):
            print(f"图片URL: {img['url']}")
            img_r = client.get(img["url"], timeout=120)
            with open("ceshi3.png", "wb") as f:
                f.write(img_r.content)
            print("图片已保存：ceshi3.png")
        elif img.get("b64_json"):
            print("图像数据已接收，正在保存...")
            with open("ceshi3.png", "wb") as f:
                f.write(base64.b64decode(img["b64_json"]))
            print("图片已保存：ceshi3.png")
        else:
            print("响应中无图像数据")
            print(data)
    else:
        print("未获取到图像数据")
        print(data)