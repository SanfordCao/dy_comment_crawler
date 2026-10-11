"""统一绘制工作台图标，EXE 和托盘使用同一套图形。"""

from PIL import Image, ImageDraw


def create_app_icon(size: int = 256) -> Image.Image:
    """绘制蓝紫色评论数据图标，高分辨率缩放保证小尺寸清晰。"""
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 248, 248), radius=58, fill="#3949AB")
    draw.rounded_rectangle((42, 44, 214, 182), radius=30, fill="#FFFFFF")
    draw.polygon([(65, 163), (65, 213), (115, 175)], fill="#FFFFFF")
    # 柱形数据标记替代旧纯对话气泡，表达评论分析功能。
    draw.rounded_rectangle((70, 116, 94, 154), radius=6, fill="#6676DB")
    draw.rounded_rectangle((116, 91, 140, 154), radius=6, fill="#5164D0")
    draw.rounded_rectangle((162, 68, 186, 154), radius=6, fill="#3949AB")
    return image.resize((size, size), Image.Resampling.LANCZOS)
