#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Google Takeout 完美恢复与修复工具
解决问题：
1. 目录隔离与哈希防重名（不同手机同名照片不再混淆）
2. Live Photo (HEIC + MOV) 强绑定与摄制时间继承
3. 多模式 JSON 元数据精准匹配 (解决 Google 截断文件名问题)
4. 更新物理文件 mtime / atime，可选写入 JPEG EXIF
"""

import os
import re
import json
import shutil
import hashlib
import argparse
from pathlib import Path
from datetime import datetime

# 支持的图片与视频后缀
IMAGE_EXTS = {'.jpg', '.jpeg', '.heic', '.png', '.webp', '.gif', '.bmp', '.tiff'}
VIDEO_EXTS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv'}
ALL_MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# 尝试导入 piexif 写入 JPG EXIF (若未安装则自动回退至修改操作系统物理时间)
try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False


def get_path_hash(path_str: str) -> str:
    """根据源相对路径生成 4 位哈希，防止不同文件夹下同名文件（如不同手机的 IMG_0001）冲突"""
    return hashlib.md5(path_str.encode('utf-8')).hexdigest()[:4]


def find_json_for_file(file_path: Path) -> Path:
    """精准查找 Google Takeout 为文件生成的 JSON 边车文件"""
    parent = file_path.parent
    filename = file_path.name
    stem = file_path.stem
    ext = file_path.suffix

    # 常见命名模式：
    # 1. IMG_0001.JPG.json
    # 2. IMG_0001.json
    # 3. Google 截断文件名后的 JSON (例如 long_name.j.json 或 long_name.jp.json)
    candidates = [
        parent / f"{filename}.json",
        parent / f"{stem}.json",
    ]

    for cand in candidates:
        if cand.exists():
            return cand

    # 模糊搜索前缀截断匹配
    short_stem = stem[:40] if len(stem) > 40 else stem
    for json_file in parent.glob("*.json"):
        if json_file.name.startswith(short_stem):
            return json_file

    return None


def parse_json_metadata(json_path: Path):
    """解析 JSON 中的拍摄时间戳与 GPS 坐标"""
    if not json_path or not json_path.exists():
        return None, None, None

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 提取拍摄时间
        timestamp = None
        photo_taken = data.get('photoTakenTime', {})
        if 'timestamp' in photo_taken:
            timestamp = int(photo_taken['timestamp'])
            if timestamp <= 0:
                timestamp = None

        # 提取 GPS
        lat, lon = None, None
        geo = data.get('geoData', {}) or data.get('geoDataExif', {})
        if geo.get('latitude') != 0.0 or geo.get('longitude') != 0.0:
            lat = geo.get('latitude')
            lon = geo.get('longitude')

        return timestamp, lat, lon
    except Exception:
        return None, None, None


def apply_file_timestamp(file_path: Path, timestamp: int):
    """更新操作系统的文件修改时间 (mtime) 与访问时间 (atime)"""
    if timestamp and timestamp > 0:
        os.utime(file_path, (timestamp, timestamp))


def apply_exif_to_jpg(file_path: Path, timestamp: int, lat=None, lon=None):
    """针对 JPG 文件写入 EXIF 摄制时间 (若安装了 piexif)"""
    if not HAS_PIEXIF or not timestamp or file_path.suffix.lower() not in {'.jpg', '.jpeg'}:
        return

    try:
        dt_str = datetime.fromtimestamp(timestamp).strftime("%Y:%m:%d %H:%M:%S")
        exif_dict = piexif.load(str(file_path))
        exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = dt_str.encode('utf-8')
        exif_dict['Exif'][piexif.ExifIFD.DateTimeDigitized] = dt_str.encode('utf-8')
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, str(file_path))
    except Exception:
        pass


def process_takeout(input_dir: Path, output_dir: Path):
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()

    print(f"📂 正在扫描输入目录: {input_dir}")
    print(f"📦 导出目标目录: {output_dir}\n")

    # 统计数据
    total_files = 0
    live_photos_fixed = 0

    # 按照 [相对源目录路径] 遍历处理，实现隔离，防止同名覆盖
    for root, _, files in os.walk(input_dir):
        current_dir = Path(root)
        rel_dir_str = str(current_dir.relative_to(input_dir))
        dir_hash = get_path_hash(rel_dir_str)

        # 收集当前文件夹内的所有媒体文件
        media_files = [current_dir / f for f in files if Path(f).suffix.lower() in ALL_MEDIA_EXTS]
        if not media_files:
            continue

        # 步骤 1：第一轮扫描，收集本目录下所有文件的元数据
        file_meta = {}  # { stem: { 'timestamp': ..., 'lat': ..., 'lon': ... } }
        grouped_by_stem = {}  # { stem: [file_path, file_path...] }

        for file_path in media_files:
            stem = file_path.stem
            # 去除 (1) 重名后缀建立组映射
            clean_stem = re.sub(r'\(\d+\)$', '', stem).strip()

            if clean_stem not in grouped_by_stem:
                grouped_by_stem[clean_stem] = []
            grouped_by_stem[clean_stem].append(file_path)

            json_path = find_json_for_file(file_path)
            ts, lat, lon = parse_json_metadata(json_path)

            file_meta[file_path] = {
                'timestamp': ts,
                'lat': lat,
                'lon': lon,
                'json': json_path
            }

        # 步骤 2：对 Live Photo (HEIC/JPG + MOV/MP4) 进行时间继承与修复
        for clean_stem, group in grouped_by_stem.items():
            images = [f for f in group if f.suffix.lower() in IMAGE_EXTS]
            videos = [f for f in group if f.suffix.lower() in VIDEO_EXTS]

            # 如果本组既有图片又有短视频（典型的 Live Photo）
            if images and videos:
                # 获取图片的最佳摄制时间
                master_ts = None
                master_lat, master_lon = None, None

                for img in images:
                    if file_meta[img]['timestamp']:
                        master_ts = file_meta[img]['timestamp']
                        master_lat = file_meta[img]['lat']
                        master_lon = file_meta[img]['lon']
                        break

                # 让短视频强行继承主图片的摄制时间和 GPS
                if master_ts:
                    for vid in videos:
                        if not file_meta[vid]['timestamp']:
                            file_meta[vid]['timestamp'] = master_ts
                            file_meta[vid]['lat'] = master_lat
                            file_meta[vid]['lon'] = master_lon
                            live_photos_fixed += 1

        # 步骤 3：生成目标路径并复制/重命名归档
        # 为了保证 Live Photo 匹配，同一个 clean_stem 里的图片与视频必须享有完全一样的目标文件名和目标目录！
        for clean_stem, group in grouped_by_stem.items():
            # 确定当前组的目标年月目录
            group_ts = None
            for f in group:
                if file_meta[f]['timestamp']:
                    group_ts = file_meta[f]['timestamp']
                    break

            if group_ts:
                dt = datetime.fromtimestamp(group_ts)
                year_str = dt.strftime("%Y")
                year_month_str = dt.strftime("%Y-%m")
                target_folder = output_dir / year_str / year_month_str
            else:
                target_folder = output_dir / "Unknown_Date"

            target_folder.mkdir(parents=True, exist_ok=True)

            # 遍历组内所有文件，赋予唯一的强绑定名称
            for f in group:
                meta = file_meta[f]
                ts = meta['timestamp']
                ext = f.suffix.lower()

                # 构建防重名目标主文件名：
                # 格式：[年月日_时分秒]_[原文件名]_[目录Hash].[原后缀]
                if ts:
                    time_prefix = datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S")
                    new_stem = f"{time_prefix}_{clean_stem}_{dir_hash}"
                else:
                    new_stem = f"{clean_stem}_{dir_hash}"

                target_file = target_folder / f"{new_stem}{ext}"

                # 复制文件
                shutil.copy2(f, target_file)

                # 更新物理修改时间和 EXIF
                if ts:
                    apply_file_timestamp(target_file, ts)
                    apply_exif_to_jpg(target_file, ts, meta['lat'], meta['lon'])

                total_files += 1

    print("\n" + "=" * 50)
    print(f"🎉 处理完成！")
    print(f"✨ 成功整理并修复文件总数: {total_files}")
    print(f"🔗 拯救并配对的 Live Photo 视频组件: {live_photos_fixed}")
    print("=" * 50)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Google Takeout 完美恢复与修复工具")
    parser.add_argument("-i", "--input", required=True, help="Takeout 解压后的根目录（如 ./Takeout）")
    parser.add_argument("-o", "--output", required=True, help="整理后的输出路径（如 ./GooglePhotos_Restored）")
    args = parser.parse_args()

    process_takeout(Path(args.input), Path(args.output))
