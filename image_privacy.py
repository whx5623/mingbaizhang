# image_privacy.py
# 最小化上传：原图只在内存中裁剪 → Base64 → 发给视觉模型 → 立即清零，任何环节不写硬盘
"""截图记账的本地图片脱敏 + 最小化上传（纯函数模块）。

产品背景：用户把一张微信/支付宝账单截图交给 App，我们要让视觉模型只读「商户 /
金额 / 时间」，绝不能把整张截图（头像、昵称、订单号、底部广告）原样送出。
调用链只有三条铁律：

  1. 本地裁剪：按**高度**只保留中间 60% —— 去掉顶部约 20%（头像/昵称/订单号）
     与底部约 20%（广告/按钮），宽度保持不变；
  2. 内存流转：重新编码成 JPEG（quality=90）顺手抹掉 EXIF/GPS，再转 Base64 发送；
  3. 立即清零：识别结束后对内存里的 bytearray 覆零，全程不落盘。

设计边界：
  · 只依赖 Pillow + 标准库，不读库、不写库、不依赖 Streamlit —— 便于单测；
  · 任何异常都降级：裁剪失败就原样返回入参，宁可不脱敏也不能让记账页红屏；
  · burn() 只能处理可变的 bytearray；bytes 不可变，只能丢弃引用交给 GC。
"""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageOps

__all__ = [
    "crop_center_privacy",
    "encode_base64",
    "burn",
    "redact_meta_summary",
]

# 中央裁剪的合法区间：小于等于 0.2 几乎只剩一条缝，大于等于 1 等于没裁，都按非法处理。
KEEP_RATIO_MIN = 0.3
KEEP_RATIO_MAX = 0.95
DEFAULT_KEEP_RATIO = 0.6

# 白名单：只有这些「解析结果」允许离开内存，原始截图的其它信息一概不带。
SAFE_META_KEYS = ("merchant", "amount", "paid_at", "type", "sub_tags", "confidence")


def _normalize_ratio(keep_ratio) -> float:
    """把外部传入的 keep_ratio 收敛成可用的浮点数。

    降级路径：None / 字符串 / NaN 等无法转成浮点数的值 → 回落到 0.6；
    非法区间（<=0.2 或 >=1）→ 夹到 [0.3, 0.95]。
    """
    try:
        ratio = float(keep_ratio)
    except (TypeError, ValueError):
        return DEFAULT_KEEP_RATIO
    if ratio != ratio:  # NaN：自己不等于自己
        return DEFAULT_KEEP_RATIO
    if ratio <= 0.2 or ratio >= 1:
        ratio = min(max(ratio, KEEP_RATIO_MIN), KEEP_RATIO_MAX)
    return ratio


def crop_center_privacy(image_bytes, keep_ratio=0.6) -> bytes:
    """按高度做中央裁剪，返回重新编码后的 JPEG 字节。

    入参 image_bytes：PNG / JPEG / WebP 的内存字节；keep_ratio：保留的高度比例。
    流程：ImageOps.exif_transpose 摆正方向 → 转 RGB → 居中裁掉上下两部分 →
    quality=90 重新编码成 JPEG（新图不带 EXIF/GPS/设备型号）。

    降级路径：输入不是非空 bytes、不是合法图片、Pillow 抛任何异常时，只打印一行
    `[image_privacy] 裁剪失败：...` 并**原样返回入参**，绝不向上抛异常 ——
    记账页宁可用未脱敏的图继续识别，也不能红屏。
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        print("[image_privacy] 裁剪失败：输入不是非空 bytes，已原样返回")
        return image_bytes

    ratio = _normalize_ratio(keep_ratio)
    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            # 手机竖屏截图常带 EXIF Orientation，先摆正，否则裁的是旋转后的「左右」。
            img = ImageOps.exif_transpose(opened)
            # JPEG 不支持调色板 / 透明通道，统一转 RGB，避免 save 时报 "cannot write mode"。
            img = img.convert("RGB")
            width, height = img.size
            keep_height = max(1, min(height, int(round(height * ratio))))
            top = max(0, (height - keep_height) // 2)
            cropped = img.crop((0, top, width, top + keep_height))
            buffer = io.BytesIO()
            # 重新编码 = 只留像素：EXIF、GPS、ICC 都不会跟着新 JPEG 走。
            cropped.save(buffer, format="JPEG", quality=90)
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 —— 兜底永远不能把页面搞崩
        print(f"[image_privacy] 裁剪失败：{exc}")
        return image_bytes


def encode_base64(data: bytes) -> str:
    """标准 Base64 编码，供视觉模型的 data URL 使用。

    降级路径：空输入（None / b""）→ ""；传入非 bytes 等无法编码的对象 → 打印一行
    `[image_privacy] Base64 编码失败：...` 并返回 ""，让调用方按「无图」处理。
    """
    if not data:
        return ""
    try:
        return base64.b64encode(data).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        print(f"[image_privacy] Base64 编码失败：{exc}")
        return ""


def burn(data):
    """阅后即焚：把内存里的图片字节原地覆零。

    只有 bytearray 可变，才能做到「同一块内存被改写」；bytes / memoryview 不可变，
    降级路径是返回 None 由调用方丢弃引用，等 GC 回收，绝不写盘、不打印内容。
    """
    # 阅后即焚：只在内存里清零，绝不落盘
    if isinstance(data, bytearray):
        data[:] = b"\x00" * len(data)
    return None


def redact_meta_summary(info: dict) -> dict:
    """过滤解析结果：只保留白名单键，其余全部丢弃（不修改入参）。

    用于向用户/答辩证明「只有解析结果会离开内存」：即使视觉模型多返回了
    手机号、订单号之类的字段，也会在这里被拦下。

    降级路径：入参不是 dict（None / 列表 / 字符串）→ 返回 {}，不抛异常。
    """
    if not isinstance(info, dict):
        return {}
    return {key: info[key] for key in SAFE_META_KEYS if key in info}