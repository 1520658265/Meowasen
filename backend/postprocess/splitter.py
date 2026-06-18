from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageDraw

from .background import estimate_background_rgb, estimate_edge_background_palette, foreground_mask_from_palette


@dataclass(frozen=True)
class SplitQuality:
    status: str = "ok"
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"status": self.status, "flags": list(self.flags)}


@dataclass(frozen=True)
class SplitFrame:
    frame_index: int
    grid_pos: tuple[int, int]
    image: Image.Image
    split_quality: SplitQuality


class SpriteSheetSplitter:
    def split(
        self,
        image: Image.Image,
        frame_grid: tuple[int, int],
        quality_profile: str = "sprite",
    ) -> list[SplitFrame]:
        rows, cols = frame_grid
        if rows <= 0 or cols <= 0:
            raise ValueError(f"Invalid frame_grid: {frame_grid}")
        if quality_profile not in {"sprite", "tile"}:
            raise ValueError(f"Invalid quality_profile: {quality_profile}")
        rgba = image.convert("RGBA")
        width, height = rgba.size
        cell_w = width // cols
        cell_h = height // rows
        components: list[tuple[int, int, int, int, int]] = []
        if quality_profile == "sprite":
            sheet_palette = estimate_edge_background_palette(rgba)
            sheet_mask = foreground_mask_from_palette(rgba, palette=sheet_palette)
            components = self._connected_components(sheet_mask, min_area=max(16, width * height // 20000))
        frames: list[SplitFrame] = []
        for row in range(rows):
            for col in range(cols):
                index = row * cols + col
                left = col * cell_w
                upper = row * cell_h
                right = width if col == cols - 1 else (col + 1) * cell_w
                lower = height if row == rows - 1 else (row + 1) * cell_h
                crop_box = (left, upper, right, lower)
                if quality_profile == "sprite":
                    bbox = self._component_bbox_for_cell(
                        components=components,
                        cell=(left, upper, right, lower),
                        image_size=(width, height),
                    )
                    crop_box = bbox or crop_box
                frame = rgba.crop(crop_box)
                quality = self._assess(frame, quality_profile=quality_profile)
                frames.append(
                    SplitFrame(
                        frame_index=index,
                        grid_pos=(row, col),
                        image=frame,
                        split_quality=quality,
                    )
                )
        return frames

    @staticmethod
    def _component_bbox_for_cell(
        components: list[tuple[int, int, int, int, int]],
        cell: tuple[int, int, int, int],
        image_size: tuple[int, int],
    ) -> tuple[int, int, int, int] | None:
        left, upper, right, lower = cell
        matches: list[tuple[int, int, int, int, int]] = []
        for component in components:
            comp_left, comp_top, comp_right, comp_bottom, area = component
            center_x = (comp_left + comp_right) / 2
            center_y = (comp_top + comp_bottom) / 2
            if left <= center_x < right and upper <= center_y < lower:
                matches.append(component)
        if not matches:
            return None

        cell_area = max(1, (right - left) * (lower - upper))
        matches = [component for component in matches if component[4] >= cell_area * 0.002]
        if not matches:
            return None

        min_left = min(component[0] for component in matches)
        min_top = min(component[1] for component in matches)
        max_right = max(component[2] for component in matches)
        max_bottom = max(component[3] for component in matches)
        padding = max(4, min(right - left, lower - upper) // 24)
        width, height = image_size
        return (
            max(0, min_left - padding),
            max(0, min_top - padding),
            min(width, max_right + padding),
            min(height, max_bottom + padding),
        )

    @staticmethod
    def _connected_components(
        mask: Image.Image,
        min_area: int,
    ) -> list[tuple[int, int, int, int, int]]:
        binary = mask.convert("L")
        width, height = binary.size
        data = binary.load()
        visited = bytearray(width * height)
        components: list[tuple[int, int, int, int, int]] = []
        for y in range(height):
            for x in range(width):
                index = y * width + x
                if visited[index] or data[x, y] <= 0:
                    continue
                visited[index] = 1
                stack = [(x, y)]
                min_x = max_x = x
                min_y = max_y = y
                area = 0
                while stack:
                    current_x, current_y = stack.pop()
                    area += 1
                    min_x = min(min_x, current_x)
                    min_y = min(min_y, current_y)
                    max_x = max(max_x, current_x)
                    max_y = max(max_y, current_y)
                    for next_x, next_y in (
                        (current_x - 1, current_y),
                        (current_x + 1, current_y),
                        (current_x, current_y - 1),
                        (current_x, current_y + 1),
                    ):
                        if next_x < 0 or next_y < 0 or next_x >= width or next_y >= height:
                            continue
                        next_index = next_y * width + next_x
                        if visited[next_index] or data[next_x, next_y] <= 0:
                            continue
                        visited[next_index] = 1
                        stack.append((next_x, next_y))
                if area >= min_area:
                    components.append((min_x, min_y, max_x + 1, max_y + 1, area))
        return components

    @staticmethod
    def _assess(frame: Image.Image, quality_profile: str = "sprite") -> SplitQuality:
        if quality_profile == "tile":
            return SpriteSheetSplitter._assess_tile(frame)

        bbox = SpriteSheetSplitter._content_bbox(frame)
        flags: list[str] = []
        if bbox is None:
            flags.append("empty_frame")
            return SplitQuality(status="failed", flags=flags)

        width, height = frame.size
        mask = SpriteSheetSplitter._content_mask(frame)
        non_zero = sum(1 for value in mask.getdata() if value > 0)
        ratio = non_zero / float(width * height)
        if ratio < 0.01:
            flags.append("low_alpha_area")

        left, top, right, bottom = bbox
        margin = max(1, min(width, height) // 64)
        if left <= margin or top <= margin or right >= width - margin or bottom >= height - margin:
            flags.append("bbox_touches_edge")
        if (right - left) > width * 0.9 or (bottom - top) > height * 0.9:
            flags.append("subject_overflow")
        if "bbox_touches_edge" in flags and "subject_overflow" in flags:
            flags.append("grid_merge_suspected")

        status = "ok"
        if flags:
            status = "warning"
        if "empty_frame" in flags or "grid_merge_suspected" in flags:
            status = "failed"
        return SplitQuality(status=status, flags=flags)

    @staticmethod
    def _assess_tile(frame: Image.Image) -> SplitQuality:
        rgba = frame.convert("RGBA")
        alpha = rgba.getchannel("A")
        width, height = rgba.size
        visible = sum(1 for value in alpha.getdata() if value > 0)
        visible_ratio = visible / float(width * height)
        flags: list[str] = []
        if visible == 0:
            return SplitQuality(status="failed", flags=["empty_frame"])
        if visible_ratio < 0.85:
            flags.append("low_alpha_area")
        return SplitQuality(status="warning" if flags else "ok", flags=flags)

    @staticmethod
    def _content_bbox(frame: Image.Image) -> tuple[int, int, int, int] | None:
        return SpriteSheetSplitter._content_mask(frame).getbbox()

    @staticmethod
    def _content_mask(frame: Image.Image) -> Image.Image:
        rgba = frame.convert("RGBA")
        alpha = rgba.getchannel("A")
        alpha_bbox = alpha.getbbox()
        if alpha_bbox and alpha_bbox != (0, 0, rgba.width, rgba.height):
            return alpha

        bg_rgb = estimate_background_rgb(rgba)
        bg = Image.new("RGBA", rgba.size, (*bg_rgb, 255))
        diff = ImageChops.difference(rgba, bg).convert("L")
        # Ignore mild compression/noise differences from generated images.
        mask = diff.point(lambda value: 255 if value > 18 else 0)
        # Generated sheets often contain grid separators. They should not make a
        # frame look like the subject touches every edge.
        border = max(2, min(rgba.width, rgba.height) // 48)
        draw = ImageDraw.Draw(mask)
        draw.rectangle([0, 0, rgba.width - 1, border], fill=0)
        draw.rectangle([0, rgba.height - border - 1, rgba.width - 1, rgba.height - 1], fill=0)
        draw.rectangle([0, 0, border, rgba.height - 1], fill=0)
        draw.rectangle([rgba.width - border - 1, 0, rgba.width - 1, rgba.height - 1], fill=0)
        return mask
