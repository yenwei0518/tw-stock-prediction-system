import os
from PIL import Image, ImageDraw

def generate_red_bull_icon():
    # 採用 1024x1024 超高解析度繪製，再縮小達到極致抗鋸齒
    SIZE = 1024
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 1. 黑曜石圓角底圖 (Squircle) + 赤紅環境光暈
    margin = 40
    r = 220
    base_bg = Image.new("RGBA", (SIZE, SIZE), (13, 13, 18, 255))
    bg_draw = ImageDraw.Draw(base_bg)
    
    # 右上方散發強勢多頭赤紅光暈 (Crimson Ambient Glow)
    for i in range(260, 0, -5):
        alpha = int((1 - i / 260) * 55)
        bg_draw.ellipse(
            [SIZE*0.7 - i*2, SIZE*0.3 - i*2, SIZE*0.7 + i*2, SIZE*0.3 + i*2],
            fill=(239, 68, 68, alpha)
        )
    
    # 圓角遮罩裁切
    mask = Image.new("L", (SIZE, SIZE), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([margin, margin, SIZE - margin, SIZE - margin], radius=r, fill=255)
    img.paste(base_bg, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    # 2. 量化座標矩陣微格點 (Matrix Grid)
    grid_color = (255, 255, 255, 14)
    for x in range(160, 900, 140):
        draw.line([(x, 140), (x, 884)], fill=grid_color, width=2)
    for y in range(160, 900, 140):
        draw.line([(140, y), (884, y)], fill=grid_color, width=2)

    # 3. 多頭「連三紅」K 棒攻勢 (三根節節高升的長紅實體)
    candles = [
        # (中心x, 實體top, 實體bottom, 影線top, 影線bottom, 顏色)
        (330, 560, 680, 510, 730, (239, 68, 68, 170)),  # 首根起漲小紅棒
        (470, 450, 610, 400, 660, (239, 68, 68, 215)),  # 次根帶量推升紅棒
        (610, 320, 530, 270, 580, (244, 63, 94, 255)),  # 主升段長紅大棒
    ]

    for cx, top, bot, w_top, w_bot, col in candles:
        # 上下影線
        draw.line([(cx, w_top), (cx, w_bot)], fill=col, width=6)
        # 實體紅 K 棒
        draw.rounded_rectangle([cx - 30, top, cx + 30, bot], radius=6, fill=col)

    # 4. 象徵 Q 與噴出起漲的突破光束 (Breakout Beam)
    points = [
        (230, 720),
        (350, 715),
        (475, 545),
        (615, 395),
        (765, 245)
    ]
    
    # 霓虹漸層光暈 (赤紅 -> 琥珀金 -> 高光白)
    glow_layers = [
        (38, (239, 68, 68, 35)),
        (24, (245, 158, 11, 80)),
        (12, (251, 191, 36, 160)),
        (5,  (255, 255, 255, 255))
    ]

    for w, col in glow_layers:
        for i in range(len(points) - 1):
            draw.line([points[i], points[i+1]], fill=col, width=w)

    # 5. 右上角「AI 突破高點算力節點 (Amber-Gold Pulse Node)」
    peak_x, peak_y = 765, 245
    # 外層金光擴散環
    draw.ellipse([peak_x - 55, peak_y - 55, peak_x + 55, peak_y + 55], outline=(245, 158, 11, 90), width=6)
    # 熾紅能量環
    draw.ellipse([peak_x - 34, peak_y - 34, peak_x + 34, peak_y + 34], fill=(239, 68, 68, 210))
    # 核心超高光白點
    draw.ellipse([peak_x - 16, peak_y - 16, peak_x + 16, peak_y + 16], fill=(255, 255, 255, 255))

    # 6. 「Q」字底圈弧形刻印 (以金紅色呈現)
    draw.arc([210, 630, 360, 780], start=40, end=320, fill=(245, 158, 11, 180), width=10)

    # 7. 匯出標準 PWA 尺寸
    os.makedirs("static", exist_ok=True)
    
    icon_512 = img.resize((512, 512), Image.Resampling.LANCZOS)
    icon_512.save("static/icon-512.png", "PNG")
    
    icon_192 = img.resize((192, 192), Image.Resampling.LANCZOS)
    icon_192.save("static/icon-192.png", "PNG")
    
    print(" 成功生成台股長紅大漲版 PWA 圖示：static/icon-512.png & static/icon-192.png")

if __name__ == "__main__":
    generate_red_bull_icon()